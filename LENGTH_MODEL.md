# Length model — research record (Sep 2026)

Why the page's **Length** is built the way it is. Implementation: `length_model.py`;
architecture: ARCHITECTURE.md §9.7; the change inventory: `LENGTH_PLAN.md`.

Notation: **▲** = median playtime of reviewers who recommend a game, **▼** = of those who do
not (`playtime.json`, per-review `playtime_forever`). HLTB values are **real only** (`raw` in
`hltb.json`), never our own estimates.

## 1. Does a middle ground of ▲ and ▼ track HLTB better than ▲? No.

68,957 games with an HLTB entry and ≥ 3 reviews on each side. Spearman rank correlation:

| Candidate | vs Main | vs Main+Extra | vs Completionist |
|---|---|---|---|
| **▲** | **0.782** | **0.826** | 0.833 |
| mean(▲, ▼) | 0.773 | 0.809 | 0.813 |
| pooled median | 0.763 | 0.801 | 0.813 |
| geomean(▲, ▼) | 0.747 | 0.769 | 0.781 |
| ▼ | 0.648 | 0.647 | 0.667 |
| pooled p75 | 0.772 | 0.836 | 0.853 |

A regression of log HLTB on log ▲ + log ▼ gives ▼ weights of 0.02 / −0.07 / −0.08 and no R²
gain (main 0.598 → 0.598). ▼ measures how long people played before quitting or refunding.
▲ wins on well-sampled games (0.84–0.89) and in every genre segment.

## 2. Which HLTB figure does ▲ match? Extra.

95,956 games with ≥ 3 fans:

| Target | n | ▲ ÷ HLTB | ±25 % | ±50 % | 2× | 2× after best single coef |
|---|---|---|---|---|---|---|
| Main | 25,119 | 1.48 | 20 % | 40 % | 63 % | 71 % |
| **Extra** | 19,105 | **1.09** | **38 %** | **60 %** | **77 %** | **77 %** |
| Completionist | 23,637 | 0.82 | 33 % | 50 % | 68 % | 70 % |

Uncorrected ▲ already sits on Extra, and Extra has the least scatter after calibration.

## 3. Genre coefficients — one genre per game

Holdout, averaged over 20 random 50/50 splits (~9,550 test games each), target HLTB Extra:

| Method | typical miss | ±25 % | ±50 % | 2× |
|---|---|---|---|---|
| global coefficient | ×1.334 | 41.5 | 60.8 | 76.5 |
| **one genre (fixed priority)** | **×1.328** | **42.1** | **61.5** | **76.9** |
| all genres, averaged | ×1.328 | 42.0 | 61.3 | 76.8 |
| all genres, stacked | ×1.330 | 41.8 | 61.3 | 76.9 |
| top-5 tags, averaged | ×1.321 | 42.9 | 61.6 | 76.9 |
| top-5 tags, **stacked** | ×1.350 | 39.8 | 60.4 | 76.9 |

Stacking per-tag effects is the worst option (effects overlap and pile up); one genre matches
any multi-genre variant; tag averaging is +0.8 pt for 70+ coefficients. Priority, most specific
first: Racing › Sports › Strategy › Simulation › RPG › Action › Adventure › Casual › none.
Genre is a small correction (~1 pt over a single global coefficient) — the within-genre scatter
(×1.26–1.61) dwarfs the between-genre spread (0.80–1.12).

Checked and rejected: a length-dependent coefficient. The ratio is roughly flat from 1 h to 50 h+
(0.86–1.04), and a fitted power law scored worse than a constant.

## 4. Few reviews — top up toward the genre, K = 20

Accuracy collapses with few fans (global coef): 3–9 → 34 % within 2×; 10–29 → 52 %;
30–99 → 71 %; 100+ → 84 %. Topping up with genre-typical values, blended in **log space**
(so each real review counts 1/K; a literal median of padded values ignores the game's own data
until it has K/2 reviews), on all games with < 50 fans:

| K | 0 | 10 | 15 | **20** | 30 | 50 |
|---|---|---|---|---|---|---|
| within 2× | 50.4 | 51.8 | 52.3 | **52.7** | 52.9 | 50.4 |
| within ±50 % | 35.0 | 35.9 | 36.1 | **36.5** | 35.9 | 34.1 |

The genre-typical figure alone misses HLTB by ×2.3 on a typical game, so by ~15 reviews a game's
own data is already better than the prior; K = 50 undoes the gain.

## 5. Idle-farmed playtime — cap and ▼ balance

`playtime_forever` counts time a game spends running in the background. The extreme case: a
$0.99 title whose 47 fans have an ~11,000 h median (▼ 0.3 h) → Length 9,074 h, QTPD 7,058, #1
site-wide. HLTB never listed such games, so the HLTB-era page never saw them.

Rule (owner's decisions, 2026-09-23): for any game whose Length would pass **1,000 h**, and for
every game tagged **Idler**, `Length = coef × √(▲_blended × ▼)` — a geometric mean, since an
arithmetic one leaves 11,000 h and 0.3 h at ~5,700 h — then capped at 1,000 h. Effect on the five
over-cap games: 9,074 → 44 h, 1,689 → 89 h, 1,013 → 271 h, 2,479 → 529 h, and one whose
detractors also played ~1,300 h stays at the 1,000 h cap. ~2,400 Idler games are adjusted
(typically ×0.72). Adjusted rows keep the raw figure; the page underlines them and the tooltip
quotes it.

Tightened later the same day (owner's call): cap **420 h** (14 h/day × 30 days). Clamp first,
then blend: `min(capped, √(capped × coef·▼))` — the blend never raises a figure. 378 games change
vs the 1,000 h rule; the largest is Granado Espada, 1,000 → 420 h.

A ▼ balance for **every game past 100 h** was tried and reverted. Measured against HLTB (all
games with a real time, within 2×): Main 66.2 → 66.3 %, Extra 77.0 → 77.1 %, Completionist
64.8 → 64.7 % — no real gain — while well-known long games were cut far below HLTB extra
(Baldur's Gate 3 137 → 74 h vs 117; Kenshi 107 → 38 vs 134; Dyson Sphere Program 122 → 46 vs
117). In deep games the players who quit early *are* the detractors, so ▼ is not an idling
signal there; gating on the ▲/▼ gap did not separate them (Kenshi 7.7×, idle games 7–170×).

## 6. What this does to the page

- Games with a Length (and so a QTPD where priced): ~27k real-HLTB-main → ~96k.
- `hltb.json` (37 MB) is no longer downloaded.
- Preset shelves (`presets.py`, 40 h / 6 h thresholds kept): *Long games* 151 → 460 results,
  *Short and cheap* 257 → 160 — Length sits near HLTB extra, not main.
- Sexual-content-tagged games without the storefront adult flag in shelf top-50s: 23 (HLTB) vs
  21 (Length) — unchanged in rate; owner confirmed no tag-based filtering. The default landing
  view now excludes storefront-flagged adult games (`ADULT_DEFAULT`).

## 7. Caveats

- ▲ is a live total and drifts upward as reviewers keep playing.
- Endless games not tagged Idler and under the cap show their reviewers' hours (owner's call);
  the length shelves still exclude them by tag.
- Under ~10 fans a Length is mostly its genre's typical figure.
