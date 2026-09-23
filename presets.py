#!/usr/bin/env python3
"""presets.py -> presets.json — the landing-page preset shelves, validated against live data.

A preset is nothing but a querystring: the same params `syncURL()` writes and `loadFromURL()`
reads. `syncURL()` writes 33; KNOWN_PARAMS below allows 32 of them — `scheme` is deliberately
out, because it is a colour-scheme display preference and no shelf sets one. Clicking one sets real filter state, so the summary chips afterwards
show exactly which controls moved. That is the whole design — a preset must never be able to
express something the user cannot then see and edit.

Why this file exists rather than a hardcoded list in index.html:

  A preset is editorial, and the catalogue moves under it continuously. A shelf that was good
  in September can go thin (too few results to be worth a click) or turn up junk as prices and
  review counts drift. Hardcoded, nobody would notice for months. Generated after each scrape,
  every shelf carries a live count and its current top titles, and anything unhealthy is
  flagged in the output.

  It REPORTS, it does not silently re-tune. A shelf quietly repopulating itself with different
  games is worse than one that is visibly stale: the thresholds stay in PRESETS below, under
  version control, and this script only ever measures them.

Stdlib only, no Steam calls — a pure local recompute over files other jobs already wrote, the
same shape as coverage.py / freshness.py / shard_health.py.

The two review-band ideas that make the shelves work:

  * POPULAR shelves floor at 5,000 reviews. Below that, QTPD-descending surfaces obscure
    long-cheap titles nobody recognises, because the metric rewards hours per dollar and an
    unknown 90-hour game at $1 beats every famous one.
  * NICHE shelves CEILING at 5,000 reviews. Without a ceiling they are just the popular
    shelves again — the well-known games dominate on absolute quality. Capping review count is
    what makes "hidden" mean hidden.

Both map onto the frontend's existing independent review bands (REV_BANDS = 0-99/100/1k/5k,
gaps allowed), so a ceiling costs no new filter: it is simply not selecting the top band.
"""

import json, os, time, urllib.parse

OUT = "presets.json"

# Mirrors ADULT_TAGS in index.html, INCLUDING its exact case: the frontend tests the raw
# SteamSpy strings, not lowercased ones. Used ONLY as isAdult()'s fallback for a game PICS has
# never covered — the shelves no longer exclude these tags by name (see the HOUSE RULE below).
#
# The precedence matters and is easy to get wrong. index.html's isAdult() treats the PICS flag
# as authoritative for any game PICS has covered — the tags are a fallback for games it has
# not, NOT an additional test. ORing the two here instead over-excluded by hundreds of games
# per shelf and made every generated count disagree with the page.
ADULT_TAGS = {"Nudity", "Sexual Content", "Mature", "NSFW", "Hentai"}

# HOUSE RULE: a preset shelf never highlights adult content. Not "rarely", not "only in the
# niche bands" — never. Shelves are the one place the site puts games in front of someone who
# did not ask for anything specific, so `adult=hide` is mandatory on every one of them and
# validate() fails the job if a shelf omits it.
#
# The lock is `adult=hide` and nothing else. Shelves used to carry a second lock — an `exc=`
# list naming every tag in ADULT_TAGS — on the reasoning that index.html's isAdult() treats the
# PICS flag as authoritative for any game PICS has covered, so the tag test never runs for those
# and a PICS-covered game tagged "Nudity" with an unset flag slips through. That is still true,
# and the tag exclusion did close it — but it closed far more than that: "Nudity" and "Mature"
# sit on plenty of games whose adult content is incidental, and excluding the tag by name threw
# out the game rather than the scene. Deliberate call: the shelves take the storefront's own
# adult flag as the definition of adult, and accept the games that only a tag would have caught.
#
# What `adult=hide` does NOT catch, stated rather than papered over:
#   * a PICS-covered game whose adult flag is unset but whose tags say otherwise;
#   * a game whose only adult signal is its title — no flag, no tag, innocuous SteamSpy tags.
# The popular shelves' 5,000-review floor thins both out; it does not reach the niche bands.


# Games with no ending. Their Length (reviewers' playtime, length.json) is meaningless-to-
# enormous — the people who recommend an MMO or an idler have played it for hundreds of hours —
# so on any length-based shelf they crowd out everything with an actual
# credits roll. Excluded from length shelves only — they are legitimate results elsewhere.
# Kept identical to the `exc=` list every length shelf carries below — the query is what the
# page actually runs, so any tag named here and not there is a count the generator reports and
# the page does not produce.
NO_ENDING = ["idle", "incremental", "clicker", "idler", "mmorpg", "massively multiplayer",
             "free to play"]
# Only the length shelves carry an exc= list, and it names the no-ending tags alone — adult
# content is handled by `adult=hide`, not by tag name (see the HOUSE RULE above).
NO_ENDING_Q = "exc=" + ",".join(t.replace(" ", "+") for t in NO_ENDING)

# Canonicalisation, mirroring CANON_GROUPS in index.html.
CANON = {}
for _c, _vs in {
    "co-op": ["online co-op", "local co-op", "co-op campaign", "online co-op campaign",
              "local co-op campaign", "4 player local"],
    "multiplayer": ["online multiplayer", "local multiplayer", "lan multiplayer"],
    "pvp": ["online pvp", "local pvp"],
    "singleplayer": ["single player"],
}.items():
    for _v in _vs:
        CANON[_v] = _c

YEAR = 365 * 86400
REL_WINDOW = {"1mo": 30, "3mo": 90, "6mo": 180, "1yr": 365}

# ── the shelves ──────────────────────────────────────────────────────────────────────────────
# `q` is the querystring the chip applies, and is the single source of truth for what the
# preset MEANS: the checks below are written to mirror it, and validate() asserts that every
# param used here is one the frontend actually reads.
#
# Every shelf sets ratesrc=all. The 30-day default would make a shelf's membership depend on a
# score only 5.9% of games have, so a preset would mean something different for those games
# than for the rest — and this script could not reproduce the frontend's answer exactly.
PRESETS = [
    dict(id="deals", label="Best deals under $10", tone="popular",
         blurb="Discounted right now, under $10, and actually good.",
         q="pc=sale&pmax=10&minscore=70&rev=4&ratesrc=all&adult=hide&sort=qtpd&dir=-1",
         rev=(5000, None), minscore=70, pmax=10.0, on_sale=True),
    # Sibling to "deals", and deliberately the opposite question. That shelf asks "what is
    # CHEAP" (a $10 ceiling) and a 15% cut on an $8 game clears it; this one asks "what is
    # heavily MARKED DOWN" and does not care what the game costs — a 60%-off $60 game is the
    # point, and it can never appear on the other shelf. Sorted by discount rather than qtpd
    # for the same reason: the label promises a big cut, so the biggest cuts open the list.
    # 50/80/5k leaves 267 games (measured 2026-09-16); dropping to 40% adds only 15, and 70%
    # costs 87, so the knee is right about here.
    dict(id="slashed", label="Half off or better", tone="popular",
         blurb="50% or more off, 80%+ positive, and thousands of people have played it.",
         q="pc=sale&minsale=50&minscore=80&rev=4&ratesrc=all&adult=hide&sort=discount_pct&dir=-1",
         rev=(5000, None), minscore=80, on_sale=True, minsale=50),
    dict(id="long", label="Long games, highly rated", tone="popular",
         blurb="40 hours or more, 80%+ positive, with an actual ending.",
         q="hmin=40&minscore=80&rev=4&ratesrc=all&adult=hide&" + NO_ENDING_Q + "&sort=qtpd&dir=-1",
         rev=(5000, None), minscore=80, hmin=40.0, no_ending=False),
    dict(id="short", label="Short and cheap", tone="popular",
         blurb="Six hours or less, under $10 — a weekend, not a commitment.",
         q="hmax=6&pmax=10&minscore=70&rev=4&ratesrc=all&adult=hide&" + NO_ENDING_Q + "&sort=qtpd&dir=-1",
         rev=(5000, None), minscore=70, pmax=10.0, hmax=6.0, no_ending=False),
    dict(id="coop", label="Co-op picks", tone="popular",
         blurb="Games to play with someone else, well reviewed.",
         q="inc=co-op&minscore=70&rev=4&ratesrc=all&adult=hide&sort=qtpd&dir=-1",
         rev=(5000, None), minscore=70, tag="co-op"),
    dict(id="new", label="New and well-reviewed", tone="popular",
         blurb="Released in the last year and already well liked.",
         q="rel=1yr&minscore=80&rev=4&ratesrc=all&adult=hide&sort=release_ts&dir=-1",
         rev=(5000, None), minscore=80, rel="1yr"),
    dict(id="gems", label="Hidden gems", tone="niche",
         blurb="80%+ positive, but under 5,000 reviews — the ones that got missed.",
         q="minscore=80&rev=2,3&ratesrc=all&adult=hide&" + NO_ENDING_Q + "&sort=qtpd&dir=-1",
         rev=(100, 5000), minscore=80, no_ending=False),
    dict(id="indie", label="Under-the-radar indie", tone="niche",
         blurb="Indie, 90%+ positive, still under 5,000 reviews.",
         q="inc=indie&minscore=90&rev=2,3&ratesrc=all&adult=hide&" + NO_ENDING_Q + "&sort=qtpd&dir=-1",
         rev=(100, 5000), minscore=90, tag="indie", no_ending=False),
]

# Params the frontend's loadFromURL() understands. A preset naming anything else would land
# the user in a state the page silently ignores, so it is a build error, not a warning.
KNOWN_PARAMS = {"q", "inc", "exc", "tagmode", "pc", "basis", "pmin", "pmax",
                "minsale", "qmin", "qmax", "hmin", "hmax", "minscore", "rev", "trend", "upd",
                "rel", "ratesrc", "pt", "flags", "noflags", "ai", "adult", "ctrl", "deck",
                "sort", "dir", "per", "wishonly"}

# A shelf below this many results is not worth a click; above the warn line it is so broad it
# is barely a filter. Both are reported, never auto-corrected.
MIN_HEALTHY = 25
BROAD_WARN = 20000


def load(name, key):
    with open(name, encoding="utf-8") as fh:
        return json.load(fh)[key]


def build():
    games = load("games.json", "games")
    prices = load("prices.json", "prices")
    tags = load("tags.json", "tags")
    # Length is the review-based figure from length_model.py — the same one the page reads.
    # hltb.json is calibration-only since Sep 2026 and must not feed a shelf.
    length = load("length.json", "length")
    try:
        pics = load("pics.json", "apps")
    except (OSError, KeyError):
        pics = {}
    # The frontend does NOT filter on SteamSpy tags where PICS has any: index.html's merge sets
    # game.tags from SteamSpy provisionally, then OVERWRITES it with the decoded PICS tag names
    # whenever PICS carries some ("Valve-ranked and more reliable on the long tail"). PICS covers
    # 129,383 games, so SteamSpy is the exception, not the rule — generating counts off
    # tags.json alone put the tag shelves ~2x out.
    try:
        with open("lookups/tags.json", encoding="utf-8") as fh:
            tag_lk = json.load(fh)
    except OSError:
        tag_lk = {}

    now = time.time()
    rows = []
    for g in games:
        a = str(g.get("appid"))
        pr = prices.get(a) or {}
        ln = length.get(a)
        pi = pics.get(a) or {}
        raw_tags = tags.get(a) or []                      # SteamSpy, the fallback source
        pics_tags = [tag_lk.get(str(i)) for i in (pi.get("tags") or [])]
        pics_tags = [t for t in pics_tags if t]
        names = pics_tags or raw_tags                     # PICS wins wherever it has any
        tg = {CANON.get(t.lower(), t.lower()) for t in names}
        price = pr.get("price_final", g.get("price_final"))
        rows.append(dict(
            title=g.get("title") or "",
            rating=g.get("rating_pct"),
            reviews=g.get("review_count") or 0,
            price=price,
            disc=pr.get("discount_pct", g.get("discount_pct")) or 0,
            free=bool(g.get("is_free")),
            rel=g.get("release_ts"),
            # Same number the page's hoursFor() returns: length.json [hours, n_up, genre].
            hours=(ln[0] if ln else None),
            # PICS-covered -> its flag decides, alone. Uncovered -> the raw tag fallback.
            adult=(bool(pi.get("adult")) if pi else bool(ADULT_TAGS & set(raw_tags))),
            no_end=bool(set(NO_ENDING) & tg),
            tags=tg,
        ))

    def qtpd(r):
        if not r["price"] or r["price"] <= 0 or r["hours"] is None or r["rating"] is None:
            return None
        return r["hours"] * (r["rating"] / 100.0) / r["price"]

    def matches(r, p):
        lo, hi = p["rev"]
        if r["reviews"] < lo or (hi is not None and r["reviews"] >= hi):
            return False
        if (r["rating"] or 0) < p["minscore"]:
            return False
        # Mirrors `adult=hide`, and that is the whole adult test — see the HOUSE RULE above.
        if r["adult"]:
            return False
        if p.get("no_ending") is False and r["no_end"]:
            return False
        if p.get("tag") and p["tag"] not in r["tags"]:
            return False
        # `pc=sale` is one of three non-overlapping price classes on the page: a game is free,
        # or paid-and-discounted, or paid-at-full-price. So "on sale" excludes FREE games too,
        # not just undiscounted ones — a free game is class "free" whatever discount_pct says.
        # Exactly one game in the catalogue is currently both free and carrying a discount, so
        # this corrects a one-game overcount rather than anything visible; it is fixed because
        # a generator whose entire job is to reproduce the page's answer should reproduce it.
        if p.get("on_sale") and not (r["disc"] > 0 and not r["free"]):
            return False
        # Min sale % floor, mirroring passNonRange(): free games are exempt (they are kept or
        # dropped by the price class alone), everything else must clear the floor.
        if p.get("minsale") is not None and not r["free"] and (r["disc"] or 0) < p["minsale"]:
            return False
        if p.get("pmax") is not None:
            if r["price"] is None or r["price"] > p["pmax"]:
                return False
        if p.get("hmin") is not None or p.get("hmax") is not None:
            if r["hours"] is None:
                return False
            if p.get("hmin") is not None and r["hours"] < p["hmin"]:
                return False
            if p.get("hmax") is not None and r["hours"] > p["hmax"]:
                return False
        if p.get("rel"):
            age = (now - r["rel"]) if r["rel"] else float("inf")
            if age > REL_WINDOW[p["rel"]] * 86400:
                return False
        return True

    out, problems = [], []
    for p in PRESETS:
        parsed = urllib.parse.parse_qs(p["q"], keep_blank_values=True)
        bad = set(parsed) - KNOWN_PARAMS
        if bad:
            raise SystemExit(f"preset {p['id']}: unknown URL params {sorted(bad)}")
        # The house rule, enforced at build time rather than trusted to review: no shelf ships
        # without adult=hide. A new shelf that forgets it fails the job loudly instead of
        # quietly putting adult content on the landing page.
        if parsed.get("adult") != ["hide"]:
            raise SystemExit(f"preset {p['id']}: every shelf must set adult=hide")

        sel = [r for r in rows if matches(r, p)]
        scored = sorted((r for r in sel if qtpd(r) is not None), key=qtpd, reverse=True)
        # The `sample` below is the shelf's OPENING ROWS, so it has to be ordered the way the
        # chip's own querystring orders them — not always by qtpd. This used to special-case
        # release_ts alone, which quietly mislabelled any shelf sorted some third way: the
        # report would print the qtpd leaders under a shelf that opens on the biggest discount.
        # Driven off the query now, so a new shelf's sort is honoured without touching this.
        sort_key = (urllib.parse.parse_qs(p["q"]).get("sort") or ["qtpd"])[0]
        SAMPLE_ORDER = {
            "release_ts":   lambda: sorted(sel, key=lambda r: r["rel"] or 0, reverse=True),
            "discount_pct": lambda: sorted(sel, key=lambda r: r["disc"] or 0, reverse=True),
        }
        top = SAMPLE_ORDER.get(sort_key, lambda: scored)()

        flags = []
        if len(sel) < MIN_HEALTHY:
            flags.append("thin")
        if len(sel) > BROAD_WARN:
            flags.append("broad")
        if not scored:
            flags.append("no-qtpd")
        if flags:
            problems.append(f"{p['id']}: {', '.join(flags)} ({len(sel):,} results)")

        out.append(dict(
            id=p["id"], label=p["label"], blurb=p["blurb"], tone=p["tone"], query=p["q"],
            # Advisory only, and deliberately NOT rendered on the chip. This count comes from
            # a Python re-implementation of passFilters(); mirroring the frontend exactly took
            # three corrections (PICS tags override SteamSpy, HLTB realness was per-metric, the
            # PICS adult flag replaces rather than adds to the tag test) and can drift again on
            # the next frontend change. It is good enough to flag a shelf going thin or broad,
            # which is this file's job; it is not good enough to show a user as fact.
            count_at_scrape=len(sel), scored=len(scored),
            # Sample titles are for the generated report and for eyeballing a shelf in review.
            # The page does NOT render them: it applies the query and shows whatever is live,
            # so a preset can never show a game that is no longer in the data.
            sample=[r["title"] for r in top[:5]],
            flags=flags,
        ))

    return dict(
        _format="presets_v1",
        generated_at=int(now),
        min_healthy=MIN_HEALTHY, broad_warn=BROAD_WARN,
        presets=out,
        problems=problems,
    )


def main():
    data = build()
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=False)
        fh.write("\n")
    print(f"{OUT}: {len(data['presets'])} shelves, {os.path.getsize(OUT):,} bytes")
    for p in data["presets"]:
        mark = ("  <-- " + ", ".join(p["flags"])) if p["flags"] else ""
        print(f"  {p['tone']:8} {p['label']:26} {p['count_at_scrape']:6,} results "
              f"({p['scored']:,} scored){mark}")
        print(f"           {' / '.join(p['sample'][:3])}")
    if data["problems"]:
        print("\nPROBLEMS:")
        for line in data["problems"]:
            print("  " + line)


if __name__ == "__main__":
    main()
