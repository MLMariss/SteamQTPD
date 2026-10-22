#!/usr/bin/env python3
"""ea_dates_probe.py — can we know BOTH dates of an Early Access game, before building on it?

READ-ONLY. Writes nothing to the repo, commits nothing, touches no data layer.

Why this exists: Steam's personal calendar lists Every Day We Fight (1546080) on its 1.0 day,
but QTPD only knows it as released — on 10 Jul 2025, its Early Access start. Two dates are
wanted per EA game: (1) when it became playable (EA start) and (2) when the full version
lands (1.0). What we read today cannot give both:
  - appdetails `release_date` and PICS `steam_release_date` hold the EA start only while the
    game is in EA; on 1.0 Steam overwrites it (BG3 / Slay the Spire / Rust / Deep Rock show
    only their 1.0 dates in our data).
  - PICS `original_release_date` is not the EA start (Subnautica's is its 1.0 date).
  - Neither source carries a planned 1.0 date at all.
Valve's StoreItem_ReleaseInfo (SteamDatabase/Protobufs, webui/common.proto) — what GetItems
returns under `include_release` — names the fields that should: `original_steam_release_date`,
`is_early_access`, `release_from_early_access_date` and `release_from_early_access_style`
(the optional "planned 1.0 date" Valve lets EA developers set; it feeds the calendar). Nobody
has seen them come back, and api.steampowered.com is blocked from a dev sandbox — so this runs
on an Actions runner and looks at the real payload. *Dump first, then tighten* (ARCHITECTURE §2.1).

The questions:

  Q1  What does the release block look like for games we know the answer for?
      -> Raw `release` of a fixed set: EA now, left EA (dates known), never EA, upcoming.

  Q2  The planned 1.0 date — how many games currently in EA carry
      `release_from_early_access_date`, at what precision (`_style`), and how many are past?
      -> Decides whether a "1.0 coming" view is worth building, and how to render fuzzy dates.

  Q3  The EA start after 1.0 — does `original_steam_release_date` keep it?
      -> Games tagged Early Access but no longer in genre 70 (left EA), bucketed by 1.0 year,
         to test the claim that Steam only kept EA dates for games that left after ~Oct 2018.
         A never-EA control sample shows what the field means otherwise.

  Q4  Upcoming games that launch INTO Early Access (The Dead We Knew, 4890760) — is that
      flagged before launch, and on which date field?

  Q5  Does `is_early_access` agree with PICS genre 70 (the EA signal the site uses)?

Output: a report on stdout (the run log), the same findings as tables in the job summary,
and every release block under `ea_dates_probe_out/` for upload as a build artifact.

Usage:
    python ea_dates_probe.py
    QTPD_PROBE_APPIDS=1546080,4890760 python ea_dates_probe.py     # extra appids for Q1
    QTPD_PROBE_CONTROL=500 python ea_dates_probe.py                 # never-EA control size
"""

import json
import os
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests

GETITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
BATCH = 50                                                  # same as price_and_sale.py
DELAY = float(os.environ.get("QTPD_PROBE_DELAY", "1.5"))    # matches the other storefront jobs
CONTROL = int(os.environ.get("QTPD_PROBE_CONTROL", "1000")) # never-EA control sample, for Q3
EXTRA = [int(a) for a in re.split(r"[,\s]+", os.environ.get("QTPD_PROBE_APPIDS", "")) if a.isdigit()]
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
TIMEOUT = 40
MAX_RETRIES = 4
OUT_DIR = "ea_dates_probe_out"
EA_TAG = 493                    # store tag "Early Access" (lookups/tags.json); lingers after 1.0
SAME_DAY = 2 * 86400            # two timestamps this close are "the same date" (time zones)

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd ea-dates probe; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Only the release block is wanted; basic info is off to keep the payload lean (the name and
# the item-level is_early_access come back regardless).
DATA_REQUEST = {"include_release": True}

# Q1's fixed set. The "known" dates are the public record, for eyeballing against the payload.
KNOWN = [
    (1546080, "EA now, 1.0 due 22 Oct 2026 (Every Day We Fight)"),
    (4890760, "upcoming, launches INTO EA 22 Oct 2026 (The Dead We Knew)"),
    (4977840, "upcoming, not EA (Otherside Tapes: Favela)"),
    (108600,  "EA since Nov 2013 (Project Zomboid)"),
    (1366540, "EA since Jan 2021 (Dyson Sphere Program)"),
    (1966720, "EA since Oct 2023 (Lethal Company)"),
    (1086940, "left EA: EA Oct 2020 -> 1.0 Aug 2023 (Baldur's Gate 3)"),
    (1794680, "left EA: EA Dec 2021 -> 1.0 Oct 2022 (Vampire Survivors)"),
    (548430,  "left EA: EA Feb 2018 -> 1.0 May 2020 (Deep Rock Galactic)"),
    (646570,  "left EA: EA Nov 2017 -> 1.0 Jan 2019 (Slay the Spire)"),
    (1145360, "left EA: Steam EA Dec 2019 -> 1.0 Sep 2020 (Hades)"),
    (588650,  "left EA: EA May 2017 -> 1.0 Aug 2018 (Dead Cells)"),
    (252490,  "left EA: EA Dec 2013 -> 1.0 Feb 2018 (Rust)"),
    (264710,  "left EA: EA Dec 2014 -> 1.0 Jan 2018 (Subnautica)"),
    (892970,  "left EA in 2026 per our data (Valheim)"),
    (1623730, "left EA in 2026 per our data (Palworld)"),
    (1062090, "left EA in 2026 per our data (Timberborn)"),
    (413150,  "never EA (Stardew Valley)"),
    (2379780, "never EA (Balatro)"),
]

SUMMARY = []      # markdown lines for $GITHUB_STEP_SUMMARY


def log(msg=""):
    print(msg, flush=True)


def hr(title):
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)
    SUMMARY.append(f"\n### {title}\n")


def get(url, *, params=None):
    """Retry/backoff like the other scrapers. Returns (status, json-or-None)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None, None


def getitems(appids):
    payload = {"ids": [{"appid": int(a)} for a in appids],
               "context": {"country_code": "US", "language": "english"},
               "data_request": DATA_REQUEST}
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    status, data = get(GETITEMS, params=params)
    time.sleep(DELAY)
    return status, ((data or {}).get("response") or {}).get("store_items") or []


def fetch(appids, label):
    """{appid: store_item} for every item Steam answered, in batches."""
    out = {}
    ids = list(dict.fromkeys(int(a) for a in appids))
    for i in range(0, len(ids), BATCH):
        status, items = getitems(ids[i:i + BATCH])
        if status != 200:
            log(f"  [{label}] batch {i // BATCH}: HTTP {status}")
        for it in items:
            aid = it.get("appid") or it.get("id")
            if aid:
                out[int(aid)] = it
    log(f"  [{label}] asked {len(ids)}, answered {len(out)}")
    return out


def num(v):
    """GetItems sends uint32s as ints, but tolerate strings."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "—"


def relation(a, b):
    """How timestamp a sits against b: same / earlier / later (None if either is missing)."""
    if not a or not b:
        return None
    if abs(a - b) < SAME_DAY:
        return "same"
    return "earlier" if a < b else "later"


def rel(it):
    return it.get("release") or {}


def is_ea(it):
    """EA per GetItems: the release block's flag, else the item-level one."""
    r = rel(it)
    return bool(r.get("is_early_access") if "is_early_access" in r else it.get("is_early_access"))


def pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "—"


def load_apps(path):
    try:
        return json.load(open(path, encoding="utf-8")).get("apps", {})
    except (OSError, ValueError):
        return {}


def populations():
    """Who to ask, from data already in the repo (pics.json is the site's EA signal)."""
    pics = load_apps("pics.json")
    up = load_apps("pics_upcoming.json")
    ea_now = [int(a) for a, v in pics.items() if v.get("ea")]
    left_ea = [int(a) for a, v in pics.items() if not v.get("ea") and EA_TAG in (v.get("tags") or [])]
    up_ea = [int(a) for a, v in up.items() if v.get("ea")]
    never = [int(a) for a, v in pics.items() if not v.get("ea") and EA_TAG not in (v.get("tags") or [])
             and str(v.get("type", "")).lower() == "game"]
    rnd = random.Random(20261022)
    control = rnd.sample(never, min(CONTROL, len(never)))
    return pics, {"ea_now": ea_now, "left_ea": left_ea, "upcoming_ea": up_ea, "never_ea": control}


def key_census(items):
    c = Counter()
    for it in items.values():
        c.update(k for k, v in rel(it).items() if v not in (None, "", 0, False))
    return c


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = int(time.time())
    pics, pops = populations()
    log(f"populations: " + ", ".join(f"{k} {len(v)}" for k, v in pops.items())
        + f"  (pics.json {len(pics)} apps)")
    dump = {}

    # ---------------------------------------------------------------- Q1
    hr("Q1  The raw release block, for games we know the answer for")
    known = KNOWN + [(a, "extra") for a in EXTRA]
    got = fetch([a for a, _ in known], "known")
    SUMMARY.append("| appid | case | name | steam_release | original_steam | release_from_EA | style "
                   "| is_EA | coming_soon |\n|---|---|---|---|---|---|---|---|---|")
    for aid, note in known:
        it = got.get(aid)
        if not it:
            log(f"\n{aid} {note}: NOT ANSWERED")
            SUMMARY.append(f"| {aid} | {note} | — | not answered | | | | | |")
            continue
        r = rel(it)
        log(f"\n{aid} {note}")
        log(f"  name={it.get('name')!r} success={it.get('success')} visible={it.get('visible')} "
            f"item.is_early_access={it.get('is_early_access')}")
        log(f"  release = {json.dumps(r, ensure_ascii=False)}")
        log(f"  steam_release={day(num(r.get('steam_release_date')))} "
            f"original_release={day(num(r.get('original_release_date')))} "
            f"original_steam_release={day(num(r.get('original_steam_release_date')))} "
            f"release_from_early_access={day(num(r.get('release_from_early_access_date')))} "
            f"style={r.get('release_from_early_access_style')}")
        SUMMARY.append(f"| {aid} | {note} | {it.get('name') or '—'} | {day(num(r.get('steam_release_date')))} "
                       f"| {day(num(r.get('original_steam_release_date')))} "
                       f"| {day(num(r.get('release_from_early_access_date')))} "
                       f"| {r.get('release_from_early_access_style', '—')} | {is_ea(it)} "
                       f"| {r.get('is_coming_soon', '—')} |")
    dump["known"] = {str(a): got.get(a) for a, _ in known}

    # ---------------------------------------------------------------- Q2 + Q5
    hr("Q2  Planned 1.0 date on games currently in Early Access")
    ea_items = fetch(pops["ea_now"], "ea_now")
    dump["ea_now"] = {str(a): {"name": it.get("name"), "is_early_access": it.get("is_early_access"),
                               "release": rel(it)} for a, it in ea_items.items()}
    n = len(ea_items)
    planned = {a: it for a, it in ea_items.items() if num(rel(it).get("release_from_early_access_date"))}
    styles = Counter(rel(it).get("release_from_early_access_style") for it in planned.values())
    future = {a: it for a, it in planned.items() if num(rel(it)["release_from_early_access_date"]) > now}
    log(f"  answered {n}; with release_from_early_access_date {len(planned)} ({pct(len(planned), n)}); "
        f"future {len(future)}, past {len(planned) - len(future)}")
    log(f"  _style values: {dict(styles)}")
    log(f"  release-block keys seen (non-empty): {dict(key_census(ea_items))}")
    SUMMARY.append(f"{n} answered; **{len(planned)} ({pct(len(planned), n)}) carry a planned 1.0 date** "
                   f"— {len(future)} in the future, {len(planned) - len(future)} past.\n")
    SUMMARY.append("| `_style` | games | examples (name — 1.0 date — coming_soon_display / custom message) |\n|---|---|---|")
    for style, cnt in styles.most_common():
        ex = sorted((it for it in planned.values() if rel(it).get("release_from_early_access_style") == style),
                    key=lambda it: num(rel(it)["release_from_early_access_date"]))
        ex = ([it for it in ex if num(rel(it)["release_from_early_access_date"]) > now][:6]
              or ex[-6:])
        log(f"\n  style={style!r}: {cnt}")
        cells = []
        for it in ex:
            r = rel(it)
            line = (f"{it.get('name')} — {day(num(r['release_from_early_access_date']))} — "
                    f"{r.get('coming_soon_display') or '·'} / {r.get('custom_release_date_message') or '·'}")
            log(f"    {it.get('appid') or it.get('id')}: {line}")
            cells.append(line)
        SUMMARY.append(f"| {style} | {cnt} | {'<br>'.join(cells)} |")

    soon = sorted((it for it in future.values()
                   if num(rel(it)["release_from_early_access_date"]) < now + 31 * 86400),
                  key=lambda it: num(rel(it)["release_from_early_access_date"]))
    log(f"\n  1.0 dates in the next 31 days ({len(soon)}) — compare with Steam's calendar:")
    for it in soon[:60]:
        r = rel(it)
        log(f"    {day(num(r['release_from_early_access_date']))}  style={r.get('release_from_early_access_style')}  "
            f"{it.get('appid') or it.get('id')}  {it.get('name')}")
    SUMMARY.append(f"\n1.0 dates in the next 31 days: **{len(soon)}** (listed in the run log).")

    # EA start while in EA: is it the steam_release_date, and does original_steam agree?
    osrd = Counter(relation(num(rel(it).get("original_steam_release_date")),
                            num(rel(it).get("steam_release_date"))) for it in ea_items.values())
    agree = sum(1 for a, it in ea_items.items()
                if relation(num(rel(it).get("steam_release_date")), num(pics.get(str(a), {}).get("released"))) == "same")
    log(f"\n  original_steam_release_date vs steam_release_date (in EA): {dict(osrd)}")
    log(f"  steam_release_date same day as PICS `released`: {agree}/{n}")
    SUMMARY.append(f"\nWhile in EA: `original_steam_release_date` vs `steam_release_date` → "
                   f"{', '.join(f'{k}: {v}' for k, v in osrd.items())}. "
                   f"`steam_release_date` = PICS `released` (same day) for {agree}/{n}.")

    hr("Q5  is_early_access vs PICS genre 70")
    flagged = sum(1 for it in ea_items.values() if is_ea(it))
    log(f"  genre-70 games GetItems calls EA: {flagged}/{n}")
    SUMMARY.append(f"Of {n} genre-70 games, GetItems says `is_early_access` for **{flagged}**.")

    # ---------------------------------------------------------------- Q3
    hr("Q3  EA start after 1.0 — does original_steam_release_date keep it?")
    SUMMARY.append("| cohort | answered | still EA | has original_steam | …earlier than steam_release "
                   "| same | later |\n|---|---|---|---|---|---|---|")
    for name in ("left_ea", "never_ea"):
        items = fetch(pops[name], name)
        dump[name] = {str(a): {"name": it.get("name"), "is_early_access": it.get("is_early_access"),
                               "release": rel(it)} for a, it in items.items()}
        still = sum(1 for it in items.values() if is_ea(it))
        has = [it for it in items.values() if num(rel(it).get("original_steam_release_date"))]
        rels = Counter(relation(num(rel(it).get("original_steam_release_date")),
                                num(rel(it).get("steam_release_date"))) for it in has)
        log(f"  {name}: answered {len(items)}, still EA {still}, original_steam present {len(has)}, "
            f"vs steam_release {dict(rels)}")
        log(f"  release-block keys seen (non-empty): {dict(key_census(items))}")
        SUMMARY.append(f"| {name} | {len(items)} | {still} | {len(has)} | {rels.get('earlier', 0)} "
                       f"| {rels.get('same', 0)} | {rels.get('later', 0)} |")
        if name != "left_ea":
            continue
        # The ~Oct 2018 cutoff claim: share with an earlier original_steam date, by 1.0 year.
        by_year = {}
        for it in items.values():
            if is_ea(it):
                continue
            srd = num(rel(it).get("steam_release_date"))
            y = datetime.fromtimestamp(srd, timezone.utc).year if srd else None
            e = relation(num(rel(it).get("original_steam_release_date")), srd) == "earlier"
            tot, hit = by_year.get(y, (0, 0))
            by_year[y] = (tot + 1, hit + e)
        log("  by 1.0 year (steam_release_date): games / with an earlier original_steam date")
        SUMMARY.append("\nLeft EA, by 1.0 year: games → with an EA start (`original_steam` earlier)\n")
        SUMMARY.append("| 1.0 year | games | with EA start | share |\n|---|---|---|---|")
        for y in sorted(by_year, key=lambda v: (v is None, v)):
            tot, hit = by_year[y]
            log(f"    {y}: {tot} / {hit} ({pct(hit, tot)})")
            SUMMARY.append(f"| {y} | {tot} | {hit} | {pct(hit, tot)} |")
        ex = [it for it in has if relation(num(rel(it).get("original_steam_release_date")),
                                           num(rel(it).get("steam_release_date"))) == "earlier"][:12]
        log("  examples (EA start -> 1.0):")
        for it in ex:
            r = rel(it)
            log(f"    {it.get('appid') or it.get('id')}  {day(num(r['original_steam_release_date']))} -> "
                f"{day(num(r.get('steam_release_date')))}  {it.get('name')}")
        stale = sum(1 for it in items.values() if num(rel(it).get("release_from_early_access_date")))
        log(f"  left-EA games still carrying release_from_early_access_date: {stale}")
        SUMMARY.append(f"\nLeft-EA games still carrying `release_from_early_access_date`: {stale}.")

    # ---------------------------------------------------------------- Q4
    hr("Q4  Upcoming games that launch into Early Access")
    items = fetch(pops["upcoming_ea"], "upcoming_ea")
    dump["upcoming_ea"] = {str(a): {"name": it.get("name"), "is_early_access": it.get("is_early_access"),
                                    "release": rel(it)} for a, it in items.items()}
    soon_flag = sum(1 for it in items.values() if rel(it).get("is_coming_soon"))
    flagged = sum(1 for it in items.values() if is_ea(it))
    dated = sum(1 for it in items.values() if num(rel(it).get("steam_release_date")))
    rfea = sum(1 for it in items.values() if num(rel(it).get("release_from_early_access_date")))
    disp = Counter(rel(it).get("coming_soon_display") for it in items.values())
    log(f"  answered {len(items)}; is_coming_soon {soon_flag}; is_early_access {flagged}; "
        f"steam_release_date {dated}; release_from_early_access_date {rfea}")
    log(f"  coming_soon_display: {dict(disp)}")
    log(f"  release-block keys seen (non-empty): {dict(key_census(items))}")
    for it in list(items.values())[:8]:
        log(f"    {it.get('appid') or it.get('id')}  {it.get('name')}  release={json.dumps(rel(it), ensure_ascii=False)}")
    SUMMARY.append(f"{len(items)} answered: coming soon {soon_flag}, flagged EA {flagged}, "
                   f"with steam_release_date {dated}, with a planned 1.0 date {rfea}. "
                   f"`coming_soon_display`: {', '.join(f'{k}: {v}' for k, v in disp.items())}.")

    with open(os.path.join(OUT_DIR, "ea_dates_probe.json"), "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=1, ensure_ascii=False)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Early Access dates probe\n" + "\n".join(SUMMARY) + "\n")
    log(f"\nraw release blocks -> {OUT_DIR}/ea_dates_probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
