#!/usr/bin/env python3
"""
Steam QTPD — update-events summarizer
=====================================
Reads the BIG sharded per-event files (updates_raw/NN.json, maintained by
updates_refresh.py) and writes the SMALL frontend-facing updates.json: per game, the
last major/minor update timestamps plus windowed big/small counts the site displays.

One writer per file:
  * updates_refresh.py -> updates_raw/NN.json   (heavy: every update event, keyed by gid —
                                                 scraper working state; browser never loads it)
  * THIS               -> updates.json          (light: last_*_ts + window counts per game)

WHY RECOMPUTE HERE (and why the frontend recomputes again)
----------------------------------------------------------
Window counts ("updates in the last 30 days") are relative to *now*. This summary is a
convenience snapshot computed at summarize-time, but it ALSO ships the raw per-window
event timestamps ("dates") so the frontend can recompute the exact same windows against
the live clock — meaning the displayed counts never go stale between scrapes. The counts
we write here are the value at generated_at; "dates" is the source of truth that lets the
client keep them honest.

Output shape (updates.json):
{
  "generated_at": 1720000000,
  "windows": [30, 90, 180, 365],            # day-boundaries the counts use (last = "over a year")
  "tiers": ["major", "regular", "minor"],   # Steam's three update categories, kept distinct
  "games": {
    "<appid>": {
      "last_major_ts":   1719000000|null,   # 13 = Major Update
      "last_regular_ts": 1718500000|null,   # 14 = Regular Update (mid-tier)
      "last_minor_ts":   1718000000|null,   # 12 = Small Update / Patch Notes
      "last_any_ts":     1719000000|null,
      "counts": {                            # snapshot at generated_at; client can recompute
        "30d":     {"major": 1, "regular": 1, "minor": 3},
        "90d":     {"major": 2, "regular": 3, "minor": 7},
        "180d":    {"major": 3, "regular": 5, "minor": 12},
        "365d":    {"major": 5, "regular": 9, "minor": 20},
        "over365": {"major": 4, "regular": 6, "minor": 9}   # older than the last window boundary
      },
      "dates": {                             # raw material for client-side recompute (compact)
        "major":   [1719000000, ...],        # newest-first, capped
        "regular": [1718500000, ...],
        "minor":   [1718000000, ...]
      }
    }
  }
}
The frontend can display all three, or merge major+regular into a single "big" bucket vs
minor — that grouping is a display choice, not baked into the data.
The tiny frontend file stays small: only timestamps, never event bodies.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARD_DIR = HERE / "updates_raw"             # dir of NN.json shards (read-only here; owned by updates_refresh.py)
OUT_FILE = HERE / "updates.json"             # THIS file's output (committed; frontend reads it)

# Window day-boundaries. Frontend windows: last month / 3mo / 6mo / year / over a year.
WINDOWS = [30, 90, 180, 365]
# Cap the dates arrays shipped to the client so updates.json stays a small download even
# for hyper-active games (CS2 etc. post hundreds of patch notes). A year+ of history at
# this cap is ample for every window count; older-than-cap still contributes to over365
# via the snapshot counts computed below (which see the full stored set).
DATES_CAP = 60

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(*a):
    print(*a, flush=True)


def iter_raw_games():
    """Yield (appid_str, events_dict) across all shards. events_dict is {gid: {ts,type,et}}."""
    if not SHARD_DIR.exists():
        return
    for p in sorted(SHARD_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for appid, rec in (d.get("games") or {}).items():
            yield appid, (rec.get("events") or {})


TIERS = ("major", "regular", "minor")


def summarize_game(events, now):
    """Roll one game's {gid: {ts,type}} into last_*_ts + windowed counts + capped date
    lists, across all three tiers (major / regular / minor)."""
    by_tier = {t: sorted((e["ts"] for e in events.values() if e.get("type") == t), reverse=True)
               for t in TIERS}

    def counts_within(days):
        cut = now - days * 86400
        return {t: sum(1 for ts in by_tier[t] if ts >= cut) for t in TIERS}

    counts = {f"{d}d": counts_within(d) for d in WINDOWS}
    last_boundary = now - WINDOWS[-1] * 86400
    counts["over365"] = {t: sum(1 for ts in by_tier[t] if ts < last_boundary) for t in TIERS}

    lasts = {t: (by_tier[t][0] if by_tier[t] else None) for t in TIERS}
    all_last = [v for v in lasts.values() if v is not None]

    return {
        "last_major_ts": lasts["major"],
        "last_regular_ts": lasts["regular"],
        "last_minor_ts": lasts["minor"],
        "last_any_ts": max(all_last) if all_last else None,
        "counts": counts,
        "dates": {t: by_tier[t][:DATES_CAP] for t in TIERS},
    }


def git_commit():
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "updates.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            "updates summary: refreshed frontend updates.json"], check=False)
            import random
            for _attempt in range(1, 9):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log("  committed updates.json")
                    break
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:                                   # noqa: BLE001
        log(f"  git commit failed: {e}")


def main():
    now = int(time.time())
    out_games = {}
    n = 0
    for appid, events in iter_raw_games():
        if not events:
            continue
        out_games[appid] = summarize_game(events, now)
        n += 1

    payload = {"generated_at": now, "windows": WINDOWS, "tiers": list(TIERS), "games": out_games}
    OUT_FILE.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                        encoding="utf-8")
    log(f"updates_summarize: wrote {n} games to updates.json")
    git_commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
