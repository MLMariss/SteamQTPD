#!/usr/bin/env python3
"""
Steam QHPP — accumulation scraper
=================================
Builds up a database of Steam games OVER TIME. Each run discovers more of the
catalog, scrapes a batch of not-yet-seen games, refreshes a batch of stale or
on-sale games (so prices/discounts stay current), and writes everything to
games.json. Run it on a schedule (the included GitHub Action does this) and the
dataset grows; the static page just reads games.json and filters it.

Why this shape: a static GitHub Pages site can't call Steam from the browser
(no CORS headers), so scraping happens server-side in the Action. State lives in
the repo (games.json + catalog.json are committed each run), so it survives
between runs without any database or extra service.

Per game it collects: title, store URL, rating (% positive) + review count,
price before/after discount (USD), discount % + discount END time, release date,
user tags, How Long To Beat (main / main+extras / completionist), and QHPP
before & after discount.

QHPP (quality hours per dollar), quality-weighted:
    avg_hours = mean of whichever HLTB times exist
    qhpp      = (avg_hours * (rating_pct / 100)) / price_usd
  Higher = more quality-adjusted hours per dollar. Null for free games (no price)
  and games with no HLTB data. qhpp_before uses full price; qhpp_after the sale price.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

try:
    from howlongtobeatpy import HowLongToBeat
except ImportError:
    HowLongToBeat = None

# --------------------------------------------------------------------------- #
# CONFIG — edit these
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # display data + scraped game state (committed)
CATALOG_FILE = HERE / "catalog.json"      # discovery cursor / queue / skips (committed)
SEEDS_FILE = HERE / "seeds.txt"           # OPTIONAL games to scrape FIRST (see file)

# The "universe" to work through over time. A broad Steam store search. Default:
# all Games, highest review score first, so the most relevant titles are captured
# earliest. Swap for release-sorted, a tag, etc. — see README.
CATALOG_SEARCH = ("https://store.steampowered.com/search/"
                  "?category1=998&supportedlang=english&sort_by=Reviews_DESC")

CATALOG_PAGES_PER_RUN = 5     # how many search pages (~100 apps each) to discover per run
PRIORITY_PAGES = 10           # max search pages to pull from each seeds.txt search entry
NEW_PER_RUN = 80              # how many never-seen games to fully scrape per run
REFRESH_PER_RUN = 40          # how many stale/on-sale games to re-scrape per run
REFRESH_DAYS = 7              # re-scrape games older than this many days

TOP_TAGS = 8
HLTB_MIN_SIMILARITY = 0.65
COUNTRY = "us"                # cc=us => USD prices

# Politeness. Steam store ~200 req/5 min; SteamSpy ~1 req/sec. Don't lower much.
STEAM_DELAY = 1.5
STEAMSPY_DELAY = 1.0
MAX_RETRIES = 4

SEARCH_URL = "https://store.steampowered.com/search/results/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (steam-qhpp scraper; github pages dataset builder)",
    "Accept-Language": "en-US,en;q=0.9",
}
COOKIES = {"birthtime": "568022401", "mature_content": "1",
           "Steam_Language": "english", "wants_mature_content": "1"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.update(COOKIES)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# HTTP with retry/backoff
# --------------------------------------------------------------------------- #
def get(url, *, params=None, timeout=25, expect_json=False):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(60, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s")
                time.sleep(wait); continue
            r.raise_for_status()
            if expect_json:
                try:
                    return r.json()
                except ValueError:
                    return None
            return r.text
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- #
# Search helpers (catalog discovery + priority seeds)
# --------------------------------------------------------------------------- #
def search_params(seed: str) -> dict:
    p = {"infinite": 1, "count": 100, "cc": COUNTRY, "l": "english"}
    if seed.startswith("http"):
        for k, v in parse_qs(urlparse(seed).query).items():
            if k not in ("start", "count", "infinite"):
                p[k] = v[0]
    else:
        p["term"] = seed
    return p


def fetch_search_page(params: dict, start: int):
    """Return (list_of_appids, total_count) for one page of a store search."""
    p = dict(params); p["start"] = start
    data = get(SEARCH_URL, params=p, expect_json=True)
    time.sleep(STEAM_DELAY)
    if not data:
        return [], 0
    html = data.get("results_html", "")
    total = int(data.get("total_count", 0) or 0)
    ids = []
    for chunk in re.findall(r'data-ds-appid="([\d,]+)"', html):
        for piece in chunk.split(","):
            if piece.isdigit():
                ids.append(int(piece))
    return ids, total


# --------------------------------------------------------------------------- #
# Per-game data sources
# --------------------------------------------------------------------------- #
def build_discount_expiration_map():
    """featuredcategories exposes discount_expiration (unix) for current specials."""
    out = {}
    data = get("https://store.steampowered.com/api/featuredcategories",
               params={"cc": COUNTRY, "l": "english"}, expect_json=True)
    if not isinstance(data, dict):
        return out

    def harvest(node):
        if isinstance(node, dict):
            if "id" in node and node.get("discount_expiration"):
                try:
                    out[int(node["id"])] = int(node["discount_expiration"])
                except (TypeError, ValueError):
                    pass
            for v in node.values():
                harvest(v)
        elif isinstance(node, list):
            for v in node:
                harvest(v)

    harvest(data)
    log(f"Discount-expiration map: {len(out)} games in current specials")
    return out


_COUNTDOWN_PATTERNS = [
    r'game_purchase_discount_countdown[^>]*data-end-?time="(\d{9,10})"',
    r'"discount_end_date"\s*:\s*"?(\d{9,10})"?',
    r'"discount_expiration"\s*:\s*"?(\d{9,10})"?',
    r'discountCountdown\D{0,40}?(\d{9,10})',
    r'data-untiltime="(\d{9,10})"',
]


def discount_end_from_page(appid: int):
    """Best-effort discount end from store HTML (the game_purchase_discount_countdown
    element). Could not be verified live in the build env — if a real sale shows no
    timer, the log tells you and you add the matching pattern above."""
    html = get(f"https://store.steampowered.com/app/{appid}/",
               params={"cc": COUNTRY, "l": "english"})
    time.sleep(STEAM_DELAY)
    if not html:
        return None
    for pat in _COUNTDOWN_PATTERNS:
        m = re.search(pat, html)
        if m:
            ts = int(m.group(1))
            if 1_500_000_000 < ts < 4_000_000_000:
                return ts
    if "game_purchase_discount_countdown" in html:
        log(f"  appid {appid}: countdown present but no timestamp matched")
    return None


def tags_from_steamspy(appid: int):
    data = get("https://steamspy.com/api.php",
               params={"request": "appdetails", "appid": appid}, expect_json=True)
    time.sleep(STEAMSPY_DELAY)
    if isinstance(data, dict):
        tags = data.get("tags") or {}
        if isinstance(tags, dict) and tags:
            ranked = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)
            return [name for name, _ in ranked[:TOP_TAGS]]
    return []


def rating_from_reviews(appid: int):
    data = get(f"https://store.steampowered.com/appreviews/{appid}",
               params={"json": 1, "language": "all", "purchase_type": "all",
                       "num_per_page": 0}, expect_json=True)
    if not isinstance(data, dict) or data.get("success") != 1:
        return None, 0
    s = data.get("query_summary", {})
    pos, neg = int(s.get("total_positive", 0)), int(s.get("total_negative", 0))
    if pos + neg == 0:
        return None, 0
    return round(pos / (pos + neg) * 100), pos + neg


def hltb_for(title: str):
    blank = {"main": None, "extra": None, "complete": None, "match": None}
    if HowLongToBeat is None:
        return blank
    try:
        results = HowLongToBeat().search(title)
    except Exception as e:
        log(f"  HLTB error '{title}': {e}")
        return blank
    if not results:
        return blank
    best = max(results, key=lambda r: r.similarity or 0)
    if (best.similarity or 0) < HLTB_MIN_SIMILARITY:
        return blank

    def hrs(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return round(v, 1) if v > 0 else None

    return {"main": hrs(best.main_story), "extra": hrs(best.main_extra),
            "complete": hrs(best.completionist), "match": best.game_name}


_RELEASE_FORMATS = ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%Y")


def parse_release(d):
    rd = d.get("release_date", {}) or {}
    s = (rd.get("date") or "").strip()
    ts = None
    if s and not rd.get("coming_soon"):
        for fmt in _RELEASE_FORMATS:
            try:
                ts = int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
                break
            except ValueError:
                continue
    return (s or None), ts


def qhpp(avg_hours, rating_pct, price_usd):
    if not avg_hours or not rating_pct or not price_usd or price_usd <= 0:
        return None
    return round((avg_hours * (rating_pct / 100)) / price_usd, 3)


# --------------------------------------------------------------------------- #
# Build one game record. Returns dict | "skip" (non-game/delisted) | None (transient)
# --------------------------------------------------------------------------- #
def build_record(appid: int, discount_map):
    detail = get("https://store.steampowered.com/api/appdetails",
                 params={"appids": appid, "cc": COUNTRY, "l": "english"},
                 expect_json=True)
    time.sleep(STEAM_DELAY)
    if detail is None:
        return None                                   # network gave up -> retry later
    node = detail.get(str(appid), {})
    if not node.get("success"):
        return "skip"                                 # delisted / not a store item
    d = node.get("data", {})
    if d.get("type") != "game":
        return "skip"                                 # DLC, soundtrack, video, tool...

    title = d.get("name", f"App {appid}")
    is_free = bool(d.get("is_free"))
    price_initial = price_final = None
    discount_pct = 0
    po = d.get("price_overview")
    if po:
        if po.get("currency") != "USD":
            log(f"  appid {appid}: currency {po.get('currency')} != USD")
        price_initial = round(po.get("initial", 0) / 100, 2) or None
        price_final = round(po.get("final", 0) / 100, 2) or None
        discount_pct = int(po.get("discount_percent", 0))

    discount_end = None
    if discount_pct > 0:
        discount_end = discount_map.get(appid) or discount_end_from_page(appid)

    rating_pct, review_count = rating_from_reviews(appid)
    tags = tags_from_steamspy(appid) or [g["description"] for g in d.get("genres", [])
                                         if g.get("description")]
    release_date, release_ts = parse_release(d)
    h = hltb_for(title)
    htimes = [t for t in (h["main"], h["extra"], h["complete"]) if t]
    avg = round(sum(htimes) / len(htimes), 1) if htimes else None

    rec = {
        "appid": appid,
        "title": title,
        "url": f"https://store.steampowered.com/app/{appid}",
        "rating_pct": rating_pct,
        "review_count": review_count,
        "price_initial": price_initial,
        "price_final": price_final,
        "discount_pct": discount_pct,
        "discount_end": discount_end,
        "is_free": is_free,
        "release_date": release_date,
        "release_ts": release_ts,
        "tags": tags,
        "hltb_main": h["main"], "hltb_extra": h["extra"], "hltb_complete": h["complete"],
        "hltb_avg": avg, "hltb_match": h["match"],
        "qhpp_before": qhpp(avg, rating_pct, price_initial),
        "qhpp_after": qhpp(avg, rating_pct, price_final),
    }
    log(f"  OK {title[:40]:40} rating={rating_pct} price={price_final}/{price_initial} "
        f"avg={avg} qhpp_after={rec['qhpp_after']}")
    return rec


# --------------------------------------------------------------------------- #
# Repo-committed state
# --------------------------------------------------------------------------- #
def load_games():
    """Return {appid_str: record}. Ignores bundled sample data on first real run."""
    if not GAMES_FILE.exists():
        return {}
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if d.get("sample"):
        log("games.json holds sample data; starting a fresh real dataset.")
        return {}
    return {str(g["appid"]): g for g in d.get("games", [])}


def save_games(processed):
    games = sorted(processed.values(), key=lambda g: g["appid"])
    GAMES_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(games), "games": games},
        ensure_ascii=False, indent=2), encoding="utf-8")


def load_catalog():
    if CATALOG_FILE.exists():
        try:
            c = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            c.setdefault("cursor", 0)
            c.setdefault("pending", [])
            c.setdefault("skipped", [])
            c.setdefault("priority", None)
            return c
        except ValueError:
            pass
    return {"cursor": 0, "pending": [], "skipped": [], "priority": None}


def save_catalog(c):
    CATALOG_FILE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Discovery + work selection
# --------------------------------------------------------------------------- #
def ensure_priority(catalog):
    if catalog.get("priority") is not None:
        return
    pr = []
    if SEEDS_FILE.exists():
        for raw in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.isdigit():
                pr.append(int(line))
            else:
                params = search_params(line); start = 0
                for _ in range(PRIORITY_PAGES):
                    ids, total = fetch_search_page(params, start)
                    if not ids:
                        break
                    pr.extend(ids); start += 100
                    if total and start >= total:
                        break
    catalog["priority"] = list(dict.fromkeys(pr))
    log(f"Priority seeds resolved: {len(catalog['priority'])} appids")


def ingest_catalog(catalog, processed):
    params = search_params(CATALOG_SEARCH)
    start = catalog["cursor"]
    pending = set(catalog["pending"])
    known = pending | {int(a) for a in processed} | set(catalog["skipped"])
    added = 0; total = 0
    for _ in range(CATALOG_PAGES_PER_RUN):
        ids, total = fetch_search_page(params, start)
        if not ids:
            start = 0; break                       # wrap to re-discover newest next time
        for a in ids:
            if a not in known:
                known.add(a); pending.add(a); added += 1
        start += 100
        if total and start >= total:
            start = 0; break
    catalog["cursor"] = start
    catalog["pending"] = sorted(pending)
    log(f"Catalog ingest: +{added} new appids (pending {len(pending)}, "
        f"cursor {start}, universe ~{total})")


def select_work(catalog, processed):
    done = {int(a) for a in processed} | set(catalog["skipped"])
    new, seen = [], set()
    for source in (catalog.get("priority") or [], catalog["pending"]):
        for a in source:
            if a not in done and a not in seen:
                new.append(a); seen.add(a)
            if len(new) >= NEW_PER_RUN:
                break
        if len(new) >= NEW_PER_RUN:
            break

    now = int(time.time())
    cands = []
    for k, rec in processed.items():
        age = now - rec.get("scraped_at", 0)
        if rec.get("discount_pct", 0) > 0 or age >= REFRESH_DAYS * 86400:
            cands.append((rec.get("scraped_at", 0), int(k)))
    cands.sort()
    refresh = [a for _, a in cands[:REFRESH_PER_RUN]]
    return new, refresh


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    processed = load_games()
    catalog = load_catalog()
    ensure_priority(catalog)
    ingest_catalog(catalog, processed)

    new, refresh = select_work(catalog, processed)
    log(f"This run: {len(new)} new + {len(refresh)} refresh "
        f"(total stored so far: {len(processed)})")
    if not new and not refresh:
        save_games(processed); save_catalog(catalog)
        log("Nothing to do this run.")
        return 0

    discount_map = build_discount_expiration_map()
    pending = set(catalog["pending"]); skipped = set(catalog["skipped"])
    work = [(a, "new") for a in new] + [(a, "refresh") for a in refresh]

    for i, (appid, kind) in enumerate(work, 1):
        log(f"[{i}/{len(work)}] {kind} appid {appid}")
        res = build_record(appid, discount_map)
        if isinstance(res, dict):
            res["scraped_at"] = int(time.time())
            processed[str(appid)] = res
            pending.discard(appid)
        elif res == "skip":
            skipped.add(appid); pending.discard(appid)
            log(f"  appid {appid}: skipped (not a store game)")
        # res is None -> transient; leave in pending to retry next run

        if i % 10 == 0:
            catalog["pending"] = sorted(pending); catalog["skipped"] = sorted(skipped)
            save_games(processed); save_catalog(catalog)

    catalog["pending"] = sorted(pending - {int(a) for a in processed})
    catalog["skipped"] = sorted(skipped)
    save_games(processed); save_catalog(catalog)
    log(f"\nDone. Stored games: {len(processed)} | pending queue: "
        f"{len(catalog['pending'])} | skipped: {len(catalog['skipped'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
