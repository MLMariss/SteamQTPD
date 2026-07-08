#!/usr/bin/env python3
"""
Steam QHPP — HowLongToBeat refresher
====================================
A SEPARATE, independent job from scraper.py, for the same reason recent/sales were
split out: HLTB lookups are the SLOWEST part of the whole pipeline. The howlongtobeatpy
library scrapes howlongtobeat.com, and each search is a 2-10s (sometimes hanging)
round-trip with no rate budget. When that ran inside the main scrape loop it dominated
per-game time — the scraper was doing ~6 games/min when Steam itself would allow ~40,
because every new game waited on an HLTB search. Pulling HLTB out makes the main scrape
~3-5x faster instantly.

Why a separate cadence works well here: HLTB completion times are mostly STATIC — a
game's "how long to beat" changes slowly, if at all. The first pass fills hltb.json for
games that don't have an entry yet (the priority). Once that pass is complete, leftover
budget goes to a RE-SCRAPE pass that revisits existing entries on staleness windows
(partials soonest, blanks rarely, full triples almost never — see T5), to pick up data
HLTB added after our first lookup. It can run slowly in the background without holding
anything up.

Ownership (one writer per file, no push collisions):
  scraper.py      -> games.json   (catalog, rating, tags, last_update, release)
  price_and_sale  -> prices.json  (price, discount, sale end)
  THIS            -> hltb.json     {appid: {main, extra, complete, avg, match}}
  recent_refresh  -> recent.json  (30-day review scores)
The frontend merges all of these by appid; QHPP is computed client-side from the merge.

Reads games.json (read-only) just to know which appids exist and their titles (HLTB
matches by title, not appid).
"""

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import hltb_estimate as HE      # shared fill logic (live median ratio + missing-value fill)
import hltb_match as HM         # Phase A: title normalization + fallback query variants

try:
    from howlongtobeatpy import HowLongToBeat
except ImportError:
    HowLongToBeat = None

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"          # read-only (owned by scraper.py)
HLTB_FILE = HERE / "hltb.json"            # this job's output (committed)

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "120"))
CHECKPOINT_SECONDS = 300
TIME_BUFFER = 45
HLTB_MIN_SIMILARITY = 0.65
HLTB_DELAY = 0.6                          # pacing between HLTB searches (howlongtobeat tolerates this)

# Re-scrape staleness gates (T5). Once the first pass is done (no never-seen games left),
# the job re-fetches EXISTING entries with leftover budget, oldest-first within each bucket,
# but only if an entry hasn't been refetched within its bucket's window. This stops any one
# title being re-hit every run while still letting incomplete/blank entries pick up data
# HLTB added later. Yield drives the cadence: partials (HLTB already has the page, missing
# fields fill in) are high-value and retried often; blanks are mostly irreducible (HLTB has
# no page) so retried rarely; full triples almost never change, so almost never.
RESCRAPE_PARTIAL_DAYS = 14               # entries with 1-2 real raw values
RESCRAPE_FULL_DAYS = 365                 # complete triples (3 real raw values)
DAY = 86400

# --- Phase B: eager-but-throttled blank retry + never-idle drain ------------------- #
# A blank (no-match) is EITHER a genuinely-dead title (shovelware HLTB will never have)
# OR a real game we lost to title noise / a transient HLTB hiccup during the first pass.
# We can't tell which up front, so: retry unproven blanks EAGERLY, then back off as the
# evidence that a title is really dead accumulates. Each blank carries an `attempts`
# counter (times we've searched and failed to match). Its staleness window grows with
# attempts, then freezes:
#
#   attempts 0-2  -> BLANK_EAGER_DAYS   (3d)   recover transient/first-pass misses fast
#   attempts 3-5  -> BLANK_BACKOFF_DAYS (30d)  probably dead, but keep an eye on it
#   attempts >=6  -> BLANK_FREEZE_DAYS  (180d) treat as dead; near-frozen
#
# This directly serves the "never sit idle" goal without burning the whole budget
# re-confirming 90k dead titles every run: eager on the unproven, throttled on the proven.
BLANK_EAGER_ATTEMPTS = 3
BLANK_EAGER_DAYS = 3
BLANK_BACKOFF_ATTEMPTS = 6
BLANK_BACKOFF_DAYS = 30
BLANK_FREEZE_DAYS = 180

# Never-idle drain: once the windowed stale queue is exhausted but budget remains, keep
# working the least-recently-attempted, least-tried blanks anyway (ignoring their window)
# so the job always has the next-in-line item instead of quitting early. Bounded per run
# so a single run can't hammer the same tail forever; the attempts counter + oldest-first
# ordering spread coverage across runs.
IDLE_DRAIN_ENABLED = True
IDLE_DRAIN_MAX = 4000                    # cap drained blanks per run (budget still gates)
IDLE_DRAIN_SKIP_FROZEN = True            # don't drain entries already at the freeze tier

DAY_ = DAY  # alias kept for readability below
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def blank_window_days(attempts):
    """Staleness window (in days) for a blank entry given how many times we've tried
    and failed to match it. Eager first, then backs off, then near-freezes. Pure."""
    a = attempts or 0
    if a < BLANK_EAGER_ATTEMPTS:
        return BLANK_EAGER_DAYS
    if a < BLANK_BACKOFF_ATTEMPTS:
        return BLANK_BACKOFF_DAYS
    return BLANK_FREEZE_DAYS


def log(msg):
    print(msg, flush=True)


def _search_one(query):
    """Search HLTB for a SINGLE query string. Returns a tri-state:
      - dict of raw values  -> a match at/above the similarity floor
      - {}                  -> searched cleanly, no acceptable match for THIS query
      - None                -> transient error (network/parse); caller must NOT
                               record a blank, should retry next run
    Pure-ish (one network call); no recording decisions made here."""
    try:
        results = HowLongToBeat().search(query)
    except Exception as e:
        log(f"  HLTB error '{query}': {e}")
        return None                       # transient -> signal retry
    if not results:
        return {}                         # clean miss for this query variant
    best = max(results, key=lambda r: r.similarity or 0)
    if (best.similarity or 0) < HLTB_MIN_SIMILARITY:
        return {}                         # below floor -> treat as miss for this variant

    def hrs(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return round(v, 1) if v > 0 else None

    return {"main": hrs(best.main_story), "extra": hrs(best.main_extra),
            "complete": hrs(best.completionist), "match": best.game_name}


def hltb_for(title):
    """Fetch best-match HLTB times for a Steam title. Returns a dict of RAW values
    {"main", "extra", "complete", "match"} on a match, a raw blank on a genuine
    no-match, or None on a transient error (leave unresolved, retry next run).

    Phase A: rather than searching the raw store title once, we walk an ordered
    list of query variants (raw first, then trademark-stripped, edition-stripped,
    re-cased, subtitle-trimmed — see hltb_match.query_variants). The raw title is
    always tried first so this can only widen matches, never regress one. We stop
    at the FIRST variant that matches. Transient-error semantics are preserved: if
    every attempted variant errored (and none matched), we return None so the game
    is retried next run rather than frozen as a permanent blank. A clean miss on
    all variants returns the blank."""
    blank = {"main": None, "extra": None, "complete": None, "match": None}
    if HowLongToBeat is None:
        return blank

    saw_transient = False
    variants = HM.query_variants(title)
    for i, q in enumerate(variants):
        res = _search_one(q)
        if res is None:
            saw_transient = True          # remember, but keep trying other variants
            continue
        if res:                           # non-empty dict -> a real match
            if i > 0:
                log(f"  matched via variant[{i}] '{q}' (raw: '{title[:40]}')")
            return res
        # res == {} -> clean miss for this variant; try the next one
        if i < len(variants) - 1:
            time.sleep(HLTB_DELAY)        # pace the extra searches politely

    # No variant matched. If ANY attempt was a transient error, don't freeze a
    # blank — return None so this title is retried next run.
    return None if saw_transient else blank


def rescrape_bucket(entry):
    """Classify an existing hltb entry by how many REAL (raw) values it has, which
    determines its re-scrape priority and staleness window. Returns one of
    'partial' (1-2 real), 'blank' (0 real / no-match), 'full' (3 real)."""
    rm, re_, rc = HE.raw_of(entry)
    n_real = sum(1 for x in (rm, re_, rc) if HE.is_real(x))
    if n_real >= 3:
        return "full"
    if n_real >= 1:
        return "partial"
    return "blank"


def _blank_attempts(entry):
    """How many times this blank has been searched-and-missed. Absent on legacy
    entries -> treated as 0 (they predate the counter and should retry eagerly)."""
    a = entry.get("attempts")
    return a if isinstance(a, int) and a >= 0 else 0


def build_rescrape_queue(games, hltb, now):
    """Build the ordered re-scrape work list from games that ALREADY have an entry.
    Priority order (highest expected yield first, §2 → T5): partial -> blank -> full.
    Within each bucket, oldest fetched_at first so retries spread evenly and no entry
    is re-hit twice before its peers get one pass. Eligibility is by staleness window;
    games with no title in games.json can't be re-searched and are skipped.

    Phase B change: the BLANK window is no longer a single 60d constant. It scales with
    the entry's `attempts` counter (blank_window_days): eager for unproven blanks, then
    backoff, then near-freeze — so real games lost to title noise / transient first-pass
    failures get retried fast, while genuinely-dead shovelware throttles down instead of
    being re-hit every run."""
    title_by_aid = {aid: title for aid, title in games}
    static_windows = {
        "partial": RESCRAPE_PARTIAL_DAYS * DAY,
        "full": RESCRAPE_FULL_DAYS * DAY,
    }
    buckets = {"partial": [], "blank": [], "full": []}
    for aid, entry in hltb.items():
        title = title_by_aid.get(aid)
        if not title:
            continue                      # no Steam title to search with -> can't re-scrape
        bucket = rescrape_bucket(entry)
        fetched_at = entry.get("fetched_at") or 0
        if bucket == "blank":
            window = blank_window_days(_blank_attempts(entry)) * DAY
        else:
            window = static_windows[bucket]
        if now - fetched_at <= window:
            continue                      # refetched recently -> not yet stale enough
        buckets[bucket].append((fetched_at, aid, title))
    queue = []
    for bucket in ("partial", "blank", "full"):   # priority order
        buckets[bucket].sort(key=lambda t: t[0])  # oldest fetched_at first
        queue.extend((aid, title, bucket) for _ts, aid, title in buckets[bucket])
    return queue


def build_idle_drain(games, hltb, now, exclude_aids):
    """Never-idle fallback work: when the windowed queue is exhausted but budget
    remains, keep pulling blanks anyway so the job never quits early with time left.
    Orders by (attempts asc, fetched_at asc): fewest-tried and least-recently-tried
    first, so unproven blanks are drained before near-dead ones and coverage spreads
    across runs. Skips frozen-tier blanks (attempts high) when IDLE_DRAIN_SKIP_FROZEN,
    and anything already queued this run (exclude_aids). Bounded to IDLE_DRAIN_MAX;
    the time budget is still the real gate."""
    if not IDLE_DRAIN_ENABLED:
        return []
    title_by_aid = {aid: title for aid, title in games}
    candidates = []
    for aid, entry in hltb.items():
        if aid in exclude_aids:
            continue
        if rescrape_bucket(entry) != "blank":
            continue                      # only blanks are drained (partials/fulls use windows)
        title = title_by_aid.get(aid)
        if not title:
            continue
        attempts = _blank_attempts(entry)
        if IDLE_DRAIN_SKIP_FROZEN and attempts >= BLANK_BACKOFF_ATTEMPTS:
            continue                      # proven-dead tier -> don't drain, let its window ride
        fetched_at = entry.get("fetched_at") or 0
        candidates.append((attempts, fetched_at, aid, title))
    candidates.sort(key=lambda t: (t[0], t[1]))   # fewest attempts, then oldest
    return [(aid, title, "blank") for _a, _f, aid, title in candidates[:IDLE_DRAIN_MAX]]


def load_games():
    if not GAMES_FILE.exists():
        return []
    try:
        d = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if d.get("sample"):
        return []
    return [(int(g["appid"]), g.get("title", "")) for g in d.get("games", [])]


def load_hltb():
    if HLTB_FILE.exists():
        try:
            d = json.loads(HLTB_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in (d.get("hltb") or {}).items()}
        except (ValueError, TypeError):
            pass
    return {}


def save_hltb(hltb):
    HLTB_FILE.write_text(json.dumps(
        {"generated_at": int(time.time()), "count": len(hltb),
         "hltb": {str(k): v for k, v in hltb.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")


def git_checkpoint(msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "hltb.json"], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=False)
            for _attempt in range(1, 9):    # retry against other jobs pushing concurrently
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                  capture_output=True, text=True).returncode == 0:
                    log(f"  committed: {msg}")
                    break
                time.sleep(2 * _attempt + random.uniform(0, 2))
    except Exception as e:
        log(f"  git checkpoint failed: {e}")


def store_entry(hltb, aid, res, ratios, now):
    """Build and store the entry for a fetched result, managing the Phase B
    `attempts` counter on blanks. Returns True if the stored entry is a real match
    (has an avg), False if it's a blank.

    - MATCH  -> store the filled entry and drop any `attempts` (it's resolved).
    - BLANK  -> store the blank and increment `attempts` (carry prior count forward),
                so build_rescrape_queue can back its retry window off over time.
    - BLANK over a PRIOR real entry -> guarded below: `make_entry` always builds `raw`
      fresh from `res` alone (it does not merge with the entry already in `hltb`), so
      without this guard a re-scrape (run_rescrape also calls store_entry) that comes
      back a clean, all-None miss for a title that previously had real data would
      silently erase it. `hltb_for` can legitimately return that all-None "blank" dict
      on a re-scrape, not just on a first attempt, whenever every query variant is a
      clean miss (as opposed to a transient error, which surfaces as `None` and is
      filtered out by the caller before `store_entry` is ever invoked). Real values
      always overwrite; a blank must never wipe existing real data — that's the
      invariant this guard restores."""
    prior = hltb.get(aid) or {}
    is_blank_result = not any(res.get(k) is not None for k in ("main", "extra", "complete"))
    if is_blank_result:
        prior_m, prior_e, prior_c = HE.raw_of(prior)
        if HE.is_real(prior_m) or HE.is_real(prior_e) or HE.is_real(prior_c):
            # Keep the prior real entry untouched; just restamp fetched_at so it isn't
            # immediately re-queued as stale under the "full"/"partial" staleness window.
            log(f"  hltb {aid}: re-scrape came back blank, keeping prior real data")
            prior["fetched_at"] = int(now)
            hltb[aid] = prior
            return True
    entry = HE.make_entry(res.get("main"), res.get("extra"),
                          res.get("complete"), res.get("match"), now, ratios)
    if entry.get("avg") is not None:
        entry.pop("attempts", None)       # resolved -> counter no longer needed
        hltb[aid] = entry
        return True
    # blank: increment attempts (prior attempts + 1); legacy/absent -> starts at 1
    entry["attempts"] = _blank_attempts(prior) + 1
    hltb[aid] = entry
    return False


def main():
    if HowLongToBeat is None:
        log("howlongtobeatpy not installed; nothing to do.")
        return 1
    # Fail-fast regression guards (Phase A + B). Abort BEFORE touching hltb.json so a
    # broken normalizer or retry curve produces a loud red failure, not silent decay.
    try:
        import hltb_selfcheck
        hltb_selfcheck.run_all()
        log("Self-checks passed (title matching + blank retry logic).")
    except AssertionError as e:
        log(f"SELF-CHECK FAILED — aborting to avoid degrading data: {e}")
        return 2
    start = time.time()
    games = load_games()
    if not games:
        log("No games in games.json (or only sample data). Nothing to do.")
        return 0

    hltb = load_hltb()
    # First-pass work: games we've never touched (no entry yet). This always takes
    # priority over re-scraping — finish covering the catalog before revisiting.
    todo = [(aid, title) for aid, title in games if aid not in hltb]
    log(f"Games total {len(games)} | HLTB entries {len(hltb)} | new to resolve {len(todo)}")

    budget = RUN_MINUTES * 60
    last_commit = time.time()
    n_hit = n_blank = 0
    rescraped = newly_filled = 0

    # Live ratios for filling missing/zero HLTB values, computed ONCE from the
    # current corpus's real `raw` triples (estimates can't pollute it — see
    # hltb_estimate.compute_ratios). Fixed at run start for determinism; new real
    # triples found this run feed next run's ratio. Falls back to frozen medians
    # until enough real triples exist.
    ratios, n_triples = HE.compute_ratios(hltb)
    log(f"Fill ratios from {n_triples} real triples "
        f"({'live median' if n_triples >= HE.MIN_TRIPLES_FOR_LIVE else 'frozen fallback'}): "
        f"1 : {ratios['extra_per_main']:.2f} : {ratios['complete_per_main']:.2f}")

    def time_left():
        return budget - (time.time() - start)

    def maybe_checkpoint(msg):
        nonlocal last_commit
        if time.time() - last_commit > CHECKPOINT_SECONDS:
            save_hltb(hltb)
            git_checkpoint(msg)
            last_commit = time.time()

    # --- Pass 1: resolve never-seen games (priority) ---------------------------- #
    for i, (aid, title) in enumerate(todo, 1):
        if time_left() < TIME_BUFFER:
            log("Time budget reached during first pass; wrapping up.")
            break
        res = hltb_for(title)
        time.sleep(HLTB_DELAY)
        if res is None:
            continue                      # transient error -> leave unresolved, retry next run
        # Build + store the entry (normalizes zeros to null in `raw`, fills missing
        # values from ratios, computes avg, marks `est`, stamps fetched_at, and manages
        # the Phase B blank `attempts` counter). A genuine no-match yields a blank.
        if store_entry(hltb, aid, res, ratios, time.time()):
            n_hit += 1
        else:
            n_blank += 1
        if i % 25 == 0 or i == len(todo):
            log(f"  [new {i}/{len(todo)}] {n_hit} matched, {n_blank} no-match (last: {title[:32]})")
        maybe_checkpoint(f"hltb: {len(hltb)} entries, {n_hit} matched this run (checkpoint)")

    first_pass_done = (time_left() >= TIME_BUFFER)

    # --- Pass 2: re-scrape existing entries with leftover budget (T5) ----------- #
    # Only once the first pass is fully done — otherwise covering new games always wins.
    # Re-fetch is IDENTICAL to a first fetch (hltb_for -> make_entry), so the raw/overwrite
    # model handles merging: real values overwrite raw, estimates recompute, and a transient
    # error (None) leaves the existing entry untouched — we never overwrite good data with a
    # blank. Each refetch restamps fetched_at, so an entry won't be revisited until it's
    # stale again.
    def run_rescrape(work, label):
        """Search + store each (aid, title, bucket) in `work` until budget runs out.
        Shared by the windowed re-scrape and the never-idle drain. Updates the outer
        rescraped/newly_filled counters via nonlocal. Returns count actually processed."""
        nonlocal rescraped, newly_filled
        processed = 0
        for j, (aid, title, _bucket) in enumerate(work, 1):
            if time_left() < TIME_BUFFER:
                log(f"Time budget reached during {label}; wrapping up.")
                break
            res = hltb_for(title)
            time.sleep(HLTB_DELAY)
            if res is None:
                continue                  # transient error -> keep existing entry as-is
            before_real = sum(1 for x in HE.raw_of(hltb[aid]) if HE.is_real(x))
            store_entry(hltb, aid, res, ratios, time.time())
            after_real = sum(1 for x in HE.raw_of(hltb[aid]) if HE.is_real(x))
            rescraped += 1
            processed += 1
            if after_real > before_real:
                newly_filled += 1
            if j % 25 == 0 or j == len(work):
                log(f"  [{label} {j}/{len(work)}] {rescraped} refetched, "
                    f"{newly_filled} gained data (last: {title[:32]})")
            maybe_checkpoint(f"hltb: rescraped {rescraped} ({newly_filled} newly filled), "
                             f"checkpoint")
        return processed

    if first_pass_done:
        queued_aids = set()
        queue = build_rescrape_queue(games, hltb, int(time.time()))
        if queue:
            queued_aids = {aid for aid, _t, _b in queue}
            n_part = sum(1 for _a, _t, b in queue if b == "partial")
            n_bl = sum(1 for _a, _t, b in queue if b == "blank")
            n_fu = sum(1 for _a, _t, b in queue if b == "full")
            log(f"First pass complete. Re-scrape queue: {len(queue)} stale "
                f"({n_part} partial, {n_bl} blank, {n_fu} full); oldest-first within each.")
            run_rescrape(queue, "rescrape")
        else:
            log("First pass complete. No entries are stale enough to re-scrape yet.")

        # --- Never-idle drain: keep working blanks if budget remains (Phase B) ----- #
        # Rather than quit with time on the clock, pull the least-tried, least-recently-
        # tried blanks and keep going. The attempts counter + oldest-first ordering make
        # this converge (each drained blank's attempts rises, backing off its future
        # window), so this recovers title-noise / transient-loss blanks fast without
        # permanently re-hitting genuinely-dead titles.
        if time_left() >= TIME_BUFFER:
            drain = build_idle_drain(games, hltb, int(time.time()), queued_aids)
            if drain:
                log(f"Budget remains — never-idle drain: working {len(drain)} more "
                    f"blank(s), fewest-attempts-first.")
                run_rescrape(drain, "drain")
            else:
                log("Budget remains but no drainable blanks (all resolved or frozen).")
    elif not todo:
        # Nothing new AND no budget left after building — shouldn't normally happen, but
        # keep the file written.
        log("No new games this run.")

    save_hltb(hltb)
    summary = f"hltb: {len(hltb)} entries"
    if n_hit or n_blank:
        summary += f", {n_hit} newly matched"
    if rescraped:
        summary += f", rescraped {rescraped} ({newly_filled} newly filled)"
    git_checkpoint(summary)
    log(f"\nDone. New: {n_hit} matched, {n_blank} no-match. "
        f"Re-scrape: {rescraped} refetched, {newly_filled} gained data. "
        f"hltb.json now has {len(hltb)} entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
