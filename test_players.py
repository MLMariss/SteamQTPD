#!/usr/bin/env python3
"""Regression tests for players.py: who is on the watchlist, how the hourly history is kept,
what counts as trending, and that no list names a game that fails adult=hide.
Pure logic only: inputs are built in memory, no network, no git."""
import players as P

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

H, D = 3600, 86400
NOW = 1_800_000_000
game = lambda a, days_old, reviews: {"appid": a, "title": f"G{a}", "release_ts": NOW - days_old * D,
                                     "review_count": reviews}

print("\n== watchlist ==")
games = {a: g for a, g in ((1, game(1, 10, 100)), (2, game(2, 10, 99)), (3, game(3, 91, 5000)),
                           (4, game(4, 400, 5000)), (5, game(5, -3, 500)))}
check("a 90-day release with 100+ reviews is watched; 99 reviews, 91 days old and unreleased are not",
      P.watchlist(games, {}, NOW) == [1])
check("a game watched until later is on the list whatever its age", P.watchlist(games, {4: NOW + 1}, NOW) == [1, 4])
check("a watch that has run out is off it", P.watchlist(games, {4: NOW}, NOW) == [1])
check("a watched appid outside the catalogue is not polled", P.watchlist(games, {999: NOW + 1}, NOW) == [1])
until = {4: NOW + 30 * D}
P.keep_watching(until, [4, 7], NOW, 7)
check("a sweep hit is watched 7 more days; a longer watch is not shortened",
      until == {4: NOW + 30 * D, 7: NOW + 7 * D})

print("\n== history ==")
st = {"times": [NOW - 8 * D, NOW - 2 * H, NOW - H], "series": {1: [5, 6, 7], 2: [1, 1, 1]}, "sticky": {}}
P.record(st, NOW, {1: 8, 3: None})
check("a snapshot older than the window is dropped, the new one appended",
      st["times"] == [NOW - 2 * H, NOW - H, NOW])
check("a watched game keeps its history in step with the times", st["series"][1] == [6, 7, 8])
check("a game new to the list starts with nulls; no answer is a null", st["series"][3] == [None, None, None])
check("a game off the watchlist is dropped", 2 not in st["series"])

print("\n== day files ==")
T0 = 1_800_000_000 - 1_800_000_000 % D                             # a UTC midnight
dt = [T0 - 2 * H, T0 - H, T0 + H, T0 + 2 * H]
ds = {1: [5, 6, 7, 8], 2: [None, None, 3, 4], 3: [9, None, None, None]}
days = P.split_days(dt, ds)
d0, d1 = sorted(days)
check("the history is cut at UTC midnight", [days[d]["times"] for d in (d0, d1)] == [dt[:2], dt[2:]])
check("a game with no reading in a day is left out of that day's file",
      sorted(days[d0]["series"]) == ["1", "3"] and sorted(days[d1]["series"]) == ["1", "2"])
check("joining the day files gives the history back, nulls where a game was missing",
      P.join_days([days[d0], days[d1]]) == (dt, ds))
check("a missing day file joins as nothing", P.join_days([{}, days[d1]])[0] == dt[2:])

print("\n== trend ==")
times = [NOW - i * H for i in range(72, -1, -1)]                  # hourly, three days
flat = [100] * len(times)
check("flat players: ratio 1", P.trend(times, flat, NOW) == (100, 100, 1.0))
spike = [100] * 49 + [100 + 50 * i for i in range(24)]            # climbs through the last day
peak, base, ratio = P.trend(times, spike, NOW)
check("the last 24 hours' peak against the earlier days' median peak", (peak, base, ratio) == (1250, 100, 12.5))
cycle = [100 + 900 * (i % 24 == 20) for i in range(len(times))]   # a daily evening peak
check("a daily cycle is not a trend: peak against peak", P.trend(times, cycle, NOW)[2] == 1.0)
check("no reading in the last 24 hours: nothing", P.trend(times, [100] * 40 + [None] * 33, NOW) is None)
late = [None] * 60 + [300] * 13
check("no earlier day with enough snapshots: a peak but no ratio", P.trend(times, late, NOW) == (300, None, None))
check("an earlier day of zeros does not divide by zero", P.trend(times, [0] * 49 + [400] * 24, NOW)[2] == 400)

print("\n== build ==")
games = {a: game(a, 10, 500) for a in (1, 2, 3, 4)}
state = {"times": times, "sticky": {},
         "series": {1: spike, 2: flat, 3: late, 4: [1] * 49 + [50] * 24, 9: spike}}
top = [{"rank": 1, "appid": 2, "players": 100, "peak_24h": 120},
       {"rank": 2, "appid": 9, "players": 90, "peak_24h": 95}]
doc = P.build(state, top, games, NOW)
check("the spiking game is trending, with its numbers",
      [(r["appid"], r["peak_24h"], r["base_peak"], r["ratio"], r["players"]) for r in doc["trending"]]
      == [(1, 1250, 100, 12.5, 1250)])
check("a flat game and a spike under MIN_PLAYERS are not", {2, 4}.isdisjoint(r["appid"] for r in doc["trending"]))
check("a game with players and no earlier day is newly tracked",
      [r["appid"] for r in doc["newly_tracked"]] == [3])
check("a game outside adult=hide is in no list, though its history spikes",
      all(r["appid"] != 9 for k in ("top", "trending", "newly_tracked") for r in doc[k]))
check("the chart keeps Valve's rank; an appid outside the catalogue is listed by id only",
      doc["top"] == [{"rank": 1, "appid": 2, "title": "G2", "players": 100, "peak_24h": 120}]
      and doc["top_uncatalogued"] == [9])
young = {"times": times[-20:], "sticky": {}, "series": {1: spike[-20:]}}
doc = P.build(young, [], games, NOW)
check("under two days of history: nothing is trending or newly tracked",
      doc["trending"] == [] and doc["newly_tracked"] == [] and doc["snapshots"] == 20)

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
raise SystemExit(1 if fails else 0)
