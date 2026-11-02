#!/usr/bin/env python3
"""
Steam QTPD — store notices: 3rd-party DRM, 3rd-party accounts, kernel anti-cheat
===========================================================================
One SEPARATE, independent job that owns the STORE NOTICES layer: for each game, whether its
store page says it "Incorporates 3rd-party DRM" (Denuvo, publisher activation …), "Requires
3rd-Party Account" (EA, Ubisoft, PSN, Rockstar …) or "Uses Kernel Level Anti-Cheat" (Easy
Anti-Cheat, BattlEye, ACE …). Players asked for all three as filters (owner, 2026-11-02); the
Steam store shows them on a game's page and cannot filter by any of them.

THE SOURCES (verified 2026-11-02 by notices_probe.py, workflow 0.6)
---------------------------------------------------------------------------
"app" checks — appdetails, the per-game storefront call scraper.py already makes, carries the
first two as free text:
  drm_notice                "Denuvo Anti-Tampering"; "EA online activation and EA app software
                            installation and background use required. Denuvo Anti-tamper"
  ext_user_account_notice   "EA Account"; "PlayStation Network (Supports Linking to Steam Account)"

"page" checks — anti-cheat is in NO API the probe tried (appdetails, GetItems). Only the store
page states it, in a block whose class is `anticheat_section`: kernel-level ones read "Uses
Kernel Level Anti-Cheat" + an `anticheat_name`; others carry `anticheat_nonkernel_notice` and
read "Uses Anti-Cheat Software …". Developers disclose this themselves (ELDEN RING, which runs
Easy Anti-Cheat, states none), so the page can only ever say "stated" or "not stated". A page is
heavy, so only games that could plausibly carry one are asked: any multiplayer or co-op
category in pics.json, games already carrying an anti-cheat entry, and big games (1,000+
reviews) the change feed flags.

Both are one call per game on the 200-per-5-min storefront budget (DELAY). All three notices are
concentrated on big games — DRM 0 and account 2 in a random sample of 100 — which is what makes a
ranked sweep worth it.

THE SWEEP (daily, time-budgeted)
---------------------------------------------------------------------------
One ranked list of (kind, game) checks (plan() below), worked down until RUN_MINUTES is spent:
  0  games that CARRY a notice of that kind, last checked over FLAGGED_DAYS ago — this is what
     makes "the game dropped Denuvo" show up within a week even if nothing else changed;
  1  games PICS's change feed reported changed since we last checked them, 100+ reviews
     (pics_state.json `recent`, written by pics_refresh.py --changes). Removing Denuvo ships a
     new build, and a new build changes the app's PICS info, so this is the fast path;
  2  never checked, 1,000+ reviews — the backfill, biggest first;
  3  changed per the feed, under 100 reviews;
  4  never checked, the rest, biggest first;
  5  the backstop: checked over BACKSTOP_BIG_DAYS ago (1,000+ reviews) or BACKSTOP_DAYS ago
     (the rest), oldest first.
Within ranks 0-4, most reviews first; at equal rank, app checks before page checks. Only what
Steam ANSWERS changes anything: a failed call, or a check the run did not reach, keeps the
previous entry and stays due.

Output notices.json (served to the browser, part of the boot load), format notices_v1:
  { "_format", "generated_at", "checked": <games app-checked>, "pages": <games page-checked>,
    "count", "games": { "<appid>": {"d": DRM notice, "a": account notice,
                                    "k": kernel anti-cheat name, "c": other anti-cheat name} } }
Only games with a notice are listed (each key only when stated), so a game absent from it is
either clean or not yet checked; "checked" / "pages" let the page say how far the sweep has got.
An anti-cheat block with no readable name is stored as "yes".

notices_state.json (NOT served): { "_format", "checked": {"<appid>": ts}, "pages": {"<appid>": ts} }
— when each game was last answered, per kind. Pruned to the catalogue on save.

Ownership (one writer per file): THIS -> notices.json + notices_state.json. Reads games.json
(scraper.py), pics.json (pics_merge.py) and pics_state.json (pics_refresh.py) read-only.
"""

import html
import json
import os
import re
import time
from pathlib import Path

import requests

import gitsync                      # commits this job's own files to main (gitsync.py)

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"                 # read-only (owned by scraper.py)
PICS_FILE = HERE / "pics.json"                   # read-only (owned by pics_merge.py)
PICS_STATE_FILE = HERE / "pics_state.json"       # read-only (owned by pics_refresh.py)
OUT_FILE = HERE / "notices.json"                 # this job's output (served)
STATE_FILE = HERE / "notices_state.json"         # this job's memory (not served)

APPDETAILS = "https://store.steampowered.com/api/appdetails"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "180"))
DELAY = float(os.environ.get("NOTICES_DELAY", "1.5"))     # the storefront's ~200 per 5 min
CHECKPOINT_MINUTES = 30            # commit progress this often, so a lost runner loses little
TIME_BUFFER = 120                  # seconds kept back for the final save + commit
TIMEOUT = 40
MAX_RETRIES = 4
NOTICE_MAX = 160                   # characters kept per notice; the longest seen are ~120

FLAGGED_DAYS = 7                   # rank 0: re-check games that carry a notice weekly
BACKSTOP_BIG_DAYS = 60             # rank 5: 1,000+ reviews
BACKSTOP_DAYS = 180                # rank 5: everyone else
BIG = 1000                         # "big game" review count for ranks 2 and 5
SMALL = 100                        # below this a changed game waits behind the big backfill
DAY = 86400
# Feature categories (lookups/categories.json) that make a game worth a store-page check for
# anti-cheat: Multi-player, Co-op, MMO, Cross-Platform Multiplayer, Online PvP, Online Co-op,
# LAN PvP, LAN Co-op, PvP.
MP_CATS = {1, 9, 20, 27, 36, 38, 47, 48, 49}

HEADERS = {"User-Agent": "Mozilla/5.0 (steam-qtpd store notices; github pages dataset builder)",
           "Accept-Language": "en-US,en;q=0.9"}
# The store's age gate: without these a mature game's page is the age-check form, not the page.
COOKIES = {"birthtime": "568022401", "mature_content": "1", "wants_mature_content": "1",
           "Steam_Language": "english"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
try:
    SESSION.cookies.update(COOKIES)
except AttributeError:              # a stubbed session in tests
    pass


def log(msg=""):
    print(msg, flush=True)


def get(url, *, params=None, text=False):
    """Retry/backoff like the other storefront jobs. JSON (or the body, with text=True), or None
    when Steam never answered usefully."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = min(90, 5 * attempt)
                log(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait); continue
            if r.status_code == 403:
                log("  403 (soft-limit); cooling down 60s"); time.sleep(60); continue
            r.raise_for_status()
            if text:
                return r.text
            try:
                return r.json()
            except ValueError:
                return None
        except requests.RequestException as e:
            wait = min(30, 3 * attempt)
            log(f"  request error ({attempt}/{MAX_RETRIES}): {e}; retry in {wait}s")
            time.sleep(wait)
    return None


def clean(text):
    """A notice as one tidy line: tags and entities out, whitespace collapsed, capped. Empty -> None."""
    if not isinstance(text, str):
        return None
    t = html.unescape(re.sub(r"<[^>]+>", " ", text))
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    return t if len(t) <= NOTICE_MAX else t[:NOTICE_MAX - 1].rstrip() + "…"


def read_notices(payload, appid):
    """appdetails JSON -> ("ok", {"d"?, "a"?}) | ("gone", None) | ("fail", None).
    "gone" is success:false (delisted, region-locked): answered, nothing to read, nothing kept
    or cleared. "fail" is no usable answer at all."""
    if not isinstance(payload, dict):
        return "fail", None
    node = payload.get(str(appid))
    if not isinstance(node, dict):
        return "fail", None
    if not node.get("success"):
        return "gone", None
    data = node.get("data") or {}
    entry = {}
    d, a = clean(data.get("drm_notice")), clean(data.get("ext_user_account_notice"))
    if d:
        entry["d"] = d
    if a:
        entry["a"] = a
    return "ok", entry


AC_SECTION = re.compile(r'class="([^"]*\banticheat_section\b[^"]*)"', re.I)
# The name is the element's OWN text, up to its first child tag: Steam nests the uninstall note
# inside it (`<div class="anticheat_name">BattlEye<span class="anticheat_uninstalls"> - Requires
# manual removal after game uninstall</span></div>`, probe Q5, 2026-11-02), and that note is not
# part of the name.
AC_NAME = re.compile(r'class="[^"]*\banticheat_name\b[^"]*"[^>]*>([^<]*)', re.I)
AC_UNINSTALL = re.compile(r'class="[^"]*\banticheat_uninstalls\b')
# A store page we can trust to be the GAME's page, not an age gate, error or region page.
PAGE_OK = re.compile(r'class="[^"]*\bgame_area_purchase\b|id="game_area_purchase"|'
                     r'class="[^"]*\bapphub_AppName\b', re.I)


def read_anticheat(page):
    """Store page HTML -> ("ok", {"k"?|"c"?}) | ("gone", None) | ("fail", None).
    "ok" with {} means the game's page loaded and states no anti-cheat. "gone" is an answer that
    is not a game page (a delisted game redirected to the front page): answered, nothing read,
    nothing kept or cleared — like appdetails' success:false. "fail" is no answer. Kernel vs not is read from the
    block's text first ("Kernel Level" / "Anti-Cheat Software") and from its class second
    (`anticheat_nonkernel_notice`), so either marker alone is enough. Names are the
    `anticheat_name` elements inside the block, joined; "yes" when none is readable."""
    if not isinstance(page, str):
        return "fail", None
    if not PAGE_OK.search(page):
        return "gone", None
    m = AC_SECTION.search(page)
    if not m:
        return "ok", {}
    cls = m.group(1).lower()
    # The block's own extent: up to the next notice block or 3,000 characters, whichever is first.
    rest = page[m.end(): m.end() + 3000]
    nxt = re.search(r'class="[^"]*\b(?:DRM_notice|anticheat_section)\b', rest)
    block = rest[:nxt.start()] if nxt else rest
    text = clean(block) or ""
    if re.search(r"kernel[\s-]*level", text, re.I):
        kernel = True
    elif re.search(r"anti-?cheat software", text, re.I) or "nonkernel" in cls:
        kernel = False
    else:
        kernel = True                  # a bare anticheat_section without the nonkernel class
    names = []
    for raw in AC_NAME.findall(block):
        n = clean(raw)
        if n and n not in names:
            names.append(n)
    name = clean(", ".join(names)) or "yes"
    return "ok", ({"k": name} if kernel else {"c": name})


def rank_of(rev, last, changed, flagged, now):
    """(rank, sort key) for one check, or None when it is not due. See the module docstring."""
    if last is None:
        rank = 2 if rev >= BIG else 4
        if changed and rev < SMALL:
            rank = 3
        return rank, -rev
    if flagged and now - last >= FLAGGED_DAYS * DAY:
        return 0, -rev
    if changed:
        return (1 if rev >= SMALL else 3), -rev
    if now - last >= (BACKSTOP_BIG_DAYS if rev >= BIG else BACKSTOP_DAYS) * DAY:
        return 5, last                                  # oldest first
    return None


def plan(games, checked, pages, recent, notices, mp, now):
    """The ranked worklist: [(kind, appid)], kind "app" or "page", in the order a run should take
    them. `games` is [(appid, reviews)]; `checked` / `pages` {appid: ts} per kind; `recent`
    {appid: ts the feed reported a change}; `notices` {appid: entry}; `mp` the appids with a
    multiplayer or co-op category. Checks that are not due are left out."""
    ranked = []
    for aid, rev in games:
        e = notices.get(aid) or {}
        changed_since = lambda last: recent.get(aid, 0) > (last or 0)
        r = rank_of(rev, checked.get(aid), changed_since(checked.get(aid)),
                    bool(e.get("d") or e.get("a")), now)
        if r:
            ranked.append((r[0], r[1], 0, aid))
        has_ac = bool(e.get("k") or e.get("c"))
        if aid in mp or has_ac or (rev >= BIG and changed_since(pages.get(aid)) and aid in recent):
            r = rank_of(rev, pages.get(aid), changed_since(pages.get(aid)), has_ac, now)
            if r:
                ranked.append((r[0], r[1], 1, aid))
    ranked.sort()
    return [("page" if k else "app", aid) for _, _, k, aid in ranked]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_inputs():
    gdoc = load_json(GAMES_FILE, {}) or {}
    games = [(int(g["appid"]), int(g.get("review_count") or 0))
             for g in gdoc.get("games") or [] if g.get("appid") is not None and not g.get("unknown")]
    apps = (load_json(PICS_FILE, {}) or {}).get("apps") or {}
    mp = {int(a) for a, p in apps.items() if MP_CATS & set((p or {}).get("cats") or [])}
    pst = load_json(PICS_STATE_FILE, {}) or {}
    recent = {int(a): int(t) for a, t in (pst.get("recent") or {}).items()}
    st = load_json(STATE_FILE, {}) or {}
    checked = {int(a): int(t) for a, t in (st.get("checked") or {}).items()}
    pages = {int(a): int(t) for a, t in (st.get("pages") or {}).items()}
    out = load_json(OUT_FILE, {}) or {}
    notices = {int(a): e for a, e in (out.get("games") or {}).items() if isinstance(e, dict)}
    return games, mp, recent, checked, pages, notices


def save(notices, checked, pages, catalogue):
    """Write both files, pruned to the catalogue (a delisted game drops out of both)."""
    keep = set(catalogue)
    games = {str(a): notices[a] for a in sorted(notices) if a in keep and notices[a]}
    chk = {str(a): checked[a] for a in sorted(checked) if a in keep}
    pgs = {str(a): pages[a] for a in sorted(pages) if a in keep}
    doc = {"_format": "notices_v1", "generated_at": int(time.time()), "checked": len(chk),
           "pages": len(pgs), "count": len(games), "games": games}
    state = {"_format": "notices_state_v1", "checked": chk, "pages": pgs}
    for path, body in ((OUT_FILE, doc), (STATE_FILE, state)):
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, separators=(",", ":"), ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)


def git_checkpoint(msg):
    """Commit notices.json + notices_state.json on top of the current main (gitsync.py). A run
    dispatched from a branch is a dry run: gitsync never pushes from a non-main checkout."""
    gitsync.commit(["notices.json", "notices_state.json"], msg)


def merge(notices, aid, keys, new):
    """Replace the `keys` part of a game's entry with `new`; returns (added_or_changed, removed)
    as entry fragments, so the run can report both."""
    old = notices.get(aid) or {}
    before = {k: old[k] for k in keys if k in old}
    after = {k: new[k] for k in keys if k in new}
    entry = {k: v for k, v in old.items() if k not in keys}
    entry.update(after)
    if entry:
        notices[aid] = entry
    else:
        notices.pop(aid, None)
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    removed = {k: v for k, v in before.items() if k not in after}
    return changed, removed


def summarize(notices, checked, pages, found, dropped, reviews):
    """Run log + job summary: how far the sweep has got, and what this run found or lost."""
    n = lambda k: sum(1 for e in notices.values() if e.get(k))
    den = sum(1 for e in notices.values() if re.search(r"denuvo", e.get("d", ""), re.I))
    lines = [f"App-checked so far: **{len(checked):,}** games; page-checked: **{len(pages):,}**. "
             f"Stating 3rd-party DRM: {n('d'):,} (Denuvo {den:,}); 3rd-party account: {n('a'):,}; "
             f"kernel anti-cheat: {n('k'):,}; other anti-cheat: {n('c'):,}."]
    cols = "| appid | reviews | DRM | account | kernel AC | other AC |\n|---|---:|---|---|---|---|"
    row = lambda aid, e: (f"| {aid} | {reviews.get(aid, 0):,} | {e.get('d', '—')} | {e.get('a', '—')} "
                          f"| {e.get('k', '—')} | {e.get('c', '—')} |")
    if found:
        lines.append(f"\nFound or changed this run (most reviewed first):\n\n{cols}")
        lines += [row(a, e) for a, e in sorted(found, key=lambda x: -reviews.get(x[0], 0))[:20]]
    if dropped:
        lines.append(f"\nNo longer stated (e.g. Denuvo removed):\n\n{cols}")
        lines += [row(a, e) for a, e in sorted(dropped, key=lambda x: -reviews.get(x[0], 0))[:20]]
    for ln in lines:
        log(ln)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("## Store notices\n" + "\n".join(lines) + "\n")


def main():
    started = time.time()
    deadline = started + RUN_MINUTES * 60 - TIME_BUFFER
    games, mp, recent, checked, pages, notices = load_inputs()
    if not games:
        log("games.json has no games; nothing to do")
        return
    reviews = dict(games)
    work = plan(games, checked, pages, recent, notices, mp, started)
    n_page = sum(1 for k, _ in work if k == "page")
    log(f"notices: {len(games):,} games ({len(mp):,} multiplayer/co-op), {len(checked):,} app- and "
        f"{len(pages):,} page-checked before, {len(notices):,} with a notice, {len(recent):,} "
        f"recently changed per PICS; due: {len(work) - n_page:,} app + {n_page:,} page checks; "
        f"budget {RUN_MINUTES:g} min at {DELAY}s/call")

    asked = answered = gone = 0
    found, dropped, last_commit = [], [], time.time()
    for kind, aid in work:
        if time.time() >= deadline:
            log("  time budget reached — the rest stay due")
            break
        if asked and asked % 250 == 0:
            log(f"  {asked:,} asked, {answered:,} answered, {len(found):,} new/changed, "
                f"{len(dropped):,} removed")
        if time.time() - last_commit >= CHECKPOINT_MINUTES * 60:
            save(notices, checked, pages, reviews)
            git_checkpoint(f"notices: checkpoint, {len(checked):,} + {len(pages):,} checked")
            last_commit = time.time()
        if kind == "app":
            status, entry = read_notices(
                get(APPDETAILS, params={"appids": aid, "cc": "us", "l": "english"}), aid)
            keys, stamp = ("d", "a"), checked
        else:
            status, entry = read_anticheat(get(STORE_PAGE.format(appid=aid), text=True))
            keys, stamp = ("k", "c"), pages
        asked += 1
        time.sleep(DELAY)
        if status == "fail":
            continue                                   # not answered: keep what we had, stay due
        stamp[aid] = int(time.time())
        answered += 1
        if status == "gone":
            gone += 1
            continue                                   # answered, nothing readable: keep the entry
        changed, removed = merge(notices, aid, keys, entry)
        if changed:
            found.append((aid, changed))
        if removed:
            dropped.append((aid, removed))             # the store no longer states it

    save(notices, checked, pages, reviews)
    log(f"done: {asked:,} asked, {answered:,} answered ({gone:,} gone from the store), "
        f"{len(found):,} new or changed, {len(dropped):,} removed")
    summarize(notices, checked, pages, found, dropped, reviews)
    git_checkpoint(f"notices: {len(notices):,} games with a store notice "
                   f"({len(checked):,} app- / {len(pages):,} page-checked)")


if __name__ == "__main__":
    main()
