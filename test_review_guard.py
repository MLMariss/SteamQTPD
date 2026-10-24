#!/usr/bin/env python3
"""Regression tests for the review wipe of 22-24 Oct 2026 (scraper.py, ARCHITECTURE §16).

From 22 Oct 23:05 UTC Steam's appreviews stopped giving the runners a usable answer, and
rating_from_reviews() returned the same (None, 0) for "failed" as for "no reviews". Every
refresh stored that zero over the real count: 15k+ games, War Thunder's 792,828 reviews
among them. Pure logic only: Steam is stubbed, no network, no git."""
import json, pathlib, sys, tempfile, types

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

S.storefront_pace = lambda: None
S.NEWS_DELAY = 0
S.log = lambda msg: None

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


def steam(reviews, news, appid=236390):
    """A fake get(): appdetails answers, appreviews / news give what the test says."""
    details = {str(appid): {"success": True, "data": {
        "type": "game", "name": "War Thunder", "is_free": True,
        "release_date": {"date": "Aug 15, 2013", "coming_soon": False},
        "genres": [{"description": "Action"}]}}}
    def get(url, *, params=None, timeout=30, expect_json=False):
        if "appdetails" in url:
            return details
        if "appreviews" in url:
            return reviews
        return news
    return get


ZERO = {"success": 1, "query_summary": {"total_positive": 0, "total_negative": 0}}
REAL = {"success": 1, "query_summary": {"total_positive": 600, "total_negative": 400}}
NEWS = {"appnews": {"newsitems": [{"date": 1792800000, "tags": ["patchnotes"]}]}}
NO_NEWS = {"appnews": {"newsitems": []}}
PREV = {"appid": 236390, "rating_pct": 71, "review_count": 792828, "last_update_ts": 1792000000}

print("\n== rating_from_reviews: failure is not 'no reviews' ==")
for name, reply in [("no reply / non-JSON body", None),
                    ("success != 1", {"success": 2}),
                    ("no query_summary", {"success": 1}),
                    ("summary without totals", {"success": 1, "query_summary": {"num_reviews": 0}})]:
    S.get = steam(reply, NEWS)
    check(f"{name} -> FAILED", S.rating_from_reviews(236390) is S.FAILED)
S.get = steam(ZERO, NEWS)
check("Steam's own 0/0 -> (None, 0)", S.rating_from_reviews(236390) == (None, 0))
S.get = steam(REAL, NEWS)
check("600 up / 400 down -> (60, 1000)", S.rating_from_reviews(236390) == (60, 1000))

print("\n== keep_reviews: the stored count survives a bad answer ==")
check("FAILED keeps the stored pair",          S.keep_reviews(S.FAILED, PREV) == (71, 792828))
check("zero after a count keeps the stored pair", S.keep_reviews((None, 0), PREV) == (71, 792828))
check("a real answer replaces it",             S.keep_reviews((60, 1000), PREV) == (60, 1000))
check("a new game takes Steam's zero",         S.keep_reviews((None, 0), None) == (None, 0))
check("a new game whose fetch failed -> (None, 0)", S.keep_reviews(S.FAILED, None) == (None, 0))
check("a stored zero takes a real answer",
      S.keep_reviews((90, 12), {"rating_pct": None, "review_count": 0}) == (90, 12))

print("\n== last_update_from_news: failure is not 'no updates' ==")
S.get = steam(REAL, None)
check("no reply -> FAILED", S.last_update_from_news(236390) is S.FAILED)
S.get = steam(REAL, {"error": "x"})
check("reply without appnews -> FAILED", S.last_update_from_news(236390) is S.FAILED)
S.get = steam(REAL, NO_NEWS)
check("no update posts -> None", S.last_update_from_news(236390) is None)
S.get = steam(REAL, NEWS)
check("patch post -> its date", S.last_update_from_news(236390) == 1792800000)

print("\n== build_record: a refresh during the outage keeps the stored record ==")
S.get = steam(None, None)
rec = S.build_record(236390, prev=dict(PREV))
check("reviews kept (was wiped to 0)", (rec["rating_pct"], rec["review_count"]) == (71, 792828))
check("last update kept",              rec["last_update_ts"] == 1792000000)
S.get = steam(ZERO, NEWS)
rec = S.build_record(236390, prev=dict(PREV))
check("success:1 with 0/0 after 792k reviews is kept too", rec["review_count"] == 792828)
S.get = steam(REAL, NEWS)
rec = S.build_record(236390, prev=dict(PREV))
check("a working fetch still updates", (rec["rating_pct"], rec["review_count"]) == (60, 1000))
check("...and the last update",        rec["last_update_ts"] == 1792800000)
S.get = steam(None, None)
rec = S.build_record(236390, prev=None)
check("a new game during the outage stores (None, 0)", (rec["rating_pct"], rec["review_count"]) == (None, 0))
check("...and no last update",         rec["last_update_ts"] is None)

print("\n== apply_review_restore: only zeros written by the wipe come back ==")
WIPE = 1792710300
tmp = pathlib.Path(tempfile.mkdtemp()) / "reviews_restore.json"
tmp.write_text(json.dumps({"wiped_from": WIPE, "games": {
    "1": [71, 792828], "2": [92, 26], "3": [80, 500], "4": [55, 40]}}), encoding="utf-8")
S.REVIEW_RESTORE_FILE = tmp
processed = {
    "1": {"rating_pct": None, "review_count": 0, "scraped_at": WIPE + 60},     # wiped
    "2": {"rating_pct": None, "review_count": 0, "scraped_at": WIPE + 9000},   # wiped
    "3": {"rating_pct": 81, "review_count": 520, "scraped_at": WIPE + 9000},   # later good fetch
    "4": {"rating_pct": None, "review_count": 0, "scraped_at": WIPE - 60},     # zero before the wipe
    "5": {"rating_pct": None, "review_count": 0, "scraped_at": WIPE + 60},     # new, no pre-wipe data
}
check("two wiped records restored", S.apply_review_restore(processed) == 2)
check("War Thunder back to 792,828",    processed["1"]["review_count"] == 792828)
check("its rating back to 71",          processed["1"]["rating_pct"] == 71)
check("a newer real count is not overwritten", processed["3"]["review_count"] == 520)
check("a zero from before the wipe is left alone", processed["4"]["review_count"] == 0)
check("a game with no pre-wipe data stays 0", processed["5"]["review_count"] == 0)
check("a second pass changes nothing",  S.apply_review_restore(processed) == 0)
S.REVIEW_RESTORE_FILE = tmp.with_name("missing.json")
check("no file -> nothing restored, no error", S.apply_review_restore(processed) == 0)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
