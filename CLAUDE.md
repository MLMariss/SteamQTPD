# Global working preferences
Apply to every project unless the project's CLAUDE.md says otherwise.

# Branches
* Never edit, commit or push on `main` or `master`. I merge into `main` myself on GitHub.
* For each new task, create a fresh branch from the latest origin/main, named after the change (for example fix-date-compare). Keep working on that branch until its PR is merged.
* Don't delete branches.
* If you find yourself on `main` or `master`, stop and tell me.
* In Claude Code on the web, the session's own branch is the task branch.

# How to work
* Read the relevant files before answering or changing anything. Don't guess about code you haven't opened.
* Write a general solution, not one that only passes the tests. Don't hard-code values to make a test pass. If a test looks wrong, tell me instead of working around it.
* Keep changes to what the task needs: no refactoring unrelated code, extra features, comments on untouched code, or error handling for cases that can't happen. Small diffs are easier for me to review.
* When you say something works, name the command you ran and what it returned.

# When stuck or unsure
* If the same approach fails twice, stop. Tell me what you tried, what the errors said, and your best guess at the cause, then ask how to continue.
* When a choice would change what you build and the code doesn't answer it, use the AskUserQuestion tool with 2 to 4 options, your recommendation first.
* Ask before anything hard to undo: deleting files, force pushing, rewriting git history, changing shared settings, or skipping checks like --no-verify.

# Finishing a task
* When a coding task is done, commit, then end with AskUserQuestion offering the next steps that fit, your recommendation first. Usual options:
  * Push the task branch and open or update the PR into `main`, then give me the PR link
  * Review the diff with a subagent in a fresh context first (fix only issues that affect correctness or the requirements)
  * Keep the commit local for now
  * Stop here
* Push once per finished task, only after I pick it. Never push mid-task.
* Never merge a PR. Merging is my step.
* Leave out options that don't apply. Don't ask after plain questions or explanations.
* If AskUserQuestion isn't available, give the same options as a short numbered list so I can answer with a number.

# Context
* When I switch to an unrelated topic, suggest /clear.
* When compacting, keep the list of modified files and the test commands.

# This project
SteamQTPD. These facts extend the rules above; where they differ, these win.

## Deploy
- Remote: `https://github.com/MLMariss/SteamQTPD`
- `main` is **live** — GitHub Pages deploys from it. Never commit or push to it.
- The scraper workflows commit their data to `main` continuously (`games.json`, `prices.json`,
  `hltb.json`, `tags.json`, ...), so `main` moves on its own all day. Fetch before you cut a task
  branch, and expect the branch to fall hundreds of commits behind while you work.
- Falling behind is normal and merges cleanly: app-code edits (`index.html`, `ARCHITECTURE.md`)
  touch files disjoint from the scraper data JSON. Verify with
  `git log --oneline HEAD..origin/main -- index.html ARCHITECTURE.md` (empty => clean).
- `test` is a stale July 2026 ancestor of `main` and is not a working branch. Nothing is promoted
  `test` -> `main`; task branches are cut from `origin/main` and merged by PR.
- Don't delete branches here: public repo, the history is intentionally on display.

## GitHub tooling
- `gh` is installed on the local machine (v2.96.0, authed as `MLMariss`, scopes
  `gist read:org repo workflow`). Read-only `gh` is expected — `gh pr view/list/diff/status/checks`,
  `gh repo view`, `gh run list/view` — and is how you get the real PR URL and CI state.
  `gh pr create` is usable; **`gh pr merge` is denied.**
- Claude Code on the web has **no** `gh` (`command not found`, verified 2026-09-16). Use the GitHub
  MCP tools there (`mcp__github__*`, load via ToolSearch): `list_pull_requests`, `pull_request_read`,
  `actions_list` / `get_check_run`, `create_pull_request`. Same rules carry over — read freely,
  **never** `merge_pull_request`. Don't report GitHub as unreachable in a web session just because
  `gh` is missing.
- Before claiming a PR exists, check:
  `gh pr list --repo MLMariss/SteamQTPD --head <branch> --json number,url,state`. An open PR means
  the push already updated it; merged/closed means a new one is needed. Compare-link fallback, as a
  markdown link with `title`/`body` URL-encoded:
  `https://github.com/MLMariss/SteamQTPD/compare/main...<branch>?expand=1&title=<t>&body=<b>`

## Product rules (non-negotiable)

- **Never highlight or feature adult content. Ever.** Anywhere the site puts games in front of
  someone who did not ask for something specific — preset shelves, the **default landing view**
  (`ADULT_DEFAULT = "hide"` in `index.html`, since 2026-09-23), any future "recommended" or
  "featured" surface, `Lucky` if it is ever promoted to the landing view — the surface sets
  `adult=hide`. `presets.py` enforces that at build time and fails the job if a shelf omits it;
  keep that guard, and extend it to any new surface of this kind.
  **A typed search is the opposite case** — the user asked for something specific, so a
  non-blank search lifts `hide` (`adultMode()` in `index.html`; owner, 2026-09-23: "if users
  looks for the thing - we show the thing"). Never let a shelf or other unrequested surface
  carry a `q=` for that reason. The same search exemption lifts the **default Min reviews floor**
  (100+) for a title search and for Find similar (`revFloorOn()`); Find similar does **not** lift
  `adult=hide` — its results are a broad list the user did not name.
  **The storefront's adult flag is the definition, and it is the only lock.** Shelves used to
  carry a second one — an `exc=` list naming every tag in `ADULT_TAGS` — because `isAdult()`
  treats the PICS flag as authoritative for any game PICS has covered, so for those games the
  tag test never runs and a PICS-covered game tagged "Nudity" with an unset flag passes
  `adult=hide` on its own. That second lock was removed deliberately (2026-09-16, at the
  owner's instruction): "Nudity" and "Mature" sit on plenty of games whose adult content is
  incidental, and excluding the tag by name threw out the game rather than the scene. Dropping
  it returned ~1,100 games to the shelves (seven at the time; there are **ten** now — *Half
  off or better* was added the same day, *No AI, no strings* and *Try before you buy* on 2026-11-02). Do not re-add it without being asked.
  **Known residuals**, to be stated rather than implied away: a PICS-covered game whose adult
  flag is unset but whose tags say otherwise now passes; so does a game whose only adult signal
  is its title — no flag, no tag, innocuous SteamSpy tags. Neither is identifiable from the data
  we hold. Say so rather than implying the filtering is complete.
  The owner reaffirmed this on 2026-09-23 when review-based Length put an unflagged game tagged
  *Sexual Content / Hentai* at the top of two niche shelves: **no tag-based pushing down** — "we
  are not making decisions for others". The flag stays the only lock.
  **Steam by the numbers** (`stats.html`, owner 2026-10-26: "counts all, rankings hide"): totals —
  release counts, prices, EA, AI, Deck, reception — count every game; anything that ranks or names
  games (tag and genre rankings, biggest releases) is built over games that pass `adult=hide`, and
  `stats.py` fails the job if a named list ever holds an adult game.
- This rule is not a default to weigh against other goals. It outranks result counts, shelf
  breadth, and "the metric says it is good value".
