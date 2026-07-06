#!/usr/bin/env python3
"""
One-off reset for hltb_igdb.json.
=================================
A broken external_games query (deprecated `category` filter) wrote ~110k BLANK
entries with fresh timestamps. The fixed query can't get a turn on those because
even eager blank-retry waits for its window, and the entries are minutes old. This
script wipes hltb_igdb.json back to an empty structure so every game becomes
"never-seen" again and is immediately eligible on the next real IGDB run.

Safe: writes an EMPTY entry set (no games lost — games.json is the source of truth;
IGDB data is derived and fully rebuildable). Manual-trigger only (see igdb-wipe.yml).
Run once, then let the normal `igdb.yml` job repopulate with the fixed query.
"""

import json
import os
import subprocess
import time
from pathlib import Path

IGDB_FILE = Path(__file__).resolve().parent / "hltb_igdb.json"


def main():
    prior = 0
    if IGDB_FILE.exists():
        try:
            prior = len((json.loads(IGDB_FILE.read_text(encoding="utf-8")) or {}).get("igdb", {}))
        except (ValueError, TypeError):
            prior = -1
    IGDB_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": 0, "igdb": {}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wiped hltb_igdb.json: {prior} entries -> 0. "
          f"Next igdb.yml run will treat all games as never-seen.")

    if os.environ.get("GITHUB_ACTIONS") == "true":
        subprocess.run(["git", "add", "hltb_igdb.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            "igdb: wipe blank entries from broken external_games query (reset)"],
                           check=False)
            subprocess.run(["git", "fetch", "origin", "main"], check=False)
            subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=False)
            print("Committed wipe to main.")
        else:
            print("Nothing to commit (already empty).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
