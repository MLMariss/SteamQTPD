#!/usr/bin/env python3
"""
Steam QTPD — store trailer index
===========================================================================
One SEPARATE, independent job that owns the TRAILER layer: for each appid, the
CDN filename(s) of its store trailer, so the frontend can play a video preview
when you hover a game's thumbnail.

WHY THIS NEEDS A SCRAPE AT ALL
------------------------------
Thumbnails are free: every capsule/header image URL is derivable from the appid
alone (`.../steam/apps/<appid>/header.jpg`), which is why the hover-enlarge has
never needed a data layer. Trailers are NOT. A trailer is addressed by a hashed CDN
path that nothing about the appid predicts — Dota 2 (570) serves its microtrailer from
`570/116737/313addee2092d0bd6f538d164610061ea8bbe79c/1749859757/microtrailer.webm`,
where only the leading `570` is derivable. It has to be looked up and stored. Hence:
one more file, one more job (ARCHITECTURE §1, one writer per file).

THE ENDPOINT
------------
IStoreBrowseService/GetItems/v1 with `data_request.include_trailers`. Same batched
endpoint price_and_sale.py already uses for sale end-dates, on api.steampowered.com
(the big budget, not the 200-per-5-min storefront one), 50 appids per call. A full
sweep of the ~127k catalog is ~2.5k calls = ~50 minutes.

WHAT VALVE ACTUALLY RETURNS (verified 2026-08-19 via QTPD_DUMP_TRAILERS)
-----------------------------------------------------------------------
The first dump run corrected two assumptions that would have shipped broken:

  1. THERE IS NO PROGRESSIVE FULL TRAILER ANY MORE. The old movie480/movie_max
     .webm/.mp4 files are gone. A highlight now carries exactly two things:
       * `microtrailer`: [{filename, type}] -- Valve's own ~6s silent loop, in
         webm AND mp4. This is the ONLY natively playable asset.
       * `adaptive_trailers`: [{cdn_path, encoding}] -- dash_av1.mpd,
         dash_h264.mpd, hls_264_master.m3u8. These are DASH/HLS MANIFESTS: a
         plain <video src> cannot play them without dash.js/hls.js, which a
         static site should not be shipping. We record only that they exist
         (`adaptive` in the output header) so the option stays visible.
     So the hover preview is the microtrailer -- which is exactly what Steam
     itself plays when you hover a capsule in the store.

  2. `trailer_url_format` IS RELATIVE, and its placeholder is `${FILENAME}`,
     not `{FILENAME}`:
         "steam/apps/${FILENAME}?t=1762820639"
     The prefix before the placeholder is a CDN-relative PATH, so it has to be
     joined onto a CDN host (CDN_HOST). The `?t=` suffix is a cache-buster and
     is dropped. Filenames themselves are now long hashed paths that begin with
     the appid, e.g.
         570/116737/313addee.../1749859757/microtrailer.webm

Set QTPD_DUMP_TRAILERS=1 to re-dump the raw blob AND probe the candidate CDN
hosts (probe_hosts) from a runner that can actually reach Steam -- a sandbox
with Steam blocked cannot, which is why this file guesses nothing about hosts
that the dump has not confirmed.

Output trailers.json (served to the browser), format trailers_v2:
  { "base": "<absolute CDN prefix>",
    "trailers": { "<appid>": ["<file>.webm", "<file>.mp4"] } }
One flat list per game, best-codec-first, from the FIRST highlight only (a game
like TF2 exposes 17 trailers; the frontend plays one). The frontend emits one
<source> per entry and lets the browser choose. Games with no playable trailer
are absent -- the hover panel falls back to the enlarged still.

Output trailers_state.json (NOT served; the queue's memory, like catalog.json):
  { "misses": { "<appid>": <ts> }, "swept_at": <ts> }
A miss is re-checked after MISS_TTL_DAYS, because an unreleased game gains a
trailer later.

Ownership (one writer per file):
  scraper.py      -> games.json    (catalog, rating, tags, last_update, release)
  price_and_sale  -> prices.json   (price, discount %, sale end)
  THIS            -> trailers.json + trailers_state.json
Reads games.json (read-only) for the appid list and review counts.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

import gitsync                      # commits this job's own files to main (gitsync.py)

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"                    # read-only (owned by scraper.py)
TRAILERS_FILE = HERE / "trailers.json"              # this job's output (served)
STATE_FILE = HERE / "trailers_state.json"           # this job's queue memory (not served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 1800       # was 300: every commit is a new version in git history (§4); trailers.json is 23 MB
TIME_BUFFER = 45

GETITEMS_BATCH = 50                                 # appids per GetItems call
GETITEMS_DELAY = 1.2                                # matches price_and_sale.py's pacing
MAX_RETRIES = 4
MISS_TTL_DAYS = int(os.environ.get("QTPD_TRAILER_MISS_TTL", "30"))

# `trailer_url_format` gives only a CDN-RELATIVE path ("steam/apps/${FILENAME}?t=..."),
# so the host has to come from here. CDN_HOST is the one probe_hosts confirmed; the
# relative prefix is still learned per-run, so a path change needs no code edit.
# CONFIRMED by probe_hosts on 2026-08-19: this host+root, with the learned "steam/apps/"
# prefix appended, returns 200 video/webm. video.cloudflare / video.akamai / video.fastly
# all serve it; cloudflare matches the host index.html already uses for capsule art.
# The subtlety that cost two rounds: `store_trailers/` is the ROOT and `steam/apps/` is
# the learned prefix — the path needs BOTH, concatenated in that order.
CDN_HOST = os.environ.get("QTPD_TRAILER_CDN",
                          "https://video.cloudflare.steamstatic.com/store_trailers/")
DEFAULT_PREFIX = "steam/apps/"          # used only if no response carried a format string

# Probed as a HOST x ROOT matrix by probe_hosts() under QTPD_DUMP_TRAILERS=1. The first
# probe tried hosts only and every combination 404'd, because the format's "steam/apps/"
# prefix is not the whole path: the site's own modern art already lives under a
# `store_item_assets/` root (index.html ASSET_CDN =
# shared.akamai.steamstatic.com/store_item_assets/steam/apps), which is exactly that
# prefix with a root in front. Kept as lists so the next surprise stays a data question
# answered from a runner rather than another guess.
CANDIDATE_HOSTS = [
    "https://shared.akamai.steamstatic.com/",
    "https://shared.cloudflare.steamstatic.com/",
    "https://shared.fastly.steamstatic.com/",
    "https://video.akamai.steamstatic.com/",
    "https://video.cloudflare.steamstatic.com/",
    "https://video.fastly.steamstatic.com/",
    "https://cdn.akamai.steamstatic.com/",
    "https://cdn.cloudflare.steamstatic.com/",
]
CANDIDATE_ROOTS = ["store_item_assets/", "", "store_trailers/"]

VIDEO_EXTS = (".webm", ".mp4")
HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd trailer indexer; github pages dataset builder)",
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
# Trailer extraction — filename-driven, not key-path-driven (see module docstring)
# --------------------------------------------------------------------------- #
def _walk(node):
    """Yield every dict nested anywhere under `node` (any depth, through lists)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _rank(filename):
    """Codec preference within one tier: VP9/WebM first (roughly half the bytes of
    the h264 mp4 at the same tier, and every browser we care about plays it except
    old Safari), mp4 second as the compatibility source."""
    f = filename.lower()
    if f.endswith(".webm"):
        return 0
    return 1


def _prefix_from_format(fmt):
    """CDN-relative path prefix out of a `trailer_url_format` value.

    Real example: "steam/apps/${FILENAME}?t=1762820639" -> "steam/apps/".
    Note the placeholder is `${FILENAME}`, not `{FILENAME}` — the leading `$` has to
    be stripped too, and everything from the brace onward (including the `?t=`
    cache-buster) is discarded.
    """
    if not isinstance(fmt, str) or "{" not in fmt:
        return None
    return fmt.split("{")[0].rstrip("$").lstrip("/")


def _playable(entries):
    """[filename] for a list of {filename,type} dicts, webm before mp4.

    WebM/VP9 first (about half the bytes of the h264 mp4 at the same tier, and
    played by everything current); mp4 second as the compatibility source for older
    Safari. `sorted` is stable, so within a codec Valve's own order survives.
    """
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fn = e.get("filename")
        if isinstance(fn, str) and fn.lower().endswith(VIDEO_EXTS):
            out.append(fn)
    return sorted(dict.fromkeys(out), key=lambda f: 0 if f.lower().endswith(".webm") else 1)


def _highlights(trailers):
    """Every trailer entry for one item, best-first.

    `highlights` is the store's own ordering (what the page shows first) and comes
    first; `other_trailers` is APPENDED rather than used as an either/or fallback, so
    an app whose highlights carry no playable asset can still fall through to one that
    does. Any other list-of-dicts under `trailers` is accepted last, so a key rename
    degrades to something rather than nothing.
    """
    if isinstance(trailers, list):
        return [t for t in trailers if isinstance(t, dict)]
    if not isinstance(trailers, dict):
        return []
    out = []
    for key in ("highlights", "other_trailers"):
        v = trailers.get(key)
        if isinstance(v, list):
            out += [t for t in v if isinstance(t, dict)]
    if out:
        return out
    for v in trailers.values():
        if isinstance(v, list) and any(isinstance(t, dict) for t in v):
            return [t for t in v if isinstance(t, dict)]
    return []


def extract_trailer(item):
    """(files, has_adaptive, prefix_or_None) for one GetItems store_item.

    `files` are CDN-relative filenames for ONE trailer — the first highlight — best
    codec first. Only the first: TF2 exposes 17 trailers and the hover panel plays
    one, so keeping them all would bloat the served file for nothing.

    Which asset: `microtrailer` is the only natively playable thing Valve still
    returns (see the module docstring). The legacy progressive tiers are checked
    first anyway, so an app that still carries them gets the better clip; in
    practice none currently do.

    `has_adaptive` reports whether DASH/HLS manifests exist for this app. It is
    aggregated into the output header only — those need dash.js/hls.js to play, and
    a static site should not ship a streaming library just for a hover preview.
    """
    trailers = item.get("trailers")
    if not isinstance(trailers, (dict, list)):
        return [], False, None

    prefix = None
    has_adaptive = False
    for d in _walk(trailers):
        if prefix is None:
            for v in d.values():
                p = _prefix_from_format(v)
                if p:
                    prefix = p
                    break
        if not has_adaptive and isinstance(d.get("cdn_path"), str):
            has_adaptive = True

    files = []
    for h in _highlights(trailers):
        # Legacy progressive tiers first (better content when present), then the
        # microtrailer, which is what actually ships today.
        for key in ("trailer_480p", "trailer_max", "microtrailer"):
            files = _playable(h.get(key))
            if files:
                break
        if files:
            break

    return files, has_adaptive, prefix


def probe_hosts(prefix, filename):
    """HEAD one real trailer file across the HOST x ROOT matrix and log every status.

    Only runs under QTPD_DUMP_TRAILERS=1. The point is to settle the base with
    evidence from a runner that can reach Steam, rather than guessing from a sandbox
    where the whole domain is blocked. Any 200 is printed again at the end as the
    value to paste into QTPD_TRAILER_CDN / CDN_HOST.
    """
    log("=== CDN base probe (host x root) ===")
    hits = []
    for root in CANDIDATE_ROOTS:
        for host in CANDIDATE_HOSTS:
            base = host + root
            url = base + (prefix or DEFAULT_PREFIX) + filename
            try:
                r = SESSION.head(url, timeout=20, allow_redirects=True)
                ctype = r.headers.get("content-type", "?")
                log(f"  {r.status_code}  {ctype:<18} {url}")
                # A real video, not an HTML error page dressed as a 200.
                if r.status_code == 200 and "video" in ctype:
                    hits.append(base)
            except requests.RequestException as e:
                log(f"  ERR  {url}  ({e})")
    log("=== result ===")
    if hits:
        for b in hits:
            log(f"  WORKS -> CDN_HOST={b}")
    else:
        log("  nothing served a video. The path shape itself is wrong — re-read a "
            "store page's own <video> src before guessing again.")


# --------------------------------------------------------------------------- #
# GetItems
# --------------------------------------------------------------------------- #
def getitems(appids):
    """One batched GetItems call -> list of store_item dicts (empty list on failure)."""
    payload = {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"country_code": os.environ.get("QHPP_CC", "US"), "language": "english"},
        # include_trailers is the only block this job needs; basic_info stays on purely
        # so the response echoes an identifiable appid per item. Everything else off.
        "data_request": {"include_basic_info": True, "include_trailers": True,
                         "include_assets": False, "include_release": False,
                         "include_tag_count": 0, "include_reviews": False,
                         "include_platforms": False, "include_all_purchase_options": False},
    }
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    data = get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", params=params)
    if not isinstance(data, dict):
        return []
    items = ((data.get("response") or {}).get("store_items")) or []
    # Diagnostic twin of price_and_sale.py's QHPP_DUMP_GETITEMS: set QTPD_DUMP_TRAILERS=1
    # and run the workflow manually to see the real shape from a runner that can actually
    # reach the API (a sandbox with Steam blocked cannot).
    if os.environ.get("QTPD_DUMP_TRAILERS") == "1":
        log("=== RAW GetItems(trailers) DUMP (first 2 items) ===")
        for it in items[:2]:
            log(json.dumps(it.get("trailers"), indent=2)[:3000])
            log("---")
        log("=== extract_trailer results ===")
        first = None
        for it in items[:10]:
            files, adaptive, prefix = extract_trailer(it)
            log(f"  {it.get('appid') or it.get('id')}: adaptive={adaptive} prefix={prefix!r} "
                f"files={files}")
            if first is None and files:
                first = (prefix, files[0])
        if first:
            probe_hosts(*first)
        else:
            log("  no playable files found in this batch — nothing to probe")
        sys.exit(0)
    return items


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def load_catalog():
    """[(appid, review_count)] from games.json, most-reviewed first.

    Ordering is the whole first-pass strategy: the backlog is ~127k games but the
    ones anybody actually hovers are the popular ones, so trailers show up on the
    games that matter within the first run rather than after the full sweep.
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


def absolute_base(prefix):
    """CDN_HOST joined to the learned prefix — unless the prefix is ALREADY absolute.

    Valve's format string is relative today ("steam/apps/${FILENAME}"), but it used to
    be a full URL, and a revert would otherwise yield
    "https://cdn.../https://video.../..." — a silently broken base on every row.
    """
    p = prefix or DEFAULT_PREFIX
    return p if p.startswith("http") else CDN_HOST + p


def save_trailers(hits, prefix, adaptive_count):
    # Compact separators, not indent=2: this file is machine-generated, browser-facing,
    # and ~100k rows deep — pretty-printing it would roughly triple the bytes on the
    # wire for zero human benefit.
    #
    # `base` is stored ABSOLUTE (host + learned prefix) so the frontend concatenates
    # and nothing else needs to know how it was assembled.
    TRAILERS_FILE.write_text(json.dumps(
        {"format": "trailers_v2", "generated_at": int(time.time()),
         "base": absolute_base(prefix), "count": len(hits),
         # Informational: how many apps also expose DASH/HLS manifests, i.e. how much
         # is on the table if a streaming player is ever worth the weight.
         "adaptive_available": adaptive_count,
         "trailers": {str(k): v for k, v in sorted(hits.items(), key=lambda kv: int(kv[0]))}},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def save_state(misses, swept_at):
    STATE_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "swept_at": swept_at,
         "misses": {str(k): v for k, v in sorted(misses.items(), key=lambda kv: int(kv[0]))}},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def git_checkpoint(msg):
    """Commit trailers.json + trailers_state.json on top of the current main (gitsync.py). A
    run dispatched from a branch is a dry run: gitsync never pushes from a non-main checkout."""
    gitsync.commit(["trailers.json", "trailers_state.json"], msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def select_work(catalog, hits, misses, now):
    """Appids to query this run: never-checked first (most-reviewed first), then
    stale misses (oldest first). A hit is never re-queried — a trailer's movie id is
    effectively permanent, and the periodic full re-sweep is a manual dispatch."""
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

    tdoc = load_json(TRAILERS_FILE, {})
    hits = dict(tdoc.get("trailers") or {})
    prefix = None                       # learned from the first response that carries it
    adaptive_count = int(tdoc.get("adaptive_available") or 0)
    sdoc = load_json(STATE_FILE, {})
    misses = dict(sdoc.get("misses") or {})
    swept_at = sdoc.get("swept_at") or 0

    queue = select_work(catalog, hits, misses, int(time.time()))
    log(f"Catalog {len(catalog)} | have trailers {len(hits)} | known misses {len(misses)} | "
        f"queued {len(queue)} | budget {RUN_MINUTES}min")
    if not queue:
        log("Nothing due. (Trailer paths are permanent; re-sweep via manual dispatch.)")
        save_state(misses, int(time.time()))
        git_checkpoint("trailers: nothing due")
        return

    last_ckpt = time.time()
    found = checked = 0
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
            files, has_adaptive, learned = extract_trailer(it)
            if learned and prefix is None:
                prefix = learned
            if has_adaptive:
                adaptive_count += 1
            if files:
                hits[key] = files
                misses.pop(key, None)
                found += 1
            else:
                # No natively playable asset. Adaptive-only apps land here too: they
                # have a trailer, we just cannot play it without a streaming library.
                misses[key] = now
        # Appids the response dropped entirely (delisted, region-locked, not an app):
        # record them as misses so they don't re-queue every single run.
        for aid in batch:
            if int(aid) not in seen_ids and str(aid) not in hits:
                misses[str(aid)] = now
        checked += len(batch)

        if time.time() - last_ckpt >= CHECKPOINT_SECONDS:
            save_trailers(hits, prefix, adaptive_count)
            save_state(misses, swept_at)
            git_checkpoint(f"trailers: {len(hits)} with video ({found} new this run)")
            last_ckpt = time.time()

    if checked >= len(queue):
        swept_at = int(time.time())
    save_trailers(hits, prefix, adaptive_count)
    save_state(misses, swept_at)
    git_checkpoint(f"trailers: {len(hits)} with video ({found} new this run)")
    log(f"Done. checked {checked}, found {found}, total with trailers {len(hits)}, "
        f"misses {len(misses)} | {(time.time()-started)/60:.1f}min")


if __name__ == "__main__":
    main()
