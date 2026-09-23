# Length: from HLTB (3 values) to one review-based Length — change inventory

**Status: executed 2026-09-23** (all items A–D below; additions in E).

Decisions (2026-09-23): one Length = ▲ (recommenders' median playtime) × per-genre coefficient,
calibrated weekly against real HLTB **Extra**; one genre per game (fixed priority); top-up blend
toward the genre-typical ▲ with K=20; floor 3 ▲ reviews; endless games treated as today (length
shelves exclude them); Playtime ▲/▼ column kept; HLTB kept **backend-only** for calibration.

## A. Backend

| # | Where | Change |
|---|---|---|
| A1 | `length_model.py` (new) | `--fit`: `playtime.json` + `hltb.json` (raw extra only) + `pics.json` genres → `length_coefs.json` (per-genre coef, genre-typical ▲, global fallback, K, priority order, calibration n, accuracy stats). Guards: genre < 100 calibration games → global; warn on > 10 % weekly move. Default mode: → `length.json` `{appid: [hours, n_up, genre]}`. |
| A2 | `.github/workflows/length-fit.yml` (new) | `3.3 Length calibration` — weekly + manual; commits `length_coefs.json`. |
| A3 | `.github/workflows/playtime-raw.yml` | Chain `length_model.py` (apply) after `playtime_summarize.py`, so Length refreshes every 3 h. |
| A4 | `.github/workflows/length-apply.yml` (new) | `3.4 Length apply [2.3 / manual]` escape hatch, same pattern as 3.1/3.2. |
| A5 | `presets.py` | `hours` from `length.json` (not `hltb.json`); drop `hltb`,`hq` from `KNOWN_PARAMS`; re-check `hmin=40` / `hmax=6` shelf thresholds (Length ≈ HLTB Extra, longer than Main); comments at :72, :208, :306. Adult guard untouched. Regenerate `presets.json`. |
| A6 | `hltb.yml`, `hltb_refresh.py`, `hltb_estimate.py` | Unchanged — still scrape; now calibration input only. |
| A7 | `test_length.py` (new) | genre pick, blend math, fallbacks, guards. |

## B. Frontend `index.html` — every HLTB touchpoint

### Filters (Value section)
| # | Line(s) | Today | Change |
|---|---|---|---|
| B1 | 1776 | Value header tooltip: "which game-length metric to use, whether estimates are allowed" | Rewrite: price basis, price band/type, Length range, QTPD range. |
| B2 | 1785–1792 | **Length metric for QTPD** — Main / +Extras / 100% / Avg | **Remove.** |
| B3 | 1794–1799 | **Length data** — Real / All (incl. est.) | **Remove.** |
| B4 | 1807 | Length range tooltip "using the Length metric picked above" | Reword: review-based Length; games with no Length dropped when a bound is set. Filter itself stays. |
| B5 | 2763 | passFilters comment "follows the Length metric + Length data toggles" | Update comment. |
| B6 | 4847 | Value section active-count includes `hltbMetric`, `hltbQuality` | Drop both terms. |
| B7 | 4883–4884 | `setGroup("[data-metric]")`, `setGroup("[data-hquality]")` | Remove. |
| B8 | 4950–4951 | Summary chips "+Extras length", "incl. estimated HLTB" | Remove. |
| B9 | 5018–5019 | Inline editors `metric`, `hq` | Remove. |
| B10 | 5575–5576, 5894–5899 | `setMetric()`, metric + hquality click handlers | Remove. |
| B11 | 5809/5813, 6070/6079 | Both reset paths reset `hltbMetric` / `hltbQuality` | Remove. |
| B12 | 2279, 2301 | state `hltbMetric:"main"`, `hltbQuality:"real"` | Remove. |

### URL / sharing
| # | Line(s) | Change |
|---|---|---|
| B13 | 5614–5615 | Stop writing `hltb=` / `hq=`. |
| B14 | 5680–5683 | Stop applying `hltb=` / `hq=`; old links load and silently ignore them. |
| B15 | sort param | `sort=length` is the new key; `sort=hltb` accepted as alias. |

### Columns / table
| # | Line(s) | Today | Change |
|---|---|---|---|
| B16 | 2169 | Header "Length · M/E/100%" + HLTB tooltip | Header **"Length"**; tooltip: estimated hours to play through (story + some side content), from Steam reviewers' playtime, calibrated per genre against HowLongToBeat; divides QTPD. |
| B17 | 3068, 3101–3118 | main/+ext/100% trio, avg line, blue estimate styling, ESTTIP | Single value `fmt(len) h`; "—" when none. Hover: based on N positive reviews · genre. |
| B18 | 3290 | `data-label="HLTB M/E/100%"` | `data-label="Length"`. |
| B19 | 1448, 1470, 1515 | card-layout CSS keyed on that label | Re-key to "Length". |
| B20 | 455, 477, 427, 463, 2155 | `c-hltb` column width sized for "1281 1464 1670" | Rename `c-length`, narrow to a single value. |
| B21 | 861–874, 1455–1458, 1556 | `.hltb`, `.hltb-stack`, `.havg`, estimated-value CSS | Remove / replace with one `.len` style. |
| B22 | 2168 | Playtime header tooltip "Not the same as Length (HLTB)" | "Length is derived from ▲ and calibrated…"; column kept as is. |

### Sort
| # | Line(s) | Change |
|---|---|---|
| B23 | 1733 | Dropdown `<option value="hltb">Length` → `value="length"`. |
| B24 | 2889 | sort accessor `k === "hltb"` → `"length"`. |
| B25 | 4921 | `SORT_LABELS.hltb` → `length`. |
| B26 | 4940 | `SORT_TIPS.hltb` HLTB text → new Length text. |
| B27 | 4790–4792 | grid sort-value slot `k === "hltb"` → `"length"`. |
| B28 | 4689, 4924 | comments naming HLTB sort → update. |

### Grid / card detail
| # | Line(s) | Change |
|---|---|---|
| B29 | 4743 | Row "Length *HLTB*" + HowLongToBeat tooltip → "Length *est.*" + review-based tooltip. |
| B30 | 4702–4709 | Comment ("Length = HowLongToBeat hours…", "11.8 % for HLTB") → updated coverage. |
| B31 | 4008 | stage-meta comment "HLTB metric" → drop. |
| B32 | 1185, 1405–1407 | CSS comments "Length HLTB", "HLTB→Length" → update. |

### QTPD / export / footer
| # | Line(s) | Change |
|---|---|---|
| B33 | 2404–2417 | `realHours()` removed; `hoursFor(g)` returns `g.len`. QTPD, free score, range slider, sort, CSV follow automatically. |
| B34 | 5126, 5138 | CSV "HLTB hours" → "Length (h)". |
| B35 | 5310 | Footer "QTPD = (chosen HLTB hours × rating%) ÷ price — pick the HLTB metric above… without HLTB data" → "QTPD = (Length × rating%) ÷ price … games without a Length (fewer than 3 positive reviews)". |
| B36 | 4655 | "no length data" tooltip — keep, still true. |

### Data load
| # | Line(s) | Change |
|---|---|---|
| B37 | 6455, 6463, 6483 | Stop fetching `hltb.json` (−37.5 MB); fetch `length.json`. |
| B38 | 6558–6565 | HLTB merge (`hltb_main/extra/complete/avg/match/est`) → `game.len`, `game.len_n`, `game.len_genre`. |
| B39 | 2224–2229 | Demo/fallback rows: `hltb_*` fields → `len`. |
| B40 | 196 | Comment on presets drift ("per-metric HLTB realness") → update. |

## C. Docs
- `ARCHITECTURE.md`: new Length-model section; `hltb.json` marked backend/calibration-only; data-file + workflow tables (3.3, 3.4); pipeline diagram; §8 intro.
- `README.md`: user-facing HLTB wording → Length.
- `COVERAGE.md` / `FRESHNESS.md`: note HLTB is calibration-only (numbers unchanged).
- `LENGTH_MODEL.md`: research write-up (why ▲, why Extra, why one genre, why K=20, accuracy).

## D. Validation
- `test_length.py`; fit + apply locally; spot-check known games vs HLTB.
- `presets.py` builds, adult guard passes.
- Headless Chromium: no console errors; table / card / grid render; `?hltb=extra&hq=loose&sort=hltb` loads cleanly; filter chips/reset/URL round-trip; screenshots.
- `grep -i hltb index.html` → only the intentional "calibrated against HowLongToBeat" tooltip wording remains.

## E. Added during execution (owner's decisions, 2026-09-23)
- **Outlier guard + Idler balance** (`length_model.py`): any game whose Length would pass
  1,000 h, and every Idler-tagged game, uses `coef × √(▲ × ▼)`, capped at 1,000 h. Adjusted rows
  carry `[…, raw_hours, "cap"|"idler"]`; the page underlines them in blue and the tooltip quotes
  the raw figure and why it is not believable.
- **Landing view excludes storefront-flagged adult games by default** (`ADULT_DEFAULT = "hide"`;
  Exclude moved to the leftmost button so "default = leftmost" still holds; `adult=any` opts in).
  CLAUDE.md updated.
- **No tag-based adult filtering** reaffirmed: an unflagged game tagged Sexual Content / Hentai
  tops two niche shelves; left as is, stated in CLAUDE.md.
- Table floor 1324 → 1272 px (tags-collapsed 1218 → 1166) from the narrower Length column.
