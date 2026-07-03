# QHPP — Architecture

The engineering deep-dive for **QHPP** (Quality Hours Per Price), a static Steam
value-hunter that ranks games by quality-adjusted playtime per dollar. For the quick
overview and setup, see **[README.md](README.md)**; this document explains *why* the
system is shaped the way it is, every job and data file, and how the frontend turns raw
JSON into the table.

---

## 1. Design principles

**Static-first, server-side scraping.** The site is plain files on GitHub Pages. There is
no application server. Steam sends no CORS headers, so the browser cannot call Steam
directly; all scraping runs inside **GitHub Actions** and the results are committed to the
repo as JSON. The frontend only ever reads static JSON. The single exception is the
optional wishlist import, which needs a live cross-origin call and therefore routes through
a small **Cloudflare Worker** proxy (§11).

**One writer per file.** Every data layer is owned by exactly one job. No two jobs ever
write the same file. This is the load-bearing decision that makes parallel scheduled
Actions safe: two jobs committing *different* files always rebase-merge cleanly, so they
can run and push concurrently without lock-step coordination or lost work. Adding a data
source means adding a file + a job, never touching another job's file.

**Merge in the browser.** The frontend downloads each file and merges them by `appid` into
one in-memory object per game, in a single O(n) pass at load. QHPP is computed client-side
from the merged fields (§10) — never stored server-side — so the score responds instantly
to the price-basis and HLTB-metric toggles without re-scraping.

**Time-budgeted, checkpoint-committing jobs.** Actions cap at 6 hours per job. Scrapers run
for a `RUN_MINUTES` budget and commit progress on an interval (`CHECKPOINT_SECONDS`), plus
on graceful shutdown, so hitting the wall (or a runner interruption) never loses more than
one checkpoint's worth of work.

---

## 2. System topology

```
                 GitHub Actions (scheduled)                     GitHub Pages
   ┌───────────────────────────────────────────────┐        ┌──────────────────┐
   │ scraper.py ─────────────► games.json + catalog │        │                  │
   │ price_and_sale.py ──────► prices.json          │        │   index.html     │
   │ hltb_refresh.py ────────► hltb.json            │  read  │  (merges all     │
   │ tags_refresh.py ────────► tags.json            │ ─────► │   JSON by appid, │
   │ recent_refresh.py ──────► recent.json          │        │   computes QHPP) │
   │ playtime_refresh.py ────► playtime_raw.json    │        │                  │
   │        │                                        │        └──────────────────┘
   │        ├─ playtime_summarize.py ─► playtime.json│
   │        └─ ratings_summarize.py ──► ratings.json │        ┌──────────────────┐
   └───────────────────────────────────────────────┘        │ Cloudflare Worker │
                                                    wishlist  │  (steamid/vanity  │
                                        index.html ◄────────► │   → wishlist)     │
                                                              └──────────────────┘
```

All scraping is server-side; the browser only reads JSON and (optionally) calls the Worker.

---

## 3. Jobs & workflows

Each job is a workflow in `.github/workflows/`. All use `actions/checkout@v5` +
`actions/setup-python@v6` (Node 24). **v5/v6 is deliberate, not the newest** — checkout v5
preserves the credential-persistence behavior the `fetch → rebase → push` commit pattern
depends on; v6/v7 changed it. Every writer job has `permissions: contents: write` and a
`concurrency` group so a job never overlaps itself.

**Steam-facing scrapers** (compete for the storefront rate budget only where noted):

| Workflow / script       | Owns file           | Cadence          | Notes |
|-------------------------|---------------------|------------------|-------|
| `scraper.py`            | `games.json`, `catalog.json` | long runs, off-peak | The only finder of *new* games. |
| `price_and_sale.py`     | `prices.json`       | frequent         | Fast-changing layer: price, discount, sale end. |
| `recent_refresh.py`     | `recent.json`       | rolling          | 30-day review score; offset cron for freshness. |
| `playtime_refresh.py`   | `playtime_raw.json` | overnight        | Per-review playtime; commits every 30 min + on shutdown. |

**Non-Steam scrapers** (hit their own sites, so no Steam-budget contention):

| Workflow / script   | Owns file   | Source    |
|---------------------|-------------|-----------|
| `hltb_refresh.py`   | `hltb.json` | HowLongToBeat |
| `tags_refresh.py`   | `tags.json` | SteamSpy  |

**Summarizers** (pure local recompute — read one file, write another; **no Steam calls**,
so they touch no rate budget). Both read `playtime_raw.json`:

| Workflow file           | Script                  | Writes         | Cron       | Concurrency group |
|-------------------------|-------------------------|----------------|------------|-------------------|
| `playtime-summary.yml`  | `playtime_summarize.py` | `playtime.json`| `50 5 * * *` | `steam-playtime-summary` |
| `playtime-ratings.yml`  | `ratings_summarize.py`  | `ratings.json` | `55 5 * * *` | `steam-playtime-ratings` |

They run right after the overnight raw scrape and a few minutes apart, so each summarizes
fresh data. Because they don't call Steam and finish in seconds, their 15-minute timeout is
generous.

**One-off (deletable) workflows.** `backfill-hltb.yml` runs `hltb_backfill.py` once to apply
the HLTB estimation model to existing entries; it shares the `steam-hltb` concurrency group
so it can't clobber an in-progress HLTB scrape. `backfill_updates.py` and `cleanup_shells.py`
are similar run-once utilities. Once run, these can be removed.

---

## 4. Data files & schemas

All frontend files are compact JSON (minified, `ensure_ascii=False`). Lean summaries use
**positional arrays** with a `_format` key in the meta so the frontend can read by index.

**`games.json`** — `{ "games": [ { appid, title, url, release_ts, rating_pct, review_count,
tags/genres, last_update_ts, … } ] }`. The catalog the frontend iterates.

**`catalog.json`** — the scraper's own state: what's been seen, the `pending` room of
unreleased games waiting on their release date, cursor/enumeration bookkeeping. Not read by
the frontend.

**`prices.json`** — `{ "prices": { "<appid>": { price_initial, price_final, discount_pct,
discount_end, is_free } } }`. The fast-changing layer; `discount_end` drives the live
countdown.

**`hltb.json`** — `{ "<appid>": { hltb_main, hltb_extra, hltb_complete, hltb_avg,
raw: {…}, hltb_est: ["extra", …], fetched_at } }`. `raw` holds the ground-truth values as
returned by HLTB; the top-level values may include estimates filled from the genre ratio
(§7). `hltb_est` lists which of the three were estimated (drives the blue flag). `fetched_at`
is groundwork for the priority re-scrape.

**`tags.json`** — `{ "tags": { "<appid>": ["Roguelike", …] } }`. SteamSpy user tags; the
frontend falls back to Steam genres when a game is absent, so the column is never blank.

**`recent.json`** — `{ "recent": { "<appid>": { recent_pct, recent_count,
recent_scraped_at } } }`. The 30-day score; staleness gates the trend arrow.

**`playtime_raw.json`** — the big working set, owned by `playtime_refresh.py`:
`{ "games": { "<appid>": { "reviews": { "<recommendationid>": { playtime, voted_up, … } },
"summary": {…} } } }`. Reviews are keyed by **`recommendationid`** (identity, not cursor
position) so re-runs catch new reviews and updated playtimes without duplication. Kept
in-repo (not gitignored) because it *is* the scraper's resumable state.

**`playtime.json`** — lean frontend summary, owned by `playtime_summarize.py`:
```
{ "generated_at", "count",
  "_format": ["median_up_min", "median_down_min", "n_up", "n_down"],
  "playtime": { "<appid>": [ median_up_min, median_down_min, n_up, n_down ] } }
```
`median_up` = recommenders' median playtime (minutes), `median_down` = non-recommenders'.
Games with **fewer than 3 reviews on a side** get no median for that side; games with no
trustworthy median at all are omitted entirely.

**`ratings.json`** — lean frontend summary, owned by `ratings_summarize.py`:
```
{ "generated_at",
  "min_reviews": 5,          // hard floor: below this, no rating computed
  "confident_reviews": 10,   // >= this: full color; 5–9: grayed as low-confidence
  "cap_mult": 2.0,           // per-review playtime capped at 2× the game's median
  "per_game_cap": {…},
  "_format": ["steam_pct", "raw_pct", "capped_pct", "n"],
  "count",
  "playtime_ratings": { "<appid>": [ steam_pct, raw_pct, capped_pct, n ] } }
```
See §9 for what the three percentages mean and why.

---

## 5. The main scraper (`scraper.py` → `games.json`)

The only job that discovers new games. Each run:

1. **Enumerate the catalog** via Steam's `IStoreService/GetAppList` (needs the free
   `STEAM_API_KEY`): games-only, appid-ordered, each with a `last_modified` timestamp.
   Without a key it falls back to the keyless `ISteamApps/GetAppList/v2`, which lists all
   app types and has no timestamps.
2. **Refresh changed games first** — any stored game whose `last_modified` advanced past the
   time we last scraped it (true change-detection when the key is present; a `REFRESH_DAYS`
   timer otherwise). Then **scrape new games**, `NEW_ORDER` (`"newest"` by default) first.
3. **Only store released games.** Unreleased ones wait in `catalog["pending"]` and are
   promoted the instant their release date passes (`cleanup_shells.py` files stray "empty
   shell" entries back into that room).
4. **Run for `RUN_MINUTES`, commit every ~`CHECKPOINT_SECONDS`.** The 6-hour Actions wall
   is therefore never a data-loss risk.

Per-game cost is ~2 storefront calls (appdetails + appreviews), which sets the pace ceiling
(§13). New games are seeded ahead of the queue via `seeds.txt` — one appid, store URL, or
search term per line, **human-edited only**; the scraper reads it but never writes to it (a
seeds ledger tracks what's been consumed so a seed isn't re-processed forever).

---

## 6. Refreshers (price/sale, tags, recent)

Independent enrichment jobs, each owning one file and enriching games the scraper already
found:

- **`price_and_sale.py` → `prices.json`.** The fast layer. Self-discovers sales from live
  Steam fetches (it does **not** depend on `games.json`'s `discount_pct`), batches appids
  per call (`PRICE_BATCH`), and pulls sale end-dates from `IStoreBrowseService/GetItems`.
- **`tags_refresh.py` → `tags.json`.** SteamSpy tags (SteamSpy, not Steam).
- **`recent_refresh.py` → `recent.json`.** The 30-day rolling review score, on an offset
  cron so it stays fresh; `RECENT_COOLDOWN_DAYS` controls how stale a score must be to
  re-check. The frontend computes the recent-vs-all-time **trend** (improving / stable /
  declining) and gates it on staleness.

---

## 7. HLTB subsystem (`hltb_refresh.py` + `hltb_estimate.py` → `hltb.json`)

HowLongToBeat completion times are static, so each game is fetched **once** (matched by
title similarity, threshold `HLTB_MIN_SIMILARITY`; obscure/oddly-named games may not match
and show `—`). QHPP is driven by the **average** of main / main+extras / completionist, so
partial data used to distort the score badly — a main-only game got `avg == main`
(understated), a completionist-only game got `avg ==` that large number (overstated).

**The estimation model.** `hltb_estimate.py` fills the missing legs from the **genre-average
ratio** between the three times. Across all games with a full real triple, the **median**
ratio is ~`1 : 1.39 : 2.19` (the **mean**, ~`1 : 1.86 : 4.21`, is skewed by grind-heavy
outliers — median is the right central tendency here). The ratio is computed **live** from
the current data with a frozen fallback, and estimates anchor on whatever real value exists.

**Anti-pollution guard.** Estimates must never train the ratio, or the model would drift
toward its own guesses. `compute_ratios` reads from the `raw` sub-object **only** — this is
the whole reason for the Option-B storage model (keep `raw` as ground truth rather than
overwriting in place). A bug where this guard was missing once fed 706 "triples" into the
ratio instead of the correct 327; reading exclusively from `raw` fixed it.

**Presentation.** Estimated legs render in **blue with a dotted underline** and a hover
tooltip (no `~` prefix — accent color is the cue). `avg` is *not* separately flagged when
its inputs are estimated. Zeros are normalized to null (treated as missing, not "0 hours").

**Backfill & re-scrape.** `hltb_backfill.py` applied the model to existing entries once
(via `backfill-hltb.yml`, sharing the `steam-hltb` concurrency group). `fetched_at` on every
entry is groundwork for a future priority re-scrape whose order is: **partial entries first**
(≥1 real value), **blank entries second**, **full-real last**.

---

## 8. Playtime pipeline (`playtime_refresh.py` + `playtime_summarize.py`)

Surfaces **how long people actually play**, split by whether they recommended the game.

**Why two files.** The raw per-review data is large and is the scraper's resumable working
set; the frontend only needs medians. So `playtime_refresh.py` maintains the big
`playtime_raw.json`, and `playtime_summarize.py` distills it into the lean `playtime.json`.

**Raw scraper.** Pulls reviews via Steam's `appreviews` and stores `playtime_forever` (total
hours) per review. **`playtime_at_review` is deliberately not used** (decided and re-decided).
Reviews are keyed by **`recommendationid`**, not cursor offset — cursor positions shift as
new reviews arrive, so identity keying is what makes resume correct under both new-review
arrival and playtime drift. It resumes each game by review identity, catching new reviews
and walking deeper into unseen ones. Because a single end-of-run commit would make a long
scrape fragile to runner interruption, it commits **every 30 minutes plus on graceful
shutdown**.

**Sentiment split.** Data is split by the **thumbs-up/down recommendation** — ▲ recommended
(green) vs ▼ not-recommended (red) — tied to the rating system itself, not persona labels
like "fans/detractors."

**Median, not mean.** Playtime is heavily right-skewed (a few thousand-hour players), so the
**median** is the primary statistic; the mean would be distorted by outliers.

**Summarizer thresholds.** A side needs **≥3 reviews** to produce a median; a game with no
trustworthy median on either side is omitted from `playtime.json` entirely. The interesting
cases this surfaces are **inversions** — e.g. a game where the ▼ non-recommenders played
*longer* than the ▲ recommenders is a credible "knows it well, still says skip it" signal.

---

## 9. Weighted rating (`ratings_summarize.py` → `ratings.json`)

A review rating where each vote counts in proportion to how long that player actually
played — a 300-hour recommendation should outweigh a 20-minute one. It sits **next to**
Steam's flat % (it is a *metric*, not a sort-only concept), in the **Weighted** column.

Reading the same `playtime_raw.json`, per game it stores **three** percentages plus the
sample size, `[steam_pct, raw_pct, capped_pct, n]`:

- **`steam`** — the plain one-vote-per-review % (reference / comparison; matches Steam).
- **`raw`** — uncapped playtime-weighted % = recommend-hours ÷ total-hours. Kept for
  debugging, but **whale-distorted**: one obsessive can dominate.
- **`capped`** — the same, but each review's playtime is capped at **2× that game's median**
  before weighting. This is the **intended display value** — a *relative* dampener that
  scales to each game's nature and neutralizes whales while staying in a sane range.

**Confidence, not smoothing.** Bayesian smoothing was prototyped (a data-driven prior worth
~10 reviews' hours) and then **deliberately dropped**: reliability is conveyed by a
**gray-out cue**, not by nudging the number toward a prior. The thresholds:
`MIN_REVIEWS_FOR_RATING = 5` (below this, no rating at all), `CONFIDENT_REVIEWS = 10`
(≥ renders full-color; **5–9 renders grayed** as low-confidence). Lowering the compute floor
to 5 is what makes the gray state actually render for the shakiest games. The frontend reads
`confident_reviews` from the meta and colors accordingly, and shows a **delta badge** vs
Steam's flat % (e.g. `Δ-5` when long-playtime detractors drag the weighted rating below
Steam).

---

## 10. Frontend (`index.html`)

A single self-contained page. On load it fetches every JSON file, merges them by `appid`
into one object per game (one O(n) pass — important at ~68k+ games), then renders, filters,
and sorts entirely client-side. Until real JSON exists it renders bundled `SAMPLE` data.

**QHPP computation.** `computeQ(game, basis)` = `(selected HLTB hours × rating%) ÷ price`,
where *basis* picks the **Sale** (after-discount) or **Full** price, and the selected HLTB
hours follow the **HLTB metric** toggle (main / +extras / 100% / avg). Null for free games
and games with no usable HLTB value. The score is recomputed on toggle, never stored.

**The table (12 columns).** In order: Game · Reviews · **Weighted** · Trend · Price ·
Discount · Sale ends · Released · Tags · **Playtime** · HLTB · QHPP.

- The table is **`table-layout: fixed`** with an explicit `<colgroup>` of `<col class="c-*">`
  — one per column. This is mandatory: with fixed layout the browser sizes columns from the
  `<col>` widths, so **adding a column without adding its `<col>` collapses the layout**
  (a real bug when Weighted + Playtime were first added). Column widths sum to **1556px**
  with `min-width: 1500px`, sized to fit within the ~1660px content frame (targets viewports
  ≥1442px) without horizontal overflow.
- **Reviews** stacks all-time over the 30-day score (recent greyed when stale/absent).
- **Weighted** shows the capped % next to Steam's, with the Δ badge and low-confidence gray.
- **Trend** is recent − all-time (improving/stable/declining), gated on staleness.
- **Price/Discount** render as one tight unit; **Sale ends** is a live countdown that
  collapses offline when a sale has expired (honest between price refreshes).
- **Playtime** stacks ▲ recommenders over ▼ non-recommenders' median hours. Hours display
  **whole for ≥10h, one decimal under 10h**; the review-count sample size is kept in the
  data + tooltip but not shown inline.
- **HLTB** shows main / +extras / 100% with `avg` below; the metric selected for QHPP is
  highlighted, estimates render blue + dotted-underline. Same 2-digit/1-digit number rule.
- **QHPP** shows the value plus a **log-scaled gold value-meter** bar. On a discounted game
  it shows both the **Sale** (primary/gold when that basis is active) and **Full** value
  (`… full`); on a game **not** on sale it shows a single value tagged **`full`** in a
  neutral color, so a full-price value is never mistaken for a discount deal.

**Filters & controls.** Title search · on-sale-only · min rating (any/60+/70+/80+/90+) ·
min & max price · min-reviews bands (0/10/100/1k/5k+, independent toggles, gaps allowed) ·
review-trend multi-toggle · updated-within (any/1mo/3mo/6mo/1yr/1yr+) · **QHPP range**
log-slider that fits current results · **tag rail** (click to require → exclude → clear,
with live per-tag counts, two-tier with a "+N more" expander). Score controls: **QHPP price
basis** (Sale/Full) and **HLTB metric** (main/+extras/100%/avg) both feed `computeQ`;
**HLTB data** (all incl. estimates / real only) — **real is the default** (estimates hidden
and not used for the selected metric).

**Sort.** Click any header to sort (`setSort`); the active header shows a gold arrow
**absolutely positioned at its bottom-center**, so it costs no column width or row height
(it previously overflowed and got clipped by the neighbor). Two selector toggles live in the
filter bar and do **not** sort on their own:

- **Reviews sort by** (all-time / 30-day) — which score the Reviews column sorts on.
- **Playtime sort** (▲ recommenders / ▼ non-recommenders) — which median a click on the
  Playtime column will sort by. Switching it updates the Playtime **header** (the selected
  side lights up, the other dims) so you can see what a header-click will do, and re-sorts
  **only if** Playtime is already the active sort. It uses the neutral segmented-control
  styling — the green/red lives in the header and cells (semantic), not on the toggle.

**State in the URL.** Every filter/sort choice is serialized to the querystring
(`q, inc, exc, sale, basis, hltb, hq, minscore, rev, pmin, pmax, trend, upd, wishonly,
ratesrc, pt, sort, dir, per, qmin, qmax`) and restored on load, so any view is a shareable
link. Defaults are omitted from the URL (e.g. `hq` only appears when not `real`, `pt` only
when not `up`). Pagination is infinite-scroll at 100 / 500 / 2000 per page.

**Thumbnails & hiding.** Capsule art with a hover-enlarge popover; a broken-image fallback
chain; adult-tagged art is **blurred with an 18+ badge** (permanent until a real age gate
exists). Each row has a slim `[x]` hide button; hidden games can be un-hidden.

---

## 11. Wishlist import & the Cloudflare Worker

The browser can't read a Steam wishlist cross-origin, so a small Worker (source in
`worker/`, free tier) proxies it. `parseSteamId` in `index.html` accepts **all five ID
formats** — profile URL, custom `/id/<name>` URL, bare vanity name, SteamID64, `STEAM_0:0:…`
(SteamID2), and `[U:1:…]` (SteamID3). Numeric formats convert **client-side** via BigInt
math; vanity names resolve through the Worker's `/?vanity=` endpoint
(`ISteamUser/ResolveVanityURL`). The Worker's `/?steamid=` endpoint returns the wishlist for
cross-referencing the catalog (and optional wishlist-only filtering). It requires the
profile's **game details to be public**.

Point `WISHLIST_PROXY` (top of the wishlist code in `index.html`) at the deployed Worker
URL. If the Worker isn't configured, the feature **self-disables** and the rest of the site
is unaffected — no backend is required for browsing, filtering, or sorting.

---

## 12. Configuration reference

Each job's knobs live at the top of its own script:

- **`RUN_MINUTES`** (env, per job) — per-run time budget. Fewer, longer runs beat many tiny
  ones: GitHub's scheduler delays/drops frequent jobs under load.
- **`STEAM_DELAY` / `STORE_DELAY` / `STEAMSPY_DELAY` / `HLTB_DELAY`** — politeness pacing.
  Steam storefront ≈ 200 req / 5 min per IP (shared by appdetails + appreviews); SteamSpy
  ≈ 1 req/sec. Lower these and you get 429s or a 5-minute 403 cooldown.
- **`CHECKPOINT_SECONDS`** — mid-run commit interval.
- **`NEW_ORDER`** (scraper) — `"newest"` / `"oldest"` appid order for new coverage.
- **`HLTB_MIN_SIMILARITY`** — HLTB title-match threshold.
- **`PRICE_BATCH`** — appids per batched price call.
- **`RECENT_COOLDOWN_DAYS`** — staleness before a recent score is re-checked.
- **`MIN_REVIEWS_FOR_RATING` (5) / `CONFIDENT_REVIEWS` (10) / `CAP_MULT` (2.0)** (ratings) —
  weighted-rating eligibility floor, full-color threshold, and per-review playtime cap.
- **`STEAM_API_KEY`** (secret) — enables the keyed catalog enumeration + change-detection.

---

## 13. Pace, limits, cost

The scraper captures ~1,000–1,200 games/hour (storefront limit ÷ ~2 calls/game), so the full
~90k catalog is a **multi-week accumulation** that just grows run to run. Faster = raise
`RUN_MINUTES` or add off-peak `cron` times. Cost is **$0** — Actions is free/unlimited on
public repos; the only ceiling is the 6-hour per-job limit. Each daily commit also keeps the
repo active — **GitHub disables scheduled workflows after 60 days of no commits**, so the
steady commits are load-bearing for the whole system staying alive.

---

## 14. Operational notes & caveats

- **HLTB** matches by title similarity; misses show `—`. First full pass is still completing;
  the priority re-scrape (partials → blanks → full-real) comes after (§7).
- **HLTB estimates** are clearly marked (blue + tooltip) and auto-replaced once real data
  arrives; they never train the ratio.
- **Weighted rating** needs public playtime and enough reviews: none below 5, grayed 5–9.
- **Tags** fall back to Steam genres when SteamSpy lacks a game.
- **Sale end times** collapse offline when expired, so countdowns stay honest.
- **Dataset size** — the single-file approach is fine into the tens of thousands; far beyond
  that, shard and load on demand. `playtime_raw.json` is the largest file and grows with
  review coverage (adding one weighted-rating number to `playtime.json` is negligible).
- **Sandbox limitation** (for maintenance): the dev environment can't reach Steam domains, so
  Steam-dependent code is unit-tested against documented response shapes and verified by
  running it live in Actions.

---

## 15. Recent changes

- **Weighted + Playtime columns** added to the table (§8, §9), loaded via the existing
  fetch/merge pattern from the new `playtime.json` / `ratings.json`.
- **Playtime pipeline** (`playtime_refresh.py`, `playtime_summarize.py`, `ratings_summarize.py`
  + their workflows) built out: recommendationid-keyed raw scrape, median split by
  recommendation, 2×-median-capped weighted rating with a gray-out confidence cue.
- **Frontend polish:** tagline corrected to "quality hours per price"; QHPP price-basis
  toggle relabeled **Sale / Full**; **HLTB data** defaults to **Real**; HLTB and playtime
  numbers drop the decimal at ≥10h; QHPP shows a **`full`** tag in neutral color for
  non-discounted games; the sort arrow moved to the header's bottom-center (no clipping); and
  the **Playtime sort** control moved into the filter bar as a non-forcing selector that only
  reorders when the Playtime column is clicked.
- **Wishlist import** extended to all five Steam ID formats via the in-repo Cloudflare Worker.
- **Workflows** bumped to `checkout@v5` + `setup-python@v6` (Node 24), chosen to preserve the
  fetch→rebase→push credential behavior.
