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
FALLBACK = {
    "extra_per_main":     1.3889,
    "complete_per_main":  2.1860,
    "main_per_extra":     0.7200,
    "complete_per_extra": 1.5739,   # = complete_per_main / extra_per_main
    "main_per_complete":  0.4574,
    "extra_per_complete": 0.6786,
}

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
    """Build the ratio table from REAL `raw` triples only. Falls back to frozen
    constants when too few exist. Returns (ratios, n_triples)."""
    em, cm = [], []          # extra/main, complete/main
    me, ce = [], []          # main/extra, complete/extra
    mc, ec = [], []          # main/complete, extra/complete
    for v in hltb.values():
        m, e, c = raw_of(v)
        if is_real(m) and is_real(e) and is_real(c):
            em.append(e / m); cm.append(c / m)
            me.append(m / e); ce.append(c / e)
            mc.append(m / c); ec.append(e / c)
    n = len(em)
    if n < MIN_TRIPLES_FOR_LIVE:
        return dict(FALLBACK), n
    return {
        "extra_per_main":     _median(em),
        "complete_per_main":  _median(cm),
        "main_per_extra":     _median(me),
        "complete_per_extra": _median(ce),
        "main_per_complete":  _median(mc),
        "extra_per_complete": _median(ec),
    }, n


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
            m = _r(re_ * ratios["main_per_extra"])
        elif has_c:                       # else from real complete
            m = _r(rc * ratios["main_per_complete"])
        if m is not None:
            est.append("main")

    # --- EXTRA ---
    if not has_e:
        if has_m:                         # from real main
            e = _r(rm * ratios["extra_per_main"])
        elif has_c:                       # from real complete (adjacent)
            e = _r(rc * ratios["extra_per_complete"])
        elif m is not None:               # from just-derived main
            e = _r(m * ratios["extra_per_main"])
        if e is not None:
            est.append("extra")

    # --- COMPLETE ---
    if not has_c:
        if has_e:                         # from real extra (adjacent, preferred)
            c = _r(re_ * ratios["complete_per_extra"])
        elif has_m:                       # from real main
            c = _r(rm * ratios["complete_per_main"])
        elif e is not None:               # from derived extra
            c = _r(e * ratios["complete_per_extra"])
        elif m is not None:               # last resort from derived main
            c = _r(m * ratios["complete_per_main"])
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
