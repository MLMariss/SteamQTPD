#!/usr/bin/env python3
"""Regression tests for ea_dates.py — the Early Access dates layer.

The release blocks below are what GetItems returned on 2026-10-22 (ea_dates_probe.py, workflow
0.5). Pure logic plus a temp-dir round trip: no network, no git."""
import json, sys, tempfile, types
from pathlib import Path

# Stub `requests` so the module imports without the dependency present.
if "requests" not in sys.modules:
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(
        headers=type("H", (), {"update": lambda s, d: None})(),
        get=lambda *a, **k: None)
    m.RequestException = Exception
    sys.modules["requests"] = m

import ea_dates as E

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)

NOW = 1792656000          # 2026-10-22 08:00 UTC, the morning the probe ran
item = lambda aid, rel, **kw: dict({"appid": aid, "release": rel}, **kw)

print("\n== entry_for: games in Early Access ==")
# Every Day We Fight: in EA since 10 Jul 2025, 1.0 announced for 22 Oct 2026 at day precision.
edwf = item(1546080, {"steam_release_date": 1752152484, "release_from_early_access_date": 1792652400,
                      "release_from_early_access_style": 1, "is_early_access": True})
served, start = E.entry_for(edwf, None, NOW)
check("planned 1.0 is served with its precision", served == {"ea": 1, "v": 1792652400, "p": 1})
check("the EA start of a launched EA game is remembered", start == 1752152484)
# Dyson Sphere Program: in EA, no 1.0 announced -> nothing to serve, still remembered.
dsp = item(1366540, {"steam_release_date": 1611212340, "is_early_access": True})
served, start = E.entry_for(dsp, None, NOW)
check("EA game with no planned 1.0 is not served", served is None)
check("...but its EA start is remembered", start == 1611212340)
# A planned date with no style comes back as day precision rather than a missing key.
served, _ = E.entry_for(item(1, {"steam_release_date": 1700000000, "is_early_access": True,
                                 "release_from_early_access_date": 1800000000}), None, NOW)
check("missing style defaults to precision 1", served == {"ea": 1, "v": 1800000000, "p": 1})
# Item-level flag when the release block has none.
served, _ = E.entry_for(item(2, {"steam_release_date": 1700000000,
                                 "release_from_early_access_date": 1800000000,
                                 "release_from_early_access_style": 4}, is_early_access=True), None, NOW)
check("item-level is_early_access is honoured", served == {"ea": 1, "v": 1800000000, "p": 4})

print("\n== entry_for: launching INTO Early Access ==")
# The Dead We Knew: coming soon, launches into EA later on 22 Oct 2026.
tdwk = item(4890760, {"steam_release_date": 1792688400, "is_coming_soon": True,
                      "coming_soon_display": "date_full", "is_early_access": True})
served, start = E.entry_for(tdwk, None, NOW)
check("an unreleased EA game is not served", served is None)
check("a future launch date is not remembered as an EA start", start is None)
# Past its date but Steam still says coming soon: not launched yet either.
served, start = E.entry_for(item(3, {"steam_release_date": NOW - 86400, "is_coming_soon": True,
                                     "is_early_access": True}), None, NOW)
check("a still-coming-soon game past its date is not remembered", start is None)

print("\n== entry_for: games that left Early Access ==")
drg = item(548430, {"steam_release_date": 1589374867, "original_steam_release_date": 1519804887})
check("Deep Rock: EA start from original_steam_release_date, 1.0 from steam_release_date",
      E.entry_for(drg, None, NOW) == ({"s": 1519804887, "v": 1589374867}, None))
valheim = item(892970, {"steam_release_date": 1788958595, "original_release_date": 0,
                        "original_steam_release_date": 1612277769,
                        "release_from_early_access_date": 1788937200,
                        "release_from_early_access_style": 1})
check("Valheim: a 1.0 date left over from EA does not make it an EA entry",
      E.entry_for(valheim, None, NOW) == ({"s": 1612277769, "v": 1788958595}, None))
bg3 = item(1086940, {"steam_release_date": 1691074826})
check("Baldur's Gate 3: no original_steam date, nothing remembered -> not served",
      E.entry_for(bg3, None, NOW) == (None, None))
check("Baldur's Gate 3: the remembered EA start fills the gap",
      E.entry_for(bg3, 1601942400, NOW) == ({"s": 1601942400, "v": 1691074826}, None))
check("Steam's date wins over the remembered one",
      E.entry_for(drg, 1500000000, NOW) == ({"s": 1519804887, "v": 1589374867}, None))
same = item(4, {"steam_release_date": 1600000000, "original_steam_release_date": 1600000000 - 3600})
check("an 'original' date on the same day is not an EA start", E.entry_for(same, None, NOW) == (None, None))
check("a remembered date that is not earlier is ignored",
      E.entry_for(item(5, {"steam_release_date": 1600000000}), 1600000000, NOW) == (None, None))
check("no release date at all -> nothing", E.entry_for(item(6, {}), 1500000000, NOW) == (None, None))

print("\n== load_worklists / save: a round trip in a temp dir ==")
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    E.PICS_FILE, E.PICS_UP_FILE = d / "pics.json", d / "pics_upcoming.json"
    E.OUT_FILE, E.STATE_FILE = d / "ea_dates.json", d / "ea_dates_state.json"
    E.PICS_FILE.write_text(json.dumps({"apps": {
        "10": {"ea": True, "tags": [493]},          # in EA
        "20": {"tags": [493, 19]},                  # EA tag, left EA
        "30": {"tags": [19]},                       # never EA
        "40": {"tags": [19]},                       # left EA, tag gone, but remembered
    }}))
    E.PICS_UP_FILE.write_text(json.dumps({"apps": {"50": {"ea": True}, "60": {}}}))
    state = {40: 1500000000, 99: 1400000000}        # 99 is no longer in the catalogue
    wl, cat = E.load_worklists(state)
    check("EA phase: genre 70 (both files) + remembered games", wl["ea"] == [10, 40, 50])
    check("tagged phase: EA tag without genre 70", wl["tagged"] == [20])
    check("catalogue is both PICS files", cat == {10, 20, 30, 40, 50, 60})
    n = E.save({10: {"ea": 1, "v": 1800000000, "p": 2}, 20: {"s": 1500000000, "v": 1600000000},
                99: {"s": 1, "v": 2}}, state, cat, 1792656000)
    doc = json.loads(E.OUT_FILE.read_text())
    st = json.loads(E.STATE_FILE.read_text())
    check("served file pruned to the catalogue", n == 2 and set(doc["games"]) == {"10", "20"})
    check("served file carries format and swept_at", doc["_format"] == "ea_dates_v1" and doc["swept_at"] == 1792656000)
    check("served file is compact (no spaces)", ", " not in E.OUT_FILE.read_text())
    check("state pruned to the catalogue", st["ea_start"] == {"40": 1500000000})

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
