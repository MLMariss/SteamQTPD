# QHPP — Steam value hunter

Finds the best **quality hours per price** across Steam games. A set of scrapers
build up a database **over time** (committed to this repo as JSON); a static page
(`index.html`) reads it and lets you browse, sort, and filter — with live discount
countdowns and a gold value-meter on the QHPP column.

Runs almost entirely on **GitHub Pages + GitHub Actions** (free and unlimited for public
repos). All scraping happens server-side in the Actions — the frontend can't call Steam
directly (Steam sends no CORS headers). The one exception is the optional **wishlist import**
feature, which routes through a tiny **Cloudflare Worker** proxy (source in `worker/`) so the
browser can read a user's Steam wishlist; everything else needs no backend, and the site works
fully without the Worker.

**QHPP** = `(avg HLTB hours × rating%) ÷ price`. Higher = more quality-adjusted hours
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
frontend merges every file by appid in the browser and computes QHPP client-side.

| Job (Action)            | Writes               | Contents                                            |
|-------------------------|----------------------|-----------------------------------------------------|
| `scraper.py`            | `games.json`         | Catalog: title, rating, reviews, release, genres, `last_update_ts`. + `catalog.json` (scraper state). |
| `price_and_sale.py`     | `prices.json`        | Live price, discount %, sale end-date (the fast-changing layer). |
| `hltb_refresh.py`       | `hltb.json`          | HowLongToBeat completion times (static; fetched once per game). Fills partial times from the genre ratio — see ARCHITECTURE.md. |
| `tags_refresh.py`       | `tags.json`          | SteamSpy user tags.                                 |
| `recent_refresh.py`     | `recent.json`        | 30-day rolling review score (recent-vs-all-time trend). |
| `playtime_refresh.py`   | `playtime_raw.json`  | Per-review playtime + recommendation, keyed by `recommendationid` (the big working set). |
| `playtime_summarize.py` | `playtime.json`      | Lean summary: median hours split by ▲ recommend / ▼ not. Pure local recompute, no Steam calls. |
| `ratings_summarize.py`  | `ratings.json`       | Playtime-weighted review rating (2×-median-capped). Pure local recompute, no Steam calls. |

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
column) · recent-review trend · store link · price before discount (USD) · discounted
price · **live** time left on the sale · release date · tags · **median playtime** split
by recommendation (Playtime column) · How Long To Beat (main / main+extras / completionist
+ avg) · QHPP at the sale & full price. HLTB values **estimated** from the genre-average
ratio (when HLTB only reports 1–2 of the 3 times) are shown in blue with a hover tooltip.

- **Weighted** — a review rating where each vote is weighted by how long that player
  actually played (capped at 2× the game's median so no single obsessive dominates), shown
  next to Steam's flat %. Grayed when there are too few reviews to be reliable.
- **Playtime** — the median hours played, split into ▲ players who recommended it (green)
  and ▼ players who didn't (red). A long playtime on a "not recommended" is a credible
  signal. Hover for the sample size.

## Frontend filters
Title search · on-sale-only · minimum rating (any / 60+ / 70+ / 80+ / 90+) · min & max
price · minimum reviews (0 / 10 / 100 / 1k / 5k+ bands) · review trend (improving /
stable / declining) · updated-within (any / 1mo / 3mo / 6mo / 1yr / 1yr+) · QHPP range
(log-scale slider that fits the current results) · tags (click to require, again to
exclude). Score controls: **QHPP price basis** (Sale / Full), **HLTB metric** (main /
+extras / 100% / avg), **HLTB data** (all incl. estimates / real only — *real* is the
default). Sort controls: **Reviews sort by** (all-time / 30-day) and **Playtime sort**
(▲ recommenders / ▼ non-recommenders — this only *selects* which median a click on the
Playtime column will sort by; it doesn't reorder on its own).

Sort by any column incl. QHPP, Weighted, Playtime, rating, price, release date.
Infinite-scroll pagination (100 / 500 / 2000 per page); all filter/sort state lives in the
URL so views are shareable.

**Wishlist import** — paste your Steam profile in *any* format (profile URL, custom
`/id/<n>` URL, bare name, SteamID64, `STEAM_0:0:…`, or `[U:1:…]`) to cross-reference the
catalog against your actual wishlist and optionally filter to wishlist-only. Numeric formats
convert in the browser; vanity names resolve via the Cloudflare Worker (see below). Requires
the profile's *game details* to be **public**. If the Worker isn't configured the feature
self-disables and the rest of the site is unaffected.

## Setup (~5 min)
1. Push these files to a **public** repo (keep the structure, incl. `.github/workflows/`).
2. **Settings → Actions → General →** Workflow permissions: **Read and write**.
3. *(recommended)* Add the `STEAM_API_KEY` secret (see above).
4. **Settings → Pages →** deploy from branch `main`, folder `/root`. Site:
   `https://<you>.github.io/<repo>/`.
5. *(optional)* Put games to scrape **first** in `seeds.txt` (one appid, store URL, or
   search term per line — human-edited only; the scraper never writes to it).
6. **Actions tab →** run each workflow once to start; they then run on schedule and the
   page updates as data accrues.

Run locally instead: `pip install -r requirements.txt && python scraper.py` (set
`STEAM_API_KEY` and optionally `RUN_MINUTES` as env vars). Git commits are skipped when
not running in Actions. Open `index.html` to view (shows sample data until real JSON exists).

### Wishlist import (optional)
The "import my wishlist" feature needs a small Cloudflare Worker (free tier is plenty), since
the browser can't call Steam directly. Source is in **`worker/`**:
1. Deploy it — `cd worker && wrangler deploy` (or paste the Worker source into the Cloudflare
   dashboard editor).
2. Set the API key secret: `wrangler secret put STEAM_API_KEY` (same key as above), or add it
   in the dashboard under the Worker's Variables → Secrets.
3. Point `WISHLIST_PROXY` (top of the wishlist code in `index.html`) at your deployed URL.

Skip all of this and the wishlist button simply self-disables — the catalog, filters, and
sorting work exactly the same. See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the endpoints
and error contract.

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

## Pace & limits
The scraper captures very roughly ~1,000–1,200 games/hour (storefront rate limit ÷ 2
calls/game), so the full catalog (~90k games) is a multi-week accumulation; coverage just
grows run to run. To go faster: raise `RUN_MINUTES`, or add more off-peak `cron` times.
Cost stays $0 — Actions is free and unlimited on public repos; the only ceiling is the
6-hour per-job limit. (Each daily commit also keeps the repo active, which matters —
GitHub disables scheduled workflows after 60 days of no commits.)

## Known caveats
- **HLTB** matches by title similarity, so obscure/oddly-named games may not match (shown
  as `—`). Each game is fetched once; the job is still completing its first full pass, and
  re-scraping (to retry no-matches and update partials) comes after — see ARCHITECTURE.md.
- **HLTB estimates** fill missing main/extras/completionist times from the typical ratio
  so the QHPP-driving `avg` isn't skewed; they're clearly marked (blue + tooltip) and
  replaced automatically once real HLTB data is found.
- **Weighted rating** needs playtime data (public profiles only) and enough reviews; below
  5 it isn't computed, and 5–9 renders grayed as low-confidence.
- **Tags** come from SteamSpy; if unavailable for a game, Steam genres are used, so the
  tag column is never blank.
- **Sale end times** come from Steam's `GetItems`; the frontend collapses any expired sale
  offline (no scraping), so countdowns are always honest between price refreshes.
- **Dataset size**: the single-`games.json` approach is fine into the tens of thousands of
  games; far beyond that, consider sharding and loading shards on demand. `playtime_raw.json`
  is the largest file and grows with review coverage.

## One-off scripts
`backfill_updates.py` (fill `last_update_ts` for old games) and `hltb_backfill.py` (apply
the HLTB estimation model to existing entries) are idempotent run-once utilities, triggered
by their own manual workflows. Once run, they can be deleted. `cleanup_shells.py` removes
unreleased "empty shell" entries, filing them back into the waiting room.
