#!/usr/bin/env python3
"""
stats.py — Steam by the numbers, for stats.html
===============================================
The rest of the pipeline answers "what is this game worth"; this answers "what is Steam doing":
how many games come out, what kind, at what price, how long Early Access lasts, how soon a new
game first goes on sale, how much of it discloses AI content. stats.html (opened from the game
counter in the main page's top bar) charts it by week, month, quarter or year and compares a
period with the one before it, the same one a year earlier, or any other.

Everything is aggregated here, by RELEASE MONTH (UTC) into stats.json and by RELEASE WEEK
(Monday to Sunday, UTC) into stats_weeks.json, so the page never downloads the 67 MB catalog: a
quarter or a year is the sum of its months, and every median the page shows is read off a
histogram or a list kept per bucket, so it stays exact (or exact to a bin) at any grouping. Weeks
don't nest in months, hence the second file; the page fetches it only when Week is picked, and it
starts in the first year with WEEKS_FROM_MIN releases (the page's timeline starts there too).
What each figure counts is the catalog as it is NOW: a game's current list price, tags, review
count and Deck rating, grouped by when it came out. Steam keeps no launch prices for us to read.

ADULT CONTENT (CLAUDE.md "Product rules"). Totals (releases, prices, EA, AI, Deck, reception)
count every game: they are industry figures and put no game in front of anyone. Anything that
RANKS or NAMES — the tag and genre rankings and the biggest-releases list — is built over games
that pass adult=hide, decided exactly as the page decides it (is_adult(), mirroring isAdult() in
index.html): the storefront's adult flag, authoritative wherever PICS has covered the game, and
the tag heuristic only where it hasn't. The flag is the only lock: no tag is pushed down by name.
build() refuses to write a biggest-releases list that contains an adult game.

SALE TIMING. prices.json holds only the current discount, so this job keeps the history it needs
in sales_state.json (not served): for each game it has watched since its release, the day it was
first priced, the last day it was seen, and every sale episode [start day, last day seen on
sale, deepest %] up to and including its first sale after the launch week. Plus one row per UTC
day for the whole catalog: games priced, games on sale, the sum of their discounts — then the same
three for each review-count group (SALE_GROUP_EDGES), so the page can tell the long tail's
discounts from the hits'. A game is
watched only if its first price check came within TRACK_SLACK days of its release, so "days to
first sale" is never measured from a sale we might have missed. Games that left Early Access are
left out: their release date is the 1.0 date, and they were on sale long before it.

Modes
  python stats.py              record the working tree's prices.json into sales_state.json,
                               rebuild stats.json, commit both. When the state is missing more
                               than a day (the job didn't run, or this is the first run since
                               the state was built), it records nothing and dispatches the
                               backfill instead, so days are never recorded out of order.
  python stats.py --backfill   replay prices.json from git history, one point a day (the last
                               commit of each UTC day, streamed from raw.githubusercontent.com,
                               never stored), from the state's last day — or from the first
                               price commit when there is no state — through the latest commit.
                               Day rows grouped by other review edges (or by none) are rebuilt
                               too: the replay starts at the first of them, recording only the
                               day row until it reaches the state's last day. Needs a
                               full-history checkout (commits only).

One writer per file: only this script writes stats.json, stats_weeks.json and sales_state.json
(workflow 4.6).
Stdlib only.
"""

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import gitsync                      # commits this job's own files to main (gitsync.py)

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "stats.json"
WEEKS_FILE = HERE / "stats_weeks.json"
STATE_FILE = HERE / "sales_state.json"
REPO = os.environ.get("GITHUB_REPOSITORY", "MLMariss/SteamQTPD")
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
DAY = 86400

# = ADULT_TAGS in index.html: the fallback for games PICS has not covered.
ADULT_TAGS = {"Nudity", "Sexual Content", "Mature", "NSFW", "Hentai"}
# PICS genres 71-74 (Sexual Content, Nudity, Violent, Gore) describe content, not a kind of game,
# so the genre ranking leaves them out — all four alike, and no game is dropped for carrying one.
GENRE_SKIP = {71, 72, 73, 74}
TOP_TAGS = 100                   # tags kept per month; the page ranks within these
TOP_GAMES = 10                   # biggest releases kept per bucket (enough for any period's top 10)
WEEKS_FROM_MIN = 50              # = START_MIN in stats.html: weeks start in the first year this busy

# Histogram edges. A value v falls in bin i where i = the number of edges <= v (bisect_right).
REV_EDGES = [1, 10, 50, 100, 500, 1000, 5000, 10000, 50000]       # review counts: 10 bins
LEN_EDGES = [1, 2, 3, 5, 8, 12, 20, 35, 60, 100]                     # Length hours: 11 bins
# Price bins by upper bound (a price p falls in the first bin with p <= ub): Steam's price points
# ($4.99, $9.99, $19.99...) each land in their own bin, so a median is a real price point.
PRICE_UB = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 17, 18, 20, 25, 30, 35, 40, 45, 50, 60, 70,
            80, 100]                                                  # + one bin above $100
EA_AGE_EDGES = [365, 730, 1095, 1826]                                 # in EA now: <1y,1-2,2-3,3-5,5+
# Discount depth, for the launch discount and the first sale after it: 10-19% (Steam's floor is
# 10%), 20-29% ... 80-89%, 90% (its ceiling). 9 bins.
DISC_EDGES = [20, 30, 40, 50, 60, 70, 80, 90]
# Review-count groups for the daily "how much of Steam is discounted" row: 0-99, 100-4,999, 5,000+
# (today's count), the main page's Min reviews bands with 100-999 and 1k-4,999 merged so the chart
# keeps to three lines. They part the long tail, which skips most sales, from the games people
# play, which join nearly all of them (3 Oct 2026, a store-wide sale: 50% / 82% / 90% discounted).
# The state records the edges its rows were built with ("groups"); changing them here rebuilds
# every row from git (stale_days), and until then the page gets the catalog numbers only.
SALE_GROUP_EDGES = [100, 5000]
DAY_ROW = 3 * (len(SALE_GROUP_EDGES) + 2)        # catalog + one triple per group
# Steam's review summary, as the store words it, for games with 10+ reviews.
LABELS = ["Overwhelmingly Positive", "Very Positive", "Positive", "Mostly Positive", "Mixed",
          "Mostly Negative", "Negative", "Very Negative", "Overwhelmingly Negative"]

LAUNCH_DAYS = 7                  # a sale starting in the first week is the launch discount
TRACK_SLACK = 3                  # first price check within 3 days of release, or not watched
BACKFILL_GAP_DAYS = 2            # a state this many days behind is filled from git, not live


def log(msg):
    print(msg, flush=True)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Classifiers
# --------------------------------------------------------------------------- #
def is_adult(pics_rec, tags):
    """isAdult() in index.html, for a released game: PICS's adult flag where PICS has covered
    the game (a record with categories, tags or a release state), else the tag heuristic."""
    if pics_rec and (pics_rec.get("cats") or pics_rec.get("tags") or pics_rec.get("state") is not None):
        return bool(pics_rec.get("adult"))
    return any(t in ADULT_TAGS for t in tags or [])


def steam_label(pct, n):
    """Index into LABELS for a game with `n` reviews at `pct`% positive, or None under 10."""
    if pct is None or n is None or n < 10:
        return None
    if pct >= 80:
        if n >= 500 and pct >= 95:
            return 0
        return 1 if n >= 50 else 2
    if pct >= 70:
        return 3
    if pct >= 40:
        return 4
    if pct >= 20:
        return 5
    return 6 if n < 50 else 7 if n < 500 else 8


def price_bin(price):
    """0 for free, else 1 + the index of the first PRICE_UB at or above it (len+1 above $100)."""
    if price <= 0:
        return 0
    for i, ub in enumerate(PRICE_UB):
        if price <= ub:
            return i + 1
    return len(PRICE_UB) + 1


def month_of(ts):
    """Months since year 0 (year * 12 + month - 1), UTC."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year * 12 + d.month - 1


def month_name(m):
    return f"{m // 12:04d}-{m % 12 + 1:02d}"


def week_of(ts):
    """Weeks since the Monday 1969-12-29, UTC: week w runs from day 7w - 3 (a Monday) to 7w + 3.
    1970-01-01 was a Thursday, so a day d falls in week (d + 3) // 7."""
    return (ts // DAY + 3) // 7


UNITS = {"m": month_of, "w": week_of}


# --------------------------------------------------------------------------- #
# Sale episodes (sales_state.json)
# --------------------------------------------------------------------------- #
def empty_state():
    return {"games": {}, "days": {}, "last_day": None, "groups": list(SALE_GROUP_EDGES)}


def load_state():
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if d.get("_format") == "sales_state_v1":
            return {"games": d.get("games") or {}, "days": d.get("days") or {},
                    "last_day": d.get("last_day"),
                    "groups": d.get("groups") if d.get("days") else list(SALE_GROUP_EDGES)}
        log("sales_state.json has another format; starting it again")
    except (OSError, ValueError):
        pass
    return empty_state()


def save_state(state):
    out = {"_format": "sales_state_v1",
           "_note": "Written by stats.py (workflow 4.6); not served. games: {appid: [first priced "
                    "day, last seen day, then per sale: start day, last day seen on sale, deepest "
                    "%]} for games watched since release, up to their first sale after launch week. "
                    "days: {day: [priced, on sale, sum of discount %, then the same three per "
                    "review group, split at the review counts in groups]}. Days are UTC days since 1970.",
           "generated_at": int(time.time()), "last_day": state["last_day"], "groups": state.get("groups"),
           "games": state["games"], "days": state["days"]}
    STATE_FILE.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def first_after_launch(rec, rday):
    """(start day, % off) of the first sale starting after the launch week, or None."""
    for i in range(2, len(rec), 3):
        if rec[i] >= rday + LAUNCH_DAYS:
            return rec[i], rec[i + 2]
    return None


def observe(state, appid, day, pct, rday):
    """Record one price check of one game: `pct` off on UTC day `day`, released on `rday`.
    Checks must arrive in day order per game (an older one is ignored); a game first checked
    more than TRACK_SLACK days after release is never watched, and a watched game stops
    changing once its first sale after the launch week has ended."""
    rec = state["games"].get(appid)
    if rec is None:
        if day - rday > TRACK_SLACK:
            return
        rec = state["games"][appid] = [day, day]
        if pct > 0:
            rec += [day, day, pct]
        return
    if day < rec[1]:
        return
    open_ep = len(rec) > 2 and rec[-2] == rec[1]       # on sale at the last check
    if first_after_launch(rec, rday) and not open_ep:
        return                                          # its first sale is over: done
    if pct > 0:
        if open_ep:
            rec[-2] = day
            rec[-1] = max(rec[-1], pct)
        else:
            rec += [day, day, pct]
    rec[1] = day


def day_row(prices, reviews):
    """One day's catalog row from a prices.json `prices` map: [priced, on sale, sum of %], then
    the same three for each review group (SALE_GROUP_EDGES, by `reviews` {appid: count}); None
    when nothing is priced."""
    row = [0] * DAY_ROW
    for k, v in prices.items():
        if not isinstance(v, dict) or (v.get("price_initial") or 0) <= 0:
            continue
        pct = _int(v.get("discount_pct")) or 0
        for i in (0, 3 + 3 * bisect_right(SALE_GROUP_EDGES, reviews.get(_int(k)) or 0)):
            row[i] += 1
            if pct > 0:
                row[i + 1] += 1
                row[i + 2] += pct
    return row if row[0] else None


def stale_days(state):
    """Every day, oldest first, when the rows were grouped by other review edges than
    SALE_GROUP_EDGES (or by none); else nothing."""
    return [] if state.get("groups") == SALE_GROUP_EDGES else sorted(int(d) for d in state["days"])


def record(state, prices, releases, exclude, reviews, day, games=True):
    """Fold one prices.json (its `prices` map) into the state: every watched or watchable game's
    check, timed by its own scraped_at, and the catalog-wide row for UTC day `day`. With
    games=False only the day row is written (the backfill rebuilding rows behind the state)."""
    if games:
        for k, v in prices.items():
            if not isinstance(v, dict):
                continue
            a = _int(k)
            rday = releases.get(a)
            ts = _int(v.get("scraped_at"))
            if rday is None or a in exclude or not ts:
                continue
            observe(state, str(a), ts // DAY, _int(v.get("discount_pct")) or 0, rday)
        if state["last_day"] is None or day > state["last_day"]:
            state["last_day"] = day
    row = day_row(prices, reviews)
    if row:
        state["days"][str(day)] = row


# --------------------------------------------------------------------------- #
# stats.json
# --------------------------------------------------------------------------- #
def first_busy_year(rel):
    """The first year with WEEKS_FROM_MIN releases, as stats.html picks where its timeline starts."""
    per = {}
    for t in rel.values():
        y = datetime.fromtimestamp(t, timezone.utc).year
        per[y] = per.get(y, 0) + 1
    return min((y for y, c in per.items() if c >= WEEKS_FROM_MIN), default=min(per))


def build(now, games, pics, tags, prices, ea_dates, length, demos, state, tag_rank=TOP_TAGS, unit="m"):
    """The stats.json document (unit "m": per release month) or the stats_weeks.json one (unit
    "w": per release week, from the first busy year on, without the catalog-wide extras). Inputs
    are the parsed maps: games (games.json `games` list), pics (`apps`), tags (`tags`), prices
    (`prices`), ea_dates (`games`), length (`length`), demos (`demos`), and the sales state."""
    of = UNITS[unit]
    cur = of(now)
    rel = {}
    for g in games:
        t = _int(g.get("release_ts"))
        if t and t > 0 and of(t) <= cur:
            rel[g["appid"]] = t
    if not rel:
        return None
    m0 = min(of(t) for t in rel.values())
    if unit == "w":
        m0 = max(m0, week_of(int(datetime(first_busy_year(rel), 1, 1, tzinfo=timezone.utc).timestamp())))
    n = cur - m0 + 1
    d0 = min(rel.values()) // DAY
    zeros = lambda: [0] * n
    A = {k: zeros() for k in ("rel", "rel_c", "pics", "ai", "ai_live", "deck3", "deck2", "deck1",
                              "ctrl_full", "ctrl_part", "vr", "demo", "ea_in", "ea_now", "rated",
                              "rating_sum", "len_n", "priced", "price_sum", "free", "rw", "price_rw")}
    lab = [zeros() for _ in LABELS]
    revb = [zeros() for _ in range(len(REV_EDGES) + 1)]
    lenb = [zeros() for _ in range(len(LEN_EDGES) + 1)]
    priceb = [zeros() for _ in range(len(PRICE_UB) + 2)]
    pricerev = [zeros() for _ in range(len(PRICE_UB) + 2)]     # Steam reviews per price bin
    daily = [0] * (now // DAY - d0 + 1)
    tag_c, genre_c = {}, {}
    top = {}
    ea_left = {_int(a): v for a, v in ea_dates.items() if isinstance(v, dict) and v.get("s") and v.get("v")}
    ea_out = {}
    ea_ages = []

    for g in games:
        a = g["appid"]
        t = rel.get(a)
        if t is None:
            continue
        m = of(t) - m0
        if m < 0:                                       # before the weekly file starts
            continue
        p = pics.get(str(a))
        tg = tags.get(str(a)) or g.get("tags") or []
        adult = is_adult(p, tg)
        A["rel"][m] += 1
        day = t // DAY - d0
        if 0 <= day < len(daily):
            daily[day] += 1

        pr = prices.get(str(a)) or {}
        price = pr.get("price_initial") if pr.get("price_initial") is not None else g.get("price_initial")
        rcw = _int(g.get("review_count")) or 0
        if g.get("is_free"):
            A["free"][m] += 1
            priceb[0][m] += 1
            pricerev[0][m] += rcw
        elif price and price > 0:
            A["priced"][m] += 1
            A["price_sum"][m] += round(price * 100)
            priceb[price_bin(price)][m] += 1
            # Reviews stand in for sales: where the players went, by price, next to where the
            # releases went. Review-weighted: each paid game counts once per Steam review it has,
            # so the mean leans to what players actually bought rather than to what was listed.
            pricerev[price_bin(price)][m] += rcw
            A["rw"][m] += rcw
            A["price_rw"][m] += round(price * 100) * rcw

        if p:
            A["pics"][m] += 1
            if p.get("ai"):
                A["ai"][m] += 1
                if p.get("ai") == 2:
                    A["ai_live"][m] += 1
            cat = (p.get("deck") or {}).get("cat")
            if cat in (1, 2, 3):
                A[f"deck{cat}"][m] += 1
            if p.get("controller") == "full":
                A["ctrl_full"][m] += 1
            elif p.get("controller") == "partial":
                A["ctrl_part"][m] += 1
            cats = set(p.get("cats") or [])
            if p.get("vr_only") or cats & {53, 54}:
                A["vr"][m] += 1
            if p.get("ea") and a not in ea_left:        # PICS can lag a game leaving EA
                A["ea_now"][m] += 1
                A["ea_in"][m] += 1
                ea_ages.append(max(0, (now - t) // DAY))
        if str(a) in demos:
            A["demo"][m] += 1

        rc, rp = _int(g.get("review_count")) or 0, _int(g.get("rating_pct"))
        revb[bisect_right(REV_EDGES, rc)][m] += 1
        li = steam_label(rp, rc)
        if li is not None:
            lab[li][m] += 1
            A["rated"][m] += 1
            A["rating_sum"][m] += rp
        ln = length.get(str(a))
        if isinstance(ln, list) and ln and ln[0]:
            A["len_n"][m] += 1
            lenb[bisect_right(LEN_EDGES, ln[0])][m] += 1

        if not adult:
            A["rel_c"][m] += 1
            for x in tg:
                tag_c.setdefault(x, zeros())[m] += 1
            for gid in (p or {}).get("genres") or []:
                if gid not in GENRE_SKIP:
                    genre_c.setdefault(gid, zeros())[m] += 1
            if rc:
                lst = top.setdefault(m, [])
                lst.append([a, g.get("title") or "", rc, rp, 1 if a in ea_left else 0])
                if len(lst) > TOP_GAMES * 4:
                    lst.sort(key=lambda r: -r[2])
                    del lst[TOP_GAMES:]

    # Early Access: a game that left EA is an EA launch in the month it started and a graduation
    # in the month it left; its EA start is known only where Steam or this job kept it (§7).
    for a, v in ea_left.items():
        s, e = _int(v["s"]), _int(v["v"])
        if not s or not e or e <= s or of(e) > cur:
            continue
        ms, me = of(s) - m0, of(e) - m0
        if 0 <= ms < n:
            A["ea_in"][ms] += 1
        if 0 <= me < n:
            ea_out.setdefault(me, []).append((e - s) // DAY)

    for lst in top.values():
        lst.sort(key=lambda r: -r[2])
        del lst[TOP_GAMES:]
    # The adult lock, enforced at build time like presets.py does for the shelves.
    for lst in top.values():
        for r in lst:
            p = pics.get(str(r[0]))
            if is_adult(p, tags.get(str(r[0])) or []):
                raise SystemExit(f"stats: adult game {r[0]} in a biggest-releases list")

    def ranked(counts, k):
        order = sorted(counts, key=lambda x: -sum(counts[x]))[:k]
        return order, [counts[x] for x in order]

    tag_names, tag_m = ranked(tag_c, tag_rank)
    genre_names = json.loads((HERE / "lookups" / "genres.json").read_text(encoding="utf-8")) \
        if (HERE / "lookups" / "genres.json").exists() else {}
    genre_ids, genre_m = ranked({g: c for g, c in genre_c.items() if str(g) in genre_names}, 30)

    # Sale timing per release bucket: watched games, launch discounts, and the first sale after
    # launch week as an event (days after release) or, not yet, censored at the last check. lh and
    # fh hold how deep the launch discount and that first sale went (DISC_EDGES bins).
    sale = {}
    for k, rec in state["games"].items():
        a = _int(k)
        t = rel.get(a)
        if t is None or a in ea_left:
            continue
        rday = t // DAY
        m = of(t) - m0
        if m < 0:
            continue
        s = sale.setdefault(m, {"n": 0, "l": 0, "lp": 0, "e": {}, "c": {}, "ep": 0,
                                "lh": [0] * (len(DISC_EDGES) + 1), "fh": [0] * (len(DISC_EDGES) + 1)})
        s["n"] += 1
        eps = [rec[i:i + 3] for i in range(2, len(rec), 3)]
        launch = [e for e in eps if e[0] < rday + LAUNCH_DAYS]
        if launch:
            s["l"] += 1
            deep = max(e[2] for e in launch)
            s["lp"] += deep
            s["lh"][bisect_right(DISC_EDGES, deep)] += 1
        first = first_after_launch(rec, rday)
        if first:
            d = str(max(0, first[0] - rday))
            s["e"][d] = s["e"].get(d, 0) + 1
            s["ep"] += first[1]
            s["fh"][bisect_right(DISC_EDGES, first[1])] += 1
        else:
            d = str(max(0, rec[1] - rday))
            s["c"][d] = s["c"].get(d, 0) + 1

    if unit == "w":
        return {
            "_format": "stats_weeks_v1",
            "_note": "Written by stats.py (workflow 4.6); charted by stats.html when Week is picked. "
                     "Per-week arrays start at week w0 and run to the current week; week w runs from "
                     "UTC day 7w-3 (a Monday) to 7w+3. Same fields as stats.json's per-month ones.",
            "generated_at": now, "w0": m0, "weeks": n,
            "m": A, "lab": lab, "revb": revb, "lenb": lenb, "priceb": priceb, "priceb_rev": pricerev,
            "tags": {"names": tag_names, "m": tag_m},
            "genres": {"names": [genre_names[str(g)] for g in genre_ids], "m": genre_m},
            "top": {str(m): v for m, v in sorted(top.items())},
            "ea_out": {str(m): sorted(v) for m, v in sorted(ea_out.items())},
            "sale": {str(m): v for m, v in sorted(sale.items())},
        }
    ea_planned = sum(1 for v in ea_dates.values() if isinstance(v, dict) and v.get("ea")
                     and _int(v.get("v")) and v["v"] > now)
    # Groups built on other edges are not published under the current ones: catalog numbers only
    # until the backfill has rebuilt them.
    grouped = state.get("groups") == SALE_GROUP_EDGES
    sale_days = sorted([int(d), *(row if grouped else row[:3])] for d, row in state["days"].items())
    return {
        "_format": "stats_v1",
        "_note": "Written by stats.py (workflow 4.6); charted by stats.html. Per-month arrays start "
                 "at m0 and run to the current month; a period is the sum of its months.",
        "generated_at": now,
        "m0": month_name(m0), "months": n, "d0": d0,
        "games": len(rel),
        "m": A,
        "labels": LABELS, "lab": lab,
        "rev_edges": REV_EDGES, "revb": revb,
        "len_edges": LEN_EDGES, "lenb": lenb,
        "price_ub": PRICE_UB, "priceb": priceb, "priceb_rev": pricerev,
        "sale_group_edges": SALE_GROUP_EDGES,
        "disc_edges": DISC_EDGES,
        "daily": daily,
        "tags": {"names": tag_names, "m": tag_m},
        "genres": {"names": [genre_names[str(g)] for g in genre_ids], "m": genre_m},
        "_top": ["appid", "title", "reviews", "rating_pct", "left_ea"],
        "top": {str(m): v for m, v in sorted(top.items())},
        "ea_out": {str(m): sorted(v) for m, v in sorted(ea_out.items())},
        "sale": {str(m): v for m, v in sorted(sale.items())},
        "sale_days": sale_days,
        "launch_days": LAUNCH_DAYS,
        "now": {"ea": len(ea_ages), "ea_age_edges": EA_AGE_EDGES,
                "ea_ages": [sum(1 for x in ea_ages if bisect_right(EA_AGE_EDGES, x) == i)
                            for i in range(len(EA_AGE_EDGES) + 1)],
                "ea_age_median": sorted(ea_ages)[len(ea_ages) // 2] if ea_ages else None,
                "ea_planned": ea_planned},
    }


# --------------------------------------------------------------------------- #
# Reading the working tree
# --------------------------------------------------------------------------- #
def read_local(name):
    try:
        return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def inputs():
    """(games list, releases {appid: release day}, exclude set, reviews {appid: count}) from the
    working tree."""
    games = (read_local("games") or {}).get("games") or []
    releases = {g["appid"]: _int(g.get("release_ts")) // DAY for g in games if _int(g.get("release_ts"))}
    ea = (read_local("ea_dates") or {}).get("games") or {}
    exclude = {_int(a) for a, v in ea.items() if isinstance(v, dict) and v.get("s")}
    reviews = {g["appid"]: _int(g.get("review_count")) or 0 for g in games}
    return games, releases, exclude, reviews


def write_stats(now, games, state):
    """Write stats.json and stats_weeks.json; the monthly document, or None."""
    src = ((read_local("pics") or {}).get("apps") or {},
           (read_local("tags") or {}).get("tags") or {},
           (read_local("prices") or {}).get("prices") or {},
           (read_local("ea_dates") or {}).get("games") or {},
           (read_local("length") or {}).get("length") or {},
           (read_local("demos") or {}).get("demos") or {})
    doc = build(now, games, *src, state)
    if doc is None:
        log("games.json missing or empty; stats.json not written")
        return None
    weeks = build(now, games, *src, state, unit="w")
    OUT_FILE.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    WEEKS_FILE.write_text(json.dumps(weeks, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def dispatch_backfill():
    """Ask workflow 4.6 to fill the missing days from git (a GITHUB_TOKEN workflow_dispatch,
    like price_and_sale.py's trigger_deploy). Best effort."""
    if not IN_ACTIONS:
        log("  (local run: run `python stats.py --backfill` to fill the gap)")
        return
    try:
        r = subprocess.run(["gh", "workflow", "run", "stats.yml", "--ref", "main", "-f", "backfill=true"],
                           capture_output=True, text=True, timeout=60)
        log("  backfill requested" if r.returncode == 0
            else f"  backfill dispatch failed: {(r.stderr or r.stdout).strip()}")
    except Exception as e:
        log(f"  backfill dispatch failed: {e}")


def live():
    now = int(time.time())
    today = now // DAY
    games, releases, exclude, reviews = inputs()
    if not games:
        log("games.json missing or unreadable; nothing written")
        return 1
    state = load_state()
    last = state["last_day"]
    if last is None or today - last >= BACKFILL_GAP_DAYS:
        log(f"sales state ends {'nowhere' if last is None else datetime.fromtimestamp(last * DAY, timezone.utc).date()}: "
            "not recording out of order; the backfill fills it from git")
        dispatch_backfill()
    else:
        prices = (read_local("prices") or {}).get("prices") or {}
        record(state, prices, releases, exclude, reviews, today)
        save_state(state)
        stale = stale_days(state)
        if stale:
            log(f"{len(stale)} day rows are grouped by other review edges; the backfill rebuilds them from git")
            dispatch_backfill()
    doc = write_stats(now, games, state)
    if doc is None:
        return 1
    log(f"stats: {doc['games']} games over {doc['months']} months; {len(state['games'])} games "
        f"watched for sales, {len(state['days'])} sale days")
    gitsync.commit(["stats.json", "stats_weeks.json", "sales_state.json"],
                   f"stats: {doc['games']} games, {len(state['games'])} watched for sales")
    return 0


# --------------------------------------------------------------------------- #
# Backfill from git history
# --------------------------------------------------------------------------- #
def history():
    """[(sha, commit_time)] oldest first, first-parent, for the branch checked out."""
    out = subprocess.run(["git", "log", "--first-parent", "--format=%H%x09%ct"],
                         capture_output=True, text=True, check=True).stdout
    rows = [(sha, int(ct)) for sha, ct in (line.split("\t") for line in out.splitlines() if line)]
    return rows[::-1]


def daily_points(commits, start_day, end_day):
    """[(day, sha)]: for each UTC day from start_day to end_day, the last commit made that day
    or before it (so a day without commits repeats the state it inherited)."""
    times = [c[1] for c in commits]
    out = []
    for day in range(start_day, end_day + 1):
        i = bisect_right(times, (day + 1) * DAY - 1)
        if i:
            out.append((day, commits[i - 1][0]))
    return out


def fetch_at(sha, name, tries=4):
    """A file as it was at commit `sha`, streamed and parsed; None if it didn't exist yet."""
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{name}.json"
    err = None
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            err = e
        time.sleep(3 * attempt + random.uniform(0, 2))
    log(f"  {name}.json at {sha[:7]}: gave up ({err})")
    return None


def backfill():
    commits = history()
    if len(commits) < 50:
        log(f"only {len(commits)} commits visible; the backfill needs a full-history checkout")
        return 1
    now = int(time.time())
    games, releases, exclude, reviews = inputs()
    if not games:
        log("games.json missing or unreadable; nothing written")
        return 1
    state = load_state()
    first = commits[0][1] // DAY
    start = state["last_day"] if state["last_day"] is not None else first
    # Rows grouped by other review edges are rebuilt: replay from the first of them, writing only
    # the day row until the state's last day (the games' episodes are already recorded).
    stale = stale_days(state)
    begin = min(start, stale[0]) if stale else start
    points = daily_points(commits, begin, now // DAY)
    log(f"{len(commits)} commits; replaying {len(points)} days of prices.json from "
        f"{datetime.fromtimestamp(begin * DAY, timezone.utc).date()}"
        + (f" ({len(stale)} day rows rebuilt with review groups)" if stale else ""))
    started, seen, rebuilt = time.time(), 0, set()
    for n, (day, sha) in enumerate(points, 1):
        doc = fetch_at(sha, "prices")
        if doc and doc.get("prices"):
            record(state, doc["prices"], releases, exclude, reviews, day, games=day >= start)
            seen += 1
            rebuilt.add(day)
        log(f"  {datetime.fromtimestamp(day * DAY, timezone.utc).date()} {sha[:7]}: "
            f"{len((doc or {}).get('prices') or {})} priced, {len(state['games'])} watched  "
            f"[{n}/{len(points)}, {time.time() - started:.0f}s]")
        if n % 15 == 0:
            save_state(state)                      # a timeout keeps what was replayed
    if stale:
        # Every row has been replayed: the groups are current. A day git couldn't give back keeps
        # its catalog numbers only, rather than groups split at the old edges.
        lost = [d for d in stale if d not in rebuilt]
        for d in lost:
            state["days"][str(d)] = state["days"][str(d)][:3]
        state["groups"] = list(SALE_GROUP_EDGES)
        log(f"review groups rebuilt for {len(stale) - len(lost)} days"
            + (f"; {len(lost)} kept catalog numbers only" if lost else ""))
    save_state(state)
    doc = write_stats(now, games, state)
    if doc is None:
        return 1
    gitsync.commit(["stats.json", "stats_weeks.json", "sales_state.json"],
                   f"stats: backfill {seen} days of prices from git ({len(state['games'])} watched for sales)")
    return 0


if __name__ == "__main__":
    sys.exit(backfill() if "--backfill" in sys.argv[1:] else live())
