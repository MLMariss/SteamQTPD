# SteamQHPP — Architecture & Maintenance Guide

A complete technical reference for the SteamQHPP project: what every file does, how
the pieces fit together, why the architecture is shaped the way it is, and how to
operate and extend it.

> This is the deep-dive companion to `README.md`. The README is the short "what is
> this / how do I set it up" intro; this document is the engineering reference for
> anyone (including future-you) maintaining or extending the system.

> **Numbers in this document are dated snapshots, not invariants.** Game counts,
> HLTB resolution rates, and similar figures grow run to run; each is tagged with the
> date it was captured (most recently **2026-06-30**). Treat them as orders of
> magnitude, not current truth — re-measure before relying on a specific value.

> ## ⏱️ Start here if you're returning to this project
>
> **§2 (Work Tracker) is the single source of truth for "where did I leave off."** It
> lists every known fix, bug, and deferred improvement as a discrete task with a status,
> a short description, and a `(§#)` pointer into the relevant deep-dive section. You can
> read §2 **alone**, without the rest of the document, decide what to pick up, and jump
> straight to the section it references.
>
> **RULE — keep §2 current.** Whenever you finish, add, or re-scope a task, **update
> §2 in the same change**: flip its status, strike it through when done, or add a new
> task entry. Every task line must carry a `(§#)` reference to wherever the supporting
> detail lives. §2 is only useful as a resumption point if it never goes stale — treat
> updating it as part of "done," not an afterthought. The dated snapshots elsewhere in
> the doc follow the same spirit (see the numbers caveat above).

---

## 1. What this is

SteamQHPP ranks Steam games by **QHPP — Quality Hours Per Price**:

```
QHPP = (average HowLongToBeat hours × rating %) ÷ price
```

Higher QHPP = more quality-adjusted playtime per dollar. The idea is to surface games
that give a lot of well-reviewed play time for the money, and to make that browsable,
sortable, and filterable.

It runs **entirely on GitHub Pages + GitHub Actions** — no server, no database, no
proxy. A set of scheduled Actions scrape Steam (and HowLongToBeat, and SteamSpy)
server-side and commit the results to the repo as JSON. A static `index.html` reads
those JSON files in the browser and does all the merging, ranking, and filtering
client-side. Cost is $0: Actions is free and unlimited on public repos, and Pages
hosts the static site for free.

The frontend **never calls Steam directly** — it can't, because Steam sends no CORS
headers — so every network fetch happens inside an Action, and the browser only ever
reads the committed JSON.

---

## 2. Work tracker — known fixes, bugs & deferred improvements

**This section is the resumption point for the whole project.** Read it on its own to
see what's outstanding, then jump to the `(§#)` reference for detail. Keep it updated as
part of any change (see the rule in the intro).

**Status key:** 🔴 not started · 🟡 in progress · 🟢 done (kept for history) ·
🔵 deferred by design (no action needed yet).

Open tasks are grouped by priority tier (§2.1 Immediate → §2.2 Improvements → §2.3
Nice-to-haves) and numbered by that priority; **completed work lives in §2.4 (history)
and keeps its original numbers.** When a new task is added or one is resolved, the OPEN
task numbers are renumbered by priority so the highest-priority open item is always the
lowest open T-number. Rough priority order within each tier is top-to-bottom.

### 2.1 Immediate — needed now

- **🔴 T7 — Filter panel is too tall and the sticky-header scroll behavior is broken.**
  On a 1440p screen the filter panel fills the *entire* viewport — the game table is
  pushed fully below the fold and never visible without scrolling; on smaller screens it's
  unusable. Root cause: the tag rail renders **every** tag above `MIN_TAG_COUNT` (≈150
  chips across ~40 rows), and the sticky `.topbar` collapse logic interacts badly with
  that height. Distinct sub-problems, all around the top nav:
  - **(1) Unusable at <1440p / panel too tall.** The whole filter block (basis, HLTB,
    rating, reviews, price, range slider, trend, updated-within, *and* the giant tag rail)
    is one sticky unit taller than most viewports.
  - **(2) Scroll skips the unseen filters.** The panel is `position:sticky` and the *first*
    scroll immediately toggles `.compact` (which `display:none`s all `.bar-filters`), so
    the middle filters that were below the fold are never seen — scrolling jumps straight
    from the top to the game table, skipping everything in between.
  - **(3) Sticky table column/sort header — CONFIRMED WANTED.** Scrolling back up from the
    table, the moment the viewport touches the sticky topbar it re-expands the full nav —
    there's no stop at the *end of the table* where the column sort headers are usable
    without the nav eating the screen. **Fix: give the table's column/sort header its own
    `position:sticky; top:0`** so the sort controls stick and stay usable on scroll-up.
    (User confirmed 2026-07-01 this is a definite requirement, not just a proposal.)
  - **(4) Full-nav reopen must be a deliberate second step.** Only after the table + its
    sticky column heads are settled should a further, intentional scroll-up (or a button)
    open the full filter nav — not on first upward contact. Simplest robust option:
    filters reopen ONLY via the "Show filters" button; scroll only ever collapses.
  - **(5) Tag groups: top-10 foundation + "+ more" overflow, BOTH alphabetical.** Trim
    each group (SKIP Players & Mode — already short) to a **foundation row of its 10
    most-USED tags** (which tags qualify is decided by full-corpus count), but **display
    those 10 alphabetically among themselves** as a stable row that never reshuffles.
    A per-group **"+ more"** expander reveals the group's remaining (less-used) tags as a
    **separate lower tier, sorted alphabetically among themselves.** Two independent
    alphabetical lists (foundation + overflow) — NOT one big count-ranked list that
    reshuffles. The count ranking only decides *membership* of the top-10; ordering within
    each tier stays alphabetical so chips don't jump around.
  - **(6) Per-page options → 100 / 500 / 2000** (currently 50 / 100 / 200); default stays
    100. Trivial markup change (existing handler reads `data-size`). Watch: 2000 rows in
    first paint may be heavy on mobile — test.
  - **(7) General audit of the sticky-header behavior** — other findings and suggestions
    (sticky-header/scroll-latch, `scroll-margin-top`, filters-collapsed-by-default, mobile
    treatment) are in §14 → T7.
  (Frontend: §5.7; scroll/collapse + `buildTagRail` logic in `index.html`. Detail: §14 → T7.)

### 2.2 Improvements — real engineering effort, not yet urgent

- **🔵 T8 — Load-time / scaling ceiling as the dataset grows.** The frontend fetches the
  **entire `games.json` (~12 MB at ~23k games) plus four more JSON layers up front**, on
  every page load, with `cache:"no-store"` — then merges and renders client-side. That's
  fine now, but it scales linearly: at the full ~90k-game catalog `games.json` alone is
  headed toward ~45 MB, which is a real mobile-data and parse-time cost, and `no-store`
  defeats browser caching so it re-downloads every visit. Deferred (not yet a problem),
  but the eventual fixes, roughly in order of effort: **(1)** drop `no-store` in favor of
  a cache-busting query param or `ETag`/`Cache-Control` so repeat visits are cheap;
  **(2)** ship a slimmer pre-merged/precomputed index (server-side merge in a build step
  so the browser downloads one compact file instead of five and doesn't merge at all);
  **(3)** gzip/precompress or move large layers to a columnar/binary format; **(4)** the
  big one — **shard `games.json`** (e.g. by appid range or by a pre-sorted QHPP page) and
  **load shards on demand / paginate server-side**, so the browser only pulls what it
  renders. The infinite-scroll UI already only *shows* a page at a time; the data layer
  just doesn't match that yet. (Frontend: §5.7; caveats: §13. Detail: §14 → T8.)

### 2.3 Nice-to-have — optional, do if convenient

- **🔵 T9 — Factor out the duplicated `get()` and `git_checkpoint` helpers.** Every
  scraping script reimplements the same HTTP retry/backoff and push-retry logic.
  This is currently a deliberate "each job is a self-contained single file" trade. Only
  worth doing if a contract needs to change in lockstep across jobs; weigh against the
  simplicity the duplication buys. (Resilience: §9. Detail: §14 → T9.)

- **🔵 T10 — Back up / version the Cloudflare Worker in-repo.** The wishlist proxy (§5.9)
  is the only component whose source isn't in this repo, so if it's lost there's nothing
  checked in to redeploy from. Consider committing the Worker script + `wrangler.toml`
  into a `worker/` directory here (even though it deploys separately) so the whole system
  is reconstructable from one repo. (Worker: §5.9. Detail: §14 → T10.)

### 2.4 Completed — done, kept for history

These keep their original T-numbers (they are NOT renumbered when open tasks shift).

- **🟢 ~~T1 — Remove the dead `cursor` field from `catalog.json`.~~** A relic of the
  pre-`GetAppList` search-pagination scraper. No current code read or wrote it; it
  survived only because `save_catalog` round-trips unknown keys. **Done (2026-06-30):**
  key dropped from the committed `catalog.json` (no code change needed — nothing
  referenced it). Zero risk. (Schema: §10. Detail: §14 → T1.)

- **🟢 ~~T2 — Purge the orphaned `sales_refresh.py` / `sales.json` references.~~**
  `price_and_sale.py` replaced the old `sales_refresh.py`, and nothing wrote
  `sales.json` anymore, but stale comments still pointed to the non-existent file: in
  `scraper.py` (the `# NOTE: sale end-dates …` block near the per-game data sources, and
  the `# discount_end is no longer scraped here …` comment inside `build_record`) and in
  `price_and_sale.py`'s own header. **Done (2026-06-30):** all four comment references
  rewritten to point to `price_and_sale.py`/`prices.json` (or had the dead-file mention
  dropped), `sales.json` deleted from the repo, and its now-defunct §4 table row and §10
  schema entry removed. Verified no `sales_refresh`/`sales.json` references remain in any
  `.py`/`.yml`; both scripts still compile. Comment-and-file-deletion only — no behavior
  change. (Ownership: §4. Detail: §14 → T2.)

- **🟢 ~~T3 — Delete the spent one-off scripts + workflows.~~** `hltb_backfill.py` +
  `backfill-hltb.yml` and `backfill_updates.py` + `backfill.yml` had already run and
  served their purpose. **Done (2026-06-30):** all four deleted. `hltb_estimate.py` kept
  (the live `hltb_refresh.py` imports it). Verified nothing else imports the removed
  scripts and no surviving workflow references them; 5 recurring workflows remain.
  (Scripts: §5.8; workflows: §9. Detail: §14 → T3.)

- **🟢 ~~T4 — Bump `backfill-hltb.yml` action versions.~~** It pinned `checkout@v4` /
  `setup-python@v5` while every recurring job is on `@v5` / `@v6`. **Resolved by T3:**
  the workflow was deleted outright (2026-06-30), so there's nothing left to bump.
  (Workflows: §9. Detail: §14 → T4.)

- **🟢 ~~T5 — HLTB coverage: re-scrape pass + matching decision.~~** Of
  ~19.5k HLTB entries (2026-06-30: 656 full-real, 1,194 partial, 17,646 blank), most blanks
  are genuine no-matches — HLTB has no page for most of Steam's long tail, so this is largely
  irreducible. **Part (a) re-scrape pass — done (2026-06-30):** `hltb_refresh.py` now re-fetches
  existing entries with leftover budget once the first pass is complete, in priority order
  partial → blank → full (oldest `fetched_at` first within each), gated by per-bucket staleness
  windows (`RESCRAPE_PARTIAL_DAYS=14` / `RESCRAPE_BLANK_DAYS=60` / `RESCRAPE_FULL_DAYS=365`) so
  no title is re-hit every run. Re-fetch reuses the same `hltb_for`→`make_entry` path, so the
  `raw`/overwrite model merges cleanly — real values overwrite, estimates recompute, and a
  transient error leaves the existing entry untouched (never overwrites good data with a blank).
  **Part (b) better matching — decided against (2026-06-30):** `HLTB_MIN_SIMILARITY` stays at
  **0.65** by choice. Loosening the threshold or adding fuzzy fallbacks risks pulling in *wrong*
  games that silently corrupt data, and there's no ground truth to validate matches against. A
  tight match that yields a blank is safer than a loose one that yields a wrong answer — so the
  remaining no-matches are accepted as the correct trade for now. (§5.3 no-match reality; §8 raw
  model. Detail: §14 → T5.)

- **🟢 ~~T6 — Missing / broken thumbnails, especially for adult-content games.~~**
  Thumbnails are derived 100% client-side from the appid (`capsule_231x87.jpg`, falling
  back to `header.jpg`) — the scraper stores no image field. Two problems were: **(a)** the
  old `onerror` was one-shot (`this.onerror=null`), so a double-404 left a broken-image
  icon; and it didn't catch Steam's **200-with-blank-image** case (CDN sometimes serves a
  degenerate image instead of a clean 404, so `onerror` never fires). **(b)** adult/
  sexually-tagged games render their real art with no maturity treatment. **Done
  (2026-07-01), pure `index.html` change, no scraper/data change:** (1) a **content-verified
  fallback chain** (`stepThumb`) advances capsule -> header on *both* a hard error and a
  "loaded but `naturalWidth <= 1`" result, catching the blank-200 case; header is the
  terminal fallback; wired to the table thumb *and* the hover popup (which previously had
  no error handling at all). (2) adult-tagged games (tags intersect {Nudity, Sexual Content,
  Mature, NSFW, Hentai} — 61 in the current `tags.json`) now render the real art under a
  **permanent CSS blur + centered "18+" badge**. Deliberately kept the deterministic
  capsule/header chain (no per-game screenshot URL) to avoid a scraper field + ~2 MB
  `games.json` growth (the T8 scaling concern) — screenshot fallback is parked. Blur is
  permanent (no click-to-reveal) until a real age-verification gate exists. (Frontend:
  §5.7. Detail: §14 → T6.)

> **§14 (Future work — detail)** holds the longer-form rationale for the engineering
> tasks above. This tracker is the index; §14 is the reference. **Numbering note:** §14
> detail headers are keyed to the CURRENT open-task numbers; when open tasks are
> renumbered, the §14 headers move with them. Completed-task detail (T1–T6) is stable.
---

## 3. The core architectural principle: ONE WRITER PER FILE

This is the single most important thing to understand about the codebase. **Every
data file is owned by exactly one job. No two jobs ever write the same file.**

### Why it exists

Multiple GitHub Actions run on independent schedules and all need to commit to the
same repo on the `main` branch. If two of them wrote the *same* file, their commits
would collide on push — one would be rejected, and a naive retry could clobber the
other's work or get stuck in a rebase conflict. This actually happened early in the
project (detached-HEAD states and race conditions across concurrent workflows) and
was painful to debug.

The fix is structural rather than defensive: if jobs write **different** files, a
`git pull --rebase` before every push *always* applies cleanly, because there are no
overlapping changes to reconcile. Concurrent commits to disjoint files merge
automatically.

### How it's enforced

Each data layer lives in its own file with a single owner:

| File           | Sole writer          | Contents                                                        |
|----------------|----------------------|-----------------------------------------------------------------|
| `games.json`   | `scraper.py`         | Catalog: title, URL, rating %, review count, release date, genre fallback, `last_update_ts`. Also a base `price_*`/`discount_pct` snapshot from the scrape. |
| `catalog.json` | `scraper.py`         | Scraper state: pending (waiting-room), skip-list, seeds ledger, force-refresh list, last sync. |
| `prices.json`  | `price_and_sale.py`  | The fast-changing pricing layer: `price_initial`, `price_final`, `discount_pct`, `discount_end`, `scraped_at`. **Source of truth for price.** |
| `hltb.json`    | `hltb_refresh.py`    | Static HowLongToBeat completion times + estimated fills.        |
| `tags.json`    | `tags_refresh.py`    | SteamSpy user tags per appid.                                   |
| `recent.json`  | `recent_refresh.py`  | 30-day rolling "recent reviews" score per appid.               |
| `seeds.txt`    | **human only**       | Optional priority appids/terms/URLs. The scraper *reads* it but **never writes** it — preserving one-writer-per-file (see §6). |
| `seeds_log.txt`| `scraper.py`         | Append-only audit of seed reconciliations.                     |

The **frontend merges all of these by appid at load time** and computes QHPP from the
merged record. QHPP is never stored server-side — it's derived in the browser from
whatever the current merge produces, so changing the formula or the chosen HLTB
metric is instant and requires no re-scrape.

> **Rule for any future change:** if you add a new data source, give it a **new file
> and a new owner**. Never add a second writer to an existing file. The one exception
> is a logically atomic fact owned together — e.g. price + discount % + sale end live
> in `prices.json` because they're "what does this cost right now" and must update
> together; splitting them would let two jobs disagree about whether a game is on sale.

---

## 4. The pipeline at a glance

```
                         ┌────────────────────────────────────────────┐
                         │            GitHub Actions (cron)            │
                         └────────────────────────────────────────────┘

  scraper.py ─────────────► games.json   ┐
  (catalog, rating, release)             │
  catalog.json (state)                   │
                                         │
  price_and_sale.py ──────► prices.json  │
  (price, discount, sale end)            │     all merged by appid,
                                         ├──►  client-side, in the browser,
  hltb_refresh.py ────────► hltb.json    │     by index.html  →  QHPP computed
  (completion times + est)               │     via computeQ()
                                         │
  tags_refresh.py ────────► tags.json    │
  (SteamSpy user tags)                   │
                                         │
  recent_refresh.py ──────► recent.json  ┘
  (30-day review trend)

                                  │
                                  ▼
                         GitHub Pages serves
                         index.html + *.json
                                  │
                                  ▼
                            User's browser ──────► Cloudflare Worker ──► Steam
                                                   (wishlist proxy,        (server-side,
                                                    §5.9 — the ONLY          no CORS)
                                                    live Steam call
                                                    the browser makes)
```

Each job reads `games.json` **read-only** to learn which appids exist, then writes
only its own file. The main scraper is the only thing that discovers *new* games; the
refreshers enrich games the scraper has already found.

> **Two pieces of server-side infrastructure, not one.** Besides the GitHub Actions,
> there is a small **Cloudflare Worker** that proxies Steam wishlist lookups for the
> frontend's "import my wishlist" feature (§5.9). It's the single exception to "the
> browser never calls Steam" — and it's the only part of the system that lives outside
> this repo.

---

## 5. File-by-file reference

### 5.1 `scraper.py` — the catalog accumulator

The heart of the system. Builds up `games.json` over many runs and owns the scraper
state in `catalog.json`.

**What each run does:**

1. **Enumerates the universe** via Steam's `IStoreService/GetAppList` (needs a free
   Web API key) — a clean, games-only, appid-ordered list with a per-app
   `last_modified` timestamp. Falls back to the keyless `ISteamApps/GetAppList/v2`
   if no key is set (lists all app types, no change timestamps).

2. **Reconciles seeds** (see §6) — pulls priority appids from `seeds.txt` into the
   work queue.

3. **Selects work** in priority order (`select_work`):
   - **Refresh** stored games whose `last_modified` moved past when we last scraped
     them, or that are flagged in `force_refresh`.
   - **Promote** games from the pending waiting-room whose release date has now passed.
   - **Priority** seeds from the ledger.
   - **New frontier** — games never seen yet, newest-appid-first (`NEW_ORDER`).

4. **Builds each record** (`build_record`): fetches `appdetails` (price, release,
   genre fallback), `appreviews` (rating %, review count), and the News API
   (`last_update_ts`). Reviews and news are fired concurrently via a thread pool to
   cut per-game latency. **HLTB and SteamSpy tags are NOT fetched here** — they were
   the per-game bottleneck and now live in their own jobs.

5. **Runs on a time budget** (`RUN_MINUTES`, default 180) and **git-commits progress
   every ~10 minutes** (`CHECKPOINT_SECONDS`), so hitting the 6-hour Actions wall
   never loses work. It stops `TIME_BUFFER` seconds before the budget to commit
   cleanly.

**Released-only with a waiting room:** only games with a concrete past release date
are stored as real records. Unreleased games go to `catalog["pending"]` (one cheap
`appdetails` probe) and are promoted automatically once their release date passes.
Nothing is ever permanently skipped for being unreleased.

**Key config (top of file):**

| Constant             | Default     | Meaning                                                       |
|----------------------|-------------|---------------------------------------------------------------|
| `RUN_MINUTES` (env)  | 180         | Scrape budget per run.                                         |
| `STEAM_DELAY`        | 1.5 s       | Between storefront calls (~200 req / 5 min per IP, shared by appdetails + appreviews). |
| `WEBAPI_DELAY`       | 1.0 s       | Between GetAppList pages.                                      |
| `NEWS_DELAY`         | 0.3 s       | Between News API calls (huge separate budget).                |
| `CHECKPOINT_SECONDS` | 600         | Commit progress at least this often.                          |
| `NEW_ORDER`          | `"newest"`  | New-coverage order: newest appid first, or `"oldest"`.        |
| `REFRESH_DAYS`       | 7           | Fallback refresh age, only when no API key (no `last_modified`). |
| `SEED_RESOLVE_TTL`   | 24 h        | Live term/URL seeds re-resolve at most once per day.          |

**Rate-limit note:** Steam's storefront is ~200 requests per 5 minutes per IP, and
that budget is **shared** between `appdetails` and `appreviews`. Don't lower
`STEAM_DELAY` much or you'll trigger 429s and a 5-minute 403 cooldown. The News API
(`api.steampowered.com`) is a *separate*, much larger budget, which is why update
detection is cheap.

**Per-game cost model (why the scrape paces the way it does):** each *released* game
costs a fixed set of calls, split across two independent rate budgets:

| Call            | Endpoint                              | Budget                  | Notes                                  |
|-----------------|---------------------------------------|-------------------------|----------------------------------------|
| `appdetails`    | `store.steampowered.com`              | storefront (~200/5 min) | Price, release, genre fallback, type.  |
| `appreviews`    | `store.steampowered.com`              | storefront (same pool)  | Rating %, review count. **Concurrent** with news. |
| News API        | `api.steampowered.com/ISteamNews`     | separate, huge          | `last_update_ts`. **Concurrent** with reviews. |

So the binding constraint is **2 storefront calls per game** (appdetails + appreviews)
against the shared ~200/5min pool ⇒ a hard ceiling around ~1,000–1,200 games/hour, and
measured throughput of **~13 games/min** once HLTB/SteamSpy were pulled out (those were
multi-second blocking calls that previously dominated — see §12). An *unreleased* game
costs just the single `appdetails` probe before being filed to the waiting room.

**Update detection is a heuristic, by necessity.** Steam's public News feed doesn't
cleanly tag which posts are patches vs. sale announcements, so `_is_update_item`
classifies them: the `patchnotes` tag is the strong signal; a keyword allow-list
(`_UPDATE_WORDS`: update, patch, hotfix, changelog, version, bug fix, balance, …) is
the fallback; and a block-list (`_NOT_UPDATE`: sale, discount, wishlist, launch,
trailer, …) suppresses obvious non-updates. It's deliberately good-enough, not exact —
`last_update_ts` drives refresh-priority and the recent-review queue, where occasional
mislabeling is harmless. (This logic was once duplicated in `backfill_updates.py` so
backfilled values matched live ones; that one-off was removed in §2 → T3, so the live
heuristic in `scraper.py` is now the sole copy.)

### 5.2 `price_and_sale.py` — the pricing layer

Owns `prices.json`: current price, discount %, and sale end-date — the fast-changing
"what does this cost right now" facts. The main scraper does **not** write these (they
change far more often than the catalog, and isolating them keeps the slow scrape lean
and collision-free).

**Two cheap endpoints, both batched:**

1. **Prices** — `appdetails?filters=price_overview&appids=<CSV>`. This is the *one*
   `appdetails` variant Valve still lets you **batch**: pass many comma-separated
   appids, get `price_overview` for all of them in a single call. (Full `appdetails`
   has been one-appid-only since 2015; price-only is the exception.) So the entire
   ~17k-game priced catalog refreshes in `ceil(N / PRICE_BATCH)` calls instead of N.

2. **Sale end dates** — `IStoreBrowseService/GetItems/v1` (batched), reading
   `best_purchase_option.active_discounts[].discount_end_date`. Only queried for the
   subset that came back on sale in step 1, so it's tiny.

`discount_end` is null unless the game is on sale with a dated end. Expired sales are
pruned (and the frontend also collapses past-due sales offline — see §7).

**Key config:** `PRICE_BATCH = 100` (appids per price call), `GETITEMS_BATCH = 50`
(appids per sale-date call), `STORE_DELAY = 1.6 s`, `GETITEMS_DELAY = 1.2 s`,
`RUN_MINUTES = 60`. Currency via `QHPP_CC` env (default `US` → USD).

### 5.3 `hltb_refresh.py` — HowLongToBeat completion times

Owns `hltb.json`. Fetches each game's main / main+extras / completionist times from
howlongtobeat.com **once**, since completion times are static. This was historically
the slowest part of the whole pipeline (2–10 s per game, sometimes hanging), which is
why it was pulled out of the main scraper into its own slow background job.

It fetches never-resolved games first (`appid not in hltb`), recording a genuine
no-match as a blank entry so it doesn't re-search it every run. Once that first pass is
done, spare budget drives a **re-scrape pass** over existing entries (partial → blank →
full, oldest-first, on per-bucket staleness windows — see §2 → T5) to recover data HLTB
added later. It hits howlongtobeat.com, **not** Steam, so it doesn't compete for the
storefront rate budget — safe to run near-continuously.

**The no-match rate is high, and that's expected — not a bug.** As of 2026-06-30,
~17.7k of ~23.2k games had an HLTB entry, and of those entries **~16.0k (≈90%) are
genuine no-matches** (HLTB simply has no page for that title), leaving ~1.7k resolved
with real data. The catalog is dominated by obscure/niche games that were never added
to HowLongToBeat; a 90% blank rate reflects Steam's long tail, not a matching failure.
The handful of recoverable misses (oddly-named games HLTB *does* have under a different
title) are what a future re-scrape pass targets (§2 → T5; detail §14).

**The estimation layer (added later — see §8 for the full design):** when HLTB only
has 1 or 2 of the 3 times, the missing ones are filled from the genre-average ratio
between the three, so the `avg` (which QHPP rides on) isn't skewed. Estimation logic
lives in the shared `hltb_estimate.py` module.

**Key config:** `HLTB_DELAY = 0.6 s`, `HLTB_MIN_SIMILARITY = 0.65` (title-match
threshold), `RUN_MINUTES = 120`, `CHECKPOINT_SECONDS = 300`.

### 5.4 `hltb_estimate.py` — shared HLTB estimation logic

Not a job — a **shared module** imported by `hltb_refresh.py` (live, for new games). It
was also used by the one-time `hltb_backfill.py` sweep (since removed, §2 → T3); keeping
the fill logic in one module means the live path and any future re-sweep can never
disagree. Full design in §8.

### 5.5 `tags_refresh.py` — SteamSpy user tags

Owns `tags.json`. SteamSpy was the slowest call left in the main scrape loop (~3–4 s
even without erroring), so it moved to its own job. Tags are effectively static (they
drift slowly as users vote), so each game is fetched once then left alone. If SteamSpy
has no tags for a game, the frontend falls back to the Steam store "genres" the main
scraper still records on the game record — so tags are never blank.

**Key config:** `TOP_TAGS = 8`, `STEAMSPY_DELAY = 1.1 s` (SteamSpy asks for ~1 req/sec),
`RUN_MINUTES = 120`.

### 5.6 `recent_refresh.py` — recent-review trend

Owns `recent.json`: each game's *recent* (last-30-day) Steam review score, so the
frontend can show a recent-vs-all-time trend (improving / stable / declining).

The recent score is a 30-day rolling window, so it drifts daily even with no new
reviews — and we can't keep ~90k games perfectly fresh within the rate limit. So it
spends calls where reviews are actually likely to be moving:

- **Cooldown** (`RECENT_COOLDOWN_DAYS = 4`): never re-check a score younger than this.
- **Update-priority:** recently *patched* games (from `last_update_ts`) jump the queue
  — a patch is exactly when reviews swing. (`UPDATE_ACTIVE_DAYS = 90`.)
- **No-update games** get a much longer cooldown (`NOUPDATE_COOLDOWN_DAYS = 30`),
  checked rarely but never skipped forever.
- **Low-volume de-prioritized:** games with `< RECENT_MIN_COUNT` (10) recent reviews
  are noisy, so they sink in the queue.
- **Oldest-first tiebreak** so everything eventually refreshes.

It reproduces Steam's exact "Recent Reviews" definition from the public
`appreviewhistogram` endpoint (summing daily up/down buckets over the trailing 30
days), shown only once a game is `MIN_AGE_DAYS = 45` old — so the number matches the
store page with no fragile HTML scraping.

### 5.7 `index.html` — the frontend

A single static page (~1,400 lines, no build step, no framework) that:

1. **Fetches all the JSON layers** (`games.json`, `prices.json`, `hltb.json`,
   `tags.json`, `recent.json`) with `cache: no-store`.
2. **Merges them by appid** into one game object per game (`game.hltb_main`,
   `game.price_final`, `game.recent_pct`, etc.). `prices.json` overrides the base
   price snapshot in `games.json`; `tags.json` overrides the genre fallback.
3. **Computes QHPP client-side** via `computeQ(g, basis)` from the merged fields,
   using whichever HLTB metric is selected (`hoursFor`) and whichever price basis
   (before/after discount).
4. **Expires ended sales offline** (`expireSaleIfEnded`) — a sale whose `discount_end`
   has passed collapses to base price with no scraping.
5. **Renders, sorts, filters, paginates** with infinite scroll.

**Frontend state defaults:** sort by QHPP descending, after-discount price basis,
`avg` HLTB metric, min rating any (`minScore:0`), **min reviews 100**, page size 100,
trend filter "any", updated-within "any". All filter/sort state is reflected in the URL
so views are shareable.

**Filters:** title search · on-sale-only · min rating (any/70+/80+/90+) · min review
count · max price · tag click-to-filter · **recent-vs-all-time trend** (improving /
declining) · **updated-within** (recently-patched games) · QHPP log-scale range slider
(auto-fits the current result set) · sort by any column.

**Recent-vs-all-time trend** (`gTrend`): simply `recent_pct − rating_pct` — a positive
delta means the game is reviewing *better* lately than its lifetime average (improving),
negative means worse (declining). Null when either score is missing. This is what the
trend filter and the trend column key off.

**Steam capsule images** come straight from Steam's CDN
(`cdn.cloudflare.steamstatic.com/steam/apps/<appid>/...`) — no image data is scraped or
stored; the browser loads them directly by appid.

### 5.8 One-off / maintenance scripts

These are run-once utilities (idempotent — safe to re-run; they no-op on clean data):

- **`cleanup_shells.py`** — removes "empty shell" entries (games scraped while still
  unreleased, carrying no real data) from `games.json`, filing them back into
  `catalog["pending"]` so the waiting-room promotes them when they release. Free and
  released-but-thin games are kept.

> Two earlier one-offs lived here and were removed once spent (2026-06-30, see §2 → T3):
> `hltb_backfill.py` (one-time sweep that rewrote every `hltb.json` entry to add
> `raw`/`est`/`fetched_at` and fix the historically-skewed `avg`) and `backfill_updates.py`
> (one-off fill of `last_update_ts` for games scraped before the scraper recorded it).
> The live estimation logic they leaned on survives in `hltb_estimate.py` (§5.4).

Once their work is done and committed, the backfill scripts (and their one-off
workflows) can be deleted from the repo.

### 5.9 The Cloudflare Worker — wishlist import proxy

The one piece of infrastructure **outside this repo**, and the only component that
makes a *live* Steam call on a user's behalf. It powers the frontend's "import my
wishlist" feature.

**Why a proxy is required at all:** the frontend wants to read a user's Steam wishlist
to filter the table down to games they actually want. But the browser cannot call Steam
directly — Steam sends no CORS headers (the same constraint that forces all scraping
server-side). A tiny **Cloudflare Worker** (`qhpp-wishlist.mlmariss.workers.dev`,
referenced in `index.html` as `WISHLIST_PROXY`) sits in between:

- The browser calls `…workers.dev/?steamid=<SteamID64>`.
- The Worker calls Steam **server-side** (no CORS limitation, and any API key stays
  secret on the Worker, never shipped to the browser).
- It returns just the appid list as JSON, which the frontend intersects with the
  loaded catalog.

**Input handling:** the frontend (`parseSteamId`) accepts a raw 17-digit **SteamID64**
or a `/profiles/<id>` URL. **Vanity `/id/<name>` URLs can't be resolved client-side**
(that needs a keyed Steam call), so the UI directs users to steamid.io to get their
numeric ID.

**Error contract** — the Worker returns `{ ok: false, reason: … }` and the frontend
renders a specific, actionable message for each:
- `private_or_empty` — wishlist empty, or the profile's *game details* are private
  (the common case; the UI links to the Steam privacy settings page).
- `bad_steamid` — the ID wasn't a valid SteamID64.
- anything else — Steam is down or rate-limiting; try again shortly.

**Operational notes:** the Worker is a separate deploy (Cloudflare dashboard / `wrangler`),
not version-controlled here, so its source isn't in this repo — **document or back it up
separately** (see §14). If `WISHLIST_PROXY` is left at its placeholder value the feature
self-disables with an explanatory toast, so the rest of the site works without it. This
is a graceful-degradation boundary: wishlist import is additive, and its absence never
breaks browsing/sorting/filtering.

---

## 6. The seeds system

`seeds.txt` lets you push specific games to the **front** of the scrape queue without
waiting for the frontier to reach their appid.

**The design (important):** `seeds.txt` is a **human-only, read-only-to-the-scraper**
file — you edit it, the scraper never writes to it. This preserves one-writer-per-file
(the scraper writing to a file you also hand-edit would reintroduce the collision
class). The scraper's record of "which seeds are already handled" lives in
`catalog.json["seeds_ledger"]`, keyed by seed provenance.

**How it works each run (`reconcile_seeds`):**

1. Read every active line in `seeds.txt` (comments / blank lines skipped).
2. For any seed not already handled, resolve it to appid(s) and push to the front of
   the priority queue.
3. Record it in the ledger so it's not re-processed (no loops).

**Seed line formats** (`parse_seed_line` / `resolve_seed`):
- A bare **appid** (`2495100`).
- A **store URL** (parsed for the appid).
- A **search term** or search URL — resolved live against Steam search, re-resolved at
  most once per `SEED_RESOLVE_TTL` (24 h) so a term keeps catching new matches.
- A **`!force` prefix** — one-shot re-scrape of an already-stored game (latched via
  `forced_applied` so it doesn't loop).

**Forget policy:** removing a line from `seeds.txt` does **not** delete the scraped
game from `games.json` — it just drops the ledger entry. The game stays.

**Live injection mid-run:** the main loop is a mutable `deque`, and at each checkpoint
(~10 min) it fetches `origin:seeds.txt` via `git show` and splices any newly-discovered
priorities to the front of the live work queue (`inject_new_seeds`). So a seed added
mid-run is picked up at the next checkpoint (~10 min latency) rather than waiting for
the next cron (~6 h).

`seeds_log.txt` is an append-only audit trail of seed reconciliations (scraper-owned,
committed alongside `catalog.json`).

---

## 7. Sale countdowns & offline expiry

Steam's price API doesn't always expose a clean sale end-date, and even when
`prices.json` has one, sales end on a schedule the frontend should respect without
needing a fresh scrape.

- `price_and_sale.py` records `discount_end` (Unix timestamp) for on-sale games from
  `GetItems`, and prunes expired sales.
- The frontend shows a **live countdown** for active sales and flags ones ending soon.
- `expireSaleIfEnded` collapses any sale whose `discount_end` has passed **entirely
  offline**: it zeroes the discount, restores base price, drops QHPP-after to
  QHPP-full, and marks `_expired_sale` for display until the next reload. No scraping
  involved — so countdowns are always honest even between price refreshes.

---

## 8. The HLTB estimation system (deep dive)

This is the most involved subsystem, added to fix a systematic distortion in QHPP.

### The problem

HowLongToBeat exposes three completion times: **main**, **main+extras**, and
**completionist**. Many games only have 1 or 2 of them. The original code computed
`avg` as the mean of *whatever happened to be present* — so:

- A game with only a **main** time got `avg == main` → understated.
- A game with only a **completionist** time got `avg == completionist` → badly
  overstated (a long 100% time treated as a typical playthrough).

Since QHPP defaults to the `avg` metric, this skewed the value score for hundreds of
games.

### The fix

When 1 or 2 of the 3 times are missing, **estimate the missing ones from the typical
ratio between the three times**, then compute `avg` over the now-complete triple.

**The ratio** is the **median** across all games that have all three real values
(median, not mean, because grind-heavy completionist outliers drag the mean up and
over-inflate a typical game). At the time of writing, that ratio was:

```
main : extra : complete  =  1 : 1.39 : 2.19      (327 real triples)
```

The ratio is **live** — recomputed from the current corpus on each run, so it
self-corrects as more real triples accumulate — with **frozen median constants as a
cold-start fallback** until there are enough real triples (`MIN_TRIPLES_FOR_LIVE = 30`).

**Anchoring:** missing values are derived from whatever real value(s) exist (not just
main), routing through the nearest reliable neighbour (main↔extra and extra↔complete
are adjacent and more reliable than the main↔complete jump).

### The `raw` ground-truth model

Each `hltb.json` entry now looks like:

```json
{
  "main": 53.4, "extra": 94.8, "complete": 171.8,
  "avg": 106.7,
  "match": "Stardew Valley",
  "fetched_at": 1782817321,
  "raw": { "main": 53.4, "extra": 94.8, "complete": 171.8 },
  "est": ["extra"]
}
```

- **`raw`** holds *only* genuine HLTB values (or null). **Zeros are normalized to
  null** on the way in — a game can't be played in zero hours, so a 0 is treated as
  "no value" and gets estimated like a missing one.
- Top-level `main`/`extra`/`complete` are the **effective** values: real where `raw`
  has them, estimated otherwise. These are what the frontend shows and QHPP uses.
- **`est`** lists which top-level fields are estimated (drives the frontend's distinct
  styling). Absent when nothing is estimated.
- **`fetched_at`** records when HLTB was last fetched (groundwork for future
  re-scraping by staleness — see §2 → T5).

**Why `raw` matters:** estimates are *always* derived from `raw`, never from prior
estimates. This guarantees (a) estimate quality only improves and never compounds
error, and (b) a future real re-scrape can losslessly overwrite into `raw` and
recompute, because the ground truth was never overwritten by a guess.

### The anti-pollution guard

The single most important correctness property: **only real `raw` triples feed the
ratio computation.** Without this, a backfilled entry — whose three values are now all
positive numbers — would masquerade as a real triple, the ratio would train on its own
estimates, and it would drift every run. `compute_ratios` reads from `raw` (which never
holds estimates), so this can't happen. This was caught and fixed during development by
a round-trip test: fill the whole corpus, recompute the ratio, and assert it's
byte-identical (327 → 327 real triples, unchanged).

### Frontend rendering

Estimated values render in a **distinct blue accent with a dotted underline** and a
hover tooltip: *"Estimated from the genre-average ratio between main / extras /
completionist times — not reported by HowLongToBeat. Replaced automatically if HLTB
data is found later."* When an estimated column is *also* the selected QHPP metric,
gold (selection) wins for the number but the dotted underline stays so it still reads
as estimated. A null value is never marked estimated even if its key is in `est`.

### The one-time backfill (historical)

`hltb_backfill.py` swept the existing `hltb.json` once to apply all of the above to
games already in the file (the live refresher fixes new games at the source). It:
added `raw`/`est`/`fetched_at`, filled missing/zero values, corrected `avg`. It was
**idempotent** (estimates derive from `raw`, so re-running was a no-op) and ran via
the `backfill-hltb.yml` one-off workflow, which shared the `steam-hltb` concurrency
group so it could never write `hltb.json` at the same time as the refresher. Both the
script and its workflow were **deleted once spent** (2026-06-30, §2 → T3); this section
is kept as a record of the migration.

Result of the run (historical, at backfill time): **387 entries received estimates,
347 skewed averages corrected, 7,600 genuine no-match blanks left untouched, 327
full-real entries untouched.** Those figures are a point-in-time record of the one-off
sweep; the live corpus has since grown well past them (as of 2026-06-30: ~17.7k HLTB
entries total, ~16.0k blanks, ~1.1k carrying ≥1 estimate, ~0.6k full-real triples).
The estimation *mechanism* is unchanged — only the volume has scaled.

---

## 9. GitHub Actions / workflows

All workflows live in `.github/workflows/`. Each is `workflow_dispatch` (manual) +
`schedule` (cron). The recurring jobs use **`actions/checkout@v5`** and
**`actions/setup-python@v6`** (both Node 24-based — bumped off the deprecated Node 20).
v5/v6 specifically, rather than the newest checkout v6/v7, because v5 keeps the
credential-persistence behavior the commit-and-push flow relies on without requiring a
newer runner. *(The old one-off `backfill-hltb.yml` pinned the stale `checkout@v4` /
`setup-python@v5`; it was deleted rather than bumped — §2 → T3/T4 — so all remaining
workflows are on the current pins.)*

| Workflow            | Job             | Cron (UTC)             | Concurrency group | Runs                  |
|---------------------|-----------------|------------------------|-------------------|-----------------------|
| `scrape.yml`        | main scraper    | `0 0,6,12,18 * * *`    | `steam-scrape`    | `scraper.py`          |
| `prices.yml`        | pricing         | `7 */3 * * *` (3 h)    | *(own)*           | `price_and_sale.py`   |
| `hltb.yml`          | HLTB            | `53 */2 * * *` (2 h)   | `steam-hltb`      | `hltb_refresh.py`     |
| `tags.yml`          | tags            | `29 */2 * * *` (2 h)   | *(own)*           | `tags_refresh.py`     |
| `recent.yml`        | recent reviews  | `41 4,10,16,22 * * *`  | *(own)*           | `recent_refresh.py`   |

That's **5 recurring jobs** (the two manual one-off workflows, `backfill.yml` and
`backfill-hltb.yml`, were deleted once spent — §2 → T3). Not in this table — and not in this
repo — is the **wishlist Cloudflare Worker** (§5.9), which is deployed and scheduled
entirely on Cloudflare's side, independent of GitHub Actions.

**Cron times are deliberately staggered** (`:07`, `:29`, `:41`, `:53`) so jobs don't
all fire at once. The two HLTB jobs share `steam-hltb` and the two catalog jobs share
`steam-scrape` (with `cancel-in-progress: false`) so members of a group **queue rather
than overlap** — protecting the file each group writes. Jobs in different groups (and
the ones writing distinct files) run freely in parallel, which is safe precisely
because of one-writer-per-file.

**Commit pattern (every job):** `git add <its-file>` → if staged changes exist →
commit → `git fetch origin main` → `git rebase --autostash origin/main` → `git push
origin HEAD:main`, with retry/backoff against concurrent pushes. Because each job only
touches its own file, the rebase always applies cleanly.

**Two shared resilience patterns, reimplemented per script (intentionally — each job is
a standalone single-file program with no shared runtime import beyond `hltb_estimate`):**

- **HTTP retry/backoff (`get()`).** Every scraping script has a near-identical `get()`
  helper with the same contract: **429** (rate-limited) → sleep `min(90, 5·attempt)` s
  and retry; **403** (soft-limit / cooldown) → sleep a flat cooldown (300 s in the
  storefront-heavy jobs, 60 s in `price_and_sale`) and retry; network exceptions →
  short exponential-ish backoff. After `MAX_RETRIES` it returns `None`, and callers
  treat `None` as "transient — leave this game unresolved, try next run" (vs. a definite
  blank/skip, which *is* recorded). This is why a flaky network never corrupts state: a
  failed fetch is simply absent, not a wrong value.
- **Push retry (`git_checkpoint`).** After committing, each job loops up to 8 times:
  fetch → rebase → push, with jittered backoff between attempts, to survive another job
  pushing in the same instant. If it still can't push, progress is kept locally and the
  next checkpoint carries it — no work is lost.

These are duplicated rather than factored into a shared module on purpose: it keeps each
job a self-contained file that can be understood, run, and uploaded in isolation. The
cost is that a contract change (e.g. a new backoff policy) must be applied in each
script. (A candidate consolidation — see §14 — but the duplication is currently a
deliberate simplicity trade.)

**Permissions:** every workflow needs `contents: write` to commit. Repo-level: Settings
→ Actions → General → Workflow permissions → **Read and write**.

> **Keep-alive note:** GitHub disables scheduled workflows after 60 days of repo
> inactivity. The frequent committing jobs keep the repo active, so this never trips
> as long as scraping is running.

---

## 10. Data file schemas (quick reference)

All files share a `{ generated_at, count, <payload> }` envelope. **Counts below are
dated snapshots (2026-06-30) that grow run to run — not fixed values.**

**`games.json`** — `{ generated_at, count, games: [ ... ] }`, **~23.2k games (2026-06-30)**.
Each record:
```json
{
  "appid": 10, "title": "Counter-Strike",
  "url": "https://store.steampowered.com/app/10",
  "rating_pct": 97, "review_count": 260284,
  "price_initial": 9.99, "price_final": 1.99, "discount_pct": 80,
  "is_free": false,
  "release_date": "Nov 1, 2000", "release_ts": 973036800,
  "tags": ["Action"],                       // genre fallback; tags.json overrides
  "last_update_ts": 1739968505,
  "scraped_at": 1782749345
}
```

**`prices.json`** — `{ generated_at, country, count, prices: { appid: {...} } }`:
```json
{ "10": { "price_initial": 9.99, "price_final": 1.99, "discount_pct": 80,
          "discount_end": 1783616400, "scraped_at": 1782809229 } }
```

**`hltb.json`** — `{ generated_at, count, hltb: { appid: {...} } }`: see §8 for the
full shape (`raw`/`est`/`fetched_at`).

**`tags.json`** — `{ generated_at, count, tags: { appid: [tag, ...] } }`:
```json
{ "10": ["Action","FPS","Multiplayer","Shooter","Classic","Team-Based","First-Person","Competitive"] }
```

**`recent.json`** — `{ generated_at, window_days, count, recent: { appid: {...} } }`:
```json
{ "4834070": { "recent_pct": null, "recent_count": 0, "recent_scraped_at": 1782661984 } }
```

**`catalog.json`** — scraper state (not merged by the frontend):
```json
{
  "pending":  { "385250": null },           // appid -> release_ts|null (waiting room)
  "skipped":  [206450, 208570],             // non-game / no-store-page appids
  "priority": [],                           // resolved seed queue (rebuilt every run)
  "seeds_ledger": { "id:2495100": { "kind": "id", "resolved_ts": 1782806146,
                                    "ids": [2495100], "forced_applied": false } },
  "force_refresh": [],
  "last_sync": 1782763772
}
```
As of 2026-06-30: `pending` ~27.3k (huge — most of the universe is unreleased/DLC/non-
game probes parked in the waiting room), `skipped` ~165, `priority` ~2.9k (mostly from
the live `strategy`/`colony sim`/`management`/`base-building` term seeds), `seeds_ledger`
5 entries.

---

## 11. Operating the system

### Normal operation

Nothing needed — the cron schedules keep all six data layers fresh and the site
updates as commits land. Coverage of new games grows run to run as the scraper's
frontier advances.

### Setup from scratch

1. Push all files to a **public** repo (keep `.github/workflows/` structure).
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Add the `STEAM_API_KEY` secret (free from
   https://steamcommunity.com/dev/apikey) — recommended; without it the scraper falls
   back to the keyless app list (more non-games, no change-detection).
4. Settings → Pages → deploy from branch `main`, folder `/root`.
5. (Optional) seed games to scrape first in `seeds.txt`.
6. Actions tab → run each workflow once to kick things off; they then run on schedule.

### Running locally

```bash
pip install -r requirements.txt
STEAM_API_KEY=... RUN_MINUTES=30 python scraper.py
```

Git commits are skipped when not running inside Actions (`GITHUB_ACTIONS` unset).
Open `index.html` directly to view (it shows sample data until a real `games.json`
exists alongside it).

### Editing workflow: all repo changes via GitHub web UI

This project is maintained by uploading files through the GitHub web UI (no local git
clone for edits). When making programmatic changes, the working pattern is: clone to a
scratch dir, edit/test there, then upload the final files through the web UI. **Caution:
a manual upload of a data file can clobber an Action that's mid-write to it** — pause
the relevant workflow (or rely on a one-off workflow that regenerates the file on the
runner) rather than hand-uploading large data files that a job owns. This is exactly
why the HLTB backfill ran as a *workflow* rather than a hand-uploaded `hltb.json`.

### Disaster recovery & regenerability

A direct payoff of one-writer-per-file: **every data file is independently regenerable
from scratch by re-running its owning job.** There is no hidden cross-file state to
reconstruct — each job derives its file from Steam/HLTB/SteamSpy plus the read-only
`games.json` appid list. Failure modes and recovery:

| What's lost / corrupted | Effect                                         | Recovery |
|-------------------------|------------------------------------------------|----------|
| `prices.json`           | Prices fall back to the `games.json` snapshot; sale countdowns disappear until refreshed. | Re-run `prices.yml`; the file is rebuilt fresh every run anyway (it's not incremental). |
| `hltb.json`             | HLTB columns blank; QHPP null (no hours). | Re-run `hltb.yml` — but this re-fetches from scratch (slow, the full first pass). The committed file is the only copy of resolved data, so **this is the most expensive file to lose.** |
| `tags.json`            | Tags fall back to Steam genres (never blank). | Re-run `tags.yml` (slow re-fetch, but degrades gracefully meanwhile). |
| `recent.json`         | Recent-trend column disappears. | Re-run `recent.yml`; rebuilds over several runs by priority. |
| `catalog.json`        | Scraper loses pending/skip/seed state → re-probes already-known unreleased games and re-resolves seeds (wasteful but self-healing). `games.json` is untouched. | Let the scraper run; it reconstructs `pending`/`skipped` over time. Seeds re-resolve from `seeds.txt`. |
| `games.json`          | **The catalog itself** — the appid list every other job reads. Without it, refreshers have nothing to enrich. | Re-run `scrape.yml`; it re-accumulates from the `GetAppList` universe. This is a multi-week rebuild (§12), so `games.json` is the **single most important file to preserve.** Git history is the backup. |

**Practical implications:**
- **Git history *is* the backup.** Every checkpoint commit is a restore point; reverting
  a bad commit recovers any file. Keep history intact.
- The **expensive-to-rebuild files are `games.json` and `hltb.json`** (both are slow
  accumulations, not cheap rebuilds). The fast-changing files (`prices`, `recent`) and
  the fallback-covered ones (`tags`) are cheap to lose.
- A corrupted *partial* write can't normally happen mid-run because each job writes the
  whole file then commits atomically; a killed run just leaves the last good commit.
- **The frontend degrades gracefully through all of the above**: each merge layer is
  guarded (`if(map && map[key])`), and missing layers fall back to whatever `games.json`
  carries or to the genre fallback — so losing any enrichment file yields a *poorer* but
  still-functional site, never a broken one.

---

## 12. Performance notes & hard-won lessons

These are real findings from building the system, recorded so they aren't
re-discovered the hard way:

- **Measure scraper pace between checkpoint-commit timestamps, never from visible log
  lines.** Eyeballing log output across a short window once produced a false "1.5
  games/min" panic reading — it was a measurement artifact from a checkpoint-commit
  pause, not the real rate (~11/min measured properly).

- **The decoupling was the big win.** Pulling HLTB, SteamSpy, and prices out of the
  main loop took it from ~6 games/min to ~13+ games/min, because each of those was a
  multi-second per-game blocking call. The general principle: the slow scrape should
  only do the fast, always-changing catalog work; static/flaky enrichment belongs in
  separate out-of-band jobs.

- **Independent endpoints don't compete for budget.** HLTB hits howlongtobeat.com,
  tags hit steamspy.com — neither touches the Steam storefront's ~200/5min limit, so
  they can run near-continuously in parallel without slowing the scraper.

- **Coverage gaps are mostly by design, not bugs.** Prices intentionally excludes
  free/unpriced games; recent is a ranked subset by design. Only HLTB and tags are
  genuine throughput bottlenecks (single-threaded, one-time-per-game), which is why
  their crons run every 2 h with tuned delays.

- **Steam 403 cooldowns are rare enough to ignore.** They occur roughly once per
  11,000+ games at the current delays — no special handling warranted; the flat
  cooldown-and-retry suffices.

- **One-writer-per-file is load-bearing.** Almost every "why is this structured this
  way" answer traces back to it. The git push-collision class that plagued early
  development disappeared entirely once each job owned a disjoint file.

---

## 13. Known caveats

- **HLTB matching is by title similarity** (`HLTB_MIN_SIMILARITY = 0.65`), so
  obscure or oddly-named games may not match (shown as `—`). A genuine no-match is
  recorded so it isn't re-searched forever; a future re-scrape pass (§2 → T5) can retry
  these.
- **Tags fall back to Steam genres** when SteamSpy has nothing for a game, so the tag
  column is never empty but may be coarser for some titles.
- **Estimated HLTB values are estimates**, clearly marked (blue + tooltip). They're a
  reasonable stand-in for the `avg`/QHPP, not ground truth, and are replaced the
  moment real HLTB data is found.
- **Dataset size:** the single-`games.json` approach is fine into the tens of
  thousands of games. Far beyond that, consider sharding `games.json` and loading
  shards on demand.

---

## 14. Future work — detail (companion to the §2 tracker)

Long-form rationale for the tasks indexed in **§2**. Items here carry the **same T#
IDs and order** as the tracker, so you can jump straight from a tracker line to its
detail. When you add a task to §2, add its detail block here with the matching ID.

### T1 — Remove the dead `cursor` state (§2.1) — ✅ done 2026-06-30

`catalog.json` used to carry a `cursor` field that no code read or wrote (a relic of the
pre-`GetAppList` search-pagination scraper, which paged the store search by an offset
cursor). It survived only because `save_catalog` does `dict(c)` and round-trips unknown
keys. **Resolved:** the key was deleted from the committed `catalog.json` directly — no
code change was required, since nothing referenced it. (Schema: §10.)

### T2 — Purge the orphaned `sales.json` / `sales_refresh.py` references (§2.1) — ✅ done 2026-06-30

`price_and_sale.py` replaced the old `sales_refresh.py`, and `sales.json` was no longer
written by anything — but stale comments still pointed to the non-existent
`sales_refresh.py`: in `scraper.py` (the `# NOTE: sale end-dates …` block around the
per-game data sources, and the `# discount_end is no longer scraped here …` comment in
`build_record`) and in `price_and_sale.py`'s own header. **Resolved:** the `scraper.py`
comments now reference `price_and_sale.py`/`prices.json`; the `price_and_sale.py` header
no longer names the deleted file (the `(replaces …)` title note and the `[was …]` tag in
its ownership block were dropped); `sales.json` was deleted from the repo, along with its
now-defunct §4 ownership-table row and §10 schema entry. Verified: no `sales_refresh` or
`sales.json` strings remain in any `.py`/`.yml`, and both scripts compile clean.
Comment-and-file-deletion only — no behavior change. (Ownership: §4.)

### T3 — Delete the spent one-off scripts + workflows (§2.1) — ✅ done 2026-06-30

`hltb_backfill.py` + `backfill-hltb.yml` and `backfill_updates.py` + `backfill.yml` had
done their jobs. **Resolved:** all four deleted from the repo. Before removing, verified
that nothing else imports `hltb_backfill.py` or `backfill_updates.py` (they were
standalone, invoked only by their own workflows) and that no surviving workflow
references them. `hltb_estimate.py` was **kept** — the live `hltb_refresh.py` imports it
permanently. Five recurring workflows remain (`scrape`, `prices`, `hltb`, `tags`,
`recent`). Deleting `backfill-hltb.yml` also closed T4. (Scripts: §5.8; workflows: §9.)

### T4 — Bump `backfill-hltb.yml` action versions if kept (§2.1) — ✅ done 2026-06-30 (via T3)

It pinned `checkout@v4` / `setup-python@v5` while every recurring job is on `@v5` / `@v6`.
**Resolved by T3:** rather than bump a spent one-off, the workflow was deleted outright,
so nothing stale remains to pin. All surviving workflows are on the current action
versions. (Workflows: §9.)

### T5 — HLTB re-scraping + better matching (§2.2) — ✅ done 2026-06-30 (re-scrape built; matching kept tight by decision)

The HLTB job used to fetch only games it had *never* touched — finishing one full pass
over the whole catalog, then idling forever (it would exit in seconds every run with
nothing to do). Two distinct improvements; (a) is now implemented, (b) is still open.

**(a) Re-scrape pass — ✅ done.** `hltb_refresh.py` now runs a second phase after the
first pass: when the never-seen queue is exhausted *and* time-budget remains, it
re-fetches existing entries in priority order **partial → blank → full**, oldest
`fetched_at` first within each bucket. Yield drives the order (measured against the live
corpus): partials are ~17× the hits-per-minute of blanks (HLTB already has the page, so
missing fields fill in over time), blanks are mostly irreducible, full triples almost
never change. Per-bucket **staleness windows** gate eligibility so no title is re-hit
every run — `RESCRAPE_PARTIAL_DAYS=14`, `RESCRAPE_BLANK_DAYS=60`, `RESCRAPE_FULL_DAYS=365`
(config knobs at the top of the file). The mechanism:
- `build_rescrape_queue()` classifies each entry via `HE.raw_of` (real-value count),
  filters out anything refetched within its window, and sorts oldest-first per bucket.
- Each re-fetch reuses the **identical** `hltb_for`→`HE.make_entry` path the first pass
  uses, so the `raw`/overwrite model merges cleanly: real values overwrite `raw`,
  estimates recompute from the new `raw`, and every refetch restamps `fetched_at`.
- **Transient-error safety:** `hltb_for` returns `None` on error; on a re-scrape that
  means *keep the existing entry untouched* — good data is never overwritten with a blank.
- **First-pass priority preserved:** the re-scrape phase only runs if the new-game queue
  finished with budget left; if new games consume the whole run, re-scrape is never
  reached. (At the 2026-06-30 state — ~4.8k new games left, ~48 min at 0.6s — the first
  pass now fits inside one 110-min run, so re-scrape activates the very next run and
  clears all 1,194 partials in run 1, then self-throttles to the slow blank window.)
Verified with a mocked-HLTB test harness covering: first-pass-then-rescrape ordering,
budget-exhaustion skip, partial-gains-data, the transient-error guard, and staleness
gating. Commit/log wording distinguishes new scrapes from re-scrapes for Actions-tab
visibility (no data-shape change — entries are still overwritten in place, one value set
per game). `HLTB_DELAY` is unchanged (0.6 s, matched to HLTB's throttle).

**(b) Better matching — ✅ decided against (keep tight).** Most blank entries are
no-matches (§5.3) — mostly irreducible (HLTB genuinely lacks the page), and some are
recoverable misses where the title-only fuzzy match (`HLTB_MIN_SIMILARITY = 0.65`) fails
on an oddly-named or differently-punctuated game. The candidate fixes — disambiguating
with release year/platform, normalizing titles (stripping edition suffixes / trademark
symbols), or accepting the borderline 0.5–0.65 similarity band — were all weighed and
**deliberately rejected for now.** Rationale: each one loosens matching, and a loose
match can pull in the *wrong* game, writing plausible-but-incorrect completion times into
`hltb.json` with no ground truth to catch it. A tight threshold that returns a blank is
strictly safer than a loose one that returns a confident wrong answer — a blank is
visibly missing, a wrong match silently corrupts QHPP. So `HLTB_MIN_SIMILARITY` stays at
0.65 and the remaining no-matches are accepted as the correct trade. This can be
revisited if a safe disambiguation signal (e.g. exact appid↔HLTB mapping) becomes
available, but there's no action to take today. (No-match reality: §5.3; `raw` model: §8.)

### T6 — Missing / broken thumbnails, especially adult-content games (§2.2) — ✅ done 2026-07-01

Thumbnails are derived **100% client-side from the appid** — `index.html` builds
`.../<appid>/capsule_231x87.jpg` and falls back to `.../<appid>/header.jpg`. The scraper
stores no image field. Two problems, both fixed in a **pure `index.html` change** (no
scraper edit, no `games.json` change, no schema change):

**Problem (a) — fallback was fragile.** The old handler was one-shot
(`onerror="this.onerror=null;this.src=header"`), so a double-404 left the browser's
broken-image icon. Worse, it only caught *hard* errors: Steam's CDN sometimes answers a
missing capsule with a **200 + a blank/degenerate image** rather than a clean 404, so
`onerror` never fires and you get a silently-blank thumb.

**Problem (b) — adult art shown untreated.** Games tagged Nudity / Sexual Content /
Mature / NSFW / Hentai (61 in the current `tags.json`; tags are merged client-side from
`tags.json`, not stored in `games.json`) rendered their real store art with no maturity
treatment.

**What shipped:**
1. **Content-verified fallback chain** — `stepThumb(img, hardError)` + `imgChain(appid)`
   (near the URL builders). The chain is `capsule -> header` (deterministic from appid;
   header is terminal). It advances on **both** a hard `onerror` **and** an `onload` where
   `naturalWidth <= 1` — so the blank-200 case is caught and falls through to header. If
   header also fails, the last attempt is left in place (rare). Wired to the **table
   thumb** (replacing the one-shot inline handler) **and** the **hover-enlarge popup**,
   which previously had *no* error handling at all — a real gap, now closed.
2. **Adult blur + 18+ badge** — a `.thumb.adult` class (added at render when
   `isAdult(g)`) applies a permanent CSS `filter: blur()` to the img plus a centered
   "18+" badge via `::after`. The real art is still fetched and rendered underneath; it's
   just blurred. `isAdult` reads the merged `g.tags` (populated before render), so it
   needs no new data.

**Deliberately NOT done (and why):**
- **No per-game screenshot fallback.** A screenshot would be a nicer terminal fallback
  than a possibly-broken header, but screenshot filenames are opaque hashes — there's no
  appid-derivable URL, so it would require the scraper to record a screenshot URL per game
  (+~2 MB to `games.json`, which is exactly the scaling pressure T8 is about) plus a slow
  re-scrape backfill. Parked; revisit only if broken-header cases prove common in practice.
- **No click-to-reveal on the blur.** The blur is permanent for now. Adding a reveal toggle
  would imply an age check we don't actually have. **Future improvement:** once a real 18+
  age-verification gate exists, make the blur a click-to-reveal / per-session toggle.

**Testing note:** the sandbox can't reach the Steam CDN (`host_not_allowed`), so the
fallback chain was verified with a mocked `<img>` harness (5/5 cases: real capsule kept;
capsule-404 -> header; capsule-blank-200 -> header; both-fail -> header terminal;
both-blank -> header terminal), `isAdult` was checked against real `tags.json` (61 flagged,
normal games not), and the full app `<script>` passes `node --check`. Final visual
confirmation (adult blur renders, broken capsules fall through, normal games unaffected)
is a **browser-side check after deploy**. (Frontend rendering: §5.7.)

### T7 — Top-nav height + sticky-scroll behavior + tag-rail trimming (§2.1) — 🔴 analysis done, not yet implemented

Reported 2026-07-01 from a 1440p screenshot: the filter panel fills the whole viewport,
the game table is entirely below the fold, and the sticky-header collapse behaves badly
in both scroll directions. This block records the diagnosis of each reported point and
the proposed direction, plus a code audit (point 7). **No code written yet — the scroll
redesign (points 2–4) needs a decision on the intended model before implementing.**

**Current mechanics (as built).** The whole filter UI lives in one `.topbar`
(`position:sticky; top:0; z-index:40`). A `.compact` class (`.topbar.compact
.bar-filters{display:none}`) hides all the filter controls, leaving only the logo + search.
The toggle is driven purely by scroll position: `applyCompact()` sets compact =
`window.scrollY > 8` unless a manual `headerLock` ("open"/"closed") overrides it (set by
the "⌃ Hide filters" `#collapseBtn`). The tag rail (`buildTagRail`) renders **every** tag
whose full-corpus count ≥ `MIN_TAG_COUNT` (=10), grouped into 4 buckets (Players & Mode,
Genre, Style/Theme/Feel, Other) and sorted **alphabetically** within each — so ~150 chips
across ~40 rows. Per-page control offers 50 / 100 / 200. Infinite scroll loads more table
rows via an IntersectionObserver sentinel.

**Point (1) — panel too tall / unusable below 1440p.** The single sticky unit stacks
basis + HLTB + rating + reviews + price + range slider + trend + updated-within + the
full tag rail. On 1440p that's ~1 full viewport before the table even starts; on laptops
it's worse. The tag rail is the dominant contributor (points 5 addresses it). Direction:
trimming the rail (5) removes most of the height; the remaining filter rows are compact
enough that the panel should fit within a viewport once the rail is ~3 rows instead of ~40.

**Point (2) — first scroll skips the unseen middle filters.** Because `.compact` triggers
at `scrollY > 8` and instantly `display:none`s ALL filter controls, any filter that was
below the fold (e.g. the tag rail, trend, price) is never seen: the user scrolls a pixel,
the whole panel collapses to the search bar, and the next thing on screen is the game
table. The middle of the panel is unreachable by scrolling — you can only see it at the
very top (scrollY ≤ 8) or not at all. Direction options (need decision):
  (a) Make the panel **internally scrollable** (the filter block scrolls within a
      max-height container) so the sticky bar is only ever, say, 60vh and the table sits
      below a real edge — scroll reveals the rest of the filters, THEN the table.
  (b) Keep the collapse but only compact after the panel has been **fully scrolled past**
      (trigger at `scrollY > panelHeight`, not `> 8`), so scrolling walks through all
      filters before the table.
  (c) Decouple: a short **always-sticky** mini-bar (logo + search + "show filters") and
      the big filter block becomes normal non-sticky content above the table.

**Point (3) — no resting point at the end of the table for its sort/column headers.**
Scrolling back UP from deep in the table, the instant the viewport touches the sticky
topbar it re-expands the full nav (`headerLock` resets to auto at `scrollY ≤ 8`, but the
sticky bar itself is always present at top). The table's own column headers (the sort
controls — QHPP, price, rating, etc.) get no stable position where they're usable without
the nav taking over. Direction: give the **table header its own `position:sticky`** (top:0)
so that when you scroll up, the column/sort headers stick and stay usable, and the big
filter nav only comes back as a deliberate further action (point 4). This likely pairs
with (2c) — a thin persistent bar + sticky table header.

**Point (4) — full-nav reopen should be a deliberate second step.** Today the full nav
reopens on first upward contact. Desired: scrolling up first settles the **table + its
sticky column headers**; only a further, intentional scroll-up (or a click on "show
filters") opens the full filter nav. Direction: a two-stage model — stage 1 = table with
sticky sort headers; stage 2 = full filters. Could be a scroll-threshold/latch, or make
the filter reopen click-only (never auto) so it never eats the screen unexpectedly.
Simplest robust option: **filters reopen ONLY via the "Show filters" button**, never
auto-expand on scroll; scroll only ever collapses. That removes the surprise entirely.

**Point (5) — two-tier tags: top-10 foundation + "+ more" overflow, BOTH alphabetical.**
(User-clarified 2026-07-01.) Within each group (SKIP Players & Mode — already short):
  - **Foundation tier:** pick the **10 most-USED tags** (membership decided by
    `state.fullCounts` corpus count, descending; name asc as the tiebreak for a stable
    top-10 set), then **display those 10 sorted ALPHABETICALLY among themselves.** This is
    a fixed foundation row that never reshuffles as long as the corpus is stable.
  - **Overflow tier:** the group's remaining eligible tags (still gated by ≥
    `MIN_TAG_COUNT`), hidden behind a per-group **"+N more"** button. When expanded they
    render as a SEPARATE lower tier, **sorted alphabetically among themselves** (their own
    independent A–Z list, appended after the foundation row).
  Key point: these are **two independent alphabetical lists**, not one count-ranked list.
  Count only decides *which* tags are in the foundation vs. overflow; ordering *within*
  each tier is alphabetical, so chips never jump around. This avoids the "chips reorder by
  count" churn while still surfacing the most-used tags as the always-visible foundation.
  Self-contained to `buildTagRail` (bucket → split into top-10-by-count vs. rest → sort
  each alphabetically → render overflow chips with a `hidden` class) + a "+ more" click
  handler (toggle the group's `expanded` class) + a little CSS. Biggest height win.

**Point (6) — per-page 100 / 500 / 2000.** Change the three `#pagesize` buttons from
50/100/200 to 100/500/2000 and update the default `.on` (100 stays default). Trivial
markup + the existing click handler already reads `data-size`. Watch: 2000 rows rendered
at once is a lot of DOM — but infinite scroll already caps initial render at
`pageSize × pagesShown`; setting pageSize=2000 means 2000 rows in the first paint, which
may be heavy on mobile. Consider whether 2000 should still paginate internally or accept
the render cost. Flag for the user; likely fine on desktop, test on mobile.

**Point (7) — code audit of the sticky-header behavior (other findings):**
  - **Scroll listener runs `applyCompact` on every scroll event** (passive, but still).
    Fine, but if we move to a threshold model, debounce/rAF-throttle it.
  - **`headerLock` is a global reset at `scrollY ≤ 8`** — this fights any "keep filters
    open while I scroll the rail" intent. If the panel becomes internally scrollable (2a),
    this auto-reset needs rethinking so scrolling *within* the panel doesn't collapse it.
  - **No `scroll-margin-top`/anchor handling** — with a sticky header, in-page anchors (and
    the search-focus jump) can land under the sticky bar. Not reported, but worth a
    `scroll-margin-top` on the table if we add a sticky table header.
  - **The collapse is all-or-nothing** — there's no middle state (e.g. keep rating/price
    but hide the rail). A partial-collapse could be a nicer default than binary.
  - **`.compact` also shrinks the logo and hides the tagline** — good, keep that.
  - **Mobile:** the `.pop` hover-enlarge is desktop-only (mouseover); fine. But the whole
    filter panel has no mobile-specific treatment — at narrow widths the chip rows wrap
    into a very tall block. The trim (5) helps; a collapsed-by-default filter panel on
    mobile (starting compact) may be the right call.
  - **Suggestion:** consider starting the page with filters **collapsed by default** and
    an obvious "Filters" button, so the table is the first thing every user sees — the
    filters are power-user tools, the table is the product.

**Proposed sequencing (for user decision):**
  1. Ship the two cheap, self-contained wins first: **point 6** (per-page numbers) and
     **point 5** (tag-rail trim + expander). These alone cut the panel height dramatically
     and are low-risk.
  2. Then tackle the **scroll-behavior redesign (points 2–4)** as one coherent change once
     the model is chosen (the (a)/(b)/(c) options above) — this is the involved part and
     shouldn't be pieced together ad hoc.
  (Frontend rendering: §5.7; all logic in `index.html`.)

### T8 — Load-time / scaling ceiling as the dataset grows (§2.2)

The frontend fetches the **entire `games.json` (~12 MB at ~23k games, 2026-06-30) plus
four more JSON layers up front**, on every page load, with `cache:"no-store"` — then
merges and renders client-side. Fine now, but it scales linearly: at the full ~90k-game
catalog, `games.json` alone heads toward ~45 MB — a real mobile-data and parse-time
cost — and `no-store` defeats browser caching, so the whole payload re-downloads every
visit. The infinite-scroll UI already only *renders* a page at a time; the data layer
just doesn't match that yet. Deferred (not yet a problem). Fixes, roughly by effort:

1. **Drop `no-store`** in favor of a cache-busting query param or proper
   `ETag`/`Cache-Control`, so repeat visits are cheap. Smallest change, immediate win.
2. **Ship a slimmer, pre-merged index** — do the five-layer merge in a build step
   (another Action) so the browser downloads one compact precomputed file and doesn't
   merge at all.
3. **Precompress** (gzip/brotli) or move the large layers to a columnar/binary format.
4. **The big one — shard `games.json`** (by appid range, or by a pre-sorted QHPP page)
   and **load shards on demand / paginate**, so the browser only pulls what it renders.

(Frontend: §5.7; caveats: §13.)

### T9 — Factor out the duplicated `get()` / `git_checkpoint` helpers (§2.3)

Every scraping script reimplements the same HTTP retry/backoff (§9) and push-retry
logic. (The update-detection heuristic was also duplicated in the now-removed
`backfill_updates.py`, but that one-off is gone — §2 → T3 — so `scraper.py` holds the
only copy.) This is currently a deliberate "each job is a self-contained
single file you can read, run, and upload in isolation" trade. A shared `_http.py` /
`_git.py` would remove the copy-paste drift risk, but only matters if a contract needs
to change in lockstep across jobs — weigh against the simplicity the duplication buys.
(Resilience patterns: §9.)

### T10 — Back up / version the Cloudflare Worker in-repo (§2.3)

The wishlist proxy (§5.9) is the only piece of infrastructure whose source isn't in
this repo. If the Worker is lost or its Cloudflare account changes, wishlist import
silently degrades and there's nothing checked in to redeploy from. Consider committing
the Worker script (and its `wrangler.toml`) into a `worker/` directory here, even though
it deploys separately, so the whole system is reconstructable from one repo. (§5.9.)
