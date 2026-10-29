#!/usr/bin/env python3
"""Regression tests for stats.py (Steam by the numbers): the classifiers, the sale-episode
recorder behind "days to first sale", the daily sampling of the git backfill, and the monthly
and weekly aggregates — above all that nothing which ranks or names games lets an adult game through.
Pure logic only: inputs are built in memory, no network, no git."""
from datetime import datetime, timezone

import stats as St

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

D = 86400
ts = lambda y, m, d: int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())
day = lambda y, m, d: ts(y, m, d) // D

print("\n== classifiers ==")
check("95% of 500+ reviews: Overwhelmingly Positive", St.steam_label(95, 500) == 0)
check("94% of 500+: Very Positive", St.steam_label(94, 5000) == 1)
check("96% of 49: Positive (too few for Very)", St.steam_label(96, 49) == 2)
check("75%: Mostly Positive", St.steam_label(75, 100) == 3)
check("40%: Mixed", St.steam_label(40, 100) == 4)
check("19% of 499: Very Negative", St.steam_label(19, 499) == 7)
check("under 10 reviews: no label", St.steam_label(100, 9) is None)
check("free is bin 0", St.price_bin(0) == 0)
check("$4.99 and $5.00 share a bin", St.price_bin(4.99) == St.price_bin(5.0) == St.PRICE_UB.index(5) + 1)
check("$19.99 is the $20 bin", St.price_bin(19.99) == St.PRICE_UB.index(20) + 1)
check("over $100 is the last bin", St.price_bin(149.99) == len(St.PRICE_UB) + 1)
check("PICS-covered, flag unset, adult tag: not adult (the flag is the only lock)",
      St.is_adult({"cats": [2], "adult": False}, ["Nudity"]) is False)
check("PICS-covered, flag set: adult", St.is_adult({"cats": [2], "adult": True}, []) is True)
check("not covered by PICS: the tag heuristic decides", St.is_adult(None, ["Hentai"]) is True
      and St.is_adult({"name": "x"}, ["Action"]) is False)

print("\n== sale episodes ==")
R = day(2026, 7, 1)
s = St.empty_state()
St.observe(s, "1", R + 5, 10, R)
check("first checked 5 days after release: never watched", "1" not in s["games"])
St.observe(s, "2", R + 1, 20, R)
St.observe(s, "2", R + 4, 20, R)
St.observe(s, "2", R + 9, 0, R)
check("launch discount is one episode", s["games"]["2"] == [R + 1, R + 9, R + 1, R + 4, 20])
St.observe(s, "2", R + 8, 50, R)
check("an older check is ignored", s["games"]["2"][1] == R + 9 and len(s["games"]["2"]) == 5)
St.observe(s, "2", R + 40, 30, R)
St.observe(s, "2", R + 41, 40, R)
check("first sale after launch week: a new episode, deepest % kept",
      s["games"]["2"][-3:] == [R + 40, R + 41, 40] and St.first_after_launch(s["games"]["2"], R) == (R + 40, 40))
St.observe(s, "2", R + 47, 0, R)
St.observe(s, "2", R + 70, 60, R)
check("watching stops once that first sale has ended", len(s["games"]["2"]) == 8 and s["games"]["2"][1] == R + 47)
St.observe(s, "3", R, 0, R)
St.observe(s, "3", R + 20, 0, R)
check("never discounted: just first and last check", s["games"]["3"] == [R, R + 20])

s = St.empty_state()
prices = {"7": {"price_initial": 10, "discount_pct": 25, "scraped_at": ts(2026, 7, 2)},
          "8": {"price_initial": 5, "discount_pct": 0, "scraped_at": ts(2026, 7, 2)},
          "9": {"price_initial": 5, "discount_pct": 50, "scraped_at": ts(2026, 7, 2)},
          "10": {"price_initial": 0, "discount_pct": 0, "scraped_at": ts(2026, 7, 2)},
          "11": {"price_initial": 60, "discount_pct": 40, "scraped_at": ts(2026, 7, 2)}}
reviews = {7: 900, 8: 99, 9: 100, 11: 5000}
St.record(s, prices, {7: R, 8: R, 9: R}, {9}, reviews, day(2026, 7, 2))
row = s["days"][str(day(2026, 7, 2))]
check("day row: priced, on sale, sum of % for the catalog", row[:3] == [4, 3, 115])
check("...then per review group: 0-99, 100-4,999, 5,000+ (edges inclusive above)",
      row[3:] == [1, 0, 0, 2, 2, 75, 1, 1, 40] and len(row) == St.DAY_ROW)
check("excluded (left Early Access) and unknown games are not watched", set(s["games"]) == {"7", "8"})
check("last_day advances", s["last_day"] == day(2026, 7, 2))
games_before = {k: list(v) for k, v in s["games"].items()}
check("a fresh state's rows are grouped by the current edges: nothing stale", St.stale_days(s) == [])
s["days"][str(day(2026, 6, 30))] = [9, 1, 10]
s["groups"] = [50, 1000]
check("rows grouped by other edges (or none) are all stale, oldest first",
      St.stale_days(s) == [day(2026, 6, 30), day(2026, 7, 2)])
St.record(s, {"7": {"price_initial": 10, "discount_pct": 90, "scraped_at": ts(2026, 6, 30)}},
          {7: R}, set(), reviews, day(2026, 6, 30), games=False)
check("a day-row-only replay rebuilds the row and leaves games and last_day alone",
      s["days"][str(day(2026, 6, 30))] == [1, 1, 90, 0, 0, 0, 1, 1, 90, 0, 0, 0]
      and s["games"] == games_before and s["last_day"] == day(2026, 7, 2))
check("nothing priced: no row", St.day_row({"10": {"price_initial": 0}}, {}) is None)

print("\n== backfill sampling ==")
commits = [("a", ts(2026, 7, 1) + 3600), ("b", ts(2026, 7, 1) + 80000), ("c", ts(2026, 7, 3) + 10)]
check("one point a day: that day's last commit, or the one it inherited",
      St.daily_points(commits, day(2026, 6, 30), day(2026, 7, 3))
      == [(day(2026, 7, 1), "b"), (day(2026, 7, 2), "b"), (day(2026, 7, 3), "c")])

print("\n== build: monthly aggregates ==")
NOW = ts(2026, 9, 15)
games = [
    {"appid": 1, "title": "Clean Hit", "release_ts": ts(2026, 7, 3), "review_count": 900, "rating_pct": 96,
     "price_initial": 19.99, "is_free": False, "tags": ["Action"]},
    {"appid": 2, "title": "Flagged", "release_ts": ts(2026, 7, 9), "review_count": 5000, "rating_pct": 90,
     "price_initial": 9.99, "is_free": False, "tags": ["Action"]},
    {"appid": 3, "title": "Free One", "release_ts": ts(2026, 8, 1), "review_count": 3, "rating_pct": 100,
     "is_free": True, "tags": []},
    {"appid": 4, "title": "Graduate", "release_ts": ts(2026, 8, 20), "review_count": 40, "rating_pct": 50,
     "price_initial": 4.99, "is_free": False, "tags": []},
    {"appid": 5, "title": "Old", "release_ts": ts(2015, 3, 2), "review_count": 0, "rating_pct": None,
     "price_initial": 0.99, "is_free": False, "tags": []},
]
pics = {"1": {"cats": [2, 53], "ai": 1, "genres": [1, 23, 72], "deck": {"cat": 3}, "controller": "full"},
        "2": {"cats": [2], "adult": True, "genres": [1], "ai": 2},
        "4": {"cats": [2], "genres": [70], "ea": True}}
tags = {"1": ["Action", "Indie"], "2": ["Action", "Sexual Content"]}
ea = {"4": {"s": ts(2025, 8, 20), "v": ts(2026, 8, 20)}}
state = St.empty_state()
state["games"] = {"1": [day(2026, 7, 3), day(2026, 9, 14), day(2026, 7, 3), day(2026, 7, 9), 10,
                        day(2026, 8, 12), day(2026, 8, 14), 30],
                  "4": [day(2026, 8, 20), day(2026, 9, 14)]}
state["days"] = {str(day(2026, 9, 14)): [100, 10, 300]}
doc = St.build(NOW, games, pics, tags, {"1": {"price_initial": 24.99}}, ea, {"1": [6.5, 30]}, {"1": 11}, state)
mi = lambda y, m: (y * 12 + m - 1) - (2015 * 12 + 2)
A = doc["m"]
check("months run from the first release to now", doc["m0"] == "2015-03" and doc["months"] == mi(2026, 9) + 1)
check("every game counts toward releases, adult ones too", A["rel"][mi(2026, 7)] == 2 and A["rel_c"][mi(2026, 7)] == 1)
tag_july = {n: c[mi(2026, 7)] for n, c in zip(doc["tags"]["names"], doc["tags"]["m"])}
check("tag counts skip the adult-flagged game", tag_july.get("Action") == 1 and "Sexual Content" not in tag_july)
check("genres skip content descriptors (72) but keep real ones",
      "Nudity" not in doc["genres"]["names"] and "Indie" in doc["genres"]["names"])
top = [r for rows in doc["top"].values() for r in rows]
check("biggest releases never name the adult-flagged game", [r[0] for r in top if r[0] == 2] == [])
check("a game that left Early Access is marked 1.0 in the list", [r[4] for r in top if r[0] == 4] == [1])
check("prices.json's list price wins over games.json's",
      doc["priceb"][St.price_bin(24.99)][mi(2026, 7)] == 1 and A["price_sum"][mi(2026, 7)] == 2499 + 999)
check("free games land in bin 0", doc["priceb"][0][mi(2026, 8)] == 1 and A["free"][mi(2026, 8)] == 1)
check("reviews per price bin: each game's reviews in its price bin, free games too",
      doc["priceb_rev"][St.price_bin(24.99)][mi(2026, 7)] == 900 and doc["priceb_rev"][St.price_bin(9.99)][mi(2026, 7)] == 5000
      and doc["priceb_rev"][0][mi(2026, 8)] == 3 and doc["sale_group_edges"] == St.SALE_GROUP_EDGES)
check("AI, Deck, controller, VR counted over PICS-covered games",
      A["pics"][mi(2026, 7)] == 2 and A["ai"][mi(2026, 7)] == 2 and A["ai_live"][mi(2026, 7)] == 1
      and A["deck3"][mi(2026, 7)] == 1 and A["ctrl_full"][mi(2026, 7)] == 1 and A["vr"][mi(2026, 7)] == 1)
check("demo and Length counted", A["demo"][mi(2026, 7)] == 1 and A["len_n"][mi(2026, 7)] == 1)
check("left EA: an EA launch in its start month, a graduation (with its length) in its 1.0 month",
      A["ea_in"][mi(2025, 8)] == 1 and doc["ea_out"][str(mi(2026, 8))] == [365] and A["ea_now"][mi(2026, 8)] == 0)
sj = doc["sale"][str(mi(2026, 7))]
check("sale timing: launch discount and first sale 40 days in",
      sj["n"] == 1 and sj["l"] == 1 and sj["lp"] == 10 and sj["e"] == {"40": 1} and sj["ep"] == 30)
check("a game that left Early Access is not counted in sale timing", str(mi(2026, 8)) not in doc["sale"])
check("reception bins and labels", doc["lab"][0][mi(2026, 7)] == 1 and A["rated"][mi(2026, 7)] == 2)
check("daily releases land on their day", doc["daily"][day(2026, 7, 3) - doc["d0"]] == 1)
check("sale days are carried as rows", doc["sale_days"] == [[day(2026, 9, 14), 100, 10, 300]])
old_groups = dict(state, days={str(day(2026, 9, 14)): [100, 10, 300, 1, 2, 3, 4, 5, 6, 7, 8, 9]}, groups=[50, 1000])
check("groups built on other edges are not published: catalog numbers only until rebuilt",
      St.build(NOW, games, pics, tags, {}, ea, {}, {}, old_groups)["sale_days"] == [[day(2026, 9, 14), 100, 10, 300]])
cur_groups = dict(old_groups, groups=list(St.SALE_GROUP_EDGES))
check("...and in full once they match", St.build(NOW, games, pics, tags, {}, ea, {}, {}, cur_groups)["sale_days"][0][4:] == list(range(1, 10)))
check("discount depth: launch 10% in the 10-19% bin, first sale 30% in the 30-39% bin",
      sj["lh"][0] == 1 and sum(sj["lh"]) == 1 and sj["fh"][St.DISC_EDGES.index(30) + 1] == 1 and sum(sj["fh"]) == 1)
check("review-weighted price: each paid game weighted by its reviews",
      A["rw"][mi(2026, 7)] == 900 + 5000 and A["price_rw"][mi(2026, 7)] == 2499 * 900 + 999 * 5000
      and A["rw"][mi(2026, 8)] == 40)

print("\n== build: weekly aggregates ==")
check("weeks start on Monday: 1970-01-01 (a Thursday) is in week 0, Monday 1970-01-05 in week 1",
      St.week_of(0) == 0 and St.week_of(ts(1970, 1, 4)) == 0 and St.week_of(ts(1970, 1, 5)) == 1)
check("week w starts on day 7w - 3, a Monday",
      all(datetime.fromtimestamp((7 * St.week_of(ts(2026, 7, d)) - 3) * D, timezone.utc).weekday() == 0 for d in range(1, 15)))
busy0 = [{"appid": 1000, "release_ts": ts(2003, 5, 1)}] + \
        [{"appid": 1001 + i, "release_ts": ts(2009, 2, 1) + i * D} for i in range(St.WEEKS_FROM_MIN)]
wk = St.build(NOW, games, pics, tags, {"1": {"price_initial": 24.99}}, ea, {"1": [6.5, 30]}, {"1": 11}, state, unit="w")
wi = lambda y, m, d: St.week_of(ts(y, m, d)) - wk["w0"]
W = wk["m"]
check("weekly file runs from the first release's week (no year here is busy) to now",
      wk["w0"] == St.week_of(ts(2015, 3, 2)) and wk["weeks"] == St.week_of(NOW) - wk["w0"] + 1)
wk2 = St.build(NOW, games + busy0, {}, {}, {}, {}, {}, {}, St.empty_state(), unit="w")
check("...and from the first busy year's first week when an earlier year is quiet",
      wk2["w0"] == St.week_of(ts(2009, 1, 1)) and sum(wk2["m"]["rel"]) == St.WEEKS_FROM_MIN + 5)
check("each release lands in its own week", W["rel"][wi(2026, 7, 3)] == 1 and W["rel"][wi(2026, 7, 9)] == 1
      and sum(W["rel"]) == 5)
check("weekly rankings skip the adult-flagged game too",
      "Sexual Content" not in wk["tags"]["names"] and [r for rows in wk["top"].values() for r in rows if r[0] == 2] == [])
check("weekly sale timing keyed by release week", wk["sale"][str(wi(2026, 7, 3))]["e"] == {"40": 1})
check("weekly reviews per price bin", wk["priceb_rev"][St.price_bin(24.99)][wi(2026, 7, 3)] == 900)
check("the weekly file carries no catalog-wide extras", "daily" not in wk and "sale_days" not in wk and "now" not in wk)
busy = [{"appid": 100 + i, "release_ts": ts(2010, 1, 1) + i * D} for i in range(St.WEEKS_FROM_MIN)]
check("first busy year: the first with WEEKS_FROM_MIN releases",
      St.first_busy_year({g["appid"]: g["release_ts"] for g in games + busy}) == 2010)

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
raise SystemExit(1 if fails else 0)
