#!/usr/bin/env python3
"""
pics_refresh.py — SteamQTPD PICS metadata scraper (Layer 1 / raw archive)

Opens ONE anonymous Steam CM session, batch-fetches the PICS `common` app-info
block for a list of appids, trims confirmed-junk fields at ingest, and writes
the trimmed blocks (plus a per-game `_ts` fetch timestamp) into the 64 shards of
pics_raw/ — the PERMANENT Layer 1 archive (PICS_METADATA_PIPELINE.md §4.2), not
scratch. Everything downstream re-derives from it without re-scraping.

Shape (see spec §4.4, "as built"):
  - SEQUENTIAL, single-threaded; batched by CHUNK appids per get_product_info
    call, NOT parallel and NOT partitioned by shard
  - results accumulate in `pending`, then flush() writes only TOUCHED shards
    (atomic temp + os.replace) on the checkpoint interval and at the end
  - shard key = (appid // 10) % 64  (existing SteamQTPD invariant)

One-writer-per-file: this script is the sole writer of pics_raw/shard_NN.json.
It holds trivially — one process, no concurrency. The spec's older two-phase
"fetchers write scratch, a partition step writes shards" design was never built;
it would only be needed if fetching ever goes parallel.

Env / deps (see spec §8):
  pip install steam gevent gevent-eventemitter "protobuf<3.21"
  # or set PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
Requires open network egress to Steam CM + api.steampowered.com.

Usage:
  python pics_refresh.py --appids appids.txt
  python pics_refresh.py --top 500                # pull live top-N as the list
  python pics_refresh.py --appids ids.txt --out ./data/pics_raw --chunk 150
  python pics_refresh.py --appids ids.txt --stale-days 30   # incremental: skip
                                                            # games fetched < N days ago
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import gevent  # hard wall-clock watchdog around CM calls (see fetch_chunk)

SHARD_COUNT = 64

# Fields dropped at ingest (spec §2.5 tier-0 junk). Everything else is kept
# verbatim so the summarizer can derive from a faithful trimmed archive.
DROP_KEYS = {
    "clienticon", "clienttga", "clienticns", "linuxclienticon",
    "logo", "logo_small", "gameid", "controllertagwizard",
    "icon", "small_capsule",
    "library_assets", "library_assets_full",
    "community_hub_visible", "community_visible_stats",
    "name_localized", "name_linux",
    "languages",   # redundant: strict keyset subset of supported_languages
    # store art / client plumbing already sourced elsewhere
}
# NOT dropped (was, until the store-art fix): `header_image`. The old rationale here
# claimed store art was "already sourced elsewhere" — it isn't. index.html only ever
# DERIVED art from the appid ({CDN}/{appid}/header.jpg), which 404s for every game on
# Steam's store_item_assets scheme, where the path carries a per-asset SHA1 that cannot
# be computed from the appid. Measured: capsule_231x87.jpg 404s even for games whose
# header.jpg works, and e.g. Bookshop Simulator (3467040) ships only
# `<sha1>/header_alt_assets_1.jpg` — no plain header.jpg at all, so it rendered broken.
# PICS `header_image` is a per-language dict of ready-to-use "<sha1>/<file>" or bare
# "<file>" values, which is exactly what we need; summarize picks one (see `hdr`).

# --- Steam Deck compatibility nested-trim (spec §3, evidence: 41% of payload) ---
# Keep the filterable top-level scalars; drop the ~1.4 KB of per-test display
# detail (tests / steam_machine_tests / steamos_tests) and most of configuration.
# Option B: also lift two genuinely-filterable flags out of `configuration`.
DECK_KEEP_SCALARS = {
    "category",                    # 1/2/3 = unknown/playable/verified (the main filter)
    "steamos_compatibility",       # SteamOS support level
    "steam_machine_compatibility", # Steam Machine support level
    "test_timestamp",              # when tested (staleness signal)
}
DECK_CONFIG_LIFT = {
    "requires_internet_for_singleplayer",  # always-online-even-solo filter
    "hdr_support",                          # HDR filter
}


def nested_trim(rec: dict) -> dict:
    """Reduce steam_deck_compatibility to filterable fields only (in place)."""
    deck = rec.get("steam_deck_compatibility")
    if isinstance(deck, dict):
        slim = {k: v for k, v in deck.items() if k in DECK_KEEP_SCALARS}
        cfg = deck.get("configuration")
        if isinstance(cfg, dict):
            for k in DECK_CONFIG_LIFT:
                if k in cfg:
                    slim[k] = cfg[k]
        rec["steam_deck_compatibility"] = slim
    return rec

SCHEMA = "pics_raw_v2"  # v2: dropped `languages`, nested-trimmed steam_deck_compatibility


def shard_of(appid: int) -> int:
    return (appid // 10) % SHARD_COUNT


def trim_common(common: dict) -> dict:
    return {k: v for k, v in common.items() if k not in DROP_KEYS}


def load_appids(args) -> list:
    """Resolve the appid worklist from --appids, --catalog, and/or --top."""
    ids = []
    if args.appids:
        with open(args.appids, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        ids.append(int(line.split(",")[0]))
                    except ValueError:
                        pass
    if args.catalog:
        ids.extend(load_catalog_appids(args.catalog))
    if args.top:
        ids.extend(get_live_top_appids(args.top))
    # dedupe, preserve order
    return list(dict.fromkeys(ids))


def load_catalog_appids(path: str) -> list:
    """Read appids from the canonical catalog/games JSON (list, dict, or
    {"apps": {...}} / {"games": [...]} shapes are all tolerated)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  catalog load failed ({e})", file=sys.stderr)
        return []
    ids = []
    if isinstance(data, dict):
        # {"12345": {...}} or {"apps": {"12345": ...}} or {"games": [...]}
        container = data.get("apps") or data.get("games") or data
        if isinstance(container, dict):
            for k in container:
                try:
                    ids.append(int(k))
                except (ValueError, TypeError):
                    pass
        elif isinstance(container, list):
            for row in container:
                aid = row.get("appid") if isinstance(row, dict) else row
                try:
                    ids.append(int(aid))
                except (ValueError, TypeError):
                    pass
    elif isinstance(data, list):
        for row in data:
            aid = row.get("appid") if isinstance(row, dict) else row
            try:
                ids.append(int(aid))
            except (ValueError, TypeError):
                pass
    print(f"  catalog: {len(ids)} appids from {path}")
    return ids


def get_live_top_appids(n: int) -> list:
    import urllib.request
    url = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
        ranks = data.get("response", {}).get("ranks", [])
        out = [row["appid"] for row in ranks[:n]]
        print(f"  live top pull: {len(out)} appids")
        return out
    except Exception as e:
        print(f"  live top pull failed: {e}", file=sys.stderr)
        return []


def read_existing_shard(out_dir: str, shard: int) -> dict:
    path = os.path.join(out_dir, f"shard_{shard:02d}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("apps", {})
    except (json.JSONDecodeError, OSError):
        return {}


def write_shard(out_dir: str, shard: int, apps: dict):
    """Sole writer of shard_NN.json. Atomic via temp + replace."""
    path = os.path.join(out_dir, f"shard_{shard:02d}.json")
    tmp = path + ".tmp"
    doc = {
        "_schema": SCHEMA,
        "_shard": shard,
        "_updated": datetime.now(timezone.utc).isoformat(),
        "apps": apps,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def ensure_logged_on(client) -> bool:
    """Re-establish the anonymous CM session if it dropped. Retrying a call on a
    dead socket just hangs again, so recovery means a fresh login, not a repeat.
    Returns True once logged on."""
    if client.logged_on:
        return True
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.anonymous_login()
    except Exception as e:
        print(f"  reconnect failed: {e}", file=sys.stderr)
    return bool(client.logged_on)


def fetch_chunk(client, chunk, timeout):
    """`get_product_info` wrapped in a hard `gevent.Timeout` a few seconds beyond
    the library's own timeout. The library timeout does not fire when the CM
    socket is dead, so without this backstop one wedged call freezes the whole
    run indefinitely (and defeats the --run-minutes budget). Raises
    gevent.Timeout on wedge, or the underlying exception on other errors."""
    with gevent.Timeout(timeout + 5):
        return client.get_product_info(apps=chunk, timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="SteamQTPD PICS raw metadata scraper")
    ap.add_argument("--appids", help="file of appids (one per line, # comments ok)")
    ap.add_argument("--catalog", help="canonical catalog/games JSON to read appids from")
    ap.add_argument("--top", type=int, help="also pull live top-N most-played")
    ap.add_argument("--out", default="pics_raw", help="output dir for raw shards")
    ap.add_argument("--chunk", type=int, default=150, help="appids per PICS call")
    ap.add_argument("--stale-days", type=float, default=0,
                    help="skip games whose stored fetch ts is younger than N days (0=refresh all)")
    ap.add_argument("--run-minutes", type=float, default=0,
                    help="time budget: stop fetching after N minutes, checkpoint-committing (0=unbounded)")
    ap.add_argument("--checkpoint-min", type=float, default=10,
                    help="write shards to disk every N minutes during a budgeted run")
    ap.add_argument("--timeout", type=int, default=60, help="per-call PICS timeout (s)")
    args = ap.parse_args()

    # protobuf shim (spec §8) — set before importing steam if not already set
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    from steam.client import SteamClient  # noqa: E402

    worklist = load_appids(args)
    if not worklist:
        print("no appids to fetch (use --appids, --catalog and/or --top)", file=sys.stderr)
        sys.exit(2)
    print(f"worklist: {len(worklist)} appids")

    os.makedirs(args.out, exist_ok=True)

    # --- incremental skip: preload existing shard data, drop fresh games ---
    now = time.time()
    existing_by_shard = {}
    if args.stale_days > 0:
        cutoff = now - args.stale_days * 86400
        skip = set()
        for shard in range(SHARD_COUNT):
            existing_by_shard[shard] = read_existing_shard(args.out, shard)
            for aid_str, rec in existing_by_shard[shard].items():
                if rec.get("_ts", 0) >= cutoff:
                    skip.add(int(aid_str))
        before = len(worklist)
        worklist = [a for a in worklist if a not in skip]
        print(f"  incremental: skipped {before - len(worklist)} fresh games "
              f"(< {args.stale_days}d), {len(worklist)} to fetch")
    else:
        for shard in range(SHARD_COUNT):
            existing_by_shard[shard] = read_existing_shard(args.out, shard)

    if not worklist:
        print("nothing stale to fetch; done.")
        return

    # --- fetch phase (time-budgeted, checkpoint-flushing) ---
    client = SteamClient()
    client.anonymous_login()
    if not client.logged_on:
        print("anonymous login failed", file=sys.stderr)
        sys.exit(1)
    print("logged in anonymously")

    start = time.time()
    budget = args.run_minutes * 60 if args.run_minutes > 0 else None
    checkpoint = args.checkpoint_min * 60
    last_flush = start

    fetched_total = 0
    pending = {}  # appid -> rec, not yet flushed to shards
    total = len(worklist)

    def flush(pending_map):
        """Merge pending records into shards and write touched shards (sole writer)."""
        touched = set()
        for appid, rec in pending_map.items():
            shard = shard_of(appid)
            existing_by_shard[shard][str(appid)] = rec
            touched.add(shard)
        for shard in sorted(touched):
            write_shard(args.out, shard, existing_by_shard[shard])
        return len(touched)

    stopped_early = False
    for i in range(0, total, args.chunk):
        if budget is not None and (time.time() - start) >= budget:
            print(f"  time budget reached ({args.run_minutes} min); stopping at {i}/{total}")
            stopped_early = True
            break
        chunk = worklist[i:i + args.chunk]
        resp = None
        for attempt in (1, 2):
            try:
                resp = fetch_chunk(client, chunk, args.timeout)
                break
            except gevent.Timeout:
                print(f"  chunk {i}: timed out after ~{args.timeout}s "
                      f"(attempt {attempt})", file=sys.stderr)
            except Exception as e:
                print(f"  chunk {i}: fetch error {e} (attempt {attempt})",
                      file=sys.stderr)
            # Recover before retrying: a dropped CM session needs a fresh login,
            # not another call on the same dead socket.
            if attempt == 1:
                if not ensure_logged_on(client):
                    print(f"  chunk {i}: session down, reconnect failed; skipping",
                          file=sys.stderr)
                    break
                time.sleep(3)
        if resp is None:
            # A hang/skip must not defeat the time budget: re-check it here so a
            # run of dead chunks still stops on schedule instead of wedging.
            if budget is not None and (time.time() - start) >= budget:
                print(f"  time budget reached during recovery; stopping at {i}/{total}")
                stopped_early = True
                break
            continue
        apps = resp.get("apps", {})
        ts = int(time.time())
        for appid in chunk:
            app = apps.get(appid)
            if app and "common" in app:
                rec = nested_trim(trim_common(app["common"]))
                rec["_ts"] = ts
                pending[appid] = rec
                fetched_total += 1
        print(f"  fetched {min(i + args.chunk, total)}/{total}")

        # periodic checkpoint so a crash / runner interruption loses <=1 interval
        if (time.time() - last_flush) >= checkpoint and pending:
            n = flush(pending)
            print(f"  checkpoint: flushed {len(pending)} games to {n} shards")
            pending = {}
            last_flush = time.time()

    try:
        client.logout()
    except Exception:
        pass

    # final flush of anything still pending
    if pending:
        n = flush(pending)
        print(f"final flush: {len(pending)} games to {n} shards")
    print(f"got {fetched_total} common blocks{' (partial run)' if stopped_early else ''}")

    # tiny run summary
    ai = sum(
        1 for shard in existing_by_shard.values()
        for r in shard.values()
        if isinstance(r, dict) and "aicontenttype" in r
    )
    print(f"summary: {fetched_total} games this run; {ai} games with aicontenttype in archive")


if __name__ == "__main__":
    main()
