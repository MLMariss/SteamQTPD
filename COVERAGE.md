# SteamQHPP — Data Coverage Snapshot

**Registry date (`games.json` generated_at):** 2026-07-07 03:03 UTC
**Base universe:** 122,854 games

Per-file generation timestamps at snapshot time:

| File | Generated (UTC) |
|---|---|
| `games.json` | 2026-07-07 03:03 |
| `hltb.json` | 2026-07-07 02:18 |
| `recent.json` | 2026-07-06 23:45 |
| `prices.json` | 2026-07-06 23:34 |
| `tags.json` | 2026-07-06 20:21 |
| `playtime.json` | 2026-07-06 10:54 |
| `ratings.json` | 2026-07-05 08:37 |
| `playtime_raw/` (shards) | 2026-07-07 04:20 |

---

## Coverage by metric

| Metric | Storage file | Covered | % of catalog | Missing | Freshness (newest / oldest) |
|---|---|---:|---:|---:|---|
| Review count | `games.json` | 122,854 | 100.0% | 0 | 1.6h / 10.7d |
| Tags | `tags.json` | 122,840 | 100.0% | 14 | re-gen ~8h ago |
| Recent reviews | `recent.json` | 122,840 | 100.0% | 14 | re-gen ~5h ago |
| HLTB | `hltb.json` | 122,840 | 100.0% | 14 | re-gen ~2h ago |
| Release date | `games.json` | 122,328 | 99.6% | 526 | — |
| Rating % | `games.json` | 115,026 | 93.6% | 7,828 | 1.6h / 10.7d |
| Price / Sales | `prices.json` | 104,755 | 85.3% | 18,099 | 6.1h / 0.3d |
| Playtime raw | `playtime_raw/` | 15,026 | 12.2% | 107,828 | see note |
| Playtime (summarized) | `playtime.json` | 8,271 | 6.7% | 114,583 | re-gen ~18h ago |
| Playtime-weighted rating | `ratings.json` | 6,855 | 5.6% | 115,999 | re-gen ~44h ago |

Integrity: near-zero orphan keys (a handful per file, timing artifacts between scraper commits). Every storage file is effectively a clean subset of `games.json`.

---

## Notes

**Everything except playtime/ratings is fresh.** Every dated source was scraped within ~11 days, most within hours. Prices are near real-time (newest ~6h). Tags, recent reviews, and HLTB all sit at ~100% coverage (the 14 gaps are timing artifacts between the scraper's registry write and the refreshers).

**Playtime raw has more than doubled since the last snapshot — the scale-up is working.** Raw playtime rose from 6,856 to **15,026 games** (5.6% → **12.2% of catalog**). Against the honest denominator — the addressable set after the `MIN_REVIEWS_FLOOR = 10` gate, now **78,476 games (64% of catalog)** — raw playtime is **19.1% of addressable**, up from 8.7%. The other **44,378 games are below the floor and correctly skipped** (they can't produce a sentiment-split median). This is the payoff of the 8-slot / `STEAM_DELAY 1.5s` scale-up landing.

**Summarizer now lags raw again — new gap to watch.** Raw 15,026 vs summarized 8,271 — a lag of **6,755** (was ~1 at the previous snapshot). The raw scraper has surged ahead of `playtime_summarize.py`; the summarized file was last re-generated ~18h ago while raw shards were touched minutes before this snapshot. Expect the summarizer to close the gap on its next runs, but if the lag keeps growing, check that the summarize workflow is firing at its intended cadence.

**Ratings file is stale relative to playtime.** `ratings.json` (6,855 entries, generated 2026-07-05) has not advanced with `playtime.json` (8,271, generated 2026-07-06). The playtime-weighted rating is derived from the same raw pass, so it should track summarized playtime; a ~44h-old ratings file against an ~18h-old playtime file suggests `ratings_summarize.py` skipped a beat. Flagged below.

**HLTB estimates still absent.** 0 of 122,840 entries carry the `estimated` flag despite the estimator (`hltb_estimate.py`) existing — the "Real only" toggle implies estimated rows should exist. Anomaly persists; flagged below.

**Sales heavy at snapshot.** 61,083 of 104,755 priced games (58%) discounted (Steam Summer Sale still live).

**No price on 18,099 games** ≈ the 18,084 `is_free` titles plus a handful delisted/region-locked.

---

## Open items

1. **Summarizer lag (new)** — raw playtime (15,026) is ~6,755 ahead of summarized (8,271). Confirm `playtime_summarize.py` is running at cadence and closing the gap, not stalled.
2. **Ratings file stale** — `ratings.json` (07-05) trails `playtime.json` (07-06). Verify `ratings_summarize.py` re-runs alongside the summarizer.
3. **HLTB estimates missing** — 0 estimated entries despite `hltb_estimate.py` existing. Trace whether it writes elsewhere or the `estimated` flag is dropped.
4. **Playtime backfill in progress** — the primary coverage gap; the 8-slot / 1.5s scale-up is the active remedy and is visibly working (raw +8,170 since last snapshot). Re-check after a few more days.
