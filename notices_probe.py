#!/usr/bin/env python3
"""notices_probe.py — find where Steam says a game uses Denuvo-style DRM, needs a 3rd-party account,
or runs kernel-level anti-cheat, before building filters on it.

READ-ONLY. Writes nothing to the repo, commits nothing, touches no data layer.

Why this exists: players asked for three filters the Steam store does not offer (owner,
2026-11-02) — "no Denuvo", "no 3rd-party account / launcher" and "no kernel anti-cheat". The
store shows all three as notices on a game's page, but none of them is in anything we hold:
PICS `common` (pics_raw/) carries no such key, and its category 64 — once guessed to be an
anti-cheat flag — sits on Factorio, RimWorld and Slay the Spire. store.steampowered.com is
blocked from a dev sandbox, so this runs on an Actions runner and looks at the real payloads.
*Dump first, then tighten* (ARCHITECTURE §2.1), as demo_probe.py did for the Demo filter.

The questions:

  Q1  Does appdetails carry the notices?
      -> The expected keys are `drm_notice` and `ext_user_account_notice`. scraper.py already
         fetches appdetails for every game it refreshes, so if they are there, keeping them is
         a few lines in build_record(). Every key path whose key OR string value mentions DRM,
         Denuvo, an account, a launcher or anti-cheat is reported, so an unexpected key name
         is found rather than missed.

  Q2  Does the batched GetItems call carry any of it?
      -> 50 games a call instead of one. The widest data_request is tried.

  Q3  Where does the store PAGE state anti-cheat (and the other two)?
      -> Valve added an anti-cheat disclosure to store pages in 2024. Snippets around every
         "anti-cheat" / "kernel" / "DRM" / "account" mention are dumped, tags stripped, so the
         markup to read is visible in the run log.

  Q4  How common is each notice?
      -> A seeded random sample of released games through appdetails. Decides whether a filter
         would list anything with Only, and how long a backfill sweep would take.

Known games are a hand-picked list of titles whose store pages are known to carry each notice,
plus controls that carry none. Extra appids come from QTPD_PROBE_APPIDS.

Output: a report on stdout (the run log), the same findings as a table in the job summary, and
the raw payloads under `notices_probe_out/` for upload as a build artifact.

Usage:
    python notices_probe.py
    QTPD_PROBE_APPIDS=2246340,553850 python notices_probe.py   # extra appids to check
    QTPD_PROBE_SAMPLE=50 python notices_probe.py               # Q4 sample size
"""

import html
import json
import os
import random
import re
import sys
import time

import requests

APPDETAILS = "https://store.steampowered.com/api/appdetails"
GETITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
DELAY = float(os.environ.get("QTPD_PROBE_DELAY", "1.5"))    # matches the other storefront jobs
SAMPLE = int(os.environ.get("QTPD_PROBE_SAMPLE", "100"))
EXTRA = [int(a) for a in re.split(r"[,\s]+", os.environ.get("QTPD_PROBE_APPIDS", "")) if a.isdigit()]
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
TIMEOUT = 40
MAX_RETRIES = 4
OUT_DIR = "notices_probe_out"

# What each game is EXPECTED to show — the probe reports what it actually finds beside it, so a
# wrong expectation is visible rather than silently believed.
KNOWN = {
    # Denuvo (3rd-party DRM)
    990080:  "Hogwarts Legacy — Denuvo",
    1196590: "Resident Evil Village — Denuvo",
    2358720: "Black Myth: Wukong — Denuvo",
    1778820: "TEKKEN 8 — Denuvo",
    2246340: "Monster Hunter Wilds — Denuvo",
    # 3rd-party account / launcher
    1517290: "Battlefield 2042 — EA account, kernel anti-cheat",
    553850:  "HELLDIVERS 2 — PSN account (made optional), anti-cheat",
    271590:  "GTA V — Rockstar launcher/account",
    1174180: "Red Dead Redemption 2 — Rockstar launcher/account",
    2369390: "Far Cry 6 — Ubisoft Connect",
    # Kernel-level / client anti-cheat
    1938090: "Call of Duty — Ricochet (kernel), Activision account",
    1172470: "Apex Legends — Easy Anti-Cheat, EA account",
    1085660: "Destiny 2 — BattlEye",
    578080:  "PUBG — anti-cheat",
    359550:  "Rainbow Six Siege — BattlEye, Ubisoft account",
    1245620: "ELDEN RING — Easy Anti-Cheat",
    2767030: "Marvel Rivals — anti-cheat",
    2507950: "Delta Force — ACE (kernel)",
    2073850: "THE FINALS — Easy Anti-Cheat",
    252490:  "Rust — Easy Anti-Cheat",
    # Controls: none of the three
    1145360: "Hades — control",
    413150:  "Stardew Valley — control",
    427520:  "Factorio — control",
}

# A key or string value matching this is reported wherever it sits in a payload.
HIT = re.compile(r"drm|denuvo|anti.?cheat|anticheat|kernel|easy ?anti|battleye|account|launcher"
                 r"|third.?party|3rd.?party|ext_user", re.I)
# On the store page: the phrases worth a snippet.
PAGE_HIT = re.compile(r"anti[-\s]?cheat|kernel|denuvo|3rd[-\s]party|third[-\s]party|drm", re.I)

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd notices probe; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)

SUMMARY = []      # markdown lines for $GITHUB_STEP_SUMMARY


def log(msg=""):
    print(msg, flush=True)


def hr(title):
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)
    SUMMARY.append(f"\n### {title}\n")


def get(url, *, params=None, want_json=True):
    """Retry/backoff like the other scrapers. Returns (status, json-or-text-or-None)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            if not want_json:
                return r.status_code, r.text
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None, None


def hits(obj, path=""):
    """Every (path, value) under obj whose key, or whose string value, matches HIT. Long free-text
    fields (descriptions) are skipped when only their VALUE matches: "account" turns up in half of
    all game descriptions and would bury the one field that matters."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if HIT.search(str(k)):
                out.append((p, v))
            elif isinstance(v, str) and len(v) < 400 and HIT.search(v):
                out.append((p, v))
            if isinstance(v, (dict, list)):
                out += hits(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += hits(v, f"{path}[{i}]")
    return out


def short(v, n=140):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def appdetails(appid):
    status, data = get(APPDETAILS, params={"appids": appid, "cc": "us", "l": "english"})
    time.sleep(DELAY)
    node = (data or {}).get(str(appid)) or {}
    return status, (node.get("data") if node.get("success") else None)


def save(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def q1(ids):
    hr("Q1  appdetails: drm_notice / ext_user_account_notice / anything anti-cheat")
    SUMMARY.append("| appid | expected | drm_notice | ext_user_account_notice | other hits |\n|---|---|---|---|---|")
    dump, keys = {}, {}
    for a in ids:
        status, d = appdetails(a)
        dump[a] = d
        if d is None:
            log(f"  {a:>8}  no data (HTTP {status})")
            SUMMARY.append(f"| {a} | {KNOWN.get(a, '')} | — no data (HTTP {status}) | | |")
            continue
        found = hits(d)
        for p, _ in found:
            keys[re.sub(r"\[\d+\]", "[]", p)] = keys.get(re.sub(r"\[\d+\]", "[]", p), 0) + 1
        drm, acct = d.get("drm_notice"), d.get("ext_user_account_notice")
        other = [f"`{p}`={short(v, 60)}" for p, v in found
                 if p not in ("drm_notice", "ext_user_account_notice")][:4]
        log(f"  {a:>8}  {d.get('name', '')[:34]:34}  drm={short(drm) if drm else '-'}  "
            f"acct={short(acct) if acct else '-'}")
        for o in other:
            log(f"            {o}")
        SUMMARY.append(f"| {a} | {KNOWN.get(a, '')} | {short(drm) if drm else '—'} | "
                       f"{short(acct) if acct else '—'} | {'<br>'.join(other) or '—'} |")
    save("q1_appdetails.json", dump)
    log("\n  key paths that matched, across all games:")
    SUMMARY.append("\nKey paths that matched, across all games:\n")
    for p, n in sorted(keys.items(), key=lambda kv: -kv[1]):
        log(f"    {n:3}  {p}")
        SUMMARY.append(f"- `{p}` — {n}")
    return dump


def q2(ids):
    hr("Q2  GetItems (widest data_request): any of it, 50 games a call?")
    req = {"include_assets": True, "include_release": True, "include_platforms": True,
           "include_all_purchase_options": True, "include_screenshots": False,
           "include_trailers": False, "include_ratings": True, "include_tag_count": 0,
           "include_reviews": False, "include_basic_info": True,
           "include_supported_languages": False, "include_full_description": False,
           "include_included_items": True, "include_links": True}
    payload = {"ids": [{"appid": int(a)} for a in ids],
               "context": {"country_code": "US", "language": "english"}, "data_request": req}
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    status, data = get(GETITEMS, params=params)
    time.sleep(DELAY)
    items = ((data or {}).get("response") or {}).get("store_items") or []
    save("q2_getitems.json", items)
    found = {}
    for it in items:
        for p, v in hits(it):
            found.setdefault(re.sub(r"\[\d+\]", "[]", p), []).append((it.get("appid"), short(v, 60)))
    log(f"  HTTP {status}, {len(items)} items")
    if not found:
        log("  no matching key or value in any item")
        SUMMARY.append(f"HTTP {status}, {len(items)} items — **nothing** matching DRM / account / anti-cheat.")
    for p, ex in sorted(found.items(), key=lambda kv: -len(kv[1])):
        log(f"  {len(ex):3}  {p}   e.g. {ex[0]}")
        SUMMARY.append(f"- `{p}` — {len(ex)} items, e.g. {ex[0][0]}: {ex[0][1]}")


def q3(ids):
    hr("Q3  store page: where anti-cheat, Denuvo and 3rd-party accounts are stated")
    pages = {}
    for a in ids:
        status, text = get(STORE_PAGE.format(appid=a), want_json=False)
        time.sleep(DELAY)
        text = text or ""
        snips, classes = [], set()
        for m in PAGE_HIT.finditer(text):
            lo, hi = max(0, m.start() - 220), min(len(text), m.end() + 220)
            raw = text[lo:hi]
            classes |= set(re.findall(r'class="([^"]*(?:anti|cheat|drm|notice|account)[^"]*)"', raw, re.I))
            snips.append(short(html.unescape(raw), 200))
            if len(snips) >= 6:
                break
        pages[a] = {"status": status, "classes": sorted(classes), "snippets": snips}
        log(f"  {a:>8}  {KNOWN.get(a, '')[:44]:44}  HTTP {status}, {len(snips)} mentions")
        for c in sorted(classes):
            log(f"            class: {c}")
        for s in snips[:3]:
            log(f"            … {s}")
        SUMMARY.append(f"- **{a}** {KNOWN.get(a, '')}: {len(snips)} mentions"
                       + (f"; classes `{'`, `'.join(sorted(classes))}`" if classes else "")
                       + (f"<br>… {snips[0]}" if snips else ""))
    save("q3_store_pages.json", pages)


def q4():
    hr(f"Q4  how common: a seeded sample of {SAMPLE} released games")
    try:
        with open("games.json", encoding="utf-8") as fh:
            games = [g for g in json.load(fh)["games"] if not g.get("unknown")]
    except (OSError, KeyError, ValueError) as e:
        log(f"  games.json unreadable ({e}); skipped")
        SUMMARY.append("games.json unreadable; skipped.")
        return
    rng = random.Random(20261102)
    pick = rng.sample(games, min(SAMPLE, len(games)))
    n = drm = acct = got = 0
    examples = {"drm": [], "acct": []}
    for g in pick:
        _, d = appdetails(g["appid"])
        n += 1
        if d is None:
            continue
        got += 1
        if d.get("drm_notice"):
            drm += 1; examples["drm"].append((g["appid"], short(d["drm_notice"], 60)))
        if d.get("ext_user_account_notice"):
            acct += 1; examples["acct"].append((g["appid"], short(d["ext_user_account_notice"], 60)))
    log(f"  answered {got}/{n};  drm_notice {drm};  ext_user_account_notice {acct}")
    for k, ex in examples.items():
        for e in ex[:5]:
            log(f"    {k}: {e}")
    SUMMARY.append(f"Answered {got}/{n}. `drm_notice` on **{drm}**, `ext_user_account_notice` on **{acct}**.")
    save("q4_sample.json", {"answered": got, "asked": n, "examples": examples})


def main():
    ids = list(KNOWN) + [a for a in EXTRA if a not in KNOWN]
    log(f"notices_probe: {len(ids)} known games, sample {SAMPLE}, delay {DELAY}s")
    q1(ids)
    q2(ids)
    q3(ids)
    q4()
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("## Store notices probe\n" + "\n".join(SUMMARY) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
