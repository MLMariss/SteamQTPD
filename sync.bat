@echo off
REM ============================================================
REM  sync.bat - get this machine up to date when you switch PCs
REM
REM  What it does (READ-ONLY toward the remote - never pushes):
REM    1. Fetches all branches and prunes deleted ones
REM    2. Switches to main
REM    3. Fast-forward pulls the latest main from GitHub
REM    4. Shows your branch list and status so you can see where
REM       you are before you start working.
REM
REM  It will NOT push, merge, or delete anything. Safe to run
REM  any time, on any machine, before you start work.
REM ============================================================

cd /d "%~dp0"

echo.
echo   Syncing this machine with GitHub...
echo   -----------------------------------

REM Stop if there are uncommitted changes, so we never clobber work.
git diff --quiet
if errorlevel 1 goto DIRTY
git diff --cached --quiet
if errorlevel 1 goto DIRTY

git fetch --all --prune
git checkout main
git pull --ff-only origin main

echo.
echo   Branches:
git branch -a
echo.
echo   Status:
git status -sb
echo.
echo   Done. You are on up-to-date main. Create or switch to a
echo   working branch before making changes, e.g.:  git checkout -b test
echo.
pause
goto END

:DIRTY
echo.
echo   *** You have uncommitted changes in this folder. ***
echo   Commit or stash them first, then run sync again.
echo   Nothing was changed.
echo.
git status -sb
echo.
pause

:END
