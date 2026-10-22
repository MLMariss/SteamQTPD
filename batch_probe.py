#!/usr/bin/env python3
"""batch_probe.py — can the batched store lookup replace per-game storefront calls?

READ-ONLY. Writes nothing to the repo, commits nothing, touches no data layer.

Why this exists: scraper.py spends its whole storefront budget (one call per ~0.9 s, shared
by everything it does) on two per-game endpoints — `appdetails` for every game it checks,
plus `appreviews` for every released one. IStoreBrowseService/GetItems, which the prices,
trailers, shots and demos jobs already use, answers 50 games per call on the much larger
api.steampowered.com budget. If it carries the same facts, the ~57k-game upcoming room could
be re-checked daily in ~25 minutes instead of over ~10 days, and released games' review
numbers might not need a storefront call at all. demo_probe.py settled the demo field the
same way: dump first, then build (ARCHITECTURE §2.1). Steam is blocked from a dev sandbox,
so this runs on an Actions runner.

The questions:

  Q1  Upcoming games: does GetItems carry what upcoming.json needs?
      -> name, coming-soon flag, release date, price, free flag, adult content descriptors.
         Compared field by field against full appdetails (the scraper's source) on a sample.

  Q2  Released games: do GetItems review numbers match the scraper's?
      -> GetItems `reviews` against live `appreviews` (language=all, purchase_type=all —
         exactly rating_from_reviews()) on a sample: count and % positive.

  Q3  What does it cost?
      -> seconds per 50-game call with the Q1/Q2 data_request, and what a full pending-room
         sweep would take at the jobs' usual 1.2 s pacing.

Output: a report on stdout (the run log), the same findings as tables in the job summary,
and the raw payloads under `batch_probe_out/` for upload as a build artifact.

Usage:
    python batch_probe.py
    QTPD_PROBE_SAMPLE=40 python batch_probe.py      # games per cohort for the comparisons
"""

import json
import os
import random
import sys
import time

import requests

GETITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
APPREVIEWS = "https://store.steampowered.com/appreviews/{appid}"
BATCH = 50
STORE_DELAY = float(os.environ.get("QTPD_PROBE_DELAY", "1.5"))   # matches the storefront jobs
GETITEMS_DELAY = 1.2
SAMPLE = int(os.environ.get("QTPD_PROBE_SAMPLE", "30"))          # per cohort, for Q1/Q2
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
TIMEOUT = 40
MAX_RETRIES = 4
OUT_DIR = "batch_probe_out"

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd batch probe; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)

# Everything a replacement for the pending-room probe and the review call could need.
DATA_REQUEST = {"include_basic_info": True, "include_release": True,
                "include_all_purchase_options": True, "include_reviews": True,
                "include_ratings": True, "include_tag_count": 20}

SUMMARY = []


def log(msg=""):
    print(msg, flush=True)


def hr(title):
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)
    SUMMARY.append(f"\n### {title}\n")


def get(url, *, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            try:
                return r.json()
            except ValueError:
                return None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


def getitems(appids):
    """{appid: store_item} for one batched call, plus the seconds it took."""
    payload = {"ids": [{"appid": int(a)} for a in appids],
               "context": {"country_code": "US", "language": "english"},
               "data_request": DATA_REQUEST}
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    t0 = time.time()
    data = get(GETITEMS, params=params)
    took = time.time() - t0
    time.sleep(GETITEMS_DELAY)
    items = ((data or {}).get("response") or {}).get("store_items") or []
    return {int(it.get("appid") or it.get("id")): it for it in items
            if (it.get("appid") or it.get("id"))}, took


def appdetails(appid):
    data = get(APPDETAILS, params={"appids": appid, "cc": "us", "l": "english"})
    time.sleep(STORE_DELAY)
    node = (data or {}).get(str(appid)) or {}
    return node.get("data") if node.get("success") else None


def appreviews(appid):
    """(pct, count) exactly as scraper.rating_from_reviews computes them."""
    data = get(APPREVIEWS.format(appid=appid),
               params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0})
    time.sleep(STORE_DELAY)
    if not isinstance(data, dict) or data.get("success") != 1:
        return None, 0
    s = data.get("query_summary", {})
    pos, neg = int(s.get("total_positive", 0)), int(s.get("total_negative", 0))
    return (round(pos / (pos + neg) * 100) if pos + neg else None), pos + neg


# --------------------------------------------------------------------------- #
# Field extraction — tolerant, since the exact GetItems shape is what's being probed
# --------------------------------------------------------------------------- #
def gi_upcoming_fields(it):
    rel = it.get("release") or {}
    bpo = it.get("best_purchase_option") or {}
    cents = lambda k: (int(bpo[k]) / 100) if str(bpo.get(k, "")).isdigit() else None
    return {
        "name": it.get("name"),
        "coming_soon": rel.get("is_coming_soon"),
        "release_ts": rel.get("steam_release_date") or rel.get("original_release_date"),
        "release_text": rel.get("custom_release_date_message") or rel.get("coming_soon_display"),
        "price_final": cents("final_price_in_cents"),
        "price_initial": cents("original_price_in_cents") or cents("final_price_in_cents"),
        "is_free": it.get("is_free"),
        "descriptors": sorted(int(x) for x in (it.get("content_descriptorids") or [])),
        "tags": len(it.get("tagids") or it.get("tags") or []),
    }


def ad_upcoming_fields(d):
    rd = d.get("release_date") or {}
    po = d.get("price_overview") or {}
    return {
        "name": d.get("name"),
        "coming_soon": rd.get("coming_soon"),
        "release_text": rd.get("date"),
        "price_final": (po.get("final") / 100) if po.get("final") else None,
        "price_initial": (po.get("initial") / 100) if po.get("initial") else None,
        "is_free": d.get("is_free"),
        "descriptors": sorted(int(x) for x in ((d.get("content_descriptors") or {}).get("ids") or [])
                              if str(x).isdigit()),
    }


def gi_reviews(it):
    """(pct, count) from GetItems' reviews block — whichever summary it carries."""
    rv = it.get("reviews") or {}
    for key in ("summary_unfiltered", "summary_filtered", "summary_language_specific"):
        s = rv.get(key)
        if isinstance(s, dict) and s.get("review_count") is not None:
            return s.get("percent_positive"), int(s.get("review_count") or 0), key
    return None, 0, None


def samples():
    rnd = random.Random(20261022)
    try:
        games = json.load(open("games.json", encoding="utf-8"))["games"]
    except (OSError, ValueError, KeyError):
        games = []
    try:
        pend = json.load(open("catalog.json", encoding="utf-8")).get("pending") or {}
    except (OSError, ValueError):
        pend = {}
    now = time.time()
    dated = [int(a) for a, ts in pend.items() if ts and ts > now]
    tba = [int(a) for a, ts in pend.items() if not ts]
    # Released: spread over review volume, so tiny and huge games are both represented.
    rel = sorted((g for g in games if (g.get("review_count") or 0) > 0 and g.get("release_ts")),
                 key=lambda g: g["review_count"])
    step = max(1, len(rel) // max(1, SAMPLE))
    released = [g["appid"] for g in rel[::step]][:SAMPLE]
    pick = lambda xs, n: rnd.sample(xs, min(n, len(xs)))
    return pick(dated, SAMPLE // 2), pick(tba, SAMPLE - SAMPLE // 2), released, len(pend)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    dump = {}
    dated, tba, released, pending_total = samples()
    upcoming = dated + tba
    log(f"samples: {len(dated)} dated + {len(tba)} TBA upcoming, {len(released)} released "
        f"(of {pending_total:,} pending)")

    # ---------------------------------------------------------------- GetItems pass
    gi, timings = {}, []
    for chunk in (upcoming, released):
        for i in range(0, len(chunk), BATCH):
            got, took = getitems(chunk[i:i + BATCH])
            gi.update(got); timings.append(took)
    dump["getitems"] = {str(a): it for a, it in gi.items()}

    # ---------------------------------------------------------------- Q1
    hr("Q1  Upcoming games: GetItems vs appdetails")
    fields = ["name", "coming_soon", "release_text", "price_final", "price_initial", "is_free", "descriptors"]
    agree = {f: [0, 0] for f in fields}       # [matches, compared]
    rows = []
    for aid in upcoming:
        d = appdetails(aid)
        it = gi.get(aid)
        if not d or not it:
            rows.append((aid, "appdetails" if not d else "getitems", "no answer"))
            continue
        a, g = ad_upcoming_fields(d), gi_upcoming_fields(it)
        for f in fields:
            agree[f][1] += 1
            agree[f][0] += (a.get(f) == g.get(f))
        diffs = {f: (a.get(f), g.get(f)) for f in fields if a.get(f) != g.get(f)}
        rows.append((aid, a.get("name"), diffs or "all match"))
        dump.setdefault("q1", {})[str(aid)] = {"appdetails": a, "getitems": g}
    for r in rows[:12]:
        log(f"  {r[0]}: {r[1]!r} -> {json.dumps(r[2], default=str)[:300]}")
    SUMMARY.append("| field | GetItems = appdetails | compared |\n|---|---|---|")
    for f in fields:
        m, n = agree[f]
        log(f"  {f:14} {m}/{n} match")
        SUMMARY.append(f"| {f} | {m} | {n} |")
    have = sum(1 for a in upcoming if a in gi)
    SUMMARY.append(f"\nGetItems answered {have} of {len(upcoming)} upcoming games. "
                   "`release_text` compares Steam's display string; GetItems also returns "
                   "`release_ts` (a timestamp) where appdetails has only text.")

    # ---------------------------------------------------------------- Q2
    hr("Q2  Released games: GetItems reviews vs appreviews (scraper's numbers)")
    SUMMARY.append("| appid | appreviews % / count | GetItems % / count | GetItems block |\n|---|---|---|---|")
    exact = close = compared = 0
    for aid in released:
        p_ar, n_ar = appreviews(aid)
        p_gi, n_gi, block = gi_reviews(gi.get(aid) or {})
        dump.setdefault("q2", {})[str(aid)] = {"appreviews": [p_ar, n_ar], "getitems": [p_gi, n_gi, block]}
        if n_ar and n_gi:
            compared += 1
            exact += (n_ar == n_gi and p_ar == p_gi)
            close += (abs(n_ar - n_gi) <= max(2, n_ar * 0.01) and p_ar is not None
                      and p_gi is not None and abs(p_ar - p_gi) <= 1)
        log(f"  {aid}: appreviews {p_ar}% / {n_ar}   getitems {p_gi}% / {n_gi} ({block})")
        SUMMARY.append(f"| {aid} | {p_ar}% / {n_ar} | {p_gi}% / {n_gi} | {block or '—'} |")
    log(f"\n  exact {exact}/{compared}, within 1 pt and 1% of count {close}/{compared}")
    SUMMARY.append(f"\n**Exact match {exact}/{compared}; within 1 point and 1% of the count "
                   f"{close}/{compared}.**")

    # ---------------------------------------------------------------- Q3
    hr("Q3  Cost")
    avg = sum(timings) / len(timings) if timings else 0
    calls = -(-pending_total // BATCH)
    sweep_min = calls * (avg + GETITEMS_DELAY) / 60
    log(f"  {len(timings)} GetItems calls, avg {avg:.2f}s each (+{GETITEMS_DELAY}s pacing)")
    log(f"  full pending sweep: {calls:,} calls ~ {sweep_min:.0f} min "
        f"(vs {pending_total:,} storefront calls ~ {pending_total * 0.9 / 3600:.1f} h at 0.9 s)")
    SUMMARY.append(f"Average {avg:.2f}s per 50-game call. A full sweep of the {pending_total:,} "
                   f"pending games: **{calls:,} calls, ~{sweep_min:.0f} min**, against "
                   f"{pending_total:,} storefront calls (~{pending_total * 0.9 / 3600:.1f} h of "
                   f"the scraper's budget at 0.9 s).")

    with open(os.path.join(OUT_DIR, "batch_probe.json"), "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=1, ensure_ascii=False, default=str)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Batched lookup probe\n" + "\n".join(SUMMARY) + "\n")
    log(f"\nraw payloads -> {OUT_DIR}/batch_probe.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
