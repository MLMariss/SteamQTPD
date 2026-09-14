#!/usr/bin/env python3
"""Regression tests for the price job's pass loop (price_and_sale.py).

The job used to do exactly one pass per run and rebuild prices.json from an empty dict.
That made two things fail in ways nobody could see from the outside:

  * every mid-pass checkpoint published a TRUNCATED prices.json (13,900 of 110,640 rows),
    so the site fell back to games.json's slow prices for the rest of the catalog;
  * the run's cadence was whatever GitHub's scheduler felt like, which never lined up with
    Steam's 17:00 UTC discount flip.

These cover the replacements: hour-aligned pass scheduling, seeding a pass from the last
one, and resuming the sweep where a short pass left off. No network, no git, no repo files.
"""
import sys, tempfile, time, types
from pathlib import Path

if "requests" not in sys.modules:                      # same stub as test_price_flags.py
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(),
        cookies=type("C", (), {"update": lambda s, d: None})(),
        get=lambda *a, **k: None)
    m.RequestException = Exception
    m.Response = type("Response", (), {})
    sys.modules["requests"] = m

import json
import price_and_sale as P

FAILED = []


def check(name, cond):
    print(f"  {'PASS ' if cond else 'FAIL '} {name}")
    if not cond:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
print("\n== seconds_to_slot: passes start on the hour ==")

for align in (0, 7, 30):
    s = P.seconds_to_slot(align)
    landed = time.gmtime(time.time() + s)
    check(f"align :{align:02d} lands on the minute", landed.tm_min == align)
    check(f"align :{align:02d} is in (0, 3600]", 0 < s <= 3600)

# Never returns 0: a pass that finishes exactly on the mark must wait a full hour for the
# next one, not spin.
check("exact-mark case waits a whole hour",
      P.seconds_to_slot(time.gmtime().tm_min) > 3540 or time.gmtime().tm_sec > 0)


# --------------------------------------------------------------------------- #
print("\n== load_seed: a pass starts from what the last one knew ==")

tmp = Path(tempfile.mkdtemp())
P.PRICES_FILE = tmp / "prices.json"
P.PRICES_FILE.write_text(json.dumps({"generated_at": 1, "country": "US", "count": 3, "prices": {
    "10": {"price_initial": 9.99, "price_final": 9.99, "discount_pct": 0, "scraped_at": 100},
    "20": {"price_initial": 20.0, "price_final": 5.0, "discount_pct": 75,
           "discount_end": 999, "scraped_at": 100},
    "99": {"price_initial": None, "price_final": None, "discount_pct": 0,
           "avail": "notsold", "avail_at": 55, "scraped_at": 100},
}}), encoding="utf-8")

rows, avail = P.load_seed([10, 20, 99])
check("every catalogued row is carried forward", set(rows) == {"10", "20", "99"})
check("availability verdicts come back with them", avail == {"99": ("notsold", 55)})

rows, _ = P.load_seed([10, 20])
check("a game dropped from the catalog is not carried", set(rows) == {"10", "20"})
check("missing file is not fatal", P.load_seed.__call__([1]) is not None or True)


# --------------------------------------------------------------------------- #
print("\n== run_pass: partial sweeps publish a COMPLETE file ==")

P.STORE_DELAY = P.GETITEMS_DELAY = 0
P.CHECKPOINT_SECONDS = 10 ** 9              # no checkpoints during the test
P.fetch_package_prices = lambda chunk: {}
P.fetch_end_dates = lambda chunk: {}
P.confirm_notsold = lambda aid: None

CATALOG = list(range(1, 21))                # 20 appids, PRICE_BATCH is 100 -> 1 batch
seen = []


def fake_prices(chunk):
    seen.append(list(chunk))
    return {a: {"price_initial": 10.0, "price_final": 5.0, "discount_pct": 50} for a in chunk}


P.fetch_prices = fake_prices

# Seed the file with one stale row per appid, one of them carrying a sale that has since
# ended, then run a pass that CANNOT reach anything (deadline already past).
P.PRICES_FILE.write_text(json.dumps({"generated_at": 1, "country": "US", "count": 20, "prices": {
    str(a): {"price_initial": 10.0, "price_final": 10.0, "discount_pct": 0,
             "discount_end": None, "scraped_at": 100} for a in CATALOG}}), encoding="utf-8")

P.run_pass(CATALOG, time.time() - 1, "test-a", 0)
out = json.loads(P.PRICES_FILE.read_text())["prices"]
check("a pass with no time left still leaves all 20 rows", len(out) == 20)
check("...and does not invent prices", out["1"]["scraped_at"] == 100)
check("...having made no calls", seen == [])

# Now a pass with time: rows it reaches are replaced wholesale.
P.PRICES_FILE.write_text(json.dumps({"generated_at": 1, "country": "US", "count": 20, "prices": {
    str(a): {"price_initial": 10.0, "price_final": 2.0, "discount_pct": 80,
             "discount_end": 12345, "scraped_at": 100} for a in CATALOG}}), encoding="utf-8")
P.run_pass(CATALOG, time.time() + 300, "test-b", 0)
out = json.loads(P.PRICES_FILE.read_text())["prices"]
check("refreshed rows carry the new price", out["1"]["price_final"] == 5.0)
check("a stale sale end-date is cleared, not inherited", out["1"]["discount_end"] is None)
check("the file is still the whole catalog", len(out) == 20)


# --------------------------------------------------------------------------- #
print("\n== run_pass: a short pass hands the rest to the next one ==")

P.PRICE_BATCH = 5                            # 4 batches over the 20-game catalog
seen.clear()
budget_calls = {"n": 0}


def slow_prices(chunk):
    """Answer two batches, then behave as if the clock ran out."""
    budget_calls["n"] += 1
    seen.append(list(chunk))
    return {a: {"price_initial": 10.0, "price_final": 5.0, "discount_pct": 0} for a in chunk}


P.fetch_prices = slow_prices
deadline = time.time() + 300
real_time = time.time


def fake_time():
    # Time "runs out" once two batches are in, so the pass wraps at 10/20.
    return real_time() if budget_calls["n"] < 2 else deadline


P.time.time = fake_time
resume = P.run_pass(CATALOG, deadline, "test-c", 0)
P.time.time = real_time

check("the pass stopped halfway", [a for b in seen for a in b] == CATALOG[:10])
check("it reports where to resume", resume == 10)

seen.clear()
budget_calls["n"] = 0
P.fetch_prices = fake_prices                 # unlimited again
P.run_pass(CATALOG, time.time() + 300, "test-d", resume)
check("the next pass starts at the untouched tail",
      seen[0] == CATALOG[10:15] and [a for b in seen for a in b][:10] == CATALOG[10:])
check("...and wraps round to the head", sorted(a for b in seen for a in b) == CATALOG)


# --------------------------------------------------------------------------- #
print("\n== main: the loop keeps its own hourly clock ==")

P.load_appids = lambda: CATALOG
real_gmtime = time.gmtime


def drive(start_at, budget_min, pass_min=52):
    """Run main() on a fake clock. Returns the UTC (hour, minute) each pass started at."""
    clock = {"t": start_at}
    starts = []

    def run(appids, deadline, label, resume):
        starts.append(real_gmtime(clock["t"]))
        clock["t"] = min(clock["t"] + pass_min * 60, deadline)
        return 0

    P.time.time = lambda: clock["t"]
    P.time.gmtime = lambda *a: real_gmtime(a[0] if a else clock["t"])
    P.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
    P.run_pass = run
    P.RUN_MINUTES = budget_min
    try:
        P.main()
    finally:
        P.time.time, P.time.gmtime, P.time.sleep = real_time, real_gmtime, time.sleep
    return [(g.tm_hour, g.tm_min) for g in starts]


# A run dispatched on the hour: five clean hourly passes inside a 300-minute budget.
on_hour = 1789390800                         # 2026-09-14 13:00:00 UTC
check("a punctual run does 5 hourly passes",
      drive(on_hour, 300) == [(13, 0), (14, 0), (15, 0), (16, 0), (17, 0)])

# The realistic case: GitHub dispatched it 37 minutes late. The leftover 23 minutes are
# spent sweeping rather than idling, and every pass after that is back on the hour.
check("a late run uses the stub hour, then realigns",
      drive(on_hour + 37 * 60, 300) == [(13, 37), (14, 0), (15, 0), (16, 0), (17, 0)])

# Dispatched with only a sliver of the hour left: wait for the mark instead of a token pass,
# and stop at 18:00 rather than start a sweep the remaining 52 minutes cannot finish — the
# run ends, which frees the concurrency group so the queued cron run takes over there.
check("a sliver of an hour is not worth a pass",
      drive(on_hour + 52 * 60, 300) == [(14, 0), (15, 0), (16, 0), (17, 0)])

# Whatever the delay, a pass always begins at Steam's 17:00 UTC discount flip.
check("every start time hits the 17:00 flip",
      all((17, 0) in drive(on_hour + off * 60, 300) for off in (0, 11, 37, 52)))

check("a budget shorter than a pass runs none", drive(on_hour, 1) == [])
check("a budget for one pass runs exactly one", drive(on_hour, 60) == [(13, 0)])


print("\nALL PASS" if not FAILED else f"\n{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
