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
    each run scrapes until RUN_MINUTES elapses, git-committing every few minutes
    so a 6-hour-wall kill never loses progress.
  * Released-only, with a waiting room. Only games actually out (a concrete past
    release date) are stored — no empty coming-soon shells. Unreleased games go to
    catalog["pending"] = {appid: release_ts|null}, costing just one appdetails
    probe, and are promoted to the scrape queue the moment their release date
    passes. Nothing is ever permanently skipped for being unreleased. New work is
    ordered newest-appid-first, so just-released titles surface near the front.

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
from collections import deque
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

# Free Steam Web API key from https://steamcommunity.com/dev/apikey (GitHub secret
# STEAM_API_KEY). Without it, falls back to the keyless full list (all app types,
# no last_modified, so refresh reverts to the REFRESH_DAYS timer).
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))   # scrape budget per run
CHECKPOINT_SECONDS = 600        # git-commit progress at least this often during a run
TIME_BUFFER = 120               # stop scraping this long before the budget, to commit
NEW_ORDER = "newest"            # "newest" (high appid first) or "oldest"
REFRESH_DAYS = 7                # fallback refresh age when no API key (no last_modified)
SEED_RESOLVE_TTL = 24 * 3600    # live term/URL seeds: re-resolve at most once per this interval

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


def rating_from_reviews(appid):
    storefront_pace()               # was UNPACED — the burst source that tripped 403s
    data = get(f"https://store.steampowered.com/appreviews/{appid}",
               params={"json": 1, "language": "all", "purchase_type": "all",
                       "num_per_page": 0}, expect_json=True)
    if not isinstance(data, dict) or data.get("success") != 1:
        return None, 0
    s = data.get("query_summary", {})
    pos, neg = int(s.get("total_positive", 0)), int(s.get("total_negative", 0))
    if pos + neg == 0:
        return None, 0
    return round(pos / (pos + neg) * 100), pos + neg


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
    cheap to refresh broadly. Returns a unix ts or None (no update posts found).

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
    if not isinstance(data, dict):
        return None
    items = (data.get("appnews") or {}).get("newsitems") or []
    stamps = [it.get("date") for it in items if _is_update_item(it) and it.get("date")]
    return max(stamps) if stamps else None


_RELEASE_FORMATS = ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%Y")


def parse_release(d):
    rd = d.get("release_date", {}) or {}
    s = (rd.get("date") or "").strip()
    ts = None
    if s and not rd.get("coming_soon"):
        for fmt in _RELEASE_FORMATS:
            try:
                ts = int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
                break
            except ValueError:
                continue
    return (s or None), ts


# --------------------------------------------------------------------------- #
# Build one game record. Returns dict (released, scraped) | ("pending", date, ts)
# (not yet released ->
# waiting room) | "skip" (non-game/delisted -> permanent) | None (transient error)
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
        return "skip"
    d = node.get("data", {})
    if d.get("type") != "game":
        return "skip"

    # Release gate: only store games actually out as of now. parse_release() yields
    # a release_ts ONLY for a concrete past/real date; coming_soon and vague future
    # strings ("Q4 2026", "2027", "To be announced") give ts=None. Unreleased games
    # go to the pending waiting room (re-checked each run, never permanently skipped)
    # and cost just this one appdetails probe -- no reviews/tags/HLTB/news fetched.
    release_date, release_ts = parse_release(d)
    if release_ts is None or release_ts > time.time():
        return ("pending", release_date, release_ts)

    title = d.get("name", f"App {appid}")
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
        rating_pct, review_count = f_reviews.result()
        last_update_ts = f_news.result()

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
    log(f"  {tag:7} {title[:38]:38} rating={rating_pct} price={price_final}/{price_initial}")
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
            # pending = unreleased games seen but not yet out: {appid: release_ts|null}.
            # Normalize from any legacy list form to the dict form we use now.
            pend = c.get("pending")
            if isinstance(pend, list):
                c["pending"] = {int(a): None for a in pend}
            elif isinstance(pend, dict):
                c["pending"] = {int(k): v for k, v in pend.items()}
            else:
                c["pending"] = {}
            return c
        except (ValueError, TypeError):
            pass
    return {"last_sync": 0, "skipped": [], "priority": [], "pending": {},
            "seeds_ledger": {}, "force_refresh": []}


def save_catalog(c):
    # Serialize pending with string keys (JSON object keys are strings anyway).
    out = dict(c)
    out["pending"] = {str(k): v for k, v in (c.get("pending") or {}).items()}
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
# Work selection
# --------------------------------------------------------------------------- #
def select_work(master, has_lm, processed, catalog):
    """Return (new_ids, refresh_ids). NEW work, in priority order:
      1. RIPE pending games -- previously seen as unreleased, whose release date has
         now arrived (release_ts <= now). These jump the queue so a game that came
         out since the last run gets scraped first.
      2. Fresh catalog games never seen before, newest appid first.
    Games still sitting unreleased in `pending` are NOT re-queued as fresh work here;
    they're only re-probed once ripe (above) or, for undated/TBA ones, occasionally
    via the catalog's last_modified signal -- so we don't burn probes on them.
    REFRESH = stored (released) games whose last_modified moved past scraped_at (or on
    sale), oldest-touched first.
    """
    now = int(time.time())
    skipped = set(catalog["skipped"])
    pending = dict(catalog.get("pending") or {})           # {appid: release_ts|None}
    done = {int(a) for a in processed} | skipped

    # 1. Pending games whose release date has now passed -> ripe for scraping.
    #    Undated/TBA (ts is None) stay pending until catalog last_modified flags a
    #    change (handled below), so they don't get blindly re-probed every run.
    ripe = sorted((aid for aid, ts in pending.items()
                   if aid not in done and ts is not None and ts <= now),
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
    # then priority seeds, then the fresh frontier.
    new_ids = (ripe
               + touched_tba
               + pri
               + [a for a in fresh if a not in pri_set
                  and a not in ripe_set and a not in touched_set])

    # Forced re-scrapes (from '!'-prefixed seeds) jump the refresh queue even though
    # last_modified hasn't moved. Only stored appids make sense to force-refresh.
    forced = [int(a) for a in (catalog.get("force_refresh") or []) if str(a) in processed]
    forced_set = set(forced)

    cands = []
    for k, rec in processed.items():
        aid = int(k); sat = rec.get("scraped_at", 0)
        if aid in forced_set:
            continue                       # queued explicitly up front; don't double-add
        if has_lm:
            lm = master.get(aid)
            changed = lm is not None and lm > sat
        else:
            changed = (now - sat) >= REFRESH_DAYS * 86400
        # Refresh ONLY when Steam's last_modified actually moved (or, without an API key,
        # the fallback timer elapsed). We deliberately do NOT refresh just because a game
        # is on sale: prices/discounts/sale-end-dates are owned by price_and_sale.py, which
        # re-checks every on-sale game cheaply every few hours. Re-scraping on-sale games
        # here would be pure waste — during a big sale that was ~1k+ needless refreshes per
        # run, starving the new-game frontier.
        if changed:
            cands.append((sat, aid))
    cands.sort()
    refresh_ids = forced + [a for _, a in cands]
    return new_ids, refresh_ids


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    processed = load_games()
    catalog = load_catalog()
    reconcile_seeds(catalog, processed)

    master, has_lm = fetch_app_universe()
    if not master:
        log("Could not fetch the app universe; aborting this run.")
        return 1

    new_ids, refresh_ids = select_work(master, has_lm, processed, catalog)
    log(f"Universe {len(master)} | stored {len(processed)} | skipped "
        f"{len(catalog['skipped'])} | pending {len(catalog.get('pending') or {})} | "
        f"to-do: {len(new_ids)} new, {len(refresh_ids)} refresh")
    log(f"Budget: {RUN_MINUTES} min | storefront pace {STOREFRONT_MIN_INTERVAL}s/call | "
        f"force-reserve {float(os.environ.get('FORCE_RESERVE_FRAC', '0.5')):.0%} "
        f"(forced re-scrape drains first, interleaved with changed + new coverage)")

    skipped = set(catalog["skipped"])
    pending = dict(catalog.get("pending") or {})
    # Durable forced-refresh queue: an '!'-seed's stored matches stay queued across runs
    # until actually re-scraped, so a budget-limited run doesn't drop them. The
    # forced_applied latch in the ledger stops them being re-added (no loop).
    force_set = set(int(a) for a in (catalog.get("force_refresh") or []))
    budget = RUN_MINUTES * 60
    last_commit = time.time()
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
    forced_ids = [a for a in refresh_ids if a in force_set]
    other_refresh = [a for a in refresh_ids if a not in force_set]
    forced_q = deque(forced_ids)           # dedicated drain queue
    # `work` holds everything NON-forced, in the old order (other refresh, then new).
    work = deque([(a, "refresh") for a in other_refresh] + [(a, "new") for a in new_ids])
    queued = {a for a, _ in work} | set(forced_q)   # every appid ever placed in a queue
    handled = set()                        # every appid already popped + processed
    _pop_counter = 0                       # drives the forced-interleave cadence

    def next_work():
        """Pop the next (appid, kind), interleaving the forced drain queue so it gets at
        least FORCE_RESERVE_FRAC of pops. Falls back to whichever queue is non-empty."""
        nonlocal _pop_counter
        take_forced = False
        if forced_q and FORCE_RESERVE_FRAC > 0:
            # every ~1/frac pops, take a forced item (e.g. frac=0.5 -> every 2nd pop)
            cadence = max(1, round(1 / FORCE_RESERVE_FRAC))
            take_forced = (_pop_counter % cadence == 0) or not work
        elif forced_q and not work:
            take_forced = True
        _pop_counter += 1
        if take_forced and forced_q:
            return forced_q.popleft(), "refresh"
        if work:
            return work.popleft()
        if forced_q:                       # work drained, forced remain
            return forced_q.popleft(), "refresh"
        return None

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
            work.appendleft((a, "new")); queued.add(a)
        for a in added_forced:                     # into the reserved drain queue
            forced_q.append(a); queued.add(a)
        if added_new or added_forced:
            log(f"  mid-run seed pickup: +{len(added_new)} new, "
                f"+{len(added_forced)} forced re-scrape queued to front")

    while work or forced_q:
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
            force_set.discard(aid)          # forced re-scrape satisfied (if it was queued)
            n_ref += kind == "refresh"; n_new += kind == "new"
        elif isinstance(res, tuple) and res and res[0] == "pending":
            # Not out yet -> waiting room. Store its release_ts (or None for TBA) so
            # select_work can promote it the moment its date passes. Never skipped.
            _, _pdate, pts = res
            pending[aid] = pts
            n_pend += 1
        elif res == "skip":
            skipped.add(aid)
            pending.pop(aid, None)

        if time.time() - last_commit > CHECKPOINT_SECONDS:
            catalog["skipped"] = sorted(skipped)
            catalog["pending"] = pending
            catalog["force_refresh"] = sorted(force_set)
            save_games(processed); save_catalog(catalog)
            git_checkpoint(f"checkpoint: {len(processed)} games stored")
            inject_new_seeds()              # after the push, pull any seeds.txt edit into this run
            last_commit = time.time()

    catalog["skipped"] = sorted(skipped)
    catalog["pending"] = pending
    catalog["force_refresh"] = sorted(force_set)
    catalog["last_sync"] = int(start)
    save_games(processed); save_catalog(catalog)
    git_checkpoint(f"scrape: {len(processed)} games (+{n_new} new, {n_ref} refreshed, "
                   f"{n_pend} pending)")
    log(f"\nDone. This run: {n_new} new, {n_ref} refreshed, {n_pend} held pending. "
        f"Stored {len(processed)}, skipped {len(skipped)}, waiting {len(pending)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
