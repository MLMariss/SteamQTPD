#!/usr/bin/env python3
"""End-to-end tests for three scraper.py behaviours, running main() over a temp dir with Steam
stubbed (no network; gitsync commits nothing outside Actions):

  * a stored record with no reviews that Steam now calls "coming soon" (a shell stored before
    the release gate) leaves games.json for the pending room, instead of being re-probed and
    never re-stamped on every run; a real game that answers the same way is kept;
  * a game upcoming_refresh.py reports out mid-run is scraped at the next checkpoint, not at
    the next run (17 games spent ~7h of launch day off the site on 26 Oct 2026);
  * a run that stops on its time budget dispatches the next one; a run that runs out of work
    does not, so an empty queue can never spin."""
import json, os, pathlib, sys, tempfile, time, types

if "requests" not in sys.modules:
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(),
        cookies=type("C", (), {"update": lambda s, d: None})(), get=lambda *a, **k: None)
    m.RequestException = Exception
    m.Response = type("Response", (), {})
    sys.modules["requests"] = m

import scraper as S

fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


D = 86400
NOW = int(time.time())
S.log = lambda msg: None
S.load_pics_rev = lambda: {}
S.CHECKPOINT_SECONDS = -1          # every pop runs the mid-run pickups
S.COMMIT_SECONDS = 10 ** 9         # ...but no mid-run commit is needed for these checks


def record(aid, reviews=10):
    return {"appid": aid, "title": f"Game {aid}", "rating_pct": 80, "review_count": reviews,
            "price_initial": 9.99, "price_final": 9.99, "discount_pct": 0, "is_free": False,
            "release_date": "Jan 1, 2020", "release_ts": NOW - 2000 * D, "tags": [],
            "last_update_ts": None}


def run(answers, *, processed, pending, master, upcoming_after=None, stop_after=None, chain="1"):
    """main() over a fresh temp dir. `answers` maps appid -> build_record's reply; after the
    first call upcoming.json gains `released: upcoming_after`; after `stop_after` calls the
    run hits its time budget. Returns (games, catalog, appids asked in order, gh calls)."""
    d = pathlib.Path(tempfile.mkdtemp())
    for name in ("GAMES_FILE", "CATALOG_FILE", "SEEDS_FILE", "SEEDS_LOG_FILE", "PICS_FILE",
                 "UPCOMING_FILE", "REVIEW_RESTORE_FILE"):
        setattr(S, name, d / pathlib.Path(getattr(S, name)).name)
    S.GAMES_FILE.write_text(json.dumps({"games": list(processed.values())}))
    S.CATALOG_FILE.write_text(json.dumps({"pending": {str(k): v for k, v in pending.items()},
                                          "last_sync": NOW - D, "skipped": []}))
    S.UPCOMING_FILE.write_text(json.dumps({"released": [], "games": []}))
    S.fetch_app_universe = lambda: ({a: 0 for a in master}, True)
    asked, gh = [], []

    def build(aid, prev=None):
        asked.append(aid)
        if len(asked) == 1 and upcoming_after:
            S.UPCOMING_FILE.write_text(json.dumps({"released": upcoming_after, "games": []}))
        if stop_after and len(asked) >= stop_after:
            S.TIME_BUFFER = 10 ** 9            # the next loop check finds the budget spent
        res = answers[aid]
        return dict(res) if isinstance(res, dict) else res
    S.build_record = build
    S.TIME_BUFFER = 120
    real_run = S.subprocess.run
    S.subprocess.run = lambda cmd, **k: gh.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr="")
    S.IN_ACTIONS, os.environ["SCRAPE_CHAIN"] = True, chain
    try:
        S.main()
    finally:
        S.subprocess.run, S.IN_ACTIONS = real_run, False
    games = {g["appid"]: g for g in json.loads(S.GAMES_FILE.read_text())["games"]}
    catalog = json.loads(S.CATALOG_FILE.read_text())
    return games, catalog, asked, gh


old = NOW - 100 * D                        # past the 60-day floor
shell = {"appid": 100, "title": "Shell", "review_count": 0, "release_date": "To be announced",
         "release_ts": None, "scraped_at": old}
ninja = {"appid": 300, "title": "Mark of the Ninja", "review_count": 15873, "rating_pct": 95,
         "release_date": None, "release_ts": None, "scraped_at": old}
real = dict(record(200), scraped_at=old)
answers = {100: ("pending", "To be announced", None), 300: ("pending", "Coming soon", None),
           200: record(200), 700: record(700), 500: record(500)}

print("\n== shells leave games.json; a real game answering 'coming soon' stays ==")
games, catalog, asked, gh = run(answers, processed={100: shell, 200: real, 300: ninja},
                                pending={500: None, 600: NOW + 30 * D}, master=[100, 200, 300, 500, 600, 700],
                                upcoming_after=[500])
check("the shell was asked", 100 in asked)
check("the shell is gone from games.json", 100 not in games)
check("...and waits in the pending room", "100" in catalog["pending"])
check("the reviewed game answering 'coming soon' is kept", 300 in games and games[300]["review_count"] == 15873)
check("an ordinary refresh still lands", games[200]["scraped_at"] >= NOW)

print("\n== a game reported out mid-run is scraped in this run ==")
check("500 was not in the queue at the start, yet was asked", 500 in asked)
check("...and is stored, out of the pending room", 500 in games and "500" not in catalog["pending"])
check("the still-future pending game was not asked", 600 not in asked)

print("\n== chaining ==")
check("a run that ran out of work does not dispatch the next one", gh == [])
games, catalog, asked, gh = run(answers, processed={200: real}, pending={}, master=[200, 700],
                                stop_after=1)
check("a run that stopped on its budget dispatches the next one",
      gh and gh[0][:3] == ["gh", "workflow", "run"] and "scrape.yml" in gh[0])
_, _, _, gh2 = run(answers, processed={200: real}, pending={}, master=[200, 700], stop_after=1, chain="0")
check("SCRAPE_CHAIN=0 switches it off", gh2 == [])

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
