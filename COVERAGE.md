# SteamQHPP — Data Coverage Snapshot

**Registry date (`games.json` generated_at):** 2026-07-03 15:37 UTC
**Base universe:** 100,076 games

Per-file generation timestamps at snapshot time:

| File | Generated (UTC) |
|---|---|
| `games.json` | 2026-07-03 15:37 |
| `tags.json` | 2026-07-03 15:38 |
| `hltb.json` | 2026-07-03 15:35 |
| `recent.json` | 2026-07-03 15:30 |
| `prices.json` | 2026-07-03 15:26 |
| `playtime_raw.json` | 2026-07-03 14:01 |
| `playtime.json` | 2026-07-03 08:41 |
| `ratings.json` | 2026-07-03 08:43 |

---

## Coverage by metric

| Metric | Storage file | Covered | % of catalog | Missing | Freshness (median / oldest) |
|---|---|---:|---:|---:|---|
| Review count | `games.json` | 100,076 | 100.0% | 0 | 1.7d / 7.2d |
| Release date | `games.json` | 99,539 | 99.5% | 537 | — |
| Tags | `tags.json` | 97,836 | 97.8% | 2,240 | full re-gen today |
| Recent reviews | `recent.json` | 97,639 | 97.6% | 2,437 | 1.6d / 5.0d |
| Rating % | `games.json` | 92,598 | 92.5% | 7,478 | 1.7d / 7.2d |
| HLTB | `hltb.json` | 89,440 | 89.4% | 10,636 | 1.7d / 3.1d |
| Price / Sales | `prices.json` | 84,148 | 84.1% | 15,928 | 0.05d / 0.0d |
| Playtime raw | `playtime_raw.json` | 6,710 | 6.7% | 93,366 | re-gen 0.07d ago |
| Playtime (summarized) | `playtime.json` | 5,493 | 5.5% | 94,583 | re-gen 0.29d ago |
| Playtime-weighted rating | `ratings.json` | 5,493 | 5.5% | 94,583 | re-gen 0.29d ago |

Integrity: no orphan keys on any file — every storage file is a clean subset of `games.json`.

---

## Notes

**Whole catalog is fresh.** Every dated source re-scrapes on a tight cycle; no entry is older than ~7 days on any metric. Prices are near real-time (median ~1.2h old).

**Playtime is the coverage gap.** The three playtime-derived metrics sit at ~5–7%. This is the newest pipeline and has not backfilled the catalog. `playtime_raw` (6,710) leads the summarized output (5,493) by ~1,217 games — those have raw reviews scraped but not yet summarized.

**HLTB is 100% real matches, zero estimates.** All 89,440 entries carry a real HLTB match; the `estimated` flag is empty across the board. Flagged for review — the "Real only" toggle implies estimated rows should exist somewhere.

**Sales heavy at snapshot.** 49,472 of 84,148 priced games (59%) discounted (Steam Summer Sale live).

**No price on ~15,928 games** — largely the 15,661 `is_free` titles plus delisted/region-locked entries.

**Discovery backlog:** `catalog.json` holds 49,327 pending appids not yet pulled into `games.json`.

---

## Open items

1. **HLTB estimates missing** — 0 estimated entries despite the T11 estimator (`hltb_estimate.py`) existing. Trace whether it writes elsewhere or the flag is dropped.
2. **Playtime summarize lag** — 1,217 games raw-but-unsummarized. Check whether `playtime_summarize.py` filters them (e.g. min-segment threshold) or just hasn't run against them.
