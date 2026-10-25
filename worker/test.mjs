// Unit tests for the parts of the Worker that hold regardless of runtime: the appid
// validation, the parameter allowlist and its translation to GetAppReviews, and the reply
// translation back. These are what stop it being an open proxy, so
// they are the parts worth testing. Run: node worker/test.mjs
import { buildUpstreamUrl, corsHeaders, toLegacyShape, FILTERS } from "./index.js";

let fail = 0;
const ok = (cond, msg) => { if (!cond) { console.error("  FAIL:", msg); fail++; } else console.log("  ok:", msg); };
const build = (qs) => buildUpstreamUrl(new URL("https://w.dev/?" + qs).searchParams);
// The input_json GetAppReviews is sent, parsed back out of the upstream URL.
const sent = (qs) => {
  const u = new URL(build(qs).url);
  return { host: u.host, path: u.pathname, keys: [...u.searchParams.keys()],
           input: JSON.parse(u.searchParams.get("input_json")) };
};

console.log("appid validation");
{
  const s = sent("appid=570");
  ok(s.host === "api.steampowered.com" && s.path === "/IUserReviewsService/GetAppReviews/v1/",
     "upstream is GetAppReviews on the Web API host (appreviews was switched off 22 Oct 2026)");
  ok(s.keys.length === 1 && s.keys[0] === "input_json", "everything travels in input_json");
  ok(JSON.stringify(s.input) === '{"appid":570}', "plain appid builds {appid} as a number");
}
ok(build("").error, "missing appid rejected");
ok(build("appid=").error, "empty appid rejected");
ok(build("appid=abc").error, "non-numeric appid rejected");
ok(build("appid=570/../../search").error, "path traversal rejected");
ok(build("appid=-1").error, "negative appid rejected");
ok(build("appid=999999999").error, "9-digit appid rejected (out of range)");
ok(build("appid=570%20").error, "trailing space rejected");

console.log("translation to GetAppReviews (spellings measured 25 Oct 2026)");
{
  const i = sent("appid=570&filter=recent&language=english&purchase_type=all&filter_offtopic_activity=0").input;
  ok(i.filter === FILTERS.recent && i.filter !== undefined, "filter=recent -> the measured newest-first value");
  ok(JSON.stringify(i.languages) === '["english"]' && !("language" in i), "language -> languages list (`language` is ignored by Steam)");
  ok(i.purchase_type === 1, "purchase_type=all -> 1 (\"all\" is ignored by Steam)");
  ok(i.filter_offtopic_activity === false, "offtopic 0 -> false (include review bombs)");
}
{
  const i = sent("appid=570&language=all").input;
  ok(JSON.stringify(i.languages) === '["all"]', "language=all -> languages [\"all\"]");
}
{
  const i = sent("appid=570&filter=bogus&purchase_type=steam&language=EN%22").input;
  ok(!("filter" in i), "unmeasured filter dropped, not guessed");
  ok(!("purchase_type" in i), "unmeasured purchase_type dropped, not guessed");
  ok(!("languages" in i), "malformed language dropped");
}
{
  const a = build("appid=570&cursor=*&filter=recent&language=all").url;
  const b = build("language=all&filter=recent&cursor=*&appid=570").url;
  ok(a === b, "query-string order doesn't change the upstream URL (one cache entry)");
}

console.log("param allowlist");
{
  const i = sent("appid=570&evil=1&redirect=http://x&key=secret&json=0&review_type=negative").input;
  ok(JSON.stringify(Object.keys(i)) === '["appid"]', "unknown params dropped (incl. key, redirect, json, review_type)");
}

console.log("num_per_page clamping");
const npp = (v) => sent("appid=570&num_per_page=" + v).input.num_per_page;
ok(npp("5000") === 100, "over-max clamped to 100");
ok(npp("50") === 50, "in-range preserved");
ok(npp("0") === 1, "zero -> 1: GetAppReviews ignores 0 (serves 20), 1 is the cheapest summary call");
ok(npp("-5") === 1, "negative clamped to 1");
ok(npp("junk") === 100, "garbage falls back to 100");

console.log("reply translation to appreviews' shape");
{
  const r = toLegacyShape({ response: { query_summary: { total_positive: 5, total_negative: 1 },
                                        reviews: [{ recommendationid: "1" }], cursor: "AoJ" } });
  ok(r && r.success === 1, "success: 1 added (the page checks it)");
  ok(r.query_summary.total_positive === 5 && r.reviews.length === 1 && r.cursor === "AoJ",
     "query_summary, reviews and cursor lifted out of `response`");
  const p2 = toLegacyShape({ response: { reviews: [{ recommendationid: "2" }], cursor: "AoK" } });
  ok(p2 && p2.success === 1 && p2.reviews.length === 1, "a later page without query_summary is still a page");
  ok(toLegacyShape({ response: {} }) === null, "empty response -> null (an error, never cached)");
  ok(toLegacyShape(null) === null && toLegacyShape("<!DOCTYPE html>") === null, "non-object -> null");
}

console.log("CORS origin allowlist");
ok(corsHeaders("https://mlmariss.github.io")["Access-Control-Allow-Origin"] === "https://mlmariss.github.io", "pages origin allowed");
ok(corsHeaders("https://evil.example") === null, "unknown origin refused");
ok(corsHeaders(null) === null, "absent origin yields no CORS headers");
ok(corsHeaders("https://mlmariss.github.io").Vary === "Origin", "Vary: Origin set");

console.log(fail ? `\n${fail} FAILED` : "\nall passed");
process.exit(fail ? 1 : 0);
