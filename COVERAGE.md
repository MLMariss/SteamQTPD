# SteamQHPP — Data Coverage Snapshot

**Registry date (`games.json` generated_at):** 2026-07-07 09:46 UTC
**Base universe:** 122,873 games

Per-file generation timestamps at snapshot time:

| File | Generated (UTC) |
|---|---|
| `games.json` | 2026-07-07 09:46 |
| `prices.json` | 2026-07-07 11:02 *(run complete)* |
| `hltb.json` | 2026-07-07 10:41 |
| `recent.json` | 2026-07-07 09:58 |
| `playtime.json` | 2026-07-07 10:11 |
| `ratings.json` | 2026-07-07 10:10 |
| `tags.json` | 2026-07-07 10:06 |
| `playtime_raw/` (shards) | newest 2026-07-07 11:09 · oldest 2026-07-07 11:09 |

---

## Coverage by metric

| Metric | Storage file | Covered | % of catalog | Missing | Freshness (newest / oldest) |
|---|---|---:|---:|---:|---|
| Review count | `games.json` | 115,032 | 93.6% | 7,841 | 0.3h / ~9d |
| Recent reviews | `recent.json` | 122,873 | 100.0% | 0 | re-gen ~1h ago |
| Tags | `tags.json` | 83,439 *(non-empty)* | 67.9% | 39,434 | re-gen ~1h ago |
| HLTB (total) | `hltb.json` | 122,864 | 100.0% | 9 | re-gen ~30m ago |
| HLTB (real) | `hltb.json` | 106,218 | 86.4% | — | — |
| HLTB (estimated) | `hltb.json` | 16,646 | 13.5% | — | see note |
| Release date | `games.json` | 122,871 | 100.0% | 2 | — |
| Rating % | `games.json` | 111,755 | 91.0% | 11,118 | 0.3h / ~9d |
| Price / Sales | `prices.json` | 104,788 | 85.3% | — | run complete |
| Playtime raw | `playtime_raw/` | 18,088 | 14.7% | 104,785 | see note |
| Playtime (summarized) | `playtime.json` | 17,198 | 14.0% | 105,675 | re-gen ~1h ago |
| Playtime-weighted rating | `ratings.json` | 17,197 | 14.0% | 105,676 | re-gen ~1h ago |

Integrity: near-zero orphan keys (a handful per file, timing artifacts between scraper commits). Every storage file is effectively a clean subset of `games.json`.

---

## Notes

**Prices are complete at this snapshot — full non-free coverage reached.** The 120-min price job finished a clean pass: `prices.json` holds **104,788 rows (85.3% of catalog)**, matching the non-free base almost exactly (122,873 − 18,084 `is_free` = 104,789). The 120-min budget bump did the job — the run reached the full non-free set in one pass with no "time budget reached" wrap-up. This resolves Open Item #2.

**A live Steam sale is on.** **61,118 of 104,788 priced titles (58.3%) are currently discounted** — far above baseline, consistent with a major seasonal sale. Sale end-dates are populated via the `IStoreBrowseService/GetItems` pass.

**Playtime/ratings backfill keeps climbing.** Raw playtime is now **18,088 games (14.7% of catalog)**, up from 17,200 at the prior snapshot. Against the honest denominator — the addressable set after the `MIN_REVIEWS_FLOOR = 10` gate, **78,479 games (63.9% of catalog)** — raw is **23.0% of addressable**. The other **44,394 games are below the floor and correctly skipped** (they can't produce a sentiment-split median).

**Summarizer/ratings currently lag raw by ~890 — this is a timing artifact, not the frozen-pipeline bug.** Raw 18,088 · summarized 17,198 · rated 17,197. The raw scraper committed a fresh batch at 11:09, *after* the summarizers last ran (10:11 / 10:10); the next 4-hourly summarize pass will close the gap. This is the expected sawtooth between an ~8×/day raw scraper and 4-hourly summarizers, not the old silent-exit freeze (that was fixed when `ratings_summarize.py` switched to reading the 64 `playtime_raw/` shards with fail-loud guards — see ARCHITECTURE.md §10).

**HLTB estimates are present but still unwired.** **16,646 of 122,864 `hltb.json` entries carry the `est` flag** (13.5%), leaving **106,218 reals (86.4%)**. As before, `hltb_estimate.py` is **not wired into any workflow**, so the est set was written by a manual/one-off run and doesn't refresh as new reals land. Still an open item below.

**Rating %/review-count coverage.** Rating % is present for **111,755 games (91.0%)**; review count for **115,032 (93.6%)**. The gap is titles with too few reviews to carry a meaningful score.

**Tags** are non-empty for **83,439 games (67.9%)**. The remainder are untagged on SteamSpy (typically very-low-review or unreleased titles).

---

## Open items

1. **Wire `hltb_estimate.py` into a workflow** — 16,646 estimates exist but are stale (written by a one-off run; no scheduled refresh). Add a scheduled job so estimates recompute as new HLTB reals arrive and the `est` set stays current.
2. ~~**Confirm the 120-min price budget finishes in one pass**~~ — **DONE.** The run reached the full non-free set (104,788 rows) in a single pass with no time-budget wrap-up, even during a live sale.
3. **Playtime backfill in progress** — still the primary coverage gap; the 8-slot / 1.5s raw scale-up is the active remedy and continues to work (raw +888 since the prior snapshot, now 23.0% of addressable). Re-check after a few more days.
