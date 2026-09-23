# SteamQTPD — working instructions

## Repo
- Remote: `https://github.com/MLMariss/SteamQTPD`
- `main` is **live** (GitHub Pages deploys from it). Scraper workflows commit data to `main` continuously.
- The global CLAUDE.md rule "work only on `test`" does **not** apply here — `test` is a stale ancestor of `main`.
- `gh` CLI **is installed on the local machine** (v2.96.0, authed as `MLMariss`, scopes `gist read:org repo workflow`) — verified 2026-07-23.
  - **Read-only `gh` is allowed and expected**: `gh pr view/list/diff/status/checks`, `gh repo view`, `gh run list/view`. Use it to fetch the **real** PR URL and CI state.
  - **`gh pr merge` is denied.** Never merge.
  - **`gh pr create` is usable** — it needs the branch on the remote, and since Claude now does the push (see handover below), that precondition is met. Prefer it over hand-building a compare link.
  - **Remote sessions (Claude Code on the web) have NO `gh`** — `gh: command not found`, verified 2026-09-16. Use the **GitHub MCP tools** (`mcp__github__*`, load via ToolSearch) for the same reads and for opening a PR: `list_pull_requests`, `pull_request_read`, `actions_list` / `get_check_run`, `create_pull_request`. Every rule above carries over unchanged — read freely, **never** `merge_pull_request`. Do not report GitHub as unreachable in a remote session just because `gh` is missing; reach for the MCP tools instead.

## Git handover — MANDATORY

**The user's single control point is the MERGE.** Everything up to and including the push is
Claude's job; the user reviews the PR, merges it, and pulls. Nothing else should require them.

### The rule: ASK ONCE, THEN PUSH

- **Claude MAY run `git push`** — but **only after asking and getting a clear yes.** It is set
  to `ask` in `~/.claude/settings.json`, so the harness prompts as well; that prompt is a
  backstop, **not** a substitute for asking in conversation first.
- **Ask once per finished task, not per commit.** The failure mode this rule exists to prevent
  is a stream of small pushes cluttering the branch. Finish the whole task, commit (one commit,
  or a few genuinely-separable ones), *then* ask: **"task is done — push?"**
- **Never push mid-task** "just to save progress." Local commits already do that.
- **Never push to `main`.** Feature branch only.
- **Never merge a PR.** `gh pr merge` is denied and stays denied — that is the user's step.
- If the user says "commit and push" up front, that IS the approval — push when the task is
  done, and say so. Don't re-ask.

### After pushing, always deliver the PR

1. **Check for an existing PR first:**
   `gh pr list --repo MLMariss/SteamQTPD --head <branch> --json number,url,state`
   - If an **open** PR exists, the push already updated it → give that real `/pull/<n>` URL and
     report CI via `gh pr checks <n>`.
   - If the only PRs are **merged/closed**, a new one is needed.
2. **Create it** with `gh pr create` (permission is `ask`, so the user confirms), or hand over
   the pre-filled compare link if creation is declined:
   `https://github.com/MLMariss/SteamQTPD/compare/main...<branch>?expand=1&title=<url-encoded-title>&body=<url-encoded-body>`
   URL-encode `title`/`body` (spaces `%20`, newlines `%0A`, `#` `%23`, `&` `%26`); present as a
   markdown link, never bare.
3. **End the turn with the clickable PR URL.** That is the deliverable — never end with just
   "pushed." The user should be able to click through, review, and merge without typing a
   command.

Title + description as copy-paste blocks too, in case a pre-fill truncates.

## Product rules (non-negotiable)

- **Never highlight or feature adult content. Ever.** Anywhere the site puts games in front of
  someone who did not ask for something specific — preset shelves, the **default landing view**
  (`ADULT_DEFAULT = "hide"` in `index.html`, since 2026-09-23), any future "recommended" or
  "featured" surface, `Lucky` if it is ever promoted to the landing view — the surface sets
  `adult=hide`. `presets.py` enforces that at build time and fails the job if a shelf omits it;
  keep that guard, and extend it to any new surface of this kind.
  **The storefront's adult flag is the definition, and it is the only lock.** Shelves used to
  carry a second one — an `exc=` list naming every tag in `ADULT_TAGS` — because `isAdult()`
  treats the PICS flag as authoritative for any game PICS has covered, so for those games the
  tag test never runs and a PICS-covered game tagged "Nudity" with an unset flag passes
  `adult=hide` on its own. That second lock was removed deliberately (2026-09-16, at the
  owner's instruction): "Nudity" and "Mature" sit on plenty of games whose adult content is
  incidental, and excluding the tag by name threw out the game rather than the scene. Dropping
  it returned ~1,100 games to the shelves (seven at the time; there are **eight** now — *Half
  off or better* was added the same day). Do not re-add it without being asked.
  **Known residuals**, to be stated rather than implied away: a PICS-covered game whose adult
  flag is unset but whose tags say otherwise now passes; so does a game whose only adult signal
  is its title — no flag, no tag, innocuous SteamSpy tags. Neither is identifiable from the data
  we hold. Say so rather than implying the filtering is complete.
  The owner reaffirmed this on 2026-09-23 when review-based Length put an unflagged game tagged
  *Sexual Content / Hentai* at the top of two niche shelves: **no tag-based pushing down** — "we
  are not making decisions for others". The flag stays the only lock.
- This rule is not a default to weigh against other goals. It outranks result counts, shelf
  breadth, and "the metric says it is good value".

## Branches
- **One working branch at a time.** Do not spin a new branch per change; commit onto the branch already checked out. If it's unclear which is the working branch, ask — don't invent one.
- **Never delete branches.** This is a public repo and the history is intentionally on display.
- Being hundreds of commits behind `main` is normal and merges cleanly — app-code edits (`index.html`, `ARCHITECTURE.md`) touch files disjoint from the scraper data JSON. Verify with:
  `git log --oneline HEAD..origin/main -- index.html ARCHITECTURE.md` (empty ⇒ clean).
