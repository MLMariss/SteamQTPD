# QTPD (SteamQHPP) — Data Coverage Snapshot

**Registry date (`games.json` generated_at):** 2026-07-05 08:55 UTC
**Base universe:** 122,765 games

Per-file generation timestamps at snapshot time:

| File | Generated (UTC) |
|---|---|
| `games.json` | 2026-07-05 08:55 |
| `playtime.json` | 2026-07-05 08:33 |
| `ratings.json` | 2026-07-05 08:37 |
| `hltb.json` | 2026-07-05 07:51 |
| `prices.json` | 2026-07-05 07:53 |
| `recent.json` | 2026-07-05 07:46 |
| `tags.json` | 2026-07-05 04:38 |
| `playtime_raw.json` | 2026-07-03 16:32 |

---

## Coverage by metric

| Metric | Storage file | Covered | % of catalog | Missing | Freshness (newest / oldest) |
|---|---|---:|---:|---:|---|
| Review count | `games.json` | 122,765 | 100.0% | 0 | 3.4h / 9.0d |
| Tags | `tags.json` | 122,763 | 100.0% | 2 | re-gen 4h ago |
| Recent reviews | `recent.json` | 122,764 | 100.0% | 1 | 4.5h / 6.9d |
| HLTB | `hltb.json` | 122,764 | 100.0% | 1 | 4.4h / 5.0d |
| Release date | `games.json` | 122,234 | 99.6% | 531 | — |
| Rating % | `games.json` | 114,987 | 93.7% | 7,778 | 3.4h / 9.0d |
| Price / Sales | `prices.json` | 104,683 | 85.3% | 18,082 | 5.4h / 0.2d |
| Playtime raw | `playtime_raw.json` | 6,856 | 5.6% | 115,909 | see note |
| Playtime (summarized) | `playtime.json` | 6,855 | 5.6% | 115,910 | re-gen ~1h ago |
| Playtime-weighted rating | `ratings.json` | 6,855 | 5.6% | 115,910 | re-gen ~1h ago |

Integrity: near-zero orphan keys (1–2 per file, timing artifacts between scraper commits). Every storage file is effectively a clean subset of `games.json`.

---

## Notes

**Everything except playtime is fresh.** Every dated source was scraped within ~9 days, most within hours. Prices are near real-time (median ~5h). Tags, recent reviews, and HLTB all sit at ~100% coverage.

**Playtime is the coverage gap — but the honest denominator is the addressable set.** Raw 5.6% of the full catalog looks low, but a sentiment-split median needs a usable review sample. After the `MIN_REVIEWS_FLOOR = 10` gate, the **addressable universe is 78,460 games (64% of catalog)** — so playtime is **8.7% of addressable**, not 5.6%. The other **44,305 games are below the floor and correctly skipped** (they can't produce a median). The playtime pipeline was just scaled **4 → 8 cron slots + `STEAM_DELAY` 2.0 → 1.5s** (≈2× throughput) to backfill this faster.

**Playtime summarizer has caught up.** Raw 6,856 vs summarized 6,855 — a lag of 1 (was ~1,217). `playtime_raw.json` last committed 2026-07-03 16:32; the raw scraper commits only when its held review set changes, so its timestamp trails the others. With the 8-slot scale-up landing, expect more frequent updates.

**HLTB estimates still absent.** 0 of 122,764 entries carry the `estimated` flag despite the estimator (`hltb_estimate.py`) existing — the "Real only" toggle implies estimated rows should exist. Anomaly persists; flagged below.

**Sales heavy at snapshot.** 61,052 of 104,683 priced games (58%) discounted (Steam Summer Sale live).

**No price on 18,082 games** ≈ the 18,080 `is_free` titles plus a couple delisted/region-locked.

**Discovery queue healthy.** 50,425 pending, of which **100.00% are unreleased/TBA** (1 dated, 0 ripe-now) — the waiting room working as designed, not a backlog. 731 permanently skipped (non-games/delisted).

---

## Open items

1. **HLTB estimates missing** — 0 estimated entries despite `hltb_estimate.py` existing. Trace whether it writes elsewhere or the `estimated` flag is dropped.
2. **Playtime backfill in progress** — the primary coverage gap; the 8-slot / 1.5s scale-up is the active remedy. Re-check this snapshot after a few days of the new cadence.
