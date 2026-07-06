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
a small **Cloudflare Worker** proxy (§12).

**One writer per file.** Every data layer is owned by exactly one job. No two jobs ever
write the same file. This is the load-bearing decision that makes parallel scheduled
Actions safe: two jobs committing *different* files always rebase-merge cleanly, so they
can run and push concurrently without lock-step coordination or lost work. Adding a data
source means adding a file + a job, never touching another job's file.

**Merge in the browser.** The frontend downloads each file and merges them by `appid` into
one in-memory object per game, in a single O(n) pass at load. QHPP is computed client-side
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
   │ recent_refresh.py ──────► recent.json          │        │   computes QHPP) │
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

- **Completion rate (achievement-based).** Weight QHPP by how many players actually *finish*
  a game, not just its length — a 60-hour game most people drop halfway is a worse "hours you
  will really get" deal than its HLTB number implies. *What to do:* pull global achievement
  percentages (Steam `ISteamUserStats/GetGlobalAchievementPercentagesForApp`), pick a
  per-game "main story complete" achievement (the hard part — needs a heuristic or a curated
  map, since achievement naming is arbitrary), expose a completion-weighted QHPP mode
  (`hours × completion_rate × rating ÷ price`). *Highest-value new metric; medium effort,
  gated on the achievement-selection heuristic.*

- **Region-specific pricing.** Take the price (and therefore QHPP) from a user-chosen Steam
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
  conflict this item called out is **resolved** (that assumption is gone). *Still open:* a
  richer per-card mobile design — thumbnail+title as a proper card header, and optionally
  collapsing Price / Discount / Sale-ends into one stacked unit and hiding secondary fields
  behind a tap. *What to do next:* design the card header + decide which fields are
  primary-vs-secondary on a phone.

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

- **Verify column-add safety under the new fluid layout.** Under the old `table-layout: fixed`
  model, adding a column without a matching `<col>` collapsed the table — a known trap. The
  layout is now `table-layout: auto` (§11), which *should* be more forgiving, **but this hasn't
  been confirmed.** *What to do:* before the next column is added, test whether a new column
  still requires its `<col>` entry (and whether the min/max sizing still holds); update §11's
  regression note with the answer.

- **Min-review filter: verify it re-filters on change.** Reported that changing the min-review
  selection may not refresh the list. *What to do:* treat as a **possible bug** — verify the
  band toggles re-run the filter pass; fix if confirmed.

- **Min-review picker: single-select vs multi-select — open design conflict.** A request to
  make min-reviews a single choice (can't tick both 1k and 5k+) directly contradicts the
  current *deliberate* design: independent bands with gaps allowed (§11). *What to do:*
  **decision required** — keep the intentional multi-band model (and just document why), or
  switch to single-select. Do not change silently.

- **Sort by review count — verify it already works.** Columns are click-to-sort (§11), so
  Reviews should already sort. *What to do:* confirm; if the Reviews column doesn't sort by
  count, wire it up. *Likely already done.*

- **Visual polish. [Mostly done — original note is stale.]** The "2009 admin panel" feedback
  predates the current design, which already has a real type pairing (IBM Plex Sans + Mono, loaded
  via Google Fonts) and a **semantic** palette (gold = labels, blue = links, coral/red =
  discount/negative, teal/green = free/positive) — so the note's "one accent color" is actively
  wrong: those colors carry meaning and must not be flattened. Padding is already reasonable.
  *Done:* added the one genuinely-missing piece, **subtle alternating row shading** (translucent
  `rgba(127,160,230,.035)` on even rows so the card gradient shows through; declared before
  `:hover` so hover wins; reset to transparent in card mode). *Tuning knob:* the zebra alpha is a
  single value if it needs to be stronger/weaker after eyeballing on a real display.

- **Rename QHPP.** Feedback that "QHPP" is unfriendly to type/say; suggestions like "Bang for
  Buck" / "WorthIt". *What to do:* branding decision only — could rename the public label while
  keeping QHPP as the internal metric name. *Zero engineering, pure product call.*

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
so they touch no rate budget). Both read the sharded `playtime_raw/` set:

| Workflow file           | Script                  | Writes         | Cron       | Concurrency group |
|-------------------------|-------------------------|----------------|------------|-------------------|
| `playtime-summary.yml`  | `playtime_summarize.py` | `playtime.json`| `50 5 * * *` | `steam-playtime-summary` |
| `playtime-ratings.yml`  | `ratings_summarize.py`  | `ratings.json` | `55 5 * * *` | `steam-playtime-ratings` |

They run right after the overnight raw scrape and a few minutes apart, so each summarizes
fresh data. Because they don't call Steam and finish in seconds, their 15-minute timeout is
generous.

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
(via `backfill-hltb.yml`, sharing the `steam-hltb` concurrency group); both were run-once
utilities and have **since been removed** (§4). `fetched_at` on every
entry is groundwork for a future priority re-scrape whose order is: **partial entries first**
(≥1 real value), **blank entries second**, **full-real last**.

### 8.1 Coverage-recovery work (Phases A / B / C)

Motivation: a full first pass left **~77% of entries blank** (~94k of 122k). Auditing the
blanks showed two populations mixed together — genuinely-dead long-tail titles HLTB will
never have, AND real games lost to title noise or transient first-pass failures (e.g.
`Far Cry® New Dawn`, `EDENS ZERO` returned zero results under their raw store titles). Three
independent, sequentially-shipped changes attack this:

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

**Phase C — IGDB secondary source (`hltb_igdb.py` → `hltb_igdb.json`).** HLTB matching is
title-based and inherently lossy; IGDB is an **independent** completion-time source keyed off
the **Steam appid** (via `external_games`, category 1 = Steam), so it recovers games HLTB
structurally can't — raising the coverage *ceiling* rather than cleaning up within it. It is the
**sole writer** of `hltb_igdb.json` and **never touches `hltb.json`** (one-writer-per-file
preserved); the frontend merges by appid and prefers real HLTB, falling back to IGDB only where
HLTB has no usable value. IGDB `game_time_to_beats` returns seconds in `hastily`/`normally`/
`completely`; mapped `normally`→main (fallback `hastily`), `completely`→complete, with `extra`
left to the **same** `hltb_estimate` model (so IGDB-sourced rows carry identical `est`/blue-flag
treatment). Auth is Twitch OAuth client-credentials via `IGDB_CLIENT_ID` / `IGDB_CLIENT_SECRET`
GitHub secrets; the job no-ops cleanly (stays green) if the secrets are absent. Own workflow
(`igdb.yml`, concurrency group `steam-igdb`, 6-hourly). **Frontend merge (Phase C.5):**
`index.html` fetches `hltb_igdb.json` alongside the other layers and applies IGDB values only
where HLTB left `hltb_avg == null`, tagging origin via `game.hltb_source` (`"hltb"` / `"igdb"`);
IGDB rows reuse the existing `est` blue-flag rendering unchanged.

**Self-checks (`hltb_selfcheck.py`).** All three phases ship with fail-fast, pure-logic
regression guards run at the top of `hltb_refresh.main()` (and importable by the IGDB job).
A failed assertion **aborts before any file is written** — a loud red failure rather than
silent coverage decay (same failure class as the `playtime_raw` silent-green bug).

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

---

## 11. Frontend (`index.html`)

A single self-contained page. On load it fetches every JSON file, merges them by `appid`
into one object per game (one O(n) pass — important at ~68k+ games), then renders, filters,
and sorts entirely client-side. Until real JSON exists it renders bundled `SAMPLE` data.

**QHPP computation.** `computeQ(game, basis)` = `(selected HLTB hours × rating%) ÷ price`,
where *basis* picks the **Sale** (after-discount) or **Full** price, and the selected HLTB
hours follow the **HLTB metric** toggle (main / +extras / 100% / avg). Null for free games
and games with no usable HLTB value. The score is recomputed on toggle, never stored.

**The table (12 columns).** In order: Game · Reviews · **Weighted** · Trend · Price ·
Discount · Sale ends · Released · Tags · **Playtime** · HLTB · QHPP.

- The table is **`table-layout: auto`** (fluid) with an explicit `<colgroup>` of
  `<col class="c-*">` — one per column — carrying a **`min-width` / `max-width` per column**.
  The `min` is a small-laptop legibility floor; the `max` is a breathing ceiling so the slim
  numeric/sort columns (Trend, Price, Discount, Weighted, Sale) don't bloat on a wide monitor.
  On large screens the slack concentrates on the content-heavy columns — **Game and Tags** —
  which carry the big ceilings; everything else stays near its natural width. The table's
  `min-width` is the **exact sum of the column minimums (1444px)**, so below that the **page**
  (not the table card) scrolls horizontally — deliberately *not* `overflow-x:auto` on the
  scroll container, because a lone `overflow-x:auto` is promoted by browsers to `overflow:auto`
  on both axes, which would trap the sticky `<thead>` in a scroll box. Below ~1040px the table
  stops being a table and becomes the stacked **card layout** (see *Responsive* below).
  - **Caveat — `max-width` is best-effort here.** Under `table-layout: auto` the per-column
    **`min-width` floors are hard and reliable**, but `max-width` on `<col>` is only a *hint*:
    the ceilings mostly hold because auto-layout naturally routes extra width to Game/Tags
    rather than short-content columns, but on an extreme ultrawide a slim column *could* grow
    past its stated max. The firm fix, if it ever matters, is a CSS-Grid table with
    `minmax()` tracks (deferred — larger refactor, must re-verify sticky thead + sort arrow).
  - **Regression watch (unconfirmed under fluid).** Under the *old* `table-layout: fixed`
    model, adding a column without adding its `<col>` **collapsed the layout** (a real bug when
    Weighted + Playtime were first added). Fluid `auto` layout is expected to be more forgiving
    (it sizes from content, not solely from `<col>`), **but this has not been re-verified** —
    treat "does adding a column still need a matching `<col>`?" as an **open item to confirm**
    (§3.2) before relying on it.
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
  The stack is a **block with both lines `nowrap`** (the three figures on one row, `avg N h`
  on the next) so large numbers can't wrap into each other — this, plus a raised HLTB column
  `min-width`, fixes the overlap that showed on wide free-game rows.
- **QHPP** shows the value plus a **log-scaled gold value-meter** bar. On a discounted game
  it shows both the **Sale** (primary/gold when that basis is active) and **Full** value
  (`… full`); on a game **not** on sale it shows a single value tagged **`full`** in a
  neutral color, so a full-price value is never mistaken for a discount deal.

**Responsive / card layout.** The table is for the desktop width range; a **fluid table alone
cannot fit a phone** (twelve columns at legible minimums sum to ~1444px). So below **1040px**
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
