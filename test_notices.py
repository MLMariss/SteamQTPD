#!/usr/bin/env python3
"""Regression tests for notices.py — the 3rd-party DRM / account / kernel anti-cheat layer.

No network, no git: appdetails answers are stubbed with the shapes notices_probe.py saw on
2026-11-02 (Battlefield 2042, HELLDIVERS 2, Hades), store pages with markup modelled on the
probe's tag-stripped snippets (the raw blocks are dumped by the probe's Q5; when that run lands,
these fixtures are the thing to compare), and main() runs against a temp directory.
Each case pins one promise: the ranked order, what a failed or delisted answer may touch, and
that a notice the store stops stating is removed (a game that dropped Denuvo leaves the filter).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

if "requests" not in sys.modules:                      # import without the dependency present
    m = types.ModuleType("requests")
    m.Session = lambda: types.SimpleNamespace(headers={}, get=lambda *a, **k: None)
    m.RequestException = Exception
    sys.modules["requests"] = m

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notices as N  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


DAY = 86400
NOW = time.time()


def answer(appid, drm=None, acct=None, success=True):
    data = {"type": "game", "name": f"App {appid}"}
    if drm is not None:
        data["drm_notice"] = drm
    if acct is not None:
        data["ext_user_account_notice"] = acct
    return {str(appid): {"success": success, "data": data} if success else {"success": False}}


print("\n== clean / read_notices ==")
check("clean: collapses whitespace and tags", N.clean(" Denuvo <br> Anti-tamper\n ") == "Denuvo Anti-tamper")
check("clean: empty -> None", N.clean("   ") is None and N.clean(None) is None)
check("clean: caps long text", len(N.clean("x" * 400)) == N.NOTICE_MAX)
bf = answer(1517290, drm="EA online activation and EA app software installation and background use "
            "required. Denuvo Anti-tamper", acct="EA Account")
st, e = N.read_notices(bf, 1517290)
check("Battlefield 2042: both notices", st == "ok" and e["a"] == "EA Account" and "Denuvo" in e["d"], e)
st, e = N.read_notices(answer(553850, acct="PlayStation Network (Supports Linking to Steam Account)"), 553850)
check("HELLDIVERS 2: account only", st == "ok" and set(e) == {"a"}, e)
check("Hades: answered, no notice", N.read_notices(answer(1145360), 1145360) == ("ok", {}))
check("success:false -> gone", N.read_notices(answer(5, success=False), 5) == ("gone", None))
check("no answer -> fail", N.read_notices(None, 5) == ("fail", None))
check("wrong appid in reply -> fail", N.read_notices(answer(6), 5) == ("fail", None))

print("\n== read_anticheat: store-page markup ==")
HEAD = '<html><div class="apphub_AppName" id="appHubAppName">X</div><div id="game_area_purchase" class="game_area_purchase">'
HD2 = (HEAD + '<div class="DRM_notice"><div>Requires 3rd-Party Account: PlayStation Network (Supports Linking'
       ' to Steam Account)</div></div><div class="anticheat_section DRM_notice"><div class="anticheat_header">'
       'Uses Kernel Level Anti-Cheat</div><div class="anticheat_name">Easy Anti-Cheat</div></div></div>')
RIVALS = (HEAD + '<div class="anticheat_section anticheat_nonkernel_notice"><div>Uses Anti-Cheat Software</div>'
          '<div class="anticheat_name">NetEase Game Security</div></div><div class="DRM_notice">Requires '
          'agreement to a 3rd-party EULA. Kernel drivers are mentioned here, not in the block.</div>')
# Real markup (probe Q5, 2026-11-02): the uninstall note is a span INSIDE anticheat_name.
FINALS = (HEAD + '<div class="anticheat_section DRM_notice" > <div>Uses Kernel Level Anti-Cheat</div> '
          '<div class="anticheat_name">Denuvo<span class="anticheat_uninstalls"> - Requires manual '
          'removal after game uninstall</span></div> </div>')
COD = (HEAD + '<div class="anticheat_section DRM_notice" > <div>Uses Kernel Level Anti-Cheat</div> '
       '<div class="anticheat_name">RICOCHET Anti-Cheat<span class="anticheat_uninstalls"></span></div> '
       '<div class="anticheat_name">Boot Protection<span class="anticheat_uninstalls"> - Requires both '
       'Secure Boot &amp; TPM 2.0</span></div> </div>')
check("HELLDIVERS 2: kernel, Easy Anti-Cheat", N.read_anticheat(HD2) == ("ok", {"k": "Easy Anti-Cheat"}), N.read_anticheat(HD2))
check("Marvel Rivals: non-kernel, name kept; a later block's 'Kernel' is not read",
      N.read_anticheat(RIVALS) == ("ok", {"c": "NetEase Game Security"}), N.read_anticheat(RIVALS))
check("THE FINALS: kernel, name without the uninstall note", N.read_anticheat(FINALS) == ("ok", {"k": "Denuvo"}), N.read_anticheat(FINALS))
check("Call of Duty: two names, both notes dropped",
      N.read_anticheat(COD) == ("ok", {"k": "RICOCHET Anti-Cheat, Boot Protection"}), N.read_anticheat(COD))
check("a block with no readable name -> 'yes'",
      N.read_anticheat(HEAD + '<div class="anticheat_section DRM_notice">Uses Kernel Level Anti-Cheat</div>') == ("ok", {"k": "yes"}))
check("Stardew-like page: no block -> ok, nothing", N.read_anticheat(HEAD + "<p>Farm.</p>") == ("ok", {}))
check("not a game page (front page, error) -> gone", N.read_anticheat("<html>Welcome to Steam</html>") == ("gone", None))
check("no answer -> fail", N.read_anticheat(None) == ("fail", None))

print("\n== plan: the ranked order ==")
games = [(1, 50000), (2, 20000), (3, 5000), (4, 300), (5, 40), (6, 9000), (7, 2000), (8, 3)]
checked = {1: NOW - 10 * DAY,      # carries a notice, 10 days old      -> rank 0
           2: NOW - 2 * DAY,       # changed per PICS since             -> rank 1
           3: NOW - 2 * DAY,       # fresh, unchanged                   -> not due
           6: NOW - 61 * DAY,      # big, 61 days old                   -> rank 5
           5: NOW - 1 * DAY}       # small, changed                     -> rank 3
recent = {2: NOW - DAY, 5: NOW - 1000, 3: NOW - 3 * DAY}   # 3's change predates its check
notices = {1: {"d": "Denuvo"}}
order = N.plan(games, checked, {}, recent, notices, set(), NOW)
check("flagged > changed big > never-checked big > changed small > never-checked rest > backstop",
      [a for k, a in order if k == "app"] == [1, 2, 7, 5, 4, 8, 6], order)
check("a fresh, unchanged game is not due", ("app", 3) not in order)
check("a flagged game checked 2 days ago is not re-asked",
      ("app", 1) not in N.plan(games, {**checked, 1: NOW - 2 * DAY}, {}, recent, notices, set(), NOW))
order = N.plan(games, checked, {}, recent, notices, {4, 7}, NOW)
pg = [a for k, a in order if k == "page"]
check("page checks: multiplayer games (7, 4) plus big games the feed flagged (2, 3); app first at equal rank",
      pg == [2, 3, 7, 4] and order.index(("app", 7)) < order.index(("page", 7)), order)
check("a small game the feed flagged gets no page check unless multiplayer", ("page", 5) not in order)
check("a game with an anti-cheat entry is page-checked weekly even if not multiplayer",
      ("page", 3) in N.plan(games, checked, {3: NOW - 8 * DAY}, recent, {3: {"k": "x"}}, set(), NOW))

print("\n== main(): a run against stubbed appdetails ==")
d = Path(tempfile.mkdtemp())
N.GAMES_FILE, N.PICS_STATE_FILE, N.PICS_FILE = d / "games.json", d / "pics_state.json", d / "pics.json"
N.OUT_FILE, N.STATE_FILE = d / "notices.json", d / "notices_state.json"
N.DELAY, commits = 0, []
N.git_checkpoint = lambda msg: commits.append(msg)
(d / "games.json").write_text(json.dumps({"games": [
    {"appid": 1517290, "review_count": 100000}, {"appid": 553850, "review_count": 800000},
    {"appid": 1145360, "review_count": 250000}, {"appid": 990080, "review_count": 370000},
    {"appid": 77, "review_count": 10}, {"appid": 88, "review_count": 5, "unknown": True}]}))
(d / "pics.json").write_text(json.dumps({"apps": {"553850": {"cats": [1, 38]}, "1145360": {"cats": [2]}}}))
PAGES = {553850: HD2}
REPLIES = {1517290: bf, 553850: answer(553850, acct="PlayStation Network"),
           1145360: answer(1145360), 990080: None, 77: answer(77, success=False)}
asked = []
def fake_get(url, params=None, text=False):
    if text:                                           # a store page
        aid = int(url.rstrip("/").split("/")[-1])
        asked.append(("page", aid))
        return PAGES.get(aid)
    asked.append(params["appids"])
    return REPLIES[params["appids"]]


N.get = fake_get
N.main()
out = json.loads((d / "notices.json").read_text())
state = json.loads((d / "notices_state.json").read_text())
check("asked biggest first, the multiplayer game's page after its appdetails; unknown-status never",
      asked == [553850, ("page", 553850), 990080, 1145360, 1517290, 77], asked)
check("notices.json lists only games with a notice",
      set(out["games"]) == {"1517290", "553850"} and out["count"] == 2, out["games"])
check("HELLDIVERS 2 carries the account AND the kernel anti-cheat",
      out["games"]["553850"] == {"a": "PlayStation Network", "k": "Easy Anti-Cheat"}, out["games"]["553850"])
check("`pages` counts page-checked games", out["pages"] == 1, out.get("pages"))
check("a game Steam did not answer stays unchecked (due next run)", "990080" not in state["checked"])
check("a delisted (success:false) game is marked checked", "77" in state["checked"])
check("`checked` counts answered games", out["checked"] == 4, out["checked"])
check("one final commit", len(commits) == 1, commits)

# Next run, eight days later: HELLDIVERS 2 is flagged and due weekly; this time the store no longer
# states the account requirement — it must leave the layer.
REPLIES[553850] = answer(553850)
PAGES[553850] = HEAD + "<p>no anti-cheat any more</p>"
real_time = time.time
N.time.time = lambda: real_time() + 8 * DAY
try:
    asked.clear()
    N.main()
finally:
    N.time.time = real_time
out = json.loads((d / "notices.json").read_text())
check("flagged games re-asked a week on", asked[:3] == [553850, ("page", 553850), 1517290], asked)
check("notices the store stopped stating are removed (account and anti-cheat)", "553850" not in out["games"], out["games"])
check("the other notice is kept", out["games"].get("1517290", {}).get("a") == "EA Account")
shutil.rmtree(d)

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("ALL PASS")
