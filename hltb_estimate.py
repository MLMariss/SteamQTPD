#!/usr/bin/env python3
"""
SteamQHPP — HLTB estimation helpers (shared)
============================================
ONE place for the "fill the missing HLTB numbers" logic, imported by
hltb_refresh.py (live, for new games). It was also used by the one-time
hltb_backfill.py sweep of existing entries (since removed); keeping the fill
logic here means the live path and any future re-sweep can never disagree.

The problem this solves
-----------------------
HLTB exposes three completion times: main / main+extra / completionist. Many
games only have one or two of them on howlongtobeat.com. The old code computed
`avg` as the mean of *whatever happened to be present*, so a game with only a
main-story time got avg == main — wildly understating it and skewing QHPP, which
rides on avg by default. A game with only a completionist time got avg == that
long completionist number, overstating a typical playthrough just as badly.

Fix: when 1 or 2 of the 3 are missing (but at least one is real), estimate the
missing ones from the typical ratio between the three times, then compute avg
over the now-complete triple.

Data model (the `raw` ground-truth design)
-------------------------------------------
Each hltb entry looks like:

    {
      "main": 53.4, "extra": 94.8, "complete": 171.8,   # display/QHPP values
      "avg": 106.7,
      "match": "Stardew Valley",
      "fetched_at": 1782817321,                          # when HLTB was last fetched
      "raw": {"main": 53.4, "extra": 94.8, "complete": 171.8},  # exactly what HLTB returned
      "est": ["extra"]                                   # which top-level fields are estimated
    }

  - `raw` holds ONLY genuine HLTB values (positive numbers) or null. Zeros from
    HLTB are normalized to null on the way in — a game cannot be played in zero
    hours, so a 0 is treated as "no value" and gets estimated like a missing one.
  - Top-level main/extra/complete are the *effective* values: real where `raw`
    has them, estimated otherwise. These are what the frontend shows and what
    QHPP uses.
  - `est` lists which top-level fields are estimated (drives the frontend's
    distinct color + tooltip). Absent/empty when nothing is estimated.
  - Estimates are ALWAYS derived from `raw`, never from prior estimates, so the
    estimate quality only ever improves and never compounds error.

Why this makes future re-scraping clean: when a game is re-fetched later, the new
real values overwrite `raw`; estimates are recomputed from the new `raw`; a
blank re-fetch keeps existing data rather than wiping it. (Re-scraping is NOT
done yet — first we complete one full pass over the whole catalog. This model is
just the foundation that makes it trivial when we get there.)

Ratio policy (decided): LIVE median, FIXED median as fallback.
  - The ratio is the MEDIAN across all games whose `raw` has all THREE values.
    Median (not mean) because grind-heavy completionist outliers drag the mean
    up and over-inflate fills for a typical game.
  - Computed live from the current corpus each run, so it self-corrects as more
    real triples arrive.
  - ANTI-POLLUTION GUARD: only `raw` triples feed the ratio. Since `raw` never
    holds estimated values, estimates can never train the ratio and drift it.
  - Cold-start fallback to frozen constants (themselves the median over the 327
    real triples present when this was written) until enough real triples exist.

Anchoring (decided): anchor on whatever real value(s) exist — not just main.
Each missing value is derived from the NEAREST real value via the median ratio
(main<->extra and extra<->complete are adjacent and more reliable than the
main<->complete jump, so we route through extra when possible).
"""

# Frozen fallback ratios — median over the 327 real triples present when written.
# These are the FLAT (non-bucketed) ratios, kept for cold-start and as the per-bucket
# fallback when a magnitude bucket is too thin to trust.
FALLBACK = {
    "extra_per_main":     1.3889,
    "complete_per_main":  2.1860,
    "main_per_extra":     0.7200,
    "complete_per_extra": 1.5739,   # = complete_per_main / extra_per_main
    "main_per_complete":  0.4574,
    "extra_per_complete": 0.6786,
}

# --- Magnitude-bucketed ratios (fixes estimate inflation on the extremes) ----------- #
# A single global ratio applied linearly over-inflates the extremes: e.g. main/complete
# is ~0.60 for short games but ~0.12 for grind/idle games, so a flat 0.46 turns a 1200 h
# completionist into a ~549 h main story (empirical ~142 h). Instead we bucket each ratio
# by the ANCHOR value's magnitude and use the per-bucket median. Validated on held-out
# data: on grind games (complete > 200 h) this cuts median error from ~320% (flat) to ~58%.
#
# Bucket edges: an anchor value falls in bucket i = number of edges it is >=.
#   complete-anchored ratios bucket by complete; main-anchored by main; extra-anchored by extra.
C_EDGES = [10, 30, 80, 200]   # 5 complete buckets: <10 / 10-30 / 30-80 / 80-200 / >200
M_EDGES = [5, 15, 40, 100]    # 5 main buckets
E_EDGES = [8, 20, 50, 150]    # 5 extra buckets

# Which anchor each direction is keyed by (the value we're multiplying FROM).
BUCKET_ANCHOR = {
    "main_per_complete":  "c", "extra_per_complete": "c",   # from a real complete
    "extra_per_main":     "m", "complete_per_main":  "m",   # from a real main
    "main_per_extra":     "e", "complete_per_extra": "e",   # from a real extra
}
_EDGES_FOR = {"c": C_EDGES, "m": M_EDGES, "e": E_EDGES}

# Frozen bucketed fallback (median per bucket over the real triples present when written),
# used at cold-start and for any bucket too thin to compute live.
FALLBACK_BUCKETS = {
    "main_per_complete":  {0: 0.6000, 1: 0.4667, 2: 0.3728, 3: 0.2571, 4: 0.1172},
    "extra_per_complete": {0: 0.7963, 1: 0.7002, 2: 0.6217, 3: 0.5134, 4: 0.3603},
    "extra_per_main":     {0: 1.3333, 1: 1.4583, 2: 1.4812, 3: 1.6238, 4: 1.7112},
    "complete_per_main":  {0: 2.0000, 1: 2.3427, 2: 2.3307, 3: 2.6411, 4: 2.8800},
    "main_per_extra":     {0: 0.8000, 1: 0.6970, 2: 0.6472, 3: 0.5473, 4: 0.3261},
    "complete_per_extra": {0: 1.3511, 1: 1.4706, 2: 1.5181, 3: 1.7990, 4: 2.0680},
}

# Minimum real samples in a magnitude bucket before we trust its live median (else fall
# back to that bucket's frozen value, then to the flat ratio).
MIN_PER_BUCKET = 15


def _bucket_of(value, edges):
    """Bucket index for a value: the count of edges it is >= (0..len(edges))."""
    i = 0
    for ed in edges:
        if value >= ed:
            i += 1
        else:
            break
    return i

# Need at least this many real `raw` triples before trusting a live ratio.
MIN_TRIPLES_FOR_LIVE = 30


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def is_real(x):
    """A usable real value: a positive number. None / 0 / non-numeric are not.
    (Zero is explicitly NOT real — a game cannot be played in zero hours.)"""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def _clean(x):
    """Normalize an incoming HLTB value for `raw`: positive number stays, anything
    else (None, 0, negative, non-numeric) becomes None."""
    return x if is_real(x) else None


def raw_of(entry):
    """The `raw` ground-truth triple for an entry. Falls back to reading the
    top-level fields when an entry predates the `raw` model AND is not marked
    estimated (legacy full/partial-real entries written before this change had
    their real values at top level with no `est`). Anything in `est` is excluded
    so legacy estimated fields can't leak in."""
    if isinstance(entry.get("raw"), dict):
        r = entry["raw"]
        return _clean(r.get("main")), _clean(r.get("extra")), _clean(r.get("complete"))
    # legacy entry: treat non-estimated top-level fields as raw
    est = set(entry.get("est") or [])
    m = None if "main" in est else _clean(entry.get("main"))
    e = None if "extra" in est else _clean(entry.get("extra"))
    c = None if "complete" in est else _clean(entry.get("complete"))
    return m, e, c


def compute_ratios(hltb):
    """Build the ratio table from REAL `raw` triples only. Returns (ratios, n_triples).

    `ratios` carries BOTH the flat medians (backward-compatible keys like
    "main_per_complete") AND magnitude-bucketed tables under `ratios["_buckets"]`
    (keyed direction -> {bucket_index: median}). `fill_entry` prefers the bucketed
    value for the anchor's magnitude and falls back to the flat ratio when a bucket is
    thin. Flat medians fall back to frozen constants when too few triples exist; each
    bucket falls back to its frozen value then the flat ratio.
    """
    em, cm = [], []          # extra/main, complete/main
    me, ce = [], []          # main/extra, complete/extra
    mc, ec = [], []          # main/complete, extra/complete
    # per-direction, per-bucket sample lists
    from collections import defaultdict
    bkt = {k: defaultdict(list) for k in BUCKET_ANCHOR}
    for v in hltb.values():
        m, e, c = raw_of(v)
        if is_real(m) and is_real(e) and is_real(c):
            em.append(e / m); cm.append(c / m)
            me.append(m / e); ce.append(c / e)
            mc.append(m / c); ec.append(e / c)
            # bucket each direction by its anchor's magnitude
            bm = _bucket_of(m, M_EDGES); be = _bucket_of(e, E_EDGES); bc = _bucket_of(c, C_EDGES)
            bkt["main_per_complete"][bc].append(m / c)
            bkt["extra_per_complete"][bc].append(e / c)
            bkt["extra_per_main"][bm].append(e / m)
            bkt["complete_per_main"][bm].append(c / m)
            bkt["main_per_extra"][be].append(m / e)
            bkt["complete_per_extra"][be].append(c / e)
    n = len(em)

    if n < MIN_TRIPLES_FOR_LIVE:
        out = dict(FALLBACK)
        # frozen buckets at cold-start
        out["_buckets"] = {k: dict(v) for k, v in FALLBACK_BUCKETS.items()}
        return out, n

    flat = {
        "extra_per_main":     _median(em),
        "complete_per_main":  _median(cm),
        "main_per_extra":     _median(me),
        "complete_per_extra": _median(ce),
        "main_per_complete":  _median(mc),
        "extra_per_complete": _median(ec),
    }
    # Build live buckets, falling back to frozen bucket value (then flat) when thin.
    buckets = {}
    for direction, per in bkt.items():
        edges = _EDGES_FOR[BUCKET_ANCHOR[direction]]
        n_buckets = len(edges) + 1
        table = {}
        for bi in range(n_buckets):
            vals = per.get(bi, [])
            if len(vals) >= MIN_PER_BUCKET:
                table[bi] = _median(vals)
            elif bi in FALLBACK_BUCKETS[direction]:
                table[bi] = FALLBACK_BUCKETS[direction][bi]
            else:
                table[bi] = flat[direction]
        buckets[direction] = table
    flat["_buckets"] = buckets
    return flat, n


def _ratio(ratios, direction, anchor_value):
    """Look up the ratio for a direction given the anchor's magnitude. Prefers the
    bucketed value; falls back to the flat ratio if buckets are absent."""
    buckets = ratios.get("_buckets")
    if buckets and direction in buckets:
        edges = _EDGES_FOR[BUCKET_ANCHOR[direction]]
        bi = _bucket_of(anchor_value, edges)
        val = buckets[direction].get(bi)
        if val is not None:
            return val
    return ratios[direction]


def _r(x):
    return round(x, 1) if x and x > 0 else None


def make_entry(main, extra, complete, match, fetched_at, ratios):
    """Build a complete hltb entry from freshly-fetched HLTB values. Normalizes
    zeros to null into `raw`, fills missing/zero values from `ratios`, computes
    avg over the completed triple, and marks estimated fields. This is what the
    refresher stores for each new game."""
    rm, re_, rc = _clean(main), _clean(extra), _clean(complete)
    entry = {
        "main": None, "extra": None, "complete": None, "avg": None,
        "match": match,
        "fetched_at": int(fetched_at),
        "raw": {"main": rm, "extra": re_, "complete": rc},
    }
    return fill_entry(entry, ratios)


def fill_entry(entry, ratios):
    """Fill an entry's top-level main/extra/complete from its `raw` ground truth
    plus the ratios, anchoring on whatever real value(s) `raw` provides. Real
    values are copied straight from `raw`; only genuinely-missing ones are
    estimated. Recomputes avg over the completed triple and sets `est`. Mutates
    and returns the entry.

    Anchor routing prefers the nearest real neighbour: main<->extra and
    extra<->complete are adjacent and more reliable than the main<->complete jump,
    so we route through extra when we can.
    """
    rm, re_, rc = raw_of(entry)
    # ensure entry carries a normalized raw block going forward
    entry["raw"] = {"main": rm, "extra": re_, "complete": rc}

    has_m, has_e, has_c = is_real(rm), is_real(re_), is_real(rc)
    present = sum((has_m, has_e, has_c))

    # Nothing real -> fully blank (HLTB had no usable match). No avg, no est.
    if present == 0:
        entry["main"] = entry["extra"] = entry["complete"] = None
        entry["avg"] = None
        entry.pop("est", None)
        return entry

    m, e, c = rm, re_, rc
    est = []

    # --- MAIN ---
    if not has_m:
        if has_e:                         # from real extra (adjacent, preferred)
            m = _r(re_ * _ratio(ratios, "main_per_extra", re_))
        elif has_c:                       # else from real complete
            m = _r(rc * _ratio(ratios, "main_per_complete", rc))
        if m is not None:
            est.append("main")

    # --- EXTRA ---
    if not has_e:
        if has_m:                         # from real main
            e = _r(rm * _ratio(ratios, "extra_per_main", rm))
        elif has_c:                       # from real complete (adjacent)
            e = _r(rc * _ratio(ratios, "extra_per_complete", rc))
        elif m is not None:               # from just-derived main
            e = _r(m * _ratio(ratios, "extra_per_main", m))
        if e is not None:
            est.append("extra")

    # --- COMPLETE ---
    if not has_c:
        if has_e:                         # from real extra (adjacent, preferred)
            c = _r(re_ * _ratio(ratios, "complete_per_extra", re_))
        elif has_m:                       # from real main
            c = _r(rm * _ratio(ratios, "complete_per_main", rm))
        elif e is not None:               # from derived extra
            c = _r(e * _ratio(ratios, "complete_per_extra", e))
        elif m is not None:               # last resort from derived main
            c = _r(m * _ratio(ratios, "complete_per_main", m))
        if c is not None:
            est.append("complete")

    entry["main"], entry["extra"], entry["complete"] = m, e, c

    # With >=1 real value the ratios always yield the other two -> full triple.
    times = [t for t in (m, e, c) if is_real(t)]
    entry["avg"] = round(sum(times) / len(times), 1) if times else None

    if est:
        entry["est"] = est
    else:
        entry.pop("est", None)
    return entry
