#!/usr/bin/env python3
"""review_probe.py — Phase 0 probe for the Review Digest (see REVIEW_DIGEST_PLAN.md).

READ-ONLY. Writes nothing to the repo, commits nothing, touches no data layer.

Why this exists: the plan's remaining unknowns are all empirical, and none of them can be
answered from a dev sandbox because `store.steampowered.com` is blocked there (verified —
the proxy returns 403 to CONNECT). This is the same wall the trailer work hit, and the
same resolution: run it on an Actions runner and look at the real payload. *Dump first,
then tighten* (ARCHITECTURE §2.1).

The five questions:

  Q1  Does `appreviews` send `Access-Control-Allow-Origin`?
      -> Decides branch A0 (browser calls Steam directly, NO backend at all) vs
         A1 (a Cloudflare Worker passthrough has to be written). Biggest question here.

  Q2  How deep does the `filter=recent` cursor page reliably?
      -> The design needs 5 clean pages for a 500-review sample. Checks for repeated
         cursors, duplicate ids across pages, and short/empty pages.

  Q3  Is `filter_offtopic_activity=0` really *include*, and does `query_summary` move
      with it?
      -> The header prints query_summary as the population anchor. If the anchor excludes
         review bombs while the sample includes them, the two numbers are measured
         differently and the bundle has to say so explicitly. (§5 of the plan.)

  Q4  Are the four flag fields actually present and populated?
      -> `[EA]` `[free]` `[deck]` `[upd]`. Flags that are always-null are wasted tokens.

  Q5  What are the REAL compaction numbers?
      -> Length distribution, ASCII-art ratio, BBCode frequency, duplicate rate. The
         plan's 600-char cap and 0.4 art-ratio are placeholders to be replaced with
         whatever the data says.

Output: a report on stdout (read it in the run log) plus a JSON dump under
`review_probe_out/` for upload as a build **artifact**. Deliberately NOT committed —
the plan's rule is that no review prose lands in the repo, and the probe follows it too.

Usage:
    python review_probe.py                 # default appids
    QTPD_PROBE_APPIDS=570,1091500 python review_probe.py
"""

import json
import os
import re
import statistics
import sys
import time
from collections import Counter

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# A deliberately mixed set — the questions have different answers per game shape:
#   1091500 Cyberpunk 2077 — huge, heavily review-bombed at launch, left early access
#                            era behind; the best test for Q3.
#   892970  Valheim        — long early-access history; the best test for [EA] in Q4.
#   570     Dota 2         — enormous and very multilingual; the best test for the
#                            English-share question and for cursor depth in Q2.
DEFAULT_APPIDS = [1091500, 892970, 570]

PAGES = int(os.environ.get("QTPD_PROBE_PAGES", "6"))      # 5 needed for 500; 6 to see the edge
PER_PAGE = 100                                            # Steam's hard max
DELAY = float(os.environ.get("QTPD_PROBE_DELAY", "1.5"))  # matches the other storefront jobs
TIMEOUT = 30
MAX_RETRIES = 4
OUT_DIR = "review_probe_out"

ORIGIN = "https://mlmariss.github.io"                     # the Pages origin the browser would send

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd review-digest probe; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)

BASE = "https://store.steampowered.com/appreviews/{appid}"


def log(msg=""):
    print(msg, flush=True)


def hr(title=""):
    log()
    log("=" * 78)
    if title:
        log(title)
        log("=" * 78)


def get_raw(url, *, params=None, headers=None):
    """Same retry/backoff contract as the other storefront scrapers, but returns the
    whole Response — the probe needs headers and status, not just the JSON body."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            return r
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


def get_json(url, *, params=None):
    r = get_raw(url, params=params)
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Q0 — GetAppReviews, the replacement Valve named when it switched `appreviews` off on
# 22 Oct 2026 (it has redirected to the store front page since; ARCHITECTURE §16).
# The docs and third-party migrations disagree on parameter names (`language` vs a
# `languages` list, `purchase_type` "all" vs an enum) and on what the defaults count, and
# a wrong guess still answers 200, just with smaller numbers. So this dumps the RAW reply
# for each variant: the totals decide which spelling matches what appreviews used to give
# (War Thunder 236390 held 71% / 792,828 at language=all, purchase_type=all).
# --------------------------------------------------------------------------- #
GAR = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"

# Run 1 (25 Oct) settled the spellings: `language` and purchase_type "all" are ignored
# (War Thunder 834 = the default, English + Steam purchases only); `languages: ["all"]`
# works (1,855) and so does purchase_type 1 (329,175). It also showed filter "recent" is
# ignored (day_range_used 30, helpful order), and num_per_page 0 is not honoured. Run 2
# answered the rest: the combination gives War Thunder 793,469 at 71% (appreviews: 792,828
# at 71% three days earlier); filter 1 is newest-first over the whole list, 100 to a page,
# with a clean cursor (no overlap); every string spelling of "recent" is ignored; review
# and author fields are unchanged; including off-topic activity adds review bombs
# (918,903 at 64%), so the scraper keeps Steam's default and excludes them.
ALL = {"languages": ["all"], "purchase_type": 1}

GAR_VARIANTS = [
    ("all langs + all purchases",  dict(ALL, num_per_page=1)),
    ("...+offtopic included",      dict(ALL, num_per_page=1, filter_offtopic_activity=False)),
    ("english + all purchases",    {"languages": ["english"], "purchase_type": 1, "num_per_page": 1}),
    ("languages=[] (docs: English)", {"languages": [], "purchase_type": 1, "num_per_page": 1}),
] + [
    (f"filter={f!r}", dict(ALL, num_per_page=5, filter=f, cursor="*"))
    for f in (0, 1, 2, 3, "Recent", "recent", "k_EUserReviewsFilterRecent", "updated", "all")
] + [
    ("100 per page, filter=1",     dict(ALL, num_per_page=100, filter=1, cursor="*")),
]


def gar(appid, body):
    """One GetAppReviews call -> (status, parsed `response` dict | None, raw text)."""
    r = get_raw(GAR, params={"input_json": json.dumps(dict(body, appid=appid))})
    time.sleep(DELAY)
    if r is None:
        return None, None, ""
    try:
        data = r.json()
    except ValueError:
        return r.status_code, None, r.text
    return r.status_code, (data.get("response") if isinstance(data, dict) else None), r.text


def describe(resp):
    """The parts of a reply that decide the migration, on one line."""
    qs = resp.get("query_summary") or {}
    revs = resp.get("reviews") or []
    ts = [rv.get("timestamp_created") for rv in revs]
    order = ("newest-first" if ts == sorted(ts, reverse=True) else
             "oldest-first" if ts == sorted(ts) else "unsorted") if len(ts) > 1 else "-"
    pos, neg = qs.get("total_positive"), qs.get("total_negative")
    pct = round(pos / (pos + neg) * 100) if pos is not None and neg is not None and pos + neg else None
    return (f"total={qs.get('total_reviews')} (+{pos}/-{neg} = {pct}%) | {len(revs)} reviews "
            f"{order} | day_range_used={resp.get('day_range_used')} "
            f"total_matching={resp.get('total_matching')} | cursor={resp.get('cursor')!r}")


def probe_getappreviews(appid):
    hr(f"Q0  GetAppReviews — what replaces appreviews  (appid {appid})")
    out = {}
    for name, body in GAR_VARIANTS:
        status, resp, text = gar(appid, body)
        if resp is None:
            log(f"  [{name}] {status} not JSON / no response: {text[:300]!r}")
            out[name] = {"status": status}
            continue
        log(f"  [{name}] {status} {describe(resp)}")
        out[name] = {"status": status, "keys": sorted(resp), "summary": resp.get("query_summary"),
                     "day_range_used": resp.get("day_range_used"),
                     "total_matching": resp.get("total_matching")}

    # Paging: a second page through the cursor, newest-first, must continue where page 1
    # ended (no repeated ids, older timestamps). This is what playtime_refresh.py walks.
    status, p1, _ = gar(appid, dict(ALL, num_per_page=20, filter=1, cursor="*"))
    if p1 and p1.get("cursor"):
        status, p2, _ = gar(appid, dict(ALL, num_per_page=20, filter=1, cursor=p1["cursor"]))
        if p2:
            ids1 = {rv.get("recommendationid") for rv in p1.get("reviews") or []}
            ids2 = [rv.get("recommendationid") for rv in p2.get("reviews") or []]
            t1 = [rv.get("timestamp_created") for rv in p1.get("reviews") or []]
            t2 = [rv.get("timestamp_created") for rv in p2.get("reviews") or []]
            log(f"  [paging filter=1] page1 {len(ids1)} / page2 {len(ids2)} reviews, "
                f"overlap {len(ids1 & set(ids2))}, page1 oldest {min(t1, default=None)} "
                f"vs page2 newest {max(t2, default=None)}, next cursor {p2.get('cursor')!r}")
    sample = (p1 or {}).get("reviews") or []
    if sample:
        log(f"  review fields: {sorted(sample[0])}")
        log(f"  author fields: {sorted(sample[0].get('author') or {})}")
    return out


# --------------------------------------------------------------------------- #
# Q1 — CORS. The one that decides whether this feature needs a backend at all.
# --------------------------------------------------------------------------- #
def probe_cors(appid):
    hr("Q1  CORS — does appreviews allow a browser to call it directly?")
    url = BASE.format(appid=appid)
    params = {"json": 1, "num_per_page": 1, "filter": "recent", "language": "english", "cursor": "*"}

    log(f"GET {url}  (with Origin: {ORIGIN})")
    r = get_raw(url, params=params, headers={"Origin": ORIGIN})
    if r is None:
        log("  FAILED — no response. Cannot answer Q1.")
        return {"reachable": False}

    log(f"  status: {r.status_code}")
    log("  response headers:")
    for k, v in sorted(r.headers.items()):
        log(f"    {k}: {v}")

    acao = r.headers.get("Access-Control-Allow-Origin")
    # A preflight is only needed for non-simple requests; ours is a simple GET, so the
    # presence of ACAO on the GET response is what actually decides it. OPTIONS is
    # probed anyway because a surprising answer there is worth seeing.
    log()
    log("  OPTIONS preflight (informational — a simple GET does not require one):")
    try:
        pre = SESSION.options(url, params=params, timeout=TIMEOUT,
                              headers={"Origin": ORIGIN,
                                       "Access-Control-Request-Method": "GET"})
        log(f"    status: {pre.status_code}")
        log(f"    allow-origin:  {pre.headers.get('Access-Control-Allow-Origin')}")
        log(f"    allow-methods: {pre.headers.get('Access-Control-Allow-Methods')}")
    except requests.RequestException as e:
        log(f"    OPTIONS failed: {e}")

    hr()
    if acao and (acao == "*" or ORIGIN in acao):
        log(f"  >>> VERDICT: BRANCH A0. Access-Control-Allow-Origin = {acao!r}")
        log("      The browser can call Steam directly. NO backend, NO Worker, NO deploy.")
        log("      The whole feature is client-side in index.html.")
    elif acao:
        log(f"  >>> VERDICT: PARTIAL. ACAO present but restrictive: {acao!r}")
        log("      Check whether the Pages origin is covered; likely still needs A1.")
    else:
        log("  >>> VERDICT: BRANCH A1. No Access-Control-Allow-Origin header.")
        log("      ARCHITECTURE §1 holds for this endpoint too — a Cloudflare Worker")
        log("      passthrough has to be written (the wishlist Worker's source is lost).")
    return {"reachable": True, "status": r.status_code, "acao": acao,
            "headers": dict(r.headers)}


# --------------------------------------------------------------------------- #
# Q2 — cursor depth. The sample needs 5 clean pages.
# --------------------------------------------------------------------------- #
def probe_cursor(appid, offtopic_included=True):
    hr(f"Q2  Cursor depth on filter=recent  (appid {appid})")
    url = BASE.format(appid=appid)
    cursor = "*"
    seen = set()
    reviews = []
    summary = None
    cursors = []
    pages_ok = 0

    for page in range(1, PAGES + 1):
        params = {"json": 1, "language": "english", "purchase_type": "all",
                  "num_per_page": PER_PAGE, "filter": "recent", "cursor": cursor,
                  "filter_offtopic_activity": 0 if offtopic_included else 1}
        data = get_json(url, params=params)
        time.sleep(DELAY)
        if not isinstance(data, dict) or data.get("success") != 1:
            log(f"  page {page}: FAILED (success != 1) — stopping")
            break
        if summary is None:
            summary = data.get("query_summary") or {}
        batch = data.get("reviews") or []
        ids = [rv.get("recommendationid") for rv in batch]
        new = [i for i in ids if i not in seen]
        dupes = len(ids) - len(new)
        seen.update(ids)
        reviews.extend(batch)

        nxt = data.get("cursor")
        repeated = nxt in cursors or nxt == cursor
        cursors.append(nxt)
        log(f"  page {page}: {len(batch):3d} returned · {len(new):3d} new · {dupes:3d} dupes"
            f" · cursor {'REPEATED' if repeated else 'advanced'}")

        if len(batch) == 0:
            log("    -> empty page: Steam is out of reviews here"); break
        if dupes == 0 and len(batch) == PER_PAGE:
            pages_ok = page
        if repeated or not nxt:
            log("    -> cursor stopped advancing"); break
        if len(batch) < PER_PAGE:
            log("    -> short page: end of list"); break
        cursor = nxt

    log()
    log(f"  clean full pages: {pages_ok}  (need 5 for a 500-review sample)")
    log(f"  unique reviews collected: {len(seen)}")
    if pages_ok >= 5:
        log("  >>> 500-review sample is reachable on filter=recent.")
    else:
        log("  >>> WARNING: could not get 5 clean pages. Either this game has too few")
        log("      English reviews, or the cursor degrades earlier than assumed.")
    return reviews, summary or {}


# --------------------------------------------------------------------------- #
# Q3 — filter_offtopic_activity polarity, and whether query_summary moves with it.
# --------------------------------------------------------------------------- #
def probe_offtopic(appid):
    hr(f"Q3  filter_offtopic_activity — polarity + query_summary coupling  (appid {appid})")
    url = BASE.format(appid=appid)
    out = {}
    for label, val in (("include (0)", 0), ("exclude (1)", 1)):
        params = {"json": 1, "language": "all", "purchase_type": "all",
                  "num_per_page": PER_PAGE, "filter": "recent", "cursor": "*",
                  "filter_offtopic_activity": val}
        data = get_json(url, params=params)
        time.sleep(DELAY)
        if not isinstance(data, dict) or data.get("success") != 1:
            log(f"  {label}: FAILED"); continue
        qs = data.get("query_summary") or {}
        ids = {rv.get("recommendationid") for rv in (data.get("reviews") or [])}
        out[val] = {"summary": qs, "ids": ids}
        log(f"  {label}:")
        log(f"    total_reviews : {qs.get('total_reviews')}")
        log(f"    total_positive: {qs.get('total_positive')}")
        log(f"    total_negative: {qs.get('total_negative')}")
        log(f"    score_desc    : {qs.get('review_score_desc')}")
        log(f"    ids on page 1 : {len(ids)}")

    log()
    if 0 in out and 1 in out:
        s0, s1 = out[0]["summary"], out[1]["summary"]
        only_in_0 = out[0]["ids"] - out[1]["ids"]
        same_totals = s0.get("total_reviews") == s1.get("total_reviews")
        log(f"  page-1 reviews present ONLY with =0: {len(only_in_0)}")
        if len(only_in_0) > 0:
            log("  >>> Confirmed: 0 = INCLUDE off-topic, 1 = exclude. Plan is correct.")
        else:
            log("  >>> INCONCLUSIVE on this game (no off-topic reviews in the newest 100).")
            log("      Re-run against a game with an active/recent review bomb.")
        delta = (s0.get("total_reviews") or 0) - (s1.get("total_reviews") or 0)
        if not same_totals:
            log(f"  >>> query_summary DOES move with the flag ({delta:+,} reviews).")
            log("      => Fetch the anchor with the same setting; one consistent number.")
        elif len(only_in_0) == 0:
            # Identical totals prove nothing on a game that has no off-topic reviews at all.
            # Only a game WITH some can distinguish "not coupled" from "nothing to exclude".
            log("  >>> NO EVIDENCE EITHER WAY on this game — it has no off-topic reviews,")
            log("      so identical totals are expected and say nothing about coupling.")
            log("      Judge coupling only on a game where the two totals CAN differ.")
        else:
            log("  >>> query_summary does NOT move despite off-topic reviews existing.")
            log("      => Anchor and sample are measured differently; the bundle header")
            log("         MUST print both numbers, explicitly labelled (plan §5).")
    return {str(k): {"summary": v["summary"], "page1_ids": len(v["ids"])} for k, v in out.items()}


# --------------------------------------------------------------------------- #
# Q4 — are the four flag fields real?
# --------------------------------------------------------------------------- #
def probe_flags(reviews):
    hr("Q4  Flag fields — present, and actually populated?")
    n = len(reviews)
    if not n:
        log("  no reviews collected"); return {}

    def present(key):
        return sum(1 for rv in reviews if key in rv)

    def truthy(key):
        return sum(1 for rv in reviews if rv.get(key))

    rows = [
        ("[EA]   written_during_early_access", "written_during_early_access"),
        ("[free] received_for_free",           "received_for_free"),
        ("[free] steam_purchase (inverted)",   "steam_purchase"),
        ("[deck] primarily_steam_deck",        "primarily_steam_deck"),
    ]
    stats = {}
    log(f"  sample: {n} reviews")
    log(f"  {'field':40s} {'present':>9s} {'true':>8s}")
    for label, key in rows:
        p, t = present(key), truthy(key)
        stats[key] = {"present": p, "true": t}
        log(f"  {label:40s} {p:9d} {t:8d}")

    upd = sum(1 for rv in reviews
              if rv.get("timestamp_updated") and rv.get("timestamp_created")
              and rv["timestamp_updated"] != rv["timestamp_created"])
    stats["edited_later"] = {"present": n, "true": upd}
    log(f"  {'[upd]  timestamp_updated != created':40s} {n:9d} {upd:8d}")

    log()
    for label, key in rows:
        if stats[key]["present"] == 0:
            log(f"  >>> WARNING: {key} absent from every review — drop that flag.")
        elif stats[key]["true"] == 0:
            log(f"  >>> NOTE: {key} present but never true in this sample — flag would")
            log(f"      cost tokens and say nothing here. Check another game before keeping.")
    return stats


# --------------------------------------------------------------------------- #
# Q5 — the real compaction numbers.
# --------------------------------------------------------------------------- #
# Whitelisted tags ONLY. An earlier version matched any [word], which the first live run
# showed was wrong — it counted "[sailing]" and "[russians]" (prose, not markup) as tags. A
# strip pass built on that would delete words out of a review, so the tag list is explicit.
BBCODE = re.compile(
    r"\[/?(?:b|i|u|h[1-3]|url|quote|spoiler|list|olist|\*|strike|code|noparse"
    r"|table|tr|th|td|img|previewyoutube|hr)(?:=[^\]]{0,200})?\]", re.I)
NON_ALNUM = re.compile(r"[^a-zA-Z0-9\s]")
REPEAT_RUN = re.compile(r"(.)\1{19,}")          # 20+ of the same char in a row


def art_ratio(text):
    """Share of non-whitespace characters that are neither letters nor digits. The
    plan's placeholder threshold for 'this is ASCII art, not prose' is 0.4."""
    body = re.sub(r"\s", "", text)
    if not body:
        return 0.0
    return len(NON_ALNUM.findall(body)) / len(body)


def norm_key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def probe_compaction(reviews):
    hr("Q5  Compaction — tuning the thresholds against real data")
    texts = [(rv.get("review") or "") for rv in reviews]
    texts = [t for t in texts if t.strip()]
    n = len(texts)
    if not n:
        log("  no review text collected"); return {}

    lens = sorted(len(t) for t in texts)

    def pct(p):
        return lens[min(len(lens) - 1, int(len(lens) * p / 100))]

    log(f"  reviews with text: {n}")
    log()
    log("  LENGTH (chars)")
    log(f"    min {lens[0]}  p25 {pct(25)}  median {pct(50)}  p75 {pct(75)}"
        f"  p90 {pct(90)}  p99 {pct(99)}  max {lens[-1]}")
    log(f"    mean {statistics.mean(lens):.0f}")
    for cap in (300, 400, 600, 800, 1200):
        over = sum(1 for L in lens if L > cap)
        kept = sum(min(L, cap) for L in lens)
        log(f"    cap {cap:5d}: truncates {over:4d}/{n} ({100*over/n:4.1f}%)"
            f" · total {kept/1000:7.1f}k chars · ~{kept/4/1000:5.1f}k tokens")

    log()
    log("  ASCII-ART RATIO (non-alnum share, plan threshold 0.4 on >80 chars)")
    ratios = [art_ratio(t) for t in texts if len(t) > 80]
    if ratios:
        rs = sorted(ratios)
        log(f"    p50 {rs[len(rs)//2]:.3f}  p90 {rs[int(len(rs)*.9)]:.3f}"
            f"  p99 {rs[int(len(rs)*.99)]:.3f}  max {rs[-1]:.3f}")
        for thr in (0.30, 0.40, 0.50, 0.60):
            hit = sum(1 for r in ratios if r > thr)
            log(f"    threshold {thr:.2f}: would drop {hit:3d}/{len(ratios)}"
                f" ({100*hit/len(ratios):4.1f}%) of >80-char reviews")
    runs = sum(1 for t in texts if REPEAT_RUN.search(t))
    log(f"    reviews with a 20+ repeated-char run: {runs}")

    log()
    log("  BBCODE")
    tags = Counter()
    with_bb = 0
    for t in texts:
        found = BBCODE.findall(t)
        if found:
            with_bb += 1
        tags.update(f.lower() for f in found)
    log(f"    reviews containing BBCode: {with_bb}/{n} ({100*with_bb/n:.1f}%)")
    log(f"    most common tags: {', '.join(f'{t}×{c}' for t, c in tags.most_common(12)) or '(none)'}")

    log()
    log("  DUPLICATES")
    keys = Counter(norm_key(t) for t in texts if len(t) > 20)
    dupe_groups = {k: c for k, c in keys.items() if c > 1}
    dupe_total = sum(c - 1 for c in dupe_groups.values())
    log(f"    near-identical groups: {len(dupe_groups)} · removable copies: {dupe_total}")
    if dupe_groups:
        worst = max(dupe_groups.items(), key=lambda kv: kv[1])
        sample = next(t for t in texts if norm_key(t) == worst[0])
        log(f"    worst offender appears {worst[1]}×: {sample[:110]!r}")

    log()
    log("  SHORT REVIEWS")
    for thr in (4, 10, 20):
        log(f"    under {thr:2d} chars: {sum(1 for L in lens if L < thr)}")

    raw = sum(lens)
    log()
    log(f"  >>> RAW total: {raw/1000:.1f}k chars ≈ ~{raw/4/1000:.1f}k tokens for {n} reviews")
    log(f"  >>> Scaled to 500: ≈ ~{(raw/n)*500/4/1000:.1f}k tokens before compaction")
    return {"n": n, "len_p50": pct(50), "len_p90": pct(90), "len_max": lens[-1],
            "bbcode_share": with_bb / n, "dupe_removable": dupe_total,
            "raw_chars": raw}


# --------------------------------------------------------------------------- #
def probe_language_share(appid):
    """How much of this game is English? Decides how misleading the English-only
    default is for this title (plan §5)."""
    url = BASE.format(appid=appid)
    out = {}
    for lang in ("all", "english"):
        data = get_json(url, params={"json": 1, "language": lang, "purchase_type": "all",
                                     "num_per_page": 0, "filter": "recent", "cursor": "*",
                                     "filter_offtopic_activity": 0})
        time.sleep(DELAY)
        qs = (data or {}).get("query_summary") or {}
        out[lang] = qs.get("total_reviews")
    tot, eng = out.get("all"), out.get("english")
    if tot and eng:
        log(f"  language share: {eng:,} english of {tot:,} total ({100*eng/tot:.1f}%)")
    return out


def main():
    raw = os.environ.get("QTPD_PROBE_APPIDS", "").strip()
    appids = [int(x) for x in raw.split(",") if x.strip()] if raw else DEFAULT_APPIDS

    os.makedirs(OUT_DIR, exist_ok=True)
    report = {"appids": appids, "pages": PAGES, "results": {}}

    log("REVIEW DIGEST — PHASE 0 PROBE")
    log(f"appids: {appids}  ·  pages/game: {PAGES}  ·  delay: {DELAY}s")

    # Q0 per game: the replacement endpoint, raw, before anything else.
    report["getappreviews"] = {str(a): probe_getappreviews(a) for a in appids}

    # Q1 once — it is a property of the endpoint, not of a game.
    report["cors"] = probe_cors(appids[0])
    if not report["cors"].get("reachable"):
        log("\nSteam unreachable from this host. This probe must run on an Actions runner.")
        sys.exit(1)

    all_reviews = []
    for appid in appids:
        hr(f"APPID {appid}")
        probe_language_share(appid)
        reviews, summary = probe_cursor(appid)
        log(f"  query_summary: {json.dumps(summary)}")
        report["results"][str(appid)] = {"summary": summary, "collected": len(reviews)}
        report["results"][str(appid)]["offtopic"] = probe_offtopic(appid)
        all_reviews.extend(reviews)

    report["flags"] = probe_flags(all_reviews)
    report["compaction"] = probe_compaction(all_reviews)

    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    # The raw sample is for eyeballing the prose and tuning §6 by hand. Artifact only —
    # never committed, per the plan's no-prose-in-the-repo rule.
    with open(os.path.join(OUT_DIR, "sample.json"), "w", encoding="utf-8") as f:
        json.dump(all_reviews[:400], f, indent=1, ensure_ascii=False)

    hr("DONE")
    log(f"  wrote {OUT_DIR}/report.json and {OUT_DIR}/sample.json")
    log("  Download them from the run's Artifacts section.")
    log()
    log("  Next: fold the answers into REVIEW_DIGEST_PLAN.md §3 (A0/A1), §5 (anchor")
    log("  coupling), §6 (real thresholds), §7 (drop any dead flag) — then build Phase 1.")


if __name__ == "__main__":
    main()
