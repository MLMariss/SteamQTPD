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

Output trailers_upcoming.json, same format: the games only upcoming.json has. trailers.json
is part of the page's boot load and upcoming.json is not (it is fetched the first time the
page shows upcoming games), so their trailers ride with it; a game that releases moves to
trailers.json on the next save.

Output trailers_state.json (NOT served; the queue's memory, like catalog.json):
  { "misses": { "<appid>": <ts> }, "checked": { "<appid>": <ts> },
    "adaptive": [<appid>, ...], "swept_at": <ts> }
A miss is re-checked after MISS_TTL_DAYS, because an unreleased game gains a
trailer later. A hit is re-checked after VERIFY_DAYS (`checked` is when Steam last
confirmed it): developers replace trailers, and the old file then 404s on the CDN,
so a hit that was never re-asked would eventually play nothing. A hit with no stamp
yet (everything stored before Oct 2026) gets one spread evenly over the window, so
the re-checks cost ~1/VERIFY_DAYS of the catalogue a day (~45 calls), not one burst.

WHICH GAMES
-----------
Everything the page can show: games.json (most-reviewed first), then upcoming.json,
whose 61k unreleased games are hovered in the Upcoming view like any other row.

ONLY WHAT STEAM ANSWERS CHANGES ANYTHING
----------------------------------------
A failed call (no response at all) changes nothing: its 50 games are asked again next
run. Until Oct 2026 a failed call came back as an empty list, so all 50 were recorded
as misses and not asked again for 30 days. A re-checked hit is dropped only when Steam
answers for that game with no playable trailer, and never from a batch where none of
the known hits came back with one (an answer that carries no trailer data at all).

Ownership (one writer per file):
  scraper.py      -> games.json    (catalog, rating, tags, last_update, release)
  price_and_sale  -> prices.json   (price, discount %, sale end)
  THIS            -> trailers.json + trailers_upcoming.json + trailers_state.json
Reads games.json and upcoming.json (read-only) for the appid list and review counts.
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
UPCOMING_FILE = HERE / "upcoming.json"              # read-only (owned by upcoming_refresh.py)
TRAILERS_FILE = HERE / "trailers.json"              # this job's output (served, boot load)
TRAILERS_UP_FILE = HERE / "trailers_upcoming.json"  # the same for upcoming-only games (served,
                                                    # fetched with upcoming.json, not at boot)
STATE_FILE = HERE / "trailers_state.json"           # this job's queue memory (not served)

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))
CHECKPOINT_SECONDS = 1800       # was 300: every commit is a new version in git history (§4); trailers.json is 23 MB
TIME_BUFFER = 45

GETITEMS_BATCH = 50                                 # appids per GetItems call
GETITEMS_DELAY = 1.2                                # matches price_and_sale.py's pacing
MAX_RETRIES = 4
MISS_TTL_DAYS = int(os.environ.get("QTPD_TRAILER_MISS_TTL", "30"))
VERIFY_DAYS = int(os.environ.get("QTPD_TRAILER_VERIFY_DAYS", "60"))   # re-check a hit this often

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
    """One batched GetItems call -> list of store_item dicts, or None when the call failed
    (so the caller can tell "no trailers" from "no answer" and keep what it had)."""
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
    if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
        if os.environ.get("QTPD_DUMP_TRAILERS") == "1":
            log("=== GetItems gave no answer — nothing to dump ===")
            sys.exit(1)               # a dump run never writes, even on failure
        return None
    items = data["response"].get("store_items") or []
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
    """([(appid, review_count)], upcoming_only) for every game the page can show: games.json
    most-reviewed first, then upcoming.json's unreleased games (review count 0), and the set
    of appids only upcoming.json has (their trailers go to trailers_upcoming.json).

    Ordering is the whole first-pass strategy: the backlog is ~127k games but the
    ones anybody actually hovers are the popular ones, so trailers show up on the
    games that matter within the first run rather than after the full sweep.
    """
    d = load_json(GAMES_FILE, {})
    if d.get("sample"):
        return [], set()
    rows = [(int(g["appid"]), int(g.get("review_count") or 0))
            for g in d.get("games", []) if g.get("appid") is not None]
    if not rows:
        return [], set()
    rows.sort(key=lambda r: -r[1])
    have = {a for a, _ in rows}
    up = load_json(UPCOMING_FILE, {}).get("games") or []
    up_only = {int(g["appid"]) for g in up if isinstance(g, dict) and g.get("appid") is not None
               and int(g["appid"]) not in have}
    return rows + [(a, 0) for a in sorted(up_only)], up_only


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


def save_trailers(hits, prefix, adaptive_count, upcoming_only):
    """Write trailers.json (every game but the upcoming-only ones: part of the page's boot
    load) and trailers_upcoming.json (upcoming-only games: fetched with upcoming.json the
    first time the page shows upcoming games, so they add nothing to the boot load). A game
    that releases moves from the second file to the first on the next save."""
    # Compact separators, not indent=2: this file is machine-generated, browser-facing,
    # and ~100k rows deep — pretty-printing it would roughly triple the bytes on the
    # wire for zero human benefit.
    #
    # `base` is stored ABSOLUTE (host + learned prefix) so the frontend concatenates
    # and nothing else needs to know how it was assembled.
    by_id = lambda m: {str(k): v for k, v in sorted(m.items(), key=lambda kv: int(kv[0]))}
    up = {k: v for k, v in hits.items() if int(k) in upcoming_only}
    rest = {k: v for k, v in hits.items() if int(k) not in upcoming_only}
    for path, rows, extra in (
            # Informational: how many apps also expose DASH/HLS manifests, i.e. how much
            # is on the table if a streaming player is ever worth the weight. Counted per
            # app as each is asked (trailers_state.json `adaptive`), so it fills in over one
            # VERIFY_DAYS cycle; it used to grow by one per check, re-checks included.
            (TRAILERS_FILE, rest, {"adaptive_available": adaptive_count}),
            (TRAILERS_UP_FILE, up, {})):
        path.write_text(json.dumps(
            {"format": "trailers_v2", "generated_at": int(time.time()),
             "base": absolute_base(prefix), "count": len(rows), **extra,
             "trailers": by_id(rows)},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def save_state(misses, checked, adaptive, swept_at):
    by_id = lambda m: {str(k): v for k, v in sorted(m.items(), key=lambda kv: int(kv[0]))}
    STATE_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "swept_at": swept_at,
         "misses": by_id(misses), "checked": by_id(checked),
         "adaptive": sorted(int(a) for a in adaptive)},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def spread_stamp(appid, now):
    """A first `checked` stamp for a hit stored before stamps existed: somewhere in the last
    VERIFY_DAYS, evenly by appid, so their re-checks spread over the window instead of all
    coming due together. (Knuth's multiplicative hash: appids are mostly multiples of 10.)"""
    window = VERIFY_DAYS * 86400
    return now - (int(appid) * 2654435761) % window


def git_checkpoint(msg):
    """Commit trailers.json + trailers_state.json on top of the current main (gitsync.py). A
    run dispatched from a branch is a dry run: gitsync never pushes from a non-main checkout."""
    gitsync.commit(["trailers.json", "trailers_upcoming.json", "trailers_state.json"], msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def select_work(catalog, hits, misses, checked, now):
    """(queue, n_fresh, n_stale, n_verify): appids to query this run. Never-checked first
    (most-reviewed first), then misses past MISS_TTL_DAYS, then hits Steam last confirmed
    VERIFY_DAYS ago or more (both oldest first)."""
    ttl, vttl = MISS_TTL_DAYS * 86400, VERIFY_DAYS * 86400
    fresh, stale, verify = [], [], []
    for appid, _rc in catalog:
        key = str(appid)
        if key in hits:
            at = int(checked.get(key) or 0)
            if now - at >= vttl:
                verify.append((at, appid))
            continue
        seen = misses.get(key)
        if seen is None:
            fresh.append(appid)
        elif (now - int(seen)) >= ttl:
            stale.append((int(seen), appid))
    stale.sort()
    verify.sort()
    return (fresh + [a for _ts, a in stale] + [a for _ts, a in verify],
            len(fresh), len(stale), len(verify))


def apply_batch(batch, items, hits, misses, checked, adaptive, now):
    """Fold one answered batch into the maps. Returns (found, changed, removed, prefix):
    new hits, re-checked hits whose files changed, hits dropped, and the CDN prefix the
    response carried (or None). A hit is dropped only when Steam answered for that game
    with no playable trailer, and not at all when none of the batch's known hits came back
    with one: that is an answer without trailer data, not 50 trailers gone at once."""
    prefix = None
    found = changed = 0
    got, empty = {}, set()
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
            adaptive.add(int(aid))
        else:
            adaptive.discard(int(aid))
        if files:
            got[key] = files
        else:
            # No natively playable asset. Adaptive-only apps land here too: they
            # have a trailer, we just cannot play it without a streaming library.
            empty.add(key)
    known = [str(a) for a in batch if str(a) in hits]
    trust_empty = not known or any(k in got for k in known)
    for key, files in got.items():
        if key in hits:
            changed += hits[key] != files
        else:
            found += 1
        hits[key] = files
        checked[key] = now
        misses.pop(key, None)
    removed = 0
    for key in empty:
        if key in hits:
            if not trust_empty:
                continue                  # keep it unconfirmed; asked again next run
            del hits[key]
            checked.pop(key, None)
            removed += 1
        misses[key] = now
    # Appids the response dropped entirely (delisted, region-locked, not an app): a miss,
    # so it doesn't re-queue every run; a hit keeps its trailer and waits a full window.
    for aid in batch:
        key = str(aid)
        if int(aid) not in seen_ids:
            if key in hits:
                checked[key] = now
            else:
                misses[key] = now
    return found, changed, removed, prefix


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER

    catalog, upcoming_only = load_catalog()
    if not catalog:
        log("games.json missing or sample-only — nothing to do.")
        return

    tdoc = load_json(TRAILERS_FILE, {})
    hits = dict(tdoc.get("trailers") or {})
    hits.update(load_json(TRAILERS_UP_FILE, {}).get("trailers") or {})
    prefix = None                       # learned from the first response that carries it
    sdoc = load_json(STATE_FILE, {})
    misses = dict(sdoc.get("misses") or {})
    checked = {k: int(v) for k, v in (sdoc.get("checked") or {}).items() if k in hits}
    adaptive = {int(a) for a in sdoc.get("adaptive") or []}
    swept_at = sdoc.get("swept_at") or 0
    now = int(time.time())
    for key in hits:
        if key not in checked:
            checked[key] = spread_stamp(key, now)

    queue, n_fresh, n_stale, n_verify = select_work(catalog, hits, misses, checked, now)
    log(f"Catalog {len(catalog)} | have trailers {len(hits)} | known misses {len(misses)} | "
        f"queued {len(queue)} ({n_fresh} new, {n_stale} misses past {MISS_TTL_DAYS}d, "
        f"{n_verify} hits past {VERIFY_DAYS}d) | budget {RUN_MINUTES}min")
    if not queue:
        log("Nothing due.")
        save_trailers(hits, prefix, len(adaptive), upcoming_only)   # moves any game that released
        save_state(misses, checked, adaptive, int(time.time()))
        git_checkpoint("trailers: nothing due")
        return

    last_ckpt = time.time()
    found = checked_n = changed = removed = failed = 0
    for i in range(0, len(queue), GETITEMS_BATCH):
        if time.time() >= deadline:
            log("  time budget reached — wrapping up")
            break
        batch = queue[i:i + GETITEMS_BATCH]
        items = getitems(batch)
        time.sleep(GETITEMS_DELAY)
        if items is None:
            failed += 1                    # no answer: every game keeps what it had
            continue
        f, c, r, learned = apply_batch(batch, items, hits, misses, checked, adaptive,
                                       int(time.time()))
        found, changed, removed = found + f, changed + c, removed + r
        if learned and prefix is None:
            prefix = learned
        checked_n += len(batch)

        if time.time() - last_ckpt >= CHECKPOINT_SECONDS:
            save_trailers(hits, prefix, len(adaptive), upcoming_only)
            save_state(misses, checked, adaptive, swept_at)
            git_checkpoint(f"trailers: {len(hits)} with video ({found} new this run)")
            last_ckpt = time.time()

    if checked_n >= len(queue):
        swept_at = int(time.time())
    save_trailers(hits, prefix, len(adaptive), upcoming_only)
    save_state(misses, checked, adaptive, swept_at)
    git_checkpoint(f"trailers: {len(hits)} with video ({found} new, {changed} changed, "
                   f"{removed} gone this run)")
    log(f"Done. checked {checked_n}, found {found}, changed {changed}, gone {removed}, "
        f"{failed} failed call(s), total with trailers {len(hits)}, misses {len(misses)} | "
        f"{(time.time()-started)/60:.1f}min")


if __name__ == "__main__":
    main()
