#!/usr/bin/env python3
"""
Steam QTPD — upcoming-games refresher
===========================================================================
One SEPARATE, independent job that owns upcoming.json: the store details of every game in
the scraper's pending room (games announced on Steam but not out yet), refreshed daily in
one batched pass, plus the list of pending games Steam now says HAVE released.

WHY A SEPARATE JOB
------------------
Until Oct 2026 scraper.py built these rows itself, one storefront `appdetails` call per game
(~0.9 s each, out of the same budget that keeps released games' reviews fresh), so a full
pass over ~57k pending games took ~10 days of sweeps. batch_probe.py (workflow 0.4) showed
the batched IStoreBrowseService/GetItems endpoint — 50 games per call, on the separate
api.steampowered.com budget — carries the same facts: name 30/30, price 30/30, adult
content descriptors 30/30, coming-soon 29/30, and the release date as a timestamp plus a
precision code instead of text to parse. A full pass is ~1,150 calls, ~26 minutes.

OWNERSHIP (one writer per file)
-------------------------------
  scraper.py          -> catalog.json (the pending room itself), games.json
  THIS                -> upcoming.json
The scraper reads `released` from here and scrapes those games straight away (they jump
its queue, like any game whose date has passed); this job reads `pending` from catalog.json
to know what to look up. Neither ever writes the other's file.

WHAT A ROW HOLDS
----------------
Same lean shape the page already reads (ARCHITECTURE §5): appid, title, release_date (the
display text), tags (user-tag names via lookups/tags.json), scraped_at, and only when
non-empty: release_ts (day-precision dates only, like the scraper's own rule), price_initial,
price_final, discount_pct, is_free, adult (content descriptor 3 or 4 — the definition
pics_summarize.py uses). Only what Steam ANSWERS changes a row: a failed call, or a game the
response leaves out, keeps the row it had.

Output upcoming.json:
  { "generated_at", "count", "swept_at", "released": [appid, ...],
    "games": [ one row per line ] }
"""

import json
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CATALOG_FILE = HERE / "catalog.json"          # read-only (owned by scraper.py)
TAG_LOOKUP = HERE / "lookups" / "tags.json"   # read-only: tag id -> name
UPCOMING_FILE = HERE / "upcoming.json"        # this job's output (served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "45"))
TIME_BUFFER = 45

GETITEMS_BATCH = 50                           # appids per GetItems call
GETITEMS_DELAY = 1.2                          # matches price_and_sale.py / trailers.py / demos.py
MAX_RETRIES = 4
MAX_TAGS = 20
# What batch_probe.py validated against appdetails, minus the reviews block it doesn't need.
DATA_REQUEST = {"include_basic_info": True, "include_release": True,
                "include_all_purchase_options": True, "include_ratings": True,
                "include_tag_count": MAX_TAGS}
ADULT_DESCRIPTORS = {3, 4}                    # same as pics_summarize.py's adult flag

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd upcoming refresher; github pages dataset builder)",
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


def getitems(appids):
    """One batched GetItems call -> list of store_item dicts, or None when the call failed
    (so the caller can tell "no items" from "no answer" and keep what it had)."""
    payload = {"ids": [{"appid": int(a)} for a in appids],
               "context": {"country_code": os.environ.get("QHPP_CC", "US"), "language": "english"},
               "data_request": DATA_REQUEST}
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
        return None
    return data["response"].get("store_items") or []


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Store item -> row
# --------------------------------------------------------------------------- #
def release_info(rel):
    """(release_date text, release_ts | None) from GetItems' release block.

    The date is a timestamp plus `coming_soon_display`, a precision code: "date_full" is a
    real day, the others say how much of the timestamp to believe. Only a full date gets a
    release_ts (the scraper's rule: "2026" must not ripen on Jan 1). A publisher's own
    message ("Early 2027") wins over everything, as it does on the store page."""
    msg = (rel.get("custom_release_date_message") or "").strip()
    ts = rel.get("steam_release_date")
    ts = int(ts) if str(ts or "").isdigit() and int(ts) > 0 else None
    disp = rel.get("coming_soon_display") or ("date_full" if ts else "text_comingsoon")
    if msg:
        return msg, (ts if disp == "date_full" else None)
    if ts:
        d = datetime.fromtimestamp(ts, timezone.utc)
        if disp == "date_full":
            return f"{d:%b} {d.day}, {d.year}", ts
        if disp == "date_month":
            return f"{d:%B} {d.year}", None
        if disp == "date_quarter":
            return f"Q{(d.month - 1) // 3 + 1} {d.year}", None
        if disp == "date_year":
            return str(d.year), None
    if disp == "text_tba":
        return "To be announced", None
    return "Coming soon", None


def is_released(item, now):
    """True when Steam no longer calls the game coming soon and its date has passed."""
    rel = item.get("release") or {}
    ts = rel.get("steam_release_date")
    return (not rel.get("is_coming_soon")) and str(ts or "").isdigit() and 0 < int(ts) <= now


def row_of(item, tag_names, now):
    """The upcoming.json row for one store item. Lean: an absent key means empty."""
    rel = item.get("release") or {}
    text, ts = release_info(rel)
    bpo = item.get("best_purchase_option") or {}
    cents = lambda k: (int(bpo[k]) / 100) if str(bpo.get(k, "")).isdigit() else None
    final = cents("final_price_in_cents")
    initial = cents("original_price_in_cents") or final
    discount = int(bpo.get("discount_pct") or 0) if str(bpo.get("discount_pct", "")).isdigit() else 0
    descriptors = {int(x) for x in (item.get("content_descriptorids") or []) if str(x).isdigit()}
    tag_ids = item.get("tagids") or [t.get("tagid") for t in (item.get("tags") or []) if isinstance(t, dict)]
    tags = [tag_names[str(t)] for t in tag_ids if str(t) in tag_names][:MAX_TAGS]
    aid = int(item.get("appid") or item.get("id"))
    row = {"appid": aid, "title": item.get("name") or f"App {aid}", "release_date": text,
           "tags": tags, "scraped_at": now}
    for k, v in (("release_ts", ts), ("price_initial", initial), ("price_final", final),
                 ("discount_pct", discount), ("is_free", bool(item.get("is_free"))),
                 ("adult", bool(descriptors & ADULT_DESCRIPTORS))):
        if v:
            row[k] = v
    return row


def save_upcoming(rows, released, swept_at):
    """One row per line: ~57k rows indented would be several times the size, and a single
    line would make every commit's diff the whole file. Atomic replace."""
    rows = sorted(rows.values(), key=lambda r: r["appid"])
    body = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    head = (f'{{"generated_at":{int(time.time())},"count":{len(rows)},"swept_at":{swept_at},'
            f'"released":{json.dumps(sorted(released))},"games":[\n')
    tmp = UPCOMING_FILE.with_suffix(".json.tmp")
    tmp.write_text(head + body + "\n]}\n", encoding="utf-8")
    os.replace(tmp, UPCOMING_FILE)
    return len(rows)


def _on_main():
    """True when the checkout really is main — a dispatch from another branch is a dry run,
    so it can never push that branch's code onto main through the HEAD:main push below."""
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "main"


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    if not _on_main():
        log(f"  [dry run: not on main] would have committed: {msg}")
        return
    try:
        subprocess.run(["git", "add", "upcoming.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            for attempt in range(1, 9):    # retry against other jobs pushing concurrently
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log(f"  committed: {msg}")
                    break
                time.sleep(2 * attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER
    pending = sorted(int(a) for a in (load_json(CATALOG_FILE, {}).get("pending") or {}))
    if not pending:
        log("catalog.json has no pending games — nothing to do.")
        return
    tag_names = load_json(TAG_LOOKUP, {})
    tag_names = tag_names.get("tags", tag_names) if isinstance(tag_names, dict) else {}
    prev = load_json(UPCOMING_FILE, {})
    pend_set = set(pending)
    # Rows only for games still pending: one the scraper promoted or dropped falls out here.
    rows = {int(r["appid"]): r for r in (prev.get("games") or [])
            if isinstance(r, dict) and int(r.get("appid", 0)) in pend_set}
    released = set()
    log(f"Pending {len(pending)} | rows carried over {len(rows)} | "
        f"{-(-len(pending) // GETITEMS_BATCH)} calls | budget {RUN_MINUTES}min")

    answered = failed = 0
    done_all = True
    for i in range(0, len(pending), GETITEMS_BATCH):
        if time.time() >= deadline:
            log("  time budget reached — the rest keep their rows until the next run")
            done_all = False
            break
        items = getitems(pending[i:i + GETITEMS_BATCH])
        time.sleep(GETITEMS_DELAY)
        if items is None:
            failed += 1
            continue
        now = int(time.time())
        for it in items:
            if not (it.get("appid") or it.get("id")) or it.get("success") not in (1, None):
                continue                      # not answered for this game: keep its row
            aid = int(it.get("appid") or it.get("id"))
            answered += 1
            if is_released(it, now):
                released.add(aid)
                rows.pop(aid, None)           # out now: the scraper takes it from here
            else:
                rows[aid] = row_of(it, tag_names, now)

    swept_at = int(time.time()) if done_all else int(prev.get("swept_at") or 0)
    n = save_upcoming(rows, released, swept_at)
    log(f"Done. {n} upcoming rows, {len(released)} released (for the scraper), "
        f"{answered} answered, {failed} failed call(s).")
    git_checkpoint(f"upcoming: {n} games, {len(released)} released"
                   f"{'' if done_all else ' (partial sweep)'}")


if __name__ == "__main__":
    main()
