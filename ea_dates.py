#!/usr/bin/env python3
"""
Steam QTPD — Early Access dates
===========================================================================
One SEPARATE, independent job that owns the EARLY ACCESS DATES layer: for a game in Early
Access, the 1.0 date its developer has announced; for a game that has left Early Access,
the day it first became playable. The first puts announced 1.0 launches in the page's
Upcoming view (Every Day We Fight, 1546080, was on Steam's own calendar on its 1.0 day and
nowhere on QTPD); the second keeps an EA start that Steam overwrites with the 1.0 date.

THE SOURCE (verified 2026-10-22 by ea_dates_probe.py, workflow 0.5)
---------------------------------------------------------------------------
IStoreBrowseService/GetItems/v1 with data_request.include_release — the batched endpoint
price_and_sale.py, trailers.py and demos.py already use, on api.steampowered.com (the big
budget, not the 200-per-5-min storefront one), 50 appids per call. Its `release` block:

  is_early_access                    true while the game is in EA (agrees with PICS genre 70
                                     on 12,360 of 12,405)
  steam_release_date                 the EA start while in EA; overwritten with the 1.0 date
                                     on release — appdetails and PICS do the same
  release_from_early_access_date     the developer's planned 1.0 date (optional: 412 of
  release_from_early_access_style    12,405 EA games set one) and its precision —
                                     1 day, 2 month, 4 year; 3 is taken to be quarter. Its
                                     dates sit on the 1st of ANY month (first real run,
                                     2026-10-22), so Steam keeps the month the developer picked
                                     inside the quarter; the page reads the quarter off that
                                     month. Not proven: the log lists each precision's months
  original_steam_release_date        after 1.0, the EA start: exact on every game checked,
                                     on ~70% of games that left EA from 2018 on, on none that
                                     left before 2017. Also on 5% of never-EA games (relaunches),
                                     so it is read only for games with EA evidence (below)

THE SWEEP
---------
Daily, ~15k games (~300 calls, ~8 min):
  "ea"     every game PICS has in genre 70 (pics.json, pics_upcoming.json), plus every game
           this job has ever seen in EA (the state file) — so a game is still swept the day
           after it leaves EA, before PICS notices;
  "tagged" games carrying the "Early Access" store tag (493) but no longer genre 70: the
           ones that left EA. The tag is the EA evidence that gates original_steam_release_date.
Only what Steam ANSWERS changes anything: a failed call, or a game the run did not reach,
keeps its previous entry.

EA-START MEMORY (the ~30% Steam drops)
--------------------------------------
While a game is in EA, its steam_release_date IS its EA start, so the job records it in
ea_dates_state.json. When the game leaves EA and Steam keeps no original_steam_release_date,
the remembered day is used instead. It only helps games that leave EA from now on.

Output ea_dates.json (served to the browser, part of the boot load), format ea_dates_v1:
  { "_format", "generated_at", "count", "swept_at",
    "games": { "<appid>": {"ea": 1, "v": <planned 1.0 ts>, "p": <precision 1-4>}     in EA
                          | {"s": <EA start ts>, "v": <1.0 ts>} } }                     left EA
Only games with something to say are listed: an EA game with no announced 1.0 date, or a
game whose EA start is unknown, is absent. A planned date already past is kept as Steam has
it; the page decides how long a slipped date still counts (EA_V1_GRACE_DAYS in index.html).

ea_dates_state.json (NOT served): { "_format", "ea_start": {"<appid>": ts} } — every EA
start this job has seen. Pruned to the catalogue on save.

Ownership (one writer per file): THIS -> ea_dates.json + ea_dates_state.json. Reads pics.json
and pics_upcoming.json (pics_merge.py) read-only.
"""

import json
import os
import random
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
PICS_FILE = HERE / "pics.json"                     # read-only (owned by pics_merge.py)
PICS_UP_FILE = HERE / "pics_upcoming.json"         # read-only (owned by pics_merge.py)
OUT_FILE = HERE / "ea_dates.json"                  # this job's output (served)
STATE_FILE = HERE / "ea_dates_state.json"          # this job's memory (not served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "30"))
TIME_BUFFER = 45

GETITEMS_BATCH = 50                         # appids per GetItems call
GETITEMS_DELAY = 1.2                        # matches demos.py / price_and_sale.py / trailers.py
MAX_RETRIES = 4
EA_TAG = 493                                # store tag "Early Access" (lookups/tags.json)
SAME_DAY = 2 * 86400                        # dates this close are one day (time zones)

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd ea-dates indexer; github pages dataset builder)",
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
    payload = {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"country_code": os.environ.get("QHPP_CC", "US"), "language": "english"},
        "data_request": {"include_release": True},   # the release block is all this job reads
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
        return None
    return data["response"].get("store_items") or []


def num(v):
    """GetItems sends uint32s as ints; tolerate strings, and read 0 as absent."""
    try:
        return int(v) or None
    except (TypeError, ValueError):
        return None


def is_ea(item):
    """EA per GetItems: the release block's flag, else the item-level one."""
    r = item.get("release") or {}
    return bool(r.get("is_early_access") if "is_early_access" in r else item.get("is_early_access"))


def entry_for(item, remembered, now):
    """(served entry or None, EA start to remember or None) for one answered store item.

    In EA: the planned 1.0 date when the developer has set one; and, once the game is out,
    its steam_release_date — which is its EA start — for the memory.
    Not in EA: the EA start (Steam's original_steam_release_date, else the remembered day)
    when it is genuinely earlier than the release date; the release date is then the 1.0."""
    r = item.get("release") or {}
    srd = num(r.get("steam_release_date"))
    if is_ea(item):
        launched = srd is not None and srd <= now and not r.get("is_coming_soon")
        v1 = num(r.get("release_from_early_access_date"))
        served = {"ea": 1, "v": v1, "p": num(r.get("release_from_early_access_style")) or 1} if v1 else None
        return served, (srd if launched else None)
    if srd is None:
        return None, None
    for start in (num(r.get("original_steam_release_date")), remembered):
        if start and start < srd - SAME_DAY:
            return {"s": start, "v": srd}, None
    return None, None


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def load_worklists(state):
    """({phase: sorted [appid]}, catalogue appid set) from the PICS files and the memory."""
    pics = load_json(PICS_FILE, {}).get("apps") or {}
    up = load_json(PICS_UP_FILE, {}).get("apps") or {}
    catalogue = {int(a) for a in pics} | {int(a) for a in up}
    ea = {int(a) for src in (pics, up) for a, v in src.items() if v.get("ea")}
    ea |= {a for a in state if a in catalogue}           # seen in EA by an earlier run
    tagged = {int(a) for a, v in pics.items()
              if not v.get("ea") and EA_TAG in (v.get("tags") or [])} - ea
    return {"ea": sorted(ea), "tagged": sorted(tagged)}, catalogue


def save(entries, state, catalogue, swept_at):
    """Write both files, pruned to the catalogue. The served one is compact."""
    games = {str(a): entries[a] for a in sorted(entries) if a in catalogue}
    doc = {"_format": "ea_dates_v1", "generated_at": int(time.time()), "count": len(games),
           "swept_at": swept_at, "games": games}
    st = {"_format": "ea_dates_state_v1",
          "ea_start": {str(a): state[a] for a in sorted(state) if a in catalogue}}
    for path, body in ((OUT_FILE, json.dumps(doc, separators=(",", ":"))),
                       (STATE_FILE, json.dumps(st, separators=(",", ":")))):
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    return len(games)


def _on_main():
    """True when the checkout really is main — a run from another branch is a dry run,
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
        subprocess.run(["git", "add", "ea_dates.json", "ea_dates_state.json"], check=False)
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


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def summarize(entries, now):
    """Log what the file says, with each precision's months and days: the evidence for what
    precision 3 means (quarter, read off whichever month inside it Steam kept)."""
    planned = {a: e for a, e in entries.items() if e.get("ea")}
    future = [a for a, e in planned.items() if e["v"] > now]
    log(f"  in EA with a planned 1.0: {len(planned)} ({len(future)} future, "
        f"{len(planned) - len(future)} past); left EA with a known EA start: "
        f"{len(entries) - len(planned)}")
    for p in sorted({e["p"] for e in planned.values()}):
        months = Counter(datetime.fromtimestamp(e["v"], timezone.utc).strftime("%b")
                         for e in planned.values() if e["p"] == p)
        days = Counter(datetime.fromtimestamp(e["v"], timezone.utc).day
                       for e in planned.values() if e["p"] == p)
        log(f"  precision {p}: {sum(months.values())} games; months {dict(months)}; "
            f"day-of-month {dict(days.most_common(4))}")
    soon = sorted((e["v"], a) for a, e in planned.items() if now < e["v"] < now + 31 * 86400)
    log(f"  planned 1.0 in the next 31 days: {len(soon)}")
    for ts, a in soon[:40]:
        log(f"    {day(ts)}  p{planned[a]['p']}  {a}")


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER
    now = int(started)

    state = {int(a): int(t) for a, t in
             (load_json(STATE_FILE, {}).get("ea_start") or {}).items()}
    worklists, catalogue = load_worklists(state)
    queue = worklists["ea"] + worklists["tagged"]
    if not queue:
        log("pics.json / pics_upcoming.json missing or empty — nothing to do.")
        return
    doc = load_json(OUT_FILE, {})
    entries = {int(a): e for a, e in (doc.get("games") or {}).items()}
    swept_at = doc.get("swept_at") or 0
    log(f"Sweep {len(queue)} ({len(worklists['ea'])} EA or seen in EA, {len(worklists['tagged'])} "
        f"EA-tagged) | listed {len(entries)} | remembered EA starts {len(state)} | "
        f"budget {RUN_MINUTES}min")

    answered = failed = remembered_new = 0
    done_all = True
    for i in range(0, len(queue), GETITEMS_BATCH):
        if time.time() >= deadline:
            log("  time budget reached — the rest keep their previous entries")
            done_all = False
            break
        items = getitems(queue[i:i + GETITEMS_BATCH])
        time.sleep(GETITEMS_DELAY)
        if items is None:
            failed += 1                       # no answer: every game keeps what it had
            continue
        for it in items:
            aid = it.get("appid") or it.get("id")
            if aid is None:
                continue
            aid = int(aid)
            answered += 1
            served, start = entry_for(it, state.get(aid), now)
            if served:
                entries[aid] = served
            else:
                entries.pop(aid, None)
            if start and aid not in state:
                state[aid] = start
                remembered_new += 1

    if done_all:
        swept_at = int(time.time())
    n = save(entries, state, catalogue, swept_at)
    summarize({a: e for a, e in entries.items() if a in catalogue}, now)
    log(f"Done. {n} games listed; {answered} answered, {failed} failed call(s); "
        f"{remembered_new} EA start(s) newly remembered ({len(state)} total)"
        f"{'' if done_all else '; sweep incomplete'}.")
    git_checkpoint(f"ea dates: {n} games ({sum(1 for e in entries.values() if e.get('ea'))} "
                   f"with a planned 1.0)")


if __name__ == "__main__":
    main()
