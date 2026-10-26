#!/usr/bin/env python3
"""Static checks over .github/workflows/: things that have broken a job silently before.

  * A sparse checkout must list every local module its scripts import, including ones
    imported inside a function: hltb_refresh.py imports hltb_selfcheck inside main(), the
    list missed it, and 2.5 failed on ModuleNotFoundError for a day (ARCHITECTURE §4). Every
    writer now imports gitsync.py, so one forgotten line would stop that job's commits.
  * Nothing commits by rebasing onto a plain fetch any more. In a shallow clone that fetch
    downloads the whole history of any old-based PR merged since the clone, and price, HLTB
    and coverage runs hung on it until their timeouts (Oct 2026; gitsync.py).
No network, no YAML library: the workflow files are read as text."""
import ast, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
WF = HERE / ".github" / "workflows"
RETIRED = {"hltb_igdb.py", "igdb_wipe.py", "igdb_probe.py", "igdb_probe2.py"}   # no workflow runs them
fails = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


def local_imports(script, seen=None):
    """Every repo-root module `script` imports, at any depth, transitively."""
    seen = set() if seen is None else seen
    path = HERE / script
    if script in seen or not path.exists():
        return seen
    seen.add(script)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module.split(".")[0]]
        for n in names:
            if (HERE / f"{n}.py").exists():
                local_imports(f"{n}.py", seen)
    return seen


def sparse_list(text):
    """The paths under each `sparse-checkout: |` block, or None when the checkout is full."""
    out, inside, indent = [], False, None
    for line in text.splitlines():
        if re.match(r"\s*sparse-checkout:\s*\|", line):
            inside, indent = True, None
            continue
        if inside:
            if not line.strip():
                continue
            ind = len(line) - len(line.lstrip())
            if indent is None:
                indent = ind
            if ind < indent or not line.strip().startswith("/"):
                inside = False
                continue
            out.append(line.strip())
    return out if out else None


def covered(path, listed):
    """True when a sparse list entry checks `path` out (an exact file or a directory prefix)."""
    return any(path == e.lstrip("/") or (e.endswith("/") and path.startswith(e.lstrip("/")))
               for e in listed)


print("\n== sparse checkouts list every local module their scripts import ==")
for wf in sorted(WF.glob("*.yml")):
    text = wf.read_text(encoding="utf-8")
    scripts = sorted(set(re.findall(r"\bpython3?\s+(?:-\S+\s+)*([A-Za-z0-9_]+\.py)", text)))
    listed = sparse_list(text)
    if listed is None or not scripts:
        continue
    need = set()
    for s in scripts:
        need |= local_imports(s)
    missing = sorted(m for m in need if not covered(m, listed))
    check(f"{wf.name}: {', '.join(sorted(need))}" + (f" — MISSING {missing}" if missing else ""),
          not missing)

print("\n== no commit path rebases onto a plain fetch ==")
for wf in sorted(WF.glob("*.yml")):
    text = wf.read_text(encoding="utf-8")
    bad = [l.strip() for l in text.splitlines()
           if not l.strip().startswith("#") and re.search(r"git (rebase|pull)\b|git fetch origin main", l)]
    check(f"{wf.name}: no git rebase / pull / plain fetch in its steps" + (f" — {bad}" if bad else ""), not bad)
for py in sorted(HERE.glob("*.py")):
    if py.name in RETIRED or py.name.startswith("test_"):
        continue
    src = py.read_text(encoding="utf-8")
    bad = re.findall(r'\[\s*"git",\s*"(rebase|pull|push|fetch)"', src)
    if py.name == "gitsync.py":
        continue
    check(f"{py.name}: commits only through gitsync" + (f" — runs git {sorted(set(bad))}" if bad else ""),
          not bad)

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)
