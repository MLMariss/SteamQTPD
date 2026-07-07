# QTPD — Architecture

The engineering deep-dive for **QTPD** (Quality Time Per Dollar — *formerly QHPP, "Quality
Hours Per Price"; the repo, GitHub project, and wishlist worker keep the legacy `qhpp`/`SteamQHPP`
names*), a static Steam value-hunter that ranks games by quality-adjusted playtime per dollar.
For the quick overview and setup, see **[README.md](README.md)**; this document explains *why* the
system is shaped the way it is, every job and data file, and how the frontend turns raw
JSON into the table.

---

## 1. Design principles

**Static-first, server-side scraping.** The site is plain files on GitHub Pages. There is
no application server. Steam sends no CORS headers, so the browser cannot call Steam
directly; all scraping runs inside **GitHub Actions** and the results are committed to the
repo as JSON. The frontend only ever reads static JSON. The single exception is the
optional wishlist import, which needs a live cross-origin call and therefore routes through
a small **Cloudflare Worker** proxy (§12).

**One writer per file.** Every data layer is owned by exactly one job. No two jobs ever
write the same file. This is the load-bearing decision that makes parallel scheduled
Actions safe: two jobs committing *different* files always rebase-merge cleanly, so they
can run and push concurrently without lock-step coordination or lost work. Adding a data
source means adding a file + a job, never touching another job's file.

**Merge in the browser.** The frontend downloads each file and merges them by `appid` into
one in-memory object per game, in a single O(n) pass at load. QTPD is computed client-side
from the merged fields (§11) — never stored server-side — so the score responds instantly
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
   │ recent_refresh.py ──────► recent.json          │        │   computes QTPD) │
   │ playtime_refresh.py ────► playtime_raw/*.json  │        │                  │
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

## 3. Community feedback & future work

A backlog of ideas raised by users, recorded here as the single resumption point for
planning. **Nothing in this section is committed or verified** — each item still needs
feasibility confirmation (is there a real data source?), impact assessment, and a complexity
estimate before it earns a job, a file, or a frontend control. Items are grouped by the part
of the system they touch. Comments are captured anonymously; only the substance is kept.

The load-bearing constraints any item must respect: **one writer per file** (§1) — a new
data source is always a *new* file + a *new* job, never a change to an existing job's file —
and **static-first** (§1): anything needing a live cross-origin call routes through the
Cloudflare Worker (§12), it does not become a server.

### 3.1 New data sources / metrics (scraper work)

Each of these implies a new scrape and a new JSON file merged by `appid` in the frontend
(§11). Listed roughly by value-to-effort.

- **Completion rate (achievement-based).** Weight QTPD by how many players actually *finish*
  a game, not just its length — a 60-hour game most people drop halfway is a worse "hours you
  will really get" deal than its HLTB number implies. *What to do:* pull global achievement
  percentages (Steam `ISteamUserStats/GetGlobalAchievementPercentagesForApp`), pick a
  per-game "main story complete" achievement (the hard part — needs a heuristic or a curated
  map, since achievement naming is arbitrary), expose a completion-weighted QTPD mode
  (`hours × completion_rate × rating ÷ price`). *Highest-value new metric; medium effort,
  gated on the achievement-selection heuristic.*

- **Region-specific pricing.** Take the price (and therefore QTPD) from a user-chosen Steam
  region/currency rather than one fixed region — most-requested item by headcount. *What to
  do:* decide between (a) scraping prices for N regions into `prices.json` (N× the price
  scrape and storage) vs (b) an on-demand per-region fetch through the Worker at view time.
  Ties into the "isthereanydeal integration" idea below (cross-store / price-history).
  *High value, high effort; needs an explicit storage-vs-live design decision first.*

- **Developer update cadence & size.** Beyond the single `last_update_ts` already in
  `games.json`, surface *how often* and *how substantially* a game is patched — an
  ongoing-support signal. *What to do:* scrape the Steam news/patchnotes feed
  (`ISteamNews/GetNewsForApp` or the events endpoint) into a new file; derive update
  frequency and a rough magnitude (post length / cadence) as a filterable column. *Medium
  value, medium-high effort (new feed scrape + heuristics for "big" vs "small").*

- **Mod support & mod count.** Flag whether a game is moddable and roughly how large its mod
  scene is — especially relevant to the survival-craft audience. *What to do:* query the
  Steam Workshop for published-file counts per app into a new file; expose as a filter/column.
  *Medium value, medium effort; Workshop count is retrievable, "quality" of mods is not.*

- **Co-op max player count.** For co-op games, show/filter by the maximum supported players.
  *What to do:* read store-page category flags (co-op / online co-op / shared-screen); note
  that a concrete max-player *number* is inconsistently exposed, so expect partial coverage.
  *Medium value, low-medium effort, partial-coverage caveat.*

- **Anti-cheat type (not cheater volume).** Show *which* anti-cheat a multiplayer game uses so
  users can avoid kernel-level / invasive systems. *What to do:* scrape the store page's tech
  list / DRM-and-anti-cheat notes into a new file; classify by AC name. *Note:* the related
  "number of *active cheaters*" request has **no reliable public source** and is not pursued.
  *Low-medium value, medium effort; "invasive or not" is a judgment call to encode.*

- **Soften the `success:false` permanent-skip (robustness, not a new metric).** `build_record`
  permanently skips any app whose Steam `appdetails` returns `success:false` (scraper.py lines
  ~477/480), which lumps genuinely-dead/delisted apps together with **region-locked (cc=us)**
  titles and transient `success:false` blips — so a handful of legitimate games are dropped
  forever (≈731 currently on the skip list, an unknown fraction of them recoverable). *What to
  do:* on `success:false`, retry once (or in an alternate region) before skipping, or file to a
  "recheck later" list instead of a permanent skip. *Low impact (small count), low effort; safe
  quick win.*

### 3.2 Frontend / UX (no new scraping)

Works off data already collected. Several are cheap and high-impact.

- **Mobile / narrow-screen layout — highest-frequency complaint. [Partly done.]** The old
  `table-layout: fixed` grid overflowed small screens, titles truncated, and the sale badge +
  discount % wasted a column. *Done so far:* the desktop table is now **fluid** (`auto` layout,
  per-column min/max — §11), and the sub-1040px **card layout was improved** (roomier spacing,
  a ~560px phone breakpoint, Tags as a full-width left-aligned row). The old fixed-1556px
  conflict this item called out is **resolved** (that assumption is gone). *Also shipped since:*
  **Price and Discount are now a single merged `Price / Sale` column** (struck full price on top,
  sale price + discount badge below) with a **split header** whose two halves sort independently
  (Price by current price, Sale by discount depth) — this reclaims the column the discount % used
  to waste, on desktop as well as mobile. *Still open:* a richer per-card mobile design —
  thumbnail+title as a proper card header, and optionally folding Sale-ends into the merged price
  unit and hiding secondary fields behind a tap. *What to do next:* design the card header +
  decide which fields are primary-vs-secondary on a phone.

- **Hover tooltips on filter controls. [Done.]** Top-of-page filters (HLTB especially) were
  opaque to new users. *Done:* most filter toggles already carried `title` tooltips; added them
  to the two that needed them most — the **HLTB metric** toggle (Main / +Extras / 100% / Avg,
  spelled out as HowLongToBeat categories) and the **Reviews-sort** toggle (matching the adjacent
  Playtime-sort). *Optional remainder:* Min-rating and Updated-within buttons are self-evident and
  were left untouched.

- **Exclude-genres discoverability. [Done.]** Genre *exclusion* already existed (click a tag in
  the rail → require → exclude → clear) but was only explained in the rail's hover `title`, which
  is itself undiscoverable. *Done:* added a **visible legend** above the rail
  (`✓ require → ✕ exclude → clear`) that reuses the real `.chip.inc`/`.chip.exc` styles, so the
  swatches can't drift from the actual state colors.

- **Column-add safety. [Done — premise was stale; verified empirically.]** The note assumed a
  `table-layout: auto` table, but the layout actually moved further: it's now **CSS Grid applied
  at the row level** (`thead tr` / `tbody tr` are each `display:grid` sharing one `--grid-cols`
  track template — §11, ~line 202), and the `<colgroup>` is inert (`colgroup{display:none}` — it
  only documents column order; adding or omitting a `<col>` has **zero** layout effect). *Tested
  (Playwright, 1700px): forcing a 12th cell into both header and body **without** a matching 12th
  track keeps header and body perfectly aligned (identical cell right-edges) — grid auto-places
  the extra cell into the same implicit track for both rows.* So the old failure mode (table
  collapses / header drifts from body) **can no longer happen**; the worst case is a
  cosmetically-mis-sized new column, not a broken layout. *To add a column now:* (1) add a
  `minmax()` track to `--grid-cols` at the right index, (2) add the `<th>` and the matching
  `<td>` (and the card-layout row) at the same index. The `<col>` is optional and inert. §11's
  regression note updated to match.

- **Min-review filter: re-filters on change. [Done — verified, not a bug.]** The reported
  "changing the selection doesn't refresh the list" could not be reproduced: the band toggles
  flip `state.revBands` and re-run the filter pass on every click, and the list updates. No fix
  needed.

- **Min-review picker: single-select vs multi-select — open design conflict.** A request to
  make min-reviews a single choice (can't tick both 1k and 5k+) directly contradicts the
  current *deliberate* design: independent bands with gaps allowed (§11). *What to do:*
  **decision required** — keep the intentional multi-band model (and just document why), or
  switch to single-select. Do not change silently.

- **Sort by review count. [Done — implemented.]** The Reviews column only sorted by *score*
  (the top All-time/30-day toggle chose which score); review *count* wasn't sortable. *Done:* the
  Reviews header is now a **split header** (`.th-split`, reusing the Price/Sale pattern) with two
  independent sort buttons — **Score** (`data-sort="rating_pct"`) and **Count**
  (`data-sort="review_count"`). The two controls are **orthogonal**: the top All-time/30-day
  toggle picks the *period*, the header picks the *dimension*, together covering all four values
  (all-time score/count, 30-day score/count). Count sorting mirrors the score's period logic via
  a new `countVal()` comparator — `recent_count` when 30-day is active, `review_count` when
  all-time — and the period toggle now re-renders when either `rating_pct` **or** `review_count`
  is the active sort. Unlike the score fall-back, a 0/absent `recent_count` is treated as a real
  0 (no recent reviews), not borrowed from the all-time total. Verified with Playwright: DESC/ASC
  ordering, direction flip, and period switch all correct; no JS errors.

- **Visual polish. [Mostly done — original note is stale.]** The "2009 admin panel" feedback
  predates the current design, which already has a real type pairing (IBM Plex Sans + Mono, loaded
  via Google Fonts) and a **semantic** palette (gold = labels, blue = links, coral/red =
  discount/negative, teal/green = free/positive) — so the note's "one accent color" is actively
  wrong: those colors carry meaning and must not be flattened. Padding is already reasonable.
  *Done:* added the one genuinely-missing piece, **subtle alternating row shading** (translucent
  `rgba(127,160,230,.035)` on even rows so the card gradient shows through; declared before
  `:hover` so hover wins; reset to transparent in card mode). *Tuning knob:* the zebra alpha is a
  single value if it needs to be stronger/weaker after eyeballing on a real display.

- **Rename QHPP → QTPD. [Done.]** "QHPP" was unfriendly to type/say. *Done:* the public
  metric is now **QTPD (Quality Time Per Dollar)** — clearer and rolls off the tongue better.
  Renamed across the frontend end-to-end: page title, logo wordmark, tagline, column header,
  every tooltip, the filter labels (QTPD price basis / HLTB metric for QTPD / QTPD range), the
  formula help text, the internal sort key + CSS classes, and the URL `sort` param value. Left
  intentionally as legacy identifiers: the repo/GitHub name **SteamQHPP** and the wishlist
  worker subdomain **qhpp-wishlist** (renaming those would break the deploy + the live proxy).
  Old shared `?sort=qhpp` links fall back to the default sort. *Pure product call, minimal risk.*

### 3.3 Nice-to-have / still to be evaluated

Lower priority, weaker sourcing, or off the core "value hunter" mission. Recorded so they
aren't lost, but each needs a source-feasibility check before it's worth scoping.

- **isthereanydeal integration.** Cross-store pricing and price history. *Overlaps with
  region-pricing (§3.1); evaluate together.* Needs an API/ToS review.

- **Sequel / franchise linkage — "what has a sequel and what it is."** Steam exposes no
  structured franchise graph; would need an external DB (e.g. IGDB) or heuristics. *Feasibility
  unclear; scope creep risk.*

- **Correlate dev teams across games.** "Made by the same people who made X." No structured
  Steam source; would ride on an external DB. *Harder than franchise linkage.*

- **"Talked about vs actually playing" metric.** Buzz-vs-engagement. Needs a social/mentions
  data source cross-referenced with playtime. *No clear source; vague; park it.*

- **Steam Trading Cards resale helper.** "How many trading cards would I sell to afford this
  game." Self-contained but niche; needs card market-price data. *Low priority.*

- **Studio-health signals (layoffs / employee turnover %).** Suggested as a publisher-health
  angle. Only viable source floated was LinkedIn scraping, which is a **ToS problem** and far
  off-mission. *Recorded but not recommended.*

---

## 4. Jobs & workflows

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
| `playtime_refresh.py`   | `playtime_raw/NN.json` | overnight     | Per-review playtime, sharded (1 bucket/run); commits every 30 min + on shutdown. |

**Non-Steam scrapers** (hit their own sites, so no Steam-budget contention):

| Workflow / script   | Owns file   | Source    |
|---------------------|-------------|-----------|
| `hltb_refresh.py`   | `hltb.json` | HowLongToBeat |
| `tags_refresh.py`   | `tags.json` | SteamSpy  |

**Summarizers** (pure local recompute — read one file, write another; **no Steam calls**,
so they touch no rate budget). Both read the sharded `playtime_raw/` set and each writes its
own file, so one-writer-per-file holds:

| Script                  | Writes         | Trigger                              | Concurrency group |
|-------------------------|----------------|--------------------------------------|-------------------|
| `playtime_summarize.py` | `playtime.json`| chained step in `playtime-raw.yml`   | (inherits `steam-playtime-raw`) |
| `ratings_summarize.py`  | `ratings.json` | chained step in `playtime-raw.yml`   | (inherits `steam-playtime-raw`) |

**They run as chained steps at the end of `playtime-raw.yml`**, right after `playtime_refresh.py`
commits the freshly-updated shards — so the lean frontend files refresh on *every* raw pass
(`23 1,4,7,10,13,16,19,22 * * *`, ~8×/day), immediately tracking the shards instead of lagging
them. Both steps use `if: always()`, so a soft-failing raw pass (e.g. a 403 storefront-budget
wrap-up) still publishes summaries from whatever shards exist. Each summarizer still self-commits
its one file with the standard `fetch → rebase → push` pattern.

*History:* these were previously two standalone workflows (`playtime-summary.yml` :47, and
`playtime-ratings.yml` :51, both `*/4`), which ran the summarizers every 4h independent of the
raw scrape. Folding them into `playtime-raw.yml` made those two workflows redundant, so **both
were retired** — the chained steps supersede them and guarantee the summarize-right-after-scrape
ordering the split schedule couldn't.

**Generated-doc workflows** (recompute a Markdown file from the live data; no Steam calls,
each single-writes its `.md`):

| Workflow file       | Script            | Writes        | Trigger                                          | Concurrency group |
|---------------------|-------------------|---------------|--------------------------------------------------|-------------------|
| `shard-health.yml`  | `shard_health.py` | `SHARDS.md`   | `35 6 * * *` (daily)                             | `shards-md`       |
| `coverage.yml`      | `coverage.py`     | `COVERAGE.md` | `workflow_run` after **scrape** succeeds (~4×/day) | `coverage-md`     |

`coverage.py` recomputes every coverage figure from the live files + shards (base universe,
per-metric covered/%/missing sorted by % descending, the on-sale count, and the
addressable-set framing) and self-commits `COVERAGE.md`. It uses only the standard library —
no `pip install` step. It's triggered off the **scrape** completion (via `workflow_run` keyed
to that workflow's exact name) so the snapshot always reflects the freshest `games.json`, the
base everything is measured against; it also has `workflow_dispatch` for manual runs.
`COVERAGE.md` is now a generated artifact — previously it was hand-authored and silently drifted
as the data jobs kept running.

**One-off (deletable) workflows.** `cleanup_shells.py` is a run-once utility that shares its
target file's concurrency group so it can't clobber an in-progress scrape. The earlier
HLTB-estimation backfill (`backfill-hltb.yml` / `hltb_backfill.py`) and `backfill_updates.py`
were also run-once utilities of this kind; they have since been **run and removed**, which is
the intended lifecycle for these once their one-time job is done.

---

## 5. Data files & schemas

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
(§8). `hltb_est` lists which of the three were estimated (drives the blue flag). `fetched_at`
is groundwork for the priority re-scrape.

**`tags.json`** — `{ "tags": { "<appid>": ["Roguelike", …] } }`. SteamSpy user tags; the
frontend falls back to Steam genres when a game is absent, so the column is never blank.

**`recent.json`** — `{ "recent": { "<appid>": { recent_pct, recent_count,
recent_scraped_at } } }`. The 30-day score; staleness gates the trend arrow.

**`playtime_raw/NN.json`** (64 shards) — the big working set, owned by `playtime_refresh.py`.
Each shard: `{ "bucket": N, "nshards": 64, "shard_ver": V, "games": { "<appid>": { "reviews":
{ "<recommendationid>": { playtime, voted_up, … } }, "summary": {…} } } }`, holding only games
where `(appid // 10) % 64 == N`. Reviews are keyed by **`recommendationid`** (identity, not
cursor position) so re-runs catch new reviews and updated playtimes without duplication. Kept
in-repo (not gitignored) because it *is* the scraper's resumable state; split across shards
because one file would blow past GitHub's 100 MB limit (§9).

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
See §10 for what the three percentages mean and why.

---

## 6. The main scraper (`scraper.py` → `games.json`)

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
(§14). New games are seeded ahead of the queue via `seeds.txt` — one appid, store URL, or
search term per line, **human-edited only**; the scraper reads it but never writes to it (a
seeds ledger tracks what's been consumed so a seed isn't re-processed forever).

---

## 7. Refreshers (price/sale, tags, recent)

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

## 8. HLTB subsystem (`hltb_refresh.py` + `hltb_estimate.py` → `hltb.json`)

HowLongToBeat completion times are static, so each game is fetched **once** (matched by
title similarity, threshold `HLTB_MIN_SIMILARITY`; obscure/oddly-named games may not match
and show `—`). QTPD is driven by the **average** of main / main+extras / completionist, so
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
(via `backfill-hltb.yml`, sharing the `steam-hltb` concurrency group); both were run-once
utilities and have **since been removed** (§4). `fetched_at` on every
entry is groundwork for a future priority re-scrape whose order is: **partial entries first**
(≥1 real value), **blank entries second**, **full-real last**.

**Estimator wiring status (live — resolved).** `hltb_estimate.py` is **not** a standalone job;
it's a shared helper module that `hltb_refresh.py` imports as `HE` and calls in its fill loop
(`HE.compute_ratios` once per run over the current corpus's real triples, then `HE.make_entry`
/ `HE.raw_of` per game). So estimates are recomputed **on the live path every 2h** (`hltb.yml`,
`53 */2 * * *`): as new real HLTB values land they shift the live median ratios and re-estimate
the dependent legs. The `est` count therefore tracks the corpus rather than being frozen — a
snapshot-to-snapshot diff shows it moving with the reals. This supersedes the earlier "open item:
wire the estimator into a workflow" — that was written against the removed one-off
`hltb_backfill.py` sweep; the live refresh path took over the job. (An even-earlier COVERAGE.md
snapshot claimed "0 estimated"; that was a flag-name reading error — the field is `est`, not
`estimated`.)

### 8.1 Coverage-recovery work (Phases A / B live; C built then retired)

Motivation: a full first pass left **~77% of entries blank** (~94k of 122k). Auditing the
blanks showed two populations mixed together — genuinely-dead long-tail titles HLTB will
never have, AND real games lost to title noise or transient first-pass failures (e.g.
`Far Cry® New Dawn`, `EDENS ZERO` returned zero results under their raw store titles). Three
independent changes were built to attack this. **Phases A and B are live** (HLTB-only, no
external dependency). **Phase C (IGDB) was built, debugged, evaluated, and deliberately
retired** — see the Phase C note below for why and its current dormant state.

**Phase A — title normalization (`hltb_match.py`).** `hltb_for` no longer searches the raw
store title once; it walks an ordered, de-duplicated list of query variants (raw first, then
trademark-glyph-stripped, edition/bracket-tail-stripped, ALLCAPS→Title-Case, subtitle-trimmed)
and stops at the first variant matching at/above `HLTB_MIN_SIMILARITY`. The raw title is always
variant 0, so this can only **widen** matches, never regress one. Transient-error semantics are
preserved across variants: if every attempted variant errored (none matched), `hltb_for`
returns `None` (retry next run) rather than freezing a permanent blank.

**Phase B — eager-but-throttled blank retry + never-idle drain.** The old flat 60-day blank
window is replaced by an **attempt-scaled** window (`blank_window_days`): eager (3d) for the
first few attempts, backing off (30d) once a title looks dead, near-freezing (180d) after that.
Each blank carries an `attempts` counter (`store_entry` increments on a miss, clears on a
match). Additionally, when the windowed re-scrape queue empties but time-budget remains, a
**never-idle drain** (`build_idle_drain`) keeps working the least-tried, least-recently-tried
blanks (skipping the frozen tier) so the job never quits early with budget on the clock —
converging because each drained blank's `attempts` rises and backs its window off. Net effect:
real games lost to noise/transients recover within days; genuinely-dead shovelware throttles
down instead of being re-hit every run.

**Phase C — IGDB secondary source (`hltb_igdb.py` → `hltb_igdb.json`). RETIRED 2026-07 —
dormant in-repo, not wired to anything.** The idea: HLTB matching is title-based and lossy, so
a second source keyed off the **Steam appid** could recover games HLTB structurally can't. IGDB
was chosen (independent completion-time data, appid-linkable). It was implemented as the sole
writer of `hltb_igdb.json` (never touching `hltb.json` — one-writer-per-file preserved), reusing
the **same** `hltb_estimate` model so IGDB rows would carry identical `est`/blue-flag treatment.
Auth is Twitch OAuth client-credentials (`IGDB_CLIENT_ID` / `IGDB_CLIENT_SECRET` secrets).

Two real bugs were found and fixed during bring-up, both worth remembering if this is ever
revived: (1) the Steam-platform filter used the **deprecated** `category = 1` field, which
silently returns zero rows on the current API — the correct filter is
`external_game_source = 1` (probe-confirmed; the code now uses this). (2) `build_worklist`
treated blank IGDB entries like matched ones, freezing ~110k first-pass blanks out of the queue
for 90 days; fixed with an eager blank-retry tier (`IGDB_BLANK_EAGER_ATTEMPTS` / `_DAYS`).

**Why retired:** with both bugs fixed, IGDB resolved ~96k of 110k appids to game ids, but its
`game_time_to_beats` table holds only **~8,829 records total** across all of IGDB — a hard,
small ceiling. The full run matched **1,471 games (1,007 net-new vs HLTB)**. Useful, but too
small to justify a standing scheduled job and its maintenance surface (an overlap spot-check
also showed IGDB and HLTB disagree per-game, sometimes >10×, often because one source has junk
data — e.g. HLTB had RAGE at 0.6h, The Walking Dead at 0.5h). Decision: drop it.

**Current dormant state:** `igdb.yml` has its `schedule:` trigger removed (`workflow_dispatch`
only — it will not run on its own; a manual dispatch is the only way to fire it). The frontend
merge was **reverted** — `index.html` no longer fetches `hltb_igdb.json` and is clean HLTB-only.
The Phase C self-check (`check_phase_c`) was **removed** from `hltb_selfcheck.py`. Left in-repo
but unreferenced: `hltb_igdb.py`, `hltb_igdb.json` (stale blank-heavy data), `igdb_wipe.py` +
`igdb-wipe.yml`, and the diagnostics `igdb_probe.py` / `igdb_probe2.py` + `igdb-probe.yml` (a
manual dropdown workflow; probe2's TEST 5 is what measured the 8,829-record ceiling). Nothing
reads any of these. To fully revive: restore the `external_game_source` query (already correct
in the file), re-add the schedule, re-add the frontend merge, and re-add the self-check.

**Self-checks (`hltb_selfcheck.py`).** Phases A and B ship with fail-fast, pure-logic regression
guards run at the top of `hltb_refresh.main()`. A failed assertion **aborts before any file is
written** — a loud red failure rather than silent coverage decay (same failure class as the
`playtime_raw` silent-green bug). (The Phase C guard was removed when IGDB was retired.)

---

## 9. Playtime pipeline (`playtime_refresh.py` + `playtime_summarize.py`)

Surfaces **how long people actually play**, split by whether they recommended the game.

**Why two files.** The raw per-review data is large and is the scraper's resumable working
set; the frontend only needs medians. So `playtime_refresh.py` maintains the big raw working
set (sharded — see below) and `playtime_summarize.py` distills it into the lean `playtime.json`.

**Raw scraper.** Pulls reviews via Steam's `appreviews` and stores `playtime_forever` (total
hours) per review. **`playtime_at_review` is deliberately not used** (decided and re-decided).
Reviews are keyed by **`recommendationid`**, not cursor offset — cursor positions shift as
new reviews arrive, so identity keying is what makes resume correct under both new-review
arrival and playtime drift. It resumes each game by review identity, catching new reviews
and walking deeper into unseen ones. Because a single end-of-run commit would make a long
scrape fragile to runner interruption, it commits **every 30 minutes plus on graceful
shutdown**.

**Sharded storage (the 100 MB wall).** The per-review set is too big for one file: at
~15 KB/game the full addressable set is ~1.1 GB, and GitHub **hard-rejects any single file
over 100 MB**. A monolithic `playtime_raw.json` hit that ceiling at ~6,850 games (98 MB) —
every push was rejected, and because the old commit code swallowed the error the runs went
*green with nothing committed*, silently freezing the pipeline for ~2 days. So the raw set is
split into **64 shards under `playtime_raw/NN.json`**, keyed by
`shard_of(appid) = (appid // 10) % 64`. The `// 10` is load-bearing: Steam appids are ~100%
multiples of 10, so a plain `appid % 64` piles every game into the *even* buckets (odd buckets
get ~nothing) — dividing by 10 first strips that factor and spreads them evenly (measured
max/mean ~1.05 vs ~2.07). Each run processes **one bucket**, chosen by `GITHUB_RUN_NUMBER % 64`,
so it loads/commits only ~18 MB and rotates through all 64 buckets over ~64 runs.
`ensure_sharding()` is idempotent and version-gated by `SHARD_KEY_VER`: on the first run it
splits the legacy monolith and `git rm`s it; if `shard_of()` ever changes it reshards in place
(a plain file upload is all it takes); otherwise it's a fast no-op.

**Robust commit.** Each shard commit hard-resets to `origin/main` and re-applies only this
run's shard before pushing — since only this job writes a given shard, that can never conflict
or wedge a rebase (the old `git rebase --autostash` + `check=False` path could stick mid-rebase
and then fail every push *silently*). If a push still can't land after retries it **exits
non-zero (a red run)**, so a broken push is visible rather than a silent freeze. `playtime_raw/`
is not web-served — the frontend reads only the summarized `playtime.json` — so its size affects
git, not the browser.

**Health monitor.** `shard_health.py` (daily via `.github/workflows/shard-health.yml`) writes
**`SHARDS.md`**: per-shard count and size, distribution evenness, staleness, and a
games-to-100 MB projection at full coverage — flagging any shard approaching the limit *before*
it becomes a problem.

**Eligibility floor.** A game must have **≥10 all-time reviews** (`MIN_REVIEWS_FLOOR`) to enter
the scrape queue at all. Below the floor there aren't enough reviews to survive the summarizer's
≥3-per-side split, so scraping them would spend storefront budget for a median that gets nulled
out anyway. The gate is applied at candidate selection against the **live `review_count` from
`games.json`**, so it's "skip for now," not permanent exclusion — a game re-qualifies the moment
its review count crosses 10. In practice this removes ~41k unusable games from the queue (~44%),
leaving the full request budget for the ~52k games that can actually yield data.

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

## 10. Weighted rating (`ratings_summarize.py` → `ratings.json`)

A review rating where each vote counts in proportion to how long that player actually
played — a 300-hour recommendation should outweigh a 20-minute one. It sits **next to**
Steam's flat % (it is a *metric*, not a sort-only concept), in the **Weighted** column.

Reading the same sharded `playtime_raw/` set, per game it stores **three** percentages plus the
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

**Shard read + fail-loud (Jul 2026 fix).** This summarizer originally read the monolithic
`playtime_raw.json`. When the raw store was split into 64 `playtime_raw/NN.json` shards (§9),
`playtime_summarize.py` was updated to read the shards but **this script was missed** — it kept
reading the now-deleted monolith, hit `RAW_FILE.exists() == False` every run, logged "nothing
to rate" and **exited 0 without writing**. `ratings.json` silently froze at its last
pre-migration output (6,855 games, 07-05) while `playtime.json` advanced normally — a coverage
gap with no red run to signal it. Fixed by porting `iter_raw_shards()` from
`playtime_summarize.py` (both now read the identical source; the legacy monolith remains a
fallback if sharding is ever un-migrated). Two **fail-loud guards** were added so this class of
silent freeze can't recur:
- **No raw source at all** (neither shard dir nor monolith) → exit 0. Legitimate empty state.
- **Source present but iteration yields 0 games** (shards unreadable / wrong shape) → **exit 1
  and preserve the existing `ratings.json`** rather than overwriting it with an empty file. The
  run goes red; the good data survives.

Summarizer cadence evolved in two steps. They first moved from once-daily to every 4h as two
standalone workflows (`playtime-summary` :47, `playtime-ratings` :51) — both are ~5s pure-local
recomputes with no storefront cost, so daily cadence was needlessly lagging the ~8×/day raw
scraper. They were then **folded into `playtime-raw.yml` as chained steps** (§4) that run right
after `playtime_refresh.py` commits its shards, so summaries now refresh on *every* raw pass
(~8×/day) and always track the shards they were computed from; the two standalone `*/4`
workflows were retired as redundant.

---

## 11. Frontend (`index.html`)

A single self-contained page. On load it fetches every JSON file, merges them by `appid`
into one object per game (one O(n) pass — important at ~68k+ games), then renders, filters,
and sorts entirely client-side. Until real JSON exists it renders bundled `SAMPLE` data.

**QTPD computation.** `computeQ(game, basis)` = `(selected HLTB hours × rating%) ÷ price`,
where *basis* picks the **Sale** (after-discount) or **Full** price, and the selected HLTB
hours follow the **HLTB metric** toggle (main / +extras / 100% / avg). Null for free games
and games with no usable HLTB value. The score is recomputed on toggle, never stored.
(Internal sort key: `qtpd`.)

**The table (11 columns).** In order: Game · Reviews · Trend · **Weighted** · Price / Sale ·
Sale ends · Released · Tags · **Playtime** · HLTB · QTPD. (**Trend** now sits directly after
Reviews — it's derived from them — and **Price + Discount are merged** into one `Price / Sale`
column, dropping the old standalone Discount column: 12 columns → 11.)

- The table is laid out with **CSS Grid** — a shared `--grid-cols` template of `minmax()`
  tracks (one per column) applied at the **row** level, so the sticky `<thead>` stays a normal
  sticky block while each `<tr>` lays its cells on the same track template. (The `<colgroup>` is
  `display:none`; `<col>` min/max is ignored by browsers, so Grid, not `<col>`, sizes the
  columns.) Each track's `min` is a small-laptop legibility floor; the `max` is a breathing
  ceiling so the slim numeric/sort columns (Trend, Price / Sale, Weighted, Sale ends) don't bloat
  on a wide monitor. On large screens the slack concentrates on the content-heavy columns —
  **Game and Tags** — which carry `fr` ceilings; everything else stays near its natural width.
  The table's `min-width` is the **exact sum of the column minimums (1240px)** — down from 1266px
  after the Price/Discount merge — so below that the **page** (not the table card) scrolls
  horizontally — deliberately *not* `overflow-x:auto` on the scroll container, because a lone
  `overflow-x:auto` is promoted by browsers to `overflow:auto` on both axes, which would trap the
  sticky `<thead>` in a scroll box. Below ~1290px the table stops being a table and becomes the
  stacked **card layout** (see *Responsive* below).
  - **`minmax()` tracks make max-width reliable.** Unlike the old `table-layout:auto` + `<col>`
    approach (where `max-width` was only a hint), Grid `minmax()` enforces both floor and ceiling,
    so a slim column can't grow past its stated max even on an extreme ultrawide.
  - **Adding a column (verified for CSS Grid).** The old `table-layout: fixed` trap — adding a
    cell without its `<col>` **collapsed the layout** (the real bug when Weighted + Playtime were
    first added) — **no longer applies.** Under row-level Grid the `<col>`/`<colgroup>` is inert
    (`display:none`), and header and body share the one `--grid-cols` template, so a
    forgotten-track cell is auto-placed into the *same* implicit track for both rows: they stay
    aligned (Playwright-verified at 1700px — identical cell right-edges). **The layout can't
    collapse from a missing `<col>` anymore.** The real recipe to add a column: (1) insert a
    `minmax()` track into `--grid-cols` at the correct index; (2) insert the `<th>` and matching
    `<td>` (plus the card-layout row) at that same index; (3) `min-width` on `table` is the sum of
    the track minimums — bump it by the new track's `min`. The `<col>` is optional documentation
    of column order and has no layout effect.
- **Reviews** stacks all-time over the 30-day score (recent greyed when stale/absent). Its header
  is a **split sort** (`.th-split`): **Score** (`rating_pct`) / **Count** (`review_count`). This
  is orthogonal to the top All-time/30-day toggle — the toggle picks the *period*, the header
  picks the *dimension*, covering all four values. Count sorting follows the period via
  `countVal()` (`recent_count` on 30-day, `review_count` on all-time; a 0/absent recent count is a
  real 0, not borrowed from all-time), and the period toggle re-renders when either `rating_pct`
  or `review_count` is active.
- **Weighted** shows the capped % next to Steam's, with the Δ badge and low-confidence gray.
- **Trend** is recent − all-time (improving/stable/declining), gated on staleness.
- **Price / Sale** is one merged column: struck full price on top, sale price below with the
  discount badge inline to its right. Its **header is split** into two independently-clickable
  sort targets — "Price" (sorts by current price) and "Sale" (sorts by discount depth) — and the
  sort arrow hops to whichever half is active. **Sale ends** is a live countdown that
  collapses offline when a sale has expired (honest between price refreshes).
- **Playtime** stacks ▲ recommenders over ▼ non-recommenders' median hours. Hours display
  **whole for ≥10h, one decimal under 10h**; the review-count sample size is kept in the
  data + tooltip but not shown inline. When a game has **no playtime data the cell renders
  completely empty** (no `—` dash) so it adds **zero height** to the row — previously the dash
  forced a line-height floor that inflated data-sparse rows.
- **HLTB** shows main / +extras / 100% with `avg` below; the metric selected for QTPD is
  highlighted, estimates render blue + dotted-underline. Same 2-digit/1-digit number rule.
  The stack is a **block with both lines `nowrap`** (the three figures on one row, `avg N h`
  on the next) so large numbers can't wrap into each other — this, plus a raised HLTB column
  `min-width`, fixes the overlap that showed on wide free-game rows.
- **QTPD** shows the value plus a **log-scaled gold value-meter** bar. On a discounted game
  it shows both the **Sale** (primary/gold when that basis is active) and **Full** value
  (`… full`); on a game **not** on sale it shows a single value tagged **`full`** in a
  neutral color, so a full-price value is never mistaken for a discount deal.

**Responsive / card layout.** The table is for the desktop width range; a **fluid table alone
cannot fit a phone** (eleven columns at legible minimums sum to ~1240px). So below **1290px**
the layout switches: `<thead>` hides, each row becomes a **card** (every `<td>` a
label→value line, the header supplied by the cell's `data-label`). A second **~560px** phone
breakpoint tightens it further (smaller thumbnail, roomier tap targets, HLTB row given
priority so its figures don't crowd the label). **Tags** on mobile render as a **full-width,
left-aligned** row (label on its own line, chips wrapping with room) rather than crammed
against the right edge. This card mode *is* the mobile-friendly view users keep asking for; a
richer per-card design (thumbnail+title as a card header, secondary fields behind a tap) is
still open (§3.2).

**Filters & controls.** Title search · on-sale-only · min rating (any/60+/70+/80+/90+) ·
min & max price · min-reviews bands (0/10/100/1k/5k+, independent toggles, gaps allowed) ·
review-trend multi-toggle · updated-within (any/1mo/3mo/6mo/1yr/1yr+) · **QTPD range**
log-slider that fits current results · **tag rail** (click to require → exclude → clear,
with live per-tag counts, two-tier with a "+N more" expander). Score controls: **QTPD price
basis** (Sale/Full) and **HLTB metric** (main/+extras/100%/avg) both feed `computeQ`;
**HLTB data** (all incl. estimates / real only) — **real is the default** (estimates hidden
and not used for the selected metric).

The **QTPD logo wordmark doubles as a filter toggle** — clicking it opens/closes the whole
filter nav (identical to the "Show / Hide filters" collapse handle, which stays). The logo is a
real `<button>` (keyboard-focusable, `title`/`aria-label` set) rather than a decorative `<div>`.

**Sort.** Click any header to sort (`setSort`); the active header shows a gold arrow
**absolutely positioned at its bottom-center**, so it costs no column width or row height
(it previously overflowed and got clipped by the neighbor). The **Price / Sale** header is a
special case: it holds **two** sort targets (`<button>`s for `price_final` and `discount_pct`)
inside one cell, and the arrow anchors under whichever half is active — so the sort machinery
selects on `.sortable` (matching both the `<th>`s and the inner split buttons), not `th.sortable`.
Two selector toggles live in the filter bar and do **not** sort on their own:

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

## 12. Wishlist import & the Cloudflare Worker

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

## 13. Configuration reference

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
- **`MIN_REVIEWS_FLOOR` (10)** (playtime) — scraper-side eligibility gate: games below this
  many all-time reviews are skipped (can't clear the summarizer's ≥3-per-side split). Checked
  against live `review_count`, so it's skip-for-now, not permanent. Distinct from the ratings
  floors below, which govern rating *compute*, not playtime *scraping*.
- **`MIN_REVIEWS_FOR_RATING` (5) / `CONFIDENT_REVIEWS` (10) / `CAP_MULT` (2.0)** (ratings) —
  weighted-rating eligibility floor, full-color threshold, and per-review playtime cap.
- **`NSHARDS` (64) / `SHARD_KEY_VER` (2)** (playtime) — shard count for `playtime_raw/NN.json`
  and the shard-key version. `shard_of(appid) = (appid // 10) % NSHARDS`; the `// 10` spreads
  the (near-100%-multiple-of-10) appids evenly instead of piling them into even buckets. Bump
  `SHARD_KEY_VER` whenever `shard_of()` changes — `ensure_sharding()` reshards in place on the
  next run when the version stamped in the shards doesn't match.
- **`STEAM_API_KEY`** (secret) — enables the keyed catalog enumeration + change-detection.

---

## 14. Pace, limits, cost

The scraper captures ~1,000–1,200 games/hour (storefront limit ÷ ~2 calls/game). The catalog
has since reached full coverage — **~122.7k stored** against a ~173k app universe, with the
fresh frontier essentially exhausted — so `scrape.py` now finishes a run in **~7 minutes** and
mostly does `last_modified`-triggered refreshes. Faster = raise `RUN_MINUTES` or add off-peak
`cron` times. Cost is **$0** — Actions is free/unlimited on public repos; the only ceiling is
the 6-hour per-job limit. Each daily commit also keeps the repo active — **GitHub disables
scheduled workflows after 60 days of no commits**, so the steady commits are load-bearing for
the whole system staying alive.

**Playtime scale-up (storefront budget reallocation).** Because `scrape.py` now uses so little
of its window, the shared ~200/5min storefront budget has headroom. The playtime raw pass was
scaled from **4 → 8 cron slots** (`:23` every 3h) and **`STEAM_DELAY` 2.0 → 1.5s**, roughly
**2× review-time throughput** (~12h/day → ~24h/day, near-continuous). At 1.5s it sits at the
storefront ceiling with no headroom; `recent_refresh.py` already sustains 3h passes at 1.5s
(separate runner IPs), so this is expected to hold — but 403 rates are worth watching, and the
revert is just `STEAM_DELAY` back to 2.0 and/or fewer slots.

---

## 15. Operational notes & caveats

- **HLTB** matches by title similarity; misses show `—`. First full pass is still completing;
  the priority re-scrape (partials → blanks → full-real) comes after (§8).
- **HLTB estimates** are clearly marked (blue + tooltip) and auto-replaced once real data
  arrives; they never train the ratio.
- **Weighted rating** needs public playtime and enough reviews: none below 5, grayed 5–9.
- **Tags** fall back to Steam genres when SteamSpy lacks a game.
- **Sale end times** collapse offline when expired, so countdowns stay honest.
- **Dataset size** — the per-review working set is **sharded** (`playtime_raw/NN.json`, 64
  buckets) because one file would exceed GitHub's 100 MB limit; shards run ~18 MB at full
  coverage, and `SHARDS.md` monitors headroom (§9). Every other file is single and comfortably
  under the limit (`games.json` is next-largest at ~59 MB and grows with the catalog — worth an
  eye, but far from the wall). Adding one weighted-rating number to `playtime.json` is negligible.
- **Sandbox limitation** (for maintenance): the dev environment can't reach Steam domains, so
  Steam-dependent code is unit-tested against documented response shapes and verified by
  running it live in Actions.
- **Table sizing** — column `min-width` floors are reliable, but `max-width` on `<col>` under
  `table-layout: auto` is best-effort (§11); on an extreme ultrawide a slim column could exceed
  its ceiling. Firm fix (if ever needed) is a CSS-Grid table with `minmax()` tracks. Whether
  adding a new column still needs a matching `<col>` under the fluid model is **unconfirmed**
  and flagged to test (§3.2).

---

## 16. Recent changes

- **COVERAGE.md automated + summarizers folded into the raw job + doc reconciled (Jul 2026).**
  Three related workflow changes. (1) **New `coverage.py` + `coverage.yml`.** `COVERAGE.md` was
  hand-authored and silently drifted (a snapshot from that morning already trailed the live data
  by a full base-universe count and ~3.5k playtime rows). `coverage.py` (stdlib-only) now
  recomputes every figure from the live files + shards — base universe, per-metric
  covered/%/missing **sorted by % descending**, the on-sale count, and the addressable-set
  framing — and self-commits `COVERAGE.md`. `coverage.yml` triggers via `workflow_run` after the
  **scrape** completes (~4×/day) so the snapshot always reflects the freshest `games.json`, plus
  `workflow_dispatch` for manual runs; concurrency group `coverage-md` (§4). (2) **Summarizers
  chained into `playtime-raw.yml`.** `playtime_summarize.py` and `ratings_summarize.py` now run
  as `if: always()` steps at the tail of the raw job, right after `playtime_refresh.py` commits
  its shards — so `playtime.json` / `ratings.json` refresh on every raw pass (~8×/day) and always
  track the shards they were computed from. The two standalone `*/4` workflows
  (`playtime-summary.yml` :47, `playtime-ratings.yml` :51) that this supersedes were **retired**
  as redundant (§4, §10). (3) **Two stale doc claims corrected.** The §4 summarizer table had
  ossified at the old `50 5`/`55 5` daily crons (superseded by the retirement above), and §8
  listed HLTB estimation as an unwired "open item." In fact `hltb_estimate.py` is a shared helper
  imported by `hltb_refresh.py` (as `HE`) and called live in the fill loop, so estimates already
  recompute every 2h as new reals land — the "open item" was written against the removed one-off
  `hltb_backfill.py` and is now closed (§8).
- **Ratings summarizer un-frozen + fail-loud (Jul 2026).** `ratings_summarize.py` was still
  reading the retired `playtime_raw.json` monolith after the shard migration, so every run hit
  `RAW_FILE.exists() == False`, logged "nothing to rate" and **exited 0 without writing** —
  silently freezing `ratings.json` at 6,855 games (07-05) while `playtime.json` advanced. Ported
  `iter_raw_shards()` from `playtime_summarize.py` so both read the same 64 shards (monolith kept
  as fallback), and added two fail-loud guards: no source → exit 0; **source present but 0 games
  → exit 1 and preserve the existing file** (never overwrite good data with empty, run goes red).
  On first run ratings jumped 6,855 → ~17,200, matching raw coverage (§10).
- **Summarizer cadence daily → every 4h.** `playtime-summary` (:47) and `playtime-ratings` (:51)
  were running once daily while raw scrapes ~8×/day, so summarized playtime and the weighted
  rating lagged raw by up to a day. Both are ~5s pure-local recomputes with **no** storefront
  budget cost, so the higher cadence is free; the lag is now hours, not a day (§4, §10).
- **Price budget 60 → 120 min.** `price_and_sale.py` rebuilds the full ~104k non-free price set
  each run; a full pass (price pass + on-sale end-date pass) needs ~75–90 min wall-clock during a
  Steam sale, so at `RUN_MINUTES=60` it wrapped mid-pass at `TIME_BUFFER` every run — which is
  what made prices look laggy. Bumped to 120 (`timeout-minutes` 75 → 135); the 3h cron still
  leaves ~60 min headroom (§4, §7, §14).
- **QHPP → QTPD rename + table restructure (Jul 2026).** Five frontend changes shipped together:
  (1) the metric was renamed **QHPP → QTPD (Quality Time Per Dollar)** end-to-end — title, logo,
  tagline, column header, tooltips, filter labels, formula text, internal sort key, CSS classes,
  and URL `sort` param (repo/GitHub `SteamQHPP` + `qhpp-wishlist` worker kept as legacy names;
  §3.2). (2) **Price + Discount merged** into one `Price / Sale` column with a **split header**
  whose two halves sort independently (Price by current price, Sale by discount depth); the sort
  machinery now selects on `.sortable` to include the inner split buttons (12 cols → 11, table
  `min-width` 1266 → 1240px; §11). (3) **Trend moved** to sit directly after Reviews (it's derived
  from them), before Weighted. (4) **Empty playtime cells render truly empty** (no `—` dash) so
  they add zero height instead of inflating data-sparse rows (§10, §11). (5) The **logo wordmark is
  now a filter toggle** — a real focusable `<button>` that opens/closes the filter nav like the
  collapse handle (§11). Layout mechanism note updated: the table is **CSS Grid** (`--grid-cols`
  `minmax()` tracks), not `table-layout:auto` + `<col>` as older entries below describe.
- **IGDB secondary source (Phase C) — built, evaluated, RETIRED.** Added an appid-keyed IGDB
  completion-time source to backfill games HLTB can't title-match. Two bugs fixed during
  bring-up (deprecated `category`→`external_game_source` filter; blank-entry worklist freeze),
  then dropped: IGDB's `game_time_to_beats` table is only ~8,829 records total, yielding just
  1,471 matches (1,007 net-new) — too small a ceiling to run continuously. Now dormant:
  `igdb.yml` schedule removed (dispatch-only), frontend merge reverted, `check_phase_c` removed.
  Files left in-repo but unreferenced (§8.1 has the full record + revival steps).
- **HLTB coverage recovery — Phases A & B (live).** `hltb_match.py` normalizes store titles
  (strips `®™`, edition/bracket tails, ALLCAPS, subtitles) and tries variants in order, recovering
  real games lost to title noise; the raw title is always tried first so matches can only widen.
  Blank re-scrape moved from a flat 60-day window to an attempt-scaled eager→backoff→freeze
  curve plus a never-idle drain so the job stops quitting early with budget left. Fail-fast
  self-checks (`hltb_selfcheck.py`) guard both at startup (§8.1).
- **Shard health monitor.** `shard_health.py` (daily via `shard-health.yml`) writes `SHARDS.md`
  — per-shard count/size, evenness, staleness, and a games-to-100 MB projection — so the file
  size that once froze the pipeline is now watched pre-emptively (§9).
- **Shard key fix: `appid % 64` → `(appid // 10) % 64`.** Steam appids are ~100% multiples of
  10, so the original key piled every game into the even buckets (32 empty, the rest 2× size,
  half the backfill throughput wasted). Dividing by 10 first spreads them evenly (max/mean
  2.07 → ~1.05). `ensure_sharding()` reshards in place on the next run via `SHARD_KEY_VER` (§9,
  §13).
- **Playtime raw sharded (100 MB fix).** A monolithic `playtime_raw.json` hit GitHub's 100 MB
  file limit at ~98 MB / 6,850 games; pushes were rejected and the old commit code swallowed the
  error, so runs went green-with-nothing-committed and the pipeline silently froze for ~2 days.
  Split into 64 shards under `playtime_raw/NN.json` (one bucket/run, rotated by
  `GITHUB_RUN_NUMBER`); `ensure_sharding()` auto-migrated the monolith and removed it. The commit
  path was rewritten to a robust single-writer push that hard-resets to `origin/main`, re-applies
  its shard, and **fails loud (red run)** instead of silently — and `playtime_summarize.py` now
  reads all shards (§8, §9).
- **Playtime scale-up: 4 → 8 slots, `STEAM_DELAY` 2.0 → 1.5s.** With `scrape.py` finishing in
  ~7 min and the frontier exhausted, the shared storefront budget had headroom, so the playtime
  raw pass now runs `:23` every 3h (near-continuous, ~24h/day vs ~12h/day) at 1.5s — roughly 2×
  review-time throughput (§14). Leans on the storefront ceiling; `recent_refresh.py` proves
  3h@1.5s holds, but watch 403 rates. Revert = delay back to 2.0 / fewer slots.
- **Frontend clarity + polish.** Added `title` tooltips to the HLTB-metric and Reviews-sort
  filter toggles (§3.2); added a visible `✓ require → ✕ exclude → clear` legend above the tag
  rail (§3.2); added subtle alternating row shading, scoped out of card mode (§3.2, §11). No
  logic change — CSS and markup only.
- **Playtime scrape: review-count floor.** `playtime_refresh.py` now gates candidate selection
  on `MIN_REVIEWS_FLOOR = 10` (§9, §13) — games with fewer all-time reviews can't clear the
  summarizer's ≥3-per-side split, so they're dropped from the queue instead of consuming
  storefront budget for a null median. Cuts the eligible queue ~44% (93k → ~52k), directing the
  full budget at games that can actually produce data. `TARGET_REVIEWS` (200) and the 4-slot
  cron are unchanged; the gate is re-evaluated against live `review_count`, so it's
  skip-for-now, not permanent exclusion.
- **Table layout: fixed → fluid.** The desktop table moved from `table-layout: fixed` with
  summed 1556px `<col>` widths to **`table-layout: auto`** with a **`min-width`/`max-width`
  per column** (§11): hard min floors for small-laptop legibility, best-effort max ceilings so
  slim sort columns don't bloat while Game and Tags absorb the slack on wide monitors. Table
  `min-width` is now the exact sum of column minimums (1444px). Fixed four issues at once —
  value overlap on wide rows, the dead gap between Tags and Playtime, cramped Tags, and poor
  use of large screens. Horizontal overflow stays on the page (not the scroll container) to
  keep the sticky `<thead>` from being trapped in a scroll box.
- **HLTB overlap fixed.** The HLTB cell is now a block with both lines `nowrap`, so the three
  figures and the `avg N h` line can't wrap into each other on wide free-game rows.
- **Card (mobile) layout improved.** Roomier spacing, a new ~560px phone breakpoint (smaller
  thumbnail, larger tap targets, HLTB row prioritized), and Tags rendered full-width and
  left-aligned instead of crammed against the right edge (§11 *Responsive*).
- **Weighted + Playtime columns** added to the table (§9, §10), loaded via the existing
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
