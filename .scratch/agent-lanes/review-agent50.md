# Review: agent-lanes spec-v2 comments and PRs #35, #36, #37, #38

Reviewer: Agent 50, 2026-10-03. Read-only review, no code changed.

Scope: the `## Comments` section of `spec-v2.md` (Phase 0, 1 and 2 notes in
this checkout, plus the Phase 3 notes that exist only in the
`..\ai-hive-lanes3` copy), and the code in PR #35 (`f747c80`), #36
(`344a96b`), #37 (`f7ea8e8`) and #38 (`277cd2e`). Line numbers below are on
the #38 tip (`..\ai-hive-lanes3`), which contains all of the lane code.

How: I read the diffs. I reproduced three claims in a scratch repo (git
2.51.2): the junction finding, the `--track` push hint, and ignored-file
deletion on `worktree remove`. I did NOT run the smoke suite or a live
Claude. Findings marked "verified" were reproduced. The rest come from
reading the code.

This file is a separate review file and not appended to `## Comments` on
purpose. See "Process" below for the reason.

---

## Summary

The stack is careful work. The dangerous parts are handled well: the
junction unlink, never using `--force`/`-D`, the per-repo serial queue with a
folder lock, collisions that fail instead of reusing, the lane path that
never changes, the user-only merge with `--match-head-commit`, and audit
lines everywhere. The tests are real (real temp repos, mutation-checked
guards).

Before merging I'd fix three things. All three fail silently, which is the
failure class CLAUDE.md exists to prevent:

| # | PR | Finding | Severity |
|---|---|---|---|
| 1 | #38 | A lane submitted a second time can be marked **merged** because of its *previous* PR | High |
| 2 | #36 | `--track origin/main` makes git itself suggest `git push origin HEAD:main` inside a lane (verified) | High |
| 3 | #38 | `tested_sha` is not the commit the suite ran on, and NEEDS_YOU items are re-approved automatically on any integrator turn end | High |

Everything else is medium or low and can follow.

---

## High

### 1. #38: a resubmitted lane can be marked merged by its earlier PR

**Where:** `app/integration.py:194` (`integrate_branch`), `:490-503`
(`read_outcome`), `tests/smoke/lanes.py:1689` (`_FakeGh.prs`).

**What:** the integrate branch is per *lane* (`integrate/<lane tail>`), not
per *item*. A lane lives until its card closes and usually does several
tasks. After item 1 merges, the user submits the same lane again and gets
item 2 on the same `integrate/agent-5-3fa9c1`. Then:

- Checklist step 1 (`git switch -c {integrate} {sha}`) fails, because the
  integrator's lane still has that local branch from item 1.
- If the integrator ends a turn before opening the new PR (a question, a
  permission prompt, a failing test), `read_outcome` runs
  `gh pr view integrate/agent-5-3fa9c1`. The only PR on that head is
  item 1's MERGED one, so gh returns it. Line 499 then returns `MERGED`
  **without checking that the PR contains `item.sha`**. Item 2 shows as
  merged, and its commits never landed.
- The test double can't show this: `_FakeGh.prs` is keyed by branch, so it
  holds only one PR per head branch.

**Fix:**
- Make the branch per item: `integrate/<lane tail>-<item.id[:6]>`.
- In the MERGED path of both `read_outcome` and `approve_merge`, require
  `_is_ancestor(item.sha, pr.head)` before accepting MERGED. Otherwise
  return NEEDS_YOU.
- Let `_FakeGh` hold a list of PRs per branch, newest first, and add a
  "submit, merge, submit again, end turn with no new PR" check.

### 2. #36: `--track` upstream makes git suggest pushing to main (verified)

**Where:** `app/lanes.py:429` and `:467` (`worktree add --track -b <branch>
<root> origin/main`).

**What:** in a scratch lane made exactly this way, with one commit:

```
$ git status
Your branch is ahead of 'origin/main' by 1 commit.
  (use "git push" to publish your local commits)
$ git push
fatal: The upstream branch of your current branch does not match
the name of your current branch.  To push to the upstream branch
on the remote, use

    git push origin HEAD:main
```

An agent following git's own hints pushes its lane straight to `main`. That
skips the integrator, the suite and the approval, which is the whole point
of Phase 3. The lane prompt says "never commit to main", and a push is not
a commit. Agent of Darkness flagged the refused push but not this hint.

**Fix:** don't set an upstream on lane branches. The only reason for
`--track` was letting `branch -d` accept a lane that is merged into the
base. `remove_lane` already checks `merge-base --is-ancestor` itself, so
delete with a compare-and-delete instead: `git update-ref -d
refs/heads/<branch> <verified head sha>`. It is atomic and refuses if the
ref moved, so it is as safe as `-d` and still never `-D`. Also add one
line to the lane prompt: "Never push your lane branch to <base>; the
integrator opens the pull request."

### 3. #38: "tested" is not what the suite tested, and the approval resets itself

**Where:** `docs/agents/integration.md` (checklist order),
`app/widgets/main_window.py:4404-4415` (`_on_integrator_turn_ended`),
`app/integration.py:509` (`AWAITING` records `pr.head` as `tested_sha`).

**What:**

- The checklist runs the suite in step 3, then commits `/code-review` fixes
  in step 4 (rerun only "if you changed anything") and the version,
  CHANGELOG and README count in step 5. So the PR head is never the commit
  the suite ran on. The CHANGELOG-equals-`__version__` smoke check, which
  exists to catch a bad step 5, never runs on the final head.
- Any integrator turn end rechecks a head item in INTEGRATING **or
  NEEDS_YOU** and records the *current* PR head as `tested_sha`. So when
  Approve refuses with "PR changed after it was tested" (NEEDS_YOU), the
  next unrelated integrator turn moves the item back to AWAITING with the
  untested head as "tested", without any user click. The spec note says
  Recheck adopting the head is "the user's explicit call", but this path
  has no user in it.

**Fix:**
- Reorder the checklist: merge base, then version/CHANGELOG, then
  code-review fixes, then the FULL suite last. Only the README count may
  change after the suite.
- Have the integrator put a machine-readable `Tested-commit: <sha>` line in
  the PR body. `read_outcome` accepts AWAITING only when `pr.head` equals
  that sha, or differs from it only in `README.md`.
- Auto-recheck on turn end only for INTEGRATING items and for the "No pull
  request yet" NEEDS_YOU case. Every other NEEDS_YOU waits for a user
  Recheck. A `reason` field on the item is simpler than matching on `note`
  text.

---

## Medium

### 4. #38: Approve merges against a stale base when the fetch fails

`app/integration.py:547`: `lanes.fetch_base(repo, base)` returns False when
offline or on an auth failure, and the return value is ignored. The "base
moved since the test run" check then compares against a stale local
`origin/main` and passes, and `gh pr merge --merge` merges anyway (GitHub
doesn't require an up-to-date head unless branch protection does). Fix:
refuse (NEEDS_YOU, "could not fetch the base") when the fetch fails, or read
the base head from GitHub (`gh api repos/{owner}/{repo}/branches/<base>
--jq .commit.sha`). Even then, a small window remains between the check and
the merge. Say so in `docs/agents/integration.md`, and suggest "Require
branches to be up to date" protection if the user wants it closed
completely.

### 5. #36: a workspace in an untracked subfolder gets a lane with no folder to start in

`plan_lane` maps the workspace subfolder into the lane (`_mapped_cwd`), and
`_on_lane_created` (`main_window.py:3907`) sets that as the cwd without
checking that it exists. If the subfolder is untracked or ignored, the
worktree doesn't contain it, so the agent starts in a missing folder.
`find_repo_root` also walks up to ANY ancestor `.git`. A project folder
under a home-directory dotfiles repo would get a lane of the whole home
repo, made at `C:\Users\<user>.lanes\...`, and the checkbox is ticked by
default. Fix: after `create_lane`, if `plan.cwd` is not a directory, remove
the fresh lane (it is clean) and fail with `LANE-FAIL location`. In the
dialog, offer lanes only when the workspace folder is the repo root or a
tracked subfolder (`git ls-files -z -- <rel> | head -1`, which can run when
the switch is on).

### 6. #36: retire removes the worktree even when the wait timed out

`app/lanes.py:596`: `wait_for_exit` returns nothing, and `retire_lane` goes
on to `remove_lane` after 10 s whether or not the processes exited. The
Phase 1 note says this is exactly the case to avoid ("a half-deleted
worktree can no longer be removed cleanly"). Processes can also hold the
folder without being in the job: an Explorer or VS Code window from "Open
lane folder", or a terminal the user opened there. Fix: return whether
everything exited. If not, keep the lane (`LANE-KEEP reason=busy`) and
retry the retire later, or leave it for the user.

### 7. #37: an overlap is reported once per lane lifetime, not once per occurrence

The seen key is `path|peer_uid|level` (`session_hook.py:442`), and peer uids
are stable for a card's whole life. Lanes are live until the card closes,
across many tasks. If Agent 5 and Agent 6 overlap on `main_window.py` in
task 1, then again a week later in task 7, the second overlap is silent.
The only expiry is the 2000-key cap. `LaneService._noticed` never forgets
either. Fix: drop a key from the seen file and from `_noticed` once that
overlap is gone from a poll (the service knows), or add the peer's head
(or merge-base) to the key so a new round of work is new news.

### 8. #37: turning the switch off is not reliable on Windows

`LaneService.stop()` (`lane_service.py:139-156`) writes `"enabled": false`
once, swallows `OSError`, then clears `_written`, so the write is never
retried. On Windows, `os.replace` fails when another process has the target
open, and every hook process opens `lanes.json` on every edit. If that write
loses the race, running agents' hooks keep warning from a frozen snapshot
until the next switch-on. Fix: retry on a short `QTimer` until it succeeds.
As a backstop, have the hooks treat a `lanes.json` whose `ts` is older than
a few poll intervals as disabled (the poller would then need to refresh
`ts` on a slow heartbeat, not only on content change). Smaller note:
`_write_json_atomic` leaves its `.tmp` behind when the replace fails.

### 9. #37: "skim the roster" doesn't save the tokens it is meant to

A laned agent is told to "skim the roster at the top" of `board.md`, but
the Read tool returns the whole file, so the agent pays for the whole board
either way. Most of the token saving comes from Phase 0's rotation. Also,
#37 is not stacked on #35, so on its own the roster's task column is still
"-" for most agents (the Phase 0 AI-title fallback isn't there). The agent
would skim a table that says nothing. Fix: write the roster to its own small
file (`.aihive/roster.md`) and point laned agents at that, or say "Read it
with limit: 25". Merge #35 before #37.

### 10. #38: the integrator's lane shows up as a peer in overlap detection

The integrator works in its own lane on `integrate/<x>`. That branch holds
the submitted lane's commits, and `lane_snap` reads `HEAD`. So the owner of
every submitted lane gets "Integrator (hive/integrator-...) also changed
x.py (committed). Keep your change there small and local; the integrator
merges both." That notice is about the integrator itself, and other lanes
touching those files get the same noise. Fix: leave the integrator's lane
out of pairwise overlaps (`snapshot_repo` entries), or drop overlaps where
one head is an ancestor of the other.

### 11. #36: a revive that recreates the branch tells the user, not the agent

Spec question 3 asks whether the resumed agent should be told that its old
branch is gone. In `main_window.py:3984`, `agent.notice(...)` only writes
to the card log (`TerminalAgent.notice` is "Append a system-stream notice
to the log"), so the model never sees it and still believes its old
commits are on its branch. #37 added the right tool for this: also
`session_hook.append_notice(...)` so it arrives with the agent's next
prompt.

### 12. #36: a "clean" lane is removed along with its ignored files (verified)

`lane_status` uses `git status --porcelain`, which hides ignored files, and
`git worktree remove` without `--force` deletes ignored files silently. In
the scratch repo, a lane with an ignored `.env` containing `SECRET` was
removed with rc 0 and the file was gone. With #37's `.worktreeinclude`
copying `.env` and local configs INTO lanes, a user who edits the lane's
copy loses that edit when the card closes. Fix: treat changed
`.worktreeinclude` copies as dirty (compare with the repo-root original),
and list the remaining ignored files in the `LANE-REMOVE` audit line.

---

## Low

13. **Fetch can block lane creation, and may pop a sign-in window.** The
    5-minute `fetch_base` runs on the per-repo LaneOps worker. It is
    non-exclusive but still FIFO, so a lane create waits behind a hanging
    fetch for up to `FETCH_TIMEOUT` (120 s). This machine's
    `credential.helper` is `manager` (GCM), which ignores
    `GIT_TERMINAL_PROMPT` and can open a GUI sign-in from a background
    poll. Set `GCM_INTERACTIVE=never` in `_subprocess_git`'s env, and
    consider a separate fetch thread (fetch and `worktree add -b` take
    different ref locks).
14. **The brief carries lane-authored text into the integrator's prompt.**
    `gather_brief` (`integration.py:440`) puts commit subjects and file
    names into the brief verbatim, and the brief is typed as a bracketed
    paste. A subject containing `ESC[201~` plus CR ends the paste early.
    Strip control characters, and fence the log and stat. It is also a
    soft channel by which one agent's text instructs another. That is fine
    under the user-only-enqueue rule, but worth stating in the invariant.
15. **The manual "Update lane to origin/main" has no busy check**
    (`terminal_card.py:1128` -> `_on_lane_action`). #38's automatic refresh
    has one. Gate the menu item on `not agent.is_busy()`, like the
    automatic path.
16. **Squash-merged lanes never retire.** `merged` is ancestry, so in a
    squash-merge repo every closed lane is "kept" forever, with no UI to
    remove it. This repo merges with `--merge`, so it is fine here. For
    other repos, let a MERGED queue item for that lane count as merged, or
    offer "Remove (merged on GitHub)" when `gh` says so.
17. **Git runs on the GUI thread once per repo.** `LaneOps.key_for` runs
    `git rev-parse` synchronously on its first call per repo
    (`lane_ops.py:78`). That contradicts "no git runs on the GUI thread".
    It is cheap, but it is a git call on the GUI thread the first time.
18. **#35: the archive note may send agents to the archive.** "_Older
    entries are moved to board-archive.md_" can lead agents to read the
    archive too. Add "(read it only if you need history)".
19. **#35: extra roster writes.** `summary_changed` -> `_recompute` rewrites
    the board on every title change, even for agents whose roster row
    didn't change (task-first). It is harmless, but it is a board write per
    title tick. Consider comparing the rendered roster first.
20. **`copy_worktree_includes` walks node_modules.** `git ls-files --others
    --ignored` traverses all of `node_modules` before the junction-name
    filter, with the 15 s `READ_TIMEOUT`. On a big JS repo it times out and
    silently copies nothing. Pass `--exclude=node_modules/` (and the other
    junction names) to the walk.

---

## On the `## Comments` section itself

**Phase 0 (Agent Smith).** Accurate. I confirmed that `update_roster`
rebuilds the scaffold and drops the log when the markers are missing, and
that its comment says otherwise. Agreed that it deserves its own small PR.
On precedence: `current_task` can go stale once a delivered task is done,
while the AI title is live. The card shows the title first, so consider
matching it. It is a judgment call; the spec asked for task first. The
full e2e has not been run on #35 yet, so run it before merging.

**Phase 1 (Agent of Darkness).**
- Check 3 is right and important. I reproduced it: `git worktree remove`
  on a lane holding a `.venv` junction exited 0 and emptied the real
  `.venv`. The invariant is justified.
- The correction to check 1 (resume works from any folder) is valuable,
  but the spec body still states the old rationale, in "Lane lifetime >
  Revived" ("`--resume` would not find the conversation there") and in
  Step 0 item 1. Fold it in.
- `--track`: see High #2.
- The rule that "an in-flight op owns the lane" is a good one. Keep it in
  CLAUDE.md, not only in the comment.
- The junctions point at the main checkout's `.venv`, so `pip install` in
  one lane changes every lane. The lane prompt should say so in one line,
  not only the docstring.

**Phase 2 (Agent 47).**
- The "lost first task" finding was real, but its explanation (typing into
  a dead spot below the box) was disproved by Agent 49. Strike or annotate
  it in the comment, so nobody "fixes" readiness later.
- Dedupe by level is a good idea. The key just needs an expiry (Medium #7).
- The "edited OUTSIDE your lane" warning is useful, and it will fire on
  legitimate edits to shared docs in the main checkout. See "Process".
- "Other providers NOT done" contradicts the Decisions section ("Codex,
  Gemini and Grok keep running in the main checkout until their own checks
  pass (Phase 2)"). Update the spec, or file it as its own issue under
  `.scratch/`.

**Phase 3 (Agent 49, in the #38 copy only).**
- The delivery fix is well-reasoned and the CLAUDE.md invariant was updated
  with it. Good.
- "Recheck takes the PR's current head as the tested commit" and "turn end
  reads the PR for INTEGRATING and NEEDS_YOU heads": see High #3.
- "Strictly one item at a time" is right for correctness. Note the cost:
  the integrator sits idle while an item waits for approval, so the user is
  the bottleneck. That is fine for v1.
- Two practical facts belong in `docs/agents/integration.md` or the README,
  not only in a comment: the `PowerShell(git:*)` permission, and that
  auto-memory is shared per main checkout.
- The manual check against a real GitHub PR was not done. Do it on a
  throwaway GitHub repo before merging #38. It is the one path with real
  side effects.

---

## Process

- **The spec has forked.** There are three different `spec-v2.md` files
  right now: 46 KB on #36, 55 KB on #37 and in this checkout, and 63 KB on
  #38. Each phase appended to `## Comments` in its own lane. That is lanes
  doing what they are designed to do to a shared append-only doc. The
  "reviewers append under ## Comments" workflow will conflict on every
  merge. Suggest one file per author per review (`review-<agent>.md`, like
  this one), or keep review notes in the untracked `.aihive/` folder.
- **The spec body is behind its comments.** The comments correct check 1,
  check 3 ("second safeguard" is now mandatory), the delivery cause,
  "Clean up lane" (now "Update lane"), and the other-providers scope.
  Before merge, fold these into the body (a v3), since the body says "the
  author folds accepted findings in".
- **Merge order and versions.** Merge #35 first: 0.23.1 is below #36's
  0.24.0/0.25.0, so no renumbering is needed. The reverse order needs
  renumbering, and the updater would not offer 0.23.1. Then #36, retarget
  and merge #37, then #38. Expected conflicts: the CHANGELOG top, the
  `MODULES` line in `tests/smoke_test.py`, and `roster_row` /
  `_render_roster`. For the roster, keep Phase 0's whitespace collapse and
  task fallback, then add the lane columns.
- **#36 also carries two unrelated features**: 8bb4321 (GitHub clone) and
  bd2707d (Count stepper), plus 9e9f435. A 4 kLOC "agent lanes" PR is
  harder to review with those inside. Land them as their own PR first, or
  at least list them at the top of #36's body.
- **The red suite.** Every PR in the stack reports "1 known failure"
  (`pty width`). The new integrator checklist says "never decide yourself
  that a failure is acceptable", and this stack is being merged on exactly
  that judgment. Fix or explicitly quarantine `test_pty_width_at_launch`
  first, so the suite is green when the integrator workflow starts relying
  on it.

---

## Short answers to "What reviewers should push hardest on"

1. **Lane location.** The sibling `<repo>.lanes` folder is the right
   default. Lanes inside the repo break JS tooling, and putting them under
   `%LOCALAPPDATA%` hides them from the user. Cut the per-lane trust
   prompts by trusting `<repo>.lanes` once (the Phase 1 note says a
   trusted parent covers child folders), and warn when the repo sits under
   OneDrive, which would sync every lane.
2. **Switch off with live lanes.** Yes, that is what a user expects. Make
   sure "off" really silences the hooks (Medium #8). The tooltip could add
   "lane warnings stop now; lanes stay".
3. **Revive.** It needs a notice to the AGENT, not only the card log
   (Medium #11).
4. **ff-only refresh of idle lanes.** It is safe. Claude's Edit refuses a
   file modified since it was read, and the notice says to re-read. Gate
   the manual action on busy too (Low #15).
5. **Invariant fit.** Yes. Every item traces back to a user click, and the
   "base moved" brief continues that same item. Write the commit-text
   channel into the invariant (Low #14).
6. **Hook cost.** About 40 ms warm per edit, for laned agents only, is
   fine. Keep the separate settings file.
