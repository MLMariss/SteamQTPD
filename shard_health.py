#!/usr/bin/env python3
"""
Shard health monitor for playtime_raw/NN.json.

Scans the shards and writes SHARDS.md — a preventive-monitoring dashboard so the
100 MB/file wall that once silently froze the pipeline can never sneak up again. Tracks:
  * per-shard game count and file size (vs GitHub's 100 MB hard limit)
  * distribution evenness (max/mean) — catches a shard-key regression
  * a games-to-100MB projection per shard AND at full addressable coverage
  * freshness (bucket rotation means a shard being several days old is normal)

Read-only: touches no external service, so it's cheap and safe to run often. Pure —
does no git; the workflow commits SHARDS.md. Run: `python shard_health.py`.
"""
import glob
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARD_DIR = HERE / "playtime_raw"
GAMES_FILE = HERE / "games.json"
OUT = HERE / "SHARDS.md"

LIMIT = 100 * 1024 * 1024          # GitHub hard file-size limit (bytes)
WARN_MB = 50                       # GitHub's soft warning threshold
CRIT_MB = 80                       # our "act now" threshold (headroom getting thin)
MIN_REVIEWS_FLOOR = 10             # addressable = games with >= this many reviews (keep in lockstep with playtime_refresh.py)


def human(b):
    return f"{b/1048576:.2f} MB" if b >= 1048576 else f"{b/1024:.0f} KB"


def main():
    now = time.time()
    shards = sorted(SHARD_DIR.glob("*.json"))
    if not shards:
        OUT.write_text("# Shard health — `playtime_raw/` · QTPD (SteamQHPP)\n\nNo shards found yet "
                       "(the pipeline migrates/creates them on first run).\n", encoding="utf-8")
        print("no shards found")
        return 0

    rows = []              # (name, count, size_bytes, generated_at, shard_ver)
    total = 0
    vers = set()
    for p in shards:
        sz = p.stat().st_size
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            rows.append((p.name, -1, sz, 0, None))
            continue
        c = len(d.get("games", {}))
        total += c
        vers.add(d.get("shard_ver"))
        rows.append((p.name, c, sz, d.get("generated_at", 0), d.get("shard_ver")))

    n = len(rows)
    counts = sorted(r[1] for r in rows if r[1] >= 0)
    sizes = sorted(r[2] for r in rows)
    mean = total / n if n else 0
    maxsz = sizes[-1] if sizes else 0
    max_mb = maxsz / 1048576
    empty = sum(1 for r in rows if r[1] == 0)
    bad = sum(1 for r in rows if r[1] < 0)
    bpg = (maxsz / counts[-1]) if counts and counts[-1] > 0 else 0   # bytes/game, from the fattest shard

    # full-coverage projection: games evenly spread across n shards at current bytes/game
    addressable = None
    if GAMES_FILE.exists():
        try:
            g = json.loads(GAMES_FILE.read_text(encoding="utf-8")).get("games", [])
            addressable = sum(1 for x in g if (x.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)
        except ValueError:
            pass
    proj_full_mb = (addressable / n * bpg / 1048576) if (addressable and n and bpg) else None
    games_to_100 = int(LIMIT / bpg) if bpg else None
    skew = (counts[-1] / mean) if (counts and mean) else 0

    # --- health flags (the preventive part) ---
    flags = []
    if max_mb >= CRIT_MB:
        flags.append(f"\U0001F534 CRITICAL: biggest shard {max_mb:.1f} MB is near the 100 MB wall — raise NSHARDS or trim per-game data NOW.")
    elif max_mb >= WARN_MB:
        flags.append(f"\U0001F7E1 WARN: biggest shard {max_mb:.1f} MB (GitHub warns at 50 MB; hard-rejects at 100).")
    if proj_full_mb is not None and proj_full_mb >= CRIT_MB:
        flags.append(f"\U0001F534 CRITICAL: projected ~{proj_full_mb:.0f} MB/shard at full coverage — raise NSHARDS before backfilling further.")
    elif proj_full_mb is not None and proj_full_mb >= WARN_MB:
        flags.append(f"\U0001F7E1 WARN: projected ~{proj_full_mb:.0f} MB/shard at full coverage.")
    if skew > 3.0:
        flags.append(f"\U0001F7E1 WARN: distribution skew max/mean={skew:.1f}. One actively-scraped bucket can spike this transiently; a sustained skew means the shard key is clustering (check shard_of).")
    if len([v for v in vers if v is not None]) > 1:
        flags.append(f"\U0001F7E1 WARN: mixed shard_ver {sorted(v for v in vers if v is not None)} — a reshard may be mid-flight or incomplete.")
    if bad:
        flags.append(f"\U0001F534 CRITICAL: {bad} shard file(s) are unreadable JSON.")
    if not flags:
        flags.append("\U0001F7E2 OK: all shards well under 100 MB and evenly distributed.")

    # --- render SHARDS.md ---
    L = [
        "# Shard health — `playtime_raw/` · QTPD (SteamQHPP)",
        "",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} by `shard_health.py`._",
        "",
        "## Status",
        "",
    ]
    L += [f"- {f}" for f in flags]
    L += [
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Shards | {n} (non-empty {n - empty - bad}) |",
        f"| shard_ver | {sorted(v for v in vers if v is not None) or 'n/a'} |",
        f"| Total games | {total:,} |",
        f"| Games/shard | min {counts[0] if counts else 0} · median {counts[n//2] if counts else 0} · max {counts[-1] if counts else 0} (max/mean {skew:.2f}) |",
        f"| Size/shard | min {human(sizes[0])} · median {human(sizes[n//2])} · **max {human(maxsz)}** |",
        f"| Headroom on biggest | {(LIMIT - maxsz)/1048576:.1f} MB under the 100 MB limit |",
        f"| Bytes/game | ~{bpg/1024:.1f} KB |",
        f"| Games-to-100MB (per shard) | ~{games_to_100:,} |" if games_to_100 else "| Games-to-100MB | n/a |",
    ]
    if addressable:
        L.append(f"| Addressable universe (≥{MIN_REVIEWS_FLOOR} reviews) | {addressable:,} → ~{addressable // n:,}/shard at full coverage |")
    if proj_full_mb is not None:
        L.append(f"| **Projected max shard at full coverage** | **~{proj_full_mb:.0f} MB** |")
    L += [
        "",
        "## Per-shard",
        "",
        "| Bucket | Games | Size | % of 100 MB | Updated (UTC) |",
        "|---:|---:|---:|---:|---|",
    ]
    for name, c, sz, ts, _ver in rows:
        upd = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "—"
        cc = "**bad JSON**" if c < 0 else f"{c:,}"
        L.append(f"| {name[:2]} | {cc} | {human(sz)} | {100*sz/LIMIT:.1f}% | {upd} |")
    L += [
        "",
        "_Buckets rotate — each is scraped roughly every ~64 runs, so a shard being several "
        "days old is normal. The number that matters is **max shard size** vs the 100 MB limit._",
        "",
    ]
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")

    # echo to the run log too
    for f in flags:
        print(f)
    print(f"shards={n} total_games={total:,} max_shard={human(maxsz)} ({100*maxsz/LIMIT:.1f}% of 100MB)"
          + (f" projected_full=~{proj_full_mb:.0f}MB" if proj_full_mb is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
