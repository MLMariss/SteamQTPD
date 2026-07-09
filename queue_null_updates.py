#!/usr/bin/env python3
"""
ONE-SHOT: queue every game with a null `last_update_ts` for a forced re-scrape.
=============================================================================
Context: the old scraper computed `last_update_ts` from the News API with a broken
`maxlength=1` fetch that only saw post titles, so any patch whose title dodged the
keyword list read as "no update". That left ~52k games falsely showing "no upd" on the
site. `scraper.py` is now fixed (maxlength=300, body-scanned), but stored records only
re-scrape when Steam's `last_modified` moves — so the already-stored false-nulls won't
self-correct until each game next gets patched.

This script forces the issue: it drops every stored game whose `last_update_ts` is null
into `catalog["force_refresh"]`, which `scraper.py`'s select_work() drains (jumping the
refresh queue, independent of last_modified) across its next runs. Because the scraper
checkpoints and commits periodically, the queue drains SAFELY across multiple runs — an
interrupted run loses no progress, and each game re-scraped drops out of the null set.

COST / SAFETY (read before running):
  * This is a FULL sweep: all ~52k null games. At STEAM_DELAY=1.5s that's ~21+ hours of
    storefront scraping, spread across however many scraper runs it takes to drain.
  * It draws on the shared ~200/5min storefront budget (prices/recent/playtime/updates
    also use it). WATCH 403 rates after the next scrape run; if they spike, the queue is
    self-limiting — just let it drain slower, or trim force_refresh to pause.
  * Many of these games are genuinely un-patched (25k+ have <10 reviews) and will
    re-scrape from null straight back to null. That's expected; the sweep is exhaustive
    by request, not surgical.

Idempotent: re-running only re-queues whatever is STILL null, and unions with any
existing force_refresh entries (no duplicates). Run once; the scraper does the rest.

Usage:
    python queue_null_updates.py            # dry-run: prints counts, writes nothing
    python queue_null_updates.py --commit   # writes catalog.json (and git-commits in CI)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"
CATALOG_FILE = HERE / "catalog.json"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def main():
    commit = "--commit" in sys.argv

    games = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    games = games.get("games") if isinstance(games, dict) else games
    catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))

    # --- Tiered queueing (added Jul 2026) -----------------------------------------
    # The original sweep queued ALL ~52k nulls at once. Per this script's own warning,
    # 25k+ of them have <10 reviews and re-scrape from null straight back to null —
    # they're genuinely un-patched and clog the front of the drain with dead work.
    #
    # So we now split by review_count:
    #   * HIGH tier (review_count >= MIN_REVIEWS): games likely to actually have patch
    #     history the fixed News-API fetch can now recover. Queued IN FULL, first.
    #   * LOW tier (< MIN_REVIEWS): still swept eventually (exhaustive by request), but
    #     only LOW_TRICKLE of them are added per run so they never starve the high tier.
    #     Re-run this script across days to drain the low tier gradually.
    # Set MIN_REVIEWS=0 to restore the old exhaustive-all-at-once behaviour.
    MIN_REVIEWS = int(os.environ.get("QNU_MIN_REVIEWS", "10"))
    LOW_TRICKLE = int(os.environ.get("QNU_LOW_TRICKLE", "3000"))

    def rc(r):
        try:
            return int(r.get("review_count") or 0)
        except (TypeError, ValueError):
            return 0

    # Every stored game with a null/absent last_update_ts. (select_work filters the queue
    # to `str(a) in processed`, i.e. stored games only, so pending/unreleased appids can't
    # sneak in even if listed — but we key off games.json records, so they won't be.)
    null_recs = [r for r in games
                 if r.get("appid") is not None and not r.get("last_update_ts")]

    high = sorted(int(r["appid"]) for r in null_recs if rc(r) >= MIN_REVIEWS)
    low_all = sorted(int(r["appid"]) for r in null_recs if rc(r) < MIN_REVIEWS)

    existing = set(int(a) for a in (catalog.get("force_refresh") or []))
    # Low tier: only add up to LOW_TRICKLE that aren't already queued this run.
    low_new = [a for a in low_all if a not in existing][:LOW_TRICKLE]
    to_add = set(high) | set(low_new)
    merged = sorted(existing | to_add)

    print(f"stored games with null last_update_ts : {len(null_recs)}")
    print(f"  high tier (>= {MIN_REVIEWS} reviews)      : {len(high)} (queued in full)")
    print(f"  low tier  (<  {MIN_REVIEWS} reviews)      : {len(low_all)} "
          f"(trickling {len(low_new)} this run, cap {LOW_TRICKLE})")
    print(f"force_refresh already queued          : {len(existing)}")
    print(f"force_refresh after union             : {len(merged)}")
    print(f"newly added this run                  : {len(merged) - len(existing)}")

    if not commit:
        print("\nDRY RUN — nothing written. Re-run with --commit to queue them.")
        return 0

    catalog["force_refresh"] = merged
    CATALOG_FILE.write_text(
        json.dumps(catalog, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"\nWROTE catalog.json — {len(merged)} appids queued for forced re-scrape.")

    if IN_ACTIONS:
        try:
            subprocess.run(["git", "add", "catalog.json"], check=True)
            if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
                subprocess.run(
                    ["git", "commit", "-m",
                     f"queue null-update re-scrape: +{len(merged) - len(existing)} "
                     f"(high {len(high)}, low +{len(low_new)}/{len(low_all)})"],
                    check=True)
                import random, time
                for attempt in range(1, 9):
                    subprocess.run(["git", "fetch", "origin", "main"], check=False)
                    subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=False)
                    if subprocess.run(["git", "push", "origin", "HEAD:main"],
                                      capture_output=True, text=True).returncode == 0:
                        print("  committed + pushed catalog.json")
                        break
                    time.sleep(2 * attempt + random.uniform(0, 2))
        except subprocess.CalledProcessError as e:
            print(f"  git step failed: {e}")
            return 1
    else:
        print("  (local run — catalog.json written; commit/push it yourself)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
