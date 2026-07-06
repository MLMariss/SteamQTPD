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
    attempts counter lifecycle is correct, and the idle-drain ordering/freeze
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

    # 3) idle drain skips frozen tier and orders fewest-attempts-first.
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


def check_phase_c():
    """Phase C (IGDB) pure-logic guards: seconds->hours conversion, field mapping,
    and worklist priority (HLTB-blank before stale). Network paths not exercised."""
    import hltb_igdb as G

    # seconds -> hours
    assert G._sec_to_hours(3600) == 1.0, G._sec_to_hours(3600)
    assert G._sec_to_hours(0) is None
    assert G._sec_to_hours(None) is None
    assert G._sec_to_hours(5400) == 1.5

    # field mapping: normally->main, completely->complete, extra always None;
    # hastily is the main fallback when normally absent.
    m, e, c = G.times_from_ttb({"normally": 36000, "completely": 72000})
    assert (m, e, c) == (10.0, None, 20.0), (m, e, c)
    m, e, c = G.times_from_ttb({"hastily": 18000, "completely": 72000})
    assert m == 5.0 and c == 20.0, (m, e, c)

    # worklist: real-HLTB game skipped; HLTB-blank game prioritized before stale IGDB.
    now = 1_000_000_000
    games = [(1, "HasHLTB"), (2, "Blank"), (3, "StaleIGDB")]
    hltb = {1: {"avg": 12.0}}                                   # real HLTB (no est)
    igdb = {3: {"avg": 5.0, "fetched_at": now - 200 * G.DAY}}   # stale IGDB entry
    work = G.build_worklist(games, hltb, igdb, now)
    assert 1 not in work, f"real-HLTB game must be skipped: {work}"
    assert work[0] == 2, f"HLTB-blank game must come first: {work}"
    assert 3 in work, f"stale IGDB entry must be re-checked: {work}"
    return True


def run_all():
    check_phase_a()
    check_phase_b()
    check_phase_c()
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
