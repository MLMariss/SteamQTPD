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
    verbatim from each scraper so this doc never drifts from the real gates.

Timestamp fields used per file (the staleness key):
  games.json     -> per-game `scraped_at`
  prices.json    -> per-row  `scraped_at`
  hltb.json      -> per-row  `fetched_at`   (windows differ: partial/full/blank)
  recent.json    -> per-row  `recent_scraped_at`
  updates_raw/   -> per-game `scraped_at`   (the real updates-layer staleness key)
  playtime_raw/  -> NO per-game stamp; PROXY = newest review `ts` per game.
                    Approximate — a game with no new reviews reads as stale even
                    if freshly walked. Flagged as (approx) in the table.
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
# playtime_refresh.py
PT_COOLDOWN_DAYS = 7
PT_NOUPDATE_COOLDOWN_DAYS = 30
# updates_refresh.py
UPD_COOLDOWN_DAYS = 7
UPD_NOUPDATE_COOLDOWN_DAYS = 45
# games.json / scraper.py (no last_modified API key -> fallback timer)
SCRAPER_REFRESH_DAYS = 7
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
    """playtime_raw: {appid: newest_review_ts} (staleness proxy) + shard bounds."""
    proxy = {}
    newest = oldest = None
    if PT_SHARD_DIR.is_dir():
        for f in sorted(PT_SHARD_DIR.glob("*.json")):
            sh = json.loads(f.read_text(encoding="utf-8"))
            g = sh.get("generated_at")
            if g:
                newest = g if newest is None else max(newest, g)
                oldest = g if oldest is None else min(oldest, g)
            for aid, rec in (sh.get("games") or {}).items():
                mx = 0
                for rv in (rec.get("reviews") or {}).values():
                    t = rv.get("ts") or 0
                    if t > mx:
                        mx = t
                proxy[str(aid)] = mx
    return proxy, newest, oldest


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
    ("genres",                  lambda r: bool(r.get("genres")),        "genre IDs"),
    ("supported languages",     lambda r: bool(r.get("langs")),         "language codes"),
    ("developer",               lambda r: bool(r.get("dev")),           "structured dev name(s)"),
    ("publisher",               lambda r: bool(r.get("pub")),           "structured publisher name(s)"),
    ("release date",            lambda r: r.get("released") is not None, "PICS steam_release_date"),
    ("review score",            lambda r: bool(r.get("rev")),           "Valve bucket + %positive (review-floor bound)"),
    ("full-audio languages",    lambda r: bool(r.get("audio")),         "carriers only"),
    ("Steam Deck compat",       lambda r: bool(r.get("deck")),          "Deck-rated titles only"),
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


def schedule_scraper(games):
    """games.json core: last_modified drives it in Actions; locally we can only see
    the fallback timer (SCRAPER_REFRESH_DAYS), reported as an overdue floor."""
    now = int(time.time())
    b = {"overdue": 0, "fresh": 0, "never": 0}
    for g in games:
        ts = g.get("scraped_at")
        if not ts:
            b["never"] += 1
        elif (now - ts) >= SCRAPER_REFRESH_DAYS * DAY:
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

    pt_proxy, pt_new_shard, pt_old_shard = read_pt_shards()
    raw_cov = len(pt_proxy)
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

    sched_pt = schedule_two_track(
        games, pt_proxy, PT_COOLDOWN_DAYS, PT_NOUPDATE_COOLDOWN_DAYS,
        floor_pred=lambda g: (g.get("review_count") or 0) >= MIN_REVIEWS_FLOOR)

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
             "skipped (below the 10-review floor / null score), not pending work.")
    L.append("")
    L.append("| Metric | Storage | 7d-track | 30d-track | overdue | empty | never |")
    L.append("|---|---|---:|---:|---:|---:|---:|")

    def track_row(name, store, b, overdue_mark=""):
        return (f"| {name} | `{store}` | {b['active_total']:,} | {b['dormant_total']:,} "
                f"| {b['overdue']:,}{overdue_mark} | {b['empty']:,} | {b['never']:,} |")

    L.append(track_row("Recent reviews", "recent.json", sched_recent))
    L.append(track_row("Playtime raw (approx)", "playtime_raw/", sched_pt, overdue_mark=" †"))
    L.append(track_row("Update events", "updates_raw/", sched_upd))
    L.append("")
    L.append(f"**†  Playtime `overdue` is proxy-inflated — read with caution.** "
             f"`playtime_raw/` stores no per-game scrape timestamp, so staleness uses each "
             f"game's **newest review `ts`** as a proxy. A game with no recent reviews reads "
             f"as overdue even if the scraper walked it days ago, so this figure is an "
             f"**upper bound**, not true backlog (contrast Update events, which has a real "
             f"per-game `scraped_at` and shows exact overdue). The `empty` column is the "
             f"{BASE - addressable:,} games below the {MIN_REVIEWS_FLOOR}-review floor "
             f"(correctly skipped, not backlog). An exact figure needs a per-game "
             f"`scraped_at` in the shards — see Future work.")
    L.append("")
    L.append("Files with a single-window (not two-track) refresh rule:")
    L.append("")
    L.append(f"- **`games.json` core** (`scraper.py`): fresh {sched_scraper['fresh']:,} · "
             f"overdue {sched_scraper['overdue']:,} · never {sched_scraper['never']:,}. "
             f"Refresh is driven by Steam's `last_modified` in Actions; the overdue count "
             f"here is the local fallback-timer view (≥{SCRAPER_REFRESH_DAYS}d since "
             f"`scraped_at`).")
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
    L.append("- **Playtime per-game timestamp.** Adding a per-game `scraped_at` to the "
             "`playtime_raw/` shard records would replace the newest-review-`ts` proxy "
             "with an exact staleness signal, removing the (approx) caveat above.")
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
