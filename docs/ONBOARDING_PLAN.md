# Easing the QTPD learning curve
### Applying `docs/TEACHING_COMPLEX_THINGS.md` to the live page

`TEACHING_COMPLEX_THINGS.md` collects the GMTK strategy-tutorial techniques. This document is
the other half: what the page **actually looks like to someone who has never seen it**, which of
those techniques transfer, which don't and why, and the full list of candidate solutions with
their cost and risk — scored and ranked by impact ÷ effort in **§4**. Nothing here is built. It
is a decision menu, not a plan of record — see §6 for the calls that need making before any of
it is scoped.

Cross-references: frontend as-built is **[ARCHITECTURE.md](../ARCHITECTURE.md) §11**; the
shipped filter/view patterns are **§3.4**; the open UX backlog is **§3.2**.

---

## 1. The complexity surface, measured

Not an impression — counted off `index.html` at the time of writing.

| Surface | Count | Where |
|---|---|---|
| Games in the base universe | **129,445** | `COVERAGE.md` |
| Interactive controls in the filter panel | **78** (73 buttons + 5 inputs, excluding the 4 section heads) | `index.html` 1669–1930 |
| Labelled filter groups (`.field`) | **21** | same |
| Accordion sections | **4** (Value · Quality · Flags · Tags) | §11 *Filters & controls* |
| Table columns | **12** | §11 *The table* |
| URL state parameters | **29** | §11 *State in the URL* |
| Views | **3** (Table · Card · Grid) | §11 *Three views* |
| `localStorage` preferences | **5** (`view`, `sections`, `tagsCollapsed`, `scheme`, `trailers`) | — |
| Tag rail chips | built at runtime from the live tag set, two-tier + "+N more" | §11 *Tags* |

The page already does several things right, and any proposal has to respect them rather than
re-solve them:

- **The filter panel ships collapsed.** `<div class="topbar compact">` is the shipped markup, so
  the 78 controls are not the first thing anyone meets. The accordions inside it are *also*
  folded by default. Two layers of progressive disclosure are already in place.
- **Defaults are leftmost and gold marks deviation.** `markChangedControls()` lights up any
  control moved off its default, so "what have I changed" is answerable at a glance (§3.4 R3).
- **Grid is the default on mobile, Table on desktop.** The device gets the view that suits it
  without being asked (§11 *Three views*).
- **Nearly every control carries a written explanation.** The `title` coverage is genuinely
  thorough, and the custom tooltip engine renders it consistently.

So this is not a page that forgot about newcomers. The gaps are narrower and more specific than
"it's complicated", and they are listed next.

### 1.1 The four concrete gaps

**G1 — The entire explanation layer is absent on touch.** The tooltip engine's first line is:

```js
if(matchMedia && matchMedia("(hover: none)").matches) return;  // touch: no hover tooltips
```

Every `title` on the page — the column headers, the filter fields, the legend keys, the QTPD
tooltips, the `rb-key` swatches — is unreachable on a phone. This is not a missing feature so
much as an **inverted one**: mobile is where the audience is least expert and the view is most
compressed, and it is the one platform where the teaching layer does not exist. The native
`title` attribute is still in the DOM, but no mobile browser surfaces it on tap.

**G2 — The headline metric is defined at the bottom of an infinite-scroll page.** The formula —
`QTPD = (chosen HLTB hours × rating%) ÷ price` — lives in `renderFoot()`, below a list that
renders 66 games at a time (`PAGE_SIZE`) and appends another 66 on every scroll to the bottom.
Reaching the footer means exhausting the filtered set, which on the default view is 129,445
games. In practice nobody reaches it. The definition is
also carried in the `qtpd` column tooltip, which returns us to G1 on mobile. **On a phone, in
the default view, the number the whole site is named after has no explanation anywhere
on screen.**

**G3 — The empty state doesn't say which filter emptied it.** `render()` prints a fixed string:

```js
empty.innerHTML = "<b>No games match these filters.</b><br>Loosen the QTPD range, clear tags, or reset.";
```

Three guesses, in fixed order, regardless of what is actually set. With 78 controls and stacked
tri-state flags, the filter that did it is frequently not one of the three named — and the user
has no way to find out other than undoing things one at a time.

**G4 — The first screen answers a question nobody asked.** The default sort is QTPD descending
over the whole catalogue. That is the correct *engineering* default and the wrong *teaching*
one: it presents a ranked list of 129,445 games by a metric the visitor has not yet been told
the meaning of. There is no framing of what the list is, why it is in that order, or what to do
next. The tagline "quality time per dollar" is the only orientation given, and on a narrow
screen `body.narrow .topbar.compact .tagline{display:none}` removes even that.

---

## 2. What transfers, and what doesn't

The source doc is about games. Three of its load-bearing assumptions do not hold here, and
pretending otherwise would produce bad features.

**A player has already paid; a visitor has not.** The doc's central principle is *pace teaching
to investment* — spread lessons out because the player's willingness to learn grows as they get
invested. A web visitor's investment at t=0 is **zero**, and their cost of leaving is one tab
close. The curve doesn't rise over a campaign; it rises or dies in the first few seconds. So the
technique transfers, but **compressed**: the "inverted pyramid" still applies, except turn 1 has
to happen immediately and be worth something on its own.

**There is no failure state, so kinaesthetic learning has no natural feedback.** The doc's
hands-on techniques rely on consequences the player can observe. Nothing here can be done wrong.
But there *is* one live feedback signal already on screen: **the result count**. `N / 129,445
games` moves on every control change. That number is the closest thing this page has to a health
bar, and it is currently rendered as small muted mono text in a meta strip. Making it the
feedback channel is the honest local version of "learn by doing".

**"Multiple playthroughs" means return visits, which most visitors won't make.** Civ V's staged
expansion model assumes a player who comes back. Anything that defers teaching to a second
session only helps the minority who have one — worth doing, but never as the primary path.

**What does transfer cleanly:** affordances (§5 of the source doc) transfer completely and are
the highest-value section for this page, because the audience is precisely definable — they are
Steam users, and Steam's own visual language is available to borrow. "Reference tools on demand"
transfers completely. "Show, don't tell" and "keep text tight" transfer completely.

---

## 3. Candidate solutions

Grouped by the source doc's techniques. Each carries a rough effort band (S = under a day, M =
a few days, L = a week-plus) and its main risk; **§4 scores all eighteen numerically and ranks
them**. **None of these are decided.**

### Technique 1 — Stage the complexity (inverted pyramid)

**S1 · A one-decision entry.**
*Problem:* G4 — the first screen is a ranked list, not a question.
*Proposal:* Above the results, a single row: **"I want to spend about [ $10 ▾ ] on something
[ short ▾ ]"** — two dropdowns, nothing else. It writes the real filter state, so the list
below reorders live and the user has seen the whole mechanism work before reading one word of
explanation. Bruce Shelley's "turn 1: where to settle" — one decision, real consequences.
*Cost:* M. *Risk:* it is a second control surface competing with the real filter panel; it must
write into `state` and be reflected by `markChangedControls()`, not run in parallel.

**S2 · Earn the sections.**
*Problem:* four accordions present four equal-weight doors on arrival; a newcomer has no basis
to pick one.
*Proposal:* On a first visit, show **Value** only; **Quality**, **Flags** and **Tags** appear
(with a brief highlight) once the visitor has changed anything in Value. Frostpunk's per-system
mini-tutorials. Persist "unlocked" in `localStorage` so it is a one-time ramp, never a recurring
obstacle, and always provide a **"show everything"** escape.
*Cost:* S. *Risk:* hiding controls from someone who already knows what they want is actively
hostile; the escape hatch and the shared-link case (a URL setting a Flags param must force-unlock)
are both mandatory, not polish.

**S3 · Preset shelves. [Done — Chunk D.]**
*Problem:* the gap between "129,445 games ranked by a metric you don't know" and "78 controls"
has nothing in it.
*Proposal:* Four or five one-click presets on the landing view — *Best deals under $10* · *Long
games, highly rated* · *Short and cheap* · *Co-op picks* · *New and well-reviewed*. Each sets a
full filter state and is **shown as chips in the summary line afterwards**, so the click also
teaches which controls it moved. This is the single highest value-per-hour item on the list: it
converts the whole filter panel from a thing you must learn into a thing you can *read the
output of*.
*Cost:* S–M (the state-setting machinery, `loadFromURL`/`syncURL`, already exists; presets are
stored querystrings).
*Risk:* preset selection is editorial and will need revisiting as the catalogue moves; a preset
that returns a thin or stale list is worse than no preset. Needs a "these are just filter
settings, here's what they set" reveal so it doesn't read as a black box.

*Done, with three findings that changed the design:*

1. **The proposed shelves surfaced junk.** At the natural gate (100+ reviews, 70%+) sorted by
   QTPD, *Best deals* led with *Tap Heroes* and *New and well-reviewed* led with an adult title.
   The metric rewards hours per dollar, so an unknown 90-hour game at $1 beats every famous one.
2. **Adult content leaks past both filters.** That title carries no PICS `adult` flag and none
   of `ADULT_TAGS` — its SteamSpy tags are *Casual, Relaxing, Cozy, Comedy*. No data-driven
   filter available to us catches it; only the review floor moves the number (6.2% of the pool
   at 100+ reviews is adult-flagged, 1.3% at 5,000+).
3. **QTPD's hours side breaks for games with no ending.** EVE Online 1,777h, Melvor Idle 1,395h,
   Dota 2 770h. Divided by a small price these top every value ranking, so they are excluded
   from the length shelves (and only those — they are legitimate results elsewhere).

*The fix is per-shelf review BANDS, not one floor.* Popular shelves floor at 5,000 reviews so
results are recognisable; niche shelves **ceiling** at 5,000 so the well-known games cannot
crowd out what the visitor has not heard of. Both map onto the existing independent review
bands (`REV_BANDS` 0/10/100/1k/5k, gaps allowed by design), so a ceiling needed no new filter —
it is simply not selecting the top band.

*Two filters had to be built first,* because three shelves could not otherwise be expressed as
real filter state — and a preset that is not real filter state cannot show the user what it
changed, which is the whole point of the chips:
- **Length range (hours)**, `hmin`/`hmax` — the twin of Price range, reading `hoursFor()` so it
  follows the Length metric and Length data toggles.
- **Released within**, `rel` — the twin of Updated within, on `release_ts`. Previously "what
  came out recently" could only be reached by *sorting*, which is a different question.

*What the shelves return now:* shapez, Divinity: Original Sin, Garry's Mod, SteamWorld Dig,
Valheim, Green Hell on the popular side; Wizardry 8, Caveblazers, Globesweeper, Crystal Story II
on the niche side.

*House rule — no shelf ever highlights adult content.* Every shelf carries **both** locks:
`adult=hide` **and** an `exc=` list of every `ADULT_TAGS` entry. The second is not redundant:
`isAdult()` treats the PICS flag as authoritative for PICS-covered games, so the tag test never
runs for them, and a PICS-covered game tagged *Nudity* with no PICS flag passed `adult=hide`
alone. Adding the tag exclusion removed **1,498 games** across the seven shelves. `presets.py`
asserts both at build time, so a new shelf missing either fails the job rather than quietly
shipping. The residual — a game whose only adult signal is its title — remains uncatchable from
the data we hold, and is stated rather than implied away. See CLAUDE.md.

*No count on the chip.* `presets.py` can count a shelf, but it counts it by re-implementing
`passFilters()` in Python, and the two drifted — three corrections were needed before they
agreed (PICS tags override SteamSpy, HLTB realness is per-metric not per-game, the PICS adult
flag *replaces* the tag test rather than adding to it). Six of seven shelves now match the page
exactly, but a chip quoting a number the page did not compute is a chip that can lie, so the
chip carries only its label. The live count is one click away in the meta strip and is always
right.

**S4 · Defer the second-order controls.**
*Problem:* the **QTPD range slider** and the **min-sale stepper** cannot be understood before
QTPD itself is, yet they sit in the same visual tier as min price.
*Proposal:* Move both behind an "advanced" disclosure inside Value. They are refinements of a
metric, not entry points to it.
*Cost:* S. *Risk:* low, but it partially undoes deliberate §11 work — the min-sale stepper's
resting-value behaviour was designed to be self-explanatory in place.

### Technique 2 — Stage across visits

**S5 · Simple / Full mode.**
*Problem:* the page has one complexity level and it is the maximum.
*Proposal:* A real reduced build — Grid view, presets, price + rating + length, and nothing else
— as the default for an unrecognised visitor, with a persistent **Full** switch. Civ V shipping
simpler and reintroducing systems later. Crucially the doc's point is that easy mode should
**remove systems**, not weaken them: a dimmed-but-present control is not simple mode.
*Cost:* L. *Risk:* highest on this list. Two modes is two surfaces to maintain, two screenshot
sets, and a permanent question of which one a bug report is about. Only worth it if S1–S3 are
measured and found insufficient.

**S6 · Recognise the returning visitor.**
*Problem:* whatever training wheels get built will annoy the regular.
*Proposal:* A visit counter in `localStorage`; first-run affordances self-retire after the
second or third visit. Cheap, and it is the prerequisite that makes S1/S2/S3 safe to ship
aggressively.
*Cost:* S. *Risk:* none material; `localStorage` clearing just replays the intro.

**S7 · Make an arriving shared link legible.**
*Problem:* with 29 serialized params, a shared link can land a stranger in a heavily-filtered
view with no indication that it *is* filtered — they will read a 40-game list as the whole site.
*Proposal:* When `loadFromURL()` applies more than one non-default param, show a one-line banner:
**"This link has 6 filters applied · [see them] · [start fresh]"**. It teaches the filter model
at the exact moment the visitor has a reason to care.
*Cost:* S. *Risk:* low. The summary line (§3.4 L2) already renders the chips; this is a banner
pointing at it.

### Techniques 3 & 4 — Teach by doing, and teach *why*

**S8 · Teach QTPD by comparison, not definition.**
*Problem:* G2. Also, a formula is a poor teacher even when read.
*Proposal:* The gold value-meter already does the real work — it makes "more" visible without
arithmetic. Build on it rather than on prose: on the landing view, annotate the **top row once**
— *"80 hours × 91% positive ÷ $12 — that's why this is #1"* — as a dismissible callout anchored
to a real game currently in the list. The visitor learns the formula from an instance, which is
the Threes lesson: a small real thing to work out, not a statement to read.
*Cost:* S–M. *Risk:* the callout must be built from the live top row, not hardcoded, or it will
rot within hours of a price refresh.

**S9 · An advisor empty state.**
*Problem:* G3.
*Proposal:* When the result set is empty, run the filter predicates **one at a time** over the
catalogue and report which single filter is responsible: *"Nothing matches. **Min rating 90+**
is the binding constraint — 1,240 games match everything else. [relax it] [reset all]"*. This is
the Offworld advisor: not "here is what the button does" but "here is what you just did and why
it didn't work".
*Cost:* M. *Risk:* an N-pass over ~129k games per empty render; must run only on empty, and can
short-circuit. The interesting design question is what to report when two filters are jointly
responsible — name the most-restrictive single one and say so, rather than claiming it is the
only one.

**S10 · Turn the result count into the feedback channel.**
*Problem:* §2 — this page's only real-time consequence signal is rendered as muted mono text.
*Proposal:* Animate the count on change and briefly attribute the delta to the control that
caused it (*"−4,102 · min rating"*). Compresses the slow-feedback problem the doc flags for
strategy games into an immediate one.
*Cost:* M. *Risk:* easy to make noisy or nauseating; needs a reduced-motion path, which the
page already respects elsewhere (`trailersOn()`).

### Technique 5 — Affordances

This is the section with the best cost-to-value ratio, and the source doc's Total War: Troy
hourglass is a direct warning about several live elements.

**S11 · The unexplained-glyph audit. [Largely moot — verified against the code.]**
*Problem:* the page uses symbols whose meaning is carried only in a hover tooltip — i.e. nowhere
on touch (G1).
*Verified Sep 2026:* every candidate below **already carries a written explanation** — `><` via
the Tags `<th>` title, `▲`/`▼` inside the Playtime header tip, `⤢` on its own button, and the
scheme-switch buttons get both text and title from `syncSchemeUI()` at runtime. So there is no
glyph here that is unexplained *on desktop*; there are only glyphs unexplained *on touch*, which
is S15's problem, not a separate one. This item collapses into S15 and should not be scoped
independently.
*Candidates:* `><` (Tags column collapse) · `⤢` (promote to player) · `▲`/`▼` (playtime
recommender split, which look like sort arrows and are not) · `Δ` (weighted-vs-Steam badge) ·
the `k-sale` gold edge · `18+` staging · `▸` section carets.
*Proposal:* For each, decide: give it a word, give it a persistent caption, or accept it as
decorative. The doc's rule is that an affordance either matches something the audience already
knows or it needs teaching — and `⤢` and `><` are inventions, not conventions.
*Cost:* S per item. *Risk:* low; mostly copy and a few pixels.

**S12 · Borrow Steam's language, not a spreadsheet's. [Done — Chunk A.]**
*Problem:* **QTPD**, **HLTB**, **Weighted**, **Trend** are all coinages or acronyms. The doc's
first stated takeaway is *don't assume your audience has played other similar games* — here,
don't assume they have used a data tool.
*Proposal:* Audit every visible label against "would this word appear on the Steam store". The
mobile card layout already does this correctly (Reviews→**Rating**, HLTB→**Length**, Price /
Sale→**Price**) — §11 *Responsive*. **Apply the same relabeling to the desktop table**, where
the acronyms currently survive. `HLTB · M / E / 100%` is the worst offender on the page: four
pieces of jargon in one header.
*Cost:* S. *Risk:* QTPD itself is the brand and should stay; the argument is for a plain-word
subtitle beside it, not a rename. (The metric was already renamed once, QHPP→QTPD, for exactly
this reason — §3.2.)
*Done:* `HLTB · M / E / 100%` → **`Length · M/E/100%`** in the table head, `HLTB metric for
QTPD` → **`Length metric for QTPD`** and `HLTB data` → **`Length data`** in the filter panel, and
`SORT_LABELS.hltb` `HLTB` → **`Length`**. That last one was the real find: it feeds the mobile
*"sorted by …"* chip, so a phone read **"sorted by HLTB"** directly above cards whose own label
said **"Length"** — two controls disagreeing about the name of one field. The grid card's
existing `Length <i>HLTB</i>` set the house pattern (plain word leads, acronym subordinate) and
every tooltip still spells out HowLongToBeat, so the sourcing is demoted, not lost.
*Left alone on purpose:* `data-label`, which is a CSS selector driving the card `::before`
labels, the `order` chain and the `:has()` no-data drops — not display text; and the CSV header
`HLTB hours`, since an exported column benefits from naming its source and renaming it would
break existing sheets.
*Scoring correction:* impact was set at 7 on the assumption this jargon was unexplained. It is
not — the desktop table is the only place that header exists, and desktop hover tooltips work.
Honest impact is **~5**.

**S13 · Make the legend real. [Done — Chunk A.]**
*Problem:* the `.rb-legend` key strip is `aria-hidden="true"` and its entries explain themselves
only via `title` — so it is invisible to assistive tech and inert on touch, while the colours it
explains (gold = value, red→green = review score, gold edge = on sale) are load-bearing
everywhere.
*Proposal:* Drop `aria-hidden`, give each key a visible short caption at wider widths, and make
it tappable on touch.
*Cost:* S. *Risk:* low; costs a little horizontal space in the toolbar row.
*Done:* `aria-hidden` removed from the strip; the two decorative colour swatches take it instead
so they no longer read as empty elements between the words. The "visible caption" half was
already shipped — each key has real text (*value score* / *review score* / *on sale*). The
"tappable on touch" half is S15's, not this item's.
*Scoring correction:* this strip only renders in **Grid view** (`.rb-legend{display:none}` +
`body.grid-view .rb-legend{display:flex}`), so its reach is narrower than the impact 5 implied.

**S14 · Playtest, which the doc names as its second explicit takeaway.**
*Proposal:* The repo already drives Playwright for layout verification. The onboarding
equivalent is a scripted first-visit walkthrough at 390px and 1700px with a written task list —
*"find a well-reviewed co-op game under $15 that takes about 20 hours"* — and a record of where
the run stalls. Every other item on this page is a guess until this exists.
*Cost:* M to set up, S per round. *Risk:* none. This should arguably precede everything else.

### Technique 6 — Supporting

**S15 · Give touch devices the explanation layer. (G1.) [Done — Chunk B.]**
*Proposal:* On `(hover: none)`, bind the same `title` corpus to **tap** — a tap on any element
carrying a tip opens the styled tip box; a second tap elsewhere dismisses it. The engine is
already fully event-delegated, so this is a second entry path into existing code, not a new
system. The one real design question is collision: a tap on a column header currently sorts, and
a tap on a card flips it — so the tip likely needs its own affordance (a small `?` on fields, a
long-press on values) rather than stealing the primary tap.
*Cost:* M. *Risk:* moderate, entirely in gesture collision. But this is the item that converts
a large body of already-written, already-good explanation from *invisible* to *available*, which
makes it the best value on the list.
*Done:* the engine no longer bails on `(hover: none)`; it detects the capability and wires a tap
path instead of the hover path. Collision was solved by **inverting the question** — rather than
finding a gesture nobody uses, a tap opens a tip only when it lands on an **inert** part of a
titled element. Anything with its own meaning (`button, a, input, select, textarea, summary,
label[for], [role=button], .sortable, .splitsort, .chip, .gcard, .gart, .stage-box, .tagstoggle,
.seg`) keeps it untouched. That costs almost nothing in coverage, because the page's controls sit
*beside* their explanatory label rather than inside it: `.field` carries the title and its
`<label>` is a sibling of the `.seg` holding the buttons. A second tap on the same element closes;
so does a scroll, or a tap anywhere else. Placement anchors to the element's box (centred under
it, flipped above near the bottom, clamped both axes) since there is no cursor to follow, and the
box is capped at `min(340px, 100vw - 24px)` so it fits a 390px phone.
*Discovery:* a pointer advertises a tip with `cursor:help`, which does not exist on a finger — so
on touch the 21 filter fields paint a small **?** beside their label. Everything else is found by
tapping the value it explains, which is the gesture people already try.
*Residual gap, deliberate:* the ~145 titled elements that are themselves controls keep their tap,
so their individual tips (e.g. *"Real only — use just real HowLongToBeat times"*) stay unreachable
on touch. The parent field's tip covers the group but not the per-option nuance. Closing that
would need a dedicated affordance per button, which is not worth the clutter — revisit only if a
playtest (S14) shows people reaching for it.

**S16 · A reference panel, reachable from the top.**
*Problem:* G2 — help exists but is at the bottom or on hover.
*Proposal:* A `?` beside the wordmark opening a short panel: what QTPD is (with the formula and
one worked example), HLTB vs Playtime (the two most-confused fields — the code comments already
note this confusion), what Weighted means, what the colours mean. The doc's "encyclopedia /
reference on demand". Deliberately *not* a modal on load.
*Cost:* M. *Risk:* low. The content mostly exists already, scattered across `title` attributes
and `renderFoot()`; this is consolidation, not authoring.

**S17 · Show, don't tell — for the two hardest concepts only.**
*Proposal:* Replace prose with a small inline diagram for (a) the QTPD formula, as three labelled
boxes over a division rule, and (b) **HLTB vs Playtime** — *hours to finish* against *hours
actually played* — which the codebase itself flags as the ambiguity that forced a card-layout
change. Into the Breach's preview animations: show the mechanism, don't describe it.
*Cost:* M. *Risk:* diagrams must work in the dark palette and at 390px; keep to two.

**S18 · Name the three views by what they answer.**
*Problem:* an existing code comment says it plainly — *"Table / Card / Grid" are three unexplained
words answering a question the user hasn't asked yet*. That comment records a fix already made
(the switcher moved out of the top toolbar down to `#resultBar`, immediately above the results it
governs) — but proximity only answers *what it affects*, not *what the three words mean*. This is
the doc's "multiple learning paths" technique already shipped, and still unlabelled.
*Proposal:* Tooltip/caption each with its purpose — Grid = *browse by box art*, Table = *compare
on numbers*, Card = *read one game at a time*.
*Cost:* S. *Risk:* none.
*Verified Sep 2026 — already shipped.* All three buttons already carry exactly this: *"Table —
every column, one row per game"*, *"Cards — the same data as the table, stacked one game per
card"*, *"Grid — Steam box art with the QTPD score and both review scores…"*. The item was
scored off a code comment that described the switcher's **position**, a problem also already
fixed. Nothing remains here except that those tooltips are invisible on touch — S15 again.
**Impact 3 → 0; drop from the list.**

---

## 4. Impact ÷ effort

Every candidate scored on two 1–10 scales. **Impact** = how much it moves a first-time
visitor's ability to use the page (10 = they can't use it without this). **Effort** = build cost
including the risk work it drags in (10 = weeks, or a permanent maintenance tax). The ratio is
impact ÷ effort; higher is cheaper value.

These are judgement calls made by reading the code, not measurements. **S14 exists precisely
because they are guesses** — a single first-visit playtest would re-order this table, and that
is the strongest argument for doing S14 before betting much on the rest of it.

| # | Solution | Impact | Effort | Ratio | Note on the score |
|---|---|:--:|:--:|:--:|---|
| S6 | Visit counter / returning-visitor | 4 | 1 | **4.00** | One `localStorage` key. Low impact alone — it's an *enabler* that lets everything else be aggressive without annoying regulars. |
| S12 | Push mobile relabeling onto the desktop table | 7 | 2 | **3.50** | Copy-only. The correct words already exist and are already shipped on the card layout; the desktop table just never got them. |
| S3 | Preset shelves | 9 | 3 | **3.00** | Presets are stored querystrings and `loadFromURL`/`syncURL` already exist. Real cost is editorial curation, and it recurs. |
| S7 | "This link has N filters applied" banner | 6 | 2 | **3.00** | `loadFromURL()` already knows the count; the summary chips already render. Only fires for link arrivals, which caps impact. |
| S11 | Unexplained-glyph audit | 6 | 2 | **3.00** | Mostly copy. Compounds hard with S15 — on touch these glyphs are currently the *only* signal. |
| S18 | Name the three views | 3 | 1 | **3.00** | Three tooltip strings. High ratio, small absolute gain. |
| S13 | Make the legend real | 5 | 2 | **2.50** | Drop `aria-hidden`, add captions, make it tappable. Costs a little toolbar width. |
| S15 | Tap-tooltips on touch | **10** | 4 | **2.50** | Highest absolute impact on the list. Engine is already event-delegated, so it's a second entry path, not a new system; the effort is entirely gesture collision. |
| S4 | Defer the second-order controls | 4 | 2 | **2.00** | Partially undoes deliberate §11 work on the min-sale stepper. |
| S8 | Annotate the live top row | 8 | 4 | **2.00** | Direct fix for G2, teaching by instance. Must build from the live row or it rots within hours of a price refresh. |
| S14 | Playtest | 8 | 4 | **2.00** | Ships nothing user-visible. Scored on leverage: it converts the other 17 guesses into knowledge. |
| S16 | Reference panel (`?` by the wordmark) | 7 | 4 | **1.75** | Consolidation, not authoring — the content exists, scattered across `title` attributes and `renderFoot()`. |
| S9 | Advisor empty state | 6 | 5 | **1.20** | Excellent when it fires; capped by how often anyone actually empties the list. Needs an N-pass and a joint-responsibility rule. |
| S1 | One-decision entry | 7 | 6 | **1.17** | Strong idea, but S3 captures most of the same value for a third of the cost. |
| S2 | Earn the sections | 4 | 4 | **1.00** | Marginal gain over "already folded" is small, and the shared-link force-unlock plus escape hatch are mandatory, not polish. |
| S10 | Result count as feedback channel | 5 | 5 | **1.00** | The one live consequence signal on the page, but delta attribution is subtle and easy to make nauseating. |
| S17 | Diagrams for QTPD and HLTB-vs-Playtime | 4 | 5 | **0.80** | S8 and S16 cover most of the same ground more cheaply. |
| S5 | Simple / Full mode | 6 | 9 | **0.67** | Two surfaces to maintain forever, two screenshot sets, and a permanent "which mode is this bug in". |

### 4.1 Where the ratio misleads

Pure impact ÷ effort rewards trivia. **S6** and **S18** top the ratio table while changing
almost nothing on their own; **S15** has the single highest impact on the page and only a
middling ratio because gesture collision is genuinely fiddly. Read the ratio as *"is this cheap
for what it gives"*, and the impact column as *"does it matter"* — then pick from the top-left
quadrant of both.

Three different questions, three different answers:

- **Easiest fix with real impact → S12.** Relabel the desktop table headers with the plain words
  the mobile card already uses. It is a copy change with no new mechanism, no new state, no
  gesture, no persistence. `HLTB · M / E / 100%` is four pieces of jargon in one header on the
  most-used desktop surface, and `Reviews`/`HLTB`/`Price / Sale` → `Rating`/`Length`/`Price` is
  **already proven in this codebase**. The only check needed is that the table's `min-width` —
  the exact sum of the column track minimums (§11) — still holds.
- **Biggest absolute impact → S15.** Everything else assumes the visitor can read the page's
  explanations. On touch, none of them exist. Until this ships, every other teaching improvement
  is desktop-only by construction.
- **Best single bet overall → S3.** Impact 9 at effort 3, and it is the only item that fills
  the void between "129,445 games ranked by an unexplained metric" and "78 controls" with
  something a visitor can act on in one click.

### 4.2 Recommended order

Weighted by impact first and ratio second, not by ratio alone.

| Phase | Items | Rationale |
|---|---|---|
| **0 — find out** | S14 | Every score above is a guess until a real 390px first-visit run exists. The source doc names playtesting as an explicit takeaway. |
| **1 — make existing teaching reachable** | S15, S12, S11, S13, S18 | Authors almost no new content; fixes *delivery* of explanation that is already written and already good. Contains both the highest-impact item and the cheapest. |
| **2 — give the first screen a first decision** | S3, S7, S8 | Presets, link legibility, one worked example. Turns arrival from "read this ranking" into "pick a thing". |
| **3 — respond to what the user does** | S6, S9, S16 | S6 first, because it is one key and it retires phase 2's affordances for regulars. |
| **4 — stage the surface itself** | S4, S1, S10, S2 | Structural changes to the control layout. Only once phases 1–3 are measured. |
| **Reconsider later** | S17, S5 | Lowest ratios on the board. S5 in particular buys an unmeasured gain for a permanent maintenance tax. |

If only one thing gets built: **S12**. If only one *afternoon* is available: **S12 + S18 + S11**
— all three are copy and tooltips, and together they remove most of the page's unexplained
vocabulary. If a week is available: **S15 and S3**.

---

### 4.3 Verified against the code — scoring corrections (Sep 2026)

Before building Chunk A, the four cheapest items were checked against the markup rather than
taken from §3's reading. Three of them were partly or wholly already shipped:

| # | Scored | Actual | Why the score was wrong |
|---|---|---|---|
| S18 | impact 3 | **already shipped — drop** | All three view buttons already carry purpose-explaining tooltips. The item was scored off a code comment about the switcher's *position*, which had also already been fixed. |
| S11 | impact 6 | **collapses into S15** | Every glyph (`><`, `▲`/`▼`, `⤢`, the scheme buttons) already carries a written explanation. There is no glyph unexplained on desktop — only glyphs unexplained on touch. |
| S12 | impact 7 | **~5, now done** | The jargon is real, but the desktop table is the only place it appears and desktop tooltips work. |
| S13 | impact 5 | **narrower, now done** | The legend renders in Grid view only. |

**The pattern this exposes is the most useful finding in this document.** On desktop, QTPD is
already thoroughly explained — the `title` corpus is extensive, well-written and consistently
rendered. Almost every "cheap copy fix" in §3 turns out to be cheap *because it is nearly a
no-op*. The learning curve is not a vocabulary problem; it is a **delivery** problem, and it is
almost entirely a **touch** problem.

That materially strengthens **S15** and weakens its neighbours: S11, S13 and S18 do not sit
*beside* S15 on the list so much as *inside* it. Phase 1 is therefore much smaller than §4.2
implies — Chunk A (S12 + S13) is essentially all of it, and the next real work is S15.

---

## 5. Deliberately not proposed

- **A modal tour on load.** The doc's whole argument is that frontloading fails — a
  click-through overlay is the literal "click here" arrow prompt it criticises, and it is the
  first thing a visitor dismisses.
- **Renaming QTPD.** It was already renamed once (QHPP→QTPD, §3.2) and the brand is the metric.
  The fix for the acronym is a plain-word gloss beside it (S12), not another rename.
- **Flattening the semantic palette.** §3.2 already records that gold/blue/coral/teal carry
  meaning; the affordance argument is to *explain* the colours (S13), never to reduce them to one
  accent.
- **Simplifying the metric itself.** QTPD's inputs are the product. The learning curve is a
  presentation problem, not a modelling one.

---

## 6. Open decisions

These need answering before anything above can be scoped properly.

1. **Who is the target visitor?** A Steam user arriving from a link, a deal-hunter who will
   return weekly, or a one-time curious click? The three want different phases first. Everything
   in §4 assumes "mostly one-time, some returning"; if the real audience is returning regulars,
   phase 1 still holds but phases 2 and 4 largely evaporate.
2. **Is mobile the priority surface?** If yes, S15 is not merely first — it is the only item
   that matters until it ships, because on touch there is currently no teaching layer at all.
3. **Presets: who curates them, and how often?** S3's value depends entirely on the shelves
   being good, and they are editorial content in a repo that otherwise generates everything.
4. **Is a two-mode page (S5) acceptable as a maintenance cost?** If the answer is no, say so now
   and it comes off the list permanently rather than resurfacing.
5. **Should any of this gate on the still-open §3.2 item** — progressive disclosure of secondary
   fields behind a per-card tap? It overlaps S2 and S4 and shouldn't be designed twice.

---

## 7. Build log

| Chunk | Items | State |
|---|---|---|
| **A** | S12 (plain-word Length labels) · S13 (legend exposed to assistive tech) | **Merged** (#87). Verified in Chromium at 1324 / 1400 / 1700 / 2400px: label renders on one line at every width, `<th>` height unchanged, no overflow, header/body column edges aligned, no JS errors. |
| **B** | S15 — tap-tooltips on touch (absorbs S11 and S18) | **Merged** (#88). Verified on an emulated Pixel 7 and at 1700px desktop — see below. |
| **D** | S3 — preset shelves, plus the Length-range and Released-within filters they needed, plus `presets.py` | **Done.** Verified against the full 129k-game dataset — see below. |
| **C** | S6 · S7 | — |
| **D** | S3 · S8 · S16 | — |
| **E** | S9 · S10 | — |
| **F** | S1 · S2 · S4 | — |

### Chunk B verification

Emulated **Pixel 7** (`hover: none`) and a **1700px** mouse context, same page:

| Check | Result |
|---|---|
| Tapping a filter field label opens the tip | pass |
| Tip stays inside a 412px viewport | pass (left 10px, right 350px) |
| Tapping the same label again closes it | pass |
| `?` affordance painted on touch | pass |
| Tapping a filter **button** still toggles it | pass — `aria-pressed` flips |
| Tapping a filter button does **not** open a tip | pass |
| Tapping a grid card's title still flips the card | pass — card opens |
| Tapping a grid card's title does **not** open a tip | pass |
| Scrolling dismisses | pass |
| **Desktop** hover still opens the tip | pass |
| **Desktop** does not paint the `?` | pass |
| **Desktop** mouse-out still hides | pass |
| JS errors | none |

The one that matters for **G2**: tapping the QTPD legend key on a phone now returns *"QTPD —
Quality Time Per Dollar, the headline metric. Higher means more game-hours of value per dollar."*
Before this chunk that string existed in the DOM and could not be reached by any gesture.

Coverage: **36 of 181** titled elements are tap-reachable; the rest are controls whose tap is
spoken for (see the residual gap under S15).

### Chunk D verification

Against the **real** dataset (81,907 games passing the default filters), not the bundled sample —
the sample is six games, which silently passes any filter test.

| Check | Result |
|---|---|
| Length range: max 6h | 81,907 → 15,907 |
| Length range: 3–6h | → 5,497, and all 60 sampled rows inside the band |
| Released within 1yr | 81,907 → 12,109 |
| 1yr and 1yr+ partition the catalogue | 12,109 + 69,798 = 81,907 exactly |
| Length stepper drives the length boxes, not the price boxes | pass |
| Both filters serialize to the URL and restore from it | pass |
| Reset clears both | pass |
| All 7 shelves render and apply | pass |
| Each shelf's live count vs the generator | 6 of 7 match exactly; *Short and cheap* differs by 14 (272 vs 258) |
| Summary chips explain the applied shelf | pass — e.g. *"reviews: 5k+ · $0–$10 · length 0h–6h"* |
| Clicking the active shelf clears it | pass |
| Row hides once the user sets their own filters | pass |
| Chips fit a phone viewport | pass |
| JS errors | none |

Two bugs this testing caught, both invisible without the real data:

- **`update()` renders before it calls `syncURL()`**, so the row read the *previous* URL and
  left a shelf looking active after the user had edited it. Fixed by repainting from `syncURL()`
  once the URL is current.
- **`.presetbar{display:flex}` outranks the UA sheet's `[hidden]{display:none}`**, so the row
  could never hide — it sat there as an empty strip before `presets.json` loaded and after the
  user filtered. Fixed with an explicit `.presetbar[hidden]{display:none}`.

### Stale figures found in `ARCHITECTURE.md` §11

Noticed while verifying, not corrected here — both are doc drift, not code bugs:

- §11 describes pagination as a **"100 / 500 / 2000 per page"** selector. The code fixes it at
  `PAGE_SIZE = 66`, and an `index.html` comment records the selector's removal ("Page size is
  now fixed at PAGE_SIZE below").
- §11 puts the table→card breakpoint at **1374px**. The `--grid-cols` comment block says the
  card layout takes over below **1280px**, with Tags folding to its strip between 1280 and 1365.
