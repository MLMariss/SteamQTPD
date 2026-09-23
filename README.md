# QTPD

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

**QTPD** = `(Length hours × rating%) ÷ price`. Higher = more quality time per dollar. Null
for free games and games with fewer than 3 recommending reviews. A filter-bar toggle picks
whether the score uses the **Sale** (discounted) or **Full** price.

**Length** is one figure: hours to play a game through (story plus some side content),
estimated from how long the Steam reviewers who **recommend** it played it, calibrated per
genre against HowLongToBeat and refit weekly (`length_model.py`). Games with few reviews lean
on their genre's typical length; idle games and anything past 1,000 h are balanced against the
players who did not recommend them and capped. Why this shape: [LENGTH_MODEL.md](LENGTH_MODEL.md).

> For the full engineering deep-dive — architecture rationale, every script explained,
> data schemas, the Length model, the HLTB calibration source, and the playtime / weighted-rating pipeline —
> see **[ARCHITECTURE.md](ARCHITECTURE.md)**. For what's planned, parked, or deliberately
> not being built, see **[ROADMAP.md](ROADMAP.md)**. Live data coverage is in the generated
> **[COVERAGE.md](COVERAGE.md)**, and how current that data is — per task, with the next
> refresh due and where the gap is too big — in the generated **[FRESHNESS.md](FRESHNESS.md)**.
>
> Two features have their own design records because they were built over many rounds:
> the **Review Digest** in **[REVIEW_DIGEST_PLAN.md](REVIEW_DIGEST_PLAN.md)** (as-built summary
> in ARCHITECTURE §17) and the **PICS metadata layer** in
> **[PICS_METADATA_PIPELINE.md](PICS_METADATA_PIPELINE.md)**. Two more capture outside
> feedback and what was done about it: **[INESKA_IMPROVEMENTS.md](INESKA_IMPROVEMENTS.md)**
> (a first-time user's cold arrival, all 23 findings shipped) and
> **[docs/ONBOARDING_PLAN.md](docs/ONBOARDING_PLAN.md)** (18 scored ideas for easing the
> learning curve, with a build log).

## How it works

The work is split across **separate scheduled jobs that each own one file** — this
"one writer per file" design is what lets them all commit to the repo in parallel
without collisions (jobs writing *different* files always rebase cleanly). The
frontend merges every file by appid in the browser and computes QTPD client-side.

| Job (Action)            | Writes               | Contents                                            |
|-------------------------|----------------------|-----------------------------------------------------|
| `scraper.py`            | `games.json`         | Catalog: title, rating, reviews, release, genres, `last_update_ts` (News-API heuristic; the accurate event-typed history lives in `updates.json`). + `catalog.json` (scraper state). |
| `price_and_sale.py`     | `prices.json`        | Live price, discount %, sale end-date (the fast-changing layer). |
| `hltb_refresh.py`       | `hltb.json`          | HowLongToBeat completion times. **Calibration input only** since Sep 2026 — the page no longer loads it; `length_model.py --fit` reads its real `extra` values weekly. |
| `tags_refresh.py`       | `tags.json`          | SteamSpy user tags.                                 |
| `recent_refresh.py`     | `recent.json`        | 30-day rolling review score (recent-vs-all-time trend). |
| `playtime_refresh.py`   | `playtime_raw/NN.json` | Per-review playtime + recommendation, keyed by `recommendationid` (the big working set). **Sharded** into 64 files (`(appid//10)%64`) after the monolith hit GitHub's 100 MB wall. |
| `playtime_summarize.py` | `playtime.json`      | Lean summary: median hours split by ▲ recommend / ▼ not. Chained step of the playtime job; pure local recompute, no Steam calls. |
| `ratings_summarize.py`  | `ratings.json`       | Playtime-weighted review rating (2×-median-capped). Chained step of the playtime job; pure local recompute, no Steam calls. |
| `length_model.py`       | `length.json` (+ `length_coefs.json` with `--fit`) | The page's **Length**: ▲ playtime × per-genre coefficient. Applied as a chained step of the playtime job; coefficients refit weekly against real HLTB extra (workflow 3.3). |
| `updates_refresh.py`    | `updates_raw/NN.json` | Per-game update-event history (major/regular/minor via Steam `event_type`), keyed by event `gid`. **Sharded** like playtime. On the storefront budget, so its own out-of-band job. |
| `updates_summarize.py`  | `updates.json`       | Lean summary: last major/regular/minor timestamps + windowed big/small counts (month/3mo/6mo/year/over-year). Chained step of the updates job; pure local recompute. |
| `pics_refresh.py`       | `pics_raw/shard_NN.json` | Steam PICS `common` app-info block, pulled over the **CM protocol** (not the storefront), so it's on its own rate budget. **Sharded** into 64 files. Source of truth for header art, Valve's own tag/genre/feature IDs, Steam Deck rating, AI disclosure, and the review score the main scraper uses as a staleness signal. |
| `pics_summarize.py`     | `pics/shard_NN.json` | Lean per-game projection storing **IDs, not names** + the derived Early-Access / adult / VR-only flags. Chained step; pure local recompute. |
| `pics_merge.py`         | `pics.json`          | Flattens the 64 `pics/` shards into the single file the browser downloads, keeping only the keys the frontend reads. Chained step; pure local recompute. |
| `trailers.py`           | `trailers.json`      | CDN filenames for each game's Steam preview clip, so hovering a thumbnail plays moving footage instead of just enlarging the still — and, on a phone, so a tap plays it in the card and a swipe up moves it into the docked player (ARCHITECTURE.md §11). The path is a hash the appid doesn't predict, so unlike every other bit of store art it has to be looked up and stored. Valve no longer serves a progressive full trailer — only this short loop and DASH/HLS manifests — see ARCHITECTURE.md §2.1. Batched `GetItems`; backlog-drain, not a refresh loop. + `trailers_state.json` (queue state, not served). |
| `shots.py`              | `shots/shard_NN.json` | Store screenshot filenames per game, so the hover panel — and the phone's preview player — rotates real gameplay stills after the preview clip ends — and shows them instead of a static piece of key art on the ~4,600 games with no clip at all. Hashed like trailers, so equally underivable from the appid; the PICS `store_screenshot` field that looks like a free substitute is dead (11.8% of apps, ~0% post-2019) — see ARCHITECTURE.md §2.2. Batched `GetItems`; backlog-drain, not a refresh loop. **Sharded** into 64 files and fetched lazily, one shard per hovered (or tapped) game, never at page load. + `shots_state.json` (queue state, not served). |
| `presets.py`            | `presets.json`       | The landing page's preset shelves — measures each hand-authored shelf against the live catalogue after every scrape and flags any that has gone thin or broad. It **reports, it never re-tunes**: the shelf definitions stay in the script under version control. Stdlib-only, no Steam calls. |
| `coverage.py`           | `COVERAGE.md`        | Regenerates the coverage/freshness snapshot from the live files after every scrape. Stdlib-only, no Steam calls. |
| `shard_health.py`       | `SHARDS.md`          | Daily per-shard size/evenness report for `playtime_raw/`, watching the 100 MB per-file limit. No Steam calls. |
| `freshness.py`          | `FRESHNESS.md`       | Daily 07:00 UTC freshness check: per-task last-run → next-run → gap, how much of the catalog each task holds up to date vs pending, and the per-game wait distribution. Reads the workflow crons themselves; stdlib-only, no Steam calls. |

`COVERAGE.md`, `SHARDS.md` and `FRESHNESS.md` are **generated** — read them for current
numbers, don't edit them. Three tiny static decode maps in `lookups/` (`tags.json`, `genres.json`,
`categories.json`) turn the PICS IDs back into names in the browser; they're refreshed by hand
via `pics_lookups.py` / `build_category_map.py`.

The **main scraper** (`scraper.py`) is the only thing that finds *new* games. Each run:
1. **Enumerates the catalog** via Steam's `IStoreService/GetAppList` — games-only,
   appid-ordered, with a per-app `last_modified` timestamp.
2. **Refreshes due games first** — a game is due when Steam's `last_modified` moved past
   when we last scraped it, when its **age-tiered review cooldown** elapsed (6h for a
   brand-new release, widening to 15d by the one-year mark, so review scores stay honest
   while they're still moving — games under 30 days old are re-checked every ~10 minutes
   *during* a run, not just when one starts), or when PICS reports a percentage that disagrees
   with our stored one. Then **scrapes new games**, newest appid first — new coverage keeps
   a reserved share of each run so a long refresh queue can't delay a fresh release.
3. Only stores games that are **actually released**; unreleased ones wait in a
   `catalog["pending"]` room and get promoted the moment their release date passes.
4. Runs for a **time budget** (`RUN_MINUTES`) and **git-commits every ~10 minutes**, so
   hitting the 6-hour Actions wall never loses work.

The **refreshers** (`hltb`, `tags`, `prices`, `recent`, `playtime`, `updates`, `pics`) run on
their own schedules and just enrich games the scraper already found. Three of them stay off
Steam's storefront rate budget entirely: HLTB and SteamSpy hit their own sites, and the PICS
job talks to Steam's content servers over a different protocol. That leaves prices, recent,
playtime and updates sharing the storefront budget with the main scraper, which is why they
run on staggered, off-peak crons. The **summarizers** (`playtime`, `ratings`, `updates`,
`pics`) read those raw files and rebuild the small frontend files — no Steam access at all,
so they're chained onto the end of the job that produced their input.

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
Header art (hover to enlarge) · title · Steam rating (% positive) + reviews ·
**playtime-weighted rating** (Weighted column) · recent-review trend · store link ·
**Price / Sale** (full price struck through, discounted price below, discount badge inline) ·
**live** time left on the sale · release date + age · **last-update recency and patch
cadence** (Updated column) · tags · **median playtime** split by recommendation (Playtime
column; empty when a game has no playtime data) · **Length** (one figure in hours; hover for
the review count behind it — adjusted idle / capped values are underlined in blue and the hover
shows the raw figure) · QTPD at the sale & full price. Adult games are blurred behind an **18+ gate** — click once and it asks "18+?", click
again to reveal. Revealing only reveals: it never opens the store, because the title beside the
art is the Steam link. **Right-click undoes** — from the "18+?" prompt or from revealed art it
goes straight back to hidden. Each row also has a slim `[x]` to hide that game for the session.

- **Weighted** — a review rating where each vote is weighted by how long that player
  actually played (capped at 2× the game's median so no single obsessive dominates). Grayed when
  the sample is too small to be reliable — either too few reviews outright, or a sample that is
  a tiny sliver of a much-reviewed game. Hover it for all three numbers: the weighted score, the
  same sample unweighted (so you can see what the weighting moved), and Steam's own published
  score with its full review count.
- **Playtime** — the median hours played, split into ▲ players who recommended it (green)
  and ▼ players who didn't (red). A long playtime on a "not recommended" is a credible
  signal. Hover for the sample size.

## Start with a shelf
Above the results is a row of one-click **preset shelves** — *Best deals under $10* · *Half off
or better* · *Long games, highly rated* · *Short and cheap* · *Co-op picks* · *New and
well-reviewed* · *Hidden gems* · *Under-the-radar indie*. Each one is just a set of filters, so
after you click it the summary line shows you exactly which controls it moved and you can edit
or clear any of them. Click the lit shelf again to clear it.

They are regenerated after every scrape (`presets.py`) so a shelf that has gone thin or turned
up junk shows as a warning in the build rather than sitting there stale for months. The popular
shelves need 5,000+ reviews so the results are recognisable; the two "hidden" shelves *cap* at
5,000 for the opposite reason. **No shelf ever features adult content** — every one of them sets
`adult=hide` and the build fails if one doesn't. The default landing view hides them too.

## Frontend filters
Filters live in four collapsible sections. Defaults are always the **leftmost** button, and any
control you move off its default lights up gold, so an open section shows at a glance what
you've touched.

- **Value** — **QTPD price basis** (Sale / Full) · **price type**
  (All / Full / Sale / Free, independent toggles) · min & max price · **min sale %** (a −5 / +5
  stepper that drops shallow discounts; its resting value is read from the current results, not
  hard-coded) · **length range in hours** (the twin of price range) · **QTPD range** (log-scale slider that fits the current results).
- **Quality** — minimum rating (any / 60+ / 70+ / 80+ / 90+) · **Review period** (30-day /
  all-time — it drives both the rating floor and the score sort) · review trend (improving /
  stable / declining) · minimum reviews (0 / 10 / 100 / 1k / 5k+ bands) · updated-within
  (any / 1mo / 3mo / 6mo / 1yr / 1yr+) · **released-within** (same windows, on the release date —
  `1yr+` means *older* than a year, so the two halves partition the catalogue) · **Playtime
  sort** (▲ recommenders / ▼ non-recommenders — this only *selects* which median a click on the
  Playtime column will sort by; it doesn't reorder on its own).
- **Flags** — Valve's own metadata, from the PICS layer: **Early Access · AI disclosure ·
  Adult content · VR-only · Family-share block · Custom EULA**, each an Any / Exclude / Only
  toggle, plus **Controller** (any / full / partial) and **Steam Deck** (any / verified /
  playable+ / unsupported). **Adult content starts on Exclude** (the storefront's adult flag) —
  the landing view never opens on adult games; pick Any to include them.
- **Tags** — click a tag to require it, again to exclude, again to clear (a visible
  `✓ require → ✕ exclude → clear` legend says so), plus a **Required tags match: ALL / ANY**
  toggle on the right. Excludes are always AND-NOT. A **tag search box** narrows the tag list
  itself — type `strategy` and you get Strategy, Grand Strategy, Turn-Based Strategy and
  Strategy RPG to pick from; the ✕ (or Esc) brings the full list back. It only filters which
  tags are shown, never the games.

Hover any filter toggle for a one-line tooltip explaining it — **or tap it on a phone**, where
the fields that carry an explanation are marked with a small `?`. Click the **QTPD logo** to
show/hide the whole filter bar; when it's collapsed, your active filters show as clickable
chips you can edit in place, with a **Reset** shortcut.

**Three views.** **Table** (desktop) and **Card** (mobile) are the same detailed view relabelled
per device; **Grid** is a box-art grid — tap a card to flip it to price / length / a Steam link.
Grid is the default on a phone, Table on desktop, and your pick is remembered. Alongside the
switcher: **Lucky** (jump to a random game from the current results), **CSV** (export the
filtered set, with a column picker) and **🔗** (copy the current view's link).

Sort by any column incl. QTPD, Weighted, Playtime, rating, price, release date. The **Price /
Sale** column's header splits into two sort targets — click **Price** to sort by current price,
**Sale** to sort by discount depth. The **Tags** column header has a `><` button that folds the
column away and gives the space to bigger cover art and full titles. Where the header isn't
visible (Card and Grid), sorting moves to the "sorted by …" chip on the filter summary line.
Infinite-scroll pagination — it loads the next 66 rows when you reach the bottom, and the old
`100 / 500 / 2000` selector is gone (setting it to 2000 only ever made the page slow). All
filter/sort state lives in the URL, so any view is a shareable link.

**Free games** normally show no QTPD — you can't divide by a zero price. But filter price type
down to **Free alone** and the QTPD column switches to ranking them by quality-weighted length
(hours × rating) instead, so free games can still be compared against each other.

**Wishlist import** — paste your Steam profile in *any* format (profile URL, custom
`/id/<n>` URL, bare name, SteamID64, `STEAM_0:0:…`, or `[U:1:…]`) to cross-reference the
catalog against your actual wishlist and optionally filter to wishlist-only. Numeric formats
convert in the browser; vanity names resolve via the Cloudflare Worker (see below). Requires
the profile's *game details* to be **public**. If the Worker isn't reachable or configured,
the Import button stays visible but each attempt just fails with an on-screen error toast —
the rest of the site is unaffected either way.

## Review Digest — ask an AI about a game's actual reviews

Next to a game's review count (and on its grid card) is a button that pulls **real Steam review
text** for that game, live in your browser, and packages it into one block with an analysis
prompt already on top. Paste that into any AI and you get a counted breakdown of what players
actually complain about and praise, instead of reading thousands of reviews by hand.

- **Up to 5,000 reviews**, newest first. Sizes are 300 / 500 / 1000 / 2000 / 5000 and nothing
  is capped from underneath you — the dialog *prices* each choice (token cost, which models can
  hold it, how long the fetch takes) rather than quietly shrinking it.
- **Reach** trades minutes for history: keeping one page in 2 or 3 covers 2–3× the time span
  for the same bundle size, which matters on a busy game where even 5,000 reviews is only about
  a month. The bundle says it was thinned, because a thinned sample makes *volume* figures
  (reviews/day) read low while leaving every proportion correct.
- **Output** is a self-contained HTML page by default, or Markdown; there's a Simplified mode
  if you want a straight answer rather than a report.
- A **quality bar** (10+ words by default) drops one-word reviews before counting, and the
  header says how many it dropped — same bargain throughout: remove what would distort the
  count, and state what was removed.

It needs one small **Cloudflare Worker** (`worker/`), because Steam's review endpoint sends no
CORS headers and this site has no server. Unlike the wishlist Worker, **that source is in this
repo** — see `worker/README.md` to deploy your own. The design record, including the live probe
that settled the numbers, is [REVIEW_DIGEST_PLAN.md](REVIEW_DIGEST_PLAN.md).

## Review-score colours

The rating % is coloured, and the scheme is switchable in the results bar (it's a display
preference, not a filter — it's remembered per device and rides along in a shared link):

- **Spectrum** (default) — a continuous ramp from red through orange and yellow to green, with
  a perfect 100 in blue so it reads as a separate mark rather than "even more green". No
  threshold cliffs: an 82% and a 70% no longer wear the same colour.
- **Rarity** — five hard loot-tier bands (Worn / Common / Uncommon / Rare / Epic) for reading a
  score as a *category* instead of a position. Scores inside a tier look identical on purpose,
  and the tier's name is in the tooltip so the scheme doesn't depend on telling five hues apart.

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
- **`STOREFRONT_MIN_INTERVAL`** (scraper, env, 0.9s) — the shared limiter every storefront call
  passes through. This is the scraper's real pacing knob; raise it if you start seeing 403s.
  (`STEAM_DELAY` in `scraper.py` is **deprecated** and no longer referenced — it survives only
  as a comment. The other jobs still use their own `STORE_DELAY` / `STEAMSPY_DELAY` /
  `HLTB_DELAY` / `STEAM_DELAY` constants.)
- **Politeness pacing generally** — Steam storefront is ~200 req/5 min per IP (shared by
  appdetails + appreviews); SteamSpy ~1 req/sec. Don't lower much, or you'll get 429s / a
  5-minute 403 cooldown.
- **`CHECKPOINT_SECONDS`** — how often each job commits progress mid-run.
- **`NEW_ORDER`** (scraper) — `"newest"` or `"oldest"` appid order for new coverage.
- **`REVIEW_TIERS` / `PICS_REV_DELTA`** (scraper) — the age-tiered review-refresh ladder and the
  PICS review-drift trigger. Set `REVIEW_TIER_REFRESH=0` and `PICS_REV_DELTA=0` to fall back to
  the old "only re-scrape when Steam's `last_modified` moves" behaviour.
- **`HLTB_MIN_SIMILARITY`** — HLTB title-match threshold. **`PRICE_BATCH`** — appids per
  batched price call. **`RECENT_COOLDOWN_DAYS`** — how stale a recent score must be to recheck.
  **`--stale-days`** (pics, 14) — how old a PICS record must be to refetch.
- **`MIN_REVIEWS_FOR_RATING` / `CONFIDENT_REVIEWS` / `CAP_MULT`** (ratings) — the weighted
  rating's eligibility floor (5), full-color threshold (10), and per-review playtime cap (2×).
- **`SLIVER_N` / `SLIVER_FRAC`** (ratings, 250 / 2%) — the second confidence test, and both
  halves must bind. A flat review-count floor can't see a sample that is large in absolute terms
  and still a sliver of the game: a 100-review first look at a title taking reviews by the minute
  can cover seven minutes of a 60,000-review game and clear a floor of 10 fifty times over.
- **`MIN_REVIEWS_FLOOR`** (playtime, 10) — the playtime scraper skips games below this many
  all-time reviews, since they can't produce a usable sentiment-split median. Re-checked each
  run against live review counts, so it's skip-for-now, not a permanent exclusion.
- **`FIRST_TOUCH_HOT_REVIEWS` / `FIRST_TOUCH_HOT_TARGET`** (playtime, 10,000 / 1,000) — how
  deep the *first* look at a never-seen game goes. Normally one page (100 reviews), which is
  most of a quiet release; a game the catalogue already shows as big gets the full first rung
  straight away, because one page of a busy launch is minutes of its life, not a sample.
- **`DEPTH_LADDER`** (playtime, `1000 → 2000 → 3000`) — how many reviews are kept per game. The
  first visit fills to 1,000 and moves on (so new games get covered fast); each later visit
  climbs one rung, up to 3,000. Only the ~10% of games with more than 1,000 reviews ever climb,
  and it rides the normal refresh schedule rather than adding extra work.
- **`REWALK_DAYS` / `REWALK_DELTA`** (playtime, 30 days / +1,000 reviews) — keeps popular games
  from going stale. A game sitting at the 3,000 ceiling gets its whole review window's playtimes
  re-fetched every 30 days, or sooner if it gained 1,000+ reviews. Spread out per game (each on
  its own clock, not a monthly batch), and it deepens visits that already happen rather than
  adding new ones.

## Pace & limits
The scraper captures very roughly ~1,000–1,200 games/hour (storefront rate limit ÷ 2
calls/game). The catalog has now reached full coverage (**124,210 games stored** against a
~173k app universe), so finding *new* games takes `scraper.py` only ~7 minutes a run; the rest
of its budget goes to the age-tiered review refresh (~7k re-scrapes/day, about a quarter of
this scraper's demonstrated peak). To go faster on any job: raise `RUN_MINUTES`, or add more
off-peak `cron` times. The playtime (review-time) pass runs on an expanded schedule — 8
slots/day at a 1.5s delay — using the storefront headroom the now-quick main scrape leaves
free; its backfill is essentially complete (99.9% of the games above its 10-review floor).
Cost stays $0 — Actions is free and unlimited on public repos; the only ceiling is the 6-hour
per-job limit. (Each daily commit also keeps the repo active, which matters — GitHub disables
scheduled workflows after 60 days of no commits.)

**Current coverage lives in [COVERAGE.md](COVERAGE.md)**, regenerated after every scrape —
per-metric fill rates, refresh backlogs, and which lane each game sits in. Don't trust
hand-written numbers here or in ARCHITECTURE.md over that file.

## Known caveats
- **Length** is an estimate from reviewers' playtime, not a measured completion time. It
  lands within 2× of HowLongToBeat's "main + extras" for ~84% of games with 100+ recommending
  reviews; under ~10 reviews it is close to its genre's typical figure. Endless games that are
  not tagged Idler get their reviewers' (large) hours. Games with fewer than 3 recommending
  reviews have no Length and no QTPD.
- **HLTB** (calibration only) matches by title similarity; ~85% of entries carry real values.
  Only real `extra` values train the Length model.
- **Weighted rating** needs playtime data (public profiles only) and enough reviews; below
  5 it isn't computed, and 5–9 renders grayed as low-confidence.
- **Tags** come from Valve's own PICS metadata where available, with SteamSpy as a coverage
  fallback and Steam genres behind that, so the tag column is never blank.
- **Sale end times** come from Steam's `GetItems`; the frontend detects any expired sale
  offline (no scraping) and actively reverts it — `expireSaleIfEnded()` zeroes `discount_pct`
  and resets `price_final` to `price_initial` in the in-memory record, not just the countdown
  display — so both the price shown and the countdown stay honest between price refreshes.
- **Dataset size**: four per-game working sets (`playtime_raw/`, `updates_raw/`, `pics_raw/`,
  `pics/`) are each sharded into 64 files to stay under GitHub's 100 MB per-file limit — the
  biggest shard currently runs ~13 MB, so there's plenty of headroom (`SHARDS.md` watches it).
  The largest *single* file is now `games.json` at ~62 MB; it grows with the catalog and is the
  one to watch. Well past that, the fix is sharding it too and loading shards on demand.

## One-off scripts
`queue_null_updates.py` (queues every game with a null `last_update_ts` into
`catalog["force_refresh"]` so the fixed scraper re-checks them) is an idempotent run-once
utility, triggered by its own manual workflow. `cleanup_shells.py` removes unreleased
"empty shell" entries, filing them back into the waiting room. Run-once utilities can be
deleted once drained.
