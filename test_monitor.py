#!/usr/bin/env python3
"""Regression tests for monitor.py (the history behind monitor.html) and the scraper's per-run
record it reads. Pure logic only: files are faked in memory or in a temp dir, no network, no git."""
import json, pathlib, sys, tempfile, types
from datetime import datetime, timezone

if "requests" not in sys.modules:           # scraper.py imports requests; the tests don't use it
    m = types.ModuleType("requests")
    m.Session = lambda: None
    m.RequestException = Exception
    sys.modules["requests"] = m

import monitor as M
import scraper as S

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

T = 1_800_000_000
H, D = 3600, 86400

print("\n== ages: median and 95th percentile of per-game timestamps ==")
check("p50/p95 over 100 stamps", M.ages(T, [T - i * H for i in range(1, 101)]) == (51 * H, 96 * H))
check("missing / future / zero stamps ignored", M.ages(T, [None, 0, T + 5, T - H]) == (H, H))
check("nothing valid -> (None, None)", M.ages(T, [None, 0]) == (None, None))

print("\n== snapshot: one row from whatever files are readable ==")
FILES = {
    "games":   {"generated_at": T - 2 * H, "games": [
                   {"appid": 1, "review_count": 10, "scraped_at": T - D, "release_ts": T - 30 * D},
                   {"appid": 2, "review_count": 0,  "scraped_at": T - 3 * D, "release_ts": T - 900 * D},
                   {"appid": 3, "review_count": 5,  "scraped_at": T - 2 * D}]},
    "prices":  {"generated_at": T - H, "prices": {"1": {"scraped_at": T - H}, "2": {"scraped_at": T - H},
                                                  "99": {"scraped_at": T - H}}},      # 99: not in catalog
    "recent":  {"generated_at": T - 4 * H, "recent": {"1": {"recent_count": 7, "recent_scraped_at": T - 5 * D},
                                                      "2": {"recent_count": 0, "recent_scraped_at": T - 5 * D}}},
    "hltb":    {"generated_at": T - 3 * H, "hltb": {"1": {"main": 10, "fetched_at": T - 9 * D},
                                                    "2": {"main": None, "fetched_at": T - 9 * D}}},
    "playtime": {"generated_at": T - H, "playtime": {"1": [1, 2, 3, 4], "3": [1, 1, 1, 1]}},
    "pics":    {"generated_at": T - 6 * H, "apps": {"1": {"type": "Game"}, "2": {"type": "Game"}, "3": {}}},
    "catalog": {"pending": {"7": None, "8": 1}},
    "demos":   {"generated_at": T - 4 * H, "demos": {"1": 11, "99": 12}},
    "shots_state": {"generated_at": T - 5 * H},
    "shots":   {"shots": {"2": ["ss.jpg"], "3": ["ss.jpg"], "99": ["ss.jpg"]}},
}
row = M.snapshot(T, "cron", lambda n: FILES.get(n), {"tags": T - 10 * H})
check("shape follows SNAP_FORMAT", len(row) == len(M.SNAP_FORMAT) and row[:4] == [T, "cron", 3, 2])
age = dict(zip(M.FILES, row[4]))
check("file age from the file's own generated_at", age["games"] == 2 * H and age["prices"] == H)
check("file age from git when the file isn't read (backfill)", age["tags"] == 10 * H)
check("unread file with no git time -> None", age["trailers"] is None)
p50 = dict(zip(M.ROW_AGES, row[5]))
check("games row age: median of 1d/2d/3d", p50["games"] == 2 * D)
check("prices row age counts catalog games only", p50["prices"] == H)
check("games split: released within a year", p50["games_young"] == D)
check("games split: older (3d) and undated (2d) together, upper median", p50["games_old"] == 3 * D)
check("an older snapshot's shorter arrays still line up by name", M.ROW_AGES[:4] == ["games", "prices", "recent", "hltb"])
cov = dict(zip(M.COVERAGE, row[7]))
check("reviews = games with a review count", cov["reviews"] == 2)
check("prices coverage excludes a non-catalog appid", cov["prices"] == 2)
check("recent coverage needs a 30-day count", cov["recent"] == 1)
check("hltb coverage needs a time", cov["hltb"] == 1)
check("playtime and pics counted", cov["playtime"] == 2 and cov["pics"] == 2)
check("file not read -> coverage None (not 0)", cov["tags"] is None)
check("demos counted over catalog games", cov["demos"] == 1)
check("screenshots counted from the shards, catalog games only", cov["shots"] == 2)
check("screenshot age is the state file's", age["shots_state"] == 5 * H and age["demos"] == 4 * H)
check("no games.json -> no snapshot", M.snapshot(T, "cron", lambda n: None) is None)

print("\n== runs: the scraper's own lines win over commit messages ==")
msg = M.runs_from_messages([
    ("a", T - 9 * D, "scrape: 2256 games (+2103 new, 134 refreshed)"),
    ("b", T - 8 * D, "scrape: 133464 games (+72 new, 8667 refreshed, 3240 pending)"),
    ("c", T - 7 * D, "checkpoint: 133477 games stored"),
    ("d", T - 1 * D, "scrape: 133500 games (+5 new, 100 refreshed, 3000 pending)")])
check("both message formats parsed, checkpoints ignored", len(msg) == 3)
check("June format has no pending", msg[0][2:6] == [2256, 2103, 134, None])
check("later format carries pending", msg[1][2:6] == [133464, 72, 8667, 3240])
own = [[T - 2 * D, T - 2 * D + 5 * H, 133490, 3, 50, 3100, 9000, 12, 0, True]]
runs = M.merge_runs([], own, msg)
check("message runs kept only before the first own run", [r[1] for r in runs if r[0] is None] == [T - 9 * D, T - 8 * D])
check("own run kept with its review counts", [r[6] for r in runs if r[0] is not None] == [9000])
again = M.merge_runs(runs, [own[0][:2] + [133495, 4, 60, 3100, 9100, 12, 0, True]])
check("an own run updated in place, not duplicated", len(again) == 3 and [r[2] for r in again if r[0]][0] == 133495)

print("\n== backfill: one point a day, the last commit before noon ==")
day = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
commits = [("s1", day("2026-07-01T05:00"), "x"), ("s2", day("2026-07-01T11:59"), "x"),
           ("s3", day("2026-07-01T13:00"), "x"), ("s4", day("2026-07-03T09:00"), "x")]
pts = M.daily_points(commits, {"2026-07-02"})
check("day 1 takes the last commit before 12:00", pts[0] == (day("2026-07-01T12:00"), "s2"))
check("a day already in monitor.json is skipped", all(t != day("2026-07-02T12:00") for t, _ in pts))
check("day 3 takes that morning's commit", (day("2026-07-03T12:00"), "s4") in pts)
check("last_before finds the latest at or before t", M.last_before([10, 20, 30], 25) == 20 and M.last_before([10], 5) is None)

print("\n== monitor.json: round trip, dedupe, cap ==")
tmp = pathlib.Path(tempfile.mkdtemp())
M.OUT_FILE = tmp / "monitor.json"
check("missing file -> empty history", M.load_monitor() == {"snaps": [], "runs": []})
out = M.save_monitor({"snaps": [row, row, [T - D] + row[1:]], "runs": runs})
check("snaps deduped by t and sorted", [s[0] for s in out["snaps"]] == [T - D, T])
back = M.load_monitor()
check("formats written, so the page can read by name", back["_snap"] == M.SNAP_FORMAT and back["files"] == M.FILES)
M.OUT_FILE.write_text(json.dumps({"_snap": ["old"], "snaps": [[1]]}), encoding="utf-8")
check("a different format is not misread", M.load_monitor() == {"snaps": [], "runs": []})
M.MAX_SNAPS, keep = 2, M.MAX_SNAPS
check("history capped, newest kept", [s[0] for s in M.save_monitor({"snaps": [[1] + row[1:], [2] + row[1:], [3] + row[1:]], "runs": []})["snaps"]] == [2, 3])
M.MAX_SNAPS = keep

print("\n== scraper: record_run upserts this run's line ==")
S.REVIEW_STATS.clear(); S.REVIEW_STATS.update(answered=90, failed=3)
cat = {}
S.record_run(cat, 1000, 50, 1, 2, 3, final=False)
S.record_run(cat, 1000, 51, 2, 4, 3, final=True)
S.record_run(cat, 2000, 52, 0, 1, 0, final=False)
check("one line per run start", [r["start"] for r in cat["runs"]] == [1000, 2000])
check("the later save wins", cat["runs"][0]["stored"] == 51 and cat["runs"][0]["final"] is True)
check("review counts carried", cat["runs"][0]["reviews_answered"] == 90 and cat["runs"][0]["reviews_failed"] == 3)
check("monitor reads it in RUN_FORMAT order", M.run_row(cat["runs"][0])[:6] == [1000, cat["runs"][0]["saved"], 51, 2, 4, 3])
S.RUNS_KEPT, keep = 2, S.RUNS_KEPT
for s in (3000, 4000):
    S.record_run(cat, s, 1, 0, 0, 0, final=True)
check("run list bounded, newest kept", [r["start"] for r in cat["runs"]] == [3000, 4000])
S.RUNS_KEPT = keep

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
