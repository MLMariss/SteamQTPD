# Decision Memo — Should PICS cover upcoming (unreleased) games?

**Date:** 2026-07-16
**Author:** MLMariss + Claude working session
**Status:** PARKED — recorded for reference, decision deferred to far future.
No code changed. Revisit only if pre-release/coming-soon discovery becomes a
product goal.
**Prompted by:** now that the PICS full sweep landed a ~123.5k-game metadata
dump, is it worth revisiting the long-standing "skip unreleased games" rule?

---

## Parked — current state in one line

PICS covers **released games only** (inherited from `games.json`); of the
**51,533** unreleased games in the `pending` waiting room, only **458 (0.9%)**
are in PICS, and **51,532 of the 51,533 are undated/TBA** (exactly 1 has a
concrete future date). The metadata dump did not touch upcoming games. No change
is being made now; the analysis below is kept so the decision can be picked up
later without re-deriving it.

---

## TL;DR

**The PICS data dump does not change the upcoming-games picture, because PICS
inherited the same released-only gate.** PICS scrapes from the released catalog,
so of the **51,533 games** sitting in the `pending` (unreleased) waiting room,
only **458 (0.9%)** are in `pics_raw/`. The "huge data dump" swept released
games only.

The real question is therefore unchanged by the dump: *should we start harvesting
metadata for unreleased games at all?* My recommendation is **a narrow yes for
PICS specifically, gated to dated-and-near releases — but NOT a general policy
change**, because 51,532 of the 51,533 pending games are undated/TBA, where the
payoff is near-zero and the cost is real.

---

## 1. How the gate works today

Two separate things are being conflated when we say "we skip upcoming games":

- **`games.json` core (`scraper.py`)** deliberately stores released games only.
  Unreleased apps go to `catalog["pending"] = {appid: release_ts|null}`, cost one
  `appdetails` probe, and are **promoted to the scrape queue the moment their
  release date passes**. Nothing is permanently skipped. This is a sound design:
  an unreleased game has no reviews, no playtime, no real price signal, no update
  history — the columns the site sorts on are all empty, so an unreleased shell
  is dead weight in `games.json`.

- **PICS (`pics_refresh.py`)** reads its worklist from the released catalog
  appids. So it never sees the `pending` set. This wasn't a PICS decision — it's
  inherited from feeding off `games.json`.

The distinction matters because **PICS is the one pipeline where unreleased games
actually carry useful data.** Unlike reviews/playtime/price, the PICS `common`
block is fully populated pre-release: store_tags, genres, dev/publisher, Steam
Deck compat (sometimes), `aicontenttype`, `releasestate: "prerelease"`,
`steam_release_date`, supported languages. A coming-soon game has a real tag
list and a real AI-disclosure flag; it just has no reviews yet.

---

## 2. The number that kills the general case

Of the 51,533 pending games:

| Bucket | Count | Meaning |
|---|---:|---|
| Concrete future date | **1** | has a real `release_ts` in the future |
| Undated / TBA | **51,532** | "Q4 2026", "2027", "Coming Soon", "To be announced" |

**99.998% of the waiting room is undated.** This is the Steam long tail: tens of
thousands of perpetually-"coming soon" apps, many of which will never ship or
will sit in limbo for years. Harvesting PICS for all of them means paying a
recurring scrape+storage+refresh cost on a set that is overwhelmingly noise, to
surface a handful of genuinely-imminent titles.

So a blanket "PICS the whole pending set" is a bad trade: +40% archive/refresh
load for ~1 dated title of real value plus a very long noise tail.

---

## 3. Where the value actually is

The value is concentrated in a tiny, high-signal slice:

- **Dated, near-term releases** (concrete `release_ts` within, say, 90 days).
  Today that's ~1 game, but that's a snapshot artifact — dated near-term
  announcements flow in continuously (SGF season, publisher dated reveals). This
  is exactly the "wishlist-worthy, coming soon" cohort your audience and the
  frontend's discovery angle care about.
- **The 470 `prerelease` titles already in `pics_raw/`** (games that leaked into
  the sweep). We're already paying to store these — and `releasestate` isn't even
  emitted to `pics/` yet (see PICS spec §10). That's a free win independent of any
  gate change.

---

## 4. Options

**Option A — Do nothing.** Keep PICS released-only. Zero cost. We forgo pre-release
metadata entirely. Coming-soon games appear in the site only once they ship and
get their first PICS pass.

**Option B — Narrow inclusion: dated-and-near only (recommended).** Feed PICS an
extra worklist = pending games with a concrete `release_ts` within N days
(e.g. 90). Skip the 51,532 undated ones entirely. Cost is bounded to the dated
near-term cohort (tens to low hundreds at a time, not 51k). These get real tags /
Deck / AI-disclosure / languages before launch, and flip to the normal path the
moment they release. Pairs naturally with emitting `releasestate` to `pics/` so
the frontend can badge/filter "Coming soon".

**Option C — Full pending inclusion.** PICS everything in `pending`. Rejected:
§2 shows 99.998% is undated noise; +40% load for almost no marginal signal, plus
churn as TBA games mutate.

**Option D — Just surface what we already have.** Independent of the gate: emit
`releasestate` from `pics_raw/` to `pics/` (summarizer-only, no scrape) so the
470 prerelease titles already swept become filterable. Cheap, do this regardless
of A/B/C.

---

## 5. If/when this is revisited — options at a glance

Not being actioned now. When picked up:

1. **Emit `releasestate` to `pics/`** (option D) — summarizer-only, no scrape,
   unlocks the 470 prerelease titles already in raw. The cheapest step and the
   natural first move.
2. **Dated-and-near inclusion** (option B) — feed PICS a second worklist of
   pending games with a concrete `release_ts` within N days. Only worth it if
   pre-release discovery becomes a product goal.
3. **Do NOT change the `games.json` core gate** — reviews/playtime/price/updates
   genuinely have nothing to scrape pre-release; released-only is correct there.

**The fork to answer first:** does the site want to do coming-soon discovery at
all? If never → this stays parked permanently and the released-only gate is
simply correct. If someday yes → D first, then B.

---

## 6. What this memo did NOT touch

No code was changed. No pipeline was re-run. The `content_descriptors` Layer-1
trim bug and the unemitted `releasestate` are logged in
`PICS_METADATA_PIPELINE.md §10`; option D above is the same `releasestate` item.
