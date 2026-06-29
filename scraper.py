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
import re
import subprocess
import sys
import threading
import time
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
SEEDS_FILE = HERE / "seeds.txt"           # OPTIONAL games to scrape FIRST

# Free Steam Web API key from https://steamcommunity.com/dev/apikey (GitHub secret
# STEAM_API_KEY). Without it, falls back to the keyless full list (all app types,
# no last_modified, so refresh reverts to the REFRESH_DAYS timer).
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))   # scrape budget per run
CHECKPOINT_SECONDS = 600        # git-commit progress at least this often during a run
TIME_BUFFER = 120               # stop scraping this long before the budget, to commit
NEW_ORDER = "newest"            # "newest" (high appid first) or "oldest"
REFRESH_DAYS = 7                # fallback refresh age when no API key (no last_modified)

# TOP_TAGS / SteamSpy tags moved to tags_refresh.py (it was the slowest per-game call).
COUNTRY = "us"                  # cc=us => USD prices

STEAM_DELAY = 1.5               # seconds between storefront calls (~200/5min limit)
WEBAPI_DELAY = 1.0             # between GetAppList pages
NEWS_DELAY = 0.3               # between News API calls (api.steampowered.com; huge budget)
MAX_RETRIES = 4

# News items counted as "updates" (vs sale posts / announcements). The store page's
# ?updates=true filter is driven by Steam update events; the public news feed doesn't
# always tag them cleanly, so this is a good-enough heuristic: the patchnotes tag is
# the strong signal, keywords are the fallback, and obvious sale posts are excluded.
_UPDATE_TAGS = {"patchnotes"}
_UPDATE_WORDS = ("update", "patch", "hotfix", "changelog", "release notes",
                 "version", "build ", "bug fix", "bugfix", "fixes", "balance")
_NOT_UPDATE = ("sale", "discount", "% off", "wishlist", "now available", "out now",
               "launch", "release date", "pre-order", "preorder", "trailer", "soundtrack")

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
    data = get(SEARCH_URL, params=p, expect_json=True); time.sleep(STEAM_DELAY)
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


def ensure_priority(catalog):
    if catalog.get("priority") is not None:
        return
    pr = []
    if SEEDS_FILE.exists():
        for raw in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.isdigit():
                pr.append(int(line))
            else:
                params = search_params(line); start = 0
                for _ in range(10):
                    ids, total = fetch_search_page(params, start)
                    if not ids:
                        break
                    pr.extend(ids); start += 100
                    if total and start >= total:
                        break
    catalog["priority"] = list(dict.fromkeys(pr))
    log(f"Priority seeds resolved: {len(catalog['priority'])} appids")


# --------------------------------------------------------------------------- #
# Per-game data sources
# --------------------------------------------------------------------------- #
# NOTE: sale end-dates are no longer fetched here. They're owned by sales_refresh.py
# (sales.json), which uses the batched IStoreBrowseService/GetItems endpoint — far
# cheaper and more reliable than the old featuredcategories map + per-page HTML scrape
# that used to live here, and decoupled from this slow, rate-limited main scrape.


def rating_from_reviews(appid):
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
    """True if a news item looks like a game update/patch rather than a sale post."""
    if any(t in _UPDATE_TAGS for t in (item.get("tags") or [])):
        return True
    text = (str(item.get("title", "")) + " " + str(item.get("feedlabel", ""))).lower()
    if any(bad in text for bad in _NOT_UPDATE):
        return False
    return any(w in text for w in _UPDATE_WORDS)


def last_update_from_news(appid):
    """Most recent *update* post timestamp via the News API. Lives on
    api.steampowered.com (huge budget, separate from the storefront limit), so this is
    cheap to refresh broadly. Returns a unix ts or None (no update posts found)."""
    params = {"appid": appid, "count": 20, "maxlength": 1, "format": "json"}
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
    detail = get("https://store.steampowered.com/api/appdetails",
                 params={"appids": appid, "cc": COUNTRY, "l": "english"},
                 expect_json=True)
    time.sleep(STEAM_DELAY)
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

    # discount_end is no longer scraped here. It's owned by sales_refresh.py (sales.json),
    # which polls IStoreBrowseService/GetItems frequently to track live end dates and
    # extensions. The main scraper only records that a discount exists (discount_pct) and
    # the prices; the frontend merges sales.json for the countdown.

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
            c.setdefault("priority", None)
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
    return {"last_sync": 0, "skipped": [], "priority": None, "pending": {}}


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
        subprocess.run(["git", "add", "games.json", "catalog.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return                                    # nothing to commit
        subprocess.run(["git", "commit", "-m", msg], check=False)
        for attempt in range(1, 5):                   # retry against concurrent pushers
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            push = subprocess.run(["git", "push"],
                                  capture_output=True, text=True)
            if push.returncode == 0:
                log(f"  committed: {msg}")
                return
            log(f"  push rejected (attempt {attempt}/4), another job pushed; "
                f"re-pulling and retrying")
            time.sleep(2 * attempt)
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

    cands = []
    for k, rec in processed.items():
        aid = int(k); sat = rec.get("scraped_at", 0)
        if has_lm:
            lm = master.get(aid)
            changed = lm is not None and lm > sat
        else:
            changed = (now - sat) >= REFRESH_DAYS * 86400
        if changed or rec.get("discount_pct", 0) > 0:
            cands.append((sat, aid))
    cands.sort()
    refresh_ids = [a for _, a in cands]
    return new_ids, refresh_ids


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    processed = load_games()
    catalog = load_catalog()
    ensure_priority(catalog)

    master, has_lm = fetch_app_universe()
    if not master:
        log("Could not fetch the app universe; aborting this run.")
        return 1

    new_ids, refresh_ids = select_work(master, has_lm, processed, catalog)
    log(f"Universe {len(master)} | stored {len(processed)} | skipped "
        f"{len(catalog['skipped'])} | pending {len(catalog.get('pending') or {})} | "
        f"to-do: {len(new_ids)} new, {len(refresh_ids)} refresh")
    log(f"Budget: {RUN_MINUTES} min (refresh changed games first, then new coverage)")

    skipped = set(catalog["skipped"])
    pending = dict(catalog.get("pending") or {})
    budget = RUN_MINUTES * 60
    last_commit = time.time()
    n_new = n_ref = n_pend = 0

    work = [(a, "refresh") for a in refresh_ids] + [(a, "new") for a in new_ids]
    for aid, kind in work:
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        prev = processed.get(str(aid)) if kind == "refresh" else None
        res = build_record(aid, prev=prev)
        if isinstance(res, dict):
            res["scraped_at"] = int(time.time())
            processed[str(aid)] = res
            pending.pop(aid, None)          # graduated from waiting room (if it was there)
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
            save_games(processed); save_catalog(catalog)
            git_checkpoint(f"checkpoint: {len(processed)} games stored")
            last_commit = time.time()

    catalog["skipped"] = sorted(skipped)
    catalog["pending"] = pending
    catalog["last_sync"] = int(start)
    save_games(processed); save_catalog(catalog)
    git_checkpoint(f"scrape: {len(processed)} games (+{n_new} new, {n_ref} refreshed, "
                   f"{n_pend} pending)")
    log(f"\nDone. This run: {n_new} new, {n_ref} refreshed, {n_pend} held pending. "
        f"Stored {len(processed)}, skipped {len(skipped)}, waiting {len(pending)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
