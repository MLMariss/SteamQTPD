#!/usr/bin/env python3
"""
Steam QTPD — demo index
===========================================================================
One SEPARATE, independent job that owns the DEMO layer: for each game, the appid of
the demo Steam lists for it, so the page's Flags → Demo control can include, exclude
or require games you can try for free first.

THE SOURCE (verified 2026-10-21 by demo_probe.py, workflow 0.3)
---------------------------------------------------------------
IStoreBrowseService/GetItems/v1 — the batched endpoint price_and_sale.py and
trailers.py already use, on api.steampowered.com (the big budget, not the
200-per-5-min storefront one), 50 appids per call. Every store item carries

    "related_items": {"demo_appid": [465090],
                      "demos": [{"appid": 465090, "show_above_purchase": true}]}

when the game has a demo, with basic_info alone switched on. The probe found it on all
four known-demo games, on 13% of a released sample, 66% of upcoming games with a date
and 32% of undated ones, and every demo appid it checked (50 of 50) was live on the
store. Full appdetails carries the same list as `demos`, but only one appid per call.

THE SWEEP
---------
Daily, over every game the page can show: upcoming.json first (where demos are most
common and change fastest — Next Fest), then games.json. ~190k appids is ~3,800 calls,
~95 minutes at this pacing. A run that hits its time budget leaves a cursor, and the
next one carries on from it; a full pass records `swept_at`.

Only what Steam ANSWERS changes anything: a game in a successful response gains or loses
its demo; a failed call, or an appid the response leaves out, keeps whatever it had.
Games no longer in either catalogue are pruned on every save.

Output demos.json (served to the browser), format demos_v1:
  { "generated_at", "count", "swept_at", "cursor": [phase, appid] | null,
    "demos": { "<appid>": <first demo appid> } }
Only games WITH a demo are listed — absence means none (as far as the last sweep saw).

Ownership (one writer per file): THIS -> demos.json. Reads games.json (scraper.py) and
upcoming.json (scraper.py) read-only.
"""

import json
import os
import random
import subprocess
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"            # read-only (owned by scraper.py)
UPCOMING_FILE = HERE / "upcoming.json"      # read-only (owned by scraper.py)
DEMOS_FILE = HERE / "demos.json"            # this job's output (served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 1800       # was 600: every commit is a new version in git history (§4)
TIME_BUFFER = 45

GETITEMS_BATCH = 50                         # appids per GetItems call
GETITEMS_DELAY = 1.2                        # matches price_and_sale.py / trailers.py
MAX_RETRIES = 4
PHASES = ("upcoming", "released")           # sweep order; the cursor names one of these

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd demo indexer; github pages dataset builder)",
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
        # related_items comes back with basic_info alone (demo_probe.py's "basic" variant).
        "data_request": {"include_basic_info": True},
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
        return None
    return data["response"].get("store_items") or []


def demo_of(item):
    """The first demo appid Steam lists for one store item, or None."""
    rel = item.get("related_items") or {}
    for x in rel.get("demo_appid") or []:
        if str(x).isdigit():
            return int(x)
    for d in rel.get("demos") or []:
        if isinstance(d, dict) and str(d.get("appid", "")).isdigit():
            return int(d["appid"])
    return None


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def load_worklists():
    """{phase: sorted [appid]} — what the page can show: upcoming.json and games.json."""
    up = load_json(UPCOMING_FILE, {}).get("games") or []
    gd = load_json(GAMES_FILE, {})
    rel = [] if gd.get("sample") else (gd.get("games") or [])
    upcoming = sorted({int(g["appid"]) for g in up if g.get("appid") is not None})
    up_set = set(upcoming)
    released = sorted({int(g["appid"]) for g in rel if g.get("appid") is not None} - up_set)
    return {"upcoming": upcoming, "released": released}


def save_demos(demos, worklists, swept_at, cursor):
    """Write demos.json, pruned to games still in either catalogue. Compact: browser-facing."""
    keep = set(worklists["upcoming"]) | set(worklists["released"])
    out = {str(a): d for a, d in sorted(demos.items(), key=lambda kv: int(kv[0])) if int(a) in keep}
    doc = {"_format": "demos_v1", "generated_at": int(time.time()), "count": len(out),
           "swept_at": swept_at, "cursor": cursor, "demos": out}
    tmp = DEMOS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, DEMOS_FILE)
    return len(out)


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
        subprocess.run(["git", "add", "demos.json"], check=False)
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


def queue_from(worklists, cursor):
    """[(phase, appid)] in sweep order, starting just after `cursor` ([phase, appid])."""
    q = [(p, a) for p in PHASES for a in worklists[p]]
    if not cursor:
        return q
    phase, after = cursor[0], int(cursor[1])
    if phase not in PHASES:
        return q
    start = PHASES.index(phase)
    return [(p, a) for p, a in q if PHASES.index(p) > start or (p == phase and a > after)]


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER

    worklists = load_worklists()
    total = len(worklists["upcoming"]) + len(worklists["released"])
    if not total:
        log("games.json / upcoming.json missing or empty — nothing to do.")
        return
    doc = load_json(DEMOS_FILE, {})
    demos = {int(a): int(d) for a, d in (doc.get("demos") or {}).items()}
    swept_at = doc.get("swept_at") or 0
    queue = queue_from(worklists, doc.get("cursor"))
    log(f"Catalogue {total} ({len(worklists['upcoming'])} upcoming) | with a demo {len(demos)} | "
        f"queued {len(queue)} (resuming from {doc.get('cursor')}) | budget {RUN_MINUTES}min")

    last_ckpt = time.time()
    cursor = doc.get("cursor")
    gained = lost = failed = 0
    done_all = True
    for i in range(0, len(queue), GETITEMS_BATCH):
        if time.time() >= deadline:
            log("  time budget reached — leaving a cursor for the next run")
            done_all = False
            break
        batch = queue[i:i + GETITEMS_BATCH]
        items = getitems([a for _p, a in batch])
        time.sleep(GETITEMS_DELAY)
        if items is None:
            failed += 1                       # no answer: every game keeps what it had
        else:
            for it in items:
                aid = it.get("appid") or it.get("id")
                if aid is None:
                    continue
                aid, d = int(aid), demo_of(it)
                if d:
                    gained += aid not in demos
                    demos[aid] = d
                elif aid in demos:
                    del demos[aid]
                    lost += 1
        cursor = [batch[-1][0], batch[-1][1]]

        if time.time() - last_ckpt >= CHECKPOINT_SECONDS:
            n = save_demos(demos, worklists, swept_at, cursor)
            git_checkpoint(f"demos: {n} games with a demo (+{gained} / -{lost} this run)")
            last_ckpt = time.time()

    if done_all:
        swept_at, cursor = int(time.time()), None      # full pass: next run starts over
    n = save_demos(demos, worklists, swept_at, cursor)
    log(f"Done. {n} games with a demo (+{gained} / -{lost} this run, {failed} failed call(s)); "
        f"{'full sweep complete' if done_all else f'resume after {cursor}'}.")
    git_checkpoint(f"demos: {n} games with a demo (+{gained} / -{lost}"
                   f"{', sweep complete' if done_all else ''})")


if __name__ == "__main__":
    main()
