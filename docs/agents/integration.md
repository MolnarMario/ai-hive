# Integrating a lane

AI Hive's integration queue (`app/integration.py`, agent lanes Phase 3) sends
the workspace's integrator agent one brief per lane the user submits. The
brief embeds the **Checklist** section below and the **Test command** line,
read from the base branch, with these placeholders filled in: `{integrate}`
(the branch to create, one per submitted item), `{sha}` (the pinned commit),
`{base_ref}` (`origin/main`), `{base}` (`main`) and `{test_command}`.

Lanes never bump the version or touch the CHANGELOG or the README check
count. The integrator does, once per pull request.

Test command: `.venv\Scripts\python.exe tests\smoke_test.py`

## Checklist

1. In your own lane, create the branch from the pinned commit:
   `git switch -c {integrate} {sha}`. Work only on that branch.
2. `git fetch origin`, then merge {base_ref} into it and resolve every
   conflict. Never rebase and never force-push.
3. Bump `__version__` in `app/__init__.py` and add the matching `## x.y.z`
   section at the top of `CHANGELOG.md` (written for users, no em dash).
4. Run `/code-review` on the branch and fix what it finds.
5. Commit.
6. Run the FULL suite, including the real-claude e2e test, on that commit:
   `{test_command}`. Don't shorten its timeouts.
7. Set the README check count from the suite's RESULT line and commit it.
   That is the only change allowed after step 6: if anything else changes,
   commit it and run step 6 again.
8. Push with `git push -u origin {integrate}` and open the pull request with
   `gh pr create --base {base} --head {integrate}`. The PR body has the line
   `Tested-commit: <full sha of the commit step 6 ran on>` and the suite's
   `RESULT` line. List every failing check, and run it on {base_ref} to say
   whether it fails there too. Never decide yourself that a failure is
   acceptable.
9. Never run `gh pr merge`. Stop and report: the user approves the merge in
   AI Hive, and AI Hive merges only the commit you tested.

## What AI Hive checks, and what it can't

- When the integrator's turn ends, AI Hive reads the pull request from the
  item's own integrate branch. It waits for approval only when the PR comes
  from that branch (not a fork), targets the item's base, contains the
  pinned commit, and its head is the `Tested-commit` or differs from it only
  in `README.md`. Anything else needs the user, with the reason shown.
- `Tested-commit` is the integrator's claim. AI Hive can't prove that the
  suite ran on that commit. The `RESULT` line next to it is there for the
  user, who approves.
- Approve merge fetches the base and asks GitHub where it is
  (`gh api repos/{owner}/{repo}/branches/<base>`). If either fails, or the
  two disagree, nothing is merged. A short window remains between that check
  and the merge, in which someone can still push to the base. GitHub's branch
  protection setting "Require branches to be up to date before merging"
  closes it. `gh pr merge --match-head-commit` already pins the PR head.
- A pull request merged on github.com, not with Approve merge, is never
  counted as merged by itself. The item waits for the user's **Mark merged**,
  which checks with GitHub first.

## Setting up the integrator

- The integrator runs git through both of Claude Code's shells. Allow
  `Bash(git:*)` and `PowerShell(git:*)` in its permissions, or every git call
  waits for a click.
- Claude Code keeps its auto-memory per main checkout, so every lane of a
  repository, the integrator's included, shares one memory folder.
