#!/usr/bin/env python3
"""Regression tests for pics_refresh.py's change-driven refresh (--changes, Nov 2026).

No network: a fake SteamClient stands in for the CM session, so the whole of main() runs —
plan, change feed, fetch loop, shard writes, state file — against a temp directory. Steam's
CM servers are unreachable from a dev sandbox, so the feed's real behaviour is only seen on a
runner; each case below pins one promise the code makes about it, whatever Steam answers:

  * changed games are fetched whatever their age, after never-seen games, before the backstop;
  * Steam saying "too far back" (force_full_*) costs one run of backstop-only, never data;
  * a changed game the run did not reach is carried, not dropped, when the number advances;
  * a feed that does not answer never advances the number;
  * with no number at all, the newest per-app change number seen bootstraps one.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types

# ---- stubs: gevent and steam.client, so pics_refresh imports without either installed ----
gev = types.ModuleType("gevent")


class _Timeout(Exception):
    def __init__(self, *a, **k):
        super().__init__()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


gev.Timeout = _Timeout
sys.modules["gevent"] = gev


class Change:
    def __init__(self, appid, cn):
        self.appid, self.change_number, self.needs_token = appid, cn, False


class Resp:
    def __init__(self, current, apps=(), full=False):
        self.current_change_number = current
        self.force_full_update = full
        self.force_full_app_update = False
        self.app_changes = [Change(a, current) for a in apps]


class FakeClient:
    """Scripted CM session. FEED is what get_changes_since returns (None = no reply);
    FAIL is a set of appids whose chunk raises on every attempt."""
    FEED = None
    FAIL = set()
    log = None

    def __init__(self):
        self.logged_on = False
        FakeClient.log = {"logins": 0, "since": [], "fetched": []}

    def anonymous_login(self):
        self.logged_on = True
        FakeClient.log["logins"] += 1

    def disconnect(self):
        pass

    def logout(self):
        pass

    def get_changes_since(self, cn, app_changes=True, package_changes=False):
        FakeClient.log["since"].append(cn)
        return FakeClient.FEED

    def get_product_info(self, apps, timeout=60):
        if FakeClient.FAIL & set(apps):
            raise RuntimeError("chunk failed")
        FakeClient.log["fetched"].extend(apps)
        return {"apps": {a: {"common": {"name": f"App {a}", "type": "game"},
                             "_change_number": 500 + a} for a in apps}}


steam = types.ModuleType("steam")
steam_client = types.ModuleType("steam.client")
steam_client.SteamClient = FakeClient
steam.client = steam_client
sys.modules["steam"] = steam
sys.modules["steam.client"] = steam_client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pics_refresh as P  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


DAY = 86400
NOW = time.time()
# Catalog: 10 and 20 stored and fresh, 30 stored and stale, 40 never fetched.
CATALOG = [10, 20, 30, 40]


def setup():
    d = tempfile.mkdtemp()
    out = os.path.join(d, "pics_raw")
    os.makedirs(out)
    by_shard = {}
    for aid, age in ((10, 1), (20, 2), (30, 20)):
        by_shard.setdefault(P.shard_of(aid), {})[str(aid)] = {"name": f"old {aid}", "_ts": int(NOW - age * DAY)}
    for sh, apps in by_shard.items():
        P.write_shard(out, sh, apps)
    with open(os.path.join(d, "games.json"), "w") as f:
        json.dump({"games": [{"appid": a} for a in CATALOG]}, f)
    return d, out


def run(d, out, *extra, feed=None, fail=()):
    FakeClient.FEED, FakeClient.FAIL = feed, set(fail)
    sys.argv = ["pics_refresh.py", "--catalog", os.path.join(d, "games.json"), "--out", out,
                "--stale-days", "14", "--chunk", "1", *extra]
    P.main()
    return FakeClient.log


def state(d):
    with open(os.path.join(d, "pics_state.json")) as f:
        return json.load(f)


print("\n== pure helpers ==")
check("read_changes: no reply -> no number", P.read_changes(None) == (None, None))
check("read_changes: force_full -> number, no set", P.read_changes(Resp(900, [1], full=True)) == (900, None))
check("read_changes: listed apps", P.read_changes(Resp(900, [1, 2])) == (900, {1, 2}))
u, c, s = P.plan_worklist([1, 2, 3, 4], known={1, 2, 3}, fresh={1, 2}, changed={2, 99}, only_new=False)
check("plan: unseen, then changed (even when fresh), then stale; strangers ignored",
      (u, c, s) == ([4], [2], [3]), (u, c, s))
check("plan: --only-new keeps changed, drops the backstop",
      P.plan_worklist([1, 2, 3, 4], {1, 2, 3}, {1, 2}, {2}, True) == ([4], [2], []))
tmp = tempfile.mkdtemp()
sp = os.path.join(tmp, "s.json")
check("save_state writes a new file", P.save_state(sp, {"change_number": 5, "carry": [3, 3]}))
check("save_state: same content -> no write (no empty commits)", not P.save_state(sp, {"change_number": 5, "carry": [3]}))
check("load_state round trip, carry deduped", P.load_state(sp) == {"change_number": 5, "carry": [3]})
check("load_state: missing file -> fresh start", P.load_state(os.path.join(tmp, "nope.json")) == {"change_number": 0, "carry": []})
shutil.rmtree(tmp)

print("\n== bootstrap: no state yet, Steam says force_full ==")
d, out = setup()
sp = os.path.join(d, "pics_state.json")
log = run(d, out, "--changes", sp, feed=Resp(1000, full=True))
check("asked for changes since 0", log["since"] == [0], log["since"])
check("backstop only: never-fetched 40, then stale 30", log["fetched"] == [40, 30], log["fetched"])
check("state takes Steam's current number", state(d)["change_number"] == 1000, state(d))

print("\n== normal run: a FRESH stored game changed ==")
log = run(d, out, "--changes", sp, feed=Resp(1010, apps=[20, 777]))
check("asked since the saved number", log["since"] == [1000], log["since"])
check("changed game 20 fetched though only 2 days old; nothing else due",
      log["fetched"] == [20], log["fetched"])
check("state advanced, nothing carried", state(d)["change_number"] == 1010 and state(d)["carry"] == [], state(d))
with open(os.path.join(out, f"shard_{P.shard_of(20):02d}.json")) as f:
    rec = json.load(f)["apps"]["20"]
check("game 20's row was rewritten", rec.get("name") == "App 20" and rec["_ts"] >= int(NOW), rec)

print("\n== a changed game's chunk fails: carried, number still advances ==")
log = run(d, out, "--changes", sp, feed=Resp(1020, apps=[10]), fail=[10])
check("state at 1020 with 10 carried", state(d) ["change_number"] == 1020 and state(d)["carry"] == [10], state(d))
log = run(d, out, "--changes", sp, feed=Resp(1030, apps=[]))
check("next run fetches the carried game with an empty feed", log["fetched"] == [10], log["fetched"])
check("carry cleared once fetched", state(d)["carry"] == [] and state(d)["change_number"] == 1030, state(d))

print("\n== the feed does not answer: number NOT advanced ==")
log = run(d, out, "--changes", sp, feed=None)
check("asked twice (one retry)", log["since"] == [1030, 1030], log["since"])
check("number kept at 1030", state(d)["change_number"] == 1030, state(d))
shutil.rmtree(d)

print("\n== bootstrap when Steam gives no number at all ==")
d, out = setup()
sp = os.path.join(d, "pics_state.json")
log = run(d, out, "--changes", sp, feed=Resp(0))
check("starts from the newest per-app change number fetched (500+40)", state(d)["change_number"] == 540, state(d))
shutil.rmtree(d)

print("\n== no --changes: the old behaviour ==")
d, out = setup()
log = run(d, out)
check("backstop as before: 40 then 30", log["fetched"] == [40, 30], log["fetched"])
check("no state file written", not os.path.exists(os.path.join(d, "pics_state.json")))
FakeClient.log = None
run(d, out, "--only-new")                       # 40 is stored now: nothing new
check("--only-new with nothing new never logs in", FakeClient.log is None)
shutil.rmtree(d)

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("ALL PASS")
