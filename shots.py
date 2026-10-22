#!/usr/bin/env python3
"""
Steam QTPD — store screenshot index
===========================================================================
One SEPARATE, independent job that owns the SCREENSHOT layer: for each appid, the
CDN filenames of its store screenshots, so the hover panel can rotate real gameplay
stills instead of showing one piece of key art.

WHY THIS NEEDS A SCRAPE AT ALL
------------------------------
Same reason trailers.py exists, and it is worth restating because the thumbnail path
has trained everyone to expect the opposite. Capsule/header art is FREE — every URL is
derivable from the appid alone (`.../steam/apps/<appid>/header.jpg`), which is why the
hover-enlarge never needed a data layer. Screenshots are NOT: they are addressed by a
content hash (`ss_<sha1>.jpg`) that nothing about the appid predicts. index.html's T6
fallback chain says so in as many words — "no screenshot step — screenshot URLs aren't
derivable from appid". So they have to be looked up and stored.

WHY NOT THE PICS FIELD WE ALREADY HAVE
--------------------------------------
PICS carries a `store_screenshot` field and pics_refresh.py already fetches it, so it
looks free. It is not usable (measured over all 64 pics_raw shards, 2026-08-19):

  * present for 14,975 of 126,742 apps = 11.8%, and it is ONE image, not a set
  * Valve stopped populating it: 61-63% of 2016-2018 releases carry it, 29% of 2019,
    0.8% of 2020, and ~0.0% of everything 2023 and later
  * it is INVERSELY correlated with popularity — only 3 of the top 500 most-reviewed
    games have it (0.6%) — because the maintained, migrated store pages are exactly
    the ones that dropped it

It is a legacy field from before `store_item_assets`, and it is dead for anything
current. Hence this job.

THE ENDPOINT
------------
IStoreBrowseService/GetItems/v1 with `data_request.include_screenshots`. The same
batched endpoint trailers.py and price_and_sale.py already use, on api.steampowered.com
(the big budget, not the 200-per-5-min storefront one), 50 appids per call. A full sweep
of the ~127k catalog is ~2.5k calls = ~50 minutes, so the first run drains the whole
backlog and every run after it is near-instant.

EXPECTED COVERAGE
-----------------
Unlike a trailer, screenshots are effectively MANDATORY: Valve requires a minimum of 5
to publish a store page. Trailers are optional and trailers.json still resolved 96.4% of
the catalog, so this layer should land at or above that. That is an inference from
Valve's submission rules, not a measurement — QTPD_DUMP_SHOTS=1 turns it into a number.

WHAT WE HAVE NOT VERIFIED (run the dump before trusting the parser)
-------------------------------------------------------------------
Steam is unreachable from the dev sandbox, so unlike trailers.py — whose surprises were
caught by a dump run — the exact response shape here is INFERRED. Two specific unknowns:

  1. THE KEY PATH. Expected `screenshots.all_ages_screenshots[] = {filename, ordinal}`,
     with `mature_content_screenshots` alongside. extract_shots() therefore refuses to
     hardcode one path: it tries the named keys, then any list-of-dicts under
     `screenshots`, and pulls `filename`/`path`-shaped values out of whatever it finds.
  2. THE SERVING HOST. `ss_<sha1>.jpg` is expected under the same `store_item_assets/`
     root the frontend already uses for modern header art (index.html ASSET_CDN), with
     an `<appid>/` segment in the path. probe_shot_hosts() HEADs a real filename across
     the host x root matrix under the dump flag and prints whatever actually returns an
     image, exactly as trailers.py settled its own base.

Set QTPD_DUMP_SHOTS=1 (workflow input `dump`) to dump the raw blob, report per-batch
coverage, probe the CDN hosts, and exit without writing anything.

ADULT CONTENT
-------------
Only `all_ages_screenshots` is stored. Valve splits mature stills into
`mature_content_screenshots`, and a game can carry those WITHOUT tripping the frontend's
PICS-based 18+ gate (pics_summarize `adult` = content_desc 3/4) — in which case the
rotation would show unblurred mature images on a thumbnail that was never gated. Storing
only the all-ages set makes that impossible. A game with nothing but mature stills is
recorded as a miss. The mature set stays available for a later change that reads the
adult flag, but that is a deliberate decision, not this job's default.

Output shots/shard_NN.json (served, sharded by `appid % 64`), format shots_v1:
  { "_format": "shots_v1", "_shard": NN, "base": "<absolute CDN prefix>",
    "count": N, "shots": { "<appid>": ["<file>.jpg", ...] } }

SHARDED, not one flat file, and this is the one place this layer departs from
trailers.json. A trailer is one short filename list per game and the frontend needs the
whole map at load; screenshots are ~4 hashes per game (~25MB across the catalog) and
ONLY the row you actually hover ever needs them. So the frontend fetches `appid % 64`
on first hover and caches it — ~400KB once, instead of 25MB on every page load, on a
page that already pulls ~240MB of JSON. Same idiom as pics_raw/ and updates_raw/.

Output shots_state.json (NOT served; the queue's memory, like catalog.json):
  { "misses": { "<appid>": <ts> }, "swept_at": <ts> }
A miss is re-checked after MISS_TTL_DAYS, because an unreleased or freshly-listed game
gains screenshots later.

Ownership (one writer per file):
  scraper.py      -> games.json    (catalog, rating, tags, last_update, release)
  price_and_sale  -> prices.json   (price, discount %, sale end)
  trailers.py     -> trailers.json + trailers_state.json
  THIS            -> shots/shard_NN.json + shots_state.json
Reads games.json (read-only) for the appid list and review counts.
"""

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"                    # read-only (owned by scraper.py)
SHOTS_DIR = HERE / "shots"                          # this job's output (served, sharded)
STATE_FILE = HERE / "shots_state.json"              # this job's queue memory (not served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 1800       # was 300: every commit is a new version in git history (§4)
TIME_BUFFER = 45

GETITEMS_BATCH = 50                                 # appids per GetItems call
GETITEMS_DELAY = 1.2                                # matches trailers.py / price_and_sale.py
MAX_RETRIES = 4
MISS_TTL_DAYS = int(os.environ.get("QTPD_SHOTS_MISS_TTL", "30"))

SHARDS = 64                                         # appid % SHARDS -> shots/shard_NN.json

# How many stills to keep per game. The hover panel shows them one at a time after the
# ~6s microtrailer, so a viewer sees maybe three before moving on; storing Valve's full
# set (often 10-30) would multiply the layer's bytes for stills nobody reaches. Valve's
# own `ordinal` is the developer's chosen order, so the first N are the best N.
MAX_SHOTS = int(os.environ.get("QTPD_SHOTS_PER_GAME", "4"))

# The `store_item_assets/` ROOT ONLY — deliberately NOT including `steam/apps/`.
#
# CONFIRMED 2026-08-20 by probe_shot_hosts on a runner (run 32365480684): this base, joined
# via join_url, returns 200 image/jpeg for BOTH rootings in the wild —
#   .../store_item_assets/steam/apps/730/ss_<sha1>.jpg                 (flat)
#   .../store_item_assets/steam/apps/578080/<sha1>/ss_<sha1>.jpg       (hash-dir)
# shared.cloudflare, shared.akamai and shared.fastly all serve both. Note that
# cdn.cloudflare/cdn.akamai + `steam/apps/` serve the FLAT shape and 404 the hash-dir one:
# a probe of a single sample would have blessed a base that is right for only part of the
# catalog, which is why the dump probes one sample per distinct rooting.
#
# CORRECTED 2026-08-19 after the first real sweep. Valve returns these filenames already
# rooted at `steam/apps/<appid>/<file>`, so the original base (which ended in
# `steam/apps/`) produced `.../store_item_assets/steam/apps/steam/apps/<appid>/...` —
# every URL doubled and 404'd. This is the exact mirror of the trap trailers.py fell into
# from the other direction, where the learned prefix was NOT the whole path: the lesson is
# that the split between "base" and "filename" is Valve's to decide, not ours to assume.
# The resulting URL now matches the shape the site already serves header art from
# (index.html ASSET_CDN + `<appid>/<hash>/header.jpg`), which is the one piece of evidence
# available without a runner: `store_item_assets/steam/apps/<appid>/<file>`.
# cloudflare over akamai to match the host the capsule art already comes from.
# akamai rather than cloudflare, though the probe showed both serving every rooting: this
# is the host ASSET_CDN already pulls every modern header image from, so it is the one edge
# the page continuously demonstrates works for whoever is looking at it. A host the app has
# never used is an unproven dependency, and when it fails for a viewer the rotation just
# silently never starts. The frontend falls back across all three regardless (SHOT_HOSTS in
# index.html); this only decides which one is tried first.
CDN_HOST = os.environ.get("QTPD_SHOTS_CDN",
                          "https://shared.akamai.steamstatic.com/store_item_assets/")

# Probed as a HOST x ROOT matrix under the dump flag. Kept as lists for the same reason
# trailers.py keeps them: when the path shape surprises us, the answer should come from a
# runner rather than from another guess.
CANDIDATE_HOSTS = [
    "https://shared.cloudflare.steamstatic.com/",
    "https://shared.akamai.steamstatic.com/",
    "https://shared.fastly.steamstatic.com/",
    "https://cdn.cloudflare.steamstatic.com/",
    "https://cdn.akamai.steamstatic.com/",
]
CANDIDATE_ROOTS = ["store_item_assets/steam/apps/", "steam/apps/", ""]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd screenshot indexer; github pages dataset builder)",
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
# Screenshot extraction — tolerant of the key path (see module docstring §unverified)
# --------------------------------------------------------------------------- #
def _clean_filename(v):
    """One usable CDN filename out of a raw field value, or None.

    Valve is inconsistent about whether these come back bare (`ss_<sha1>.jpg`), as a
    relative path (`<appid>/ss_<sha1>.jpg`), or as a full URL with a cache-buster
    (`https://.../ss_<sha1>.1920x1080.jpg?t=1762820639`). All three are reduced to
    what the frontend can join onto `base`: the host is dropped, the `?t=` suffix is
    dropped, and any leading slash goes. A retained "/" is meaningful and is left
    alone — index.html uses exactly that character to tell a full relative path from a
    bare filename needing an `<appid>/` segment (the same discriminator `artUrl` uses
    for PICS header art).
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    s = s.split("?", 1)[0]                       # drop the ?t= cache-buster
    if s.startswith("http"):
        # Keep everything after the STORE_ITEM_ASSETS root, so a full URL reduces to the
        # same `steam/apps/<appid>/<file>` shape the relative form already arrives in.
        if "/store_item_assets/" in s:
            s = s.split("/store_item_assets/", 1)[1]
        elif "/steam/apps/" in s:
            s = "steam/apps/" + s.split("/steam/apps/", 1)[1]
        else:
            s = s.rsplit("/", 1)[-1]             # unknown layout: keep the bare filename
    s = s.lstrip("/")
    return s if s.lower().endswith(IMAGE_EXTS) else None


def _entries(node):
    """Every dict in a list-of-dicts, tolerating a bare list of filename strings."""
    out = []
    if isinstance(node, list):
        for e in node:
            if isinstance(e, dict):
                out.append(e)
            elif isinstance(e, str):
                out.append({"filename": e})
    return out


def _ordered_filenames(entries):
    """[filename] for screenshot entries, in Valve's own `ordinal` order.

    `ordinal` is the order the store page shows them in, i.e. the developer's pick of
    what represents the game best — so taking the first MAX_SHOTS is taking the best
    MAX_SHOTS, not an arbitrary slice. Entries without an ordinal keep their position
    (sorted is stable), and duplicates are dropped while preserving order.
    """
    rows = []
    for i, e in enumerate(entries):
        fn = None
        for key in ("filename", "path_thumbnail", "path_full", "path", "url"):
            fn = _clean_filename(e.get(key))
            if fn:
                break
        if not fn:
            continue
        try:
            ordinal = int(e.get("ordinal"))
        except (TypeError, ValueError):
            ordinal = i
        rows.append((ordinal, i, fn))
    rows.sort(key=lambda r: (r[0], r[1]))
    return list(dict.fromkeys(fn for _o, _i, fn in rows))


def extract_shots(item):
    """(files, had_mature_only) for one GetItems store_item.

    `files` are base-relative filenames, best-first, capped at MAX_SHOTS. ALL-AGES ONLY
    — see the module docstring: a game can carry mature stills without tripping the
    frontend's PICS 18+ gate, and an ungated rotation of those is not a risk worth
    taking for a hover preview.

    `had_mature_only` reports a game that has screenshots but none we will store, so the
    run log can distinguish "Valve returned nothing" from "we declined what was there".
    """
    shots = item.get("screenshots")
    if not isinstance(shots, (dict, list)):
        return [], False

    # A bare list means the shape changed under us; treat it as the all-ages set rather
    # than dropping the game entirely.
    if isinstance(shots, list):
        return _ordered_filenames(_entries(shots))[:MAX_SHOTS], False

    files = _ordered_filenames(_entries(shots.get("all_ages_screenshots")))
    if files:
        return files[:MAX_SHOTS], False

    # Named key gone or empty: accept any OTHER list-of-dicts under `screenshots`, so a
    # rename degrades to something rather than nothing. Mature-looking keys stay skipped.
    # This runs BEFORE the mature check on purpose — checking mature first would make a
    # game that has both a renamed all-ages set and a mature set report as mature-only,
    # discarding perfectly storable stills.
    for key, v in shots.items():
        if key == "all_ages_screenshots" or "mature" in key.lower():
            continue
        files = _ordered_filenames(_entries(v))
        if files:
            return files[:MAX_SHOTS], False

    # Nothing storable. Distinguish "Valve returned nothing" from "we declined what was
    # there", so the run log can tell the two apart.
    mature = _ordered_filenames(_entries(shots.get("mature_content_screenshots")))
    return [], bool(mature)


def join_url(base, appid, filename):
    """base + filename, collapsing any overlap between them.

    The Python twin of index.html's joinShot(), and it exists for the same reason: Valve
    roots these filenames at `steam/apps/<appid>/` while a candidate base may or may not
    already end in those segments, and concatenating blindly is what produced
    `.../steam/apps/steam/apps/<appid>/...` in the first sweep. The prober below MUST use
    it — probing candidate roots with a naive join tests malformed URLs and reports that
    nothing works, which is worse than not probing at all.
    """
    b = base if base.endswith("/") else base + "/"
    path = filename if "/" in filename else f"steam/apps/{appid}/{filename}"
    b_segs = [x for x in b.split("/") if x]
    p_segs = [x for x in path.split("/") if x]
    overlap = 0
    for n in range(min(len(b_segs), len(p_segs)), 0, -1):
        if b_segs[-n:] == p_segs[:n]:
            overlap = n
            break
    return b + "/".join(p_segs[overlap:])


def probe_shot_hosts(appid, filename):
    """HEAD one real screenshot across the HOST x ROOT matrix and log every status.

    Only runs under QTPD_DUMP_SHOTS=1. The point is to settle the base with evidence
    from a runner that can reach Steam rather than guessing from a sandbox where the
    whole domain is blocked — the same move that took trailers.py two rounds to get
    right. Any 200 with an image content-type is reprinted at the end as the value to
    paste into QTPD_SHOTS_CDN / CDN_HOST.
    """
    log("=== CDN base probe (host x root) ===")
    hits = []
    for root in CANDIDATE_ROOTS:
        for host in CANDIDATE_HOSTS:
            base = host + root
            url = join_url(base, appid, filename)
            try:
                r = SESSION.head(url, timeout=20, allow_redirects=True)
                ctype = r.headers.get("content-type", "?")
                log(f"  {r.status_code}  {ctype:<18} {url}")
                if r.status_code == 200 and "image" in ctype:
                    hits.append(base)
            except requests.RequestException as e:
                log(f"  ERR  {url}  ({e})")
    log("=== result ===")
    if hits:
        for b in hits:
            log(f"  WORKS -> QTPD_SHOTS_CDN={b}")
    else:
        log("  nothing served an image. The path shape itself is wrong — read a store "
            "page's own <img> src before guessing again.")


# --------------------------------------------------------------------------- #
# GetItems
# --------------------------------------------------------------------------- #
def getitems(appids):
    """One batched GetItems call -> list of store_item dicts (empty list on failure)."""
    payload = {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"country_code": os.environ.get("QHPP_CC", "US"), "language": "english"},
        # include_screenshots is the only block this job needs; basic_info stays on purely
        # so the response echoes an identifiable appid per item. Everything else off.
        "data_request": {"include_basic_info": True, "include_screenshots": True,
                         "include_trailers": False, "include_assets": False,
                         "include_release": False, "include_tag_count": 0,
                         "include_reviews": False, "include_platforms": False,
                         "include_all_purchase_options": False},
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict):
        return []
    items = ((data.get("response") or {}).get("store_items")) or []
    # Diagnostic twin of trailers.py's QTPD_DUMP_TRAILERS. The response shape here was
    # never verified against a live call (see the module docstring), so this is not a
    # nicety — run it before trusting a single row this parser writes.
    if os.environ.get("QTPD_DUMP_SHOTS") == "1":
        log("=== RAW GetItems(screenshots) DUMP (first 2 items) ===")
        for it in items[:2]:
            log(json.dumps(it.get("screenshots"), indent=2)[:3000])
            log("---")
        log("=== extract_shots results ===")
        samples, got = [], 0
        for n, it in enumerate(items):
            aid = it.get("appid") or it.get("id")
            files, mature_only = extract_shots(it)
            if files:
                got += 1
                samples.append((aid, files[0]))
            if n < 10:
                log(f"  {aid}: n={len(files)} mature_only={mature_only} files={files}")
        log(f"=== coverage in this batch: {got}/{len(items)} items returned screenshots ===")
        if samples:
            log(f"=== URL the frontend would build, under the shipped base ===")
            for aid, f in samples[:5]:
                log(f"  {join_url(CDN_HOST, aid, f)}")
            # Probe up to three DISTINCT rootings. The catalog carries at least three
            # (hash-dir `<appid>/<sha1>/ss_<sha1>.jpg`, flat `<appid>/ss_<sha1>.jpg`, and
            # legacy `<appid>/<digits>.jpg`), and a host that serves one need not serve
            # all of them — probing a single sample could bless a base that is only
            # right for a third of the catalog.
            seen_shapes, probed = set(), 0
            for aid, f in samples:
                shape = f.count("/")
                if shape in seen_shapes:
                    continue
                seen_shapes.add(shape)
                probe_shot_hosts(aid, f)
                probed += 1
                if probed >= 3:
                    break
        else:
            log("  no screenshots found in this batch — nothing to probe")
        sys.exit(0)
    return items


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def load_catalog():
    """[(appid, review_count)] from games.json, most-reviewed first.

    Ordering is the whole first-pass strategy: the backlog is ~127k games but the ones
    anybody actually hovers are the popular ones, so stills show up on the games that
    matter within the first run rather than after the full sweep.
    """
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    rows = [(int(g["appid"]), int(g.get("review_count") or 0))
            for g in d.get("games", []) if g.get("appid") is not None]
    rows.sort(key=lambda r: -r[1])
    return rows


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def shard_of(appid):
    return int(appid) % SHARDS


def shard_path(n):
    return SHOTS_DIR / f"shard_{n:02d}.json"


def load_shots():
    """({appid_str: [filename]}, {stale shard numbers}) from the shards on disk.

    The second value is every shard whose stored `base` no longer matches CDN_HOST. A hit
    is never re-queried, so without this a base correction would never reach the rows
    already committed — they would serve broken URLs until someone forced a full re-sweep.
    Returning them as pre-dirtied shards makes the next run repair itself.
    """
    hits, stale = {}, set()
    for n in range(SHARDS):
        doc = load_json(shard_path(n), {})
        rows = doc.get("shots") or {}
        if rows and doc.get("base") != CDN_HOST:
            stale.add(n)
        for k, v in rows.items():
            if isinstance(v, list) and v:
                hits[str(k)] = v
    return hits, stale


def save_shots(hits, dirty):
    """Write only the shards that changed this run.

    Rewriting all 64 every checkpoint would make a ~25MB diff out of a few hundred new
    rows, and every one of those bytes is a commit on a repo that GitHub Pages serves
    live. `dirty` is the set of shard numbers touched since the last save.
    """
    SHOTS_DIR.mkdir(exist_ok=True)
    buckets = {n: {} for n in dirty}
    for k, v in hits.items():
        n = shard_of(k)
        if n in buckets:
            buckets[n][k] = v
    for n, rows in buckets.items():
        # Compact separators, not indent=2: machine-generated, browser-facing, and
        # pretty-printing would roughly triple the bytes on the wire for no human gain.
        shard_path(n).write_text(json.dumps(
            {"_format": "shots_v1", "_shard": n,
             "_doc": "Store screenshots by appid, sharded appid%%%d. Written by shots.py; "
                     "see ARCHITECTURE.md 2.2. Join `base` + (filename with a '/' as-is, "
                     "else '<appid>/' + filename)." % SHARDS,
             "base": CDN_HOST, "generated_at": int(time.time()), "count": len(rows),
             "shots": {str(k): v for k, v in sorted(rows.items(), key=lambda kv: int(kv[0]))}},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def save_state(misses, swept_at):
    STATE_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "swept_at": swept_at,
         "misses": {str(k): v for k, v in sorted(misses.items(), key=lambda kv: int(kv[0]))}},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _on_main():
    """True when the checkout really is main.

    Guard for the workflow's `ref: ${{ github.ref_name }}` checkout: a manual dispatch
    from a fix branch must never be able to push that branch's code onto main via the
    `HEAD:main` checkpoint below. A branch run is therefore always a dry run.
    """
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
        subprocess.run(["git", "add", "shots", "shots_state.json"], check=False)
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
def select_work(catalog, hits, misses, now):
    """Appids to query this run: never-checked first (most-reviewed first), then stale
    misses (oldest first). A hit is never re-queried — a screenshot's content hash is
    effectively permanent, and the periodic full re-sweep is a manual dispatch.

    This is also what keeps NEW GAMES covered without a second job: a freshly scraped
    appid has no entry in either map, so it lands at the head of `fresh` on the next
    daily run.
    """
    ttl = MISS_TTL_DAYS * 86400
    fresh, stale = [], []
    for appid, _rc in catalog:
        key = str(appid)
        if key in hits:
            continue
        seen = misses.get(key)
        if seen is None:
            fresh.append(appid)
        elif (now - int(seen)) >= ttl:
            stale.append((int(seen), appid))
    stale.sort()
    return fresh + [a for _ts, a in stale]


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER

    catalog = load_catalog()
    if not catalog:
        log("games.json missing or sample-only — nothing to do.")
        return

    hits, stale_base = load_shots()
    sdoc = load_json(STATE_FILE, {})
    misses = dict(sdoc.get("misses") or {})
    swept_at = sdoc.get("swept_at") or 0

    queue = select_work(catalog, hits, misses, int(time.time()))
    log(f"Catalog {len(catalog)} | have shots {len(hits)} | known misses {len(misses)} | "
        f"queued {len(queue)} | budget {RUN_MINUTES}min | max {MAX_SHOTS}/game")
    if not queue:
        log("Nothing due. (Screenshot hashes are permanent; re-sweep via manual dispatch.)")
        if stale_base:
            log(f"  ...but {len(stale_base)} shard(s) carry an outdated base — rewriting")
            save_shots(hits, stale_base)
        save_state(misses, int(time.time()))
        git_checkpoint("shots: base rewrite" if stale_base else "shots: nothing due")
        return

    last_ckpt = time.time()
    found = checked = mature_only = 0
    # Shards carrying an outdated base start dirty, so the correction lands on the next
    # save even for games this run never re-queries.
    dirty = set(stale_base)
    if stale_base:
        log(f"  {len(stale_base)} shard(s) carry an outdated base — rewriting them")
    for i in range(0, len(queue), GETITEMS_BATCH):
        if time.time() >= deadline:
            log("  time budget reached — wrapping up")
            break
        batch = queue[i:i + GETITEMS_BATCH]
        items = getitems(batch)
        time.sleep(GETITEMS_DELAY)
        now = int(time.time())
        seen_ids = set()
        for it in items:
            aid = it.get("appid") or it.get("id")
            if aid is None:
                continue
            key = str(aid)
            seen_ids.add(int(aid))
            files, only_mature = extract_shots(it)
            if only_mature:
                mature_only += 1
            if files:
                hits[key] = files
                dirty.add(shard_of(key))
                misses.pop(key, None)
                found += 1
            else:
                # Nothing storable: no screenshots at all, or all-ages set empty and
                # only mature stills offered (which this job declines by design).
                misses[key] = now
        # Appids the response dropped entirely (delisted, region-locked, not an app):
        # record them as misses so they don't re-queue every single run.
        for aid in batch:
            if int(aid) not in seen_ids and str(aid) not in hits:
                misses[str(aid)] = now
        checked += len(batch)

        if time.time() - last_ckpt >= CHECKPOINT_SECONDS:
            save_shots(hits, dirty)
            save_state(misses, swept_at)
            git_checkpoint(f"shots: {len(hits)} with screenshots ({found} new this run)")
            dirty.clear()
            last_ckpt = time.time()

    if checked >= len(queue):
        swept_at = int(time.time())
    save_shots(hits, dirty)
    save_state(misses, swept_at)
    git_checkpoint(f"shots: {len(hits)} with screenshots ({found} new this run)")
    log(f"Done. checked {checked}, found {found}, mature-only declined {mature_only}, "
        f"total with shots {len(hits)}, misses {len(misses)} | "
        f"{(time.time()-started)/60:.1f}min")


if __name__ == "__main__":
    main()
