#!/usr/bin/env python3
"""
gitsync.py — how every job puts its own files on main
=====================================================
Every data job commits the files it owns straight to main while a dozen others do the same.
They used to do it with `git fetch origin main` + `git rebase --autostash origin/main` + push.
That hung. Each job runs from a shallow (often partial) clone, and when a PR branch cut
from an older main was merged, the plain fetch went after that branch's whole history
(GBs here), or the rebase walked into it one lazily fetched object at a time. The job sat
there until its timeout killed it, so the pass it had just finished never landed. Price
runs died that way on 22, 23, 24 and 25 Oct 2026, and HLTB and coverage did too, each
within minutes of a PR merge (ARCHITECTURE §4, §16).

So every commit goes through here instead, and nothing in it walks history:

  1. copy the job's own paths aside (files, or whole directories with deletions),
  2. `git fetch --depth=1 origin main`: the tip only, whatever was merged into it,
  3. `git reset --hard FETCH_HEAD`: the tree as every other writer left it,
  4. put the job's own paths back exactly as it wrote them, `git add -A` them, commit,
  5. push; if another job pushed first, go back to 2.

Every git call has a timeout, so a stuck network call costs one attempt, not the run. One
writer per file is what makes 3-4 safe: nothing else on main ever changes these paths, so
"main's tree plus ours" loses nobody's work. A path outside a sparse checkout is refused by
`git add`, loudly, rather than dropped.

  commit(paths, msg)        -> True when pushed or nothing changed, False when it gave up
                               (fail_loud=True exits 1 instead: a red run, not a quiet one)
  read_main(path)           -> that file's text on the current main, or None
  python gitsync.py -m MSG PATH...    the same commit from a workflow step (exit 1 on failure)

Outside GitHub Actions (GITHUB_ACTIONS != "true") nothing is committed. On any branch but
main nothing is committed either: a run dispatched from a branch is a dry run, so a branch's
data can never reach main. Stdlib only.
"""

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BRANCH = "main"
ATTEMPTS = 8
# Per git call, seconds. A fetch of the tip and a reset that downloads a few changed data files
# take seconds; pics' ~40 MB push about 25 s. Anything near these limits is a stuck call.
TIMEOUT = {"fetch": 300, "reset": 600, "add": 300, "commit": 120, "push": 600, "other": 60}


def log(msg):
    print(msg, flush=True)


def _enabled():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _git(args, cwd, kind="other", check=True):
    """Run one git command with its timeout. Returns the CompletedProcess; raises
    CalledProcessError (check=True) or TimeoutExpired."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=TIMEOUT[kind], check=check)


def _clear_wedges(cwd):
    """Undo what a killed git call or an older job's half-finished rebase can leave behind."""
    gd = Path(cwd) / ".git"
    for lock in ("index.lock", "HEAD.lock", "shallow.lock"):
        (gd / lock).unlink(missing_ok=True)
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        _git(["rebase", "--abort"], cwd, check=False)


def _branch(cwd):
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _fetch_tip(cwd):
    """The current main, tip only. --depth=1 is the point: a plain fetch into a shallow clone
    downloads the full history of any branch merged since the clone."""
    _git(["fetch", "--depth=1", "--no-tags", "origin", BRANCH], cwd, "fetch")


def _stash(paths, cwd, into):
    """Copy each path that exists (file or directory) under `into`; the ones that don't exist
    are recorded too, so putting them back deletes them."""
    for p in paths:
        src = Path(cwd) / p
        dst = Path(into) / p
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _restore(paths, cwd, frm):
    """Make each path in the working tree exactly what the job wrote (stashed under `frm`)."""
    for p in paths:
        dst = Path(cwd) / p
        src = Path(frm) / p
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def commit(paths, msg, *, cwd=None, fail_loud=False, attempts=ATTEMPTS):
    """Commit `paths` (relative to the repo root) to main as one commit on top of the current
    tip, retrying when another job pushed first. See the module docstring."""
    cwd = str(cwd or Path.cwd())
    paths = [str(p).rstrip("/") for p in paths]
    if not _enabled():
        log(f"  (local run, not committing: {msg})")
        return True
    branch = _branch(cwd)
    if branch != BRANCH:
        log(f"  [dry run: checkout is {branch or 'detached'}, not {BRANCH}] would have committed: {msg}")
        return True
    stash = tempfile.mkdtemp(prefix="gitsync-")
    last_err = ""
    try:
        _stash(paths, cwd, stash)
        for attempt in range(1, attempts + 1):
            try:
                _clear_wedges(cwd)
                _fetch_tip(cwd)
                _git(["reset", "--hard", "FETCH_HEAD"], cwd, "reset")
                _restore(paths, cwd, stash)
                # A path that is neither on disk nor on main has nothing to stage, and naming it
                # would fail the whole add ("pathspec did not match").
                addable = [p for p in paths if (Path(cwd) / p).exists()
                           or _git(["ls-files", "--", p], cwd, check=False).stdout.strip()]
                if addable:
                    _git(["add", "-A", "--", *addable], cwd, "add")
                if _git(["diff", "--staged", "--quiet"], cwd, check=False).returncode == 0:
                    log(f"  (nothing changed vs main; no commit: {msg})")
                    return True
                _git(["commit", "-q", "-m", msg], cwd, "commit")
                push = _git(["push", "origin", f"HEAD:{BRANCH}"], cwd, "push", check=False)
                if push.returncode == 0:
                    log(f"  committed: {msg}")
                    return True
                last_err = (push.stderr or push.stdout or "").strip()
                log(f"  push attempt {attempt}/{attempts} rejected: {last_err[-200:]}")
            except subprocess.TimeoutExpired as e:
                last_err = f"git {' '.join(e.cmd[1:3])} timed out after {e.timeout}s"
                log(f"  git attempt {attempt}/{attempts}: {last_err}")
            except subprocess.CalledProcessError as e:
                last_err = ((e.stderr or "") + (e.stdout or "")).strip() or str(e)
                log(f"  git attempt {attempt}/{attempts} error: {last_err[-300:]}")
            except OSError as e:
                last_err = str(e)
                log(f"  git attempt {attempt}/{attempts} error: {last_err}")
            if attempt < attempts:
                time.sleep(2 * attempt + random.uniform(0, 2))
        log(f"  ERROR: could not push after {attempts} attempts ({msg}): {last_err[-300:]}")
        if fail_loud:
            sys.exit(1)
        return False
    finally:
        # The working tree keeps the job's own version of its paths whatever happened, so a
        # later commit (or the next checkpoint) still carries everything.
        try:
            _restore(paths, cwd, stash)
        except OSError:
            pass
        shutil.rmtree(stash, ignore_errors=True)


def read_main(path, *, cwd=None):
    """`path` as it is on the current main (tip only, no history), or None when it can't be
    read. Touches neither the working tree nor the index."""
    if not _enabled():
        return None
    cwd = str(cwd or Path.cwd())
    try:
        _fetch_tip(cwd)
        r = _git(["show", f"FETCH_HEAD:{path}"], cwd, "fetch", check=False)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError) as e:
        log(f"  could not read {path} from {BRANCH}: {e}")
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Commit this job's own paths to main (see gitsync.py).")
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args(argv)
    return 0 if commit(a.paths, a.message, fail_loud=True) else 1


if __name__ == "__main__":
    sys.exit(main())
