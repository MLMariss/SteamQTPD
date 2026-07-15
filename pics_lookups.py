#!/usr/bin/env python3
"""
pics_lookups.py - Fetch/generate the ID->name decode tables for pics_summarize.py

Produces three static JSON maps (committed, refreshed rarely):
  tags.json       tagid  -> name   (live, from IStoreService/GetTagList)
  genres.json     genreid-> name   (seeded; Steam genre IDs are stable)
  categories.json category_N -> name (seeded; Steam category IDs are stable)

The raw PICS shards store numeric IDs (store_tags:{"0":"113",...},
genres:{"0":"1"}, category:{"category_9":"1"}). The summarizer joins those
against these maps to emit readable names.

Run:
    py pics_lookups.py --out lookups
Requires open network egress to api.steampowered.com (tag list only).
"""

import argparse
import json
import os
import sys
import urllib.request

TAG_LIST_URL = "https://api.steampowered.com/IStoreService/GetTagList/v1/?language=english"

# --- Steam genre IDs (stable; from store.steampowered.com genre routing) ---
# These map the numeric genre IDs seen in PICS `genres` / `primary_genre`.
GENRES = {
    "1": "Action",
    "2": "Strategy",
    "3": "RPG",
    "4": "Casual",
    "9": "Racing",
    "18": "Sports",
    "23": "Indie",
    "25": "Adventure",
    "28": "Simulation",
    "29": "Massively Multiplayer",
    "37": "Free to Play",
    "70": "Early Access",
    "51": "Animation & Modeling",
    "52": "Audio Production",
    "53": "Design & Illustration",
    "54": "Education",
    "55": "Photo Editing",
    "56": "Software Training",
    "57": "Utilities",
    "58": "Video Production",
    "59": "Web Publishing",
    "60": "Game Development",
    "71": "Sexual Content",
    "72": "Nudity",
    "73": "Violent",
    "74": "Gore",
    "81": "Documentary",
    "84": "Tutorial",
}

# --- Steam store category IDs (stable; the category_N feature flags in PICS) ---
CATEGORIES = {
    "category_1": "Multiplayer",
    "category_2": "Single-player",
    "category_6": "Mods (require HL2)",
    "category_8": "Valve Anti-Cheat enabled",
    "category_9": "Co-op",
    "category_10": "Game demo",
    "category_13": "Captions available",
    "category_14": "Commentary available",
    "category_15": "Stats",
    "category_16": "Includes Source SDK",
    "category_17": "Includes level editor",
    "category_18": "Partial Controller Support",
    "category_19": "Mods",
    "category_20": "MMO",
    "category_21": "Downloadable Content",
    "category_22": "Steam Achievements",
    "category_23": "Steam Cloud",
    "category_24": "Shared/Split Screen",
    "category_25": "Steam Leaderboards",
    "category_27": "Cross-Platform Multiplayer",
    "category_28": "Full controller support",
    "category_29": "Steam Trading Cards",
    "category_30": "Steam Workshop",
    "category_31": "VR Support (legacy)",
    "category_32": "Steam Turn Notifications",
    "category_33": "Native Steam Controller",
    "category_35": "In-App Purchases",
    "category_36": "Online PvP",
    "category_37": "Shared/Split Screen PvP",
    "category_38": "Online Co-op",
    "category_39": "Shared/Split Screen Co-op",
    "category_40": "SteamVR Collectibles",
    "category_41": "Remote Play on Phone",
    "category_42": "Remote Play on Tablet",
    "category_43": "Remote Play on TV",
    "category_44": "Remote Play Together",
    "category_45": "Commentary available",
    "category_46": "In-game (Cloud saves)",
    "category_47": "LAN PvP",
    "category_48": "LAN Co-op",
    "category_49": "PvP",
    "category_50": "Additional High-Quality Audio",
    "category_51": "Workshop",
    "category_52": "Tracked Controller Support",
    "category_53": "VR Supported",
    "category_54": "VR Only",
    "category_55": "HDR available",
    "category_56": "Includes Source SDK",
    "category_57": "Includes level editor",
    "category_58": "Family Sharing",
    "category_59": "Steam Timeline",
    "category_60": "Steam China Workshop",
    "category_61": "Hardware",
    "category_62": "Family Sharing",
    "category_63": "Steam China Approved",
    "category_64": "Anti-Cheat (recommended)",
    "category_65": "Native VR",
    "category_66": "Native controller support",
    "category_67": "HDR",
    "category_68": "Ultrawide",
    "category_69": "Native retina",
    "category_70": "Steam Deck compatible feature",
    "category_71": "Steam Deck feature",
    "category_74": "Native display feature",
    "category_75": "Steam Deck feature",
    "category_76": "Remote Play feature",
    "category_77": "Remote Play feature",
    "category_78": "Steam feature",
    "category_79": "Steam feature",
}


def fetch_tags():
    """Live pull of the full tagid->name map from Steam."""
    try:
        req = urllib.request.Request(TAG_LIST_URL, headers={"User-Agent": "SteamQTPD-lookups/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        tags = data.get("response", {}).get("tags", [])
        out = {str(t["tagid"]): t["name"] for t in tags if "tagid" in t and "name" in t}
        print(f"  fetched {len(out)} tags")
        return out
    except Exception as e:
        print(f"  tag fetch failed: {e}", file=sys.stderr)
        return {}


def main():
    ap = argparse.ArgumentParser(description="SteamQTPD lookup-table fetcher")
    ap.add_argument("--out", default="lookups", help="output dir for lookup JSON")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    tags = fetch_tags()
    if not tags:
        print("WARNING: tag list empty; writing genre/category maps only.", file=sys.stderr)

    outputs = {
        "tags.json": tags,
        "genres.json": GENRES,
        "categories.json": CATEGORIES,
    }
    for fname, obj in outputs.items():
        path = os.path.join(args.out, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        print(f"wrote {path} ({len(obj)} entries)")


if __name__ == "__main__":
    main()
