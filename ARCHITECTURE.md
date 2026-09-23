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
to the price-basis toggle without re-scraping.

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
   │ hltb_refresh.py ─────────► hltb.json (calibration   │  read  │  (merges all     │
   │                              only, never served)    │ ─────► │   JSON by appid, │
   │ tags_refresh.py ─────────► tags.json                │        │   computes QTPD) │
   │ recent_refresh.py ───────► recent.json              │        │                  │
   │ playtime_refresh.py ─────► playtime_raw/NN.json     │        └──────────────────┘
   │        ├─ playtime_summarize.py ─► playtime.json    │
   │        ├─ ratings_summarize.py ──► ratings.json     │
   │        └─ length_model.py ───────► length.json      │
   │ length_model.py --fit (weekly) ► length_coefs.json  │
   │ updates_refresh.py ──────► updates_raw/NN.json      │        ┌──────────────────┐
   │        └─ updates_summarize.py ──► updates.json     │        │ Cloudflare Worker │
   │ pics_refresh.py ─────────► pics_raw/shard_NN.json   │wishlist│  (steamid/vanity  │
   │        ├─ pics_summarize.py ─────► pics/shard_NN    │◄──────►│   → wishlist)     │
   │        └─ pics_merge.py ─────────► pics.json        │        └──────────────────┘
   │ trailers.py ─────────────► trailers.json            │        ┌──────────────────┐
   │                            + trailers_state.json    │        │ Cloudflare Worker │
   │ shots.py ────────────────► shots/shard_NN.json      │reviews │   qtpd-reviews    │
   │                            + shots_state.json       │◄──────►│ (Steam appreviews │
   │ presets.py ──────────────► presets.json             │        │   passthrough)    │
   │ coverage.py ─────────────► COVERAGE.md              │        └──────────────────┘
   │ shard_health.py ─────────► SHARDS.md                │  (generated docs, not read
   │ freshness.py ────────────► FRESHNESS.md             │   by the frontend)
   └────────────────────────────────────────────────────┘
```

All scraping is server-side; the browser only reads JSON and (optionally) calls a Worker.
There are **two** Workers, and only the second one's source is in this repo: the wishlist
proxy (§12) and `qtpd-reviews`, the Review Digest's `appreviews` passthrough (§17, source in
`worker/`).

The frontend fetches **14 files at load**: the eight data layers above (`games`, `prices`,
`length`, `tags`, `recent`, `playtime`, `ratings`, `updates`), the merged `pics.json`,
`trailers.json` (§2.1), `presets.json` (§11, the preset shelves), and the three static decode
maps in `lookups/` (`tags.json`, `genres.json`, `categories.json`, §9.6). `shots/` (§2.2) is
the one layer fetched **lazily** — one shard per hovered game, never at load; `review_prompt*.md`
(§17) is fetched lazily too, only when the digest modal first opens. `catalog.json`,
`trailers_state.json`, `shots_state.json`, the `*_raw/` shard sets, `pics/`, and the three
generated `.md` files are never served to the browser. Nor, since Sep 2026, is `hltb.json`
(37 MB): HowLongToBeat is calibration input for `length_model.py` (§9.7) and nothing else —
the page shows one review-based **Length** instead of HLTB's Main / +Extras / 100%.

### 2.1 The trailer layer (`trailers.py` → `trailers.json`)

**Why it has to exist at all.** Every other piece of store art QTPD shows is free: capsule
and header URLs are derivable from the appid, which is why the thumbnail and its
hover-enlarge never needed a data layer. Trailers are the exception. A trailer is
addressed by a hashed CDN path that nothing about the appid predicts — Dota 2 (570)
serves its preview clip from
`570/116737/313addee2092d0bd6f538d164610061ea8bbe79c/1749859757/microtrailer.webm`, where
only the leading `570` is derivable. So it gets a file and a job, per §1's one-writer rule.

**Source.** `IStoreBrowseService/GetItems/v1` with `data_request.include_trailers`, the
same batched endpoint `price_and_sale.py` uses for sale end-dates: 50 appids per call, on
`api.steampowered.com` (the large budget, not the 200-per-5-min storefront one). A full
sweep of the catalog is ~2.5k calls ≈ 50 minutes, so the first run drains the whole
backlog and every run after it handles only new releases.

**What Valve actually returns** — established by running the job's own dump mode
(`QTPD_DUMP_TRAILERS=1`, workflow input `dump`) on a runner that can reach Steam, which a
dev sandbox with the domain blocked cannot. Two findings, both of which invalidated the
first implementation:

1. **There is no progressive full trailer any more.** The old `movie480`/`movie_max`
   `.webm`/`.mp4` files are gone. A highlight carries a `microtrailer` (webm **and** mp4)
   — Valve's own ~6s silent loop, the only natively playable asset — and
   `adaptive_trailers`, which are DASH/HLS **manifests** (`dash_av1.mpd`,
   `hls_264_master.m3u8`). Those need dash.js or hls.js; a static-first site should not
   ship a streaming library for a hover preview, so the job records only *how many* apps
   have them (`adaptive_available`) to keep the option visible. The preview we play is
   therefore the microtrailer — which is exactly what Steam itself plays when you hover a
   capsule on its own store.
2. **`trailer_url_format` is relative and uses `${FILENAME}`, not `{FILENAME}`** —
   `"steam/apps/${FILENAME}?t=1762820639"`. The part before the placeholder is a
   CDN-relative path; the `?t=` suffix is a cache-buster and is dropped. The catch that
   cost two probe rounds: that prefix is **not the whole path**. The working URL is the
   video CDN's `store_trailers/` **root** *plus* the learned `steam/apps/` prefix —
   `https://video.cloudflare.steamstatic.com/store_trailers/steam/apps/<filename>` —
   and either segment alone 404s. `probe_hosts` established this with a HEAD sweep over
   a host × root matrix, requiring a `video/*` content-type so an HTML error page served
   as 200 cannot pass. akamai, cloudflare and fastly all serve it.

**Schema tolerance.** `extract_trailer` still refuses to hardcode one key path — it reads
`highlights`, falls back to `other_trailers`, then to any list-of-dicts under `trailers`,
and prefers the legacy progressive tiers over the microtrailer where an app still has
them. But the lesson of the first pass stands recorded: filename conventions were *not*
the stable thing, and the fix came from dumping the real payload rather than reasoning
about it. Dump first, then tighten.

**Files.** `trailers.json` (served, `trailers_v2`) holds hits only —
`{appid: [filename, ...]}`, one flat best-codec-first list from the **first highlight
only** (TF2 exposes 17 trailers; the panel plays one) — plus an absolute `base` prefix, so
a Valve host migration is a data change rather than a code change.
`trailers_state.json` (**not** served) is the queue's memory, `{misses: {appid: ts}}`, so
games with no playable clip aren't re-queried every run; a miss retries after
`QTPD_TRAILER_MISS_TTL` days, because an unreleased game gains a trailer later.
Adaptive-only apps count as misses — they have a trailer we can't play. The first pass
walks the catalog **most-reviewed first**, so the games anyone actually hovers get covered
in the opening minutes rather than at the end of the sweep.

**Frontend.** The clip plays in the enlarged hover popup after a 350 ms dwell, muted,
cross-faded in only once the browser reports `playing`. It no longer loops forever: when it
ends it **hands over to the rotating screenshots** (§2.2), and only restarts itself where a
game has no stills to hand over to. The layer is purely additive — absent, still filling, or
switched off via the **Preview: Video** control, the popup shows the enlarged still exactly
as it did before.

**Frontend, on touch.** Grid view is the view that survives on a phone, and there it plays
**in place** in the card's art rather than in a popup. The art carries two gestures, and
everything else on the card still flips it to the details face:

| gesture | effect |
| --- | --- |
| **tap** | start the preview / stop it and go back to the box art |
| **swipe left** | next media in the playlist |
| **swipe right** | previous media |

Tap alone was a *switch*, not a browser: it could reach the clip and nothing else, while the
screenshots behind it were reachable only by waiting out the rotation. The swipe makes the
whole playlist navigable, and the pips — already drawn for the rotation — become its
read-out, which is why they are enlarged on touch and the current one stretches to a bar.

Three details decide whether that gesture feels right:

- **Horizontal intent is latched during the move, not judged at the release**, so a swipe
  that curves upward as the finger lifts still counts, and a scroll that drifts sideways
  never does. The listeners are `passive` and never `preventDefault` — vertical scrolling
  through the grid has to stay untouched — with `touch-action: pan-y pinch-zoom` on a
  swipeable card so the browser does not claim horizontal drags.
- **A swipe that lands before the playlist has a second item is parked, not dropped.** On a
  tap the list is `[clip]` until the screenshot shard arrives; the direction is remembered
  and applied the moment the stills land. One step is remembered, not a queue.
- **The stray-click guard is a one-shot swallow, not a time window.** Some engines deliver a
  `click` after a drag no scroll consumed, and it would stop the very preview the swipe just
  moved. Chromium (measured) emits none, but iOS Safari is not Chromium. A 500ms window was
  tried first and was wrong in both directions: it swallowed deliberate taps that followed a
  swipe, and bought nothing the one-shot does not.

Three further rules make the tap itself work, and each replaced a bug:

- **The tap contract is a separate, persistent class** (`.hasclip`, painted at render) from
  the playback cycle's own `.hastrailer`. When they were one class, stopping a preview — or
  the cycle merely reaching its first screenshot — stripped it, the art silently went back to
  being a flip target, and a clip could be watched exactly once per render.
- **The badge is a real button** (a gold disc), not a small glyph on a chip, because on touch
  there is no hover to discover the clip with: the badge *is* the discovery path. While a
  preview runs it becomes the stop control, so art↔clip is somewhere you can move both ways.
- **The give-up watchdog measures silence, not elapsed time.** It exists for the clip that
  will never start (autoplay refused, codec unsupported, dead CDN edge), which never fires
  `ended`. It was a flat 1.4 s from the moment the element was built — and on a phone 1.4 s is
  not "never started", it is "still downloading", so a deliberately tapped trailer was
  discarded mid-flight and the preview opened on screenshot #1. Any sign of life from the
  element now resets the window (1.4 s of total silence, 6 s between signs, 20 s ceiling).
  Detecting a genuinely dead clip promptly is a separate job and needs care: with `<source>`
  **children** a failed candidate is silent on the `<video>` — Chromium fires no `error` on
  the element and rejects no `play()`; each candidate errors on its own `<source>` and the
  element lands in `NETWORK_NO_SOURCE`. Watching the candidates, a 404 now hands over in
  ~150 ms, faster than the old flat timer, while a slow one is waited for.


### 2.2 The screenshot layer (`shots.py` → `shots/shard_NN.json`)

**Why it exists.** §2.1's preview answers "what does this game look like in motion" with
Valve's ~6s microtrailer — and for a great many games that clip is a logo sting with no
gameplay in it at all. Worse, **4,597 games (3.6% of the catalog) have no playable video
whatsoever**; on those the panel was an enlarged piece of key art and nothing more. Store
screenshots close both gaps: they play *after* the clip, and they *are* the preview where
there is no clip.

Like trailers, they cannot be derived. A screenshot is addressed by a content hash
(`ss_<sha1>.jpg`) that nothing about the appid predicts, which is exactly what index.html's
T6 fallback chain records — *"no screenshot step — screenshot URLs aren't derivable from
appid"*. So: one more file, one more job.

**Why not the PICS field we already have.** PICS carries `store_screenshot` and
`pics_refresh.py` already fetches it, so it looks free. Measured across all 64 `pics_raw`
shards, it is unusable: present for **14,975 of 126,742 apps (11.8%)**, and it is one image
rather than a set. Valve stopped populating it — 61–63% of 2016–2018 releases carry it,
29% of 2019, 0.8% of 2020, ~0.0% of 2023 onward — and it is *inversely* correlated with
popularity, with only **3 of the top 500 most-reviewed games (0.6%)** carrying one, because
the maintained, `store_item_assets`-migrated store pages are precisely the ones that
dropped it. It is a legacy field, dead for anything current.

**Why one job and not two.** The `appdetails` blob `scraper.py` already fetches per game
(`scraper.py:588`) contains a `screenshots` array that is currently discarded, and the
scraper walks the whole catalog every ~50 days (measured: p50 re-scrape age 42d, max 54d),
so harvesting it there would have been free and would have covered new releases within ~6h
instead of ~24h. It was **rejected on §1 grounds**: it would make two jobs write one data
layer, and one-writer-per-file is what lets every scheduled job push concurrently without
coordination. The trade bought ~18h of freshness on ~100 games/day at the cost of the
invariant, which is not a trade worth making — `select_work` already queues never-checked
appids first on every daily run, so new games are covered by this job alone.

**Source.** `IStoreBrowseService/GetItems/v1` with `data_request.include_screenshots` — the
same batched endpoint as §2.1 and `price_and_sale.py`, 50 appids per call, ~2.5k calls ≈ 50
minutes for a full sweep. Catalog order is **most-reviewed first**, so the games anyone
actually hovers are covered in the opening minutes.

**Coverage.** Screenshots are effectively mandatory — Valve requires a minimum of five to
publish a store page, where a trailer is optional and `trailers.json` still resolved 96.4%
of the catalog — so this layer was predicted to land at or above that. The first real sweep
bore that out: **78,213 hits in the first ~50 minutes**, against a queue walked
most-reviewed first.

**What the first sweep corrected, and the lesson it repeats.** The key path was right:
`screenshots.all_ages_screenshots[] = {filename, ordinal}`, and `extract_shots`'s
tolerance (named key, then any non-mature list-of-dicts, pulling
`filename`/`path_thumbnail`/`path_full`/`path`/`url` values) was never exercised. **The
join was wrong.** Valve returns filenames *already rooted* at `steam/apps/<appid>/<file>`,
and the shipped base also ended in `steam/apps/`, so every URL came out as
`.../store_item_assets/steam/apps/steam/apps/<appid>/ss_….jpg` — doubled, and 404 on every
row.

This is §2.1's trap seen from the other side. There, the learned prefix was *not the whole
path* and a segment had to be **added**; here a segment had to be **removed**. The
generalisable point: **the split between "base" and "filename" is Valve's to decide, not
ours to assume**, and no amount of reasoning about it substitutes for looking at one real
response. The dump mode exists precisely to make that look cheap; skipping it cost a
78k-row sweep that had to be repaired afterwards rather than five minutes up front.

Three things came out of the fix:

1. **`CDN_HOST` stops at the `store_item_assets/` root.** **Confirmed** on a runner
   (`probe_shot_hosts`, run 32365480684): joined via `join_url` this base returns
   `200 image/jpeg` for both rootings in the wild — `…/steam/apps/730/ss_<sha1>.jpg` and
   `…/steam/apps/578080/<sha1>/ss_<sha1>.jpg` — from `shared.cloudflare`, `shared.akamai`
   and `shared.fastly` alike. Worth recording why the probe samples one filename **per
   distinct rooting**: `cdn.cloudflare` + `steam/apps/` serves the flat shape and 404s the
   hash-dir one, so a single-sample probe would have blessed a base that is correct for
   only part of the catalog.
2. **`_clean_filename` normalises every input to one rooting.** A full URL, a legacy
   `cdn.…/steam/apps/…` URL and the relative form all reduce to `steam/apps/<appid>/<file>`,
   so the frontend never has to work out which of several rootings it is holding.
3. **The base is self-healing.** `load_shots` returns every shard whose stored `base` no
   longer matches `CDN_HOST`, and those start the next run pre-dirtied. Without it a base
   correction could never reach rows already committed — a hit is never re-queried, so the
   broken URLs would have survived every future run until someone forced a full re-sweep.

`probe_shot_hosts()` (HEAD across a host × root matrix, requiring an `image/*` content-type
so an HTML error page served as 200 cannot pass) remains under `QTPD_DUMP_SHOTS=1` for the
next time the path shape moves.

**Adult content.** Only `all_ages_screenshots` is stored. Valve splits mature stills into
`mature_content_screenshots`, and a game can carry those *without* tripping the frontend's
PICS-based 18+ gate (`pics_summarize` `adult` = content_desc 3/4) — in which case the
rotation would show ungated mature images on a thumbnail that was never blurred. Storing
only the all-ages set makes that impossible by construction; a game offering nothing else
is recorded as a miss, and the run log counts those separately from genuine empties.

**Files, and why this one is sharded.** `shots/shard_NN.json` (served, `shots_v1`, 64
shards by `appid % 64`) holds hits only — `{appid: [filename, ...]}`, capped at
`QTPD_SHOTS_PER_GAME` (default **4**) in Valve's own `ordinal` order, which is the
developer's pick of what represents the game best. Each shard carries its own absolute
`base`, so a host migration stays a data change — and a *changed* base re-dirties every
shard that still carries the old one, so the correction propagates on its own. This is the one layer that departs from
§2.1's single flat file: a trailer is one short filename list per game and the whole map is
small enough to ship at load, whereas screenshots are ~4 hashes per game (~25 MB across the
catalog) and **only the row you actually hover ever needs them**. Shipping that eagerly
would add ~10% to a page that already pulls ~240 MB of JSON, to serve hovers that mostly
never happen. Checkpoints rewrite **only the shards touched since the last save**, so a few
hundred new rows produce a small diff rather than a 25 MB one on a repo Pages serves live.
`shots_state.json` (**not** served) is the queue's memory, `{misses: {appid: ts}}`, retried
after `QTPD_SHOTS_MISS_TTL` days because a freshly-listed game gains screenshots later.

**Host resilience.** The stored `base` names one CDN edge, but three serve byte-identical
files. When a frame fails to load, `startShots` retries the same path across `SHOT_HOSTS`
(akamai, cloudflare, fastly) before treating it as dead — because a viewer who cannot reach
one edge would otherwise get a panel that silently never starts, which is indistinguishable
from the feature being broken. `CDN_HOST` leads with **akamai** for the same reason: it is
the host `ASSET_CDN` already pulls every modern header image from, so it is the one edge the
page continuously demonstrates works for whoever is looking at it. Picking a host the app
had never used was the original mistake here — it passed every server-side check and still
showed nothing in a browser.

**Frontend — one cycling playlist.** The panel runs a single list, `[clip, still, still,
still, still]`, and wraps back to the clip. It did not start that way: the clip played once
and handed over to the stills for good, which lost the moving footage — the part that
actually tells you what a game *is* — for the rest of the hover. A game with no clip is the
same list minus its first item; a game with no stills is a one-item list that repeats,
which is §2.1's original looping behaviour. Every entry point (the dwell, a finished clip,
a still's timer, a click) goes through `showMedia()`, so one place cancels the previous
item's timer and one place paints.

**Clicking the thumbnail steps forward.** The *thumbnail* is the target, not the panel: the
panel is `pointer-events:none` and positioned clear of the thumb, so the thing you are
already hovering is the thing you click and the hover never breaks. The 18+ gate keeps
priority — and it cannot simply rely on being registered first, because both listeners sit
on `document.body`, where `stopPropagation` does **not** stop a sibling listener on the same
node; the rotation's handler defers to the gate explicitly.

The clip starts on the 350 ms dwell **without waiting for the shard fetch**; the stills
append to the playlist when they arrive and the pips rebuild. Making the video wait on a
~400 KB fetch would trade away the responsiveness the dwell exists to protect. The two
cross-fade layers are built on first use, so a clip-only game — or any game before its
shard is populated — never gets empty `<img>` elements for having been hovered.

`joinShot()` concatenates `base` + filename after dropping the longest run
of leading path segments the base already ends with. That is not defensive
over-engineering: shards written under the old doubled base are committed and being served,
so the join must resolve both rootings to the same URL — which is also what let the fix ship
without waiting for all 64 shards to be rewritten. One control governs every kind of panel
motion: the existing **Preview:
Video** toggle (and the `prefers-reduced-motion` override inside `trailersOn()`) suppresses
the rotation too — a cross-fading slideshow is motion, and turning video off must not leave
the panel animating. Where a game has a clip, the clip plays once and then hands over;
where it has none, the stills start after the same 350 ms dwell, so dragging the pointer
down the table still costs nothing. Frames cross-fade at 1.8 s through **two stacked `<img>`
layers** — a single element would swap instantly, and fading one element out and back in
would flash the header art between every pair — with dot pips showing position in the set.
Nothing is prefetched: the next frame is requested only when the current one is displayed,
for the same reason the trailer waits for a dwell. Screenshots are true 16:9 and so fill the
512×288 panel with no crop, unlike the ~2.14:1 header art. The rotating stills sit inside
the same `.pop` the 18+ blur already targets, so the adult gate covers them automatically.
The layer is purely additive: before the first run, on a shard 404, or on a game with no
stills, the panel behaves exactly as it did before.

---

## 3. Community feedback & future work

**Moved. The planning material lives in [ROADMAP.md](ROADMAP.md) — read it there.**

This section used to hold the whole backlog (§3.1 new data sources · §3.2 frontend/UX ·
§3.3 nice-to-have · §3.4 the Lorenzo list · §3.5 loose ends). It was split out so a planning
session need not read the reference doc and a reference lookup need not scroll past a
backlog — but the text was left behind here as well, and the two copies then **drifted**:
by Sep 2026 the duplicate still carried a superseded reading of the `updates.json`
precedence switch, among other differences. Two copies of a backlog is one copy too many,
so this one is now a pointer.

**The section numbers did not move.** A citation of **`§3.1`–`§3.5`** anywhere in this repo
means *ROADMAP.md*; every other `§N` means this file. That asymmetry is deliberate —
~22 cross-references point at `§3.x` and rewriting them all buys nothing.

| Looking for | Go to |
|---|---|
| New data sources / metrics (scraper work) | ROADMAP.md §3.1 |
| Frontend / UX items needing no new scraping | ROADMAP.md §3.2 |
| Nice-to-have / still being evaluated | ROADMAP.md §3.3 |
| The Lorenzo list (filter/view patterns, shipped) | ROADMAP.md §3.4 |
| Loose ends noted elsewhere in this doc | ROADMAP.md §3.5 |
| What is open right now, and what blocks it | ROADMAP.md → *Status index* |

Two other planning surfaces sit outside ROADMAP.md and are **not** duplicated here either:
`docs/ONBOARDING_PLAN.md` (the 18 scored onboarding solutions and their build log) and
`INESKA_IMPROVEMENTS.md` (an outside reviewer's cold-arrival findings, all 23 shipped).
The as-built record for anything that *has* shipped stays in this file — §16 is the
changelog.

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
| `0.1` | Manual diagnostic against the live site's backend, not a pipeline stage | `review-probe.yml` (§17) |
| `1.` | The catalog scraper — the only finder of new games | `scrape.yml` |
| `2.x` | Refreshers — enrich games the scraper already found | prices (2.1), recent (2.2), playtime-raw (2.3), updates (2.4), hltb (2.5), tags (2.6), pics (2.7), pics-new (2.7b), trailers (2.8), shots (2.9) |
| `3.x` | Summarizers — pure local recompute, `[2.3 / manual]` marks that job 2.3 is their real trigger | playtime-summary (3.1), playtime-ratings (3.2), length-fit (3.3, weekly), length-apply (3.4) |
| `4.x` | Monitors / generated files | shard-health (4.1), coverage (4.2), freshness (4.3), presets (4.4) |
| `[ONE-OFF]` | Run-once utilities, deletable when drained | `queue-null-updates.yml` |

**22 workflow files, 21 numbered** — only the `[ONE-OFF]` sits outside the sequence, which is
deliberate: it isn't part of the standing pipeline. **15 of the 22 are cron-scheduled**; the
rest fire on `workflow_run` (coverage 4.2, presets 4.4) or by hand (review-probe 0.1,
playtime-summary 3.1, playtime-ratings 3.2, length-apply 3.4, queue-null-updates). Numbering is cosmetic —
nothing keys off it — with one exception: the `workflow_run` triggers in `coverage.yml` and
`presets.yml` match the scrape workflow's **exact `name:` string**, so renaming `scrape.yml`
silently breaks both (see the callout below).

**4.4 is the odd one in the `4.x` tier.** The other three monitors write Markdown for a human;
`presets.yml` writes `presets.json`, which the frontend actually downloads. It sits here anyway
because what it *does* is the monitor pattern — a stdlib-only local recompute that measures
hand-authored thresholds against the live data and reports, never re-tunes (§11, *Preset
shelves*).

> **One more workflow appears in the Actions sidebar that is not in this repo:
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
| `trailers.py`         | `trailers.json`     | daily `13 5 * * *` | Batched `GetItems`; backlog-drain, not a refresh loop (§2.1). + `trailers_state.json`. |
| `shots.py`            | `shots/shard_NN.json` | daily `47 5 * * *` | Batched `GetItems`; backlog-drain like trailers (§2.2). + `shots_state.json`. |

**Non-Steam scrapers** (hit their own sites, so no Steam-budget contention):

| Workflow / script   | Owns file   | Source    |
|---------------------|-------------|-----------|
| `hltb_refresh.py`   | `hltb.json` | HowLongToBeat — **calibration input only** since Sep 2026 (§9.7); not served to the page |
| `tags_refresh.py`   | `tags.json` | SteamSpy  |

**Summarizers** (pure local recompute — read one file, write another; **no Steam calls**,
so they touch no rate budget). The first two read the sharded `playtime_raw/` set; the Length
model reads `playtime.json` (plus `pics.json`, and `hltb.json` when fitting). Each writes its
own file, so one-writer-per-file holds:

| Script                  | Writes         | Trigger                              | Concurrency group |
|-------------------------|----------------|--------------------------------------|-------------------|
| `playtime_summarize.py` | `playtime.json`| chained step in `playtime-raw.yml`   | (inherits `steam-playtime-raw`) |
| `ratings_summarize.py`  | `ratings.json` | chained step in `playtime-raw.yml`   | (inherits `steam-playtime-raw`) |
| `length_model.py`       | `length.json`  | chained step in `playtime-raw.yml` (after the two above); manual `length-apply.yml` (3.4) | (inherits `steam-playtime-raw`) / `steam-length` |
| `length_model.py --fit` | `length_coefs.json` (+ re-applies `length.json`) | `length-fit.yml` (3.3), `17 3 * * 1` weekly | `steam-length` |

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
| `freshness.yml`     | `freshness.py`    | `FRESHNESS.md`| `0 7 * * *` (daily, the morning oversight pass)  | `freshness-md`    |
| `presets.yml`       | `presets.py`      | `presets.json`| `workflow_run` after **scrape** succeeds (~4×/day) | `presets-json`    |

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

`freshness.py` is the third generated doc (§11.6). It answers the *time* question the coverage
snapshot doesn't: for every scheduled task, when it last actually wrote its file, when its cron
fires next, and how big the gap between those two gets. It reads the `cron:` and
`timeout-minutes` lines out of `.github/workflows/` directly — a schedule change shows up in the
doc on the next run with no edit here — and imports `coverage.py` for the cooldown constants and
bucketers, so the two docs can never disagree about what "overdue" means. Daily at **07:00 UTC**,
deliberately behind 4.1 (06:35) and an hour after the 06:00 scrape slot, so it reads a settled
tree.

`presets.py` is the fourth generated artifact and the only one the browser downloads (§11,
*Preset shelves*). It re-implements the frontend's `passFilters()` in Python over
`games.json` + `prices.json` + `pics.json` + `lookups/tags.json`, measures each hand-authored
shelf against the live catalogue, and writes a live count, the shelf's opening titles and a
`thin` / `broad` / `no-qtpd` flag per shelf. **It reports, it never re-tunes** — the
thresholds stay in `PRESETS` under version control, and a shelf that has gone thin surfaces
as a `::warning` in the Actions step summary for a human to decide about. Two things it
*does* enforce at build time, as hard `SystemExit`s: every shelf must set `adult=hide`, and
every param a shelf names must be one `loadFromURL()` actually reads.

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
`is_free` / `discount_pct` are **reconciled against the prices at capture** — a live free-to-keep
promo answers `is_free: true` + `discount_percent: 100` on top of the full price, and stored
verbatim that snapshot outlives the promo (§15).

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

A handful of rows carry three extra keys — `price_src: "package"`, `pkg_name`, `pkg_count`.
These are apps Steam quotes **no app-level price** for because their store page fronts
several distinct products; the shared Call of Duty launcher (`1938090`) sells Modern
Warfare 4 and Black Ops 7 side by side, so `appdetails?filters=price_overview` returns
success with an empty data block. The price is then the **cheapest** qualifying package
from GetItems and the frontend renders it `from $X`. Qualifying is deliberately strict —
the package must sit in a *named* `package_group` (not `default`, not a `display_type: 1`
dropdown) — because the loose reading produces confidently wrong prices: delisted apps
offer their successor in the default group (Half-Life 2: Deathmatch → "The Orange Box",
ARK: SOTF → "ARK: Survival Evolved"), dead games offer leftover DLC, and dropdown groups
are in-game currency (CoD Points would price Call of Duty at $1.99). A wrong price is
worse than none here — it feeds QTPD, sorting and the CSV — so ambiguity keeps the `—`.

Rows with no price instead carry **`avail`** — why there is none — plus `avail_at`:

| `avail` | meaning | rendered |
|---|---|---|
| `only` | not sold on its own; the page's sole purchase option is `only_name` at `only_price` (Horizon Zero Dawn Complete Edition → the Remastered Bundle) | `only $49.99` |
| `notsold` | no package on Steam at all — what a delisting looks like from outside (FIFA 22, Ori and the Blind Forest) | `not sold` |
| `unknown` | unpurchasable but a package exists — usually a free app Steam mislabels (It Takes Two Friend's Pass) | `—` |

`only_price` is display-only and never becomes `price_final`: a bundle's price is not the
game's price, and letting it through would poison QTPD and the price sort.

Splitting `notsold` from `unknown` costs one **per-app** call — `appdetails` batches only
with `filters=price_overview`, and asking for `packages` across several appids is a hard
400. GetItems cannot substitute: a free app and a delisted one are byte-identical there,
both `visible: true` with zero purchase options. It is worth the calls — on a 26-app sample
4 (15%) were free-or-otherwise-available and would have been labelled delisted. The cost is
contained by caching the verdict in `prices.json` and rotating: verdicts younger than
`AVAIL_TTL` (7d) are carried over on rebuild, and at most `AVAIL_MAX_PER_RUN` (60) stale
ones are re-checked, oldest first — enough to cycle the ~400-app bucket about daily.

**`hltb.json`** — **backend-only since Sep 2026**: the page no longer downloads it; its one
consumer is `length_model.py --fit`, which reads `raw.extra` only (§9.7).
`{ generated_at, count, "hltb": { "<appid>": { main, extra, complete, avg,
match, fetched_at, raw: { main, extra, complete }, est?: ["extra", …], attempts?: N } } }`.
The four time fields are **unprefixed** (`main`, not `hltb_main`). `raw` holds the ground-truth
values as returned by HLTB; the top-level values may include estimates filled from the typical
main/extras/completionist ratio (§8 — corpus-wide, magnitude-bucketed, not genre-based despite
the name this feature used to go by). Three keys are conditional or easy to miss:
- **`est`** (present on 18,829 entries) lists which of the three legs were estimated — this is
  what drove the blue flag while the page showed HLTB. It is `est`, not `hltb_est`; reading the wrong name is what
  produced an old "0 estimated" claim in COVERAGE.md (§8).
- **`attempts`** (present on 90,704 entries — every blank) is the miss counter that drives
  Phase B's attempt-scaled blank-retry curve (§8.1). Incremented on a miss, cleared on a match.
- **`match`** is the HLTB title actually matched (or `null`), useful for auditing a bad match.

`fetched_at` drives the priority re-scrape ordering.

**`length.json`** — the page's one game-length figure, owned by `length_model.py` (§9.7):
```
{ "generated_at", "coefs_generated_at", "k": 20, "min_up": 3, "cap_h": 1000,
  "genres": ["Racing", "Sports", …, "Casual", "none"], "balance_tags": ["Idler"],
  "_format": ["hours", "n_up", "genre_idx", "raw_hours?", "reason? (cap|idler)"], "count",
  "length": { "<appid>": [ hours, n_up, genre_idx ]                       // normal row
              "<appid>": [ hours, n_up, genre_idx, raw_hours, "cap"|"idler" ] } }  // adjusted
```
`n_up` is the number of recommending reviews behind it (the tooltip quotes it); `genre_idx`
indexes `genres`. Elements 4–5 appear **only** on adjusted rows: `raw_hours` is the ▲-only figure
the reviews literally gave, `reason` says why it was not used. ~96k rows, ~2 MB.

**`length_coefs.json`** — the weekly fit (`length_model.py --fit`): `global` and per-genre
`{ coef, typical_up_h, n_fit, n_typical, fallback }`, the constants the fit used (`k`, `min_up`,
`cap_h`, `fit_min_up`, `typical_min_up`, `min_genre_games`, `genre_priority`) and an `accuracy`
block (share within ±25 % / ±50 % / 2× of real HLTB extra, all games and ≥100-fan games) so a
drifting model is visible in the weekly diff.

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
  "sliver_n": 250,           // ...and grayed anyway if n < this AND n < sliver_frac × the
  "sliver_frac": 0.02,       //    storefront's own review count (the sliver gate, §10)
  "cap_mult": 2.0,           // per-review playtime capped at 2× the game's median
  "per_game_cap": 3000,      // newest-N ring buffer per game, from playtime_refresh.py
  "_format": ["steam_pct", "raw_pct", "capped_pct", "n", "span_days"],
  "count",
  "playtime_ratings": { "<appid>": [ steam_pct, raw_pct, capped_pct, n, span_days ] } }
```
See §10 for what the three percentages mean and why. **`span_days` is the fifth element and a
later addition** (Sep 2026) — the calendar span of the stored sample, newest review minus
oldest. The format is **additive on purpose**: a page reading an older four-element file just
drops the clause that quotes the span rather than printing `undefined`.

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

**`presets.json`** — owned by `presets.py` (§4, §11):
```
{ "_format": "presets_v1", "generated_at", "min_healthy": 25, "broad_warn": 20000,
  "presets": [ { id, label, blurb, tone: "popular"|"niche", query,
                 count_at_scrape, scored, sample: [title, …], flags: ["thin"|"broad"|…] } ],
  "problems": [ "<id>: <flags> (<n> results)", … ] }
```
`query` is the **only** load-bearing field — it is the querystring the chip applies, and the
page then shows whatever is live. `count_at_scrape` / `scored` / `sample` / `flags` /
`problems` exist for the generated health report and are deliberately **not rendered**: the
count comes from a Python re-implementation of `passFilters()` that can drift from the page,
and a chip quoting a number the page did not compute is a chip that can lie (§11).

**`lookups/{tags,genres,categories}.json`** — small static ID→name maps for decoding the PICS
IDs client-side. Committed, refreshed manually via `pics_lookups.py` / `build_category_map.py`.

**`review_prompt.md` / `review_prompt_simple.md` / `review_prompt_html.md`** — not data files
and no job writes them; they are the Review Digest's hand-authored prompts, fetched lazily by
the browser when the digest modal opens, each carrying a `<!-- vN -->` version line echoed into
the bundle header so an output can be traced to the prompt that produced it (§17).

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
  It reads `games.json` for one thing only — which appids to price — and takes every game
  that is **not free, plus any free-flagged one that still carries a price** (§15): skipping
  free games wholesale is what let promo snapshots fossilise, since the stale flag itself
  suppressed the re-check that would have disproved it.
- **`tags_refresh.py` → `tags.json`.** SteamSpy tags (SteamSpy, not Steam).
- **`recent_refresh.py` → `recent.json`.** The 30-day rolling review score, on an offset
  cron so it stays fresh; `RECENT_COOLDOWN_DAYS` controls how stale a score must be to
  re-check. The frontend computes the recent-vs-all-time **trend** (improving / stable /
  declining) and gates it on staleness.

---

## 8. HLTB subsystem (`hltb_refresh.py` + `hltb_estimate.py` → `hltb.json`)

> **Backend-only since Sep 2026.** The page no longer shows HowLongToBeat: its Main / +Extras /
> 100% / Avg were replaced by one review-based **Length** (§9.7). Everything below still runs —
> the scraper, matcher and estimator are unchanged — but its output now has one consumer, the
> weekly Length calibration, which reads real `raw.extra` values only. The `est` estimates and
> the blue flag no longer reach a user.

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

**The first touch is a *sized* stake, not a flat page (Sep 2026).** Phase 0 of a run serves the
whole never-seen frontier across all shards before the normal rotation starts —
`FIRST_TOUCH_BATCH` (300) games, batched **by shard** so peak memory is still one shard and
one-writer-per-file is untouched — because a first touch is the cheapest visit there is
(`FIRST_TOUCH_TARGET = 100 == PER_PAGE`, so page 1 satisfies it and the walk breaks
immediately). That page is sized for the game it *usually* lands on: a release taking a handful
of reviews a day, where 100 reviews is most of the game and days of its life.

On a big launch it buys something else entirely. WARDOGS (appid 1867240) launched into ~14
reviews a **minute**, so its 100-review stake was written inside a **seven-minute window** of
launch evening — 0.17% of a 60,202-review game — and it then sat there for 5 days waiting for
its shard to open (~46 h median, ~81 h worst), rating 48.3% against Steam's 81%. `ratings.json`'s
sliver gate now refuses to show such a sample at full confidence (§10), **but a greyed number is
a number we failed to produce** — the real fix is not to take a seven-minute sample at all.

So the stake is sized by what the catalog already knows: a game whose `games.json` review count
is ≥ **`FIRST_TOUCH_HOT_REVIEWS` (10,000)** is walked to **`FIRST_TOUCH_HOT_TARGET`** — rung 1 of
`DEPTH_LADDER`, exactly what its first *normal* visit would have given it, just taken on day one
instead of two days later. Cost is bounded three ways and stays small: games this popular are
rare in the frontier (5 of 95,297 rated games sat at exactly 100 with >10k reviews when this was
measured), `FIRST_TOUCH_HOT_MAX` (25) caps deep touches per run, and the sweep's existing
`time_left()` check still ends the phase when the budget runs out. A hot touch is ~10 pages /
~15 s at `STEAM_DELAY`, against ~1.5 s for a cold one. A `None` review count is treated as cold
— no evidence the game is big.

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

## 9.7 Length model (`length_model.py` → `length.json`, `length_coefs.json`)

**What the page calls Length.** One figure per game: hours to play it through — the story plus
some side content — estimated from **▲**, the median playtime of reviewers who *recommend* the
game (`playtime.json`), and calibrated per genre against real HowLongToBeat **extra** times.
It replaced the three HLTB values in Sep 2026. QTPD, the free-value score, the Length range
filter, the Length sort and the CSV all read it through `hoursFor()`. The research behind every
choice below is in `LENGTH_MODEL.md`; the change inventory in `LENGTH_PLAN.md`.

**Why this shape** (measured on ~69k games, Sep 2026):
- ▲ tracks HLTB better than ▼, the pooled median, or any average of ▲ and ▼ (rank correlation
  0.83 vs HLTB completionist, against 0.81 for the ▲/▼ mean and 0.67 for ▼). A regression gives
  ▼ a weight of ~0 once ▲ is known — non-recommenders' hours measure quitting, not length.
- Uncorrected, ▲ ≈ HLTB **extra** (ratio 1.09), closer than main (1.48) or completionist (0.82),
  and extra has the least scatter after calibration — so extra is the target.
- **One genre per game.** The first of `Racing › Sports › Strategy › Simulation › RPG › Action ›
  Adventure › Casual` the game carries (Indie / F2P / Early Access / MMO / adult are not types);
  else `none`. On a 20-split holdout: one genre ×1.328 typical error vs global ×1.334; averaging
  top-5 tags ×1.321; *stacking* tag coefficients ×1.350 — worse than no genre at all.
- **Top-up to K = 20.** Below 20 fans, each real review counts 1/20 and the rest comes from the
  genre's typical ▲, blended in log space so every real review moves the answer. K = 20 tested
  best (within-2× 52.7 % on <50-fan games vs 50.4 % with no top-up); K = 50 erased the gain.

**Formula (apply, every 2.3 pass).**
`Length = coef[g] × 10^( w·log10 ▲ + (1−w)·log10 typical[g] )`, `w = min(n_up, 20) / 20`.
**Floor:** 3 recommending reviews (the same floor `playtime.json` publishes a median at).

**Outlier guard and idlers.** ▲ includes time a game spends **running in the background** —
idle games, achievement / trading-card farming. One $0.99 title's 47 fans had a ~11,000 h median
(detractors: 0.3 h), which made its Length 9,074 h, its QTPD 7,058 and put it at #1 site-wide.
So, for (a) any game whose Length would pass `LENGTH_CAP_H` (1,000 h) and (b) every game tagged
**Idler** (`BALANCE_TAGS`; PICS tags, SteamSpy fallback), ▼ is brought in as a check:
`Length = coef × √(▲_blended × ▼)` — a geometric mean, since an arithmetic one leaves
11,000 h and 0.3 h at ~5,700 h — and the result never exceeds 1,000 h. That title lands at 44 h;
a genuinely endless game whose detractors also played for ages (Granado Espada, ▼ 1,328 h)
stays at the cap. Idlers without a ▼ median keep the ▲ figure, capped. Adjusted rows carry the
raw ▲ figure and a reason (`cap` / `idler`) so the page can show both — a blue dotted underline
and a tooltip that quotes the raw number and says why it is not believable (Sep 2026: 5 capped,
~2,400 idlers).

**Tightened (Sep 2026, owner's call): cap 420 h, balance from 100 h.** `LENGTH_CAP_H` = 420 h —
14 h a day for 30 days, the most a person plausibly plays — and `BALANCE_ABOVE_H` = 100 h: any
game whose ▲-based Length passes 100 h is balanced too, not only those past the cap. Order is
**clamp first, then blend**: `Length = min(capped, √(capped × coef·▼))` with `capped =
min(coef·▲_blended, 420)`, so the blend never raises a figure (a game whose detractors played
longer keeps its fans' Length). New reason `long` for rows balanced between 100 h and the cap.
Dry run on the data at the time: 760 games change — 33 `cap`, 381 `long`, 2,059 `idler`; Granado
Espada 1,000 → 420 h, MIR4 844 → 230 h, FINAL FANTASY XIV Online 442 → 149 h. The cap and the
threshold are policy, not fitted values: apply overrides whatever `length_coefs.json` carries.

**Fit (weekly, 3.3).** Coefficient per genre = geometric median of `HLTB raw.extra / ▲` over
games with ≥ 30 fans; typical ▲ = median ▲ of games with ≥ 50 fans. A genre with < 100
calibration games uses the global values. Only `raw` HLTB values — fitting to our own HLTB
estimates would be circular. A coefficient moving > 10 % in one fit logs a WARNING; the
`accuracy` block in `length_coefs.json` records within-±25/50 %/2× against real HLTB extra.
First fit: global 0.90; Racing 1.12 · RPG 0.96 · Strategy 0.96 · Action 0.93 · Simulation 0.90 ·
Sports 0.89 · Adventure 0.83 · Casual 0.80 · none 0.85. In-sample 77 % within 2× (84 % for
games with ≥ 100 fans).

**Honest limits.** Genre is a small correction (~1 point of accuracy); the number of reviews
matters far more — under 10 fans a Length is close to its genre's typical figure. `playtime_forever`
is a *live* total, so ▲ drifts up as reviewers keep playing. Endless / multiplayer games get a
Length like any other (owner's decision); the length shelves keep excluding them by tag.

## 10. Weighted rating (`ratings_summarize.py` → `ratings.json`)

A review rating where each vote counts in proportion to how long that player actually
played — a 300-hour recommendation should outweigh a 20-minute one. It sits **next to**
Steam's flat % (it is a *metric*, not a sort-only concept), in the **Weighted** column.

Reading the same sharded `playtime_raw/` set, per game it stores **three** percentages plus the
sample size and its calendar span, `[steam_pct, raw_pct, capped_pct, n, span_days]`:

- **`steam`** — the plain one-vote-per-review % **of the stored sample**. The key name is a
  historical misnomer and cost a real bug: it is *not* Steam's published score, and on a big
  game the two diverge hard (New World: Aeternum reads **27.7%** here against a published
  **67%**, because the sample is the newest 3,000 reviews and the published figure is
  all-time). Treat it as "the same sample, unweighted" — it is the correct baseline for
  measuring what playtime weighting *moved*, and the wrong number to print under Steam's name.
- **`raw`** — uncapped playtime-weighted % = recommend-hours ÷ total-hours. Kept for
  debugging, but **whale-distorted**: one obsessive can dominate.
- **`capped`** — the same, but each review's playtime is capped at **2× that game's median**
  before weighting. This is the **intended display value** — a *relative* dampener that
  scales to each game's nature and neutralizes whales while staying in a sane range.
- **`span_days`** — newest review minus oldest, in days. `n` alone does not say what the
  rating measured: `n=3000` is four years of reviews on a back-catalogue game and four days on
  a launch-week hit. See the sliver gate below for the case this was added to catch.

**Confidence, not smoothing.** Bayesian smoothing was prototyped (a data-driven prior worth
~10 reviews' hours) and then **deliberately dropped**: reliability is conveyed by a
**gray-out cue**, not by nudging the number toward a prior. The thresholds:
`MIN_REVIEWS_FOR_RATING = 5` (below this, no rating at all), `CONFIDENT_REVIEWS = 10`
(≥ renders full-color; **5–9 renders grayed** as low-confidence). Lowering the compute floor
to 5 is what makes the gray state actually render for the shakiest games. The frontend reads
`confident_reviews` from the meta and colors accordingly, and shows a **delta badge** for what
playtime weighting moved — `capped` against the same sample's unweighted `steam` (e.g. `Δ-5`
when long-playtime detractors drag the weighted rating below the flat count).

**The sliver gate (Sep 2026).** `CONFIDENT_REVIEWS` is an **absolute** floor, and it cannot see
the failure it most needs to. `playtime_refresh.py`'s phase 0 spends exactly one page —
`FIRST_TOUCH_TARGET`, 100 reviews — on every never-seen game, so a new release gets a number on
day one instead of waiting ~46 h for its shard to open (§9). On a quiet release that page is
most of the game; on a busy one it is a sliver. WARDOGS (appid 1867240) launched into ~14
reviews a minute, so its stored 100 were all written inside **seven minutes** of launch
evening — 0.17% of a 60,202-review game — and the Weighted column rendered **48.3% in full
colour** beside a row reading 81%. `n = 100` clears `n ≥ 10` fifty times over. The sample's own
unweighted score was 46.0%, so the weighting was not what put it 35 points under Steam: the
sample was.

A sample is a sliver when it is **both** small in absolute terms **and** a tiny share of what
the storefront counted — `SLIVER_N = 250` **and** `SLIVER_FRAC = 0.02`. One test without the
other is wrong in both directions: a 40-review game sampled 40 times is *complete*, and a
3,000-review sample of Counter-Strike 2 is 0.03% of the catalogue count but a perfectly good
read on current sentiment. Both constants ship in the meta so the page cannot drift from them.
Measured over the live file on 2026-09-16 the gate moves **5 games out of 95,297** beyond what
the absolute floor already caught, every one a first-touch stake on a popular release. It is a
scalpel, not a net — which is what it should be.

**Not a defect, and deliberately left alone:** a big game's legitimate newest-3,000 window. New
World: Aeternum reads 37.2% against an all-time 67% because its 30-day score is 23% — the
sample is right and the all-time number is the outdated one. Across 1,491 big games with a
solid 30-day score, the sample sits closer to the 30-day figure (mean |diff| **2.9**) than to
the all-time one (**4.1**). That gap is a *labelling* problem, now labelled in the tooltip
(§11), not a confidence problem.

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

**QTPD computation.** `computeQ(game, basis)` = `(Length hours × rating%) ÷ price`,
where *basis* picks the **Sale** (after-discount, the default) or **Full** price, and Length is
the review-based figure from `length.json` (§9.7), read through `hoursFor()`. Null for free
games and games with no Length (fewer than 3 recommending reviews). The score is recomputed on
toggle, never stored. (Internal sort key: `qtpd`.) *Until Sep 2026 the hours were HowLongToBeat's,
picked by an **HLTB metric** toggle (Main / +Extras / 100% / Avg) and an **HLTB data** toggle
(Real / All incl. estimates); both toggles, their URL params `hltb` / `hq`, chips and editors
were removed with the switch. Old links carrying them still load and ignore them; `sort=hltb`
is read as `sort=length`.*

**Landing view hides flagged adult games (Sep 2026).** `ADULT_DEFAULT = "hide"`: the page opens
with the Flags → *Adult content* control on **Exclude**, using the same lock as the shelves
(`isAdult()` — the storefront flag, not tag names). The trigger was the Length switch: it gave
idle-farmed adult titles a QTPD for the first time and one opened the default ranking at #1.
*Any* is one click away and `adult=any` in a link opts back in; a link with no `adult=` now means
Exclude. **A typed search is exempt** (`adultMode()`): someone searching for a title has asked for
something specific, so while the search box is non-blank *Exclude* is lifted for the table and the
title suggestions alike — if the user looks for the thing, we show the thing. *Only* still applies.
Added the same day, after a title search for a flagged game came back empty and read as data loss.

**Free-only mode.** Dividing by a zero price is undefined, so free games normally show no QTPD.
But when the price-type filter is narrowed to **Free alone** (`freeMode()` — `priceClass` is
exactly `{free}`), the QTPD column switches to **`freeScore()`** = `hours × rating%` with no
price division, so free games can at least be ranked *against each other* by quality-weighted
length. `colValue()` is the single accessor the column, the sort, the value-meter and the QTPD
range slider all read, so the swap is consistent everywhere. Any other price-type selection
uses the normal price-based score.

**Preset shelves (`#presetBar` / `presets.json`, Sep 2026).** A row of one-click chips above the
results — *Best deals under $10* · *Half off or better* · *Long games, highly rated* · *Short and
cheap* · *Co-op picks* · *New and well-reviewed* · *Hidden gems* · *Under-the-radar indie* —
labelled **"Start with"**. Eight shelves in two tones: `popular` (the first six) and `niche`
(the last two), styled apart because they answer opposite questions.

- **A preset is a querystring and nothing else.** `applyPreset()` writes `p.query` to the URL
  with `history.replaceState()` and then fires a synthetic `popstate`, which is the path the
  **back button** already uses: its handler resets every field to its default and re-reads the
  URL through `loadFromURL()`. Deliberately *not* a bespoke setter — a second way to set filter
  state is a second place for it to drift out of step with `loadFromURL()`. It also means the
  summary chips afterwards show **exactly which controls moved**, which is the entire teaching
  value of the feature: a preset can never express something the user cannot then see and edit.
- **The row is always present.** It shipped as a *landing* affordance that hid itself the moment
  the user had filters of their own — which in practice meant **any querystring at all**: one
  search, one price bound, a bookmarked view, a shared link. Anyone who had ever touched a
  filter never saw the shelves again. A preset row is **navigation, not a state indicator**, so
  it stays put; the only thing current filters change is which chip (if any) reads as active.
  One CSS trap made that fix non-obvious: `.presetbar{display:flex}` outranks the UA sheet's
  `[hidden]{display:none}`, so the row could never hide even when asked to — it needs an
  explicit `.presetbar[hidden]{display:none}`.
- **Active detection is set-equality on the querystring**, not string equality:
  `activePresetId()` sorts both sides' `key=value` pairs before comparing, so param order never
  matters. Empty query → `""` (default view, no chip lit); a query matching no shelf → `null`
  (the user's own filters). Clicking the lit chip clears it, so a preset is never a one-way door.
  The repaint runs from `syncURL()`, not from `update()` — `update()` renders *before* it syncs,
  so painting there left a shelf looking active after the user had edited away from it.
- **No count on the chip, no titles on the chip.** `presets.json` carries both, and neither is
  rendered — see §5. The live count is one click away in the meta strip and is always right.
- **Applying a shelf does not scroll.** It used to `scrollIntoView()` the table, which pushed the
  chip you just clicked off the top of the screen — so the one thing you wanted to check (which
  chip is lit, and which filter chips it set) was the first thing to disappear.
- **The house rule is `adult=hide`, and it is the only lock** (§1, CLAUDE.md). Shelves are the
  one surface that puts games in front of someone who did not ask for anything specific, so
  every shelf sets it and `presets.py` fails the job if one does not. An earlier revision
  carried a *second* lock — an `exc=` list naming every `ADULT_TAGS` entry — and it was removed
  deliberately (2026-09-16): `isAdult()` treats the PICS flag as authoritative for any
  PICS-covered game, so the tag test never runs for those, and excluding "Nudity" or "Mature"
  by name threw out ~1,100 games whose adult content is incidental — the game rather than the
  scene. **Known residuals, stated rather than implied away:** a PICS-covered game whose adult
  flag is unset but whose tags say otherwise now passes, and so does a game whose only adult
  signal is its title. Neither is identifiable from the data we hold.
- **Every shelf sets `ratesrc=all`.** The 30-day default would make a shelf's membership depend
  on a score only 5.9% of games have, so a preset would mean something different for those games
  than for the rest — and `presets.py` could not reproduce the page's answer exactly.
- **Review *bands*, not one floor.** Popular shelves floor at **5,000 reviews**; niche shelves
  **ceiling** at 5,000. Without the ceiling the niche shelves are just the popular ones again,
  because well-known games win on absolute quality. Both map onto the existing independent
  `REV_BANDS` (0-99/100/1k/5k, gaps allowed), so the ceiling cost no new filter — it is simply
  not selecting the top band. The floor exists because QTPD-descending rewards hours per dollar,
  so at a 100-review gate *Best deals* led with *Tap Heroes* and *New and well-reviewed* led with
  an adult title.
- **Length shelves exclude games with no ending** via `exc=idle,incremental,clicker,idler,
  mmorpg,massively+multiplayer,free+to+play`. Their Length is enormous — the people who
  recommend an MMO or an idler have played it for hundreds of hours (it was the same under HLTB:
  EVE Online's "main" was 1,777 h) — so divided by a small price they top every value ranking. Excluded from **length shelves
  only** — they are legitimate results everywhere else.

**Two filters were built because three shelves could not otherwise be expressed as real filter
state** — and a preset that is not real filter state cannot show the user what it changed:

- **Length range (hours)** — `state.minHours` / `state.maxHours`, URL `hmin` / `hmax`. The twin
  of Price range, reading `hoursFor()` — the same Length the column shows. A game with no Length
  is dropped once either bound is set. Leave a box blank for no bound.
- **Released within** — `state.releasedWithin`, URL `rel`, values `any` / `1mo` / `3mo` / `6mo` /
  `1yr` / `1yr+`. The same shape as the existing *Updated within*, on `release_ts`. `1yr+` means
  released **more than** a year ago, so `1yr` and `1yr+` partition the catalogue exactly.
  Previously "what came out recently" could only be reached by *sorting*, which is a different
  question.

**Min sale % floor (`state.minSale`, Sep 2026).** A stepper — `−5%` · readout · `+5%` — that
drops shallow discounts out of the table. It is not a preset list: the resting value is read
from the data, as the **shallowest discount currently in the results**, floored to a multiple of
5 (`floor5`), and every step moves 5 points, so the readout can never show a stray `12%`.

- `computeSaleBounds()` walks the games passing `passNonRange()` and records the shallowest and
  deepest discount as `saleFloor` / `saleCeil`. It runs with `IGNORE_MIN_SALE` set, i.e. with the
  floor itself switched off — otherwise the shallowest surviving discount would *always* equal
  the floor and `−5%` could never light up.
- `−5%` is disabled at rest (nothing below the shallowest discount is being excluded, so there is
  nothing to relax); `+5%` is disabled once the floor reaches `saleCeil`, where one more notch
  would empty the table. Stepping back down to the resting floor **clears** the filter
  (`minSale = null`) rather than holding a floor that excludes nothing.
- Setting a floor implies "only discounted games", so `setMinSale()` un-presses the **Full**
  price type and remembers it did (`minSaleForcedFull`) so clearing the floor puts it back.
  **Free** is deliberately left alone — a free game is exempt from the floor and is kept or
  dropped by its own price-type toggle. The coupling runs both ways: clicking **Full** (or
  **All**) while a floor is set releases the floor, rather than leaving a toggle that visibly
  does nothing.

**The table (12 columns).** In order: Game · Reviews · Trend · **Weighted** · Price / Sale ·
Sale ends · Released · **Updated** · Tags · **Playtime** · **Length** · QTPD. (**Trend** sits directly
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
  would trap the sticky `<thead>` in a scroll box. Below **1280px** (`TABLE_MIN_W`) the table
  stops being a table and becomes the stacked **card layout** (see *Responsive* below).
  *(The min-width moved with the Updated column: 1240→1324. The breakpoint has since stopped
  being a single cliff — see the two-step note under* Responsive *below.)*
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
- **Weighted** shows the capped % with the Δ badge and the low-confidence gray (§10). Its
  tooltip **names all three numbers for what they are**, which was a real defect until Sep 2026:
  it used to call `wr_steam` "Steam's flat %" and print a delta "vs Steam", but `wr_steam` is
  the plain one-vote % *of the sample*, not Steam's published score — so a row could carry two
  different numbers under Steam's name, 39 points apart, with a delta that never touched Steam
  at all. The tip now reads as the weighted %, the **same sample unweighted** (with the delta
  described as what playtime weighting moved), and **Steam's published score with its full
  review count** — plus the sample stated as "the newest N of M" and, where `span_days` is
  present, how much calendar time those N cover.
- **Trend** is recent − all-time (improving/stable/declining), gated on staleness.
- **Released** shows more than the raw date: a computed **age string** (`ageStr()`, e.g.
  "15.2 yrs old" / "N mo old") stacked with a **last-content-update recency badge**
  (`updatedStr()`, e.g. "upd 3mo" / "no upd", full date on hover) — both derived client-side
  from `release_ts` / `last_update_ts`, not separate stored fields.
- **Price / Sale** is one merged column: struck full price on top, sale price below with the
  discount badge inline to its right. Its **header is split** into two independently-clickable
  sort targets — "Price" (sorts by current price) and "Sale" (sorts by discount depth) — and the
  sort arrow hops to whichever half is active. **Sale ends** is a live countdown that
  collapses offline when a sale has expired (honest between price refreshes). Two offline
  corrections run over the merged record before anything renders: `expireSaleIfEnded()` for a
  sale whose end date has passed, and `reconcilePriceFlags()` for a record whose flags
  contradict its own prices — free, or discounted, while quoting the full price (§15).
- **Playtime** stacks ▲ recommenders over ▼ non-recommenders' median hours. Hours display
  **whole for ≥10h, one decimal under 10h**; the review-count sample size is kept in the
  data + tooltip but not shown inline. When a game has **no playtime data the cell renders
  completely empty** (no `—` dash) so it adds **zero height** to the row — previously the dash
  forced a line-height floor that inflated data-sparse rows.
- **Length** (Sep 2026, replaced the HLTB main / +extras / 100% / avg stack) shows one value,
  `N h`, same 2-digit/1-digit number rule (`fmtLen`). The hover (`lengthTip`) names the
  recommending-review count and calibration genre, says when a game under 20 fans leans on its
  genre, and on **adjusted** rows (idlers, anything balanced past 100 h, and anything past the 420 h cap — §9.7) quotes the
  raw figure and why it is not believable; those values carry a **blue dotted underline**. The
  column narrowed from `minmax(132px,164px)` to `minmax(80px,104px)`, dropping the table floor
  from 1324px to 1272px (tags-collapsed 1218 → 1166); the 1366 / 1280 breakpoints were left as
  measured, so both now have ~50px to spare.
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
`setView()` coerces one to the other across the 1280px breakpoint (the off-device button is
dimmed). **Grid** is the third, device-independent view: a box-art grid of `gridCardHTML()`
cards (Steam header art at `aspect-ratio:460/215` over a dark info panel with title + Steam
rating on one line and QTPD below; tap to flip to a price/length/`Steam ↗` overlay). **Grid is
the default on mobile**, Table on desktop; a saved choice always wins. Full as-built record,
including the six refinement rounds, in **§3.4**.

**Responsive / card layout (single-column spec sheet, 2026-07 redesign).** The table is for the
desktop width range; a **fluid table alone cannot fit a phone** (twelve columns at legible
minimums sum to ~1324px). So below **1280px** `<thead>` hides and each row becomes a
**single-column spec-sheet card**.

**The breakpoint is a two-step, not a cliff.** It was a single 1374px line, and that was wrong at
both ends: a 1366px window fits the full table comfortably and was still being sent to cards,
while a 1280px one was never offered a table at all. Two constants now bracket it —
`TABLE_FULL_W = 1366` (narrowest width where the full twelve-column template lands exactly) and
`TABLE_MIN_W = 1280` (narrowest window that gets a table at all). Between them the **Tags column
folds to its strip** (`tags-collapsed`, table `min-width` 1218px) so the rest still fits, and the
table is squeezed rather than abandoned. Below 1280px the columns genuinely do not fit and cards
take over. Both thresholds are `matchMedia` queries (`max-width:1365px`, `max-width:1279px`) read
by a tiny inline script at parse time and by a listener afterwards. The card leads with a **thumbnail + title header** and the
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

- **Value** — **QTPD price basis** (Sale *(default)* / Full) · **Length range** · **price type** (All / Full / Sale / Free — an independent multi-toggle, URL `pc`; this
  **replaced the old boolean on-sale-only filter**) · min & max price · **QTPD range**
  log-slider that fits current results.
- **Quality** — min rating (any/60+/70+/80+/90+) · **Review period** (30-day *(default)* /
  All-time) · review-trend multi-toggle · min-reviews bands (0/10/100/1k/5k+, independent
  toggles, gaps allowed) · updated-within (any/1mo/3mo/6mo/1yr/1yr+) · **Playtime sort**
  (▲ *(default)* / ▼).
- **Flags** — the PICS cluster (§9.6): six tri-state Any/Exclude/Only presence flags (Early
  Access · AI disclosure · Adult content · VR-only · Family-share block · Custom EULA) plus
  two graded controls (Controller, Steam Deck). Folded by default; no-ops behind the `HAS_PICS`
  guard when `pics.json` is empty. **Adult content defaults to Exclude** (`ADULT_DEFAULT`, Sep
  2026 — see *QTPD computation* above); the other flags default to Any.

**`periodRating(g)` — one resolver for the review period.** *(Sep 2026.)* Min rating and the
Score-column sort both call it, so a game is judged on the same number whichever way you reach
it. It returns `recent_pct` when the period is 30-day **and the game has one**, else
`rating_pct`.

The fall-back is the load-bearing decision. Steam publishes a 30-day score for only **7,504 of
128,292 games (5.9%)** — it needs 45+ days since release and enough recent reviews — so testing
`recent_pct` strictly would collapse the catalogue to ≤7.5k games the moment any rating floor is
set, which reads as a broken filter rather than a strict one. Falling back means a 70+ floor on
the 30-day period keeps three groups apart correctly: a game with a weak 30-day score is
**dropped** even when its lifetime score clears the floor (the Occupy Mars case: 70% all-time,
38% for 30d); a game whose *recent* score clears it **passes** even when its lifetime score does
not (measured: 189 such games at 70+, previously invisible); a game with no 30-day score at all
is still judged on its lifetime score (52,601 games) rather than silently vanishing.

**Min reviews deliberately does NOT follow the period.** Recent counts run one to two orders of
magnitude below lifetime ones (Occupy Mars: 16 recent vs 3,140 all-time), so pointing the
0-99/100/1k/5k+ bands at `recent_count` would empty the list at the default `100+` setting. The
bands stay all-time; only the *score* follows the period.

**Min reviews bands (Sep 2026): 0-99 / 100 / 1k / 5k+, default 100+.** The old 0 and 10 bands
were merged into one 0-99 band, off by default — 10 reviews say about as little as 0 do. The
surviving bands keep their URL indices (`rev=0,2,3,4`), so shelf links (`rev=4`, `rev=2,3`) and
old shared links are unchanged; an old link's `1` (10-99) is read as `0`. The default moved from
10+ to 100+, which takes ~52k games with 10-99 reviews out of the landing view.
**Any search lifts that default** (`revFloorOn()`, `searching()`): a typed title search or an
active Find similar ignores the review bands while they are still at the default, so a
61-review game looked up by name is shown. Bands the user changed are respected. (Find similar
lifts only this, not the adult default — its result list is not something the user named.) Carrying the period into the
**weighted score / QTPD** is a separate, larger question — registered in ROADMAP §3.2, not built,
because a 16-review sample needs a far stronger prior than the all-time-tuned one.

Because the period now decides **which games pass**, its click handler runs the full `update()`
(paging reset + QTPD-domain refit + re-render), not the bare `render()` it used while it was
sort-only.
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

- **Review period** (30-day *(default)* / all-time) — which score the Reviews column sorts on,
  **and** which score the Min rating floor tests. Both go through one resolver, `periodRating()`
  (§11), so the filtered set and the sorted order can never disagree about the number a game is
  being judged on. It was sort-only until Sep 2026, which produced the bug it was fixed for: a
  70+ floor with 30-day selected still listed games showing **38% for 30d**, because the floor
  read all-time. Min **reviews** deliberately does *not* follow the period — see §11.
- **Playtime sort** (▲ recommenders / ▼ non-recommenders) — which median a click on the
  Playtime column will sort by. Switching it updates the Playtime **header** (the selected
  side lights up, the other dims) so you can see what a header-click will do, and re-sorts
  **only if** Playtime is already the active sort. It uses the neutral segmented-control
  styling — the green/red lives in the header and cells (semantic), not on the toggle.

**State in the URL.** Every filter/sort choice is serialized to the querystring by `syncURL()`
and restored by `loadFromURL()`, so any view is a shareable link. Defaults are omitted (e.g.
`adult` only appears when not `hide`, `pt` only when not `up`), which keeps shared links short and
means a bare URL is the default view. **The full set is 31 params** (`hltb` and `hq` were retired
with HLTB in Sep 2026 — read and ignored, never written):

| Group | Params |
|---|---|
| Search & tags | `q`, `inc`, `exc`, `tagmode` |
| Value | `pc`, `basis`, `pmin`, `pmax`, `minsale`, `qmin`, `qmax`, `hmin`, `hmax` |
| Quality | `minscore`, `rev`, `trend`, `upd`, `rel`, `ratesrc`, `pt` |
| Flags (PICS, §9.6) | `flags`, `noflags`, `ai`, `adult`, `ctrl`, `deck` |
| Display | `scheme` |
| Sort & paging | `sort`, `dir`, `per` |
| Wishlist | `wishonly` |

`hmin` / `hmax` (Length range) and `rel` (Released within) arrived with the preset shelves —
three shelves could not be expressed without them. `scheme` carries the review-score colour
scheme; it is a *display* preference rather than a filter, and a link's scheme beats the one
saved in `localStorage`. `presets.py`'s `KNOWN_PARAMS` allowlist mirrors this table minus
`scheme` (no shelf sets a colour scheme) and fails the build if a shelf names anything outside
it.

Two things are deliberately **not** serialized: the **hidden-games list** (session-only, see
*Thumbnails & hiding*) and the three `localStorage` preferences — `qtpd.view`,
`qtpd.sections`, `qtpd.tagsCollapsed` — which are per-device chrome, not the query a link is
meant to reproduce. The `qmin`/`qmax` range is written only once the slider is manually moved
(`qRangeTouched`), since it otherwise auto-fits the result set. Pagination is infinite-scroll
at a fixed **`PAGE_SIZE = 66`** per page. *(The `100 / 500 / 2000` selector is gone —
INESKA_IMPROVEMENTS.md §23. Infinite scroll already loaded the next page on reaching the bottom,
so the control's only real effect was how much work one `render()` did, and `2000` meant building
two thousand DOM subtrees on every filter keystroke. `?per=` is still honoured on read so links
shared before the removal resolve; its validator became a 1…2000 range check, because the old
`[100, 500, 2000]` whitelist would have rejected the new default.)* `minsale` is read **after**
`pc`, so a link carrying a floor always lands with the Full price type off whatever `pc` asked
for. *(Historical note: the boolean `sale` param was removed when the on-sale-only toggle became
the three-way `pc` price-type filter; old links carrying `sale` are simply ignored. The min-sale
floor is spelled `minsale` precisely so it cannot be mistaken for that retired flag — `sale=1`
would otherwise have silently become a 5% floor.)*

**The explanation layer works on touch too (Sep 2026).** Every explanation on the page is
authored as plain `title` text and drawn by one event-delegated engine in a styled `.uitip` box
(the full mechanics are in §16, *Custom tooltip layer*). That engine used to bail out on
`matchMedia("(hover: none)")`, which meant every `[title]` on the page — column headers, filter
fields, legend keys, the QTPD explanation itself — existed for pointer users only. On a phone
**Grid is the default view and the audience is least expert**, so the one platform that most
needed the teaching layer was the one platform with none (`docs/ONBOARDING_PLAN.md` G1 / S15).

- **Same corpus, different gesture.** Touch gets a `click` listener instead of
  `mouseover`/`mousemove`/`mouseout`. There is no cursor to hang the box off, so `placeNear()`
  anchors to the tapped element's own bounding box — centred under it, flipped above when the
  bottom of the screen is closer, clamped to the viewport on both axes. Same box, same keyword
  colouring; only the anchor differs. Tapping the same element again closes it; scrolling
  dismisses.
- **A tip must never swallow a gesture that already means something.** A tap opens a tip only
  when it lands on an **inert** part of a titled element — a `<label>`, a legend key, a
  spec-sheet row, a meta figure. `INERT_BLOCKERS` names everything that keeps its own meaning:
  `button, a, input, select, textarea, summary, label[for], [role=button], .sortable,
  .splitsort, .chip, .gcard, .gart, .stage-box, .tagstoggle, .seg`. This costs nothing in
  coverage, because the controls sit **beside** their explanatory label rather than inside it —
  `.field` carries the `title` and its `<label>` is a sibling of the `.seg` holding the buttons.
- **On touch, an available tip has to be painted.** A pointer advertises one with `cursor:help`;
  a finger has no cursor, so under `@media (hover: none)` each `.field[title] > label` grows a
  small `?` after it. The filter fields are the densest explanatory surface on the page (21 of
  them, each with a written tip), so they carry the mark; everything else is found by tapping
  the value it explains, which is the gesture people already try. The box also narrows to
  `min(340px, 100vw - 24px)` — a flat 340px plus the anchor clamp can go edge-to-edge on a
  390px phone. **Nothing about desktop changes**, and the `?` is not painted there.
- Coverage as shipped: **36 of 181** titled elements are tap-reachable. The rest are controls
  whose tap is spoken for by `INERT_BLOCKERS`.

**Plain words over jargon in the labels (Sep 2026).** The QTPD-side controls were named after
the data source rather than the thing being measured — "HLTB metric", "HLTB data", a `HLTB
M/E/100%` column header — which asks a first-time visitor to learn an acronym before they can
use a filter. They then read **Length metric**, **Length data** and **Length · M/E/100%**, with
HLTB kept as an upright source qualifier beside the value rather than as the label itself. (All
three went in Sep 2026, when the page moved to one review-based **Length**, §9.7.) The
Grid's colour legend was also **exposed to assistive tech**: its key was decorative markup that
carried meaning only visually.

**The preview stage — a docked player for phones (`.stage` / `stageOpen` / `stageLoad`).**
The grid plays its preview *inside* the card, and on a 390px phone that card's art is
**171×80px** — roughly 3% of the viewport, with a 16:9 clip cropped into a 2.14:1 box on top of
that. The stage promotes what you tapped into a **full-width 16:9 player docked under the sticky
nav**, and turns the grid below it into the remote control: tap any card and that game loads up
there instead. Measured on a 390×844 phone the player is **352×198px plus a two-row meta strip,
about 34% of the viewport**, leaving three and a half rows of grid on screen.

- **It is a third `.mediabox`, not a second player.** `armMedia(carrier, box, dwell, step, seek)`
  takes any element wearing the class, which is how one code path already drives both the
  desktop hover popup and a grid card's art. The stage adds no playback code at all — only which
  box is current, the chrome around it, and the gestures in and out. Exactly one `<video>` still
  exists on the page at a time, which is *why* a card cannot keep playing while the player is
  open: a tap on a card while staged **loads the stage** rather than starting a rival session.
- **The stage broke an assumption `armMedia`'s re-arm guard had always been allowed to make.**
  That guard exists so a `mouseover` re-firing on child nodes does not restart the clip, and it
  used to test the **box** alone — safe while every carrier was one box per game (one `.pop` per
  row, one `.gart` per card), so "same box" and "same game" meant the same thing. The stage is
  **one box whose game changes under it**, so with a preview running every later `stageLoad()`
  returned early and did nothing: the title and figures updated (they are painted before the
  arm) while the previous game's clip carried on underneath, and tapping a second card looked
  dead until you stopped the first. The guard now tests box **and** game, and the appid is read
  off the **session** (`media.appid`) rather than the element — `stageLoad` sets the box's
  dataset before arming, so an element-level comparison would see the new id on both sides and
  still match.
- **It lives OUTSIDE `#gridview`, and that is the whole design.** `render()` assigns
  `#gridview.innerHTML` on every filter keystroke, every sort click **and every infinite-scroll
  page bump** — so promoting the card element itself (`position:fixed` + a placeholder, which
  preserves the running video) would have it destroyed by the very act the feature exists for:
  scrolling the grid while watching. As a sibling inside `.tablecard` it survives, and render's
  existing `if (media.pop.closest("#gridview")) stopMedia()` guard is left exactly right. Being
  the first child of `.tablecard` rather than of `#gridview` also serves **both** phone views —
  the box-art grid and the Card view whose table follows below it.
- **Sticky and in flow, not fixed and overlaid.** The block pushes the grid down by itself, so
  opening and closing needs no scroll-position compensation and the list never jumps under the
  finger. There is no clipping ancestor on `body > .wrap.body > .tablecard` (and `.tablescroll`
  goes `overflow:visible` once narrow), so `position:sticky` works untouched. `top` is
  `var(--nav-h)`, written from a **`ResizeObserver` on `#topbar`** (`syncNavH`) because the nav
  is not a fixed height: past 24px of scroll `body.nav-scrolled` hides `.bar-main` and it shrinks
  (measured: 194px → 93px). `z-index:35` sits below `.topbar`'s 40, so opening the filter panel
  covers the player rather than fighting it — and `setFiltersCompact(false)` closes it outright,
  since an open panel takes most of the screen.
- **16:9, not the card's 460/215.** Steam's microtrailers *and* its screenshots are both 16:9;
  the 2.14:1 shape belongs to **box art**, which is only the poster frame the video replaces
  ~350ms later. At card size the crop costs little; at 198px tall it is ~17% of the picture. So
  the player takes the ratio of the thing you actually watch and lets the poster lose its sides
  to `object-fit:cover` instead — the same trade the hover popup already made. A
  `width:min(100%, 46vh*16/9)` cap keeps a **landscape** phone from handing the whole screen to
  the player: past that point the box narrows and centres.
- **Gestures.** Tap the player to start/stop · swipe **left/right** to step the playlist ·
  swipe **down** (or `✕`) to close. Up does nothing on purpose — a second hidden meaning for the
  gesture that opened it would be a coin-flip for the user, and `‹ ›` are right there. On a
  **card**: tap to play in place (unchanged) · swipe left/right to step (unchanged) · swipe
  **up** to promote · swipe **down** to stop.
- **Claiming vertical is the one delicate part**, because vertical is how the page scrolls.
  `touch-action` is read when a gesture *starts*, so it cannot be decided mid-drag — it has to be
  a class that goes on with playback and comes off with it. `.gart.armed{touch-action:none}` is
  added by `armMedia` and removed by `stopMedia`, **on touch only**, so vertical is claimed
  exactly on the one card that is currently playing. The trade is deliberate and narrow: that
  card stops being a place you can scroll from, and it always has two ways out that hand
  scrolling straight back (the `■` badge, or the downward swipe that stops it). Every other card
  keeps `pan-y pinch-zoom` and pans as it always did. `bindMediaSwipe(root, pick, vert, vertWhen)`
  is the one recogniser both surfaces share — passive throughout, deciding by *measuring* rather
  than capturing, with the iOS phantom-click swallow (`swallowNextClick`) on both.
- **Discovery is a control, not a gesture.** The `⤢` chip appears on a card *while it is
  playing*, opposite the `■` badge — the swipe is the accelerator, not the way in. This repeats
  the reasoning that turned `.gplay` from a 13px glyph into a gold disc when the trailers were
  going unfound on touch: on a phone there is no hover, so the badge **is** the discovery path.
  The chip hands over the card's **current playlist index**, so the player opens on the frame
  that was on screen rather than restarting the game — free, because the screenshot shard is
  already in `shotsShardCache` from the card's own playback (`armMedia`'s `seek`).
- **Card view gets previews for the first time.** Its only path was the `.pop` panel, which is
  built on `mouseover` — so on a phone the thumbnail was simply **inert** and the clip and
  screenshots were unreachable in that view entirely. It now opens the player, marked by a
  `.tplay` badge. A real element rather than `.thumb::after`, which the 18+ gate already owns —
  the same collision that gave the grid card a `.gplay` element instead of a pseudo-element.
- **Phone only, by design.** `stageAvailable() = isNarrow() && trailersOn()`. On a pointer device
  a grid card is already 220px+ and hover plays the clip in place, so **nothing about desktop
  changes** — the affordances are not even painted (`touchClipCls()` is empty when `CAN_HOVER`).
  `trailersOn()` carries both the **Video** switch and the **reduced-motion** override, so one
  call gates the player the way it already gates every other kind of panel motion.
- **Session-only state.** `state.stageAppid` is not serialized to the URL and not persisted —
  the same rule as `openCards` and the hidden-games list: per-device chrome is not part of the
  query a shared link is meant to reproduce. `markStaged()` re-applies the gold "on stage" edge
  after every render (a class alone would vanish on the next filter keystroke) and repaints the
  meta strip, whose figures move with the price basis and free-only mode.

**Infinite scroll appends in grid view (`growPage`).** Reaching the bottom used to be
`state.pagesShown += 1; render()`, and render assigns `#gridview.innerHTML` — so it threw away
every card on screen and rebuilt it from scratch just to add the next hundred. Merely wasteful
before; with the player docked above the grid, **scrolling is what the user does while
watching**, which put a rebuild of several hundred DOM subtrees next to a decoding `<video>` on
every page bump. The next slice is now appended. The detailed view still re-renders: its rows
are built by a closure inside `render()` that depends on per-render values (the QTPD bar scale,
computed across the whole filtered set), and its rows carry no video.

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
| price, discount, sale-end | `prices.json` | `price_and_sale.py` | `scraped_at` | no cooldown — whole non-free base re-batched hourly, on the hour |
| tags | `tags.json` | `tags_refresh.py` | **none** | fetch-once, **no rescrape** ([ROADMAP.md](ROADMAP.md) §3.5) |
| recent 30d % / count | `recent.json` | `recent_refresh.py` | `recent_scraped_at` | two-track: active 4d / dormant 30d |
| HLTB main/extra/complete/avg + `est` (**calibration only**, not shown) | `hltb.json` | `hltb_refresh.py` | `fetched_at` | partial 14d / full 365d; blank backoff 3→30→180d |
| Length (hours, n_up, genre, raw/reason on adjusted rows) | `length.json` ← `playtime.json` + `length_coefs.json` | `length_model.py` (apply on every 2.3 pass; `--fit` weekly) | `generated_at` (file-level) | follows `playtime.json`; coefficients weekly |
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

## 11.6 Freshness tracking (`freshness.py` → `FRESHNESS.md`)

`COVERAGE.md` measures **volume** ("how much of the catalog do we hold?"). `FRESHNESS.md`
measures **time** ("when was each task last run, when does it run next, how much of what it owns
is up to date, and where is the wait too long?"). The split is deliberate: coverage was already
carrying a partial freshness story in its Axis 2, but nothing in the repo answered the operator
question — *is every job actually still firing, and how long until this file gets touched again?*
Answering that meant opening the Actions tab and reading fifteen workflow histories by hand.

Rebuilt **daily at 07:00 UTC** (`freshness.yml`, task 4.3). Stdlib-only, no network, no git —
the workflow commits the file, same one-writer discipline as `shard_health.py`.

### The two clocks (why there are three tables, not one number)

A single "freshness %" would fuse two failures that need opposite fixes, so they are reported
separately:

1. **Job clock — is the task running?** (Table 1.) Cron, worst-case cadence, last write, next
   fire, and `gap (last → next)` = how stale the file will be, at worst, by the time the task
   next gets a chance to touch it.
2. **Game clock — how long does one title wait?** (Table 3.) Each file's per-game refresh window
   next to the observed row-age distribution (p50 / p95 / oldest).
3. Between them, **Table 2** splits covered rows into up-to-date / pending-refresh /
   pending-fill / skipped-by-design, using `coverage.py`'s bucketers unchanged.

A stalled job is a broken pipeline (check Actions). A healthy job with a blown p95 is a budget
shortfall (more run minutes, more slots, or accept the tail). One number would hide which.

### Status = missed fires, not an age ratio

A task is judged by **how many of its own cron fires came and went without its output file being
re-stamped** — 🟢 0, 🟡 1, 🔴 2+. An age-vs-cadence ratio was tried first and rejected: it flatters
slow schedules (a daily job can skip an entire run and still sit at 1.05×) while being trigger-happy
on fast ones. Each fire is granted the job's declared `timeout-minutes` as grace before it can
count as missed, so a long pass still in flight (playtime legitimately stamps up to 5.5h after
its cron) is never mistaken for a miss.

The signal works because **every scraper here rewrites `generated_at` on each successful pass** —
a missed fire really does mean "no successful write". The one exception is handled explicitly:

- **Fill-only tasks** (`tags_refresh.py`) write only while unresolved games remain and commit
  nothing when they find none. Scoring those on missed fires would print a red light daily for a
  job that is working as designed, so they carry a 🔵 **fill-only** status, are exempt from the
  gate, and get a standing alert instead — because a fill-only task with a drained frontier means
  its data is **frozen**, which is the pipeline's real structural freshness gap (tags have no
  rescrape cadence at all; [ROADMAP.md](ROADMAP.md) §3.5).

### Invariants

- **One writer:** `freshness.py` → `FRESHNESS.md` only. All reads read-only.
- **No duplicated gates.** Cooldown constants and bucketers are imported from `coverage.py`
  (`import coverage as CV`), never copied. Add a scraper → update `coverage.py`, and both docs
  follow.
- **Schedules are read, not restated.** Crons and timeouts come from the workflow files at run
  time; the only per-task metadata in `freshness.py` is the registry mapping task → workflow →
  output file.
- **Generated, never hand-edited** — a banner at the top of `FRESHNESS.md` says so.

---

## 12. Wishlist import & the Cloudflare Worker

The browser can't read a Steam wishlist cross-origin, so a small Cloudflare Worker (free tier)
proxies it. **This repo does not contain *this* Worker's source.** The `WISHLIST_PROXY`
constant points at an already-deployed Worker
(`https://qhpp-wishlist.mlmariss.workers.dev`) that was never committed and is now
unrecoverable — reviving or replacing it means writing one that implements the two endpoints
below from scratch (see README's "Wishlist import (optional)" section for the shape).

> **`worker/` does now exist in this repo — it is the *other* Worker.** It holds
> `qtpd-reviews`, the Review Digest's `appreviews` passthrough (§17), whose source is in git
> **precisely because** this one's is not: losing the wishlist Worker is the entire reason
> that feature was expensive to scope. Do not read `worker/` as the wishlist proxy; they are
> two deployments with two URLs and no shared code.

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
- **`PASS_ALIGN_MINUTE` (0) / `MIN_PASS_MINUTES` (55) / `MIN_CHUNK_MINUTES` (15)** (prices) —
  the pass loop's clock. A run repeats full sweeps until its `RUN_MINUTES` budget is gone,
  each starting at `PASS_ALIGN_MINUTE` past the hour; it ends rather than begin a sweep with
  under `MIN_PASS_MINUTES` of budget left, and spends a leftover window on a partial sweep
  (resumed next pass) only if it is at least `MIN_CHUNK_MINUTES` long.
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
- **`SLIVER_N` (250) / `SLIVER_FRAC` (0.02)** (ratings) — the **sliver gate** (§10). Both must
  bind: a sample greys when it is under 250 reviews **and** under 2% of the storefront's own
  count. Shipped in `ratings.json`'s meta so the frontend cannot drift from them.
- **`FIRST_TOUCH_BATCH` (300, env) / `FIRST_TOUCH_TARGET` (100 = `PER_PAGE`) /
  `FIRST_TOUCH_HOT_REVIEWS` (10,000) / `FIRST_TOUCH_HOT_TARGET` (`DEPTH_LADDER[0]`) /
  `FIRST_TOUCH_HOT_MAX` (25, env) / `FIRST_TOUCH_COMMIT_GROUP` (8, env)** (playtime) — phase 0,
  the never-seen fill frontier (§9). Batch size per run, the cold one-page stake, the catalog
  review count above which one page stops being a sample, the depth those get instead, how many
  such deep touches a single run will do, and how many finished shards are committed per push
  (also the memory ceiling, since `_robust_commit` snapshots each shard's bytes). `FIRST_TOUCH_BATCH=0`
  disables the phase entirely.
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
- **HLTB estimates** are auto-replaced once real data arrives and never train the ratio. Since
  Sep 2026 neither real nor estimated HLTB values reach the page — HLTB is calibration input for
  the review-based **Length** (§9.7), which uses real `raw.extra` only.
- **Length** exists for games with ≥ 3 recommending reviews (~96k, against ~27k that had a real
  HLTB main). Under ~10 reviews it is close to its genre's typical figure; it is within 2× of
  HLTB extra for ~84 % of games with ≥ 100 fans. Idle-farmed playtime is balanced and capped
  (§9.7) and flagged in the UI; an endless game that is *not* tagged Idler and stays under the
  cap is shown at its reviewers' hours.
- **Weighted rating** needs public playtime and enough reviews: none below 5, grayed 5–9.
- **Tags** fall back to Steam genres when SteamSpy lacks a game.
- **Sale end times** collapse offline when expired — `expireSaleIfEnded()` (§11) actively
  zeroes `discount_pct` and resets `price_final` to `price_initial` in the merged in-memory
  record once `discount_end` has passed, not just the countdown UI, so the displayed price
  stays honest between price refreshes.
- **Free-to-keep promos poison a price snapshot.** While a paid game is free for a weekend,
  `appdetails` answers `is_free: true` **and** `discount_percent: 100` while `price_overview`
  still quotes the **full** price on both `initial` and `final` — a self-contradictory response
  that, stored verbatim, outlives the promo. Moonlighter (`606150`) and Breathedge (`738520`)
  sat at **“-100% off” on a full-price game** — the top two rows of the discount sort, gold
  sale edging, and no QTPD at all (a "free" game has no price to divide by) — for weeks. It
  fossilised because every layer that could have corrected it was gated on the bad flag:
  `price_and_sale.py` skipped `is_free` games, so the fast layer never re-priced them, and
  only a full-catalog re-scrape (weeks away for a 2018 game) would have. Fixed in three
  places, prices believed over flags each time: `reconcile_price_flags()` in `scraper.py` at
  capture, the same rule in `price_and_sale.fetch_prices()`, and `reconcilePriceFlags()` in the
  frontend merge so records written before the fix are corrected in the reader. `scraper.py`
  additionally re-scrapes any stored record still carrying the contradiction (`promo_residue()`)
  ahead of the normal queue. A free app that *also* sells a genuinely discounted package
  (Capcom Arcade Stadium `1515950`: free, $59.99 → $14.99) keeps both facts — only the
  impossible combination is undone. `test_price_flags.py` covers the rule and the two real
  records.
- **Review percentages differ from the store page, deliberately.** `rating_from_reviews()`
  queries `appreviews` with `purchase_type=all`, which counts every review — key, gift and
  giveaway copies included. The store page's headline summary counts **Steam purchasers only**
  and drops flagged review bombs, so its count is lower and usually its percentage is higher
  (Moonlighter: 21,971 reviews / 82% here vs 17,224 / *Very Positive* there). Both are correct
  for what they measure; `purchase_type=all` is the wider, harder-to-game population and is
  what the weighted rating (§10) is built on. The 30-day figure is unaffected: `recent.json`
  reads Steam's own `appreviewhistogram`, so it tracks the store's *Recent Reviews* closely
  (Moonlighter 79% / 149 vs the store's 146) — which is why only the all-time line looks off.
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

- **Length replaces HowLongToBeat on the page (Sep 2026).** The three HLTB values (Main /
  +Extras / 100% / Avg) and their two toggles were replaced by one review-based **Length**:
  recommenders' median playtime × a per-genre coefficient fitted weekly against real HLTB extra,
  topped up toward the genre's typical length below 20 reviews, and — for idlers and anything
  past 1,000 h — balanced against non-recommenders' playtime and capped (§9.7). New:
  `length_model.py`, `length.json`, `length_coefs.json`, workflows 3.3 (weekly fit) / 3.4
  (manual apply), a chained step in 2.3, `test_length.py`, `LENGTH_MODEL.md`, `LENGTH_PLAN.md`.
  The page stops downloading `hltb.json` (37 MB); games with a QTPD go from ~27k to ~78k.
  Removed: the Length metric / Length data controls, their chips, editors, URL params
  (`hltb`, `hq` — read and ignored), the M/E/100% column and its estimate styling; `sort=hltb`
  is an alias of `sort=length`. `presets.py` takes hours from `length.json`: *Long games* went
  151 → 460 results and *Short and cheap* 257 → 160, because Length sits near HLTB extra rather
  than main; the 40 h / 6 h thresholds were kept, since the labels promise hours of Length.
  Alongside, the **landing view now excludes storefront-flagged adult games by default**
  (`ADULT_DEFAULT`, §11) — the switch had put one at #1 of the default ranking.

- **Preset shelves — the landing page got a first decision (Sep 2026, PRs #89–#92).** The gap
  between "129,578 games ranked by a metric you don't know" and "78 controls" had nothing in
  it. Eight one-click shelves now sit above the results under **"Start with"**, each a stored
  querystring that sets *real* filter state, so the summary chips afterwards teach which
  controls moved. Full mechanics in §11; the generator and its build-time guards in §4;
  `presets.json`'s shape in §5. Three things this shipped with that are worth knowing:
  - **Two filters had to be built first** — *Length range* (`hmin`/`hmax`) and *Released
    within* (`rel`) — because three shelves could not otherwise be expressed as real filter
    state, and a preset that is not real filter state cannot show the user what it changed.
  - **Two bugs only the real data exposed.** `update()` renders *before* it calls `syncURL()`,
    so the row read the previous URL and left a shelf looking active after the user had edited
    it; and `.presetbar{display:flex}` outranks the UA sheet's `[hidden]{display:none}`, so the
    row could never hide — it sat as an empty strip before `presets.json` loaded. Both were
    invisible against the six-game bundled `SAMPLE`, which silently passes any filter test.
  - **The adult lock was cut back to one, deliberately.** Shelves shipped carrying `adult=hide`
    *and* an `exc=` list of every `ADULT_TAGS` entry. The second lock did close a real hole —
    `isAdult()` treats the PICS flag as authoritative for PICS-covered games, so the tag test
    never runs for them — but it closed far more than that, excluding the *game* rather than
    the scene on ~1,100 titles whose adult content is incidental. The storefront's own flag is
    now the definition, and the residuals are stated rather than implied away (§11, CLAUDE.md).

- **Weighted rating: name the three numbers, and stop trusting a sliver (Sep 2026, PR #91).**
  Reported against WARDOGS — the Weighted column read **48.3% in full colour** on a game whose
  own row showed 81% of 60,202 reviews, and the tooltip said "from 100 reviews". Both halves
  were real defects: a 100-review first-touch page on a game taking ~14 reviews a minute spans
  **seven minutes**, and `n ≥ 10` cannot see that. A **sliver gate** (`SLIVER_N = 250` *and*
  `SLIVER_FRAC = 0.02`, both shipped in the meta) now greys those, `ratings.json` carries the
  sample's calendar `span_days` as an additive fifth element, and the tooltip names all three
  numbers for what they are — the previous copy printed the *sample's* unweighted % under
  Steam's name, 39 points off the published score on some games. Full detail in §10, schema
  in §5.

- **The explanation layer reached touch, and the labels dropped the jargon (Sep 2026, PRs
  #87–#88).** The tooltip engine bailed out on `(hover: none)`, so every `[title]` on the page
  existed for pointer users only — on the one platform where Grid is the default view and the
  audience is least expert. Tap now opens the same tips, anchored to the tapped element and
  blocked from swallowing any gesture that already means something; the filter fields paint a
  `?` because a finger has no cursor to advertise with. Separately, "HLTB metric" / "HLTB data"
  / `HLTB M/E/100%` became **Length metric** / **Length data** / **Length · M/E/100%**, and the
  Grid colour legend was exposed to assistive tech. Both in §11; the plan and its scoring are
  in `docs/ONBOARDING_PLAN.md`.

- **Review Digest: a 5000-review sample and a reach selector (Sep 2026, PR #84).** The digest's
  defaults were all set when it was the cheap read; every one of them moved. It also gained the
  axis a bigger sample cannot buy — keeping one page in N for N times the history at the same
  token cost — with the sampling factor disclosed in the bundle, because a thinned sample makes
  *volume* figures N times under the truth while leaving every proportion intact. The feature
  now has an as-built section: **§17**. Design record: `REVIEW_DIGEST_PLAN.md` §24.

- **Playtime: the first touch is sized by how big the release already is (Sep 2026).**
  `playtime_refresh.py`'s phase 0 spent a flat one page on every never-seen game, which is most
  of a quiet release and a rounding error on a busy one — the input that produced the WARDOGS
  reading above. See §9.

- **Prices: the run keeps its own hourly clock (Sep 2026).** Steam flips its discount waves at
  10:00 America/Los_Angeles — **17:00 UTC** in summer, 18:00 in winter — and essentially the
  whole day's price movement lands in that one minute: **8,873 of the 9,947** dated sales in a
  September `prices.json` ended at exactly 17:00 UTC. Whether the site looks current is
  therefore decided entirely by how soon after that minute a pass runs, and cron could not
  decide it. GitHub delivered `prices.yml`'s `7 */3 * * *` **30–160 minutes late and dropped
  about a third of the firings** (11 cron workflows in this repo contend for the scheduler):
  the observed cadence was **~5 passes a day, worst gap 7.8h**, with nothing tied to 17:00. On
  14 Sep the last pass ended 13:42, the wave flipped at 17:00, and the table showed Expedition
  33, Divinity: OS2 and everything else in that wave at **full price** while the store had them
  at −20% / −82%. Three changes, all in `price_and_sale.py` + `prices.yml`:

  - **The run loops passes instead of doing one.** `main()` now repeats `run_pass()` until its
    `RUN_MINUTES` budget (300, ≈5 passes) is spent, **sleeping between passes so each one
    starts on the hour**. A pass always begins at 17:00. The cron drops to `0 * * * *` and
    becomes a *watchdog* — its only job is restarting the looper when a run ends or dies, which
    it may do late without anyone noticing, and the existing `steam-prices` concurrency group
    collapses firings that arrive mid-run. A late dispatch is no longer wasted either: a run
    that boots at :10 sweeps the leftover 50 minutes and **resumes where the clock cut it off**
    on the next pass, so a short window can never starve the tail of the catalog.
  - **A pass is seeded from the last one.** Passes used to build `prices` from `{}`, so every
    mid-pass checkpoint published a **truncated** `prices.json` — at 12:59 the live file held
    13,900 of 110,640 rows and the frontend fell back to `games.json`'s slow prices for the
    rest. Tolerable at 5 passes a day; at one an hour the site would have spent most of its
    life on a partial file. `load_seed()` starts each pass from the committed file (pruned to
    the current catalog), and the price pass replaces every row it reaches wholesale, so a
    checkpoint is now "everything we knew, plus what this pass has refreshed" and nothing stale
    survives a sweep. It also fixes a latent bug: the ~340 cached `avail` verdicts were read off
    disk in pass 1c, *after* this pass's own checkpoints had already overwritten them.
  - **`CHECKPOINT_SECONDS` 300 → 600.** A checkpoint rewrites all 18 MB of `prices.json`; at
    ~5 passes per run the old interval would have quintupled commit volume on `main`.

  Net effect: a price is at most ~1h old instead of up to 8h, and the 17:00 wave is picked up
  the same hour it lands. Covered by `test_price_cadence.py` (hour alignment, seeding, resume
  rotation, loop scheduling on a fake clock — no network, git or repo files). Note that
  `FRESHNESS.md` scored this job **🟢 on time** throughout, because §11.6 compares against the
  *nominal* cron rather than observed dispatch times — an 8-hour hole reported green (§4, §7).

- **Min sale % floor (Sep 2026).** A `−5%` / readout / `+5%` stepper in the Value section, so a
  20-page table of `-10%` cuts can be narrowed to real discounts. Its resting value and both its
  limits are read from the current results rather than hard-coded, and setting a floor takes the
  **Full** price type off with it. Full mechanics in §11.

- **Review-score colour: two switchable schemes, one anchor format (Sep 2026).** `ratingColor()`
  cut at **85 / 70 / 40**, which made colour a worse signal than the number it decorated: an
  **82%** game and a **70%** game wore the *same* gold — a 12-point gap rendered identically —
  while 84 vs 85, one point apart, flipped gold→teal. The thresholds are gone. Colour is now
  declared as **anchors on the 0–100 scale**, blended per-channel in RGB between consecutive
  anchors, and that single format expresses both shipped schemes with **no mode flag and no
  branching**:

  - repeat a colour at two anchors → that span is **flat**
  - put two anchors **one point apart** → no integer between them to blend → **hard edge**

  **`spectrum`** (default) — a continuous warm ramp with a cool cap. 0–50 flat red `#FF5252`,
  51–59 red→orange, 60 orange `#FFA033`, 61–74 orange→yellow, 75 yellow `#FFE14D`, 76–84
  yellow→green, 85–95 flat green `#4ADE80`, 96–99 green→blue, 100 blue `#4DA6FF`. Blue sits
  deliberately off the warm→cool temperature axis so a perfect score reads as a separate mark
  rather than "even more green". Straight RGB lerping is normally wrong for a red→green ramp —
  the line cuts through the middle of the colour cube and the midpoint goes muddy olive — but it
  holds here because the ramp never interpolates red→green directly: it routes through orange and
  yellow, and every leg keeps a channel pinned high (R stays 255 from red to yellow), so the path
  follows the bright face of the cube instead of diving through its centre.

  **`rarity`** — five hard loot-tier bands, for reading a score as a *category* rather than a
  position: **Worn** 0–49 `#868C96`, **Common** 50–69 `#F0F2F5`, **Uncommon** 70–84 `#4ADE80`,
  **Rare** 85–94 `#4DA6FF`, **Epic** 95–100 `#B478FF`. Scores inside a tier are identical on
  purpose.

  Switching lives in the results bar beside View and Preview (it is a *display* preference, not a
  filter), persists to `localStorage` under `qtpd.scheme`, and rides along in `?scheme=` so a
  shared link carries it — a link's scheme beats the saved one. Each scheme's ramp is baked once
  into a **101-entry lookup** and cached per scheme (`ratingRamp()`), so no blending runs during a
  render. Output is **hex, not a CSS colour function**: an unparsed colour is an invalid
  declaration that drops silently — scores would fall back to inherited white and the dots, which
  take the value as `background`, would vanish. Every review-score surface reads the active scheme
  through the single `ratingColor()` — table **SCORE** (`all` + `30d`), the **WEIGHTED** column,
  card view and grid cards — so they cannot drift apart. `tierName()` puts the tier word into
  those tooltips, which is also what keeps `rarity` usable for anyone who cannot separate its five
  hues. The grid legend's swatch follows the scheme via `--rate-key`. The two greys that override
  all of this are about *confidence*, not score, and stay: `30d` greys when stale, Weighted greys
  below `CONFIDENT_REVIEWS` (§10).

  **Two known trade-offs, both deliberate and both reversible in the anchor data alone:**
  1. *spectrum* — an exact **100%** is 19.7% of scored games (23,671), but that cohort has a
     **median of 4 reviews** and 92.4% of it sits under 20 reviews. So blue, the most distinctive
     colour on the page, mostly marks statistically meaningless perfect scores, while the
     genuinely well-reviewed excellent games (96–99%, median **91** reviews) get the blend.
  2. *rarity* — **Worn `#868C96` is within 1.3:1 of `--muted-2` `#94a0c0`**, the colour this UI
     already uses for "no score / stale / low confidence", so a genuinely bad score and missing
     data look alike; and the tier populations invert the metaphor — **Epic** is only 6 points
     wide but **25.5%** of the catalogue (the largest tier), while the 50-point **Worn** band is
     10.8%.

- **The preview stage — a docked player for phones (Aug 2026).** The mobile grid could play a
  game's preview, but only inside the card: **171×80px** on a 390px phone, with a 16:9 clip
  cropped into a 2.14:1 box. Swiping up on a playing card — or pressing the new `⤢` chip —
  now moves it into a full-width **16:9 player docked under the sticky nav**, and every card
  below loads into that player on a tap, so you browse and watch without leaving the grid.
  Swipe down or `✕` to collapse. **Card view gains previews it never had**: its thumbnail was
  inert on touch, because its only path was a `mouseover` popup. The player is a third
  `.mediabox`, so `armMedia()` drives it with no new playback code; it lives outside
  `#gridview` because `render()` rebuilds that on every page bump, which would destroy a
  promoted card mid-watch. Phone-only — **nothing on desktop changes**. Grid pagination now
  **appends** instead of re-rendering, since scrolling is what you do while the player is up.
  Full design in §11.

- **Free-to-keep promos no longer fossilise as “-100% off” (Aug 2026).** A paid game that is
  free for a weekend makes `appdetails` self-contradictory — `is_free: true` and
  `discount_percent: 100` on top of the full price — and nothing undid that snapshot once the
  promo ended, because `price_and_sale.py` skipped `is_free` games and so never re-priced them.
  Moonlighter and Breathedge headed the discount sort at a fake -100% for weeks, with no QTPD.
  Now the **prices decide** in all three layers (`reconcile_price_flags()` at capture, the same
  rule in the price job, `reconcilePriceFlags()` in the frontend merge for records already on
  disk), the price job no longer drops a free-flagged game that carries a price, and
  `scraper.py` re-scrapes leftover contradictions first (`promo_residue()`). Five stored
  records were affected. Full write-up in §15; regression tests in `test_price_flags.py`.

- **`FRESHNESS.md` — daily data-freshness check (Jul 2026).** New generated doc + workflow
  (`freshness.py`, `freshness.yml`, task **4.3**, daily **07:00 UTC**), full design in §11.6.
  Coverage told us *how much* we hold; nothing told us *how current it is per task* without
  reading fifteen workflow histories by hand. The doc reports, for every scheduled task: its
  cron and worst-case cadence, when it **last actually wrote** its file, when it **fires next**,
  the **gap between those two**, then how many catalog rows it holds **up to date vs pending
  refresh vs pending fill**, and finally the **per-game wait distribution** (p50/p95/oldest)
  against each scraper's own refresh window. Three design decisions worth keeping:
  1. **Schedules are read, not restated.** `cron:` and `timeout-minutes` are parsed out of
     `.github/workflows/` at run time, so a schedule edit can never leave the doc stale — the
     failure mode that silently broke `coverage.yml`'s `workflow_run` link (see §4).
  2. **Gates are imported, not copied.** `import coverage as CV` pulls the cooldown constants and
     bucketers, so FRESHNESS.md and COVERAGE.md cannot disagree about "overdue".
  3. **Status counts missed fires, not an age ratio.** Each cron fire that passed without the
     output being re-stamped counts as a miss (🟢 0 / 🟡 1 / 🔴 2+), with the job's own
     `timeout-minutes` as grace. A ratio would let a daily job skip a whole run and still read
     green. Fill-only tasks (`tags`) are exempt and flagged 🔵 instead — they write only while
     unresolved games remain, so their silence is expected and their real problem is that the
     data is frozen, not that a run failed.

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
- **Wishlist import** extended to all five Steam ID formats via the Cloudflare Worker. *(That
  Worker's source is **not** in this repo and never was — see §12. `worker/` holds the Review
  Digest's proxy, a different deployment.)*
- **Workflows** bumped to `checkout@v5` + `setup-python@v6` (Node 24), chosen to preserve the
  fetch→rebase→push credential behavior.

---

## 17. Review Digest (`worker/` + `review_prompt*.md` + the `rd*` code in `index.html`)

An on-demand, per-game pull of **real Steam review text**, compacted into one block with an AI
prompt on top, so a reader gets a quantitative issue breakdown from actual players instead of
reading thousands of reviews by hand. It is the only feature here that is *not* part of the
scheduled pipeline: nothing is scraped ahead of time, nothing is committed, and no review prose
ever lands in the repo.

> **The design record is [REVIEW_DIGEST_PLAN.md](REVIEW_DIGEST_PLAN.md)** — 24 sections, every
> decision with the measurement behind it, including the Phase 0 probe (§14) that answered the
> empirical questions. This section is the *as-built* summary: what exists, where it lives, and
> the handful of properties you need to know before touching it. **Read the plan for any
> number you intend to change.**

**Shipped and live.** Phases 0 → 1 are done and everything through plan §24 (2026-09-15) is in
`main`. The plan's own phase list is a historical record, not a status board.

### What runs where

| Piece | Lives in | Role |
|---|---|---|
| `qtpd-reviews` Worker | `worker/index.js` (+ `README.md`, `wrangler.toml`, `test.mjs`) | The one piece of backend. Steam's `appreviews` endpoint sends **no** `Access-Control-Allow-Origin` (probe Q1) and QTPD is static, so the browser cannot call it directly. |
| The digest itself | `index.html`, the `rd*` functions | Fetch, compaction, signal precomputation, bundle assembly, the modal, copy/download. |
| The prompts | `review_prompt.md`, `review_prompt_simple.md`, `review_prompt_html.md` | Hand-authored, fetched lazily on first modal open, `cache: "no-store"`, each with a `<!-- vN -->` line echoed into the bundle. An inline fallback constant ships in the page — a digest must never be produced with no instructions attached. |
| The probe | `review_probe.py`, workflow `0.1` | Manual-only diagnostic. **Commits nothing**; findings go to the run log, the raw sample to a build artifact. |
| The tests | `test_review_digest.mjs` | Nine scenarios, **252 checks**, no network — the fixtures are synthetic. |

**The Worker is deliberately not a general-purpose proxy.** It forwards exactly one upstream
path shape with one allowlisted parameter set, to a `Set` of allowed browser origins — never
`*`. Without both constraints, anyone who found the URL would have an open relay to Steam's
whole domain running on someone else's Cloudflare account.

**Its source is in git on purpose, and that is the lesson from §12.** The wishlist Worker was
deployed without its source ever being committed and is now unrecoverable, which is precisely
why this feature was expensive to scope. Edit `worker/index.js`, deploy from it, keep the two
in sync; never patch it only in the Cloudflare dashboard. The frontend also keeps an escape
hatch — the deployed subdomain is an account-level detail the page cannot know for certain, so
a failed digest offers the proxy URL as an editable field and remembers the correction in
`localStorage` under `qtpd_reviews_proxy`. `REVIEWS_PROXY` stays the default.

### The properties that matter

- **Defaults, as of plan §24:** sample **5000**, output **HTML page**, quality bar **10+
  words**, reach **Every page**, mode **advanced**. Sizes offered are 300 / 500 / 1000 / 2000 /
  5000. Every one of those defaults moved once the constraint behind it moved — the 2000
  ceiling was 200k-context arithmetic, not a Steam limit (Steam paginates past 12,000 happily).
- **Nothing caps a size from underneath the reader.** 5000 means 5000. The cost is *priced*
  instead — the pill's tooltip names the token cost, the composer that survives it and the
  fetch time, and the result panel prices the finished bundle.
- **Reach is history, not accuracy.** `filter=recent` is newest-first in pages of 100, so any
  sample is a contiguous run of the newest N — on a busy game, 5000 reviews is about a month
  and a six-week-old patch is out of reach at the ceiling. *Every 2nd / 3rd page* keeps one page
  in N for N times the span at the same bundle size and the same token cost. **The skipped pages
  are still fetched**: Steam's pagination is cursor-chained, so a skip is a discard, never a
  saving — the reader pays in minutes, and the UI says so. Page 1 is always kept.
- **A uniform thinning is safe for proportions and unsafe for volume.** NOW and BEFORE thin by
  the same factor, so trends, sentiment splits and topic rates are unchanged; **reviews/day
  comes out N times under the truth**. The bundle therefore states the factor in two places —
  a `reach:` line on `SAMPLE` and a `SAMPLING:` line on `COVERAGE` — because the failure mode
  is a *confidently wrong* report, not a visibly short one. Same bargain as the quality bar:
  remove what would distort the counting, and say what was removed.
- **The compaction thresholds are measured, not guessed** (probe Q5 and the comments on each):
  `cap: 600` chars truncates 6.1% of reviews and saves ~35% of the budget; ASCII-art detection
  is a ratio + a 40-char floor because the prose/art gap is huge (prose ratio p90 **0.061**, art
  ~1.0); copypasta dedupe needs 20+ chars before identical text means anything.
- **A review count is not a size.** Measured across five games, one compacted line costs **57
  chars on Dota 2 and 185 on Valheim** — a 3× spread, so the same "2000" is 130 KB on one game
  and 312 KB on another. That spread is **reported, never enforced**.
- **`noiseFetchMax: 3`** bounds the over-fetch the quality bar needs. With the bar on, the walk
  is bounded by reviews *kept*, and how many pages that costs is a property of the game and
  unknowable before fetching. Hitting the ceiling is **not an error** — the header reports the
  short sample. Note the ordering: the page ceiling is computed in *kept* pages first and
  multiplied by the reach afterwards. The other order is the bug that makes reach 3 return a
  third of a sample and call it a quiet game.

### Where it collides with the rest of the doc

- It is the **one live cross-origin call** besides the wishlist import, and §1's static-first
  rule is what forced it through a Worker rather than a server.
- `review_prompt*.md` are the only `.md` files in this repo that the **browser downloads**. They
  are content, not documentation and not data — no job writes them, so §1's one-writer rule
  does not apply.
- Workflow `0.1` is numbered in the `0.` tier (§4) because it is a diagnostic against the live
  site's backend, not a pipeline stage. It is **manual-only and must stay that way**.
