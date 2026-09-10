#!/usr/bin/env python3
"""
SteamQHPP — coverage snapshot generator
=======================================
Recomputes data coverage from the live database and writes COVERAGE.md.

Two axes are reported (see ARCHITECTURE §11.5 for the design rationale):

  AXIS 1 — TOTAL COVERAGE (unchanged):
    For each metric, how many of the `games.json` universe have data. Sorted by
    % of catalog descending. Answers "how much of the catalog do we have?"

  AXIS 2 — REFRESH SCHEDULE (new):
    For every file that stores a per-game timestamp, split the *covered* rows by
    how their owning scraper will treat them on the next pass. Answers "is the
    refresh pipeline keeping up, and what's the shape of the queue?"

    Buckets (mirror each scraper's own is_eligible() gate — NOT a flat target):
      * overdue    — already past its cooldown; the scraper should re-grab it now.
                     This is the real backlog signal.
      * 7d-track   — on the ACTIVE lane (last_update_ts within 90d -> short
                     cooldown, 4/7d): refresh lands soon.
      * 30d-track  — on the DORMANT lane (long cooldown, 30/45d): refresh is
                     further out *by design*, not a shortfall.
      * empty      — scraped, but correctly produced no usable row (below the
                     MIN_REVIEWS_FLOOR gate, or a null score). Not missing work.
      * never      — no data yet; still in the fill frontier = true pending backlog.

    Primary split is by TRACK (active vs dormant); `overdue` is called out across
    both. A game "on the 7d track" that is 9 days stale counts as overdue, not as
    due-in-7d. Track totals INCLUDE their overdue members; overdue is a separate
    column so it can be read either way. Cooldown constants below are copied
    verbatim from each scraper so this doc never drifts from the real gates —
    EXCEPT playtime, whose gate is a per-game function rather than a constant and
    is therefore imported live from playtime_refresh.py (copying it is what let
    this file report a flat 7d/30d pair that had not been the real rule in months).

    Playtime is reported on TWO axes, because one visit does not refresh one game
    uniformly: the SURFACE axis (`scraped_at`, when we last looked at all) and the
    DEEP axis (`walk_at`, when we last re-read every stored playtime rather than
    just the newest page). A ceiling game can be surface-fresh and deep-stale.

Timestamp fields used per file (the staleness key):
  games.json     -> per-game `scraped_at`
  prices.json    -> per-row  `scraped_at`
  hltb.json      -> per-row  `fetched_at`   (windows differ: partial/full/blank)
  recent.json    -> per-row  `recent_scraped_at`
  updates_raw/   -> per-game `scraped_at`   (the real updates-layer staleness key)
  playtime_raw/  -> per-game `scraped_at`   (EXACT — set at walk time by
                    playtime_refresh.py; present on 100% of records). Also carries
                    `walk_at`, the last FULL-window re-walk, which is the only thing
                    that unfreezes stored playtimes past the first page — reported
                    separately as the deep-refresh axis.
  tags.json      -> NO timestamp of any kind. Coverage-only; no refresh axis.
                    (Tags rarely change and have no rescrape schedule yet — see
                    the "Future work" note for the planned periodic tag re-check.)
  playtime.json / ratings.json -> derived, positional arrays, no timestamps;
                    staleness inherits from playtime_raw. Coverage-only here.

One writer per file (THIS -> COVERAGE.md), same as shard_health.py -> SHARDS.md.
All reads here are read-only; each file is owned by its own scraper.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The playtime refresh ladder is READ FROM THE SCRAPER, never copied. Hard-copied
# constants are exactly what rotted last time: this file carried a flat 7d/30d pair
# long after playtime_refresh.py moved to a release-age ladder with a popularity
# floor, so COVERAGE.md reported a gate that had not existed for months. Importing
# keeps one source of truth. playtime_refresh degrades gracefully without `requests`
# (see its import block), so this stays a stdlib-only job.
import playtime_refresh as PT

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "COVERAGE.md"
PT_SHARD_DIR = HERE / "playtime_raw"
UPD_SHARD_DIR = HERE / "updates_raw"
PICS_RAW_DIR = HERE / "pics_raw"
PICS_DIR = HERE / "pics"

DAY = 86400
MIN_REVIEWS_FLOOR = 10   # addressable-set gate (playtime + updates layers)
UPDATE_ACTIVE_DAYS = 90  # "actively updated" if last_update_ts within this many days

# --- cooldown constants copied VERBATIM from each scraper's is_eligible() ---
# recent_refresh.py
RECENT_COOLDOWN_DAYS = 4
RECENT_NOUPDATE_COOLDOWN_DAYS = 30
# playtime_refresh.py — NOT a flat pair. The real gate is a per-game function of
# release age, review count and the popularity floor, so it is imported (PT.*) rather
# than copied. These aliases exist only so the rendering code can name the tiers.
PT_AGE_TIERS = PT.AGE_TIER_DAYS                    # [(max_age_days, cooldown_days), ...]
PT_AGE_FALLBACK_DAYS = PT.AGE_TIER_FALLBACK_DAYS   # older than the last tier edge
PT_REWALK_DAYS = PT.REWALK_DAYS                    # deep (full-window) re-walk backstop
PT_CEILING = PT.PER_GAME_CAP                       # a game only deep-walks at the ceiling
# updates_refresh.py
UPD_COOLDOWN_DAYS = 7
UPD_NOUPDATE_COOLDOWN_DAYS = 45
# games.json / scraper.py (no last_modified API key -> fallback timer)
SCRAPER_REFRESH_DAYS = 7
# games.json / scraper.py age-tiered review refresh: (max_age_days, cooldown_days).
# Games older than the last tier keep the last_modified-only rule.
REVIEW_TIERS = [(3, 0.25), (10, 0.5), (30, 1), (60, 2), (90, 3.5), (180, 7), (365, 15)]
# pics_refresh.py (single-window --stale-days gate; default from pics.yml)
PICS_STALE_DAYS = 14
# hltb_refresh.py (different shape: static windows + blank backoff)
HLTB_PARTIAL_DAYS = 14
HLTB_FULL_DAYS = 365
HLTB_BLANK_EAGER_DAYS = 3
HLTB_BLANK_BACKOFF_DAYS = 30
HLTB_BLANK_FREEZE_DAYS = 180

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(*a):
    print(*a, flush=True)


def load(name):
    p = HERE / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def ts_utc(epoch):
    if not epoch:
        return "—"
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M")


def game_map(obj, *candidate_keys):
    if not isinstance(obj, dict):
        return {}
    for k in candidate_keys:
        if isinstance(obj.get(k), dict):
            return obj[k]
    for v in obj.values():
        if isinstance(v, dict) and v and str(next(iter(v))).isdigit():
            return v
    return {}


def is_active(last_update_ts, now):
    """A game is on the ACTIVE (short-cooldown) track if updated recently."""
    return bool(last_update_ts) and (now - last_update_ts) <= UPDATE_ACTIVE_DAYS * DAY


# --------------------------------------------------------------------------- #
# Shard readers
# --------------------------------------------------------------------------- #
def read_pt_shards():
    """playtime_raw: {appid: scraped_at} (the REAL per-game walk stamp) + shard bounds
    + the deep-re-walk axis.

    This used to return the newest review `ts` per game as a "staleness proxy", which
    was simply wrong: every record carries a genuine `scraped_at` written at walk time
    (verified present on 100% of stored records). The proxy measured when reviewers
    last posted, not when we last looked, so a heavily-walked game with no recent
    reviews read as years stale — which is how FRESHNESS.md came to claim a 3,762-day
    max staleness and a 76% backlog that did not exist.

    `deep` is the second axis (see COVERAGE.md → deep refresh). A normal visit to a
    game already holding PER_GAME_CAP reviews only refreshes the newest page — stored
    playtimes below that stay frozen until a FULL re-walk, stamped as `walk_at`. So
    for ceiling games `scraped_at` alone overstates how fresh the medians really are.
      deep = {appid: walk_at} for ceiling games that have an anchor
      deep_unanchored = count of ceiling games with NO anchor (never deep-walked)
    """
    scraped = {}
    deep = {}
    deep_unanchored = 0
    newest = oldest = None
    if PT_SHARD_DIR.is_dir():
        for f in sorted(PT_SHARD_DIR.glob("*.json")):
            sh = json.loads(f.read_text(encoding="utf-8"))
            g = sh.get("generated_at")
            if g:
                newest = g if newest is None else max(newest, g)
                oldest = g if oldest is None else min(oldest, g)
            for aid, rec in (sh.get("games") or {}).items():
                scraped[str(aid)] = rec.get("scraped_at", 0)
                if len(rec.get("reviews") or {}) >= PT_CEILING:
                    w = rec.get("walk_at")
                    if w:
                        deep[str(aid)] = w
                    else:
                        deep_unanchored += 1
    return scraped, newest, oldest, deep, deep_unanchored


def read_upd_shards():
    """updates_raw: {appid: scraped_at} + populated-shard count + bounds."""
    scraped = {}
    newest = oldest = None
    populated = 0
    files = sorted(UPD_SHARD_DIR.glob("*.json")) if UPD_SHARD_DIR.is_dir() else []
    for f in files:
        sh = json.loads(f.read_text(encoding="utf-8"))
        games = sh.get("games") or {}
        if games:
            populated += 1
        g = sh.get("generated_at")
        if g:
            newest = g if newest is None else max(newest, g)
            oldest = g if oldest is None else min(oldest, g)
        for aid, rec in games.items():
            scraped[str(aid)] = rec.get("scraped_at", 0)
    return scraped, populated, len(files), newest, oldest


def _iso_to_epoch(s):
    """Parse pics_raw '_updated' ISO strings to epoch (shard-level stamp)."""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def read_pics_raw_shards():
    """pics_raw: {appid: _ts} (REAL per-game fetch stamp) + populated count + bounds.

    Unlike playtime_raw (which has no per-game stamp and uses a review-ts proxy),
    pics_raw stores a genuine per-game `_ts` set at fetch time, so the refresh axis
    below is EXACT, not a proxy. Shard-level freshness comes from `_updated` (ISO).
    """
    fetched = {}
    newest = oldest = None
    populated = 0
    files = sorted(PICS_RAW_DIR.glob("*.json")) if PICS_RAW_DIR.is_dir() else []
    for f in files:
        sh = json.loads(f.read_text(encoding="utf-8"))
        apps = sh.get("apps") or {}
        if apps:
            populated += 1
        g = _iso_to_epoch(sh.get("_updated"))
        if g:
            newest = g if newest is None else max(newest, g)
            oldest = g if oldest is None else min(oldest, g)
        for aid, rec in apps.items():
            fetched[str(aid)] = rec.get("_ts", 0)
    return fetched, populated, len(files), newest, oldest


# Sub-metrics of the summarized pics/ view. (label, predicate, note). The
# predicate decides "does this game carry the field". Truthy-only fields
# (rev_bomb, ai, fse, eula, orig_released, mc) are by-design sparse — they are
# present only on carrier games, so their % is a real-world incidence rate, not
# a pipeline gap. Structural fields (tags/genres/cats/langs) should sit ~100%.
PICS_SUBMETRICS = [
    ("primary_genre",           lambda r: r.get("pgenre") is not None,  "genre ID (structural)"),
    ("feature categories",      lambda r: bool(r.get("cats")),          "single-player/co-op/cloud/... flags"),
    ("store_tags",              lambda r: bool(r.get("tags")),          "ranked community tag IDs"),
    # Structural, and the one PICS field with a directly VISIBLE failure mode: `art` is
    # the store-header path, and index.html has no other way to reach a modern app's
    # capsule (the appid-derived legacy URLs 404 for anything Valve moved to
    # store_item_assets). A game without it drew an empty frame in the table. This sat
    # untracked while a broken pics.yml push silently dropped three runs' worth of new
    # games, so the gap was only ever found by looking at the site. It belongs here,
    # alongside the other ~100% structural fields, where a dip is an alarm.
    ("store header art",        lambda r: bool(r.get("art")),           "capsule/header path (structural — blank thumbnails without it)"),
    ("genres",                  lambda r: bool(r.get("genres")),        "genre IDs"),
    ("supported languages",     lambda r: bool(r.get("langs")),         "language codes"),
    ("developer",               lambda r: bool(r.get("dev")),           "structured dev name(s)"),
    ("publisher",               lambda r: bool(r.get("pub")),           "structured publisher name(s)"),
    ("release date",            lambda r: r.get("released") is not None, "PICS steam_release_date"),
    ("release state",           lambda r: bool(r.get("state")),         "released/prerelease (structural)"),
    ("review score",            lambda r: bool(r.get("rev")),           "Valve bucket + %positive (review-floor bound)"),
    ("full-audio languages",    lambda r: bool(r.get("audio")),         "carriers only"),
    ("controller support",      lambda r: bool(r.get("controller")),    "full/partial (from cats 28/18)"),
    ("Steam Deck compat",       lambda r: bool(r.get("deck")),          "Deck-rated titles only"),
    ("franchise",               lambda r: bool(r.get("franchise")),     "structured franchise name(s)"),
    ("content descriptors",     lambda r: bool(r.get("content_desc")),  "mature flags (adult gate = code 3 or 4)"),
    ("Early Access (genre-70)", lambda r: 70 in (r.get("genres") or []), "Valve EA signal (not the SteamSpy tag)"),
    ("AI content disclosure",   lambda r: bool(r.get("ai")),            "aicontenttype 1/2 carriers only"),
    ("custom EULA",             lambda r: bool(r.get("eula")),          "carriers only"),
    ("original release date",   lambda r: r.get("orig_released") is not None, "EA->1.0 carriers only"),
    ("metacritic",              lambda r: r.get("mc") is not None,      "carriers only"),
    ("family-share excluded",   lambda r: bool(r.get("fse")),           "excluded titles only"),
    ("review-bomb adjusted",    lambda r: bool(r.get("rev_bomb")),      "bombed titles only"),
]


def read_pics_summary():
    """pics/: total summarized records + per-sub-metric fill counts + AI/deck splits."""
    total = 0
    fills = {label: 0 for label, _, _ in PICS_SUBMETRICS}
    ai_pre = ai_live = 0
    deck_verified = deck_playable = deck_unsupported = 0
    if PICS_DIR.is_dir():
        for f in sorted(PICS_DIR.glob("*.json")):
            sh = json.loads(f.read_text(encoding="utf-8"))
            for aid, r in (sh.get("apps") or {}).items():
                total += 1
                for label, pred, _ in PICS_SUBMETRICS:
                    if pred(r):
                        fills[label] += 1
                ai = r.get("ai")
                if ai == 1:
                    ai_pre += 1
                elif ai == 2:
                    ai_live += 1
                dk = r.get("deck") or {}
                dc = dk.get("cat")
                if dc == 3:
                    deck_verified += 1
                elif dc == 2:
                    deck_playable += 1
                elif dc == 1:
                    deck_unsupported += 1
    return {
        "total": total, "fills": fills,
        "ai_pre": ai_pre, "ai_live": ai_live,
        "deck_verified": deck_verified, "deck_playable": deck_playable,
        "deck_unsupported": deck_unsupported,
    }


# --------------------------------------------------------------------------- #
# Refresh-schedule bucketers (each mirrors its scraper's gate)
# --------------------------------------------------------------------------- #
def schedule_two_track(games, present_ts, short_days, long_days,
                       empty_ids=None, floor_pred=None):
    """Bucketer for two-track cooldown scrapers (recent / playtime / updates).
      present_ts : {appid: last_scrape_ts} for covered games (0/None -> no ts)
      empty_ids  : set of appids that are 'scraped-empty' (walked, null result)
      floor_pred : fn(game) -> True if game is *addressable* (can ever be covered)
    Track totals include their overdue members; `overdue` is reported separately."""
    now = int(time.time())
    empty_ids = empty_ids or set()
    b = {"overdue": 0, "empty": 0, "never": 0,
         "active_total": 0, "dormant_total": 0}
    for g in games:
        aid = str(g.get("appid"))
        if floor_pred and not floor_pred(g):
            b["empty"] += 1                      # below addressable floor: correct skip
            continue
        ts = present_ts.get(aid)
        if not ts:
            if aid in empty_ids:
                b["empty"] += 1                  # walked but produced nothing usable
            else:
                b["never"] += 1                  # true pending frontier
            continue
        active = is_active(g.get("last_update_ts"), now)
        cooldown = (short_days if active else long_days) * DAY
        if active:
            b["active_total"] += 1
        else:
            b["dormant_total"] += 1
        if (now - ts) >= cooldown:
            b["overdue"] += 1
    return b


def _pct(vals, q):
    """q-th percentile (0..1) of a sorted-able list, nearest-rank. [] -> None."""
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def schedule_playtime(games, scraped_ts, floor_pred=None):
    """Bucketer for playtime_raw, against the REAL per-game ladder.

    Unlike the two-track scrapers there is no 'active/dormant' pair here — every game
    gets its own cooldown from PT.cooldown_days(release age, review count), so this
    reports the ladder the way the scraper actually applies it: one row per release-age
    tier, each with its promise, the measured median/p90 staleness, and the share of
    games actually inside their window.

    Returns the standard overdue/empty/never counts plus `tiers` for the breakdown."""
    now = int(time.time())
    b = {"overdue": 0, "empty": 0, "never": 0, "covered": 0}
    tiers = []
    edges = [d for d, _ in PT_AGE_TIERS]
    lo = 0
    for e in edges:                              # 0–7 d, 7–30 d, 30–90 d
        tiers.append({"label": f"{lo}–{e} d", "ages": [], "inside": 0,
                      "promise_min": None, "promise_max": None})
        lo = e
    tiers.append({"label": f"{lo} d+", "ages": [], "inside": 0,      # 90 d+ fallback
                  "promise_min": None, "promise_max": None})

    def tier_for(age_days):
        for i, (edge, _) in enumerate(PT_AGE_TIERS):
            if age_days < edge:
                return tiers[i]
        return tiers[-1]

    for g in games:
        aid = str(g.get("appid"))
        if floor_pred and not floor_pred(g):
            b["empty"] += 1                      # below addressable floor: correct skip
            continue
        ts = scraped_ts.get(aid)
        if not ts:
            b["never"] += 1                      # true pending frontier
            continue
        b["covered"] += 1
        rel = PT._released_ts(g)
        rc = g.get("review_count")
        cd = PT.cooldown_days(rel, g.get("last_update_ts"), rc, now)
        age_days = (now - ts) / DAY
        if age_days >= cd:
            b["overdue"] += 1
        # Bucket by release age so the table mirrors the ladder's primary axis.
        t = tier_for((now - rel) / DAY) if rel else tiers[-1]
        t["ages"].append(age_days)
        if age_days < cd:
            t["inside"] += 1
        t["promise_min"] = cd if t["promise_min"] is None else min(t["promise_min"], cd)
        t["promise_max"] = cd if t["promise_max"] is None else max(t["promise_max"], cd)

    for t in tiers:
        n = len(t["ages"])
        t["n"] = n
        t["median"] = _pct(t["ages"], 0.5)
        t["p90"] = _pct(t["ages"], 0.9)
        t["inside_pct"] = (t["inside"] / n * 100.0) if n else None
        del t["ages"]
    b["tiers"] = tiers
    return b


def schedule_playtime_deep(deep_ts, unanchored):
    """The DEEP (full-window re-walk) axis for ceiling games — the metric that answers
    'are our medians drifting as reviewers keep playing?'.

    A ceiling game's normal visit refreshes only its newest page, so its stored
    playtimes below that are frozen until a full re-walk (`walk_at`). Games with no
    anchor at all have never had the clock started: on cold start playtime_refresh
    stamps the anchor WITHOUT walking, so their deep positions stay frozen and the
    first real re-walk is a further REWALK_DAYS out."""
    now = int(time.time())
    ages = [(now - w) / DAY for w in deep_ts.values() if w]
    over = sum(1 for a in ages if a >= PT_REWALK_DAYS)
    anchored = len(ages)
    return {"anchored": anchored, "unanchored": unanchored,
            "ceiling": anchored + unanchored,
            "median": _pct(ages, 0.5), "p90": _pct(ages, 0.9),
            "max": max(ages) if ages else None,
            "overdue": over,
            "overdue_pct": (over / anchored * 100.0) if anchored else None}


def schedule_scraper(games):
    """games.json core. Two populations:
      * within REVIEW_TIERS (age since release <= the last tier): a real per-game
        cooldown that widens with age, so fresh/overdue are exact here.
      * older than the last tier: refresh is driven by Steam's `last_modified`, which
        we can't see locally, so those are reported separately as `lm_only` rather
        than being scored against a cooldown they don't have.
    """
    now = int(time.time())
    b = {"overdue": 0, "fresh": 0, "never": 0, "lm_only": 0}
    for g in games:
        ts = g.get("scraped_at")
        if not ts:
            b["never"] += 1
            continue
        rt = g.get("release_ts")
        cooldown = None
        if rt and rt <= now:
            age_days = (now - rt) / DAY
            for max_age, cooldown_days in REVIEW_TIERS:
                if age_days <= max_age:
                    cooldown = cooldown_days * DAY
                    break
        if cooldown is None:
            b["lm_only"] += 1
        elif (now - ts) >= cooldown:
            b["overdue"] += 1
        else:
            b["fresh"] += 1
    return b


def schedule_pics(fetched):
    """pics_raw: single-window --stale-days gate (default 14d), keyed on the REAL
    per-game `_ts` fetch stamp. `never` = catalog games with no PICS record yet.
    No two-track split: pics_refresh.py uses one flat stale window for all games."""
    now = int(time.time())
    b = {"fresh": 0, "overdue": 0, "never": 0}
    have = set(fetched)
    for aid, ts in fetched.items():
        if not ts:
            b["never"] += 1
        elif (now - ts) >= PICS_STALE_DAYS * DAY:
            b["overdue"] += 1
        else:
            b["fresh"] += 1
    return b, have


def schedule_hltb(hltb):
    """hltb.json: static windows (partial 14d / full 365d) + blank backoff."""
    now = int(time.time())
    b = {"overdue": 0, "fresh": 0, "blank_frozen": 0, "blank_active": 0}
    for v in hltb.values():
        if not isinstance(v, dict):
            continue
        age = now - v.get("fetched_at", 0)
        raw = v.get("raw") or {}
        reals = sum(1 for k in ("main", "extra", "complete") if raw.get(k) is not None)
        if reals == 0:
            attempts = v.get("attempts", 0)
            win = (HLTB_BLANK_EAGER_DAYS if attempts < 3
                   else HLTB_BLANK_BACKOFF_DAYS if attempts < 6
                   else HLTB_BLANK_FREEZE_DAYS)
            if age >= win * DAY:
                b["overdue"] += 1
            elif win == HLTB_BLANK_FREEZE_DAYS:
                b["blank_frozen"] += 1
            else:
                b["blank_active"] += 1
        else:
            win = HLTB_PARTIAL_DAYS if reals < 3 else HLTB_FULL_DAYS
            if age >= win * DAY:
                b["overdue"] += 1
            else:
                b["fresh"] += 1
    return b


# --------------------------------------------------------------------------- #
def main():
    games_doc = load("games.json")
    if not games_doc:
        log("games.json missing; cannot compute coverage.")
        return 0
    games = games_doc.get("games") or []
    BASE = len(games)
    if BASE == 0:
        log("games.json has no games; nothing to do.")
        return 0

    # ---- AXIS 1: total coverage ----
    rev_cov    = sum(1 for x in games if (x.get("review_count") or 0) > 0)
    rating_cov = sum(1 for x in games if (x.get("rating_pct") or 0) > 0)
    rel_cov    = sum(1 for x in games if x.get("release_date"))
    is_free    = sum(1 for x in games if x.get("is_free") is True)
    nonfree    = BASE - is_free
    addressable = sum(1 for x in games if (x.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)

    prices = game_map(load("prices.json"), "prices")
    price_cov = len(prices)
    on_sale = sum(1 for v in prices.values() if (v.get("discount_pct") or 0) > 0)

    hltb = game_map(load("hltb.json"), "hltb")
    hltb_total = len(hltb)
    hltb_est = sum(1 for v in hltb.values() if isinstance(v, dict) and v.get("est"))
    hltb_real = hltb_total - hltb_est

    recent = game_map(load("recent.json"), "recent")
    recent_cov = len(recent)

    tags = game_map(load("tags.json"), "tags")
    tags_cov = sum(1 for v in tags.values() if v)

    pt = game_map(load("playtime.json"), "playtime")
    pt_cov = len(pt)

    rt = game_map(load("ratings.json"), "playtime_ratings")
    rt_cov = len(rt)

    updates = game_map(load("updates.json"), "games")
    upd_cov = len(updates)

    pt_scraped, pt_new_shard, pt_old_shard, pt_deep, pt_deep_unanchored = read_pt_shards()
    raw_cov = len(pt_scraped)
    upd_scraped, upd_populated, upd_total_shards, upd_new_shard, upd_old_shard = read_upd_shards()
    upd_raw_cov = len(upd_scraped)

    pics_fetched, pics_populated, pics_total_shards, pics_new_shard, pics_old_shard = read_pics_raw_shards()
    pics_raw_cov = len(pics_fetched)
    pics_sum = read_pics_summary()
    pics_cov = pics_sum["total"]

    def pct(n):
        return n / BASE * 100.0

    rows = [
        ("Recent reviews",           "recent.json",   recent_cov, BASE - recent_cov),
        ("HLTB (total)",             "hltb.json",     hltb_total, BASE - hltb_total),
        ("Release date",             "games.json",    rel_cov,    BASE - rel_cov),
        ("Review count",             "games.json",    rev_cov,    BASE - rev_cov),
        ("Rating %",                 "games.json",    rating_cov, BASE - rating_cov),
        ("HLTB (real)",              "hltb.json",     hltb_real,  None),
        ("Price / Sales",            "prices.json",   price_cov,  None),
        ("Tags (non-empty)",         "tags.json",     tags_cov,   BASE - tags_cov),
        ("Playtime raw (shards)",    "playtime_raw/", raw_cov,    BASE - raw_cov),
        ("Playtime (summarized)",    "playtime.json", pt_cov,     BASE - pt_cov),
        ("Playtime-weighted rating", "ratings.json",  rt_cov,     BASE - rt_cov),
        ("Update events (summ.)",    "updates.json",  upd_cov,    BASE - upd_cov),
        ("Update events (raw)",      "updates_raw/",  upd_raw_cov, BASE - upd_raw_cov),
        ("PICS metadata (raw)",      "pics_raw/",     pics_raw_cov, BASE - pics_raw_cov),
        ("PICS metadata (summ.)",    "pics/",         pics_cov,   BASE - pics_cov),
        ("HLTB (estimated)",         "hltb.json",     hltb_est,   None),
    ]
    rows.sort(key=lambda r: r[2], reverse=True)

    # ---- AXIS 2: refresh schedule ----
    recent_ts = {aid: v.get("recent_scraped_at", 0) for aid, v in recent.items()}
    recent_empty_ids = {aid for aid, v in recent.items() if v.get("recent_pct") is None}
    sched_recent = schedule_two_track(
        games, recent_ts, RECENT_COOLDOWN_DAYS, RECENT_NOUPDATE_COOLDOWN_DAYS,
        empty_ids=recent_empty_ids)

    sched_pt = schedule_playtime(
        games, pt_scraped,
        floor_pred=lambda g: (g.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)
    sched_pt_deep = schedule_playtime_deep(pt_deep, pt_deep_unanchored)

    sched_upd = schedule_two_track(
        games, upd_scraped, UPD_COOLDOWN_DAYS, UPD_NOUPDATE_COOLDOWN_DAYS,
        floor_pred=lambda g: (g.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)

    sched_scraper = schedule_scraper(games)
    sched_hltb = schedule_hltb(hltb)
    sched_pics, pics_have = schedule_pics(pics_fetched)
    pics_never = BASE - len(pics_have)

    now = ts_utc(int(time.time()))

    # ---- build markdown ----
    L = []
    L.append("# SteamQHPP — Data Coverage Snapshot")
    L.append("")
    L.append("> Generated by `coverage.py` after each scrape. Do not edit by hand — "
             "changes will be overwritten on the next run. Design rationale for both "
             "axes is in ARCHITECTURE §11.5.")
    L.append("")
    L.append(f"**Snapshot generated (UTC):** {now}")
    L.append(f"**Base universe (`games.json`):** {BASE:,} games")
    L.append("")
    L.append("Per-file generation timestamps at snapshot time:")
    L.append("")
    L.append("| File | Generated (UTC) |")
    L.append("|---|---|")
    fresh = [
        ("games.json",    games_doc.get("generated_at")),
        ("prices.json",   (load("prices.json")   or {}).get("generated_at")),
        ("hltb.json",     (load("hltb.json")     or {}).get("generated_at")),
        ("recent.json",   (load("recent.json")   or {}).get("generated_at")),
        ("playtime.json", (load("playtime.json") or {}).get("generated_at")),
        ("ratings.json",  (load("ratings.json")  or {}).get("generated_at")),
        ("tags.json",     (load("tags.json")     or {}).get("generated_at")),
        ("updates.json",  (load("updates.json")  or {}).get("generated_at")),
    ]
    for name, g in fresh:
        L.append(f"| `{name}` | {ts_utc(g)} |")
    pt_shard_line = (f"newest {ts_utc(pt_new_shard)} · oldest {ts_utc(pt_old_shard)}"
                     if pt_new_shard else "—")
    L.append(f"| `playtime_raw/` (shards) | {pt_shard_line} |")
    upd_shard_line = (f"newest {ts_utc(upd_new_shard)} · oldest {ts_utc(upd_old_shard)}"
                      if upd_new_shard else "—")
    L.append(f"| `updates_raw/` ({upd_populated}/{upd_total_shards} populated) | {upd_shard_line} |")
    pics_shard_line = (f"newest {ts_utc(pics_new_shard)} · oldest {ts_utc(pics_old_shard)}"
                       if pics_new_shard else "—")
    L.append(f"| `pics_raw/` ({pics_populated}/{pics_total_shards} populated) | {pics_shard_line} |")
    L.append("")
    L.append("---")
    L.append("")

    # ---- AXIS 1 table ----
    L.append("## Axis 1 — Coverage by metric (sorted by % of catalog, descending)")
    L.append("")
    L.append("How much of the catalog we hold for each metric. \"Have we got it?\"")
    L.append("")
    L.append("| Metric | Storage file | Covered | % of catalog | Missing |")
    L.append("|---|---|---:|---:|---:|")
    for metric, store, cov, miss in rows:
        misstr = "—" if miss is None else f"{miss:,}"
        L.append(f"| {metric} | `{store}` | {cov:,} | {pct(cov):.1f}% | {misstr} |")
    L.append("")
    L.append("Integrity: every storage file is effectively a clean subset of "
             "`games.json` (a handful of orphan keys can appear as timing artifacts "
             "between scraper commits).")
    L.append("")
    L.append("---")
    L.append("")

    # ---- AXIS 2 table ----
    L.append("## Axis 2 — Refresh schedule (is the pipeline keeping up?)")
    L.append("")
    L.append("Covered rows split by how each row's **own scraper** will treat it on the "
             "next pass — NOT against a flat target. The **7d (active)** lane is games "
             "updated within 90d (short cooldown); the **30d (dormant)** lane is everyone "
             "else (long cooldown, refreshed rarely *by design*). Track totals **include** "
             "their overdue members. **overdue** = already past its lane's cooldown = real "
             "backlog. **never** = no data yet = fill frontier. **empty** = correctly "
             f"skipped (below the {MIN_REVIEWS_FLOOR}-review floor / null score), not "
             "pending work.")
    L.append("")
    L.append("| Metric | Storage | 7d-track | 30d-track | overdue | empty | never |")
    L.append("|---|---|---:|---:|---:|---:|---:|")

    def track_row(name, store, b, overdue_mark=""):
        return (f"| {name} | `{store}` | {b['active_total']:,} | {b['dormant_total']:,} "
                f"| {b['overdue']:,}{overdue_mark} | {b['empty']:,} | {b['never']:,} |")

    L.append(track_row("Recent reviews", "recent.json", sched_recent))
    L.append(track_row("Update events", "updates_raw/", sched_upd))
    L.append("")
    L.append(f"Playtime is **not** a two-track row — every game gets its own cooldown from "
             f"`playtime_refresh.cooldown_days()` — so it gets its own table below. The "
             f"`empty` column above is the {BASE - addressable:,} games below the "
             f"{MIN_REVIEWS_FLOOR}-review floor (correctly skipped, not backlog).")
    L.append("")

    # ---- AXIS 2b: playtime, against its real per-game ladder ----
    L.append("### Playtime raw — surface refresh (`playtime_raw/`)")
    L.append("")
    L.append(f"Staleness is the **real per-game `scraped_at`**, measured against the real "
             f"ladder (release-age tiers "
             + " · ".join(f"<{d}d→{c}d" for d, c in PT_AGE_TIERS)
             + f" · else {PT_AGE_FALLBACK_DAYS}d; halved above "
             f"{PT.HOT_REVIEWS_BOOST:,} reviews, then floored by the popularity tiers "
             + "/".join(f">{e}→{d}d" for e, d in PT.POPULAR_FLOOR_TIERS)
             + f"). Because the cooldown varies per game, **promise** is the range actually "
             f"applied within each release-age tier. `covered` {sched_pt['covered']:,} · "
             f"`overdue` {sched_pt['overdue']:,} · `never` {sched_pt['never']:,} · "
             f"`empty` {sched_pt['empty']:,}.")
    L.append("")
    L.append("| Release age | n | Promise | Median stale | p90 | Inside window |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for t in sched_pt["tiers"]:
        if not t["n"]:
            continue
        pmin, pmax = t["promise_min"], t["promise_max"]
        promise = (f"{pmin:g} d" if pmin == pmax else f"{pmin:g}–{pmax:g} d")
        L.append(f"| {t['label']} | {t['n']:,} | {promise} | "
                 f"{t['median']:.1f} d | {t['p90']:.1f} d | {t['inside_pct']:.1f}% |")
    L.append("")
    L.append("**A surface refresh is not a full one.** For a game already holding "
             f"{PT_CEILING:,} reviews a normal visit stops after page 1, so only its newest "
             "~100 playtimes are re-read; positions below that stay frozen at whatever they "
             "were when first captured. Small games (below their rung) are walked to the end "
             "of Steam's list every visit, so for them this table *is* the whole story.")
    L.append("")

    # ---- AXIS 2c: the deep re-walk clock (ceiling games only) ----
    d = sched_pt_deep
    L.append("### Playtime raw — deep re-walk (drift control)")
    L.append("")
    if d["ceiling"]:
        med = f"{d['median']:.1f} d" if d["median"] is not None else "—"
        p90 = f"{d['p90']:.1f} d" if d["p90"] is not None else "—"
        mx = f"{d['max']:.1f} d" if d["max"] is not None else "—"
        opct = f"{d['overdue_pct']:.1f}%" if d["overdue_pct"] is not None else "—"
        unanch_pct = d["unanchored"] / d["ceiling"] * 100.0
        L.append(f"The only axis that unfreezes a ceiling game's older playtimes is a FULL "
                 f"window re-walk, stamped `walk_at` and promised every "
                 f"{PT_REWALK_DAYS} d. This is the number that answers *are our medians "
                 f"drifting as reviewers keep playing*.")
        L.append("")
        L.append("| Metric | Value |")
        L.append("|---|---:|")
        L.append(f"| Games at the {PT_CEILING:,}-review ceiling | {d['ceiling']:,} |")
        L.append(f"| — anchored (deep clock running) | {d['anchored']:,} |")
        L.append(f"| — **no `walk_at` anchor** (never deep-walked) | "
                 f"**{d['unanchored']:,}** ({unanch_pct:.1f}%) |")
        L.append(f"| Anchored: median since last full walk | {med} |")
        L.append(f"| Anchored: p90 | {p90} |")
        L.append(f"| Anchored: max | {mx} |")
        L.append(f"| Anchored: past the {PT_REWALK_DAYS} d promise | "
                 f"{d['overdue']:,} ({opct}) |")
        L.append("")
        if d["unanchored"]:
            L.append(f"**{d['unanchored']:,} ceiling games have never had the deep clock "
                     f"started.** On cold start the scraper stamps the anchor *without* "
                     f"walking (deliberate — it staggers the population instead of firing "
                     f"every ceiling game at once), so those records' deep positions have "
                     f"been frozen since the ladder filled them. `DEEP_BACKFILL_PER_RUN` in "
                     f"`playtime_refresh.py` drains this set at a fixed rate per run.")
    else:
        L.append(f"No games at the {PT_CEILING:,}-review ceiling yet — nothing can be "
                 f"deep-stale.")
    L.append("")
    L.append("Files with a single-window (not two-track) refresh rule:")
    L.append("")
    L.append(f"- **`games.json` core** (`scraper.py`): fresh {sched_scraper['fresh']:,} · "
             f"overdue {sched_scraper['overdue']:,} · never {sched_scraper['never']:,} · "
             f"last-modified-only {sched_scraper['lm_only']:,}. Games within "
             f"{REVIEW_TIERS[-1][0]}d of release are on the age-tiered review refresh "
             f"("
             + ", ".join(f"≤{d}d→{c}d" for d, c in REVIEW_TIERS)
             + f"), so fresh/overdue are exact for them. Older games refresh on Steam's "
             f"`last_modified` (invisible locally) plus the PICS review-drift trigger, "
             f"and are counted separately rather than scored against a cooldown they "
             f"don't have.")
    L.append(f"- **`hltb.json`** (`hltb_refresh.py`): fresh {sched_hltb['fresh']:,} · "
             f"overdue {sched_hltb['overdue']:,} · blank-frozen "
             f"{sched_hltb['blank_frozen']:,} · blank-active {sched_hltb['blank_active']:,}. "
             f"Windows: partial {HLTB_PARTIAL_DAYS}d, full {HLTB_FULL_DAYS}d; blanks back "
             f"off {HLTB_BLANK_EAGER_DAYS}→{HLTB_BLANK_BACKOFF_DAYS}→"
             f"{HLTB_BLANK_FREEZE_DAYS}d by attempt count.")
    L.append(f"- **`prices.json`** (`price_and_sale.py`): no cooldown gate — the whole "
             f"non-free base is re-batched every 3h, so there is no meaningful staleness "
             f"backlog to bucket.")
    L.append(f"- **`tags.json`** (`tags_refresh.py`): **no timestamp stored and no "
             f"rescrape schedule** — coverage-only. See Future work below.")
    L.append(f"- **`playtime.json` / `ratings.json`**: derived from `playtime_raw/` on "
             f"every raw pass; staleness inherits from the raw shard row above.")
    L.append(f"- **`pics_raw/`** (`pics_refresh.py`): fresh {sched_pics['fresh']:,} · "
             f"overdue {sched_pics['overdue']:,} · never {pics_never:,}. Single flat "
             f"`--stale-days {PICS_STALE_DAYS}` window (daily cron), and — unlike playtime — "
             f"keyed on a **real per-game `_ts` fetch stamp**, so overdue here is exact, not "
             f"a proxy. `never` = catalog games not yet in `pics_raw/` (the fill frontier). "
             f"`pics/` is derived from `pics_raw/` by `pics_summarize.py` on every pass, so "
             f"its staleness inherits from this row.")
    L.append("")
    L.append("---")
    L.append("")

    # ---- PICS sub-metric coverage ----
    if pics_cov:
        L.append("## PICS metadata — sub-metric coverage")
        L.append("")
        L.append(f"Fill rates **within the {pics_cov:,} summarized `pics/` records** "
                 f"(not against the full catalog). Structural fields (tags, genres, "
                 f"categories, languages, dev/pub) sit near 100%; the sparse rows below "
                 f"them are **by-design carrier-only** fields — their % is a real-world "
                 f"incidence rate (how many games actually carry an AI disclosure, a "
                 f"Deck rating, a review-bomb adjustment, …), **not** a pipeline gap. "
                 f"These figures are live over the full library — they supersede the "
                 f"120-game probe sample in `PICS_METADATA_PIPELINE.md §2`.")
        L.append("")
        L.append("| Sub-metric | Covered | % of `pics/` | Note |")
        L.append("|---|---:|---:|---|")
        pf = pics_sum["fills"]
        for label, _, note in PICS_SUBMETRICS:
            n = pf[label]
            p = (n / pics_cov * 100) if pics_cov else 0
            L.append(f"| {label} | {n:,} | {p:.1f}% | {note} |")
        L.append("")
        ai_pre, ai_live = pics_sum["ai_pre"], pics_sum["ai_live"]
        ai_tot = ai_pre + ai_live
        dv, dp, du = (pics_sum["deck_verified"], pics_sum["deck_playable"],
                      pics_sum["deck_unsupported"])
        L.append(f"**AI content disclosure.** {ai_tot:,} games carry an `aicontenttype` "
                 f"flag ({pct(ai_tot):.1f}% of catalog): **{ai_pre:,} pre-generated** "
                 f"(shipped assets made with AI) and **{ai_live:,} live-generated** "
                 f"(runtime generation). The other ~{100 - (ai_tot/pics_cov*100):.0f}% "
                 f"of `pics/` records carry no disclosure flag. PICS gives the typed "
                 f"flag + category only; the free-text blurb lives on the store page.")
        L.append("")
        L.append(f"**Steam Deck.** Of the {pf['Steam Deck compat']:,} Deck-rated titles: "
                 f"**{dv:,} Verified**, **{dp:,} Playable**, **{du:,} Unsupported**. The "
                 f"long tail of the catalog is simply unrated by Valve (no Deck category), "
                 f"which is why Deck coverage is ~{pf['Steam Deck compat']/pics_cov*100:.0f}% "
                 f"of `pics/`, not the ~97% the AAA-heavy probe sample suggested.")
        L.append("")
        L.append("---")
        L.append("")

    # ---- Notes ----
    L.append("## Notes")
    L.append("")
    if price_cov:
        sale_pct = on_sale / price_cov * 100
        L.append(f"**Prices.** `prices.json` holds **{price_cov:,} rows "
                 f"({pct(price_cov):.1f}% of catalog)**, tracking the non-free base "
                 f"(**{nonfree:,}** games after removing {is_free:,} `is_free`). "
                 f"**{on_sale:,} of {price_cov:,} priced titles ({sale_pct:.1f}%) "
                 f"are currently discounted**; sale end-dates come from the "
                 f"`IStoreBrowseService/GetItems` pass.")
        L.append("")
    addr_pct_base = addressable / BASE * 100
    raw_of_addr = (raw_cov / addressable * 100) if addressable else 0
    below = BASE - addressable
    L.append(f"**Playtime backfill.** Raw playtime covers **{raw_cov:,} games "
             f"({pct(raw_cov):.1f}% of catalog)**. The honest denominator is the "
             f"addressable set after the `MIN_REVIEWS_FLOOR = {MIN_REVIEWS_FLOOR}` gate "
             f"— **{addressable:,} games ({addr_pct_base:.1f}% of catalog)** — against "
             f"which raw is **{raw_of_addr:.1f}% of addressable**. The other "
             f"**{below:,} games are below the floor and correctly skipped** (too few "
             f"reviews to produce a sentiment-split median).")
    L.append("")
    lag = raw_cov - pt_cov
    L.append(f"**Summarizers run on every raw pass.** Raw {raw_cov:,} · summarized "
             f"{pt_cov:,} · rated {rt_cov:,} (current gap {lag:,}). "
             f"`playtime_summarize.py` and `ratings_summarize.py` are chained into "
             f"`playtime-raw.yml`, so the small frontend files refresh right after the "
             f"shards do (8×/day). Any residual gap is just within-run ordering between "
             f"the shard commit and the summarize steps, not a stalled pipeline.")
    L.append("")
    L.append(f"**Update events.** `updates.json` (event_type-based patch history) covers "
             f"**{upd_cov:,} games ({pct(upd_cov):.1f}%)**, built from **{upd_populated} of "
             f"{upd_total_shards} `updates_raw/` shards populated**. This layer feeds the "
             f"frontend's **Updated column cadence badge** (`N · 90d` / `N · 1y`, from the "
             f"summed 90d/365d `counts`) and backfills a null News-API `last_update_ts`. "
             f"It is gated by the same {MIN_REVIEWS_FLOOR}-review floor as playtime. As the "
             f"remaining shards populate this coverage rises; the precedence flip (event "
             f"layer becomes primary over News-API) is gated on that coverage — see "
             f"ARCHITECTURE §9.5 / §3.1.")
    L.append("")
    L.append(f"**HLTB estimates refresh live.** **{hltb_est:,} of {hltb_total:,} "
             f"`hltb.json` entries carry the `est` flag** ({pct(hltb_est):.1f}%), "
             f"leaving **{hltb_real:,} reals ({pct(hltb_real):.1f}%)**. `hltb_estimate.py` "
             f"is a shared helper imported by `hltb_refresh.py` (as `HE`) and called in "
             f"the fill loop, so estimates recompute from live median ratios every 2h as "
             f"new reals land — not a stale one-off.")
    L.append("")
    L.append(f"**Rating % / review count.** Rating % is present for **{rating_cov:,} "
             f"games ({pct(rating_cov):.1f}%)**; review count for **{rev_cov:,} "
             f"({pct(rev_cov):.1f}%)**. The gap is titles with too few reviews to carry "
             f"a meaningful score.")
    L.append("")
    L.append(f"**Tags** are non-empty for **{tags_cov:,} games ({pct(tags_cov):.1f}%)**. "
             f"The remainder are untagged on SteamSpy (typically very-low-review or "
             f"unreleased titles).")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Future work")
    L.append("")
    L.append("- **Tag refresh schedule.** `tags.json` currently has no per-entry "
             "timestamp and no rescrape cadence — tags are fetched once and left. Tags do "
             "drift (Steam user-tag votes shift, new tags get added), so a light periodic "
             "re-check (e.g. weekly/monthly, oldest-first, small budget) is worth adding. "
             "That needs a per-entry `scraped_at` in `tags.json` first, after which tags "
             "can join Axis 2 as a proper row here.")
    L.append("- **Uniform-refresh review.** Once the fill frontier drains (near-100% "
             "coverage), revisit whether the dormant 30–45d cooldowns should tighten "
             "toward a flatter, faster whole-catalog refresh. Today's split (active 4–7d / "
             "dormant 30–45d) deliberately front-loads budget onto games whose data "
             "actually moves; a uniform target only makes sense once no backlog is "
             "competing for that budget.")
    L.append("")

    OUT_FILE.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"Wrote COVERAGE.md — base {BASE:,}, {len(rows)} metrics · "
        f"updates {upd_cov:,} ({upd_populated}/{upd_total_shards} shards) · "
        f"recent overdue {sched_recent['overdue']:,}.")
    git_commit()
    return 0


def git_commit():
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "COVERAGE.md"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            "coverage: refresh COVERAGE.md"], check=False)
            import random
            for attempt in range(1, 6):
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log("  committed COVERAGE.md")
                    return
                time.sleep(2 * attempt + random.uniform(0, 2))
            log("  ERROR: could not push COVERAGE.md after 5 attempts")
        else:
            log("  COVERAGE.md unchanged; nothing to commit.")
    except Exception as e:
        log(f"  git commit failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
