#!/usr/bin/env python3
"""
Steam QHPP — playtime summarizer
================================
Reads the BIG per-review file (playtime_raw.json, maintained by
playtime_refresh.py) and writes the SMALL frontend-facing playtime.json:
just the medians + counts the site needs to display. Runs on its own daily cron.

Why a separate script + file (one writer per file):
  * playtime_refresh.py owns playtime_raw.json (heavy: every review, keyed by
    recommendationid — this is scraper working state; the browser never loads it).
  * THIS owns playtime.json (light: per-game medians only — this is what the
    frontend downloads). Two Actions writing different files never collide on push.

The frontend playtime.json is intentionally tiny (medians + sample sizes), so it
stays a small download no matter how deep the raw file gets. This is the whole
point of the split: raw depth is unbounded-ish (capped per game), display size is
flat.

Recomputing here (rather than trusting the summary mirror inside the raw file)
keeps the frontend file authoritative and lets us change the summary shape / add
derived stats without re-scraping — everything needed is in the raw reviews.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_FILE = HERE / "playtime_raw.json"        # read-only here (owned by playtime_refresh.py)
OUT_FILE = HERE / "playtime.json"            # THIS file's output (committed; frontend reads it)

MIN_SEGMENT_FOR_MEDIAN = 3                    # keep in lockstep with playtime_refresh.py
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


def _median_or_none(values):
    if len(values) < MIN_SEGMENT_FOR_MEDIAN:
        return None
    return int(round(statistics.median(values)))


def summarize(reviews):
    """reviews: {recommendationid: {'pt':int,'up':bool,'ts':int}} -> summary dict.
    Medians are in MINUTES (frontend converts to hours)."""
    up = [r["pt"] for r in reviews.values() if r.get("up")]
    down = [r["pt"] for r in reviews.values() if not r.get("up")]
    combined = up + down
    return {
        "median_up": _median_or_none(up),       # fans' median playtime (min)
        "median_down": _median_or_none(down),   # detractors' median playtime (min)
        "median_all": _median_or_none(combined),
        "n_up": len(up), "n_down": len(down), "n_all": len(combined),
    }


def load_raw_games():
    if not RAW_FILE.exists():
        return {}, None
    try:
        d = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}, None
    return d.get("games", {}), d.get("per_game_cap")


def save_summary(summary, per_game_cap):
    OUT_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()),
         "per_game_cap": per_game_cap,
         "min_segment": MIN_SEGMENT_FOR_MEDIAN,
         "count": len(summary),
         "playtime": summary},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_commit():
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "playtime.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            f"playtime summary: refreshed frontend playtime.json"], check=False)
            for _attempt in range(1, 9):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log("  committed playtime.json")
                    break
                import random
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git commit failed: {e}")


def main():
    raw_games, per_game_cap = load_raw_games()
    if not raw_games:
        log("No playtime_raw.json yet (run playtime_refresh.py first); nothing to summarize.")
        return 0

    summary = {}
    skipped = 0
    for aid, rec in raw_games.items():
        reviews = rec.get("reviews") or {}
        if not reviews:
            skipped += 1
            continue
        s = summarize(reviews)
        # Only publish games that have at least one usable segment median. A game
        # with a handful of reviews (all segments < MIN_SEGMENT) carries no median,
        # so there's nothing for the frontend to show — omit it to keep the file lean.
        if s["median_up"] is None and s["median_down"] is None and s["median_all"] is None:
            skipped += 1
            continue
        summary[aid] = s

    save_summary(summary, per_game_cap)
    git_commit()
    log(f"Summarized {len(summary)} games ({skipped} skipped: no usable median). "
        f"Wrote playtime.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
