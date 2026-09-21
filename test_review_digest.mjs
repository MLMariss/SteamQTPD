// test_review_digest.mjs — end-to-end test for the Review Digest (REVIEW_DIGEST_PLAN.md).
//
// Loads the REAL index.html in Chromium with the JSON data layers deliberately absent, so
// the page falls back to its built-in SAMPLE catalogue, and mocks the Cloudflare Worker
// with synthetic Steam payloads that exercise every compaction path. It asserts on the
// bundle the page actually produces, not on functions in isolation — which is how the two
// bugs in the first pass were caught (74-char ASCII art slipping under the art floor, and
// a test fixture that never reached the truncation path).
//
// Playwright is NOT a dependency of this repo — everything else here is Python — so:
//     npm install playwright
//     node test_review_digest.mjs
// Serves the working copy over localhost; needs no network and no Worker.
//
// The console 404s it reports are expected: they are the 13 absent JSON layers, which is
// exactly what triggers the SAMPLE fallback. Real faults surface as uncaught JS errors.

import { chromium } from "playwright";
import http from "http"; import fs from "fs"; import path from "path";

const ROOT = process.env.RD_ROOT || process.cwd();
const srv = http.createServer((req, res) => {
  const f = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]));
  if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) {
    res.writeHead(404); res.end("nope"); return;                 // JSON layers absent -> SAMPLE fallback
  }
  res.writeHead(200, { "Content-Type": f.endsWith(".md") ? "text/plain" : "text/html" });
  res.end(fs.readFileSync(f));
});
await new Promise(r => srv.listen(8099, r));

// Synthetic Steam payloads exercising every compaction path.
const mk = (i, o = {}) => ({
  recommendationid: String(1000 + i),
  review: o.review ?? `Solid game, runs fine after the patch. Review number ${i} with enough text to be substantive.`,
  voted_up: o.voted_up ?? (i % 4 !== 0),
  votes_up: i,
  timestamp_created: o.ts ?? 1756000000 - i * 3600,
  timestamp_updated: o.updated ? 1756900000 : (o.ts ?? 1756000000 - i * 3600),
  written_during_early_access: !!o.ea,
  received_for_free: !!o.free,
  steam_purchase: o.notSteam ? false : true,
  primarily_steam_deck: !!o.deck,
  author: { playtime_at_review: o.mins ?? (i * 37), playtime_forever: 9999 },
});
const special = [
  mk(1, { ea: true, updated: true }),
  mk(2, { deck: true, voted_up: false, review: "Runs at 25fps on Deck, stutters constantly." }),
  mk(3, { free: true }),
  mk(4, { notSteam: true }),
  mk(5, { review: "COPYPASTA THE PUBLISHER IS GREEDY AND THIS IS PASTED EVERYWHERE OK" }),
  mk(6, { review: "COPYPASTA THE PUBLISHER IS GREEDY AND THIS IS PASTED EVERYWHERE OK" }), // dupe
  mk(7, { review: "█▓▒░ ★彡 ▄▀▄▀▄ ★ ▓▓▓ |___| >>>>>> ##### @@@@ ★彡 ░▒▓█ ▄▀▄▀▄ ★ ▓▓▓ |___| >>>>>>" }), // art
  mk(8, { review: ("This is a genuinely long and detailed prose review that keeps going. " .repeat(20)) }), // truncation
  mk(9, { review: "gg" }),                                                // non-substantive
  mk(10, { review: "[b]Great[/b] see [url=http://x.com]this[/url] and [sailing] stays" }), // bbcode + prose brackets
];
// TOPIC MENTIONS needs text that actually mentions something. The generic filler above
// matches no family, so the table came out with a header and no rows — which asserts
// nothing. Three reviews per family is exactly the floor, so each of these four appears as
// a row and one more removed from any of them would drop it to the "fewer than 3" line.
const topical = [
  mk(300, { review: "Crashes to desktop every time I try to launch it. Black screen, then nothing." }),
  mk(301, { review: "Won't start at all since the update. Fails to launch on two different machines." }),
  mk(302, { review: "Constant crashing in the second act, lost an hour of play each time." }),
  mk(303, { review: "Terrible frame rate in towns, stutters badly and the optimisation is nonexistent." }),
  mk(304, { review: "Drops to 20 fps whenever it rains. Performance is rough on a mid-range card." }),
  mk(305, { review: "Runs poorly for how it looks, choppy in every fight, needs real optimization work." }),
  mk(306, { review: "Requires a Microsoft account to play a single player game. Instant refund." }),
  mk(307, { review: "Can't play offline, it wants an Xbox login before the menu even loads." }),
  mk(308, { review: "Always online for a solo campaign, and the third party account nonsense broke twice." }),
  mk(309, { review: "The battle pass and premium currency ruin what is otherwise a decent game." }),
  mk(310, { review: "Every cosmetic is a microtransaction, and the DLC is priced like a second game." }),
  mk(311, { review: "Pay to win in the worst way, the loot box odds are insulting for a paid title." }),
];
// The fixture used to span ~4 days, so every review fell in one window and the NOW/BEFORE
// split was never exercised. These 60 sit a year back, forcing a real BEFORE side and a
// multi-quarter breakdown.
//
// The total must exceed RD_NOW_MIN (100) or the widened window swallows the whole sample and
// BEFORE comes back empty — which is the trap this fixture fell into at 100 reviews. At 142
// (140 after the dupe and the art are dropped) only 80 are inside 90 days, so the window
// widens to 100, leaving 40 in BEFORE: widening and the split are both exercised at once.
// The busy-game scenario at the bottom of this file covers the opposite branch.
const old = Array.from({ length: 60 }, (_, i) => mk(i + 200, { ts: 1756000000 - (300 + i * 3) * 86400 }));
const page1 = [...special, ...topical, ...Array.from({ length: 60 }, (_, i) => mk(i + 20)), ...old];

const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push("PAGEERROR: " + e.message));
const noise = [];
page.on("console", m => { if (m.type() === "error") noise.push(m.text()); });

await page.route("**/qtpd-reviews.*/**", route => {
  const u = new URL(route.request().url());
  const per = u.searchParams.get("num_per_page");
  const body = per === "0"
    ? { success: 1, query_summary: { total_reviews: u.searchParams.get("language") === "all" ? 983491 : 417281,
        total_positive: u.searchParams.get("language") === "all" ? 849681 : 370832,
        total_negative: u.searchParams.get("language") === "all" ? 133810 : 46449,
        review_score_desc: "Very Positive" }, reviews: [] }
    : { success: 1, query_summary: {}, reviews: page1, cursor: "" };   // empty cursor -> stop after p1
  route.fulfill({ status: 200, contentType: "application/json",
                  headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(body) });
});

await page.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
await page.waitForTimeout(600);

const btns = await page.locator("button.gsub-rev").count();
console.log(`table entry-point buttons rendered: ${btns}`);
if (!btns) throw new Error("no .gsub-rev buttons rendered");

// Grid cards only exist in grid view; the page opens in table view, so switch first.
await page.locator('#viewSwitch [data-view="grid"]').click();
await page.waitForTimeout(500);
const gridBtns = await page.locator("button.gi-rev").count();
console.log(`grid entry-point buttons rendered: ${gridBtns}`);
await page.locator('#viewSwitch [data-view="table"]').click();
await page.waitForTimeout(400);
const label = await page.locator("button.gsub-rev").first().textContent();
console.log(`first button label: ${JSON.stringify(label)}`);
await page.locator("button.gsub-rev").first().click();
await page.waitForSelector("#rdHost.on", { timeout: 4000 });
console.log("modal opened");

// Setup-dialog state, read before anything is clicked (plan §15.3, §15.4). `check` is
// defined further down, so these are collected here and asserted with the rest.
const sizeOpts  = await page.locator("[data-rdsize]").allTextContents();
const sizeOn    = await page.locator("[data-rdsize].on").allTextContents();
const focusOpts = await page.locator("[data-rdfocus]").count();
const focusOn   = await page.locator("[data-rdfocus].on").count();
const goLabel   = (await page.locator("#rdGo").textContent()).trim();
const modeOpts  = await page.locator("[data-rdmode]").allTextContents();
const modeOn    = await page.locator("[data-rdmode].on").allTextContents();
// §23 — two more axes in the same dialog: how the report is RENDERED, and how much a review
// has to say before it counts. Both defaults are load-bearing, and they point opposite ways:
// Markdown is the default because it is what the page has always produced, and the bar is ON
// because the denominator it fixes was wrong in every digest before it.
const outOpts   = await page.locator("[data-rdout]").allTextContents();
const outOn     = await page.locator("[data-rdout].on").allTextContents();
const barOpts   = await page.locator("[data-rdbar]").allTextContents();
const barOn     = await page.locator("[data-rdbar].on").allTextContents();
// §24.2 — the fourth axis. Reach 1 is the default and is the behaviour every other scenario
// in this file assumes, so the ninth scenario at the bottom is the one that exercises a skip.
const reachOpts = await page.locator("[data-rdreach]").allTextContents();
const reachOn   = await page.locator("[data-rdreach].on").allTextContents();
// A focus whose family name does not exist in RD_TOPICS points the model at nothing and
// fails silently — no error, just a focus the bundle cannot answer. Typos are the whole risk.
const badFocusMap = await page.evaluate(() =>
  RD_FOCUS.flatMap(f => f.topics.filter(t => !RD_TOPICS.some(x => x.name === t))));

// The defaults are read above; the bundle below is asserted on the MARKDOWN path at the
// 5-word bar, which is what this scenario was written to cover and what scenario 8 does not.
// §24.1 moved the defaults to HTML and 10 words, so these two clicks are what puts the fixture
// back on the path its assertions describe — including the 6-word [deck] review, which a
// 10-word bar legitimately drops.
await page.locator('[data-rdout="md"]').click();
await page.locator('[data-rdbar="5"]').click();
await page.locator("#rdGo").click();
await page.waitForSelector("#rdOut", { timeout: 15000 });
const bundle = await page.locator("#rdOut").inputValue();
// §18.3 — the handoff row only exists once there is something to hand off, so it is read
// here rather than with the setup-dialog state above.
const aiOpts = await page.locator("[data-rdai]").allTextContents();
// The handoff buttons live in the FOOTER now, beside Copy all, because they are actions and
// not another settings row. The prose that used to sit under them in the body was cut to one
// line; the "why" moved onto each button's own tooltip. So the paste shortcut is asserted on
// the body's one-liner and the .txt escape hatch on the Download button's title.
const aiInFoot = await page.locator("#rdFoot [data-rdai]").count();
const aiHint   = await page.locator("#rdBody .rd-note").first().textContent();
const aiTips   = await page.locator("#rdFoot [data-rdai]").evaluateAll(els => els.map(e => e.title));
const dlTip    = await page.locator("#rdFoot #rdDl").getAttribute("title");
// §25 — the saved file's NAME is half of what the footer buys. Two pulls of one game differ by
// when they were taken and by how many reviews they hold, and both have to be readable off the
// folder listing without opening either file. Read through the naming function rather than by
// driving a real download, so this stays with the other bundle assertions instead of needing a
// downloads directory; the click path is one line and is asserted by #rdDl's presence above.
const txtName  = await page.evaluate(() => rdTxtName(gameOf(rdState.appid), rdState.bundle));
// Every option in the setup dialog has to carry its own explanation now that the paragraphs
// are gone — an untitled pill is a dead end for anyone who does not already know the answer.
const setupUntitled = await page.evaluate(() => {
  rdRenderSetup(gameOf(rdState.appid));
  return [...document.querySelectorAll("#rdBody [data-rdmode],#rdBody [data-rdsize],#rdBody [data-rdlang],#rdBody [data-rdfocus],#rdBody [data-rdout],#rdBody [data-rdbar],#rdBody [data-rdreach]")]
    .filter(b => !b.title || b.title.trim().length < 12).map(b => b.textContent.trim());
});
await browser.close();

console.log("\n================ BUNDLE (first 40 lines) ================");
console.log(bundle.split("\n").slice(0, 40).join("\n"));
console.log("================ (end excerpt) ================\n");

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok:  " : "  FAIL:") + " " + msg); if (!cond) fails++; };
check(gridBtns > 0, `grid card Reviews button rendered (${gridBtns})`);
check(/^=== QTPD REVIEW DIGEST( · prompt \S+)? ===/.test(bundle),
      "title line present, carrying the prompt version when the file declares one");
// §18.5. WHICH GAME, above the instructions rather than 230 lines into them. Both prompts
// copy these two lines into the report's heading, so they are an interface, not decoration:
// asserted by label and by position, ahead of --- INSTRUCTIONS ---.
{
  const head = bundle.slice(0, bundle.indexOf("--- INSTRUCTIONS ---"));
  check(/^GAME: .+\(appid \d+\)$/m.test(head), "game name and appid stated above the instructions");
  check(/^REVIEWS: \d+ sampled · \d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}$/m.test(head),
        "sample size and date span stated above the instructions");
}
check(/ALL-TIME \(all languages\).*983,491/.test(bundle), "all-language anchor printed");
check(/ALL-TIME \(english only\): 89% of 417,281/.test(bundle), "english anchor printed and scoped");
check(/language: english only \(~42%/.test(bundle), "non-English share reported");
check(bundle.includes("off-topic / review-bombing reviews: INCLUDED"), "review bombs declared included");
check(/substantive: \d+ of \d+/.test(bundle), "substantive count reported");
check(/covering: \d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}/.test(bundle), "sample date span reported");
// §25 — date then count, so one game's pulls sort chronologically in a folder and a re-pull
// never lands on the identical name (the browser used to silently make that one "(1)").
check(/^qtpd-reviews-[a-z0-9-]+-\d+-\d{4}-\d{2}-\d{2}-\d+rev\.txt$/.test(txtName),
      `saved .txt carries the date and the review count (${txtName})`);
{
  // The name is derived from the file, not from the clock at save time: a bundle built at
  // 23:58 UTC and saved at 00:01 must not carry two different dates.
  const genDay = (bundle.match(/^GENERATED : (\d{4}-\d{2}-\d{2})$/m) || [])[1];
  const kept   = (bundle.match(/^--- REVIEWS \((\d+)\) ---$/m) || [])[1];
  check(!!genDay && txtName.includes(genDay), "the filename's date is the one printed in the file");
  check(!!kept && txtName.endsWith(`-${kept}rev.txt`), "the filename's count is the bundle's kept count");
}
check(bundle.includes("[EA]"), "EA flag emitted");
check(bundle.includes("[deck]"), "deck flag emitted");
check(bundle.includes("[free]"), "free flag emitted");
check(bundle.includes("[upd]"), "upd flag emitted");
check((bundle.match(/COPYPASTA THE PUBLISHER/g) || []).length === 1, "copypasta deduped to one copy");
check(!bundle.includes("█▓▒░"), "ASCII art dropped");
check(bundle.includes("…"), "long review truncated");
check(!/\[b\]|\[\/b\]|\[url=/.test(bundle), "BBCode stripped");
check(bundle.includes("[sailing]"), "bracketed PROSE preserved (not treated as BBCode)");
check(/1 duplicates/.test(bundle) && /1 ASCII-art/.test(bundle), "drops counted in header");
// §23.1 — the bar is on by default, so the DEFAULT bundle has to own what it removed. The
// fixture's one-word review ("gg") is the only thing under it here.
check(/ · 1 under the 5-word bar · /.test(bundle), "the bar's drops sit on the excluded line beside the others");
check(/^  quality bar: ON at 5 words\./m.test(bundle),
      "the bar states itself in OVERVIEW rather than silently shrinking the sample");
// §23.4 — the two splits sit on consecutive lines in the same shape, with the gap between
// them named on the line after. Split apart, neither one answers "is that the filter or the
// game?", which is the only question a reader has when a rate moves.
{
  const lines = bundle.split("\n");
  const i = lines.findIndex(l => l.startsWith("  sample split:"));
  check(i > 0 && /^  sample split: \d+ up \/ \d+ down \(\d+% positive\) — the \d+ reviews in this bundle, after the bar$/.test(lines[i]),
        `the sample split says which population it is (${JSON.stringify(lines[i] || "")})`);
  check(/^  before the bar: \d+ up \/ \d+ down \(\d+% positive\) — the \d+ reviews read to fill it$/.test(lines[i + 1] || ""),
        `the unfiltered split sits directly under it in the same shape (${JSON.stringify(lines[i + 1] || "")})`);
  check(/^  the difference: \d+ removed, \d+ up \/ \d+ down — \d+ under the 5-word bar/.test(lines[i + 2] || ""),
        `and the line after breaks down what came out (${JSON.stringify(lines[i + 2] || "")})`);
  check(/differ by -?\d+ points — that gap is the filter, not the game/.test(lines[i + 3] || ""),
        "the gap between the two rates is named, not left as arithmetic");
}
check(/^BASIS  : over the \d+ reviews that cleared the 5-word bar/m.test(bundle),
      "TIMELINE declares which population its rates are measured over");
check(bundle.includes("--- INSTRUCTIONS ---"), "instructions section present");
{
  // TIMELINE exists so the AI copies rates instead of deriving them from 500 dated lines.
  // Assert the numbers are actually printed AND internally consistent — a block that says
  // 76% while tagging a different number of rows is worse than no block at all.
  check(bundle.includes("--- TIMELINE"), "TIMELINE block present");
  const now = bundle.match(/^NOW    : \S+ to \S+ · (\d+) reviews · (\d+)▲\/(\d+)▼ · (\d+)% positive$/m);
  check(!!now, "NOW line printed with span, counts and rate");
  if (now) {
    const [, n, up, down, pctd] = now.map(Number);
    check(up + down === n, `NOW ▲+▼ equals its review count (${up}+${down}=${n})`);
    check(Math.round(100 * up / n) === pctd, `NOW rate matches its own counts (${pctd}%)`);
    const tagged = (bundle.match(/^[▲▼] .*\[now\]/gm) || []).length;
    check(tagged === n, `[now] tags on review lines match the NOW count (${tagged} vs ${n})`);
  }
  check(/^BEFORE : /m.test(bundle), "BEFORE window printed (fixture spans a year)");
  check(/^TREND  : [+-]?\d+ pts/.test(bundle) || /^TREND  : /m.test(bundle), "TREND printed in points");
  check(/^BASELINE ▼ RATE: \d+%/m.test(bundle), "baseline ▼ rate printed");
  check(/^BY QUARTER: .*Q\d \d+% \(\d+\)/m.test(bundle), "quarterly breakdown printed");
  // Fewer than RD_NOW_MIN reviews are recent here, so the window must widen AND say so.
  check(/widened from the last 90 days/.test(bundle), "widened window disclosed, not silently applied");
  check(/^LAST 90D: \d+ reviews · \d+% positive/m.test(bundle), "sharper 90-day rate still reported");
  check(/FLAG SPLITS: .*\[free\] \d+ reviews \d+% vs \d+%/.test(bundle), "flag splits precomputed");
  check(/\[now\]\[EA\]|\[now\]\[free\]|\[now\]\[deck\]/.test(bundle) || /\[now\]/.test(bundle),
        "[now] composes with the other flags");
  check(/\[now\] posted inside the NOW window/.test(bundle), "legend documents the [now] flag");
  // A review count says nothing about the time it covers; these two lines are the only
  // place the bundle admits whether 500 reviews is two years or two days.
  check(/^COVERAGE: this sample spans \d+ days · ~[\d.]+ reviews\/month$/m.test(bundle),
        "COVERAGE states the span and the rate (per MONTH on a year-long fixture)");
  check(/^SPANS  : NOW covers \d+ days? · BEFORE covers \d+ days?$/m.test(bundle),
        "window spans reported in days, unwarned on a fixture that spans a year");
  check(!/narrowed:/.test(bundle), "slow fixture widens, so it must NOT also narrow");
}
{
  // TOPIC MENTIONS is the floor the model checks its own counting against. It is INPUT:
  // the guard line matters as much as the table, because a pipe table sitting in the
  // context is the most copy-pasteable thing in the whole bundle.
  check(bundle.includes("--- TOPIC MENTIONS"), "TOPIC MENTIONS block present");
  check(/THIS BLOCK IS INPUT, NOT OUTPUT/.test(bundle), "block declares itself input, not output");
  check(/^\| topic \| hits \| now \| before \| ▼\/▲ \| ↑votes on ▼ \| % of NOW \| % of BEFORE \|$/m.test(bundle),
        "topic table header printed");
  const trows = bundle.match(/^\| [A-Z][^|]*\| \d+ \| \d+ \| \d+ \| \d+▼\/\d+▲ \| \d+ \| \d+% \| \d+% \|$/gm) || [];
  check(trows.length > 0, `topic rows emitted with counts and a ▼/▲ split (${trows.length})`);
  const badRow = trows.find(r => {
    const [, tot, now, before] = r.match(/\| (\d+) \| (\d+) \| (\d+) \|/).map(Number);
    return now + before !== tot;
  });
  check(!badRow, `every topic row's now+before equals its hit count${badRow ? " — " + badRow : ""}`);
  // "Nobody mentioned microtransactions" is a finding. Without these lines the model cannot
  // tell a family that was checked and found absent from one that was never looked for.
  check(/^(?:Fewer than 3 hits|Checked, zero hits): /m.test(bundle),
        "families below the floor are named rather than silently dropped");
  const tops = (bundle.match(/^[▲▼] .*\[top\]/gm) || []).length;
  check(tops === 10, `[top] tags the 10 most-upvoted reviews (${tops})`);
  check(/\[top\] among the 10 most-upvoted reviews/.test(bundle), "legend documents the [top] flag");
}
{
  // The setup dialog. §18.2 reversed the rule this used to assert: the sizes are a NUMBER
  // LINE, so they read smallest -> largest even though that puts the default second. The lit
  // pill is what marks the default now, which makes the second check load-bearing rather
  // than a restatement of the first.
  check(sizeOpts.join("·") === "300·500·1000·2000·5000", `sample sizes offered in ascending order (${sizeOpts.join("·")})`);
  // §24.1 — the deepest sample is the default now. History the fetch never reached is the one
  // thing a reader cannot recover afterwards; every other cost is stated up front.
  check(sizeOn.length === 1 && sizeOn[0] === "5000", `5000 is the default and the only one lit (${sizeOn.join(",")})`);
  check(modeOpts.join("·") === "Simplified·Advanced", `both report modes offered, simple first (${modeOpts.join("·")})`);
  check(modeOn.length === 1 && modeOn[0] === "Advanced", `Advanced is the default report mode (${modeOn.join(",")})`);
  check(goLabel === "Fetch 5000 reviews", `fetch button quotes the chosen size (${JSON.stringify(goLabel)})`);
  // §23.2 — the output is its own axis, not a third depth. §24.1 made the page the default:
  // the reader who wants a keepable report per game is the reader this dialog is for, and the
  // addendum's ~3k tokens of stylesheet is noise beside a 5000-review sample.
  check(outOpts.join("·") === "Markdown·HTML page", `both output formats offered (${outOpts.join("·")})`);
  check(outOn.length === 1 && outOn[0] === "HTML page", `the HTML page is the default output (${outOn.join(",")})`);
  // §23.1 — the bar ships ON. Off is offered and is the pre-§23 behaviour exactly, but a
  // default of Off would leave the wrong denominator as what everybody gets. §24.1 raised it
  // to 10: with 5000 reviews the sample is not scarce, so the tokens go on reviews that argue.
  check(barOpts.join("·") === "Off·3+ words·5+ words·10+ words", `the quality bar offers off and three heights (${barOpts.join("·")})`);
  check(barOn.length === 1 && barOn[0] === "10+ words", `the 10-word bar is on by default (${barOn.join(",")})`);
  // §24.2 — reach is offered as its own row and defaults to the contiguous run. A default of
  // anything else would silently hand every reader a 1-in-N sample they never asked for.
  check(reachOpts.join("·") === "Every page·Every 2nd page·Every 3rd page",
        `reach offers the contiguous run and two skips (${reachOpts.join("·")})`);
  check(reachOn.length === 1 && reachOn[0] === "Every page", `every page is kept by default (${reachOn.join(",")})`);
  check(focusOpts === 7, `seven reader-focus toggles offered (${focusOpts})`);
  check(focusOn === 0, `no focus is on by default (${focusOn})`);
  check(badFocusMap.length === 0,
        `every reader focus maps to a real RD_TOPICS family${badFocusMap.length ? " — orphaned: " + badFocusMap.join(", ") : ""}`);
  check(!bundle.includes("--- READER FOCUS"), "no READER FOCUS block when nothing was ticked");
  // §18.3. The three targets, and the hint that has to stay honest about WHY there is still a
  // paste: nothing here can prefill 100+ KB through a link, and a button that implied it could
  // would be the one thing worse than the paste itself.
  check(aiOpts.join("·") === "Claude ↗·ChatGPT ↗·Gemini ↗", `three chat handoffs offered (${aiOpts.join("·")})`);
  check(aiInFoot === 3, `handoff buttons sit in the footer next to Copy all (${aiInFoot} of 3)`);
  check(/Ctrl\+V|⌘V/.test(aiHint || ""), `handoff line names the paste shortcut (${JSON.stringify((aiHint || "").slice(0, 60))})`);
  check(aiTips.every(t => /Ctrl\+V|⌘V/.test(t || "")), "each handoff button's tooltip names the paste shortcut");
  check(/attach|\.txt file/i.test(dlTip || ""), "Download .txt explains the oversized-paste route on itself");
  check(setupUntitled.length === 0, `every setup option carries its own tooltip (untitled: ${JSON.stringify(setupUntitled)})`);
}
{
  // §15.1 restructured the report so the first screen is the answer and the Issues table is
  // the working. The skeleton lives in review_prompt.md and rides into the bundle verbatim,
  // so its section ORDER is assertable here — and order is exactly what the three models
  // disagreed on. Headings only: pinning prose made this break on every prompt edit.
  const at = h => bundle.indexOf("\n" + h + "\n");
  const seq = ["### Snapshot", "### Who it's for", "### Loved vs hated",
               "### Where the complaints land", "### Notes", "### Issues"].map(h => [h, at(h)]);
  check(seq.every(([, i]) => i > 0), `every output section present (${seq.filter(([, i]) => i < 0).map(([h]) => h).join(", ") || "all"})`);
  check(seq.every(([, i], n) => n === 0 || i > seq[n - 1][1]),
        `output sections ordered snapshot -> who -> loved/hated -> buckets -> notes -> issues`);
  // An empty `| | |` header row is dropped whole by strict renderers, taking its table with
  // it — observed live in one of the three models.
  check(bundle.includes("| Field | Value |"), "Snapshot carries a real header row, not an empty one");
  check(bundle.includes("| Dragging the score |"), "Snapshot carries the Dragging-the-score row");
  check(/\| Quit \/ stayed \| What they say \|/.test(bundle), "Issues header names the split column in words");
  check(/max\(5 reviews, 2% of substantive\)/.test(bundle), "row floor raised off the fixed 3");
  check(/headline row/i.test(bundle), "headline-row rule carried in the instructions");
  // §18.5. The report opens on the game's name and closes on the receipt. Both are asserted
  // by position against the skeleton's own sections: a title that drifts below the Snapshot,
  // or an INTEGRITY line that creeps back to the top, is the failure this was written to fix.
  const title = bundle.indexOf("\n# <game title");
  check(title > 0 && title < at("### Snapshot"), "skeleton opens on the game title, above the Snapshot");
  check(/copied character for character from the GAME: line/.test(bundle),
        "title is ordered copied verbatim from the GAME: line, not recalled");
  const integrity = bundle.indexOf("\nINTEGRITY: read");
  check(integrity > at("### Issues"), "INTEGRITY demoted to a footer below the Issues table");
}
{
  // Order is INSTRUCTIONS -> OVERVIEW -> REVIEWS. Asserted by position, not presence, so a
  // future edit cannot quietly put the task back behind 500 lines of review text.
  const i = bundle.indexOf("--- INSTRUCTIONS ---");
  const o = bundle.indexOf("--- OVERVIEW ---");
  const r = bundle.indexOf("--- REVIEWS (");
  check(i >= 0 && o > i && r > o, `sections ordered instructions(${i}) < overview(${o}) < reviews(${r})`);
  const ptr = bundle.indexOf("--- END OF REVIEWS.");
  const foot = bundle.indexOf("--- DIGEST FOOTER");
  check(ptr > r && foot > ptr,
        `closing pointer back to the instructions(${ptr}), footer last(${foot})`);
  // §25 — the tail block, for a saved file opened months later on nothing but its last screen.
  // The header already carries these; the point is that the END of the file carries them too.
  check(/^GENERATED : \d{4}-\d{2}-\d{2}$/m.test(bundle), "footer dates the file it sits in");
  check(/^REVIEWS   : \d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2} \(\d+ days?\) · \d+ kept$/m.test(bundle),
        "footer restates the sample's date range and size at the end of the file");
  check(/oldest and newest KEPT in this bundle/.test(bundle),
        "footer says the span is of reviews kept, not of everything Steam served");
  check(/^GAME {6}: .+\(appid \d+\)$/.test(bundle.trimEnd().split("\n").pop()),
        "the game is the last line, so the tail names what it is a digest of");
  check(/LEGEND: ▲\/▼/.test(bundle) && /^▲ |\n▲ /m.test(bundle),
        "review lines use the ▲/▼ glyphs the prompt documents");
}
// Pinning a phrase from the prompt made this break on every prompt edit. The version
// marker is the durable signal: only the file carries one, the inline fallback does not.
check(/=== QTPD REVIEW DIGEST · prompt v\S+ ===/.test(bundle),
      "real review_prompt.md loaded, not the inline fallback");
check(errors.length === 0, `no uncaught JS errors (${errors.length})`);
console.log(`  (${noise.length} console 404s from the deliberately absent JSON layers - expected)`);
errors.slice(0, 5).forEach(e => console.log("     " + e));
// --- second scenario: the proxy is unreachable ------------------------------------
// This is the exact failure the first live click hit. A network-level failure must not
// dead-end: it has to name the likely cause and offer the URL as an editable field, so a
// wrong subdomain is a five-second fix in the page rather than a code change and redeploy.
{
  const b2 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p2 = await b2.newPage();
  const errs2 = [];
  p2.on("pageerror", e => errs2.push(e.message));
  await p2.route("**/qtpd-reviews.*/**", r => r.abort("connectionrefused"));
  await p2.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p2.waitForTimeout(500);
  await p2.locator("button.gsub-rev").first().click();
  await p2.waitForSelector("#rdHost.on");
  await p2.locator("#rdGo").click();
  await p2.waitForSelector("#rdProxyIn", { timeout: 10000 });
  const shown = await p2.locator("#rdProxyIn").inputValue();
  const note  = await p2.locator("#rdErr").textContent();
  await b2.close();
  console.log("\nfailure path:");
  check(shown.startsWith("https://"), `proxy URL offered for editing (${shown})`);
  check(/couldn't reach the proxy/i.test(note), "network failure explained, not just echoed");
  check(/origin/i.test(note), "origin named as a possible cause");
  check(errs2.length === 0, "no uncaught JS errors on the failure path");
}

// --- third scenario: a BUSY game -------------------------------------------------
// The sample does not reach back 90 days, so the calendar window covers all of it. Before
// the narrowing branch that produced NOW === the whole sample, an empty BEFORE, and a
// TIMELINE with no TREND line at all — while the prompt still asked the model for a trend,
// which it then supplied from nowhere. 300 reviews across ~12 days trips every new guard
// at once: narrowing, the per-day rate, and both span warnings.
{
  const busy = Array.from({ length: 300 }, (_, i) => mk(i + 500));   // default ts: an hour apart
  const b3 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p3 = await b3.newPage();
  const errs3 = [];
  p3.on("pageerror", e => errs3.push(e.message));
  await p3.route("**/qtpd-reviews.*/**", route => {
    const per = new URL(route.request().url()).searchParams.get("num_per_page");
    route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify(per === "0"
        ? { success: 1, query_summary: { total_reviews: 4000, total_positive: 3000,
            total_negative: 1000, review_score_desc: "Mostly Positive" }, reviews: [] }
        : { success: 1, query_summary: {}, reviews: busy, cursor: "" }) });
  });
  await p3.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p3.waitForTimeout(500);
  await p3.locator("button.gsub-rev").first().click();
  await p3.waitForSelector("#rdHost.on");
  await p3.locator("#rdGo").click();
  await p3.waitForSelector("#rdOut", { timeout: 15000 });
  const b = await p3.locator("#rdOut").inputValue();
  await b3.close();

  console.log("\nbusy game (sample shorter than the 90-day window):");
  const nowLine = b.match(/^NOW    : \S+ to \S+ · (\d+) reviews/m);
  const befLine = b.match(/^BEFORE : \S+ to \S+ · (\d+) reviews/m);
  check(!!nowLine && !!befLine, "both windows printed");
  if (nowLine && befLine) {
    const nowN = Number(nowLine[1]), befN = Number(befLine[1]);
    check(befN > 0, `BEFORE is not empty (${befN}) — the bug this branch exists for`);
    check(nowN / (nowN + befN) <= 0.62,
          `NOW capped near RD_NOW_MAX, not swallowing the sample (${Math.round(100 * nowN / (nowN + befN))}%)`);
    const tagged = (b.match(/^[▲▼] .*\[now\]/gm) || []).length;
    check(tagged === nowN, `[now] tags still match the capped NOW count (${tagged} vs ${nowN})`);
  }
  check(/^TREND  : [+-]?\d+ pts/m.test(b), "TREND printed — nothing left for the model to invent");
  check(/narrowed: the last 90 days hold \d+ of the \d+ sampled reviews/.test(b),
        "narrowing disclosed, not silently applied");
  check(!/widened from the last 90 days/.test(b), "busy fixture narrows, so it must NOT also widen");
  check(/^COVERAGE: this sample spans \d+ days? · ~[\d.]+ reviews\/day$/m.test(b),
        "short sample reports a per-DAY rate, not an extrapolated per-month one");
  check(/WARNING: fewer than 60 days/.test(b), "short sample warns it carries no history");
  check(/WARNING: a \d+-day NOW window is a same-fortnight read/.test(b),
        "short NOW window warns the trend is not a trend");
  check(errs3.length === 0, `no uncaught JS errors on the busy path (${errs3.length})`);
}

// --- fourth scenario: sample size and reader focus (plan §15.3, §15.4) ---------------
// The only scenario that pages. Every other fixture returns an empty cursor and stops after
// one page, so the loop that turns "1000" into ten requests was never exercised — and the
// size selector is nothing but that loop plus four places that quote the number, all of
// which must move together or the card promises 500 while the dialog fetches 1000.
{
  const b4 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p4 = await b4.newPage();
  const errs4 = [];
  p4.on("pageerror", e => errs4.push(e.message));
  const cursors = [];
  let seq = 0;
  await p4.route("**/qtpd-reviews.*/**", route => {
    const q = new URL(route.request().url()).searchParams;
    const fulfil = body => route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(body) });
    if (q.get("num_per_page") === "0")
      return fulfil({ success: 1, reviews: [], query_summary: { total_reviews: 60000,
        total_positive: 45000, total_negative: 15000, review_score_desc: "Mostly Positive" } });
    cursors.push(q.get("cursor"));
    const base = seq; seq += 100;
    // 7200s apart, so 1000 reviews span ~83 days — wide enough for a real NOW/BEFORE split.
    const reviews = Array.from({ length: 100 }, (_, i) =>
      mk(base + i + 1, { ts: 1756000000 - (base + i) * 7200 }));
    fulfil({ success: 1, query_summary: {}, reviews, cursor: "c" + seq });
  });
  await p4.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p4.waitForTimeout(500);
  await p4.locator("button.gsub-rev").first().click();
  await p4.waitForSelector("#rdHost.on");
  await p4.locator('[data-rdsize="1000"]').click();
  await p4.locator('[data-rdfocus="deck"]').click();
  await p4.locator('[data-rdfocus="value"]').click();
  await p4.locator('[data-rdfocus="value"]').click();   // and off again — a toggle, not a latch
  await p4.locator('[data-rdfocus="mtx"]').click();
  const goLabel4    = (await p4.locator("#rdGo").textContent()).trim();
  const countCopy4  = (await p4.locator("#rdCount").textContent()).trim();
  const entryTitle4 = await p4.locator("button.gsub-rev").first().getAttribute("title");
  const focusOn4    = await p4.locator("[data-rdfocus].on").count();
  const pressed4    = await p4.locator('[data-rdfocus="deck"]').getAttribute("aria-pressed");
  // Ten pages at the 250ms inter-page delay is ~2.5s of deliberate waiting, so this one
  // gets a longer leash than the single-page scenarios above.
  await p4.locator("#rdGo").click();
  await p4.waitForSelector("#rdOut", { timeout: 40000 });
  const b = await p4.locator("#rdOut").inputValue();
  await b4.close();

  console.log("\nsample size + reader focus:");
  check(goLabel4 === "Fetch 1000 reviews", `fetch button follows the size (${JSON.stringify(goLabel4)})`);
  check(countCopy4 === "1000", `dialog copy follows the size (${countCopy4})`);
  check(/Pull the 1000 newest/.test(entryTitle4 || ""),
        `entry-point tooltip re-synced after the change (${JSON.stringify(entryTitle4)})`);
  check(cursors.length === 10, `1000 reviews fetched as ten pages (${cursors.length})`);
  check(cursors[0] === "*" && cursors[1] === "c100", `each page follows the previous cursor (${cursors.slice(0, 2)})`);
  check((b.match(/^--- REVIEWS \(1000\) ---$/m) || []).length === 1, "all 1000 reviews reached the bundle");
  check(focusOn4 === 2, `two focuses lit after one was toggled back off (${focusOn4})`);
  check(pressed4 === "true", `lit focus reports aria-pressed (${pressed4})`);
  {
    // The focus block is an amendment to the task, not another input to weigh, so it sits
    // with the instructions rather than down with the data.
    const i = b.indexOf("--- INSTRUCTIONS ---"), f = b.indexOf("--- READER FOCUS"), o = b.indexOf("--- OVERVIEW ---");
    check(f > i && o > f, `READER FOCUS sits between instructions(${i}) and overview(${o}) at ${f}`);
    const lines = b.match(/^\* .+ · start from TOPIC MENTIONS: .+$/gm) || [];
    check(lines.length === 2, `one line per ticked focus, in dialog order (${lines.length})`);
    check(/^\* Steam Deck /.test(lines[0] || "") && /^\* Microtransactions /.test(lines[1] || ""),
          "focus lines follow the dialog order, not the click order");
    // Each line names the families it should be answered from; if the name has drifted out
    // of RD_TOPICS the topic block will not mention it and the focus is unanswerable.
    const topics = b.slice(b.indexOf("--- TOPIC MENTIONS"), b.indexOf("LEGEND:"));
    const named = lines.flatMap(l => l.split("TOPIC MENTIONS: ")[1].split(" + "));
    check(named.every(t => topics.includes(t)), `every named family appears in the topic block (${named.join(", ")})`);
    check(/gets its OWN row in the Issues table — below the floor, and at 0/.test(b),
          "the guaranteed-row-at-zero rule is stated, not merely implied");
    check(/Counts only\. Do not recommend, defend, dismiss or condemn/.test(b),
          "the no-editorialising rule is stated for every focus");
  }
  check(errs4.length === 0, `no uncaught JS errors on the size/focus path (${errs4.length})`);
}

// --- fifth scenario: the Simplified report mode (plan §18.4) -------------------------
// Both modes ride the same bundle and differ only in the INSTRUCTIONS block, so the thing
// worth pinning is that the SWAP happened: the simple skeleton is in, the advanced one is
// out, and the data blocks the short report still depends on are untouched. Asserting the
// absence matters as much as the presence — the failure this guards against is a Simplified
// pick that quietly ships the advanced prompt, which looks fine until the model returns a
// twelve-row issue table nobody asked for.
{
  const b5 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p5 = await b5.newPage();
  const errs5 = [];
  p5.on("pageerror", e => errs5.push(e.message));
  await p5.route("**/qtpd-reviews.*/**", route => {
    const per = new URL(route.request().url()).searchParams.get("num_per_page");
    route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify(per === "0"
        ? { success: 1, query_summary: { total_reviews: 40000, total_positive: 30000,
            total_negative: 10000, review_score_desc: "Mostly Positive" }, reviews: [] }
        // 7200s apart, so 300 reviews span ~25 days and TIMELINE still prints both windows.
        : { success: 1, query_summary: {},
            reviews: Array.from({ length: 300 }, (_, i) => mk(i + 1, { ts: 1756000000 - i * 7200 })),
            cursor: "" }) });
  });
  await p5.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p5.waitForTimeout(500);
  await p5.locator("button.gsub-rev").first().click();
  await p5.waitForSelector("#rdHost.on");
  await p5.locator('[data-rdout="md"]').click();      // §24.1's default is the page; this scenario is about the Markdown skeleton
  await p5.locator('[data-rdmode="simple"]').click();
  const modeOn5 = await p5.locator("[data-rdmode].on").allTextContents();
  await p5.locator('[data-rdfocus="deck"]').click();
  await p5.locator("#rdGo").click();
  await p5.waitForSelector("#rdOut", { timeout: 20000 });
  const b = await p5.locator("#rdOut").inputValue();
  await b5.close();

  console.log("\nsimplified report mode:");
  check(modeOn5.length === 1 && modeOn5[0] === "Simplified", `picking a mode lights exactly one (${modeOn5.join(",")})`);
  // Only the file carries a version marker; the inline fallback does not. This is what says
  // review_prompt_simple.md was actually fetched rather than silently fallen back to.
  check(/=== QTPD REVIEW DIGEST · prompt v\S+-simple ===/.test(b),
        "real review_prompt_simple.md loaded, not the inline fallback");
  const at = h => b.indexOf("\n" + h + "\n");
  const seq = ["### Summary", "### Who it's for", "### Best and worst"].map(h => [h, at(h)]);
  check(seq.every(([, i]) => i > 0), `simple sections present (${seq.filter(([, i]) => i < 0).map(([h]) => h).join(", ") || "all"})`);
  check(seq.every(([, i], n) => n === 0 || i > seq[n - 1][1]), "simple sections ordered summary -> who -> best/worst");
  check(b.includes("| # | Best | N | Worst | N |"), "best/worst table keeps its raw review counts");
  const sTitle = b.indexOf("\n# <game title");
  check(sTitle > 0 && sTitle < at("### Summary"), "simple skeleton opens on the game title too");
  check(/copied character for character from the GAME: line/.test(b),
        "simple title is ordered copied verbatim from the GAME: line");
  check(b.indexOf("*Read <N> of <N> reviews") > at("### Best and worst"),
        "simple report ends on the one-line sample footer");
  // The advanced skeleton's own headings must be gone. "### Notes" is checked too: it is the
  // one heading both files could plausibly want, and it is exactly the kind of section that
  // creeps back into a report meant to be three sections long.
  const advanced = ["### Snapshot", "### Where the complaints land", "### Issues", "### Notes"];
  const leaked = advanced.filter(h => b.includes("\n" + h + "\n"));
  check(leaked.length === 0, `no advanced sections leaked into the simple prompt${leaked.length ? " — " + leaked.join(", ") : ""}`);
  check(!/max\(5 reviews, 2% of substantive\)/.test(b), "the advanced row floor is not carried into the simple prompt");
  check(/No percentages anywhere/.test(b), "the no-percentages rule is stated, not merely implied by the skeleton");
  // Same bundle, different instructions: the precomputed blocks are what keep a five-line
  // report honest, so stripping them to match the shorter output would be the wrong economy.
  check(/^--- TIMELINE \(precomputed/m.test(b), "TIMELINE still rides along in simple mode");
  check(/^--- TOPIC MENTIONS /m.test(b), "TOPIC MENTIONS still rides along in simple mode");
  check(/^--- REVIEWS \(\d+\) ---$/m.test(b), "the reviews themselves are unchanged by the mode");
  // A focus is binding in BOTH modes; the simple prompt answers it in its own last section.
  check(b.includes("--- READER FOCUS"), "reader focus still reaches the bundle in simple mode");
  check(/### What you asked about/.test(b), "simple prompt gives the focus its own section");
  check(errs5.length === 0, `no uncaught JS errors on the simple path (${errs5.length})`);
}

// --- sixth scenario: short pages, the full 2000, and the paste warning (plan §21-§22) --
// Three things that only appear on a deep pull, all of which failed silently before:
//   1. Steam serves 98-99 reviews on ~2% of pages and keeps going. The fetch used to treat
//      that as end-of-list, so roughly one ten-page pull in five came back short and said
//      nothing about it. Page 3 here returns 98, and the run must still reach the end.
//   2. 2000 has to MEAN 2000 (§22.1). A char budget used to trim the tail on exactly the
//      text-heavy games the big sizes exist for — this fixture is one of those (~140 chars a
//      review, ~346 KB at 2000) and it must come back with all 2000 and no "size cap" line.
//      The short page is why the walk is bounded by the count rather than by 20 pages of
//      arithmetic: with a 98 in the middle, page 21 is what fetches the last two reviews.
//   3. Past ~60 KB Gemini truncates a paste instead of refusing it, which is how a digest
//      gets analysed half-read. The result panel has to say so — and has to say it about
//      GEMINI, since Claude and ChatGPT turn the same paste into an attachment (§22.2).
{
  const b6 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p6 = await b6.newPage();
  const errs6 = [];
  p6.on("pageerror", e => errs6.push(e.message));
  const pages6 = [];
  let seq6 = 0;
  // ~140 chars of prose per review — a text-heavy game like Cyberpunk 2077, measured live at
  // 156 chars/review. 2000 of those is ~300 KB, so this one fixture crosses both the hard
  // paste threshold and the size cap.
  const prose = "Great combat, rough launch, and the last two patches finally fixed the frame stutter in the city. Worth it now, was not at release. Runs fine.";
  await p6.route("**/qtpd-reviews.*/**", route => {
    const q = new URL(route.request().url()).searchParams;
    const fulfil = body => route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(body) });
    if (q.get("num_per_page") === "0")
      return fulfil({ success: 1, reviews: [], query_summary: { total_reviews: 417281,
        total_positive: 371000, total_negative: 46281, review_score_desc: "Very Positive" } });
    const pageNo = pages6.length + 1;
    pages6.push(q.get("cursor"));
    const count = pageNo === 3 ? 98 : 100;         // the gap that used to end the walk
    const base = seq6; seq6 += count;
    const reviews = Array.from({ length: count }, (_, i) =>
      mk(base + i + 1, { ts: 1756000000 - (base + i) * 3600, review: prose + " #" + (base + i) }));
    fulfil({ success: 1, query_summary: {}, reviews, cursor: "c" + seq6 });
  });
  await p6.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p6.waitForTimeout(500);
  await p6.locator("button.gsub-rev").first().click();
  await p6.waitForSelector("#rdHost.on");
  await p6.locator('[data-rdout="md"]').click();      // the KB and token figures below are the Markdown bundle's
  await p6.locator('[data-rdsize="2000"]').click();
  const goLabel6 = (await p6.locator("#rdGo").textContent()).trim();
  await p6.locator("#rdGo").click();
  await p6.waitForSelector("#rdOut", { timeout: 60000 });   // up to 20 pages at 250ms apart
  const out6    = await p6.locator("#rdOut").inputValue();
  const warnCls = await p6.locator("#rdWarn").getAttribute("class").catch(() => null);
  const warnTxt = await p6.locator("#rdWarn").textContent().catch(() => "");
  const primary = await p6.locator("#rdFoot .rd-go").getAttribute("id");
  const footIds = await p6.locator("#rdFoot button, #rdFoot a").evaluateAll(
    els => els.map(e => e.id || e.getAttribute("data-rdai")));
  const got6    = Number((out6.match(/^--- REVIEWS \((\d+)\) ---$/m) || [])[1]);
  const aiTips6 = await p6.locator("#rdFoot [data-rdai]").evaluateAll(
    els => els.map(e => e.getAttribute("data-rdai") + "|" + e.title));
  // §22.4 — the handoff buttons are anchors, and the whole point is what the BROWSER does
  // with them: ctrl-click, middle-click and "open in new window" are the browser's, not ours,
  // and none of them exist on a <button> that calls window.open.
  const aiHrefs = await p6.locator("#rdFoot [data-rdai]").evaluateAll(
    els => els.map(e => [e.tagName, e.getAttribute("href"), e.target].join("|")));
  const headCnt = await p6.locator("#rdHeadCount").textContent();
  const meter6  = await p6.locator("#rdFoot .rd-size").textContent();
  await b6.close();

  console.log("\nshort pages, size cap and paste warning:");
  check(goLabel6 === "Fetch 2000 reviews", `2000 is selectable and the button follows it (${JSON.stringify(goLabel6)})`);
  // The short page is at page 3. Anything at or below 300 means it ended the walk, which is
  // the whole bug — the count is capped by the size budget, not by that page.
  check(pages6.length > 3, `the short page did not end the walk (${pages6.length} pages fetched)`);
  check(got6 === 2000, `asking for 2000 on a text-heavy game returns 2000 (${got6} in the bundle)`);
  check(pages6.length === 21, `the short page cost one extra page, not two reviews (${pages6.length})`);
  check(new RegExp(`^SAMPLE: ${got6} newest`, "m").test(out6),
        "the header states the count it actually got, not the one asked for");
  check(!/size cap:/.test(out6),
        "nothing trims the sample from underneath the reader's choice any more");
  check((warnCls || "") === "rd-warn hard", `a ~300 KB bundle gets the hard paste warning (${warnCls})`);
  check(/Download \.txt/.test(warnTxt) && /upload the file/.test(warnTxt),
        "the warning names the download-and-upload route, not just the problem");
  // The correction of §22.2: the blanket "too long to paste" was false on two of the three
  // tabs it was shown next to, and a warning that is wrong where the reader is standing is
  // one they learn to skip — including on the tab where it is true.
  check(/Gemini/.test(warnTxt) && /Claude and ChatGPT/.test(warnTxt),
        "the warning names which composer cuts and which two do not");
  check(!/too long to paste/i.test(warnTxt),
        "it no longer tells a Claude user their digest cannot be pasted");
  const cutTip6  = aiTips6.find(t => t.startsWith("gemini")) || "";
  const fileTip6 = aiTips6.find(t => t.startsWith("claude")) || "";
  check(/cuts the paste/.test(cutTip6), `the Gemini button warns about the cut (${cutTip6})`);
  check(/arrives whole/.test(fileTip6) && !/\.txt/.test(fileTip6),
        `the Claude button does not send the reader to the file (${fileTip6})`);
  check(primary === "rdDl", `past the hard threshold the file takes the primary button (${primary})`);
  check(footIds[0] === "rdDl" && footIds.includes("rdCopy"),
        `the file leads the footer and Copy all survives as a secondary (${footIds.join(",")})`);
  check(errs6.length === 0, `no uncaught JS errors on the deep-pull path (${errs6.length})`);
  // §22.4 — the three things the result panel has to say about what it just built.
  check(headCnt.trim() === "2000 reviews",
        `the dialog header states how many reviews came back (${JSON.stringify(headCnt)})`);
  check(/\d+ KB · ~[\d.]+k tokens in the AI/.test(meter6),
        `the footer prices the bundle in the AI's tokens, labelled (${JSON.stringify(meter6)})`);
  check(aiHrefs.every(h => /^A\|https:\/\/.+\|_blank$/.test(h)),
        `every handoff is a real target=_blank link, not a button (${aiHrefs.join(" ")})`);
  // One line, not a paragraph: who can't take it, what to do, who is unaffected.
  check(warnTxt.replace(/\s+/g, " ").trim().length < 160,
        `the banner stays one line (${warnTxt.replace(/\s+/g, " ").trim().length} chars)`);
}

// --- seventh scenario: the budget and threshold arithmetic ----------------------------
// Unit-style against the real page's functions. Driving every tier through the UI would mean
// three more multi-page fixtures to assert what these two decide between them.
{
  const b7 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p7 = await b7.newPage();
  await p7.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  const r = await p7.evaluate(() => ({
    budget:    typeof RD.charBudget,
    aiPaste:   RD_AI.map(a => a.key + ":" + a.paste),
    fileNames: RD_AI_FILE,
    cutNames:  RD_AI_CUT,
    sizes:    RD_SIZES,
    tip5000:  RD_SIZE_TIP[5000] || null,
    reaches:  RD_REACH,
    reachTips: RD_REACH.map(n => RD_REACH_TIP[n] || ""),
    warnNone: rdPasteAdvice(40 * 1024),
    warnSoft: rdPasteAdvice(80 * 1024).hard,
    warnHard: rdPasteAdvice(200 * 1024).hard,
  }));
  await b7.close();
  console.log("\ncomposer table and paste thresholds:");
  check(r.budget === "undefined", `no char budget survives to cap a chosen size (${r.budget})`);
  // Every AI button's copy branches on this field; an entry without one falls through to the
  // "your paste gets cut" branch and tells a Claude user something untrue.
  check(r.aiPaste.every(x => /:(file|cut)$/.test(x)),
        `every AI has a declared paste behaviour (${r.aiPaste.join(", ")})`);
  check(r.fileNames === "Claude and ChatGPT" && r.cutNames === "Gemini",
        `the copy names the composers from that table (${r.fileNames} / ${r.cutNames})`);
  check(r.sizes[r.sizes.length - 1] === 5000, `5000 is the largest sample offered (${r.sizes.join(",")})`);
  // Every size pill carries its own argument; a new option with no tip falls back to a bare
  // "5000 reviews." and silently loses the one thing the reader needs to choose it. 5000 is
  // past a 200k context window (§24.1), so its tooltip owes the reader that number and the
  // composer that survives it — the option is offered on a stated price, not on silence.
  check(!!r.tip5000 && /Gemini/.test(r.tip5000) && /tokens/.test(r.tip5000),
        "the 5000 pill's tooltip prices it in tokens and names Gemini's cut");
  // §24.2 — reach costs wall clock, not tokens, and a reader who learns that at page 40 of a
  // fetch has been misled by omission. Every skipping option has to say so on itself.
  check(r.reaches.join(",") === "1,2,3", `reach offers 1x, 2x and 3x (${r.reaches.join(",")})`);
  check(r.reachTips.slice(1).every(t => /fetch|wait/.test(t)),
        "each skipping option's tooltip admits it costs fetch time");
  check(r.warnNone === null, "a small bundle warns about nothing");
  check(r.warnSoft === false && r.warnHard === true, "60 KB warns, 150 KB insists on the file");
}

// --- eighth scenario: the quality bar and HTML output (plan §23) -----------------------
// The two options added on 2026-09-03, on one fixture because they are one dialog and the
// failure that matters is a cross one: an HTML run that also drags the Markdown skeleton in.
//
// The fixture is 40% noise BY CONSTRUCTION — 96 of 240 reviews are "gg" / "10/10" / "cool" /
// "👍" — which is the measured shape of a real sample and the whole reason §23.1 exists. It
// also carries one 14-character Japanese review: whitespace word-splitting scores that as a
// single word and would delete it, so it is the check that the CJK path is not just theory.
{
  const b8 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p8 = await b8.newPage();
  const errs8 = [];
  p8.on("pageerror", e => errs8.push(e.message));
  const shorts = ["gg", "10/10", "cool", "👍", "meh", "Good game"];
  const jp = "神ゲーだけど最適化が酷いです";                       // 14 chars, no spaces at all
  let seq8 = 0, pages8 = 0;
  await p8.route("**/qtpd-reviews.*/**", route => {
    const q = new URL(route.request().url()).searchParams;
    const fulfil = body => route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(body) });
    if (q.get("num_per_page") === "0")
      return fulfil({ success: 1, reviews: [], query_summary: { total_reviews: 51234,
        total_positive: 40000, total_negative: 11234, review_score_desc: "Mostly Positive" } });
    pages8++;
    // 40 of every 100 are one-liners, and they are voted_up far more often than the prose —
    // which is the bias §23.1 reports rather than hides, and is why the pre-bar split has to
    // come out ABOVE the post-bar one for the assertion below to mean anything.
    const reviews = Array.from({ length: 100 }, (_, i) => {
      const n = seq8 + i;
      if (n % 100 === 7) return mk(n, { ts: 1756000000 - n * 3600, review: jp });
      return n % 10 < 4
        ? mk(n, { ts: 1756000000 - n * 3600, voted_up: true, review: shorts[n % shorts.length] + " " + n })
        : mk(n, { ts: 1756000000 - n * 3600, voted_up: n % 3 !== 0,
                  review: `The story and the writing carry this one, but the frame pacing in towns is rough. Run ${n}.` });
    });
    seq8 += 100;
    fulfil({ success: 1, query_summary: {}, reviews, cursor: "c" + seq8 });
  });
  await p8.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p8.waitForTimeout(500);

  // --- run A: the 5-word bar, rendered as Markdown ------------------------------------------
  // Pinned rather than defaulted since §24.1, which moved the defaults to the 10-word bar and
  // the HTML page. Both halves matter here: run C below is the HTML contrast and is only a
  // contrast if this one is Markdown, and the "ON at 5 words" disclosure is a distinct sentence
  // from the 10-word one — run C leaves the bar at its default and covers that.
  await p8.locator("button.gsub-rev").first().click();
  await p8.waitForSelector("#rdHost.on");
  await p8.locator('[data-rdsize="300"]').click();
  await p8.locator('[data-rdout="md"]').click();
  await p8.locator('[data-rdbar="5"]').click();
  // §23.5 — the counter is watched WHILE it runs, because that is the only place it exists.
  // A MutationObserver catches every value the reader would have seen; asserting the final
  // state only would pass on a counter that sat at 0% and jumped to 100%, which is exactly
  // the failure the percentage was introduced to fix.
  await p8.evaluate(() => {
    window.__prog = [];
    const host = document.getElementById("rdProg");
    new MutationObserver(() => {
      const el = host.querySelector(".rd-prog-pct");
      const w = host.querySelector(".rd-prog-bar i");
      if (el && el.textContent) window.__prog.push([el.textContent, w ? w.style.width : ""]);
    }).observe(host, { subtree: true, childList: true, characterData: true });
  });
  await p8.locator("#rdGo").click();
  await p8.waitForSelector("#rdOut", { timeout: 60000 });
  const barred = await p8.locator("#rdOut").inputValue();
  const pagesBar = pages8;
  const prog8 = await p8.evaluate(() => window.__prog);
  const headBar = (await p8.locator("#rdHeadCount").textContent()).trim();
  await p8.locator("#rdClose").click();

  // --- run B: same fixture, bar off --------------------------------------------------------
  pages8 = 0; seq8 = 0;
  await p8.locator("button.gsub-rev").first().click();
  await p8.waitForSelector("#rdHost.on");
  await p8.locator('[data-rdsize="300"]').click();
  await p8.locator('[data-rdbar="0"]').click();
  const offNote = await p8.locator("#rdCountQual").textContent();
  await p8.locator("#rdGo").click();
  await p8.waitForSelector("#rdOut", { timeout: 60000 });
  const unbarred = await p8.locator("#rdOut").inputValue();
  const pagesOff = pages8;
  const headOff = (await p8.locator("#rdHeadCount").textContent()).trim();
  await p8.locator("#rdClose").click();

  // --- run C: HTML output, bar back on ------------------------------------------------------
  await p8.locator("button.gsub-rev").first().click();
  await p8.waitForSelector("#rdHost.on");
  await p8.locator('[data-rdsize="300"]').click();
  await p8.locator('[data-rdout="html"]').click();
  await p8.locator("#rdGo").click();
  await p8.waitForSelector("#rdOut", { timeout: 60000 });
  const html8 = await p8.locator("#rdOut").inputValue();
  const readyNote = await p8.locator("#rdBody .rd-note").first().textContent();
  const words8 = await p8.evaluate(() => ({
    gg:   rdWords("gg"),
    ten:  rdWords("10/10"),
    four: rdWords("great combat, terrible optimisation"),
    punc: rdWords("!!! ... ---"),
    jp:   rdWords("神ゲーだけど最適化が酷いです"),   // 14 characters, zero spaces
  }));

  // §23.11 — the saver, driven the way the reader drives it: paste a whole chat reply, watch
  // the read-out, click Save, get a file. Asserting on rdExtractHtml alone would pass on a
  // panel that never rendered, which is the failure mode of every "it works in isolation" test.
  const saverInResult = await p8.locator("#rdSave").count();
  const saveName = (await p8.locator("#rdSave summary code").textContent()).trim();
  // Collapsed until asked for, which is the point of a <details> — so open it the way the
  // reader does. A test that reached into a hidden textarea would pass on a panel nobody can
  // get at.
  const shutFirst = await p8.locator("#rdHtmlIn").isVisible();
  await p8.locator("#rdSave summary").click();
  await p8.waitForSelector("#rdHtmlIn", { state:"visible" });
  const doc8 = '<!DOCTYPE html>\n<html lang="en"><head><style>:root{--ink-soft:#5a6470}</style>'
             + '<title>x — Steam review digest</title></head><body><div class="wrap">report</div></body>\n</html>';
  // The reply as it actually arrives: chatter, a fenced block, more chatter.
  await p8.locator("#rdHtmlIn").fill("Here's the digest for that game!\n\n```html\n" + doc8 + "\n```\n\nLet me know if you want it tweaked.");
  const sayGood = (await p8.locator("#rdHtmlSay").textContent()).trim();
  const canSaveGood = !(await p8.locator("#rdHtmlSave").isDisabled());
  // The §23.10 reply: an announcement and no document anywhere.
  await p8.locator("#rdHtmlIn").fill("<a_file_has_been_created_or_edited_view_it_in_the_drawer>\nstar-wars-zero-company.html\n</a_file_has_been_created_or_edited_view_it_in_the_drawer>\n\nI have compiled the digest into star-wars-zero-company.html.");
  const sayFake = (await p8.locator("#rdHtmlSay").textContent()).trim();
  const canSaveFake = !(await p8.locator("#rdHtmlSave").isDisabled());
  // Back to a good paste, and take the file.
  await p8.locator("#rdHtmlIn").fill("```html\n" + doc8 + "\n```");
  const [dl8] = await Promise.all([
    p8.waitForEvent("download"),
    p8.locator("#rdHtmlSave").click(),
  ]);
  const dlName = dl8.suggestedFilename();
  const dlBody = fs.readFileSync(await dl8.path(), "utf8");
  // And the recovery entry point: a reader who closed the dialog gets the saver without
  // paying for another fetch.
  await p8.locator("#rdClose").click();
  await p8.locator("button.gsub-rev").first().click();
  await p8.waitForSelector("#rdHost.on");
  const saverInSetup = await p8.locator("#rdSave").count();
  await b8.close();

  const count = b => Number((b.match(/^--- REVIEWS \((\d+)\) ---$/m) || [])[1]);
  const pct   = re => Number((barred.match(re) || [])[1]);

  // The addendum is a hard-wrapped markdown file, so a phrase in it can be split across a line
  // break at any time someone reflows a paragraph. Prose assertions run against a
  // whitespace-flattened copy: they are testing that the RULE is present, not how it wrapped.
  const flat8 = html8.replace(/\s+/g, " ");

  console.log("\nquality bar and HTML output:");
  // §23.3 — the number on the pill is the number in the bundle. With ~40% of the fixture
  // under the bar it takes five pages to fill 300, and the point of the whole change is that
  // it DOES fill it rather than handing back 180 and calling it 300.
  check(count(barred) === 300, `the bar still delivers the size that was asked for (${count(barred)})`);
  check(count(unbarred) === 300, `so does the walk with the bar off (${count(unbarred)})`);
  check(pagesBar > pagesOff, `filling 300 past the bar costs more pages (${pagesBar} vs ${pagesOff})`);
  check(/^  quality bar: ON at 5 words\. Short reviews skew positive/m.test(barred),
        "the bar names itself and why the two splits differ");
  check(/ · \d+ under the 5-word bar · /.test(barred), "and again on the excluded line");
  // Scoped to OVERVIEW on purpose: both prompt files now TALK about the bar (they have to —
  // one prompt serves both settings and has to say which line to read), so a whole-bundle
  // match here would pass on the instructions and never test the header at all.
  const over8 = b => b.slice(b.indexOf("--- OVERVIEW ---"), b.indexOf("LEGEND:"));
  check(!/quality bar:|before the bar:|under the \d+-word bar/.test(over8(unbarred)),
        "with the bar off the header says nothing about a filter that did not run");
  check(/quality bar:/.test(over8(barred)), "and says it plainly when it did");
  check(!/^BASIS  :/m.test(unbarred) && /^BASIS  : over the 300 reviews that cleared/m.test(barred),
        "TIMELINE declares its basis only when the basis changed");
  // The bias, reported rather than hidden. The one-liners in this fixture are all ▲, so the
  // pre-bar rate MUST come out above the post-bar one — if these ever match, the line has
  // stopped measuring anything and the model has no way to see the filter's effect.
  const preRate  = pct(/^  before the bar: \d+ up \/ \d+ down \((\d+)% positive\)/m);
  const postRate = pct(/^  sample split: \d+ up \/ \d+ down \((\d+)% positive\)/m);
  check(preRate > postRate,
        `the pre-bar split is reported and sits above the post-bar one (${preRate}% vs ${postRate}%)`);
  check(/Quote the sample split; the before-the-bar line is context, not a figure to report/.test(barred),
        "and it is labelled as the one number not to quote");
  // §23.1 — CJK. A whitespace split scores this review as one word and deletes it; every
  // Japanese, Chinese and Korean review in an "All languages" sample rides on this.
  check(barred.includes(jp), "a 14-character Japanese review survives the 5-word bar");
  check(words8.gg === 1 && words8.ten === 1 && words8.four === 4 && words8.jp === 14,
        `rdWords counts latin words, keeps "10/10" whole, and counts CJK per character (${JSON.stringify(words8)})`);
  check(words8.punc === 0, `punctuation alone is not words (${words8.punc})`);
  check(offNote.trim() === "Steam reviews",
        `turning the bar off corrects the sentence that quotes the size (${JSON.stringify(offNote)})`);

  // §23.2 — HTML output. The addendum has to arrive, has to say what it replaces, and the
  // Markdown run has to be untouched by any of it.
  check(/^--- OUTPUT FORMAT \(replaces the output skeleton in the INSTRUCTIONS above\) ---$/m.test(html8),
        "the addendum arrives under a header that says what it replaces");
  check(html8.indexOf("--- OUTPUT FORMAT") > html8.indexOf("--- INSTRUCTIONS ---") &&
        html8.indexOf("--- OUTPUT FORMAT") < html8.indexOf("--- OVERVIEW ---"),
        "it sits after the depth prompt and before the data");
  check(/prefers-color-scheme: dark/.test(html8) && /--ink-soft:/.test(html8),
        "the stylesheet rides in with it, dark mode included");
  check(/class="minibar"/.test(html8) && /m-down/.test(html8),
        "the Quit / stayed minibar recipe is carried, not left to invention");
  check(/Do not produce the Markdown report as well|NOT also produced/.test(html8),
        "the addendum forbids emitting both renderings");
  // html-v3 — the whole point of the option is that the reader ends up with a .html in their
  // downloads. The addendum has to ask for a written file, name the tool each chat writes it
  // with, and refuse the two things that merely LOOK like a file: a fenced block and a preview
  // pane. v2 asked only for "a file" and Claude answered with an Artifact, which is why the
  // panes are now named in the negative and this check reads for that.
  check(/file-creation \/ code tool/.test(html8) && /python tool/.test(html8) &&
        /create and provide a downloadable HTML file/i.test(flat8),
        "the addendum asks for a downloadable file and names the tool that writes it");
  check(/Do not answer with an Artifact/.test(html8) && /not a preview pane/i.test(html8),
        "and rules out the preview panes by name, not just by implication");
  // html-v4 — the failure the escape opened up: Gemini Flash, told to give a download link,
  // wrote a `sandbox:` URL (ChatGPT's scheme) for a file it never created, and Gemini resolved
  // it to a Google search. A link to nothing looks like success, which makes it worse than the
  // code block v1 produced, so the ban has to ride in the bundle rather than in a comment.
  check(/sandbox:/.test(html8) && /did not actually\s+create|you did not actually create/.test(html8),
        "the addendum bans links to files that were never created, sandbox: URLs by name");
  check(!/inside a single ```html fence|nothing outside it/.test(html8),
        "and no longer offers a fenced code block as the deliverable");
  // html-v5 — v4 banned Canvas in its opening rule and in the hand-over checklist, then told
  // Gemini to use Canvas in between. Gemini Flash took the bans, found its bullet's remaining
  // path impossible (it cannot attach a file), and dropped to the code-block escape — which was
  // gated on "only if you genuinely have neither", a fact no model can check about itself. The
  // fix is a decidable ladder, so the assertions are that the contradiction is GONE: Canvas is
  // no longer banned outright, the unverifiable gate is deleted, and the ladder is ordered.
  check(!/genuinely have neither/.test(html8),
        "the addendum no longer gates an escape on a fact the model cannot check about itself");
  // v5 built Gemini a Canvas-first ladder and v10 retired it (§23.12): every attempt at that
  // top rung produced mangled output, and the reader's own page now does the naming the rung
  // existed for. So the assertions invert — the ladder must be GONE, not ordered.
  check(!/rung/i.test(html8),
        "the ladder is retired: no rungs left to climb, mis-climb, or invent a third of");
  check(!/title the canvas/i.test(html8),
        "and nothing tells Gemini to title a canvas, which is what it wrote out as markup");
  check(/One chat reads the bans differently: Gemini/.test(flat8),
        "and the exemption is granted next to the bans it excepts, not a paragraph later");
  // html-v6 — v5's ladder was decidable and Gemini still went off it, because the bullet ABOVE
  // it named a real mechanism for writing a real file ("ChatGPT — use the python tool") and that
  // was the most concrete instruction on the page. Flash answered STAR WARS Zero Company with a
  // PYTHON block: `html_content = """<!DOCTYPE html>..."""` plus a write. Worse than a bare
  // fence — the block's download icon saves a .py, so the reader gets a script instead of a
  // page. The fix is a rule above all the bullets, a fence around the python tool in the same
  // breath it is granted, and rung 2 pinned to what the block CONTAINS.
  check(/never a program that writes it/i.test(flat8) && /html_content/.test(html8),
        "the addendum bans handing over code that writes the page, naming the shape that arrived");
  check(html8.indexOf("never a program that writes it") < html8.indexOf("python tool"),
        "and states that rule ABOVE the per-chat bullets, so no bullet can be read out of it");
  check(/ChatGPT's alone/.test(flat8) && /imitate it by printing a Python block/.test(flat8),
        "the python tool is fenced to ChatGPT in the same breath it is named");
  check(/do not print a Python block that writes the page/.test(flat8),
        "and Gemini's bullet names that imitation as the thing not to do");
  check(/its first characters are `<!DOCTYPE html>`/.test(flat8),
        "the block is still pinned to what it CONTAINS, not merely to what it is not");
  check(/There is nothing else to try/.test(flat8),
        "and the bullet closes itself, since every failure here has been an invented option");
  check(/not a program/i.test(html8.slice(html8.indexOf("Before you hand it over"))),
        "the hand-over checklist asserts the reply is HTML rather than a script");
  // html-v7 — v6 HELD: on STAR WARS Zero Company, Flash answered with rung 2 (an `html` block,
  // DOCTYPE to </html>, download icon on it), not a python block and not a sandbox: link. What
  // it never did was try rung 1, and two lines were talking it down there. The ask was phrased
  // as a MECHANISM Gemini's reply cannot perform — "write the page to a real file and attach
  // it" — so it read as not-for-me before the ladder arrived; and Gemini's own bullet opened
  // with "you have **no** way to attach a file to a reply", the addendum arguing the model out
  // of its tooling one line above instructing it to use it. v7 restates the ask as the OUTCOME,
  // in the vocabulary Gemini itself reports as its trigger for file output, and drops the denial.
  check(/create and provide a downloadable HTML file/i.test(flat8),
        "the ask is phrased as the outcome — a downloadable file — rather than as a mechanism");
  check(html8.indexOf("Create and provide a downloadable HTML file") < html8.indexOf("- **Claude**"),
        "and it leads the section, above the per-chat bullets");
  check(!/\*\*no\*\* way to attach a file|cannot attach a file to a reply at all/.test(html8),
        "Gemini's bullet no longer opens by denying it the tooling the next line prescribes");
  // v7 had Gemini's bullet lead with that same outcome sentence; v10 replaced the bullet
  // wholesale, so what survives of v7 here is the SECTION opening above and the deleted denial.
  check(/one `html` code block holding the document, and nothing else/i.test(flat8),
        "Gemini's bullet is one instruction now, not a ladder to be climbed");
  // The same run exposed a fidelity failure that is not about delivery at all: the page came
  // back with the stylesheet minified onto single lines and titled "STAR WARS Zero Company™ –
  // Steam Review Analysis". Both render identically and both defeat the point of shipping a
  // closed stylesheet — ten games are meant to produce ten pages of one publication, and that
  // fails at the browser tab as surely as at the palette.
  check(/Verbatim includes the whitespace/.test(flat8) && /do not minify it/.test(flat8),
        "verbatim is spelled out to include the whitespace, so minifying counts as an edit");
  check(/The `<title>` is exactly `<game title> — Steam review digest`/.test(flat8),
        "and the exact <title> string is a rule of its own, not left to the skeleton alone");
  // html-v8 — v7 made it WORSE, and in the way this series keeps rediscovering. Flash answered
  // STAR WARS Zero Company with `<a_file_has_been_created_or_edited_view_it_in_the_drawer>`
  // wrapping the filename, plus a sentence claiming the digest had been compiled into that file.
  // No Canvas, no block, no file: the drawer was empty and the report existed nowhere. That is
  // the §23.4 `sandbox:` failure in a third costume, and the imitated thing this time is the
  // INTERFACE's own file-saved message — the most convincing fake available, because a UI marker
  // does not read as a claim the model is making. It is also the first failure here to lose the
  // document outright; every earlier one shipped the page somewhere.
  //
  // Six versions ranked hand-overs against each other and never stated the thing underneath all
  // of them, so v8 states it above the bullets and above the ladder: THE REPLY HAS TO CARRY THE
  // DOCUMENT. Three things count — a Canvas holding it, a genuinely attached file, one `html`
  // block from `<!DOCTYPE html>` to `</html>` — and a sentence saying a file was created is not
  // one of them. It is the first rule in the series checkable against the reply itself rather
  // than against the model's beliefs about its tooling: "use Canvas" cannot be verified by a
  // model that believes it did.
  check(/The reply has to carry the document/.test(flat8),
        "the addendum states the invariant under every rung: the reply carries the page");
  check(html8.indexOf("The reply has to carry the document") < html8.indexOf("- **Claude**"),
        "and states it above the per-chat bullets, where no bullet can be read out of it");
  check(/a_file_has_been_created_or_edited_view_it_in_the_drawer/.test(html8),
        "the fabricated interface marker is named, the way html_content was named in v6");
  check(/Never announce a file instead of sending one/.test(flat8) &&
        /only the interface can produce them/.test(flat8),
        "and imitating the chat's own file-saved message is banned in words");
  check(/that tool did not run/.test(flat8),
        "a marker arriving instead of a document is defined as the rung failing to open");
  check(/nothing opened, so put the document in the code block instead/.test(flat8),
        "and names the recovery, rather than leaving it to the model's judgement");
  check(/The document is in the reply/.test(html8.slice(html8.indexOf("Before you hand it over"))),
        "and the hand-over checklist opens with the invariant, not with the table rules");
  // §23.11 — the saver. Eight versions of the addendum could not make a chat file the page, so
  // the page files it here: the reply is on the clipboard and this page knows the title, which
  // is everything the filename needs. These checks are the ones the prompt series never had —
  // deterministic, and identical whichever chat the reader used.
  check(saverInResult === 1, "the HTML result view carries the saver");
  check(shutFirst === false, "it starts collapsed, so a run that went fine costs one line of text");
  check(saverInSetup === 1, "and so does the setup view, so closing the dialog costs no re-fetch");
  check(/\.html$/.test(saveName) && saveName === saveName.toLowerCase() && !/\s/.test(saveName),
        `the saver names the file after the game, lowercased and hyphenated (${saveName})`);
  check(/Found the page/.test(sayGood) && canSaveGood,
        `a whole chat reply — chatter, fence and all — is read as the page (${JSON.stringify(sayGood)})`);
  check(/only ANNOUNCED a file/.test(sayFake) && !canSaveFake,
        `and the §23.10 reply is named as what it is, with Save held shut (${JSON.stringify(sayFake)})`);
  check(dlName === saveName, `the download lands under that exact name (${dlName})`);
  check(/^<!DOCTYPE html>/i.test(dlBody) && /<\/html>$/i.test(dlBody.trim()),
        "and holds the document itself, DOCTYPE to </html>, with the chatter stripped");
  check(!/```/.test(dlBody), "no fence markers survive into the saved file");
  // html-v10 — the ladder is retired, and the evidence is our own instruction coming back at
  // us. v5-v9 told Gemini to "put the document into Canvas and TITLE THE CANVAS <game>.html".
  // Flash cannot call that tool, so it did what the words say and wrote the tag: the reply was
  // `<canvas title="star-wars-zero-company.html">` wrapped around the page. Gemini labelled the
  // block XML because that is what it now was, its download icon saved a .xml, and the browser
  // refused it — "error on line 2 at column 2: StartTag: invalid element name", because a
  // DOCTYPE inside an XML document is invalid. Third distinct kind of garbage out of that one
  // rung (a marker in §23.10, a python block in §23.8, a wrapper here), and since §23.11 the
  // rung buys nothing: the reader's page does the naming it existed for.
  //
  // So Gemini's bullet is now one sentence — one `html` block holding the document — with the
  // wrapper banned by name and the reason given in the reader's terms (it downloads as .xml and
  // will not open). The bans it is excepted from are carved beside themselves rather than a
  // paragraph later (§23.7), because "not a fenced code block" three lines above "send a fenced
  // code block" is the contradiction that started this whole series.
  check(/Do not wrap the document in anything/.test(flat8) && /<canvas …>/.test(flat8),
        "the wrapper element is banned by name, the way html_content and the drawer marker were");
  check(/a panel does not open because you wrote its tag/.test(flat8),
        "with the reason stated as a fact about the world, not as a preference");
  check(/malformed XML/.test(flat8) && /\.xml/.test(flat8),
        "and the consequence given in the reader's terms: it downloads as .xml and will not open");
  check(/Gemini excepted/.test(html8) &&
        html8.indexOf("Gemini excepted") < html8.indexOf("- **Claude**"),
        "the code-block exception is carved beside the ban it excepts, not a paragraph later");
  check(/=== QTPD REVIEW DIGEST · prompt v\S+ \+ html-\S+ ===/.test(html8),
        "the title line carries both prompt versions, since two files shaped this output");
  // The whole point of an option: the run that did not ask for it pays nothing for it.
  check(!/OUTPUT FORMAT|prefers-color-scheme|minibar/.test(barred),
        "a Markdown run carries none of the HTML addendum");
  check(/=== QTPD REVIEW DIGEST · prompt v[^+]*===/.test(barred),
        "and its title line names one prompt, not two");
  check(/HTML page/.test(readyNote) && /\.html/.test(readyNote) && /paste the reply/i.test(readyNote),
        `the result panel says what comes back and where to paste it (${JSON.stringify(readyNote.trim().slice(0, 90))})`);
  check(errs8.length === 0, `no uncaught JS errors on either path (${errs8.length})`);

  // §23.5 — the fetch counter. A raw review count climbs 100/200/300 with the bar off and
  // 63/141/197 with it on; the second reads as a stall to anyone who does not know the drop
  // rate. The percentage is the same shape in both, which is the whole point.
  const pcts8 = prog8.map(([t]) => t).filter(t => /%$/.test(t)).map(t => parseInt(t, 10));
  check(pcts8.length >= 4, `the counter ticked several times during the fetch (${pcts8.length} updates)`);
  check(pcts8.every((v, i) => i === 0 || v >= pcts8[i - 1]),
        `it only ever climbs (${pcts8.join(" ")})`);
  check(pcts8[0] === 0 && pcts8[pcts8.length - 1] === 100,
        `it runs 0 -> 100, not part of the way (${pcts8[0]} -> ${pcts8[pcts8.length - 1]})`);
  check(pcts8.every(v => v >= 0 && v <= 100), `and never leaves the range (${Math.max(...pcts8)})`);
  // 100% belongs to a full sample. 299 of 300 rounding up while the fetch carries on is the
  // one reading of this counter that would be a lie, so the floor is asserted, not assumed.
  const midway = prog8.find(([t]) => t === "100%");
  check(!!midway && midway[1] === "100%", `the bar fills to match the number (${JSON.stringify(midway)})`);
  check(prog8.some(([, w]) => w && w !== "0%" && w !== "100%"),
        "the bar shows intermediate fill, not just empty and full");
  // §23.4 — what landed and what it cost, where the human reads it.
  check(/^300 reviews · \d+ filtered out of \d+$/.test(headBar),
        `the header pairs what landed with what was filtered (${JSON.stringify(headBar)})`);
  check(headOff === "300 reviews",
        `and says nothing about filtering when nothing was filtered (${JSON.stringify(headOff)})`);
}

// --- ninth scenario: reach — a 1-in-N page sample (plan §24.2) -------------------------
// The one thing this option must not do is quietly return a normal sample. Three failures
// are possible and all three are silent: keeping every page anyway (nothing gained, twice the
// wait), stopping at the page ceiling with a third of the sample (the ceiling multiplied in
// the wrong order), and delivering the gaps without telling the model they are gaps — which is
// the one that produces a WRONG report rather than a short one, since a thinned stream read as
// contiguous is a game whose reviews mysteriously halved.
//
// The fixture numbers every review, so which pages landed is readable off the bundle itself
// rather than inferred from a count: at reach 2, review numbers 1-100 and 201-300 and 401-500
// are in and 101-200 must be nowhere.
{
  const b9 = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const p9 = await b9.newPage();
  const errs9 = [];
  p9.on("pageerror", e => errs9.push(e.message));
  const cursors9 = [];
  let seq9 = 0;
  await p9.route("**/qtpd-reviews.*/**", route => {
    const q = new URL(route.request().url()).searchParams;
    const fulfil = body => route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(body) });
    if (q.get("num_per_page") === "0")
      return fulfil({ success: 1, reviews: [], query_summary: { total_reviews: 60000,
        total_positive: 45000, total_negative: 15000, review_score_desc: "Mostly Positive" } });
    cursors9.push(q.get("cursor"));
    const base = seq9; seq9 += 100;
    // 7200s apart: five pages of this fixture span ~41 days, enough for a real NOW/BEFORE.
    const reviews = Array.from({ length: 100 }, (_, i) =>
      mk(base + i + 1, { ts: 1756000000 - (base + i) * 7200 }));
    fulfil({ success: 1, query_summary: {}, reviews, cursor: "c" + seq9 });
  });
  await p9.goto("http://127.0.0.1:8099/index.html", { waitUntil: "networkidle" });
  await p9.waitForTimeout(500);
  await p9.locator("button.gsub-rev").first().click();
  await p9.waitForSelector("#rdHost.on");
  await p9.locator('[data-rdsize="300"]').click();
  await p9.locator('[data-rdreach="2"]').click();
  const reachOn9  = await p9.locator("[data-rdreach].on").allTextContents();
  const copy9     = (await p9.locator("#rdBody .rd-note").first().textContent()).replace(/\s+/g, " ");
  const entry9    = await p9.locator("button.gsub-rev").first().getAttribute("title");
  const goLabel9  = (await p9.locator("#rdGo").textContent()).trim();
  await p9.locator("#rdGo").click();
  await p9.waitForSelector("#rdOut", { timeout: 40000 });
  const b = await p9.locator("#rdOut").inputValue();
  await b9.close();

  console.log("\nreach — a 1-in-2 page sample:");
  check(reachOn9.length === 1 && reachOn9[0] === "Every 2nd page", `picking a reach lights exactly one (${reachOn9.join(",")})`);
  // Reach does not change the SIZE, and a button that started quoting a different number would
  // be describing a bundle nobody is going to get.
  check(goLabel9 === "Fetch 300 reviews", `the fetch button still quotes the size, not the reach (${JSON.stringify(goLabel9)})`);
  check(/one page of 100 in every 2/.test(copy9), `the dialog copy stops claiming a contiguous run (${JSON.stringify(copy9.slice(0, 110))})`);
  check(/sampled one page in 2/.test(entry9 || ""), `the entry-point tooltip follows it too (${JSON.stringify(entry9)})`);
  // Three kept pages at one skipped page between them: 1,2,3,4,5 fetched, 1,3,5 kept. Four
  // would mean the skips were not fetched (impossible — the cursor is chained) and six would
  // mean the loop kept walking after the count was met.
  check(cursors9.length === 5, `three kept pages cost five requests (${cursors9.length})`);
  check((b.match(/^--- REVIEWS \((\d+)\) ---$/m) || [])[1] === "300",
        `the sample is still full size (${(b.match(/^--- REVIEWS \((\d+)\) ---$/m) || [])[1]})`);
  // The actual skip, read off the reviews rather than off a counter that could agree with
  // itself while the fetch did something else.
  const nums = new Set((b.match(/Review number (\d+) with/g) || []).map(m => Number(m.match(/\d+/)[0])));
  check(nums.has(1) && nums.has(100), "page 1 is kept — the newest review is always in the sample");
  check(![...nums].some(n => n > 100 && n <= 200), `page 2 was discarded whole (${[...nums].filter(n => n > 100 && n <= 200).length} leaked)`);
  check(nums.has(201) && nums.has(401), "pages 3 and 5 are kept");
  // §24.2 — the disclosure. A thinned sample that does not say so is the failure mode that
  // produces a confidently wrong report instead of a visibly short one.
  check(/^  reach: every 2nd page of 100 kept — 2 pages \(200 reviews\) were fetched and passed over/m.test(b),
        "the header states the skip, in pages and in reviews");
  check(/1-in-2 sample of the same stream/.test(b) && /not missing, not deleted and not filtered/.test(b),
        "and says what the skipped reviews are, so the gaps are not read as quiet periods");
  check(/^          SAMPLING: 1 page in 2\./m.test(b),
        "COVERAGE carries the sampling factor beside the rate it distorts");
  check(/real review volume is about 2x it/.test(b) && /thinned by the same 2/.test(b),
        "the factor is applied to volume only, and proportions are declared untouched");
  // The rest of the bundle is an ordinary bundle: reach changes which reviews are in it, not
  // what is computed from them.
  check(/^TREND  : [+-]?\d+ pts/m.test(b), "TIMELINE still computes a trend over the thinned sample");
  check(errs9.length === 0, `no uncaught JS errors on the reach path (${errs9.length})`);
}

srv.close();
console.log(fails ? `\n${fails} CHECKS FAILED` : "\nALL CHECKS PASSED");
process.exit(fails ? 1 : 0);
