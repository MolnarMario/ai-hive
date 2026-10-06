# Shipping finished lanes

Every laned agent ends its finished work with a commit whose message has a
last line of just `Task done`. AI Hive reads every lane, sees that commit
(`lanes.DONE_GREP`), puts a check mark on the lane chip, turns it green and
logs it. That is all it does. It never tells the integrator.

The user decides when there is enough finished work for a pull request and
asks the integrator to ship it. The integrator starts only then, never on
its own and never because a lane got flagged, and does everything else in
its own lane by the checklist below.

Lanes never bump the version or touch the CHANGELOG or the README check
count. The integrator does, once per pull request.

## Testing policy

- A lane agent commits each finished task on its lane branch and runs the
  tests for the area it changed (`-k <area>` or `--quick` here). It never
  needs the full suite.
- The integrator runs the full suite, e2e included, once per pull request,
  on the commit it pushes.
- After that run only the README count may change, with no rerun. Any
  other change means a new commit, a new full run and a new GPT-6-Luna
  review.

Test command: `.venv\Scripts\python.exe tests\smoke_test.py`

## Checklist

0. If your own `integrate/` pull request is still open when the user asks
   for more, don't start a new one: merge the lanes they asked for into
   that branch (step 2), keep its one version bump, and carry on from
   step 4. Two open integration PRs would fight over the version and the
   CHANGELOG. A lane flagged while your PR is open waits for the user too.
1. `git fetch origin`. Ship the lanes the user named. If they named none,
   take every lane with finished work that main lacks, including work left
   over from earlier:
   ```
   git for-each-ref --format="%(refname:short)" refs/heads/hive/
   git log -1 --format=%H -i -E --grep="^[[:space:]]*task done[.!]?[[:space:]]*$" origin/main..<branch>
   ```
   Take each lane's newest flagged commit, not its head: commits after it
   are unfinished work.
2. In your own lane, branch from main: `git switch -c integrate/<yyyy-mm-dd>-<short-name> origin/main`.
   Merge each flagged commit with `git merge --no-ff <sha>` and resolve every
   conflict. Never rebase, never squash, never force-push.
3. Bump `__version__` in `app/__init__.py` and add the matching `## x.y.z`
   section at the top of `CHANGELOG.md`, written for users, no em dash.
4. Run `/code-review` on the branch and fix what it finds. Commit.
5. Run the FULL suite, including the real-claude e2e test, on that commit.
   Don't shorten its timeouts. A failing check is never acceptable on your
   say-so: fix it, or run it on `origin/main` and, if it fails there too,
   say so in the PR body.
6. Set the README check count from the suite's RESULT line and commit it.
7. Push with `git push -u origin <branch>` and open the pull request with
   `gh pr create --base main --head <branch>`. The body lists the lanes and
   commits it ships, `Tested-commit: <full sha the suite ran on>` and the
   suite's `RESULT` line.
8. Get the review from GPT-6-Luna at high effort:
   `codex review --base origin/main -c model=gpt-6-luna -c model_reasoning_effort=high -c sandbox_mode=read-only`.
   Fix every finding that holds up, rerun the suite, push, update the PR
   body (each finding and what you did about it) and run the review again.
   Repeat until Luna has nothing left that you accept as a real problem.
   If Codex is missing or fails, stop and tell the user. Never merge
   without a clean review.
9. Merge only when all of these hold: the last Luna review is clean, the
   suite passed on the PR head (or the head adds only README.md on top of
   the tested commit), `gh pr view` says MERGEABLE, and `origin/main` has
   not moved since you tested. If main moved, merge it in and go back to
   step 5. Then:
   `gh pr merge <number> --merge --match-head-commit <head sha>`.
   If a permission check refuses the merge, stop and tell the user. Never
   retry it in another form to get past the check.
10. Bring the main checkout up to date so the user's AI Hive runs what
    merged: `git -C <main checkout> switch main` (only if it sits on a
    branch whose commits are all on main) and
    `git -C <main checkout> pull --ff-only`. Skip it and tell the user if
    the main checkout has uncommitted changes or commits main lacks. Never
    edit files there.
11. Report in a few plain sentences: which lanes shipped, the version, the
    PR link, and anything you skipped or couldn't fix.

## Setting up the integrator

- The integrator runs git through both of Claude Code's shells. Allow
  `Bash(git:*)` and `PowerShell(git:*)` in its permissions, or every git call
  waits for a click.
- Claude Code keeps its auto-memory per main checkout, so every lane of a
  repository, the integrator's included, shares one memory folder.
