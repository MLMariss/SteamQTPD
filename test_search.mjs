// test_search.mjs — the title search box's query language (ARCHITECTURE.md §11, "Title search
// query language").
//
// Loads the REAL index.html in Chromium with every JSON layer absent, so the page boots on its
// built-in SAMPLE catalogue, then runs the page's own parseQ() / qPass() / qTypo() against a
// small synthetic catalogue whose answers are known. The live catalogue changes hourly; these
// titles don't, so a failure here is the parser, not the data.
//
// Playwright is NOT a dependency of this repo — everything else here is Python — so:
//     npm install playwright
//     node test_search.mjs

import { chromium } from "playwright";
import http from "http"; import fs from "fs"; import path from "path";

const ROOT = process.env.RD_ROOT || process.cwd();
const srv = http.createServer((req, res) => {
  const f = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]));
  if (!f.endsWith("index.html") || !fs.existsSync(f)) { res.writeHead(404); res.end("nope"); return; }
  res.writeHead(200, { "Content-Type": "text/html" });
  res.end(fs.readFileSync(f));
});
await new Promise(r => srv.listen(8098, r));

const browser = await chromium.launch();
const page = await browser.newPage();
const errs = [];
page.on("pageerror", e => errs.push(String(e)));
await page.route(u => !u.href.startsWith("http://127.0.0.1:8098/"), r => r.abort());
await page.goto("http://127.0.0.1:8098/index.html", { waitUntil: "networkidle" });
await page.waitForFunction(() => typeof state !== "undefined" && state.data && state.data.games.length > 0);

const r = await page.evaluate(() => {
  const day = (y, m, d) => Date.UTC(y, m, d) / 1000;
  const G = [
    { appid: 1,  title: "Scape", upcoming: true, due_ts: null },
    { appid: 2,  title: "Side Scape", release_ts: day(2021, 4, 1), review_count: 40 },
    { appid: 3,  title: "Construct: Escape the System", release_ts: day(2016, 11, 1), review_count: 173 },
    { appid: 4,  title: "Scapeland", release_ts: day(2023, 1, 1), review_count: 12 },
    { appid: 5,  title: "Half-Life 2", release_ts: day(2004, 10, 16), review_count: 200000 },
    { appid: 6,  title: "The Sims™ 4", release_ts: day(2014, 8, 2), review_count: 200545 },
    { appid: 7,  title: "Pokémon Café", release_ts: day(2019, 0, 1), review_count: 50 },
    { appid: 8,  title: "DARK SOULS™ III", release_ts: day(2016, 3, 11), review_count: 300000 },
    { appid: 9,  title: "DARK SOULS™: REMASTERED", release_ts: day(2018, 4, 23), review_count: 90000 },
    { appid: 10, title: "ELDEN RING", release_ts: day(2022, 1, 25), review_count: 900000 },
    { appid: 11, title: "逃出图书馆(Escape from Library)", release_ts: day(2020, 4, 1), review_count: 10 },
    { appid: 12, title: "The Witcher 3: Wild Hunt", release_ts: day(2015, 4, 18), review_count: 700000 },
    { appid: 13, title: "Future Thing", upcoming: true, due_ts: day(2031, 2, 31) },
    { appid: 14, title: "Simson Tuningwerkstatt 3D", release_ts: day(2015, 0, 1), review_count: 452 },
  ];
  const ids = q => G.filter(g => qPass(g, parseQ(q))).map(g => g.appid).join(",");
  const out = { ids: {} };
  for (const q of ["scape", '"scape"', '="Scape"', '="scape', "“scape”", "half life", "life half", "half-life",
                   "the sims 4", "pokemon cafe", '"dark souls"', "dark souls -remastered", '-"dark souls" iii',
                   "elden OR witcher", "elden | witcher", "elden or witcher", "dark souls OR ring", "OR elden",
                   "逃出", "year:2016", "year:2015-2016", "before:2005", "after:2021", "after:2021-04",
                   "-year:2016 dark", '"dark sou', "year:20", '"year:2016"', "- = ™"])
    out.ids[q] = ids(q);
  const P = q => parseQ(q);
  out.bad = P("portal year:20 before:2024-13").bad.join(" ");
  out.named = ["x", "year:2020", "-x", "-x y", "after:2022 -horror", '="X"'].map(q => P(q).named).join(",");
  out.future = ["year:2031", "after:2026-12 OR year:2040", "year:2026", "after:2021", "-year:2031", "x year:2031"]
    .map(q => P(q).future).join(",");
  // simSugRank tiers: 0 whole title, 1 every word starts a title word, 2 buried mid-word.
  const rank = (q, id) => simSugRank(G.find(g => g.appid === id), P(q));
  out.rank = [rank("scape", 1), rank("scape", 2), rank("scape", 3), rank("sims", 6), rank("sims", 14),
              rank("dark souls", 8)].join(",");

  // The defaults a typed search lifts, and the ones an operator-only query must not.
  const save = state.q, lifts = q => { state.q = q; return [adultMode(), releaseMode(), searching()].join("/"); };
  out.lifts = { scape: lifts("scape"), y2020: lifts("year:2020"), excl: lifts("-horror"), y2031: lifts("year:2031") };

  // Did you mean — against the synthetic catalogue, the real one put back after.
  const real = state.data.games;
  state.data.games = G; VOCAB = null;
  const fix = q => { state.q = q; const t = qTypo(); return t ? String(t.fix) : "(words match)"; };
  out.fix = { witchr: fix("witchr 3"), halflife: fix("halflife 2"), dakr: fix('dakr "souls"'),
              ok: fix("scape"), none: fix("zzzzqqq"), exact: fix('="Scpe"') };
  state.data.games = real; VOCAB = null; TYPO_KEY = null;

  // Picking a suggestion writes the exact form; quotes inside the title are dropped.
  pickTitle('Scape "Deluxe"');
  out.pick = state.q;
  out.chip = document.querySelector('#filterSummary [data-sumf="q"]')?.textContent || "(no chip)";
  state.q = save;
  return out;
});

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok:  " : "  FAIL:") + " " + msg); if (!cond) fails++; };
const want = {
  "scape": "1,2,3,4,11",                  // plain typing still matches inside words
  '"scape"': "1,2",                       // a phrase is whole words: not Escape, not Scapeland
  '="Scape"': "1",                        // the exact title — what a picked suggestion writes
  '="scape': "1,4",                       // unclosed: the title so far
  "“scape”": "1,2",                       // smart quotes are quotes
  "half life": "5", "life half": "5", "half-life": "5",
  "the sims 4": "6",                      // ™ ignored
  "pokemon cafe": "7",                    // accents folded
  '"dark souls"': "8,9",
  "dark souls -remastered": "8",
  '-"dark souls" iii': "",
  "elden OR witcher": "10,12", "elden | witcher": "10,12",
  "elden or witcher": "",                 // lower-case "or" is a word, as on Google
  "dark souls OR ring": "8,9",            // dark AND (souls OR ring)
  "OR elden": "10",                       // a leading OR has nothing to join
  "逃出": "11",                           // other scripts keep their letters
  "year:2016": "3,8", "year:2015-2016": "3,8,12,14",
  "before:2005": "5",
  "after:2021": "4,10,13",                // plain English: from 2022; an upcoming game by its due day
  "after:2021-04": "2,4,10,13",
  "-year:2016 dark": "9",
  '"dark sou': "8,9",                     // an unclosed phrase matches as typed so far
  "year:20": "1,2,3,4,5,6,7,8,9,10,11,12,13,14",   // unreadable: ignored, not guessed at
  '"year:2016"': "",                      // quoted, it is text
  "- = ™": "1,2,3,4,5,6,7,8,9,10,11,12,13,14",     // operators with nothing after them filter nothing
};
for (const [q, w] of Object.entries(want)) check(r.ids[q] === w, `${q}  →  [${r.ids[q]}]${r.ids[q] === w ? "" : ` (want [${w}])`}`);
check(r.bad === "year:20 before:2024-13", `unreadable date operators are reported: ${r.bad}`);
check(r.named === "true,false,false,true,false,true", `qNamed: only a title word or phrase names a game (${r.named})`);
check(r.future === "true,true,false,false,false,true", `wholly-future date groups (${r.future})`);
check(r.rank === "0,1,2,1,1,1", `suggestion tiers: exact 0, word start 1, mid-word 2 (${r.rank})`);
check(r.lifts.scape === "any/all/true", `a typed title lifts adult, Released and the review floor (${r.lifts.scape})`);
check(r.lifts.y2020 === "hide/released/false", `year:2020 alone lifts nothing — a broad list (${r.lifts.y2020})`);
check(r.lifts.excl === "hide/released/false", `-horror alone lifts nothing (${r.lifts.excl})`);
check(r.lifts.y2031 === "hide/all/false", `a wholly-future year lifts only the Released default (${r.lifts.y2031})`);
check(r.fix.witchr === "witcher 3", `did you mean: witchr 3 → ${r.fix.witchr}`);
check(r.fix.halflife === "half life 2", `did you mean: halflife 2 → ${r.fix.halflife}`);
check(r.fix.dakr === 'dark "souls"', `did you mean keeps the quotes: ${r.fix.dakr}`);
check(r.fix.ok === "(words match)", "no correction while the words match a title");
check(r.fix.none === "null", "nothing close: no correction offered");
check(r.fix.exact === '="scape"', `an exact title is corrected in its own form (case is ignored): ${r.fix.exact}`);
check(r.pick === '="Scape Deluxe"', `picking a suggestion writes the exact form: ${r.pick}`);
check(r.chip === '="Scape Deluxe"', `the summary chip shows a quoted query as typed: ${r.chip}`);
check(errs.length === 0, `no uncaught JS errors (${errs.join(" | ") || 0})`);

await browser.close();
srv.close();
console.log(fails ? `\n${fails} CHECKS FAILED` : "\nALL CHECKS PASSED");
process.exit(fails ? 1 : 0);
