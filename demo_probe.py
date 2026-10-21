#!/usr/bin/env python3
"""demo_probe.py — find out where Steam says a game has a demo, before building on it.

READ-ONLY. Writes nothing to the repo, commits nothing, touches no data layer.

Why this exists: the "has a demo" filter needs one field per game, and the cheapest
candidate is the batched IStoreBrowseService/GetItems call price_and_sale.py already
makes (50 games per call). Its StoreItem format has a `related_items` block with
demo appids, but nobody has seen it come back from the requests we send, and
api.steampowered.com / store.steampowered.com are blocked from a dev sandbox — so
this runs on an Actions runner and looks at the real payload. *Dump first, then
tighten* (ARCHITECTURE §2.1).

The questions:

  Q1  Which GetItems request returns demo appids at all?
      -> Three data_request variants: exactly what price_and_sale.py sends, basic info
         only, and every include_* flag on. Every key path containing "demo" (or
         related_items) is reported per variant, with an example value.

  Q2  Does full appdetails carry `demos` for the same games?
      -> The fallback source (scraper.py already fetches appdetails per game; it would be
         one line there). One call per known-demo game.

  Q3  How common are demos — released vs upcoming (dated / TBA)?
      -> A seeded random sample of each, through the best Q1 variant. Decides whether
         the filter is worth a whole job, and whether "Only" would list anything.

  Q4  Are the demo appids live?
      -> GetItems on the demo appids themselves: visible on the store, or a dead link?

Known-demo games are found in the data rather than hard-coded: every Demo-type app in
pics_raw/ that names a `parent` is a demo of that parent.

Output: a report on stdout (the run log), the same findings as a table in the job
summary, and the raw payloads under `demo_probe_out/` for upload as a build artifact.

Usage:
    python demo_probe.py
    QTPD_PROBE_APPIDS=2153600,720620 python demo_probe.py     # extra appids to check
    QTPD_PROBE_SAMPLE=50 python demo_probe.py                  # per-cohort sample size
"""

import glob
import json
import os
import random
import re
import sys
import time

import requests

GETITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
BATCH = 50                                                  # same as price_and_sale.py
DELAY = float(os.environ.get("QTPD_PROBE_DELAY", "1.5"))    # matches the other storefront jobs
SAMPLE = int(os.environ.get("QTPD_PROBE_SAMPLE", "100"))    # per cohort, for Q3
EXTRA = [int(a) for a in re.split(r"[,\s]+", os.environ.get("QTPD_PROBE_APPIDS", "")) if a.isdigit()]
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip()
TIMEOUT = 40
MAX_RETRIES = 4
OUT_DIR = "demo_probe_out"

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd demo probe; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)

# Q1's request variants. "current" must stay byte-for-byte what price_and_sale.getitems()
# sends: if the field comes back there, the filter costs no new request shape at all.
VARIANTS = {
    "current": {"include_basic_info": True, "include_assets": False,
                "include_release": False, "include_tag_count": 0,
                "include_reviews": False, "include_platforms": False,
                "include_all_purchase_options": True},
    "basic": {"include_basic_info": True},
    "everything": {"include_assets": True, "include_release": True, "include_platforms": True,
                   "include_all_purchase_options": True, "include_screenshots": True,
                   "include_trailers": True, "include_ratings": True, "include_tag_count": 20,
                   "include_reviews": True, "include_basic_info": True,
                   "include_supported_languages": True, "include_full_description": True,
                   "include_included_items": True, "include_links": True},
}

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


def getitems(appids, data_request):
    payload = {"ids": [{"appid": int(a)} for a in appids],
               "context": {"country_code": "US", "language": "english"},
               "data_request": data_request}
    params = {"input_json": json.dumps(payload, separators=(",", ":"))}
    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY
    status, data = get(GETITEMS, params=params)
    time.sleep(DELAY)
    items = ((data or {}).get("response") or {}).get("store_items") or []
    return status, items


def demo_paths(obj, path=""):
    """Every (path, value) under obj whose key mentions a demo or related_items."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if re.search(r"demo|related_items", k, re.I):
                out.append((p, v))
            out.extend(demo_paths(v, p))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(demo_paths(v, path + "[]"))
    return out


def demo_ids(item):
    """Demo appids named anywhere in one store item (any key containing 'demo')."""
    ids = set()
    for p, v in demo_paths(item):
        if not re.search(r"demo", p.rsplit(".", 1)[-1], re.I):
            continue
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, dict):
                x = x.get("appid")
            if isinstance(x, (int, str)) and str(x).isdigit():
                ids.add(int(x))
    return ids


def known_demo_parents():
    """{parent_appid: [demo_appid]} from the Demo-type apps PICS has archived."""
    out = {}
    for f in sorted(glob.glob("pics_raw/shard_*.json")):
        try:
            apps = json.load(open(f, encoding="utf-8")).get("apps", {})
        except (OSError, ValueError):
            continue
        for aid, rec in apps.items():
            if str(rec.get("type", "")).lower() == "demo" and str(rec.get("parent", "")).isdigit():
                out.setdefault(int(rec["parent"]), []).append(int(aid))
    return out


def cohorts():
    """Seeded random samples: released (games.json), upcoming dated / TBA (catalog pending)."""
    rnd = random.Random(20261021)
    try:
        released = [g["appid"] for g in json.load(open("games.json", encoding="utf-8"))["games"]
                    if g.get("release_ts")]
    except (OSError, ValueError, KeyError):
        released = []
    try:
        pend = json.load(open("catalog.json", encoding="utf-8")).get("pending") or {}
    except (OSError, ValueError):
        pend = {}
    now = time.time()
    dated = [int(a) for a, ts in pend.items() if ts and ts > now]
    tba = [int(a) for a, ts in pend.items() if not ts]
    pick = lambda xs: rnd.sample(xs, min(SAMPLE, len(xs)))
    return {"released": pick(released), "upcoming dated": pick(dated), "upcoming TBA": pick(tba)}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    dump = {}
    parents = known_demo_parents()
    known = sorted(set(parents) | set(EXTRA))[:BATCH]       # one GetItems call's worth
    log(f"known-demo games from pics_raw/: {parents}  (+ extra: {EXTRA})")
    if not known:
        log("No known-demo games found; Q1/Q2 have nothing to check against.")

    # ---------------------------------------------------------------- Q1
    hr("Q1  Which GetItems request returns demo appids?")
    SUMMARY.append("| variant | HTTP | items | items naming a demo | key paths seen |\n|---|---|---|---|---|")
    best = None
    for name, req in VARIANTS.items():
        status, items = getitems(known, req)
        hits = [it for it in items if demo_ids(it)]
        paths = sorted({p for it in items for p, _ in demo_paths(it)})
        log(f"\n[{name}] HTTP {status}, {len(items)} items, {len(hits)} name a demo")
        for p in paths:
            ex = next((v for it in items for q, v in demo_paths(it) if q == p), None)
            log(f"  {p} = {json.dumps(ex)[:200]}")
        if items:
            log("  first item, raw (truncated):")
            log("  " + json.dumps(items[0])[:1500])
        dump[f"q1_{name}"] = {"status": status, "items": items}
        SUMMARY.append(f"| {name} | {status} | {len(items)} | {len(hits)} | {', '.join(f'`{p}`' for p in paths) or '—'} |")
        if hits and best is None:
            best = name
    log(f"\nQ1 answer: first variant naming demos = {best or 'NONE'}")
    SUMMARY.append(f"\n**Answer:** {'`' + best + '` returns demo appids' if best else 'no variant returned demo appids'}.")

    # ---------------------------------------------------------------- Q2
    hr("Q2  Does full appdetails carry `demos`?")
    SUMMARY.append("| appid | name | appdetails `demos` | PICS demo appids |\n|---|---|---|---|")
    for aid in known:
        status, data = get(APPDETAILS, params={"appids": aid, "cc": "us", "l": "english"})
        time.sleep(DELAY)
        node = (data or {}).get(str(aid)) or {}
        d = node.get("data") or {}
        demos = d.get("demos")
        log(f"  {aid} {d.get('name')!r}: success={node.get('success')} demos={json.dumps(demos)}")
        dump.setdefault("q2", {})[aid] = {"status": status, "success": node.get("success"),
                                          "name": d.get("name"), "demos": demos}
        SUMMARY.append(f"| {aid} | {d.get('name') or '—'} | `{json.dumps(demos)}` | {parents.get(aid, '—')} |")

    # ---------------------------------------------------------------- Q3
    hr("Q3  How common are demos — released vs upcoming?")
    req = VARIANTS[best or "everything"]
    SUMMARY.append(f"Variant used: `{best or 'everything'}`, sample {SAMPLE} per cohort.\n")
    SUMMARY.append("| cohort | asked | answered | with a demo | share |\n|---|---|---|---|---|")
    found_demo_ids = set()
    for name, ids in cohorts().items():
        answered = with_demo = 0
        for i in range(0, len(ids), BATCH):
            _, items = getitems(ids[i:i + BATCH], req)
            answered += len(items)
            for it in items:
                di = demo_ids(it)
                if di:
                    with_demo += 1
                    found_demo_ids |= di
        share = f"{100 * with_demo / answered:.1f}%" if answered else "—"
        log(f"  {name:15} asked {len(ids):4}  answered {answered:4}  with a demo {with_demo:4}  ({share})")
        dump.setdefault("q3", {})[name] = {"asked": len(ids), "answered": answered, "with_demo": with_demo}
        SUMMARY.append(f"| {name} | {len(ids)} | {answered} | {with_demo} | {share} |")

    # ---------------------------------------------------------------- Q4
    hr("Q4  Are the demo appids live?")
    demo_list = sorted(found_demo_ids | {d for ds in parents.values() for d in ds})[:BATCH]
    if demo_list:
        _, items = getitems(demo_list, VARIANTS["basic"])
        live = sum(1 for it in items if it.get("visible") and it.get("success") == 1)
        log(f"  {len(demo_list)} demo appids asked, {len(items)} answered, {live} visible on the store")
        for it in items[:10]:
            log(f"    {it.get('appid') or it.get('id')}: success={it.get('success')} visible={it.get('visible')} "
                f"type={it.get('type')} name={it.get('name')!r} related={json.dumps(it.get('related_items'))}")
        dump["q4"] = items
        SUMMARY.append(f"{len(demo_list)} demo appids asked, {len(items)} answered, **{live} visible** on the store.")
    else:
        log("  no demo appids to check")
        SUMMARY.append("No demo appids to check.")

    with open(os.path.join(OUT_DIR, "demo_probe.json"), "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=1, ensure_ascii=False)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Demo probe\n" + "\n".join(SUMMARY) + "\n")
    log(f"\nraw payloads -> {OUT_DIR}/demo_probe.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
