# QHPP — Steam value hunter

Finds the best **quality hours per dollar** across Steam games. A scraper builds up
a database **over time** (committed to this repo as `games.json`); a static page
(`index.html`) reads it and lets you browse, sort, and filter — with live discount
countdowns and a gold value-meter on the QHPP column.

Runs entirely on **GitHub Pages + GitHub Actions** (free and unlimited for public
repos). No backend, no proxy. The frontend never calls Steam — it can't, Steam sends
no CORS headers — so all scraping happens server-side in the Action.

## How it works
Each scheduled run:
1. **Enumerates the catalog** via Steam's `IStoreService/GetAppList` — a clean,
   games-only, appid-ordered list with a per-app `last_modified` timestamp.
2. **Refreshes changed games first** — any stored game whose `last_modified` moved
   past when we last scraped it (or that's on sale). HLTB beat-times are reused, never
   re-fetched; only price/discount/rating/tags are refreshed.
3. **Then scrapes new games** it hasn't seen yet, newest appid first.
4. Runs for a **time budget** (`RUN_MINUTES`, default 180) and **git-commits progress
   every ~10 minutes**, so hitting the 6-hour wall never loses work.

State lives in the repo: `games.json` (data) + a small `catalog.json` (last sync time
+ a skip-list of non-game / no-store-page appids, so they aren't re-probed). Coverage
just keeps growing run to run.

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
Title · Steam rating (% positive) + reviews · store link · price before discount (USD) ·
discounted price · **live** time left on the sale · release date · tags · How Long To
Beat (main / main+extras / completionist) · QHPP before & after discount.

**QHPP** = `(avg HLTB hours × rating%) ÷ price`. Higher = more quality-adjusted hours
per dollar. Null for free games and games HLTB can't match. The header toggles whether
the table sorts on the before- or after-discount value.

## Frontend filters
Title search · on-sale-only · minimum rating (any / 70+ / 80+ / 90+) · maximum price ·
tags (click any tag) · sort by any column incl. QHPP, rating, price, release date.

## Setup (~5 min)
1. Push these files to a **public** repo (keep the structure, incl. `.github/workflows/`).
2. **Settings → Actions → General →** Workflow permissions: **Read and write**.
3. *(recommended)* Add the `STEAM_API_KEY` secret (see above).
4. **Settings → Pages →** deploy from branch `main`, folder `/root`. Site:
   `https://<you>.github.io/<repo>/`.
5. *(optional)* Put games to scrape **first** in `seeds.txt`.
6. **Actions tab →** run the workflow once to start; it then runs on schedule and the
   page updates as data accrues.

Run locally instead: `pip install -r requirements.txt && python scraper.py` (set
`STEAM_API_KEY` and optionally `RUN_MINUTES` as env vars). Git commits are skipped when
not running in Actions. Open `index.html` to view (shows sample data until a real
`games.json` exists).

## Config (top of `scraper.py`)
- `RUN_MINUTES` (env) — scrape budget per run. More frequent **long** runs beat many
  tiny ones, because GitHub's scheduler delays/drops frequent jobs under load.
- `STEAM_DELAY` / `STEAMSPY_DELAY` — politeness. Steam storefront is ~200 req/5 min per
  IP (shared by appdetails + appreviews); SteamSpy ~1 req/sec. Don't lower much, or
  you'll get 429s / a 5-minute 403 cooldown.
- `NEW_ORDER` — `"newest"` or `"oldest"` appid order for new coverage.
- `REFRESH_DAYS` — fallback refresh age, only used when there's no API key.
- `CHECKPOINT_SECONDS` — how often to commit progress mid-run.
- `TOP_TAGS`, `HLTB_MIN_SIMILARITY` — enrichment knobs.

## Pace & limits
At ~200 storefront req/5 min and 2 calls per game, expect ~1,000-1,200 games/hour, so a
180-min run captures very roughly ~3,000 games; the full catalog (~90k games) is a
multi-week accumulation. To go faster: raise `RUN_MINUTES`, or add more off-peak `cron`
times (each daily commit also keeps the repo active, which matters because GitHub
disables scheduled workflows after 60 days of no commits). Cost stays $0 — Actions is
free and unlimited on public repos; the only ceiling is the 6-hour per-job limit.

## Known caveats
- **Discount end time** isn't in Steam's price API. The scraper uses `featuredcategories`
  (reliable for current specials), then falls back to parsing the store page's
  `game_purchase_discount_countdown` element. That HTML parse couldn't be verified
  against a live page in the build environment — if a real timed sale shows no countdown,
  the scraper logs it; add the matching pattern to `_COUNTDOWN_PATTERNS`. Discounted
  price and percent always come through.
- **Tags** come from SteamSpy; if it's unavailable for a game, Steam genres are used.
- **HLTB** matching is by title similarity, so obscure/oddly-named games may not match
  (shown as `—`). Because HLTB isn't re-fetched on refresh, a non-match stays a non-match
  until you clear that game from `games.json`.
- **Dataset size**: one `games.json` is fine into the low thousands of games; for tens of
  thousands, consider sharding it and loading shards on demand.
