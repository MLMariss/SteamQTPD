# QTPD — Ineska improvement list

First-time-user feedback on the live site, collected as a running comment thread with
screenshots. Ineska is an outside reviewer: no prior exposure to the app, no knowledge of
the data model, arriving cold at `mlmariss.github.io/SteamQTPD/` on a 1028px-wide desktop
window, incognito, in Chrome.

**What this file is.** The raw findings, one per item: what was pointed out, what the
underlying problem is, and where in the code it lives. It is a *capture* document — the
value is that nothing gets lost between the chat thread and a fix.

**What this file is not.** It is not a decision record and not a commitment. Fixes are
proposed per item and reviewed before anything is implemented; the `Fix` line stays
`pending review` until a direction is accepted. Once an item ships it gets annotated
`[Done]` and stays here as a record so it isn't re-raised.

**Why it deserves its own file** rather than a `ROADMAP.md` section: these are *usability*
defects observed by a real first-time user, not feature proposals. ROADMAP §3.x asks "is
there a data source, is it worth building"; this file asks "the thing we already built
confused somebody — why?". Different question, different bar, different lifecycle.

Each item quotes the comment in English, then the problem, where it lives, and the fix.

---

## Status index

| # | Item | Area | Status |
|---|---|---|---|
| 1 | View switcher reads as noise at the top of the page | Header / layout | **Done** |
| 2 | "Table view is desktop-only" is wrong on a 1028px desktop | Breakpoints | **Done** |
| 3 | Hover affordance inconsistent across the view switcher | Tooltips | **Done** |
| 4 | Gold card outline is unexplained | Grid view | **Done** |
| 5 | Expanded card overlay is not what was expected | Grid view | **Done** |
| 6 | Sort-direction control is a sentence, not an arrow | Sort UI | **Done** |
| 7 | QTPD wordmark toggles filters instead of going "home" | Header / reset | **Done** |
| 8 | Hover affordance inconsistent across grid cards | Grid view | **Done** |
| 9 | Bare `%` figures have no visible meaning | Grid view | **Done** |
| 10 | Card height jumps with longer titles | Grid view | **Done** |
| 11 | Accidental text selection can't be cleared | Grid view | **Done** |
| 12 | A fully-opened grid row collapses | Grid view | **Done** |
| 13 | Opened cards revert after scrolling away and back | Grid view | **Done** |
| 14 | Discount badge is the smallest type on the card | Grid view / type | **Done** |
| 15 | The Video preview toggle does nothing in Grid view | Grid view | **Done** |
| 16 | Titles with a leading space sort to the very top | Sort | **Done** |
| 17 | "Review score" sort looks unsorted in Grid view | Sort / Grid view | **Done** |
| 18 | Grid sorts by fields the card never shows | Sort / Grid view | **Done** |
| 19 | The card shows HLTB "Length", the sparsest field we have | Grid view / data | **Done** |
| 20 | "HLTB" is never expanded anywhere | Tooltips | **Done** |
| 21 | The header count strip has no tooltip | Tooltips | **Done** |
| 22 | Truncated titles can't be read in full | Tooltips | **Done** |
| 23 | "Per page" only lets the user hurt themselves | Header / paging | **Done** |
| 24 | "Close" is cut off on the opened grid card | Grid view | **Done** |
| 25 | Back does not return after Find similar | Navigation | **Done** |
| 26 | The opened card's scrollbar goes unnoticed | Grid view | **Done** (with 24) |
| 27 | The opened card's title is hidden under the rows | Grid view | **Done** |
| 28 | The table's top-left corner looks cut off | Table view | **Done** |
| 29 | Long table titles are cut mid-letter | Table view | **Done** |
| 30 | The search dropdown looks cut off | Search | **Done** |
| 31 | Row hover pokes past the table's rounded corners | Table view | **Done** (with 28) |
| 32 | Tag chips in the Showing line touch each other | Header / summary | **Done** |
| 33 | Length max can be set to 0 | Filters | **Done** |
| 34 | Price type has no hover and looks unselected | Filters | **Done** |
| 35 | Stale "Showing 66 of 7511" above "No games match" | Paging | **Done** |

---

## 1 — View switcher reads as noise at the top of the page

**Said** (20:26):

> First of all, the thing the eye immediately snags on and that raises questions: even though
> everything is written out and the tooltips are in place, my first reaction was still 'dafaq
> is this'. In my opinion these belong somewhere closer to the content.

**Problem.** `Table / Card / Grid / CSV` sits in the top header bar, far from the results it
governs. It is the first cluster the eye lands on, before the user has seen a single game,
so it is being read with zero context — at that moment "Table/Card/Grid" are four unexplained
words. Labels and `title=` tooltips do not fix this, because the confusion is about
*relevance*, not about wording: the control answers a question the user has not asked yet.

**Where.** `index.html` — `#viewSwitch` in the header (~L1136–L1142).

**Fix.** `[Done]` — the switcher moved out of the top bar into a new `#resultBar` that sits
directly above the results, prefixed with a `VIEW` label. It now reads in the place where it
has a subject: the thing immediately under it is what it changes. CSV, Lucky and the share
link stay in the top bar — those are actions, not layouts, so they don't gain anything from
being next to the results.

---

## 2 — "Table view is desktop-only" is wrong on a 1028px desktop

**Said** (20:27), on the toast shown when Table is clicked at 1028px:

> Pardon my french..? A 1028px screen is still desktop.

**Problem.** The message says *desktop-only* but the actual gate is a **width** threshold:
`matchMedia("(max-width:1374px)")` flips the layout to card. The table itself has a hard
`min-width:1324px` (1218px with the Tags column collapsed), so it genuinely does not fit at
1028px — the mechanism is correct, the explanation is not. Telling a desktop user their
desktop isn't a desktop reads as a bug in the app, not as a constraint of the layout. The
same wording problem exists in the mirrored "Card view is mobile-only" toast.

**Where.** `index.html` — toast text (~L3628–L3630), `isNarrow()` (~L2493), table
`min-width` (~L410–L412).

**Fix.** `[Done]` — in two passes. The first only fixed the *wording*, and left the threshold at
1374px on the assumption that it was correct. It wasn't, and the second pass fixed the real
problem — see the measurements below.

**Pass 1 — the message.** It now names real numbers instead of guessing at the device: *"The
table needs a window at least 1280px wide — this one is 1028px. Cards below that width show the
same data, one game per card."* The width is read live, so it always quotes the window actually
in front of the reader. The mirrored card-view message got the same treatment.

**Pass 2 — the threshold, which was simply wrong.** Measured in Chromium rather than reasoned
about:

| Window | Table width needed | Available | Verdict |
|---|---|---|---|
| 1440px | 1324px floor | 1398px | fits, room to spare |
| 1366px | 1324px floor | 1326px | **fits** — was being sent to cards anyway |
| 1340px | 1324px floor | 1300px | overflows by 24px |
| 1280px | 1324px floor | 1242px | overflows by 82px |
| 1280px | 1218px *tags folded* | 1242px | **fits**, 24px spare |

So the old 1374px cliff was wrong in both directions: it denied a table to 1366px windows that
fit one comfortably, and it never offered one to 1280px windows that could have had it. The
stale code comments claiming the card layout took over "at ≤1290px" were a leftover from an
earlier threshold and disagreed with the 1374px the code actually used.

There are now **two** measured widths instead of one guessed one:

- `TAGS_OPEN_MIN_W` (1366) — the narrowest window fitting the table with every column open.
- `TABLE_MIN_W` (1280) — the narrowest window that gets a table at all.

Between them the Tags column folds to its strip automatically, which drops the floor from 1324px
to 1218px and makes the whole table fit at 1280px with room over. That reuses a mode the table
already had rather than inventing a cramped one — the first attempt tightened every column floor
instead, and at 1280px that put every column on its floor at once and clipped the *headers*
("WEIGHTED" rendering as "IGHTED"). The folded-tags table at 1280px clips exactly the same four
headers that already clip at 1366px, and nothing more.

Folding is driven only by crossing the breakpoint, never by the general layout pass, so it can't
silently re-fold a column the user has just expanded by hand. Expanding Tags at 1280px still
works and still scrolls sideways — the user's call, as before.

---

## 3 — Hover affordance inconsistent across the view switcher

**Said** (20:28, on the CSV button showing a tooltip):

> There's an action on hover.

**Said** (20:30, on Table / Card / Grid in the same row):

> But these don't have one. So — yes or no?

**Problem.** Not the tooltips — those are fine. The custom tip engine is delegated over every
`[title]` element, so CSV and Table/Card/Grid get the identical styled bubble. What differs is
the **button itself**: `.util-btn` (CSV, Lucky, share) has a `:hover` rule and `.seg button`
(the view switcher) had none at all. So in a single row of controls, hovering one half lit up
and hovering the other half did nothing — which is precisely "some have an action on hover and
some don't", and it makes the dead-looking half read as not-interactive.

**Where.** `index.html` — `.seg button` had no `:hover` (~L143–L151) while `.util-btn:hover`
exists (~L745); the shared tip engine is the `TIP` block (~L3930–L3950).

**Fix.** `[Done]` — `.seg button` had no `:hover` rule at all, while the `.util-btn` group
beside it in the same row did; that, not the tooltips, is what made half the row look dead.
Segmented buttons now take a hover background. Disabled ones are deliberately included: they
are still clickable (clicking is how you get the explanation from item 2), so they have to look
like they'll do something — their existing 38% opacity dims the hover to read as "there's
something here, but not a choice right now". Only the pressed button is excluded, so
"which one is selected" stays the strongest signal in the group.

---

## 4 — Gold card outline is unexplained

**Said** (20:31), arrows drawn at two cards in the grid that carry a gold border while
neighbours don't:

> I don't understand why some have the yellow edge and some don't.

**Problem.** There are in fact **two different gold cues** stacked on the same card edge and
nothing distinguishes them or explains either:

- `.gcard.sale` — a gold `border-color`, meaning the game is discounted right now.
- `.gcard.hi` — a gold `box-shadow` ring, applied when the card's QTPD value is in the top
  third of the current result set (`q >= state.qDomain * 0.66`).

They are near-identical visually, they can appear on the same card, and neither is
documented anywhere in the UI — no legend, no tooltip, no hover text. A user seeing a mixed
grid has no path to the answer.

**Where.** `index.html` — `.gcard.sale` / `.gcard.hi` CSS (~L803–L804), class assignment in
`gridCardHTML()` (~L2536).

**Fix.** `[Done]` — the card edge now means exactly one thing: **on sale**. The top-third-QTPD
mark moved off the edge and onto the QTPD value itself, which is the number it was always
about — it renders as a gold pill around `QTPD 115`. Both marks gained real tooltips, and the
grid now carries a permanent legend in `#resultBar` (`QTPD value score · review score · on
sale`) so the answer is on screen rather than hidden behind a hover.

The pill is drawn with an inset `box-shadow` rather than a `border` on purpose: a border adds
1px of box model per side, which made top-third cards 2px taller than their neighbours and
quietly reintroduced item 10. Caught by the height check, which is why it's a shadow.

---

## 5 — Expanded card overlay is not what was expected

**Said** (20:32), on the spec-sheet card view reached from the switcher:

> Well, I honestly didn't expect it to be like this.

**Problem.** The detailed single-column view was not what the user anticipated from where
they clicked — the transition from a dense grid of box art to one tall stacked spec sheet
happens with no visual continuity and no indication that this is the *same data, different
density*. The switcher's `title=` ("Stacked spec-sheet cards") is not read at click time.
Related to item 1: the switcher is being operated before its options mean anything.

**Where.** `index.html` — `setView()` (~L2520), `body.layout-card` styles (~L942 onward).

**Fix.** `[Done]` — resolved with item 2 rather than separately. Digging into it, this wasn't
a view the user had chosen: at 1028px the app had already coerced them into card layout, and
the surprise was arriving somewhere they didn't pick. The width-aware message explains that,
and the switcher's labels now say what each view is in full — Card reads *"the same data as
the table, stacked one game per card. Used when the window is under 1375px."*

---

## 6 — Sort-direction control is a sentence, not an arrow

**Said** (20:36), on the `↓ High → Low — flip` button in the sort popover:

> Wut? Are two plain up/down arrows just not stylish enough any more?

**Problem.** A direction toggle is one of the most conventionalised controls on the web — an
arrow, or a pair of them. This one renders as a compound string that mixes **current state**
(`↓ High → Low`) with **the action** (`— flip`) in a single label, so the user has to parse
which half describes now and which half describes what happens on click. The conventional
control needs no parsing at all.

**Where.** `index.html` — sort popover action button (~L2794); the grid sort row has its own
direction button (~L3589, ~L3600).

**Fix.** `[Done]` — replaced with two arrow buttons, `↑` and `↓`, one lit. The lit arrow is
the current direction and the other one is what the click does, which is the ordinary shape of
a direction control and needs no reading. Clicking the already-lit arrow is now a no-op rather
than a flip — it's a two-state control, not a toggle, so pressing "descending" while already
descending shouldn't reverse it.

---

## 7 — QTPD wordmark toggles filters instead of going "home"

**Said** (20:38), with an arrow at the `QTPD` logo showing its "Show / hide filters" tooltip:

> This is an unusual solution. But is there some way to get everything I've poked at back to
> the starting view, quickly and simply? Because that's what I'd expect from clicking QTPD.

**Said** (20:41):

> I can reset the filters with that link, but I still have to open 'Sorted by' and try to
> remember what you had there in the first place.

**Problem.** Two defects in one item:

1. **The wordmark does the wrong thing.** Clicking a site's logo means "take me back to the
   start" in essentially every web UI. Here it toggles the filter nav — a legitimate control,
   but bound to the one element with the strongest pre-existing expectation attached to it.
2. **There is no full reset.** `Reset all filters` clears filters, search and flags — but it
   does **not** restore `sortKey` / `sortDir`, and its own tooltip claims it resets "the sort
   back to their defaults", which is false. So a user who has changed the sort has no way
   back except remembering the original value. The second comment is exactly this: the reset
   link is found, and it is not enough.

**Where.** `index.html` — `#logoToggle` handler (~L3516), `#clear` handler (~L3520–L3537,
no sort reset), `#clear` tooltip claim (~L1162).

**Fix.** `[Done]` — both halves fixed.

1. The wordmark is now **back to the start**: it clears filters, search and sort, and scrolls
   to the top, confirming with a short toast. The filter show/hide it used to do is unchanged
   and still available on the "⌄ Show filters" handle directly below it, which is a control
   that says what it does.
2. `Reset all filters` now actually resets the sort (back to QTPD ↓) — it didn't, while its own
   tooltip claimed it did. Both entry points share one `resetAll()` so they cannot drift apart
   again, and it also clears any flipped-open grid cards, which are part of "how the page
   started" too.

---

## 8 — Hover affordance inconsistent across grid cards

**Said** (20:44):

> Again, some have an action on hover and some don't.

**Problem.** Same complaint as item 3, and — like item 3 — it turned out to be a real defect
rather than an impression. `.gcard:hover` sets a border colour, but `.gcard.sale` sets one too,
and the two selectors have **equal specificity** (two classes each). `.sale` is written on the
next line, so at equal specificity the later rule wins and a discounted card's border never
changed on hover. Every card *looks* uniformly interactive, and the discounted ones silently
weren't responding — exactly the "some do, some don't" that was reported, and not something a
user could ever form a rule about, because the rule is "the ones that happen to be on sale".

**Where.** `index.html` — `.gcard:hover` immediately followed by `.gcard.sale` (~L802–L803).

**Fix.** `[Done]` — this turned out to be a CSS specificity bug rather than a matter of taste.
`.gcard:hover` and `.gcard.sale` have *equal* specificity, and `.sale` was written later, so it
won: **every discounted card had no hover response at all**, which is exactly the "some do and
some don't" that was reported. The hover rules now come after the `.sale` rule, and a sale card
gets its own brighter gold on hover. Cards also gained a soft shadow on hover so the whole card
responds as one object.

---

## 9 — Bare `%` figures have no visible meaning

**Said** (20:46), arrow at the green `90%` on a card, tooltip reading *"Steam review score
90% · 15,917 revi…"*:

> Maybe add some icon or something? Coming at it from outside, I have no idea at first what
> those % are.

**Problem.** The card shows a coloured `90%` with no label, no icon and no unit. Its meaning
— Steam all-time review score — exists **only** in the `title=` tooltip, so it is invisible
until hovered, and unavailable entirely on touch. The colour coding (`ratingColor()`) is
itself an unexplained second signal. A first-time user's most likely guesses are discount or
completion, neither of which is right, and there is nothing on screen to correct them.

**Where.** `index.html` — `.grate` render in `gridCardHTML()` (~L2540–L2543), `.grate` CSS
(~L831–L832).

**Fix.** `[Done]` — the `%` now carries a thumbs-up icon, so it reads as a review score at a
glance and, unlike a tooltip, still works on touch. It's a CSS mask painted in `currentColor`,
so it takes the same red→green rating colour as the number and costs no per-card markup. The
tooltip was rewritten from a bare figure into a sentence (*"Steam review score — 90% of 15,917
reviews are positive. Colour runs red (low) to green (high)"*), which also explains the colour
coding — a second unexplained signal nobody had asked about yet. The `#resultBar` legend from
item 4 covers the same ground permanently, on screen.

---

## 10 — Card height jumps with longer titles

**Said** (20:49), with a screen recording of the grid resizing as content loads:

> When a title is longer, the block height jumps around. That's usually not great.

**Problem.** `.gcard` has no fixed height — it is art (fixed `aspect-ratio:460/215`) plus an
info panel that grows with its content. `.gname` is clamped to two lines via
`-webkit-line-clamp`, so a one-line title and a two-line title produce cards of different
heights, and because CSS grid stretches every card in a row to the tallest one, a single
long title in a row re-heights that entire row. Scrolling through the grid therefore shows
a ragged, shifting rhythm rather than a stable one.

**Where.** `index.html` — `.gcard` / `.gart` / `.gcard .gmeta` / `.gname` (~L800–L834), grid
container `.gridview` (~L793–L794).

**Fix.** `[Done]` — the title box now reserves both of its clamped lines whether or not the
title needs them (`min-height:2.5em` on `.gname`), so a one-line title and a two-line title
build identical cards. Every card in the grid is the same height and the row rhythm no longer
shifts as you scroll.

*Verified in Chromium:* across the first 120 cards, every card measures 188px. Before the fix
they came in two heights.

---

## 11 — Accidental text selection can't be cleared

**Said** (20:50), with the word "Price" highlighted blue on an opened card:

> A small thing, but it's annoying: if you accidentally select something while clicking, you
> can't get rid of it any more.

**Problem.** Repeated or double clicking on a card selects its text — the card is a click
target, so double-clicks happen naturally. Normally a click on empty space clears a
selection, but every click inside the grid is intercepted by the card handler
(`card.classList.toggle("open")`), so the usual "click elsewhere to deselect" gesture instead
flips cards open and closed while the highlight stays. Card labels are chrome, not content
anybody wants to copy, so the selection has no upside here.

**Where.** `index.html` — grid click handler (~L3637–L3651); no `user-select` rule on
`.gcard` (~L800).

**Fix.** `[Done]` — card chrome is now `user-select:none`. None of it (title, QTPD, `%`) is
text anybody wants to copy, and once the selection can't happen the "can't clear it" problem
goes with it. The details overlay opts back in with `user-select:text`, because a price is
worth copying.

---

## 12 — A fully-opened grid row collapses

**Said**, with a screenshot of a row of six opened cards squashed to a fraction of the
normal card height while the untouched cards beside them keep full size:

> In grid view — if all game cards in the row are opened, they all collapse how it is visible
> on the screen right now.

**Problem.** `.gcard.open` sets `display:none` on both `.gart` and `.gmeta`, and the details
panel `.ginfo` is `position:absolute; inset:0` — absolutely positioned, therefore
contributing **zero** height. An opened card has no content left in normal flow, so its
intrinsic height collapses to nothing. This is invisible as long as at least one card in the
row is still closed, because grid rows are sized by their tallest item and the closed card
holds the row open. Open *every* card in a row and there is nothing left to hold it: the row
shrinks to the height of the `.ginfo` content alone, and the layout visibly breaks.

**Where.** `index.html` — `.gcard.open` rules (~L835–L837), `.ginfo` (~L833–L834).

**Fix.** `[Done]` — `.gcard.open .gart, .gcard.open .gmeta` now hide with `visibility:hidden`
instead of `display:none`. Both boxes stay in flow at full size, so an open card is exactly as
tall as a closed one and opening or closing never moves the layout — the row cannot collapse
because there is always something holding its height, whether or not any card is still closed.

*Verified in Chromium at 1600px:* opening all six cards of a row left every height at 186px.
Before the fix the same row went 186px → 2px.

---

## 13 — Opened cards revert after scrolling away and back

**Said**:

> In grid view — if cards are opened and you scroll down some time and then come back up,
> some of those cards are reset/reversed back to original. That should not be the case — the
> selected (turned over) cards should remain the same no matter if the user is scrolling
> down or not.

**Problem.** Open/closed is stored **only** as a CSS class on a DOM node, and that node does
not survive. Scrolling down trips the infinite-scroll `IntersectionObserver`, which bumps
`state.pagesShown` and calls `render()`; `render()` rebuilds the grid with
`$("#gridview").innerHTML = games.map(gridCardHTML).join("")` — a complete replacement of
every card, including the ones already on screen and opened. The freshly generated markup
has no `open` class, so every opened card silently reverts. Any other re-render (filter
change, sort change, search) does the same. The state was never persisted anywhere it could
be restored from.

**Where.** `index.html` — `render()` grid branch (~L1978), `ensurePageObserver()`
(~L2194–L2210), open toggle (~L3650, ~L3653).

**Fix.** `[Done]` — open/closed moved out of the DOM and into `state.openCards`, a Set of
appids. `gridCardHTML()` re-applies the `open` class from that Set on every build, so a card
comes back open no matter how many times the grid is thrown away and rebuilt. Both toggles
(click and keyboard) and the Close button now go through one `setCardOpen(card, open)` helper
that writes the class *and* the Set together, so the two can't drift apart. This also covers
re-renders from filter, sort and search changes, which dropped the state for the same reason.

*Verified in Chromium:* six cards opened, scrolled down until pagination had grown the grid
from 100 to 1300 cards, scrolled back — all six still open. Before the fix all six had
reverted.

**Known sibling, not fixed here.** The 18+ art reveal (`.gcard.adult.revealed`) is stored the
same DOM-only way and is lost on exactly the same re-render — a revealed card re-blurs after
scrolling. Same cause, same shape of fix (a Set of revealed appids); left out because it is a
separate report. Raise it and it's a small change.

---

# Round 2 — grid, sort and tooltip pass

A second sitting on the live site, this time driving the Grid view and the sort bar rather
than reading the page cold. Same capture rules as above: the comment as said, then the
problem, where it lives, and a proposed direction. Nothing in this
round shipped in the same sitting; each `Fix` records the option that was chosen and what
verifying it showed.

---

## 14 — Discount badge is the smallest type on the card

**Said**:

> I'd give those sale badges either the same font size and weight as the rating, or a
> different font.

> Update discount font for better readability, also check other fonts to avoid any shit
> fonts — we need clear, easy-reading digital fonts.

**Problem.** The `-60%` badge is the one number on a grid card that sits **on top of the box
art** — the busiest, lowest-contrast surface on the page — and it is set smaller than every
number that sits on the calm dark panel below it:

| Element | Size | Weight |
|---|---|---|
| `.gdisc` — discount badge, over the art | **12px** | 700 |
| `.grate` — review score, on the panel | 13px | 600 |
| `.gq` — QTPD, on the panel | 13px | 600 |
| `.disc` — the same badge in the table | 13px | 700 |

So the badge is not only the smallest thing on the card, it is smaller than *its own
counterpart in the table view*. The 700 weight was doing the work that size should have been
doing, which is why it reads as dense rather than loud.

The wider "check other fonts" ask: the page runs two families, **IBM Plex Sans** (UI text)
and **IBM Plex Mono** (every figure). There is also a long tail of very small type: 14 rules
at `11px`, 2 at `10px`, 32 at `12px`; the 10px ones (`body.layout-card tbody td::before`, the
mobile column labels) are the genuinely marginal ones.

> **Correction — this paragraph originally claimed "both are clear digital faces with
> unambiguous `0/O`… there is no bad font on the page to remove." That was asserted without
> inspecting a single glyph, and it was wrong.** See the second round on this item below.

**Where.** `index.html` — `.gdisc` (L905), `.grate` (L923), `.gq` (L913), `.disc` (L600);
the small-type tail at L1072 and L1117.

**Fix — pending review.** Three options, not mutually exclusive:

- **(a) Match the rating.** `.gdisc` → `font-size:13px; font-weight:600`, i.e. identical to
  `.grate` and `.gq`. One card, one type scale. *Recommended* — it is what was asked for,
  it is two values, and it makes the card internally consistent.
- **(b) Match, and let it carry over the art.** (a) plus `letter-spacing:.01em`,
  `font-variant-numeric:tabular-nums` (so `-5%` and `-75%` don't jitter between cards), and
  a `box-shadow:0 1px 6px #0008` so the coral chip separates from bright key art.
  *Recommended alongside (a).*
- **(c) A different family for figures.** Rejected unless asked for again: adding a third
  webfont costs a network round-trip on first paint for one badge, and Plex Mono is already
  a digital-clear face. If the badge still doesn't pop after (a)+(b), the lever is size and
  the chip, not a new typeface.
- **(d) Raise the 10px floor** — the mobile `td::before` column labels to 11px. Separate,
  small, safe.

**Shipped, first pass:** (a) + (b) + (d). The longest mobile label ("Sale ends") still fits
its 72px gutter at 11px, so nothing reflowed.

---

### 14b — …and the mono font draws a dot inside its zero

**Said**:

> Fix this shit for real this time — the font used for discount have a dot in null character
> — what the fuck is that?! Pick a normal font that is visible and understandable with normal
> characters where in null 0 there is no shit in it.

**Problem.** Correct, and option (c) above was rejected on a claim that had never been
checked. Inspecting the actual font binary:

| | contours in `0` | contours in `O` |
|---|---|---|
| IBM Plex Mono | **3** — ring, counter, **and a dot** | 2 |

Plex Mono draws a dot inside its zero. That is a deliberate coding-font convention for
telling `0` from `O` in source code, and it is clutter on a page whose entire job is showing
numbers — `-60%`, `1080h`, `$10.00`, `43,201`, `100%` all carry it.

It also **cannot be turned off in CSS.** Plex Mono's GSUB feature list is exactly
`ccmp, dnom, frac, numr` — no `zero` feature, no stylistic sets. The dotted zero is the only
zero the font has, so the family had to change.

**Survey.** Nineteen candidates checked by counting contours on `0` against `O`. Almost every
monospace face marks its zero — that is the point of a coding font. Only four came back
plain: **IBM Plex Sans**, **Inter**, **Azeret Mono**, **Chivo Mono**.

The two sans faces were then rejected on a *different* legibility test: in both Plex Sans and
Inter, lowercase `l` and capital `I` are the identical bar. Trading an ambiguous `0/O` for an
ambiguous `l/I` is not a fix on a data-dense page.

**Fix.** `--mono` → **Chivo Mono**. Plain zero, `1 / l / I` stay distinct, all three weights
(400/500/600) served, and — the deciding factor — it is a **metric drop-in**:

| | advance/em | x-height | cap height |
|---|---|---|---|
| IBM Plex Mono | 0.600 | 0.516 | 0.698 |
| **Chivo Mono** | **0.600** | 0.511 | 0.686 |
| Azeret Mono | 0.650 ✗ | 0.544 | 0.698 |

Azeret is 8.3% wider and would have pushed every table column. Chivo changes nothing: measured
in Chromium against the live page, `Playtime · ▲/▼` at 12px is **101.16px in both fonts**,
ten digits at 13px are **78.28px in both**, `$1,234.56` is **70.45px in both**, with no clipped
headers and no table overflow under either. Every hard-coded px width on the page was tuned
against Plex Mono's metrics and keeps working untouched.

**Shipped: the swap, applied globally to `--mono`** rather than to the discount badge alone —
the dotted zero was in every number on the site, not just that one chip.

---

## 15 — The Video preview toggle does nothing in Grid view

**Said**:

> If this only applies to Table view, maybe hide it when another view is selected? Otherwise
> you click it while in Grid view and wait for the miracle. And nothing happens.

**Problem.** Confirmed, and it is exactly as described. The hover preview is bound to
`.thumb` — the small capsule image in a **table/card row**. A grid card has no `.thumb`; its
art is `.gart > img.gcap`, which nothing in `armMedia()` ever matches. So in Grid view the
`Preview / Video` switch is a live, pressable, tooltipped control that changes a
`localStorage` key and produces no observable effect whatsoever. Worse than a missing
feature: it is a control that lies.

**Where.** `index.html` — toggle markup (L1530–L1539), `.thumb .pop` CSS (L496–L540),
`armMedia()` / `stopMedia()` (~L2376–L2600), `gridCardHTML()` (L3045–L3100).

**Fix — pending review.** Two directions:

- **(a) Hide it in Grid.** `body.grid-view` hides the `Preview` label and its `.seg`, the
  way `#pagesize` is already hidden on card layouts (L1138). Two CSS lines, zero risk,
  ships today. The switch reappears intact on Table/Card, and the saved preference is
  untouched. *Recommended as the immediate fix.*
- **(b) Make it true — hover preview on grid cards.** The machinery is view-agnostic
  already; what it needs is a second mount point. Give `.gart` the same `.pop` panel and
  point `armMedia()` at `.thumb, .gart`. This is the better product answer — box art *is*
  the thing you want to see move — but it is a real change: the pop is `512×288` fixed and
  would need positioning against a card rather than a row, and the 18+ blur gate has to be
  respected on the card path too. *Recommended as a follow-up, not bundled with (a).*

**Shipped: (b), not (a).** Hiding the switch was the safe answer to the wrong question — Grid
is the view that survives on a phone, so the feature belonged there most. The video /
screenshot / pips CSS moved off `.thumb .pop` onto a `.mediabox` class worn by both the
floating panel and a card's `.gart`, and the clip plays **in place** inside the art rather
than in a popup: a card's art is already 220px+ at capsule ratio, and a 512px panel over it
would bury three neighbours — playing in place is also what Steam's own store grid does, so
the gesture needs no explaining.

Two gestures over one code path: hover-dwell on pointer devices, tap-to-play on touch (tap
again to stop). The card therefore has two zones — art = watch, everything else = flip — and
a ▶ badge marks the split. `.hastrailer` is the contract the tap handler checks, so a card
with no clip stays a plain flip target instead of eating the tap.

*Three lifecycle cases had to be closed, all verified in Chromium at 414px with touch
emulation:* flipping a card stops a clip playing behind the details face; a grid re-render
stops one before `innerHTML` discards its element, which would otherwise strand a detached
`<video>` streaming; and switching Video off re-renders so the badges go with it. Table-view
hover preview re-tested unchanged.

---

## 16 — Titles with a leading space sort to the very top

**Said**:

> Interesting. Everything that ends up next to the symbols apparently has a space put before
> the name, which is why they show first. You could probably rig something so only titles
> starting with a letter come first (Latin → Cyrillic → Japanese etc.) and dump the rest at
> the end. But dunno. Not sure it's worth the hassle.

**Problem.** Correct diagnosis. Sorting by Name is
`dir * a.title.localeCompare(b.title)` on the **raw** title, and Steam ships titles with
leading whitespace. Measured against the current dataset (127,226 games):

- **22** titles begin with whitespace — ` Fieldrunners 2`, ` Wanba Warriors`,
  ` Virtua Fighter 5 R.E.V.O. World Stage`, ` Promise with My Sister`, …
- **295** titles begin with a non-alphanumeric character — `//SNOWFLAKE TATTOO//`,
  `#KILLALLZOMBIES`, `[the Sequence]`, `¡Zombies!`, `🔴 Circles`, …

Twenty-two rows is a small defect with an outsized effect, because they occupy the entire
**first screen** of an A→Z sort. The first thing a user sees when they sort by name is a
page of games that look like they were sorted by nothing.

**Where.** `index.html` — `visibleGames()` sort comparator (L2028–L2036).

**Fix — pending review.**

- **(a) Trim.** Sort on `title.trim()`. Two characters of code, fixes 22 of the 22 rows the
  complaint is actually about. *Recommended — this alone closes the report.*
- **(b) Trim + letters-before-symbols.** A one-line bucket in front of the compare: a title
  whose first character is a letter or digit sorts before one that isn't. This handles the
  remaining 295 (`//`, `#`, `[`, `¡`, emoji) without needing any script-ordering table —
  `isalnum`-style classification is script-agnostic, so Latin, Cyrillic and Japanese all
  land in the same "real letter" bucket and then order among themselves by the collator, in
  that natural order, for free. **The "Latin → Cyrillic → Japanese" ordering asked for is
  what a default collator already does** — no per-script list to write. *Recommended: it is
  ~4 lines and answers the whole idea, not just the visible symptom.*
- **(c) Also fix the collation while we're in here.** Replace `String.localeCompare` with a
  single hoisted `Intl.Collator(undefined, {numeric:true, sensitivity:"base"})`. Two wins:
  `Portal 2` sorts after `Portal`, not after `Portal 10`; and one reused collator is
  markedly faster than 127k `localeCompare` calls, which is a real cost on the "all games"
  name sort. *Recommended.*

Cheap enough that the "not sure it's worth the hassle" reservation doesn't apply — (a)+(b)+(c)
together are under ten lines in one function.

**Shipped: (a) + (b) + (c),** plus one case the survey above missed — eight titles open with a
zero-width space or joiner (General_Category `Cf`), which `.trim()` does not touch, so
`Triple Fantasy` looked like it started with T and sorted among the symbols. The sort key
strips those too.

*Verified over the full 80,827-row result set:* A→Z now opens `0.5%`, `0.0035％`, `0°N 0°W`,
`0Gravity`, `0RBITALIS` and closes on the symbol bucket (`🚀 Human Rocket Person`, `$1 Ride`,
`€100`); the 22 space-prefixed titles now sit at their real alphabetical positions, the first
at index 2,817 rather than index 0. Z→A keeps the symbol bucket at the end rather than
flipping it to the front.

---

## 17 — "Review score" sort looks unsorted in Grid view

**Said**:

> Looks like this doesn't really work. Sorted by review score (visible in corner) and it's not
> sorted by it at all.

**Problem.** The sort is working. The **card is showing a different number than the one
being sorted on**, which is worse than a broken sort, because it makes correct output look
broken.

`state.ratingSource` defaults to `"recent"`, so sorting by Review score sorts on the
**30-day** score (`recent_pct`). `gridCardHTML()` prints `g.rating_pct` — the **all-time**
score — with a tooltip that says "Steam review score". Reproduced exactly against the games
in the screenshot:

| Card, in the order shown | Printed (all-time) | Sorted on (30-day) |
|---|---|---|
| GUN™ | 92% | **100%** (18 reviews) |
| Peggle™ Nights | 97% | **100%** (51) |
| Trackmania United Forever | 95% | **100%** (17) |
| X-COM: UFO Defense | 95% | **100%** (11) |
| Brothers in Arms: Hell's Highway™ | 91% | **100%** (14) |
| Oddworld: Abe's Exoddus® | 95% | **100%** (13) |
| Sid Meier's Civilization IV: Colonization | 87% | **100%** (13) |

Perfectly sorted, descending, on a column the card never displays. The table view does not
have this bug — it prints whichever value the toggle selects. Grid inherited the number but
not the toggle. `Review count` has the identical mismatch (`review_count` printed,
`recent_count` sorted).

**Where.** `index.html` — `ratingVal` / `countVal` (L2013–L2025), `gridCardHTML()` rating
line (L3070–L3072), All-time/30-day toggle (filters panel, `state.ratingSource`).

**Fix — pending review.**

- **(a) Print what we sort.** The card's `%` follows `state.ratingSource`, with a small
  `30d` / `all` marker next to it so the number is self-describing. *Recommended — this is
  the actual fix; everything else is a variation on it.*
- **(b) Put the toggle where Grid users can reach it.** The All-time / 30-day switch lives
  in the filter panel; a Grid user sorting from the grid sort bar never opens it. Surface it
  in `#resultBar` next to the legend, or as a click on the `30d`/`all` marker itself.
  *Recommended alongside (a).*
- **(c) Separate observation, decide separately.** Even fixed, the top of a 30-day sort is
  *100% of 11 reviews*. That is the correct value and a nearly useless ranking. The table
  survives it because it prints the count beside the score; a grid card would too, once (a)
  lands. A stronger option is a minimum sample for the 30-day sort (fall back to all-time
  below, say, 10 recent reviews) — but that changes a live ranking rule, so it is **not**
  bundled here. Raise it as its own item if wanted.

**Shipped: (a) + (b). (c) was explicitly declined** — "don't limit the review count". The
ranking is unchanged; only its legibility is. Both periods print, stacked, each tagged `30d` /
`all` with its own tooltip, and the row the sort *actually used* is tagged in gold. "Actually
used" is the subtle part: `ratingVal()` falls back to all-time when a game has no 30-day
score, so a card whose `30d` cell is a dash gold-tags `all` instead — tagging the dash would
have pointed at the wrong number, which is the very confusion being fixed.

*Costs no layout:* the stack's line-height is tightened to 1.15 so two 13px rows fit inside
the 2.5em the `.gname` min-height already holds open for a two-line title. Measured in
Chromium, every card in a row is 187px before and after, open or closed.

---

## 18 — Grid sorts by fields the card never shows

**Said**:

> So in Grid view we have sorting options with parameters that this view doesn't even
> display..? Need to be adjusted for this view.

**Problem.** The grid sort bar is built from the full `SORT_LABELS` map — all thirteen
fields — while a grid card shows exactly three: QTPD, review score, title (plus price and
length behind the flip). Sort by **Review count**, **Trend**, **Weighted**, **Updated**,
**Playtime**, **HLTB** or **Sale ends** and the grid reorders itself by a quantity that is
nowhere on screen. The user is asked to trust an ordering they cannot verify — which is the
same trust problem as #17, arriving from the other direction.

**Where.** `index.html` — `buildGridSortButtons()` (L3615–L3625), `SORT_LABELS` (L3203),
`gridCardHTML()` (L3045–L3100).

**Fix — pending review.**

- **(a) Trim the list.** Restrict the grid sort bar to fields the card shows: QTPD, Name,
  Review score, Price, Discount, Length. Honest, and one line. But it *removes* working
  functionality — "cheapest first among 4k+ reviews" is a real thing to want — and it
  desyncs Grid from the table and the mobile `<select>`, which keep all thirteen.
- **(b) Show the sort key on the card.** Keep all thirteen fields; when the active sort is
  a field the card doesn't already display, the card grows one small line: `Reviews 43,201`
  / `Trend ▲ +4` / `Updated 3d ago` / `Playtime 12.4h`. It disappears again when sorting by
  QTPD or Name, which the card already shows. *Recommended* — it makes every sort verifiable
  instead of deleting the ones that aren't, and it costs one `<div>` and a small
  `key → (game) => text` map. It also has to reserve its line height whether or not a given
  card has a value, or rows go ragged again (the problem item #10 fixed).
- **(c) Both.** Do (b), and drop only `Sale ends` from the grid bar — it is the one field
  that is meaningless for the ~90% of cards not on sale.

**Shipped: (b).** No sort was removed. The card's QTPD line carries the active sort's value on
its free right-hand half, and stays empty for sorts the card already shows. Each value keeps a
dimmed unit — `43,201 rev`, `1281h len`, `1108h ▲play` — rather than the bare number
originally suggested: an unlabelled figure on a card is the same problem item #9 was raised
about, and *both* length fields render as `Nh`, so the unit is the only thing telling Length
from Playtime.

---

## 19 — The card shows HLTB "Length", the sparsest field we have

**Said**:

> What is 'Playtime'? The same as 'Length'? If so, something's not working there.

> So we can sort by playtime (upvote or downvote) but the card shows HLTB length. Maybe
> worth swapping and showing the playtime — as that value is ~100% there, where HLTB is
> just a small portion of games.

**Problem.** They are two different fields, and nothing on the card says so:

- **Length** = HowLongToBeat hours (`hoursFor()`), community-reported time to finish. This
  is the numerator of QTPD.
- **Playtime** = median hours actually played by Steam reviewers, split by whether they
  recommended the game (`pt_up` / `pt_down`).

The instinct about coverage is right, and the gap is even wider than "a small portion".
Measured on the current data (127,226 games):

| Field | Games with a value | Coverage |
|---|---|---|
| Playtime (`pt_up`/`pt_down`) | 80,846 | **63.5%** |
| HLTB — any `main` value | 34,021 | 26.7% |
| HLTB — *real*, not estimated (the default, `hltbQuality:"real"`) | ~15,002 | **11.8%** |

So on the default settings the card's `Length` row is blank for roughly **seven cards in
eight**, while a Playtime figure exists for nearly two in three. That is also why so many
cards read `QTPD —`: no length, no score.

But a straight swap is wrong. `Length` is not decoration — it is the number QTPD divides
price by, so removing it makes the headline metric unexplainable on the card.

**Where.** `index.html` — `hoursFor()` / `realHours()` (L1742–L1752), grid `Length` row
(L3095), playtime fields (L4476), Playtime column header (L1564).

**Fix — pending review.**

- **(a) Label them apart.** `Length (HLTB)` and `Playtime (median)`, each with a tooltip
  saying what it measures. Answers "is it the same thing?" on its own. *Recommended
  minimum.*
- **(b) Show both on the flipped card.** Keep `Length` (it explains QTPD) and add a
  `Playtime` row beneath it. Two rows, ~63% and ~12% filled — the card stops being empty for
  most games. *Recommended.*
- **(c) Fall back.** When `Length` is `—`, print the playtime figure in its place, marked as
  playtime. Denser, but it hides the fact that QTPD is unscored *because* the length is
  missing. Prefer (b).
- **(d) Straight swap, as suggested.** Not recommended, for the reason above — but it is a
  one-line change if the coverage argument is judged to outweigh explaining QTPD.

**Shipped: (a) + (b), on the opened face only** — the front of the card stays a three-figure
summary. `Length HLTB` keeps the top slot because it is what QTPD divides price by;
`Playtime median` sits beneath it. The Playtime and HLTB column headers were rewritten to say
outright that one is time-to-finish and the other time-actually-played.

---

## 20 — "HLTB" is never expanded anywhere

**Said**:

> I, for one, have no idea what HLTB is. :D You could in theory put the expansion in the
> tooltip.

> Yes, the tooltips are absolutely not helping here.

**Problem.** The grid sort buttons are generated with a placeholder tooltip:
`b.title = "Sort by " + lbl`. So hovering `HLTB` says **"Sort by HLTB"** — the acronym
restated, and nothing else. The word *HowLongToBeat* does not appear on the page. The table
header has a real tooltip but still never expands it ("HLTB — Main / Main+Extras /
Completionist hours…"). Same emptiness on every other grid sort button: "Sort by Weighted",
"Sort by Trend", "Sort by Updated".

**Where.** `index.html` — `buildGridSortButtons()` (L3621), HLTB `<th>` (L1565), Playtime
`<th>` (L1564).

**Fix — pending review.** Add a `SORT_TIPS` map keyed the same as `SORT_LABELS`, and have
`buildGridSortButtons()` use `SORT_TIPS[k] || "Sort by " + lbl`. The table already has good
prose for most of these — the tips get written once and reused by the grid bar, the mobile
`<select>` and the summary chip. HLTB's becomes: *"HLTB — HowLongToBeat, community-reported
hours to finish a game. Main story / +Extras / Completionist, with the average below."*
*Recommended*; it is the one fix in this round that also improves Table and mobile.

**Shipped as proposed.** All thirteen sort fields now carry a real explanation instead of
`"Sort by " + label`.

---

## 21 — The header count strip has no tooltip

**Said**, pointing at `80637 / 126910 games · 43m ago · USD`:

> Looks like a tooltip might be missing here.

**Problem.** Four separate facts crammed into one unlabelled mono line, none explained:
what the two numbers are (matching your filters / total in the dataset), what `43m ago`
timestamps (when the scrape that produced this data ran, not the page load), and why `USD`
is asserted (every price on the page is USD; there is no currency switch). `#meta` is built
by `renderMeta()` with no `title` anywhere on it.

**Where.** `index.html` — `renderMeta()` (L3434–L3443), `.meta` CSS (L66).

**Fix — pending review.** Wrap the three groups in their own spans with their own tooltips
rather than one blob over the whole strip — the existing tooltip engine picks up any
`[title]` and gives it the help cursor automatically, so it is markup only:

- `80637 / 126910 games` → *"Results — games matching your current filters, out of every
  game in the dataset."*
- `43m ago` → *"Data age — how long since the scraper last refreshed prices and reviews.
  Updates run continuously."*
- `USD` → *"Currency — all prices are US dollars from the Steam US store. There is no
  currency conversion."*

*Recommended.* Small, and it retires three "dafaq is this" moments in one line.

**Shipped as proposed** — three separate tooltips, one per group, rather than one blob over
the whole strip.

---

## 22 — Truncated titles can't be read in full

**Said**:

> Text cut is by design, but we could add a tooltip with full text at least on mouse over,
> so everything is readable — which it was not before.

**Problem.** Both title elements clamp to two lines with `-webkit-line-clamp:2` and neither
carries a `title` attribute, so a clipped name is simply unreadable — `The Binding of
Isaac: Rebirt…` with no way to see the rest short of opening the store page. Affects
`.gtitle` (table/card rows) and `.gname` (grid cards). The grid card *does* repeat the full
title on its flipped `.gi-title` face, but that costs a click and is not discoverable as
"this is how I read the name".

**Where.** `index.html` — `.gtitle` (L561–L562) and its markup (L2305); `.gname` (L932–L934)
and its markup (L3086).

**Fix — pending review.** Add `title="${esc(g.title)}"` to both. The tooltip engine renders
it in the same styled box as everything else and applies the help cursor for free. Two
notes:

- Set it **unconditionally**, not only when truncated — measuring truncation means reading
  `scrollHeight` per card, which is a forced layout on up to 2,000 nodes per render. A
  redundant tooltip on a short title costs nothing.
- `.gtitle` is an `<a>` to Steam, so it keeps the pointer cursor; the `[title]` help-cursor
  rule needs the same exemption `.util-btn`/`.seg button` already have (L115–L118).

*Recommended.*

**Shipped as proposed,** including the cursor exemption — `.gname` needed it too, since the
grid card is itself a click target.

---

## 23 — "Per page" only lets the user hurt themselves

**Said**:

> Is this even needed, given the content loads automatically as soon as you scroll to the
> bottom? Some numpty will set 2000 and then get annoyed that everything is slow.

> True — reduce it to 66 by default and keep it that way.

**Problem.** `PER PAGE 100 / 500 / 2000` is a performance footgun wearing the costume of a
feature. Infinite scroll already loads the next page on reaching the bottom
(`ensurePageObserver`), so the control's only real effect is **how much work one render
does**: `render()` rebuilds every visible row or card from scratch, so `2000` means
building two thousand DOM subtrees on every filter keystroke, every sort click, every tag
cycle. There is no user goal that "2000" serves and infinite scroll doesn't — the genuine
"give me everything at once" path is the CSV export, which already exists two buttons away.
It is also already hidden on card layouts and on narrow grid (L1138), i.e. it has been
half-retired once already.

**Where.** `index.html` — markup (L1243–L1247), `.pagesize` CSS (L167–L173), `state.pageSize`
default (L1663), URL write (L3782), URL read/validate (L3841–L3842), click binding
(L3919–L3922), pagination (L2091–L2092).

**Fix — pending review.**

- **(a) Remove the control; fix the page at 66.** `state.pageSize:66`, delete the three
  buttons and the label, keep `?per=` honoured on read so existing shared links don't break
  (validate as a number in 1…2000 rather than the current hard-coded `[100,500,2000]`
  whitelist, which would otherwise reject `66` itself). *Recommended* — it is what was
  asked for, and it removes a control rather than tuning one.
- **(b) Keep the control, default 66, drop 2000.** `66 / 200` only. Safer if anyone is
  attached to the buttons, but it keeps a control whose entire purpose is now internal.
- **(c) Worth confirming:** 66 is a slightly odd number against the grid, which is
  `auto-fill minmax(220px, 1fr)` — at a 1600px window that is 7 columns, so 66 ends a page
  mid-row. Harmless with infinite scroll (the next page fills it in), and 66 rows is a fine
  table page. Flagging it only so the number is a decision and not a surprise.

**Shipped: (a), at 66,** confirmed. The control is gone and `PAGE_SIZE` is a constant. `?per=`
survives so links shared before the removal still resolve — its validator was the
`[100, 500, 2000]` whitelist noted above, which would have rejected the new default, and is
now a range check.

---

# Round 3 — narrow screens, navigation and filter details

A third batch of screenshots, several from windows narrower than the original 1028px. Each
item was reproduced in headless Chromium before it was fixed — on a 390px phone viewport,
at 1028px, and at the 1300–1440px laptop widths — and re-measured after. All twelve shipped
together, with the options chosen at review noted per item.

---

## 24 — "Close" is cut off on the opened grid card

**Said**, on an opened card whose fourth button is half outside the card:

> Hi! Let me show you a bit more. It looks like "Close" isn't fully visible on every screen.

**Problem.** The opened card's action row was four labelled buttons (`Steam ↗`, `Reviews ⇩`,
`≈ Similar`, `Close ✕`), each `flex:1` but never narrower than its own text. Together they
need ~250px; a desktop grid card is ~220–240px and a two-column phone card ~154–170px, so
the row overflowed on every common width. Nothing clipped it visibly: `.ginfo` has
`overflow-y:auto`, and a non-visible `overflow-y` silently turns `overflow-x` to `auto` as
well, so the overlay scrolled sideways behind a scrollbar thin enough to miss (item 26). On
a 390px phone `Similar` was half cut too, and `Close` entirely off the card.

**Where.** `index.html` — `.ginfo` (L1288), `.gi-close` (L1316), `.gi-actions` (L1331) and the
two `@container` blocks after it (L1342); markup in `gridCardHTML()` (L5311).

**Fix — shipped as (A1).**

- `Close` became a small ✕ in the overlay's top-right corner. Tapping anywhere on an opened
  card already closes it, so the button only needs to be findable.
- The remaining three actions are equal tiles, icon over label, on a
  `repeat(3, minmax(0, 1fr))` grid — the row is exactly the card's width, whatever the text.
- `overflow-x:hidden` on `.ginfo`, so there is never a sideways scroll to discover.
- The card is a size container (`container-type:inline-size`), so the CARD's width, not the
  viewport's, decides the compact sizes: under 200px the title clamps to one line and the
  spacing tightens; under 164px (a 360px phone) the tile labels drop to 10px.

Rejected at review: a 2×2 button block (the opened card would grow taller than the closed
one, which item 12 made sure never happens) and one card per row on phones (half the games
per screen). Measured after: zero horizontal overflow and all three labels untruncated at
360, 390, 1028 and 1600px.

---

## 25 — Back does not return after Find similar

**Said**:

> A pretty weird user experience: after clicking "Similar" I can't get back with the Back
> button.

**Problem.** `syncURL()` only ever called `history.replaceState()`. The URL stayed shareable,
but the page never created a history entry, so Back had nothing on this page to return to and
left the site instead. Preset shelves and picking a title from the search list had the same
problem: each one replaces the whole question being asked, and none of them could be undone
with Back.

**Where.** `index.html` — `pushStep()` (L6183), next to `syncURL()`; called from
`findSimilar()` (L4817), `pickTitle()` (L4939) and `applyPreset()` (L5819); restore in the
`popstate` handler (L6394).

**Fix — shipped as (D1).** A *jump* — Find similar, a preset shelf, a picked search title —
now opens a new history entry before it changes the filters, and `syncURL()` writes the
jump's URL into it. Ordinary edits (typing, steppers, chips, sort) still rewrite the current
entry, so Back doesn't replay every keystroke. The entry being left records its scroll offset
and how many pages were loaded; on Back the handler regrows the list to that length and
scrolls back, landing on the same card. Notes:

- It scrolls twice. On a phone the first scroll past 24px collapses the sticky nav, which
  moves the content up by the nav's height, and scroll anchoring then left the view ~100px
  off. The second scroll runs after the nav has settled.
- `history.scrollRestoration` is `manual`. The browser's own restore would apply the offset
  before the list had regrown.
- Forward onto a jump has no saved offset, so it starts at the top, as the jump itself did.
  `applyPreset()`'s synthetic `popstate` (`isTrusted` false) still leaves the scroll alone,
  and now resets paging to page one like any other filter change.

Verified on a 390px viewport: scrolled seven pages deep, Find similar, then Back → same URL,
same seven pages, same card at the same offset.

---

## 26 — The opened card's scrollbar goes unnoticed

**Said**, after finding the scrollbar under the card:

> Ah, there's a scrollbar at the bottom. I didn't even notice it 😄 Dunno, maybe I'd
> consider an option where you just drag those little blocks to see everything.

**Problem.** The same overflow as item 24, seen from the other side: the only way to reach
the cut-off button was an inner horizontal scrollbar.

**Fix.** `[Done]` with item 24: everything fits the card's width, and `overflow-x:hidden`
means there is nothing to scroll to or drag.

---

## 27 — The opened card's title is hidden under the rows

**Said**, on "Towers of Altrac - Epic Defense Battles", whose second line runs under `Price`:

> Part of the title is hidden behind the rest of the content. Could truncate it with "…".

**Problem.** `.gi-title` is clamped with `overflow:hidden`, which drops its flex `min-height`
to 0. In a short card it was the flex item that gave way: squashed below its two lines (the
second line drawn under the Price row), clipped at the top ("Black Desert"), and on a
two-column phone grid squashed to **0px** — no title at all.

**Where.** `index.html` — `.ginfo > *{flex:none}` and `.gi-title` (just below `.ginfo`, L1288).

**Fix.** `[Done]` Nothing in the overlay's column may shrink. The title keeps its two-line
clamp, which ends in "…", and on cards under 200px it clamps to one line. Row labels stay on
one line too (`Playtime median` used to wrap onto a second line on narrow cards, which was the
extra height that pushed the buttons off the bottom).

---

## 28 — The table's top-left corner looks cut off

**Said** (owner, circling the top-left corner of the table, where the GAME header meets the
card edge):

> The corners are cut off or something.

**Problem.** `.tablecard` has a 14px radius, but nothing inside it is clipped to that radius.
`overflow:hidden` would make the card a scroll container, and the sticky header would stop
sticking. The header's opaque square fill therefore covered the card's rounded top-left
border, which read as a notch.

**Where.** `index.html` — after `tbody tr:hover` (L676–L683).

**Fix.** `[Done]` The two square fills that reach the card's corners carry the curve
themselves: `thead` gets a 13px top radius and the last row a 13px bottom radius (13px = the
card's 14px less its border). `overflow:clip` was considered and rejected: it would keep the
header sticky, but it would also cut off the sort menu that drops out below a short list.

---

## 29 — Long table titles are cut mid-letter

**Said**, on "Encased: A Sci-Fi Post-Apocalyptic RPG" wrapping into the next column:

> Maybe this also needs truncating with "..".

(The same screenshot set shows "Pathfinder: Wratł" — cut through the letter.)

**Problem.** The Game column is `minmax(268px, 2.2fr)`, and on every window from 1280px to about
1520px it sits at that 268px floor: the fixed-ceiling columns take the free space before any
`fr` track grows. The 150px art (180px with Tags folded), the hide button and the gaps left
the title ~52px, but `.gmeta` has a 136px floor, so the title block ran ~80px past the cell.
The cell's `overflow:hidden` then cut it mid-letter. There was no ellipsis, because the two-line
clamp never saw the edge. Measured before the fix: every one of the first 40 rows overflowed
at 1440px.

**Where.** `index.html` — `body:not(.layout-card) .gamecell …` rules after `.gmeta` (L846).

**Fix — shipped as (B1).** In the table (not the card layout) the art gives way first: it
shrinks with `flex-shrink:4` down to 96px, keeping its ratio. The title takes what is left,
and its clamp ends in "…". `overflow-wrap:anywhere` breaks a single over-long word instead of
clipping it. Rejected: raising the column's floor, which would push the switch to the card
layout ~80px wider. After: zero overflowing rows at 1280, 1290 and 1366px.

---

## 30 — The search dropdown looks cut off

**Said**, typing "fall":

> Should the dropdown show more results, or does it just look like that? Right now it seems
> like something is cut off.

> I believe if we search just 1 word 'fall' there should be way more titles showing up than
> just two? Fallout? Falldown? Humans Fall Flat?

**Problem.** Two causes, and the tester's first reading was right:

1. **Clipped.** The list did render eight rows, but `.barwrap` (the rounded box around the
   search bar) is `overflow:hidden` on desktop, so every row past the bar's bottom edge was
   cut off. Only "FALL" and "FALL 坠落之后" were visible. Narrow screens already ran unclipped.
2. **Crowded.** Five games are titled exactly "FALL" or ".fall". Exact matches rank first,
   so they filled all five title slots, and Fallout never made it onto the list. Picking a row
   searches its text, so the five rows all led to the same results.

**Where.** `index.html` — `.barwrap:has(...)` (L105–L108), `.simsug` max-height (L951),
`SIMSUG_JUMP` (L4850) and `oneEach` in `renderSimSug()` (L4906).

**Fix — shipped as (E1).**

- While a suggestion list is open the clip is lifted (`.barwrap:has(.simsug:not([hidden]))`).
  This covers the publisher/developer list too. The "Show filters" strip carries its own
  corner curve, so the un-clipped bar still looks rounded.
- Titles that differ only in case or punctuation collapse to their best-ranked game, in both
  the title rows and the ≈ rows.
- The title group shows up to eight rows (was five), and the list's max height grows to
  `min(70vh, 560px)`, so on a laptop-height window all three groups show without scrolling the list.

"fall" now lists FALL, Fallout 4, Fallout: New Vegas, Human Fall Flat, STAR WARS Jedi: Fallen
Order, Fallout 76, WUCHANG: Fallen Feathers and Fallout Shelter. No row is covered.

---

## 31 — Row hover pokes past the table's rounded corners

**Said**, on the last row of a short list:

> It shows fine at the bottom, but on hover the corners poke out over the rounded edge.

**Problem / Fix.** Same cause as item 28, at the other end of the card: the last row's square
hover fill reached past both lower corners. `[Done]` with item 28: the last row carries the
bottom radius.

---

## 32 — Tag chips in the Showing line touch each other

**Said**, on the tag chips in the collapsed filter summary:

> Don't you want to put at least a couple of pixels of space between the bubbles? 😅 In all
> directions.

**Problem.** `interactedTagChipsHTML()` returns one run of buttons with no whitespace between
them, and at full rail size each chip (~29px) is taller than the line's 26px. Measured: 0px
apart side to side, and 0px between rows once the run wrapped.

**Where.** `index.html` — `.filter-summary .chip` (L1143).

**Fix.** `[Done]` Each chip gets a 3px margin, with its padding trimmed slightly to sit closer
to the `.sumf` filter chips beside it. After: 3px apart side to side, 6px between rows.

---

## 33 — Length max can be set to 0

**Said**, on the Length range with max showing 0:

> Not sure I'd allow choosing zero here. One mistake and there are no results any more.

**Problem.** ▼ on the empty max box ("∞") stepped to 0, and a max of 0 hours matches
nothing, because no game has a Length of 0. One misclick blanked the list.

**Where.** `index.html` — `hoursHandler` (L6617), the stepper handler (L6642), `hmax` in
`loadFromURL()` (L6288).

**Fix — shipped as (G1).** A Length max of 0 now means "no bound", like an empty box:

- ▼ steps down to 1 and then back to empty ("∞").
- ▼ on an empty box stays empty.
- A typed 0 is ignored while typing, so "0.5" still works, and tidies to empty on `change`.
- `hmax=0` in a link is ignored.

Price is untouched: max $0 is a real filter (free games).

---

## 34 — Price type has no hover and looks unselected

**Said**, hovering over All / Full / Sale / Free:

> It seems a bit weird that these have no hover state when none of them is selected.

**Problem.** By default all four buttons were pressed. The pressed fill is quiet, and with
every button wearing it the group read as *nothing* selected. Segment buttons skip the hover
fill when pressed, so none of the four answered the pointer.

**Where.** `index.html` — markup in the Price type field, `syncPriceClassUI()` (L5074), the
click handler (L6515), the `.seg` hover ring (L312).

**Fix — shipped as (H1), a behaviour change.**

- With every type included, only **All** is lit.
- From All, clicking Full, Sale or Free shows *only* that kind. The same click used to
  switch that kind off. Later clicks add or remove kinds as before, and lighting all three
  goes back to All.
- Pressed segment buttons site-wide now answer hover with a ring, not a fill, so the fill
  still marks the selection.
- The Showing line's Price type popover ticks options by the same rule (none ticked when all
  three are included), because it clicks the same buttons: a ticked Free that meant "only
  Free" when clicked would have read as "untick Free".

Found while testing: clicking Full while a Min sale floor had switched it off released the
floor and then toggled Full straight off again. It now brings Full back, which is what the
existing comment said it was for.

---

## 35 — Stale "Showing 66 of 7511" above "No games match"

**Said**, on a screenshot with "Showing 66 of 7511 — scroll for more…" underlined above the
empty-state message:

> Well, I dunno, dunno 😅

**Problem.** `render()` returns early when no games match, before it reaches the code that
hides the paging footer. The previous list's "Showing 66 of 7511 — scroll for more…" stayed
on screen above "No games match these filters."

**Where.** `index.html` — the empty branch of `render()` (L3470).

**Fix.** `[Done]` The empty branch hides `#loadmore` itself.

**Not an item:** the round ‹ › arrows at the edges of two of these screenshots are the chat
app's image-viewer controls, not part of the site.
