#!/usr/bin/env python3
"""
Steam QHPP — SteamSpy tags refresher
====================================
A SEPARATE, independent job from scraper.py, same pattern as HLTB and prices. SteamSpy
was the slowest call left in the main scrape loop: even when it doesn't error (no 429s),
steamspy.com is often slow to respond (~3-4s), and because build_record waited on it
inside the concurrent block, it dominated per-game time and dragged the scrape down to
~8-9 games/min. Pulling it out drops the main scrape to just appdetails + reviews + news
(~13+ games/min).

Why a separate cadence works: a game's tags are effectively static (they shift very
slowly as users vote). So each game needs the tags fetched once, then rarely. This job
fills tags.json for games that don't have an entry yet and otherwise leaves them be. It
runs slowly in the background and never holds up the main scrape.

If SteamSpy has no tags for a game, this job falls back to the game's Steam store page and
reads the user tags embedded there (the InitAppTagModal list) — Steam has these for many
titles SteamSpy misses, including brand-new and unreleased ones. Only if that too comes up
empty does the frontend fall back to the Steam store "genres" the main scraper records, so
tags are never blank. Store-fallback empties are tracked in `store_checked` so they aren't
re-fetched on every run.

Ownership (one writer per file):
  scraper.py      -> games.json   (catalog, rating, last_update, release, genre fallback)
  price_and_sale  -> prices.json  (price, discount %, sale end)
  hltb_refresh    -> hltb.json    (static completion times)
  THIS            -> tags.json    {appid: [tag, tag, ...]}   (SteamSpy user tags)
  recent_refresh  -> recent.json  (30-day review scores)
The frontend merges all of these by appid.

Reads games.json (read-only) for the appid list.
"""

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # read-only (owned by scraper.py)
TAGS_FILE = HERE / "tags.json"            # this job's output (committed)

TOP_TAGS = 8
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "120"))
CHECKPOINT_SECONDS = 300
TIME_BUFFER = 45
STEAMSPY_DELAY = 1.1                       # SteamSpy asks for ~1 req/sec
STORE_DELAY = 0.7                          # Steam storefront pacing for the fallback fetch
MAX_RETRIES = 3
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qhpp tags refresher; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Cookies that clear Steam's age / mature-content interstitial so the store page renders
# its real tag list (adult titles otherwise serve an age-check page with no tags). Sent
# only on the store-page fallback request, never to SteamSpy.
STORE_COOKIES = {"birthtime": "0", "mature_content": "1", "wants_mature_content": "1",
                 "lastagecheckage": "1-0-1990", "Steam_Language": "english"}
# The store page embeds its user tags in an inline InitAppTagModal( appid, [ {...}, ... ] )
# call; the array is already ordered by vote count, matching TOP_TAGS truncation.
TAG_MODAL_RE = re.compile(r"InitAppTagModal\(\s*\d+\s*,\s*(\[.*?\])\s*,", re.S)


def log(msg):
    print(msg, flush=True)


def get(url, *, params=None, timeout=20):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(60, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return None
        except requests.RequestException as e:
            wait = min(20, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


def get_html(url, *, timeout=25, cookies=None):
    """Fetch a page as text (used for the Steam store fallback). Returns the HTML string,
    or None on a hard fetch failure so the caller can retry next run."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=timeout, cookies=cookies)
            if r.status_code == 429:
                wait = min(60, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            wait = min(20, 3 * attempt)
            log(f"  store request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


def steamspy_tags_for(appid):
    """Top-N SteamSpy user tags, ranked by vote count. Returns a list (possibly empty if
    SteamSpy has no tags), or None on a hard fetch failure (retry next run)."""
    data = get("https://steamspy.com/api.php",
               params={"request": "appdetails", "appid": appid})
    time.sleep(STEAMSPY_DELAY)
    if data is None:
        return None                       # transient -> don't record, retry next run
    if isinstance(data, dict):
        tags = data.get("tags") or {}
        if isinstance(tags, dict) and tags:
            ranked = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)
            return [name for name, _ in ranked[:TOP_TAGS]]
    return []                             # SteamSpy responded but no tags


def store_tags_for(appid):
    """Fallback: the Steam store page's own user tags (the InitAppTagModal list), which
    exist for many games SteamSpy hasn't tagged — including brand-new and unreleased ones.
    Returns a list (possibly empty if the page has no tag modal), or None on a hard fetch
    failure. Mature-content cookies are sent so age-gated titles render their tags."""
    html = get_html(f"https://store.steampowered.com/app/{appid}/", cookies=STORE_COOKIES)
    time.sleep(STORE_DELAY)
    if html is None:
        return None                       # transient -> don't record, retry next run
    m = TAG_MODAL_RE.search(html)
    if not m:
        return []                         # no tag modal (delisted / age-wall / region-lock)
    try:
        arr = json.loads(m.group(1))
    except ValueError:
        return []
    names = [n for n in (t.get("name", "").strip() for t in arr if isinstance(t, dict)) if n]
    return names[:TOP_TAGS]


def tags_for(appid):
    """Resolve a game's tags. SteamSpy is primary; when it responds with no tags we fall
    back to the Steam store page. Returns (tags, store_checked): `tags` is a list, or None
    on a transient failure (don't record, retry next run); `store_checked` is True when the
    store fallback was consulted, so a genuinely-empty result isn't re-fetched every run."""
    res = steamspy_tags_for(appid)
    if res is None:
        return None, False                # SteamSpy transient -> retry, store not consulted
    if res:
        return res, False                 # SteamSpy had tags -> primary source wins
    store = store_tags_for(appid)         # SteamSpy empty -> try the store page
    if store is None:
        return None, False                # store transient -> retry whole game next run
    return store, True                    # store answered (tags or genuinely empty)


def load_appids():
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return [int(g["appid"]) for g in d.get("games", [])]


def load_tags():
    """Returns (tags, store_checked). `store_checked` is the set of appids whose empty
    result was already confirmed via the store fallback, so we don't re-fetch them."""
    if TAGS_FILE.exists():
        try:
            d = json.loads(TAGS_FILE.read_text(encoding="utf-8"))
            tags = {int(k): v for k, v in (d.get("tags") or {}).items()}
            checked = {int(a) for a in (d.get("store_checked") or [])}
            return tags, checked
        except (ValueError, TypeError):
            pass
    return {}, set()


def save_tags(tags, store_checked):
    TAGS_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(tags),
         "tags": {str(k): v for k, v in tags.items()},
         "store_checked": sorted(store_checked)},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "tags.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            for _attempt in range(1, 9):    # retry against other jobs pushing concurrently
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log(f"  committed: {msg}")
                    break
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


def main():
    start = time.time()
    appids = load_appids()
    if not appids:
        log("No games in games.json (or only sample data). Nothing to do.")
        return 0

    tags, checked = load_tags()
    # To resolve: games with no entry yet, plus games recorded empty by the SteamSpy-only
    # path that haven't been through the store fallback (backfills the pre-existing gap).
    # Games with real tags, or empties already store-confirmed, are skipped.
    todo = [a for a in appids if a not in tags or (not tags[a] and a not in checked)]
    log(f"Games total {len(appids)} | tags resolved {len(tags)} | to resolve {len(todo)}")
    if not todo:
        log("Every game already has a tags entry. Done.")
        save_tags(tags, checked)
        return 0

    budget = RUN_MINUTES * 60
    last_commit = time.time()
    n_tagged = n_empty = n_store = 0
    for i, aid in enumerate(todo, 1):
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        res, store_checked = tags_for(aid)
        if res is None:
            continue                      # transient error -> leave unresolved, retry next run
        tags[aid] = res
        if store_checked:
            checked.add(aid)              # store fallback ran -> don't re-fetch this empty
            n_store += 1
        if res:
            n_tagged += 1
        else:
            n_empty += 1
        if i % 50 == 0 or i == len(todo):
            log(f"  [{i}/{len(todo)}] {n_tagged} tagged, {n_empty} no-tags "
                f"({n_store} via store fallback)")
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_tags(tags, checked)
            git_checkpoint(f"tags: {len(tags)} games resolved (checkpoint)")
            last_commit = time.time()

    save_tags(tags, checked)
    git_checkpoint(f"tags: {len(tags)} games resolved")
    log(f"\nDone. This run: {n_tagged} tagged, {n_empty} no-tags, {n_store} store lookups. "
        f"tags.json now has {len(tags)} entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
