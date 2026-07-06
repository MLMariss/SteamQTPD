#!/usr/bin/env python3
"""
SteamQHPP — HLTB title matching helpers (Phase A)
=================================================
HLTB matches by TITLE, not appid, so a title that carries store-page noise
(trademark glyphs, edition suffixes, ALLCAPS, a subtitle after a colon) can drop
below the similarity floor or return zero results even when the game genuinely
exists on howlongtobeat.com. Observed live: "Far Cry® New Dawn" -> no results,
"EDENS ZERO" -> no results, while the plain names match fine.

This module produces an ORDERED list of query variants to try for a given raw
Steam title, cheapest/most-likely first, de-duplicated. `hltb_refresh.hltb_for`
walks the list and stops at the first variant that yields a match at or above the
similarity threshold. Every function here is PURE (no network, no I/O) so it can
be unit-tested and guarded by a self-check assertion at startup.

Design rules:
  - NEVER mutate the caller's title; only build alternates.
  - The raw title is ALWAYS tried first (variant 0) so this can only ever WIDEN
    what matches — it can never make a previously-good match worse.
  - Variants are ordered by how much they alter the string: light cleanup before
    aggressive truncation, so we prefer the closest form that still matches.
  - Bounded output (<= MAX_VARIANTS) so a pathological title can't explode the
    per-game search budget.
"""

import re
import unicodedata

MAX_VARIANTS = 5

# Trademark / copyright / registered glyphs and similar decorations HLTB never carries.
_TRADEMARK = dict.fromkeys(map(ord, "®™©℠"), None)

# Edition / packaging suffixes that Steam appends but HLTB's base entry omits.
# Matched case-insensitively at the END of the title, optionally after a separator.
_EDITION_TAIL = re.compile(
    r"""[\s:–—-]*         # optional separator before the suffix
        (?:
            (?:the\s+)?(?:definitive|deluxe|ultimate|complete|enhanced|standard|
               gold|goty|game\s+of\s+the\s+year|legendary|premium|special|
               collector'?s|anniversary|remastered|remaster|redux|director'?s\s+cut)
            (?:\s+edition|\s+cut)?
          | edition
          | prologue
          | demo
        )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bracketed trailing tags like "(Classic)", "[Beta]".
_TRAILING_BRACKET = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def _collapse_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def _strip_trademark(s):
    return _collapse_ws(s.translate(_TRADEMARK))


def _strip_edition_tail(s):
    prev = None
    # Peel repeatedly: "X: Deluxe Edition" -> "X: Deluxe" is unlikely, but
    # "X Remastered Edition" -> "X" needs one pass; loop guards against stacked tails.
    while prev != s:
        prev = s
        s = _EDITION_TAIL.sub("", s)
        s = _TRAILING_BRACKET.sub("", s)
    return _collapse_ws(s)


def _before_colon(s):
    # Text before the FIRST subtitle separator (colon / dash variants), if any.
    m = re.split(r"\s*[:–—]\s*|\s+-\s+", s, maxsplit=1)
    return _collapse_ws(m[0]) if m else s


def _titlecase_if_allcaps(s):
    # HLTB stores "Edens Zero", not "EDENS ZERO". Only re-case when the title is
    # (almost) entirely uppercase letters, to avoid mangling deliberate casing.
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.9:
        return _collapse_ws(s.title())
    return None


def normalize(title):
    """Single 'best cleaned' form: strip trademark glyphs + edition tail + brackets,
    NFC-normalize unicode, collapse whitespace. Used both as a query variant and by
    the self-check. Pure."""
    if not title:
        return ""
    s = unicodedata.normalize("NFC", title)
    s = _strip_trademark(s)
    s = _strip_edition_tail(s)
    return _collapse_ws(s)


def query_variants(title):
    """Ordered, de-duplicated list of query strings to try for a raw Steam title,
    most-conservative first. The raw title is always element 0. Pure; bounded to
    MAX_VARIANTS.

    Order rationale:
      0. raw title                      (never regress an existing match)
      1. trademark-stripped             (cheapest, highest-yield: "Far Cry® ..." )
      2. + edition/bracket tail removed ("... Deluxe Edition", "(Classic)")
      3. ALLCAPS -> Title Case          ("EDENS ZERO" -> "Edens Zero")
      4. before first colon/subtitle    (last resort: "A: B" -> "A")
    """
    if not title:
        return [title]
    out = []
    seen = set()

    def add(cand):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)

    raw = title
    add(raw)                                   # 0 — always first, always tried
    tm = _strip_trademark(unicodedata.normalize("NFC", raw))
    add(tm)                                     # 1
    ed = _strip_edition_tail(tm)
    add(ed)                                     # 2
    rc = _titlecase_if_allcaps(ed)
    if rc:
        add(rc)                                 # 3
    add(_before_colon(ed))                      # 4

    return out[:MAX_VARIANTS]


# --- Self-check (Phase A regression guard) ------------------------------------ #
# Runs at import time from hltb_refresh via assert_healthy(); a failure here fails
# the whole run rather than silently degrading match quality. These are the exact
# real-world misses that motivated Phase A, plus invariants that must always hold.

_CASES = [
    # (raw title, must-appear-among-variants)
    ("Far Cry® New Dawn", "Far Cry New Dawn"),
    ("EDENS ZERO", "Edens Zero"),
    ("Chainsaw Warrior (Classic)", "Chainsaw Warrior"),
    ("The Witcher 3: Wild Hunt - Game of the Year Edition", "The Witcher 3"),
    ("Portal", "Portal"),                       # clean title unchanged, still present
]


def assert_healthy():
    """Raise AssertionError if the normalizer regresses. Called at startup."""
    for raw, expected in _CASES:
        variants = query_variants(raw)
        assert variants[0] == raw, (
            f"raw title must be tried first: {raw!r} -> {variants!r}")
        assert expected in variants, (
            f"expected cleaned variant {expected!r} not produced for {raw!r}; got {variants!r}")
        assert len(variants) <= MAX_VARIANTS, (
            f"too many variants for {raw!r}: {len(variants)}")
        assert len(variants) == len(set(variants)), (
            f"duplicate variants for {raw!r}: {variants!r}")
    # A clean title must not spawn spurious variants (keeps budget tight).
    assert query_variants("Hades") == ["Hades"], query_variants("Hades")
    return True


if __name__ == "__main__":
    assert_healthy()
    for t in ("Far Cry® New Dawn", "EDENS ZERO",
              "The Witcher 3: Wild Hunt - Game of the Year Edition",
              "Chainsaw Warrior (Classic)", "Hades"):
        print(f"{t!r:60} -> {query_variants(t)}")
    print("hltb_match self-check OK")
