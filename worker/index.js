/**
 * qtpd-reviews — Cloudflare Worker passthrough for Steam's review data: `appreviews` until
 * Valve switched it off on 22 Oct 2026, `IUserReviewsService/GetAppReviews` since.
 *
 * WHY THIS EXISTS
 * ---------------
 * The Phase 0 probe (REVIEW_DIGEST_PLAN.md §14) established that
 * `store.steampowered.com/appreviews/` returns **no** `Access-Control-Allow-Origin`
 * header, so a page on GitHub Pages cannot call it directly. QTPD is static — there is no
 * application server — so the only way to reach Steam from the browser is a proxy. This is
 * that proxy, and it is the sole backend the Review Digest needs. It keeps speaking
 * appreviews' query string and reply shape to the page, and translates both ways.
 *
 * ITS SOURCE LIVES IN THE REPO ON PURPOSE. The older wishlist Worker
 * (qhpp-wishlist.mlmariss.workers.dev) was deployed without its source in git, and it is
 * now unrecoverable — which is the entire reason this feature was expensive to scope. Do
 * not repeat that: edit this file, redeploy from it, keep them in sync.
 *
 * WHAT IT DELIBERATELY IS NOT
 * ---------------------------
 * Not a general-purpose proxy. It forwards exactly one upstream path shape with exactly one
 * allowlisted parameter set. Without those two constraints anyone who found the URL would
 * have an open relay to Steam's whole domain running on someone else's Cloudflare account.
 *
 * DEPLOY: see worker/README.md
 */

// The browser origins allowed to use this Worker. NOT "*" — an open CORS policy on an
// open-ish proxy is how a hobby Worker becomes someone else's free infrastructure.
// GitHub Pages project sites are served from the user origin, so the path
// (/SteamQTPD/) is not part of the origin and must not appear here.
const ALLOWED_ORIGINS = new Set([
  "https://mlmariss.github.io",
  "http://localhost:8000",       // local `python -m http.server` while developing
  "http://127.0.0.1:8000",
]);

// Valve switched `store.steampowered.com/appreviews` off on 22 Oct 2026 — it has redirected
// to the store front page since, so this Worker handed the page Steam's home page HTML. The
// replacement is GetAppReviews on the Web API host: no key, parameters in one `input_json`
// value, the reply wrapped in `response` with no `success` field.
const UPSTREAM = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/";

// The page still speaks appreviews' query string, so the Worker translates it. Steam IGNORES
// a field it doesn't recognise without an error and counts its defaults instead — English
// and Steam purchases only, which turned War Thunder's 792,828 into 834 — so every mapping
// below was measured on a runner (review_probe.py Q0, 25 Oct 2026), and a value nobody has
// measured is dropped rather than guessed. The allowlist is the same as before.
const ALLOWED_PARAMS = new Set([
  "filter",                    // recent | updated | all  -> FILTERS
  "language",                  // english | all | ...     -> languages: [value]
  "purchase_type",             // all                     -> 1
  "num_per_page",              // clamped to 1-100 below
  "cursor",                    // "*" then whatever Steam echoes back
  "day_range",                 // only meaningful with filter=all
  "filter_offtopic_activity",  // 0 includes review bombs — QTPD sends 0 (plan §5)
]);
// filter: 1 is newest-first over the whole list (total_matching = every review). The strings
// "recent" / "Recent" / "k_EUserReviewsFilterRecent" are ignored and leave the default,
// helpful-first over the last 30 days. 2 looks like "updated" but wasn't confirmed.
export const FILTERS = { recent: 1 };
const PURCHASE_TYPES = { all: 1 };

// Steam's numbers barely move in half an hour, and a digest is often re-opened moments
// after it is closed. Caching also keeps this Worker's shared egress IP well clear of the
// storefront rate limiter, which matters far more here than it does for the scrapers
// (they get a fresh runner IP; every user of this Worker shares one pool).
const CACHE_SECONDS = 1800;

// The store's age-gate cookies went with appreviews: the Web API host doesn't read them.
const UPSTREAM_HEADERS = {
  "User-Agent": "Mozilla/5.0 (steam-qtpd review-digest; +https://github.com/MLMariss/SteamQTPD)",
  "Accept-Language": "en-US,en;q=0.9",
};

/** CORS headers for a request, or null when the origin is not allowed. */
export function corsHeaders(origin) {
  if (!origin || !ALLOWED_ORIGINS.has(origin)) return null;
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Max-Age": "86400",
    // The allowed origin varies per request, so caches must key on it.
    "Vary": "Origin",
  };
}

/**
 * Build the upstream Steam URL from a caller's params.
 * Exported so the allowlist and the appid validation are unit-testable without a runtime.
 * Returns {url} or {error, status}.
 */
export function buildUpstreamUrl(searchParams) {
  const appid = searchParams.get("appid");
  // Steam appids are plain integers. Anchored, digits only, bounded — this is what stops
  // the Worker being pointed at anything but one game's reviews.
  if (!appid || !/^[0-9]{1,8}$/.test(appid)) {
    return { error: "appid must be 1-8 digits", status: 400 };
  }

  // Built in a fixed key order, so two callers who ordered their query string differently
  // still produce the same upstream URL — and share one cache entry.
  const input = { appid: Number(appid) };
  const get = (k) => (ALLOWED_PARAMS.has(k) ? searchParams.get(k) : null);

  const filter = get("filter");
  if (filter != null && Object.hasOwn(FILTERS, filter)) input.filter = FILTERS[filter];

  const language = get("language");
  if (language != null && /^[a-z_]{2,24}$/.test(language)) input.languages = [language];

  const purchase = get("purchase_type");
  if (purchase != null && Object.hasOwn(PURCHASE_TYPES, purchase)) input.purchase_type = PURCHASE_TYPES[purchase];

  const perPage = get("num_per_page");
  if (perPage != null) {
    // Steam's hard max is 100; clamp rather than reject so a caller asking for more gets a
    // page instead of a failure. appreviews took 0 to mean "summary only", but GetAppReviews
    // doesn't honour 0 and serves its default 20 — so 0 becomes 1, the cheapest page that
    // still carries query_summary, which is all the page's anchor call reads.
    const parsed = parseInt(perPage, 10);
    input.num_per_page = Number.isNaN(parsed) ? 100 : Math.min(100, Math.max(1, parsed));
  }

  const cursor = get("cursor");
  if (cursor != null && cursor.length <= 200) input.cursor = cursor;

  const dayRange = parseInt(get("day_range"), 10);
  if (!Number.isNaN(dayRange)) input.day_range = Math.min(3650, Math.max(1, dayRange));

  const offtopic = get("filter_offtopic_activity");
  if (offtopic === "0" || offtopic === "1") input.filter_offtopic_activity = offtopic === "1";

  return { url: `${UPSTREAM}?input_json=${encodeURIComponent(JSON.stringify(input))}` };
}

/**
 * GetAppReviews' reply in appreviews' shape — `success: 1` beside query_summary, reviews and
 * cursor at the top level — so the page reads it unchanged, or null when it carries none of
 * the three (the page then reports "Steam returned no data" rather than parsing something
 * else). Any one is enough: only the first page was measured to carry query_summary, and a
 * later page that lacks it is still a page of reviews.
 */
export function toLegacyShape(data) {
  const resp = data && typeof data === "object" ? data.response : null;
  if (!resp || typeof resp !== "object") return null;
  if (!("query_summary" in resp || "reviews" in resp || "cursor" in resp)) return null;
  return { success: 1, ...resp };
}

function json(body, status, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...(extraHeaders || {}) },
  });
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin");
    const cors = corsHeaders(origin);

    if (request.method === "OPTIONS") {
      // A plain GET with no custom headers is a "simple request" and browsers will not
      // preflight it — but answer correctly anyway rather than 405 a legitimate probe.
      return cors
        ? new Response(null, { status: 204, headers: cors })
        : new Response(null, { status: 403 });
    }

    if (request.method !== "GET") {
      return json({ error: "method not allowed" }, 405, cors || {});
    }

    // A browser request from an origin we do not serve is refused. Note this is a
    // courtesy boundary, not a security one: Origin is only sent by browsers and anything
    // else can omit or forge it. The real protections are the appid regex and the param
    // allowlist above, which hold regardless of who is calling.
    if (origin && !cors) {
      // Echo the caller's origin on THIS response only, so the browser can actually read
      // the message. Without it the rejection is opaque — fetch() just throws "Failed to
      // fetch" and the page cannot tell a disallowed origin from a dead URL or bad DNS,
      // which is exactly the dead end this cost once already. The body carries no data,
      // only the reason, so letting an unlisted origin read it gives nothing away.
      return json({ error: "origin not allowed", origin, allowed: [...ALLOWED_ORIGINS] }, 403,
                  { "Access-Control-Allow-Origin": origin, "Vary": "Origin" });
    }

    const url = new URL(request.url);
    if (!url.searchParams.has("appid")) {
      return json({
        service: "qtpd-reviews",
        usage: "/?appid=<steam appid>&filter=recent&language=english&num_per_page=100&cursor=*",
        source: "https://github.com/MLMariss/SteamQTPD/tree/main/worker",
      }, 200, cors || {});
    }

    const built = buildUpstreamUrl(url.searchParams);
    if (built.error) return json({ error: built.error }, built.status, cors || {});

    // Cache on the normalised upstream URL, so two callers who ordered their query string
    // differently still share one entry.
    const cacheKey = new Request(built.url, { method: "GET" });
    const cache = caches.default;

    let upstream = await cache.match(cacheKey);
    let cacheStatus = "HIT";

    if (!upstream) {
      cacheStatus = "MISS";
      let fetched;
      try {
        fetched = await fetch(built.url, { headers: UPSTREAM_HEADERS });
      } catch (e) {
        return json({ error: "upstream fetch failed", detail: String(e) }, 502, cors || {});
      }

      if (!fetched.ok) {
        // Pass the real status through (429 and 403 are the ones that matter) but do NOT
        // cache a failure — the next caller should get a fresh attempt.
        return json({ error: "upstream error", status: fetched.status },
                    fetched.status === 429 ? 429 : 502, cors || {});
      }

      // Rebuild the response rather than forwarding Steam's: its Set-Cookie (browserid,
      // steamCountry) is useless to the page, would be stored against this Worker's own
      // domain, and must never be written into the shared cache. The body is translated to
      // appreviews' shape; a reply without review data is an error, and is not cached.
      let legacy = null;
      try { legacy = toLegacyShape(JSON.parse(await fetched.text())); } catch (e) {}
      if (!legacy) {
        return json({ error: "Steam answered without review data" }, 502, cors || {});
      }
      upstream = new Response(JSON.stringify(legacy), {
        status: 200,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": `public, max-age=${CACHE_SECONDS}`,
        },
      });
      // Cache the CORS-free copy; the per-origin headers are added on the way out below.
      ctx.waitUntil(cache.put(cacheKey, upstream.clone()));
    }

    const headers = new Headers(upstream.headers);
    for (const [k, v] of Object.entries(cors || {})) headers.set(k, v);
    headers.set("X-QTPD-Cache", cacheStatus);
    return new Response(upstream.body, { status: 200, headers });
  },
};
