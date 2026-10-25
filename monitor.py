#!/usr/bin/env python3
"""
monitor.py — the pipeline's history, for monitor.html
=====================================================
FRESHNESS.md and COVERAGE.md say how the data stands NOW, and the next run overwrites them.
This keeps the history: one compact snapshot per capture, appended to monitor.json, which the
monitoring page (monitor.html, opened from the data-age counter on the main page) charts.
The review wipe of 22-24 Oct 2026 (ARCHITECTURE §16) ran for a day and a half partly because
nothing showed a number going the wrong way over time; on these charts it would have shown
as a cliff in review coverage on the first snapshot after it began.

Each snapshot records, at time t:
  * games / pending        catalog size and the unreleased waiting room
  * file_age   per file    seconds since the file was last written (its own generated_at)
  * row_p50/95 per file    median and 95th-percentile age of the per-game timestamps
                           (games scraped_at, prices scraped_at, recent recent_scraped_at,
                           hltb fetched_at): how old the data is that a visitor sees
  * coverage   per layer   how many catalog games carry it (reviews, price, 30-day score,
                           playtime, weighted rating, HLTB, tags, trailer, length, PICS)
and the scraper's per-run lines (catalog.json `runs`: new / refreshed / pending and the
review fetches answered vs failed) are carried over as they are.

Modes
  python monitor.py              one snapshot from the working tree (MONITOR_SRC names the
                                 trigger: scrape / cron / manual), appended to monitor.json
  python monitor.py --backfill   replay git history: every scrape commit's numbers from its
                                 message, plus one full snapshot per day, its files streamed
                                 from raw.githubusercontent.com at that commit (nothing is
                                 written to disk, so ~20 GB of history fits a runner). Needs a
                                 full-history checkout; days already in monitor.json are skipped,
                                 so it can be re-run after a timeout.

One writer per file: only this script writes monitor.json (workflow 4.5). Stdlib only.
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "monitor.json"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
REPO = os.environ.get("GITHUB_REPOSITORY", "MLMariss/SteamQTPD")

# Every file whose age is tracked, in the order of the file_age array.
FILES = ["games", "prices", "recent", "playtime", "ratings", "hltb", "tags", "trailers",
         "length", "updates", "pics", "upcoming"]
# Files with a per-game timestamp, in the order of the row_p50 / row_p95 arrays.
ROW_AGES = ["games", "prices", "recent", "hltb"]
# Coverage layers, in the order of the coverage array; each is counted over catalog games only.
COVERAGE = ["reviews", "prices", "recent", "playtime", "ratings", "hltb", "tags", "trailers",
            "length", "pics"]
# The backfill streams only these (~145 MB a day); tags, trailers, pics, updates and upcoming
# would add ~130 MB for coverage alone, so history has their file age (from git) but no counts.
BACKFILL_FILES = ["games", "prices", "recent", "playtime", "ratings", "hltb", "length"]

SNAP_FORMAT = ["t", "src", "games", "pending", "file_age", "row_p50", "row_p95", "coverage"]
RUN_FORMAT = ["start", "saved", "stored", "new", "refreshed", "pending",
              "reviews_answered", "reviews_failed", "reviews_zero_kept", "final"]
MAX_SNAPS = 20000                # ~3 years at 16 a day; the oldest drop off first
BACKFILL_HOUR = 12               # the daily backfill point: the last commit before 12:00 UTC

# "scrape: 133464 games (+72 new, 8667 refreshed, 3240 pending)"; June 2026 had no pending.
SCRAPE_MSG = re.compile(r"^scrape: (\d+) games \(\+(\d+) new, (\d+) refreshed(?:, (\d+) pending)?\)")


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Reading one file's contribution
# --------------------------------------------------------------------------- #
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _keyed(d, key):
    """The {appid: entry} map under `key`, with int appids."""
    m = (d or {}).get(key) or {}
    return {a: v for a, v in ((_int(k), v) for k, v in m.items()) if a is not None}


def per_game(name, d, appids):
    """(timestamps, covered) for one parsed file, restricted to catalog appids: the per-game
    timestamps behind row_p50/95 (or None for files without one) and how many games carry the
    layer (or None for files that aren't a coverage layer)."""
    if name == "prices":
        m = _keyed(d, "prices")
        return ([v.get("scraped_at") for a, v in m.items() if a in appids and isinstance(v, dict)],
                sum(1 for a in m if a in appids))
    if name == "recent":
        m = _keyed(d, "recent")
        return ([v.get("recent_scraped_at") for a, v in m.items() if a in appids and isinstance(v, dict)],
                sum(1 for a, v in m.items() if a in appids and isinstance(v, dict) and v.get("recent_count")))
    if name == "hltb":
        m = _keyed(d, "hltb")
        times = ("main", "extra", "complete", "avg")
        return ([v.get("fetched_at") for a, v in m.items() if a in appids and isinstance(v, dict)],
                sum(1 for a, v in m.items() if a in appids and isinstance(v, dict)
                    and any(v.get(k) for k in times)))
    key = {"playtime": "playtime", "ratings": "playtime_ratings", "tags": "tags",
           "trailers": "trailers", "length": "length", "pics": "apps"}.get(name)
    if key is None:
        return None, None                      # updates / upcoming: file age only
    m = _keyed(d, key)
    return None, sum(1 for a, v in m.items() if a in appids and v)


def ages(t, stamps):
    """(p50, p95) of t - stamp in seconds over the valid stamps, or (None, None)."""
    vals = sorted(t - s for s in (_int(x) for x in stamps or []) if s and 0 < s <= t)
    if not vals:
        return None, None
    pick = lambda q: vals[min(len(vals) - 1, int(q * len(vals)))]
    return pick(0.50), pick(0.95)


def snapshot(t, src, read, file_times=None):
    """One snapshot row at time t. `read(name)` returns that file parsed, or None when it isn't
    available; files are read one at a time and dropped, so peak memory stays one file's worth.
    `file_times` gives a file's last-write time where the file itself isn't read (backfill)."""
    file_times = file_times or {}
    games_doc = read("games")
    if not games_doc:
        return None
    games = games_doc.get("games") or []
    appids = {g.get("appid") for g in games}
    stamp = {"games": games_doc.get("generated_at")}
    p50 = {}
    p95 = {}
    cov = {"reviews": sum(1 for g in games if g.get("review_count"))}
    p50["games"], p95["games"] = ages(t, [g.get("scraped_at") for g in games])
    del games_doc, games

    for name in FILES[1:]:
        d = read(name)
        if d is None:
            continue
        stamp[name] = d.get("generated_at") or d.get("swept_at")
        stamps, covered = per_game(name, d, appids)
        if name in ROW_AGES:
            p50[name], p95[name] = ages(t, stamps)
        if name in COVERAGE:
            cov[name] = covered
        del d

    catalog = read("catalog")
    pending = len((catalog or {}).get("pending") or {}) if catalog else None

    def age(name):
        ts = _int(stamp.get(name)) or _int(file_times.get(name))
        return t - ts if ts and ts <= t else None

    return [t, src, len(appids), pending,
            [age(n) for n in FILES],
            [p50.get(n) for n in ROW_AGES], [p95.get(n) for n in ROW_AGES],
            [cov.get(n) for n in COVERAGE]]


# --------------------------------------------------------------------------- #
# monitor.json
# --------------------------------------------------------------------------- #
def load_monitor():
    try:
        d = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if d.get("_snap") == SNAP_FORMAT and d.get("_run") == RUN_FORMAT:
            return d
        log("monitor.json has another format; starting it again")
    except (OSError, ValueError):
        pass
    return {"snaps": [], "runs": []}


def save_monitor(m):
    snaps = sorted({s[0]: s for s in m.get("snaps") or []}.values(), key=lambda s: s[0])
    runs = sorted(m.get("runs") or [], key=lambda r: r[1] or 0)
    out = {"_note": "Written by monitor.py (workflow 4.5); charted by monitor.html. "
                    "Ages are seconds before t; coverage counts catalog games.",
           "generated_at": int(time.time()), "files": FILES, "row_ages": ROW_AGES,
           "coverage": COVERAGE, "_snap": SNAP_FORMAT, "_run": RUN_FORMAT,
           "snaps": snaps[-MAX_SNAPS:], "runs": runs}
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
    return out


def run_row(r):
    """A catalog.json `runs` entry as a RUN_FORMAT row."""
    return [r.get(k) for k in RUN_FORMAT]


def merge_runs(existing, catalog_runs, message_runs=()):
    """The runs list: the scraper's own lines (they carry the review counts) win; a line read
    from a commit message is kept only from before the first of them, so a run never shows twice."""
    own = {r[0]: r for r in existing if r[0] is not None}
    for r in catalog_runs:
        own[r[0]] = r
    first_own = min((r[0] for r in own.values()), default=None)
    from_msgs = {r[1]: r for r in list(existing) + list(message_runs) if r[0] is None}
    if first_own is not None:
        from_msgs = {k: r for k, r in from_msgs.items() if k < first_own}
    return list(from_msgs.values()) + list(own.values())


# --------------------------------------------------------------------------- #
# Live snapshot
# --------------------------------------------------------------------------- #
def read_local(name):
    try:
        return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def live():
    t = int(time.time())
    src = os.environ.get("MONITOR_SRC", "manual")
    row = snapshot(t, src, read_local)
    if row is None:
        log("games.json missing or unreadable; no snapshot")
        return 1
    m = load_monitor()
    m["snaps"].append(row)
    catalog = read_local("catalog") or {}
    m["runs"] = merge_runs(m["runs"], [run_row(r) for r in catalog.get("runs") or []])
    out = save_monitor(m)
    log(f"snapshot {src} at {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC: "
        f"{row[2]} games, {len(out['snaps'])} snapshots, {len(out['runs'])} runs")
    git_commit(f"monitor: {src} snapshot ({len(out['snaps'])} in history)")
    return 0


# --------------------------------------------------------------------------- #
# Backfill from git history
# --------------------------------------------------------------------------- #
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def history():
    """[(sha, commit_time, subject)] oldest first, for the branch checked out."""
    rows = []
    for line in git("log", "--first-parent", "--format=%H%x09%ct%x09%s").splitlines():
        sha, ct, subject = line.split("\t", 2)
        rows.append((sha, int(ct), subject))
    return rows[::-1]


def runs_from_messages(commits):
    """RUN_FORMAT rows from scrape commit messages: start unknown, review counts not recorded."""
    out = []
    for _sha, ct, subject in commits:
        m = SCRAPE_MSG.match(subject)
        if m:
            stored, new, ref, pend = (int(g) if g else None for g in m.groups())
            out.append([None, ct, stored, new, ref, pend, None, None, None, True])
    return out


def write_times():
    """{name: sorted commit times that touched that file} — the backfill's file ages for the
    files it doesn't stream."""
    out = {}
    for name in FILES:
        out[name] = sorted(int(x) for x in git("log", "--format=%ct", "--", f"{name}.json").split())
    return out


def last_before(times, t):
    """The latest of the sorted `times` at or before t, or None."""
    i = bisect_right(times, t)
    return times[i - 1] if i else None


def daily_points(commits, done_days, hour=BACKFILL_HOUR):
    """[(t, sha)] one per UTC day: the last commit at or before `hour`:00 that day, skipping
    days that already have a snapshot and today (the live job covers it)."""
    if not commits:
        return []
    first = datetime.fromtimestamp(commits[0][1], timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    out, i, last = [], 0, None
    day = first
    while day < today:
        cut = int(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp())
        while i < len(commits) and commits[i][1] <= cut:
            last = commits[i]
            i += 1
        if last and day.isoformat() not in done_days:
            out.append((cut, last[0]))
        day += timedelta(days=1)
    return out


def fetch_at(sha, name, tries=4):
    """A file as it was at commit `sha`, streamed and parsed; None if it didn't exist yet."""
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{name}.json"
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            err = e
        time.sleep(3 * attempt + random.uniform(0, 2))
    log(f"  {name}.json at {sha[:7]}: gave up ({err})")
    return None


def backfill():
    commits = history()
    if len(commits) < 50:
        log(f"only {len(commits)} commits visible; the backfill needs a full-history checkout")
        return 1
    m = load_monitor()
    m["runs"] = merge_runs(m["runs"], [], runs_from_messages(commits))
    done = {datetime.fromtimestamp(s[0], timezone.utc).date().isoformat() for s in m["snaps"]}
    points = daily_points(commits, done)
    times = write_times()
    log(f"{len(commits)} commits, {len(m['runs'])} runs; {len(points)} days to replay "
        f"({len(done)} already in monitor.json)")
    started = time.time()
    for n, (t, sha) in enumerate(points, 1):
        def read(name, sha=sha):
            return fetch_at(sha, name) if name in BACKFILL_FILES or name == "catalog" else None
        file_times = {name: last_before(times[name], t) for name in FILES}
        row = snapshot(t, "backfill", read, file_times)
        if row:
            m["snaps"].append(row)
        log(f"  {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d} {sha[:7]}: "
            f"{row[2] if row else 'no games.json'} games  [{n}/{len(points)}, "
            f"{time.time() - started:.0f}s]")
        if n % 10 == 0:
            save_monitor(m)                  # a timeout keeps what was replayed
    out = save_monitor(m)
    git_commit(f"monitor: backfill {len(points)} days from git history "
               f"({len(out['snaps'])} snapshots, {len(out['runs'])} runs)")
    return 0


# --------------------------------------------------------------------------- #
# Commit (same pattern as coverage.py: fetch, rebase over the other writers, push)
# --------------------------------------------------------------------------- #
def git_commit(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "monitor.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            log("  monitor.json unchanged; nothing to commit.")
            return
        subprocess.run(["git", "commit", "-m", msg], check=False)
        for attempt in range(1, 6):
            subprocess.run(["git", "fetch", "origin", "main"], check=False)
            subprocess.run(["git", "rebase", "--autostash", "FETCH_HEAD"], check=False)
            if subprocess.run(["git", "push", "origin", "HEAD:main"],
                              capture_output=True, text=True).returncode == 0:
                log(f"  committed: {msg}")
                return
            time.sleep(2 * attempt + random.uniform(0, 2))
        log("  ERROR: could not push monitor.json after 5 attempts")
    except Exception as e:
        log(f"  git commit failed: {e}")


if __name__ == "__main__":
    sys.exit(backfill() if "--backfill" in sys.argv[1:] else live())
