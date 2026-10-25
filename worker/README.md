# `qtpd-reviews` — the Review Digest proxy

The one piece of backend the Review Digest needs, and the reason it needs it:

> **Phase 0 probe finding (REVIEW_DIGEST_PLAN.md §14):** `store.steampowered.com/appreviews/`
> returns **no** `Access-Control-Allow-Origin` header. A page on GitHub Pages therefore
> cannot call it, and QTPD has no application server. This Worker is the bridge.

**This source is in git on purpose.** The older wishlist Worker
(`qhpp-wishlist.mlmariss.workers.dev`) was deployed without its source ever being committed,
and it is now unrecoverable — which is precisely why this feature was expensive to scope.
Edit this file, deploy from this file, keep the two in sync. Never patch it only in the
dashboard.

## Deploy

Either route works; pick one and stay with it.

**Wrangler (preferred — the source stays authoritative):**

```bash
cd worker
npx wrangler login      # once
npx wrangler deploy
```

**Dashboard:** Cloudflare → Workers & Pages → Create → paste `index.js` → Deploy. If you do
this, commit any edit you made in the editor straight back to this file.

Then point the frontend at the deployed URL — one constant near the Review Digest code in
`index.html`:

```js
const REVIEWS_PROXY = "https://qtpd-reviews.<your-subdomain>.workers.dev";
```

## Check it works

```bash
# usage banner
curl "https://qtpd-reviews.<sub>.workers.dev/"

# a real page of reviews
curl "https://qtpd-reviews.<sub>.workers.dev/?appid=1091500&filter=recent&language=english&num_per_page=5&cursor=*"

# the CORS header the whole thing exists to add
curl -sI -H "Origin: https://mlmariss.github.io" \
  "https://qtpd-reviews.<sub>.workers.dev/?appid=570&num_per_page=1&cursor=*" \
  | grep -i access-control-allow-origin

# second identical call should report X-QTPD-Cache: HIT
```

## What it does, and deliberately does not

| | |
|---|---|
| **Forwards** | exactly one upstream call — `IUserReviewsService/GetAppReviews` for one numeric appid — built from the parameters in `ALLOWED_PARAMS` (plan §4). Everything else the caller sends is dropped. Until 22 Oct 2026 this was `store.steampowered.com/appreviews`, which Valve switched off (it redirects to the store front page) |
| **Translates** | the page's appreviews-style query string into GetAppReviews' `input_json` (`language` → `languages: [...]`, `purchase_type=all` → `1`, `filter=recent` → the measured newest-first value, `filter_offtopic_activity` → a boolean), and the reply back into appreviews' shape (`success: 1` beside `query_summary`, `reviews`, `cursor`). Steam silently ignores a spelling it doesn't know and counts its defaults instead, so only measured mappings exist; anything else is dropped, never guessed (ARCHITECTURE §16) |
| **`num_per_page`** | clamped to 1–100. A caller's **0** ("summary only", the bundle header's cheap anchor call) becomes **1**: GetAppReviews doesn't honour 0 and serves its default 20 |
| **CORS** | scoped to `ALLOWED_ORIGINS`, never `*` |
| **Caches** | 30 min at the edge, keyed on the normalised upstream URL. Keeps the shared Worker IP clear of Steam's rate limiter — which matters more here than for the scrapers, since they get a fresh runner IP and every user of this Worker shares one pool |
| **Strips** | Steam's `Set-Cookie` (`browserid`, `steamCountry`) — useless to the page and must never enter the shared cache |
| **Does not** | forward arbitrary paths, take an API key, log anything, or store state |

The appid regex and the parameter allowlist are what stop this being an open relay to Steam's
whole domain on someone else's Cloudflare account. The `Origin` check is a courtesy boundary
on top — only browsers send `Origin`, and anything else can forge it — so those two are the
protections that actually hold.

## Tests

```bash
node worker/test.mjs
```

Covers the appid validation (traversal, non-numeric, out-of-range), the parameter allowlist and
its translation to GetAppReviews, `num_per_page` clamping including the zero case, the reply
translation back to appreviews' shape, and the CORS origin allowlist. It needs no
Cloudflare runtime — these are pure functions, exported for exactly that reason.
