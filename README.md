# QTPD — Steam value hunter

Finds the best **quality time per dollar** across Steam games. A set of scrapers
build up a database **over time** (committed to this repo as JSON); a static page
(`index.html`) reads it and lets you browse, sort, and filter — with live discount
countdowns and a gold value-meter on the QTPD column.

*(QTPD was formerly called **QHPP**, "Quality Hours Per Price". The metric is unchanged; only
the name is friendlier. The repo, GitHub project, and wishlist worker keep their legacy
`SteamQHPP` / `qhpp-wishlist` names.)*

Runs almost entirely on **GitHub Pages + GitHub Actions** (free and unlimited for public
repos). All scraping happens server-side in the Actions — the frontend can't call Steam
directly (Steam sends no CORS headers). The one exception is the optional **wishlist import**
feature, which routes through a tiny **Cloudflare Worker** proxy so the browser can read a
user's Steam wishlist; everything else needs no backend, and the site works fully without the
Worker. *(The live Worker this deployment points at is `qhpp-wishlist.mlmariss.workers.dev` —
its source is **not** checked into this repo; see "Wishlist import" below for what deploying
your own would take.)*

**QTPD** = `(avg HLTB hours × rating%) ÷ price`. Higher = more quality time
per dollar. Null for free games and games HLTB can't match. Filter-bar toggles pick whether
the score uses the **Sale** (discounted) or **Full** price, and which HLTB metric (main /
extras / completionist / avg) feeds the formula.

> For the full engineering deep-dive — architecture rationale, every script explained,
> data schemas, the HLTB estimation system, and the playtime / weighted-rating pipeline —
> see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## How it works

The work is split across **separate scheduled jobs that each own one file** — this
"one writer per file" design is what lets them all commit to the repo in parallel
without collisions (jobs writing *different* files always rebase cleanly). The
frontend merges every file by appid in the browser and computes QTPD client-side.

| Job (Action)            | Writes               | Contents                                            |
|-------------------------|----------------------|-----------------------------------------------------|
| `scraper.py`            | `games.json`         | Catalog: title, rating, reviews, release, genres, `last_update_ts` (News-API heuristic; the accurate event-typed history lives in `updates.json`). + `catalog.json` (scraper state). |
| `price_and_sale.py`     | `prices.json`        | Live price, discount %, sale end-date (the fast-changing layer). |
| `hltb_refresh.py`       | `hltb.json`          | HowLongToBeat completion times (static; fetched once per game). Fills partial times from the typical main/extras/completionist ratio (not genre-based — see ARCHITECTURE.md). |
| `tags_refresh.py`       | `tags.json`          | SteamSpy user tags.                                 |
| `recent_refresh.py`     | `recent.json`        | 30-day rolling review score (recent-vs-all-time trend). |
| `playtime_refresh.py`   | `playtime_raw/NN.json` | Per-review playtime + recommendation, keyed by `recommendationid` (the big working set). **Sharded** into 64 files (`(appid//10)%64`) after the monolith hit GitHub's 100 MB wall. |
| `playtime_summarize.py` | `playtime.json`      | Lean summary: median hours split by ▲ recommend / ▼ not. Chained step of the playtime job; pure local recompute, no Steam calls. |
| `ratings_summarize.py`  | `ratings.json`       | Playtime-weighted review rating (2×-median-capped). Chained step of the playtime job; pure local recompute, no Steam calls. |
| `updates_refresh.py`    | `updates_raw/NN.json` | Per-game update-event history (major/regular/minor via Steam `event_type`), keyed by event `gid`. **Sharded** like playtime. On the storefront budget, so its own out-of-band job. |
| `updates_summarize.py`  | `updates.json`       | Lean summary: last major/regular/minor timestamps + windowed big/small counts (month/3mo/6mo/year/over-year). Chained step of the updates job; pure local recompute. |

The **main scraper** (`scraper.py`) is the only thing that finds *new* games. Each run:
1. **Enumerates the catalog** via Steam's `IStoreService/GetAppList` — games-only,
   appid-ordered, with a per-app `last_modified` timestamp.
2. **Refreshes changed games first** — any stored game whose `last_modified` moved past
   when we last scraped it. Then **scrapes new games**, newest appid first.
3. Only stores games that are **actually released**; unreleased ones wait in a
   `catalog["pending"]` room and get promoted the moment their release date passes.
4. Runs for a **time budget** (`RUN_MINUTES`) and **git-commits every ~10 minutes**, so
   hitting the 6-hour Actions wall never loses work.

The **refreshers** (`hltb`, `tags`, `prices`, `recent`, `playtime`) run on their own
schedules and just enrich games the scraper already found. HLTB and SteamSpy hit their own
sites (not Steam), so they don't compete for Steam's rate budget. The two **summarizers**
read the raw playtime file and rebuild the small frontend files — no Steam access at all.

### The Steam API key (recommended, free)
`IStoreService/GetAppList` needs a free Steam Web API key:
1. Get one at **https://steamcommunity.com/dev/apikey** (any domain name works).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**,
   name it `STEAM_API_KEY`, paste the key.

Without the key it falls back to the keyless `ISteamApps/GetAppList/v2` — that still
works, but it lists *all* app types (more non-games to skip) and has no change
timestamps, so refresh reverts to a simple `REFRESH_DAYS` timer instead of
change-detection.

## Each game shows
Title · Steam rating (% positive) + reviews · **playtime-weighted rating** (Weighted
column) · recent-review trend · store link · **Price / Sale** (full price struck through,
discounted price below, discount badge inline) · **live** time left on the sale · release
date · tags · **median playtime** split by recommendation (Playtime column; empty when a game
has no playtime data) · How Long To Beat (main / main+extras / completionist + avg) · QTPD at
the sale & full price. HLTB values **estimated** from the typical main/extras/completionist
ratio (when HLTB only reports 1–2 of the 3 times — computed corpus-wide, not per-genre despite
the name this used to go by) are shown in blue with a hover tooltip.

- **Weighted** — a review rating where each vote is weighted by how long that player
  actually played (capped at 2× the game's median so no single obsessive dominates), shown
  next to Steam's flat %. Grayed when there are too few reviews to be reliable.
- **Playtime** — the median hours played, split into ▲ players who recommended it (green)
  and ▼ players who didn't (red). A long playtime on a "not recommended" is a credible
  signal. Hover for the sample size.

## Frontend filters
Title search · on-sale-only · minimum rating (any / 60+ / 70+ / 80+ / 90+) · min & max
price · minimum reviews (0 / 10 / 100 / 1k / 5k+ bands) · review trend (improving /
stable / declining) · updated-within (any / 1mo / 3mo / 6mo / 1yr / 1yr+) · QTPD range
(log-scale slider that fits the current results) · tags (click to require, again to
exclude). Score controls: **QTPD price basis** (Sale / Full), **HLTB metric** (main /
+extras / 100% / avg), **HLTB data** (all incl. estimates / real only — *real* is the
default). Sort controls: **Reviews sort by** (all-time / 30-day) and **Playtime sort**
(▲ recommenders / ▼ non-recommenders — this only *selects* which median a click on the
Playtime column will sort by; it doesn't reorder on its own). Hover any filter toggle for a
one-line tooltip explaining it; the tag rail shows a visible `✓ require → ✕ exclude → clear`
legend, and even rows are lightly shaded for readability. Click the **QTPD logo** to show/hide
the whole filter bar.

Sort by any column incl. QTPD, Weighted, Playtime, rating, price, release date. The **Price /
Sale** column's header splits into two sort targets — click **Price** to sort by current price,
**Sale** to sort by discount depth.
Infinite-scroll pagination (100 / 500 / 2000 per page); all filter/sort state lives in the
URL so views are shareable.

**Wishlist import** — paste your Steam profile in *any* format (profile URL, custom
`/id/<n>` URL, bare name, SteamID64, `STEAM_0:0:…`, or `[U:1:…]`) to cross-reference the
catalog against your actual wishlist and optionally filter to wishlist-only. Numeric formats
convert in the browser; vanity names resolve via the Cloudflare Worker (see below). Requires
the profile's *game details* to be **public**. If the Worker isn't reachable or configured,
the Import button stays visible but each attempt just fails with an on-screen error toast —
the rest of the site is unaffected either way.

## Setup (~5 min)
1. Push these files to a **public** repo (keep the structure, incl. `.github/workflows/`).
2. **Settings → Actions → General →** Workflow permissions: **Read and write**.
3. *(recommended)* Add the `STEAM_API_KEY` secret (see above).
4. **Settings → Pages →** deploy from branch `main`, folder `/root`. Site:
   `https://<you>.github.io/<repo>/`.
5. *(optional)* Put games to scrape **first** in `seeds.txt` (one appid, store URL, or
   search term per line — human-edited only; the scraper never writes to it, but it
   **reads it live**: edits are picked up at the start of every run *and* mid-run, from
   `origin/main`, at every checkpoint (~10 min). Prefix a line with `!` to force a
   one-shot re-scrape of that seed's already-stored matches. Removing a line "forgets"
   it — already-scraped games stay in `games.json`, only the priority ordering is
   dropped. See **[ARCHITECTURE.md](ARCHITECTURE.md)** §6 for the full reconciliation model;
   every change is logged to `seeds_log.txt`).
6. **Actions tab →** run each workflow once to start; they then run on schedule and the
   page updates as data accrues.

Run locally instead: `pip install -r requirements.txt && python scraper.py` (set
`STEAM_API_KEY` and optionally `RUN_MINUTES` as env vars). Git commits are skipped when
not running in Actions. Open `index.html` to view (shows sample data until real JSON exists).

### Local dev helpers (`setup/`)
Two Windows `.bat` helpers for working on this repo from a local machine (not used by CI,
not served by Pages — safe to ignore if you only deploy via Actions):
- **`setup/preview.bat`** — serves the project root at `http://localhost:8000` via
  `py -m http.server` (falls back to `python` if the `py` launcher isn't found) and opens it
  in the browser. Read-only toward git; just a local static-file server for previewing
  `index.html` against whatever JSON is currently checked out.
- **`setup/sync.bat`** — brings a local checkout up to date when switching machines: refuses
  to run if there are uncommitted changes, then `git fetch --all --prune`, checks out `main`,
  fast-forward-pulls it, and prints the branch list + status. Never pushes, merges, or deletes
  anything.

### Wishlist import (optional)
The "import my wishlist" feature needs a small Cloudflare Worker (free tier is plenty), since
the browser can't call Steam directly. **This repo does not include the Worker's source** —
this deployment's `WISHLIST_PROXY` (top of the wishlist code in `index.html`) points at an
already-deployed Worker (`qhpp-wishlist.mlmariss.workers.dev`) that lives outside this repo. To
run your own:
1. Write a Worker that exposes two GET endpoints: `/?vanity=<name>` (proxies Steam's
   `ISteamUser/ResolveVanityURL` to resolve a vanity name to a SteamID64) and
   `/?steamid=<id64>` (returns that profile's wishlist appids for cross-referencing).
2. Deploy it (`wrangler deploy`, or paste the source into the Cloudflare dashboard editor) and
   set the API key secret: `wrangler secret put STEAM_API_KEY` (same key as above), or add it
   in the dashboard under the Worker's Variables → Secrets.
3. Point `WISHLIST_PROXY` (top of the wishlist code in `index.html`) at your deployed URL.

Skip all of this and the wishlist **Import** button still shows — it just fails per-click with
a "Couldn't reach the wishlist service" toast (it does not hide or disable itself); the
catalog, filters, and sorting work exactly the same regardless. See
**[ARCHITECTURE.md](ARCHITECTURE.md)** for the endpoints and error contract.

## Config
Each job's knobs are at the top of its own script. The main ones:
- **`RUN_MINUTES`** (env, per job) — time budget per run. More frequent **long** runs
  beat many tiny ones, because GitHub's scheduler delays/drops frequent jobs under load.
- **`STEAM_DELAY` / `STORE_DELAY` / `STEAMSPY_DELAY` / `HLTB_DELAY`** — politeness pacing.
  Steam storefront is ~200 req/5 min per IP (shared by appdetails + appreviews); SteamSpy
  ~1 req/sec. Don't lower much, or you'll get 429s / a 5-minute 403 cooldown.
- **`CHECKPOINT_SECONDS`** — how often each job commits progress mid-run.
- **`NEW_ORDER`** (scraper) — `"newest"` or `"oldest"` appid order for new coverage.
- **`HLTB_MIN_SIMILARITY`** — HLTB title-match threshold. **`PRICE_BATCH`** — appids per
  batched price call. **`RECENT_COOLDOWN_DAYS`** — how stale a recent score must be to recheck.
- **`MIN_REVIEWS_FOR_RATING` / `CONFIDENT_REVIEWS` / `CAP_MULT`** (ratings) — the weighted
  rating's eligibility floor (5), full-color threshold (10), and per-review playtime cap (2×).
- **`MIN_REVIEWS_FLOOR`** (playtime, 10) — the playtime scraper skips games below this many
  all-time reviews, since they can't produce a usable sentiment-split median. Re-checked each
  run against live review counts, so it's skip-for-now, not a permanent exclusion.

## Pace & limits
The scraper captures very roughly ~1,000–1,200 games/hour (storefront rate limit ÷ 2
calls/game). The catalog has now reached full coverage (~122k games stored against a ~173k
app universe), so `scraper.py` finishes a run in ~7 minutes and mostly does incremental
refreshes; new games are added as they release. To go faster on any job: raise `RUN_MINUTES`,
or add more off-peak `cron` times. The playtime (review-time) pass is the current backfill
focus and runs on an expanded schedule — 8 slots/day at a 1.5s delay — using the storefront
headroom the now-quick main scrape leaves free. Cost stays $0 — Actions is free and unlimited
on public repos; the only ceiling is the 6-hour per-job limit. (Each daily commit also keeps
the repo active, which matters — GitHub disables scheduled workflows after 60 days of no
commits.)

## Known caveats
- **HLTB** matches by title similarity, so obscure/oddly-named games may not match (shown
  as `—`). Each game is fetched once; the job is still completing its first full pass, and
  re-scraping (to retry no-matches and update partials) comes after — see ARCHITECTURE.md.
- **HLTB estimates** fill missing main/extras/completionist times from the typical ratio
  so the QTPD-driving `avg` isn't skewed; they're clearly marked (blue + tooltip) and
  replaced automatically once real HLTB data is found.
- **Weighted rating** needs playtime data (public profiles only) and enough reviews; below
  5 it isn't computed, and 5–9 renders grayed as low-confidence.
- **Tags** come from SteamSpy; if unavailable for a game, Steam genres are used, so the
  tag column is never blank.
- **Sale end times** come from Steam's `GetItems`; the frontend detects any expired sale
  offline (no scraping) and actively reverts it — `expireSaleIfEnded()` zeroes `discount_pct`
  and resets `price_final` to `price_initial` in the in-memory record, not just the countdown
  display — so both the price shown and the countdown stay honest between price refreshes.
- **Dataset size**: the single-`games.json` approach is fine into the tens of thousands of
  games; far beyond that, consider sharding and loading shards on demand. The `playtime_raw/`
  and `updates_raw/` shard sets are the largest, and grow with review/update coverage —
  both are already sharded into 64 files to stay under GitHub's 100 MB per-file limit.

## One-off scripts
`queue_null_updates.py` (queues every game with a null `last_update_ts` into
`catalog["force_refresh"]` so the fixed scraper re-checks them) is an idempotent run-once
utility, triggered by its own manual workflow. `cleanup_shells.py` removes unreleased
"empty shell" entries, filing them back into the waiting room. Run-once utilities can be
deleted once drained.
