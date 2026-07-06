#!/usr/bin/env python3
"""
SteamQHPP — IGDB time-to-beat secondary source (Phase C)
========================================================
HLTB matching is by TITLE and inherently lossy; even after Phase A cleanup a real
game with no HLTB page (or an unmatchable name) stays blank. IGDB is a second,
INDEPENDENT completion-time source that keys off the **Steam appid directly**
(via its `external_games` table), so it recovers games HLTB structurally can't —
raising the coverage *ceiling* rather than cleaning up within it.

One-writer-per-file, preserved
------------------------------
This job is the SOLE writer of `hltb_igdb.json`. It NEVER touches `hltb.json`
(owned by hltb_refresh.py). The frontend merges the two by appid at read time and
prefers real HLTB, falling back to IGDB only where HLTB has no usable value — so
the two sources can never collide on a push and HLTB remains authoritative.

    hltb_refresh.py -> hltb.json        (primary, HowLongToBeat, title-matched)
    THIS            -> hltb_igdb.json   (secondary, IGDB, appid-matched)   <-- new

Data model (mirrors the raw/est shape so the estimator can fill it identically)
-------------------------------------------------------------------------------
    hltb_igdb.json = {
      "generated_at": <epoch>, "count": N,
      "igdb": { "<appid>": { main, extra, complete, avg, match,
                             raw:{main,extra,complete}, est:[...],
                             igdb_id, fetched_at, attempts } }
    }

IGDB `game_time_to_beats` returns SECONDS in fields `hastily` / `normally` /
`completely`. We map: normally -> main, completely -> complete. IGDB has no
direct "main+extras" leg, so `extra` is always left for the shared estimator to
fill from whatever real leg(s) exist (marked in `est`). `hastily` (speed-run rush
time) is recorded only as a fallback for `main` when `normally` is absent.

Auth: Twitch OAuth client-credentials (IGDB_CLIENT_ID + IGDB_CLIENT_SECRET as
GitHub secrets) -> bearer token. Rate limit: 4 req/s, <=8 concurrent (we stay
well under with serial requests + IGDB_DELAY). Batched by appid in groups of
BATCH so one request resolves many games.
"""

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

import hltb_estimate as HE      # SAME estimator as HLTB -> identical fill/avg semantics

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # read-only (owned by scraper.py)
HLTB_FILE = HERE / "hltb.json"            # read-only here (owned by hltb_refresh.py)
IGDB_FILE = HERE / "hltb_igdb.json"       # THIS job's output (committed)

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "110"))
CHECKPOINT_SECONDS = 300
TIME_BUFFER = 45
IGDB_DELAY = 0.30                         # ~3 req/s, under IGDB's 4 req/s ceiling
BATCH = 200                               # appids resolved per external_games query
IGDB_RESCRAPE_DAYS = 90                   # re-check a MATCHED IGDB entry only this often
# Blank (no-match) IGDB entries are retried more eagerly than matched ones, then settle
# into the normal cadence — a real game may simply be missing an IGDB time-to-beat record
# today that gets added later, and we don't want a transient/first-pass miss frozen for 90
# days. Two tiers (mirrors Phase B, simpler): blanks with attempts < BLANK_EAGER_ATTEMPTS
# retry after BLANK_EAGER_DAYS; at/above the cap they fall to the normal IGDB_RESCRAPE_DAYS
# cadence so genuinely-timeless games aren't re-hit forever.
IGDB_BLANK_EAGER_ATTEMPTS = 3
IGDB_BLANK_EAGER_DAYS = 7

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.igdb.com/v4"
STEAM_SOURCE = 1                          # external_games.external_game_source == 1 -> Steam
# NOTE: IGDB deprecated the old `category` enum in favour of `external_game_source`.
# The old `where category = 1` filter returns ZERO rows on the current API (the field
# still appears in responses but is no longer queryable), which silently matched nothing.
# Probe-confirmed 2026-07: `external_game_source = 1` returns Steam links correctly.
DAY = 86400
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


# --- Auth --------------------------------------------------------------------- #
def get_token():
    """Client-credentials OAuth against Twitch. Returns (client_id, bearer) or
    (None, None) if credentials are absent/invalid (job then no-ops cleanly)."""
    cid = os.environ.get("IGDB_CLIENT_ID")
    secret = os.environ.get("IGDB_CLIENT_SECRET")
    if not cid or not secret:
        log("IGDB_CLIENT_ID / IGDB_CLIENT_SECRET not set; nothing to do.")
        return None, None
    try:
        r = requests.post(TOKEN_URL, params={
            "client_id": cid, "client_secret": secret,
            "grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        return cid, r.json()["access_token"]
    except Exception as e:
        log(f"IGDB auth failed: {e}")
        return None, None


def _post(endpoint, body, cid, token, tries=4):
    """POST an Apicalypse query to an IGDB endpoint, returning parsed JSON list.
    Retries on 429/5xx with backoff. Raises on unrecoverable error."""
    url = f"{API_BASE}/{endpoint}"
    headers = {"Client-ID": cid, "Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    for attempt in range(1, tries + 1):
        r = requests.post(url, headers=headers, data=body, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries:
            time.sleep(1.5 * attempt + random.uniform(0, 1))
            continue
        raise RuntimeError(f"IGDB {endpoint} {r.status_code}: {r.text[:200]}")
    return []


# --- Field mapping ------------------------------------------------------------ #
def _sec_to_hours(v):
    """IGDB times are seconds. Convert to rounded hours; non-positive/absent -> None."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v / 3600.0, 1) if v > 0 else None


def times_from_ttb(rec):
    """Extract (main, extra, complete) hours from a game_time_to_beats record.
    normally -> main (fallback hastily), completely -> complete, extra left to the
    estimator. All in hours, None where absent."""
    main = _sec_to_hours(rec.get("normally")) or _sec_to_hours(rec.get("hastily"))
    complete = _sec_to_hours(rec.get("completely"))
    return main, None, complete


# --- IGDB resolution: Steam appid -> igdb game id -> time-to-beat ------------- #
def resolve_external(appids, cid, token):
    """Map a batch of Steam appids to IGDB game ids via external_games.
    Returns {steam_appid(int): igdb_game_id(int)} for those IGDB knows."""
    uids = ",".join(f'"{a}"' for a in appids)
    body = (f'fields uid,game; '
            f'where external_game_source = {STEAM_SOURCE} & uid = ({uids}); '
            f'limit {len(appids)};')
    out = {}
    for row in _post("external_games", body, cid, token):
        uid = row.get("uid")
        game = row.get("game")
        if uid is None or game is None:
            continue
        try:
            out[int(uid)] = int(game)
        except (TypeError, ValueError):
            continue
    return out


def fetch_ttb(igdb_ids, cid, token):
    """Fetch game_time_to_beats for a batch of IGDB game ids.
    Returns {igdb_game_id(int): rec} keyed by the ttb record's `game_id`."""
    ids = ",".join(str(i) for i in igdb_ids)
    body = (f'fields game_id,hastily,normally,completely; '
            f'where game_id = ({ids}); limit {len(igdb_ids)};')
    out = {}
    for rec in _post("game_time_to_beats", body, cid, token):
        gid = rec.get("game_id")
        if gid is None:
            continue
        try:
            out[int(gid)] = rec
        except (TypeError, ValueError):
            continue
    return out


# --- Storage ------------------------------------------------------------------ #
def load_json(path, key):
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): v for k, v in (d.get(key) or {}).items()}
        except (ValueError, TypeError):
            pass
    return {}


def load_games():
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return [(int(g["appid"]), g.get("title", "")) for g in d.get("games", [])]


def save_igdb(igdb):
    IGDB_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(igdb),
         "igdb": {str(k): v for k, v in igdb.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "hltb_igdb.json"], check=False)
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
        log(f"  git checkpoint failed: {e}")


# --- Priority: which appids to resolve this run ------------------------------- #
def build_worklist(games, hltb, igdb, now):
    """Ordered appids to resolve via IGDB this run. Priority:
      1. Games with no IGDB entry yet (never-seen) — highest value, incl. HLTB-blank.
      2. BLANK IGDB entries still in their eager-retry window (attempts under the cap):
         an unproven miss that may just need another try / a newly-added IGDB record.
      3. Stale entries past IGDB_RESCRAPE_DAYS (matched entries, or blanks past the eager
         cap now on normal cadence), oldest first.
    Games already covered by a real (non-estimated) HLTB match are skipped — no need for
    a second source there.

    Fix (2026-07): previously ANY existing entry (blank OR matched) only re-ran on the
    90-day stale timer, so 110k blanks written by a broken run were frozen out of the
    worklist entirely. Blanks are now distinguished from matches and retried eagerly."""
    def hltb_has_real(aid):
        e = hltb.get(aid)
        if not e:
            return False
        return e.get("avg") is not None and not e.get("est")  # a real (non-estimated) match

    def is_blank(entry):
        return entry.get("avg") is None

    never, eager, stale = [], [], []
    for aid, _title in games:
        if hltb_has_real(aid):
            continue                      # real HLTB already -> secondary not needed
        entry = igdb.get(aid)
        if entry is None:
            never.append(aid)             # priority 1: no IGDB entry yet
            continue
        fetched = entry.get("fetched_at") or 0
        age = now - fetched
        if is_blank(entry):
            attempts = entry.get("attempts") or 0
            if attempts < IGDB_BLANK_EAGER_ATTEMPTS:
                if age > IGDB_BLANK_EAGER_DAYS * DAY:
                    eager.append((fetched, aid))   # priority 2: unproven blank, eager window
                # else: retried too recently, skip this run
            elif age > IGDB_RESCRAPE_DAYS * DAY:
                stale.append((fetched, aid))       # blank past eager cap -> normal cadence
        elif age > IGDB_RESCRAPE_DAYS * DAY:
            stale.append((fetched, aid))           # matched entry, normal re-check cadence
    eager.sort(key=lambda t: t[0])
    stale.sort(key=lambda t: t[0])
    return never + [aid for _f, aid in eager] + [aid for _f, aid in stale]


def main():
    start = time.time()
    cid, token = get_token()
    if not token:
        return 0                          # no creds -> clean no-op (job stays green)

    games = load_games()
    if not games:
        log("No games in games.json (or only sample). Nothing to do.")
        return 0
    hltb = load_json(HLTB_FILE, "hltb")   # read-only: informs priority only
    igdb = load_json(IGDB_FILE, "igdb")
    ratios, n_triples = HE.compute_ratios(igdb)   # fill from IGDB's own real triples
    log(f"Games {len(games)} | HLTB entries {len(hltb)} | IGDB entries {len(igdb)} "
        f"| fill from {n_triples} IGDB triples")

    work = build_worklist(games, hltb, igdb, int(time.time()))
    log(f"IGDB worklist: {len(work)} appids to resolve "
        f"(HLTB-blank first, then stale).")

    budget = RUN_MINUTES * 60
    last_commit = time.time()
    resolved = matched = 0

    def time_left():
        return budget - (time.time() - start)

    def maybe_checkpoint(msg):
        nonlocal last_commit
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_igdb(igdb)
            git_checkpoint(msg)
            last_commit = time.time()

    title_by_aid = dict(games)

    for bstart in range(0, len(work), BATCH):
        if time_left() < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        batch = work[bstart:bstart + BATCH]
        try:
            ext = resolve_external(batch, cid, token)   # steam appid -> igdb id
            time.sleep(IGDB_DELAY)
            ttb = fetch_ttb(list(set(ext.values())), cid, token) if ext else {}
            time.sleep(IGDB_DELAY)
        except Exception as e:
            log(f"  IGDB batch error (offset {bstart}): {e}")
            time.sleep(2)
            continue                      # transient -> skip batch, retry next run

        for aid in batch:
            gid = ext.get(aid)
            rec = ttb.get(gid) if gid is not None else None
            prior = igdb.get(aid) or {}
            if rec:
                m, e, c = times_from_ttb(rec)
                entry = HE.make_entry(m, e, c, title_by_aid.get(aid), time.time(), ratios)
                entry["igdb_id"] = gid
                entry.pop("attempts", None)
                igdb[aid] = entry
                if entry.get("avg") is not None:
                    matched += 1
            else:
                # No IGDB time-to-beat for this appid. Record a blank so we don't
                # re-hit it every run; carry an attempts counter like the HLTB job.
                entry = HE.make_entry(None, None, None, None, time.time(), ratios)
                if gid is not None:
                    entry["igdb_id"] = gid
                entry["attempts"] = (prior.get("attempts") or 0) + 1
                igdb[aid] = entry
            resolved += 1
        log(f"  [batch {bstart // BATCH + 1}] resolved {resolved}, {matched} with IGDB times")
        maybe_checkpoint(f"igdb: {len(igdb)} entries, {matched} timed (checkpoint)")

    save_igdb(igdb)
    git_checkpoint(f"igdb: {len(igdb)} entries, {matched} with IGDB times this run")
    log(f"\nDone. Resolved {resolved} appids, {matched} gained IGDB times. "
        f"hltb_igdb.json now has {len(igdb)} entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
