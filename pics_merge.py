#!/usr/bin/env python3
"""
pics_merge.py — merge the 64 summarized pics/ shards into ONE slim pics.json
for the frontend.

Why this exists
---------------
`pics/` is 64 sharded files (the write-once-per-writer archive layer). The
frontend loads every data file as a single merged JSON keyed by appid (games/
prices/tags/... all follow this shape). Rather than teach the client to fetch
64 shards (64 requests, 64 JSON.parse calls, brittle Promise.all), we merge to
one file here — same load pattern as every other data file.

Slim cut (transfer optimization)
--------------------------------
The frontend only consumes a subset of the summarized record. The parked,
backend-only fields (dev / pub / franchise / langs / audio / orig_released /
rev_bomb / asset_mtime / raw content_desc codes) are DROPPED from this merged
file — they stay available in `pics/` for future backend work, but shipping them
to every browser is pure waste. Measured: full merge 12.4 MB gz -> slim 7.8 MB
gz (~37% smaller). The adult/ea/vr_only booleans are pre-derived by
pics_summarize.py, so the raw content_desc / genre lists aren't needed client-side
for filtering.

Companies side file
-------------------
dev / pub stay out of pics.json, but they are ALSO written, interned, to a separate
`companies.json` that index.html fetches only when someone uses the Publisher / dev
filter (typed, or a shared link carrying `co=`). Every other visitor never downloads
it. Shape: {"names": [...], "pub": {appid: [name_idx, ...]}, "dev": {...}} — one
name table shared by both roles, so a studio that self-publishes is stored once.
~2.7 MB gz for ~130k games, against ~5 MB gz if the names were repeated inline.

Pattern match: analog of the other *_summarize -> single-file steps. One writer
(this script) -> one output file (`pics.json`). Reads pics/ read-only.

Usage:
    py pics_merge.py --in pics --out pics.json
"""

import argparse
import glob
import json
import os

SHARD_COUNT = 64
OUT_FORMAT = "pics_v2_frontend"

# The ONLY keys shipped to the browser. Everything else in the summarized record
# is parked backend-only and dropped here. Keep this list in sync with what
# index.html actually reads.
FRONTEND_KEYS = frozenset({
    "name", "type",
    "tags", "genres", "pgenre", "cats",   # decode via lookup maps client-side
    "rev",                                 # [score_1_9, pct]
    "deck",                                # {cat, os, machine, ...}
    "ai",                                  # 0/1/2
    "fse", "eula",                         # family-share excluded / custom EULA
    "controller",                          # full / partial
    "state",                               # released / prerelease
    "released", "mc",                      # scalars
    "ea", "adult", "vr_only",              # derived filter flags (pics_v2)
    "art",                                 # store header path; index.html builds the URL
})


def shard_path(in_dir, shard):
    return os.path.join(in_dir, f"shard_{shard:02d}.json")


def merge(in_dir):
    apps = {}
    companies = {"pub": {}, "dev": {}}
    missing = []
    for shard in range(SHARD_COUNT):
        path = shard_path(in_dir, shard)
        if not os.path.exists(path):
            missing.append(shard)
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        shard_apps = data.get("apps", data) if isinstance(data, dict) else {}
        for appid, rec in shard_apps.items():
            if not isinstance(rec, dict):
                continue
            slim = {k: rec[k] for k in FRONTEND_KEYS if k in rec}
            apps[appid] = slim
            for role in ("pub", "dev"):
                names = [n for n in rec.get(role) or [] if isinstance(n, str) and n.strip()]
                if names:
                    companies[role][appid] = names
    return apps, companies, missing


def intern_companies(companies):
    """{role: {appid: [name]}} -> {names, pub: {appid: [idx]}, dev: {appid: [idx]}}.
    Names sorted so the table (and the committed diff) is stable run to run."""
    names = sorted({n for role in companies.values() for ns in role.values() for n in ns})
    idx = {n: i for i, n in enumerate(names)}
    out = {"_format": "companies_v1", "names": names}
    for role, by_app in companies.items():
        out[role] = {a: [idx[n] for n in ns] for a, ns in sorted(by_app.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)}
    return out


def write_json(path, obj):
    # Compact separators — these files are transfer-optimized, not hand-read.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", default="pics")
    ap.add_argument("--out", dest="out_path", default="pics.json")
    ap.add_argument("--companies-out", dest="companies_path", default="companies.json")
    args = ap.parse_args()

    apps, companies, missing = merge(args.in_dir)

    out = {
        "_format": OUT_FORMAT,
        "count": len(apps),
        "apps": apps,
    }
    write_json(args.out_path, out)
    size_mb = os.path.getsize(args.out_path) / 1024 / 1024
    print(f"merged {len(apps):,} games -> {args.out_path} ({size_mb:.1f} MB raw)")

    co = intern_companies(companies)
    write_json(args.companies_path, co)
    size_mb = os.path.getsize(args.companies_path) / 1024 / 1024
    print(f"  {len(co['names']):,} publisher/developer names "
          f"({len(co['pub']):,} games with pub, {len(co['dev']):,} with dev) "
          f"-> {args.companies_path} ({size_mb:.1f} MB raw)")
    if missing:
        print(f"  WARNING: {len(missing)} shard(s) missing: {missing}")


if __name__ == "__main__":
    main()
