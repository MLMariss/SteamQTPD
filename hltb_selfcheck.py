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
  - Popularity fast lane: the review-count tier can only ever SHORTEN a staleness
    window (min() semantics), tier boundaries are strict >, a popular entry with a
    complete triple does not freeze for RESCRAPE_FULL_DAYS, and the free count_comp
    signal is extracted type-strictly so a renamed HLTB field degrades to "no
    signal" rather than raising.

NOTE ON FIXTURES: load_games() yields (appid, title, review_count) TRIPLES. Any
fixture list passed to build_idle_drain / build_rescrape_queue must match that
shape — a 2-tuple raises ValueError at unpack and reds the run at startup.
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
    # load_games() yields (appid, title, review_count) triples — the third element
    # drives the popularity fast lane. 0 here means "not popular", so these drain
    # assertions exercise the attempt-scaled blank curve in isolation.
    games = [(10, "X", 0), (11, "Y", 0), (12, "Z", 0)]
    hh = {10: blank(0, 1), 11: blank(2, 1), 12: blank(R.BLANK_BACKOFF_ATTEMPTS, 1)}
    drain = R.build_idle_drain(games, hh, now, exclude_aids=set())
    ids = [aid for aid, _t, _b in drain]
    assert 12 not in ids, f"frozen blank must be skipped: {ids}"
    assert ids and ids[0] == 10, f"fewest-attempts first: {ids}"
    return True


def check_popularity_fast_lane():
    """Guards the review-count fast lane (and the free count_comp signal) that lets a
    popular game's entry be re-checked in days instead of sitting out a 14-day partial
    or 365-day full window. The regression this exists to prevent is the one that
    motivated the feature: an entry completing its triple and freezing for a year while
    HLTB keeps accumulating submissions behind it."""
    import hltb_refresh as R

    now = int(time.time())

    # 1) min() semantics: the fast lane may only ever SHORTEN a window, never lengthen
    #    one. If this inverts, unpopular games silently start refreshing less often.
    for bucket, ceiling in (("partial", R.RESCRAPE_PARTIAL_DAYS),
                            ("full", R.RESCRAPE_FULL_DAYS)):
        for rc in (0, 500, 501, 1000, 1001, 250000):
            w = R.effective_window_days({}, rc, bucket)
            assert w <= ceiling, f"{bucket} window grew for rc={rc}: {w} > {ceiling}"

    # 2) tier boundaries are strict > (a game exactly at a floor must NOT be boosted).
    assert R.popular_window_days(1000) == R.POPULAR_TIERS[1][1], "1000 is not > 1000"
    assert R.popular_window_days(1001) == R.POPULAR_TIERS[0][1]
    assert R.popular_window_days(500) is None, "500 is not > 500"
    assert R.popular_window_days(501) == R.POPULAR_TIERS[1][1]

    # 3) THE BLACK FLAG CASE: a popular entry with a complete triple must not freeze
    #    for RESCRAPE_FULL_DAYS. This is the whole point of overriding `full`.
    assert R.effective_window_days({}, 22000, "full") == R.POPULAR_TIERS[0][1]
    assert R.effective_window_days({}, 50, "full") == R.RESCRAPE_FULL_DAYS, \
        "unpopular full entries must keep their long window"

    # 4) the blank backoff curve still governs unpopular blanks, but a popular blank
    #    is pulled forward even from the frozen tier.
    frozen = {"attempts": R.BLANK_FREEZE_DAYS}
    assert R.effective_window_days(frozen, 50, "blank") == R.BLANK_FREEZE_DAYS
    assert R.effective_window_days(frozen, 22000, "blank") == R.POPULAR_TIERS[0][1]

    # 5) count_comp growth pulls a cold entry forward; extraction is type-strict so a
    #    renamed/absent field degrades to "no signal" instead of raising.
    assert R.effective_window_days({"comp_grew": True}, 0, "full") == R.COMP_GROWTH_DAYS
    assert R._comp_count({"count_comp": 120}) == 120
    assert R._comp_count({"count_comp": True}) is None, "bool must not read as a count"
    assert R._comp_count({"count_comp": "120"}) is None, "str must not read as a count"
    assert R._comp_count(None) is None
    assert R._comp_count({}) is None

    # 6) queue ordering: within a bucket, more-reviewed games come first.
    full = {"raw": {"main": 20.0, "extra": 35.0, "complete": 60.0},
            "fetched_at": now - 30 * R.DAY}
    q = R.build_rescrape_queue([(10, "Popular", 22000), (11, "Mid", 700)],
                               {10: dict(full), 11: dict(full)}, now)
    ids = [aid for aid, _t, _b in q]
    assert ids and ids[0] == 10, f"most-reviewed first within bucket: {ids}"
    return True


def run_all():
    check_phase_a()
    check_phase_b()
    check_popularity_fast_lane()
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
