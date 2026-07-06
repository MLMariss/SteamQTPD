#!/usr/bin/env python3
"""
IGDB step-2 probe — why does game_time_to_beats drop ~98% of valid IGDB ids?
============================================================================
Step 1 (appid -> IGDB game id) works: 96,201 of 110,639 games resolved. But step 2
(IGDB game id -> time-to-beat) only returned times for 1,471 of them (1.5%). That's
implausibly low if the data exists, so this probe tests the exact ways the batch
query could be silently dropping records.

It uses REAL IGDB ids pulled from the actual output: 50 that resolved but got NO
time (the failing population) and 5 that DID get a time (the working population).
If the "no time" ids start returning records under a corrected query, it's a query
bug; if they stay empty even one-at-a-time, IGDB genuinely lacks their data.

USAGE (locally or via the debug workflow with secrets):
    export IGDB_CLIENT_ID=...   export IGDB_CLIENT_SECRET=...
    pip install requests
    python igdb_probe2.py
"""

import json
import os
import sys
import time

import requests

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.igdb.com/v4"

# Real ids from the actual run.
NO_TIME_IDS = [19349, 9409, 14789, 19350, 14794, 15614, 8935, 119176, 2608, 15623,
               27790, 3939, 14801, 14802, 9319, 9317, 3347, 14803, 15627, 10358,
               27813, 27819, 3756, 8324, 27818, 27812, 27817, 27816, 27814, 14810,
               14811, 15648, 28976, 14814, 14877, 14816, 293, 27820, 6046, 865,
               15716, 839, 24770, 14822, 15718, 15719, 11353, 5840, 5841, 84532]
GOT_TIME_IDS = [2369, 621, 622, 8321, 27786]


def die(m):
    print("\n!! " + m); sys.exit(1)


def token():
    cid = os.environ.get("IGDB_CLIENT_ID"); sec = os.environ.get("IGDB_CLIENT_SECRET")
    if not cid or not sec:
        die("Set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET.")
    r = requests.post(TOKEN_URL, params={"client_id": cid, "client_secret": sec,
                                         "grant_type": "client_credentials"}, timeout=30)
    if r.status_code != 200:
        die(f"auth {r.status_code}: {r.text[:200]}")
    print("[auth] OK")
    return cid, r.json()["access_token"]


def q(endpoint, body, cid, tok, label):
    r = requests.post(f"{API_BASE}/{endpoint}",
                      headers={"Client-ID": cid, "Authorization": f"Bearer {tok}",
                               "Accept": "application/json"},
                      data=body, timeout=30)
    if r.status_code != 200:
        print(f"[{label}] HTTP {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    print(f"[{label}] {len(data)} row(s) returned")
    time.sleep(0.35)
    return data


def main():
    cid, tok = token()

    print("\n" + "=" * 68)
    print("CONTROL — ids that DID get times (should return ~5 rows)")
    print("=" * 68)
    ids = ",".join(map(str, GOT_TIME_IDS))
    q("game_time_to_beats",
      f"fields game_id,hastily,normally,completely; where game_id = ({ids}); limit 500;",
      cid, tok, "control")

    print("\n" + "=" * 68)
    print("TEST 1 — the JOB'S EXACT query on 50 'no time' ids (limit = len)")
    print("If this returns ~0, reproduce the bug; if it returns many, the bug is")
    print("elsewhere (batching/dedup in main).")
    print("=" * 68)
    ids = ",".join(map(str, NO_TIME_IDS))
    q("game_time_to_beats",
      f"fields game_id,hastily,normally,completely; where game_id = ({ids}); "
      f"limit {len(NO_TIME_IDS)};",
      cid, tok, "job-query")

    print("\n" + "=" * 68)
    print("TEST 2 — same ids, limit 500 (rules out a limit-too-low bug)")
    print("=" * 68)
    got2 = q("game_time_to_beats",
             f"fields game_id,hastily,normally,completely; where game_id = ({ids}); limit 500;",
             cid, tok, "limit500")
    if got2:
        print("    ids that returned a record:",
              sorted({r.get("game_id") for r in got2}))

    print("\n" + "=" * 68)
    print("TEST 3 — first 5 'no time' ids ONE AT A TIME (isolates per-id truth)")
    print("If singles return data that the batch didn't, it's an IN-list/limit bug.")
    print("If singles are ALSO empty, IGDB truly lacks ttb for these ids.")
    print("=" * 68)
    for gid in NO_TIME_IDS[:5]:
        rows = q("game_time_to_beats",
                 f"fields game_id,hastily,normally,completely; where game_id = {gid}; limit 5;",
                 cid, tok, f"single-{gid}")
        if rows:
            print("    ->", json.dumps(rows))

    print("\n" + "=" * 68)
    print("TEST 4 — do these ids exist as GAMES at all, and what are they?")
    print("(Confirms the ids are valid games, and shows names to sanity-check.)")
    print("=" * 68)
    sample = ",".join(map(str, NO_TIME_IDS[:10]))
    games = q("games", f"fields id,name; where id = ({sample}); limit 20;", cid, tok, "games")
    if games:
        for g in games:
            print(f"    {g.get('id')}: {g.get('name')}")

    print("\n" + "=" * 68)
    print("TEST 5 — total ttb table size (how much data does IGDB have at all?)")
    print("=" * 68)
    cnt = requests.post(f"{API_BASE}/game_time_to_beats/count",
                        headers={"Client-ID": cid, "Authorization": f"Bearer {tok}",
                                 "Accept": "application/json"},
                        data="", timeout=30)
    if cnt.status_code == 200:
        print("    game_time_to_beats total records:", cnt.json().get("count"))
    else:
        print(f"    count failed {cnt.status_code}: {cnt.text[:150]}")

    print("\n" + "=" * 68)
    print("READ:")
    print("  - Control returns rows but TEST 1 doesn't  -> job query bug on the id set")
    print("  - TEST 2 (limit 500) returns more than TEST 1 -> limit-too-low was the bug")
    print("  - TEST 3 singles return data -> IN-list/batch bug; singles empty -> real gap")
    print("  - TEST 5 shows whether IGDB's ttb table is small (real sparse coverage)")
    print("    vs large (our query is the problem).")
    print("=" * 68)


if __name__ == "__main__":
    main()
