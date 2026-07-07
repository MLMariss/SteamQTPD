# SteamQHPP — Data Coverage Snapshot

**Registry date (`games.json` generated_at):** 2026-07-07 09:46 UTC
**Base universe:** 122,873 games

Per-file generation timestamps at snapshot time:

| File | Generated (UTC) |
|---|---|
| `games.json` | 2026-07-07 09:46 |
| `prices.json` | 2026-07-07 10:00 *(mid-run — see note)* |
| `hltb.json` | 2026-07-07 09:58 |
| `recent.json` | 2026-07-07 09:58 |
| `playtime.json` | 2026-07-07 09:56 |
| `ratings.json` | 2026-07-07 09:56 |
| `tags.json` | 2026-07-07 04:31 |
| `playtime_raw/` (shards) | newest 2026-07-07 08:22 · oldest 2026-07-06 06:27 |

---

## Coverage by metric

| Metric | Storage file | Covered | % of catalog | Missing | Freshness (newest / oldest) |
|---|---|---:|---:|---:|---|
| Review count | `games.json` | 122,873 | 100.0% | 0 | 0.3h / ~9d |
| Recent reviews | `recent.json` | 122,873 | 100.0% | 0 | re-gen ~5m ago |
| Tags | `tags.json` | 122,854 | 100.0% | 19 | re-gen ~5.5h ago |
| HLTB | `hltb.json` | 122,864 | 100.0% | 9 | re-gen ~5m ago |
| Release date | `games.json` | 122,871 | 100.0% | 2 | — |
| Rating % | `games.json` | 115,032 | 93.6% | 7,841 | 0.3h / ~9d |
| Price / Sales | `prices.json` | 15,300 *(mid-run)* | — | — | refresh in progress |
| Playtime raw | `playtime_raw/` | 17,200 | 14.0% | 105,673 | see note |
| Playtime (summarized) | `playtime.json` | 17,198 | 14.0% | 105,675 | re-gen ~7m ago |
| Playtime-weighted rating | `ratings.json` | 17,197 | 14.0% | 105,676 | re-gen ~7m ago |

Integrity: near-zero orphan keys (a handful per file, timing artifacts between scraper commits). Every storage file is effectively a clean subset of `games.json`.

---

## Notes

**Prices are mid-refresh at this snapshot — 15,300 is NOT the coverage figure.** The price job was bumped to `RUN_MINUTES=120` (from 60) on 2026-07-07 and was still inside a pass when this snapshot was taken; `prices.json` rebuilds fresh each run, so the count climbs from 0 toward the full non-free set (~104k) over the run. All 15,300 rows share a single `scraped_at` (09:55) — a mid-run checkpoint, not a stall. Steady-state price coverage is ~85% of catalog (the ~18k `is_free` titles carry no price). Re-check after the run completes for the true figure.

**Playtime/ratings backfill has more than doubled since the last snapshot — and ratings is unfrozen.** Raw playtime is now **17,200 games (14.0% of catalog)**, up from 6,856. Against the honest denominator — the addressable set after the `MIN_REVIEWS_FLOOR = 10` gate, **78,479 games (63.9% of catalog)** — raw is **21.9% of addressable**, up from 8.7%. The other **44,394 games are below the floor and correctly skipped** (they can't produce a sentiment-split median).

**Summarizer + ratings now track raw in lockstep (both fixes landed).** Raw 17,200 · summarized 17,198 · rated 17,197 — a spread of 1–3, not the 6,755 / stale-44h gaps flagged previously. Two changes drove this:
- **`ratings_summarize.py` was reading the retired `playtime_raw.json` monolith** and hitting `RAW_FILE.exists() == False` every run, silently exiting without writing — which froze `ratings.json` at 6,855 (07-05) through the shard migration. It now reads the 64 `playtime_raw/` shards via `iter_raw_shards()` (mirroring `playtime_summarize.py`), with fail-loud guards so a wiring bug can't silently pass green again (see ARCHITECTURE.md §10 → "Shard read + fail-loud").
- **Both summarizers were bumped from once-daily to every 4h** (`playtime-summary` at :47, `playtime-ratings` at :51). They're ~5s pure-local recomputes with no Steam budget cost, so daily cadence was needlessly lagging the ~8×/day raw scraper.

**HLTB estimates ARE present (previous "0 estimated" note was wrong).** 16,620 of 122,864 `hltb.json` entries carry the `est` flag — the estimator (`hltb_estimate.py`) has clearly run and populated them. The remaining gap is that `hltb_estimate.py` is **not wired into any workflow**, so estimates were written by a manual/one-off run and aren't refreshed as new HLTB reals land. Reframed as an open item below.

**Sales at snapshot** reflect the mid-run price file (9,227 of 15,300 discounted so far); the full sale count resolves once the price pass finishes.

**No price on ~18k games** ≈ the 18,084 `is_free` titles plus a handful delisted/region-locked.

---

## Open items

1. **Wire `hltb_estimate.py` into a workflow** — 16,620 estimates exist but are stale (written by a one-off run; no scheduled refresh). Add a scheduled job so estimates recompute as new HLTB reals arrive and the `est` set stays current. (Supersedes the old, incorrect "0 estimated" item.)
2. **Confirm the 120-min price budget finishes in one pass** — verify a full price run reaches `Done. Refreshed …` without hitting "Time budget reached … wrapping up", especially during a live Steam sale (the on-sale end-date pass is the heavy tail).
3. **Playtime backfill in progress** — still the primary coverage gap; the 8-slot / 1.5s raw scale-up is the active remedy and is visibly working (raw +10,344 since the 07-05 snapshot). Re-check after a few more days.
