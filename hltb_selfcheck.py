#!/usr/bin/env python3
"""
SteamQHPP — HLTB startup self-checks
====================================
Fail-fast regression guards run at the top of hltb_refresh.main(). If any assert
here fails, the run aborts BEFORE touching hltb.json — a loud red failure is far
better than silently degrading match quality or retry cadence (the same failure
class as the playtime_raw silent-green bug). All checks are pure (no network).

Covers:
  - Phase A: title normalizer produces the expected recovery variants and never
    regresses a clean title or reorders the raw-first invariant.
  - Phase B: blank retry window backs off monotonically with attempts, the
    attempts counter lifecycle is correct, a blank re-scrape result can never
    erase a prior entry's real raw data, and the idle-drain ordering/freeze
    rules hold.
"""

import time


def check_phase_a():
    import hltb_match as HM
    HM.assert_healthy()                   # the module's own case table
    # Extra invariants beyond the module's internal cases:
    v = HM.query_variants("HELLDIVERS™ 2")
    assert "Helldivers 2" in v or "HELLDIVERS 2" in v, v
    assert HM.query_variants("") == [""]  # empty title -> single empty variant, no crash
    return True


def check_phase_b():
    import hltb_refresh as R
    import hltb_estimate as HE

    # 1) window backs off monotonically and never below eager / above freeze.
    days = [R.blank_window_days(a) for a in range(0, 9)]
    assert days == sorted(days), f"blank window must be non-decreasing: {days}"
    assert days[0] == R.BLANK_EAGER_DAYS, days
    assert days[-1] == R.BLANK_FREEZE_DAYS, days

    now = int(time.time())
    ratios, _ = HE.compute_ratios({})

    # 2) attempts lifecycle: blank increments, match clears.
    h = {}
    R.store_entry(h, 1, {"main": None, "extra": None, "complete": None, "match": None},
                  ratios, now)
    assert h[1]["attempts"] == 1, h[1]
    R.store_entry(h, 1, {"main": None, "extra": None, "complete": None, "match": None},
                  ratios, now)
    assert h[1]["attempts"] == 2, h[1]
    R.store_entry(h, 1, {"main": 5, "extra": 8, "complete": 12, "match": "X"}, ratios, now)
    assert "attempts" not in h[1] and h[1]["avg"] is not None, h[1]

    # 3) a blank re-scrape result must NEVER wipe a prior entry's real raw data
    #    (the bug: make_entry builds `raw` fresh from the fetch alone, so a clean
    #    all-None miss on a re-scrape used to silently erase existing real values).
    h2 = {}
    R.store_entry(h2, 2, {"main": 5, "extra": 8, "complete": 12, "match": "X"}, ratios, now)
    prior_raw = dict(h2[2]["raw"])
    R.store_entry(h2, 2, {"main": None, "extra": None, "complete": None, "match": None},
                  ratios, now + 1)
    assert h2[2]["raw"] == prior_raw, f"blank re-scrape must not erase real raw data: {h2[2]}"
    assert h2[2]["avg"] is not None, h2[2]

    # 4) idle drain skips frozen tier and orders fewest-attempts-first.
    def blank(a, age_d):
        return {"main": None, "extra": None, "complete": None, "avg": None,
                "raw": {"main": None, "extra": None, "complete": None},
                "attempts": a, "fetched_at": now - age_d * R.DAY}
    games = [(10, "X"), (11, "Y"), (12, "Z")]
    hh = {10: blank(0, 1), 11: blank(2, 1), 12: blank(R.BLANK_BACKOFF_ATTEMPTS, 1)}
    drain = R.build_idle_drain(games, hh, now, exclude_aids=set())
    ids = [aid for aid, _t, _b in drain]
    assert 12 not in ids, f"frozen blank must be skipped: {ids}"
    assert ids and ids[0] == 10, f"fewest-attempts first: {ids}"
    return True


def run_all():
    check_phase_a()
    check_phase_b()
    return True


if __name__ == "__main__":
    # Allow standalone execution with a stubbed HLTB lib (no network).
    import sys, types
    if "howlongtobeatpy" not in sys.modules:
        stub = types.ModuleType("howlongtobeatpy")
        class _H:  # noqa
            def search(self, q):
                return []
        stub.HowLongToBeat = _H
        sys.modules["howlongtobeatpy"] = stub
    run_all()
    print("HLTB self-checks OK")
