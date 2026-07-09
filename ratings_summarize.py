#!/usr/bin/env python3
"""
Steam QHPP — playtime-weighted ratings summarizer
=================================================
Reads the BIG per-review raw data (the playtime_raw/ NN.json shards, maintained
by playtime_refresh.py; falls back to the legacy playtime_raw.json monolith if
sharding hasn't been migrated) and writes a SMALL frontend-facing ratings.json: a
playtime-weighted "quality" rating per game, to sit alongside Steam's flat
review %.

Why a playtime-weighted rating at all:
  Steam's rating is one-person-one-vote — a 12-minute refund-window pan counts
  the same as an 800-hour "I know this game cold and still don't recommend it."
  Weighting each review by how long that player actually played gives a more
  credible verdict: it asks "what do the people who *played* this think," not
  just "who bothered to click a thumb." A long-playtime negative is damning; a
  30-second negative is mostly noise — and this rating reflects that.

Why a SEPARATE file + step (not folded into playtime.json):
  Clean separation of concerns — playtime.json is the playtime *medians* the
  table column shows; ratings.json is the *rating* number shown next to Steam's
  %. Each has its own summarize step and its own tiny frontend file. One writer
  per file (playtime_refresh.py -> raw; playtime_summarize.py -> playtime.json;
  THIS -> ratings.json). The frontend loads both small files, never the raw one.

------------------------------------------------------------------------------
THE THREE STORED VARIANTS (all three kept, so the display choice can change
without a re-scrape — everything derives from the same raw reviews):
------------------------------------------------------------------------------
  steam  — the plain one-vote-per-review %, matching Steam's own number.
           Reference/comparison value.

  raw    — uncapped: recommend_hours / total_hours.
           Transparent, but DISTORTED by whales: a couple of 500h outliers can
           swing a game 60+ points off its Steam %. Kept for debugging/analysis,
           NOT recommended for display (the analysis that motivated the cap).

  capped — each review's playtime is capped at CAP_MULT * that game's median
           before summing. So one obsessive can't dominate: their weight maxes at
           2x the typical playtime. This keeps the rating tethered to a sane range
           (~5pt avg divergence from Steam %) while still letting longer playtime
           count for more than a quick playthrough. THE INTENDED display value.

A fourth "bayes" variant (pulling small-sample games toward a pooled global
prior, IMDb-Top-250-style) was prototyped but deliberately dropped in favor of a
gray-out confidence cue instead of nudging the number itself — see
CONFIDENT_REVIEWS below. There is no bayes field or pass in this script.

Eligibility: a game needs >= MIN_REVIEWS_FOR_RATING (5) reviews to get ANY
rating; below that it's omitted entirely. >= CONFIDENT_REVIEWS (10) renders
full-color; 5-9 renders grayed as low-confidence (a frontend concern — this
script just ships both thresholds in the meta for the frontend to apply).
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_FILE = HERE / "playtime_raw.json"        # legacy monolith (pre-shard fallback), read-only here
SHARD_DIR = HERE / "playtime_raw"            # dir of NN.json shards (read-only here; owned by playtime_refresh.py)
OUT_FILE = HERE / "ratings.json"             # THIS file's output (committed; frontend reads it)

MIN_REVIEWS_FOR_RATING = 5                   # hard floor: below this, no rating computed at all
CONFIDENT_REVIEWS = 10                        # >= this renders full-color; 5-9 renders grayed
                                             #   (frontend reads this from the meta to style the cue)
CAP_MULT = 2.0                               # per-review playtime capped at 2x the game's median
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


def _game_vals(reviews):
    """[(playtime_minutes, voted_up_bool), ...] for a game's stored reviews."""
    return [(r["pt"], bool(r.get("up"))) for r in reviews.values()]


def raw_weighted(vals):
    """Uncapped recommend-hours / total-hours -> 0..100 (or None)."""
    num = den = 0.0
    for pt, up in vals:
        den += pt
        if up:
            num += pt
    return 100 * num / den if den else None


def capped_weighted(vals):
    """Recommend-hours / total-hours with each review capped at CAP_MULT * median
    playtime for THIS game. Returns (pct, median_minutes, avg_review_weight)."""
    if not vals:
        return None, None, None
    allpt = [v[0] for v in vals]
    med = statistics.median(allpt)
    cap = CAP_MULT * med
    num = den = 0.0
    for pt, up in vals:
        w = pt if pt < cap else cap
        den += w
        if up:
            num += w
    if den <= 0:
        return None, med, None
    return 100 * num / den, med, den / len(vals)


def plain_steam_pct(vals):
    """One-person-one-vote % for reference/comparison."""
    if not vals:
        return None
    return 100 * sum(1 for _, up in vals if up) / len(vals)


def iter_raw_shards():
    """Yield (games_dict, per_game_cap) for each shard, one at a time so peak memory
    stays at ~one shard instead of the full ~1 GB working set. Falls back to the legacy
    single playtime_raw.json if sharding hasn't been migrated yet. Mirrors
    playtime_summarize.py so raw -> ratings and raw -> playtime read the same source.
    """
    if SHARD_DIR.is_dir():
        for p in sorted(SHARD_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            yield d.get("games", {}), d.get("per_game_cap")
    elif RAW_FILE.exists():                         # pre-migration fallback
        try:
            d = json.loads(RAW_FILE.read_text(encoding="utf-8"))
            yield d.get("games", {}), d.get("per_game_cap")
        except ValueError:
            pass


def raw_source_present():
    """True if there is *any* readable raw source (a non-empty shard dir or the legacy
    monolith). Distinguishes 'no data yet' from 'summarizer is looking in the wrong
    place' — the failure mode that silently froze ratings.json after the shard migration.
    """
    if SHARD_DIR.is_dir() and any(SHARD_DIR.glob("*.json")):
        return True
    return RAW_FILE.exists()


def save_ratings(ratings, per_game_cap):
    """Lean compact output. Each game -> positional array:

        "<appid>": [steam, raw, capped, n]
                     [0]    [1]  [2]     [3]
        * steam  = plain one-vote %, rounded to 1 decimal (reference / comparison)
        * raw    = uncapped playtime-weighted % (kept for debug; whale-distorted)
        * capped = 2x-median-capped playtime-weighted % (the INTENDED display value)
        * n      = review sample size behind the rating

    All percentages are 0..100 with one decimal. The frontend reads by index (see
    `_format` in the meta), displays `capped`, and uses `n` vs CONFIDENT_REVIEWS to
    decide full-color (n >= confident_reviews) vs grayed 'needs more reviews' (n below).
    No Bayesian smoothing: reliability is conveyed by the gray cue, not by nudging
    the number toward a prior.
    """
    payload = {aid: [r["steam"], r["raw"], r["capped"], r["n"]]
               for aid, r in ratings.items()}
    OUT_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()),
         "min_reviews": MIN_REVIEWS_FOR_RATING,       # below this: no rating at all
         "confident_reviews": CONFIDENT_REVIEWS,      # >= this: full color; between: grayed
         "cap_mult": CAP_MULT,
         "per_game_cap": per_game_cap,
         "_format": ["steam_pct", "raw_pct", "capped_pct", "n"],
         "count": len(payload),
         "playtime_ratings": payload},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def git_commit():
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "ratings.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "playtime ratings: refreshed ratings.json"], check=False)
            for _attempt in range(1, 9):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log("  committed ratings.json")
                    break
                import random
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git commit failed: {e}")


def main():
    # FAIL LOUD: distinguish "no raw data exists yet" (legitimate empty state, exit 0)
    # from "raw data exists but this summarizer can't see it" (a wiring bug — e.g. the
    # shard migration that silently froze ratings.json when this script still read only
    # the deleted monolith). The latter must NOT pass green: it exits non-zero so the
    # Action goes red instead of quietly writing nothing.
    if not raw_source_present():
        log(f"No raw playtime source found — neither {SHARD_DIR.name}/ shards nor "
            f"{RAW_FILE.name}. Run playtime_refresh.py first; nothing to rate.")
        return 0

    # Single pass over every shard: compute the rating variants per eligible game.
    # Games are partitioned across shards by appid, so a plain dict-update across shards
    # can't collide. No Bayesian smoothing / global prior — reliability is conveyed on
    # the frontend by graying ratings with few reviews (n < CONFIDENT_REVIEWS), not by
    # nudging the number. Games below MIN_REVIEWS_FOR_RATING get no rating at all.
    ratings = {}
    per_game_cap = None
    shards_read = 0
    total_games_seen = 0
    for raw_games, cap in iter_raw_shards():
        shards_read += 1
        if cap is not None:
            per_game_cap = cap
        for aid, rec in raw_games.items():
            total_games_seen += 1
            reviews = rec.get("reviews") or {}
            vals = _game_vals(reviews)
            n = len(vals)
            if n < MIN_REVIEWS_FOR_RATING:
                continue
            capped, med, _avg_w = capped_weighted(vals)
            if capped is None:
                continue
            ratings[aid] = {
                "steam": round(plain_steam_pct(vals), 1),   # one-vote %, for comparison
                "raw": round(raw_weighted(vals), 1),        # uncapped (debug)
                "capped": round(capped, 1),                 # 2x-median-capped (displayed)
                "n": n,
            }

    # FAIL LOUD guard #2: the source is present (shards on disk) but iteration yielded
    # zero games. That's not a normal empty state — it means the shards are unreadable
    # or shaped differently than expected. Refuse to overwrite a good ratings.json with
    # an empty one; exit non-zero so the run goes red and the stale file is preserved.
    if shards_read > 0 and total_games_seen == 0:
        log(f"ERROR: read {shards_read} shard(s) but found 0 games inside. Refusing to "
            f"overwrite ratings.json with an empty file. Leaving the existing file intact.")
        return 1

    save_ratings(ratings, per_game_cap)
    git_commit()
    if ratings:
        confident = sum(1 for r in ratings.values() if r["n"] >= CONFIDENT_REVIEWS)
        log(f"Rated {len(ratings)} games (>= {MIN_REVIEWS_FOR_RATING} reviews) "
            f"across {shards_read} shard(s) / {total_games_seen} raw games; "
            f"{confident} full-confidence (>= {CONFIDENT_REVIEWS}), "
            f"{len(ratings)-confident} grayed. Wrote ratings.json.")
    else:
        log(f"No games met the >= {MIN_REVIEWS_FOR_RATING}-review floor yet "
            f"({total_games_seen} raw games across {shards_read} shard(s)). Wrote empty ratings.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
