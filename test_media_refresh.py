#!/usr/bin/env python3
"""Tests for how the trailer (trailers.py) and screenshot (shots.py) jobs choose what to ask
and fold Steam's answers in. Pure logic: no network, no git; files only in a temp dir.

What they pin: a failed call is not "no media" (it used to park 50 games for 30 days); the
upcoming catalogue is swept too; a known hit is re-checked on a spread-out 60-day clock; a
re-checked hit Steam answers without media is dropped, unless the whole batch looks like an
answer with no media data at all."""
import json, pathlib, sys, tempfile, types

if "requests" not in sys.modules:           # both import requests; nothing here calls it
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(), get=None, head=None)
    m.RequestException = Exception
    sys.modules["requests"] = m

import shots as S
import trailers as T

fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


NOW = 1_800_000_000
D = 86400
trailer = lambda aid: {"appid": aid, "trailers": {"highlights": [{"microtrailer": [
    {"filename": f"{aid}/x/micro.webm"}, {"filename": f"{aid}/x/micro.mp4"}]}]}}
no_trailer = lambda aid: {"appid": aid, "trailers": {"highlights": []}}
stills = lambda aid, n=2: {"appid": aid, "screenshots": {"all_ages_screenshots": [
    {"filename": f"ss_{aid}_{i}.jpg", "ordinal": i} for i in range(n)]}}
no_stills = lambda aid: {"appid": aid, "screenshots": {"all_ages_screenshots": []}}

for M, name in ((T, "trailers"), (S, "shots")):
    print(f"\n== {name}: a failed call is not 'no media' ==")
    calls = []
    M.get = lambda url, params=None, timeout=40: calls.append(1) or None
    check("getitems -> None when Steam doesn't answer", M.getitems([10, 20]) is None)
    M.get = lambda url, params=None, timeout=40: {"error": "x"}
    check("getitems -> None for an answer without a response", M.getitems([10]) is None)
    M.get = lambda url, params=None, timeout=40: {"response": {}}
    check("getitems -> [] for an answer with no items", M.getitems([10]) == [])

    print(f"\n== {name}: the catalogue includes upcoming games ==")
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "games.json").write_text(json.dumps({"games": [
        {"appid": 10, "review_count": 5}, {"appid": 20, "review_count": 900}, {"appid": 30}]}))
    (d / "upcoming.json").write_text(json.dumps({"games": [{"appid": 40}, {"appid": 30}, {"appid": 5}]}))
    M.GAMES_FILE, M.UPCOMING_FILE = d / "games.json", d / "upcoming.json"
    cat_of = lambda: M.load_catalog()[0] if M is T else M.load_catalog()
    check("released most-reviewed first, then upcoming by appid, no duplicates",
          [a for a, _ in cat_of()] == [20, 10, 30, 5, 40])
    if M is T:
        check("upcoming-only games are the ones games.json lacks", M.load_catalog()[1] == {5, 40})
    (d / "upcoming.json").unlink()
    check("no upcoming.json: released games only", [a for a, _ in cat_of()] == [20, 10, 30])
    (d / "games.json").write_text(json.dumps({"games": []}))
    check("empty games.json: nothing to do", cat_of() == [])

    print(f"\n== {name}: what gets asked, in what order ==")
    hits = {"20": ["a"], "30": ["b"]}
    misses = {"10": NOW - 40 * D, "11": NOW - 5 * D}
    checked = {"20": NOW - 61 * D, "30": NOW - 10 * D}
    catalog = [(20, 900), (10, 5), (11, 4), (30, 0), (40, 0)]
    q, nf, ns, nv = M.select_work(catalog, hits, misses, checked, NOW)
    check("new first, then old misses, then hits past the window",
          q == [40, 10, 20] and (nf, ns, nv) == (1, 1, 1))
    spread = [NOW - M.spread_stamp(a, NOW) for a in range(10, 600010, 10)]
    check("first stamps fall inside the window", all(0 <= s < M.VERIFY_DAYS * D for s in spread))
    per_day = [0] * M.VERIFY_DAYS
    for s in spread:
        per_day[s // D] += 1
    check(f"and spread evenly (busiest day {max(per_day)}, quietest {min(per_day)} of {len(spread)})",
          max(per_day) < 1.25 * len(spread) / M.VERIFY_DAYS)

print("\n== trailers: folding an answer in ==")
hits, misses, checked, adaptive = {"20": ["old.webm"], "30": ["keep.webm"]}, {}, {}, set()
f, c, r, _ = T.apply_batch([10, 20, 30, 50], [trailer(10), trailer(20), no_trailer(30)],
                           hits, misses, checked, adaptive, NOW)
check("new hit stored", hits.get("10") == ["10/x/micro.webm", "10/x/micro.mp4"] and f == 1)
check("changed hit updated", hits["20"][0] == "20/x/micro.webm" and c == 1)
check("re-checked hit Steam now answers without a trailer is dropped",
      "30" not in hits and r == 1 and misses.get("30") == NOW)
check("an appid the answer left out is a miss", misses.get("50") == NOW)
check("confirmed hits are stamped", checked.get("10") == NOW and checked.get("20") == NOW)
hits, misses, checked = {"20": ["a.webm"], "30": ["b.webm"]}, {}, {"20": 1, "30": 1}
f, c, r, _ = T.apply_batch([20, 30], [no_trailer(20), no_trailer(30)], hits, misses, checked, set(), NOW)
check("a batch where every known hit comes back empty removes nothing",
      hits == {"20": ["a.webm"], "30": ["b.webm"]} and r == 0 and checked == {"20": 1, "30": 1})
hits, checked = {"20": ["a.webm"]}, {"20": 1}
T.apply_batch([20], [], hits, {}, checked, set(), NOW)
check("a hit the answer left out keeps its trailer and waits a full window",
      hits == {"20": ["a.webm"]} and checked["20"] == NOW)

print("\n== trailers: upcoming-only games go to their own file ==")
d = pathlib.Path(tempfile.mkdtemp())
T.TRAILERS_FILE, T.TRAILERS_UP_FILE = d / "trailers.json", d / "trailers_upcoming.json"
T.save_trailers({"10": ["a.webm"], "40": ["b.webm"], "99": ["c.webm"]}, None, 7, {40})
main_t = json.loads(T.TRAILERS_FILE.read_text())
up_t = json.loads(T.TRAILERS_UP_FILE.read_text())
check("boot file: everything but the upcoming-only games",
      set(main_t["trailers"]) == {"10", "99"} and main_t["adaptive_available"] == 7)
check("upcoming file: the upcoming-only games, same base",
      set(up_t["trailers"]) == {"40"} and up_t["base"] == main_t["base"])
T.save_trailers({"10": ["a.webm"], "40": ["b.webm"], "99": ["c.webm"]}, None, 7, set())
check("a game that released moves to the boot file",
      "40" in json.loads(T.TRAILERS_FILE.read_text())["trailers"]
      and not json.loads(T.TRAILERS_UP_FILE.read_text())["trailers"])

print("\n== shots: folding an answer in, and which shards change ==")
hits, misses, checked, dirty = {"20": ["ss_20_0.jpg", "ss_20_1.jpg"], "30": ["x.jpg"]}, {}, {}, set()
f, c, r, m = S.apply_batch([20, 30, 70], [stills(20), no_stills(30), stills(70)],
                           hits, misses, checked, dirty, NOW)
check("unchanged hit: stamped, shard not rewritten",
      checked.get("20") == NOW and S.shard_of(20) not in dirty and c == 0)
check("emptied hit dropped and its shard rewritten",
      "30" not in hits and r == 1 and S.shard_of(30) in dirty)
check("new hit stored and its shard rewritten", "70" in hits and f == 1 and S.shard_of(70) in dirty)
hits, dirty = {"20": ["a.jpg"], "30": ["b.jpg"]}, set()
S.apply_batch([20, 30], [no_stills(20), no_stills(30)], hits, {}, {}, dirty, NOW)
check("a batch where every known hit comes back empty removes nothing",
      hits == {"20": ["a.jpg"], "30": ["b.jpg"]} and not dirty)

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
