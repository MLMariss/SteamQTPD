#!/usr/bin/env python3
"""
Steam QTPD — review-based game Length
=====================================
The site's one game-length figure. It replaced the three HowLongToBeat values (Main /
+Extras / 100%) on the page in Sep 2026; HLTB is still scraped, but only to CALIBRATE
this model — it is never shown to users any more. The research that chose this shape is in
LENGTH_MODEL.md; the short version:

  * ▲ (the median playtime of reviewers who RECOMMEND a game, from playtime.json) tracks
    HLTB better than ▼ or any blend of the two. ▼ adds nothing once ▲ is known.
  * Uncorrected, ▲ already sits on HLTB **Extra** (story + some side content): ratio 1.09,
    closer than Main (1.48) or Completionist (0.82). So Length is calibrated to Extra.
  * One genre per game, one coefficient per genre. Stacking per-tag coefficients scored
    WORSE than no genre at all; averaging tags was only ~1 point better than one genre.
  * Games with few reviews are topped up toward their genre's typical ▲: every real review
    counts 1/K of the answer until there are K of them (K = 20 tested best; 50 undid the
    gain). Blended in log space, so a single real review always moves the result.

Two modes, one writer per file:

  python length_model.py --fit   weekly  (3.3)  -> length_coefs.json
      coefficient per genre = median(HLTB extra / ▲) over games with >= FIT_MIN_UP fans
      and a REAL HLTB extra (hltb.json `raw`, never the estimates); genre-typical ▲ =
      median ▲ of games with >= TYPICAL_MIN_UP fans. A genre with fewer than
      MIN_GENRE_GAMES calibration games uses the global coefficient.

  python length_model.py         every playtime pass (chained in 2.3, or 3.4) -> length.json
      Length(h) = coef[g] × 10^( w·log10(▲) + (1−w)·log10(typical[g]) ),  w = min(n_up, K)/K,
                  capped at LENGTH_CAP_H (420 h); and when the uncapped figure passes
                  BALANCE_ABOVE_H (100 h), or the game is tagged Idler, it is blended down
                  with ▼: √(capped × coef·▼), never above the capped figure

Pure local compute over files already in the repo — no network, no rate budget.
"""

import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAYTIME_FILE = HERE / "playtime.json"
HLTB_FILE = HERE / "hltb.json"
PICS_FILE = HERE / "pics.json"
GENRE_LOOKUP = HERE / "lookups" / "genres.json"
COEF_FILE = HERE / "length_coefs.json"      # written by --fit only
OUT_FILE = HERE / "length.json"             # written by apply only (frontend + presets read it)

# One genre per game: the first of these the game carries, most specific first. Indie, Free to
# Play, Early Access, MMO and the adult genres are not game TYPES and are skipped; a game with
# none of these lands in "none" (still gets a Length, from the global coefficient).
GENRE_PRIORITY = ["Racing", "Sports", "Strategy", "Simulation", "RPG", "Action",
                  "Adventure", "Casual"]
NONE = "none"

K = 20                  # top-up target: real reviews count 1/K each until there are K
MIN_UP = 3              # floor — playtime.json publishes no ▲ median below this anyway
FIT_MIN_UP = 30         # games used to fit a coefficient need this many fans
TYPICAL_MIN_UP = 50     # games used for a genre's typical ▲ need this many fans
MIN_GENRE_GAMES = 100   # below this many calibration games a genre uses the global coef
WARN_MOVE = 0.10        # log a warning when a coefficient moves more than this in one fit
# Outlier guard. Recommenders' playtime includes achievement / trading-card IDLING: one $0.99
# title's 47 fans have a ~11,000 h median while its detractors played 0.3 h, which made its
# Length 9,074 h and its QTPD 7,058 — #1 on the whole site by 30x. HLTB never listed such games,
# so the HLTB-era page never saw them. When the ▲-based Length would pass LENGTH_CAP_H, ▼ is
# brought in as a check: Length = coef × √(▲ × ▼) (geometric mean — an arithmetic one would keep
# 11,000 h and 0.3 h at ~5,700 h), and the result is still capped at LENGTH_CAP_H. That game
# lands at 44 h; a genuinely endless game whose detractors ALSO played for ages (Granado Espada,
# ▼ 1,328 h) stays at the cap. 5 games affected (Sep 2026). --fit is unaffected: the coefficients
# are fitted on raw ▲ from games with a real HLTB time.
# Tightened the same month (owner's call): the cap is 420 h — 14 h a day for 30 days, the most a
# person plausibly plays — and the ▼ balance starts at 100 h rather than at the cap, since few
# games genuinely take that long and those that do keep big numbers anyway. Order: clamp to the
# cap FIRST, then blend with ▼ — √(capped × coef·▼) — and never let the blend raise the figure
# (a game whose detractors played longer than its fans keeps the fans' Length).
LENGTH_CAP_H = 420.0
BALANCE_ABOVE_H = 100.0
# Games whose recommenders' playtime is idle time by design: the same ▼ balance is applied to
# every game carrying one of these tags, not only to the ones that pass the cap (owner's call,
# Sep 2026). Tag test = the page's own source order: PICS store tags, SteamSpy as fallback.
BALANCE_TAGS = {"Idler"}
TAG_LOOKUP = HERE / "lookups" / "tags.json"
STEAMSPY_TAGS = HERE / "tags.json"

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


def load(path, key=None):
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get(key, {}) if key else d


def genre_of(genre_ids, id_to_name):
    names = {id_to_name.get(str(g)) for g in genre_ids or []}
    for g in GENRE_PRIORITY:
        if g in names:
            return g
    return NONE


def game_genres(pics, id_to_name):
    return {a: genre_of(p.get("genres"), id_to_name) for a, p in pics.items()}


def fans(playtime):
    """appid -> (▲ hours, n_up, ▼ hours or None) for every game with a published ▲ median."""
    out = {}
    for a, row in playtime.items():
        up_min, down_min, n_up = row[0], row[1], row[2]
        if up_min is None or up_min <= 0 or (n_up or 0) < MIN_UP:
            continue
        out[a] = (up_min / 60.0, n_up, down_min / 60.0 if down_min else None)
    return out


def geo_median(ratios):
    return 10 ** statistics.median(math.log10(r) for r in ratios)


def length_hours(up_h, n_up, genre, coefs, down_h=None, balance=False):
    g = coefs["genres"].get(genre) or {}
    coef = g.get("coef", coefs["global"]["coef"])
    typical = g.get("typical_up_h", coefs["global"]["typical_up_h"])
    k = coefs.get("k", K)
    cap = coefs.get("cap_h", LENGTH_CAP_H)
    bal = coefs.get("balance_h", BALANCE_ABOVE_H)
    w = min(n_up, k) / k
    up_eff = 10 ** (w * math.log10(up_h) + (1 - w) * math.log10(typical))
    raw = coef * up_eff
    h = min(raw, cap)
    if (balance or raw > bal) and down_h:   # ▼ balance — see LENGTH_CAP_H / BALANCE_TAGS
        h = min(h, math.sqrt(h * coef * down_h))
    return h


def accuracy(pairs):
    """pairs: (predicted, actual). Share within ±25 %, ±50 %, 2× — the figures the docs quote."""
    if not pairs:
        return None
    errs = [abs(math.log10(p / a)) for p, a in pairs]
    n = len(errs)
    return {"n": n,
            "within_25": round(sum(e < math.log10(1.25) for e in errs) / n, 3),
            "within_50": round(sum(e < math.log10(1.5) for e in errs) / n, 3),
            "within_2x": round(sum(e < math.log10(2) for e in errs) / n, 3)}


def fit(playtime, hltb, pics, id_to_name, previous=None):
    genre = game_genres(pics, id_to_name)
    f = fans(playtime)

    # Calibration set: real HLTB extra only. `raw` is what HLTB returned; the top-level
    # value may be an estimate from hltb_estimate.py, and fitting to our own estimates
    # would be circular.
    ratios, ratios_all, typical = {}, [], {}
    for a, (up_h, n_up, _down) in f.items():
        g = genre.get(a, NONE)
        if n_up >= TYPICAL_MIN_UP:
            typical.setdefault(g, []).append(up_h)
        extra = ((hltb.get(a) or {}).get("raw") or {}).get("extra")
        if extra and extra > 0 and n_up >= FIT_MIN_UP:
            ratios.setdefault(g, []).append(extra / up_h)
            ratios_all.append(extra / up_h)
    if len(ratios_all) < MIN_GENRE_GAMES:
        raise SystemExit(f"only {len(ratios_all)} calibration games — refusing to fit")

    all_typical = [u for us in typical.values() for u in us]
    glob = {"coef": round(geo_median(ratios_all), 4),
            "typical_up_h": round(statistics.median(all_typical), 3),
            "n_fit": len(ratios_all)}
    genres = {}
    for g in GENRE_PRIORITY + [NONE]:
        rs, ts = ratios.get(g, []), typical.get(g, [])
        own = len(rs) >= MIN_GENRE_GAMES
        genres[g] = {
            "coef": round(geo_median(rs), 4) if own else glob["coef"],
            "typical_up_h": round(statistics.median(ts), 3) if len(ts) >= MIN_GENRE_GAMES
                            else glob["typical_up_h"],
            "n_fit": len(rs),
            "n_typical": len(ts),
            "fallback": not own,
        }

    coefs = {"generated_at": int(time.time()), "k": K, "min_up": MIN_UP, "cap_h": LENGTH_CAP_H,
             "balance_h": BALANCE_ABOVE_H,
             "fit_min_up": FIT_MIN_UP, "typical_min_up": TYPICAL_MIN_UP,
             "min_genre_games": MIN_GENRE_GAMES, "target": "hltb_extra",
             "genre_priority": GENRE_PRIORITY, "global": glob, "genres": genres}

    # In-sample accuracy against every game with a real extra (any fan count), so the
    # weekly commit shows at a glance whether the model is drifting.
    pairs_all, pairs_100 = [], []
    for a, (up_h, n_up, down_h) in f.items():
        extra = ((hltb.get(a) or {}).get("raw") or {}).get("extra")
        if not extra or extra <= 0:
            continue
        p = (length_hours(up_h, n_up, genre.get(a, NONE), coefs, down_h), extra)
        pairs_all.append(p)
        if n_up >= 100:
            pairs_100.append(p)
    coefs["accuracy"] = {"all": accuracy(pairs_all), "n_up_100_plus": accuracy(pairs_100)}

    if previous:
        for g, v in genres.items():
            old = ((previous.get("genres") or {}).get(g) or {}).get("coef")
            if old and abs(v["coef"] / old - 1) > WARN_MOVE:
                log(f"  WARNING: {g} coefficient moved {old:.3f} -> {v['coef']:.3f}")
    return coefs


def balanced_games(pics, tag_names, steamspy):
    """appids carrying a BALANCE_TAGS tag (PICS names first, SteamSpy where PICS has none)."""
    out = set()
    for a in set(pics) | set(steamspy):
        names = [tag_names.get(str(t)) for t in (pics.get(a) or {}).get("tags") or []]
        names = [n for n in names if n] or steamspy.get(a) or []
        if BALANCE_TAGS & set(names):
            out.add(a)
    return out


def apply(playtime, pics, id_to_name, coefs, balanced=frozenset()):
    genre = game_genres(pics, id_to_name)
    idx = {g: i for i, g in enumerate(GENRE_PRIORITY + [NONE])}
    out = {}
    for a, (up_h, n_up, down_h) in fans(playtime).items():
        g = genre.get(a, NONE)
        idler = a in balanced
        h = length_hours(up_h, n_up, g, coefs, down_h, balance=idler)
        row = [round(h, 2) if h < 10 else round(h, 1), n_up, idx[g]]
        # Adjusted (see LENGTH_CAP_H / BALANCE_TAGS): keep the ▲-only figure and the reason as
        # elements 4 and 5, so the page can show what the reviews literally said and why it
        # was not used. "cap" = passed the ceiling; "long" = passed BALANCE_ABOVE_H and was
        # balanced with ▼; "idler" = balanced for its Idler tag.
        raw = length_hours(up_h, n_up, g, dict(coefs, cap_h=float("inf"), balance_h=float("inf")))
        if raw > coefs.get("cap_h", LENGTH_CAP_H):
            row += [round(raw), "cap"]
        elif abs(raw - h) > 0.05:
            row += [round(raw, 1), "idler" if idler else "long"]
        out[a] = row
    return out


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def git_commit(path, msg):
    if not IN_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", path.name], check=False)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", msg], check=False)
        import random
        for attempt in range(1, 9):
            subprocess.run(["git", "fetch", "origin", "main"], check=False)
            subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
            if subprocess.run(["git", "push", "origin", "HEAD:main"],
                              capture_output=True, text=True).returncode == 0:
                log(f"  committed {path.name}")
                return
            time.sleep(2 * attempt + random.uniform(0, 2))
    except Exception as e:  # never fail the chained job over a push race
        log(f"  git commit failed: {e}")


def main(argv):
    playtime = load(PLAYTIME_FILE, "playtime")
    pics = load(PICS_FILE, "apps")
    id_to_name = load(GENRE_LOOKUP)

    if "--fit" in argv or not COEF_FILE.exists():
        previous = load(COEF_FILE) if COEF_FILE.exists() else None
        coefs = fit(playtime, load(HLTB_FILE, "hltb"), pics, id_to_name, previous)
        write_json(COEF_FILE, coefs)
        acc = coefs["accuracy"]["all"]
        log(f"Fitted {len(coefs['genres'])} genres on {coefs['global']['n_fit']} games; "
            f"global coef {coefs['global']['coef']}; within 2x of HLTB extra: "
            f"{acc['within_2x']:.1%} (n={acc['n']})")
        for g, v in coefs["genres"].items():
            log(f"  {g:11s} coef {v['coef']:.3f}  typical ▲ {v['typical_up_h']:.1f}h  "
                f"n_fit {v['n_fit']}{'  (global fallback)' if v['fallback'] else ''}")
        git_commit(COEF_FILE, "length: weekly recalibration of genre coefficients")
        if "--fit" in argv:
            return 0

    # The ceiling and the balance threshold are this file's policy, not a fitted value: a
    # length_coefs.json written before a change must not hold the old ones in place.
    coefs = dict(load(COEF_FILE), cap_h=LENGTH_CAP_H, balance_h=BALANCE_ABOVE_H)
    balanced = balanced_games(pics, load(TAG_LOOKUP), load(STEAMSPY_TAGS, "tags"))
    lengths = apply(playtime, pics, id_to_name, coefs, balanced)
    write_json(OUT_FILE, {
        "generated_at": int(time.time()),
        "coefs_generated_at": coefs["generated_at"],
        "k": coefs["k"], "min_up": coefs["min_up"], "cap_h": LENGTH_CAP_H,
        "balance_h": BALANCE_ABOVE_H,
        "genres": GENRE_PRIORITY + [NONE],
        "_format": ["hours", "n_up", "genre_idx", "raw_hours?", "reason? (cap|long|idler)"],
        "balance_tags": sorted(BALANCE_TAGS),
        "count": len(lengths),
        "length": lengths})
    git_commit(OUT_FILE, "length: refreshed length.json from playtime.json")
    log(f"Wrote length.json: {len(lengths)} games.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
