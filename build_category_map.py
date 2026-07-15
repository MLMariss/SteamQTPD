#!/usr/bin/env python3
"""
build_category_map.py - Derive category_N -> name from Steam appdetails (ground truth)

Steam doesn't publish a clean public category-ID list, and hardcoding names from
memory is error-prone. But appdetails returns, per game:
    "categories": [{"id": 9, "description": "Co-op"}, ...]
So we sample a set of appids, pull appdetails, and harvest every id->description
pair we see. Union across enough varied games converges on the full map.

Feeds pics_summarize.py (joins PICS `category:{"category_9":"1"}` -> "Co-op").

Run:
    py build_category_map.py --appids pics_raw --out lookups
    py build_category_map.py --top 100 --out lookups
`--appids` accepts a pics_raw dir (reads appids from shards) OR a text file.
Requires open network egress to store.steampowered.com.
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.request

APPDETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english"


def appids_from_pics_raw(path):
    ids = []
    for f in glob.glob(os.path.join(path, "shard_*.json")):
        try:
            doc = json.load(open(f, encoding="utf-8"))
            ids.extend(int(a) for a in doc.get("apps", {}))
        except (json.JSONDecodeError, OSError):
            pass
    return ids


def appids_from_file(path):
    ids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    ids.append(int(line.split(",")[0]))
                except ValueError:
                    pass
    return ids


def get_live_top(n):
    url = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
        return [row["appid"] for row in data.get("response", {}).get("ranks", [])[:n]]
    except Exception as e:
        print(f"  live top failed: {e}", file=sys.stderr)
        return []


def fetch_appdetails(appid):
    url = APPDETAILS.format(appid=appid)
    req = urllib.request.Request(url, headers={"User-Agent": "SteamQTPD-catmap/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    entry = data.get(str(appid), {})
    if not entry.get("success"):
        return None
    return entry.get("data", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appids", help="pics_raw dir OR text file of appids")
    ap.add_argument("--top", type=int, help="also sample live top-N most-played")
    ap.add_argument("--out", default="lookups")
    ap.add_argument("--limit", type=int, default=300, help="max appids to sample")
    ap.add_argument("--sleep", type=float, default=1.5, help="seconds between requests (rate-limit safety)")
    args = ap.parse_args()

    ids = []
    if args.appids:
        if os.path.isdir(args.appids):
            ids.extend(appids_from_pics_raw(args.appids))
        else:
            ids.extend(appids_from_file(args.appids))
    if args.top:
        ids.extend(get_live_top(args.top))
    ids = list(dict.fromkeys(ids))[:args.limit]
    if not ids:
        print("no appids (use --appids and/or --top)", file=sys.stderr)
        sys.exit(2)
    print(f"sampling {len(ids)} appids for category names")

    os.makedirs(args.out, exist_ok=True)
    catmap = {}          # "category_9" -> "Co-op"
    genremap = {}        # "1" -> "Action"  (bonus: appdetails also has genre names)
    ok = 0
    for i, appid in enumerate(ids):
        try:
            data = fetch_appdetails(appid)
        except Exception as e:
            print(f"  {appid}: error {e}", file=sys.stderr)
            time.sleep(args.sleep)
            continue
        if data:
            ok += 1
            for c in data.get("categories", []):
                cid, desc = c.get("id"), c.get("description")
                if cid is not None and desc:
                    catmap[f"category_{cid}"] = desc
            for g in data.get("genres", []):
                gid, desc = g.get("id"), g.get("description")
                if gid and desc:
                    genremap[str(gid)] = desc
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(ids)}  (cats so far: {len(catmap)})")
        time.sleep(args.sleep)  # be polite to the store endpoint

    print(f"\nsampled {ok} games ok; harvested {len(catmap)} categories, {len(genremap)} genres")

    # write category map (authoritative from appdetails)
    cat_path = os.path.join(args.out, "categories.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(catmap, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print(f"wrote {cat_path} ({len(catmap)} entries)")

    # merge/refresh genres too (appdetails is authoritative; only add what we saw)
    gen_path = os.path.join(args.out, "genres.json")
    existing = {}
    if os.path.exists(gen_path):
        try:
            existing = json.load(open(gen_path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    existing.update(genremap)
    with open(gen_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print(f"wrote {gen_path} ({len(existing)} entries, +{len(genremap)} from appdetails)")

    # sorted preview so you can eyeball correctness
    print("\ncategory map preview:")
    for k in sorted(catmap, key=lambda x: int(x.split("_")[1])):
        print(f"  {k:14} {catmap[k]}")


if __name__ == "__main__":
    main()
