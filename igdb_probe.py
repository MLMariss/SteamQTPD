#!/usr/bin/env python3
"""
IGDB diagnostic probe — run LOCALLY to find why external_games matches 0 games.
==============================================================================
The scheduled job authenticates fine (IGDB entries header appears) but every batch
reports "0 with IGDB times", which means the external_games query is matching
nothing. This script walks the full chain against a few KNOWN games and dumps the
RAW IGDB responses at each step, plus tests several candidate Steam-platform
filters side by side, so the actual cause is visible rather than guessed.

USAGE (locally, not in Actions):

    export IGDB_CLIENT_ID=your_client_id
    export IGDB_CLIENT_SECRET=your_client_secret
    pip install requests
    python igdb_probe.py

It makes ~10 small requests total. Nothing is written to disk. Read the output
top-to-bottom: the first section that returns non-empty rows is the query shape
the real job should use.
"""

import json
import os
import sys
import time

import requests

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.igdb.com/v4"

# Known games with Steam appids that DEFINITELY exist on IGDB with time-to-beat data.
# (appid -> name, for readable output)
KNOWN = {
    "620": "Portal 2",
    "413150": "Stardew Valley",
    "1145360": "Hades",
    "367520": "Hollow Knight",
    "1091500": "Cyberpunk 2077",
}


def die(msg):
    print(f"\n!! {msg}")
    sys.exit(1)


def get_token():
    cid = os.environ.get("IGDB_CLIENT_ID")
    secret = os.environ.get("IGDB_CLIENT_SECRET")
    if not cid or not secret:
        die("Set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in your environment first.")
    r = requests.post(TOKEN_URL, params={
        "client_id": cid, "client_secret": secret,
        "grant_type": "client_credentials"}, timeout=30)
    if r.status_code != 200:
        die(f"Auth failed {r.status_code}: {r.text[:300]}")
    tok = r.json()["access_token"]
    print(f"[auth] OK — token acquired (expires in {r.json().get('expires_in')}s)")
    return cid, tok


def post(endpoint, body, cid, token, label=""):
    """POST an Apicalypse query; print the raw result. Returns parsed JSON or None."""
    url = f"{API_BASE}/{endpoint}"
    headers = {"Client-ID": cid, "Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    r = requests.post(url, headers=headers, data=body, timeout=30)
    tag = f"[{endpoint}{(' ' + label) if label else ''}]"
    if r.status_code != 200:
        print(f"{tag} HTTP {r.status_code}: {r.text[:300]}")
        return None
    data = r.json()
    print(f"{tag} {len(data)} row(s):")
    print("    " + json.dumps(data, indent=2)[:1200].replace("\n", "\n    "))
    time.sleep(0.35)
    return data


def main():
    cid, token = get_token()
    appids = list(KNOWN.keys())
    uids_str = ",".join(f'"{a}"' for a in appids)
    uids_num = ",".join(appids)  # numeric variant (no quotes)

    print("\n" + "=" * 70)
    print("STEP 1 — external_games: which Steam-platform filter returns rows?")
    print("Testing appids:", ", ".join(f"{a}({n})" for a, n in KNOWN.items()))
    print("=" * 70)

    # -- The query the CURRENT job uses (category = 1, uid as quoted strings) --
    print("\n--- A) current job query: category = 1, uid = (\"...\") ---")
    post("external_games",
         f'fields uid,game,category,external_game_source,name; '
         f'where category = 1 & uid = ({uids_str}); limit 50;',
         cid, token, "A")

    # -- external_game_source = 1 (the newer field replacing category) --
    print("\n--- B) external_game_source = 1 (newer field), uid quoted ---")
    post("external_games",
         f'fields uid,game,category,external_game_source,name; '
         f'where external_game_source = 1 & uid = ({uids_str}); limit 50;',
         cid, token, "B")

    # -- No platform filter at all: just uid. Shows what category/source Steam actually uses --
    print("\n--- C) NO platform filter, uid quoted — reveals the real Steam enum ---")
    post("external_games",
         f'fields uid,game,category,external_game_source,name; '
         f'where uid = ({uids_str}); limit 50;',
         cid, token, "C")

    # -- uid as NUMBERS instead of strings (in case uid is an integer field) --
    print("\n--- D) uid as numbers (unquoted), no platform filter ---")
    post("external_games",
         f'fields uid,game,category,external_game_source,name; '
         f'where uid = ({uids_num}); limit 50;',
         cid, token, "D")

    # -- Broad sanity: ANY Steam external_games rows exist at all? --
    print("\n--- E) sanity: any category = 1 rows exist at all (no uid filter)? ---")
    post("external_games",
         f'fields uid,game,category,external_game_source,name; '
         f'where category = 1; limit 5;',
         cid, token, "E")

    print("\n" + "=" * 70)
    print("STEP 2 — game_time_to_beats: does the endpoint return data at all?")
    print("=" * 70)
    # Portal 2's IGDB game id is 72 (stable). If STEP 1 gives you real game ids,
    # swap them in; this hardcoded id just proves the ttb endpoint works.
    print("\n--- F) game_time_to_beats for a known IGDB game id (72 = Portal 2) ---")
    post("game_time_to_beats",
         'fields game_id,hastily,normally,completely; where game_id = 72; limit 5;',
         cid, token, "F")

    print("\n--- G) game_time_to_beats unfiltered (does ANYTHING come back?) ---")
    post("game_time_to_beats",
         'fields game_id,hastily,normally,completely; limit 5;',
         cid, token, "G")

    print("\n" + "=" * 70)
    print("HOW TO READ THIS:")
    print("  - Whichever of A/B/C/D returned rows is the correct external_games query.")
    print("    If C/D work but A/B don't -> the platform filter field/value is wrong;")
    print("    look at the `category` and `external_game_source` values in the C/D rows")
    print("    to see what Steam actually uses on your tier.")
    print("  - If NONE of A-E return rows -> uid may not be the Steam appid field, or")
    print("    your app lacks external_games access. Paste the full output back.")
    print("  - F/G confirm the time-to-beat endpoint itself works independently.")
    print("=" * 70)


if __name__ == "__main__":
    main()
