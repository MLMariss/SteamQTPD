#!/usr/bin/env python3
"""
pics_summarize.py - Layer 2 derive: pics_raw/ -> pics.json shards (frontend view)

Reads the raw PICS shards, projects each game into a lean, typed, index-friendly
record, and writes sharded pics.json for the frontend to merge by appid.

Design (spec §4.5, Option B): store IDs, NOT decoded names. Ranked order of
store_tags is preserved. The frontend loads tags/genres/categories maps once and
builds filter indexes from these IDs. This script does NOT decode to names.

Derived fields computed here:
  - family_share_excluded  (bool)  from exfgls presence  (spec §2.4)
  - has_custom_eula        (bool)  from eulas presence   (spec §2.3)
  - ai_content_type        (int)   0=none, 1=pre-gen, 2=live-gen  (spec §2.1)
  - review_bombed          (bool)  raw vs de-bombed score divergence (spec §2.2)
  - langs / audio          (lists) collapsed supported_languages (spec §4.1)
  - deck                   (obj)   already nested-trimmed in Layer 1

Run:
    py pics_summarize.py --in pics_raw --out pics
"""

import argparse
import glob
import json
import os

SHARD_COUNT = 64
FORMAT = "pics_v2"
# lean array/object contract, documented in the output header
FORMAT_DOC = {
    "tags": "ranked tag IDs (decode via tags.json)",
    "genres": "genre IDs (decode via genres.json); pgenre=primary",
    "cats": "category feature IDs as ints (decode via categories.json)",
    "rev": "[score_1_9, pct_positive]",
    "rev_bomb": "[debombed_score, debombed_pct] present only if review-bombed",
    "deck": "{cat, os, machine, tested_ts, online_solo?, hdr?}",
    "ai": "0=none 1=pre-generated 2=live-generated",
    "fse": "family_share_excluded (bool)",
    "eula": "has_custom_eula (bool)",
    "langs": "supported language codes",
    "audio": "language codes with full audio",
    "content_desc": "mature-content codes (1=violence 2=gore 3=mature 4=nudity/sexual 5=container)",
    "controller": "full / partial (from cats 28/18)",
    "state": "released / prerelease",
    # --- v2 derived filter flags (computed once here so the frontend never re-derives) ---
    "ea": "Early Access (bool) = genre-70 present. Valve signal, NOT the SteamSpy tag.",
    "adult": "adult gate (bool) = content_desc code 3 or 4. Replaces the ADULT_TAGS heuristic; excludes code 1 (over-flags AAA).",
    "vr_only": "VR Only (bool) = cats category 54.",
}

# Genre / category IDs used by the derived flags (keep in one place).
GENRE_EARLY_ACCESS = 70
CONTENT_DESC_ADULT_ONLY_SEXUAL = 3   # "Adult Only Sexual Content"
CONTENT_DESC_FREQUENT_NUD_SEXUAL = 4 # "Frequent Nudity or Sexual Content"
CAT_VR_ONLY = 54
# NB: content_desc code 1 ("Some Nudity/Sexual") and code 5 ("General Mature",
# a container present on ~all classified games) are DELIBERATELY excluded — code 1
# over-flags mainstream titles (Witcher 3, Baldur's Gate 3, Cyberpunk) exactly like
# the old ADULT_TAGS heuristic did. Codes 3+4 target the true adult catalog with no
# AAA false positives.


def shard_of(appid: int) -> int:
    return (appid // 10) % SHARD_COUNT


def ids_from_indexed(obj) -> list:
    """Steam stores ordered lists as {"0":v,"1":v,...}; return values in order."""
    if not isinstance(obj, dict):
        return []
    try:
        return [obj[k] for k in sorted(obj, key=lambda x: int(x))]
    except (ValueError, TypeError):
        return list(obj.values())


def cat_ids(category) -> list:
    """category:{"category_9":"1",...} -> [9, ...] as ints."""
    out = []
    if isinstance(category, dict):
        for k in category:
            if k.startswith("category_"):
                try:
                    out.append(int(k.split("_", 1)[1]))
                except ValueError:
                    pass
    return out


def collapse_languages(sl):
    """supported_languages structs -> (supported list, full_audio list)."""
    langs, audio = [], []
    if isinstance(sl, dict):
        for code, meta in sl.items():
            langs.append(code)
            if isinstance(meta, dict) and str(meta.get("full_audio", "")).lower() in ("1", "true"):
                audio.append(code)
    return langs, audio


def summarize_deck(deck):
    """Already nested-trimmed in Layer 1; project to short keys."""
    if not isinstance(deck, dict):
        return None
    out = {}
    if "category" in deck:
        out["cat"] = _int(deck["category"])
    if "steamos_compatibility" in deck:
        out["os"] = _int(deck["steamos_compatibility"])
    if "steam_machine_compatibility" in deck:
        out["machine"] = _int(deck["steam_machine_compatibility"])
    if "test_timestamp" in deck:
        out["tested_ts"] = _int(deck["test_timestamp"])
    if "requires_internet_for_singleplayer" in deck:
        out["online_solo"] = deck["requires_internet_for_singleplayer"] in ("1", 1, "true")
    if "hdr_support" in deck:
        out["hdr"] = _int(deck["hdr_support"])
    return out or None


def _int(v, default=None):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def summarize_game(appid: int, rec: dict) -> dict:
    g = {}

    if "name" in rec:
        g["name"] = rec["name"]
    if "type" in rec:
        g["type"] = rec["type"]

    # --- IDs (kept, not decoded); ranked order preserved for tags ---
    tags = ids_from_indexed(rec.get("store_tags"))
    if tags:
        g["tags"] = [_int(t, t) for t in tags]
    genres = ids_from_indexed(rec.get("genres"))
    if genres:
        g["genres"] = [_int(x, x) for x in genres]
    if "primary_genre" in rec:
        g["pgenre"] = _int(rec["primary_genre"], rec["primary_genre"])
    cats = cat_ids(rec.get("category"))
    if cats:
        g["cats"] = cats

    # --- reviews + bomb detection (spec §2.2) ---
    rs = _int(rec.get("review_score"))
    rp = _int(rec.get("review_percentage"))
    if rs is not None or rp is not None:
        g["rev"] = [rs, rp]
    rsb = _int(rec.get("review_score_bombs"))
    rpb = _int(rec.get("review_percentage_bombs"))
    if rsb is not None and (rsb != rs or rpb != rp):
        g["rev_bomb"] = [rsb, rpb]
        g["review_bombed"] = True

    # --- Steam Deck (already trimmed in Layer 1) ---
    deck = summarize_deck(rec.get("steam_deck_compatibility"))
    if deck:
        g["deck"] = deck

    # --- AI disclosure (spec §2.1): 0 none / 1 pre-gen / 2 live-gen ---
    g["ai"] = _int(rec.get("aicontenttype"), 0)

    # --- derived booleans ---
    g["fse"] = "exfgls" in rec                     # family_share_excluded
    g["eula"] = "eulas" in rec                     # has_custom_eula

    # --- associations (dev/publisher/franchise), kept structured but compact ---
    assoc = rec.get("associations")
    if isinstance(assoc, dict):
        devs, pubs, franch = [], [], []
        for a in ids_from_indexed(assoc):
            if not isinstance(a, dict):
                continue
            t, name = a.get("type"), a.get("name")
            if not name:
                continue
            if t == "developer":
                devs.append(name)
            elif t == "publisher":
                pubs.append(name)
            elif t == "franchise":
                franch.append(name)
        if devs:
            g["dev"] = devs
        if pubs:
            g["pub"] = pubs
        if franch:
            g["franchise"] = franch

    # --- languages (collapsed, spec §4.1) ---
    langs, audio = collapse_languages(rec.get("supported_languages"))
    if langs:
        g["langs"] = langs
    if audio:
        g["audio"] = audio

    # --- misc scalars worth surfacing ---
    for src, dst in (("steam_release_date", "released"),
                     ("original_release_date", "orig_released"),
                     ("metacritic_score", "mc"),
                     ("releasestate", "state"),
                     ("store_asset_mtime", "asset_mtime"),
                     ("controller_support", "controller")):
        if src in rec:
            v = rec[src]
            g[dst] = _int(v, v) if src.endswith(("date", "score", "mtime")) else v

    # content descriptors (mature flags) as int list
    cd = ids_from_indexed(rec.get("content_descriptors"))
    if cd:
        g["content_desc"] = [_int(x, x) for x in cd]

    # --- v2 derived filter flags (emit only when True; sparse, index-once friendly) ---
    genre_ids = g.get("genres") or []
    cat_ids_list = g.get("cats") or []
    cd_codes = g.get("content_desc") or []

    if GENRE_EARLY_ACCESS in genre_ids:
        g["ea"] = True
    # Adult gate: PICS content descriptor codes 3 (Adult Only Sexual) or 4 (Frequent
    # Nudity/Sexual). Deliberately NOT code 1 (over-flags Witcher 3 / BG3 / Cyberpunk)
    # and NOT user "Sexual Content" tags. Targets the true adult catalog, no AAA noise.
    if (CONTENT_DESC_ADULT_ONLY_SEXUAL in cd_codes
            or CONTENT_DESC_FREQUENT_NUD_SEXUAL in cd_codes):
        g["adult"] = True
    if CAT_VR_ONLY in cat_ids_list:
        g["vr_only"] = True

    return g


def read_raw_shard(in_dir, shard):
    path = os.path.join(in_dir, f"shard_{shard:02d}.json")
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8")).get("apps", {})
    except (json.JSONDecodeError, OSError):
        return {}


def write_shard(out_dir, shard, apps):
    path = os.path.join(out_dir, f"shard_{shard:02d}.json")
    tmp = path + ".tmp"
    doc = {"_format": FORMAT, "_shard": shard, "_doc": FORMAT_DOC, "apps": apps}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", default="pics_raw")
    ap.add_argument("--out", dest="out_dir", default="pics")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    total = 0
    ai_n = bomb_n = 0
    for shard in range(SHARD_COUNT):
        raw = read_raw_shard(args.in_dir, shard)
        if not raw:
            continue
        out = {}
        for aid_str, rec in raw.items():
            try:
                appid = int(aid_str)
            except ValueError:
                continue
            g = summarize_game(appid, rec)
            out[aid_str] = g
            total += 1
            if g.get("ai"):
                ai_n += 1
            if g.get("review_bombed"):
                bomb_n += 1
        if out:
            write_shard(args.out_dir, shard, out)

    print(f"summarized {total} games -> {args.out_dir}/")
    print(f"  {ai_n} with AI disclosure, {bomb_n} review-bombed")


if __name__ == "__main__":
    main()
