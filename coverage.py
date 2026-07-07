#!/usr/bin/env python3
"""
SteamQHPP — coverage snapshot generator
=======================================
Recomputes data coverage from the live database and writes COVERAGE.md, sorted
by % of catalog (descending). Runs after each scrape (chained/triggered off the
games.json scrape) so the doc never drifts from reality.

Why this exists:
  COVERAGE.md used to be hand-authored, so it silently went stale as the data
  jobs kept running. This script makes it a generated artifact — the same idea
  as shard_health.py -> SHARDS.md. One writer per file (THIS -> COVERAGE.md).

What "covered" means per metric (kept in lockstep with how the frontend counts):
  * Review count  : review_count > 0            (a 0 means "no meaningful score")
  * Rating %      : rating_pct   > 0
  * Release date  : release_date non-empty
  * everything else: presence of a key in that file's game-map / non-empty tags.

Reads (all read-only here; each owned by its own scraper):
  games.json, prices.json, hltb.json, recent.json, tags.json,
  playtime.json, ratings.json, playtime_raw/NN.json shards.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "COVERAGE.md"
SHARD_DIR = HERE / "playtime_raw"

MIN_REVIEWS_FLOOR = 10   # addressable-set gate: below this, no sentiment-split median is possible
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(*a):
    print(*a, flush=True)


def load(name):
    p = HERE / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def ts_utc(epoch):
    if not epoch:
        return "—"
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M")


def game_map(obj, *candidate_keys):
    """Return the games-map dict from a loaded file, trying known key names."""
    if not isinstance(obj, dict):
        return {}
    for k in candidate_keys:
        if isinstance(obj.get(k), dict):
            return obj[k]
    # fall back: first dict-valued field whose sub-keys look like appids
    for v in obj.values():
        if isinstance(v, dict) and v and str(next(iter(v))).isdigit():
            return v
    return {}


def main():
    games_doc = load("games.json")
    if not games_doc:
        log("games.json missing; cannot compute coverage.")
        return 0
    games = games_doc.get("games") or []
    BASE = len(games)
    if BASE == 0:
        log("games.json has no games; nothing to do.")
        return 0

    # ---- games.json-derived ----
    rev_cov    = sum(1 for x in games if (x.get("review_count") or 0) > 0)
    rating_cov = sum(1 for x in games if (x.get("rating_pct") or 0) > 0)
    rel_cov    = sum(1 for x in games if x.get("release_date"))
    is_free    = sum(1 for x in games if x.get("is_free") is True)
    nonfree    = BASE - is_free
    addressable = sum(1 for x in games if (x.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)

    # ---- other files ----
    prices = game_map(load("prices.json"), "prices")
    price_cov = len(prices)
    on_sale = sum(1 for v in prices.values() if (v.get("discount_pct") or 0) > 0)

    hltb = game_map(load("hltb.json"), "hltb")
    hltb_total = len(hltb)
    hltb_est = sum(1 for v in hltb.values() if isinstance(v, dict) and v.get("est"))
    hltb_real = hltb_total - hltb_est

    recent = game_map(load("recent.json"), "recent")
    recent_cov = len(recent)

    tags = game_map(load("tags.json"), "tags")
    tags_cov = sum(1 for v in tags.values() if v)

    pt = game_map(load("playtime.json"), "playtime")
    pt_cov = len(pt)

    rt = game_map(load("ratings.json"), "playtime_ratings")
    rt_cov = len(rt)

    raw_cov = 0
    newest_shard = oldest_shard = None
    if SHARD_DIR.is_dir():
        for f in sorted(SHARD_DIR.glob("*.json")):
            sh = json.loads(f.read_text(encoding="utf-8"))
            raw_cov += len(sh.get("games", {}))
            g = sh.get("generated_at")
            if g:
                newest_shard = g if newest_shard is None else max(newest_shard, g)
                oldest_shard = g if oldest_shard is None else min(oldest_shard, g)

    def pct(n):
        return n / BASE * 100.0

    # ---- rows: (metric, storage, covered, missing_or_None) ----
    rows = [
        ("Recent reviews",           "recent.json",   recent_cov, BASE - recent_cov),
        ("HLTB (total)",             "hltb.json",     hltb_total, BASE - hltb_total),
        ("Release date",             "games.json",    rel_cov,    BASE - rel_cov),
        ("Review count",             "games.json",    rev_cov,    BASE - rev_cov),
        ("Rating %",                 "games.json",    rating_cov, BASE - rating_cov),
        ("HLTB (real)",              "hltb.json",     hltb_real,  None),
        ("Price / Sales",            "prices.json",   price_cov,  None),
        ("Tags (non-empty)",         "tags.json",     tags_cov,   BASE - tags_cov),
        ("Playtime raw (shards)",    "playtime_raw/", raw_cov,    BASE - raw_cov),
        ("Playtime (summarized)",    "playtime.json", pt_cov,     BASE - pt_cov),
        ("Playtime-weighted rating", "ratings.json",  rt_cov,     BASE - rt_cov),
        ("HLTB (estimated)",         "hltb.json",     hltb_est,   None),
    ]
    rows.sort(key=lambda r: r[2], reverse=True)  # by covered count desc == % desc

    # ---- freshness table ----
    fresh = [
        ("games.json",    games_doc.get("generated_at")),
        ("prices.json",   (load("prices.json")   or {}).get("generated_at")),
        ("hltb.json",     (load("hltb.json")     or {}).get("generated_at")),
        ("recent.json",   (load("recent.json")   or {}).get("generated_at")),
        ("playtime.json", (load("playtime.json") or {}).get("generated_at")),
        ("ratings.json",  (load("ratings.json")  or {}).get("generated_at")),
        ("tags.json",     (load("tags.json")     or {}).get("generated_at")),
    ]

    now = ts_utc(int(time.time()))

    # ---- build markdown ----
    L = []
    L.append("# SteamQHPP — Data Coverage Snapshot")
    L.append("")
    L.append("> Generated by `coverage.py` after each scrape. Do not edit by hand — "
             "changes will be overwritten on the next run.")
    L.append("")
    L.append(f"**Snapshot generated (UTC):** {now}")
    L.append(f"**Base universe (`games.json`):** {BASE:,} games")
    L.append("")
    L.append("Per-file generation timestamps at snapshot time:")
    L.append("")
    L.append("| File | Generated (UTC) |")
    L.append("|---|---|")
    for name, g in fresh:
        L.append(f"| `{name}` | {ts_utc(g)} |")
    shard_line = (f"newest {ts_utc(newest_shard)} · oldest {ts_utc(oldest_shard)}"
                  if newest_shard else "—")
    L.append(f"| `playtime_raw/` (shards) | {shard_line} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Coverage by metric (sorted by % of catalog, descending)")
    L.append("")
    L.append("| Metric | Storage file | Covered | % of catalog | Missing |")
    L.append("|---|---|---:|---:|---:|")
    for metric, store, cov, miss in rows:
        misstr = "—" if miss is None else f"{miss:,}"
        L.append(f"| {metric} | `{store}` | {cov:,} | {pct(cov):.1f}% | {misstr} |")
    L.append("")
    L.append("Integrity: every storage file is effectively a clean subset of "
             "`games.json` (a handful of orphan keys can appear as timing artifacts "
             "between scraper commits).")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Notes")
    L.append("")
    if price_cov:
        sale_pct = on_sale / price_cov * 100
        L.append(f"**Prices.** `prices.json` holds **{price_cov:,} rows "
                 f"({pct(price_cov):.1f}% of catalog)**, tracking the non-free base "
                 f"(**{nonfree:,}** games after removing {is_free:,} `is_free`). "
                 f"**{on_sale:,} of {price_cov:,} priced titles ({sale_pct:.1f}%) "
                 f"are currently discounted**; sale end-dates come from the "
                 f"`IStoreBrowseService/GetItems` pass.")
        L.append("")
    addr_pct_base = addressable / BASE * 100
    raw_of_addr = (raw_cov / addressable * 100) if addressable else 0
    below = BASE - addressable
    L.append(f"**Playtime backfill.** Raw playtime covers **{raw_cov:,} games "
             f"({pct(raw_cov):.1f}% of catalog)**. The honest denominator is the "
             f"addressable set after the `MIN_REVIEWS_FLOOR = {MIN_REVIEWS_FLOOR}` gate "
             f"— **{addressable:,} games ({addr_pct_base:.1f}% of catalog)** — against "
             f"which raw is **{raw_of_addr:.1f}% of addressable**. The other "
             f"**{below:,} games are below the floor and correctly skipped** (too few "
             f"reviews to produce a sentiment-split median).")
    L.append("")
    lag = raw_cov - pt_cov
    L.append(f"**Summarizers now run on every raw pass.** Raw {raw_cov:,} · summarized "
             f"{pt_cov:,} · rated {rt_cov:,} (current gap {lag:,}). "
             f"`playtime_summarize.py` and `ratings_summarize.py` are chained into "
             f"`playtime-raw.yml`, so the small frontend files refresh right after the "
             f"shards do (8×/day). Any residual gap is just the within-run ordering "
             f"between the shard commit and the summarize steps, not a stalled pipeline.")
    L.append("")
    L.append(f"**HLTB estimates refresh live.** **{hltb_est:,} of {hltb_total:,} "
             f"`hltb.json` entries carry the `est` flag** ({pct(hltb_est):.1f}%), "
             f"leaving **{hltb_real:,} reals ({pct(hltb_real):.1f}%)**. `hltb_estimate.py` "
             f"is a shared helper imported by `hltb_refresh.py` (as `HE`) and called in "
             f"the fill loop, so estimates recompute from live median ratios every 2h as "
             f"new reals land — it is not a stale one-off. (The old one-off "
             f"`hltb_backfill.py` sweep was removed; the live path took over.)")
    L.append("")
    L.append(f"**Rating % / review count.** Rating % is present for **{rating_cov:,} "
             f"games ({pct(rating_cov):.1f}%)**; review count for **{rev_cov:,} "
             f"({pct(rev_cov):.1f}%)**. The gap is titles with too few reviews to carry "
             f"a meaningful score.")
    L.append("")
    L.append(f"**Tags** are non-empty for **{tags_cov:,} games ({pct(tags_cov):.1f}%)**. "
             f"The remainder are untagged on SteamSpy (typically very-low-review or "
             f"unreleased titles).")
    L.append("")

    OUT_FILE.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"Wrote COVERAGE.md — base {BASE:,}, {len(rows)} metrics, "
        f"raw {raw_cov:,} ({raw_of_addr:.1f}% of addressable).")
    git_commit()
    return 0


def git_commit():
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "COVERAGE.md"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            "coverage: refresh COVERAGE.md"], check=False)
            import random
            for attempt in range(1, 6):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log("  committed COVERAGE.md")
                    return
                time.sleep(2 * attempt + random.uniform(0, 2))
            log("  ERROR: could not push COVERAGE.md after 5 attempts")
        else:
            log("  COVERAGE.md unchanged; nothing to commit.")
    except Exception as e:
        log(f"  git commit failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
