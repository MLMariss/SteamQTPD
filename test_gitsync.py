#!/usr/bin/env python3
"""Tests for gitsync.py against real git: a throwaway bare "origin" with some history, and job
clones made the way the workflows make them (depth 1, partial, sparse). Covers the case that
hung the price, HLTB and coverage jobs in Oct 2026: a PR branch cut from an old main gets
merged while a job is running, and the job's next commit must neither download that branch's
history nor stall. No network: the remote is a local file:// repo."""
import os, pathlib, subprocess, sys, tempfile

import gitsync as G

G.time.sleep = lambda s: None          # retries without the backoff
fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


def git(cwd, *args, ok=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if ok and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


root = pathlib.Path(tempfile.mkdtemp(prefix="gitsync-test-"))
remote = root / "origin.git"
url = f"file://{remote}"
git(root, "init", "-q", "--bare", "-b", "main", str(remote))
git(remote, "config", "uploadpack.allowFilter", "true")
git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")

dev = root / "dev"                      # plays every other writer and the PR author
git(root, "clone", "-q", url, str(dev))
git(dev, "config", "user.email", "dev@x"); git(dev, "config", "user.name", "dev")
for i in range(120):
    write(dev / "games.json", f"games v{i}\n" + "x" * 3000)
    write(dev / "prices.json", f"prices v{i}\n")
    write(dev / "shots" / f"shard_{i % 4:02d}.json", f"shard v{i}\n")
    write(dev / "index.html", f"page v{i}\n")
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", f"data {i}")
git(dev, "push", "-q", "origin", "main")
old_base = git(dev, "rev-parse", "HEAD~100")


def job_clone(name, sparse=("/prices.json", "/games.json", "/shots/")):
    """A job checkout as actions/checkout makes one: depth 1, blob:none, sparse, on main."""
    d = root / name
    git(root, "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout", url, str(d))
    git(d, "config", "user.email", "job@x"); git(d, "config", "user.name", "job")
    if sparse:
        git(d, "sparse-checkout", "set", "--no-cone", *sparse)
    git(d, "checkout", "-q", "main")
    return d


def remote_show(path):
    return git(remote, "show", f"main:{path}", ok=False)


os.environ["GITHUB_ACTIONS"] = "true"

print("\n== a plain commit lands on main ==")
job = job_clone("job1")
write(job / "prices.json", "prices from job1\n")
check("commit() returns True", G.commit(["prices.json"], "prices: job1", cwd=job))
check("main has the job's file", remote_show("prices.json") == "prices from job1")
check("main's other files untouched", remote_show("index.html") == "page v119")

print("\n== another writer pushed first: ours goes on top, theirs survives ==")
git(dev, "pull", "-q", "--rebase", "origin", "main")
write(dev / "index.html", "page from a sibling\n")
git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "sibling"); git(dev, "push", "-q", "origin", "main")
write(job / "prices.json", "prices from job1, pass 2\n")
check("commit() returns True", G.commit(["prices.json"], "prices: pass 2", cwd=job))
check("our file is on main", remote_show("prices.json") == "prices from job1, pass 2")
check("the sibling's file is still on main", remote_show("index.html") == "page from a sibling")
check("our commit's parent is the sibling's",
      git(remote, "log", "-1", "--format=%s", "main~1") == "sibling")

print("\n== a PR cut from an old main is merged mid-run (the Oct 2026 hang) ==")
git(dev, "pull", "-q", "--rebase", "origin", "main")
git(dev, "checkout", "-q", "-b", "pr", old_base)
for i in range(30):
    write(dev / "index.html", f"pr change {i}\n"); git(dev, "commit", "-q", "-am", f"pr {i}")
git(dev, "checkout", "-q", "main")
git(dev, "merge", "-q", "--no-ff", "-X", "theirs", "pr", "-m", "Merge pull request #1")
git(dev, "push", "-q", "origin", "main")
before = int(git(job, "count-objects", "-v").split("in-pack: ")[1].split()[0])
write(job / "prices.json", "prices after the merge\n")
check("commit() returns True", G.commit(["prices.json"], "prices: after merge", cwd=job))
check("our file is on main", remote_show("prices.json") == "prices after the merge")
check("the merge is still on main", "Merge pull request #1" in git(remote, "log", "--format=%s", "-3", "main"))
after = int(git(job, "count-objects", "-v").split("in-pack: ")[1].split()[0])
local_commits = int(git(job, "rev-list", "--count", "HEAD"))
check(f"none of the PR's history was downloaded ({local_commits} commits reachable locally)",
      local_commits <= 3)
check(f"only a handful of objects fetched ({after - before})", after - before < 20)

print("\n== a full-tree (non-partial) shallow clone behaves the same ==")
full = root / "full"
git(root, "clone", "-q", "--depth", "1", url, str(full))
git(full, "config", "user.email", "job@x"); git(full, "config", "user.name", "job")
git(dev, "checkout", "-q", "-b", "pr2", old_base)
write(dev / "index.html", "pr2\n"); git(dev, "commit", "-q", "-am", "pr2")
git(dev, "checkout", "-q", "main"); git(dev, "pull", "-q", "--rebase", "origin", "main")
git(dev, "merge", "-q", "--no-ff", "-X", "theirs", "pr2", "-m", "Merge pull request #2")
git(dev, "push", "-q", "origin", "main")
write(full / "games.json", "games from the full clone\n")
check("commit() returns True", G.commit(["games.json"], "scrape: full", cwd=full))
check("our file is on main", remote_show("games.json") == "games from the full clone")
check("no PR history downloaded", int(git(full, "rev-list", "--count", "HEAD")) <= 3)

print("\n== a directory: rewritten shards and a deleted one ==")
write(job / "shots" / "shard_00.json", "new shard 0\n")
(job / "shots" / "shard_03.json").unlink()
write(job / "shots" / "shard_09.json", "brand new shard\n")
check("commit() returns True", G.commit(["shots"], "shots: rewrite", cwd=job))
check("rewritten shard on main", remote_show("shots/shard_00.json") == "new shard 0")
check("new shard on main", remote_show("shots/shard_09.json") == "brand new shard")
check("deleted shard gone from main", remote_show("shots/shard_03.json") == "")
check("untouched shard kept", remote_show("shots/shard_01.json").startswith("shard v"))

print("\n== nothing changed: no commit ==")
head = git(remote, "rev-parse", "main")
check("commit() returns True", G.commit(["prices.json"], "prices: same", cwd=job))
check("main did not move", git(remote, "rev-parse", "main") == head)

print("\n== a rejected push is retried ==")
hook = remote / "hooks" / "pre-receive"
flag = root / "rejected-once"
hook.write_text(f"#!/bin/sh\nif [ ! -e {flag} ]; then touch {flag}; echo busy >&2; exit 1; fi\nexit 0\n")
hook.chmod(0o755)
write(job / "prices.json", "prices after a rejection\n")
check("commit() returns True", G.commit(["prices.json"], "prices: retried", cwd=job))
check("first push was rejected", flag.exists())
check("our file is on main", remote_show("prices.json") == "prices after a rejection")
hook.unlink()

print("\n== giving up leaves the job's own file in place ==")
hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
write(job / "prices.json", "prices that cannot land\n")
check("commit() returns False", G.commit(["prices.json"], "prices: blocked", cwd=job, attempts=2) is False)
check("working tree still has our version", (job / "prices.json").read_text() == "prices that cannot land\n")
try:
    G.commit(["prices.json"], "prices: blocked", cwd=job, attempts=1, fail_loud=True)
    check("fail_loud exits", False)
except SystemExit as e:
    check("fail_loud exits 1", e.code == 1)
hook.unlink()
check("the next commit carries it", G.commit(["prices.json"], "prices: unblocked", cwd=job)
      and remote_show("prices.json") == "prices that cannot land")

print("\n== a path outside the sparse checkout is refused, loudly ==")
write(job / "index.html", "a job must not write this\n")
check("commit() returns False", G.commit(["index.html"], "bad", cwd=job, attempts=1) is False)
check("main's index.html untouched", remote_show("index.html") != "a job must not write this")

print("\n== dry runs ==")
head = git(remote, "rev-parse", "main")
branchy = job_clone("job-branch")
git(branchy, "checkout", "-q", "-b", "claude/fix")
write(branchy / "prices.json", "from a branch\n")
check("off main: returns True", G.commit(["prices.json"], "branch run", cwd=branchy))
os.environ["GITHUB_ACTIONS"] = "false"
write(job / "prices.json", "from a laptop\n")
check("outside Actions: returns True", G.commit(["prices.json"], "local run", cwd=job))
os.environ["GITHUB_ACTIONS"] = "true"
check("neither pushed anything", git(remote, "rev-parse", "main") == head)

print("\n== read_main ==")
check("reads the current main", G.read_main("prices.json", cwd=job) == "prices that cannot land\n")
check("a missing path -> None", G.read_main("nope.txt", cwd=job) is None)

print("\n== a wedged rebase from older code is cleared ==")
wedged = job_clone("job-wedged")
(wedged / ".git" / "rebase-merge").mkdir()
(wedged / ".git" / "index.lock").write_text("")
write(wedged / "games.json", "after a wedge\n")
check("commit() returns True", G.commit(["games.json"], "scrape: unwedged", cwd=wedged))
check("our file is on main", remote_show("games.json") == "after a wedge")

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
