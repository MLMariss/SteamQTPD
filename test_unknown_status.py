#!/usr/bin/env python3
"""Regression tests for stored games Steam stops answering for (scraper.py strike_no_answer).

Valkyrie Girl (4671710) was stored as an upcoming game in June 2026; then Steam's appdetails
began answering success:false for it. The old rule — MAX_RECHECK strikes, then `skipped` —
was written for apps never stored: for a stored one it left the record in games.json, frozen,
and its strikes started over, so it headed the Upcoming view four months later. A stored game
now keeps its record, is asked again every run, and from MAX_RECHECK strikes on carries
`unknown` (the latest unanswered check) until Steam answers again. Pure logic only: no network."""
import sys, types

# Stub `requests` so the module imports without the dependency present.
if "requests" not in sys.modules:
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(),
        cookies=type("C", (), {"update": lambda s, d: None})(),
        get=lambda *a, **k: None)
    m.RequestException = Exception
    m.Response = type("Response", (), {})
    sys.modules["requests"] = m

import scraper as S

S.log = lambda msg: None
S.load_pics_rev = lambda: {}

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


CAP = S.MAX_RECHECK
T0 = 1_790_000_000

print("An app never stored keeps the bounded retry:")
recheck, skipped = {}, set()
for i in range(CAP - 1):
    S.strike_no_answer(5, None, recheck, skipped, T0 + i)
check(f"{CAP - 1} strikes -> still rechecking", recheck.get(5) == CAP - 1 and 5 not in skipped)
S.strike_no_answer(5, None, recheck, skipped, T0 + CAP)
check(f"strike {CAP} -> skipped, out of recheck", 5 in skipped and 5 not in recheck)

print("A stored game is marked, never skipped:")
rec = {"appid": 4671710, "title": "Valkyrie Girl", "release_date": "Q2 2026", "scraped_at": T0 - 86400}
recheck, skipped = {}, set()
for i in range(CAP - 1):
    S.strike_no_answer(4671710, rec, recheck, skipped, T0 + i)
check(f"{CAP - 1} strikes -> not marked yet", "unknown" not in rec)
S.strike_no_answer(4671710, rec, recheck, skipped, T0 + 100)
check(f"strike {CAP} -> marked with that check's time", rec.get("unknown") == T0 + 100)
check("not skipped", 4671710 not in skipped)
check("strikes keep counting", recheck.get(4671710) == CAP)
check("scraped_at still says when Steam last answered", rec["scraped_at"] == T0 - 86400)
S.strike_no_answer(4671710, rec, recheck, skipped, T0 + 200)
check("a later miss moves the mark to the latest check", rec["unknown"] == T0 + 200)
check("...and still counts", recheck.get(4671710) == CAP + 1)

print("A mark outlives lost strikes:")
rec2 = {"appid": 7, "scraped_at": T0, "unknown": T0 + 5}
S.strike_no_answer(7, rec2, {}, set(), T0 + 9)
check("one strike on an already-marked record keeps it marked", rec2.get("unknown") == T0 + 9)

print("select_work asks again every run, first:")
now = int(S.time.time())
processed = {
    "1": {"appid": 1, "scraped_at": now - 3600, "release_ts": now - 30 * 86400},  # store page moved
    "2": {"appid": 2, "scraped_at": now - 200 * 86400, "unknown": now - 3600},   # marked
    "3": {"appid": 3, "scraped_at": now - 100 * 86400},                           # 1 strike, not marked
    "4": {"appid": 4, "scraped_at": now},                                         # nothing due
}
master = {1: now, 2: 0, 3: 0, 4: 0}        # only 1's last_modified moved past scraped_at
catalog = {"skipped": [], "pending": {}, "priority": [], "force_refresh": [], "recheck": {3: 1}}
_, refresh = S.select_work(master, True, processed, catalog)
check("marked and struck games are queued", 2 in refresh and 3 in refresh)
check("ahead of a last_modified refresh", refresh.index(2) < refresh.index(1)
      and refresh.index(3) < refresh.index(1))
check("nothing else is added", 4 not in refresh)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
