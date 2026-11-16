#!/usr/bin/env python3
"""
Steam QTPD — player counts and what is spiking
===========================================================================
One SEPARATE, independent job that records how many people are in a game right now, hourly,
and from that history works out which games are spiking. Nothing else in the repo holds
player counts; this is the raw material for spotting a breakout early (video discovery).

THE SOURCES (verified 2026-11-16, both keyless, both api.steampowered.com)
--------------------------------------------------------------------------
  ISteamChartsService/GetGamesByConcurrentPlayers/v1   one call: Valve's top 100 by players
      now, each with its 24-hour peak — the numbers SteamDB's "Most Played Games" shows.
  ISteamUserStats/GetNumberOfCurrentPlayers/v1?appid=N one game's players now. 600 calls
      at 8 in parallel took 6 s with no throttling; an unknown appid is a 404.

There is NO feed of "trending" games: SteamDB computes its list from its own history, and
so does this job. That is why it keeps a state file.

WHAT IS TRACKED
---------------
The top 100 covers games above roughly 9,000 players, and a breakout starts far below that.
So each run also polls a watchlist, one call per game:
  * games released in the last NEW_DAYS (90) with at least NEW_MIN_REVIEWS (100) reviews,
  * every game seen in the top 100 or listed as trending in the last STICKY_DAYS (30), so
    a game is still followed after it leaves the chart or turns 90 days old.
Only games in games.json are tracked: the rest of the chart is software and apps the
catalogue does not hold, listed by appid only (`top_uncatalogued`).

WHAT COUNTS AS TRENDING
-----------------------
Players swing with the time of day, so two single readings compare badly. Each game's
history is cut into 24-hour windows back from now, and the peak of the last 24 hours is
compared with the median peak of the earlier days (up to six). A game is trending when
that ratio is at least MIN_RATIO and the 24-hour peak is at least MIN_PLAYERS; the list is
ranked by ratio. A game with players but no earlier day to compare with (it just joined
the watchlist) goes to `newly_tracked` instead, ranked by peak. Until the job has run for
two days both lists are empty.

HOUSE RULE: both lists and `top` name games nobody asked for, so they are built over games
that pass adult=hide, decided as the page decides it (stats.is_adult). Adult games are not
tracked at all, and build() refuses to write a list that names one.

Output players.json, format players_v1:
  { "generated_at", "since", "snapshots", "tracked", "window_days",
    "top":           [ { rank, appid, title, players, peak_24h } ],
    "top_uncatalogued": [ appid ],
    "trending":      [ { appid, title, release_ts, players, peak_24h, base_peak, ratio,
                         spark: [players per snapshot, null = no answer] } ],
    "newly_tracked": [ { appid, title, release_ts, players, peak_24h, spark } ] }
players_state.json (the history, not published): { "times": [ts], "series": { "<appid>":
[players per time, null = no answer] }, "sticky": { "<appid>": last seen in the chart } }.

Ownership (one writer per file): THIS -> players.json, players_state.json. Reads
games.json, pics.json and tags.json read-only. From a branch it is a dry run (gitsync).
"""

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import gitsync                      # commits this job's own files to main (gitsync.py)
from stats import is_adult          # the page's isAdult(), the one definition of adult

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"            # read-only (owned by scraper.py)
PICS_FILE = HERE / "pics.json"              # read-only (owned by pics_merge.py)
TAGS_FILE = HERE / "tags.json"              # read-only (owned by tags_refresh.py)
OUT_FILE = HERE / "players.json"            # this job's output (served)
STATE_FILE = HERE / "players_state.json"    # this job's history (not served)

DAY = 86400
WINDOW_DAYS = 7              # history kept, and the span a trend is judged over
NEW_DAYS = 90                # a release this recent is watched ...
NEW_MIN_REVIEWS = 100        # ... once it has this many reviews
STICKY_DAYS = 30             # a game seen in the chart or trending stays watched this long
MIN_GAP = 45 * 60            # the price job's dispatch and the cron can both fire: one snapshot an hour
MIN_PLAYERS = 200            # a 24-hour peak below this is noise, not a trend
MIN_RATIO = 1.5              # last 24 hours' peak against the earlier days' median peak
MIN_DAY_SAMPLES = 12         # a 24-hour window with fewer snapshots is not a day to compare with
LIST_LIMIT = 50              # rows in trending / newly_tracked
WORKERS = 4

API = "https://api.steampowered.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (steam-qtpd player counts; github pages dataset builder)"})


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def save_json(path, doc):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Steam
# --------------------------------------------------------------------------- #
def fetch_top():
    """Valve's top 100 by players now: [{rank, appid, players, peak_24h}], or None."""
    for attempt in range(3):
        try:
            r = SESSION.get(f"{API}/ISteamChartsService/GetGamesByConcurrentPlayers/v1/", timeout=30)
            if r.status_code == 200:
                ranks = r.json()["response"]["ranks"]
                return [{"rank": x["rank"], "appid": int(x["appid"]),
                         "players": int(x["concurrent_in_game"]), "peak_24h": int(x["peak_in_game"])}
                        for x in ranks]
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log(f"  chart call failed ({attempt + 1}/3): {e}")
        time.sleep(5 * (attempt + 1))
    return None


def fetch_count(appid):
    """Players in one game right now, or None when Steam gave no answer for it."""
    for attempt in range(2):
        try:
            r = SESSION.get(f"{API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                            params={"appid": appid}, timeout=20)
            if r.status_code == 404:              # Steam has no player stat for this app
                return None
            if r.status_code == 200:
                resp = r.json().get("response") or {}
                return int(resp["player_count"]) if resp.get("result") == 1 else None
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass
        time.sleep(2 * (attempt + 1))
    return None


def poll(appids):
    """{appid: players or None} for every appid."""
    with ThreadPoolExecutor(WORKERS) as ex:
        return dict(zip(appids, ex.map(fetch_count, appids)))


# --------------------------------------------------------------------------- #
# Watchlist and history
# --------------------------------------------------------------------------- #
def watchlist(games, sticky, now):
    """Sorted appids to poll: recent releases with enough reviews, plus the sticky ones.
    `games` is {appid: games.json row} already limited to games that pass adult=hide."""
    new = {a for a, g in games.items()
           if g.get("release_ts") and 0 <= now - g["release_ts"] < NEW_DAYS * DAY
           and (g.get("review_count") or 0) >= NEW_MIN_REVIEWS}
    return sorted(new | {a for a in sticky if a in games})


def record(state, now, counts):
    """Append one snapshot to the history, keep WINDOW_DAYS of it, and keep only the games in
    `counts` (the current watchlist). A game new to the list starts with nulls."""
    keep = [i for i, t in enumerate(state["times"]) if now - t < WINDOW_DAYS * DAY]
    times = [state["times"][i] for i in keep]
    series = {}
    for a, n in counts.items():
        old = state["series"].get(a)
        series[a] = ([old[i] for i in keep] if old else [None] * len(times)) + [n]
    state["times"], state["series"] = times + [now], series


def daily_peaks(times, vals, now):
    """[(peak or None, snapshots answered)] for each 24-hour window back from `now`:
    index 0 is the last 24 hours."""
    out = [[None, 0] for _ in range(WINDOW_DAYS)]
    for t, v in zip(times, vals):
        d = int((now - t) // DAY)
        if v is None or not 0 <= d < WINDOW_DAYS:
            continue
        out[d][0] = v if out[d][0] is None else max(out[d][0], v)
        out[d][1] += 1
    return [tuple(x) for x in out]


def trend(times, vals, now):
    """(peak_24h, base_peak, ratio) for one game. base_peak and ratio are None when no earlier
    day has enough snapshots to compare with; None altogether without a reading in the last 24h."""
    days = daily_peaks(times, vals, now)
    peak = days[0][0]
    if peak is None:
        return None
    earlier = [p for p, n in days[1:] if n >= MIN_DAY_SAMPLES]
    if not earlier:
        return peak, None, None
    base = statistics.median(earlier)
    return peak, base, peak / max(base, 1)


def build(state, top, games, now):
    """The players.json document. `games` holds only games that pass adult=hide, and every
    named row is taken from it: a list that names any other game is refused."""
    times = state["times"]
    # No comparison is possible until the job itself has a full earlier day of snapshots.
    has_history = sum(1 for t in times if DAY <= now - t < 2 * DAY) >= MIN_DAY_SAMPLES
    trending, newly = [], []
    for a, vals in state["series"].items():
        tr = trend(times, vals, now) if a in games else None
        if not tr or tr[0] < MIN_PLAYERS or not has_history:
            continue
        peak, base, ratio = tr
        g = games[a]
        row = {"appid": a, "title": g.get("title"), "release_ts": g.get("release_ts"),
               "players": next((v for v in reversed(vals) if v is not None), None), "peak_24h": peak}
        if ratio is None:
            newly.append({**row, "spark": vals})
        elif ratio >= MIN_RATIO:
            trending.append({**row, "base_peak": round(base), "ratio": round(ratio, 2), "spark": vals})
    trending.sort(key=lambda r: (-r["ratio"], -r["peak_24h"]))
    newly.sort(key=lambda r: -r["peak_24h"])
    top_rows = [{"rank": x["rank"], "appid": x["appid"], "title": games[x["appid"]].get("title"),
                 "players": x["players"], "peak_24h": x["peak_24h"]} for x in top if x["appid"] in games]
    doc = {"_format": "players_v1", "generated_at": now, "since": times[0] if times else now,
           "snapshots": len(times), "tracked": len(state["series"]), "window_days": WINDOW_DAYS,
           "top": top_rows,
           "top_uncatalogued": [x["appid"] for x in top if x["appid"] not in games],
           "trending": trending[:LIST_LIMIT], "newly_tracked": newly[:LIST_LIMIT]}
    # The adult lock, enforced at build time like presets.py and stats.py do for theirs.
    for key in ("top", "trending", "newly_tracked"):
        for r in doc[key]:
            if r["appid"] not in games:
                raise SystemExit(f"players: {r['appid']} in `{key}` is not a game that passes adult=hide")
    return doc


def load_games():
    """{appid: games.json row} for every game that passes adult=hide."""
    gd = load_json(GAMES_FILE, {})
    if gd.get("sample") or not gd.get("games"):
        return {}
    pics = load_json(PICS_FILE, {}).get("apps") or {}
    tags = load_json(TAGS_FILE, {}).get("tags") or {}
    return {int(g["appid"]): g for g in gd["games"]
            if not is_adult(pics.get(str(g["appid"])), tags.get(str(g["appid"])) or g.get("tags") or [])}


def load_state():
    doc = load_json(STATE_FILE, {})
    return {"times": doc.get("times") or [],
            "series": {int(a): v for a, v in (doc.get("series") or {}).items()},
            "sticky": {int(a): t for a, t in (doc.get("sticky") or {}).items()}}


def main():
    now = int(time.time())
    games = load_games()
    if not games:
        log("games.json missing or empty — nothing to do.")
        return
    state = load_state()
    if state["times"] and now - state["times"][-1] < MIN_GAP:
        log(f"Last snapshot is {(now - state['times'][-1]) // 60} min old — nothing to do.")
        return

    top = fetch_top()
    if top is None:
        sys.exit("players: the top-100 chart did not answer; no snapshot this run")
    sticky = {a: t for a, t in state["sticky"].items() if now - t < STICKY_DAYS * DAY}
    sticky.update({x["appid"]: now for x in top})
    watch = watchlist(games, sticky, now)
    counts = poll(watch)
    answered = sum(v is not None for v in counts.values())
    log(f"Watchlist {len(watch)} ({answered} answered) | chart {len(top)} | "
        f"history {len(state['times'])} snapshot(s)")
    if answered < len(watch) / 2:
        sys.exit("players: under half the watchlist answered; no snapshot this run")

    record(state, now, counts)
    doc = build(state, top, games, now)
    sticky.update({r["appid"]: now for r in doc["trending"]})
    save_json(OUT_FILE, doc)
    save_json(STATE_FILE, {"_format": "players_state_v1", "times": state["times"],
                           "series": {str(a): v for a, v in state["series"].items()},
                           "sticky": {str(a): t for a, t in sorted(sticky.items())}})
    log(f"Done. {len(doc['trending'])} trending, {len(doc['newly_tracked'])} newly tracked, "
        f"{len(doc['top'])} of the chart in the catalogue.")
    gitsync.commit(["players.json", "players_state.json"],
                   f"players: {len(watch)} games, {len(doc['trending'])} trending")


if __name__ == "__main__":
    main()
