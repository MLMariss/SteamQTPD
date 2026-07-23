# QTPD — Roadmap & future work

The planning surface for **QTPD**: everything proposed, deferred, decided-against, or
half-shipped, in one place. Split out of `ARCHITECTURE.md` (which was ~2,100 lines, a third
of it planning material) so a planning session doesn't have to read the reference doc, and a
reference lookup doesn't have to scroll past a backlog.

**Division of labour:**

| Read this file for | Read [ARCHITECTURE.md](ARCHITECTURE.md) for |
|---|---|
| What we might build next, and what we decided not to | How the thing that exists works |
| Why an idea was parked, and what would unpark it | Job/file ownership, schemas, config, data flow |
| Whether a request has already been raised | The as-built record and §16 changelog |

**Nothing here is committed or verified.** Each item still needs feasibility confirmation
(is there a real data source?), impact assessment, and a complexity estimate before it earns
a job, a file, or a frontend control. Comments are captured anonymously; only the substance
is kept.

The load-bearing constraints any item must respect: **one writer per file** (ARCHITECTURE §1)
— a new data source is always a *new* file + a *new* job, never a change to an existing job's
file — and **static-first** (§1): anything needing a live cross-origin call routes through the
Cloudflare Worker (§12), it does not become a server.

> **Section numbering is deliberately preserved.** These were ARCHITECTURE.md §3.1–§3.4 and
> are still numbered that way, because ~22 cross-references across the docs point at `§3.1`,
> `§3.2` and `§3.4`. A citation of `§3.x` anywhere in the repo means *this file*; every other
> `§N` means ARCHITECTURE.md.

---

## Status index

Open items only — the shipped ones are annotated `[Done]` inline below and kept as a record so
they aren't re-proposed. Sorted roughly by value-to-effort within each group.

| Item | Where | Blocked on |
|---|---|---|
| Completion rate (achievement-weighted QTPD) | §3.1 | An achievement-selection heuristic ("which one means finished?") |
| Region-specific pricing | §3.1 | An explicit store-N-regions vs fetch-live design decision |
| Update-events **precedence flip** | §3.1 / §9.5 | Re-scoped — needs a per-game preference or a lower review floor, not more coverage |
| Mod support & mod count | §3.1 | Nothing; Workshop counts are retrievable |
| Co-op max player count | §3.1 | Nothing; expect partial coverage |
| Anti-cheat type | §3.1 | Encoding "invasive or not" as a judgement |
| Min-reviews: single- vs multi-select | §3.2 | **A decision from you** — the request contradicts the current deliberate design |
| Mobile progressive disclosure | §3.2 / §3.4 | Nothing; partly addressed by Grid's tap-to-expand |
| Short-link encoder for filter URLs | §3.4 | Choosing client-side compression vs a stateful Worker+KV |
| `shard_health.py` covers only `playtime_raw/` | §3.5 | Nothing — cheap add |
| `tags.json` has no timestamp or rescrape cadence | §3.5 | Needs a per-entry `scraped_at` first |
| Exact playtime staleness (per-game `scraped_at`) | §3.5 | Needs a shard-record field added |
| Scraper throughput levers (reviews-only pass) | §3.5 | Not needed today; documented for when it is |
| Uniform-refresh review (dormant lanes) | §3.5 | Waiting for the fill frontier to fully drain |
| isthereanydeal · sequel graph · dev-team grouping · buzz metric · trading cards · studio health | §3.3 | Source feasibility, per item |

Open items living in **other** docs (not duplicated here — go to the source):

- **PICS pipeline** — `PICS_METADATA_PIPELINE.md` §10 *Still open / deferred*: `aicontenttype="3"`
  handling, `exfgls` reason codes, Deck `tests[]` detail, the deferred `supported_languages`
  lean-list collapse.
- **PICS for unreleased games** — `UPCOMING_GAMES_PICS_MEMO.md`: parked in full, with the
  analysis preserved so the decision can be picked up without re-deriving it.
- **Coverage/refresh gaps** — `COVERAGE.md` *Future work* (generated, so always current).

---

## 3. Community feedback & future work (moved from ARCHITECTURE.md)

Items are grouped by the part of the system they touch. See the intro above for the
constraints every item must respect, and the status index for what is actually open.

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
  *Still open, but the framing has changed — see §9.5 for the full analysis.* The **precedence
  switch** was to flip the event layer from fallback to primary "once shard coverage is broad
  enough." Shard rotation is **complete** (64/64 populated) and the raw layer has reached its
  ceiling, so that trigger has been met — yet the flip is still wrong as originally specified.
  `updates_refresh.py` gates on `MIN_REVIEWS_FLOOR = 10`, so ~45k sub-floor games are never
  event-scraped at all; `updates.json` covers **41.1%** against the floor-free News API's
  **61.7%**, and it currently fills only **886** of the News API's nulls. A blanket flip would
  therefore *lose* recency on ~25k games. **Revised trigger:** either make the preference
  **per-game** (use `last_any_ts` where present, else `last_update_ts`) — which is strictly an
  improvement and needs no coverage gate at all — or drop the updates review floor first and
  re-measure. **Live figures are in `COVERAGE.md` (Axis 1, `updates.json` / `updates_raw/`
  rows) — read those rather than any hand-written count here.**

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
  *(That control has since been superseded: `#mobileSort` is now hidden at every width and
  mobile sorting lives on the summary line's `sorted by …` chip, with desktop Grid getting its
  own `#gridSort` — §3.4 R6, §11. The card layout itself is also no longer the mobile default;
  **Grid** is.)* *Still open (optional):* progressive disclosure — folding secondary fields
  behind a per-card tap (partly addressed by Grid's tap-to-expand). *Known caveat:* CSS `order`
  reorders visually only, so screen-reader / tab order still follows the table's DOM column
  order.

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
  into Quality → 3; the PICS work then added **Flags** → **4 sections**, `SECTION_DEFAULT =
  {value, quality, flags, tags}`):
  - **Value** — QTPD price basis, HLTB metric, HLTB data, price range, **price type**
    (All / Full / Sale / Free — a multi-toggle that replaced the old on-sale-only checkbox,
    URL `pc`), QTPD range.
  - **Quality** — min rating, reviews-sort, review trend, min reviews, updated-within,
    playtime-sort. (Named "Quality & Activity" when R3 merged the two; later shortened to
    just **Quality** — §9.6.)
  - **Flags** — the PICS cluster: six tri-state presence flags + Controller and Steam Deck
    (§9.6). Added after this round; folded by default.
  - **Tags** — click-cycle legend, tag rail, and the **ALL/ANY match toggle** (see L-tags below).
  - *Decisions:* open/closed state **persists in `localStorage["qtpd.sections"]`** (not the URL —
    keeps shared links clean). **Defaults:** Value + Quality open, **Tags and Flags folded**. The planned
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
  - **localStorage keys (3):** `qtpd.view`, `qtpd.sections` (JSON
    `{value,quality,flags,tags:bool}`), and `qtpd.tagsCollapsed` (`"1"`/`"0"` — the
    Tags-*column* collapse, §11, unrelated to the Tags filter *section*).
  - **Body classes** (set by `applyLayout()` + inline FOUC script): `layout-card`, `grid-view`,
    `narrow`, plus `tags-collapsed` (§11) and `nav-scrolled` (R6). Breakpoint
    `matchMedia("(max-width:1374px)")`; phone tier `@media (max-width:560px)`.
  - **URL params:** existing set + **`tagmode`**, and (from the PICS work, §9.6) **`pc`,
    `flags`, `noflags`, `ai`, `ctrl`, `deck`, `adult`. The old boolean `sale` param is gone**,
    superseded by `pc`. Full current list in §11 *State in the URL*
    (`syncURL`/`loadFromURL`).
  - **Key functions:** view — `applyLayout` / `setView` / `updateViewButtons` / `gridCardHTML`
    (R5's `bestRating` rating-fallback helper was **removed** by R6 when the grid card went
    back to the plain Steam all-time %);
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

### 3.5 Loose ends noted elsewhere in the reference doc

Small, concrete items that were recorded inline where they were discovered rather than in the
backlog, and so were easy to lose. Collected here; each site in ARCHITECTURE.md now carries a
pointer back to this section.

- **`shard_health.py` monitors only `playtime_raw/`.** There are now four 64-way shard sets
  (`playtime_raw/`, `updates_raw/`, `pics_raw/`, `pics/`) but only the first is size-watched
  against GitHub's 100 MB limit. The others are far lighter — `updates_raw/` stores timestamps,
  not review objects — so this is pre-emptive rather than urgent. *Cheap add: point the existing
  script at the other three directories.* (ARCHITECTURE §9.5, §15.)

- **`tags.json` has no per-entry timestamp and no rescrape cadence.** Tags are fetched once and
  left. They do drift (Steam user-tag votes shift, new tags appear), so a light periodic
  re-check — weekly/monthly, oldest-first, small budget — is worth adding. **Ordering matters:**
  a per-entry `scraped_at` has to land in `tags.json` first, after which tags can join
  `COVERAGE.md` Axis 2 as a proper row instead of being Axis-1-only. (ARCHITECTURE §11.5;
  also in `COVERAGE.md` *Future work*.)

- **Playtime staleness is a proxy, not a measurement.** `playtime_raw/` stores a timestamp per
  *review*, not per *game*, so `coverage.py` uses each game's newest review `ts` as a staleness
  proxy. A game with no recent reviews reads as overdue even if freshly walked, which is why its
  `overdue` figure is reported as an **upper bound** (marked `†`). *Fix: add a per-game
  `scraped_at` to the shard records — `updates_raw/` already has one and reports exact overdue.*
  (ARCHITECTURE §11.5.)

- **Scraper throughput levers, if the review ladder ever needs to go faster.** Roughly a further
  2× fits inside the daily window before the scraper becomes the constraint — but the binding
  limit past that is Steam's ~200/5min per-IP soft limit and the resulting 403 cooldowns, not
  the clock, so **403 rates are the number to watch, not run duration**. Two levers, in order of
  preference:
  1. **A reviews-only pass** — skip `appdetails`, 1 call/game instead of 2, halving the cost of
     the whole ladder. Needs a second per-game timestamp in `games.json` (reusing `scraped_at`
     would let a light pass mask a genuine `last_modified` refresh), plus matching `coverage.py`
     and doc updates.
  2. **Checkpoint granularity** — below a 6h cooldown, `CHECKPOINT_SECONDS` (10 min) becomes the
     real floor, since that is how often `requeue_due_young()` runs.

  *Neither is needed today.* (ARCHITECTURE §14.)

- **Uniform-refresh review.** Once the fill frontier fully drains, revisit whether the dormant
  30–45d cooldowns should tighten toward a flatter, faster whole-catalog refresh. Today's split
  (active 4–7d / dormant 30–45d) deliberately front-loads budget onto games whose data actually
  moves; a uniform target only makes sense once no backlog is competing for that budget.
  (`COVERAGE.md` *Future work*.)

- **IGDB revival (decided against, but the path is recorded).** Phase C was built, evaluated and
  retired — IGDB's `game_time_to_beats` table holds only ~8,829 records total, yielding 1,471
  matches (1,007 net-new), too small a ceiling for a standing job. The files remain in-repo and
  unreferenced. **Do not revive without a reason to expect that ceiling has moved.** The full
  record, the two bugs fixed during bring-up, and the four concrete revival steps are in
  ARCHITECTURE §8.1 — deliberately left there, since it documents a retirement rather than a
  plan.

---
