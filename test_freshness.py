#!/usr/bin/env python3
"""Tests for freshness.py's cron projection (the "next run" and "missed" columns). Pure logic.

The weekday field used to be unsupported, so 3.3 (length-fit, `17 3 * * 1`) showed no next run
and could never be scored as missed."""
import sys
from datetime import datetime, timezone

import freshness as F

fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


UTC = timezone.utc
MON = datetime(2026, 10, 26, 19, 0, tzinfo=UTC)          # a Monday evening
ts = lambda *a: int(datetime(*a, tzinfo=UTC).timestamp())

print("\n== next fire ==")
check("daily", F.next_fire(["13 5 * * *"], MON) == datetime(2026, 10, 27, 5, 13, tzinfo=UTC))
check("every 2h", F.next_fire(["53 */2 * * *"], MON) == datetime(2026, 10, 26, 20, 53, tzinfo=UTC))
check("weekly on Monday, from a Monday evening -> next Monday",
      F.next_fire(["17 3 * * 1"], MON) == datetime(2026, 11, 2, 3, 17, tzinfo=UTC))
check("Sunday as 0 and as 7", F.next_fire(["0 12 * * 0"], MON) == F.next_fire(["0 12 * * 7"], MON)
      == datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
check("a day-of-month cron is not projected", F.next_fire(["0 0 1 * *"], MON) is None)

print("\n== cadence (worst-case wait) ==")
check("weekly -> 7 days", F.cadence_seconds(["17 3 * * 1"], MON) == 7 * 86400)
check("irregular 4x a day -> its longest gap", F.cadence_seconds(["41 4,10,16,22 * * *"], MON) == 6 * 3600)

print("\n== missed fires ==")
check("weekly, written after this Monday's fire -> 0", F.missed_fires(["17 3 * * 1"], ts(2026, 10, 26, 9, 12), 15, MON) == 0)
check("weekly, last written 9 days ago -> 2", F.missed_fires(["17 3 * * 1"], ts(2026, 10, 17, 9, 12), 15, MON) == 2)
check("a fire still inside its grace is not missed",
      F.missed_fires(["0 18 * * *"], ts(2026, 10, 25, 18, 30), 120, MON) == 0)

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
