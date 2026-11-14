#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keeps Claude off main/master.

Blocks (exit code 2, reason shown to Claude):
  * Edit/Write/MultiEdit/NotebookEdit on a file whose repo is on main or master
  * git commit, merge, rebase, cherry-pick, revert, reset, am, apply while on main or master
  * git push to main or master (explicit target, HEAD while on main, plain push while on main, --all, --mirror)
  * git branch -f/-d/-m main, git checkout -B main, git switch -C main, git update-ref on main
Asks you first (normal permission prompt):
  * gh pr merge, because the target branch can't be checked offline

Anything it can't parse is allowed, so a bug here never locks you out of normal work.
Standard library only. Works on macOS, Linux and Windows.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CHANGES_HISTORY = {"commit", "merge", "rebase", "cherry-pick", "revert", "reset", "am", "apply"}


def block(reason):
    sys.stderr.write(
        "BLOCKED by branch guard: " + reason
        + " Work on the repo's working branch and open a PR into main; the user merges.\n"
    )
    sys.exit(2)


def ask(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def branch_of(path):
    """Current branch of the repo containing path, or None (not a repo, detached HEAD, git missing)."""
    if not path:
        return None
    path = os.path.expanduser(path)
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    try:
        out = subprocess.run(
            ["git", "-C", path, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    name = out.stdout.strip()
    return name if out.returncode == 0 and name else None


def short_ref(ref):
    ref = ref.lstrip("+")
    for prefix in ("refs/heads/", "refs/remotes/origin/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    return ref


def check_push(args, current):
    if any(a in ("--all", "--mirror") for a in args):
        block("`git push --all/--mirror` would push main or master.")
    positional, skip_next = [], False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        positional.append(a)
    refspecs = positional[1:]  # first positional is the remote
    if not refspecs:
        if current in PROTECTED:
            block("`git push` from `%s` would update %s." % (current, current))
        return
    for spec in refspecs:
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        dst = short_ref(dst)
        if dst == "HEAD":
            dst = current or ""
        if dst in PROTECTED:
            block("pushing to `%s` is not allowed." % dst)


def check_git(tokens, workdir):
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "-C" and i + 1 < len(tokens):
            workdir = os.path.join(workdir, os.path.expanduser(tokens[i + 1]))
            i += 2
        elif t in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(tokens):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            break
    if i >= len(tokens):
        return
    sub, args = tokens[i], tokens[i + 1:]
    current = branch_of(workdir)

    if sub == "push":
        check_push(args, current)
    elif sub in CHANGES_HISTORY and current in PROTECTED:
        block("`git %s` while on `%s` is not allowed." % (sub, current))
    elif sub == "branch" and any(short_ref(a) in PROTECTED for a in args) and any(
        a in ("-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C") for a in args
    ):
        block("changing the `main`/`master` branch itself is not allowed.")
    elif sub in ("checkout", "switch") and any(a in ("-B", "-C", "--force-create") for a in args) and any(
        short_ref(a) in PROTECTED for a in args
    ):
        block("resetting `main`/`master` with checkout -B or switch -C is not allowed.")
    elif sub == "update-ref" and any(short_ref(a) in PROTECTED for a in args):
        block("`git update-ref` on main/master is not allowed.")


def check_bash(command, cwd):
    # Safety net for anything the parser below misses, e.g. bash -c "git push origin main"
    if re.search(r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*[\s:/+'\"](main|master)(?=$|[\s'\"])", command):
        block("pushing to main/master is not allowed.")

    workdir = cwd
    for segment in re.split(r"&&|\|\||;|\||\n", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]  # skip env assignments like FOO=bar git ...
        if not tokens:
            continue
        cmd = os.path.basename(tokens[0])
        if cmd == "cd" and len(tokens) > 1:
            workdir = os.path.join(workdir, os.path.expanduser(tokens[1]))
        elif cmd in ("git", "git.exe"):
            check_git(tokens, workdir)
        elif cmd in ("gh", "gh.exe") and tokens[1:3] == ["pr", "merge"]:
            ask("Branch guard: `gh pr merge` may promote work into main. Approve only if the target is not main or master.")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and not os.path.isabs(path):
            path = os.path.join(cwd, path)
        branch = branch_of(path)
        if branch in PROTECTED:
            block("editing files while the repo is on `%s` is not allowed." % branch)
    elif tool == "Bash":
        check_bash(tool_input.get("command", ""), cwd)
    sys.exit(0)


if __name__ == "__main__":
    main()
