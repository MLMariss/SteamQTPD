#!/usr/bin/env python3
"""
Steam QHPP — accumulation scraper (v3)
======================================
Builds up a database of Steam games over time and commits it to the repo, so a
static GitHub Pages frontend can read and filter it. Designed to run in a GitHub
Action on a schedule.

What changed in v3 (the upgrades):
  * Universe from GetAppList, not store search. The whole games catalog is
    enumerated cleanly via Steam's IStoreService/GetAppList (needs a free Web API
    key) — games-only, appid-ordered, with a per-app last_modified timestamp.
    Falls back to the keyless ISteamApps/GetAppList/v2 if no key is set.
  * Change-driven refresh. Instead of a blind time-based re-scrape, a stored game
    is refreshed only when GetAppList reports its last_modified is newer than when
    we last scraped it (or it's currently on sale). HLTB is NEVER re-fetched on a
    refresh — beat times don't change — only price/discount/rating/tags are.
  * Time-budgeted runs that commit as they go. Rather than fixed per-run counts,
    each run scrapes until RUN_MINUTES elapses (capped by the job's own clock, so the
    final save always lands), git-committing every COMMIT_SECONDS so a lost runner
    never loses more than that.
  * Released-only games.json, with a waiting room. Only games actually out (a
    concrete past release date) go into games.json — no empty coming-soon shells
    there. Unreleased games go to catalog["pending"] = {appid: release_ts|null},
    costing just one appdetails probe, and are promoted to the scrape queue the
    moment their release date passes. Nothing is ever permanently skipped for being
    unreleased. New work is ordered newest-appid-first, so just-released titles
    surface near the front.
  * Upcoming games are upcoming_refresh.py's: it owns upcoming.json, refreshed daily
    in one batched pass, and lists the pending games Steam says have released; this
    scraper scrapes those straight away, like any game whose date has passed.

Per game: title, store URL, rating (% positive) + reviews, price before/after
discount (USD), discount % + end time, release date, tags, HLTB
(main/main+extras/completionist), and QHPP before & after discount.

QHPP = (avg HLTB hours × rating%) ÷ price. Higher = more quality-adjusted hours
per dollar. Null for free games and games with no HLTB data.
"""

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

# HLTB lookups moved to hltb_refresh.py (they were the per-game bottleneck).

# --------------------------------------------------------------------------- #
# CONFIG — edit these (or set the env vars)
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # display data + scraped state (committed)
CATALOG_FILE = HERE / "catalog.json"      # tiny state: last_sync, skip list, priority (committed)
SEEDS_FILE = HERE / "seeds.txt"           # OPTIONAL games to scrape FIRST (human-edited only)
SEEDS_LOG_FILE = HERE / "seeds_log.txt"   # append-only audit of seed reconciliations (scraper-owned)
PICS_FILE = HERE / "pics.json"            # READ-ONLY here (owned by pics_merge.py) — review-drift signal
UPCOMING_FILE = HERE / "upcoming.json"    # READ-ONLY (owned by upcoming_refresh.py) — its `released` list
REVIEW_RESTORE_FILE = HERE / "reviews_restore.json"  # READ-ONLY one-off: pre-wipe review counts (§16)

# Free Steam Web API key from https://steamcommunity.com/dev/apikey (GitHub secret
# STEAM_API_KEY). Without it, falls back to the keyless full list (all app types,
# no last_modified, so refresh reverts to the REFRESH_DAYS timer).
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))   # scrape budget per run
CHECKPOINT_SECONDS = 600        # mid-run pickups (seeds.txt edits, newly-due young games) this often
# Save + git-commit progress this often. Was every CHECKPOINT_SECONDS (10 min): each commit is a
# new 63 MB version of games.json in history, ~144 a day, and that history (9.6 GB by Oct 2026)
# is what made every full-depth checkout take ~30 min. A run now ends cleanly (job-clock cap
# below), so the commits only guard against a lost runner; 30 min of work is the most that costs.
COMMIT_SECONDS = int(os.environ.get("COMMIT_SECONDS", "1800"))
TIME_BUFFER = 120               # stop scraping this long before the budget, to commit
# Job-clock cap (JOB_STARTED_AT / JOB_TIMEOUT_MINUTES, set by scrape.yml). RUN_MINUTES used to
# equal the job's 330-min timeout and was counted from the script's own start, AFTER a ~30-min
# checkout, so GitHub cancelled every run before its final save: last_sync froze on 2026-10-02
# and the coverage/presets jobs, which only follow a successful scrape, stopped running. The
# budget is now also capped at what is left of the job, minus this reserve for the final save,
# commit and push (rebase retries included).
FINAL_SAVE_RESERVE = 600
NEW_ORDER = "newest"            # "newest" (high appid first) or "oldest"
REFRESH_DAYS = 7                # fallback refresh age when no API key (no last_modified)
SEED_RESOLVE_TTL = 24 * 3600    # live term/URL seeds: re-resolve at most once per this interval
COMING_SOON_GRACE_DAYS = 14     # dated coming_soon games stay "ripe" this long past the date
MAX_RECHECK = int(os.environ.get("MAX_RECHECK", "4"))  # appdetails success:false retries
                                # before a game is treated as a permanent skip. success:false
                                # lumps region-locked (cc=us) + transient blips with dead apps,
                                # so we retry across this many runs before giving up (§3.1).
                                # A STORED game is never skipped: at this many it is marked
                                # `unknown` and still asked every run (strike_no_answer).

# --- Review freshness: age-tiered refresh + PICS drift trigger (Jul 2026) ----
# WHY: Steam's last_modified (GetAppList) tracks store/depot changes, NOT review
# counts. With an API key set (always, in Actions) `has_lm` is true, so the
# REFRESH_DAYS fallback timer never fires and a game's ONLY refresh trigger is a
# store-page change. A game scraped on release day therefore froze at its day-one
# score forever — the single least representative moment. Assassin's Creed Black
# Flag Resynced sat at 49% / 2,019 reviews for 13 days while the real figure moved
# to 79% / 19k. Systemic, not a one-off: games released in the last 30 days had a
# median scrape age of 13 days.
#
# Fix 1 — REVIEW_TIERS: a per-game cooldown that widens with age since release, so
# the score is re-checked hardest exactly when it moves fastest. (max_age_days,
# cooldown_days); a game older than the last tier keeps the old last_modified-only
# behaviour. Steady state ~7.1k refreshes/day = ~14.1k storefront calls = ~3.5h/day
# of run time at STOREFRONT_MIN_INTERVAL — about 23% of this scraper's proven peak
# (30.8k games in one day) and ~16% of its ~22h/day window. The FIRST pass is a
# one-time catch-up, which shares the run with the forced drain
# (FORCE_RESERVE_FRAC) and settles within a day or two.
REVIEW_TIERS = [(3, 0.25), (10, 0.5), (30, 1), (60, 2), (90, 3.5), (180, 7), (365, 15)]
REVIEW_TIER_REFRESH = os.environ.get("REVIEW_TIER_REFRESH", "1") != "0"   # kill-switch

# Tiers at or under this many days since release are re-checked MID-RUN, not just
# when the run starts. WHY: select_work() builds the queue ONCE, at the top of a
# 5.5h run on a 6h cron grid, so a game that comes due 20 minutes in waits for the
# next run. That puts a hard ~6h floor under every cooldown and would make the 6h
# and 12h tiers partly cosmetic (a nominal 6h tier delivering 6-12h intervals).
# requeue_due_young() re-checks this population at every checkpoint (~10 min),
# which is what actually buys the near-real-time end of the ladder. Only the young
# tiers qualify: they're a small set (~2.4k games) so the re-scan is free, and
# they're the only ones whose cooldown is shorter than a run.
REVIEW_LIVE_MAX_AGE_DAYS = float(os.environ.get("REVIEW_LIVE_MAX_AGE_DAYS", "30"))

# Fix 2 — PICS drift: pics.json already carries `rev: [score_1_9, pct]` harvested
# daily over the WHOLE catalog via the CM protocol, entirely off the storefront
# budget. When PICS's percentage disagrees with our stored rating_pct by more than
# this many points, our number is provably stale — queue a re-scrape regardless of
# age. Free signal, small volume (~1.7k games on the first pass at 3pt, a few
# hundred/day after), and it reaches old games no tier ever would. Covers the ~64%
# of the catalog that has `rev`; PICS carries no review COUNT, so this complements
# the tiers rather than replacing them. Set to 0 to disable.
PICS_REV_DELTA = int(os.environ.get("PICS_REV_DELTA", "3"))

# TOP_TAGS / SteamSpy tags moved to tags_refresh.py (it was the slowest per-game call).
COUNTRY = "us"                  # cc=us => USD prices

STEAM_DELAY = 1.5               # DEPRECATED: replaced by STOREFRONT_MIN_INTERVAL /
                                # storefront_pace() below. Kept only as documentation of
                                # the old ~200/5min-derived spacing; no longer referenced.
WEBAPI_DELAY = 1.0             # between GetAppList pages
NEWS_DELAY = 0.3               # between News API calls (api.steampowered.com; huge budget)
MAX_RETRIES = 4

# --- Shared storefront rate limiter -----------------------------------------
# WHY: build_record makes TWO storefront calls per game — appdetails (paced by a
# serial time.sleep(STEAM_DELAY)) AND appreviews (fired UNPACED inside the thread
# pool). The reviews call therefore ignored the budget entirely, so bursts of
# 2 calls per 1.5s tripped the ~200/5min soft-limit and each 403 cost a 5-minute
# time.sleep(300) stall — the real reason the null-update drain crawled.
#
# This limiter paces EVERY storefront call (appdetails + reviews) through one
# shared gate at STOREFRONT_MIN_INTERVAL, so calls go out smoothly instead of
# bursting into 403 walls. With 2 calls/game at 0.9s spacing => ~1.8s/game
# (~2000 games/hr) with far fewer cooldown stalls — a real net speedup vs the
# old ~3s-effective/game-plus-403-penalties path.
#
# NOTE (revert plan): STOREFRONT_MIN_INTERVAL=0.9 is tuned aggressively to drain
# the ~48k null-update backlog fast. Once the backlog is closed, raise it back
# toward ~1.4-1.5 (steady state) — see ARCHITECTURE.md §16 revert note.
STOREFRONT_MIN_INTERVAL = float(os.environ.get("STOREFRONT_MIN_INTERVAL", "0.9"))
_sf_lock = threading.Lock()
_sf_next_ok = 0.0

def storefront_pace():
    """Block until the shared storefront budget allows the next call. Thread-safe:
    appdetails (main thread) and appreviews (pool thread) both call this, so the
    two per-game storefront calls share one rate budget instead of double-paying
    or bursting unpaced."""
    global _sf_next_ok
    with _sf_lock:
        now = time.monotonic()
        wait = _sf_next_ok - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _sf_next_ok = now + STOREFRONT_MIN_INTERVAL

# News items counted as "updates" (vs sale posts / announcements). The store page's
# ?updates=true filter is driven by Steam update events; the public news feed doesn't
# always tag them cleanly, so this is a good-enough heuristic: the patchnotes tag is
# the strong signal, keywords are the fallback, and obvious sale posts are excluded.
#
# NOTE (quick-fix layer): this is the cheap News-API fallback for last_update_ts only.
# True major/minor magnitude comes from updates_refresh.py -> updates.json, which reads
# the store events endpoint's structured event_type (12=minor, 13=major). This heuristic
# stays as the broad, budget-free coverage floor for games that job hasn't reached yet.
_UPDATE_TAGS = {"patchnotes", "mod_reviewed"}
# Matched against title + feedlabel + the (now non-empty) body blurb, word-ish substrings.
_UPDATE_WORDS = ("update", "patch", "hotfix", "changelog", "change log", "release notes",
                 "patch notes", "patchnotes", "version", "build ", "rev ", "bug fix",
                 "bugfix", "fixes", "fixed", "balance", "rebalance", "content update",
                 "season", "new content", "improvements", "optimization", "optimisation")
# Only strong sale/marketing signals — kept tight so a patch post that merely *mentions*
# a sale in its body isn't wrongly excluded (body is now scanned, so this must not over-fire).
_NOT_UPDATE = ("% off", "weekend deal", "midweek madness", "daily deal", "wishlist now",
               "add to your wishlist", "pre-order", "preorder", "coming soon",
               "release date trailer", "announcement trailer", "soundtrack",
               "now available on", "out now on", "cross promotion")
# Feedlabels that are pure marketing channels — never updates regardless of wording.
_NOT_UPDATE_FEEDS = {"steam_updates_sale", "steam_community_announcements_sale"}

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
SEARCH_URL = "https://store.steampowered.com/search/results/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (steam-qhpp scraper; github pages dataset builder)",
    "Accept-Language": "en-US,en;q=0.9",
}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}

_thread_local = threading.local()

def _session():
    """Thread-local requests.Session — requests Sessions aren't thread-safe to share, and
    build_record now fires reviews/tags/news concurrently, so each thread gets its own."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.cookies.update(COOKIES)
        _thread_local.session = s
    return s


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# HTTP with retry/backoff
# --------------------------------------------------------------------------- #
# How many unusable replies get() / the fetchers print in full per run. A blocked endpoint
# fails on every call, so the first few say what Steam sent and the rest would be noise.
BAD_REPLY_LOG_LIMIT = 5
_bad_replies_logged = 0


def log_bad_reply(what):
    """Print one unusable Steam reply, up to BAD_REPLY_LOG_LIMIT per run. Silent failures
    are how the 22-24 Oct 2026 review wipe ran for over a day and a half unexplained (§16)."""
    global _bad_replies_logged
    if _bad_replies_logged < BAD_REPLY_LOG_LIMIT:
        _bad_replies_logged += 1
        log(f"  unusable reply: {what}")


def get(url, *, params=None, timeout=30, expect_json=False):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 5 min"); time.sleep(300); continue
            r.raise_for_status()
            if expect_json:
                try:
                    return r.json()
                except ValueError:
                    log_bad_reply(f"{urlparse(url).path} {r.status_code} "
                                  f"{r.headers.get('Content-Type')!r} {r.text[:200]!r}")
                    return None
            return r.text
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- #
# The games universe (GetAppList)
# --------------------------------------------------------------------------- #
def fetch_app_universe():
    """Return ({appid: last_modified}, has_last_modified). Keyed IStoreService gives
    a clean games-only list with timestamps; keyless v2 is the all-types fallback."""
    if STEAM_API_KEY:
        out, last, pages = {}, 0, 0
        while True:
            params = {"key": STEAM_API_KEY, "include_games": "true",
                      "max_results": 50000, "last_appid": last}
            data = get("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                       params=params, expect_json=True)
            time.sleep(WEBAPI_DELAY)
            resp = (data or {}).get("response", {})
            apps = resp.get("apps", []) or []
            for a in apps:
                try:
                    out[int(a["appid"])] = int(a.get("last_modified", 0))
                except (KeyError, TypeError, ValueError):
                    pass
            pages += 1
            nxt = resp.get("last_appid", last)
            if resp.get("have_more_results") and apps and nxt != last:
                last = nxt
            else:
                break
            if pages > 30:                       # safety stop (~1.5M apps)
                break
        log(f"GetAppList (keyed): {len(out)} games across {pages} page(s)")
        return out, True

    data = get("https://api.steampowered.com/ISteamApps/GetAppList/v2/", expect_json=True)
    apps = ((data or {}).get("applist", {}) or {}).get("apps", []) or []
    out = {}
    for a in apps:
        if a.get("name"):
            try:
                out[int(a["appid"])] = 0
            except (KeyError, TypeError, ValueError):
                pass
    log(f"GetAppList (keyless v2): {len(out)} apps — all types, no change timestamps")
    return out, False


# --------------------------------------------------------------------------- #
# Priority seeds (optional, from seeds.txt)
# --------------------------------------------------------------------------- #
def search_params(seed: str) -> dict:
    p = {"infinite": 1, "count": 100, "cc": COUNTRY, "l": "english"}
    if seed.startswith("http"):
        for k, v in parse_qs(urlparse(seed).query).items():
            if k not in ("start", "count", "infinite"):
                p[k] = v[0]
    else:
        p["term"] = seed
    return p


def fetch_search_page(params, start):
    p = dict(params); p["start"] = start
    storefront_pace()               # shared storefront budget (was serial STEAM_DELAY)
    data = get(SEARCH_URL, params=p, expect_json=True)
    if not data:
        return [], 0
    html = data.get("results_html", "")
    total = int(data.get("total_count", 0) or 0)
    ids = []
    for chunk in re.findall(r'data-ds-appid="([\d,]+)"', html):
        for piece in chunk.split(","):
            if piece.isdigit():
                ids.append(int(piece))
    return ids, total


def parse_seed_line(raw):
    """Parse one seeds.txt line. Returns (key, kind, force, payload) or None.

    Three seed kinds are accepted, each optionally prefixed with '!' to FORCE a
    re-scrape of already-stored matches (one-shot per edit; see reconcile_seeds):
      * bare numeric APP ID        -> kind 'id',   payload = int(appid)
      * full Steam store SEARCH URL -> kind 'url',  payload = the URL
      * plain search TERMS          -> kind 'term', payload = the term string

    `key` is a stable identity used in the ledger. It deliberately EXCLUDES the '!'
    so toggling force is never seen as add+remove (it would re-resolve/re-log for
    nothing). For URLs the query is canonicalized (sorted params, minus volatile
    start/count/infinite) so cosmetic URL edits don't fork the identity.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    force = line.startswith("!")
    if force:
        line = line[1:].strip()
        if not line:
            return None
    if line.isdigit():
        return (f"id:{int(line)}", "id", force, int(line))
    if line.startswith("http"):
        u = urlparse(line)
        q = parse_qs(u.query)
        for k in ("start", "count", "infinite"):
            q.pop(k, None)
        canon = "&".join(f"{k}={','.join(q[k])}" for k in sorted(q))
        return (f"url:{u.path}?{canon}", "url", force, line)
    term = " ".join(line.lower().split())
    return (f"term:{term}", "term", force, line)


def resolve_seed(kind, payload):
    """Resolve a seed to a list of appids. Bare IDs cost ZERO network; term/URL seeds
    page the storefront search (same path the old resolver used)."""
    if kind == "id":
        return [int(payload)]
    params = search_params(payload); start = 0
    ids = []
    for _ in range(10):
        page, total = fetch_search_page(params, start)
        if not page:
            break
        ids.extend(page); start += 100
        if total and start >= total:
            break
    return list(dict.fromkeys(ids))


def seed_log(lines):
    """Append human-readable reconciliation events to seeds_log.txt (scraper-owned,
    append-only; committed alongside catalog.json). No-op when there's nothing to say."""
    if not lines:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = "".join(f"{stamp}  {ln}\n" for ln in lines)
    with open(SEEDS_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(block)
    for ln in lines:
        log(f"  seed: {ln}")


def fetch_origin_seeds():
    """Return origin/main's current seeds.txt content, so a mid-run web edit is picked
    up without waiting for the next cron. Read-only: fetches the remote ref and reads
    the blob via `git show` — never touches the working tree or index, so the scraper
    still only ever *commits* games.json/catalog.json/seeds_log.txt (seeds.txt stays
    human-only). Returns None outside Actions or on any git error (caller then skips)."""
    if not IN_ACTIONS:
        return None
    try:
        subprocess.run(["git", "fetch", "origin", "main"], check=False,
                       capture_output=True)
        r = subprocess.run(["git", "show", "origin/main:seeds.txt"],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    except Exception as e:
        log(f"  seed origin-fetch failed: {e}")
        return None


def reconcile_seeds(catalog, processed, seeds_text=None):
    """Declarative seed reconciliation (replaces the old one-shot ensure_priority).

    Every run we diff the seed list against catalog['seeds_ledger'] FROM SCRATCH, so the
    state self-heals against mid-run edits and concurrent pushes:
      * ADDED line     -> resolve, store a ledger entry, queue its ids.
      * REMOVED line   -> FORGET: drop the ledger entry only. games.json is never
                          touched; the games just stop being prioritized.
      * live term/URL  -> re-resolved when older than SEED_RESOLVE_TTL; newly matched
                          ids are merged in (so seeds keep catching new releases).
      * '!' force      -> one-shot per edit: queue this seed's already-STORED matches
                          for a re-scrape, then latch (forced_applied) so it does NOT
                          loop every run; dropping the '!' resets the latch so re-adding
                          it later forces again.

    seeds_text: if given, reconcile against that text instead of reading SEEDS_FILE.
    Used for the mid-run re-check against origin/main's seeds.txt (see fetch_origin_seeds)
    so a web edit lands in the CURRENT run; at run start it's None (reads the local file).

    catalog['priority'] is then REBUILT as the union of all active seeds' ids (minus
    what's already stored/skipped), so it self-shrinks and never accumulates stale ids.
    The release gate is unaffected: priority only orders new_ids; build_record still
    sends any unreleased match to the pending waiting room, and ripe-promotion scrapes
    it once its date passes. The ledger only ever holds entries for CURRENTLY-ACTIVE
    seeds, which keeps catalog.json clean by construction.
    """
    now = int(time.time())
    ledger = dict(catalog.get("seeds_ledger") or {})
    stored = {int(a) for a in processed}
    events = []

    # Parse the current seed list into {key: (kind, force, payload)}; last dup line wins.
    if seeds_text is None:
        seeds_text = SEEDS_FILE.read_text(encoding="utf-8") if SEEDS_FILE.exists() else ""
    active = {}
    for raw in seeds_text.splitlines():
        parsed = parse_seed_line(raw)
        if parsed:
            key, kind, force, payload = parsed
            active[key] = (kind, force, payload)

    force_refresh = set(int(a) for a in (catalog.get("force_refresh") or []))

    # 1. REMOVED seeds -> forget (drop entry; never purge games.json).
    for key in [k for k in ledger if k not in active]:
        ent = ledger.pop(key)
        events.append(f"removed {key} (forget; {len(ent.get('ids', []))} ids "
                      f"deprioritized, games kept)")

    # 2. ADDED / refreshed / force, for every currently-active seed.
    for key, (kind, force, payload) in active.items():
        ent = ledger.get(key)
        if ent is None:                                   # ADDED
            ids = resolve_seed(kind, payload)
            ent = {"kind": kind, "resolved_ts": now, "ids": ids,
                   "forced_applied": False}
            ledger[key] = ent
            events.append(f"added {key} (+{len(ids)} ids queued)")
        elif kind in ("term", "url") and (now - ent.get("resolved_ts", 0)) >= SEED_RESOLVE_TTL:
            fresh = resolve_seed(kind, payload)            # live re-resolution
            old = set(ent.get("ids", []))
            merged = list(dict.fromkeys(list(ent.get("ids", [])) + fresh))
            added_n = len(set(merged) - old)
            ent["ids"] = merged; ent["resolved_ts"] = now
            if added_n:
                events.append(f"re-resolved {key} (+{added_n} new match(es))")

        # One-shot force latch.
        if force and not ent.get("forced_applied"):
            requeue = [a for a in ent.get("ids", []) if a in stored]
            force_refresh.update(requeue)
            ent["forced_applied"] = True
            events.append(f"force {key} ({len(requeue)} stored match(es) re-queued; "
                          f"remove '!' when done)")
        elif not force and ent.get("forced_applied"):
            ent["forced_applied"] = False                  # reset latch; '!' can re-trigger

    # 3. Rebuild priority declaratively from the surviving (active) ledger entries.
    skipped = set(catalog.get("skipped") or [])
    pri = []
    for ent in ledger.values():
        for a in ent.get("ids", []):
            if a not in stored and a not in skipped:
                pri.append(a)
    catalog["priority"] = list(dict.fromkeys(pri))
    catalog["seeds_ledger"] = ledger
    catalog["force_refresh"] = sorted(force_refresh)

    seed_log(events)
    log(f"Seeds: {len(active)} active line(s), {len(catalog['priority'])} priority appid(s), "
        f"{len(force_refresh)} forced re-scrape(s) pending")


# --------------------------------------------------------------------------- #
# Per-game data sources
# --------------------------------------------------------------------------- #
# NOTE: sale end-dates are no longer fetched here. They're owned by price_and_sale.py
# (prices.json), which uses the batched IStoreBrowseService/GetItems endpoint — far
# cheaper and more reliable than the old featuredcategories map + per-page HTML scrape
# that used to live here, and decoupled from this slow, rate-limited main scrape.


# A fetch that got no usable answer from Steam. Distinct from every real answer — a
# (None, 0) "no reviews yet", a None "no update posts" — so build_record keeps what it
# already holds instead of storing the failure as fact. Returning (None, 0) for a failed
# review call is what wiped 15k+ games' reviews from 22 Oct 2026 23:05 UTC (§16).
FAILED = object()
REVIEW_STATS = Counter()            # this run's review fetches: answered / failed / zero_kept


# Valve switched store.steampowered.com/appreviews off on 22 Oct 2026 (it has redirected
# to the store front page since, for every caller). GetAppReviews on the Web API host
# replaces it: no key, parameters in input_json, the reply wrapped in "response" and no
# `success` field. The OLD SPELLINGS ARE SILENTLY IGNORED — `language` and purchase_type
# "all" leave Steam's defaults, English and Steam purchases only, and a free-to-play game
# collapses (War Thunder 834 instead of 792,828). `languages: ["all"]` and purchase_type 1
# are what count every review, as appreviews' language=all&purchase_type=all did
# (review_probe.py Q0, 25 Oct 2026). num_per_page 0 is not honoured, so ask for 1.
REVIEWS_API = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
REVIEWS_ALL = {"languages": ["all"], "purchase_type": 1}


def rating_from_reviews(appid):
    """(rating_pct, review_count) from Steam's own summary — (None, 0) for a game Steam
    says has no reviews — or FAILED when the reply can't be read as an answer."""
    # Still paced on the storefront budget: GetAppReviews is on the Web API host, whose
    # keyless limit Valve doesn't document, and this is the pace already proven safe.
    storefront_pace()
    data = get(REVIEWS_API, params={"input_json": json.dumps(
                   dict(REVIEWS_ALL, appid=appid, num_per_page=1))}, expect_json=True)
    if not isinstance(data, dict):
        return FAILED               # no reply, or not JSON (get() logged the body)
    resp = data.get("response")
    s = resp.get("query_summary") if isinstance(resp, dict) else None
    if not isinstance(s, dict) or "total_positive" not in s or "total_negative" not in s:
        log_bad_reply(f"GetAppReviews {appid}: {json.dumps(data)[:200]}")
        return FAILED
    pos, neg = int(s["total_positive"]), int(s["total_negative"])
    if pos + neg == 0:
        return None, 0
    return round(pos / (pos + neg) * 100), pos + neg


def keep_reviews(fetched, prev):
    """(rating_pct, review_count) to store, given this run's fetch and the stored record.

    The fetch wins unless it can't be trusted: FAILED keeps the stored pair, and so does
    a zero after a positive count — reviews don't vanish, so "0" for a game we hold
    reviews for is a bad answer, whatever shape Steam gave it. A game with nothing
    stored takes the fetch, or (None, 0) if it failed, and is re-fetched on its next
    refresh."""
    prev = prev or {}
    held = (prev.get("rating_pct"), int(prev.get("review_count") or 0))
    if fetched is FAILED:
        return held
    if fetched[1] == 0 and held[1] > 0:
        return held
    return fetched


def _is_update_item(item):
    """True if a news item looks like a game update/patch rather than a sale post.

    Quick-fix improvements over the old title-only check:
      * the `patchnotes` tag is still the strong positive signal (short-circuits),
      * the body blurb (`contents`) is now scanned too — previously discarded because
        the fetch used maxlength=1, so any patch whose *title* dodged the keyword list
        was invisible (this is what made e.g. Cyberpunk read as "no updates"),
      * exclusions are tightened to strong sale/marketing phrases + marketing feeds only,
        so a patch post that merely mentions a sale in its body isn't dropped.
    """
    tags = item.get("tags") or []
    if any(t in _UPDATE_TAGS for t in tags):
        return True
    feed = str(item.get("feedname", "")).lower()
    if feed in _NOT_UPDATE_FEEDS:
        return False
    text = " ".join(str(item.get(k, "")) for k in ("title", "feedlabel", "contents")).lower()
    if any(bad in text for bad in _NOT_UPDATE):
        return False
    return any(w in text for w in _UPDATE_WORDS)


def last_update_from_news(appid):
    """Most recent *update* post timestamp via the News API. Lives on
    api.steampowered.com (huge budget, separate from the storefront limit), so this is
    cheap to refresh broadly. Returns a unix ts, None (no update posts found), or
    FAILED (no usable reply — the caller keeps the stored value).

    maxlength=300 (was 1): the body blurb is now needed so _is_update_item() can match
    on content, not just the title — the maxlength=1 truncation was the main reason real
    patches were missed. 300 chars is enough for the keyword scan while keeping payloads
    small; count=30 (was 20) gives a little more history headroom for chatty feeds."""
    params = {"appid": appid, "count": 30, "maxlength": 300, "format": "json"}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
               params=params, expect_json=True)
    time.sleep(NEWS_DELAY)
    if not isinstance(data, dict) or not isinstance(data.get("appnews"), dict):
        return FAILED
    items = data["appnews"].get("newsitems") or []
    stamps = [it.get("date") for it in items if _is_update_item(it) and it.get("date")]
    return max(stamps) if stamps else None


_DAY_FORMATS = ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y")
_RELEASE_FORMATS = _DAY_FORMATS + ("%b %Y", "%Y")


def parse_release(d):
    """Return (date_string, release_ts, coming_soon).

    For a released game any recognised format gives a ts. For a coming_soon game only a
    day-precision date does ("6 Oct, 2026"): that's the date it ripens on in the pending
    waiting room. A vague "Q4 2026" / "October 2026" / "2026" stays None -- "2026" would
    parse to Jan 1 and ripen on every run. Discarding coming_soon dates entirely (the old
    behaviour) filed ~58k pending games as undated, so the ripe path never fired and a
    dated launch like Gears of War: E-Day (3010850) sat in pending after release."""
    rd = d.get("release_date", {}) or {}
    s = (rd.get("date") or "").strip()
    coming_soon = bool(rd.get("coming_soon"))
    ts = None
    if s:
        for fmt in (_DAY_FORMATS if coming_soon else _RELEASE_FORMATS):
            try:
                ts = int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
                break
            except ValueError:
                continue
    return (s or None), ts, coming_soon


# --------------------------------------------------------------------------- #
# Free-to-keep promos: reconciling a self-contradictory appdetails response
# --------------------------------------------------------------------------- #
# While a paid game is "free to keep for the next 48 hours", appdetails flips `is_free`
# to true and `discount_percent` to 100 while `price_overview` still quotes the FULL
# price on BOTH `initial` and `final`. Stored verbatim that snapshot outlives the promo,
# and nothing else can undo it: `price_and_sale.py` skipped every is_free game, so the
# fast layer that corrects all other prices never reached it, and only a full-catalog
# re-scrape (weeks away for an old game) would. Moonlighter (606150) and Breathedge
# (738520) sat at "-100% off" on a full-price game — the top two rows of the discount
# sort — for weeks that way, with no QTPD at all because a "free" game has no price to
# divide by.
#
# The prices are the trustworthy half of the response, so they decide:
#   * a final price at or above the initial price means NO cut is in effect -> discount 0,
#   * a full, undiscounted price means the game is NOT free.
# A free app that also sells a genuinely discounted package at the app level (Capcom
# Arcade Stadium 1515950: free, $59.99 -> $14.99) keeps both facts — that pairing is real,
# only the full-price-yet-free-and-100%-off combination is impossible.
def reconcile_price_flags(is_free, price_initial, price_final, discount_pct):
    """Return (is_free, discount_pct) with promo-snapshot contradictions resolved."""
    if price_final is None or price_final <= 0:
        return is_free, discount_pct              # no price to argue with (real free game)
    if discount_pct > 0 and price_initial is not None and price_final >= price_initial:
        discount_pct = 0                          # "-100%" while charging the full price
    if is_free and discount_pct == 0:
        is_free = False                           # a full price is not free
    return is_free, discount_pct


def promo_residue(rec):
    """True if a STORED record still carries a free-promo snapshot — its own prices say
    "paid, not discounted" while is_free/discount_pct say free and/or on sale."""
    free = bool(rec.get("is_free"))
    disc = int(rec.get("discount_pct") or 0)
    if not free and disc == 0:
        return False                              # nothing to contradict
    return (free, disc) != reconcile_price_flags(
        free, rec.get("price_initial"), rec.get("price_final"), disc)


def strike_no_answer(aid, rec, recheck, skipped, now):
    """appdetails said success:false for `aid`; `rec` is its stored record, or None.
    An app we never stored keeps the bounded retry: MAX_RECHECK strikes, then `skipped`.
    A STORED game is never given up on that way. Moving one to `skipped` never removed its
    record — it stayed in games.json, frozen, and its strikes started over (Valkyrie Girl sat
    in the Upcoming view four months after Steam stopped answering for it) — and a run of
    blips must not lose a real game either. So its strikes keep counting, select_work asks
    again every run, and from MAX_RECHECK on the record carries `unknown`: the time of the
    latest unanswered check. `scraped_at` is left alone, so it still says when Steam last
    answered. The site shows such a game only to a typed search, badged. The next answer
    replaces the record, mark and all."""
    strikes = recheck.get(aid, 0) + 1
    if rec is None:
        if strikes >= MAX_RECHECK:
            skipped.add(aid)
            recheck.pop(aid, None)
        else:
            recheck[aid] = strikes
        return
    recheck[aid] = strikes
    if strikes >= MAX_RECHECK or rec.get("unknown"):
        rec["unknown"] = int(now)


def parse_price(appid, d):
    """(is_free, price_initial, price_final, discount_pct) from an appdetails data block."""
    is_free = bool(d.get("is_free"))
    price_initial = price_final = None
    discount_pct = 0
    po = d.get("price_overview")
    if po:
        if po.get("currency") != "USD":
            log(f"  appid {appid}: currency {po.get('currency')} != USD")
        price_initial = round(po.get("initial", 0) / 100, 2) or None
        price_final = round(po.get("final", 0) / 100, 2) or None
        discount_pct = int(po.get("discount_percent", 0))
    # A live free-to-keep promo reports is_free + 100% off on top of the full price.
    # Believe the prices, not the flags, so the promo can't fossilise (see above).
    is_free, discount_pct = reconcile_price_flags(
        is_free, price_initial, price_final, discount_pct)
    return is_free, price_initial, price_final, discount_pct


# --------------------------------------------------------------------------- #
# Build one game record. Returns dict (released, scraped) | ("pending", date, ts)
# (not yet released -> waiting room) | "skip" (confirmed non-game -> permanent)
# | "recheck" (appdetails success:false -> soft, bounded retry, §3.1)
# | None (transient error)
# --------------------------------------------------------------------------- #
def build_record(appid, prev=None):
    storefront_pace()               # shared budget (see STOREFRONT_MIN_INTERVAL)
    detail = get("https://store.steampowered.com/api/appdetails",
                 params={"appids": appid, "cc": COUNTRY, "l": "english"},
                 expect_json=True)
    if detail is None:
        return None
    node = detail.get(str(appid), {})
    if not node.get("success"):
        # success:false is ambiguous: region-locked (cc=us), a transient blip, OR a
        # genuinely dead/delisted app. Don't permanently skip on the first sighting —
        # hand back "recheck" so the caller retries across MAX_RECHECK runs before
        # giving up (a hard "skip" here dropped recoverable games forever). §3.1.
        return "recheck"
    d = node.get("data", {})
    if d.get("type") != "game":
        return "skip"                # confirmed non-game (soundtrack/DLC/video) -> permanent

    # Release gate: only store games actually out as of now. parse_release() yields
    # a release_ts ONLY for a concrete past/real date; coming_soon and vague future
    # strings ("Q4 2026", "2027", "To be announced") give ts=None. Unreleased games
    # go to the pending waiting room (re-checked each run, never permanently skipped)
    # and cost just this one appdetails probe -- no reviews/tags/HLTB/news fetched.
    release_date, release_ts, coming_soon = parse_release(d)
    if coming_soon:
        # Steam still says "coming soon": never store it, even past its date (launch is
        # usually later that day, or it slipped). Keep a dated ts so it ripens and is
        # re-probed each run -- but only for COMING_SOON_GRACE_DAYS past the date, so a
        # stale/abandoned date can't cost a probe every run forever; after that it is
        # filed undated, and upcoming_refresh.py reports it once Steam says it is out.
        if release_ts is not None and time.time() - release_ts > COMING_SOON_GRACE_DAYS * 86400:
            release_ts = None
        return ("pending", release_date, release_ts)
    if release_ts is None or release_ts > time.time():
        return ("pending", release_date, release_ts)

    title = d.get("name", f"App {appid}")
    is_free, price_initial, price_final, discount_pct = parse_price(appid, d)

    # discount_end is no longer scraped here. It's owned by price_and_sale.py (prices.json),
    # which polls IStoreBrowseService/GetItems frequently to track live end dates and
    # extensions. The main scraper only records that a discount exists (discount_pct) and
    # the prices; the frontend merges prices.json for the countdown.

    # Two remaining per-game lookups — reviews (storefront) and last-update (News API) —
    # run concurrently. SteamSpy tags were decoupled to tags_refresh.py (tags.json): even
    # without erroring, SteamSpy was slow to respond and dominated per-game time. We store
    # the Steam store "genres" here as a fallback; the frontend merges richer SteamSpy
    # user-tags from tags.json on top when available.
    tags = [g["description"] for g in d.get("genres", []) if g.get("description")]
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_reviews = ex.submit(rating_from_reviews, appid)
        f_news = ex.submit(last_update_from_news, appid)
        fetched_reviews = f_reviews.result()
        last_update_ts = f_news.result()

    # A failed or implausible fetch keeps what the stored record already says (§16).
    rating_pct, review_count = keep_reviews(fetched_reviews, prev)
    if fetched_reviews is FAILED:
        REVIEW_STATS["failed"] += 1
    elif (rating_pct, review_count) != fetched_reviews:
        REVIEW_STATS["zero_kept"] += 1
    else:
        REVIEW_STATS["answered"] += 1
    if last_update_ts is FAILED:
        last_update_ts = (prev or {}).get("last_update_ts")

    # HLTB is no longer fetched in the main loop — it was the dominant per-game cost
    # (slow, flaky scrape of howlongtobeat.com). hltb_refresh.py fills hltb.json out of
    # band, once per game (completion times are static). The frontend merges it and
    # computes QHPP client-side, so QHPP isn't stored here anymore.

    rec = {
        "appid": appid, "title": title,
        "url": f"https://store.steampowered.com/app/{appid}",
        "rating_pct": rating_pct, "review_count": review_count,
        "price_initial": price_initial, "price_final": price_final,
        "discount_pct": discount_pct, "is_free": is_free,
        "release_date": release_date, "release_ts": release_ts, "tags": tags,
        "last_update_ts": last_update_ts,
    }
    tag = "refresh" if prev is not None else "new"
    kept = " (reviews kept)" if prev and (rating_pct, review_count) != fetched_reviews else ""
    log(f"  {tag:7} {title[:38]:38} rating={rating_pct} price={price_final}/{price_initial}{kept}")
    return rec


# --------------------------------------------------------------------------- #
# Repo-committed state
# --------------------------------------------------------------------------- #
def load_games():
    if not GAMES_FILE.exists():
        return {}
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if d.get("sample"):
        log("games.json holds sample data; starting a fresh real dataset.")
        return {}
    return {str(g["appid"]): g for g in d.get("games", [])}


def save_games(processed):
    games = sorted(processed.values(), key=lambda g: g["appid"])
    GAMES_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(games), "games": games},
        ensure_ascii=False, indent=2), encoding="utf-8")


def apply_review_restore(processed):
    """Put back the reviews the 22-24 Oct 2026 wipe zeroed (§16). reviews_restore.json
    holds every game's last pre-wipe (rating_pct, review_count), built once from git
    history. A record gets them back only if it still says 0 reviews AND was scraped
    after `wiped_from` — a zero written during the wipe, never a zero Steam really
    reported before it, and never a count a later, working fetch has written. Once
    saved, those records hold a count again, so later runs change nothing; the file
    is inert after the first run and can be deleted. Returns how many were restored."""
    try:
        d = json.loads(REVIEW_RESTORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0                    # no file (the normal case once it's deleted)
    since = int(d.get("wiped_from") or 0)
    old = d.get("games") or {}
    n = 0
    for key, rec in processed.items():
        if rec.get("review_count") or (rec.get("scraped_at") or 0) < since:
            continue
        pair = old.get(key)
        if pair and pair[1]:
            rec["rating_pct"], rec["review_count"] = pair[0], int(pair[1])
            n += 1
    return n


def load_released():
    """Pending appids upcoming_refresh.py saw Steam report as released (upcoming.json
    `released`); an empty set when the file is missing, unreadable or older-format."""
    try:
        d = json.loads(UPCOMING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {int(a) for a in (d.get("released") or []) if str(a).isdigit()}


def load_catalog():
    if CATALOG_FILE.exists():
        try:
            c = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            c.setdefault("last_sync", 0)
            c.setdefault("skipped", [])
            # priority is now REBUILT every run by reconcile_seeds, so its stored value
            # is just a cache. (Legacy catalogs froze it once; that's why a late-added
            # seed never appeared. Reconciliation ignores the stale value and recomputes.)
            c.setdefault("priority", [])
            c.setdefault("seeds_ledger", {})     # {seed_key: {kind, resolved_ts, ids, forced_applied}}
            c.setdefault("force_refresh", [])    # appids queued for a one-shot forced re-scrape
            # recheck = {appid: attempts} for apps whose appdetails returned success:false;
            # retried until they resolve or hit MAX_RECHECK, then promoted to skipped. §3.1.
            rec = c.get("recheck")
            c["recheck"] = {int(k): int(v) for k, v in rec.items()} if isinstance(rec, dict) else {}
            # pending = unreleased games seen but not yet out: {appid: release_ts|null}.
            # Normalize from any legacy list form to the dict form we use now.
            pend = c.get("pending")
            if isinstance(pend, list):
                c["pending"] = {int(a): None for a in pend}
            elif isinstance(pend, dict):
                c["pending"] = {int(k): v for k, v in pend.items()}
            else:
                c["pending"] = {}
            # tba_probed drove the retired TBA sweep; dropped so the next save removes it.
            c.pop("tba_probed", None)
            return c
        except (ValueError, TypeError):
            pass
    return {"last_sync": 0, "skipped": [], "priority": [], "pending": {},
            "seeds_ledger": {}, "force_refresh": [], "recheck": {}}


# This run's line for the monitoring page (monitor.py copies the list into monitor.json).
# Upserted at every save, so a run cut off by the job timeout still leaves the numbers of its
# last checkpoint, with final=False.
RUNS_KEPT = 400                  # ~100 days at 4 runs a day


def record_run(catalog, start, stored, n_new, n_ref, n_pend, final):
    row = {"start": int(start), "saved": int(time.time()), "stored": stored,
           "new": n_new, "refreshed": n_ref, "pending": n_pend,
           "reviews_answered": REVIEW_STATS["answered"],
           "reviews_failed": REVIEW_STATS["failed"],
           "reviews_zero_kept": REVIEW_STATS["zero_kept"], "final": final}
    runs = [r for r in (catalog.get("runs") or []) if r.get("start") != row["start"]]
    catalog["runs"] = (runs + [row])[-RUNS_KEPT:]


def save_catalog(c):
    # Serialize pending with string keys (JSON object keys are strings anyway).
    out = dict(c)
    out["pending"] = {str(k): v for k, v in (c.get("pending") or {}).items()}
    out["recheck"] = {str(k): v for k, v in (c.get("recheck") or {}).items()}
    CATALOG_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def git_checkpoint(msg):
    """Commit games.json + catalog.json and push, rebasing onto whatever the other jobs
    (prices/hltb/tags/recent) have pushed in the meantime. With several Actions all
    committing to main, a naked push gets rejected ('fetch first') whenever another job
    pushed between our pull and our push — so we pull --rebase first and retry the
    pull→push a few times in case another job pushes again mid-rebase. Each job owns
    distinct files, so the rebase replays cleanly without conflicts."""
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "games.json", "catalog.json", "seeds_log.txt"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return                                    # nothing to commit
        subprocess.run(["git", "commit", "-m", msg], check=False)
        for attempt in range(1, 9):                   # retry against concurrent pushers
            subprocess.run(["git", "fetch", "origin", "main"], check=False)
            subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
            push = subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True)
            if push.returncode == 0:
                log(f"  committed: {msg}")
                return
            log(f"  push rejected (attempt {attempt}/8), another job pushed; "
                f"re-pulling and retrying")
            time.sleep(2 * attempt + random.uniform(0, 2))
        log(f"  push still failing after retries; progress kept locally, "
            f"next checkpoint will carry it")
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


# --------------------------------------------------------------------------- #
# Review freshness helpers (see REVIEW_TIERS / PICS_REV_DELTA in CONFIG)
# --------------------------------------------------------------------------- #
def review_tier(release_ts, now):
    """(tier_index, cooldown_seconds) for a game's age since release, or None when it
    is older than the last tier (or has no usable release_ts) — those keep the old
    last_modified-only behaviour."""
    if not release_ts or release_ts > now:
        return None
    age_days = (now - release_ts) / 86400
    for i, (max_age, cooldown_days) in enumerate(REVIEW_TIERS):
        if age_days <= max_age:
            return i, int(cooldown_days * 86400)
    return None


def load_pics_rev():
    """{appid: review_percentage} from pics.json — the PICS `rev` field, harvested
    daily over the whole catalog off the storefront budget (§9.6). READ-ONLY: this
    script never writes pics.json. Missing/unreadable file is not an error; the drift
    trigger just goes quiet (the tiers still run)."""
    if PICS_REV_DELTA <= 0 or not PICS_FILE.exists():
        return {}
    try:
        apps = (json.loads(PICS_FILE.read_text(encoding="utf-8")) or {}).get("apps") or {}
    except Exception as e:
        log(f"  pics.json unreadable ({e}); review-drift trigger disabled this run")
        return {}
    out = {}
    for k, rec in apps.items():
        rev = rec.get("rev")
        if isinstance(rev, list) and len(rev) >= 2 and rev[1] is not None:
            try:
                out[int(k)] = int(rev[1])
            except (TypeError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------- #
# Work selection
# --------------------------------------------------------------------------- #
def select_work(master, has_lm, processed, catalog, released=None):
    """Return (new_ids, refresh_ids). NEW work, in priority order:
      1. RIPE pending games -- previously seen as unreleased, whose release date has
         now arrived (release_ts <= now), plus any upcoming_refresh.py saw Steam report as
         released (`released`). These jump the queue so a game that came out since the
         last run gets scraped first.
      2. Fresh catalog games never seen before, newest appid first.
    Games still sitting unreleased in `pending` are NOT re-queued as fresh work here;
    they're only re-probed once ripe (above) or, for undated/TBA ones, via the catalog's
    last_modified signal. Their store details are upcoming_refresh.py's job, batched daily
    off the storefront budget — this scraper no longer sweeps them one call at a time.
    REFRESH = stored (released) games due for a re-scrape, ranked by why they're due
    (see the rank table below) and oldest-touched first within each rank. Three
    triggers, in the order they were added:
      * last_modified moved past scraped_at (the original signal — store/depot only),
      * the game's age-since-release tier cooldown elapsed (REVIEW_TIERS),
      * PICS's review percentage disagrees with our stored one (PICS_REV_DELTA).
    Ahead of all three, the forced queue and any record still carrying free-promo
    residue (promo_residue) are re-scraped first, then every stored game Steam stopped
    answering for (catalog["recheck"] or marked `unknown`; strike_no_answer).
    """
    now = int(time.time())
    skipped = set(catalog["skipped"])
    pending = dict(catalog.get("pending") or {})           # {appid: release_ts|None}
    done = {int(a) for a in processed} | skipped

    # 1. Pending games whose release date has now passed -> ripe for scraping.
    #    Undated/TBA (ts is None) stay pending until catalog last_modified flags a
    #    change (handled below), so they don't get blindly re-probed every run.
    released = released or set()
    ripe = sorted((aid for aid, ts in pending.items()
                   if aid not in done and ((ts is not None and ts <= now) or aid in released)),
                  reverse=(NEW_ORDER == "newest"))
    ripe_set = set(ripe)

    # Undated pending whose store page was touched since we filed it (keyed list only):
    # worth a cheap re-probe in case it finally got a real release date.
    if has_lm:
        # We don't store when each pending game was filed, so use last_sync as the
        # watermark: any TBA pending app modified after our last full sync is re-probed.
        watermark = catalog.get("last_sync", 0)
        touched_tba = sorted(
            (aid for aid, ts in pending.items()
             if aid not in done and aid not in ripe_set and ts is None
             and master.get(aid, 0) > watermark),
            reverse=(NEW_ORDER == "newest"))
    else:
        touched_tba = []
    touched_set = set(touched_tba)

    # 2. Genuinely fresh catalog apps: never scraped, never skipped, not already in
    #    the pending waiting room (those are covered by ripe/touched logic above).
    pending_ids = set(pending)
    fresh = [a for a in master if a not in done and a not in pending_ids]
    fresh.sort(reverse=(NEW_ORDER == "newest"))

    pri = [a for a in (catalog.get("priority") or [])
           if a not in done and a not in pending_ids]
    pri_set = set(pri)

    # Ripe + touched-TBA first (a release that just happened beats brand-new coverage),
    # then priority seeds, then the fresh frontier. The TBA and upcoming sweeps that used to
    # sit in here (up to ~2,000 storefront calls a run) are retired: upcoming_refresh.py
    # covers every pending game daily in ~1,150 batched calls and reports releases.
    new_ids = (ripe
               + touched_tba
               + pri
               + [a for a in fresh if a not in pri_set
                  and a not in ripe_set and a not in touched_set])
    log(f"Pending: {len(ripe)} ripe ({len(released & ripe_set)} reported released by "
        f"upcoming_refresh.py), {len(touched_tba)} touched-TBA")

    # Forced re-scrapes (from '!'-prefixed seeds) jump the refresh queue even though
    # last_modified hasn't moved. Only stored appids make sense to force-refresh.
    forced = [int(a) for a in (catalog.get("force_refresh") or []) if str(a) in processed]
    forced_set = set(forced)

    # Records left self-contradictory by a free-to-keep promo (see reconcile_price_flags):
    # stored free and/or -100% off while carrying a full price. They sort to the very top
    # of the site's discount view and no other job can correct them, so they jump the
    # refresh queue alongside the forced ones. The population is a handful of games
    # catalog-wide, so this can't crowd out real work.
    promo_bad = sorted((int(k) for k, rec in processed.items()
                        if int(k) not in forced_set and promo_residue(rec)),
                       key=lambda a: processed[str(a)].get("scraped_at", 0))
    promo_set = set(promo_bad)
    if promo_bad:
        log(f"Free-promo residue: {len(promo_bad)} stored record(s) priced but flagged "
            f"free/discounted -> re-scraping first")

    # Review-drift signal from the daily PICS pass — free, no storefront cost.
    # (Returns {} when PICS_REV_DELTA is 0 or pics.json isn't readable.)
    pics_rev = load_pics_rev()

    # Refresh queue ordering (lower rank = scraped earlier within this run). Review
    # freshness on young games is the whole point of the tiers, so they outrank a
    # store-page edit on an old game; the slow backfill tiers queue behind it, which
    # keeps the one-time first-pass catch-up from starving genuine last_modified work:
    #   0,1,2  tiers 0-3d / 3-10d / 10-30d  (reviews move fastest here)
    #   3      PICS says our stored rating_pct is provably wrong
    #   4      last_modified moved (a real store-page change)  <- the old sole trigger
    #   5..8   tiers 30-60d / 60-90d / 90-180d / 180-365d      (slow backfill)
    #   -1     Steam stopped answering for it (strike_no_answer): asked again every run, first
    RANK_UNANSWERED, RANK_PICS, RANK_LM = -1, 3, 4
    n_tier = n_pics = n_lm = n_unans = 0
    recheck = catalog.get("recheck") or {}

    cands = []
    for k, rec in processed.items():
        aid = int(k); sat = rec.get("scraped_at", 0)
        if aid in forced_set or aid in promo_set:
            continue                       # queued explicitly up front; don't double-add
        if aid in recheck or rec.get("unknown"):
            n_unans += 1
            cands.append((RANK_UNANSWERED, sat, aid))
            continue
        if has_lm:
            lm = master.get(aid)
            changed = lm is not None and lm > sat
        else:
            changed = (now - sat) >= REFRESH_DAYS * 86400
        # last_modified is a STORE/DEPOT change signal — it does not move when review
        # counts do, which is why the tier + drift triggers below exist. We still
        # deliberately do NOT refresh just because a game is on sale: prices/discounts/
        # sale-end-dates are owned by price_and_sale.py, which re-checks every on-sale
        # game cheaply every few hours. Re-scraping on-sale games here would be pure
        # waste — during a big sale that was ~1k+ needless refreshes per run, starving
        # the new-game frontier.
        rank = RANK_LM if changed else None
        if changed:
            n_lm += 1

        if REVIEW_TIER_REFRESH:
            tier = review_tier(rec.get("release_ts"), now)
            if tier is not None:
                i, cooldown = tier
                if (now - sat) >= cooldown:
                    n_tier += 1
                    tier_rank = i if i <= 2 else i + 2
                    rank = tier_rank if rank is None else min(rank, tier_rank)

        # Only worth checking when it could actually improve the rank (or queue at all).
        if PICS_REV_DELTA > 0 and (rank is None or rank > RANK_PICS):
            stored_pct = rec.get("rating_pct")
            pct = pics_rev.get(aid)
            if stored_pct is not None and pct is not None and abs(pct - stored_pct) >= PICS_REV_DELTA:
                n_pics += 1
                rank = RANK_PICS if rank is None else min(rank, RANK_PICS)

        if rank is not None:
            cands.append((rank, sat, aid))   # within a rank: oldest-scraped first
    cands.sort()
    if REVIEW_TIER_REFRESH or PICS_REV_DELTA > 0:
        log(f"Refresh triggers: {n_unans} unanswered, {n_tier} age-tier, {n_pics} PICS "
            f"review-drift (>={PICS_REV_DELTA}pt), {n_lm} last_modified "
            f"(overlapping; {len(cands)} distinct)")
    refresh_ids = forced + promo_bad + [a for _, _, a in cands]
    return new_ids, refresh_ids


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    processed = load_games()
    restored = apply_review_restore(processed)     # one-off repair of the Oct 2026 wipe (§16)
    if restored:
        log(f"Restored pre-wipe reviews for {restored} games from {REVIEW_RESTORE_FILE.name}.")
    catalog = load_catalog()
    released = load_released()                     # pending games Steam now says are out
    reconcile_seeds(catalog, processed)

    master, has_lm = fetch_app_universe()
    if not master:
        log("Could not fetch the app universe; aborting this run.")
        return 1

    new_ids, refresh_ids = select_work(master, has_lm, processed, catalog, released)
    log(f"Universe {len(master)} | stored {len(processed)} | skipped "
        f"{len(catalog['skipped'])} | pending {len(catalog.get('pending') or {})} | "
        f"recheck {len(catalog.get('recheck') or {})} | "
        f"to-do: {len(new_ids)} new, {len(refresh_ids)} refresh")
    log(f"Budget: {RUN_MINUTES} min | storefront pace {STOREFRONT_MIN_INTERVAL}s/call | "
        f"force-reserve {float(os.environ.get('FORCE_RESERVE_FRAC', '0.5')):.0%} | "
        f"new-reserve {float(os.environ.get('NEW_RESERVE_FRAC', '0.25')):.0%} "
        f"(both interleave into the refresh queue)")
    if REVIEW_TIER_REFRESH:
        log("Review tiers (age since release -> cooldown): "
            + ", ".join(f"<={d}d:{c}d" for d, c in REVIEW_TIERS))

    skipped = set(catalog["skipped"])
    pending = dict(catalog.get("pending") or {})
    recheck = dict(catalog.get("recheck") or {})   # {appid: attempts} soft-skip retries (§3.1)
    # Drop recheck entries no longer in Steam's app list (delisted + removed): they can't be
    # re-scraped, so they'd otherwise linger forever without ever reaching the strike cap.
    # If such an app is relisted later it re-enters as fresh work with a clean slate. A stored
    # game keeps its strikes: it is asked again every run regardless (strike_no_answer).
    recheck = {aid: n for aid, n in recheck.items() if aid in master or str(aid) in processed}
    # Durable forced-refresh queue: an '!'-seed's stored matches stay queued across runs
    # until actually re-scraped, so a budget-limited run doesn't drop them. The
    # forced_applied latch in the ledger stops them being re-added (no loop).
    force_set = set(int(a) for a in (catalog.get("force_refresh") or []))
    budget = RUN_MINUTES * 60
    job_started = os.environ.get("JOB_STARTED_AT", "").strip()
    job_timeout = os.environ.get("JOB_TIMEOUT_MINUTES", "").strip()
    if job_started.isdigit() and job_timeout.isdigit():
        left = int(job_started) + int(job_timeout) * 60 - int(start) - FINAL_SAVE_RESERVE
        if left < budget:
            log(f"Budget capped by the job clock: {max(0, left) // 60} min, so the final save "
                f"lands inside the job's {job_timeout}-min timeout")
            budget = max(0, left)
    last_commit = last_tick = time.time()
    n_new = n_ref = n_pend = 0

    # Reserve a guaranteed share of each run for forced re-scrapes (the null-update
    # drain). select_work already front-loads `forced` ahead of last_modified refreshes,
    # but during a big sale the `changed` queue can balloon to 1k+ and, combined with the
    # new-game frontier, push the tail of a large forced backlog past the time budget for
    # run after run. FORCE_RESERVE_FRAC guarantees at least this fraction of processed
    # games are drawn from the forced queue: we interleave 1 forced item every Nth pop so
    # the backlog drains steadily regardless of how much other work is pending.
    # Set to 0 to disable (pure priority order). Env-overridable for tuning.
    FORCE_RESERVE_FRAC = float(os.environ.get("FORCE_RESERVE_FRAC", "0.5"))

    # New coverage gets its own reserve for the SAME reason. `work` used to be
    # "refresh first, then new", which was harmless while the refresh queue was a few
    # hundred games a day. The age-tiered review refresh (REVIEW_TIERS) makes it
    # thousands — and ~11k on the first pass — which would push ripe/newly-released
    # games behind hours of re-scraping. That directly undercuts the point of the
    # tiers: a game must be *scraped* before it can be kept fresh. Set to 0 for the
    # old refresh-first-always behaviour.
    NEW_RESERVE_FRAC = float(os.environ.get("NEW_RESERVE_FRAC", "0.25"))

    forced_ids = [a for a in refresh_ids if a in force_set]
    other_refresh = [a for a in refresh_ids if a not in force_set]
    forced_q = deque(forced_ids)           # dedicated drain queue
    new_q = deque(new_ids)                 # ripe + priority seeds + fresh frontier
    work = deque((a, "refresh") for a in other_refresh)   # tier / drift / last_modified
    queued = {a for a, _ in work} | set(forced_q) | set(new_q)  # every appid ever queued
    handled = set()                        # every appid already popped + processed
    # Reserve accounting: each pop credits every non-empty reserve by its fraction; a
    # reserve spends a credit to jump the queue. Equivalent to the old modulo cadence
    # for a single reserve (0.5 -> every 2nd pop) but composes cleanly for two, and an
    # empty queue accrues nothing, so it can't bank credits and then burst later.
    _force_credit = _new_credit = 0.0

    def next_work():
        """Pop the next (appid, kind). Forced re-scrapes and new coverage each get at
        least their reserve share of pops; the rest goes to the refresh queue. Falls
        back to whichever queue is still non-empty."""
        nonlocal _force_credit, _new_credit
        if forced_q:
            _force_credit += FORCE_RESERVE_FRAC
        if new_q:
            _new_credit += NEW_RESERVE_FRAC
        if forced_q and _force_credit >= 1.0:
            _force_credit -= 1.0
            return forced_q.popleft(), "refresh"
        if new_q and _new_credit >= 1.0:
            _new_credit -= 1.0
            return new_q.popleft(), "new"
        if work:
            return work.popleft()
        if new_q:                          # refresh drained, new remain
            return new_q.popleft(), "new"
        if forced_q:                       # everything else drained, forced remain
            return forced_q.popleft(), "refresh"
        return None

    # Young games, precomputed once: [(appid, cooldown_seconds)] for every stored game
    # within REVIEW_LIVE_MAX_AGE_DAYS of release. Small (~2.4k), so re-checking it every
    # checkpoint costs nothing, unlike re-scanning all ~124k stored records.
    young = []
    if REVIEW_TIER_REFRESH:
        for k, rec in processed.items():
            tier = review_tier(rec.get("release_ts"), int(time.time()))
            if tier is not None and REVIEW_TIERS[tier[0]][0] <= REVIEW_LIVE_MAX_AGE_DAYS:
                young.append((int(k), tier[1]))

    def requeue_due_young():
        """Mid-run pickup for the FAST tiers. The queue is built once at the top of a
        5.5h run, so without this a game coming due 20 minutes in would wait for the
        next cron — putting a ~6h floor under cooldowns that are supposed to be 6h and
        12h. Re-checks the precomputed `young` set against live scraped_at and splices
        whatever is newly due to the FRONT of the refresh queue. `handled` makes it
        idempotent within a run: a game already scraped this run is never re-queued, so
        this cannot loop."""
        now_ = int(time.time())
        due = [a for a, cooldown in young
               if a not in handled and a not in queued
               and (now_ - processed[str(a)].get("scraped_at", 0)) >= cooldown]
        for a in due:
            work.appendleft((a, "refresh")); queued.add(a)
        if due:
            log(f"  mid-run review refresh: +{len(due)} newly-due young game(s) to front")

    def inject_new_seeds():
        """Mid-run pickup: re-read seeds.txt from origin/main and splice any newly-added
        priorities to the FRONT of the queue, so a web edit lands in THIS run instead of
        waiting ~6h for the next cron. Idempotent / no-op when nothing changed. Removals
        and force edits are honored here too (reconcile is fully declarative)."""
        text = fetch_origin_seeds()
        if text is None:
            return
        reconcile_seeds(catalog, processed, seeds_text=text)
        added_new = [a for a in catalog.get("priority", [])
                     if a not in queued and a not in handled]
        force_set.update(a for a in catalog.get("force_refresh", []) if a not in handled)
        added_forced = [a for a in catalog.get("force_refresh", [])
                        if a not in queued and a not in handled and str(a) in processed]
        for a in reversed(added_new):              # newest priority appids first
            new_q.appendleft(a); queued.add(a)
        for a in added_forced:                     # into the reserved drain queue
            forced_q.append(a); queued.add(a)
        if added_new or added_forced:
            log(f"  mid-run seed pickup: +{len(added_new)} new, "
                f"+{len(added_forced)} forced re-scrape queued to front")

    while work or forced_q or new_q:
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        nxt = next_work()
        if nxt is None:
            break
        aid, kind = nxt
        if aid in handled:                 # dedup (e.g. re-injected after already done)
            continue
        handled.add(aid)
        prev = processed.get(str(aid)) if kind == "refresh" else None
        res = build_record(aid, prev=prev)
        if isinstance(res, dict):
            res["scraped_at"] = int(time.time())
            processed[str(aid)] = res
            pending.pop(aid, None)          # graduated from waiting room (if it was there)
            recheck.pop(aid, None)          # resolved -> clear any soft-skip strikes
            force_set.discard(aid)          # forced re-scrape satisfied (if it was queued)
            n_ref += kind == "refresh"; n_new += kind == "new"
        elif isinstance(res, tuple) and res and res[0] == "pending":
            # Not out yet -> waiting room. Store its release_ts (or None for TBA) so
            # select_work can promote it the moment its date passes. Never skipped.
            _, _pdate, pts = res
            pending[aid] = pts
            recheck.pop(aid, None)          # it responded -> not a dead app after all
            processed.get(str(aid), {}).pop("unknown", None)
            n_pend += 1
        elif res == "recheck":
            # appdetails success:false: retry across runs (region-lock / transient blip)
            # rather than dropping forever. Only an app we never stored is ever a hard skip.
            strike_no_answer(aid, processed.get(str(aid)), recheck, skipped, time.time())
            pending.pop(aid, None)
        elif res == "skip":
            skipped.add(aid)
            recheck.pop(aid, None)
            pending.pop(aid, None)
            processed.get(str(aid), {}).pop("unknown", None)

        if time.time() - last_tick > CHECKPOINT_SECONDS:
            if time.time() - last_commit > COMMIT_SECONDS:
                catalog["skipped"] = sorted(skipped)
                catalog["pending"] = pending
                catalog["force_refresh"] = sorted(force_set)
                catalog["recheck"] = recheck
                record_run(catalog, start, len(processed), n_new, n_ref, n_pend, final=False)
                save_games(processed); save_catalog(catalog)
                git_checkpoint(f"checkpoint: {len(processed)} games stored")
                last_commit = time.time()
            inject_new_seeds()              # pull any seeds.txt edit into this run (own fetch)
            requeue_due_young()             # ...and any young game that came due mid-run
            last_tick = time.time()

    catalog["skipped"] = sorted(skipped)
    catalog["pending"] = pending
    catalog["force_refresh"] = sorted(force_set)
    catalog["recheck"] = recheck
    catalog["last_sync"] = int(start)
    record_run(catalog, start, len(processed), n_new, n_ref, n_pend, final=True)
    save_games(processed); save_catalog(catalog)
    git_checkpoint(f"scrape: {len(processed)} games (+{n_new} new, {n_ref} refreshed, "
                   f"{n_pend} pending)")
    log(f"\nDone. This run: {n_new} new, {n_ref} refreshed, {n_pend} held pending. "
        f"Stored {len(processed)}, skipped {len(skipped)}, waiting {len(pending)}, "
        f"rechecking {len(recheck)}, "
        f"{sum(1 for r in processed.values() if r.get('unknown'))} stored marked unknown.")
    rs = REVIEW_STATS
    log(f"Reviews: {rs['answered']} answered, {rs['failed']} failed and "
        f"{rs['zero_kept']} zero-after-count, both kept the stored value.")
    if rs["failed"] + rs["zero_kept"] > rs["answered"]:
        # An Actions annotation, so the run page says it without opening the log.
        log(f"::warning::appreviews gave no usable answer for most games this run "
            f"({rs['failed']} failed, {rs['zero_kept']} zero-after-count, "
            f"{rs['answered']} answered). Stored reviews were kept; see 'unusable reply'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
