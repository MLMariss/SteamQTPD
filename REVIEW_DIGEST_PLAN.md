# Review Digest — design memo

**Status: Phase 0 run (§14) · Phase 0.5 Worker written, awaiting deploy. Phase 1 next.** Design record for a per-game, on-demand pull of
*real Steam review text*, packaged into one copy-paste block with an AI prompt attached, so
the user gets a quantitative issue breakdown from actual players instead of reading 500
reviews by hand.

All design questions are **decided** (§12); the empirical ones were answered by the Phase 0
probe on 2026-08-28 (**§14 — read this first**, it changes §3, §5 and §6).

**Later than the phases below:** §16 and §17 record what shipped on 2026-08-30 — precomputed
topic signals and the NOW-window fix (§16), then the whole deferred output redesign (§17).
§15 holds the specs §17 was built against and is kept as written, not updated to match.

Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md) (§1 design principles, §3.1 the
review-TEXT roadmap item this is adjacent to, §12 the Worker) · [ROADMAP.md](ROADMAP.md).

---

## 1. The flow

```
1. find a game            → existing search / filters, no change
2. hit the reviews entry  → grid card button, or the review count in the table row
3. QTPD fetches 500 real reviews from Steam, live, in the browser
4. QTPD compacts them into one text block, AI prompt on top
5. Copy / Download .txt   → paste into any AI → quantitative answer
```

The benefit is step 3–4: **capture of most/all recent reviews stops being a manual
process.** QTPD does not summarize — it does the *collection and packaging*, which is the
part that is tedious, mechanical, and currently impossible by hand.

---

## 2. Why on-demand, and not a scraped data layer

ARCHITECTURE §3.1 already did this arithmetic for the keyword-extraction item: raw review
prose for ~1000 reviews × ~78k games is **1 GB+**, and `playtime_raw/` is *already* sharded
across 64 files because it hit GitHub's 100 MB/file cap. Storing review text in the repo is
not on the table.

On-demand fetch dodges the whole problem: **nothing is stored.** It also gets freshness for
free — the reviews are as of the moment the button was pressed, which for "is this game
fixed yet" is the entire question.

**This is not the ROADMAP §3.1 item.** That one is *aggregate keyword counts for every
game*, scraped during the existing playtime walk, surfaced as a column. This one is
*per-game, full-fidelity, on request*. They are complementary and should not be merged:

| | §3.1 keyword layer | this (Review Digest) |
|---|---|---|
| coverage | all ~78k games | one game, on click |
| fidelity | counts from a fixed lexicon | the actual prose |
| freshness | last playtime sweep | live |
| cost | free (rides an existing walk) | ~5 requests per click |
| storage | one small JSON | none |

§3.1 explicitly parked its Option **C** ("LLM one-line summary per game") because it "adds
an external-model dependency + cost + batch job, breaking the runs-entirely-free model."
The Review Digest gets C's payoff **without** that dependency: it exports the prompt and
lets the user's own AI do the inference. No model, no key, no cost, no batch job.

---

## 3. ~~THE BLOCKING UNKNOWN~~ — ANSWERED: no CORS. Branch A1.

> **Probe result (2026-08-28): `appreviews` returns NO `Access-Control-Allow-Origin`.**
> Status 200, full JSON, but no CORS header — and the OPTIONS preflight returns nothing
> either. ARCHITECTURE §1 holds for this endpoint too. **A Cloudflare Worker passthrough
> is required.** The rest of this section is the reasoning that led there.

ARCHITECTURE §1 states flatly: *"Steam sends no CORS headers, so the browser cannot call
Steam directly."* That was established for the **storefront/wishlist** endpoints. It has
**not** been verified for `store.steampowered.com/appreviews/`, which is a different
endpoint, and the answer decides whether this feature needs a backend at all:

- **Branch A0 — CORS present.** Zero backend. The whole feature is client-side in
  `index.html`. No new job, no new data file, no Worker, nothing to deploy or keep alive.
- **Branch A1 — no CORS.** A Cloudflare Worker passthrough (~40 lines) forwarding
  `/?reviews=<appid>&cursor=…` to Steam and re-serving it with
  `Access-Control-Allow-Origin`. Same pattern as the wishlist proxy (§12) — but that
  Worker's source is **not in this repo and appears to be lost**, so A1 realistically means
  writing a new one.

**The probe cannot run from a dev sandbox** — `store.steampowered.com` is blocked there
(verified 2026-08: proxy returns 403 to CONNECT). Same wall the trailer work hit, resolved
there with a runner-side dump mode (`QTPD_DUMP_TRAILERS=1`). Do the same here.

**This does not block starting.** The UI, compaction, format and prompt are identical in
both branches, behind one seam:

```js
async function fetchReviewPage(appid, params, cursor) { … }   // A0: Steam. A1: Worker.
```

---

## 4. What Steam actually gives us

`GET https://store.steampowered.com/appreviews/<appid>?json=1` — public, **keyless**,
already called by three jobs in this repo (`scraper.py:506`, `playtime_refresh.py:531`,
`recent_refresh.py`).

| param | values | note |
|---|---|---|
| `filter` | `recent` / `updated` / `all` | `recent` paginates deep and reliably; `all` is helpfulness-ranked and unstable past a few cursor pages — **verify in the probe** |
| `language` | `english` / `all` | §5 |
| `review_type` | `all` / `positive` / `negative` | reserved for a possible Balanced mode |
| `purchase_type` | `all` / `steam` / `non_steam_purchase` | repo jobs use `all` |
| `num_per_page` | max **100** | hard cap → 500 = 5 requests |
| `cursor` | `*` then echo | must be URL-encoded |
| `filter_offtopic_activity` | `0` **includes** review bombs | we include — §5. Confirm the polarity in the probe |

**Per-review fields** (audited in ARCHITECTURE §3.1 — all of this is already fetched and
discarded on every playtime run): `recommendationid`, `review`, `voted_up`, `votes_up`,
`votes_funny`, `weighted_vote_score`, `comment_count`, `timestamp_created`,
`timestamp_updated`, `language`, `steam_purchase`, `received_for_free`,
`written_during_early_access`, `primarily_steam_deck`, and `author{ steamid,
num_games_owned, num_reviews, playtime_forever, playtime_at_review, last_played }`.

**`query_summary`** (first page, `cursor=*`): `total_reviews`, `total_positive`,
`total_negative`, `review_score_desc`. **The population anchor** that keeps the AI's
percentages honest (§5).

---

## 5. Sampling — decided

**Default: the 500 most recent reviews, `filter=recent`, English, review bombs included.**

**Size — 500.** 5 requests. ~125 KB raw ≈ **~31k tokens**, ~20–25k after compaction (§6).
Comfortable for Claude and most modern chat boxes; revisit if it proves too big in
practice.

**Mode — `recent`.** Answers "what is this game like *now*", which is the actual question,
and it is the filter that paginates reliably.

**Language — English by default, an *All languages* toggle in the modal.** English-only
gives clean signal and reliable counting; the toggle exists because on a
Chinese- or Russian-majority game, English-only is a genuinely misleading minority slice.
The header always prints the non-English share so the limitation is visible either way.

**Review bombs — INCLUDED (`filter_offtopic_activity=0`).** Deliberate. There is no such
thing as an illegitimate review here: if a publisher or developer did something people are
angry about, that *is* something a prospective buyer needs to see, and it will come out in
the analysis on its own. Suppressing it would be QTPD deciding which player anger counts.

**But the prompt must separate it, not blend it.** A coordinated campaign and a crash bug
are both real and both worth knowing — they are just not the same fact. So the prompt
requires the AI to:
1. detect a coordinated spike (publisher, DRM, price change, politics) and report it as its
   **own section** with its own count and date range, and
2. give the issue table **both ways — with and without those reviews**.

That way you see the campaign *and* the underlying game quality, and neither one hides the
other.

**Anchor coupling — RESOLVED (probe §14).** `query_summary` **does** respond to
`filter_offtopic_activity`, so the anchor is fetched with the *same* setting as the sample
and there is exactly one consistent number. No dual-anchor needed. It also responds to
`language`, so an English sample gets an English anchor — which is the correct comparison.

**Honesty rules that make the numbers mean something:**
1. `query_summary` is always printed — the true all-time split is at the top, fetched with
   the same `language` and `filter_offtopic_activity` as the sample.
2. The sampling mode is stated verbatim in the header.
3. The prompt must report counts as "N of the 500 sampled", never as a bare percentage of
   the game.

---

## 6. Compaction — the token problem

Measured, not guessed — every number below comes from the Phase 0 run (§14).

| pass | what | measured effect (§14) |
|---|---|---|
| **Per-review cap** | 600 chars + ellipsis | truncates only **6.1%** of reviews, saves **~35%** of the budget (20.9k → 13.7k tokens). The best lever by far — **keep 600** |
| **Near-duplicate drop** | hash of lowercased alphanumerics | 4 groups, **32 removable copies** in 1793 (1.8%). Small on tokens, but the worst offender was posted **30×** — exactly the skew this exists to stop |
| **ASCII/emoji-art drop** | non-alnum ratio > 0.4 on >80 chars | drops **2.1%** of long reviews. The distribution is **bimodal** (p50 0.034, p99 0.993), so *any* threshold from 0.30–0.60 gives the same answer — **0.4 confirmed, and it is not a sensitive knob** |
| **BBCode strip** | whitelisted tags only | **0.5%** of reviews contain markup. Near-worthless as a token saver — **keep it for cleanliness, not budget**. Must whitelist real tags: the probe's first regex matched `[sailing]` and `[russians]` in prose, and stripping those would delete words out of a review |
| **Whitespace collapse** | newlines → space, runs → one | cheap, keeps the one-review-per-line format intact |
| **Min length** | keep everything ≥1 char | see below — do **not** drop short reviews |

**The headline measurement: the median Steam review is 35 characters.** Not the ~250 the
plan assumed. The distribution is p25 **10** · p50 **35** · p75 **115** · p90 **377** ·
p99 **2081** · max **7953**.

**Budget at 500 reviews** (scaled from the probe's 1793):

| cap | tokens | truncated |
|---|---|---|
| none | ~20.9k | 0% |
| 800 | ~15.0k | 4.5% |
| **600** | **~13.7k** | **6.1%** |
| 400 | ~11.7k | 9.5% |
| 300 | ~10.4k | 12.2% |

Even **uncapped, 500 reviews is only ~21k tokens** — comfortably pasteable anywhere. The
600 cap is kept because a single 7,953-char essay is ~2,000 tokens of one person's opinion
and distorts the tally, not because the budget demands it.

**38% of reviews are under 20 characters** ("good", "gg", "+rep", "10/10"); 6.2% are under
4. This does *not* become a filter. Short reviews are real sentiment, they cost ~2 tokens
each, and dropping them would bias the sample **negative** — one-word reviews skew positive.
Filtering them would also be QTPD deciding which reviews count, which is the same thing we
refused to do with review bombs (§5).

Instead the header **reports** it: `substantive: 310 of 500 have >20 chars of text`. The AI
is told the issue counts can only come from those, while the ▲/▼ tally legitimately uses all
500. Same principle as everywhere else here — do not hide the shape of the sample, state it.

**Every drop is counted and printed in the header.** The bundle never hides what it did to
the sample.

---

## 7. Output format

One review per line — JSON would roughly double the token count on braces, quotes and
repeated keys and buys nothing.

```
=== QTPD REVIEW DIGEST ===
You are given real Steam reviews for one game. Instructions are at the BOTTOM.

GAME: Cyberpunk 2077  (appid 1091500)
ALL-TIME: Very Positive — 79% of 723,411 reviews  (571,494 ▲ / 151,917 ▼)
LAST 30 DAYS: 88% of 4,210
SAMPLE: 500 newest English reviews (filter=recent) · fetched 2026-08-28
  sample split: 402 ▲ / 98 ▼  (80% positive)
  off-topic/campaign reviews: INCLUDED
  excluded: 21 ASCII-art · 9 duplicates · 78 truncated at 600 chars
  language: english only (≈38% of this game's reviews are other languages)
LEGEND: ▲/▼ recommends · Nh hours at review · date · ↑N helpful votes
        [EA] written during early access · [free] free/non-Steam copy
        [deck] played on Steam Deck · [upd] review edited after posting

--- REVIEWS (500) ---
▲ 142h 2026-08-27 ↑31 [upd] | Best it's ever been. Holds 90fps on a 3070 since 2.3 …
▼ 8h 2026-08-27 ↑4 | Crashes on every alt-tab with a DX12 device-removed error. Refunded.
▼ 61h 2026-08-26 ↑12 [deck] | Runs at 25fps docked, police AI still teleports …
▲ 340h 2026-08-26 ↑8 [EA][free] | Review key. Rough at launch, but the 2.0 rework …

--- INSTRUCTIONS ---
[loaded from review_prompt.md — see §8]
```

**Prompt at top and bottom.** A short framing line first (so the reader knows what they are
looking at), the full instructions last (so they are the most recent thing in context after
a long paste). Known reliability pattern for long pastes; costs ~200 tokens.

### The four flags — and the requirement they create

All four ship, at ~2–4 tokens per review. **All four are confirmed live and populated**
(§14) — none is dead weight. Each changes what a review *means*:

| flag | source field | why it matters |
|---|---|---|
| `[EA]` | `written_during_early_access` | an EA review complaining about missing content is a completely different fact from a post-1.0 one |
| `[free]` | `received_for_free` or `!steam_purchase` | review-key and gifted reviews skew positive |
| `[deck]` | `primarily_steam_deck` | Deck performance is its own technical category, currently blended into general perf |
| `[upd]` | `timestamp_updated != timestamp_created` | someone revisited their verdict after patches — direct evidence for the trend question |

**Measured hit rates over 1,800 live reviews:** `[EA]` 600 (all of Valheim — still in early
access, so the flag is doing exactly its job) · `[free]` 165 (6 `received_for_free` + 159
`!steam_purchase` — the *inverted purchase* half carries almost all the signal, which is why
`[free]` is defined as either) · `[deck]` 22 · `[upd]` 68.

**The flags are useless unless the prompt uses them.** `review_prompt.md` must explicitly
instruct the AI to classify and sort on them — separate EA-era complaints from current
ones, break technical issues out by Deck vs desktop, note whether `[free]` reviews skew the
sentiment, and lean on `[upd]` for the trend section. Flags in the data with no instruction
to read them is just wasted tokens.

---

## 8. The prompt lives in its own file

**`review_prompt.md`, at the repo root, fetched at modal-open time.** The prompt is the
part that will be iterated on hardest and longest, and it should not require touching a
5,500-line `index.html` to tune a sentence.

- **Not a 14th load-time fetch.** The site fetches 13 files at load (ARCHITECTURE §2); this
  one is **lazy** — requested only when the modal first opens, ~2 KB, then cached in memory
  for the session.
- **`cache: "no-store"`**, matching the wishlist fetch pattern (`index.html:4488`), so an
  edit is live on the next modal open rather than after a CDN cache expiry.
- **Inline fallback constant.** If the fetch fails, the bundle still gets a minimal built-in
  prompt. A digest must never be produced with no instructions attached.
- **Not a data-layer file.** No job writes it; it is authored by hand. ARCHITECTURE §1's
  one-writer rule is about jobs racing each other and is unaffected.
- Optionally a `<!-- v3 -->` first line, echoed into the bundle header, so an output can be
  traced back to the prompt that produced it.

The prompt content itself is deliberately **not frozen in this memo** — it gets its own
iteration cycle in that file. It must cover: verdict · issue table with counts · the
technical/design/content/monetization/service rollup · the headline technical share ·
campaign detection as a separate section with both tallies (§5) · trend from dates and
`[upd]` · flag-aware classification (§7) · praise · and the sample-vs-population caveat.

---

## 9. UI — where it lives in `index.html`

**Grid card.** `.gi-actions` on the details face — `index.html:3973`, next to the existing
`Steam ↗`. Add **`Reviews ⇩`**. Already-styled slot, works on phone.

**Table row — zero extra space.** The Game cell already prints the review count under the
title (`.gsub`, `index.html:2668` — *"1,015,944 reviews"*). **That text becomes the
button.** It costs no new pixels, it already says the word "reviews", and clicking a review
count to get the reviews needs no explaining. Underline on hover + pointer cursor + tooltip
carry the affordance; make it a real `<button>` for keyboard access. Games with no
`review_count` render `"app 570"` instead and are simply not clickable. It sits *outside*
the `<a class="gtitle">` store link, so there is no nested-interactive problem.

**Modal.** New `#revModal`, reusing the `.pop-host` / `.pop-backdrop` styling at
`index.html:1907` — but **not** `#popover` itself, which is the small filter/CSV editor and
is the wrong size and lifecycle.

- **Controls:** language (English / All) · size · **Fetch**.
- **Progress:** `page 3/5 · 287 reviews · ~19k tokens`, live. Five sequential requests is
  2–4 s and silence reads as broken.
- **Result:** readonly `<textarea>` preview + **Copy all** + **Download .txt** (reuse the
  Blob pattern at `index.html:4366`) + live char/token estimate.
- **Abort:** `AbortController`; closing the modal cancels in flight.
- **Cache:** last few bundles in memory / `sessionStorage`, so re-opening is instant.
- **Failure:** the existing `toast()` at `index.html:4417`, same voice as the wishlist
  failure path.

---

## 10. Risks and answers

| risk | answer |
|---|---|
| **Steam rate-limits the browser** (A0) | 5 requests is far under the ~200/5min budget; 250–400 ms between pages, disable Fetch while one runs |
| **Worker IP shared across users** (A1) | Cloudflare Cache API keyed on appid+params, 30–60 min TTL — Steam's numbers barely move in an hour, and repeat opens become free |
| **The scrapers' own budget** | Untouched under A0 (the fetch comes from the *user's* IP). Under A1 it is the Worker's IP pool, still separate from the runners already sitting at `STEAM_DELAY = 1.5` |
| **`filter=all` cursor unreliable past a few pages** | Everything is built on `filter=recent`; confirm the depth limit in the probe |
| **Non-English noise** | English default, All as a toggle, non-English share always printed |
| **ToS** | Public, keyless, documented endpoint the repo already calls in three jobs; output is a user-initiated copy for personal use; **no prose cached in the repo** — which is also the storage answer from §2 |

---

## 11. Phases

**Phase 0 — probe. ✅ DONE — run 2026-08-28, findings in §14.** `review_probe.py` +
`.github/workflows/review-probe.yml` (`workflow_dispatch` only, never scheduled),
runner-side because the sandbox is blocked.

**To run it:** Actions → *0.1 Review Digest probe* → **Run workflow**. Optional inputs
`appids` (comma-separated) and `pages`. Read the findings in the run log; download
`review-probe-findings` from the run's Artifacts for `report.json` and the raw
`sample.json`.

It answers, in one ~5-minute run:

| # | question | what it decides |
|---|---|---|
| **Q1** | does `appreviews` send `Access-Control-Allow-Origin`? | **A0 (no backend at all) vs A1 (write a Worker)** |
| **Q2** | how deep does the `recent` cursor page cleanly? | whether a 500-review sample is even reachable |
| **Q3** | is `filter_offtopic_activity=0` really *include*, and does `query_summary` move with it? | whether the header prints one anchor or two (§5) |
| **Q4** | are the four flag fields present *and* populated? | whether any of `[EA]` `[free]` `[deck]` `[upd]` is dead weight |
| **Q5** | real length / art-ratio / BBCode / duplicate distributions | replaces the placeholder 600-char cap and 0.4 art ratio with measured numbers (§6) |

It also reports the **English share** per game, which is the evidence for how misleading
the English-only default is on a given title.

**It commits nothing.** Findings go to the run log, the raw sample goes out as a build
artifact — the no-prose-in-the-repo rule (§2) applies to the probe too. `permissions:
contents: read`, so it *cannot* write even by accident.

**Phase 0.5 — the Worker. ✅ WRITTEN (`worker/`), awaiting deploy.** The probe put us on
branch A1, so the browser cannot reach Steam and this had to exist before any of the UI
could be tested against real data. Deploy steps are in
[`worker/README.md`](worker/README.md); until it is deployed and `REVIEWS_PROXY` points at
it, Phase 1 has nothing to fetch from. What it does:

- One route: `/?appid=<n>&cursor=<c>&…` → `store.steampowered.com/appreviews/<appid>`.
- **Param allowlist and a numeric-appid check.** Without them this is an open proxy for
  anything on Steam's domain — it must forward only the handful of params in §4.
- CORS headers scoped to the Pages origin, not `*`.
- Cloudflare **Cache API**, 30–60 min TTL keyed on the full param set. Steam's numbers
  barely move in an hour and this makes repeat opens free.
- **Its source lives in this repo this time** (`worker/`). The wishlist Worker's source was
  lost, which is the entire reason A1 is expensive today; do not repeat that.
- `num_per_page=0` is preserved rather than clamped — it means *summary only, no bodies*,
  which is how the bundle header fetches its population anchor in one cheap call. An early
  `|| 100` fallback silently turned that into a full page fetch; caught by `worker/test.mjs`.
- Unit tests (`node worker/test.mjs`) cover the appid validation, the param allowlist, the
  clamping and the CORS allowlist — pure functions, no Cloudflare runtime needed.

**Phase 1 — MVP.** `fetchReviewPage` (pointed at the Worker) + compaction + formatter +
`review_prompt.md` loader + modal + copy + download. Recent / 500 / English, bombs in, all
four flags, substantive-count in the header. Both entry points (grid card + table review
count) — the table one is a one-line change, no reason to defer it.

**Phase 2.** Language toggle, size picker, session cache, prompt iteration against real
games.

**Decided against — the in-browser lexicon counter.** A JS keyword count of
technical-vs-design issues was considered and **dropped**. A hardcoded word list cannot
handle negation ("zero crashes, runs great" scores as a crash complaint), sarcasm, or
unlisted synonyms, so it would systematically undercount — and it would sit next to the
AI's answer as a second, worse verdict with no way to tell which is wrong when they
disagree. The AI doing this properly *is* the feature.

**…reversed on 2026-08-30. See §16.** The objection above is about a lexicon count in the
**output**, and it still stands — nothing keyword-derived is ever printed as a verdict. What
shipped is a lexicon count in the **input**: a `TOPIC SIGNALS` block inside the bundle, read
by the model and thrown away. It never reaches the reader, so there is no second verdict to
disagree with, and negation and synonyms are the model's job to correct rather than the
regex's job to get right. What forced the reversal was measurement: three models given the
same 500 reviews returned counts differing by up to 3.4x for the same issue, while every
number they merely *copied* from TIMELINE was identical in all three.

---

## 12. Decisions — all closed

| # | decision | choice |
|---|---|---|
| 1 | sample size | **500** most recent (revisit if it proves too big) |
| 2 | sample mode | **recent** |
| 3 | prompt placement | **inline in the bundle**, top + bottom |
| 4 | review bombs | **included** — a publisher screw-up is legitimate signal; prompt reports it separately and gives both tallies |
| 5 | in-browser lexicon count | **dropped as output; reversed as input 2026-08-30** — precomputed into the bundle for the model to check itself against, never printed (§16) |
| 6 | language | **English default, All as a toggle** |
| 7 | grid entry point | `.gi-actions`, next to `Steam ↗` |
| 8 | table entry point | **the review count in `.gsub` becomes the button** — zero extra space |
| 9 | prompt location | **`review_prompt.md`**, its own file, lazily fetched, iterated separately |
| 10 | per-review flags | **all four** — `[EA]` `[free]` `[deck]` `[upd]`, and the prompt must classify/sort on them |

## 13. Files touched

| file | change |
|---|---|
| `index.html` | one new ~400-line section: fetch, compact, format, modal; plus two small entry-point edits (`:2668`, `:3973`) |
| `review_prompt.md` | **new** — the Advanced prompt, iterated independently |
| `review_prompt_simple.md` | **added §18.4** — the Simplified prompt; same bundle, three sections, no percentages |
| `review_probe.py` + `.github/workflows/review-probe.yml` | **added** — Phase 0 diagnostic; read-only, commits nothing, manual trigger only |
| `worker/` | **added** — the A1 proxy (`index.js`, `wrangler.toml`, `README.md`, `test.mjs`). In-repo by design; the wishlist Worker's source was lost |
| `ARCHITECTURE.md` / `ROADMAP.md` | new section + cross-reference |

**No new data file, no new scheduled job, no change to any existing writer.**
ARCHITECTURE §1's one-writer-per-file rule is untouched and nothing here can interfere with
the scrapers.

---

## 14. Phase 0 findings — the live run

**Run:** [33168828313](https://github.com/MLMariss/SteamQTPD/actions/runs/33168828313) ·
2026-08-28 · Cyberpunk 2077 (1091500), Valheim (892970), Dota 2 (570) · 6 pages each ·
1,800 reviews · 55 s.

### Q1 — CORS: **no. Branch A1.**

`appreviews` returns 200 with full JSON and **no `Access-Control-Allow-Origin`** (the
OPTIONS preflight carries nothing either). ARCHITECTURE §1's claim holds for this endpoint
too. **A Cloudflare Worker passthrough is required** — this is the expensive answer, and it
adds Phase 0.5 (§11).

### Q2 — cursor depth: **no constraint whatsoever.**

All three games: **6/6 clean pages, 600 unique reviews, zero duplicates, cursor advanced
every time.** 500 is reachable with room to spare; the ceiling was never tested because
nothing pushed back. If 500 ever proves too small, more is simply available.

### Q3 — off-topic flag: **`query_summary` IS coupled.**

Cyberpunk: `include` 983,491 vs `exclude` 975,799 — **7,692 off-topic reviews, and the
summary moved.** Valheim and Dota 2 were identical only because neither game has any
off-topic reviews at all.

So the anchor is fetched with the same `filter_offtopic_activity` as the sample and there is
**one consistent number** — the two-anchor fallback §5 was braced for is not needed.

`query_summary` also tracks `language`: Cyberpunk is 983,491 reviews overall but 417,281 in
English. An English sample therefore gets an English anchor, which is the correct comparison
rather than a bug.

*Still unproven:* the **polarity** of `filter_offtopic_activity`. No game in the set had
off-topic reviews inside its newest 100, so "0 = include" is inferred from the totals, not
demonstrated per-review. Re-run against a game with an **active** bomb to close it. Low
risk — the totals only make sense one way — but it is inference, not observation.

### Q4 — flags: **all four alive.**

| flag | true | of 1,800 |
|---|---|---|
| `[EA]` | 600 | exactly Valheim's whole block — it is still in early access, so the flag is behaving |
| `[free]` | 165 | 6 `received_for_free` + 159 `!steam_purchase` — **the inverted-purchase half carries the signal**, so `[free]` must be defined as either |
| `[deck]` | 22 | rare but real |
| `[upd]` | 68 | real |

`received_for_free` alone (0.3%) would have been near-dead weight. Defining `[free]` as
`received_for_free OR !steam_purchase` is what makes it worth its tokens.

### Q5 — compaction: **the assumptions were wrong in useful ways.**

1. **The median review is 35 characters**, not ~250. Steam reviews are dominated by
   one-liners.
2. **500 reviews is only ~21k tokens uncapped.** The budget was never the problem.
3. **BBCode is 0.5% of reviews.** It was assumed to be everywhere. It is not a token lever.
4. **The art-ratio threshold is not a sensitive knob** — the distribution is bimodal
   (p50 0.034, p99 0.993), so anything from 0.30 to 0.60 drops the same ~2%.
5. **38% of reviews are under 20 characters.** The single most consequential finding for
   the prompt (§6).

### Bugs the live data exposed in the probe itself

- **BBCode regex matched any `[word]`.** The run reported `[sailing]×1` and `[russians]×1` —
  people writing brackets in prose, not markup. A strip pass built on that regex would have
  silently deleted words out of reviews. Now whitelisted to real tags.
- **Q3's verdict conflated "no off-topic reviews" with "not coupled".** Valheim and Dota 2
  were reported as "query_summary does NOT move" when the truth is they had nothing to
  exclude. That false negative would have sent the design down the two-anchor path for no
  reason. The verdict now distinguishes the two cases.

Both fixed. They are worth recording because both were reasoning errors that only real data
could catch — the same lesson ARCHITECTURE §2.1 recorded from the trailers: *dump first,
then tighten*.

---

## 15. Planned upgrades — output redesign (ALL FOUR SHIPPED — see §17)

Specs only, no rationale. Sourced from the three-model comparison on 2026-08-30 (Claude, GPT
and Gemini against one Salt 2 bundle, plus a Gears Tactics bundle, appid 1184050). Items 1
and 2 of that review shipped as **§16**; **15.1–15.4 shipped 2026-08-30 as §17**, which
records what was built, the two places the spec was read against a rule rather than
literally, and how it was verified. The specs below are left as written — they are the
contract §17 was measured against, not a running description of the code.

### 15.1 Output restructure

`review_prompt.md` only. No code change.

**Section order** — first screen is the answer, the rest is evidence:

1. `INTEGRITY`
2. `### Snapshot`
3. `### Who it's for` — new
4. `### Loved vs hated` — new
5. `### Where the complaints land` — new
6. `### Notes`
7. `### Issues` — the existing detail table, moved last

**Snapshot**
- Header row becomes `| Field | Value |`. The current `| | |` is an empty header row; strict
  markdown renderers drop the whole table (observed in Gemini, which rendered every digest
  table as raw pipes).
- New row: `| Dragging the score | <top issue> — <N>% of all ▼ reviews, <rising / flat / falling> vs before |`.

**`### Who it's for`** — two bullet lists, max 4 bullets each:

```
**Buy it if you** — <trait>; <trait>; …
**Skip it if you** — <trait>; <trait>; …
```

Rule: every `Skip it if` clause traces to an Issues row that cleared the floor, or to a
TOPIC SIGNALS family with hits. No clause may be inferred from the genre.

**`### Loved vs hated`** — one table, max 5 rows, columns ranked independently. Replaces the
standalone Praise table.

```
| # | Loved | N | Hated | N |
```

**`### Where the complaints land`** — bucket rollup, 5 fixed rows, counted at review level
(a review complaining about two Technical things counts once):

```
| Bucket | Reviews raising ≥1 | % subst |
```

**Issues table**
- `▼/▲` header becomes `Quit / stayed`, order pinned ▼ first (Gemini flipped it to ▲/▼).
- One legend line directly under the table: *"Quit / stayed — of the reviewers who raised
  this: how many refused to recommend / recommended anyway."*
- Blank line required before every table.

**Acceptance**: the same bundle through all three models produces the same section order,
and every `Skip it if` clause in all three traces to a table row.

### 15.2 Floor raise and headline row

`review_prompt.md` only.

- Row floor rises from 3 reviews to `max(5, 2% of substantive)`; everything under goes to the
  Other tally.
- **Headline row**: when one concrete complaint clears 8% of substantive, it gets a row whose
  `Category` is free text in players' own words, overriding the fixed taxonomy for that row
  only. Max one per report; sorts first regardless of `Now`.
  Reference case — Gears Tactics 1184050: the Xbox/Microsoft account requirement is ~12% of
  substantive and 30% of all ▼ reviews, but the fixed taxonomy splits it across
  *Always-online & DRM*, *Support & communication* and *Crashes & launch*, so it never
  surfaces as one thing.
- Notes must call out the Other tally when it is the largest row.

### 15.3 Sample-size selector

`index.html`. `RD.size` becomes state.

- Three options in the setup dialog beside the language toggle: **300 · 500 · 1000**, default
  500. Confirm ordering against the "default leftmost" rule before building.
- `rdCollect` already derives its page count from `RD.size`; the constant becomes
  `rdState.size`. Steam caps `num_per_page` at 100, so 1000 = 10 pages ≈ +2.5s at the current
  250ms inter-page delay. Worker needs no change — I/O-bound, nowhere near free-plan limits.
- Button label and the `Fetch <N> reviews` copy read from state. `RD.size` also appears in
  both entry-point `title=` attributes; both must follow.
- Dialog copy must say what 1000 buys: *history* on slow games, not accuracy.

**Blocked on**: §16. Unaided model counting degrades with length, so 1000 without the
precomputed signals is worse than 500.

### 15.4 Reader-focus toggles

`index.html` + `review_prompt.md`.

Checkbox row in the setup dialog. Each checked box appends one line to a
`--- READER FOCUS ---` block emitted immediately after `--- INSTRUCTIONS ---`.

| toggle | TOPIC SIGNALS family it maps to (§16) |
|---|---|
| Potato PC | Performance & FPS |
| Steam Deck / handheld | Steam Deck & handheld |
| Needs a third-party account | Third-party account & always-online |
| Microtransactions / DLC | Microtransactions & DLC |
| Co-op with friends | Multiplayer & servers |
| Short game / value | Length & content · Price & value |
| Political or ideological content | Political & ideological content |

Rules the block states to the model:

- Each focus gets a guaranteed Issues row **even below the floor**, including `0` — "nobody
  mentioned this" is a valid and useful answer.
- Each focus gets one line in `### Who it's for`.
- Counts only. No editorialising in either direction, on any focus.

**Blocked on**: §16 for the families, §15.1 for `### Who it's for`.

---

## 16. Shipped 2026-08-30 — precomputed signals and the window fix

Two changes, both aimed at the same measured problem: **the numbers this report copies are
right and the numbers it counts are not.** Three models given the identical 500-review bundle
returned 58 / 25 / 17 reviews for the same issue and 97 / 38 / 128 for the top praise row,
while every figure they copied from TIMELINE — NOW sentiment, the trend in points, the
`[free]` split — came back identical in all three.

### 16.1 The NOW window could swallow the sample — fixed

`rdWindows` widened the NOW window when the last 90 days held fewer than `RD_NOW_MIN`
reviews, but had no matching guard at the other end. On a game busy enough that 500 reviews
do not reach back 90 days, `calN === byTime.length`: NOW became the entire sample, `before`
came back empty, and TIMELINE printed **no BEFORE and no TREND line at all** — while the
prompt still asked the model for a trend in points. The model supplied one anyway.

Now the two cases are separate branches on `calN < RD_NOW_MIN`:

- **below** — widen to the newest `RD_NOW_MIN` (unchanged behaviour)
- **at or above** — cap NOW at `RD_NOW_MAX` (0.6) of the sample, guaranteeing a BEFORE side

They are exclusive on purpose. A single `min`/`max` chain lets the cap undo the widening on a
slow game, where NOW legitimately is most of the sample. Narrowing is disclosed on its own
line the way widening already was.

### 16.2 Coverage — how much real time the sample is

A review count says nothing about the span it covers, and the same 500 reviews are 25 months
of Gears Tactics (~20/month) and about two days of a hit. `-15 pts` means opposite things in
each, and nothing in the bundle distinguished them. Added:

- `COVERAGE:` sample span in days plus the rate — **per day** under `RD_THIN_SAMPLE_DAYS`
  (60), per month above it, because "~5073 reviews/month" extrapolated off three days is
  arithmetic nobody asked for.
- `SPANS  :` how many days NOW and BEFORE each cover.
- Warnings under `RD_THIN_SAMPLE_DAYS` (the sample carries no history) and under
  `RD_THIN_NOW_DAYS` = 14 (the trend is a same-fortnight read). Prompt rule 9 makes the model
  say so in the Snapshot rather than reporting a direction.

### 16.3 TOPIC MENTIONS — the lexicon count, reversed as an input

**This reverses §12 decision 5, and only halfway.** The original objection — a keyword tally
printed *next to* the model's answer is a second, worse verdict — still holds, and nothing
here is ever shown to a reader. What ships is a tally printed *inside* the bundle, which the
model reads, corrects against the text, and discards.

- 17 keyword families in `RD_TOPICS`. Each is a list of clauses; a clause may carry a `near`
  term that must also match, which is what makes "account" usable ("account" alone is
  meaningless, "account" plus "xbox" is decisive). Never add `/g` — the regexes are reused
  across every review and `/g` makes `.test()` stateful.
- Emitted as a pipe table: hits · now · before · ▼/▲ · helpful votes on the ▼ side · share of
  each window. Sorted like the Issues table so the model reads them in the same order.
- **A hit is a mention, not a complaint.** Praise matches too. Detecting complaints by regex
  is the negation problem that killed the original idea; the ▼/▲ column carries that
  distinction instead, read against `BASELINE ▼ RATE`.
- Families below 3 hits and at zero are named rather than dropped. "Nobody mentioned
  microtransactions" is a finding, and without the line the model cannot tell a family that
  was checked and found absent from one that was never looked for.
- Prompt rule 10: use it as a floor, explain in Notes any count that lands below half or
  above double the hits, and never reproduce the table. The block says the same about itself
  — a pipe table sitting in context is the most copy-pasteable thing in the bundle.

### 16.4 `[top]` — the reviews a buyer actually reads

The `RD_TOP_VOTED` (10) most-upvoted reviews are flagged on their own lines. A per-review
tally weights a drive-by one-liner the same as the review at the top of the store page, and
helpful votes are the only weight the sample carries.

Measured on Gears Tactics 1184050, 496 English reviews: **nine of the ten `[top]` reviews are
the Xbox account requirement, and all ten are ▼.** The same family carries 685 helpful votes
on its negative side and went from 8% of BEFORE to 16% of NOW. Nothing in the previous bundle
surfaced any of that, and no model counting by hand found it.

### 16.5 Verification

`test_review_digest.mjs` gains a third scenario (a 300-review sample across ~12 days) that
covers the narrowing branch, the per-day rate and both span warnings — the slow fixture only
ever exercised widening. The main fixture gains twelve topic-bearing reviews, three per
family across four families: without them every review matched nothing and the topic table
asserted a header with no rows under it.

Prompt is now **v6**: `[top]` in the line format, rule 9 (obey the coverage warnings, never
invent a missing TREND), rule 10 (TOPIC MENTIONS is a floor, not a source), a `Sample reach`
Snapshot row, and a Notes trigger for what the `[top]` reviews are about. The inline
`RD_PROMPT_FALLBACK` was updated in step — a stale fallback is the failure mode §8 already
recorded once.

**Not changed here**: the output skeleton. Restructuring it is §15.1–15.2 and is deliberately
separate, so the effect of the precomputed signals is visible against the current format
before the format moves under it.

---

## 17. Shipped 2026-08-30 — §15.1–15.4, the whole deferred set

Prompt is **v7**. All four specs went in together because 15.4 is blocked on 15.1 and
building the two prompt specs separately would have meant two rewrites of the same skeleton.

### 17.1 Output restructure (§15.1) and the floor raise (§15.2)

`review_prompt.md`, plus the inline `RD_PROMPT_FALLBACK` kept in step — §8's stale-fallback
failure mode, avoided the same way §16 avoided it.

Section order is now `INTEGRITY` · Snapshot · Who it's for · Loved vs hated · Where the
complaints land · Notes · **Issues last**. The Issues table is the working, not the finding,
and it spent five versions on the first screen because it was the first thing built.

Both spec'd tables landed as written: `| # | Loved | N | Hated | N |` with the columns ranked
independently, and the five-bucket rollup counted at review level. Snapshot gained the
`| Field | Value |` header and the `Dragging the score` row; the floor moved from a flat 3 to
`max(5, 2% of substantive)`; the `▼/▲` header became `Quit / stayed` with the order pinned and
a legend line under the table; every table now has a blank line before it.

Two things the spec did not say, decided here:

- **`Dragging the score` breaks rule 2's single-denominator rule** — it is a share of ▼
  reviews, not of substantive. Rule 2 now carries that as its one labelled exception, because
  an unexplained denominator switch in a file that forbids denominator switches is how a
  model decides the rule is soft.
- **The headline row's `Bucket` cell is not free text.** §15.2 frees the `Category`; leaving
  `Bucket` free too would put a sixth bucket in the rollup table and break its fixed five
  rows. The headline row keeps the bucket that fits best, and Notes says which others it drew
  from — which is the Gears Tactics finding stated rather than hidden.

### 17.2 Sample size (§15.3)

`RD.size` is gone; `rdState.size` replaces it, from `RD_SIZES = [500, 300, 1000]`.

**Order is 500 · 300 · 1000, not the spec's 300 · 500 · 1000.** §15.3 said to confirm against
the default-leftmost rule before building; the rule wins, and 500 leads.

Four places quote the number and all four now read state: the dialog copy, the fetch button,
and **both** entry-point `title=` attributes. The tooltips were the only hard part — the cards
are rendered long before the dialog opens, so `rdSyncEntryTitles()` reaches back and corrects
them on change. Without it the card promises 500 while the dialog fetches 1000.

Measured, on the ten-page fixture: 1000 reviews is 117 KB / ~29.8k tokens against 500's ~14k,
and 300 is ~12.2k. The dialog says what that buys — *history*, not accuracy — because on a
quiet game all three sizes cover the same years and the extra 15k tokens buy nothing.

### 17.3 Reader focus (§15.4)

`RD_FOCUS`, seven toggles, each mapping to the RD_TOPICS families that can answer it. Ticking
any emits a `--- READER FOCUS ---` block directly under the instructions — an amendment to the
task, not another input to weigh, which is why it sits there and not with the data.

The block restates all three rules in full even though prompt rule 11 carries them, because
the prompt is generic and only the block knows which focuses were asked for. The rule that
matters is the guaranteed row **at zero**: "nobody in 1000 reviews mentioned microtransactions"
is the answer the reader came for, and it is the one answer an unprompted report can never
give, because a bucket with no complaints simply does not appear in it.

Spec-vs-code naming: the spec's table says "TOPIC SIGNALS" and "Length & content"; the block
is called TOPIC MENTIONS (§16.3) and the family is `Length & amount of content`. Code uses the
real names — a focus pointing at a family that does not exist fails **silently**, which is why
the test asserts the mapping rather than trusting it.

Toggles are `.rd-opt` pills with `aria-pressed`, square-cornered to read as a multi-select
against the mutually-exclusive rows above them, rather than literal checkboxes.

### 17.4 Verification

`test_review_digest.mjs` gains 25 checks: the setup dialog's default-leftmost ordering, the
seven focus toggles and their `RD_FOCUS -> RD_TOPICS` mapping, the six output sections
asserted **by position** (§15.1's acceptance criterion is order, so order is what is pinned —
headings only, never prose, which is the mistake §16.5 already recorded), and a fourth
scenario that pages: 1000 reviews as ten cursor-chained requests, the four number sites moving
together, a focus toggled back off, and the block landing between INSTRUCTIONS and OVERVIEW.
It is the only scenario that pages — every other fixture returns an empty cursor and stops
after one page, so the loop the size selector consists of was previously never exercised.

**Not run here**: `node` is not installed on this machine, so the Playwright suite could not
be executed. Every one of the 25 assertions was instead run against a bundle built by the real
page in a browser — same page, same `rdBuildBundle`, same prompt file, with `fetch` stubbed to
serve ten pages — and all 25 pass. The Playwright wrapper around them is unexecuted code.

---

## 18. Shipped 2026-09-02 — discoverability, the handoff, and a short mode

Four items, all raised against the shipped feature rather than against the plan. Three are
about the *seams* — getting into the dialog, getting out of it, and reading the options — and
one adds a second report shape. Nothing in §16's precomputed arithmetic changes, and neither
does the bundle: §18.4 swaps instructions, not data.

### 18.1 The entry point was invisible

§9 chose the review count under the title as the table's entry point, on the reasoning that
"1,015,944 reviews" needs no explaining as the way to reach the reviews. That reasoning holds.
The *styling* did not: it was a **dotted** underline in `--line` sitting under text coloured
`--muted-2` — two of the faintest values in the palette stacked on each other. The affordance
was technically present and practically absent, and the count read as a label.

Now: `--muted` text (one step brighter than the subline around it), a **solid** 1px underline
in `--muted-2`, and a `⇩` caret after the number saying something gets pulled down. The caret
is a `::after` set to `display:inline-block`, which both keeps it out of `textContent` (screen
readers still hear "15,917 reviews") and stops the underline running beneath a glyph that is
not a word.

**Not gold at rest, deliberately.** Gold is this page's "this is the live pick" colour — the
lit filter, the active sort, the chosen sample size. Spending it on a control that repeats on
every row of a 128,000-row table would flatten its meaning everywhere it is doing real work.
Gold stays the hover and focus state, where it still means *this one*.

The grid card's `.gi-rev` was left alone: it is already a full button in an actions row and
was never the discoverability problem.

### 18.2 Sample sizes read as a number line, not a menu

`RD_SIZES` was `[500, 300, 1000]` — 500 first because §15.3 followed QTPD's "default leftmost"
rule for segmented controls. Against three *quantities*, that rule loses. The eye checks the
ordering of a number row before it reads the values, so `500·300·1000` registers as a typo and
costs more attention than a default that is not first.

Now `[300, 500, 1000]`, ascending, with `RD_DEFAULT_SIZE = 500` as the single place that
decides the default and the lit pill as the only thing that marks it. The test assertion was
inverted rather than deleted — it now pins ascending order, and the "500 is the one lit" check
becomes load-bearing instead of a restatement of the first check.

This is a local override of the leftmost rule, not a repeal of it. It applies where the
options form an ordered scale; the Language and Report rows are unordered choices and keep
their default first.

### 18.3 The handoff — three chat buttons

**What is not possible:** prefilling the digest into a chat through a link. A finished bundle
is 100–150 KB; `?q=` prefills cap out somewhere between 2k and 8k characters depending on the
service and the browser, and Gemini has no prefill parameter at all. There is no size of
digest we would realistically produce that fits, so a button promising "opens with the text
already in it" would be a button that lies.

**What is possible** is deleting every step except the paste. `Claude` / `ChatGPT` / `Gemini`
each put the bundle on the clipboard and open an **empty new conversation** in a new tab, so
the user arrives at a cursor in a blank composer with one keystroke left. The hint under the
row states the constraint plainly and points at `Download .txt` as the route for a composer
that refuses text that long — all three accept a `.txt` attachment.

Two implementation notes worth keeping:

- **Order is load-bearing.** Both the clipboard write and the `window.open` have to happen
  inside the same user gesture. Awaiting the copy first resumes in a later microtask with the
  gesture spent, and the popup blocker eats the tab. So the write is *started* and the tab is
  opened synchronously behind it; only the toast waits on the promise.
- `rdCopyBundle()` is shared with `Copy all`, and keeps the deprecated
  `execCommand("copy")` fallback: it is the only path that works on a page served without
  https, and on older iOS Safari where `navigator.clipboard` exists but rejects outside a
  narrow gesture.

The buttons are `.rd-opt` chips, not `.rd-go`. Three gold buttons in one row would leave the
panel with no primary action at all — same argument as §18.1.

### 18.4 Simplified vs Advanced

A second prompt file, `review_prompt_simple.md`, selected by a `Report` row at the top of the
setup dialog. Advanced is the v7 skeleton and stays the default — it is what this page
shipped with, and Simplified is the addition.

Simplified outputs **three sections and nothing else**: a 3–5 sentence prose `### Summary`,
the `### Who it's for` Buy/Skip pair, and one `### Best and worst` table of exactly five rows.
No Issues table, no bucket table, **no percentages anywhere** — but raw review counts stay, in
an `| # | Best | N | Worst | N |` table. Dropping the counts too was considered and rejected:
counting 500 reviews is the one thing the reader cannot do himself, and a ranked list with no
numbers behind it is unfalsifiable. A reader focus stays binding and gets its own
`### What you asked about` section, still answering zero as zero.

**Both modes ride the same bundle.** Identical reviews, identical TIMELINE, identical TOPIC
MENTIONS. Stripping the precomputed blocks to match the shorter output is the obvious economy
and the wrong one — they are ~1% of the bundle and they are the only thing standing between a
five-line report and five confident guesses. So this is a prompt-file swap, not a second code
path: `rdLoadPrompt(mode)` picks the file, caches per mode, and falls back per mode.

The `<!-- v7-simple -->` marker means the title line reads `prompt v7-simple`, which is what
the test uses to prove the file was really fetched rather than silently falling back.

### 18.5 Verification

`test_review_digest.mjs` goes from 74 to 88 checks and **all 88 pass**, run here against
Chromium — unlike §17, which had no `node` on the machine and had to verify by hand.

New: the ascending size order and the two report-mode pills in the setup dialog; the three
handoff buttons and the hint naming both the paste shortcut and the file fallback; and a fifth
scenario that picks Simplified with a focus ticked and asserts the swap actually happened —
the three simple headings present and in order, the counts column intact, the no-percentages
rule stated, and `### Snapshot` / `### Where the complaints land` / `### Issues` / `### Notes`
**absent**. That absence check is the point of the scenario: a Simplified pick that quietly
ships the advanced prompt looks completely fine until the model returns a twelve-row issue
table nobody asked for.

---

## 19. Shipped 2026-09-02 — the dialog was a wall of text

§18 shipped four working controls and explained each of them in a paragraph. Read back on a
laptop it was three screens of 11.5–12px prose wrapped around the four things you actually
click, and on a phone it was worse: the label gutter ate a third of the width and the copy ran
to a dozen lines before the first button. The controls were fine. The reading was the problem.

### 19.1 The prose moved onto the controls

Every option now carries its own `title`, and the paragraph it replaced is gone:

- **Report** — each mode's tooltip **names the sections it emits**, which is the only thing
  anyone wanted from those four lines. Simplified: *Summary · Who it's for · Best and worst.*
  Advanced: the same plus *Integrity · Snapshot · Loved vs hated · Where the complaints land ·
  Notes · Issues.* Held in `RD_MODES[].tip` beside the prompt filename, so the tooltip and the
  file it describes sit on one line and drift is visible in review. **Keep them in step with
  the two prompt files** — a tooltip promising a table the prompt no longer asks for is worse
  than no tooltip.
- **Sample** — `RD_SIZE_TIP` carries the "bigger buys *history*, not accuracy" argument per
  option: 300 is the cheap read that loses nothing on a quiet game, 1000 is the only one that
  reaches past the last patch cycle on a busy one. One six-word hint survives in the body
  because that trade-off is counter-intuitive enough to need saying unprompted.
- **Focus** — the tooltip is generated from the entry's existing `ask` string, so a new focus
  gets its explanation for free and cannot ship without one.
- **Language** and the intro's review-bombing disclosure likewise became tooltips.

The body copy that remains is one line at the top and two short hints. A test check enforces
this: every `[data-rdmode]`/`[data-rdsize]`/`[data-rdlang]`/`[data-rdfocus]` button in the
setup dialog must carry a `title` of at least 12 characters, so cutting the prose can never
leave a bare pill with the explanation nowhere.

### 19.2 The handoff buttons moved to the footer

Claude / ChatGPT / Gemini were a `Open in` **row in the body**, styled exactly like the Report
and Sample rows above them — so three *actions* were dressed as a fourth *setting*, in the
region of the dialog you had just finished configuring. They are now in the footer beside
`Copy all`, which is what they are: each one copies **and** opens a tab, so `Copy all` is the
same action minus the tab. Sitting side by side says that without the paragraph that used to.

`Download .txt` is the escape hatch for a composer that refuses a 74 KB paste, not part of the
normal path, so a `.rd-spacer` strands it on the **far right** with the size readout. The
delegated click handler is document-level and unchanged — the move is markup only, and the
synchronous copy-then-`window.open()` ordering from §18.3 still holds.

§18.3's reasoning that three gold buttons would leave the panel with no primary action is
**preserved, not reversed**: they are still `.rd-opt` chips and `Copy all` is still the only
`.rd-go`. What changed is where they sit.

### 19.3 Type sizes and a real phone layout

Options went 12px → 13px with padding to match, clearing a 34px hit target; the body note
12 → 13.5px in `--muted` rather than `--muted-2`; hints 11.5 → 12.5px; the digest textarea
11.5 → 12px. At ≤560px the label gutter is dropped (`flex-basis:100%` on `.rd-lbl`) so each
row breaks to a label line plus full-width options, the modal takes the full viewport less
8px, the textarea drops to 190px so the footer stays on screen, and footer buttons go
full-width with the spacer collapsed so `Download .txt` is not stranded alone on a line.

### 19.4 Verification

`test_review_digest.mjs` goes from 110 to **115 checks, all passing**. The two assertions that
read the old body hint were rewritten to read where the copy actually lives now: the handoff
buttons are asserted **in `#rdFoot`**, the paste shortcut on the body one-liner *and* on every
handoff button's tooltip, the oversized-paste route on `Download .txt`'s own title, and the
no-untitled-option rule above. Both states were also rendered in Chromium at 1280×900 and
390×800 and read back as screenshots.

---

## 20. Shipped 2026-09-02 — which game is this a report about?

Prompt is **v8** / **v8-simple**. Two changes, both about the first line of the output.

### 20.1 The report names its game

The digest never told the reader which game he was reading about. The title appeared exactly
once, on the OVERVIEW's `GAME:` line, roughly 230 lines of instructions into the paste — near
enough for the model, useless for a human, and far enough from the report's own first line
that all three models opened on a verdict with no game attached to it. That is fine for one
game and unusable for four, which is the normal way this feature gets used: run several
candidates in one sitting, end up with four reports in one conversation, and be unable to tell
them apart without scrolling back to the paste.

**Bundle side.** `GAME:` and `REVIEWS:` now sit directly under the `=== QTPD REVIEW DIGEST ===`
title line, above the instructions — the name, the appid, the sample size and the date span,
before anything else. The OVERVIEW copy stays: repeating it next to the data costs ~15 tokens
and keeps the anchor close to the reviews it describes.

**Prompt side.** Both files open on the rule and both skeletons open on the output:

```
# <game title, copied character for character from the GAME: line>
*Steam reviews <first date> to <last date> · <N> reviews sampled · appid <N>*
```

The verbatim wording is deliberate, and it is stated twice (prose rule and skeleton) because a
model that "tidies" a title is the whole failure mode: a trimmed subtitle, a translated name, a
franchise name in place of the edition's, or a title written from memory rather than copied all
produce a heading that looks right and identifies the wrong product. The second line is what
separates two runs over the *same* game at different sample sizes, so it is not decoration
either. Nothing editorial is allowed in either line — a verdict there defeats the one job they
have, which is being scannable.

### 20.2 INTEGRITY became a footer

`INTEGRITY: read N of N reviews · denominator N substantive · OK` was the first line of every
advanced report. It is a service announcement about the report, not a finding about the game,
and it was occupying the position the reader's eye lands on first. It now sits last, under a
`---` rule, below the Issues table — reachable by anyone who wants the receipt, in nobody's way.

The one exception is the failure case, and it is why this is not a pure move: counting the
model cannot trust still puts INTEGRITY at the **top**, directly under the title, where it
stops the report rather than footnoting it. A failure notice below 200 lines of tables it is
warning you not to trust would be worse than no notice at all.

The Simplified report had no integrity line to move — it forbids one outright — so it gets the
same idea at its own scale: one italic line at the very bottom, `*Read <N> of <N> reviews · <N>
of them substantive.*`, and the title block above carries the date range and size. Same shape
in both modes, nothing added to the middle of the short report.

### 20.3 Verification

`test_review_digest.mjs` goes from 115 to **123 checks, all passing**. The new assertions are
positional rather than textual, because that is what actually broke: `GAME:` and `REVIEWS:`
asserted by label *and* by sitting above `--- INSTRUCTIONS ---`; the advanced skeleton's title
above `### Snapshot` and its INTEGRITY line below `### Issues`; the simple skeleton's title
above `### Summary` and its footer below `### Best and worst`; and the "copied character for
character" rule asserted in both prompts, since dropping that phrase is how the guarantee
quietly becomes a suggestion. Both inline `RD_PROMPT_FALLBACK` entries were updated in the same
pass — §8's stale-fallback failure mode, avoided the same way §16 and §17 avoided it.

---

## 21. Shipped 2026-09-02 — how deep we can actually go

The question that started this: how many reviews can we pull, and how many can an AI actually
take? Both halves were answered by measurement against live Steam, not by extrapolating from
the Phase 0 fixture.

### 21.1 Steam has no ceiling — and the fetch was stopping short of it anyway

§14 Q2 called cursor depth "no constraint whatsoever", but it only ever tested 600. Walked
properly, `filter=recent` goes **120 pages / 12,000 reviews** on both Cyberpunk 2077 and
Valheim with the cursor advancing every time and **zero duplicates**. Nothing pushed back.
There is no Steam-side limit anywhere near the range this feature cares about.

What there *is*, is a gap. **Steam serves 98 or 99 reviews on roughly 2% of pages** — a review
deleted between the cursor being issued and the page being served — and then carries on
normally. Both fetchers treated a short page as end-of-list:

| site | was | consequence |
|---|---|---|
| `rdCollect` (`index.html`) | `batch.length < RD.perPage` → `break` | at 2% a page, **about one ten-page pull in five ended early**, silently — the header honestly reported the short sample it was handed, so the only symptom was a digest covering less time than it should have |
| `scrape_game` (`playtime_refresh.py`) | `len(reviews) < PER_PAGE` → `exhausted = True` | far worse: `exhausted` is **persisted** and gates `_eligible`, so one unlucky page **permanently retired a game from growing** while it still held fewer than target |

Both now stop only on an empty page or a cursor that stops advancing, which is what Steam's
actual end-of-list looks like.

### 21.2 A review count is not a budget

Measured live across five games, formatting each review exactly as the REVIEWS block does:

| game | chars/review | 500 | 1000 | 2000 | 3000 |
|---|---:|---:|---:|---:|---:|
| Dota 2 | 57–75 | 6.9k tok | 15.9k | 34.5k | 53.6k |
| Stardew Valley | ~107 | 13.7k | 26.8k | 54.8k | 81.4k |
| Black Myth Wukong | ~147 | 18.4k | 37.5k | 73.1k | 110.4k |
| Cyberpunk 2077 | ~156 | 19.8k | 39.8k | 78.5k | 115.8k |
| Valheim | ~165–185 | 23.4k | 42.8k | 81.9k | 120.9k |

**A review costs 15 tokens on Dota 2 and 47 on Valheim — a 3× spread.** §17.2's "1000 is
~29.8k tokens" came from one fixture and understates the text-heavy end by ~40%. So the number
in the size selector was never a budget, and `RD.charBudget` (300 KB of review lines, ~82k
tokens once the ~18 KB of instructions and header are added) is the ceiling that makes the
selector safe on any game. It only binds at 2000 on a text-heavy game; at 1000 and below
nothing reaches it. The trim cuts from the **old** end, so the sample stays a clean "most
recent N" — the one property every window, trend and topic count is computed against — and
the header states it, like every other thing done to the sample.

### 21.3 2000 is the ceiling, and it is an AI limit

At ~47 tokens/review, 2000 is ~92k tokens with the prompt; 3000 would be ~126k, which is more
of a 200k window spent holding the list than reasoning over it — and the binding constraint
was never context anyway, it is the model's counting, which §16 exists to prop up. Fetch time
is not a factor: the Worker answers in **0.42 s/page** measured, so 2000 is ~13 s.

The case for going deeper at all is coverage. At 500 reviews the sample reaches back
**3.4 days on Cyberpunk 2077** and 4.5 on Dota 2 — against an `RD_NOW_DAYS` of 90 and a
`RD_THIN_SAMPLE_DAYS` warning at 60. On a busy game the default cannot fill the window the
TIMELINE block was built to compare against; 2000 gets Cyberpunk to ~14 days and Valheim past
50. The argument sits on the 2000 pill's own tooltip, per §19.1.

### 21.4 Chat composers truncate a long paste instead of refusing it

Reported from use, and it is the failure mode that matters most: **Gemini cuts a 1000-review
digest partway through**, and because the bundle leads with INSTRUCTIONS and ends with the
reviews, the model reads a plausible-looking fragment and answers from it. Nothing in the
reply says it only saw part of the sample. §19.2's Download tooltip assumed a composer would
*refuse* text this long — that is the polite failure, and not the one that happens.

So the result panel now prices the paste. Under 60 KB, nothing (Dota 2 at 500 is 26 KB,
Stardew 52 KB). Over 60 KB — the range a 1000-review pull lands in, 60–163 KB — a gold warning
that composers truncate silently, and that **Download .txt** and attaching the file is the fix.
Over 150 KB it turns coral, says the paste will be cut, and the footer **inverts**: Download
.txt becomes the primary and leads the row, Copy all drops to a secondary. The three AI buttons
stay — they still open the right tab — but at that size their tooltip and their toast both say
to attach the file rather than paste, and the Copy toast stops congratulating.

Copy is never removed. It still works in a composer with a big enough input, and hiding it
would be a guess about the reader's AI.

### 21.5 Verification

`test_review_digest.mjs` gains a sixth scenario (a 2000-review pull with a 98-review page 3 —
asserting the walk runs well past it, the size cap is declared in the header, and the hard
warning names the download route and leads the footer) and a seventh (`rdLineCost` pricing,
the budget constant, the 2000 tooltip, and all three `rdPasteAdvice` tiers).

**Not run here**: `node` is still not installed on this machine. Instead every path was driven
through the **real page in a browser** with `fetch` stubbed at the Worker: a 2000-review pull
whose page 3 returned 98 reached 1698 reviews before the budget stopped it, trimmed 68 more to
1630, printed `size cap: 68 oldest reviews dropped`, rendered at 299 KB with the hard warning
and `rdDl` first in the footer; the same bundle re-rendered at 100 KB gave the gold warning
with Copy still primary, and at 40 KB no warning at all. `rdLineCost`, `RD.charBudget` and all
three `rdPasteAdvice` tiers were read straight off the live page.

**A note on where this landed.** The work was first built on `feat-review-digest-recency`,
which turned out to be fully merged and **1191 commits behind `main`** — and `main` had since
shipped §18–§20 (ascending sizes, per-pill tooltips, the three chat buttons, the slimmed
dialog). Committing there would have produced a PR reverting all of it. The branch was
fast-forwarded to `main` and the change re-applied on top, which is why the footer logic here
knows about `RD_AI` and the size argument lives in `RD_SIZE_TIP` rather than in body copy.

---

## 22. Shipped 2026-09-02 — the ceiling that undid the sizes, and a warning that was wrong

Both fixes here correct §21, which shipped hours earlier. Reported from use, on the same run:
**a 2000-review pull came back with 991 reviews and a banner saying the result was too long to
paste into Claude.** Neither half was right.

### 22.1 A budget that only ever bit the games it was there for

`RD.charBudget` (300 KB of review lines) trimmed the sample from the old end whenever the
chosen size cost more than that. §21.2 argued it "only ever binds at 2000 on a text-heavy
game" — which is correct, and is exactly the problem. A text-heavy busy game is the *only*
reason to pick 2000: §21.3's own case for the size is that 500 reviews reach back 3.4 days on
Cyberpunk 2077. The cap therefore fired precisely when the reader had asked for depth, silently
handing back well under half of it — and on a game where §21.3's Valheim/Cyberpunk arithmetic
says a 2000-review sample is 312–346 KB, i.e. every one of them.

A header line owning up to the trim does not fix that. The reader picked a number off a
segmented control; the number has to mean what it says.

**The budget is gone.** `charBudget`, `lineOverhead` and `rdLineCost` (which existed only to
price the budget) are deleted. The size the reader picks is the size fetched, and the panel
prices the *finished* bundle instead — reporting is the right place for a spread that runs 3×
between games, because the spread is a fact about the game, not a limit on the request.

Nothing downstream needed the ceiling. It was justified by a 200k context window, but the
route for a bundle that big is the .txt, and an attachment has no length limit anywhere.

**The walk is now bounded by the count, not the page arithmetic.** `pages` (size ÷ 100) is what
a *clean* run costs, and §21.1's short pages are 98–99: two of them in a 20-page pull ends four
reviews under the 2000 that was asked for. The loop runs while `raw.length < size` with five
spare pages of slack, so a fixture with one 98-page reaches 2000 on page 21 and a clean game
still stops at page 20.

### 22.2 "Too long to paste" was false in two of the three tabs it sat next to

§21.4 generalised one true observation — Gemini cuts a long paste — into "Gemini and most other
composers will cut this." **Claude and ChatGPT do not truncate an over-long paste; their
composers convert it into an attached file**, which is the same thing **Download .txt** does,
done automatically. So the panel told a Claude user the digest could not be pasted, next to a
Claude button, while the paste worked fine.

That is worse than saying nothing. A warning that is visibly wrong about the tool in front of
the reader is one they learn to dismiss — including on Gemini, where it is true and where the
failure is silent.

`RD_AI` gains a `paste` field (`"file"` / `"cut"`) and it is the single source of truth for
every sentence about size. `RD_AI_FILE` / `RD_AI_CUT` build the names into the copy, so the
warning, the ready line, both footer tooltips, the per-AI tooltips and both toasts now say
which composer does what instead of "most of them":

| surface | before | now |
|---|---|---|
| hard banner | "too long to paste… all three accept the file" | names Claude + ChatGPT as fine, Gemini as cut |
| Claude / ChatGPT button | "the paste will be cut, attach the .txt" | "attaches the paste as a file by itself, so it arrives whole" |
| Gemini button | same generic line | "Gemini cuts the paste partway through — attach the .txt there" |
| AI-button toast | error toast for all three | error only on a `"cut"` composer; the other two get the normal copy toast |
| Copy all toast | `err` — "most composers will cut this" | `ok` — names who takes it and who cuts it |

The thresholds (60 KB / 150 KB) and the footer inversion are unchanged: **Download .txt** still
takes the primary button above 150 KB, now framed as the route with no limit anywhere rather
than as a rescue from a paste that was never going to work.

### 22.3 Verification

Driven through the **real page in a browser** (`py -m http.server`, `window.fetch` stubbed at
the Worker), same fixture as §21.5 — ~140 chars/review, page 3 returns 98:

- 2000 requested → **21 pages fetched, 2000 reviews**, `SAMPLE: 2000 newest`,
  `--- REVIEWS (2000) ---`, **no `size cap:` line**, bundle 346 KB / ~88.6k tokens.
- Hard banner reads: *"346 KB — past what a chat box will hold as text. Claude and ChatGPT
  handle that for you and turn the paste into an attachment… Gemini instead cuts it partway
  through…"*
- Footer: `rdDl, rdCopy, claude, gpt, gemini` — file still primary. Claude/ChatGPT tooltips say
  *"attaches the paste as a file by itself, so it arrives whole"*; Gemini's says *"cuts the
  paste partway through, so attach the downloaded .txt there instead"*.
- No console errors on load or on the deep-pull path.

`test_review_digest.mjs` scenario 6 now asserts `got === 2000`, `pages === 21`, **no** size-cap
line, that the warning names Gemini *and* Claude + ChatGPT, that "too long to paste" is gone,
and the two shapes of AI tooltip. Scenario 7 asserts `RD.charBudget` is `undefined`, that every
`RD_AI` entry declares a `paste` behaviour, and that the name lists come out as
"Claude and ChatGPT" / "Gemini". **Not run here** — `node` is still not installed on this
machine.

### 22.4 The result panel — what it got, what it costs, and links that behave like links

Four notes from using the finished panel, all of them about the same screen.

**It never said how many reviews it got.** The reader picks a size, watches a progress line
count 21 pages, and then the panel changes to a wall of text with no number on it — the count
was only inside the bundle, five lines down in a mono block. The dialog header now carries it
next to the game's title (`2000 reviews`), read back out of the finished bundle's
`--- REVIEWS (n) ---` line rather than passed in, so it can never disagree with the block. It
is its own element beside the `<h3>`, not a suffix inside it: the h3 is `nowrap`/ellipsis, and
a long game title would have eaten the one part that must not disappear. It is also how a short
sample announces itself — 300 back from a request for 2000 means the game only has 300.

**The banner was three sentences where one would do.** §22.2's copy explained the mechanism —
that the cut is silent, that the reviews sit at the end so the fragment reads as complete — and
that is a paragraph, which is a thing readers skip. It is now one line:

> **346 KB.** Gemini can't take a paste this long — use **Download .txt** and upload the file
> instead. Claude and ChatGPT take it whole.

Who can't take it, what to do instead, who is unaffected. The mechanism survives in the
tooltips for anyone who wants it.

**The three handoff buttons are now `<a href>`.** They were `<button>`s calling
`window.open`, which is a link wearing the wrong element: ctrl-click, middle-click, "open in
new window" and the status-bar preview all do nothing on a button, and a reader who ctrl-clicks
by habit gets silence. As anchors with `target="_blank" rel="noopener"` the browser does all of
it, and the page is still never left. The click handler no longer opens anything — it only
copies — which also retires the popup-blocker tightrope §18.3 walked, where the clipboard write
had to be *started* before the open inside a single gesture. Middle-click fires no `click`
event at all, so a delegated `auxclick` listener runs the same copy; the browser opens the tab
itself either way.

**The token figure was unlabelled, and estimated at the wrong rate.** `length/4` is the generic
approximation; these bundles measure ~**3.9** chars/token, because a review line is short
English words behind a mono prefix of digits and symbols. `RD_CHARS_PER_TOKEN` is now that, and
the footer says what the number *is* — `346 KB · ~90.9k tokens in the AI` — with a tooltip
noting it is the same whether the text is pasted or the .txt is uploaded (an attachment is
read, not summarised) and that it is comfortable in a 200k-context model. The KB says whether
the paste will survive the composer; this says whether the model has room to read it.

**Verified in the real page** (same stubbed fixture): header reads `2000 reviews`; banner is
133 chars in one line; all three handoffs are `A|https://…|_blank`; footer reads
`346 KB · ~90.9k tokens in the AI`; a synthetic ctrl-click and a synthetic middle-click each
put all 354,342 characters on the clipboard with **`window.open` called zero times**; mobile
(375×812) keeps the count in the header and the anchors full-width. `test_review_digest.mjs`
scenario 6 gains checks for the header count, the labelled token meter, the anchor shape of all
three handoffs, and a hard cap on the banner's length.

## 23. Shipped 2026-09-03 — the denominator was wrong, and the report is now also a page

Two requests off the same run — the Blood of Dawnwalker digest that produced
`bloodofdawnwalkerreviewdigest.html` by hand, afterwards, because the Markdown report was not
the artefact wanted. Both are options in the setup dialog; neither changes what a digest with
the options off produces.

### 23.1 "500 reviews complain about the story" was 30% of the game and 50% of the sentences

`RD.substantive` (20 characters) has been **measured and never acted on** since §6, on the
argument that dropping one-liners biases the sample negative — they skew positive, so removing
them lowers the reported score. That argument is sound about the **sentiment split** and wrong
about everything else in the report, and everything else is most of the report.

The live case: 1,651 reviews sampled, ~1,000 of them carrying an actual sentence. Every
percentage in the digest is a share of the substantive count, and the substantive count is
itself inflated by "gg", "10/10" and a thumbs-up emoji. A story complaint raised by 500
reviewers reads as **~30% of the game** when, among everyone who said anything at all, it is
**~50%**. The reader draws the wrong conclusion from an arithmetically correct number.

**The quality bar** is a word-count floor applied during compaction, alongside the ASCII-art
and copypasta filters that already delete what would poison the counting. `Off · 3+ · 5+ · 10+`
words, default **5**: the shortest review that can name a thing and say what is wrong with it
("combat feels stiff and slow"). 3 keeps two-clause verdicts; 10 keeps only reviews arguing a
case, and on a quiet game will hit the fetch ceiling before it fills the sample.

Words, not characters, and CJK counts per character: whitespace splitting makes a whole
Japanese review one "word", and at the "All languages" setting the bar would then delete every
one of them. `rdWords` counts each CJK/kana/Hangul character as its own unit — the usual
approximation, and it errs toward keeping a review, which is the right way to err for a filter
that removes data.

**The positive-skew bias is handled by reporting it, not by pretending it is absent.** Two new
OVERVIEW lines when the bar is on: what it dropped, and the **pre-bar split** beside the
post-bar one, labelled — *"the gap is the filter, not the game: quote the sample split, never
this line."* TIMELINE gains a matching `BASIS` line, because every rate in that block is now
measured over the qualitative reviews and a model reading it against the ALL-TIME anchor
without knowing that would report the filter as a slump. That is the one way this feature could
manufacture a wrong finding, and it is the line that stops it.

### 23.2 HTML output — the same report, rendered as a page

`Output: Markdown | HTML page`, a **separate axis from Simplified/Advanced**: the depth decides
what is counted, the output decides how it is drawn, and all four combinations are legitimate.
HTML **replaces** the Markdown skeleton rather than adding to it — a model asked for both
writes the report twice, and the second copy is where the numbers drift.

It ships as an **addendum** (`review_prompt_html.md`), appended after the depth prompt and after
the READER FOCUS block, under a header that says what it replaces. Not a third prompt file: the
counting rules, buckets, floor and focus contract are identical in HTML, and a fork of
`review_prompt.md` differing only in its last forty lines would be two files to keep in step
and one of them would rot.

**The stylesheet ships inside the addendum, verbatim and closed to edits** — it is the one from
the hand-made Dawnwalker page: light/dark via `prefers-color-scheme`, a serif masthead, the
hero split bar, `.scroll` tables, the `.split`/`.minibar` cell for Quit / stayed, and a print
block. That is the whole point of the option. A report per game is only comparable if ten of
them look like ten pages of one publication, and a model left to style it itself picks a new
palette every run. The addendum also forbids what would break the file: no external CSS, fonts,
images or scripts, no charts, no interactivity.

**`html-v2` — a file, not a fence.** v1 asked for the document inside a single ` ```html `
fence, and every model obliged: the reader got a wall of markup in a chat window and a
save-as-`.html` to do by hand, which is the work this option exists to remove. v2 asked for the
page as a **file** instead and forbade the fence, naming each chat's mechanism — an Artifact on
Claude, a written `<game>.html` with a download link on ChatGPT, a Canvas file on Gemini.

**`html-v3` — a DOWNLOAD, not a panel.** v2 was read as "a thing that *looks* like a file":
Claude answered with an Artifact, which renders beautifully and hides the download inside its ⋮
menu. Tested on *STAR WARS Zero Company* that is the same manual work as the fence, one menu
further away — and a reader keeping a folder of these side by side has to do it every time. v3
therefore asks for the one artefact all three chats can actually produce: **a downloadable
`.html`, named after the game** (`star-wars-zero-company.html`), written with the file/code tool
and attached. It forbids the preview panes **by name** — no Artifact, no Canvas, no document
view — because a rule stated only in the positive gets satisfied by the nearest thing to hand.
The test is stated in the reader's terms: *a `.html` in their downloads, having copied nothing
and clicked nothing but Save.*

**`html-v4` — no fabricated handovers.** The escape left a hole. Told to *"give the download
link"*, Gemini Flash wrote `[Download star-wars-zero-company.html](sandbox:/star-wars-zero-company.html)`
— **ChatGPT's** download scheme, imitated by a model with no file backend — and Gemini's UI
resolved it to a *Google search for the string*. That is the worst failure this feature can
produce: v1 at least handed over a code block with a working download icon, whereas this looks
like success and isn't. The addendum now bans the fabricated handover outright: **no link to a
file you did not create with a tool, and no `sandbox:` URL unless that scheme is genuinely
yours**, with the honest alternative spelled out — *"I can't attach files here, so the document
is in the block below, use its download icon"* beats a confident link to nothing. The
before-you-hand-it-over checklist gains the matching line.

Naming all three chats is deliberate — the bundle is built before the reader picks one, so every
line rides along and each model reads its own. **No fenced fallback is offered**, with one
measured exception: **Gemini Flash**, tested on the same game, ignores the file instruction and
answers with a code block. Gemini's blocks carry a download icon, so the addendum lets *Gemini
alone* fall back to one block plus *"click the download icon"* — which converts that failure
into a one-click save rather than a copy-paste. The escape names Gemini so no other model can
read itself into it; everywhere else a get-out clause in the sentence is the clause a model
takes.

It costs ~3k tokens of instructions and is fetched only when selected, so a Markdown run pays
nothing for it. Both version markers ride the title line — `prompt v9 + html-v4` — because the
output is now the product of two files and `v9` alone would not identify it.

### 23.3 The size now counts reviews that LAND, not reviews fetched

The walk counted raw reviews and compacted afterwards, so a 500 that lost eight to duplicates
and art delivered 492. At 2% that was pedantry. At the bar's ~40% it is not: 500 with the bar
on would have meant ~300, and an option that improves the denominator by halving the sample is
a bad trade the reader never agreed to.

So **compaction moved into the fetch loop** — `rdCompact` became `rdCompactor`, an incremental
object holding the dedupe set and the counters across pages — and the loop runs until `size`
reviews have been **kept**, stopping mid-page the moment it gets there. With the bar off this
finally makes 2000 mean 2000 after the dupes come out; §22.1's argument ("the number has to
mean what it says") always implied it and the page arithmetic never delivered it.

The ceiling: five spare pages is right for a 2% drop rate and useless at 40%, so with the bar
on the page cap becomes `RD.noiseFetchMax` (3) × the request — well past the worst measured
ratio (~1.8x at the 5-word bar) and still bounding a pathological game at 60 pages for a 2000
pull. Hitting it is not an error; the header reports the short sample as it always has. The
progress line reads `page 7 · 412/500 reviews · 689 read`, so the over-fetch is visible while
it happens rather than explained afterwards.

An emptied sample is its own failure and says so: *"all 40 reviews in that language are under
the 5-word bar — lower it or turn it off"* is a different problem from a game with no reviews,
and the old message named the wrong one.

### 23.4 The two splits, side by side

Reported from the first run of §23.1: the disclosure was *correct* and still made the reader
do the work. `sample split` and `before the bar` sat several lines apart in different shapes,
one counting `up / down` and the other `▲/▼`, and the reader was left to subtract two
percentages to find out whether a lower rate was the filter or the game.

They are now consecutive, identically shaped, and followed by the difference itself:

```
  sample split: 225 up / 75 down (75% positive) — the 300 reviews in this bundle, after the bar
  before the bar: 425 up / 75 down (85% positive) — the 500 reviews read to fill it
  the difference: 200 removed, 200 up / 0 down — 200 under the 5-word bar
  quality bar: ON at 5 words. Short reviews skew positive, which is why those two splits differ
  by 10 points — that gap is the filter, not the game. Quote the sample split; the
  before-the-bar line is context, not a figure to report.
```

The removed reviews' own split (`200 up / 0 down`) is the line that makes the point without
argument: the bar took 200 positives and no negatives, which is *why* the rate moved, stated
as data rather than as a warning to be believed. Where nothing fell under the bar, all of this
collapses to one line saying so — an elaborate disclosure of a filter that removed nothing is
just noise.

The same pairing runs in the dialog header, where the human looks: **`300 reviews · 200
filtered out of 500`**. "300 reviews" alone invites the reader to compare it against the 500
they asked for and conclude the fetch fell short.

### 23.5 The counter had to stop being a review count

Also reported from that first run. The fetch progress was the running review count, which on a
clean pull climbs 100 · 200 · 300 and reads as progress on its own. With a bar in front of it
the same counter climbs 63 · 141 · 197 — no round numbers, no sense of how far along it is, and
a page that drops most of its reviews looks like a stall.

The goal is known before the first request in every configuration, so **the counter is a
percentage of it**, with a fill bar under it and the raw numbers demoted to a second line that
explains rather than competes:

```
  40%
  ▓▓▓▓▓▓▓▓░░░░░░░░░░░░
  page 3 · 300 read · 120 of 300 kept
```

It behaves identically whether the bar is off, at 3 or at 10 — which is the point; the reader
should not have to know the drop rate to read a progress indicator. Two details are
load-bearing: the percentage is rounded **down**, so 299 of 300 never shows "100%" while the
fetch carries on; and it ticks both before and after each page, so the page that just landed
moves the bar rather than the next one appearing to.

### 23.6 Verified


`test_review_digest.mjs` scenario 8 runs one fixture three times — bar at 5, bar off, and HTML
output — because the failure that matters is a cross one. The fixture is **40% one-liners by
construction** ("gg", "10/10", "cool", "👍"), which is the measured shape of a real sample, and
they are all ▲ so the positive skew is real rather than asserted. It also carries one
14-character Japanese review with no spaces in it: a whitespace split scores that as one word
and deletes it, so it is the check that the CJK path is not just a comment.

Asserted: a 300 sample comes back as **300** with the bar on and takes five pages instead of
three to do it (§23.3); the `quality bar:` and `before the bar:` lines print, with the pre-bar
rate above the post-bar one (80% vs 67%) and labelled as the one not to quote; `BASIS` appears
in TIMELINE only when the basis changed; the bar-off run's OVERVIEW says nothing about a filter
that did not run — scoped to the OVERVIEW, because both prompt files now *talk* about the bar
and a whole-bundle match would pass on the instructions and test nothing; `rdWords` counts
"10/10" as one word, punctuation as none, and CJK per character. For HTML: the addendum arrives
under its replacing header, positioned after the depth prompt and before the data, carrying the
stylesheet and the minibar recipe and the ban on emitting both renderings, with both version
markers on the title line — and the Markdown run carries **none** of it, which is the whole
point of an option.

Scenario 1 also now asserts the two new dialog rows and their defaults (Markdown, and the bar
ON at 5), that both carry tooltips like every other option in that dialog, and that the two
splits and the difference sit on four consecutive lines in the shapes §23.4 fixes.

The counter (§23.5) is asserted **while it runs** — a MutationObserver installed before the
fetch collects every value the reader would have seen, because a check on the final state
alone would pass on a counter that sat at 0% and jumped to 100%, which is the exact failure
the percentage was introduced to fix. On the 40%-noise fixture it records `0 20 20 40 40 60 60
80 80 100 100`: monotonic, in range, starting at 0, ending at 100, with the fill width tracking
the number and intermediate values actually present. 201 checks pass.

### 23.7 `html-v5` — the addendum contradicted itself, and Gemini read it correctly

Reported from a Flash run on *STAR WARS Zero Company*: the reply was one `html` code block. The
first reading is that Gemini ignored the instruction again. It did not. It picked the only rung
the file was not of two minds about.

v4 said Canvas four times and meant two different things:

| where | what it said |
|---|---|
| opening rule | "not a preview pane — not an Artifact, **not a Canvas**, not a document view" |
| Gemini's bullet | "or put it in **Canvas** as an HTML file" |
| the honest-failure paragraph | "fall back to what you do have: **Canvas**, or the one code block" |
| hand-over checklist | "not an Artifact, **not a Canvas**, not a code block" |

Three against, one for, and the two againsts are the emphatic ones — a bolded ban at the top and
a checklist line at the bottom, the two places a model re-reads. Take the bans and Gemini's
bullet reduces to *"write it to a file the reader can download"*, which Gemini cannot do: there
is no attach-a-file affordance in that chat. Both stated paths gone, the escape is what is left.
And the escape was gated on **"only if you genuinely have neither"** — a condition about the
model's own tool inventory, which no model can check and every model resolves in favour of the
concrete option. The escape was also the *specific* half of the bullet: it named a mechanism and
supplied the sentence to say, against two abstractions. Specificity wins that contest every time.
§23.2 wrote down the reason this would happen — *"a get-out clause in the sentence is the clause
a model takes"* — and then put one in the sentence.

**v5 makes the ladder decidable rather than tightening the ban.** Gemini is carved out of the
opening rule by name, once, with the reason stated (the bans are written for the chats that have
a file tool). Its bullet becomes an ordered ladder with a condition a model can actually
evaluate — *stop at the first rung that opens*:

1. **Canvas**, titled `<game>.html` — the canvas title *is* the filename Canvas's own Download
   hands over, which is the whole reason the naming rule survives on this path.
2. **One `html` code block**, only if Canvas will not open, with the download-icon sentence.

"Only if you genuinely have neither" is deleted. The checklist line is rewritten to accept the
highest rung that opened instead of failing Gemini's own sanctioned answer, and the
honest-failure paragraph now points *down* the ladder rather than listing Canvas as a last
resort under the block.

**The block stays as rung 2 on purpose.** The tempting fix is to delete it and let Canvas be the
only answer — but that is what v3 did to the fence, and v4 exists because a model with nowhere
legitimate to go fabricated a `sandbox:` link instead of admitting it could not attach a file. A
stated, honest fallback is the thing that keeps the dishonest one away; what v4 got wrong was
not offering one, it was making it the easiest rung to reach.

The lesson generalises past Gemini: **a per-model exception has to be granted in the same breath
as the rule it excepts.** Stating a blanket ban and then quietly contradicting it a paragraph
later does not produce a model that follows the exception. It produces one that follows the ban
and falls to whatever is left over — and the leftover was the outcome the option exists to
prevent.

### 23.8 `html-v6` — the ladder held, the *other* chat's bullet did not

Same game, same model, one version later: *STAR WARS Zero Company* through Gemini Flash, and the
reply was a **Python code block** — `import os`, `html_content = """<!DOCTYPE html>…"""`, a write
at the bottom. Not Canvas, not the `html` block. Both rungs of the v5 ladder skipped for a rung
that does not exist.

**v5 fixed the contradiction and left the imitation.** §23.7 made Gemini's own bullet decidable,
and it worked in the sense that mattered — nothing in that bullet is ambiguous any more. But the
bullet directly above it says:

> **ChatGPT** — use the **python tool**: write `<game>.html` to disk and give the download link.

That is the only line in the addendum that describes, concretely, a real file being written by a
real mechanism. Everything addressed to Gemini describes what a chat window can *display*. A
model reaching for the most specific instruction on the page — the same pull §23.2 named, and the
same pull that produced the `sandbox:` link in §23.4 — reaches for that one. The `sandbox:`
failure was Gemini imitating ChatGPT's download *scheme*; this is Gemini imitating ChatGPT's
*tool*. One mechanism, named once, belonging to one chat, taken by another. Third time.

**And this failure is worse than the code block v5 sanctioned.** The download icon on a `python`
block saves a `.py`. The reader clicks the one affordance the block offers and lands a script
they have no interpreter for, with the actual document sealed inside a triple-quoted string —
strictly further from a page in a folder than v1's bare fence, which at least saved as markup.

**v6 states the rule the first five versions only implied.** It goes above the per-chat bullets,
where nothing can read itself out of it: *hand over the document, never a program that writes
it.* A chat with a code tool **runs** it and attaches what it produced; it never prints the code.
A chat without one emits the HTML itself. `html_content = """…"""` is named as a wrong answer
explicitly, because that is the exact shape that arrived, and it is wrong even when the HTML
inside the string is perfect.

Three smaller edits carry it:

| where | v5 | v6 |
|---|---|---|
| ChatGPT bullet | "use the **python tool**" | "…which is ChatGPT's alone — no other chat may reach for it, or imitate it by printing a Python block", and *run* it, the reply carrying the link and never the script |
| Gemini bullet | "do not imitate another chat's" | names the thing not to imitate: do not reach for ChatGPT's python tool, do not print a Python block — *you cannot run it* |
| rung 2 | "One `html` code block" | pinned to what the block **contains**: tagged `html`, opening `<!DOCTYPE html>`, closing `</html>`; a `python` block is not this rung and does not satisfy it |

Plus a closing line under the ladder — **there is no rung 3** — because every failure in this
series has been a model inventing one, and a checklist item asserting the reply is HTML rather
than a program.

**The lesson, which is now a pattern and not an anecdote:** *naming a per-chat mechanism arms
every other chat with it.* A bundle built before the reader picks a model carries all the bullets
to all of them, so any capability mentioned anywhere is a capability every model has read about
and none can verify it lacks. §23.7's lesson was that an exception must be granted beside the
rule it excepts; v6's is the mirror image — **a grant must be fenced in the same breath it is
made**, and the fence has to say what the right answer *looks like*, not merely what it is not.
Pinning rung 2 to `<!DOCTYPE html>` is the same move that made Canvas hold in v5: a shape a model
can check its own output against beats a prohibition it has to reason its way into.

**Verified.** Seven assertions were added to `test_review_digest.mjs` alongside the v5 set — the
document-not-a-program rule is present and names `html_content`; it sits *above* the first
mention of the python tool in the assembled bundle; the tool is fenced to ChatGPT; Gemini's
bullet names the imitation; rung 2 is pinned to `<!DOCTYPE html>`; the ladder closes at rung 2;
and the hand-over checklist asserts HTML rather than a script. The suite now stands at 208
checks, all passing.

### 23.9 `html-v7` — the ask was phrased as a mechanism Gemini does not have

Same game, same model, one version later: *STAR WARS Zero Company* through Gemini Flash, and the
reply was **rung 2** — one `html` code block, `<!DOCTYPE html>` to `</html>`, the download icon
sitting on it. **That is v6 working.** No Python block, no `html_content = """…"""`, no
`sandbox:` link to a file that was never written: every invented rung this series has produced
stayed shut. What did not happen is **rung 1**. Canvas was never tried.

A fallback taken while the rung above it was open is a different failure from an invented rung,
and it is a quieter one — the reader still gets a saveable file, just named after nothing. It is
also, again, not disobedience. Two lines in the addendum were talking the model down the ladder
before it ever reached one.

**The ask was phrased as a mechanism, not as an outcome.** The section opened: *"Write the page
to a real file and attach it for download."* Attaching a file to a reply is precisely what
Gemini's chat does not do, so the one sentence carrying the whole point of the option reads, to
the one model that most needs to hear it, as addressed to somebody else. Everything after it is
exceptions. A model that has filed the section under *not for me* arrives at its own bullet
looking for the least-wrong thing it can do rather than the best thing it can do.

**And its own bullet then argued it out of its tooling.** v5 opened Gemini's bullet with the
carve-out — *"you have **no** way to attach a file to a reply"* — written to stop it faking one
(§23.4), which it did. But it is a capability denial one line above a capability instruction, and
the instruction under it (open Canvas, title it `<game>.html`, hand over what its Download saves)
is a version of the very thing the denial calls impossible. Told first what it cannot do and
second to do a form of it, the model took the rung that needs no tool at all.

**v7 restates the ask as the outcome — in the reader's words, which turn out to be Gemini's
own.** Asked directly how to get a file out of it, Gemini answers with phrasings, not
mechanisms: *"create and provide a downloadable HTML file"*, *"generate an `index.html` file I
can download"*, *"output this as a file deliverable"*. A model's self-report about its own
triggers is not evidence, and the mechanism it volunteers for *why* they work — "it triggers me
to run a code script in the background" — is the exact shape §23.8 banned, so that half is
ignored. The phrasing half costs nothing and names the outcome instead of the plumbing, which is
independently the fix this failure calls for. So the section now opens **Create and provide a
downloadable HTML file** and states the test in one line — *did a `.html` land in the reader's
downloads folder?* — and Gemini's bullet leads with the same sentence, naming the file.

| where | v6 | v7 |
|---|---|---|
| section opening | "Write the page to a real file and attach it for download" | "**Create and provide a downloadable HTML file**", plus the test restated as the reader's downloads folder rather than as your plumbing |
| the carve-out | "One chat is exempt: Gemini. Gemini cannot attach a file to a reply at all" | "One chat reads the bans differently: Gemini" — the exemption kept, the capability denial gone |
| Gemini bullet | "you have **no** way to attach a file to a reply" | "**create and provide a downloadable HTML file, `<game>.html`, and output it as the deliverable**", then the unchanged ladder, with *try rung 1 before concluding it is shut* |

Rung 1 is relabelled with it, from a consolation prize into the answer: Canvas is not a preview
of the reply, its **Download** *is* the handover and the canvas title *is* the filename — so
putting the page in Canvas **is** creating a downloadable HTML file, not a lesser substitute for
one. **The ladder and the bans are otherwise untouched.** §23.4 exists because taking the honest
fallback away produced a dishonest one, so rung 2 stays exactly where it is, which bounds the
risk: the worst case for v7 is the v6 answer — the same block, the same download icon.

**A second failure in the same reply, and nothing to do with delivery.** The page came back with
the stylesheet **minified onto single lines** and titled *"STAR WARS Zero Company™ – Steam Review
Analysis"* instead of the skeleton's *"— Steam review digest"*. Both render identically and both
defeat the reason the option ships a closed stylesheet at all: ten games are meant to produce ten
pages of one publication, and that fails at the browser tab as surely as it fails at the palette.
So "copy the stylesheet verbatim" now says in words that verbatim includes the whitespace —
minifying it is an edit — and the exact `<title>` string is a hard rule of its own as well as a
line on the hand-over checklist.

**The lesson.** §23.7: an exception must be granted beside the rule it excepts. §23.8: a grant
must be fenced in the same breath it is made. v7's is the one underneath both — **state the ask
as the outcome the reader can check, never as the mechanism you imagine producing it.** A
mechanism named in a bundle that three chats will read is wrong for at least one of them, and the
chat it is wrong for reads the entire section as somebody else's.

**Verified.** Two v3/v5 assertions in `test_review_digest.mjs` were retargeted at the reworded
lines, and seven added: the ask is phrased as the outcome; it leads the section above the
per-chat bullets; the capability denial is gone; Gemini's bullet leads with the same ask and
names the file; the ladder says to *try* rung 1; verbatim is spelled out to include the
whitespace; and the exact `<title>` string is stated as a rule of its own. The suite now stands
at 215 checks, all passing.

### 23.10 `html-v8` — the reply has to carry the document

v7 made it worse, and it is the only version in this series that did. Same game, same model,
one version later, and the entire reply was this:

```
<a_file_has_been_created_or_edited_view_it_in_the_drawer>
star-wars-zero-company.html
</a_file_has_been_created_or_edited_view_it_in_the_drawer>
```

followed by *"I have analyzed the provided Steam reviews for **STAR WARS Zero Company™** and
compiled the digest into the requested standalone HTML document `star-wars-zero-company.html`."*

**No Canvas. No block. No file.** The drawer was empty; the document existed nowhere. Every
earlier failure in this series at least shipped the page somewhere — a fence, an Artifact, a
`.py` with the HTML sealed in a string — and the argument each time was about how much manual
work the reader had left to do. v8 exists because v7's answer left the reader with **nothing**,
and a confident sentence saying otherwise.

**It is §23.4 for the third time.** A model with no file backend imitates the most concrete
file-shaped thing it has read. §23.4: it imitated ChatGPT's `sandbox:` download *scheme*. §23.8:
it imitated ChatGPT's python *tool*. Here it imitated **the interface itself** — and that is the
most convincing fake available, because a UI marker does not read as a claim the model is making.
It reads as the app reporting a fact. (Whether Flash hallucinated the string or reached for
Canvas and got the tool-result text without the tool running, the reader's outcome is identical
and so is the fix.)

**And v7 invited it.** §23.9 restated the ask as *"create and provide a downloadable HTML file …
output it as the deliverable"* to stop the model reading the section as somebody else's. It
worked, in the sense that Flash stopped treating the instruction as not-for-it — and then
performed the file it had been told to provide, because performing one was the only way it had
to comply. Naming the outcome fixed the addressing problem and armed a new failure: **an
outcome, stated hard enough, will be simulated by a model that cannot produce it.**

**So v8 states the thing six versions never said.** Every earlier rule ranks hand-overs against
each other — Canvas over a block, a file over a pane, a document over a program — and every one
of them is checkable only against the model's beliefs about its own tooling, which is exactly
what has been wrong each time. The new rule is checkable against the artefact:

> **The reply has to carry the document.** Three things count — a Canvas holding the page, a file
> you genuinely attached, one `html` block from `<!DOCTYPE html>` to `</html>`. A sentence saying
> a file was created is not one of them.

It sits above the per-chat bullets and above the ladder, where nothing can read itself out of it
(§23.8's placement lesson), and it names the failure that arrived (§23.8's naming lesson): the
fabricated marker is quoted in full, alongside "view it in the drawer" and "I've saved it to your
files", with the reason — those messages belong to the interface and only the interface can
produce them. One more line converts the failure into a rung condition rather than a judgement
call: **if you reached for a document tool and got a marker naming a file instead of a document
you can see, that tool did not run** — so rung 2 catches it, and the hand-over checklist now
opens with the invariant instead of with the table rules.

**What is deliberately not changed:** v7's outcome vocabulary stays. The addressing problem it
solved was real, and the fix for a simulated outcome is a check the simulation fails, not a
retreat to wording that read as addressed to another chat. The ladder, the bans and the two
fidelity rules are untouched.

**The lesson.** §23.7: an exception must be granted beside the rule it excepts. §23.8: a grant
must be fenced in the same breath it is made. §23.9: state the ask as the outcome, not the
mechanism. v8's is the floor under all three — **every rule about the hand-over must be checkable
against the reply itself, because a model's belief about its own tooling is the one thing that
has been unreliable in every failure here.** "Use Canvas" cannot be verified by a model that
believes it did. "Is the document in what I am about to send" can.

**Verified.** Seven assertions added to `test_review_digest.mjs`: the invariant is present; it
sits above the per-chat bullets; the fabricated marker is named; imitating the interface is
banned in words; a marker in place of a document is defined as the rung failing to open; rung 2
names that case; and the checklist opens with the invariant. The suite now stands at 222 checks.

### 23.11 The page saves the file, because eight versions could not make the chat do it

§23.2 through §23.10 are one long argument with language models about a hand-over, and the score
after eight versions is: two chats comply, the third finds a new way not to. An Artifact instead
of a file. A `sandbox:` link to nothing. A Python script with the page sealed in a string. The
interface's own *"a file has been created"* marker written out over an empty drawer. Each fix was
a better sentence; each better sentence was obeyed by the models that already complied and
reinterpreted by the one that did not — and after all of it the reader was still selecting text
out of a code block by hand.

**The mistake was the location, not the wording.** Look at what the reader actually wants:
`<game>.html`, in their downloads, named after the game. Now look at what that needs — the
document, and the title. The document is the one part only the model can produce. **The title
this page has known all along**, and the document arrives on the reader's clipboard whichever
rung the chat lands on. Nothing in the naming or the saving requires model tooling at all. It was
handed to the model because the model happened to be holding the document, and that is the only
reason.

So the file is written **here**: a Blob and an `<a download>`, in the dialog the digest came out
of. Deterministic, testable, and identical on every chat.

**What arrives is a reply, not a file**, so `rdExtractHtml` reduces one to the other: it takes the
fenced block that contains a document (the longest, when a reply carries more than one), slices
`<!DOCTYPE html>` to the last `</html>` out of whatever prose surrounds it, and hands back the
page. **Paste the whole reply** is therefore the instruction — chatter, fence and all — because
selecting the document by hand is precisely the work being removed. It rescues §23.8 for free: a
page sealed inside `html_content = """…"""` is still a page, and it comes out.

Two inputs get named rather than refused generically, because both are things a chat did to this
reader and neither is his mistake:

| what the chat sent | what the saver says |
|---|---|
| the §23.10 marker, no document anywhere | *"That reply only ANNOUNCED a file — there's no page in it. Ask the chat to paste the document itself into one html code block."* |
| a script that would write the page, with no page in it | *"That's a script that would write the page, not the page. Ask for the document itself."* |

It sits in the result view **and** in the setup view: a reader who closed the dialog while waiting
on a slow reply should not have to pay for another twenty-second fetch to reach it. A `<details>`
rather than a panel, so on the run where the chat behaved it is one line of text nobody opens.

**And the prompt gets to relax.** `html-v9` tells the model what is now true: *the reader's own
page does the filing, so you do not have to* — the naming is handled, the saving is handled, and
**a document in one `html` block is a complete answer**, not a consolation prize. That removes the
exact pressure §23.10 diagnosed: v7 pushed for a file hard enough that a model without one
performed having made it. Nothing can be gained now by inventing an affordance, because the
affordance the reader needs is on this side of the clipboard.

**The lesson, and it is the one this whole series was circling.** §23.7 through §23.10 each made
the instruction better. The instruction was never the problem. **When a step can be done on
either side of a boundary, put it on the side you control** — the model gets the part only it can
do, and everything downstream of that becomes code, with tests, that behaves the same on every
chat and cannot be talked out of it.

**Verified.** Nine assertions in `test_review_digest.mjs`, driving the panel the way a reader does
rather than testing the extractor in isolation: the saver renders in both views; it names the file
after the game; a whole chat reply (chatter, fence, page) is read as the page and enables Save;
the §23.10 marker is named as an announcement and holds Save shut; clicking Save produces a real
download under that exact filename; and the saved bytes run `<!DOCTYPE html>` to `</html>` with no
fence markers in them.

### 23.12 `html-v10` — the ladder is retired, and the evidence is our own sentence

The reply this time was the page wrapped in a tag:

```
<canvas title="star-wars-zero-company.html">
<!DOCTYPE html>
<html lang="en">
…
```

Gemini labelled the block **XML**, because with that wrapper around it that is what it was. Its
download icon saved `gemini-code-1788601693253.xml`. The browser refused to render it —
*"error on line 2 at column 2: StartTag: invalid element name"* — because a DOCTYPE is invalid
inside an XML document.

**That is this addendum's own instruction coming back at us.** From v5 to v9, Gemini's bullet
said: *put the document into Canvas as an HTML file, and **title the canvas `<game>.html`***.
Flash cannot call the Canvas tool. So it did exactly what the sentence describes, in the only
medium it has — it wrote the element. `<canvas title="star-wars-zero-company.html">` is not
disobedience or hallucination; it is a literal reading of a literal instruction by a model with
no way to perform the non-literal one.

**Third distinct kind of garbage out of that one rung.** §23.8: a python block. §23.10: the
interface's file-created marker. §23.12: a wrapper element. Every failure in this series since
v5 has come from Gemini reaching for Canvas and producing something Canvas-shaped instead of
Canvas — and it reaches because we keep telling it to.

**And since §23.11 the rung buys nothing.** Canvas was ranked first for exactly one reason: its
Download button hands over a file named by the canvas title, which is how the reader was going
to get `<game>.html` rather than `gemini-code-1788601693253.xml`. The reader's own page does that
naming now, from any code block, deterministically. A rung that cannot be reached by
instruction, produces mangled output whenever it is attempted, and no longer pays for itself is
not a fallback worth keeping.

So Gemini's bullet is one sentence:

> **One `html` code block holding the document, and nothing else.**

with the wrapper banned by name — `<canvas …>`, `<immersive …>`, `<document …>` — and the reason
given as a fact rather than a preference: **a panel does not open because you wrote its tag**,
and the wrapper turns the block into malformed XML that downloads as `.xml` and will not open.
The ladder, its ordering, its stop condition, "there is no rung 3" and the word *rung* itself are
gone from the file.

**One old trap re-sprung and was closed with it.** The section still opened *"Not a fenced code
block"* three lines above a bullet asking for exactly one — §23.7's contradiction, quietly grown
back while the exception drifted downward. The exception is now carved inline, beside the ban:
*"Not a fenced code block (**Gemini excepted** — its bullet below asks for exactly one)"*.

**The saver already handles the reply that prompted this**, which is the first time in twelve
sections that a Gemini failure needed no prompt change to be survivable: `rdExtractHtml` ignores
the fence's language tag, finds `<!DOCTYPE html>` past the stray `<canvas …>` opener, and saves
a valid page. v10 stops us *causing* the failure; §23.11 already stopped it *costing* anything.

**The lesson.** §23.11 said to move a step to the side of the boundary you control. This is its
corollary: **once you have, delete what you built to survive the other side.** The Canvas rung
was scaffolding for a problem that no longer exists, and scaffolding left up is not neutral —
it was still issuing an instruction, and the instruction was still being obeyed literally by
the one model it was written for.

**Verified.** Four assertions added and six inverted or retired: the wrapper ban is present and
names `<canvas …>`; the reason is stated as a fact about the world; the `.xml` consequence is
given in the reader's terms; the code-block exception sits beside the ban it excepts, above the
per-chat bullets; and the word *rung* no longer appears anywhere in the addendum. Prose
assertions now run against a whitespace-flattened copy of the bundle, because three separate
re-wraps of this file have failed checks on text that was still present — the test should assert
that a rule is there, not how it happened to wrap.

## 24. Shipped 2026-09-15 — a bigger sample, a different default, and history the size could not buy

Three asks off one screenshot of the setup dialog: make the settings in it the defaults, add a
5000 option, and — the interesting one — *"what if we drop every other page of reviews, so we
can cover a bigger time period?"* The first two are a re-pricing of decisions §21-§23 made
against a smaller context window. The third is a new axis.

### 24.1 The defaults were set when the digest was the cheap read

Every default in that dialog was chosen under a constraint that has since moved:

| Option | Was | Now | Why it moved |
|---|---|---|---|
| Sample | 500 | **5000** | §21.3 put the ceiling at 2000 by dividing a 200k context window by ~47 tokens a review. The arithmetic is still right; the window is not. |
| Output | Markdown | **HTML page** | §23.11 made the HTML reply a one-click save. What was an opt-in that cost a manual save-as is now the artefact most readers actually wanted. |
| Quality bar | 5+ words | **10+ words** | A bar is rationing. At 500 reviews the sample was scarce and 5 was the cautious pick; at 5000 it is not, and the binding constraint has moved from *how many* to *which*. |

**5000 as the new ceiling.** Steam was never the limit — it paginates past 12,000 without
complaint (§21.1) — and §21.3 said so explicitly while capping at 2000 anyway, because 5000 is
~235k tokens on a text-heavy game and a 200k model cannot hold it. That is not a reason to
withhold the option from a reader running a 1M-context model; it is a reason to **price it on
the pill**. So the tooltip names the token cost, the composer that survives it, and the fetch
time, and the result panel prices the finished bundle as it always has. The rule from §22.1
holds unchanged: nothing caps a size from underneath the reader — 5000 means 5000.

**Why the deepest sample is the default rather than the deepest option.** Every cost here is
recoverable or stated up front: tokens are priced in the footer, minutes are spent while the
progress bar is on screen, and a smaller sample is one click away. The one thing a reader
cannot recover after the fact is **history the fetch never reached** — they get a confident
report about the last three days and no indication that is what it is.

### 24.2 Reach — the size says how many, not how far back

`filter=recent` is newest-first in pages of 100, so every sample this page has built is a
**contiguous run of the newest N**. On a busy game that run is short and no size fixes it:
Cyberpunk 2077 at 500 reviews is 3.4 days (§21.3), and even 5000 is about a month — a patch
that shipped six weeks ago is still out of reach at the ceiling. The size buys *more of the
same window*, which is precisely what §15.3 promised ("history, not accuracy") and cannot
deliver once the window itself is the limit.

**Keep one page in N, discard the rest.** The same N reviews become a systematic 1-in-N sample
of N times the period: identical bundle, identical tokens, N times the history. `Reach: Every
page | Every 2nd page | Every 3rd page`, default **Every page** — the contiguous run, exactly
what the page has always produced.

**The skipped pages are still fetched, and that is the entire cost.** Steam's pagination is
cursor-chained: the only way to learn page 4's cursor is to ask for page 3. A skip is a
*discard*, never a saving. Reach 2 costs twice the requests and twice the wall clock for the
same bundle; reach 3, three times. The reader pays in **minutes, not tokens** — the opposite
trade from the size selector — and the hint under the row says so, because discovering it at
page 40 of a fetch you thought would be short is being misled by omission. The page ceiling is
computed in kept pages first and multiplied by the reach afterwards; the other order is the bug
that would make reach 3 return a third of a sample and call it a quiet game.

**A page, not every second review.** One request, one cursor, no bookkeeping — and 100
consecutive reviews is a few hours of one game and a week of another, small enough everywhere
to be a sampling unit rather than a blind spot. Page 1 is always kept, so the newest review is
in the sample at every reach.

**What survives a uniform thinning, and what does not.** Everything the digest computes from
**proportions** is untouched: NOW and BEFORE are thinned by the same N, so the trend between
them, the sentiment splits and every topic rate mean exactly what they meant at reach 1. The
one family of figures that is not safe is **volume** — reviews/day comes out N times under the
truth. So the bundle states the factor rather than hiding it, in the two places a reader or a
model would otherwise go wrong:

- `SAMPLE` gains a `reach:` line naming the pages and reviews passed over, and saying what they
  are: *"not missing, not deleted and not filtered — they are the other 1 in 2. Do not describe
  the gaps between review dates as quiet periods."* This is the failure mode that produces a
  **confidently wrong** report rather than a visibly short one.
- `COVERAGE` gains a `SAMPLING:` line: the span is fact, the rate beside it is N times under,
  multiply before quoting it or do not quote it — and everything below is a proportion and is
  unaffected.

That is the same bargain §23.1 struck for the quality bar and §6 struck for the copypasta
filter: remove what would distort the counting, and **say what was removed**.

### 24.3 Verified

`test_review_digest.mjs` gains a ninth scenario and pins three older ones. 252 checks pass.

The reach scenario numbers every review in the fixture, so *which* pages landed is read off the
bundle rather than inferred from a total: at reach 2 with a 300 sample, review numbers 1-100,
201-300 and 401-500 are present, **101-200 are nowhere**, three kept pages cost exactly five
requests (four would mean the skips were not fetched, which is impossible; six would mean the
loop kept walking after the count was met), and the bundle still says `--- REVIEWS (300) ---`.
Both disclosure lines are asserted by shape, and the dialog copy and the entry-point tooltip
are asserted to stop claiming a contiguous run the moment a skip is picked.

Scenario 1 asserts the new defaults (5000, HTML page, 10+ words, Every page) and that the reach
row carries tooltips like every other option in that dialog — then **clicks back to Markdown and
the 5-word bar** before fetching, because its bundle assertions were written for that path and
include a six-word review that a 10-word bar legitimately drops. Scenarios 5, 6 and scenario 8's
first run pin Markdown for the same reason: each was written as a Markdown-path test and §24.1
moved the ground under it. Scenario 8's run C now exercises the 10-word default end-to-end.

**The lesson, and it is §22.1's inverted.** §22.1 removed a char budget that silently returned
less than the reader asked for. This section adds two options that cost the reader real money —
a quarter-million tokens, four minutes of fetching — and the thing that makes them safe is the
same thing: **state the price on the control, and deliver exactly what was picked.** A limit
you can see and pay is an option; a limit that acts on your behalf is a bug.
