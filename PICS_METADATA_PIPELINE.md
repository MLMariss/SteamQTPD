# PICS Metadata Pipeline — Design Spec

**Status:** SHIPPED — full-library sweep complete (123,560 games in `pics_raw/`
and `pics/` as of 2026-07-16). §2 coverage figures below updated from the
original 120-game probe to live full-library numbers; the authoritative live
figures regenerate into `COVERAGE.md` on every pass.
**Author:** MLMariss + Claude working session
**Scope:** New SteamQTPD data pipeline harvesting the Steam PICS `common`
app-info block via anonymous CM session. Adds AI-content disclosure plus a
large batch of previously-unscraped or better-sourced metadata (store tags,
Steam Deck compatibility, review-bomb-adjusted scores, dev/publisher, feature
categories, family-share exclusion, custom EULA presence).

This document is the authoritative record of **what we decided and why**.
The scraper (`pics_refresh.py`) and summarizer (`pics_summarize.py`) implement it.

---

## 1. Motivation

The original goal was narrow: scrape Steam's **AI content disclosure** (the
"AI Generated Content Disclosure" block on store pages). Investigation
established:

- There is **no dedicated AI-disclosure field** in the public
  `store.steampowered.com/api/appdetails` JSON. The store-page block is not
  surfaced as a top-level key there.
- The disclosure **does** exist as a clean structured field in the Steam
  **PICS** (`get_product_info`) `common` block, under the key **`aicontenttype`**.
  This is the same internal app-info source SteamDB reads.
- Fetching PICS requires a **stateful CM websocket session** (via
  `ValvePython/steam`), not a stateless HTTP GET — a different execution model
  from SteamQTPD's existing HTTP scrapers. **Anonymous login is sufficient**;
  no account credentials are required.

Because a PICS session is the expensive part, and the `common` block carries
**dozens of other useful fields for free in the same call**, the scope expanded
from "AI disclosure" to "harvest the whole `common` block once, derive many
fields."

---

## 2. Field investigation — what `common` contains

Probed against a 120-game sample (live top-100 most-played + curated seed
spanning F2P, AAA, indie, EA, VR, DLC, survival/strategy, AI-disclosed titles).

### 2.1 `aicontenttype` — the origin field

Small int enum stored as a string:

| Value | Meaning | Sample |
|---|---|---|
| absent | No AI disclosure | Dota 2, Balatro, most games |
| `"1"` | Pre-generated AI content (shipped assets made with AI) | THE FINALS (TTS commentators) |
| `"2"` | Live-generated AI content (runtime generation) | DREAMIO (ChatGPT + Stable Diffusion + TTS) |

- `"3"` (both) is plausible but no carrier seen; handle gracefully.
- **Live coverage (full sweep, 123,560 games): 12,653 ≈ 10.2%** — 12,247
  pre-generated, 406 live-generated. (Original 120-game probe read 13/120 ≈ 11%,
  which held up well.)
- The **free-text disclosure blurb** ("we use TTS for…") is **NOT** in `common` —
  it lives only on the rendered store page. PICS gives the **flag + category only**.
  This is deemed sufficient: a typed flag is more filterable than prose.

### 2.2 High-value keepers (tier 1)

The **Sample** column is the original 120-game probe (top-played/AAA-heavy).
The **Live** column is the full-library sweep (123,560 games in `pics/`) and is
the authoritative figure — it is regenerated automatically into `COVERAGE.md`
(§"PICS metadata — sub-metric coverage") on every pass. Where the two diverge
sharply (Deck, review-bombs, metacritic), the sample was biased toward famous
titles; the Live figure is the true real-world incidence.

| Key | Sample | **Live** | Shape | Why we keep it |
|---|---|---|---|---|
| `store_tags` | 99% | **99.9%** | `{"0":"4115","1":"1695",…}` ordered tag-ID list (top ~20, rank order) | Ranked community tags. Hard to get cleanly elsewhere. **Biggest win.** Needs tag-ID→name lookup. |
| `steam_deck_compatibility` | 97% | **27.2%** | struct: `category`, `steamos_compatibility`, `steam_machine_compatibility`, `test_timestamp`, `tested_build_id`, `tests{}` | Deck verified/playable/unsupported + new Steam Machine compat. Not in appdetails. Sample was AAA-heavy; most of the long tail is simply unrated by Valve. Of 33,658 rated: 8,116 Verified / 19,401 Playable / 6,141 Unsupported. |
| `review_score` | 99% | **64.0%** | `"1"`–`"9"` bucket | Valve's canonical review bucket. Live figure tracks the review-floor (games with too few reviews carry no bucket). |
| `review_percentage` | 99% | **64.0%** | `"0"`–`"100"` | Canonical % positive. |
| `review_score_bombs` / `review_percentage_bombs` | ~10% | **0.1%** | same shapes | **Review-bomb-adjusted** score. Present only on bombed games (just 97 across the library — the sample's ~10% was famous-bombed-title bias). Compare vs raw to *detect* review bombing. |
| `associations` | 100% | **99.9% dev / 99.6% pub** | `{"0":{"type":"developer","name":"…"},…}` | Structured dev/publisher/franchise. No HTML parsing. |
| `category` | 100% | **100.0%** (emitted as `cats`) | `{"category_2":"1","category_1":"1",…}` | Feature flags. **Primary source for mode filters** (Single-player 95.9%, Multiplayer 17.8%, Co-op 9.8%, Online Co-op, Split-screen, PvP/Online PvP), **controller support** (cat 28=Full 22.7% / 18=Partial 11.6% — `controller` field is 100% derivable from these), and **VR Only** (cat 54, 4.3%). Trusted over user tags for modes. |
| `genres` / `primary_genre` | 99% | **99.9% / 100.0%** | `{"0":"3"}` / `"3"` | Genre IDs. Needs genre-ID→name lookup. |
| `releasestate` | 94% | **98.8%** (emitted as `state`) | `"released"` / `"prerelease"` / … | Live vs coming-soon filter. **SHIPPED to `pics/` as `state`** (122,110 records: 121,640 released, 470 prerelease). Corrects the earlier "not yet emitted" note. |
| `supported_languages` | 99% | **99.9%** (langs) / **44.0%** (full-audio) | per-language `{supported, full_audio, subtitles}` | Richer than a flat list. |
| `steam_release_date` | 95% | **98.7%** | unix ts string | Release date. |
| `original_release_date` | 15% | **9.1%** | unix ts string | True original date for EA→1.0 games. |
| `metacritic_score` / `metacritic_name` / `metacritic_fullurl` | ~40-50% | **3.3%** | int / str / url | Metacritic when present. Sample was AAA-heavy; library-wide only ~4k titles carry a Metacritic score. |
| `content_descriptors` | 36% | **22.7%** (emitted as `content_desc`) | `[1,3,4,5]` int list | Mature-content flags. **Unrelated to AI** despite adjacency. **SHIPPED to `pics/` as `content_desc`** (28,006 carriers). Canonical Steam codes: 1=Some Nudity/Sexual, 2=Frequent Violence/Gore, 3=Adult Only Sexual, 4=Frequent Nudity/Sexual, 5=**General Mature — container marker** (present in 100% of carriers; never gate on 5). The adult gate uses **codes 3+4** (7,825 titles). **Code 1 is deliberately NOT used** — it over-flags mainstream titles (Witcher 3, BG3, Cyberpunk) just like the old user-tag heuristic. |

### 2.3 Promoted keepers — investigated specially (tier 2)

Sample = 120-game probe; Live = full sweep (`fse`/`eula` are emitted to `pics/`
and tracked in COVERAGE.md; the cheap tier-2 flags below `eulas` are kept in raw
but not all surfaced in the summarized view yet).

| Key | Sample | **Live** | Decision | Notes |
|---|---|---|---|---|
| `exfgls` | ~24% | **0.7%** | **KEEP as family-share-exclusion flag** | See §2.4. Name = "EXclude From Family Library Sharing". Presence ⇒ NOT shareable. Only 838 titles library-wide — the sample's 24% was launcher/DRM-AAA bias. |
| `eulas` | 60% | **8.9%** | **KEEP as `has_custom_eula` + names** | Presence ⇒ extra agreement(s) to accept. Struct: `{id,name,url,version}`. 10,988 titles library-wide. |
| `market_presence` | 10% | not in summarized view | keep (cheap) | Steam Market items exist. |
| `workshop_visible` | 30% | not in summarized view | keep (cheap) | Workshop support. |
| `parent` | 1-2% | not in summarized view | keep | DLC → base app linkage. |
| `mastersubs_granting_app` | 1-2% | not in summarized view | keep | Subscription-granting app linkage. |

### 2.4 `exfgls` — family-share exclusion (verified)

Tested against 33 games split into known SHAREABLE vs NOT_SHAREABLE buckets.
**Result: 31/33 agreement.** Confirmed decode:

- **absent** ⇒ family-shareable (18/18 clean in sample)
- **`"1"`** ⇒ excluded — launcher/account/DRM games (Apex, Sims 4, Diablo IV, GTA V, R6, DayZ, CoD, PoE2, Warframe, CS2)
- **`"3"`** ⇒ excluded — Valve F2P + similar (Dota 2, TF2, War Thunder)
- **`"6"`** ⇒ Applications, not games (Wallpaper Engine, OBS, Crosshair X)
- **`"0"`** ⇒ Application variant (Soundpad)

**Modeling rule (important — polarity + soft edge):**
- `family_share_excluded = ("exfgls" in common)` is **high-confidence** when TRUE
  (0 false positives in 33 games).
- **Absence does NOT guarantee shareable** — 2 own-account games (PUBG, Destiny 2)
  lacked the key despite being non-shareable (their restriction lives at the
  account layer, not this Steam flag). So model as a **positive exclusion signal**,
  never as an inverted "shareable" boolean. Surface the reason code optionally.

*Caveat: the "exclude" meaning is inferred from strong correlation, not official
Valve docs. Confident but not certified.*

### 2.5 Confirmed junk — dropped at ingest (tier 0)

Verified against sample; **dropped before archiving** (see §4 trim-at-ingest):

| Key | Why dropped |
|---|---|
| `clienticon`, `clienttga`, `logo`, `logo_small` | SHA1 hashes of *client-mode* icons (not URLs). Store art URLs already scraped elsewhere & more usable. |
| `clienticns`, `linuxclienticon` | SHA1 of Mac/Linux client icons. **Unreliable as OS-support signal** — disagreed with `oslist` on 19 (Mac) / 12 (Linux) of 120 games. `oslist` supersedes. |
| `gameid` | Literal duplicate of appid. |
| `controllertagwizard` | Internal Valve tagging-wizard flag. No player-facing meaning. |
| `icon`, `small_capsule`, `header_image`, `library_assets`, `library_assets_full` | Store art already sourced via existing pipeline. |
| `community_hub_visible`, `community_visible_stats` | Plumbing. |
| `clienticns`, `name_localized`, `name_linux` | Low value / redundant with `name`. |

*If ever wrong about a dropped field: re-sweep to recover (accepted worst case).*

---

## 3. Size analysis (measured, not estimated)

Measured on the 120-game probe:

| Metric | Value |
|---|---|
| Raw `common` per game | mean **5.4 KB**, median 5.3 KB, max 15 KB (tag-heavy AAA) |
| Trimmed (junk dropped) per game | **3.2 KB** (~42% reduction) |
| gzip ratio on this data | **10-15%** (keys repeat identically across games → highly compressible) |

Extrapolated to full library:

| Library size | Raw uncompressed | Trimmed uncompressed | Trimmed + gzip |
|---|---|---|---|
| 50k games | 259 MB | ~150 MB | **~14 MB** |
| 100k games | 519 MB | ~300 MB | **~29 MB** |

Per 64-shard file @ 100k games: ~1,560 games/shard ≈ **464 KB gzipped**
(~1.4 MB raw). Well under the near-limit shard-size warning previously hit.

### 3.1 Decompression cost (measured)

On a realistic full-size shard (1,560 games, 447 KB gzipped):

- gzip **decompress: ~9 ms** (negligible — native, linear, single-pass)
- **`JSON.parse`: ~118 ms** ← the actual cost, 13× the decompress
- The compression adds ~7% overhead; **data volume dominates, not gzip.**

**Conclusion:** gzip is a non-issue. Never store a manual "zip blob." See §5.

---

## 4. Storage architecture — decisions

### 4.1 Trim-at-ingest (DECIDED)

Drop the tier-0 junk (§2.5) **before archiving**. Rationale: the junk fields
are verified worthless, gzip already crushes them, and the ~40% archive
reduction is worth it. If ever wrong, re-sweep. Chosen over full-raw archiving.

**v2 additions (measured over 45 real games, evidence-driven):**
Bytes-per-key profiling showed two fields dominate the payload:
`steam_deck_compatibility` = **41%**, `supported_languages` = **17%**.

- **`languages` → dropped at ingest.** Verified strict keyset-subset of
  `supported_languages` (pure redundancy, 100% recoverable from the sibling key).
- **`steam_deck_compatibility` → nested-trim at ingest (Option B).** The bulk is
  the nested `tests` / `steam_machine_tests` / `steamos_tests` / `configuration`
  blocks (~1.4 KB/game of per-test display tokens — verified non-filterable).
  Keep the filterable scalars `category`, `steamos_compatibility`,
  `steam_machine_compatibility`, `test_timestamp`; **lift** two genuinely-useful
  flags out of `configuration` before dropping it —
  `requires_internet_for_singleplayer` (always-online-even-solo filter) and
  `hdr_support`. This is the **one deliberate exception** to "keep Layer 1
  faithful," justified because the dropped detail is pure UI trivia and
  re-scrapable.
- **`supported_languages` → NOT trimmed at ingest.** Per-language
  audio/subtitle flags are genuinely filterable (e.g. "has Latvian subtitles"),
  so kept faithful in Layer 1; collapse to lean lists in `pics_summarize.py`
  (Layer 2) instead. (Deferred summarizer task.)

**Measured result:** 3,607 → 2,126 B/game uncompressed (**−41%**). Note: gzipped
transfer size is ~unchanged (gzip already crushed the repetitive Deck tokens);
the win is on-disk archive size and JSON.parse speed (fewer objects), which is
the real client-side cost (§3.1: parse 118 ms ≫ decompress 9 ms).

Schema bumped to **`pics_raw_v2`** to mark the shape change.

### 4.2 Two-layer model

**Layer 1 — Raw archive (`pics_raw/`, source of truth, write-once-per-refresh)**
- The **trimmed** `common` block, stored **verbatim** (compact JSON), sharded 64-way.
- Per-game **fetch timestamp** stored for incremental refresh.
- This is the cold store. The frontend does NOT read it.
- Purpose: adding a future field = re-derive from this archive, **no re-scrape**.
  Satisfies "swap/include/exchange data points whenever" + "future-proof."
- ~50 MB gzipped @ 100k. ~780 KB/shard.

**Layer 2 — Derived `game_meta` view (`pics.json` shards, what the site loads)**
- Trimmed, **typed, decoded** projection: tags→names, deck→int category,
  review+bomb fields, `family_share_excluded` bool, `has_custom_eula` bool,
  AI type, dev/publisher, feature-category booleans.
- Merged client-side by appid, exactly like `playtime.json` / `ratings.json`.
- Regenerated from Layer 1 by `pics_summarize.py` — a field change is a
  summarizer edit + regenerate, **never** a scraper change.
- ~29 MB gzipped @ 100k. ~464 KB/shard.

This is the established SteamQTPD **`*_raw/` → `*_summarize.py` → shards**
pattern (as used by playtime), extended.

### 4.3 Shard key

Reuse the existing invariant: **`(appid // 10) % 64`**. One appid → one shard →
one file → one writer. The partition is a pure function of appid, so
one-writer-per-file holds by construction regardless of fetch parallelism.

### 4.5 Frontend decode strategy — IDs + maps, index-once (DECIDED: Option B)

`pics.json` (Layer 2) stores **numeric IDs**, not decoded names, for `store_tags`,
`genres`, and `category`. The three lookup maps (`tags.json`, `genres.json`,
`categories.json`, ~30 KB total) ship as static files the frontend loads **once**.

**Why B over A (names baked in):** the user-facing metric that matters is
toggle/filter responsiveness, and that is governed by *what filter comparisons
operate on*, not file size. IDs win on every axis the user feels:
- **Filtering:** integer/set-membership (`game.catSet.has(9)`) is faster than
  string matching (`includes("Co-op")`), and this runs across every game on
  every toggle.
- **Load/parse:** smaller data; short IDs parse faster than long strings repeated
  tens of thousands of times (JSON.parse is the real client cost — §3.1, 118 ms
  ≫ 9 ms decompress). Baking names writes "Free to Play" ~40k times.
- **Transfer:** IDs are shorter and compress well; the maps are a fixed one-time
  ~30 KB cost, not per-game.
- (A's only advantage is frontend-code simplicity — i.e. *developer* experience,
  not end-user experience.)

**MANDATORY implementation rule — index once, filter on IDs, decode only for
labels.** B is *only* faster if the frontend obeys this. On load:
1. Parse `pics.json` shards.
2. For each game, build filter indexes **once** — e.g. convert each game's tag /
   category / genre ID lists into `Set`s (`game._tagSet = new Set(tagIds)`),
   or build inverted indexes (tagId -> Set of appids) for O(1) filtering.
3. Toggling a filter = set-membership test against the prebuilt index. **Never
   re-parse or re-decode on a filter change.**
4. The lookup maps are touched **only when rendering a visible label** (turning
   ID `113` into "Free to Play" for a chip/column), never during filtering.

Filtering and display are decoupled: filter on IDs (fast), decode to names only
for the handful of on-screen labels (cheap). The anti-pattern that throws away
B's advantage is re-joining names on every toggle — this MUST NOT happen. If the
frontend ever re-decodes per filter change, B degrades to worse-than-A; the
index-once discipline is what makes it decisively smoother.

`pics_summarize.py` therefore emits IDs (ranked order preserved for tags), and
the frontend build must construct the filter index once at load.

### 4.4 Parallelism vs one-writer-per-file (resolved)

PICS fetching is **not** per-shard (a single `get_product_info` call batches
arbitrary appids across all shards). So we split phases:

- **Fetch phase** — parallel by *batch* (chunks of ~100-200 appids per call),
  NOT by shard. One anonymous session. Writes raw scratch (each worker owns its
  own scratch file → one-writer-per-file even here).
- **Write/partition phase** — a single summarize step groups by shard key and
  writes each shard file exactly once.

**Rule: fetchers never write shard files.** Fetchers write their own scratch;
the partition step writes shards. This avoids the multi-writer collision class
(same discipline as the earlier multi-workflow git-push collision fix).

---

## 5. Serving — HTTP Content-Encoding (Path 1, DECIDED)

**Store plain `.json` shard files. Do NOT store `.gz` blobs.**

GitHub Pages / CDN serves them gzipped over the wire automatically via
`Content-Encoding: gzip`; the **browser transparently decompresses before JS
sees it**. Consequences:

- `fetch('pics_shard_12.json').then(r => r.json())` is byte-identical to today.
  **Zero code changes, zero manual decompression, no library.**
- Files on disk stay **plain readable JSON** — `cat`-able, greppable, editable.
  No binary format, no lock-in, no schema-migration tooling.
- Transfer size is the compressed ~464 KB/shard; on-disk/committed size is the
  raw JSON (fine for the repo).

Rejected alternatives: explicit `.json.gz` files (needs `DecompressionStream` /
lib, marginal benefit) and binary formats like protobuf/msgpack (breaks
greppability, over-engineered for a solo project — gzip'd JSON gets ~90% of the
size win while staying debuggable).

---

## 6. Future-proofing

1. **Schema version field** on Layer 2 (`_schema` / `_format`) so old vs new
   derivations are distinguishable (matches existing `_format` convention).
2. **Per-game fetch timestamp** in Layer 1 → enables **incremental refresh**
   (re-pull only games whose `store_asset_mtime` changed, or on an age cycle)
   instead of full sweeps forever. Keeps ongoing cost bounded.
3. **Layer 1 archive** means any newly-wanted field is a re-derivation, not a
   re-scrape (except the tier-0 junk we deliberately dropped).
4. Size scales linearly and gzips to ~29-79 MB even at 100k — fine for the repo.

---

## 7. Lookup tables needed (one-time, small, committed)

The IDs in `store_tags`, `genres`, `category` are numeric and stable. Fetch
once, commit, cache:

- **tag-ID → name** (~450 entries; Steam tag endpoint). 278 distinct tags seen
  in just 120 games, so this is essential for readable tag filters.
- **genre-ID → name** map.
- **category_N → feature name** map (single-player, co-op, achievements, …).

These are stable enough to commit as static JSON and refresh rarely.

---

## 8. Environment / dependencies (verified on Windows)

`ValvePython/steam` 1.4.4 install gotchas encountered and resolved:

- `pip install steam` — core lib.
- Also requires `gevent`, `gevent-eventemitter` (NOT the generic PyPI
  `eventemitter`, which shadows the ValvePython fork and breaks `_listeners`).
- Requires `protobuf<3.21` **or** env var
  `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` (protobuf-version
  incompatibility otherwise crashes on import).
- Anonymous login: `client.anonymous_login()` — no credentials.
- Batch fetch: `client.get_product_info(apps=[...], timeout=…)` — batches
  hundreds per call.

**Note:** the CM session needs open network egress to Steam's servers
(`api.steampowered.com` + CM hosts). Runs fine on a normal dev box or GitHub
Actions runner; will NOT run in a restricted/allowlisted sandbox.

---

## 9. Pipeline components (to build)

| Component | Role | Pattern match |
|---|---|---|
| `pics_refresh.py` | Anonymous PICS session; batched fetch; write trimmed `common` + fetch-ts to `pics_raw/` scratch shards | analog of `playtime_refresh.py` → `playtime_raw` |
| `pics_summarize.py` | Read `pics_raw/`; decode via lookup tables; emit typed `game_meta` → `pics.json` shards | analog of `playtime_summarize.py` → `playtime.json` |
| `pics.yml` | Workflow: schedule fetch + summarize, staggered cron, concurrency guard | analog of playtime/ratings workflows |
| tag/genre/category lookup JSON | Static decode maps | new, committed |
| COVERAGE.md rows | Two-axis coverage for the new layer | extends existing coverage.py |
| ARCHITECTURE.md §N | Field→source→refresh table for PICS layer | extends existing authority doc |

---

## 10. Open items / deferred

- Confirm `aicontenttype = "3"` (both pre+live) handling when a carrier appears.
- `exfgls` reason-code meanings (1/3/6/0) are inferred, not certified — safe to
  ship the binary flag; treat codes as advisory.
- Incremental-refresh trigger (store_asset_mtime delta vs age cycle) — design in
  the refresh step; first run is a full sweep.
- Whether to surface Deck `tests[]` detail or just the top-level `category`.

### Shipped since first spec (corrected 2026-07-16)

The two fields previously logged here as "not yet implemented" **are now emitted
to `pics/`.** This block is kept as a correction of the record; both are live in
the summarized view and counted by `coverage.py`:

- **`content_descriptors` → `content_desc`** — SHIPPED, **22.7%** (28,006
  carriers). The earlier "dropped at Layer-1 trim, needs a re-sweep" note no
  longer holds — the key is retained through ingest and surfaced by the
  summarizer. Canonical Steam codes: 1=Some Nudity/Sexual, 2=Frequent Violence/
  Gore, 3=Adult Only Sexual, 4=Frequent Nudity/Sexual, **5=General Mature
  container marker (100% co-occurrence — never gate on it)**. The mature/adult
  gate uses **codes 3+4** (7,825 titles); **code 1 is excluded** because it
  over-flags mainstream titles. This **replaces** the old client-side
  `ADULT_TAGS` heuristic, which mis-flagged normal titles (e.g. Witcher 3) that
  merely carry a user "Sexual Content" tag.
- **`releasestate` → `state`** — SHIPPED, **98.8%** (122,110 records: 121,640
  released, 470 prerelease). Summarizer-emitted, no re-scrape was needed. Note:
  `state` is the live/coming-soon signal; it is **not** the Early Access signal —
  EA is **genre-70** (see §11 below).

### Still open / deferred

- Confirm `aicontenttype = "3"` (both pre+live) handling when a carrier appears.
- `exfgls` reason-code meanings (1/3/6/0) are inferred, not certified — safe to
  ship the binary flag; treat codes as advisory.
- Incremental-refresh trigger (store_asset_mtime delta vs age cycle).
- Whether to surface Deck `tests[]` detail beyond the top-level `category`.

---

## 11. Frontend data-model direction (decided 2026-07-16)

Working-session decision record for how the shipped PICS fields feed the
frontend. Implementation is a **separate future session**; this section is the
authoritative plan, mirrored in `ARCHITECTURE.md`.

**Tags — PICS primary, SteamSpy supplement.** `store_tags` (ranked IDs, 99.9%)
becomes the source of truth for the tag rail and display; the `TAG_GROUPS` /
`TAG_CAT` taxonomy remaps onto stable tag IDs (retiring the `CANON_GROUPS`
synonym-collapse maintenance). SteamSpy `tags.json` is kept only as a thin
coverage fallback for the ~34 games PICS lacks — **never** as a structured
signal.

**Feature categories (`cats`) — PRIMARY for modes.** User tags are unreliable on
long-tail / lesser-known titles; `cats` is authoritative Valve feature data.
Mode filters (Single-player / Multiplayer / Co-op / Online Co-op / Split-screen /
PvP / Online PvP) read `cats`, not tags.

**Genres — backend only, no filter rail.** PICS `genres` is the trustworthy
taxonomy (Indie/Action/RPG/Strategy/…), but a genre rail would duplicate the tag
rail, so genres stay backend-only: used for the **EA signal (genre-70)** and
primary-genre display/sort.

**Early Access — genre-70 only (11,776 titles).** The SteamSpy "Early Access"
tag is **dropped for this signal** — user tags linger after a game leaves EA and
are wrongly added, so they are unreliable. genre-70 is Valve-authoritative and
catches ~4,344 titles the tag missed.

**Mature/adult gate — switched entirely to PICS (SHIPPED).** `content_desc`
codes **3+4** (Adult-Only Sexual / Frequent Nudity-Sexual), precomputed as the
`adult` flag by `pics_summarize.py`. Code 1 excluded (over-flags mainstream
titles); code 5 is a container marker. Replaces `ADULT_TAGS` (now a pre-PICS
fallback only). New blur UX: image blurred → 1st click shows an "18+?" confirm →
2nd click reveals the image and opens the store link (table thumb + mobile card).

**"Flags" cluster — its own collapsible filter-section** (SHIPPED): Early Access
(genre-70), AI disclosure (any/hide/only, 10.2%), Controller (any/full/partial
from `cats` 28/18), Steam Deck (any/verified/playable+/unsupported from
`deck.cat`), Adult (any/hide/only), VR Only (`cats` 54, 5,372), Family sharing
(excluded, 0.7%), Custom EULA (8.9%). All URL-serialized/shareable. Filters
no-op unless `pics.json` carries games (HAS_PICS guard), so the empty placeholder
never zeroes the list.

**Filter-bar layout (SHIPPED).** Compact side-by-side: header (caret + title +
active-count) in a ~132px left column, controls beside it in the reclaimed
gutter. Collapse toggles on the header column only (never the control body), so
a near-miss/hover-then-click on a filter or tag never folds the section. Reverts
to stacked layout ≤720px. "Quality & Activity" renamed to "Quality".

**Data delivery — one slim merged file.** `pics_merge.py` merges the 64 `pics/`
shards into a single `pics.json` (the file the frontend fetches), dropping the
parked backend-only fields (dev/pub/franchise/langs/audio) for a ~37% smaller
transfer (12.4 MB → 7.8 MB gz). Wired into `pics.yml` as the stage after
summarize; without it `pics.json` is never written.

**Rating — games.json primary, PICS validator.** games.json `rating_pct` (93.6%)
stays primary; PICS `rev` (64.0%) validates (agrees ±2 pts on 77,556/79,013
overlapping games; a >5-pt divergence flags staleness / review bombing).

**Parked (backend only, no frontend now):** dev/publisher (99.9%), franchise
(23.5%), supported languages (99.9% / 44% audio), review-bomb-adjusted score and
review-bombing detection (retained in data, deliberately **not** surfaced).
