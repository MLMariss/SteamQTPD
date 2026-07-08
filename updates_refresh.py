#!/usr/bin/env python3
"""
Steam QTPD — update-events refresher (sharded, event_type-classified)
====================================================================
A SEPARATE, independent job from scraper.py. For each game it records the
*history of update posts* — each with a Steam-native MAJOR/MINOR classification —
so the frontend can answer "how many big vs small updates in the last month / 3mo /
6mo / year / over a year", always against *today's* date, with no staleness.

WHY THIS EXISTS (vs scraper.py's last_update_ts)
------------------------------------------------
scraper.py already fills a single `last_update_ts` in games.json from the News API
(ISteamNews/GetNewsForApp). That is a cheap, high-budget signal but it is only ONE
timestamp and it has NO magnitude — the News feed doesn't expose the update's size.

The store *events* endpoint DOES. Each partner event carries an integer `event_type`.
Steam defines THREE update categories, and we keep all three rather than collapse them:
    12 = Small Update / Patch Notes   -> MINOR   (smallest, routine)
    14 = Regular Update               -> REGULAR (normal meaningful update, the middle tier)
    13 = Major Update                 -> MAJOR   ("biggest moments of the year")
Everything else (sales=20/21/23, streams, announcements, cross-promo, ...) is NOT an
update and is discarded here. This is Valve's own taxonomy, not a keyword guess, so
the tiering is authoritative. Storing all three keeps every downstream grouping open
(the frontend can merge major+regular into "big" if it likes) with no re-scrape.

Trade-off: this endpoint lives on store.steampowered.com — the STOREFRONT rate budget
that scraper.py and price_and_sale.py already compete for. So this is deliberately its
own out-of-band, time-boxed, one-bucket-per-run job (like playtime_refresh.py), never
folded into the main scraper, so it can't starve the price/catalog pipelines.

ONE WRITER PER FILE
-------------------
  * scraper.py            -> games.json        (catalog, last_update_ts, ...)
  * price_and_sale.py     -> prices.json
  * playtime_refresh.py   -> playtime_raw/NN.json
  * THIS                  -> updates_raw/NN.json   (per-game dated+typed event list)
  * updates_summarize.py  -> updates.json          (small frontend file: last_*_ts +
                                                     window counts, recomputed from raw)

WHY RAW DATED EVENTS (not pre-computed counts)
----------------------------------------------
"Updates in the last 30 days" rots every single day. If we stored counts, a game
scraped today would show stale counts a week later, and keeping them fresh would mean
constantly re-scraping the whole catalog — exactly the cost we want to avoid. Instead
we store the raw (ts, type) events ONCE; updates_summarize.py rolls them into windowed
counts, and the frontend can recompute the same windows client-side against the live
"now" so the numbers never drift regardless of when the game was last scraped. Re-scrape
is then only ever needed to catch NEW posts, not to keep old counts current.

SHARDED FROM DAY ONE
--------------------
A per-game dated event list, across ~120k games, is exactly the kind of file that blew
past GitHub's 100 MB wall for playtime. So updates_raw is sharded the same way from the
start: 64 buckets keyed (appid // 10) % 64 (dividing by 10 first because Steam appids are
~all multiples of 10 — `appid % 64` would pile everything into the even buckets). One run
processes ONE bucket, rotated by GITHUB_RUN_NUMBER, so commits stay small and only one
writer ever touches a given shard. shard_health.py already reports on any *_raw/ dir.

RESUMABILITY / SAFETY
---------------------
  * Identity-keyed by event `gid` (stable, never moves) — re-runs never double-count;
    a known gid means we already have that post.
  * REFRESH-ON-REVISIT: an event's type/time is updated in place if Steam changed it.
  * Per-game cap (PER_GAME_CAP) ring-buffers oldest events out, bounding file growth.
  * Commits every COMMIT_SECONDS *during* the run (ephemeral runners can be evicted),
    via the same hard-reset-to-origin single-writer push as playtime_refresh.py — so a
    failed push is a LOUD red run, never a silent green one.
  * A soft failure (endpoint 403/blip) skips that game this pass; the previous record is
    left untouched. Nothing is ever blanked by a transient error.

Run: `python updates_refresh.py`   (RUN_MINUTES, default 180, bounds the drain.)
"""
import glob
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"
SHARD_DIR = HERE / "updates_raw"                 # directory of NN.json shards

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
NSHARDS = 64
# Bump when shard_of() changes — ensure_sharding() reshards on mismatch.
SHARD_KEY_VER = 1

# Steam event_type -> our magnitude tier. Only these three are "updates". Steam itself
# defines THREE update categories, not two, so we preserve all three rather than collapse
# 14 into 13 — 14 (Regular) is a meaningful mid-tier update, bigger than a patch note but
# not a tentpole "biggest of the year" event. Storing the true tier keeps every downstream
# grouping choice open (the frontend can merge major+regular into "big" if it wants) without
# ever needing a re-scrape.
EVENT_TYPE_TIER = {
    13: "major",     # Major Update  — headline, "biggest moments of the year"
    14: "regular",   # Regular Update — normal meaningful update
    12: "minor",     # Small Update / Patch Notes — smallest, routine
}
UPDATE_EVENT_TYPES = set(EVENT_TYPE_TIER)

STORE_DELAY = float(os.environ.get("STORE_DELAY", "1.6"))   # between storefront calls (~200/5min)
EVENTS_COUNT = 50                # events pulled per game (newest-first); plenty of history
PER_GAME_CAP = 200               # max events stored per game (ring-buffer oldest out)
MAX_RETRIES = 4

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))
COMMIT_SECONDS = 1800            # 30 min: commit the shard this often during the run
TIME_BUFFER = 90                 # stop this many seconds before the wall to commit cleanly

# Eligibility cadence: re-scrape a game's events sooner if it's actively updated.
MIN_REVIEWS_FLOOR = 10           # skip near-invisible games until they matter (re-checked, not permanent)
UPDATE_ACTIVE_DAYS = 90          # "actively updated" if last_update_ts within this many days
COOLDOWN_DAYS = 7                # active games: re-check events weekly
NOUPDATE_COOLDOWN_DAYS = 45      # quiet games: re-check rarely (their history barely changes)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()  # not required (endpoint is keyless)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "QTPD-updates/1.0 (+github.com/MLMariss/SteamQHPP)"})


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# Sharding (mirrors playtime_refresh.py exactly)
# --------------------------------------------------------------------------- #
def shard_of(appid):
    # Divide by 10 first: Steam appids are ~all multiples of 10, so `appid % NSHARDS`
    # would pile every game into the even buckets. The quotient spreads evenly.
    return (int(appid) // 10) % NSHARDS


def shard_path(bucket):
    return SHARD_DIR / f"{bucket:02d}.json"


def bucket_for_run():
    """Rotate through buckets by CI run number so every shard is revisited in turn.
    Local/manual runs (no GITHUB_RUN_NUMBER) default to bucket 0."""
    try:
        return int(os.environ.get("GITHUB_RUN_NUMBER", "0")) % NSHARDS
    except ValueError:
        return 0


def load_shard(bucket):
    p = shard_path(bucket)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return d.get("games", {})


def save_shard(bucket, games):
    SHARD_DIR.mkdir(exist_ok=True)
    payload = {
        "bucket": bucket, "nshards": NSHARDS, "shard_ver": SHARD_KEY_VER,
        "generated_at": int(time.time()), "games": games,
    }
    shard_path(bucket).write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def _shard_ver_on_disk():
    """The shard_ver stamped in existing shards (0 if shards exist but predate versioning,
    None if there are no shards yet)."""
    for p in sorted(SHARD_DIR.glob("*.json")):
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("shard_ver", 0)
        except ValueError:
            continue
    return None


def ensure_sharding():
    """One-time reshard if the on-disk shard key version doesn't match the code. For v1
    (first ship) there's nothing to migrate — shards are created on first write. This
    hook exists so a future shard_of() change triggers a rebuild, same as playtime."""
    ver = _shard_ver_on_disk()
    if ver is None:
        log("no updates_raw shards yet — will be created on first write")
        return
    if ver != SHARD_KEY_VER:
        log(f"shard_ver {ver} != {SHARD_KEY_VER}: resharding updates_raw/ …")
        reshard_all()


def reshard_all():
    """Re-bucket every stored game under the current shard_of(). Reads all shards into
    memory (bounded by PER_GAME_CAP), re-distributes, rewrites all. Rare (only on key
    change) so simplicity beats cleverness."""
    everything = {}
    for p in sorted(SHARD_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        everything.update(d.get("games", {}))
    buckets = {}
    for appid, rec in everything.items():
        buckets.setdefault(shard_of(appid), {})[appid] = rec
    for b in range(NSHARDS):
        save_shard(b, buckets.get(b, {}))
    log(f"resharded {len(everything)} games into {NSHARDS} buckets")


# --------------------------------------------------------------------------- #
# Fetch + classify
# --------------------------------------------------------------------------- #
def _tier(event_type):
    """Steam event_type -> "major" | "regular" | "minor", or None if not an update."""
    return EVENT_TYPE_TIER.get(event_type)


def fetch_events(appid):
    """Pull this game's recent partner events, keep only real updates, classified.
    Returns a dict {gid: {"ts": posttime, "type": "major"|"minor", "et": raw_event_type}}
    or None on a transient failure (so the caller leaves the prior record untouched).
    Note the None-vs-{} distinction: {} means "fetched fine, no updates found"."""
    url = "https://store.steampowered.com/events/ajaxgetpartnereventspageable/"
    params = {"clan_accountid": 0, "appid": int(appid), "offset": 0,
              "count": EVENTS_COUNT, "l": "english"}
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 429:                       # rate limited: back off hard
                last_err = "429"
                time.sleep(STORE_DELAY * 4 + random.uniform(0, 2))
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:                              # noqa: BLE001 — transient net/JSON
            last_err = str(e)[:120]
            time.sleep(STORE_DELAY + random.uniform(0, 1.5))
    else:
        log(f"  appid {appid}: events fetch failed ({last_err})")
        return None

    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        # success:0 or unexpected shape -> treat as "no updates" (not a transient error):
        # many valid games simply have no partner events.
        return {}

    out = {}
    for ev in events:
        et = ev.get("event_type")
        tier = _tier(et)
        if tier is None:
            continue                                        # sale/stream/announcement — skip
        gid = str(ev.get("gid") or "")
        body = ev.get("announcement_body") or {}
        ts = ev.get("rtime32_start_time") or body.get("posttime") or 0
        if not gid or not ts:
            continue
        out[gid] = {"ts": int(ts), "type": tier, "et": int(et)}
    return out


def merge_events(prev_events, fresh):
    """Merge freshly-fetched events into the stored map (identity-keyed by gid).
    New gids are added; known gids are refreshed in place (Steam may re-type/re-time a
    post). Then ring-buffer down to PER_GAME_CAP, dropping the OLDEST by ts."""
    merged = dict(prev_events or {})
    merged.update(fresh)                                    # refresh-on-revisit + adds
    if len(merged) > PER_GAME_CAP:
        keep = sorted(merged.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:PER_GAME_CAP]
        merged = dict(keep)
    return merged


# --------------------------------------------------------------------------- #
# Eligibility + priority
# --------------------------------------------------------------------------- #
def is_eligible(rec, last_update_ts, review_count, now):
    """Eligible if never scraped; OR past its cooldown (shorter for actively-updated
    games). Gated by MIN_REVIEWS_FLOOR so near-invisible games don't burn storefront
    budget — re-checked every run, never a permanent skip."""
    if (review_count or 0) < MIN_REVIEWS_FLOOR:
        return False
    if not rec:
        return True
    age = now - rec.get("scraped_at", 0)
    actively_updated = last_update_ts and (now - last_update_ts) <= UPDATE_ACTIVE_DAYS * 86400
    cooldown = COOLDOWN_DAYS if actively_updated else NOUPDATE_COOLDOWN_DAYS
    return age >= cooldown * 86400


def priority(rec, last_update_ts, review_count, now):
    """Higher = scrape sooner. Never-seen and recently-updated games win; near-invisible
    games are pushed down. Mirrors playtime_refresh.py's shape."""
    score = 0.0
    if not rec:
        score += 1000
    if last_update_ts:
        days = (now - last_update_ts) / 86400
        score += 300 if days <= 30 else 150 if days <= 90 else 50 if days <= 365 else 0
    if review_count is not None and review_count < MIN_REVIEWS_FLOOR:
        score -= 300
    if rec:
        score += min(200, (now - rec.get("scraped_at", 0)) / 86400)  # staleness nudge
    return score


# --------------------------------------------------------------------------- #
# Git (single-writer hard-reset push — identical strategy to playtime_refresh.py)
# --------------------------------------------------------------------------- #
def git_commit_shard(bucket, msg):
    """Robust single-writer push of exactly this run's one shard. Hard-resets to
    origin/main and re-applies only OUR shard, so concurrent other-bucket writers are
    never clobbered and a rebase can never wedge. Fails LOUD (non-zero exit) if it can't
    land, so a broken push is a visible RED run — never a silent green one."""
    if not IN_ACTIONS:
        log(f"  (local run — skipping git commit: {msg})")
        return
    name = f"{bucket:02d}.json"
    src = SHARD_DIR / name
    snap = src.read_bytes() if src.exists() else None
    last_err = ""
    for attempt in range(1, 9):
        try:
            subprocess.run(["git", "fetch", "origin", "main"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "reset", "--hard", "origin/main"],
                           check=True, capture_output=True, text=True)
            SHARD_DIR.mkdir(exist_ok=True)
            if snap is not None:
                (SHARD_DIR / name).write_bytes(snap)        # re-apply only our shard
            subprocess.run(["git", "add", f"updates_raw/{name}"], check=True)
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
    log(f"  ERROR: updates shard commit failed after 8 attempts — {last_err[:200]}")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_games():
    if not GAMES_FILE.exists():
        return []
    g = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    if isinstance(g, list):
        return g
    return g.get("games") or list(g.values())


def main():
    start = time.time()
    deadline = start + RUN_MINUTES * 60 - TIME_BUFFER

    ensure_sharding()
    bucket = bucket_for_run()
    log(f"updates_refresh: bucket {bucket:02d}/{NSHARDS} · run budget {RUN_MINUTES} min")

    games = load_games()
    if not games:
        log("games.json empty/missing — nothing to do")
        return 0

    now = int(time.time())
    shard_games = load_shard(bucket)

    # Candidates: games that hash to THIS bucket and are eligible, ordered by priority.
    cands = []
    for rec in games:
        appid = rec.get("appid")
        if appid is None or shard_of(appid) != bucket:
            continue
        stored = shard_games.get(str(appid))
        lut = rec.get("last_update_ts")
        rc = rec.get("review_count")
        if not is_eligible(stored, lut, rc, now):
            continue
        cands.append((priority(stored, lut, rc, now), int(appid), rec))
    cands.sort(key=lambda t: t[0], reverse=True)
    log(f"  {len(cands)} eligible game(s) in this bucket")

    processed = 0
    changed = 0
    last_commit = time.time()
    for _score, appid, _rec in cands:
        if time.time() >= deadline:
            log("  run budget reached — committing and stopping")
            break

        fresh = fetch_events(appid)
        time.sleep(STORE_DELAY)
        processed += 1
        if fresh is None:
            continue                                        # transient — leave prior record intact

        key = str(appid)
        prev = shard_games.get(key) or {}
        prev_events = prev.get("events") or {}
        merged = merge_events(prev_events, fresh)

        # Only rewrite if something actually changed (avoids churny no-op commits).
        if merged != prev_events or not prev:
            shard_games[key] = {"events": merged, "scraped_at": now}
            changed += 1

        if time.time() - last_commit >= COMMIT_SECONDS:
            save_shard(bucket, shard_games)
            git_commit_shard(bucket, f"updates_raw/{bucket:02d}: {len(shard_games)} games "
                                     f"({changed} changed this run so far)")
            last_commit = time.time()

    save_shard(bucket, shard_games)
    git_commit_shard(bucket, f"updates_raw/{bucket:02d}: {len(shard_games)} games "
                             f"(+{changed} changed, {processed} probed)")
    log(f"Done. Bucket {bucket:02d}: probed {processed}, changed {changed}, "
        f"stored {len(shard_games)} games.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
