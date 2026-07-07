#!/usr/bin/env python3
"""
Steam QHPP — prices + sale end-dates refresher
===========================================================================
One SEPARATE, independent job that owns the whole fast-changing PRICING layer:
current price, discount %, and sale end-date. It writes a single prices.json; the main
scraper no longer touches any of these (they change far more often than catalog/tags do,
and keeping them here keeps the slow scrape lean and avoids write collisions).

Why prices and sales are bundled (vs. two jobs): they're the same logical fact — "what
does this game cost right now" — and they refresh on the same cadence. Bundling means one
schedule, one file, one merge on the frontend. They use two endpoints, but that's an
implementation detail inside this one job.

The two endpoints, both cheap:
  1. PRICES — store.steampowered.com/api/appdetails?filters=price_overview&appids=<CSV>
     This is the ONE appdetails variant Valve still lets you BATCH: pass many comma-
     separated appids and it returns price_overview for all of them in a single call.
     (Full appdetails is one-appid-only since 2015; price-only is the exception.) So the
     entire ~3,200-game catalog refreshes in ~ceil(N/BATCH) calls instead of N.
  2. SALE END DATES — IStoreBrowseService/GetItems/v1 (batched), reading
     best_purchase_option.active_discounts[].discount_end_date. Only queried for games
     that came back on sale in step 1, so it's tiny.

Output prices.json keyed by appid -> { price_initial, price_final, discount_pct,
discount_end, scraped_at }. discount_end is null unless the game is on sale with a dated
end. Ended/expired sales are pruned (frontend also collapses past-due sales offline).

Ownership (one writer per file):
  scraper.py      -> games.json   (catalog, rating, tags, last_update, release)
  THIS            -> prices.json  (price, discount %, sale end)
  hltb_refresh    -> hltb.json    (static completion times)
  recent_refresh  -> recent.json  (30-day review scores)
Frontend merges all four by appid; QHPP is computed client-side from the merge.

Reads games.json (read-only) for the appid list (and to know which games are free).
"""

import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # read-only (owned by scraper.py)
PRICES_FILE = HERE / "prices.json"        # this job's output (committed)

COUNTRY = os.environ.get("QHPP_CC", "US")
COUNTRY_LC = COUNTRY.lower()
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 300
TIME_BUFFER = 45

PRICE_BATCH = 100                         # appids per batched price-only appdetails call
GETITEMS_BATCH = 50                       # appids per GetItems call (sale end dates)
STORE_DELAY = 1.6                         # between storefront calls (~200/5min budget)
GETITEMS_DELAY = 1.2
MAX_RETRIES = 4
PAST_SLACK = 120                          # treat end dates this far past as already-ended

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qhpp price/sale refresher; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(msg, flush=True)


def get(url, *, params=None, timeout=40):
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
# 1. Batched prices via appdetails?filters=price_overview
# --------------------------------------------------------------------------- #
def fetch_prices(appids):
    """Return {appid: {price_initial, price_final, discount_pct}} for a batch. Games that
    are free or have no price block are returned with nulls/0 so the frontend can clear a
    stale sale. The response is keyed by appid string, each {success, data:{price_overview}}."""
    csv = ",".join(str(a) for a in appids)
    data = get("https://store.steampowered.com/api/appdetails",
               params={"appids": csv, "filters": "price_overview", "cc": COUNTRY_LC, "l": "english"})
    out = {}
    if not isinstance(data, dict):
        return out
    for aid in appids:
        node = data.get(str(aid))
        if not isinstance(node, dict) or not node.get("success"):
            continue
        po = (node.get("data") or {}).get("price_overview")
        if not po:
            # success but no price -> free or unpriced. Record explicit nulls so any prior
            # sale is cleared.
            out[int(aid)] = {"price_initial": None, "price_final": None, "discount_pct": 0}
            continue
        pi = round(po.get("initial", 0) / 100, 2) or None
        pf = round(po.get("final", 0) / 100, 2) or None
        out[int(aid)] = {"price_initial": pi, "price_final": pf,
                         "discount_pct": int(po.get("discount_percent", 0))}
    return out


# --------------------------------------------------------------------------- #
# 2. Batched sale end-dates via GetItems
# --------------------------------------------------------------------------- #
# Every key Steam has been observed to return a sale-end unix timestamp under, across
# the various GetItems schema revisions. We check all of them so a schema tweak on
# Valve's side can't silently null us out again.
_END_KEYS = ("discount_end_date", "discount_end", "end_date", "ends_at", "expiry_time")


def _coerce_ts(v):
    """Return a plausible future-ish unix timestamp int, or None. Accepts ints, numeric
    strings, and {'seconds': ...} / {'value': ...} wrapper objects Steam sometimes uses."""
    if isinstance(v, dict):
        v = v.get("seconds") or v.get("value") or v.get("time")
    if v in (None, "", 0, "0"):
        return None
    try:
        ts = int(v)
    except (TypeError, ValueError):
        return None
    # sanity window: after 2017-07 and before 2100. Rejects millisecond values,
    # release years, and other garbage.
    return ts if 1_500_000_000 < ts < 4_100_000_000 else None


def _iter_purchase_options(item):
    """Yield every purchase-option dict, regardless of which container Steam used.
    Different GetItems responses put discounts under best_purchase_option,
    purchase_options[], or (rarely) a bare active_discounts[] at the item root."""
    bpo = item.get("best_purchase_option")
    if isinstance(bpo, dict):
        yield bpo
    for po in (item.get("purchase_options") or []):
        if isinstance(po, dict):
            yield po
    # some responses omit the wrapper and hang active_discounts off the item itself
    if isinstance(item.get("active_discounts"), list):
        yield item


def _extract_end_date(item):
    """Robustly pull the earliest sale-end timestamp from any purchase-option shape.
    Returns None if the item carries no dated discount (permanent price cut, or Steam
    simply didn't send an end date)."""
    ends = []
    for po in _iter_purchase_options(item):
        discounts = po.get("active_discounts")
        if not isinstance(discounts, list):
            continue
        for d in discounts:
            if not isinstance(d, dict):
                continue
            for k in _END_KEYS:
                ts = _coerce_ts(d.get(k))
                if ts is not None:
                    ends.append(ts)
                    break  # one hit per discount is enough
    return min(ends) if ends else None


def fetch_end_dates(appids):
    out = {}
    payload = {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"country_code": COUNTRY, "language": "english"},
        # include_basic_info + include_all_purchase_options are what actually populate the
        # purchase-option / active_discounts blocks that carry the sale end date. With both
        # off (the previous state) the response came back with no discount info at all, so
        # every end date resolved to null — the silent bug that made every row show a flat
        # "on sale". Keep the rest off to stay lean.
        "data_request": {"include_basic_info": True, "include_assets": False,
                         "include_release": False, "include_tag_count": 0,
                         "include_reviews": False, "include_platforms": False,
                         "include_all_purchase_options": True},
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict):
        return out
    items = ((data.get("response") or {}).get("store_items")) or []
    # Diagnostic: set QHPP_DUMP_GETITEMS=1 to print the raw JSON of the first batch's
    # first few items, then exit. Run this once via the workflow's manual dispatch to see
    # the exact field Steam uses, from a runner that can actually reach the API.
    if os.environ.get("QHPP_DUMP_GETITEMS") == "1":
        log("=== RAW GetItems DUMP (first 3 items) ===")
        for item in items[:3]:
            log(json.dumps(item, indent=2)[:4000])
            log("---")
        log(f"=== _extract_end_date results: "
            f"{[(it.get('appid') or it.get('id'), _extract_end_date(it)) for it in items[:10]]}")
        sys.exit(0)
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
def load_appids():
    """All non-free appids from games.json (free games have no price to refresh)."""
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return [int(g["appid"]) for g in d.get("games", []) if not g.get("is_free")]


def save_prices(prices):
    PRICES_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "country": COUNTRY,
         "count": len(prices), "prices": {str(k): v for k, v in prices.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "prices.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            for _attempt in range(1, 9):    # retry against other jobs pushing concurrently
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log(f"  committed: {msg}")
                    break
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    start = time.time()
    now = int(start)
    appids = load_appids()
    if not appids:
        log("No priced games in games.json (or only sample data). Writing empty prices.json.")
        save_prices({})
        git_checkpoint("prices: nothing to refresh")
        return 0

    log(f"Priced games to refresh: {len(appids)} "
        f"({math.ceil(len(appids)/PRICE_BATCH)} price batches)")

    prices = {}                # rebuilt fresh each run
    budget = RUN_MINUTES * 60
    last_commit = time.time()
    onsale = []                # appids that came back discounted -> need an end date

    # --- pass 1: batched prices for the whole catalog ---
    for i in range(0, len(appids), PRICE_BATCH):
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached during price pass; wrapping up.")
            break
        chunk = appids[i:i + PRICE_BATCH]
        got = fetch_prices(chunk)
        time.sleep(STORE_DELAY)
        for aid, p in got.items():
            prices[str(aid)] = {**p, "discount_end": None, "scraped_at": now}
            if (p.get("discount_pct") or 0) > 0:
                onsale.append(aid)
        if i % (PRICE_BATCH * 5) == 0:
            log(f"  [prices {min(i+PRICE_BATCH, len(appids))}/{len(appids)}] {len(onsale)} on sale so far")
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_prices(prices)
            git_checkpoint(f"prices: {len(prices)} priced (checkpoint)")
            last_commit = time.time()

    # --- pass 2: sale end-dates only for the on-sale subset ---
    log(f"Fetching sale end-dates for {len(onsale)} on-sale games "
        f"({math.ceil(len(onsale)/GETITEMS_BATCH)} batches)")
    n_dated = 0
    for i in range(0, len(onsale), GETITEMS_BATCH):
        if budget - (time.time() - start) < TIME_BUFFER:
            log("Time budget reached during sale-date pass; wrapping up.")
            break
        chunk = onsale[i:i + GETITEMS_BATCH]
        ends = fetch_end_dates(chunk)
        time.sleep(GETITEMS_DELAY)
        for aid, end in ends.items():
            if end <= now - PAST_SLACK:
                continue                  # already ended -> leave discount_end null (prune)
            key = str(aid)
            if key in prices:
                prices[key]["discount_end"] = end
                n_dated += 1
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_prices(prices)
            git_checkpoint(f"prices: {len(prices)} priced, {n_dated} sale dates (checkpoint)")
            last_commit = time.time()

    save_prices(prices)
    git_checkpoint(f"prices: {len(prices)} priced, {len(onsale)} on sale, {n_dated} dated")
    log(f"\nDone. Refreshed {len(prices)} prices; {len(onsale)} on sale; "
        f"{n_dated} with a live sale end-date. prices.json updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
