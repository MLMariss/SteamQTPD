#!/usr/bin/env python3
"""Regression tests for the playtime age-ladder + multi-shard scheduler and the
HLTB popularity fast lane. Pure-logic only: no network, no git, no disk."""
import sys, time, types

# Stub `requests` so playtime_refresh imports without the dependency present.
if "requests" not in sys.modules:
    m = types.ModuleType("requests")
    class _S:
        def __init__(self): self.headers = {}; self.cookies = {}
        def update(self, *a, **k): pass
        def get(self, *a, **k): raise RuntimeError("no network in tests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(),
        cookies=type("C", (), {"update": lambda s, d: None})(),
        get=lambda *a, **k: None)
    m.RequestException = Exception
    m.Response = type("Response", (), {})   # howlongtobeatpy type-hints against this
    sys.modules["requests"] = m

import playtime_refresh as P

NOW = int(time.time())
DAY = 86400
fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

print("\n== playtime: cooldown ladder ==")
check("released 2d ago -> 1d cooldown",
      P.cooldown_days(NOW - 2*DAY, None, 50, NOW) == 1)
check("released 20d ago -> 3d cooldown",
      P.cooldown_days(NOW - 20*DAY, None, 50, NOW) == 3)
check("released 60d ago -> 7d cooldown",
      P.cooldown_days(NOW - 60*DAY, None, 50, NOW) == 7)
check("released 400d ago -> 30d cooldown",
      P.cooldown_days(NOW - 400*DAY, None, 50, NOW) == 30)
check(">1k reviews halves the tier (20d old: 3d -> 1.5d)",
      P.cooldown_days(NOW - 20*DAY, None, 5000, NOW) == 1.5)
check(">1k reviews on old game: popularity floor pulls 15d -> 5d (HLTB-aligned)",
      P.cooldown_days(NOW - 400*DAY, None, 5000, NOW) == 5)
check("exactly 1000 reviews: no halving (strict >), but >500 floor applies -> 10d",
      P.cooldown_days(NOW - 400*DAY, None, 1000, NOW) == 10)
check("floor: hot new release never below 12h",
      P.cooldown_days(NOW - 1*DAY, None, 999999, NOW) >= 0.5)

print("\n== playtime: popularity floor (HLTB alignment) ==")
check(">1000 reviews -> 5d floor", P.popular_floor_days(5000) == 5)
check(">500 reviews -> 10d floor", P.popular_floor_days(700) == 10)
check("500 exactly -> no floor (strict >, matches HLTB edge)",
      P.popular_floor_days(500) is None)
check("low reviews -> no floor", P.popular_floor_days(50) is None)
check("mid-popular old game: 30d -> 10d floor",
      P.cooldown_days(NOW - 400*DAY, None, 700, NOW) == 10)
check("floor only pulls FORWARD: fresh popular release keeps its faster 12h, not 5d",
      P.cooldown_days(NOW - 1*DAY, None, 5000, NOW) == 0.5)
check("floor rescues a popular game with NO release date from the 30d dormant tier",
      P.cooldown_days(None, None, 5000, NOW) == 5)
check("unpopular game is untouched by the floor (old 30d stays 30d)",
      P.cooldown_days(NOW - 400*DAY, None, 50, NOW) == 30)
check("no release date + recent patch -> legacy 7d",
      P.cooldown_days(None, NOW - 5*DAY, 50, NOW) == P.COOLDOWN_DAYS)
check("no release date + no patch -> legacy 30d",
      P.cooldown_days(None, None, 50, NOW) == P.NOUPDATE_COOLDOWN_DAYS)
check("future-dated release falls back to legacy, not tier-0",
      P.cooldown_days(NOW + 30*DAY, None, 50, NOW) == P.NOUPDATE_COOLDOWN_DAYS)

print("\n== playtime: eligibility ==")
fresh = {"reviews": {str(i): {} for i in range(500)}, "exhausted": True,
         "scraped_at": NOW - 12*3600}
stale = dict(fresh, scraped_at=NOW - 10*DAY)
check("below MIN_REVIEWS_FLOOR is never eligible",
      not P.is_eligible({}, NOW - DAY, None, 5, NOW, 200))
check("never-scraped game is eligible",
      P.is_eligible({}, NOW - DAY, None, 50, NOW, 200))
check("new release scraped 12h ago is NOT yet due (1d cooldown)",
      not P.is_eligible(fresh, NOW - 2*DAY, None, 50, NOW, 200))
check("new release scraped 10d ago IS due",
      P.is_eligible(stale, NOW - 2*DAY, None, 50, NOW, 200))
check("old dormant game scraped 10d ago is NOT due (30d cooldown)",
      not P.is_eligible(stale, NOW - 400*DAY, None, 50, NOW, 200))
check("old but POPULAR game scraped 20d ago IS due (30d->5d popularity floor)",
      P.is_eligible(dict(fresh, scraped_at=NOW - 20*DAY),
                    NOW - 400*DAY, None, 5000, NOW, 200))
check("incomplete corpus is eligible regardless of cooldown",
      P.is_eligible({"reviews": {"1": {}}, "exhausted": False, "scraped_at": NOW},
                    NOW - 400*DAY, None, 50, NOW, 200))

print("\n== playtime: priority ordering ==")
p_new = P.priority(stale, NOW - 2*DAY, None, 50, NOW, 200)
p_old = P.priority(stale, NOW - 400*DAY, None, 50, NOW, 200)
check("new release outranks old at equal staleness", p_new > p_old)
p_pop = P.priority(stale, NOW - 400*DAY, None, 5000, NOW, 200)
check("popular outranks unpopular at equal age", p_pop > p_old)
check("never-scraped outranks everything",
      P.priority({}, NOW - 400*DAY, None, 50, NOW, 200) > p_new)
check("sub-10-review game is penalised",
      P.priority(stale, NOW - 2*DAY, None, 5, NOW, 200) < p_new)

print("\n== playtime: multi-shard scheduler ==")
# Build a synthetic catalog whose hot games are deliberately scattered across shards.
games = []
for i in range(2000):
    aid = 1000 + i*10                      # realistic: appids are multiples of 10
    games.append({"appid": aid, "review_count": 5000 if i % 7 == 0 else 50,
                  "released_ts": NOW - (2*DAY if i % 5 == 0 else 500*DAY),
                  "last_update_ts": NOW - 10*DAY})
sched = P.build_candidates(games, NOW, 200)
check("due games are spread across many shards", len(sched) > 20)
chosen = P.select_buckets(sched, anchor=3)
check("selects multiple shards per run", len(chosen) > 1)
check("respects MAX_SHARDS_PER_RUN", len(chosen) <= P.MAX_SHARDS_PER_RUN)
check("anchor bucket always included (starvation guard)", 3 in chosen)
check("chosen shards are the hottest",
      len(sched[chosen[0]]) >= len(sched[chosen[-1]]))
check("no duplicate shards selected", len(chosen) == len(set(chosen)))
# Anchor injection must not exceed the cap even when anchor isn't hot.
cold = P.select_buckets(sched, anchor=999 % P.NSHARDS)
check("anchor injection still respects the cap", len(cold) <= P.MAX_SHARDS_PER_RUN)
check("every selected bucket exists in scoring or is the anchor",
      all(b in sched or b == (999 % P.NSHARDS) for b in cold))
# Sub-floor games must never be scheduled.
tiny = [{"appid": 500, "review_count": 2, "released_ts": NOW - DAY}]
check("sub-floor games are excluded from scheduling",
      P.build_candidates(tiny, NOW, 200) == {})

print("\n== playtime: shard key sanity ==")
counts = {}
for i in range(64000):
    counts[P.shard_of(1000 + i*10)] = counts.get(P.shard_of(1000 + i*10), 0) + 1
mx, mean = max(counts.values()), sum(counts.values())/len(counts)
check("all 64 buckets used", len(counts) == 64)
check(f"even distribution (max/mean={mx/mean:.2f})", mx/mean < 1.1)

# ---------------------------------------------------------------- HLTB ---- #
import hltb_refresh as H

print("\n== hltb: popularity window ==")
check(">1000 reviews -> 5d", H.popular_window_days(5000) == 5)
check(">500 reviews -> 10d", H.popular_window_days(700) == 10)
check("500 exactly -> no tier (strict >)", H.popular_window_days(500) is None)
check("low reviews -> no tier", H.popular_window_days(50) is None)

print("\n== hltb <-> playtime: same hot games, same cadence ==")
check("playtime >1k floor matches HLTB >1k window (5d)",
      P.popular_floor_days(5000) == H.popular_window_days(5000) == 5)
check("playtime >500 floor matches HLTB >500 window (10d)",
      P.popular_floor_days(700) == H.popular_window_days(700) == 10)
check("both share the strict-> edge at 1000 and 500",
      P.popular_floor_days(1000) == H.popular_window_days(1000) and
      P.popular_floor_days(500) == H.popular_window_days(500))

print("\n== hltb: effective window (the Black Flag fix) ==")
check("unpopular full entry keeps 365d",
      H.effective_window_days({}, 50, "full") == 365)
check("POPULAR full entry drops 365d -> 5d",
      H.effective_window_days({}, 22000, "full") == 5)
check("popular partial drops 14d -> 5d",
      H.effective_window_days({}, 22000, "partial") == 5)
check("mid-popular partial drops 14d -> 10d",
      H.effective_window_days({}, 700, "partial") == 10)
check("unpopular partial keeps 14d",
      H.effective_window_days({}, 50, "partial") == 14)
check("blank curve preserved for unpopular (attempts=0 -> 3d)",
      H.effective_window_days({"attempts": 0}, 50, "blank") == 3)
check("frozen blank stays frozen when unpopular (attempts=9 -> 180d)",
      H.effective_window_days({"attempts": 9}, 50, "blank") == 180)
check("frozen blank but popular -> pulled forward to 5d",
      H.effective_window_days({"attempts": 9}, 22000, "blank") == 5)
check("window is never LONGER than the bucket's own (min semantics)",
      all(H.effective_window_days({}, rc, "partial") <= 14
          for rc in (0, 50, 500, 501, 1000, 1001, 99999)))
check("comp_grew pulls a cold full entry forward to 5d",
      H.effective_window_days({"comp_grew": True}, 50, "full") == 5)
check("comp_grew never lengthens an already-shorter window",
      H.effective_window_days({"comp_grew": True, "attempts": 0}, 50, "blank") == 3)

print("\n== hltb: count_comp extraction ==")
check("reads count_comp", H._comp_count({"count_comp": 120}) == 120)
check("missing key -> None", H._comp_count({"game_name": "x"}) is None)
check("non-dict -> None", H._comp_count(None) is None)
check("bool is rejected", H._comp_count({"count_comp": True}) is None)
check("string is rejected", H._comp_count({"count_comp": "120"}) is None)

print("\n== hltb: rescrape queue ordering ==")
games_h = [(10, "Black Flag Resynced", 22000), (20, "Tiny Indie", 40),
           (30, "Mid Game", 700)]
hltb = {
    10: {"raw": {"main": 22.5, "extra": 38.5, "complete": 66.5},
         "fetched_at": NOW - 30*DAY},          # popular + full + 30d old
    20: {"raw": {"main": 5.0, "extra": None, "complete": None},
         "fetched_at": NOW - 30*DAY},          # unpopular + partial + 30d old
    30: {"raw": {"main": 8.0, "extra": 12.0, "complete": 20.0},
         "fetched_at": NOW - 30*DAY},          # mid + full + 30d old
}
q = H.build_rescrape_queue(games_h, hltb, NOW)
ids = [aid for aid, _t, _b in q]
check("popular full entry is now queued (was frozen 365d)", 10 in ids)
check("mid-tier full entry queued at 30d > 10d window", 30 in ids)
check("unpopular partial still queued (30d > 14d)", 20 in ids)
check("partial bucket runs before full", ids.index(20) < ids.index(10))
check("within full bucket, more-reviewed first", ids.index(10) < ids.index(30))
# An unpopular full entry must remain frozen.
q2 = H.build_rescrape_queue([(40, "Old Game", 50)],
                            {40: {"raw": {"main": 1.0, "extra": 2.0, "complete": 3.0},
                                  "fetched_at": NOW - 30*DAY}}, NOW)
check("unpopular full entry stays frozen at 30d", q2 == [])

print("\n" + ("ALL TESTS PASSED" if not fails
              else f"{len(fails)} FAILURE(S): " + "; ".join(fails)))
sys.exit(1 if fails else 0)
