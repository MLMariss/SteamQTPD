#!/usr/bin/env python3
"""
Steam QHPP — sale end-date refresher
====================================
A SEPARATE, independent job from scraper.py. It keeps each on-sale game's discount
*end date* fresh, so the frontend can count it down and (when it hits zero) collapse
the sale back to base price offline — no scraping needed for that.

Why it's its own script + its own file (same rule as recent_refresh.py):
  * scraper.py owns games.json (catalog, price, discount_pct, rating, tags, update_ts).
  * recent_refresh.py owns recent.json (30-day review scores).
  * THIS owns sales.json keyed by appid -> {discount_end, discount_pct, scraped_at}.
    Three Actions each writing a DIFFERENT file never collide on push (a git
    pull --rebase before push always replays cleanly). scraper.py no longer writes
    discount_end at all — sales.json is the single source of truth for it.

Source of the end date — IStoreBrowseService/GetItems/v1:
  Steam's storefront/SteamDB read the sale expiry from this endpoint's
  best_purchase_option.active_discounts[].discount_end_date (a unix timestamp). It
  works for ANY appid (unlike featuredcategories, which only lists curated specials)
  and — crucially — accepts a BATCH of appids in one call, so refreshing every on-sale
  game costs only ceil(N/BATCH) calls. That makes this cheap enough to run often, which
  is how we catch sale *extensions* / early-endings / %-changes: we simply re-poll all
  on-sale games every run and overwrite. No event/webhook exists on Steam's side; cheap
  frequent polling is the robust substitute.

Which games we check:
  Only those games.json already marks on sale (discount_pct > 0). We don't independently
  discover new sales — the main scraper force-refreshes on-sale games, so new sales show
  up there quickly, then we attach the end date. Keeps this job cheap and targeted.

Pruning:
  Each run rewrites sales.json from scratch with only games still genuinely on sale
  (GetItems returns an active discount with a future end date). Ended/expired sales drop
  out automatically, so the file never accumulates stale dates.
"""

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # read-only here (owned by scraper.py)
SALES_FILE = HERE / "sales.json"          # THIS file's output (committed)

COUNTRY = os.environ.get("QHPP_CC", "US")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()   # not required (public method)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 300                  # commit progress every few minutes
TIME_BUFFER = 45

BATCH = 50                                # appids per GetItems call
GETITEMS_DELAY = 1.2                      # politeness between batch calls
MAX_RETRIES = 4
# A discount_end_date this far in the past is treated as already-ended (clock skew slack)
PAST_SLACK = 120

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qhpp sales-refresher; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(msg, flush=True)


def get(url, *, params=None, timeout=30):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- #
# GetItems batch -> {appid: discount_end_ts}
# --------------------------------------------------------------------------- #
def _extract_end_date(item):
    """From one GetItems store_item, return the soonest active discount_end_date (unix)
    or None. Tolerates every field being absent (permanent discounts, no purchase
    option, odd shapes)."""
    bpo = item.get("best_purchase_option") or {}
    discounts = bpo.get("active_discounts") or []
    ends = []
    for d in discounts:
        v = d.get("discount_end_date")
        if v in (None, "", 0, "0"):
            continue
        try:
            ts = int(v)
        except (TypeError, ValueError):
            continue
        if ts > 1_500_000_000:                  # sane unix range (post-2017)
            ends.append(ts)
    return min(ends) if ends else None          # soonest end if several stacked


def fetch_end_dates(appids):
    """Batch-query GetItems for a list of appids. Returns {appid: end_ts} for those that
    currently have an active dated discount. appids without one are simply omitted."""
    out = {}
    payload = {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"country_code": COUNTRY, "language": "english"},
        "data_request": {"include_basic_info": False, "include_assets": False,
                         "include_release": False, "include_tag_count": 0,
                         "include_reviews": False, "include_platforms": False,
                         "include_all_purchase_options": False},
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict):
        return out
    items = ((data.get("response") or {}).get("store_items")) or []
    for item in items:
        aid = item.get("appid") or item.get("id")
        if aid is None:
            continue
        end = _extract_end_date(item)
        if end is not None:
            out[int(aid)] = end
    return out


# --------------------------------------------------------------------------- #
# I/O + git
# --------------------------------------------------------------------------- #
def load_onsale_appids():
    """The set of games games.json currently marks on sale (discount_pct > 0)."""
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return [int(g["appid"]) for g in d.get("games", []) if (g.get("discount_pct") or 0) > 0]


def save_sales(sales):
    SALES_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "country": COUNTRY,
         "count": len(sales), "sales": sales},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "sales.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"  committed: {msg}")
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    now = int(start)
    onsale = load_onsale_appids()
    if not onsale:
        log("No on-sale games in games.json (or only sample data). Writing empty sales.json.")
        save_sales({})
        git_checkpoint("sales: no active sales")
        return 0

    log(f"On-sale games to refresh: {len(onsale)} "
        f"({math.ceil(len(onsale)/BATCH)} GetItems batches)")

    sales = {}            # rebuilt fresh each run -> automatic pruning of ended sales
    n_dated = n_undated = 0
    budget = RUN_MINUTES * 60
    last_commit = time.time()

    for i in range(0, len(onsale), BATCH):
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached; wrapping up.")
            break
        chunk = onsale[i:i + BATCH]
        ends = fetch_end_dates(chunk)
        time.sleep(GETITEMS_DELAY)
        for aid in chunk:
            end = ends.get(aid)
            if end is None:
                n_undated += 1            # on sale but no dated end (e.g. permanent/launch) -> omit
                continue
            if end <= now - PAST_SLACK:
                n_undated += 1            # already ended -> prune (don't record)
                continue
            sales[str(aid)] = {"discount_end": end, "scraped_at": now}
            n_dated += 1

        if i % (BATCH * 10) == 0:
            log(f"  [{min(i+BATCH, len(onsale))}/{len(onsale)}] {n_dated} dated so far")
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_sales(sales)
            git_checkpoint(f"sales: {len(sales)} active end-dates (checkpoint)")
            last_commit = time.time()

    save_sales(sales)
    git_checkpoint(f"sales: {len(sales)} active sale end-dates")
    log(f"\nDone. {n_dated} games with a live end date, {n_undated} on sale without a "
        f"dated/future discount (omitted). sales.json now tracks {len(sales)} sales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
