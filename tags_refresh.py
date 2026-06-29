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

If SteamSpy has no tags for a game, the frontend falls back to the Steam store "genres"
that the main scraper still records on the game record, so tags are never blank.

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
MAX_RETRIES = 3
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qhpp tags refresher; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


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


def tags_for(appid):
    """Top-N SteamSpy user tags for a game, ranked by vote count. Returns a list (possibly
    empty if SteamSpy has no tags), or None on a hard fetch failure (retry next run)."""
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
    return []                             # SteamSpy responded but no tags -> record empty


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
    if TAGS_FILE.exists():
        try:
            d = json.loads(TAGS_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in (d.get("tags") or {}).items()}
        except (ValueError, TypeError):
            pass
    return {}


def save_tags(tags):
    TAGS_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(tags),
         "tags": {str(k): v for k, v in tags.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "tags.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"  committed: {msg}")
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


def main():
    start = time.time()
    appids = load_appids()
    if not appids:
        log("No games in games.json (or only sample data). Nothing to do.")
        return 0

    tags = load_tags()
    # Only games we've never resolved (no entry). Already-resolved games — including ones
    # recorded as an empty list (SteamSpy had none) — are skipped.
    todo = [a for a in appids if a not in tags]
    log(f"Games total {len(appids)} | tags resolved {len(tags)} | to resolve {len(todo)}")
    if not todo:
        log("Every game already has a tags entry. Done.")
        save_tags(tags)
        return 0

    budget = RUN_MINUTES * 60
    last_commit = time.time()
    n_tagged = n_empty = 0
    for i, aid in enumerate(todo, 1):
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        res = tags_for(aid)
        if res is None:
            continue                      # transient error -> leave unresolved, retry next run
        tags[aid] = res
        if res:
            n_tagged += 1
        else:
            n_empty += 1
        if i % 50 == 0 or i == len(todo):
            log(f"  [{i}/{len(todo)}] {n_tagged} tagged, {n_empty} no-tags")
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_tags(tags)
            git_checkpoint(f"tags: {len(tags)} games resolved (checkpoint)")
            last_commit = time.time()

    save_tags(tags)
    git_checkpoint(f"tags: {len(tags)} games resolved")
    log(f"\nDone. This run: {n_tagged} tagged, {n_empty} no-tags. tags.json now has "
        f"{len(tags)} entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
