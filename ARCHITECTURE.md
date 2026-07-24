# QTPD — Architecture

The engineering deep-dive for **QTPD** (Quality Time Per Dollar — *formerly QHPP, "Quality
Hours Per Price"; the repo, GitHub project, and wishlist worker keep the legacy `qhpp`/`SteamQHPP`
names*), a static Steam value-hunter that ranks games by quality-adjusted playtime per dollar.
For the quick overview and setup, see **[README.md](README.md)**; this document explains *why* the
system is shaped the way it is, every job and data file, and how the frontend turns raw
JSON into the table. For what might be built *next* — the backlog, deferred items and
decided-againsts — see **[ROADMAP.md](ROADMAP.md)** (§3, split out of this file).

**Companion docs:** [ROADMAP.md](ROADMAP.md) (plans) · [COVERAGE.md](COVERAGE.md) and
[SHARDS.md](SHARDS.md) (generated — always current, never hand-edit) ·
[PICS_METADATA_PIPELINE.md](PICS_METADATA_PIPELINE.md) (the PICS layer's design record) ·
[UPCOMING_GAMES_PICS_MEMO.md](UPCOMING_GAMES_PICS_MEMO.md) (a parked decision).

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
                    GitHub Actions (scheduled)                        GitHub Pages
   ┌────────────────────────────────────────────────────┐        ┌──────────────────┐
   │ scraper.py ──────────────► games.json + catalog.json│        │                  │
   │ price_and_sale.py ───────► prices.json              │        │   index.html     │
   │ hltb_refresh.py ─────────► hltb.json                │  read  │  (merges all     │
   │ tags_refresh.py ─────────► tags.json                │ ─────► │   JSON by appid, │
   │ recent_refresh.py ───────► recent.json              │        │   computes QTPD) │
   │ playtime_refresh.py ─────► playtime_raw/NN.json     │        │                  │
   │        ├─ playtime_summarize.py ─► playtime.json    │        └──────────────────┘
   │        └─ ratings_summarize.py ──► ratings.json     │
   │ updates_refresh.py ──────► updates_raw/NN.json      │        ┌──────────────────┐
   │        └─ updates_summarize.py ──► updates.json     │        │ Cloudflare Worker │
   │ pics_refresh.py ─────────► pics_raw/shard_NN.json   │wishlist│  (steamid/vanity  │
   │        ├─ pics_summarize.py ─────► pics/shard_NN    │◄──────►│   → wishlist)     │
   │        └─ pics_merge.py ─────────► pics.json        │        └──────────────────┘
   │ coverage.py ─────────────► COVERAGE.md              │
   │ shard_health.py ─────────► SHARDS.md                │  (generated docs, not read
   └────────────────────────────────────────────────────┘   by the frontend)
```

All scraping is server-side; the browser only reads JSON and (optionally) calls the Worker.
The frontend fetches **12 files**: the eight data layers above (`games`, `prices`, `hltb`,
`tags`, `recent`, `playtime`, `ratings`, `updates`), the merged `pics.json`, and the three
static decode maps in `lookups/` (`tags.json`, `genres.json`, `categories.json`, §9.6).
`catalog.json`, the `*_raw/` shard sets, `pics/`, and the two generated `.md` files are
never served to the browser.

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

- **Developer update cadence & size. [Done — pipeline built; frontend backfill live.]**
  Beyond the single `last_update_ts` already in `games.json`, surface *how often* and *how
  substantially* a game is patched. *Done:* built as its own sharded pipeline (§9.5) rather
  than a News-API heuristic — `updates_refresh.py` → `updates_raw/NN.json` (64 shards) →
  `updates_summarize.py` → `updates.json`, keyed off Steam's native `event_type` (13/14/12 =
  major/regular/minor) so "big vs small" is Valve's own taxonomy, not a post-length guess.
  `updates.json` ships per-tier `last_*_ts`, windowed `counts` (30/90/180/365d), and capped
  `dates` arrays for client-side window recompute. The frontend already loads `updates.json`
  and uses `last_any_ts` to backfill null `last_update_ts` (News-API stays primary for now).
  *Update (2026-07): the dedicated **Updated column** is now shipped — a standalone
  sortable column (between Released and Tags) showing last-update recency plus a
  patch-cadence badge (`N · 90d` / `N · 1y`, summed across tiers from `updates.json` counts),
  degrading to nothing where updates.json has no coverage yet. Sort is by update recency.*
  *Still open (tracked in §9.5): the **precedence switch** — flip the event-based layer from
  fallback to primary once shard coverage is broad enough to beat the News-API's ~42% null
  `last_update_ts`. Trigger: leave News-API primary (the column still sorts by it) until the
  event layer covers materially more games than News-API does, then invert the precedence in
  `index.html`. **Live coverage is now tracked in `COVERAGE.md` (Axis 1, `updates.json` /
  `updates_raw/` rows) — read the current figure there rather than any hand-written count.**
  (As of 2026-07 the rotation had advanced well past its start — tens of shards populated, not
  the "1/64" this note used to claim — which is exactly the drift that motivated adding the
  updates layer to coverage tracking; see §11.5.)*

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

- **Soften the `success:false` permanent-skip (robustness, not a new metric). [Done —
  2026-07.]** `build_record` used to permanently skip any app whose Steam `appdetails`
  returned `success:false`, which lumped genuinely-dead/delisted apps together with
  **region-locked (cc=us)** titles and transient `success:false` blips — so a handful of
  legitimate games were dropped forever (≈731 were on the skip list, an unknown fraction
  recoverable). *Done:* `success:false` now returns a new **`"recheck"`** sentinel instead of
  `"skip"` (the confirmed-non-game `type != "game"` path keeps its permanent `"skip"`). A
  bounded `catalog["recheck"]` map (`{appid: strikes}`) retries the app across runs —
  `select_work` re-queues it as fresh work because it's neither stored nor skipped — and only
  after **`MAX_RECHECK` (=4, env-overridable)** strikes is it promoted to a permanent skip. A
  success or pending result clears its strikes; each run also prunes entries no longer in
  Steam's app list (delisted-and-removed apps), so nothing lingers in limbo. See §5. *Low
  impact (small count), low effort; safe quick win.*

- **Review-TEXT analysis: keyword / sentiment extraction (a free data source — no new
  scrape).** *The exception to this section's rule that every item is a new scrape:* we
  already fetch the **full written review text** on every playtime run and discard it.
  `playtime_refresh.py` calls `appreviews` with `num_per_page=100, filter=recent,
  language=all` (~line 455), so Steam returns complete review objects — but `_parse_review`
  (~lines 410–428) keeps only `{pt, up, ts}` and drops the rest. The same `rv` also carries,
  at **zero extra request cost**: **`review`** (the text), `votes_up` / `votes_funny` /
  `weighted_vote_score` (helpfulness), `language`, `steam_purchase`, `received_for_free`,
  `written_during_early_access`, `comment_count`. (The other two callers are *not*
  candidates: `scraper.py:rating_from_reviews` asks for `num_per_page=0` = zero bodies, and
  `recent_refresh.py` reads only the 30-day `query_summary`.) *The real constraint is
  storage, not fetching:* raw prose for ~1000 reviews × ~78k games is 1 GB+, and the playtime
  set is **already sharded across 64 files** because it hit GitHub's 100 MB/file cap (§9,
  SHARDS.md) — so the design must **extract at scrape time and store only aggregates**,
  exactly like `playtime_summarize.py` does for medians. *What to do (Option A, recommended):*
  in `_parse_review` also read `review` + `language`; during the existing walk (text already
  in memory) count hits from a curated lexicon split by ▲recommend / ▼not (e.g. `buggy`,
  `crash`, `optimiz`, `p2w`, `grindy`, `masterpiece`, `refund`, `unfinished`,
  `microtransaction`, `addictive`); write only the counts to a **new one-writer
  `review_keywords.json`** (respecting §1 — new file, not a change to the playtime shards),
  merged by `appid` in the frontend as a "common praise / complaints" column or tooltip.
  *Higher-ambition alternatives if A proves useful:* **B** — keep the ~5 most-helpful review
  texts per game (by `weighted_vote_score`) for representative quotes (bounded text storage,
  own sharded file); **C** — LLM one-line "what players say" summary per game (highest value,
  but adds an external-model dependency + cost + batch job, breaking the "runs entirely free
  on GitHub Actions" model). *Open decisions before coding:* hand-curate the lexicon vs.
  derive it from a live raw-`appreviews` sample first (recommended — eyeball the text, tune
  terms); English-only the keyword pass via the `language` field vs. multilingual term lists;
  and confirm a separate file over piggybacking the playtime shards' `summary` block (leaning
  separate — keeps one-writer-per-file and doesn't grow the already-capped shards).
  *Free-to-fetch, low-medium effort, de-risk with Option A before B/C.*

### 3.2 Frontend / UX (no new scraping)

Works off data already collected. Several are cheap and high-impact.

- **Mobile / narrow-screen layout — highest-frequency complaint. [Done.]** The old
  `table-layout: fixed` grid overflowed small screens, titles truncated, and the sale badge +
  discount % wasted a column. *Fixed in stages:* the desktop table became **fluid** (`auto`
  layout, per-column min/max — §11); the old fixed-1556px conflict this item called out is
  **resolved**; **Price and Discount** merged into one `Price / Sale` column with a **split
  header** whose halves sort independently (Price by current price, Sale by discount depth). Then
  (2026-07) the narrow-screen view was **rebuilt into a proper card** (§11 *Responsive*). Below
  1374px each row is now a **single-column spec-sheet card**: a **thumbnail + title header**, the
  **QTPD** value + meter as the headline metric, then one metric per line (fixed **label gutter**
  + value) in a **logical order** — name → QTPD → price → ratings → length → release → updates →
  tags — set by CSS **`order`**, independent of the table's column order. Column names are
  relabeled to plain words on mobile (Reviews→**Rating**, HLTB→**Length**, Price / Sale→**Price**)
  and **no-data cells are dropped** (`:has()`) so cards carry no dead "—" lines. Crucially,
  because the sortable `<thead>` is hidden in card mode, a **native `<select>` Sort control +
  direction toggle** was added to the bar — **sorting on a phone was previously impossible**.
  *Still open (optional):* progressive disclosure — folding secondary fields behind a per-card
  tap. *Known caveat:* CSS `order` reorders visually only, so screen-reader / tab order still
  follows the table's DOM column order.

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

- **Sequel / franchise linkage — "what has a sequel and what it is."** ~~Steam exposes no
  structured franchise graph; would need an external DB.~~ **Partly solved by PICS:** the
  `associations` block yields a structured `franchise` name, now shipped to `pics/` at **23.5%**
  (29,067 titles) — no external DB needed for franchise *grouping*. What PICS does **not** give
  is an ordered sequel graph (which title is the sequel of which); that ordering would still need
  heuristics or an external DB. *Franchise grouping: available now (parked, backend-only). Ordered
  sequel graph: still open.*

- **Correlate dev teams across games.** "Made by the same people who made X." **PICS `dev`/`pub`
  (99.9% / 99.6%, structured) makes same-developer grouping directly available** — no external DB
  for the grouping itself. Parked backend-only for now (no frontend). *Was "harder than franchise
  linkage"; now the easy half is a shipped field.*

- **"Talked about vs actually playing" metric.** Buzz-vs-engagement. Needs a social/mentions
  data source cross-referenced with playtime. *No clear source; vague; park it.*

- **Steam Trading Cards resale helper.** "How many trading cards would I sell to afford this
  game." Self-contained but niche; needs card market-price data. *Low priority.*

- **Studio-health signals (layoffs / employee turnover %).** Suggested as a publisher-health
  angle. Only viable source floated was LinkedIn scraping, which is a **ToS problem** and far
  off-mission. *Recorded but not recommended.*

### 3.4 Lorenzo list — filter/view patterns (comparison-sourced 2026-07; **SHIPPED**)

Ideas lifted from a **side-by-side with Lorenzo Stanco's Steam Wishlist Filters tool**
(<https://www.lorenzostanco.com/lab/steam/wishlist/>), reviewed from its two view modes
(**Detailed list** and **Cool grid**). Context: his tool is a **wishlist organizer** — filters
by tags / features / platforms / languages, light on metrics; QTPD is a **value-analysis
engine** over the whole catalog. So we borrowed his **filter *organization* and view modes**,
not his features. All frontend / UX only (no new scraping) — see §3.2.

**Status: L1–L5 all built, tested, and merged to `main` (PR #6, 2026-07). Two follow-up
polish passes then shipped (R2, R3 — see "Refinements" below).** This section is now an
**as-built record**: each item marks what shipped, the decisions taken (resolving the earlier
open questions), and anything **decided against**. See the *Implementation reference* subsection
for the concrete state/URL/CSS contract, and *Future work* for what's still open.

- **L1. Accordion filter sections — ✅ SHIPPED.** The flat `bar-filters` wall was split into
  **independently collapsible `.filter-section` accordions**, each with a header showing an
  **"N active" count badge**. **As-built grouping** (started as 4 sections; R3 merged Activity
  into Quality → **3 sections**):
  - **Value** — QTPD price basis, HLTB metric, HLTB data, price range, on-sale, QTPD range.
  - **Quality & Activity** — min rating, reviews-sort, review trend, min reviews, updated-within,
    playtime-sort.
  - **Tags** — click-cycle legend, tag rail, and the **ALL/ANY match toggle** (see L-tags below).
  - *Decisions:* open/closed state **persists in `localStorage["qtpd.sections"]`** (not the URL —
    keeps shared links clean). **Defaults:** Value + Quality open, **Tags folded**. The planned
    "weighted" control under Quality was **not added** (there is only a Weighted *column*, no such
    filter). The wishlist import row stays outside the accordions (global action) — clicking its
    dead space folds the whole bar instead. See "Fold zones" in the changelog for what folds a
    section: the header, plus any dead space in its row.

- **L2. Collapsed-bar filter summary + inline editors — ✅ SHIPPED.** Rejected the permanent
  sentence (space waste); instead `#filterSummary` renders **only when the nav bar is compact**
  (`.topbar.compact`). It lists the currently-picked filters as clickable `.sumf` chips plus a
  trailing sort chip. *Decisions:* clicking a chip opens an **inline popover** (`#popHost`) that
  **drives the real hidden controls** (`.click()` on the actual buttons — zero duplicated state
  logic); segmented/toggle/multi/sort filters get an in-popover editor, while **complex filters
  (search, price, QTPD range, tags) deep-link** into the expanded section instead.

- **L3 / L5. View switcher (Table · Card · Grid) + cool grid — ✅ SHIPPED.** Switcher lives in the
  `.bar-tools` cluster (right of the bar). **Grid** = Steam **header art** (`header.jpg`, capsule
  fallback) + a **QTPD badge overlay** + discount flag + coloured status border (on-sale = gold).
  **Tap a card to expand** → shows QTPD / price / rating / length and **`Steam ↗` / `Close ✕`**
  actions. *Decisions:* layout is **class-driven** — `body.layout-card` (stacked spec-sheet),
  `body.grid-view` (grid), `body.narrow` — set by `applyLayout()` + a tiny inline FOUC script.
  The **"detailed" view is one thing relabelled per device**: **Table = desktop-only, Card =
  mobile-only** (the off-device button is dimmed and shows a hint toast). View persists in
  `localStorage["qtpd.view"]`. Breakpoint = **1374px** (the table's real floor) via `matchMedia`.

- **L4. Utility actions — ✅ SHIPPED** (in `.bar-tools`).
  - **Random** — picks from the **current filtered list**, grows the page until the pick is
    rendered, then **scrolls to + flashes** its row/card. *Decision:* went with **option (b)** —
    surface the pick **within QTPD** (not open Steam), since the grid card now *is* a detail view.
  - **CSV export** — a **column-picker popover** (15 columns; defaults: name, Steam URL, QTPD,
    price, rating). Exports the whole filtered set, honouring the basis/metric toggles; BOM + CRLF.
  - **Copy link** — one-click copy of the current (already state-encoding) URL to the clipboard.

**Refinements — Round 2 (mobile polish, 2026-07).** ✅ SHIPPED.
  - Mobile card **group-separator lines** chunk the tall spec-sheet into price · ratings ·
    length/playtime · dates · tags.
  - **Per-page selector hidden on mobile** (meaningless on an infinite-scroll list).
  - **Table = desktop-only / Card = mobile-only** device-aware disabling (see L3).
  - **Wishlist import demoted** to the bottom of the mobile filter panel so quick filters lead.

**Refinements — Round 3 (declutter + tags, 2026-07).** ✅ SHIPPED.
  - **Activity merged into "Quality & Activity"** — one fewer section/header, no wasted whitespace.
  - **Default-option-on-left theme** (repo-wide convention): the default value is the leftmost
    button. Applied: HLTB data → **Real**, All; Reviews sort → **30-day**, All-time.
  - **Tagline trimmed** to "quality time per dollar" (dropped "· steam value hunter").
  - **View tools never reflow** when switching Table↔Grid: the top-bar sort shows on **mobile
    only**; **desktop grid gets its own `#gridSort`**, so `bar-main` is identical across desktop
    table/grid. `body.narrow` splits the two sort controls; `bindSortControl()` wires both and
    `setSort()`/`syncMobileSort()` keep them in step.
  - **Tag AND/OR match mode** — `state.tagMode` (`"and"` default / `"or"`), toggled by the
    prominent **"Required tags match: ALL / ANY"** control that sits **on one line with the
    click-cycle legend** (right-aligned) in the Tags section — the legend's base `flex-basis:100%`
    is overridden inside `.tagmode-bar` so the toggle doesn't wrap to a second, empty row. Required
    (✓) tags combine with AND or OR; **exclude (✕) is always AND-NOT**. Serialized as `tagmode=or`.
  - **Grid card expand fix** — an open card originally *grew to fit* its info so the `Steam ↗`/
    `Close ✕` actions weren't clipped on tiny mobile cells. **Superseded by R4's fixed-height cards.**

**Refinements — Round 4 (grid sizing + tag mini, 2026-07).** ✅ SHIPPED.
  - **Fixed-height grid cards** — cards use a fixed height (`--gh`: 188px desktop / 176px phone)
    instead of an aspect ratio, so a card is the **exact same size collapsed or expanded** — clicking
    never resizes it and the grid never reflows (the deliberate "bigger cards, one height" trade;
    replaces R3's grow-to-fit). The box art fills via `object-fit:cover` (centre-cropped); `--gh` is
    sized to fit the expanded KPIs + actions, with `.ginfo{overflow-y:auto}` as a safety net.
    **Superseded by R5** — the fixed height cropped the art and clipped titles; R5 sizes the art by
    aspect ratio and drops `--gh`.
  - **Collapsed-Tags mini chips** — when the Tags section is folded, the currently-picked tags render
    as chips in the header's otherwise-empty middle band (`.tag-mini` / `#tagMini`, built by
    `buildTagMini()` from `buildTagRail()`). The overlay is `pointer-events:none` (clicking the empty
    area still opens the section) while the chips are `pointer-events:auto` and reuse the normal
    `[data-tag]` cycle handler (require → exclude → clear). Hidden when the section is open or empty.

**Refinements — Round 5 (grid card rebuild + inline tag chips, 2026-07).** ✅ SHIPPED.
  - **Grid cards rebuilt "art on top, info below"** — *replaces R4's fixed-height cover-crop.* The
    art frame is sized by **`aspect-ratio:460/215`** (the exact Steam header ratio) with
    `object-fit:cover`, so header art fills it with **no crop and no letterbox matte** at any column
    width. Below it sits a **solid, darker (`#080b12`) info panel**: a stats line with **QTPD (left)
    and the best-available rating (right)** over the title. The on-art QTPD badge is gone (moved into
    the panel); the discount stays as a corner flag. Card height = art + panel (no fixed `--gh`);
    columns share width so each row stays even. Tap flips to a slimmed details overlay
    (**price / length / `Steam ↗`**). Rating uses a fallback chain **playtime-weighted (`wr`) →
    recent 30-day (`recent_pct`) → all-time (`rating_pct`)**, colour-coded via `ratingColor()`, with
    a `wtd`/`30d`/`all` source tag + review-count tooltip — **superseded by R6** (now the plain
    Steam all-time %).
  - **Compact summary: tags are inline cycle-chips.** `renderSummary()` no longer emits a single
    `tags +2 −1` chip that deep-links into the bar (the old L2 behaviour). The interacted tags now
    render as their **own `.chip` inc/exc cycle-chips at the end of the line** (shared with the
    collapsed Tags header via `interactedTagChipsHTML()`), each re-cyclable in place; an **`all`/
    `any`** chip (`data-sumf="tagmode"`) trails when >1 tag is required and opens its own popover. So
    **every** summarized filter now edits in place or via a small popover — only the input-heavy
    search / price / QTPD-range chips still deep-link into the expanded bar.
  - **Tag linger + fade (mis-click undo).** Cycling a tag back to neutral no longer removes its chip
    instantly: it **holds full opacity ~3s, then fades to 0 over ~3s** (JS-driven opacity via
    `state.tagLinger` map + `startLingerTicker()`, robust to re-renders), then is dropped so the line
    may collapse. Re-cycling the chip during the window cancels the fade. Applies to both the summary
    line and the collapsed Tags mini-rail.
  - **Folded filter bar tidy-up.** `.bar-filters` vertical padding → 0; the wishlist row gets its own
    balanced vertical space and is centred; the divider between the wishlist row and Value is dropped
    so the input isn't sandwiched between two lines (desktop only — mobile keeps the reordered
    wishlist row and its separator).
  - **QTPD range control** — the current value moved **onto the label line** (`QTPD range 0 to ∞`)
    and the `(log scale, fits current results)` note demoted to a **hover tooltip** on the label.

**Refinements — Round 6 (mobile overhaul, 2026-07).** ✅ SHIPPED.
  - **Grid is the default view on mobile** — `init()`: no saved `qtpd.view` + `isNarrow()` ⇒ `grid`
    (desktop still defaults to Table; a saved choice always wins).
  - **Leaner mobile top bar that fits the screen.** The dedicated top-bar **Sort** control is gone on
    mobile (`#mobileSort` hidden everywhere) — sorting lives on the summary line's "sorted by …" chip.
    **Per-page** is now hidden on mobile **grid** too (was only hidden in card layout); infinite scroll
    loads 100/page. The `meta` count line is hidden in the compact browsing nav (dupes the summary).
    Bar gaps/padding tightened so nothing runs off the right edge.
  - **Sticky filter bar (mobile / `body.narrow`).** While browsing (compact), the nav is
    `position:sticky; top:0` so filters are always one tap away. When the panel is **open**, the
    **"Hide filters" bar is `position:sticky; bottom:0`** — pinned to the viewport bottom so the close
    control is never scrolled off-screen, releasing to scroll up at the panel's end. Needs a
    non-clipping ancestor, so `.barwrap` drops `overflow:hidden` on mobile (rounded corners moved to
    the first/last child).
  - **Random hidden until filtered** — `#randomBtn` shows only when ≥1 non-default filter is active
    (toggled in `renderSummary()` from the `active` flag; applies on all screen sizes).
  - **Scroll-collapsed sticky nav** — once scrolled >24px on mobile, `body.nav-scrolled` (a passive
    scroll listener) hides the logo/search/view/tools row (`.bar-main`) inside the sticky compact
    nav, leaving just the filter summary + the Show-filters bar; scrolling back to the top or opening
    the filters restores it. Keyed on `body.narrow` so **Card and Grid behave identically**.
  - **Warning-styled Reset on the summary line** — a coral, uppercase **`Reset`** floats to the line's
    top-right whenever filters are active (`.sumreset` / `data-reset`, reuses the real `#clear`),
    replacing a trip into the panel for "Reset all filters".
  - **Leaner tools cluster labels** (all screens, keeps the mobile bar from overflowing once Random
    appears): Random renamed **`Lucky`** (no dice icon), **CSV** keeps letters only (no ⬇), and Copy
    link is **icon-only `🔗`** (with `title` + `aria-label="Copy link"`).
  - **Grid card rating simplified** to the **plain Steam all-time review %** (number + `%`, no
    source letter) — supersedes R5's weighted→recent→all-time fallback; `bestRating()` removed.
  - **Grid card info panel finalised** — *supersedes R5's "stats line over the title".* The **title
    leads on top**; the **Steam rating shares the title's line, right-aligned** (`.gmeta-top` =
    `.gname` flex:1 + truncate, `.grate` flex:none); **QTPD sits on its own line below**. Reads
    title-first with the rating at a glance and QTPD as the standout metric underneath.

**Implementation reference (as-built).** For a future session touching this UI (`index.html`):
  - **State additions** (on `state`): `view` (`"table"|"card"|"grid"`), `tagMode` (`"and"|"or"`),
    `tagLinger` (Map `tag → clearedAt` ms, for the summary/mini chip fade — R5).
  - **localStorage keys:** `qtpd.view`, `qtpd.sections` (JSON `{value,quality,tags:bool}`).
  - **Body classes** (set by `applyLayout()` + inline FOUC script): `layout-card`, `grid-view`,
    `narrow`. Breakpoint `matchMedia("(max-width:1374px)")`; phone tier `@media (max-width:560px)`.
  - **URL params:** existing set + **`tagmode`** (see §2/§ URL-state; `syncURL`/`loadFromURL`).
  - **Key functions:** view — `applyLayout` / `setView` / `updateViewButtons` / `gridCardHTML`
    (+ `bestRating` for the card's rating fallback — R5);
    accordions — `applySections` / `toggleSection` / `sectionActiveCounts` / `updateSectionCounts`
    / `markChangedControls` (per-control gold highlight);
    summary+popover — `renderSummary` / `openSummaryEditor` / `buildOptionPopover` /
    `buildSortPopover` / `showPopover`; tag chips + fade (R5) — `interactedTagChipsHTML` /
    `tagChipHTML` / `buildTagMini` / `startLingerTicker` / `applyLingerOpacities`;
    utilities — `randomPick` / `openExportPopover` / `doExportCSV` (`CSV_COLS`) / `copyLink`;
    sort — `bindSortControl` / `syncMobileSort`.
  - **Shared-class gotcha — `.gmeta` is used by BOTH views.** In the table it's the title cell
    (`min-width:136px; flex:1`); in a grid card it's the dark info panel (`background:#080b12`).
    The grid rule **must** be scoped `.gcard .gmeta`, not bare `.gmeta`, or the near-black panel
    background bleeds behind table-view titles as an ugly black block. (Regression fixed post-R6.)
  - **"Changed from default" = gold (single source of truth).** A control at its default reads
    neutral (blue-ish, grey pressed state); a control the user has *manually adjusted* lights up
    gold, so an opened accordion shows at a glance what's been touched (mirrors the header's
    `N active` badge, one level down). `markChangedControls()` (called from `renderMeta` after
    `updateSectionCounts`) adds `.changed` to the pressed button(s) of any non-default group.
    Its default definitions **must** stay in lockstep with `sectionActiveCounts()` — both read the
    same `state` fields against the same defaults (`qBasis:"after"`, `hltbMetric:"main"`,
    `hltbQuality:"real"`, `minScore:0`, `ratingSource:"recent"`, `revBands:[1,2,3,4]`, full trend
    set, `updatedWithin:"any"`, `ptMetric:"up"`; price bounds `null`; `qRangeTouched:false`). CSS
    hooks: `.seg button[aria-pressed="true"].changed`, `.numinput.changed` (price boxes),
    `.rangelabel.changed` (QTPD range). The old blanket `.seg.metric [aria-pressed]` gold was
    removed — it lit "Main" gold even untouched, defeating the whole signal. Sale/Wishlist toggles
    are gold whenever pressed (pressed == changed for them); **tag chips keep their own green/red
    include/exclude colours** and are deliberately left out of the gold convention.
  - **Test harness:** `.claude/launch.json` "sample" config serves a copy of `index.html` with no
    JSON so the app falls back to its 6-game `SAMPLE` — fast, deterministic UI testing.

**Future work / still open.**
  - **Short-link encoder for filter URLs — ⏳ NOT DONE (design pending).** QTPD packs full
    filter/sort state into the querystring, so shared links are long. Encode into a short code
    that expands back. *Architecture tension (see §1 static-first, §12 Worker):* a **true**
    shortener needs persistent storage. Two routes: **(1)** client-side reversible compression
    (LZ-string / bitfield in the URL fragment — 100% static, no storage/abuse surface, but only
    "shorter", not "a few digits"); **(2)** Worker + KV `{shortcode → state}` (truly short random
    codes, but adds a stateful write path with abuse/expiry concerns — departs from static-first).
    **Recommendation: evaluate (1) first;** reach for (2) only if genuinely-short codes are a hard
    requirement.
  - **Mobile progressive disclosure (per-card fold, §3.2 / §11)** — partially addressed by the
    grid tap-to-expand; the spec-sheet **card** view still shows all fields at once.

**Decided against (do NOT re-add without cause).** Permanent L2 summary sentence (space waste →
collapsed-only); a "weighted" *filter* control (only a column exists); per-page selector on mobile
(removed as noise); forcing the "Card" view on desktop / "Table" on mobile (they render identically
per device, so the off-device button is disabled instead).

---

## 4. Jobs & workflows

Each job is a workflow in `.github/workflows/`. All use `actions/checkout@v5` +
`actions/setup-python@v6` (Node 24). **v5/v6 is deliberate, not the newest** — checkout v5
preserves the credential-persistence behavior the `fetch → rebase → push` commit pattern
depends on; v6/v7 changed it. Every writer job has `permissions: contents: write` and a
`concurrency` group so a job never overlaps itself.

**Naming scheme.** Workflow `name:` fields carry a tier prefix so the Actions sidebar sorts by
pipeline hierarchy:

| Prefix | Meaning | Workflows |
|---|---|---|
| `0.` | **Publish** — puts the site live; the one job a user actually sees the output of | `pages.yml` |
| `1.` | The catalog scraper — the only finder of new games | `scrape.yml` |
| `2.x` | Refreshers — enrich games the scraper already found | prices, recent, playtime-raw, updates, hltb, tags, pics |
| `3.x` | Summarizers — pure local recompute, `[2.3 / manual]` marks that job 2.3 is their real trigger | playtime-summary, playtime-ratings |
| `4.x` | Monitors / generated docs | shard-health, coverage |
| `[ONE-OFF]` | Run-once utilities, deletable when drained | `queue-null-updates.yml` |

**14 workflow files, 13 numbered** — only the `[ONE-OFF]` sits outside the sequence, which is
deliberate: it isn't part of the standing pipeline. Numbering is cosmetic — nothing keys off
it — with one exception: `coverage.yml`'s `workflow_run` trigger matches the scrape workflow's
**exact `name:` string**, so renaming `scrape.yml` silently breaks it (see the callout below).

> **A 15th workflow appears in the Actions sidebar that is not in this repo:
> `pages-build-deployment`.** GitHub injects it automatically whenever **Settings → Pages →
> Source** is set to *"Deploy from a branch."* It cannot be renamed, numbered, or deleted from
> `.github/workflows/` — it is not a file. Its presence means the Pages source was **never
> switched to "GitHub Actions"**, which is exactly the condition `pages.yml`'s own header
> warns about: the branch build keeps firing on *every* push to `main` — i.e. on every scraper
> checkpoint commit, ~50+ rebuilds/day — alongside the scheduled deploy. Switching the source
> to **GitHub Actions** retires it and is the whole point of `pages.yml` existing. See §16.

**Steam-facing scrapers** (compete for the storefront rate budget only where noted):

| Workflow / script       | Owns file           | Cadence          | Notes |
|-------------------------|---------------------|------------------|-------|
| `scraper.py`            | `games.json`, `catalog.json` | long runs, off-peak | The only finder of *new* games. |
| `price_and_sale.py`     | `prices.json`       | frequent         | Fast-changing layer: price, discount, sale end. |
| `recent_refresh.py`     | `recent.json`       | rolling          | 30-day review score; offset cron for freshness. |
| `playtime_refresh.py`   | `playtime_raw/NN.json` | overnight     | Per-review playtime, sharded (**up to 24 buckets/run, oldest-scraped first** — a staleness sweep that cycles all 64 shards in ~8 h, §9); commits every 30 min + per shard. |
| `pics_refresh.py`     | `pics_raw/` (64 shards)  | daily, time-budgeted | Anonymous Steam CM (PICS) session, NOT storefront HTTP; separate rate surface. Reads appids from `games.json`, `--stale-days` incremental drain, checkpoint-commits every 15 min. |
| `pics_summarize.py`   | `pics/` (64 shards)      | after refresh     | Derives frontend view from `pics_raw/`; stores IDs (decode via lookup maps). |

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
raw scrape. Folding them into `playtime-raw.yml` made those standalone schedules redundant, so
**both `schedule:` triggers were removed** — the chained steps supersede them and guarantee the
summarize-right-after-scrape ordering the split schedule couldn't. **The two `.yml` files still
exist** as `workflow_dispatch`-only escape hatches, named `3.1 Playtime medians [2.3 / manual]`
and `3.2 Playtime-weighted ratings [2.3 / manual]` — the `[2.3 / manual]` tag marks that job 2.3
is now their sole *scheduled* trigger. (An earlier revision of this paragraph said the two
workflows had been deleted; they were cron-stripped and renamed, not removed — see §16.)

**Generated-doc workflows** (recompute a Markdown file from the live data; no Steam calls,
each single-writes its `.md`):

| Workflow file       | Script            | Writes        | Trigger                                          | Concurrency group |
|---------------------|-------------------|---------------|--------------------------------------------------|-------------------|
| `shard-health.yml`  | `shard_health.py` | `SHARDS.md`   | `35 6 * * *` (daily)                             | `shards-md`       |
| `coverage.yml`      | `coverage.py`     | `COVERAGE.md` | `workflow_run` after **scrape** succeeds (~4×/day) | `coverage-md`     |

`coverage.py` recomputes every coverage figure from the live files + shards and self-commits
`COVERAGE.md`. It reports **two axes** (full design in §11.5): **Axis 1 — total coverage**
(per-metric covered/%/missing sorted by % descending, the on-sale count, addressable-set
framing) and **Axis 2 — refresh schedule** (covered rows bucketed by each scraper's own
cooldown gate: active-vs-dormant track, overdue backlog, correctly-skipped `empty`, and the
`never`-seen fill frontier). It uses only the standard library — no `pip install` step. It's
triggered off the **scrape** completion (via `workflow_run` keyed to that workflow's exact
name) so the snapshot always reflects the freshest `games.json`, the base everything is
measured against; it also has `workflow_dispatch` for manual runs. The per-row Axis-2 pass
reads all `playtime_raw/` + `updates_raw/` shards, so a run takes ~1 min — fine for a
background job with no user-facing path.

> **Fixed (was silently broken since the workflow rename).** `coverage.yml`'s `workflow_run`
> trigger named the workflow `"Scrape Steam -> games.json"`, but the "Workflow renumbering"
> change below renamed `scrape.yml` to `"1. Steam game catalog -> games.json"`. GitHub's
> `workflow_run` trigger matches on the upstream workflow's exact current `name:` string, so
> that link was severed — `coverage.yml` stopped firing automatically after a scrape (manual
> `workflow_dispatch` still worked, masking the gap). Caught during a documentation audit and
> fixed by updating the `workflows:` array to the new name (2026-07).
`COVERAGE.md` is now a generated artifact — previously it was hand-authored and silently drifted
as the data jobs kept running.

**One-off (deletable) workflows.** `cleanup_shells.py` is a run-once utility that shares its
target file's concurrency group so it can't clobber an in-progress scrape. `queue_null_updates.py`
is the current live one-off — it force-queues every null-`last_update_ts` game for re-scrape after
the News-API fix (§16); deletable once the queue drains. The earlier HLTB-estimation backfill
(`backfill-hltb.yml` / `hltb_backfill.py`) and `backfill_updates.py` were also run-once utilities
of this kind; they have since been **run and removed**, which is the intended lifecycle for these
once their one-time job is done. The three `[DELETE]`-tagged IGDB workflows followed the same
lifecycle in Jul 2026 — tagged when Phase C was retired, then actually deleted (§8.1, §16).
**The tag is a standing instruction, not decoration: a `[DELETE]` workflow is meant to go.**

---

## 5. Data files & schemas

All frontend files are compact JSON (minified, `ensure_ascii=False`). Lean summaries use
**positional arrays** with a `_format` key in the meta so the frontend can read by index.
Nearly every file carries a `generated_at` + `count` envelope around its payload key; the
shapes below are verified against the live files (2026-07-22).

**`games.json`** — `{ generated_at, count, "games": [ { appid, title, url, release_date,
release_ts, rating_pct, review_count, tags, last_update_ts, scraped_at, is_free,
price_initial, price_final, discount_pct } ] }`. The catalog the frontend iterates, and the
**only array-shaped** data file (everything else is keyed by appid). Note it carries its own
price snapshot from the initial scrape: `prices.json` overrides it, but a game scraped before
the price job reaches it still renders a price. `scraped_at` is the staleness key every
refresh trigger in §6 compares against, and `is_free` lives **here**, not in `prices.json`.

**`catalog.json`** — the scraper's own state; not read by the frontend. Seven keys:
`last_sync` (enumeration watermark), `skipped` (permanent — confirmed non-games), `pending`
(`{appid: release_ts|null}` — the unreleased waiting room), `priority` (rebuilt each run from
the active seeds, §6), `force_refresh` (the drained-first forced queue), `seeds_ledger`
(`{seed_key: {kind, resolved_ts, ids, forced_applied}}`), and **`recheck`** (`{appid:
strikes}` — apps whose `appdetails` returned `success:false`, retried across runs until they
resolve or hit `MAX_RECHECK`, then moved to `skipped`; §3.1). Ints in memory, string keys on
disk.

**`prices.json`** — `{ generated_at, country, count, "prices": { "<appid>": { price_initial,
price_final, discount_pct, discount_end, scraped_at } } }`. The fast-changing layer;
`discount_end` drives the live countdown. There is **no `is_free` here** — free games are
simply absent (the job tracks only the non-free base) and the flag lives in `games.json`.
Because `price_and_sale.py` rebuilds the whole set each run rather than patching it, a
mid-run checkpoint legitimately holds a **partial** set — a count well below the ~106k
non-free base is a pass in progress, not data loss.

**`hltb.json`** — `{ generated_at, count, "hltb": { "<appid>": { main, extra, complete, avg,
match, fetched_at, raw: { main, extra, complete }, est?: ["extra", …], attempts?: N } } }`.
The four time fields are **unprefixed** (`main`, not `hltb_main`). `raw` holds the ground-truth
values as returned by HLTB; the top-level values may include estimates filled from the typical
main/extras/completionist ratio (§8 — corpus-wide, magnitude-bucketed, not genre-based despite
the name this feature used to go by). Three keys are conditional or easy to miss:
- **`est`** (present on 18,829 entries) lists which of the three legs were estimated — this is
  what drives the blue flag. It is `est`, not `hltb_est`; reading the wrong name is what
  produced an old "0 estimated" claim in COVERAGE.md (§8).
- **`attempts`** (present on 90,704 entries — every blank) is the miss counter that drives
  Phase B's attempt-scaled blank-retry curve (§8.1). Incremented on a miss, cleared on a match.
- **`match`** is the HLTB title actually matched (or `null`), useful for auditing a bad match.

`fetched_at` drives the priority re-scrape ordering.

**`tags.json`** — `{ generated_at, count, "tags": { "<appid>": ["Roguelike", …] },
"store_checked": [appid, …] }`. SteamSpy user tags. `store_checked` is the ledger of appids
already tried against the Steam store-page tag fallback, so a game that SteamSpy can't serve
isn't re-fetched from the store every run. PICS `store_tags` is now the frontend's primary tag
source with this file as the coverage fallback (§9.6), and Steam genres behind that, so the
column is never blank.

**`recent.json`** — `{ generated_at, window_days, count, "recent": { "<appid>": { recent_pct,
recent_count, recent_scraped_at } } }`. The 30-day score; staleness gates the trend arrow.

**`playtime_raw/NN.json`** (64 shards) — the big working set, owned by `playtime_refresh.py`.
Each shard: `{ generated_at, per_game_cap, "bucket": N, "nshards": 64, "shard_ver": V, count,
"games": { "<appid>": { "reviews":
{ "<recommendationid>": { playtime, voted_up, … } }, "summary": {…} } } }`, holding only games
where `(appid // 10) % 64 == N`. Reviews are keyed by **`recommendationid`** (identity, not
cursor position) so re-runs catch new reviews and updated playtimes without duplication. Kept
in-repo (not gitignored) because it *is* the scraper's resumable state; split across shards
because one file would blow past GitHub's 100 MB limit (§9).

**`playtime.json`** — lean frontend summary, owned by `playtime_summarize.py`:
```
{ "generated_at", "per_game_cap", "min_segment", "count",
  "_format": ["median_up_min", "median_down_min", "n_up", "n_down"],
  "playtime": { "<appid>": [ median_up_min, median_down_min, n_up, n_down ] } }
```
`min_segment` (3) is echoed from the summarizer so the threshold below is self-describing.
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

**`updates_raw/NN.json`** (64 shards) — owned by `updates_refresh.py`:
`{ "bucket": N, "nshards": 64, "shard_ver": 1, generated_at, "games": { "<appid>": {
"events": { "<gid>": { ts, type, et } }, scraped_at } } }`. `type` is the resolved tier
(`major`/`regular`/`minor`), `et` the raw Steam `event_type` int it came from (§9.5).
Note the `shard_ver` here is versioned **independently** of playtime's.

**`updates.json`** — lean frontend summary, owned by `updates_summarize.py`:
```
{ "generated_at", "windows": [30,90,180,365], "tiers": ["major","regular","minor"],
  "games": { "<appid>": {
    last_major_ts, last_regular_ts, last_minor_ts, last_any_ts,
    "counts": { "30d"|"90d"|"180d"|"365d"|"over365": {major, regular, minor} },
    "dates":  { major: [ts,…], regular: […], minor: […] } } } }
```
The `dates` arrays are capped at `DATES_CAP` (60) per tier and exist so the **frontend
recomputes windows against the live clock** — stored counts would rot daily (§9.5).

**`pics_raw/shard_NN.json`** (64 shards) — `{ "_schema": "pics_raw_v2", "_shard": N,
"_updated", "apps": { "<appid>": { …trimmed PICS `common` block…, "_ts": fetched_at } } }`.
Source of truth; `_ts` is the per-game staleness key `--stale-days` compares against.

**`pics/shard_NN.json`** (64 shards) — `{ "_format": "pics_v2", "_shard": N, "_doc": {…},
"apps": {…} }`. The summarized projection; `_doc` is a self-describing field glossary.
Note both PICS shard sets use `shard_NN.json`, **not** the bare `NN.json` of the other two.

**`pics.json`** — `{ "_format": "pics_v2_frontend", "count", "apps": { "<appid>": {…} } }`.
The merged browser file, restricted to `pics_merge.py`'s `FRONTEND_KEYS` (§9.6). Read by
`index.html` and — read-only — by `scraper.py` for the review-drift trigger (§6).

**`lookups/{tags,genres,categories}.json`** — small static ID→name maps for decoding the PICS
IDs client-side. Committed, refreshed manually via `pics_lookups.py` / `build_category_map.py`.

---

## 6. The main scraper (`scraper.py` → `games.json`)

The only job that discovers new games. Each run:

1. **Enumerate the catalog** via Steam's `IStoreService/GetAppList` (needs the free
   `STEAM_API_KEY`): games-only, appid-ordered, each with a `last_modified` timestamp.
   Without a key it falls back to the keyless `ISteamApps/GetAppList/v2`, which lists all
   app types and has no timestamps.
2. **Refresh due games first** — see "Refresh triggers" below. Then **scrape new games**,
   `NEW_ORDER` (`"newest"` by default) first. New coverage is no longer strictly last in
   line: it holds a `NEW_RESERVE_FRAC` (25%) share of each run's pops, so a large refresh
   queue can't delay a just-released game.
3. **Only store released games.** Unreleased ones wait in `catalog["pending"]` and are
   promoted the instant their release date passes (`cleanup_shells.py` files stray "empty
   shell" entries back into that room).
4. **Run for `RUN_MINUTES`, commit every ~`CHECKPOINT_SECONDS`.** The 6-hour Actions wall
   is therefore never a data-loss risk.

### Refresh triggers (what makes a stored game due)

Originally there was exactly one: Steam's `last_modified` from `GetAppList` moving past
`scraped_at`. That signal tracks **store/depot changes only — it does not move when review
counts do**, and because `REFRESH_DAYS` is a no-API-key fallback it never fires in Actions.
The result was that a game scraped on release day froze at its day-one review score
*forever*: Assassin's Creed Black Flag Resynced showed 49% / 2,019 reviews for 13 days
while the real figure moved to 79% / 19k. Systemic, not a one-off — games released in the
prior 30 days had a **median scrape age of 13 days**. Three triggers now feed one queue:

| # | Trigger | What it catches |
|---|---|---|
| 1 | `last_modified` moved past `scraped_at` | store/depot edits (the original signal) |
| 2 | **`REVIEW_TIERS`** — per-game cooldown widening with age since release | review score/count drift on everything under a year old |
| 3 | **`PICS_REV_DELTA`** — `pics.json`'s `rev` % disagrees with stored `rating_pct` by ≥3 pt | provably-stale scores on games of *any* age |

**`REVIEW_TIERS`** = `(max_age_days, cooldown_days)`: `≤3d→6h`, `≤10d→12h`, `≤30d→1d`,
`≤60d→2d`, `≤90d→3.5d`, `≤180d→7d`, `≤365d→15d`. Past a year, trigger 1 + 3 take over. The
shape follows where the number actually moves — a day-one score is worthless, a
six-month-old one barely drifts.

**The queue is rebuilt mid-run for the fast tiers.** `select_work()` runs *once*, at the
top of a 5.5h run on a 6h cron grid, so a game coming due 20 minutes in would otherwise
wait for the next cron — a hard ~6h floor under every cooldown, which would make the 6h and
12h tiers largely cosmetic (a nominal 6h tier delivering 6–12h intervals, averaging ~9h).
`requeue_due_young()` re-checks every stored game within `REVIEW_LIVE_MAX_AGE_DAYS` (30) of
release at each checkpoint (~10 min) and splices the newly-due ones to the **front** of the
refresh queue. That population is precomputed once (**2,421 games** at the 2026-07-22
snapshot — the ladder's first three bands), so the re-scan is free
next to re-scanning all ~124k records. `handled` makes it idempotent within a run — a game
already scraped this run is never re-queued, so it cannot loop. This is what actually buys
the near-real-time end of the ladder; without it, halving the cooldowns below 6h would
change the constant and not the behaviour.

**Trigger 3 is free.** `pics.json` already harvests `rev: [score_1_9, pct]` daily across the
whole catalog over the CM protocol (§9.6), entirely off the storefront budget, so it costs
nothing to *detect* drift; only the corrective re-scrape spends calls. At a 3 pt threshold
it flagged **1,783** games on the first pass (488 were off by >5 pt, 101 by >10 pt). It
covers the ~64% of the catalog that has `rev`, and PICS carries **no review count**, so it
complements the tiers rather than replacing them. `scraper.py` reads `pics.json` read-only —
`pics_merge.py` remains its sole writer.

**Queue order.** `select_work()` ranks every due game so the fastest-moving numbers go first
and the one-time catch-up can't starve genuine work: forced re-scrapes (reserved share) →
tiers `0-3d` / `3-10d` / `10-30d` → PICS drift → `last_modified` → tiers `30-60d` through
`180-365d`. Within a rank, oldest-scraped first. Set `REVIEW_TIER_REFRESH=0` and
`PICS_REV_DELTA=0` to restore the old `last_modified`-only behaviour exactly.

Per-game cost is ~2 storefront calls (appdetails + appreviews), which sets the pace ceiling
(§14). New games are seeded ahead of the queue via `seeds.txt` — human-edited only, the
scraper never writes to that file — but the reconciliation against it (`reconcile_seeds` in
scraper.py) is a **live, declarative diff**, not a one-shot consume:

- **Three seed kinds**, one per line: a bare numeric **appid**, a Steam store **URL**
  (`search_params` parses its query string into SteamSpy/store search params), or a plain
  **search term**. Lines starting with `#` are comments.
- **`!` prefix = force.** `!2495100` or `!survival craft` forces a one-shot re-scrape of that
  seed's already-**stored** matches (bypasses the normal `last_modified` change-detection).
  It fires once per edit, then latches so it doesn't loop — remove and re-add the `!` to force
  again. Under the hood this pushes appids into `catalog["force_refresh"]`, the **same** queue
  `queue_null_updates.py` (§16) uses; `select_work()` drains it ahead of the normal refresh
  queue every run.
- **Every run reconciles the full seed list from scratch** against `catalog["seeds_ledger"]`
  (`{seed_key: {kind, resolved_ts, ids, forced_applied}}`), rather than a one-time consume:
  removing a line **"forgets"** it (the ledger entry is dropped so `catalog.json` stays clean;
  already-scraped games are **never** deleted from `games.json`), and `catalog["priority"]` is
  fully **rebuilt** each run as the union of every currently-active seed's resolved ids — a
  stale `priority` value left over from a since-removed seed can never linger.
  Bare-appid seeds cost zero network to resolve; term/URL seeds are **live-re-resolved** at
  most once per `SEED_RESOLVE_TTL` (24h), so a search term keeps catching newly-released
  matching games over time instead of freezing at its first-seen result set.
- **Picked up mid-run, not just at start.** A running scrape re-fetches `origin/main`'s
  `seeds.txt` (`fetch_origin_seeds`, via `git show origin/main:seeds.txt` — it never reads the
  local working copy for this) and re-reconciles at every checkpoint (~`CHECKPOINT_SECONDS`,
  ~10 min), so an edit lands within the *current* run rather than waiting up to ~6h for the
  next one. Manually dispatching the scrape workflow is only needed for instant pickup.
- **The release gate is absolute.** Seed priority only changes *order* — an unreleased /
  coming-soon match is never stored early; it still waits in `catalog["pending"]` (§6 step 3)
  and is scraped the moment its release date passes. Seeds cannot pull shell entries forward.
- **`seeds_log.txt`** (git-committed, scraper-owned, append-only) logs every add / remove /
  re-resolve / force event in human-readable form (`seed_log()`), giving an audit trail of
  what the seed list has done over time independent of `git log` on `seeds.txt` itself.

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

**The estimation model.** `hltb_estimate.py` fills the missing legs from the **typical ratio**
between the three times — computed **corpus-wide across every game with real data, not
per-genre** (an earlier informal name for this, "genre-average ratio," still surfaces
elsewhere in older prose/comments; there is no genre grouping anywhere in the code). Across
all games with a full real triple, the **median**
ratio is ~`1 : 1.39 : 2.19` (the **mean**, ~`1 : 1.86 : 4.21`, is skewed by grind-heavy
outliers — median is the right central tendency here; the mean figure is a one-off historical
observation, not a value stored anywhere in code). The ratio is computed **live** from the
current data (needs `MIN_TRIPLES_FOR_LIVE = 30` real triples before it's trusted; below that
it uses the frozen `FALLBACK` constants — the median over the 327 real triples on hand when
the model was written), and estimates anchor on whatever real value exists, routing through
the *nearest* real neighbor first (main↔extra and extra↔complete are adjacent and more
reliable than jumping straight from main to completionist).

**Magnitude-bucketed ratios (refinement on top of the flat model).** A single flat ratio
applied linearly over-inflates the extremes — e.g. `main/complete` is empirically ~0.60 for
short games but ~0.12 for grind/idle games, so a flat ~0.46 would turn a 1200h completionist
entry into a ~549h estimated main-story time against an empirical ~142h. To fix this, each of
the six ratio directions is **bucketed by the anchor value's own magnitude** (`C_EDGES = [10,
30, 80, 200]` for a real-`complete` anchor, `M_EDGES = [5, 15, 40, 100]` for real-`main`,
`E_EDGES = [8, 20, 50, 150]` for real-`extra` — 5 buckets each) and the **per-bucket live
median** is used instead of the flat one. A bucket needs `MIN_PER_BUCKET = 15` live samples to
be trusted; thinner buckets fall back to a frozen per-bucket constant (`FALLBACK_BUCKETS`,
same cold-start philosophy as `FALLBACK`), then to the flat ratio as a last resort. Validated
on held-out data, this cuts median estimate error on grind games (`complete > 200h`) from
**~320% (flat) to ~58% (bucketed)**. `_ratio()` always prefers the bucketed value when present;
the flat model remains as the fallback chain's base case, so the two are not competing
implementations — the bucketed model is strictly additive precision on top of it.

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
utilities and have **since been removed** (§4). `fetched_at` on every entry drives a live
priority re-scrape whose order is **partial entries first** (≥1 real value, re-checked every
`RESCRAPE_PARTIAL_DAYS = 14`), **blank entries second** (governed by the attempt-scaled window
below), **full-real last** (re-checked only every `RESCRAPE_FULL_DAYS = 365`, since a complete
real triple is the least likely to have changed).

**Popularity fast lane (Jul 2026) — the windows above are a ceiling, not the rule.** Those
buckets key only on how complete *our* entry is, which assumes HLTB's data is static. That
holds for back-catalogue titles and is badly wrong for big new releases. The case that
exposed it: *Assassin's Creed Black Flag Resynced* (released Jul 2026) was first fetched days
after launch when HLTB had almost nothing, storing a lone `extra` value; HLTB has since
accumulated a full real triple (22.5 / 38.5 / 66.5) while our entry sat on the 14-day partial
window — and worse, the moment a re-scrape completes that triple the entry graduates to `full`
and freezes for a **year**. The games most likely to be gaining data were the ones checked
least often.

The fix adds a review-count tier that is min()'d against the bucket window, so it can only
ever pull a re-scrape *forward*, never delay one:

| Steam `review_count` | window | applies to |
|---|---:|---|
| > 1000 | 5 d | **all buckets** incl. `full` |
| > 500  | 10 d | **all buckets** incl. `full` |
| ≤ 500  | bucket default | partial 14 d / blank curve / full 365 d |

Overriding `full` is the load-bearing part: leaving it at 365 d would let the Black Flag case
recur indefinitely. Steam `review_count` is the proxy — both it and HLTB submissions are driven
by the same player population, and it's already in `games.json` at zero cost. Ordering within
each bucket is now **most-reviewed first, then oldest `fetched_at`**.

A second, stronger signal rides along free: **`count_comp`**, HLTB's own completion-submission
count (the "N Beat" figure on a game page). `howlongtobeatpy` doesn't map it, so it's read
defensively out of the untyped `json_content` payload and stored as `n_comp`; growth of
`COMP_GROWTH_MIN = 3` or more since the last fetch sets `comp_grew`, pulling the next re-scrape
to `COMP_GROWTH_DAYS = 5`. Every access is guarded — a missing or renamed field degrades
silently to the review-count ladder rather than erroring. *Note: HLTB's page-visible `Updated:`
timestamp was evaluated as the ideal signal and rejected — it is rendered only on the HTML
detail page and absent from the search API this pipeline uses; fetching it would mean a second
request per game.*

**Fixed data-loss edge case (2026-07).** Re-scraping a partial or full entry used to carry a
sharp edge: `store_entry` built each entry fresh from the current fetch via
`HE.make_entry(...)` with **no merge** against the prior entry's `raw`. `hltb_for` can
legitimately return an all-blank (no-match) result on a re-scrape — not just on a first
attempt — if every title-variant query comes back a clean miss that run. That used to let a
blank result **overwrite** a previously partial-or-full entry's real `raw` data, contradicting
the function's own inline comment ("a blank never wipes existing real data"). `store_entry` now
guards this explicitly: a fully-blank fetch result over an entry with any existing real `raw`
value is discarded (only `fetched_at` is restamped, so the entry isn't immediately re-queued as
stale) rather than replacing the entry. Covered by a new `hltb_selfcheck.py` regression case.

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
blanks — skipping the frozen tier when `IDLE_DRAIN_SKIP_FROZEN = True`, capped at
`IDLE_DRAIN_MAX = 4000` drained blanks per run — so the job never quits early with budget on
the clock, converging because each drained blank's `attempts` rises and backs its window off.
Net effect:
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

**Current dormant state (workflows deleted 2026-07).** All three `[DELETE]`-tagged IGDB
workflows — `igdb.yml`, `igdb-wipe.yml`, `igdb-probe.yml` — have now been **removed from
`.github/workflows/`**, so IGDB has no entry point at all: not scheduled, not
manually-dispatchable. They are recoverable from git history if ever needed. The frontend merge
was **reverted** — `index.html` no longer fetches `hltb_igdb.json` and is clean HLTB-only. The
Phase C self-check (`check_phase_c`) was **removed** from `hltb_selfcheck.py`.

**Still in-repo but now fully orphaned** (no workflow, no import, nothing reads them):
`hltb_igdb.py`, `hltb_igdb.json` (~34 MB of stale blank-heavy data), `igdb_wipe.py`, and the
diagnostics `igdb_probe.py` / `igdb_probe2.py` (probe2's TEST 5 is what measured the
8,829-record ceiling). Keeping them costs a one-time ~34 MB in git history that deleting them
would **not** reclaim, so deletion is cosmetic — they are retained as the evidence trail behind
the retirement decision.

**To fully revive:** restore the `external_game_source` query (already correct in the file),
**write a new workflow file** (the old ones are gone — recover from git history or start
fresh), re-add the frontend merge, and re-add the self-check.

**IGDB implementation notes (for a future revival).** `hltb_igdb.py` batches appid lookups at
`BATCH = 200` per `external_games` query, paced at `IGDB_DELAY = 0.30`s. It reuses
`hltb_estimate`'s bucketed model for `est`/blue-flag treatment, but only ever populates `main`
and `complete` from IGDB's `game_time_to_beats` table — `extra` (main+extras) has no IGDB
equivalent, so it's **always** left for the shared estimator to fill, never sourced directly.
`main` itself has a quiet fallback: `times_from_ttb` uses IGDB's "normally" completion time
when present, but silently substitutes the much-shorter "hastily" (speed-run) time when
"normally" is absent — worth knowing before trusting an IGDB-sourced `main` figure at face
value. `igdb_wipe.py` (formerly manual-dispatch-only via the typed-confirmation-gated
`igdb-wipe.yml`, now deleted) resets
`hltb_igdb.json` to an empty `{"igdb": {}}`; it exists because the original deprecated-filter
bug (`category = 1`) poisoned ~110k entries with fresh-timestamp blanks the worklist would
otherwise have skipped for 90 days, and a clean wipe was the only way to give the fixed query a
fair baseline.

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

**Raw scraper.** Pulls reviews via Steam's `appreviews` and stores `playtime_forever`
(**minutes**, Steam's native unit — converted to hours only at display time) per review.
**`playtime_at_review` is deliberately not used** (decided and re-decided).
Reviews are keyed by **`recommendationid`**, not cursor offset — cursor positions shift as
new reviews arrive, so identity keying is what makes resume correct under both new-review
arrival and playtime drift. It resumes each game by review identity, catching new reviews
and walking deeper into unseen ones. Because a single end-of-run commit would make a long
scrape fragile to runner interruption, it commits **every 30 minutes plus on graceful
shutdown**.

**Sharded storage (the 100 MB wall).** The per-review set is too big for one file: at the
~10.4 KB/game it actually measures (`SHARDS.md`) the full addressable set is ~820 MB, and
GitHub **hard-rejects any single file over 100 MB**. A monolithic `playtime_raw.json` hit that ceiling at ~6,850 games (98 MB) —
every push was rejected, and because the old commit code swallowed the error the runs went
*green with nothing committed*, silently freezing the pipeline for ~2 days. So the raw set is
split into **64 shards under `playtime_raw/NN.json`**, keyed by
`shard_of(appid) = (appid // 10) % 64`. The `// 10` is load-bearing: Steam appids are ~100%
multiples of 10, so a plain `appid % 64` piles every game into the *even* buckets (odd buckets
get ~nothing) — dividing by 10 first strips that factor and spreads them evenly (measured
max/mean 1.06). Each run processes **one bucket**, chosen by `GITHUB_RUN_NUMBER % 64`,
so it loads/commits only ~12 MB (measured median; max 13.27 MB — see `SHARDS.md`) and rotates
through all 64 buckets over ~64 runs.
`ensure_sharding()` is idempotent and version-gated by `SHARD_KEY_VER`: on the first run it
splits the legacy monolith and `git rm`s it; if `shard_of()` ever changes it reshards in place
(a plain file upload is all it takes); otherwise it's a fast no-op.

**Multi-shard staleness-sweep scheduling (Jul 2026) — supersedes hot-first, which superseded
one-bucket-per-run.** Processing exactly one bucket per run capped *any* game's refresh cadence
at once per 64 runs — ~8 days at 8 runs/day. The first fix (work many shards, chosen
**hottest-first** by due-game count) unblocked the fast-lane tiers but **traded one starvation
for another**: a stable hot core of ~12 shards won the slots every run and stayed fresh (~0.1 d),
while every shard *not* in that core fell back to the once-per-run anchor — right back to the
~8-day tail. Measured on `main`: 23 of 64 shards >5 days stale, worst 8.4 days. That tail is
exactly where *Assassin's Creed Black Flag Resynced* (shard 27) sat un-refreshed for 8 days
despite being the single most-overdue hot game in the catalog — because ranking was by a shard's
*total* due-count and one blazing game can't lift a shard whose total is below the core's.

The constraint was never *one shard per run*; it is **one writer per file**, and that still
holds exactly: the `steam-playtime-raw` concurrency group guarantees no two raw runs overlap, so
a single run can open, mutate and commit as many shards as its budget allows with no other job
ever touching them. A run now works up to `MAX_SHARDS_PER_RUN` (**24**) buckets:

1. **Schedule (no shard bodies read).** The whole catalog is scored against the ladder from
   `games.json` alone — which already carries release date, `review_count` and `last_update_ts`
   — and due games are grouped by shard. Reading no bodies is the point: choosing among 64 ×
   ~18 MB files must not cost ~1.1 GB of I/O. This over-counts slightly (a game whose stored
   record is already fresh still scores as due), which is fine — it only affects *which shards
   have work*, not the sweep order.
2. **Select — oldest-scraped first (the sweep).** Buckets are ordered by their stored
   `generated_at` (read from a ~200-byte file *header*, never the body), oldest first, so every
   run drains whatever has waited longest. This bounds the full cycle to
   `ceil(NSHARDS / (shards_per_run × runs_per_day))` — every shard swept on a predictable
   schedule instead of a hot core hogging the budget. Ties break by due-count (clear the most
   backlog per open). The `GITHUB_RUN_NUMBER % 64` anchor is kept only as a **floor** for the
   degenerate case where headers are unreadable; `FORCE_SHARDS` pins specific buckets on demand.
   **Per-game priority is not lost — it moved to the layer where it can't starve anyone:** the
   overdue-ratio ladder still orders games *within* a shard, so hot games are served first once
   their shard opens; the sweep only decides *which shards* open, and guarantees none is skipped.
3. **Execute.** Shards are loaded, worked and committed **one at a time**, so peak memory stays
   at ~one shard and an interrupted run has already persisted every completed shard. The exact
   eligibility gate is re-applied per shard once its bodies are in hand.

The cap is 24, not 12, because it was free: measured runs cleared 12 shards in ~23 of their
180-minute budget — 87% idle. At 24 shards × 8 runs/day = 192 shard-visits/day against 64 shards,
a full sweep of *every* shard completes in **~8 h worst-case**, versus the old ~8-day tail, with
the time budget still far from binding (raise the cap further for a faster cycle).

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
its review count crosses 10. Measured at the 2026-07-22 snapshot: the floor removes **45,312**
games (~36% of the 124,210 catalog), leaving an addressable set of **78,898** — of which raw
playtime already covers **78,796 (99.9% of addressable)**. The backfill is effectively done; the
job's remaining work is re-walking covered games on their two-track cooldown, not filling.

**Refresh ladder (Jul 2026) — replaces the flat 7 d / 30 d cooldown.** The old gate treated a
game released yesterday the same as one from 2019, but review playtime moves fastest exactly
where that gate was slowest: a new release accumulates its whole review corpus in the first
days, and each reviewer's `playtime_forever` is still climbing. Waiting 7 days there meant the
medians shown for the most-searched games on the site were the stalest data held. The cooldown
now scales with **release age**, deliberately coarser than the 7-tier review ladder (§6) because
playtime costs ~2–20 requests/game versus 2 for a review refresh:

| release age | cooldown | with > 1000 reviews | after popularity floor |
|---|---:|---:|---:|
| 0–7 d   | 1 d  | 12 h (floor) | 12 h |
| 7–30 d  | 3 d  | 1.5 d | 1.5 d |
| 30–90 d | 7 d  | 3.5 d | 3.5 d |
| older   | 30 d | 15 d | **5 d** |

The **review-count boost** halves every tier for games over `HOT_REVIEWS_BOOST = 1000` all-time
reviews — the games with both the most churn and the most site traffic — keeping a popular
perennial fresher than a dead new release. `MIN_COOLDOWN_HOURS = 12` floors the whole ladder so
nothing is re-walked twice a day. Games with **no parsed release date** fall back to the legacy
`last_update_ts` behaviour (7 d if patched within 90 d, else 30 d): unknown age is treated as the
conservative case, never as brand-new.

**Popularity floor — HLTB alignment (Jul 2026).** The halving alone still left the worst case
un-fixed: a popular perennial (>1k reviews, years old) sat on the *older* tier at 15 d even after
halving — while the HLTB re-scraper (§8) re-checks those exact games every **5 d**. Both signals
ride the same live player population, so that split made no sense: if HLTB submissions are worth a
5-day look, the reviewers' `playtime_forever` on the same title is churning just as fast. It was
the same *"most-viewed games refreshed least often"* anti-pattern §8's fast lane was built to
kill, quietly re-inherited on playtime's slow tiers. So each game's cooldown is now `min()`'d
against a review-count floor identical in shape to HLTB's `POPULAR_TIERS`:

| Steam `review_count` | floor | HLTB window |
|---|---:|---:|
| > 1000 | 5 d  | 5 d |
| > 500  | 10 d | 10 d |
| ≤ 500  | — (age ladder unchanged) | 365 d full / 14 d partial |

`min()` semantics mean the floor can only ever pull a refresh **forward**, never delay one, so it
bites only where the age ladder is too slow for a high-traffic game (the *30–90 d* and *older*
tiers) and leaves the aggressive fresh-release fast lane (12 h – 1.5 d) untouched. It applies on
the legacy no-release-date path too, rescuing a popular game with no parsed release from the 30-day
dormant cooldown. Net effect is a **redistribution** of the fixed request budget toward hot
back-catalogue games (the overdue-ratio ordering keeps genuinely-new releases ahead of them), not
an increase — mirroring §8's own "work redistributed, not increased" property.

Within a shard, ordering is driven by **overdue ratio** (`age ÷ own cooldown`) rather than raw
age, which makes the ladder self-balancing — a 1-day-cooldown release 2 days stale outranks a
30-day-cooldown title 40 days stale, because it's proportionally further behind the promise the
ladder makes about it. Raw age would invert that and let ancient dormant games crowd out the
fast lane permanently.

**Depth ladder (`DEPTH_LADDER = 1000 → 2000 → 3000`).** How many reviews we keep per game is a
**rung**, not a flat cap. `cap_for(held)` returns the first rung strictly above what a game
already holds, so:

| visit | rung | what happens |
|---|---:|---|
| 1st touch | 1,000 | fills and **releases** the game — the fill frontier keeps draining fast |
| 2nd | 2,000 | walks ~10 pages deeper |
| 3rd | 3,000 | reaches the ceiling |
| 4th+ | 3,000 | stops on `SEEN_STREAK_STOP` after absorbing new reviews — back to today's cost |

**It piggybacks on the existing cooldown — no extra visits.** Deepening deliberately does *not*
feed `is_eligible()`: `held < cap_for(held)` is true by construction, so using it there would
mark every game permanently due. A game climbs only when it comes round on its normal
refresh cadence, which means the full climb completes in ~3 weeks by itself. The ladder needs
**no new per-game state** either — `len(reviews)` *is* the rung pointer.

*Why the `len >= target` guard is load-bearing:* on a re-visit the first ~10 pages are all
already-known reviews, so `seen_streak` hits 50 almost immediately. Without that guard the walk
would stop there and a game could never climb past rung 1.

**Why deepen at all — it is a noise fix, not a bias fix.** Measured against 779 games holding
their *full* uncapped history, the newest-N sample is close to unbiased (median 1.03× vs the
true median). What shallow sampling costs is **per-game stability**:

| sample depth | median vs true | games >10% off | >25% off |
|---|---|---:|---:|
| newest 200 | 1.035× | **51.1%** | 21.2% |
| newest 400 | 1.037× | 39.5% | 13.1% |
| newest 600 | 1.027× | 24.0% | 5.6% |

The sharpest win is the **minority sentiment side**, which on capped games has a median of just
**158 reviews** (61% under 200, 27% under 100). That thin side is exactly what the inversion
signal — "played it a long time, still says skip it" — is computed from, so it is the number
that most deserves more samples.

**Sizing (measured 2026-07).** Only **8,386 games (10.6% of coverage)** hold >1000 reviews, so
the ladder touches a small, high-value slice. At ~**50 B/review** the ceiling costs
**+7.8 MB/shard → ~21 MB max**, nowhere near GitHub's 100 MB per-file limit. The one-time climb
is ~**44 h** of scrape time, which spread across the 7-day cooldown lands in ~3 weeks at ~10%
of the daily budget.

> **The binding cost is git growth, not file size.** Shards are rewritten whole on every commit,
> so raw storage roughly doubles (777 MB → ~1.3 GB) and every shard write pushes a full copy into
> history. **3000 was chosen over 5000 for exactly this reason** — ~70% of the benefit for ~60%
> of the bytes. Secondary: both summarizers read all 64 shards on every raw pass (~8×/day), so
> their parse time roughly doubles too.

**Ceiling staleness — the periodic full re-walk.** A game pinned at the 3,000 ceiling otherwise
refreshes only its **top ~100 playtimes per visit** (the walk breaks as soon as `len >= target`),
so positions 100–3,000 **freeze** — and since `playtime_forever` keeps growing, a long-tenured
game would report ever-staler playtimes. Two triggers force a **deep re-walk** (every held
playtime refreshed + all new reviews caught up), whichever fires first:

- **Time backstop — `REWALK_DAYS` (30).** The load-bearing one, because playtime staleness is
  **clock-driven, not review-count-driven**: a beloved back-catalogue game earning ~50 reviews a
  year never trips a churn threshold, yet its reviewers keep playing. Measured against the last
  *full* walk (`walk_at`), not the last visit — shallow top-ups don't reset it, so the countdown
  actually elapses.
- **Churn accelerator — `REWALK_DELTA` (1000).** Brings the deep walk *forward* for a trending
  game whose review count grew by ≥1,000 since its last full walk, catching it sooner than 30
  days. Floored at `REWALK_MIN_DAYS` (7) so a mega-game earning thousands of reviews a week can't
  thrash a deep pass every visit — its newest-3,000 window is the freshest slice already and does
  not need constant deep passes.

**It is spread per game, never a synchronized sweep.** Each game carries its own `walk_at` /
`rc_at_walk` anchors, stamped when it last did a full walk — which for most games is the moment
the **depth ladder first filled them**, an event already staggered across the shard rotation. So
due-dates scatter across the calendar; there is no once-a-month batch. It also adds **no
visits** — a ceiling game is already visited every cooldown to catch new reviews, and this
merely makes roughly every 30-days-worth of those visits a deep one. **Cold start** (a ceiling
game with no anchor yet) only *initializes* the clocks — no forced walk — so the first backstops
land ~30 days out rather than all at once on the deploy. The deep walk stops at exactly the
window depth (`target // PER_PAGE` pages) and no deeper: walking past it would start adding
reviews *older* than the window and evict just-refreshed recent ones, drifting the sample
backwards.

*Cost:* ~4,228 games sit at the ceiling; each deep pass is ~30 pages (~45 s), and at a 30-day
cadence that is **~2 h/day (~8% of the playtime budget)** — affordable because the backfill
frontier is essentially drained. Set `REWALK_DAYS=0` **or** `REWALK_DELTA=0` to disable a
trigger; both off restores the pure top-100 behaviour.

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

## 9.5 Update-events pipeline (`updates_refresh.py` + `updates_summarize.py` → `updates.json`)

Surfaces **how often and how substantially a game is patched** — an ongoing-support signal
for the value hunter (a game still getting major updates is a different buy than an abandoned
one). Answers "how many big vs small updates in the last month / 3mo / 6mo / year / over a
year", per game.

**Why this is separate from `last_update_ts`.** `scraper.py` already fills a single
`last_update_ts` in `games.json` from the **News API** (`ISteamNews/GetNewsForApp`). That is
cheap (huge `api.steampowered.com` budget, no storefront contention) but it is ONE timestamp
with **no magnitude** — the News feed doesn't expose an update's size. The **store events**
endpoint does, via a native `event_type`, so the big/small split lives here, in its own job.

**The magnitude signal (`event_type`).** `store.steampowered.com/events/ajaxgetpartnereventspageable/`
(`clan_accountid=0&appid=N&offset=0&count=50&l=english`) returns `events[]`, each carrying an
integer `event_type`. Steam defines **three** update categories and we keep all three distinct:
`13` = Major Update → **major** ("biggest moments of the year"); `14` = Regular Update →
**regular** (a normal meaningful update — the middle tier, bigger than a patch note but not a
tentpole event); `12` = Small Update / Patch Notes → **minor** (smallest, routine). Everything
else (sales `20/21/23`, streams, cross-promo, announcements) is discarded. This is Valve's own
taxonomy, not a keyword guess. We store the true tier rather than collapse 14 into 13, so the
frontend can group major+regular into a single "big" bucket vs minor if it wants — that's a
display choice, not baked into the data, and it never requires a re-scrape to change.

**The budget trade-off.** This endpoint is on the **storefront budget** (~200/5min) shared with
scrape/prices/recent — unlike the News-API `last_update_ts`. So it is deliberately its own
**out-of-band, time-boxed, one-bucket-per-run** job (like playtime), never folded into the main
scraper, and it runs only on the **quiet cron slots** (`:53` of 2,8,14,20) at a conservative
`STORE_DELAY=1.6s` so it can't starve prices/catalog. Update history changes slowly, so 4×/day
rotation (all 64 buckets every ~16 days) is ample.

**Why raw dated events, not stored counts (the staleness fix).** "Updates in the last 30 days"
rots every single day. Storing pre-computed counts would mean a game scraped today shows stale
counts a week later, forcing constant re-scrapes to stay honest. Instead the raw `(gid → {ts,
type})` events are stored **once**; `updates_summarize.py` rolls them into windowed counts AND
ships the per-window timestamp lists (`dates`), so the **frontend recomputes the same windows
against the live clock** — counts never drift regardless of scrape age. Re-scrape is then only
ever needed to catch NEW posts, not to keep old counts current.

**Sharded from day one.** A per-game dated event list across ~120k games is exactly the shape
that blew past the 100 MB wall for playtime, so `updates_raw/NN.json` is sharded identically:
64 buckets, key `(appid // 10) % 64` (divide-by-10 first because appids are ~all multiples of
10), one bucket per run rotated by `GITHUB_RUN_NUMBER`, `shard_ver`-gated reshard hook for any
future key change. (It's far lighter than playtime — timestamps, not review objects — so the
100 MB ceiling is distant. NOTE: `shard_health.py` currently monitors only `playtime_raw/`;
pointing it at `updates_raw/` too is a cheap future add if these shards ever grow — tracked in
[ROADMAP.md](ROADMAP.md) §3.5.)

**Fixed: the reshard path didn't used to commit (2026-07).** `updates_refresh.py`'s
`ensure_sharding()` → `reshard_all()` (triggered whenever the stamped `shard_ver` doesn't match
`SHARD_KEY_VER`, currently `1`) rewrote all 64 shard files **locally on the runner** but never
pushed them — unlike `playtime_refresh.py`'s equivalent path, which does push right after
resharding. Since `SHARD_KEY_VER` had never changed since ship, this had never fired in
practice, so there was no live impact — but if it were ever bumped, a full reshard would have
happened on an ephemeral GitHub Actions runner and then been **silently discarded** except for
whichever single bucket that run's normal end-of-run commit happened to cover. Fixed by
extracting `_robust_commit_shards(msg, names)` (a multi-shard-capable generalization of the
existing single-shard push, mirroring `playtime_refresh.py`'s `_robust_commit`) and having
`reshard_all()` call it with all 64 shard filenames right after rewriting them; `git_commit_shard`
is now a one-line wrapper over the same helper. Verified locally against a scratch shard set
(pre-fix: reshard produced 64 files with no push call at all; post-fix: the commit path is
correctly reached and would push all 64 — it no-ops only because local runs outside Actions
skip git by design, same as every other writer in this codebase).

**Resumability / safety** mirror the playtime writer exactly: identity-keyed by event `gid`
(re-runs never double-count; a known gid is already held), **refresh-on-revisit** (a post's
type/time is updated in place if Steam changed it), a `PER_GAME_CAP=200` ring-buffer (oldest
events dropped first) bounding growth, commits **every 30 min during the run** via the same
hard-reset-to-`origin/main` single-writer push (a failed push is a **loud red run**, never a
silent green one), and a transient endpoint failure (403/blip) **skips that game and leaves its
prior record untouched** — nothing is ever blanked by an error. Eligibility is gated by
`MIN_REVIEWS_FLOOR=10` and cadence is shorter for actively-updated games (`COOLDOWN_DAYS=7` vs
`NOUPDATE_COOLDOWN_DAYS=45`), so budget flows to games whose history actually moves.

**`updates.json` shape** (small; frontend reads it): per appid `last_major_ts`,
`last_regular_ts`, `last_minor_ts`, `last_any_ts`, a `counts` snapshot (`30d`/`90d`/`180d`/
`365d`/`over365`, each `{major, regular, minor}`), and capped `dates.{major,regular,minor}`
timestamp lists for client-side window recompute. One writer per file holds:
`updates_refresh.py` owns `updates_raw/NN.json`; `updates_summarize.py` owns `updates.json`;
the raw job never writes the summary.

**Frontend integration — column shipped; precedence flip still pending.** `index.html` loads
`updates.json` and uses it two ways: (a) as a **fallback** for recency — where games.json's
News-API `last_update_ts` is null, it backfills from `last_any_ts` (max of the tier
timestamps); and (b) as the source for the **Updated column's cadence badge** — `upd_c90` /
`upd_c365` summed across tiers from the `counts` windows (§11). The standalone **sortable
Updated column** (between Released and Tags) is now shipped, sorting by update recency and
showing the cadence badge where covered.

**The precedence flip — still not done, but for a different reason than first written.** The
plan was to make the event layer primary and News-API the fallback once shard coverage caught
up. The original gate ("only 1/64 shards populated, rotation just started") **has been met and
is no longer the blocker**: as of the 2026-07-22 snapshot all **64/64 `updates_raw/` shards are
populated**, holding 78,663 games (63.3% of catalog). The real limiter is now structural:

- `updates_refresh.py` gates on `MIN_REVIEWS_FLOOR = 10`, so ~45k sub-floor games are **never**
  event-scraped. Raw coverage is therefore capped near 63% — and it has already reached that
  ceiling (78,663 scraped vs a 78,898-game addressable set).
- `updates.json` only carries games with **≥1 stored update event**, so it lands at **51,065
  games (41.1%)** — the other ~27.6k scraped games genuinely have no qualifying events.
- The News-API `last_update_ts` has **no review floor** and covers **76,666 games (61.7%)**.

So a global precedence flip would *lose* recency data on ~25k games. The event layer currently
fills only **886** of the News-API's nulls. The honest conclusion: **keep News-API primary**,
and if the flip is ever wanted it has to be **per-game** ("prefer `last_any_ts` where present,
else `last_update_ts`") rather than a blanket swap — or `updates_refresh.py`'s review floor has
to drop first. See §3.1.

---

## 9.6 PICS metadata layer (AI disclosure, tags, Deck, reviews, family-share)

A **three**-layer pipeline harvesting the Steam PICS `common` app-info block via an
anonymous CM session (`ValvePython/steam`). Full design + decision record in
`PICS_METADATA_PIPELINE.md`. All three layers run as chained steps of the one
daily `pics.yml` job.

**Layer 1 — `pics_raw/shard_NN.json` (source of truth).** `pics_refresh.py` batch-fetches
the `common` block, trims junk + nested-trims `steam_deck_compatibility` at ingest
(schema `pics_raw_v2`), and writes 64 shards keyed by the existing
`(appid // 10) % 64`. One writer per shard file. Per-game `_ts` enables
incremental refresh via `--stale-days` (the script's own default is **0** = refetch
everything; `pics.yml` passes **14**, overridable per dispatch). Time-budgeted with
periodic checkpoint flush (playtime pattern).

**Layer 2 — `pics/shard_NN.json` (summarized view).** `pics_summarize.py` projects each
game to a lean, index-friendly record (format `pics_v2`) storing **IDs, not names**
(Option B, see spec §4.5), and precomputes the three derived filter flags (`ea`,
`adult`, `vr_only`). The frontend loads three static maps once (`lookups/tags.json`,
`genres.json`, `categories.json`) and builds filter indexes from the IDs — filter on
IDs (fast set-membership), decode to names only for visible labels.

**Layer 3 — `pics.json` (the browser file).** `pics_merge.py` flattens the 64 `pics/`
shards into one file (format `pics_v2_frontend`) keeping **only the keys in its
`FRONTEND_KEYS` set**. Everything else stays backend-only in `pics/`. The 19 keys that
reach the browser are: `name`, `type`, `tags`, `genres`, `pgenre`, `cats`, `rev`,
`deck`, `ai`, `fse`, `eula`, `controller`, `state`, `released`, `mc`, `ea`, `adult`,
`vr_only`, `art`.

**Field → source → refresh.** The **Layer** column says how far each field travels:
`pics.json` = shipped to the browser; `pics/` = summarized but dropped at the merge
(backend-only); `pics_raw/` = never summarized.

| Field | Layer | From `common` key | Coverage | Meaning |
|---|---|---|---:|---|
| `tags` | `pics.json` | `store_tags` | 99.9% | ranked tag IDs (decode via `lookups/tags.json`) |
| `genres`, `pgenre` | `pics.json` | `genres`, `primary_genre` | 99.9% / 100% | genre IDs (decode via `lookups/genres.json`); **EA = genre-70** |
| `cats` | `pics.json` | `category` | 100% | feature IDs (decode via `lookups/categories.json`) — modes, controller, VR |
| `rev` | `pics.json` | `review_score`, `review_percentage` | 63.9% | `[score_1_9, pct]`. Also read by `scraper.py` as the review-drift refresh trigger (§6). |
| `deck` | `pics.json` | `steam_deck_compatibility` | 27.3% | `{cat, os, machine, tested_ts, online_solo?, hdr?}` — cat 1=Unsupported/2=Playable/3=Verified |
| `controller` | `pics.json` | `category` (28/18) | 34.2% | `full` / `partial` — 100% derivable from `cats` |
| `ai` | `pics.json` | `aicontenttype` | 10.4% | 0 none / 1 pre-generated / 2 live-generated |
| `fse` | `pics.json` | `exfgls` (presence) | 0.7% | family-share excluded |
| `eula` | `pics.json` | `eulas` (presence) | 8.9% | has custom EULA |
| `mc` | `pics.json` | `metacritic_score` | 3.3% | scalar |
| `released` | `pics.json` | `steam_release_date` | 98.7% | unix ts scalar |
| `state` | `pics.json` | `releasestate` | 98.8% | `released` / `prerelease` — live/coming-soon (**not** the EA signal) |
| `art` | `pics.json` | `header_image` | ~100% | store header path: `<sha1>/<file>` (modern) or `<file>` (legacy). Un-prefixed; `index.html` `artUrl()` picks the CDN base by whether a `/` is present. **Only authoritative art source** — appid-derived URLs 404 on the `store_item_assets` scheme. |
| **`ea`** | `pics.json` | *derived* — `genres` contains 70 | 9.5% | **Early Access** filter flag, precomputed by `pics_summarize.py` |
| **`adult`** | `pics.json` | *derived* — `content_desc` has 3 **or** 4 | 6.3% | **Adult gate** flag (blur + 18+ badge), precomputed |
| **`vr_only`** | `pics.json` | *derived* — `cats` contains 54 | 4.3% | **VR-Only** filter flag, precomputed |
| `content_desc` | `pics/` | `content_descriptors` | 22.7% | int list; 1=violence 2=gore 3=mature 4=nudity/sexual **5=container (not adult)**. Dropped at merge — the browser reads the derived `adult` flag instead. |
| `orig_released` | `pics/` | `original_release_date` | 9.0% | unix ts (EA→1.0 carriers only) |
| `rev_bomb`, `review_bombed` | `pics/` | `review_score_bombs`, `review_percentage_bombs` | 0.1% | de-bombed score; present only when divergent. **Backend only — not surfaced.** |
| `dev`/`pub` | `pics/` | `associations` | 99.9% / 99.6% | structured names. **Backend only (parked).** |
| `franchise` | `pics/` | `associations` | 23.5% | structured franchise name(s). **Backend only (parked).** |
| `langs`/`audio` | `pics/` | `supported_languages` | 99.9% / 44.0% | supported + full-audio codes. **Backend only (parked).** |

*(Coverage figures are live as of the 2026-07-22 `COVERAGE.md` snapshot, measured over all
124,120 summarized records — they supersede the 120-game probe sample in
`PICS_METADATA_PIPELINE.md §2` and are regenerated by `coverage.py` on every scrape.)*

**Lookup maps** (`pics_lookups.py` + `build_category_map.py`, refreshed rarely):
`tags.json` (live from `IStoreService/GetTagList`), `genres.json`,
`categories.json` (derived from appdetails ground truth). Committed static.

**Frontend data-model direction (decided 2026-07-16, SHIPPED 2026-07-16).**
Full record in `PICS_METADATA_PIPELINE.md §11`. Data flows from one slim merged
`pics.json` (built by `pics_merge.py`, see §9.6). Summary of what shipped:

- **Tags → PICS primary, SteamSpy supplement.** `store_tags` (ranked IDs,
  decoded to names via `lookups/tags.json`) drive the rail; the existing
  name-based tag taxonomy/canon works unchanged on top. SteamSpy `tags.json`
  kept as a coverage fallback only.
- **Modes / controller / VR → `cats` primary, not tags.** Authoritative Valve
  feature flags; controller is derived from cats 28/18, VR Only from cats 54.
- **Genres → backend only** (no rail — would duplicate the tag rail); used for
  the EA signal (genre-70) and primary-genre display.
- **Early Access → genre-70 only.** SteamSpy EA tag dropped (lingers post-launch,
  unreliable). Precomputed as the `ea` flag by the summarizer.
- **Mature/adult gate → `content_desc` codes 3+4 only** (Adult-Only Sexual /
  Frequent Nudity-Sexual), precomputed as the `adult` flag. Code 1 ("Some
  Nudity") is EXCLUDED — it over-flags mainstream titles (Witcher 3, BG3,
  Cyberpunk) exactly like the old `ADULT_TAGS` heuristic did; code 5 is a
  container marker. Replaces `ADULT_TAGS`, which is now a pre-PICS fallback only.
  New blur UX: blur → "18+?" confirm tap → reveal + open store link (table + card).
- **"Flags" cluster** (own collapsible filter-section). Six *presence* flags all
  share ONE toggle schema — **Any / Exclude / Only** (`state.flags[k]` =
  `"any"|"hide"|"only"`): Early Access · AI disclosure · Adult content · VR-only
  · Family-share block · Custom EULA. Two *graded* controls keep bespoke buttons
  because they pick among values rather than the presence of one flag:
  Controller (any/full/partial) · Steam Deck (any/verified/playable+/unsupported).
  All URL-serialized/shareable; the filters no-op unless `pics.json` actually
  carries games (HAS_PICS guard), so the empty placeholder doesn't zero the list.
  - Because the buttons are generic, **the label has to name the flag** — it is
    the only thing that says what Exclude/Only act on. Hence "Early Access", not
    "EA only". Per-field `title` tooltips carry the nuance that won't fit in a
    label — advertised by the help (`?`) cursor on the whole field (§16; the old
    dotted-underline affordance was dropped as too cluttered).
  - **`Family-share block` is labelled for the BLOCK, never inverted into a
    "Shareable" control** — PICS_METADATA_PIPELINE.md §2.4 verified `exfgls` as a
    positive exclusion signal *only*: absence does not prove shareability (PUBG,
    Destiny 2 restrict at the account layer with no flag). So Exclude means "no
    known block", and the tooltip says exactly that.
  - **URL (as built — the two lists don't cover all six).** `flags=` lists the
    `only` keys and `noflags=` the `hide` keys, but only for the **four** tokens in
    the `TRI` table: `ea`, `eula`, `fse`, `vr`. Each list sets only the keys it
    names, so the two never clobber each other, and `flags=` keeps its
    pre-tri-state meaning — old shared links still resolve. The remaining two
    presence flags get **their own params** — **`ai=hide|only`** and
    **`adult=hide|only`** — as do the two graded controls, **`ctrl=full|partial`**
    and **`deck=verified|playable|unsupported`**. All six are still one *UI*
    schema; only the serialization splits.
  - Flags is the widest cluster in the bar (8 groups / ~23 buttons), so it alone
    trims 3px of horizontal button padding to hold ONE row; it wraps to two below
    ~1870px, which is fine.
- **Rating → games.json primary, PICS `rev` validator** (>5-pt divergence flags
  staleness / review bombing).
- **Parked backend-only:** dev/publisher, franchise, languages, review-bomb
  adjusted score / review-bombing detection (not surfaced; dropped from the
  slim `pics.json` cut entirely).

**Filter-bar layout (shipped same day).** Filter sections use a compact
side-by-side layout: the section header (caret + title + active-count badge)
sits in a fixed ~132px left column, with controls flowing beside it in the
reclaimed gutter (previously the controls sat on a second row below the title).
On viewports ≤720px the layout reverts to header-above-controls. The "Quality &
Activity" section was renamed to just "Quality".

**Fold zones (superseding the header-only rule above).** Collapse originally
toggled on the header column only, which left the wide empty strips beside the
controls inert. Now the header *and* any dead space in the section's own row
fold it — those strips are the largest fold targets on the row. Controls opt out
via `FOLD_SAFE` (a selector list in the `.filter-section` click handler) with a
CSS list mirroring it: each safe island carries `padding:6px; margin:-6px`, which
grows its hit box while cancelling the layout shift, so the 12px `.ctl-row`
gutters belong entirely to the controls and a near-miss still can't fold. Tag
chips use a `::after{inset:-4px}` ring instead — their 7px spacing is too tight
for negative margins without overlapping hit boxes. The wishlist row isn't a
section (it's a global action), so its dead space folds the whole bar via the
`.collapse-handle`. A `getSelection()` guard prevents folding at the end of a
text drag.

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
where *basis* picks the **Sale** (after-discount, the default) or **Full** price, and the
selected HLTB hours follow the **HLTB metric** toggle (main — the default — / +extras / 100% /
avg). Null for free games and games with no usable HLTB value. The score is recomputed on
toggle, never stored. (Internal sort key: `qtpd`.)

**Free-only mode.** Dividing by a zero price is undefined, so free games normally show no QTPD.
But when the price-type filter is narrowed to **Free alone** (`freeMode()` — `priceClass` is
exactly `{free}`), the QTPD column switches to **`freeScore()`** = `hours × rating%` with no
price division, so free games can at least be ranked *against each other* by quality-weighted
length. `colValue()` is the single accessor the column, the sort, the value-meter and the QTPD
range slider all read, so the swap is consistent everywhere. Any other price-type selection
uses the normal price-based score.

**The table (12 columns).** In order: Game · Reviews · Trend · **Weighted** · Price / Sale ·
Sale ends · Released · **Updated** · Tags · **Playtime** · HLTB · QTPD. (**Trend** sits directly
after Reviews — it's derived from them — and **Price + Discount are merged** into one
`Price / Sale` column. The **Updated** column (2026-07) sits after Released: last-update recency
+ a patch-cadence badge, sortable by recency — see §9.5 / §3.1.)

- The table is laid out with **CSS Grid** — a shared `--grid-cols` template of `minmax()`
  tracks (one per column) applied at the **row** level, so the sticky `<thead>` stays a normal
  sticky block while each `<tr>` lays its cells on the same track template. (The `<colgroup>` is
  `display:none`; `<col>` min/max is ignored by browsers, so Grid, not `<col>`, sizes the
  columns.) Each track's `min` is a small-laptop legibility floor; the `max` is a breathing
  ceiling so the slim numeric/sort columns (Trend, Price / Sale, Weighted, Sale ends) don't bloat
  on a wide monitor. On large screens the slack concentrates on the content-heavy columns —
  **Game and Tags** — which carry `fr` ceilings; everything else stays near its natural width.
  The table's `min-width` is the **exact sum of the column minimums (1324px)** — 1240px before
  the Updated column added its 84px track (2026-07) — so below that the **page** (not the table
  card) scrolls horizontally — deliberately *not* `overflow-x:auto` on the scroll container,
  because a lone `overflow-x:auto` is promoted by browsers to `overflow:auto` on both axes, which
  would trap the sticky `<thead>` in a scroll box. Below ~1374px the table stops being a table and
  becomes the stacked **card layout** (see *Responsive* below). *(Both numbers moved by exactly
  the new column's 84px min: min-width 1240→1324, breakpoint 1290→1374, preserving the ~50px
  comfort gap between them.)*
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
- **Released** shows more than the raw date: a computed **age string** (`ageStr()`, e.g.
  "15.2 yrs old" / "N mo old") stacked with a **last-content-update recency badge**
  (`updatedStr()`, e.g. "upd 3mo" / "no upd", full date on hover) — both derived client-side
  from `release_ts` / `last_update_ts`, not separate stored fields.
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

**Grid rows stretch their cells, deliberately.** `thead tr` / `tbody tr` are grids with
`align-items:center`, which sizes each cell to its **own content** and centres it in the track —
so a cell's borders span only that content, not the row. This silently broke two things once the
collapsed Tags column added borders: every header's `border-bottom` sat at a slightly different
height (**measured 217px vs 219px** — the header rule was stepped, not straight, because the
collapsed `<>` pill is taller than plain text), and the collapsed Tags **body** seam rendered as a
**10px stub floating in a 79px row** rather than a full-height divider. `thead th` and the
collapsed Tags `<td>` therefore carry **`align-self:stretch`**, which fills the track without
changing row height (content stays centred by each cell's own flex). Any future full-height rule
on a cell needs the same.

**Collapsible Tags column (desktop table only).** The Tags header carries a `><` toggle
(`#tagsToggle`) that folds the whole column away, setting `body.tags-collapsed` and persisting
the choice in `localStorage["qtpd.tagsCollapsed"]`. Tags is the widest low-density column, so
folding it is the cheapest way to buy width for the content that benefits most: the reclaimed
space goes to **Game**, whose thumbnail grows from **150×57 to 180×68** and whose title gets
more room before truncating. The table's `min-width` drops **1324 → 1218px** to match (the
Tags track's own minimum), so the horizontal-overflow floor moves with it. Scoped
`:not(.layout-card)` throughout — the toggle is meaningless in Card and Grid, where there is no
column grid to reclaim.

**Three views, not two.** `VIEWS = ["table", "card", "grid"]`, chosen from the switcher in
`.bar-tools` and persisted in `localStorage["qtpd.view"]`. **Table and Card are the same
"detailed" view relabelled per device** — Table is desktop-only, Card is mobile-only, and
`setView()` coerces one to the other across the 1374px breakpoint (the off-device button is
dimmed). **Grid** is the third, device-independent view: a box-art grid of `gridCardHTML()`
cards (Steam header art at `aspect-ratio:460/215` over a dark info panel with title + Steam
rating on one line and QTPD below; tap to flip to a price/length/`Steam ↗` overlay). **Grid is
the default on mobile**, Table on desktop; a saved choice always wins. Full as-built record,
including the six refinement rounds, in **§3.4**.

**Responsive / card layout (single-column spec sheet, 2026-07 redesign).** The table is for the
desktop width range; a **fluid table alone cannot fit a phone** (twelve columns at legible
minimums sum to ~1324px). So below **1374px** `<thead>` hides and each row becomes a
**single-column spec-sheet card**. The card leads with a **thumbnail + title header** and the
**QTPD** value + meter as a headline row, then lays out **one metric per line** — a fixed muted
**label gutter** with the value beside it — so labels and values form two aligned columns the eye
scans straight down. Fields run in a **logical order** (name → QTPD → price → ratings → length →
release → updates → tags) driven by CSS **`order`**, decoupled from the table's column order.
Long/technical headers are **relabeled** on mobile (Reviews→**Rating**, HLTB→**Length**,
Price / Sale→**Price**), and cells whose value is only "no data" (no active sale, no weighted
rating, no trend, no playtime) are **dropped via `:has()`** so no dead "—" lines clutter the card.
A **~560px** breakpoint tightens type and spacing. *(This superseded the earlier "every `<td>` a
label→value line" card and its ~560px HLTB-priority tweak — both gone.)* The one caveat: CSS
`order` changes visual order only, so assistive-tech reads cells in table (DOM) order;
progressive disclosure of secondary fields behind a tap is the remaining open item
([ROADMAP.md](ROADMAP.md) §3.2 / §3.4).

**Sorting without a `<thead>` (superseding the "mobile `<select>`" design).** Because the
sortable header is hidden in Card and Grid, sorting needed another entry point. It was first
solved with a **native `<select>` Sort control** (`#mobileSort`) in the bar — that control is
**still in the markup but hidden on every screen size** (R6). Sorting is now reached two ways:
on mobile via the **`sorted by …` chip** on the compact filter-summary line (which opens a sort
popover), and in **desktop Grid** via its own **`#gridSort`** bar — split so `bar-main` never
reflows when switching Table↔Grid on desktop. Both are wired by `bindSortControl()`, and
`setSort()` / `syncMobileSort()` keep every entry point, the header arrow, and the URL in step.

**Filters & controls.** All of it lives in **four collapsible accordion sections** whose
open/closed state persists in `localStorage["qtpd.sections"]` (§3.4 L1). Defaults per group are
**leftmost** (repo convention, §3.4 R3), and any control moved off its default lights up **gold**
via `markChangedControls()`.

- **Value** — **QTPD price basis** (Sale *(default)* / Full) · **HLTB metric** (Main *(default)*
  / +Extras / 100% / Avg) · **HLTB data** (Real *(default)* / All incl. estimates) ·
  **price type** (All / Full / Sale / Free — an independent multi-toggle, URL `pc`; this
  **replaced the old boolean on-sale-only filter**) · min & max price · **QTPD range**
  log-slider that fits current results.
- **Quality** — min rating (any/60+/70+/80+/90+) · **Reviews sort by** (30-day *(default)* /
  All-time) · review-trend multi-toggle · min-reviews bands (0/10/100/1k/5k+, independent
  toggles, gaps allowed) · updated-within (any/1mo/3mo/6mo/1yr/1yr+) · **Playtime sort**
  (▲ *(default)* / ▼).
- **Flags** — the PICS cluster (§9.6): six tri-state Any/Exclude/Only presence flags (Early
  Access · AI disclosure · Adult content · VR-only · Family-share block · Custom EULA) plus
  two graded controls (Controller, Steam Deck). Folded by default; no-ops behind the `HAS_PICS`
  guard when `pics.json` is empty.
- **Tags** — the **tag rail** (click to require → exclude → clear, with live per-tag counts,
  two-tier with a "+N more" expander) plus a **tag-name search** and the **`Required tags
  match: ALL / ANY`** toggle (`state.tagMode`, URL `tagmode`; ALL is the default, and
  **exclude is always AND-NOT** regardless). The rail groups tags into three fixed categories —
  **Players & Mode / Genre / Style, Theme & Feel** — via a hardcoded `TAG_GROUPS`/`TAG_CAT`
  taxonomy, plus a `CANON_GROUPS` synonym map that canonicalizes near-duplicate tags (e.g.
  different "co-op" spellings collapse to one chip) before counting and display. Folded by
  default; while folded, the picked tags render as mini cycle-chips in the header band (§3.4 R4).
  - **Tag search (`#tagSearch`)** narrows *which tags are offered*, not which games are listed:
    typing `strategy` reduces the rail to Strategy / Grand Strategy / Turn-Based Strategy /
    Strategy RPG so a related family can be picked from a shortlist. Tag names are stored
    canonical-lowercase, so the match is a plain lowercased substring test. While a query is
    active the **"+N more" split is bypassed** and every match is shown outright — burying
    matches behind an expander would defeat the point. A ✕ (shown only when non-empty, Esc
    also works) clears it and restores the full rail.
  - It is a **display filter only**, so it is deliberately **not serialized to the URL** and
    **not counted** in the section's "N active" badge — it changes nothing about the result
    set. Only `buildTagRail()` re-runs, never `render()`; the input is **debounced 120 ms**
    because the rail's contextual counts are an O(games) pass and one per keystroke would be
    wasteful at 124k games. For the same reason it is **not gold when active** — gold means
    "a filter is changed from its default", and this one filters nothing.
  - **Styling is borrowed, not invented:** same `--panel` fill / `--line` border / 9px radius
    as the header game-search and the wishlist box, the same mono 13px as the wishlist input,
    and literally the same 24-viewBox magnifier SVG as `.search` (at 14px). Its vertical
    padding is 1px tighter than the wishlist's so the control lands at **exactly 34.4px — the
    tag row's existing height** — because the match-mode control sets that height and the row
    must not grow. (It therefore reads ~2px shorter than the wishlist box; that is the
    deliberate trade for zero row growth.)

Outside the accordions: the **title search**, the **wishlist import** row (a global action,
§12), and — when the bar is collapsed — the **filter summary line** of clickable chips with
inline popover editors, a **`Reset`** chip, and the **`sorted by …`** chip that is how sorting
is reached on mobile (§3.4 L2, R5, R6). The `.bar-tools` cluster holds the **view switcher**
(Table · Card · Grid) and **`Lucky`** (random pick from the filtered set, shown only once a
filter is active), **`CSV`** (column-picker export of the whole filtered set) and **`🔗`**
(copy the current state URL).

The **QTPD logo wordmark doubles as a filter toggle** — clicking it opens/closes the whole
filter nav (identical to the "Show / Hide filters" collapse handle, which stays). The logo is a
real `<button>` (keyboard-focusable, `title`/`aria-label` set) rather than a decorative `<div>`.

**Sort.** Click any header to sort (`setSort`); the active header shows a gold arrow
**absolutely positioned at its bottom-center**, so it costs no column width or row height
(it previously overflowed and got clipped by the neighbor). The **Price / Sale** header is a
special case: it holds **two** sort targets (`<button>`s for `price_final` and `discount_pct`)
inside one cell, and the arrow anchors under whichever half is active — so the sort machinery
selects on `.sortable` (matching both the `<th>`s and the inner split buttons), not `th.sortable`.
For a split header the arrow is inserted **into the sub-button** (not the `<th>`), whose box is
only as tall as its text, so `.arrow{bottom:1px}` sat on top of the label; `.splitsort > .arrow
{top:100%}` re-anchors it just beneath the sub-button, matching a single-column header's arrow
(§16). Score/Count works the same way.
Two selector toggles live in the filter bar and do **not** sort on their own:

- **Reviews sort by** (all-time / 30-day) — which score the Reviews column sorts on.
- **Playtime sort** (▲ recommenders / ▼ non-recommenders) — which median a click on the
  Playtime column will sort by. Switching it updates the Playtime **header** (the selected
  side lights up, the other dims) so you can see what a header-click will do, and re-sorts
  **only if** Playtime is already the active sort. It uses the neutral segmented-control
  styling — the green/red lives in the header and cells (semantic), not on the toggle.

**State in the URL.** Every filter/sort choice is serialized to the querystring by `syncURL()`
and restored by `loadFromURL()`, so any view is a shareable link. Defaults are omitted (e.g.
`hq` only appears when not `real`, `pt` only when not `up`), which keeps shared links short and
means a bare URL is the default view. **The full set is 28 params:**

| Group | Params |
|---|---|
| Search & tags | `q`, `inc`, `exc`, `tagmode` |
| Value | `pc`, `basis`, `hltb`, `hq`, `pmin`, `pmax`, `qmin`, `qmax` |
| Quality | `minscore`, `rev`, `trend`, `upd`, `ratesrc`, `pt` |
| Flags (PICS, §9.6) | `flags`, `noflags`, `ai`, `adult`, `ctrl`, `deck` |
| Sort & paging | `sort`, `dir`, `per` |
| Wishlist | `wishonly` |

Two things are deliberately **not** serialized: the **hidden-games list** (session-only, see
*Thumbnails & hiding*) and the three `localStorage` preferences — `qtpd.view`,
`qtpd.sections`, `qtpd.tagsCollapsed` — which are per-device chrome, not the query a link is
meant to reproduce. The `qmin`/`qmax` range is written only once the slider is manually moved
(`qRangeTouched`), since it otherwise auto-fits the result set. Pagination is infinite-scroll
at 100 / 500 / 2000 per page (the selector is hidden on mobile). *(Historical note: the boolean
`sale` param was removed when the on-sale-only toggle became the three-way `pc` price-type
filter; old links carrying `sale` are simply ignored.)*

**Thumbnails & hiding.** Header art with a hover-enlarge popover, sourced from the PICS
`art` field (§9.5) and falling back to a content-verified chain of appid-derived URLs for
games PICS hasn't covered. `art` comes first because it is the only *authoritative* source:
Steam's `store_item_assets` scheme puts a per-asset SHA1 in the path, which cannot be
derived from the appid, so derived URLs 404 for those games — including some that ship no
plain `header.jpg` at all (only `<sha1>/header_alt_assets_1.jpg`), which previously rendered
as broken images. The chain advances on both a hard error and a "loaded but empty" result,
because Steam sometimes answers missing art with 200 + a degenerate image rather than a
clean 404. Adult art is **blurred with an 18+ badge** behind a **three-stage gate** (permanent
until a real age gate exists); the flag is PICS `content_desc` codes 3/4 (Valve-authoritative),
falling back to the legacy `ADULT_TAGS` heuristic only for games PICS hasn't covered.

**The 18+ gate (`adultStage` / `adultSetStage`, shared by the table thumb and the grid card).**
Stage 1 blurred + `18+` → stage 2 `.confirm` shows `18+?` → stage 3 `.revealed` unblurs.
**Revealing never navigates.** Stage 3 used to *also* `window.open()` the store, so the
confirming click unblurred the art and immediately threw you out to Steam — you could never
look at the image you had just agreed to see. Opening the store is a separate intent with its
own control: the **title beside the art** is the `<a>` link (and in the grid, the expanded
card's `Steam ↗`). Art reveals; links navigate. **Right-click undoes**, mirroring the tag
chips' forward/backward cycle: stage 2 → 1 ("no, I'm not 18") and stage 3 → 1 (re-hide);
at stage 1 there is nothing to undo, so the browser's own context menu opens untouched.
Re-hiding also closes the hover-popover, which otherwise only re-checks `revealed` on
mouseenter and would leave a large un-blurred still on screen. Each
row has a slim `[x]` hide button; hidden games can be un-hidden, but the hide list is
**session-only and deliberately excluded from URL serialization** (unlike every other
filter/sort choice, §11 *State in the URL* below) — a reload or a shared link does not carry
hidden games with it.

---

## 11.5 Coverage tracking (`coverage.py` → `COVERAGE.md`)

`COVERAGE.md` is a **generated, two-axis** snapshot of the database's completeness and
freshness, rebuilt after every scrape (§4). It exists because coverage silently drifted while
the data jobs kept running — most sharply, the `updates.json` / `updates_raw/` layer was **not
measured at all**, which is why a simple "why does the badge show for some games and not
others?" question had no answer without manually reading shards. **Rule of thumb: every value
the frontend renders must have a row here.** If you add a scraper or a displayed field, add it
to `coverage.py` in the same change.

### Field → source → refresh map (the authoritative list)

Every frontend-consumed value, its storage file, its producing scraper, and the per-game
timestamp used for staleness. This table *is* the checklist for "did we forget to track
something."

| Frontend value(s) | Storage file | Scraper | Per-game staleness key | Refresh rule |
|---|---|---|---|---|
| name, appid, release, rating %, review count, `last_update_ts` | `games.json` | `scraper.py` | `scraped_at` | age-tiered by time since release (6h → 15d, `REVIEW_TIERS`, re-checked mid-run under 30d); plus Steam `last_modified` and PICS review-drift (§6) |
| price, discount, sale-end | `prices.json` | `price_and_sale.py` | `scraped_at` | no cooldown — whole non-free base re-batched ~3h |
| tags | `tags.json` | `tags_refresh.py` | **none** | fetch-once, **no rescrape** ([ROADMAP.md](ROADMAP.md) §3.5) |
| recent 30d % / count | `recent.json` | `recent_refresh.py` | `recent_scraped_at` | two-track: active 4d / dormant 30d |
| HLTB main/extra/complete/avg + `est` | `hltb.json` | `hltb_refresh.py` | `fetched_at` | partial 14d / full 365d; blank backoff 3→30→180d |
| median playtime ↑/↓ + n | `playtime.json` ← `playtime_raw/` | `playtime_refresh.py` → `playtime_summarize.py` | **none per-game** (proxy: newest review `ts`) | two-track: active 7d / dormant 30d; floor 10 reviews |
| weighted rating (steam/raw/capped + n) | `ratings.json` ← `playtime_raw/` | `ratings_summarize.py` | inherits `playtime_raw/` | derived on every raw pass |
| cadence badge `upd_c90` / `upd_c365`, `last_update_ts` backfill | `updates.json` ← `updates_raw/` | `updates_refresh.py` → `updates_summarize.py` | `scraped_at` (in `updates_raw/`) | two-track: active 7d / dormant 45d; floor 10 reviews |
| header art, PICS tags, Deck/controller, EA / adult / VR-only flags, AI disclosure, EULA, family-share, Metacritic | `pics.json` ← `pics/` ← `pics_raw/` | `pics_refresh.py` → `pics_summarize.py` → `pics_merge.py` | `_ts` (in `pics_raw/`) | single flat window: `--stale-days` (14), daily cron; **no review floor** |
| tag-name / genre / category decode maps | `lookups/*.json` | `pics_lookups.py`, `build_category_map.py` | **none** | committed static, refreshed manually |

### Axis 1 — total coverage

Per metric: how many of the `games.json` universe have data, as a % of catalog, sorted
descending. The "have we got it?" axis. Playtime/updates also get an **addressable-set**
framing in the Notes (denominator = games above the `MIN_REVIEWS_FLOOR = 10` gate), because
measuring them against the *full* catalog understates them — the sub-floor games are
deliberately never fetched, not missing.

### Axis 2 — refresh schedule

Covered rows bucketed by **how each row's own scraper will treat it on the next pass** — never
against a flat target. This answers "is the pipeline keeping up, and what's the queue shape?"

The two-track scrapers (recent, playtime, updates) classify each game as **active** (its
`last_update_ts` is within `UPDATE_ACTIVE_DAYS = 90` → short cooldown) or **dormant** (long
cooldown). The dormant lane refreshing rarely is a **feature**, not a shortfall — budget is
front-loaded onto games whose data actually moves. Buckets:

- **7d-track / 30d-track** — which cooldown lane the game sits in (totals include overdue members).
- **overdue** — already past its lane's cooldown = the real backlog signal.
- **empty** — scraped but correctly produced nothing usable (below the review floor, or a null
  score). Not pending work; keeping it separate stops "correctly skipped" from inflating backlog.
- **never** — no data yet = the fill frontier / true pending backlog.

`coverage.py` copies each scraper's cooldown constants **verbatim** (see the constants block at
the top of the file) so the doc can't drift from the real `is_eligible()` gates. Single-window
scrapers that don't fit the two-track shape (`games.json` core, `hltb.json`, `prices.json`) are
reported as inline fresh/overdue lines rather than table rows.

**Known approximation (playtime).** `playtime_raw/` stores a timestamp per *review*, not per
*game*, so playtime staleness uses each game's **newest review `ts`** as a proxy. A game with
no recent reviews reads as overdue even if freshly walked, so its `overdue` figure is an
**upper bound** (marked `†` in the table), not exact backlog — unlike Update events, which has
a real per-game `scraped_at`. An exact figure needs a per-game `scraped_at` added to the shard
records. **Tags** have no timestamp at all and are Axis-1-only until one is added. Both are
tracked in [ROADMAP.md](ROADMAP.md) §3.5.

### Invariants

- **One writer:** `coverage.py` → `COVERAGE.md` only, same discipline as `shard_health.py` →
  `SHARDS.md`. All reads are read-only; each source file stays owned by its own scraper.
- **Generated, never hand-edited** — a banner at the top of `COVERAGE.md` says so.
- **Superset discipline:** the script must stay a superset of what the frontend renders. The
  field→source table above is the contract; update both when the data model changes.

---

## 12. Wishlist import & the Cloudflare Worker

The browser can't read a Steam wishlist cross-origin, so a small Cloudflare Worker (free tier)
proxies it. **This repo does not contain the Worker's source** — there is no `worker/`
directory in git history; this deployment's `WISHLIST_PROXY` constant points at an
already-deployed Worker (`https://qhpp-wishlist.mlmariss.workers.dev`) that lives outside the
repo. Reviving/replacing it means writing a Worker that implements the two endpoints below
from scratch (see README's "Wishlist import (optional)" section for the shape).

`parseSteamId` in `index.html` recognizes **six named ID formats** across 5 regex branches
(one branch handles both the profile-URL and bare-SteamID64 cases) — profile URL, custom
`/id/<name>` URL, bare vanity name, SteamID64, `STEAM_0:0:…` (SteamID2), and `[U:1:…]`
(SteamID3). Numeric formats convert **client-side** via BigInt math; vanity names resolve
through the Worker's `/?vanity=` endpoint (`ISteamUser/ResolveVanityURL`). The Worker's
`/?steamid=` endpoint returns the wishlist for cross-referencing the catalog (and optional
wishlist-only filtering). It requires the profile's **game details to be public**.

Point `WISHLIST_PROXY` (top of the wishlist code in `index.html`) at the deployed Worker URL.
"Self-disables" is a simplification: there's no code that hides or disables the wishlist UI on
load. The only literal self-disable check is `WISHLIST_PROXY.includes("REPLACE-WITH-YOUR-WORKER")`
— if the constant still holds that placeholder string, the import action is blocked with a
toast before it tries to fetch anything. If instead the constant points at a real but
unreachable/misconfigured URL, the **Import** button stays fully visible and clickable; each
attempt just fails with a "Couldn't reach the wishlist service" toast. Either way the rest of
the site (browsing, filtering, sorting) is unaffected — no backend is required for that.

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
- **`REVIEW_TIERS`** (scraper, code constant) — age-since-release → refresh-cooldown ladder
  for review score/count (§6). `REVIEW_TIER_REFRESH=0` disables it.
- **`REVIEW_LIVE_MAX_AGE_DAYS` (30)** (scraper) — games this recently released are re-checked
  for due-ness at every checkpoint, not just at run start, so cooldowns shorter than the 6h
  cron grid are real rather than nominal (§6). Raising it widens the mid-run re-scan set.
- **`PICS_REV_DELTA` (3)** (scraper) — percentage-point disagreement between `pics.json`'s
  `rev` and stored `rating_pct` that queues a corrective re-scrape (§6). `0` disables.
- **`FORCE_RESERVE_FRAC` (0.5) / `NEW_RESERVE_FRAC` (0.25)** (scraper) — guaranteed minimum
  share of each run's pops for the forced-re-scrape drain and for new coverage, so neither
  can be starved by a large refresh queue. Both `0` = pure priority order.
- **`STOREFRONT_MIN_INTERVAL` (0.9, env)** (scraper) — the shared storefront rate limiter every
  `appdetails` / `appreviews` / search call passes through (`storefront_pace()`). This, not the
  now-deprecated `STEAM_DELAY`, is the scraper's real pacing knob; raise it if 403 rates spike
  (§16 revert plan).
- **`MAX_RECHECK` (4, env)** (scraper) — how many runs an app whose `appdetails` returns
  `success:false` is retried before moving to `skipped`. `success:false` lumps region-locked and
  transient blips in with dead apps, hence the retries (§5, `catalog["recheck"]`).
- **`SEED_RESOLVE_TTL` (24h)** (scraper) — how often a term/URL seed is live-re-resolved, so a
  search term keeps catching newly-released matches (§6).
- **`HLTB_MIN_SIMILARITY` (0.65)** — HLTB title-match threshold.
- **`RESCRAPE_PARTIAL_DAYS` (14) / `RESCRAPE_FULL_DAYS` (365)** (hltb) — re-check windows for
  partial vs complete real triples. **`BLANK_EAGER_DAYS` (3) / `BLANK_BACKOFF_DAYS` (30) /
  `BLANK_FREEZE_DAYS` (180)** with **`BLANK_EAGER_ATTEMPTS` (3) / `BLANK_BACKOFF_ATTEMPTS` (6)**
  — the attempt-scaled blank-retry curve (§8.1 Phase B). **`IDLE_DRAIN_MAX` (4000) /
  `IDLE_DRAIN_SKIP_FROZEN` (True)** — the never-idle drain's per-run cap and frozen-tier skip.
- **`PRICE_BATCH` (100) / `GETITEMS_BATCH` (50)** — appids per batched `appdetails` price call
  and per `IStoreBrowseService/GetItems` sale-end call.
- **`RECENT_COOLDOWN_DAYS` (4)** — staleness before a recent score is re-checked. Its two-track
  partner is **`NOUPDATE_COOLDOWN_DAYS` (30)**; **`MIN_AGE_DAYS` (45)** skips games too new for
  Steam to show a recent score at all, and **`RECENT_MIN_COUNT` (10)** is the count below which
  Steam suppresses it.
- **Two-track cooldowns (the shared shape).** `recent_refresh.py`, `playtime_refresh.py` and
  `updates_refresh.py` each classify a game as *active* or *dormant* on
  **`UPDATE_ACTIVE_DAYS` (90, all three)** — whether `last_update_ts` is within that window —
  then apply **`COOLDOWN_DAYS` / `NOUPDATE_COOLDOWN_DAYS`**: recent **4 / 30**, playtime
  **7 / 30**, updates **7 / 45**. `coverage.py` copies all six verbatim so Axis 2 can't drift
  from the real gates (§11.5).
- **`DEPTH_LADDER` (`1000 → 2000 → 3000`) / `PER_GAME_CAP` (3000 = the ladder's last rung)**
  (playtime) — the per-game storage ceiling is a **ladder**, not a flat number: `cap_for(held)`
  returns the first rung strictly above what a game already holds, so a first touch fills to
  1000 and releases the game, and each later visit climbs one rung. Raising the ceiling is a
  one-line edit to `DEPTH_LADDER`. See §9 *Depth ladder* for the sizing evidence.
- **`REWALK_DAYS` (30, env) / `REWALK_DELTA` (1000, env) / `REWALK_MIN_DAYS` (7)** (playtime) —
  ceiling-staleness triggers. A game pinned at the ceiling gets a **deep re-walk** (every held
  playtime refreshed) when `REWALK_DAYS` have passed since its last full walk (`walk_at`) **or**
  its review count grew by `REWALK_DELTA` (floored at `REWALK_MIN_DAYS` between firings). Either
  knob at `0` disables that trigger; both `0` restores pure top-100 refresh. See §9 *Ceiling
  staleness*.
- **`TARGET_REVIEWS` (200, env) / `SEEN_STREAK_STOP` (50)** (playtime) — the run-wide *floor*
  target (the ladder raises it per game) and the consecutive-already-seen streak that ends a
  walk early. `DEEPEN_TARGET` (env, one-off) still forces a deeper pass for a whole run.
  `updates_refresh.py` has its own unrelated **`PER_GAME_CAP` (200)** and **`EVENTS_COUNT`
  (50)** per fetch — same name, different pipeline.
- **`WINDOWS` ([30, 90, 180, 365]) / `DATES_CAP` (60)** (`updates_summarize.py`) — the count
  windows shipped in `updates.json` and the per-tier cap on the client-recomputable `dates`
  arrays (§9.5).
- **`--stale-days`** (CLI, `pics_refresh.py`) — per-game `_ts` age that makes a PICS record due
  again. The script defaults to **0** (refetch everything); the daily `pics.yml` passes **14**
  and exposes it as a `workflow_dispatch` input, so 14 is the effective production value and
  `coverage.py` copies it as `PICS_STALE_DAYS` (§9.6, §11.5).
- **`QNU_MIN_REVIEWS` (10) / `QNU_LOW_TRICKLE` (3000)** (env, `queue_null_updates.py`) — the
  high/low review split for the one-off null-`last_update_ts` drain and the per-run cap on the
  low tier (§16).
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
  next run when the version stamped in the shards doesn't match. `updates_raw/` uses the same
  key/count but its own `SHARD_KEY_VER` (currently `1`, versioned independently of playtime's).
- **`WARN_MB` (50) / `CRIT_MB` (80)** (`shard_health.py`) — per-shard size thresholds for the
  🟡/🔴 status flags in `SHARDS.md`, against the 100 MB hard GitHub limit.
- **`MIN_TRIPLES_FOR_LIVE` (30) / `MIN_PER_BUCKET` (15)** (`hltb_estimate.py`) — cold-start
  gates for the live ratio model (§8): below 30 real triples overall, the flat ratio falls back
  to frozen constants; below 15 samples in a given magnitude bucket, that bucket falls back to
  its own frozen value before the flat ratio.
- **`STEAM_API_KEY`** (secret) — enables the keyed catalog enumeration + change-detection.

---

## 14. Pace, limits, cost

The scraper captures ~1,000–1,200 games/hour (storefront limit ÷ ~2 calls/game). The catalog
has since reached full coverage — **124,210 stored** (2026-07-22 `COVERAGE.md` snapshot)
against a ~173k app universe, with the fresh frontier essentially exhausted — so `scraper.py`
now finishes a run in **~7 minutes** of *new-coverage* work and spends the rest of its budget on
the refresh ladder below. Faster = raise `RUN_MINUTES` or add off-peak
`cron` times. Cost is **$0** — Actions is free/unlimited on public repos; the only ceiling is
the 6-hour per-job limit. Each daily commit also keeps the repo active — **GitHub disables
scheduled workflows after 60 days of no commits**, so the steady commits are load-bearing for
the whole system staying alive.

**Sizing the age-tiered review refresh (§6).** Measured against the real cohort sizes in
`games.json` (~68 new released games/day); re-counted 2026-07-23 against the live 124,210-game
file, which is the "live" column:

| age band | cooldown | games in band (live) | refreshes/day | storefront calls/day |
|---|---|---:|---:|---:|
| 0–3 d | 6 h | 188 | 752 | 1,504 |
| 3–10 d | 12 h | 708 | 1,416 | 2,832 |
| 10–30 d | 1 d | 1,525 | 1,525 | 3,050 |
| 30–60 d | 2 d | 2,228 | 1,114 | 2,228 |
| 60–90 d | 3.5 d | 2,042 | 583 | 1,166 |
| 90–180 d | 7 d | 6,499 | 928 | 1,857 |
| 180–365 d | 15 d | 11,004 | 734 | 1,467 |
| **total** | | **24,194** | **~7,052** | **~14,104** |

Cohort sizes drift a few percent day to day as releases age through the bands; the totals are
stable to within ~2%, so treat this as a sizing estimate rather than a live figure. The
`young` set that `requeue_due_young()` re-scans at each checkpoint is the first three bands —
**2,421 games** at this snapshot.

That is **~3.6 h/day** of run time at `STOREFRONT_MIN_INTERVAL=0.9`. For scale: this
scraper's *proven* peak is **30,809 games in one day** (2026-07-09, during the null-update
drain = 61.6k calls ≈ 15.4 h), and its window is ~22 h/day, so the ladder is **~23% of
demonstrated capacity and ~16% of the window**. Steady-state throughput before this change
was only **~200–500 games/day**. The first pass is a one-time catch-up (~19.8k games due at
once against a 6-day-old snapshot, ~9.9 h), which shares the run with the forced drain and
settles within a day or two. Nothing else changes: the other jobs run on separate runner
IPs with their own budgets.

**Where the remaining headroom is.** Roughly a further 2× fits inside the window before the
*scraper* becomes the constraint — but the binding limit past that is Steam's ~200/5min
per-IP soft limit and the resulting 403 cooldowns, **not the clock**, so 403 rates are the
number to watch rather than run duration. The two levers for spending that headroom (a
reviews-only pass at 1 call/game, and the `CHECKPOINT_SECONDS` granularity floor below a 6h
cooldown) are written up in [ROADMAP.md](ROADMAP.md) §3.5. Neither is needed today.

**Playtime scale-up (storefront budget reallocation).** Because `scraper.py` now uses so little
of its window, the shared ~200/5min storefront budget has headroom. The playtime raw pass was
scaled from **4 → 8 cron slots** (`:23` every 3h) and **`STEAM_DELAY` 2.0 → 1.5s**, roughly
**2× review-time throughput** (~12h/day → ~24h/day, near-continuous). At 1.5s it sits at the
storefront ceiling with no headroom; `recent_refresh.py` already sustains 3h passes at 1.5s
(separate runner IPs), so this is expected to hold — but 403 rates are worth watching, and the
revert is just `STEAM_DELAY` back to 2.0 and/or fewer slots.

---

## 15. Operational notes & caveats

- **HLTB** matches by title similarity; misses show `—`. **The first full pass is complete** —
  `hltb.json` now holds an entry for 124,166 of 124,210 games (100.0%), of which **105,337
  (84.8%) carry real values** and 18,829 (15.2%) are estimate-filled. The job's steady state is
  now the priority re-scrape ladder (partials 14d → blanks on the attempt-scaled curve →
  full-real 365d) plus the never-idle drain, not first-pass coverage (§8, §8.1).
- **HLTB estimates** are clearly marked (blue + tooltip) and auto-replaced once real data
  arrives; they never train the ratio.
- **Weighted rating** needs public playtime and enough reviews: none below 5, grayed 5–9.
- **Tags** fall back to Steam genres when SteamSpy lacks a game.
- **Sale end times** collapse offline when expired — `expireSaleIfEnded()` (§11) actively
  zeroes `discount_pct` and resets `price_final` to `price_initial` in the merged in-memory
  record once `discount_end` has passed, not just the countdown UI, so the displayed price
  stays honest between price refreshes.
- **Dataset size** — the two per-game working sets are **sharded** (`playtime_raw/NN.json` and
  `updates_raw/NN.json`, 64 buckets each) because one file would exceed GitHub's 100 MB limit.
  Measured (`SHARDS.md`, 2026-07-22): the biggest `playtime_raw/` shard is **13.27 MB**, median
  12.18 MB — ~87 MB of headroom, and the projection at full coverage is still only ~13 MB, so
  the wall is no longer a near-term concern. `pics_raw/` and `pics/` are sharded 64-ways too.
  The **largest single file is now `games.json` at 62 MB**, ahead of `pics.json` (39 MB) and
  `hltb.json` (35 MB) — it grows with the catalog and is the one to keep an eye on, though it
  is still comfortably clear of the limit. Note `SHARDS.md` monitors **only `playtime_raw/`**;
  pointing `shard_health.py` at the other three shard sets is a cheap open item (§9.5,
  [ROADMAP.md](ROADMAP.md) §3.5).
- **Sandbox limitation** (for maintenance): the dev environment can't reach Steam domains, so
  Steam-dependent code is unit-tested against documented response shapes and verified by
  running it live in Actions.
- **Table sizing — resolved, no longer a caveat.** This entry used to warn that `max-width` on
  `<col>` under `table-layout: auto` was best-effort and that adding a column might still need a
  matching `<col>`. Both were answered when the table moved to **CSS Grid `minmax()` tracks**
  (§11): `minmax()` enforces floor *and* ceiling, so a slim column cannot exceed its max even on
  an extreme ultrawide, and the `<colgroup>` is now `display:none` and inert — a cell with no
  matching `<col>` is auto-placed into the same implicit track in header and body, so the layout
  can't collapse (Playwright-verified at 1700px). §11's *Adding a column* recipe is authoritative.

---

## 16. Recent changes

- **Custom tooltip layer, full filter-tooltip coverage, tags readability, arrow fix (Jul 2026).**
  Frontend only, no data changes.
  1. **One custom tooltip replaces the native `title` box everywhere.** All ~98 explanations
     (filters, column headers, cell values) are still authored as plain `title` text — kept as
     the accessible, JS-off fallback — but a single event-delegated engine (bottom of
     `index.html`) draws them in one styled floating box (`.uitip`): **bigger text (13.5px)**,
     **generous padding**, a raised panel with a border and a real shadow so it no longer blends
     into the page. It hijacks the native tooltip by **blanking the element's `title` on hover**
     (attribute kept, so the `[title]` cursor rules still match) and restoring it on leave, so the
     OS never draws its grey box on top. Delegation means dynamically-rendered rows are covered
     with zero per-element wiring. **Subtle keyword coloring is automatic** from the plain text —
     no per-tip markup: the leading subject (before the first `—`/`:`) is gold, numbers /
     percentages are blue, and a small positive/negative word set picks up the app's teal / coral.
  2. **Help (`?`) cursor is now the single tooltip affordance.** The old per-field dotted
     **underline was dropped** — it cluttered the page once every explained field wore one.
     `[title]{cursor:help}` is the base signal; genuine action buttons (view/CSV/steppers/tags)
     are overridden back to `pointer`, but the sortable column headers keep the `?` (they are the
     fields this touches, even though a click also sorts).
  3. **Split-header sort arrow no longer overlaps its label.** In Score/Count and Price/Sale the
     arrow is inserted **into the clicked sub-button**, whose box is only as tall as its text, so
     the shared `.arrow{bottom:1px}` landed on top of the label. `.splitsort > .arrow{top:100%}`
     re-anchors it just beneath the sub-button — reading below the text like a single-column
     header, centered under the actual sub-label, adding **no row height** (still absolute, inside
     the header's padding). *Verified in-browser: arrow top now sits below the button's bottom
     edge for Score, Count and Sale; thead row height unchanged at 42px.*
  4. **Weighted tooltip shows the real per-game review count.** The tip already interpolated
     `g.wr_n`, so it tracks the **playtime depth ladder** (1000 → 2000 → 3000, below)
     automatically — now formatted with a thousands separator (`toLocaleString`) and reworded.
     All tooltip copy was tightened for plain-language clarity in the same pass.
  5. **Full tooltip coverage across the nav bar.** Every filter now carries an explanation, not
     just the flags: each **field** (QTPD price basis, HLTB metric, HLTB data, price range, price
     type, QTPD range, min rating, reviews-sort, review trend, min reviews, updated-within,
     playtime sort) and each **section header** (Value / Quality / Flags / Tags) got a `title`
     overview drawn from §11/§13, and the option buttons that lacked one (Min rating, Updated
     within) were filled in. Top-bar controls too — Search, Wishlist + Import, Reset all filters,
     and the tag rail's group labels and "+N more" expanders. Field titles cover the whole field
     (label + control); a button's own more-specific tip wins when hovered directly.
  6. **Tags panel readability.** The legend, per-tag counts, group labels and "+N more" were
     small and low-contrast. Bumped: `.taglegend` 12→13px on `--muted` (was `--muted-2`) with
     `.tl-arrow` opacity .5→.8; chips 13→13.5px on `--text` (was `--muted`); the count `.ct`
     opacity .6→.8; `.grouplabel` to full opacity; `.tagmore` onto `--muted`.

- **Tag search + tag-row alignment fixes (Jul 2026).** Three frontend fixes, no data changes.
  1. **`Required tags match` never actually moved right.** `.tagmode` has carried
     `margin-left:auto` since it shipped, but the fold-zones work later added
     `.filter-section.open > .section-body .tagmode{padding:6px; margin:-6px}` — and that
     shorthand (specificity 0,4,0 vs 0,1,0) silently reset the auto margin, leaving the control
     mid-row. Restored with a following `margin-left:auto` at equal specificity, keeping the
     fold-safe negative margins on the other three sides. *Confirmed in-browser: computed
     `margin-left` was `-6px`, now resolves to the free space and the control sits flush right.*
  2. **New tag-name search** in the space the match-mode control vacated (§11 *Filters*):
     narrows which tags the rail offers (`strategy` → Strategy / Grand Strategy / Turn-Based
     Strategy / Strategy RPG), bypassing the "+N more" split so every match is visible, with a
     ✕ / Esc to restore the full rail. Display-only — not in the URL, not an "active" filter,
     never re-renders the game list; debounced 120 ms.
  3. **Collapsed Tags column alignment.** Grid rows use `align-items:center`, so cells are only
     as tall as their own content — which put the header underlines at **two different heights
     (217px and 219px)** and rendered the collapsed body seam as a **10px stub in a 79px row**.
     Fixed with `align-self:stretch` on `thead th` and the collapsed Tags `<td>`; verified by
     measuring before/after in the live DOM (header bottoms collapse to a single 219px, seam
     goes 10px → 78px).

- **Playtime ceiling staleness: periodic deep re-walk (Jul 2026).** Closes the residual left by
  the depth ladder below. A game pinned at the 3,000 ceiling refreshed only its **top ~100
  playtimes per visit**, so positions 100–3,000 froze while `playtime_forever` kept growing —
  ever-staler playtimes on the catalogue's most popular games. Now a **deep re-walk** (whole
  window's playtimes refreshed + all new reviews caught up) fires when **`REWALK_DAYS` (30)** have
  elapsed since the last full walk **or** review count grew by **`REWALK_DELTA` (1000)**
  (floored at 7 days so mega-games can't thrash). The time backstop is the load-bearing trigger —
  playtime staleness is clock-driven, so a slow-churn back-catalogue game that never trips the
  count threshold still refreshes every 30 days. **Spread, not batched:** each game's own
  `walk_at` / `rc_at_walk` anchors (stamped when the ladder first filled it, already staggered)
  give it an independent due-date, and it adds **no visits** — it just makes ~every 30-days-worth
  of the existing cooldown visits deep. Cold start only initializes the clocks (no forced walk),
  so the deploy doesn't stampede. Cost ~2 h/day (~8% of the playtime budget) across ~4,228 ceiling
  games; storage and git growth are unchanged (still capped at 3,000). Verified with a stubbed
  Steam: shallow until the 30-day mark, then a 30-page deep pass refreshing all ~3,000; churn
  gated by the 7-day floor; cold start initializes without walking; two games last-walked 20 days
  apart come due 20 days apart (independent clocks). Disable via `REWALK_DAYS=0` / `REWALK_DELTA=0`.

- **Playtime depth ladder: 1000 → 2000 → 3000 (Jul 2026).** `PER_GAME_CAP` was a flat 1,000,
  so **7,702 games (9.7% of coverage) sat pinned at the cap**, holding 1,000 of a median 3,034
  real reviews. It is now a **ladder** (`DEPTH_LADDER`): `cap_for(held)` returns the first rung
  above what a game holds, so the first touch still fills to 1,000 and *releases* the game —
  keeping the fill frontier draining — while each later visit climbs one rung to a 3,000
  ceiling. **No new per-game state** (`len(reviews)` is the rung pointer) and **no extra
  visits** — it piggybacks on the normal cooldown, so the climb completes in ~3 weeks by itself.
  Evidence and the 3,000-vs-5,000 call are in §9 *Depth ladder*: the newest-N sample is nearly
  unbiased (1.03×), so this buys **noise reduction** — and above all a thicker **minority
  sentiment side**, median just 158 reviews on capped games, which is the exact number the
  "played long, still says skip it" inversion signal rests on. Cost ~44 h one-time and
  +7.8 MB/shard; the binding constraint is **git growth**, not the 100 MB file limit, which is
  why the ceiling is 3,000. Verified end-to-end against a stubbed Steam: fresh game → 1,000 in
  10 pages; climbs to 2,000 then 3,000 on successive visits; holds exactly 3,000 at the ceiling
  with the ring buffer sliding the window forward; small (350) and mid (1,500) games stop
  correctly at `exhausted` with no wasted pages.

- **Playtime refresh ladder + multi-shard scheduling (Jul 2026).** Two coupled changes that
  only work together. (1) `playtime_refresh.py` replaced its flat `COOLDOWN_DAYS=7` /
  `NOUPDATE_COOLDOWN_DAYS=30` gate with a 4-tier **release-age ladder** (0–7 d → 1 d, 7–30 d →
  3 d, 30–90 d → 7 d, else 30 d), **halved** for games over 1,000 all-time reviews and floored at
  12 h; games without a parsed release date keep the legacy `last_update_ts` behaviour.
  Within-shard ordering moved from raw staleness to **overdue ratio** (`age ÷ own cooldown`) so
  the ladder is self-balancing. (2) The ladder was unreachable under one-bucket-per-run
  scheduling — `GITHUB_RUN_NUMBER % 64` capped every game at one refresh per ~8 days, and hot
  games are spread uniformly across all 64 shards — so a run now works **up to
  `MAX_SHARDS_PER_RUN=12` buckets**, ranked by due-game count, scheduled from `games.json` with
  **no shard bodies read** (scoring 64 × ~18 MB files would cost ~1.1 GB of I/O), with the
  round-robin bucket always included as a starvation guard. Shards are loaded/worked/committed
  one at a time, so peak memory is ~one shard and one-writer-per-file is untouched (the
  `steam-playtime-raw` concurrency group already prevents overlapping runs). Net: every shard
  reachable ~1.5×/day instead of once per ~8 days (§9, §4).
- **Playtime popularity floor — HLTB alignment (Jul 2026).** The release-age ladder above still
  left popular perennials (>1k reviews, years old) on the *older* tier at 15 d even after halving,
  while the HLTB re-scraper re-checks those same games every 5 d — the same "most-viewed refreshed
  least often" split §8 was built to kill, re-inherited on playtime's slow tiers. Each game's
  cooldown is now `min()`'d against a review-count floor matching HLTB's `POPULAR_TIERS` (>1k → 5 d,
  >500 → 10 d), so a popular back-catalogue title drops 15 d → **5 d** and a mid-popular one 30 d →
  10 d. `min()` only pulls a refresh **forward**: the fresh-release fast lane (12 h – 1.5 d) is
  untouched, and the change redistributes the fixed budget toward hot games rather than adding
  requests (§9).
- **Playtime staleness sweep — fixes the hot-first tail (Jul 2026).** Hot-first shard selection
  (rank by due-count) kept a stable ~12-shard core fresh but starved every other shard back to the
  once-per-run anchor — a ~8-day tail (measured: 23/64 shards >5 d stale, worst 8.4 d). *Black Flag
  Resynced* (shard 27) sat 8 days stale despite being the most-overdue hot game, because one game
  can't lift a shard ranked on its *total* due-count. Selection now sweeps **oldest-scraped shards
  first** (staleness read from a ~200-byte header, not the 18 MB body), bounding the full cycle to
  `ceil(64 / (shards_per_run × runs_per_day))`. Per-game priority still lives *within* a shard (the
  overdue-ratio ladder), so hot games are served first once their shard opens — priority moved to
  the layer where it can't starve anyone. `MAX_SHARDS_PER_RUN` was raised 12 → **24** (runs were
  idling 87% of their 180-min budget), giving a ~8 h full-sweep cycle; the anchor is retained only
  as a floor and `FORCE_SHARDS` pins buckets on demand (§9).
- **HLTB popularity fast lane (Jul 2026).** Re-scrape windows keyed only on entry completeness
  assumed HLTB data is static — true for back-catalogue, false for new releases. *AC Black Flag
  Resynced* held a lone `extra` value from a launch-week fetch while HLTB had since filled a
  full real triple (22.5 / 38.5 / 66.5), and completing that triple would have frozen the entry
  for 365 days. Windows are now `min()`'d against a **review-count tier** (>1k → 5 d, >500 →
  10 d) that overrides **every bucket including `full`** — overriding `full` is the actual fix —
  and can only pull a re-scrape forward, never delay one. Queue ordering within each bucket is
  now most-reviewed-first, then oldest. A free second signal rides along: HLTB's own
  `count_comp` submission count, read defensively from the untyped `json_content` (the library
  doesn't map it), stored as `n_comp`; growth ≥3 sets `comp_grew` and pulls the next check to
  5 days. HLTB's page-visible `Updated:` timestamp was evaluated and **rejected** — it exists
  only on the HTML detail page, not the search API, so using it would double the request count
  per game (§8). **Follow-up fix:** the first deploy went red at startup — `load_games()` now
  yields `(appid, title, review_count)` triples, but `hltb_selfcheck.py`'s idle-drain fixture
  still built 2-tuples, so `build_idle_drain` raised `ValueError: not enough values to unpack`.
  The self-check did its job (aborted **before** `hltb.json` was touched — no data damage);
  fixture updated and a `check_popularity_fast_lane()` case added covering min() semantics,
  strict-`>` tier boundaries, the frozen-blank interaction, `count_comp` type-strictness, and
  the Black Flag regression directly.

- **Review freshness: age-tiered refresh + PICS drift trigger (Jul 2026).** Fixes new
  releases freezing at their day-one review score. `last_modified` — until now the scraper's
  *only* refresh trigger in Actions — tracks store/depot changes and **never moves on review
  count**, and the `REFRESH_DAYS` fallback only applies without an API key, so a game scraped
  on release day was never re-checked. Black Flag Resynced sat at 49% / 2,019 reviews for 13
  days against a true 79% / 19k; the 30-day release cohort had a median scrape age of 13
  days. Two new triggers join `last_modified` in `select_work()`: **`REVIEW_TIERS`**, a
  cooldown ladder keyed on age since release (`≤3d→6h` … `≤365d→15d`), and
  **`PICS_REV_DELTA`**, which queues a re-scrape whenever `pics.json`'s already-harvested
  `rev` percentage disagrees with our stored one by ≥3 pt — a **free** signal (CM protocol,
  off the storefront budget) that flagged 1,783 stale scores on the first pass. The queue is
  rank-ordered so the fastest-moving cohorts go first (§6), and new coverage gained its own
  `NEW_RESERVE_FRAC` (25%) share of each run so the bigger refresh queue can't delay a
  just-released game — the failure mode the tiers exist to prevent. A game under
  `REVIEW_LIVE_MAX_AGE_DAYS` (30) is re-checked for due-ness at **every checkpoint**, not
  just at run start: `select_work()` runs once per 5.5h run on a 6h cron grid, which would
  otherwise put a ~6h floor under every cooldown and make the fast tiers nominal. Cost:
  ~7.2k refreshes/day ≈ 3.6 h/day of run time, ~23% of this scraper's proven peak (§14).
  `REVIEW_TIER_REFRESH=0` + `PICS_REV_DELTA=0` restores the old behaviour exactly.
- **Mobile card redesign + mobile sort (Jul 2026).** The narrow-screen (<1374px) view was
  rebuilt from the old "every `<td>` a label→value line" stack into a **single-column spec-sheet
  card**: thumbnail+title header, **QTPD** value + meter as the headline metric, then one metric
  per line (fixed label gutter + value) in a **logical order** — name → QTPD → price → ratings →
  length → release → updates → tags — set by CSS **`order`**, independent of the table's column
  order. Headers are **relabeled** on mobile (Reviews→Rating, HLTB→Length, Price / Sale→Price),
  and **no-data cells are dropped via `:has()`** (no active sale / weighted / trend / playtime) so
  cards carry no dead "—" lines; card height dropped ~620→~455px. Because the sortable `<thead>`
  is hidden in card mode, **sorting was previously impossible on a phone** — fixed by a **native
  `<select>` Sort control + direction toggle** in the bar, kept in sync with the header/URL sort
  state via `syncMobileSort()`. Filter controls that had fixed pixel widths (QTPD range, wishlist
  input) go fluid, and the price steppers get larger tap targets. CSS + markup + a small sort
  handler only; **desktop table untouched** (§11 *Responsive*, §3.2). *Caveat:* CSS `order`
  reorders visually only — assistive-tech reads cells in DOM (table-column) order.
- **Null-`last_update_ts` drain speedup (Jul 2026).** The one-off News-API fix (below) left a
  backlog of **52,371 false-null `last_update_ts` games** queued via `queue_null_updates.py`. The
  drain crawled — after the first window only ~3,900 had cleared (48,461 still null). Root cause
  was NOT Steam's rate limit but our own pacing: (a) `build_record` made **two** storefront calls
  per game — `appdetails` (serially paced by `time.sleep(STEAM_DELAY=1.5)`) and `appreviews`
  (fired **unpaced** inside the ThreadPool) — so bursts of 2 calls/1.5s tripped the ~200/5min
  soft-limit, and **each 403 cost a 5-minute `time.sleep(300)` stall**; and (b) the ~52k sweep
  included **25k+ games with <10 reviews** that re-scrape null→null (genuinely un-patched), clogging
  the front of the queue with dead work. Three fixes:
  1. **Shared storefront limiter.** New `storefront_pace()` + `STOREFRONT_MIN_INTERVAL` (env,
     default **0.9s**) gate **every** storefront call (appdetails + reviews + search pages) through
     one thread-safe budget. Ends the unpaced-reviews burst → far fewer 403 cooldowns → smooth
     ~1.8s/game (~2000/hr) vs the old ~3s-effective/game-plus-403-penalties. `STEAM_DELAY` is now
     deprecated (kept only as a doc comment; no longer referenced).
  2. **Reserved forced-drain budget.** `FORCE_RESERVE_FRAC` (env, default **0.5**) guarantees the
     forced queue gets at least that share of each run's pops via a dedicated `forced_q` +
     `next_work()` interleave, so a big-sale flood of `last_modified` refreshes can't starve the
     null drain run after run.
  3. **Tiered re-queue.** `queue_null_updates.py` now splits by `review_count`: the **high tier
     (>= `QNU_MIN_REVIEWS`=10 reviews, ~23.7k games)** is queued in full and first; the **low tier
     (<10, ~24.8k)** trickles in at `QNU_LOW_TRICKLE`=3000/run so it's still swept exhaustively
     without blocking the recoverable games. High tier drains in ~12 scraper-hours (~half a day).

  **REVERT PLAN (do after the backlog closes).** These knobs are tuned aggressively for the drain.
  Once null `last_update_ts` is down to steady-state churn: in `scrape.yml` raise
  `STOREFRONT_MIN_INTERVAL` back toward **~1.4–1.5** and drop `FORCE_RESERVE_FRAC` to **~0.2**;
  `queue_null_updates.py` is deletable once the queue drains (it's the current live one-off, §4).
  Watch 403 rates in the first run or two at 0.9s — if they spike, nudge `STOREFRONT_MIN_INTERVAL`
  up (it's env-overridable, no code change needed).

- **Update-events layer + last_update_ts fix + workflow renumbering (Jul 2026).** A multi-part
  overhaul of update tracking. (1) **`last_update_ts` bug fixed.** `scraper.py`'s News-API fetch
  used `maxlength=1`, so `_is_update_item()` only ever saw post *titles* — any patch whose title
  dodged the keyword list read as "no update", leaving ~42.6% of the catalog (52,371 games,
  Cyberpunk 2077 among them) with a null `last_update_ts`. Now fetches `maxlength=300`, scans the
  body, widens keywords, tightens sale exclusions. Self-heals via `last_modified` churn; a one-off
  `queue_null_updates.py` force-queues the existing nulls for immediate re-scrape. (2) **New
  event-typed update pipeline** (§9.5): `updates_refresh.py` → `updates_raw/NN.json` (sharded,
  keyed by event `gid`) reads the store events endpoint's `event_type` for a true three-tier
  major(13)/regular(14)/minor(12) classification the News API can't give; `updates_summarize.py` →
  `updates.json` rolls it into windowed big/small counts + client-recomputable date arrays. Own
  out-of-band job on the storefront budget (quiet `:53` slots), never folded into the scraper.
  Frontend merges `updates.json` as a **fallback** — `games.json`'s `last_update_ts` stays primary,
  `updates.last_any_ts` fills its nulls, precedence to flip once shard coverage is broad. (3)
  **Workflow renumbering.** All 14 workflow `name:` fields renamed to a tiered scheme
  (`1.` scraper · `2.x` refreshers · `3.x` summarizers · `4.x` monitors · `[DELETE]` dormant IGDB)
  so the Actions sidebar sorts by pipeline hierarchy. (4) **Double-run fixed.** `playtime-summary`
  / `playtime-ratings` still had live `*/4` crons *and* ran as chained steps in the playtime raw
  job — double-writing `playtime.json`/`ratings.json` each cycle. Their standalone crons were
  removed (kept `workflow_dispatch`), so the raw job is the sole scheduled trigger, names tagged
  `[2.3 / manual]`. NOTE: this corrects the prior §16 entry below, which claimed these two were
  already "retired" — they were renamed/cron-stripped now, not then. One known follow-up remains:
  `shard_health.py` still monitors only `playtime_raw/` (not `updates_raw/`). The renaming also
  broke the single `workflow_run` link that keyed off the old scrape name (`4.2 Coverage` — the
  only such trigger in the repo); that has since been fixed (see §4's Coverage callout).

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
- **Playtime scale-up: 4 → 8 slots, `STEAM_DELAY` 2.0 → 1.5s.** With `scraper.py` finishing in
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
