# QHPP — Steam value hunter

Finds the best **quality hours per dollar** across Steam games. A scraper builds up
a database **over time** (committed to this repo as `games.json`); a static page
(`index.html`) reads that file and lets you browse, sort, and filter it — with live
discount countdowns and a gold value-meter on the QHPP column.

Runs entirely on **GitHub Pages + GitHub Actions**. No backend, no database, no
proxy. The frontend never calls Steam (it can't — Steam sends no CORS headers); all
scraping happens server-side in the Action, which is why this works as pure static
hosting.

## How accumulation works
Each scheduled run:
1. **Discovers** more of the catalog — pages through a broad Steam search
   (`CATALOG_SEARCH`) and adds newly-seen app IDs to a pending queue.
2. **Scrapes** up to `NEW_PER_RUN` never-seen games (full detail + HLTB + QHPP).
3. **Refreshes** up to `REFRESH_PER_RUN` games that are stale (older than
   `REFRESH_DAYS`) or currently on sale, so prices/discounts stay current.
4. **Commits** `games.json` (the data) and `catalog.json` (cursor + queue + skips).

State lives in the repo, so coverage carries over between runs and just keeps
growing. Non-games (DLC, soundtracks, delisted apps) are recorded in `catalog.json`'s
skip list so they aren't re-fetched every run.

Pace: at the defaults (~80 new games every 6 hours) you add roughly 300/day. Raise
`NEW_PER_RUN` and the `cron` frequency to go faster — mind Steam's ~200 req / 5 min
rate limit.

## Each game shows
Title · Steam rating (% positive) + review count · store link · price before discount
(USD) · discounted price · **live** time left on the sale · release date · user tags ·
How Long To Beat (main / main+extras / completionist) · QHPP before & after discount.

**QHPP** = `(avg HLTB hours × rating%) ÷ price`, where `avg HLTB hours` is the mean of
whichever HLTB times exist. Higher = more quality-adjusted hours per dollar. It's `null`
for free games and games HLTB can't match. `qhpp_before` uses full price; `qhpp_after`
the sale price (toggle which one drives the table in the header).

## Frontend filters (over the stored data)
Search by title · on-sale-only · minimum rating (any / 70+ / 80+ / 90+) · maximum price ·
tags (click any tag to filter) · sort by any column incl. QHPP, rating, price, release date.

## Setup (~5 min)
1. Create a repo and push these files (keep the structure, incl. `.github/workflows/`).
2. **Settings → Pages →** deploy from branch `main`, folder `/root`. Site:
   `https://<you>.github.io/<repo>/`.
3. **Settings → Actions → General →** Workflow permissions: **Read and write** (so the
   Action can commit the dataset).
4. *(optional)* Put games to scrape **first** in `seeds.txt`. Otherwise it just works
   through the whole catalog.
5. **Actions tab →** run "Scrape Steam → games.json" once to seed it; after that it runs
   every 6 hours and the page updates as data accrues.

Run locally instead: `pip install -r requirements.txt && python scraper.py`, then open
`index.html` (it shows bundled sample data until a real `games.json` exists).

## Config (top of `scraper.py`)
- `CATALOG_SEARCH` — the universe to work through. Default: all Games, best-reviewed
  first. Swap for `sort_by=Released_DESC`, a specific tag, a price ceiling, etc.
- `CATALOG_PAGES_PER_RUN` — search pages discovered per run (~100 apps each).
- `NEW_PER_RUN` / `REFRESH_PER_RUN` / `REFRESH_DAYS` — batch sizes and refresh window.
- `TOP_TAGS`, `HLTB_MIN_SIMILARITY`, `STEAM_DELAY`, `STEAMSPY_DELAY` — enrichment knobs.

## Known caveats
- **Discount end time** isn't in Steam's price API. The scraper uses
  `featuredcategories` (reliable for current specials), then falls back to parsing the
  store page's `game_purchase_discount_countdown` element. That HTML parse couldn't be
  verified against a live page in the build environment — if a real sale shows no timer,
  the scraper logs it; add the matching pattern to `_COUNTDOWN_PATTERNS`. Discounted
  price and percent always come through.
- **Tags** come from SteamSpy; if it's unavailable for a game, Steam genres are used.
- **HLTB** has no official API; matching is by title similarity, so obscure/oddly-named
  games may not match (shown as `—` for HLTB and QHPP).
- **Dataset size**: committing one `games.json` is fine into the low thousands of games.
  For tens of thousands, consider sharding the JSON or trimming fields — the frontend
  would then load shards on demand.
