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
G2 — low commit churn: this script commits each shard periodically DURING the
     run (every ~30 min) plus once when that shard is finished, rather than on
     every checkpoint.
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
RAW_FILE = HERE / "playtime_raw.json"            # LEGACY monolith — split into shards on first run

# --- sharding (GitHub's 100 MB/file hard limit) ---------------------------- #
# A single playtime_raw.json crossed GitHub's 100 MB file-size limit at ~6,850 games
# (98 MB), which made every push get rejected — and because the commit code swallowed
# the error, runs went green with nothing committed and the whole pipeline silently
# froze. The full addressable set (~78k games) would be ~1.1 GB in one file, so the
# per-review working set is split into shards under playtime_raw/NN.json, keyed by
# (appid // 10) % NSHARDS. Each shard tops out ~18 MB at full coverage (raise NSHARDS to
# shrink further). A run processes SEVERAL buckets, chosen hottest-first by how many of
# their games are due under the refresh ladder, with the run-number bucket always
# included as a starvation guard (see MAX_SHARDS_PER_RUN below). Each shard is loaded,
# worked and committed one at a time, so commits stay small, peak memory stays at ~one
# shard, and only a single writer ever touches a given shard.
NSHARDS = 64
SHARD_DIR = HERE / "playtime_raw"                # directory of NN.json shards (replaces the monolith)
# Shard-key version. Bump this whenever shard_of() changes — ensure_sharding() reads the
# version stamped in the shards and does a one-time reshard when it doesn't match.
SHARD_KEY_VER = 2

def shard_of(appid):
    # Steam appids are ~100% multiples of 10, so `appid % NSHARDS` piles every game into
    # the even buckets (odd buckets get ~nothing) — half the shards wasted, the rest 2x
    # size. Dividing by 10 first strips that factor, so the quotient spreads evenly across
    # all 64 buckets (measured max/mean ~1.05, 0 empty).
    return (int(appid) // 10) % NSHARDS

def shard_path(bucket):
    return SHARD_DIR / f"{bucket:02d}.json"

def rotation_bucket():
    """The round-robin bucket for this run, by CI run number. Retained as the
    STARVATION GUARD anchor: whatever the hot-first scheduler picks, this bucket is
    always included so every shard is still guaranteed a visit every ~64 runs even if
    it never scores hot. Local / manual runs (no GITHUB_RUN_NUMBER) default to 0."""
    try:
        return int(os.environ.get("GITHUB_RUN_NUMBER", "0")) % NSHARDS
    except ValueError:
        return 0


# --- multi-shard, hot-first scheduling ------------------------------------- #
# WHY THIS EXISTS. The original design processed exactly ONE shard per run
# (GITHUB_RUN_NUMBER % 64), which capped any individual game's refresh cadence at
# "once per 64 runs" — ~8 days at 8 runs/day — no matter how hot it was. That made
# the age-tier ladder below (0-7d -> 1d cooldown) unreachable in practice: a game due
# for a daily refresh sat in a shard that only opened every 8th day. Hot games are
# scattered uniformly across all 64 buckets (the shard key is a hash of appid, which
# has nothing to do with release date or popularity), so a hot backlog is ALWAYS
# spread thin across every shard — exactly the worst case for one-bucket-per-run.
#
# THE FIX: a run now works MANY shards, chosen hottest-first. The load-bearing
# invariant was never "one shard per run" — it is ONE WRITER PER FILE, which still
# holds exactly: the `steam-playtime-raw` concurrency group guarantees no two raw runs
# overlap, so within a run we can open, mutate and commit as many shards as the time
# budget allows and no other job ever touches them.
#
# SCORING IS HEADER-ONLY. Scoring must not read shard bodies (~18 MB each, ~1.1 GB
# total — minutes of I/O before a single request goes out). Instead we score from
# games.json alone, which already carries everything the ladder needs (release date,
# review_count, last_update_ts) and is loaded once anyway. A shard's score is the count
# of DUE-BY-LADDER games mapping into it. The only per-shard disk touch during
# scheduling is a tiny header read (`count`), and even that is optional.
MAX_SHARDS_PER_RUN = int(os.environ.get("MAX_SHARDS_PER_RUN", "12"))
SHARD_MIN_HOT = int(os.environ.get("SHARD_MIN_HOT", "1"))   # skip shards with fewer due games than this

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

COOLDOWN_DAYS = 7                 # legacy flat cooldown (kept as the ladder's fallback)
NOUPDATE_COOLDOWN_DAYS = 30       # dormant games: refresh far less often
UPDATE_ACTIVE_DAYS = 90           # "recently updated" = patched within this many days

# --- age-tiered refresh ladder (ported from the review-refresh ladder, ARCHITECTURE §6) --- #
# A flat 7d/30d cooldown treats a game released yesterday the same as one from 2019.
# But review playtime moves fastest exactly where the flat gate is slowest: a brand-new
# release accumulates its entire review corpus in the first days, and each reviewer's
# `playtime_forever` is still climbing. Waiting 7 days there means the medians the
# frontend shows for the most-searched games on the site are the stalest data we hold.
#
# So the cooldown now scales with RELEASE AGE, coarsely (4 tiers, not the 7-tier review
# ladder — playtime costs ~2-20 requests/game vs 2 for a review refresh, so a finer
# ladder at the top would blow the storefront budget for little gain):
#
#   released  0-7d   -> 1d    the corpus is still forming; refresh daily
#   released  7-30d  -> 3d    still moving, but the shape is set
#   released 30-90d  -> 7d    matches the old "actively updated" cooldown
#   older            -> 30d   matches the old dormant cooldown
#
# REVIEW-COUNT BOOST: a game with >1k all-time reviews has both the most churn and the
# most site traffic, so each tier's cooldown is HALVED for it. This is what keeps a
# popular older game (a perennial like a Souls title) fresher than a dead new release.
AGE_TIER_DAYS = [
    (7,   1),      # released within 7 days   -> 1-day cooldown
    (30,  3),      # within 30 days           -> 3-day
    (90,  7),      # within 90 days           -> 7-day
]
AGE_TIER_FALLBACK_DAYS = 30        # older than the last edge
HOT_REVIEWS_BOOST = 1000           # >this many all-time reviews halves the cooldown
HOT_BOOST_FACTOR = 0.5
MIN_COOLDOWN_HOURS = 12            # floor: never re-walk the same game twice in 12h

# POPULARITY FLOOR — aligns playtime cadence with the HLTB fast lane (hltb_refresh.py
# POPULAR_TIERS / ARCHITECTURE §8). The age ladder above keys on RELEASE AGE, so a
# popular perennial (>1k reviews, years old) lands on the 30-day back-catalogue tier —
# even halved that is 15 days. But HLTB re-checks those exact games every 5 days, and
# both signals are driven by the same live player population: if HLTB submissions are
# worth a 5-day look, the reviewers' `playtime_forever` is churning just as fast. Left
# alone this is the very "most-viewed games refreshed least often" anti-pattern the HLTB
# fix was built to kill — playtime just inherited it on the old tiers.
#
# So each game's age-tier cooldown is min()'d against a review-count tier, IDENTICAL in
# shape to HLTB's. min() semantics mean it can only ever pull a refresh FORWARD, never
# delay one: it bites only where the age ladder is too slow for a high-traffic game (the
# 30-90d and older tiers), and leaves the aggressive fresh-release fast lane (halved to
# 12h-1.5d) untouched — those are already faster than the floor. Net effect is a
# REDISTRIBUTION toward hot back-catalogue games within the same rate envelope (the
# overdue-ratio priority keeps genuinely-new releases ahead of them), not more requests.
POPULAR_FLOOR_TIERS = [
    (1000, 5),      # >1000 all-time reviews -> refresh at least every 5 days  (HLTB: 5d)
    (500, 10),      # >500                   -> at least every 10 days         (HLTB: 10d)
]


def popular_floor_days(review_count):
    """The popularity-tier cooldown FLOOR (in days) for a game, or None when it isn't
    popular enough to qualify. Mirrors hltb_refresh.popular_window_days so the two jobs
    re-check the same hot games on the same cadence. Strict `>` matches the HLTB edge."""
    rc = review_count or 0
    for edge, days in POPULAR_FLOOR_TIERS:
        if rc > edge:
            return days
    return None


def cooldown_days(released_ts, last_update_ts, review_count, now):
    """The refresh cooldown (in DAYS, may be fractional) for one game.

    Primary axis is release age via AGE_TIER_DAYS. When `released_ts` is missing (a
    sizeable slice of the catalog has no parsed release date), we fall back to the
    legacy last_update_ts behaviour so those games are never treated as brand-new and
    hammered — unknown age is the conservative case, not the eager one.

    The >1k-review boost halves whichever tier applies; the popularity FLOOR then
    min()'s that against a review-count tier (aligning with HLTB), so a popular
    back-catalogue game can't sit on the slow older tiers. Both are bounded below by
    MIN_COOLDOWN_HOURS so no game can be re-walked more than twice a day even at the top
    of the ladder. Pure function — unit-testable, no I/O."""
    base = None
    if released_ts:
        age_days = (now - released_ts) / 86400.0
        if age_days >= 0:                       # guard against future-dated releases
            for edge, days in AGE_TIER_DAYS:
                if age_days < edge:
                    base = days
                    break
            if base is None:
                base = AGE_TIER_FALLBACK_DAYS
    if base is None:
        # No usable release date -> legacy behaviour keyed off patch activity.
        actively_updated = last_update_ts and (now - last_update_ts) <= UPDATE_ACTIVE_DAYS * 86400
        base = COOLDOWN_DAYS if actively_updated else NOUPDATE_COOLDOWN_DAYS
    if (review_count or 0) > HOT_REVIEWS_BOOST:
        base *= HOT_BOOST_FACTOR
    # Popularity floor: only ever pulls the cooldown FORWARD (min), keeping high-traffic
    # games on the same cadence HLTB re-checks them. Applies on ANY axis (age-tier or the
    # legacy no-release-date path), so a popular game with no parsed release date is
    # rescued from the 30-day dormant cooldown too.
    pop = popular_floor_days(review_count)
    if pop is not None:
        base = min(base, pop)
    return max(base, MIN_COOLDOWN_HOURS / 24.0)


def _released_ts(g):
    """Best-effort release timestamp from a games.json record. The catalog has carried
    a few different key spellings over its life, so we probe them in order rather than
    hard-coding one and silently returning None for older rows. Non-numeric or absent
    -> None, which cooldown_days() treats as 'unknown age' (conservative)."""
    for key in ("released_ts", "release_ts", "release_date_ts", "released_at"):
        v = g.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None
MIN_SEGMENT_FOR_MEDIAN = 3        # below this many samples, a segment median is null
# Hard eligibility floor: a sentiment-split median needs a usable sample. Games
# with fewer than this many all-time reviews can't produce one (they'd null out at
# MIN_SEGMENT_FOR_MEDIAN anyway), so we don't spend request budget on them. This is
# "skip for now", NOT permanent exclusion: eligibility is re-checked every run
# against the live review_count from games.json, so a game re-qualifies the moment
# it crosses the floor. Removes ~41k unusable games from the queue, leaving budget
# for the ~52k that can actually yield data.
MIN_REVIEWS_FLOOR = 10

STEAM_DELAY = 1.5                 # storefront limit (~200/5min); at 1.5s this run sits AT the
                                  # ceiling (no headroom). Matches recent_refresh.py, which
                                  # already sustains 3h storefront passes at 1.5s — so this is
                                  # proven viable on a dedicated runner. Was 2.0 (150/5min, 50
                                  # headroom); dropped for the 8-slot aggressive backfill. If 403
                                  # log lines spike after deploy, revert to 2.0.
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


def load_shard(bucket):
    p = shard_path(bucket)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("games", {})
        except ValueError:
            pass
    return {}


def save_shard(bucket, games):
    """Compact separators — machine-only (the frontend reads the summarized
    playtime.json, never the shards)."""
    SHARD_DIR.mkdir(exist_ok=True)
    shard_path(bucket).write_text(json.dumps(
        {"generated_at": int(time.time()), "per_game_cap": PER_GAME_CAP,
         "bucket": bucket, "nshards": NSHARDS, "shard_ver": SHARD_KEY_VER,
         "count": len(games), "games": games},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _shard_ver_on_disk():
    """The shard_ver stamped in the existing shards (0 if shards exist but predate the
    stamp; None if there are no shards yet)."""
    if not SHARD_DIR.is_dir():
        return None
    for p in sorted(SHARD_DIR.glob("*.json")):
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("shard_ver", 0)
        except ValueError:
            continue
    return None


def ensure_sharding():
    """Idempotently guarantee the shards exist AND use the current shard key. Handles two
    one-time events with the same code path: (1) the initial split of the legacy
    playtime_raw.json monolith, and (2) a RESHARD when shard_of() changes (detected via
    SHARD_KEY_VER). Fast no-op on the common path once shards are current — so it's safe
    to call every run. Doing it here (not a separate script) means a plain file upload is
    all it takes; the next run self-migrates."""
    monolith = RAW_FILE.exists()
    ver = _shard_ver_on_disk()
    if ver == SHARD_KEY_VER and not monolith:
        return                                   # already current

    # Gather every game from wherever it currently lives (monolith and/or existing shards).
    allgames = {}
    if monolith:
        try:
            allgames.update(json.loads(RAW_FILE.read_text(encoding="utf-8")).get("games", {}))
        except ValueError:
            log("  legacy monolith unreadable; skipping it")
    if SHARD_DIR.is_dir():
        for p in sorted(SHARD_DIR.glob("*.json")):
            try:
                allgames.update(json.loads(p.read_text(encoding="utf-8")).get("games", {}))
            except ValueError:
                continue
    if not allgames:
        return                                   # nothing to (re)shard yet — fresh repo

    reason = "monolith split" if monolith else f"reshard to key v{SHARD_KEY_VER}"
    log(f"(Re)sharding {len(allgames):,} games ({reason}) across {NSHARDS} buckets...")
    buckets = {}
    for aid, rec in allgames.items():
        buckets.setdefault(shard_of(aid), {})[aid] = rec
    for n in range(NSHARDS):
        save_shard(n, buckets.get(n, {}))        # rewrites ALL shards with the current key
    if monolith:
        RAW_FILE.unlink(missing_ok=True)
    _robust_commit(f"playtime raw: {reason} ({len(allgames)} games -> {NSHARDS} buckets, key v{SHARD_KEY_VER})",
                   [f"{n:02d}.json" for n in range(NSHARDS)], drop_monolith=monolith)


def _robust_commit(msg, our_shards, drop_monolith=False):
    """Robust single-writer push. `our_shards` are the shard filenames THIS run wrote —
    only these are re-applied after we hard-reset to origin/main, so concurrent
    other-bucket shards on main are never clobbered. Because only this job writes each
    shard, the hard reset can never conflict or wedge a rebase (the old code used
    `git rebase --autostash` with `check=False` and no `--abort`, which could stick the
    repo mid-rebase and then fail every push SILENTLY — a green run that committed
    nothing). Retries with backoff, logs the real error, and FAILS LOUD (non-zero exit)
    if it still can't land, so a broken push is a visible RED run."""
    if not IN_ACTIONS:
        return
    snaps = {name: (SHARD_DIR / name).read_bytes()
             for name in our_shards if (SHARD_DIR / name).exists()}
    last_err = ""
    for attempt in range(1, 9):
        try:
            subprocess.run(["git", "fetch", "origin", "main"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "reset", "--hard", "origin/main"],
                           check=True, capture_output=True, text=True)   # latest remote tree
            SHARD_DIR.mkdir(exist_ok=True)
            for name, data in snaps.items():                              # re-apply only our shard(s)
                (SHARD_DIR / name).write_bytes(data)
            if drop_monolith:
                subprocess.run(["git", "rm", "-f", "--ignore-unmatch", "playtime_raw.json"],
                               check=False, capture_output=True, text=True)
                RAW_FILE.unlink(missing_ok=True)
            for name in snaps:
                subprocess.run(["git", "add", f"playtime_raw/{name}"], check=True)
            if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
                log("  (nothing new vs remote; skipping commit)")
                return
            subprocess.run(["git", "commit", "-m", msg],
                           check=True, capture_output=True, text=True)
            push = subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True)
            if push.returncode == 0:
                log(f"  committed: {msg}")
                return
            last_err = (push.stderr or push.stdout or "").strip()
            log(f"  push attempt {attempt}/8 rejected: {last_err[:180]}")
        except subprocess.CalledProcessError as e:
            last_err = ((e.stderr or "") + (e.stdout or "")).strip() or str(e)
            log(f"  git attempt {attempt}/8 error: {last_err[:180]}")
        time.sleep(2 * attempt + random.uniform(0, 2))
    log(f"  ERROR: playtime raw commit failed after 8 attempts — {last_err[:200]}")
    sys.exit(1)                                          # visible RED run, not a silent green one


def git_commit_shard(bucket, msg):
    _robust_commit(msg, [f"{bucket:02d}.json"])


# --------------------------------------------------------------------------- #
# Eligibility + priority
# --------------------------------------------------------------------------- #
def effective_target():
    return min(PER_GAME_CAP, max(TARGET_REVIEWS, DEEPEN_TARGET) if DEEPEN_TARGET else TARGET_REVIEWS)


def is_eligible(rec, released_ts, last_update_ts, review_count, now, target):
    """Eligible if never scraped; OR holding fewer than target AND not exhausted
    (more to gather); OR past its LADDER cooldown (to catch NEW reviews + refresh drift).

    Gated by MIN_REVIEWS_FLOOR: games below the floor can't produce a usable
    sentiment-split median, so they're skipped regardless of the above until their
    live review_count crosses the floor (re-checked every run — not permanent).

    The cooldown is no longer the flat 7d/30d pair — it comes from cooldown_days(),
    which tiers by release age and halves for >1k-review games (see the ladder above)."""
    if (review_count or 0) < MIN_REVIEWS_FLOOR:
        return False
    if not rec:
        return True
    held = len((rec.get("reviews") or {}))
    if held < target and not rec.get("exhausted"):
        return True
    age = now - rec.get("scraped_at", 0)
    return age >= cooldown_days(released_ts, last_update_ts, review_count, now) * 86400


def priority(rec, released_ts, last_update_ts, all_time_count, now, target):
    """Ordering score WITHIN a shard. Higher = worked first.

    The dominant term is now OVERDUE RATIO — how many multiples of its own cooldown a
    game is past due (age / cooldown). That makes the ladder self-balancing: a
    1-day-cooldown new release 2 days stale outranks a 30-day-cooldown back-catalogue
    game 40 days stale, because the former is proportionally further behind the promise
    the ladder makes about it. Using a raw age here (the old behaviour) would do the
    opposite and let ancient dormant games crowd out the fast lane forever."""
    score = 0.0
    if not rec:
        score += 1000                       # never scraped -> always first
    else:
        held = len((rec.get("reviews") or {}))
        if held < target and not rec.get("exhausted"):
            score += 500                    # incomplete corpus -> still filling

    cd = cooldown_days(released_ts, last_update_ts, all_time_count, now)
    if rec:
        age_days = (now - rec.get("scraped_at", 0)) / 86400.0
        overdue_ratio = age_days / cd if cd > 0 else 0
        score += min(400, overdue_ratio * 100)    # capped so it can't swamp the flags above

    # Fast-lane bonuses: recent release and/or high review volume.
    if released_ts:
        rel_days = (now - released_ts) / 86400.0
        if 0 <= rel_days < 7:
            score += 300
        elif rel_days < 30:
            score += 150
        elif rel_days < 90:
            score += 50
    if (all_time_count or 0) > HOT_REVIEWS_BOOST:
        score += 120                        # popular -> more churn, more site traffic
    if all_time_count is not None and all_time_count < 10:
        score -= 300
    return score


def build_candidates(games, now, target, raw_by_bucket=None, buckets=None):
    """Score every catalog game against the ladder and group DUE ones by shard.

    Returns {bucket: [(score, appid, released_ts, last_update_ts, review_count), ...]}
    with each bucket's list sorted hottest-first.

    Called in two modes:
      * SCHEDULING (raw_by_bucket=None): shard bodies are NOT loaded, so `rec` is
        unknown and treated as {}. That over-counts slightly — a game whose shard record
        is already fresh still scores as due — but it's a header-free O(catalog) pass
        over data already in memory, which is the whole point: picking which shards to
        open must never require opening them. The exact per-game gate is re-applied for
        real once a shard is loaded.
      * EXECUTION (raw_by_bucket supplied): the true eligibility test, with each game's
        stored record in hand.
    `buckets`, when given, restricts scoring to those shard ids."""
    out = {}
    for g in games:
        aid = g["appid"]
        b = shard_of(aid)
        if buckets is not None and b not in buckets:
            continue
        rc = g.get("review_count")
        if (rc or 0) < MIN_REVIEWS_FLOOR:
            continue                        # cheap reject before any further work
        rel = _released_ts(g)
        lu = g.get("last_update_ts")
        rec = {}
        if raw_by_bucket is not None:
            rec = (raw_by_bucket.get(b) or {}).get(str(aid), {})
        if not is_eligible(rec, rel, lu, rc, now, target):
            continue
        out.setdefault(b, []).append(
            (priority(rec, rel, lu, rc, now, target), int(aid), rel, lu, rc))
    for b in out:
        out[b].sort(key=lambda t: -t[0])
    return out


def select_buckets(due_by_bucket, anchor):
    """Choose which shards this run works, hottest-first.

    Ranking is by DUE-GAME COUNT: the shard holding the most overdue games clears the
    most backlog per shard-open, and shard opens are the expensive unit here (an ~18 MB
    read + a commit each). `anchor` — the round-robin bucket for this run number — is
    ALWAYS included regardless of score. That is the starvation guard: without it a
    shard that never scores hot could go unvisited indefinitely, and its games would
    rot no matter what the ladder says. With it, every shard is still guaranteed a
    visit at least every NSHARDS runs, exactly as under the old rotation."""
    ranked = sorted(due_by_bucket.items(), key=lambda kv: -len(kv[1]))
    chosen = [b for b, items in ranked if len(items) >= SHARD_MIN_HOT][:MAX_SHARDS_PER_RUN]
    if anchor not in chosen:
        chosen = chosen[:max(0, MAX_SHARDS_PER_RUN - 1)] + [anchor]
    return chosen


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    games = load_games()
    if not games:
        log("No real games.json yet (run scraper.py first); nothing to refresh.")
        return 0

    ensure_sharding()                     # one-time: split monolith and/or reshard on key change; no-op afterwards

    now = int(time.time())
    target = effective_target()
    anchor = rotation_bucket()

    # --- Phase 1: SCHEDULE (no shard bodies read) --------------------------- #
    # Score the whole catalog against the ladder purely from games.json, group the due
    # games by shard, and pick the hottest shards. Reading no shard bodies here is the
    # point: choosing among 64 x ~18 MB files must not cost 1.1 GB of I/O.
    sched = build_candidates(games, now, target)
    chosen = select_buckets(sched, anchor)
    total_due = sum(len(v) for v in sched.values())

    mode = f"DEEPEN to {target}" if DEEPEN_TARGET else f"target {target}"
    log(f"Catalog {len(games)} | due by ladder: ~{total_due} across {len(sched)} shard(s)")
    log(f"Working {len(chosen)} shard(s) this run (max {MAX_SHARDS_PER_RUN}, "
        f"anchor b{anchor:02d} always included): "
        + ", ".join(f"b{b:02d}(~{len(sched.get(b, []))})" for b in chosen))
    log(f"Budget: {RUN_MINUTES} min · {mode}/game · cap {PER_GAME_CAP} · delay {STEAM_DELAY}s · "
        f"ladder 0-7d/1d · 7-30d/3d · 30-90d/7d · else {AGE_TIER_FALLBACK_DAYS}d "
        f"(>{HOT_REVIEWS_BOOST} reviews: halved; popularity floor >1k/5d, >500/10d — HLTB-aligned)")

    budget = RUN_MINUTES * 60
    grand_done = grand_added = grand_refreshed = 0
    shards_touched = 0

    def time_left():
        return budget - (time.time() - start)

    # --- Phase 2: EXECUTE, one shard at a time ------------------------------ #
    # Each shard is loaded, worked, and committed before the next is opened, so peak
    # memory stays at ~one shard (never 64) and an interrupted run has already
    # persisted every completed shard. One writer per file is untouched: this job is
    # still the sole writer of playtime_raw/, and the concurrency group means no other
    # run of it is alive at the same time.
    for bucket in chosen:
        if time_left() < TIME_BUFFER:
            log("Time budget reached; stopping before opening another shard.")
            break
        raw = load_shard(bucket)
        # Re-score THIS shard's games for real, now that we hold their stored records.
        # The scheduling pass deliberately guessed (no bodies); this is the exact gate.
        cands = build_candidates(games, now, target,
                                 raw_by_bucket={bucket: raw}, buckets={bucket}).get(bucket, [])
        if not cands:
            log(f"  b{bucket:02d}: nothing actually due once the shard was read; skipping.")
            continue
        shards_touched += 1
        log(f"[b{bucket:02d}] shard holds {len(raw)} games · {len(cands)} due")

        last_commit = time.time()
        done = tot_added = tot_refreshed = 0
        hit_budget = False
        for _score, aid, _rel, _lu, _rc in cands:
            if time_left() < TIME_BUFFER:
                hit_budget = True
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

            # G2: commit to GIT periodically — not just to ephemeral disk — so an
            # interrupted run persists its progress to the repo (see COMMIT_SECONDS).
            if time.time() - last_commit > COMMIT_SECONDS:
                save_shard(bucket, raw)
                git_commit_shard(bucket, f"playtime raw b{bucket:02d}: checkpoint, {done} games so far "
                                 f"(+{tot_added} reviews, ~{tot_refreshed} refreshed; shard {len(raw)})")
                last_commit = time.time()

        # Always commit this shard before moving to the next one (or exiting), so no
        # completed shard's work is ever left only on the ephemeral runner disk.
        save_shard(bucket, raw)
        tag = "budget stop" if hit_budget else "complete"
        git_commit_shard(bucket, f"playtime raw b{bucket:02d}: {tag}, {done} games this run "
                         f"(+{tot_added} reviews, ~{tot_refreshed} refreshed; shard {len(raw)})")
        grand_done += done; grand_added += tot_added; grand_refreshed += tot_refreshed
        log(f"[b{bucket:02d}] done: {done} games (+{tot_added} new, ~{tot_refreshed} refreshed)")
        if hit_budget:
            log("Time budget reached; wrapping up.")
            break

    log(f"\nDone. {shards_touched} shard(s), {grand_done} games this run: "
        f"+{grand_added} new reviews, ~{grand_refreshed} refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
