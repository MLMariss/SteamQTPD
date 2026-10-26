#!/usr/bin/env python3
"""Tests for updates_refresh.py's per-run plan. Pure logic: Steam is stubbed, files live in a
temp dir, no git (gitsync commits nothing outside Actions).

What they pin: an answered check stamps scraped_at even when no event changed (the old code
kept the stale stamp, so FRESHNESS.md read 97% of the layer as overdue and every visit asked
the same games again); a failed fetch leaves the record alone; a run works through several
buckets, the one written longest ago first, instead of one bucket per run number."""
import json, pathlib, sys, tempfile, time, types

if "requests" not in sys.modules:
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(headers=type("H", (), {"update": lambda s, d: None})())
    m.RequestException = Exception
    sys.modules["requests"] = m

import updates_refresh as U

fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


D = 86400
U.time.sleep = lambda s: None
tmp = pathlib.Path(tempfile.mkdtemp())
U.SHARD_DIR, U.GAMES_FILE = tmp / "updates_raw", tmp / "games.json"
now = int(time.time())

print("\n== one probe ==")
shard = {"10": {"events": {"g1": {"ts": 5, "type": "minor", "et": 12}}, "scraped_at": now - 100 * D}}
U.fetch_events = lambda appid: {"g1": {"ts": 5, "type": "minor", "et": 12}}
check("unchanged events -> False", U.probe(10, shard) is False)
check("...and scraped_at is stamped anyway", now - shard["10"]["scraped_at"] < 60)
U.fetch_events = lambda appid: {"g2": {"ts": 9, "type": "major", "et": 13}}
check("a new event -> True", U.probe(10, shard) is True and "g2" in shard["10"]["events"])
before = dict(shard["10"])
U.fetch_events = lambda appid: None
check("a failed fetch -> None", U.probe(10, shard) is None)
check("...and the record is untouched", shard["10"] == before)
U.fetch_events = lambda appid: {}
check("a never-seen game with no updates is stored (so it waits its cooldown)",
      U.probe(20, shard) is True and shard["20"]["events"] == {})

print("\n== the sweep ==")
# appid -> bucket is (appid // 10) % 64; pick games in buckets 1, 2 and 3.
games = [{"appid": 10 + 640 * i, "review_count": 50, "last_update_ts": now - 10 * D} for i in range(3)]   # bucket 1
games += [{"appid": 20 + 640 * i, "review_count": 50} for i in range(2)]                                # bucket 2
games += [{"appid": 30, "review_count": 3}]                                                            # bucket 3, below the floor
U.GAMES_FILE.write_text(json.dumps({"games": games}))
U.SHARD_DIR.mkdir()
U.save_shard(1, {})
time.sleep(0)
json_b1 = json.loads(U.shard_path(1).read_text()); json_b1["generated_at"] = now - 5 * D
U.shard_path(1).write_text(json.dumps(json_b1))
U.save_shard(2, {"20": {"events": {}, "scraped_at": now - 50 * D}})
json_b2 = json.loads(U.shard_path(2).read_text()); json_b2["generated_at"] = now - 9 * D
U.shard_path(2).write_text(json.dumps(json_b2))
order = U.bucket_order()
check("never-written buckets first, then oldest-written", order.index(0) < order.index(2) < order.index(1))
asked = []
U.fetch_events = lambda appid: asked.append(appid) or {}
U.RUN_MINUTES = 5
U.main()
check(f"one run covers every bucket with due games ({sorted(asked)})",
      sorted(asked) == sorted([10, 650, 1290, 20, 660]))
check("a game below the review floor is not asked", 30 not in asked)
b2 = json.loads(U.shard_path(2).read_text())["games"]
check("bucket 2 written with fresh stamps", all(now - r["scraped_at"] < 60 for r in b2.values()))
asked.clear()
U.main()
check("a second run asks nothing (all inside their cooldown)", asked == [])

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
