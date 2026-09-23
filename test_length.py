#!/usr/bin/env python3
"""Regression tests for length_model.py — the review-based Length that replaced HLTB on the
page. Pure logic only: synthetic inputs, no repo data files, no git."""
import math
import sys

import length_model as M

fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


IDS = {"1": "Action", "2": "Strategy", "3": "RPG", "4": "Casual", "9": "Racing",
       "18": "Sports", "23": "Indie", "25": "Adventure", "28": "Simulation", "37": "Free to Play"}

print("\n== genre_of: one genre per game, most specific first ==")
check("Racing beats Action", M.genre_of([1, 9], IDS) == "Racing")
check("Strategy beats RPG and Action", M.genre_of([1, 3, 2], IDS) == "Strategy")
check("list order does not matter", M.genre_of([25, 1], IDS) == M.genre_of([1, 25], IDS) == "Action")
check("Indie / F2P alone -> none", M.genre_of([23, 37], IDS) == M.NONE)
check("Indie is skipped, real genre wins", M.genre_of([23, 4], IDS) == "Casual")
check("no genres -> none", M.genre_of(None, IDS) == M.NONE)

COEFS = {"k": 20, "global": {"coef": 0.9, "typical_up_h": 4.0},
         "genres": {"Action": {"coef": 1.0, "typical_up_h": 10.0},
                    "Racing": {"coef": 1.2, "typical_up_h": 5.0}}}

print("\n== length_hours: top-up blend toward the genre-typical ▲ ==")
check("n >= K uses the game's own ▲ only",
      abs(M.length_hours(40.0, 20, "Action", COEFS) - 40.0) < 1e-9)
check("n > K still uses own ▲ only",
      abs(M.length_hours(40.0, 500, "Action", COEFS) - 40.0) < 1e-9)
check("coefficient multiplies", abs(M.length_hours(5.0, 50, "Racing", COEFS) - 6.0) < 1e-9)
# n = 10 of K = 20 -> geometric midpoint of 40 and the typical 10 -> 20
check("half weight = geometric midpoint",
      abs(M.length_hours(40.0, 10, "Action", COEFS) - 20.0) < 1e-9)
check("n = 3 leans toward typical",
      abs(M.length_hours(40.0, 3, "Action", COEFS)
          - 10 ** (0.15 * math.log10(40) + 0.85 * 1)) < 1e-9)
check("unknown genre falls back to global",
      abs(M.length_hours(4.0, 20, "Strategy", COEFS) - 3.6) < 1e-9)
check("cap is 420 h, balance starts at 100 h", M.LENGTH_CAP_H == 420.0 and M.BALANCE_ABOVE_H == 100.0)
check("capped at LENGTH_CAP_H", M.length_hours(50000.0, 500, "Action", COEFS) == M.LENGTH_CAP_H)
check("over the cap, clamp THEN balance with ▼ (geometric mean)",
      abs(M.length_hours(10000.0, 500, "Action", COEFS, 0.4) - (420 * 0.4) ** 0.5) < 1e-9)
check("over the cap with long ▼ stays capped",
      M.length_hours(5000.0, 500, "Action", COEFS, 3000.0) == M.LENGTH_CAP_H)
check("100-420 h: ▼ balances it",
      abs(M.length_hours(200.0, 500, "Action", COEFS, 2.0) - 20.0) < 1e-9)
check("100-420 h: a longer ▼ never raises it",
      abs(M.length_hours(150.0, 500, "Action", COEFS, 400.0) - 150.0) < 1e-9)
check("100-420 h with no ▼ stays as is", abs(M.length_hours(200.0, 500, "Action", COEFS) - 200.0) < 1e-9)
check("at or under 100 h, ▼ is ignored",
      abs(M.length_hours(100.0, 500, "Action", COEFS, 0.1) - 100.0) < 1e-9
      and abs(M.length_hours(40.0, 500, "Action", COEFS, 0.1) - 40.0) < 1e-9)
check("cap_h in coefs overrides", M.length_hours(500.0, 500, "Action", dict(COEFS, cap_h=100.0)) == 100.0)
check("balance_h in coefs overrides",
      abs(M.length_hours(200.0, 500, "Action", dict(COEFS, balance_h=float("inf")), 2.0) - 200.0) < 1e-9)
check("one real review still moves the result",
      M.length_hours(80.0, 3, "Action", COEFS) > M.length_hours(40.0, 3, "Action", COEFS))

print("\n== fans: floor and bad rows ==")
F = M.fans({"a": [600, 30, 3, 1], "b": [600, None, 2, 9], "c": [None, 60, 0, 5],
            "d": [0, 0, 5, 5]})
check("keeps n_up >= 3 with a median", set(F) == {"a"})
check("minutes -> hours, ▼ carried", F["a"] == (10.0, 3, 0.5))

print("\n== fit: real HLTB extra only, per-genre fallback ==")
play, hltb, pics = {}, {}, {}
for i in range(150):                      # Action: extra = 2 × ▲, all real
    a = f"A{i}"
    play[a] = [600, None, 60, 0]          # 10 h, 60 fans
    hltb[a] = {"extra": 20.0, "raw": {"extra": 20.0}}
    pics[a] = {"genres": [1]}
for i in range(40):                       # Racing: too few to fit alone
    a = f"R{i}"
    play[a] = [600, None, 60, 0]
    hltb[a] = {"extra": 5.0, "raw": {"extra": 5.0}}
    pics[a] = {"genres": [9]}
for i in range(50):                       # estimated-only extras must be ignored
    a = f"E{i}"
    play[a] = [600, None, 60, 0]
    hltb[a] = {"extra": 999.0, "raw": {}, "est": ["extra"]}
    pics[a] = {"genres": [1]}
C = M.fit(play, hltb, pics, IDS)
check("Action coef = 2.0 (estimates ignored)", abs(C["genres"]["Action"]["coef"] - 2.0) < 1e-6)
check("Racing below MIN_GENRE_GAMES -> global fallback",
      C["genres"]["Racing"]["fallback"] and C["genres"]["Racing"]["coef"] == C["global"]["coef"])
check("accuracy block present", C["accuracy"]["all"]["n"] == 190)
L = M.apply(play, pics, IDS, C)
check("apply covers every fan game", len(L) == 240)
play["X"] = [600000, 30, 500, 20]          # 10,000 h fans, 0.5 h detractors: idle-farmed
pics["X"] = {"genres": [1]}
LX = M.apply(play, pics, IDS, C)["X"]
check("guarded row carries raw figure + reason", len(LX) == 5 and LX[3] > 420 and LX[0] < 420 and LX[4] == "cap")
play["G"] = [9000, 60, 500, 20]             # 150 h fans x coef 2 = 300 h, 1 h detractors
pics["G"] = {"genres": [1]}
LG = M.apply(play, pics, IDS, C)["G"]
check("100-420 h row is balanced and says 'long'",
      len(LG) == 5 and LG[4] == "long" and abs(LG[3] - 300) < 0.5 and abs(LG[0] - (300 * 2.0) ** 0.5) < 0.1)
play["I"] = [6000, 60, 500, 20]             # 100 h fans, 1 h detractors, tagged Idler
pics["I"] = {"genres": [1], "tags": [7]}
LI = M.apply(play, pics, IDS, C, M.balanced_games(pics, {"7": "Idler"}, {}))["I"]
check("Idler balanced below the cap", LI[4] == "idler" and abs(LI[0] - 2.0 * 10) < 0.1 and LI[3] > LI[0])
check("balanced_games uses SteamSpy only when PICS has no tags",
      M.balanced_games({"p": {"tags": [1]}}, {"1": "Action"}, {"p": ["Idler"], "s": ["Idler"]}) == {"s"})
check("ordinary rows stay 3 elements", len(M.apply(play, pics, IDS, C)["A0"]) == 3)
check("apply row = [hours, n_up, genre_idx]",
      L["A0"][1] == 60 and M.GENRE_PRIORITY[L["A0"][2]] == "Action" and abs(L["A0"][0] - 20.0) < 0.05)

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
