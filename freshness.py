#!/usr/bin/env python3
"""
SteamQHPP — data freshness check
================================
Recomputes the freshness picture from the live files + the workflow crons and
writes FRESHNESS.md. Companion to `coverage.py` / COVERAGE.md, not a duplicate:

    COVERAGE.md  answers  "how much of the catalog do we HAVE?"          (volume)
    FRESHNESS.md answers  "when was each task last run, when does it run
                           next, how much of what it owns is UP TO DATE,
                           and where is the wait too long?"              (time)

Three questions, three tables:

  TABLE 1 — SCHEDULE FRESHNESS (per scheduled task)
    Reads each workflow's real `cron:` lines straight out of `.github/workflows/`
    (never a hardcoded copy — a cron edit shows up here on the next run) and pairs
    them with the *data-side* proof of the last run: the `generated_at` stamp the
    task's own output file carries. From that:
      last refresh -> age -> next scheduled fire -> gap(last -> next)
    `gap` is the headline number: how stale the file will be, at worst, by the
    moment the task next gets a chance to touch it. Status counts MISSED FIRES —
    cron fires that passed without the file being re-stamped — so a 2h task and a
    daily task are each judged against their own promise, never a flat target.

  TABLE 2 — CATALOG UP-TO-DATE vs PENDING (per storage file)
    Covered rows split into up-to-date / pending-refresh (overdue) / pending-fill
    (never) / skipped-by-design. The bucketers are IMPORTED FROM coverage.py
    rather than reimplemented, so the two docs can never disagree about what
    "overdue" means and the cooldown constants live in exactly one place.

  TABLE 3 — PER-GAME WAIT (the gap that actually bites)
    A task running every 3h says nothing about how long one *game* waits: the
    two-track scrapers deliberately park dormant games on a 30–45d cooldown. This
    table shows each file's per-game refresh window next to the observed row-age
    distribution (p50 / p95 / oldest), which is where a real starvation shows up —
    a p95 far past the window means the budget isn't reaching the tail.

Job cadence and per-game cadence are reported separately ON PURPOSE. They fail
differently: a stalled *job* (table 1) is a broken pipeline; a blown *per-game*
window (table 3) with a healthy job is a budget shortfall. Collapsing them into
one "freshness %" would hide which of the two is happening.

Every fire gets the job's own `timeout-minutes` as grace before it can count as
missed: GitHub's scheduler drifts by minutes-to-hours under load and the long
passes stamp their file hours after the cron fired, so a tighter gate would cry
wolf daily.

Stdlib only — no pip install, no network, all reads local and read-only. Pure:
does no git, the workflow commits FRESHNESS.md (same split as shard_health.py).
Run: `python freshness.py`.
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import coverage as CV        # cooldown constants + bucketers, single source of truth

HERE = Path(__file__).resolve().parent
WF_DIR = HERE / ".github" / "workflows"
OUT_FILE = HERE / "FRESHNESS.md"

DAY = 86400
HOUR = 3600
PT_HOT = CV.PT.HOT_REVIEWS_BOOST   # >this many reviews halves a game's playtime cooldown


def _pt_deep_note(d):
    """The SECOND playtime staleness axis, for the 2.3 row's note.

    `pending refresh` above counts games whose SURFACE walk is overdue. But a game
    already holding PER_GAME_CAP reviews only gets its newest page re-read on a normal
    visit — every stored playtime below that stays frozen until a full window re-walk
    (`walk_at`). So a ceiling game can be surface-fresh and still carry medians built
    from months-old playtimes. This is the drift signal; without it the row overstates
    how current the published medians are."""
    if not d["ceiling"]:
        return f"No games at the {CV.PT_CEILING:,}-review ceiling — no deep-walk drift possible."
    med = f"{d['median']:.1f}d" if d["median"] is not None else "—"
    p90 = f"{d['p90']:.1f}d" if d["p90"] is not None else "—"
    opct = f"{d['overdue_pct']:.0f}%" if d["overdue_pct"] is not None else "—"
    note = (f"**Deep re-walk axis:** {d['ceiling']:,} games sit at the "
            f"{CV.PT_CEILING:,}-review ceiling, where a normal visit refreshes only the "
            f"newest page. Their last FULL re-walk (`walk_at`, promised every "
            f"{CV.PT_REWALK_DAYS}d): median {med}, p90 {p90}, "
            f"{d['overdue']:,} ({opct}) past promise.")
    if d["unanchored"]:
        note += (f" **{d['unanchored']:,} have no anchor at all** (never deep-walked). ")
    else:
        note += (" Every ceiling game has its deep clock running. ")
    note += ("Games below the ceiling are walked to the end of Steam's list on every "
             "visit, so this axis does not apply to them.")
    return note

# --- status: MISSED SCHEDULED FIRES, not an age ratio -------------------------
# A flat "age vs cadence" ratio judges a 30-min task and a daily task on different
# terms: a daily job can skip a whole run and still sit at 1.05x. So instead we count
# how many of the task's own cron fires have come and gone WITHOUT the output file
# being re-stamped. Each fire gets a grace allowance = the job's declared
# `timeout-minutes` (an honest upper bound on how late a legitimate run can stamp),
# so a long pass in flight is never mistaken for a missed one.
GRACE_DEFAULT_MIN = 60   # workflows that declare no timeout-minutes
LATE_MISSES = 1          # 🟡 one fire produced no write
STALLED_MISSES = 2       # 🔴 two or more — the task is not writing at all

# --- backlog thresholds (overdue rows / covered rows) ---
BACKLOG_WARN = 0.10
BACKLOG_CRIT = 0.25

# Prices carry no cooldown gate (the whole non-free base is re-batched every run),
# so there is no scraper-declared window to score them against. 24h is OUR
# observation window: at 8 runs/day every priced row should be re-stamped daily.
PRICE_FRESH_WINDOW_H = 24


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# Time formatting
# --------------------------------------------------------------------------- #
def ts_utc(epoch):
    if not epoch:
        return "—"
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M")


def dur(seconds):
    """Human duration: '42m', '3h 12m', '2d 4h', '31d'."""
    if seconds is None:
        return "—"
    s = int(seconds)
    neg = s < 0
    s = abs(s)
    if s < HOUR:
        out = f"{s // 60}m"
    elif s < DAY:
        h, rem = divmod(s, HOUR)
        m = rem // 60
        out = f"{h}h" if m == 0 else f"{h}h {m}m"
    elif s < 100 * DAY:
        d, rem = divmod(s, DAY)
        h = rem // HOUR
        out = f"{d}d" if h == 0 else f"{d}d {h}h"
    else:
        out = f"{s // DAY}d"
    return ("-" + out) if neg else out


# --------------------------------------------------------------------------- #
# Cron: read the REAL schedules out of the workflow files, then project them
# --------------------------------------------------------------------------- #
CRON_RE = re.compile(r"^\s*-\s*cron:\s*[\"']([^\"']+)[\"']", re.M)
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.M)
TIMEOUT_RE = re.compile(r"^\s*timeout-minutes:\s*(\d+)", re.M)


def read_workflow(fname):
    """(display name, [cron strings], timeout minutes) from .github/workflows/<fname>.

    Parsed with a regex rather than a YAML lib on purpose: keeps this script
    stdlib-only (no pip step in the workflow) and the three fields we need are
    unambiguous single-line forms in every workflow here. A commented-out cron
    can't sneak in — `-` and `cron:` must start the line's first token.
    """
    p = WF_DIR / fname
    if not p.is_file():
        return None, [], GRACE_DEFAULT_MIN
    txt = p.read_text(encoding="utf-8")
    name = NAME_RE.search(txt)
    tmo = TIMEOUT_RE.search(txt)
    return ((name.group(1).strip().strip('"') if name else fname),
            CRON_RE.findall(txt),
            int(tmo.group(1)) if tmo else GRACE_DEFAULT_MIN)


def _field(spec, lo, hi):
    """Expand one cron field to a sorted list of ints. Handles *, */n, a-b, a-b/n, lists."""
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step > 1:            # "5/15" == "5-59/15"
                end = hi
        out.update(range(start, end + 1, step))
    return sorted(x for x in out if lo <= x <= hi)


def _fire_after(cron, after_dt):
    """First fire of a single cron strictly after `after_dt` (UTC), or None.

    Only minute+hour fields are projected; every cron in this repo leaves
    day/month/weekday as `*`. Anything else returns None and the caller falls
    back to reporting the raw cron string without a projection — better a blank
    cell than a confidently wrong 'next run'.
    """
    parts = cron.split()
    if len(parts) != 5:
        return None
    m, h, dom, mon, dow = parts
    if (dom, mon, dow) != ("*", "*", "*"):
        return None
    mins, hrs = _field(m, 0, 59), _field(h, 0, 23)
    if not mins or not hrs:
        return None
    t = (after_dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for day in range(2):
        base = t + timedelta(days=day)
        for H in hrs:
            for M in mins:
                cand = base.replace(hour=H, minute=M)
                if cand >= t:
                    return cand
    return None


def next_fire(crons, now_dt):
    """Earliest next fire across all of a workflow's cron lines."""
    fires = [f for f in (_fire_after(c, now_dt) for c in crons) if f]
    return min(fires) if fires else None


def fires_between(crons, start_dt, end_dt, cap=200):
    """Every scheduled fire in (start_dt, end_dt]. `cap` stops a pathological
    schedule (or a very old stamp on a 30-min cron) from spinning forever — the
    count is only ever read as '0, 1, or many'."""
    out = []
    cur = start_dt
    while len(out) < cap:
        nxt = next_fire(crons, cur)
        if not nxt or nxt > end_dt:
            break
        out.append(nxt)
        cur = nxt
    return out


def cadence_seconds(crons, now_dt):
    """WORST-CASE scheduled wait: the largest gap between consecutive fires over
    the next 7 days. Used for the status ratio because judging an irregular cron
    (e.g. `41 4,10,16,22`) by its *average* gap would flag its longest legitimate
    quiet stretch as late."""
    fires = []
    cur = now_dt
    end = now_dt + timedelta(days=7)
    while True:
        nxt = next_fire(crons, cur)
        if not nxt or nxt > end:
            break
        fires.append(nxt)
        cur = nxt
    if len(fires) < 2:
        return None
    return max((b - a).total_seconds() for a, b in zip(fires, fires[1:]))


# --------------------------------------------------------------------------- #
# Data-side "last refresh" stamps
# --------------------------------------------------------------------------- #
def json_stamp(name):
    """`generated_at` of a top-level data file (the task's own proof of its last run)."""
    try:
        p = HERE / name
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8")).get("generated_at")
    except (ValueError, OSError):
        return None


MD_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def md_stamp(name):
    """Generated-timestamp of a generated .md, parsed out of its own header.

    File mtime is useless here — a fresh `actions/checkout` stamps every file with
    the checkout time — and `git log` needs history the doc jobs don't fetch. The
    timestamp each generator prints into its own header is the only honest signal.
    """
    p = HERE / name
    if not p.is_file():
        return None
    head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:12])
    m = MD_TS_RE.search(head)
    if not m:
        return None
    try:
        return int(datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                     "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def age_stats(ts_map, now):
    """(p50, p95, oldest) age in seconds over a {key: stamp} map, ignoring blanks."""
    ages = sorted(now - t for t in ts_map.values() if t)
    if not ages:
        return None, None, None
    return (ages[len(ages) // 2],
            ages[min(len(ages) - 1, int(len(ages) * 0.95))],
            ages[-1])


def missed_fires(crons, stamp, grace_min, now_dt):
    """How many scheduled fires have passed since the file was last written.

    A fire at T only counts as missed once T + grace is in the past, where grace is
    the job's declared `timeout-minutes` — a run that is legitimately still working
    (playtime can run 5.5h) must never read as a miss. Returns None when there is
    nothing to judge (no cron, or no stamp to measure from).
    """
    if not crons or not stamp:
        return None
    stamp_dt = datetime.fromtimestamp(int(stamp), timezone.utc)
    deadline = now_dt - timedelta(minutes=grace_min)
    return len(fires_between(crons, stamp_dt, deadline))


def status_of(missed):
    """🟢/🟡/🔴 from the missed-fire count."""
    if missed is None:
        return "—", 0
    if missed >= STALLED_MISSES:
        return "🔴 stalled", 2
    if missed >= LATE_MISSES:
        return "🟡 late", 1
    return "🟢 on time", 0


def pct(n, d):
    return (n / d * 100.0) if d else 0.0


# --------------------------------------------------------------------------- #
def main():
    now = int(time.time())
    now_dt = datetime.fromtimestamp(now, timezone.utc)

    games_doc = CV.load("games.json")
    if not games_doc:
        log("games.json missing; cannot compute freshness.")
        return 0
    games = games_doc.get("games") or []
    BASE = len(games)
    if BASE == 0:
        log("games.json has no games; nothing to do.")
        return 0

    # ---- per-game staleness keys, one map per storage file ----
    games_ts = {str(g.get("appid")): g.get("scraped_at") or 0 for g in games}

    prices = CV.game_map(CV.load("prices.json"), "prices")
    prices_ts = {k: v.get("scraped_at", 0) for k, v in prices.items()}

    hltb = CV.game_map(CV.load("hltb.json"), "hltb")
    hltb_ts = {k: v.get("fetched_at", 0) for k, v in hltb.items() if isinstance(v, dict)}

    recent = CV.game_map(CV.load("recent.json"), "recent")
    recent_ts = {k: v.get("recent_scraped_at", 0) for k, v in recent.items()}
    recent_empty = {k for k, v in recent.items() if v.get("recent_pct") is None}

    tags = CV.game_map(CV.load("tags.json"), "tags")
    tags_cov = sum(1 for v in tags.values() if v)

    pt_scraped, pt_new, pt_old, pt_deep, pt_deep_unanchored = CV.read_pt_shards()
    upd_ts, upd_pop, upd_shards, upd_new, upd_old = CV.read_upd_shards()
    pics_ts, pics_pop, pics_shards, pics_new, pics_old = CV.read_pics_raw_shards()

    # Two different floors: playtime scrapes from 5 reviews (where a sentiment-split
    # median first becomes guaranteed), updates keeps its own 10. One shared lambda
    # would silently score one layer against the other's gate.
    pt_floor = lambda g: (g.get("review_count") or 0) >= CV.PT_MIN_REVIEWS_FLOOR
    upd_floor = lambda g: (g.get("review_count") or 0) >= CV.UPD_MIN_REVIEWS_FLOOR

    # ---- buckets (coverage.py's gates verbatim — never re-derived here) ----
    b_scraper = CV.schedule_scraper(games)
    b_recent = CV.schedule_two_track(games, recent_ts, CV.RECENT_COOLDOWN_DAYS,
                                     CV.RECENT_NOUPDATE_COOLDOWN_DAYS,
                                     empty_ids=recent_empty)
    b_pt = CV.schedule_playtime(games, pt_scraped, floor_pred=pt_floor)
    b_pt_deep = CV.schedule_playtime_deep(pt_deep, pt_deep_unanchored)
    b_upd = CV.schedule_two_track(games, upd_ts, CV.UPD_COOLDOWN_DAYS,
                                  CV.UPD_NOUPDATE_COOLDOWN_DAYS, floor_pred=upd_floor)
    b_hltb = CV.schedule_hltb(hltb)
    b_pics, pics_have = CV.schedule_pics(pics_ts)

    price_window = PRICE_FRESH_WINDOW_H * HOUR
    price_stale = sum(1 for t in prices_ts.values() if t and (now - t) >= price_window)
    price_fresh = len(prices_ts) - price_stale

    # ---- TASK REGISTRY -----------------------------------------------------
    # One row per scheduled task. `stamp` is the data-side proof of the last run;
    # `cov` is (covered, up_to_date, pending_refresh, pending_fill, skipped) or None
    # for tasks that own no per-game rows. `window` describes the PER-GAME refresh
    # rule, which is a different promise from the cron.
    def two_track_cov(b):
        covered = b["active_total"] + b["dormant_total"]
        return covered, covered - b["overdue"], b["overdue"], b["never"], b["empty"]

    scr_cov = b_scraper["fresh"] + b_scraper["overdue"]
    hltb_cov = len(hltb_ts)
    hltb_over = b_hltb["overdue"]
    pics_cov = len(pics_ts)

    tasks = [
        dict(key="1", kind="data", wf="scrape.yml", label="Catalog scrape", script="scraper.py",
             owns="games.json", stamp=json_stamp("games.json"), ts_map=games_ts,
             cov=(scr_cov, b_scraper["fresh"], b_scraper["overdue"], b_scraper["never"],
                  b_scraper["lm_only"]),
             window="age-tiered 0.25d→15d (≤365d old); older = `last_modified`-driven",
             note="Only finder of new games. `skipped` = games past the last age tier, "
                  "refreshed on Steam's `last_modified` instead of a cooldown."),
        dict(key="2.1", kind="data", wf="prices.yml", label="Prices + sale dates", script="price_and_sale.py",
             owns="prices.json", stamp=json_stamp("prices.json"), ts_map=prices_ts,
             cov=(len(prices_ts), price_fresh, price_stale, 0, 0),
             window=f"no per-game gate — full non-free base re-batched every run "
                    f"({PRICE_FRESH_WINDOW_H}h observation window)",
             note="Fastest-moving layer; rows are re-stamped wholesale each pass, so "
                  "`pending` here means a run wrapped up on its time budget before "
                  "reaching them."),
        dict(key="2.2", kind="data", wf="recent.yml", label="30-day review scores", script="recent_refresh.py",
             owns="recent.json", stamp=json_stamp("recent.json"), ts_map=recent_ts,
             cov=two_track_cov(b_recent),
             window=f"{CV.RECENT_COOLDOWN_DAYS}d active / {CV.RECENT_NOUPDATE_COOLDOWN_DAYS}d dormant",
             note=""),
        dict(key="2.3", kind="data", wf="playtime-raw.yml", label="Playtime raw (shards)",
             script="playtime_refresh.py", owns="playtime_raw/",
             stamp=pt_new, ts_map=pt_scraped,
             cov=(b_pt["covered"], b_pt["covered"] - b_pt["overdue"], b_pt["overdue"],
                  b_pt["never"], b_pt["empty"]),
             window="per-game ladder: "
                    + " · ".join(f"<{d}d→{c}d" for d, c in CV.PT_AGE_TIERS)
                    + f" · else {CV.PT_AGE_FALLBACK_DAYS}d "
                      f"(halved >{PT_HOT:,} reviews; popularity floor "
                    + "/".join(f">{e}→{d}d" for e, d in CV.PT.POPULAR_FLOOR_TIERS) + ")",
             note=_pt_deep_note(b_pt_deep)),
        dict(key="2.4", kind="data", wf="updates.yml", label="Update events (shards)",
             script="updates_refresh.py", owns="updates_raw/",
             stamp=upd_new, ts_map=upd_ts, cov=two_track_cov(b_upd),
             window=f"{CV.UPD_COOLDOWN_DAYS}d active / {CV.UPD_NOUPDATE_COOLDOWN_DAYS}d dormant",
             note=f"{upd_pop}/{upd_shards} shards populated."),
        dict(key="2.5", kind="data", wf="hltb.yml", label="HLTB completion times", script="hltb_refresh.py",
             owns="hltb.json", stamp=json_stamp("hltb.json"), ts_map=hltb_ts,
             cov=(hltb_cov, hltb_cov - hltb_over, hltb_over, BASE - hltb_cov, 0),
             window=f"partial {CV.HLTB_PARTIAL_DAYS}d / full {CV.HLTB_FULL_DAYS}d; blanks back off "
                    f"{CV.HLTB_BLANK_EAGER_DAYS}→{CV.HLTB_BLANK_BACKOFF_DAYS}→{CV.HLTB_BLANK_FREEZE_DAYS}d",
             note=f"Blank rows awaiting retry: {b_hltb['blank_active']:,} active, "
                  f"{b_hltb['blank_frozen']:,} frozen (both inside their window)."),
        dict(key="2.6", kind="data", wf="tags.yml", label="SteamSpy tags", script="tags_refresh.py",
             owns="tags.json", stamp=json_stamp("tags.json"), ts_map={}, cov=None,
             fill_only=True, unresolved=BASE - len(tags),
             window="**none** — no per-entry timestamp, no rescrape gate",
             note=f"Coverage-only: {tags_cov:,} non-empty entries of {len(tags):,} recorded. "
                  f"**Fill-only**: `tags_refresh.py` resolves games that have no entry yet and "
                  f"commits nothing when it finds none, so a quiet run is expected behaviour, "
                  f"not a miss — which is why this row is exempt from the missed-fire gate. "
                  f"Per-game freshness is unmeasurable until a `scraped_at` is stored "
                  f"(COVERAGE.md → Future work)."),
        dict(key="2.7", kind="data", wf="pics.yml", label="PICS metadata", script="pics_refresh.py",
             owns="pics_raw/", stamp=pics_new, ts_map=pics_ts,
             cov=(pics_cov, b_pics["fresh"], b_pics["overdue"], BASE - len(pics_have), 0),
             window=f"flat {CV.PICS_STALE_DAYS}d (`--stale-days`)",
             note=f"{pics_pop}/{pics_shards} shards populated; real per-game `_ts`, so "
                  f"`pending refresh` is exact. Separate CM rate surface, not the storefront budget. "
                  f"A second workflow, `pics-new.yml`, writes the same shards on a 6-hourly "
                  f"`--only-new` pass (never-seen appids only, no re-fetch), so a freshly-listed "
                  f"game gets its store art in ~6h instead of waiting for this row's daily run. "
                  f"It shares this job's concurrency group, so `pics_refresh.py` stays "
                  f"single-writer; the schedule below is the FULL refresh's."),
        dict(key="3.1", kind="derived", wf="playtime-raw.yml", label="Playtime medians (chained)",
             script="playtime_summarize.py", owns="playtime.json",
             stamp=json_stamp("playtime.json"), ts_map={}, cov=None, derived="2.3",
             window="recomputed in full on every 2.3 pass",
             note="Derived — freshness inherits from `playtime_raw/`."),
        dict(key="3.2", kind="derived", wf="playtime-raw.yml", label="Playtime-weighted ratings (chained)",
             script="ratings_summarize.py", owns="ratings.json",
             stamp=json_stamp("ratings.json"), ts_map={}, cov=None, derived="2.3",
             window="recomputed in full on every 2.3 pass",
             note="Derived — freshness inherits from `playtime_raw/`."),
        dict(key="3.3", kind="derived", wf="updates.yml", label="Update events summary (chained)",
             script="updates_summarize.py", owns="updates.json",
             stamp=json_stamp("updates.json"), ts_map={}, cov=None, derived="2.4",
             window="recomputed in full on every 2.4 pass",
             note="Derived — freshness inherits from `updates_raw/`."),
        dict(key="4.1", kind="monitor", wf="shard-health.yml", label="Shard health doc", script="shard_health.py",
             owns="SHARDS.md", stamp=md_stamp("SHARDS.md"), ts_map={}, cov=None,
             window="n/a (monitor)", note=""),
        dict(key="4.2", kind="monitor", wf="coverage.yml", label="Coverage doc", script="coverage.py",
             owns="COVERAGE.md", stamp=md_stamp("COVERAGE.md"), ts_map={}, cov=None,
             follows="scrape.yml", window="n/a (monitor)",
             note="Triggered by `workflow_run` after task 1 succeeds — its cadence IS the "
                  "scrape cadence. A stalled row here usually means the `workflow_run` name "
                  "link broke, not that the scrape stopped (that exact bug bit once — §4)."),
        dict(key="4.3", kind="monitor", wf="freshness.yml", label="Freshness doc (this file)",
             script="freshness.py", owns="FRESHNESS.md", stamp=md_stamp("FRESHNESS.md"),
             ts_map={}, cov=None, window="n/a (monitor)",
             note="Self-report: the stamp in table 1 is the PREVIOUS run's — this run has "
                  "not written the file yet at the moment the row is computed."),
        dict(key="0", kind="publish", wf="pages.yml", label="Pages deploy", script="—",
             owns="(site)", stamp=None, ts_map={}, cov=None, window="n/a (publish)",
             note="Republishes current `main`. Leaves no artifact in the repo, so there is "
                  "no local stamp to read — schedule only."),
    ]

    # ---- resolve schedules ----
    for t in tasks:
        wf_name, crons, timeout_min = read_workflow(t["wf"])
        t["wf_name"] = wf_name or t["wf"]
        t["crons"] = crons
        t["grace"] = timeout_min
        if t.get("follows"):
            # workflow_run: fires when the upstream job *finishes*, so it inherits the
            # upstream cron AND owes it the upstream's whole runtime as extra grace.
            _, up_crons, up_timeout = read_workflow(t["follows"])
            t["crons"] = up_crons
            t["grace"] = timeout_min + up_timeout
        t["next"] = next_fire(t["crons"], now_dt)
        t["cadence"] = cadence_seconds(t["crons"], now_dt)
        t["age"] = (now - t["stamp"]) if t.get("stamp") else None
        t["gap"] = ((t["next"].timestamp() - t["stamp"])
                    if (t.get("stamp") and t["next"]) else None)
        t["missed"] = missed_fires(t["crons"], t.get("stamp"), t["grace"], now_dt)
        t["status"], t["sev"] = status_of(t["missed"])
        if t.get("fill_only"):
            # A fill-only task writes only while unfilled rows remain, so silence is the
            # expected steady state once the frontier drains — scoring it on missed fires
            # would print a red light every single day for a job that is working fine.
            t["status"], t["sev"] = "🔵 fill-only", 0

    # ---- alerts ------------------------------------------------------------
    alerts = []
    for t in tasks:
        if t.get("fill_only"):
            alerts.append(f"🔵 **{t['key']} {t['label']} has no refresh cycle at all** — "
                          f"`{t['owns']}` is written only while unresolved games remain "
                          f"({t['unresolved']:,} left of {BASE:,}); once that frontier drains "
                          f"the file stops changing and its data quietly freezes. Last write "
                          f"{dur(t['age'])} ago. This is the pipeline's structural freshness "
                          f"gap — not a broken run — and closing it needs a per-entry "
                          f"`scraped_at` plus a rescrape cadence (COVERAGE.md → Future work).")
            continue
        if t["sev"] == 2:
            alerts.append(f"🔴 **{t['key']} {t['label']} is not writing** — {t['missed']} scheduled "
                          f"fires have passed with no new `{t['owns']}`; last write was "
                          f"{dur(t['age'])} ago ({ts_utc(t['stamp'])} UTC) on a "
                          f"{dur(t['cadence'])} cadence. Check the Actions tab for "
                          f"`{t['wf_name']}` — runs are failing, cancelling, or wrapping up "
                          f"empty.")
        elif t["sev"] == 1:
            alerts.append(f"🟡 **{t['key']} {t['label']} missed a run** — one scheduled fire "
                          f"passed without re-stamping `{t['owns']}` (last write {dur(t['age'])} "
                          f"ago, {dur(t['cadence'])} cadence). One miss is usually scheduler "
                          f"drift or a run that wrapped up on its time budget; worth a look "
                          f"only if it repeats.")
    for t in tasks:
        if not t.get("cov"):
            continue
        covered, _up, over, _never, _skip = t["cov"]
        if not covered or not over:
            continue
        share = over / covered
        if share >= BACKLOG_CRIT:
            alerts.append(f"🔴 **{t['key']} {t['label']} backlog** — {over:,} of {covered:,} rows "
                          f"({share*100:.0f}%) are past their own refresh window. The job is "
                          f"running but not keeping up with its per-game promise ({t['window']}).")
        elif share >= BACKLOG_WARN:
            alerts.append(f"🟡 **{t['key']} {t['label']} backlog** — {over:,} of {covered:,} rows "
                          f"({share*100:.0f}%) are past their refresh window.")
    if not alerts:
        alerts.append("🟢 **All scheduled tasks are on time and no file has a material backlog.**")

    fills = sum(1 for t in tasks if t.get("fill_only"))
    greens = sum(1 for t in tasks
                 if t["sev"] == 0 and t["age"] is not None and not t.get("fill_only"))
    yellows = sum(1 for t in tasks if t["sev"] == 1)
    reds = sum(1 for t in tasks if t["sev"] == 2)

    # ---- render ------------------------------------------------------------
    L = []
    L.append("# SteamQHPP — Data Freshness Check")
    L.append("")
    L.append("> Generated by `freshness.py` (workflow `4.3`), **daily at 07:00 UTC**. Do not edit "
             "by hand — the next run overwrites it. Companion to [COVERAGE.md](COVERAGE.md): "
             "coverage answers *how much do we have*, this answers *when was it last touched, "
             "when is it touched next, and where is that gap too big*. Design notes: "
             "ARCHITECTURE §11.6.")
    L.append("")
    L.append(f"**Snapshot generated (UTC):** {ts_utc(now)}  ")
    L.append(f"**Base universe (`games.json`):** {BASE:,} games  ")
    L.append(f"**Scheduled tasks:** {greens} on time · {yellows} late · {reds} stalled · "
             f"{fills} fill-only (no refresh cycle)")
    L.append("")
    L.append("## Alerts")
    L.append("")
    for a in alerts:
        L.append(f"- {a}")
    L.append("")
    L.append("---")
    L.append("")

    # ---- TABLE 1 ----
    L.append("## 1. Schedule freshness — when each task last ran and when it runs next")
    L.append("")
    L.append("`Last refresh` is the **data-side** stamp (the `generated_at` the task's own output "
             "carries), not \"the workflow reported success\" — a green run that wrote nothing "
             "shows up here as an ageing file. `Cadence` is the **worst-case** scheduled wait "
             "implied by the cron. `Gap (last → next)` is the headline: how stale the file will "
             "be, at worst, by the time the task next gets to touch it.")
    L.append("")
    L.append("| # | Task → file | Cron (UTC) | Cadence | Last refresh (UTC) | Age | Missed | Next run (UTC) | Gap (last → next) | Status |")
    L.append("|---|---|---|---|---|---:|---:|---|---:|---|")
    for t in tasks:
        cron = " · ".join(f"`{c}`" for c in t["crons"]) if t["crons"] else "chained/manual"
        if t.get("follows"):
            cron = f"after task 1 · {cron}"
        if t.get("derived"):
            cron = f"chained to {t['derived']} · {cron}"
        nxt = t["next"]
        nxt_cell = (f"{nxt.strftime('%Y-%m-%d %H:%M')} (in {dur(nxt.timestamp() - now)})"
                    if nxt else "—")
        L.append(
            f"| {t['key']} | {t['label']} → `{t['owns']}` | {cron} | {dur(t['cadence'])} "
            f"| {ts_utc(t['stamp'])} | {dur(t['age'])} "
            f"| {'n/a' if t.get('fill_only') else '—' if t['missed'] is None else t['missed']} "
            f"| {nxt_cell} "
            f"| {dur(t['gap'])} | {t['status']} |")
    L.append("")
    L.append(f"**Missed** = scheduled fires that came and went without the output file being "
             f"re-stamped, each given the job's own `timeout-minutes` as grace so a long pass "
             f"still in flight never counts against it (playtime may legitimately stamp 5.5h "
             f"after its cron). 🟢 0 · 🟡 {LATE_MISSES} · 🔴 {STALLED_MISSES}+. This is a truer "
             f"gate than an age-vs-cadence ratio, which lets a daily job skip a whole run and "
             f"still look fine. 🔵 **fill-only** marks a task that writes only while unfilled "
             f"rows remain — silence there is expected, so it is exempt from the gate and "
             f"called out in Alerts instead.")
    L.append("")
    L.append("*Caveat: a file is only re-stamped when its task actually writes it. Every scraper "
             "here rewrites its `generated_at` on each successful pass, so a miss really does "
             "mean \"no successful write\" — but it says nothing about whether the run failed "
             "loudly or just wrapped up on its time budget. The Actions tab has that answer.*")
    L.append("")
    L.append("---")
    L.append("")

    # ---- TABLE 2 ----
    L.append("## 2. How much is up to date, how much is pending")
    L.append("")
    L.append("Per-game rows split by **each scraper's own gate** (constants imported from "
             "`coverage.py`, so this can't drift from COVERAGE.md). **Up to date** = inside its "
             "refresh window. **Pending refresh** = past the window, the real backlog. "
             "**Pending fill** = never fetched, the frontier. **Skipped by design** = correctly "
             "not scraped (below the 10-review floor, or a `last_modified`-driven game).")
    L.append("")
    L.append("| # | Task | Store | Covered | Up to date | % | Pending refresh | Pending fill | Skipped by design |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for t in tasks:
        if not t.get("cov"):
            continue
        covered, up, over, never, skip = t["cov"]
        L.append(f"| {t['key']} | {t['label']} | `{t['owns']}` | {covered:,} | {up:,} "
                 f"| {pct(up, covered):.1f}% | {over:,} | {never:,} | {skip:,} |")
    L.append("")
    L.append("Every row above is measured against a **real per-game timestamp** and its own "
             "scraper's real gate. For 2.3 that is the surface walk (`scraped_at`); its second "
             "axis, the full re-walk of ceiling games, is in the row note rather than this table "
             "because it applies to only part of the population.")
    L.append("")
    L.append("---")
    L.append("")

    # ---- TABLE 3 ----
    L.append("## 3. Per-game wait — where the gap actually gets big")
    L.append("")
    L.append("A task running every 3h does **not** mean every game is 3h fresh. Each scraper "
             "spends its budget on a queue, and the two-track ones park dormant games on a long "
             "cooldown *by design*. This table puts the promised per-game window next to what the "
             "data actually looks like: **p50** = typical row, **p95** = the tail, **oldest** = "
             "worst row in the file. p95 far past the window is where the gap is too big.")
    L.append("")
    L.append("| # | Store | Per-game refresh window | p50 age | p95 age | Oldest row |")
    L.append("|---|---|---|---:|---:|---:|")
    for t in tasks:
        if not t.get("ts_map"):
            continue
        p50, p95, oldest = age_stats(t["ts_map"], now)
        L.append(f"| {t['key']} | `{t['owns']}` | {t['window']} | {dur(p50)} | {dur(p95)} "
                 f"| {dur(oldest)} |")
    L.append("")
    L.append("Row-age distributions cover **every row in the file**, including rows the window "
             "column deliberately excludes — `games.json`'s p50 is dominated by the 100k "
             "`last_modified`-driven games that are on no cooldown at all. The window describes "
             "the gate; the percentiles describe the file.")
    L.append("")
    blind = [t for t in tasks if t.get("kind") == "data" and not t.get("ts_map")]
    if blind:
        L.append("**Blind spots — data files with no per-game staleness signal at all:**")
        L.append("")
        for t in blind:
            L.append(f"- **{t['key']} `{t['owns']}`** — {t['window']}. {t['note']}")
        L.append("")
    inherit = [t for t in tasks if t.get("kind") in ("derived", "monitor", "publish")]
    L.append("Not in this table by design: " +
             ", ".join(f"`{t['owns']}`" for t in inherit) +
             " — derived files are rebuilt wholesale from their parent (so their freshness *is* "
             "the parent's row above), and the monitor docs plus the Pages deploy hold no "
             "per-game rows. Their own timing is table 1.")
    L.append("")
    L.append("---")
    L.append("")

    # ---- per-task notes ----
    L.append("## 4. Task notes")
    L.append("")
    for t in tasks:
        if t.get("note"):
            L.append(f"- **{t['key']} {t['label']}** (`{t['wf']}` → `{t['script']}`): {t['note']}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## How to read this")
    L.append("")
    L.append("**Two different clocks, kept apart on purpose.** Table 1 measures the *job*: is the "
             "cron firing and is the file being written. Table 3 measures the *game*: how long an "
             "individual title waits for its turn. They fail in different ways and want different "
             "fixes — a stalled job is a broken pipeline (check Actions), while a healthy job with "
             "a blown p95 is a budget shortfall (raise the run minutes, widen the slots, or accept "
             "the tail).")
    L.append("")
    L.append("**A big gap is not automatically a bug.** Dormant games sit on 30–45d cooldowns "
             "because their data does not move; HLTB completion times are near-static and refresh "
             "yearly. The numbers to react to are the ones this doc flags in **Alerts** — a task "
             f"that has missed {STALLED_MISSES}+ of its own scheduled fires, or a file whose "
             f"backlog crosses {int(BACKLOG_CRIT*100)}% of covered rows.")
    L.append("")
    L.append("**Known blind spots.** `tags.json` has no timestamp at all, so it cannot appear in "
             "tables 2–3 — tags are fetched once and never rechecked. Tracked in COVERAGE.md → "
             "Future work; adding a per-entry `scraped_at` would give it a real row here.")
    L.append("")

    OUT_FILE.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"Wrote FRESHNESS.md — {len(tasks)} tasks · {greens} on time / {yellows} late / "
        f"{reds} stalled / {fills} fill-only · base {BASE:,}.")
    for a in alerts:
        log("  " + re.sub(r"[*`]", "", a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
