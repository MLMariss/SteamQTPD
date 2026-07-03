#!/usr/bin/env python3
"""
Steam QHPP — playtime refresher (identity-keyed, sentiment-split)
=================================================================
A SEPARATE, independent job from scraper.py. It captures, per game, how long
reviewers have actually played it — split by whether they recommended it — from
the public `appreviews` endpoint (the "X hrs on record" on each review card).

This maintains the BIG file (playtime_raw.json). A separate script,
playtime_summarize.py, reads this file and writes the small frontend-facing
playtime.json (medians + counts). One writer per file:
  * scraper.py -> games.json, recent_refresh.py -> recent.json, etc.
  * THIS -> playtime_raw.json     (per-review detail; scraper working set)
  * playtime_summarize.py -> playtime.json   (summarized medians for the frontend)

------------------------------------------------------------------------------
WHY IDENTITY-KEYED (recommendationid), not a cursor position
------------------------------------------------------------------------------
`filter=recent` orders reviews newest-first, so a cursor is a position in an
ordering that SHIFTS every time new reviews arrive (they insert at the top). A
saved cursor therefore can't reliably "resume" — after new reviews land it points
at different reviews than before. The fix is to track review IDENTITY, not
position: every review has a stable `recommendationid` that never moves.

Each scrape walks from the top (newest) and, per review:
  * unseen id  -> add it (new review, or deeper-than-before on first pass)
  * known id   -> we've reached reviews we already have. Because new reviews are
                  always at the top, a run of known ids means everything below is
                  also known -> we can stop growing.
This makes every run BOTH an update (new reviews caught at the top) AND, on
demand, a deepening (keep walking past known ids into genuinely-unseen older
reviews via the cursor). Re-runs can't double-count: known ids are recognised.

------------------------------------------------------------------------------
REFRESH-ON-REVISIT (fixes playtime drift)
------------------------------------------------------------------------------
`playtime_forever` is a LIVE value — a reviewer we saw at 40h may now be at 120h
because they kept playing. When a walk passes over a review we already store, we
UPDATE its stored playtime to the freshly-returned value (free — we already
fetched the page). So the reviews most likely to still be changing (recent ones,
near the top) stay current at no extra request cost.

------------------------------------------------------------------------------
GUARDRAILS (keep the in-repo file from ballooning)
------------------------------------------------------------------------------
G1 — per-game cap: store at most PER_GAME_CAP (1000) reviews per game. When full
     and new reviews arrive, drop the OLDEST stored reviews (ring-buffer by
     timestamp) to make room. 1000 reviews is far more than a median needs; the
     cap bounds the file to a known ceiling no matter how popular a game gets.
G2 — low commit churn: this script commits the big file periodically DURING the
     run (every ~30 min) plus once at run-end, rather than on every checkpoint.
     Committing during the run (not only at the end) matters because GitHub runners
     are ephemeral: a single end-of-run commit means a cancelled / timed-out /
     evicted 3-hour job loses ALL its work when the runner's disk is destroyed.
     ~30-min commits bound that loss to ~30 min while keeping history growth modest
     (a 3h run makes ~6 commits, not ~18 and not 1).
G3 — history pruning: deferred (add a scheduled squash workflow if/when the repo
     actually grows). Not needed at current scale.

------------------------------------------------------------------------------
RATE-LIMIT ISOLATION
------------------------------------------------------------------------------
Hits store.steampowered.com, sharing a ~200-request / 5-minute / IP budget with
scraper.py, price_and_sale.py, and recent_refresh.py. A 403 soft-limit costs a
5-MINUTE cooldown. This job runs in its OWN cron slot, staggered away from the
other storefront jobs (see playtime.yml), so it runs with the budget to itself.
Do NOT co-schedule it with the other storefront jobs without re-tuning delays.
"""

import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"                 # read-only here (owned by scraper.py)
RAW_FILE = HERE / "playtime_raw.json"            # THIS file's output (the big file)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()  # not required (appreviews is keyless)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))
# G2 (revised): commit the raw file to git every COMMIT_SECONDS *during* the run —
# not once at the end. A single end-of-run commit is fragile: GitHub runners are
# ephemeral, so if a 3-hour job is cancelled / times out / gets evicted before the
# final commit, the runner's disk is destroyed and the ENTIRE run's work is lost.
# Committing every ~30 min means an interruption loses at most ~30 min, while still
# keeping history growth modest (a 3h run makes ~6 commits, not ~18 and not 1).
COMMIT_SECONDS = 1800             # 30 min: git-commit the raw file this often
TIME_BUFFER = 90

# --- sampling / growth ----------------------------------------------------- #
# On a normal pass we try to reach TARGET_REVIEWS *stored* reviews per game. New
# reviews at the top are always taken; growth beyond what we have walks deeper.
TARGET_REVIEWS = int(os.environ.get("TARGET_REVIEWS", "200"))
PER_PAGE = 100                    # appreviews hard max is 100/page
# One-off deep pass: `DEEPEN_TARGET=1000 python playtime_refresh.py` raises the
# target for THIS run and prefers games below it. Capped by PER_GAME_CAP.
DEEPEN_TARGET = int(os.environ.get("DEEPEN_TARGET", "0"))

# --- G1: per-game hard cap ------------------------------------------------- #
PER_GAME_CAP = 1000               # max reviews stored per game (ring-buffer when full)

# --- stop-on-seen tuning --------------------------------------------------- #
# When growing, stop after this many CONSECUTIVE already-known reviews (a small
# cushion absorbs minor reordering at the boundary without walking the whole list).
SEEN_STREAK_STOP = 50

COOLDOWN_DAYS = 7                 # don't re-walk a game's reviews younger than this
NOUPDATE_COOLDOWN_DAYS = 30       # dormant games: refresh far less often
UPDATE_ACTIVE_DAYS = 90           # "recently updated" = patched within this many days
MIN_SEGMENT_FOR_MEDIAN = 3        # below this many samples, a segment median is null
# Hard eligibility floor: a sentiment-split median needs a usable sample. Games
# with fewer than this many all-time reviews can't produce one (they'd null out at
# MIN_SEGMENT_FOR_MEDIAN anyway), so we don't spend request budget on them. This is
# "skip for now", NOT permanent exclusion: eligibility is re-checked every run
# against the live review_count from games.json, so a game re-qualifies the moment
# it crosses the floor. Removes ~41k unusable games from the queue, leaving budget
# for the ~52k that can actually yield data.
MIN_REVIEWS_FLOOR = 10

STEAM_DELAY = 2.0                 # storefront limit (~200/5min); a touch slower (we paginate)
MAX_RETRIES = 4

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qhpp playtime-refresher; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)


def log(msg):
    print(msg, flush=True)


def get(url, *, params=None, timeout=30):
    """Same retry/backoff contract as the other storefront scrapers."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 5 min"); time.sleep(300); continue
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- #
# Per-review extraction
# --------------------------------------------------------------------------- #
# Big-file per-review record (kept minimal — this is the storage cost driver):
#   recommendationid -> {"pt": <playtime_forever minutes>,
#                        "up": <voted_up bool>,
#                        "ts": <timestamp_updated unix>}   # for ring-buffer + future smart-refresh
def _parse_review(rv):
    """Return (recommendationid, record) or None if unusable (no id / no playtime)."""
    rid = rv.get("recommendationid")
    if not rid:
        return None
    a = rv.get("author") or {}
    pt = a.get("playtime_forever")
    try:
        pt = int(pt)
    except (TypeError, ValueError):
        return None
    if pt <= 0:
        return None
    ts = rv.get("timestamp_updated") or rv.get("timestamp_created") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = 0
    return str(rid), {"pt": pt, "up": bool(rv.get("voted_up")), "ts": ts}


# --------------------------------------------------------------------------- #
# Scrape one game: grow (new + deeper) + refresh-on-revisit, with the G1 cap
# --------------------------------------------------------------------------- #
def scrape_game(appid, stored_reviews, target):
    """Walk appreviews newest-first, updating `stored_reviews` (a dict keyed by
    recommendationid) IN PLACE. Returns (added, refreshed, exhausted).

      * unseen id           -> add (respecting the per-game cap via compaction)
      * known id            -> refresh its playtime (drift fix); count toward the
                               consecutive-seen streak that decides when to stop
      * stop growing when   -> we hit SEEN_STREAK_STOP consecutive known ids AND
                               we already hold >= target (new reviews all caught),
                               OR Steam runs out of reviews, OR we hit the cap.
    """
    added = refreshed = 0
    seen_streak = 0
    exhausted = False
    cursor = "*"
    pages = 0
    # Absolute page ceiling so a pathological loop can't run forever. Cap governs
    # total stored; this governs total *walked* in one run.
    max_pages = (PER_GAME_CAP // PER_PAGE) + 5

    while pages < max_pages:
        data = get(f"https://store.steampowered.com/appreviews/{appid}",
                   params={"json": 1, "language": "all", "purchase_type": "all",
                           "num_per_page": PER_PAGE, "filter": "recent",
                           "cursor": cursor})
        time.sleep(STEAM_DELAY)
        if not isinstance(data, dict) or data.get("success") != 1:
            break
        reviews = data.get("reviews") or []
        if not reviews:
            exhausted = True
            break

        for rv in reviews:
            parsed = _parse_review(rv)
            if parsed is None:
                continue
            rid, rec = parsed
            if rid in stored_reviews:
                # Known review -> refresh its (possibly-grown) playtime for free.
                if stored_reviews[rid]["pt"] != rec["pt"] or stored_reviews[rid]["ts"] != rec["ts"]:
                    stored_reviews[rid] = rec
                    refreshed += 1
                seen_streak += 1
            else:
                # New review -> add, enforcing the cap (drop oldest if full).
                if len(stored_reviews) >= PER_GAME_CAP:
                    _evict_oldest(stored_reviews)
                stored_reviews[rid] = rec
                added += 1
                seen_streak = 0        # reset: we're still finding new reviews

        # Stop conditions -------------------------------------------------- #
        # Once we've caught all the new reviews (a solid streak of known ids) and
        # we already hold enough for a stable median, there's no reason to keep
        # walking deeper on a routine run.
        if seen_streak >= SEEN_STREAK_STOP and len(stored_reviews) >= min(target, PER_GAME_CAP):
            break
        if len(stored_reviews) >= PER_GAME_CAP:
            break                      # cap reached; ring-buffer holds newest CAP

        next_cursor = data.get("cursor")
        pages += 1
        if not next_cursor or next_cursor == cursor:
            exhausted = True           # Steam's end-of-list sentinel
            break
        cursor = next_cursor
        if len(reviews) < PER_PAGE:
            exhausted = True
            break

    return added, refreshed, exhausted


def _evict_oldest(stored_reviews):
    """G1 ring-buffer: drop the single oldest review (smallest ts) to free a slot.
    Recent reviews matter most for a current median, so the oldest are the safest
    to shed when at the cap."""
    if not stored_reviews:
        return
    oldest_rid = min(stored_reviews, key=lambda k: stored_reviews[k].get("ts", 0))
    del stored_reviews[oldest_rid]


# --------------------------------------------------------------------------- #
# Summary computed alongside the raw store (so the big file is self-describing;
# playtime_summarize.py recomputes from raw, but keeping a summary here makes the
# big file inspectable).
# --------------------------------------------------------------------------- #
def _median_or_none(values):
    if len(values) < MIN_SEGMENT_FOR_MEDIAN:
        return None
    return int(round(statistics.median(values)))


def summarize_game(stored_reviews):
    up = [r["pt"] for r in stored_reviews.values() if r["up"]]
    down = [r["pt"] for r in stored_reviews.values() if not r["up"]]
    combined = up + down
    return {
        "median_up": _median_or_none(up),
        "median_down": _median_or_none(down),
        "median_all": _median_or_none(combined),
        "n_up": len(up), "n_down": len(down), "n_all": len(combined),
    }


# --------------------------------------------------------------------------- #
# State (the BIG file)
# --------------------------------------------------------------------------- #
# Shape:
# { "generated_at": ts, "per_game_cap": 1000,
#   "games": { "<appid>": {
#        "reviews": { "<recommendationid>": {"pt":int,"up":bool,"ts":int}, ... },
#        "summary": {...},                # convenience mirror of summarize_game
#        "exhausted": bool, "scraped_at": ts } } }
def load_games():
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return d.get("games", [])


def load_raw():
    if RAW_FILE.exists():
        try:
            d = json.loads(RAW_FILE.read_text(encoding="utf-8"))
            return d.get("games", {})
        except ValueError:
            pass
    return {}


def save_raw(games):
    """Compact separators — this file is machine-only (the frontend reads the
    summarized playtime.json, never this)."""
    RAW_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "per_game_cap": PER_GAME_CAP,
         "count": len(games), "games": games},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def git_commit_raw(msg):
    """G2: commit the big file ONCE (at run-end). Rebase first so it never fights
    other jobs' pushes (different files => clean replay)."""
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "playtime_raw.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            for _attempt in range(1, 9):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log(f"  committed: {msg}")
                    break
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git commit failed: {e}")


# --------------------------------------------------------------------------- #
# Eligibility + priority
# --------------------------------------------------------------------------- #
def effective_target():
    return min(PER_GAME_CAP, max(TARGET_REVIEWS, DEEPEN_TARGET) if DEEPEN_TARGET else TARGET_REVIEWS)


def is_eligible(rec, last_update_ts, review_count, now, target):
    """Eligible if never scraped; OR holding fewer than target AND not exhausted
    (more to gather); OR past cooldown (to catch NEW reviews + refresh drift).

    Gated by MIN_REVIEWS_FLOOR: games below the floor can't produce a usable
    sentiment-split median, so they're skipped regardless of the above until their
    live review_count crosses the floor (re-checked every run — not permanent)."""
    if (review_count or 0) < MIN_REVIEWS_FLOOR:
        return False
    if not rec:
        return True
    held = len((rec.get("reviews") or {}))
    if held < target and not rec.get("exhausted"):
        return True
    age = now - rec.get("scraped_at", 0)
    actively_updated = last_update_ts and (now - last_update_ts) <= UPDATE_ACTIVE_DAYS * 86400
    cooldown = COOLDOWN_DAYS if actively_updated else NOUPDATE_COOLDOWN_DAYS
    return age >= cooldown * 86400


def priority(rec, last_update_ts, all_time_count, now, target):
    score = 0.0
    if not rec:
        score += 1000
    else:
        held = len((rec.get("reviews") or {}))
        if held < target and not rec.get("exhausted"):
            score += 500
    if last_update_ts:
        days = (now - last_update_ts) / 86400
        score += 300 if days <= 30 else 150 if days <= 90 else 50 if days <= 365 else 0
    if all_time_count is not None and all_time_count < 10:
        score -= 300
    if rec:
        score += min(200, (now - rec.get("scraped_at", 0)) / 86400)
    return score


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    games = load_games()
    if not games:
        log("No real games.json yet (run scraper.py first); nothing to refresh.")
        return 0
    raw = load_raw()
    now = int(time.time())
    target = effective_target()

    cands = []
    for g in games:
        aid = str(g["appid"])
        rec = raw.get(aid, {})
        lu = g.get("last_update_ts")
        if is_eligible(rec, lu, g.get("review_count"), now, target):
            cands.append((priority(rec, lu, g.get("review_count"), now, target),
                          int(aid), lu))
    cands.sort(reverse=True)

    mode = f"DEEPEN to {target}" if DEEPEN_TARGET else f"target {target}"
    log(f"Catalog {len(games)} | raw has {len(raw)} | eligible now: {len(cands)}")
    log(f"Budget: {RUN_MINUTES} min · {mode}/game · cap {PER_GAME_CAP} · delay {STEAM_DELAY}s · "
        f"cooldown {COOLDOWN_DAYS}d (dormant {NOUPDATE_COOLDOWN_DAYS}d)")

    budget = RUN_MINUTES * 60
    last_commit = time.time()
    done = tot_added = tot_refreshed = 0
    for _score, aid, _lu in cands:
        if budget - (time.time() - start) < TIME_BUFFER:
            # Graceful time-budget stop. Commit what we have before breaking so a
            # long run always persists its work even if it never exhausts the queue
            # (belt-and-suspenders alongside the periodic commit below).
            log("Time budget reached; committing and wrapping up.")
            save_raw(raw)
            git_commit_raw(f"playtime raw: budget stop, {done} games this run "
                           f"(+{tot_added} reviews, ~{tot_refreshed} refreshed; {len(raw)} tracked)")
            last_commit = time.time()
            break
        aids = str(aid)
        rec = raw.get(aids, {})
        reviews = dict(rec.get("reviews") or {})     # mutate a copy, store on success
        added, refreshed, exhausted = scrape_game(aid, reviews, target)
        summary = summarize_game(reviews)
        raw[aids] = {"reviews": reviews, "summary": summary,
                     "exhausted": exhausted, "scraped_at": int(time.time())}
        done += 1; tot_added += added; tot_refreshed += refreshed
        mu, md = summary["median_up"], summary["median_down"]
        mu_h = f"{mu/60:.1f}h" if mu is not None else "—"
        md_h = f"{md/60:.1f}h" if md is not None else "—"
        log(f"  {aid:>8}: fans {mu_h} (n={summary['n_up']}) · det {md_h} (n={summary['n_down']})"
            f" · +{added} new, ~{refreshed} refreshed, held {summary['n_all']}")

        # G2 (revised): commit to GIT periodically — not just to ephemeral disk —
        # so an interrupted run persists its progress to the repo (see COMMIT_SECONDS).
        if time.time() - last_commit > COMMIT_SECONDS:
            save_raw(raw)
            git_commit_raw(f"playtime raw: checkpoint, {done} games so far "
                           f"(+{tot_added} reviews, ~{tot_refreshed} refreshed; {len(raw)} tracked)")
            last_commit = time.time()

    # Final commit at run-end (covers the normal case where the loop drains the
    # queue before the time budget; no-op commit is skipped inside git_commit_raw
    # when there's nothing new since the last checkpoint).
    save_raw(raw)
    git_commit_raw(f"playtime raw: {done} games this run "
                   f"(+{tot_added} reviews, ~{tot_refreshed} refreshed; {len(raw)} tracked)")
    log(f"\nDone. {done} games this run: +{tot_added} new reviews, ~{tot_refreshed} refreshed. "
        f"{len(raw)} games tracked total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
