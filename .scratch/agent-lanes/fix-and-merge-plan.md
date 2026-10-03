# Agent lanes: fix plan, proof rules and merge order

Status: needs-triage (5 decisions for the user, see "Decisions")

Author: Agent 51, 2026-10-03. Inputs: `review-feedback.md` (called **R1**
below), `review-agent50.md` (**A50**), the `## Comments` of `spec-v2.md` (the
63 KB copy on #38), and the code of PRs #35 (`f747c80`), #36 (`344a96b`),
#37 (`f7ea8e8`) and #38 (`277cd2e`). Line numbers are on the #38 tip unless
a finding says otherwise.

This plan touches no code. Every finding from both reviews and from the
spec comments has an entry in the ledger below: a fix in a named PR, or a
follow-up issue with the reason. Each fix also lists the proof that has to
appear in its PR before that PR can merge.

---

## TL;DR

- **7 merge blockers.** R1 found 2, A50 found 3, and A50's "red suite" turns
  out to be a real regression (F7). One of the 3 A50 fixes would not have
  worked as written (F4). The 7: a merged PR is accepted without approval
  (F1), a resubmitted lane is marked merged by its old PR (F2), the PR base is
  never checked (F3), lane branches track `origin/main` (F4), "tested" is not
  the commit the suite ran on (F5), NEEDS_YOU re-approves itself (F6), and
  the suite is red (F7).
- **The red suite is a real regression, not a flake.** `pty width` passes at
  0.22.5 and fails from `babeb51` (PR #33, GitHub activity menu) on, bisected
  on this machine. It has shipped since 0.23.0.
- **Merge order:** PR A (pty fix) -> #35 -> PR B (Count stepper + GitHub
  clone, split out of #36) -> #36 -> #37 -> #38. Always `--merge`, never
  squash, retarget each stacked PR before deleting its base branch.
- **Main is the release channel.** `app/self_update.py` fast-forwards the
  user's app to `origin/main`, so every merge ships. Each PR has to be
  correct on its own, so every fix lands in the lowest PR whose code it
  touches, before that PR merges.

---

## What was verified while writing this plan

These are new facts. They change the fixes the reviews proposed.

1. **`pty width` is a regression from PR #33.**
   `tests\smoke_test.py -k pty_width`: 7/0 at `ad8696c` (0.22.5), 6/1 at
   `f747c80` and at `391fb13`. `git bisect run` between `ad8696c` and
   `391fb13` gave `babeb51 feat: add GitHub repository activity menu (#33)`
   as the first bad commit. The failing check is "no child was ever told two
   different widths": all 8 children spawned at 40 columns and were then
   resized to 46 or 47. That is exactly the bug the test guards against:
   resumed conversations get wrapped at the wrong width after a reopen.
2. **Dropping `--track` does not fix A50 #2.** In a scratch repo (git
   2.51.2), `git worktree add -b hive/x <path> origin/main` without `--track`
   still sets `origin/main` as the upstream, because `branch.autoSetupMerge`
   defaults to true for a remote-tracking start point. `git status` still
   says "ahead of 'origin/main'", and a bare `git push` still suggests the
   upstream push. With `--no-track` there is no upstream, and the bare push
   suggests `git push --set-upstream origin hive/z`, which only publishes
   the lane's own branch.
3. **Without an upstream, `git branch -d` refuses a merged lane.** With the
   main checkout on another branch, `branch -d` said "not fully merged" for a
   lane that was merged into `origin/main`. So the delete has to change too.
   `git update-ref -d refs/heads/<b> <sha>` refused a wrong expected sha
   ("is at X but expected Y") and deleted the branch with the right one.
4. **A50 #20's `--exclude=node_modules/` is wrong.** Measured on a repo with
   10,000 files under `node_modules` and `.worktreeinclude` = `.env`:

   | `git ls-files -z --others --ignored --exclude-from=.worktreeinclude` | time | paths |
   |---|---|---|
   | as today | 97 ms | 1 (`.env`) |
   | `+ --exclude=node_modules/` (A50's fix) | 92 ms | **10,001** |
   | `+ -- . ':(exclude)node_modules'` | 47 ms | 1 (`.env`) |

   The pathspec exclusion prunes the walk. The `--exclude` form marks every
   file under `node_modules` as ignored, so git lists all of them.
5. **The shared checkout's uncommitted work is commit `9e9f435`.** Its diff
   to `bd2707d` (CHANGELOG, README, `main_window.py`, `tests/smoke/sidebar.py`)
   has the same hunks as `9e9f435` on #36. The only difference is the README
   check count, which was rebased (2150->2159 vs 2283->2292). Its untracked
   `spec.md` and `docs/ISOLATED-AGENT-TASKS-PLAN.md` are byte-identical to
   #36's tracked copies, and its `spec-v2.md` is identical to #37's copy.
   `review-agent50.md`, `review-feedback.md` and this file exist **only**
   there, untracked.
6. **GitHub settings:** `delete_branch_on_merge` is false and `main` has no
   branch protection. Stacked PRs are not retargeted automatically, and
   deleting a PR's base branch closes the PR.

---

## Decisions for the user

| # | Question | Recommendation |
|---|---|---|
| D1 | Split the Count stepper (`bd2707d`), GitHub clone (`8bb4321`) and its fix (`9e9f435`) out of #36 into their own PR B? (R1 and A50 both raised it) | **Yes.** They are finished and can ship now without waiting for the lane fix rounds, and #36 shrinks to lanes only. |
| D2 | If the `pty width` fix (F7) isn't done within one working session, may that one check be quarantined so the lane PRs can proceed? | **Only with an issue file and your OK.** It is a live bug shipping since 0.23.0, so it should be fixed first. |
| D3 | Roster task column: the delivered task first (spec), or the live AI title first (what the card shows)? (A50, Phase 0) | **Title first.** `current_task` goes stale once a task is done, and the card already shows the title first. |
| D4 | May an agent create a private throwaway GitHub repo for the live #38 check (F35)? It opens and merges real PRs there, and the repo is deleted after. | **Yes.** It is the only path with real side effects, and the fake `gh` could not show F2. |
| D5 | Squash-merged lanes never retire (F20, A50 #16). Fix it in #38 with a user-only "Remove lane (merged as #N)", or leave it as a follow-up issue? | **#38.** It is small, and other repos you open in AI Hive may squash-merge. |

---

## The proof standard

A fix counts as done only when its PR body shows all of these:

- **P1, red first.** A regression check in `tests/smoke/<area>.py` that
  fails on the pre-fix code, on the named check (an `ImportError` does not
  count). Procedure, in a throwaway worktree (no `.venv` needed there; use
  the shared checkout's interpreter, as the bisect above did):

  ```powershell
  git worktree add --detach $env:TEMP\proof <fix-commit>
  cd $env:TEMP\proof
  git checkout <pre-fix-commit> -- app/        # old code, new tests
  C:\Users\Mario\ai-hive\.venv\Scripts\python.exe tests\smoke_test.py -k <test>   # must FAIL on <check>
  git checkout <fix-commit> -- app/            # new code
  C:\Users\Mario\ai-hive\.venv\Scripts\python.exe tests\smoke_test.py -k <test>   # must PASS
  cd C:\Users\Mario\ai-hive; git worktree remove $env:TEMP\proof
  ```

  Write checks against behavior (the folder still exists, the branch is
  still there, no `gh pr merge` call was recorded) rather than against new
  function names, so they can run against the old code.
- **P2, mutation.** For every guard: remove the guard, see the check fail,
  then restore it. Write the guard and the check name in the PR body.
- **P3, real git.** Git behavior is tested on real temp repos (the
  `tests/smoke/lanes.py` helpers). A fake must be able to represent the
  failing case. For example, `_FakeGh` must hold several PRs per branch
  (F2).
- **P4, green suite.** The full suite including e2e on the PR tip after
  `main` is merged in, with `RESULT: N passed, 0 failed` pasted into the PR
  body. The README count is set from that N.
- **P5, live.** Where the ledger asks for it, a live run against a real CLI,
  with a short transcript excerpt.

PR body template:

```markdown
## Fixes (.scratch/agent-lanes/fix-and-merge-plan.md)
| F | Check(s) | Red on <old sha> | Green on <new sha> | Mutation |
|---|---|---|---|---|
## Suite
RESULT: N passed, 0 failed, S skipped (full, incl. e2e) on <sha>
## Live checks
```

---

## Ledger

Severity is the reviewer's. "PR" is where the fix lands.

| F | Source | Sev | PR | Finding |
|---|---|---|---|---|
| F1 | R1 High 1, A50 #1 | High | #38 | A merged PR is accepted without approval, and without checking that it contains the item |
| F2 | A50 #1 | High | #38 | One integrate branch per lane, so a resubmitted lane is marked merged by its old PR |
| F3 | R1 High 2 | High | #38 | The PR's base and head branches are never checked |
| F4 | A50 #2 | High | #36 | Lane branches track `origin/main`, so git suggests `push HEAD:main` |
| F5 | A50 #3a | High | #38 | `tested_sha` is not the commit the suite ran on |
| F6 | A50 #3b | High | #38 | Any integrator turn end re-approves a NEEDS_YOU item |
| F7 | A50 process | High | PR A | The suite is red: `pty width` regression from `babeb51` |
| F8 | A50 #4 | Med | #38 | Approve merges against a stale base when the fetch fails |
| F9 | A50 #5 | Med | #36 | A lane for an untracked subfolder or an ancestor repo has no folder to start in |
| F10 | A50 #6 | Med | #36 | Retire removes the worktree after the exit wait timed out |
| F11 | A50 #7 | Med | #37 | An overlap is reported once per lane lifetime |
| F12 | A50 #8 | Med | #37 | Switching off is not reliable on Windows |
| F13 | A50 #9 | Med | #37 | "Skim the roster" still reads the whole board |
| F14 | A50 #10 | Med | #38 | The integrator's lane shows up as an overlap peer |
| F15 | A50 #11 | Med | #37 | A revive that recreates the branch tells the user, not the agent |
| F16 | A50 #12 | Med | #36 + #37 | A clean lane is removed together with its ignored files |
| F17 | A50 #13 | Low | #36 + #37 | Fetch blocks lane creation, and Git Credential Manager can pop up a sign-in window |
| F18 | A50 #14 | Low | #38 | Lane-authored text reaches the integrator brief unsanitized |
| F19 | A50 #15 | Low | #37 | The manual "Update lane" has no busy check |
| F20 | A50 #16 | Low | #38 (D5) | Squash-merged lanes never retire |
| F21 | A50 #17 | Low | #36 | `LaneOps.key_for` runs git on the GUI thread |
| F22 | A50 #18 | Low | #35 | The archive note may send agents to read the archive |
| F23 | A50 #19 | Low | #35 | The roster is rewritten on every title tick |
| F24 | A50 #20 | Low | #37 | `copy_worktree_includes` walks `node_modules` (fix corrected) |
| F25 | Phase 0 note, A50 | Med | #35 | `update_roster` and `append_activity` drop the board log when a marker is missing |
| F26 | R1, A50 | Gate | #35 | #35 never ran the real-claude e2e |
| F27 | A50 Phase 0 | Decision | #35 | Roster precedence, task vs title (D3) |
| F28 | A50 process | Docs | #38 | The spec forked into 3 copies, and the body is behind its comments |
| F29 | A50 Phase 1 | Docs | #36 | "An in-flight op owns the lane" is only in a comment |
| F30 | A50 Phase 1, #2 | Prompt | #36 | The lane prompt omits the shared `.venv` and the push rule |
| F31 | A50 Phase 2 | Docs | #38 | Phase 2's disproved delivery cause is still stated |
| F32 | A50 Phase 2 | Process | docs | "Edited outside your lane" fires on shared-doc edits |
| F33 | A50 Phase 2, R1 | Docs | #38 + issue | Other providers: the spec contradicts itself |
| F34 | A50 Phase 3 | Docs | #38 | `PowerShell(git:*)` permission and shared auto-memory are only in a comment |
| F35 | A50 Phase 3 | Gate | #38 (D4) | No live check against a real GitHub PR |
| F36 | A50 Phase 3 | Accepted | spec | One item at a time makes the user the bottleneck |
| F37 | R1, A50 | Process | PR B (D1) | #36 carries two unrelated features |
| F38 | R1 | Process | issue | No GitHub status checks |
| F39 | R1, A50 | Process | merge plan | Merge order and version numbers |
| F40 | A50 short answers | Low | #37 + issues | Switch-off tooltip; trust `<repo>.lanes` once; OneDrive warning |

---

## Blockers

### F1. A merged PR is accepted without approval (#38)

**Where:** `app/integration.py:499` (`read_outcome`, MERGED path) and `:532`
(`approve_merge`, "already merged" path).

**Fix:**
- `read_outcome` never returns MERGED. It maps a MERGED PR to NEEDS_YOU with
  reason `merged-outside` ("Pull request #N was merged outside Approve merge.
  Check it, then Mark merged or Skip."). The queue does not advance.
- `approve_merge` does the same for a PR that was already MERGED before its
  own `gh pr merge` call. MERGED stays possible only after AI Hive's own
  merge call succeeds and the re-read says MERGED (unchanged).
- New user-only row action **Mark merged** (NEEDS_YOU with reason
  `merged-outside` only). It runs on LaneOps, re-reads the PR, and accepts
  only when the state is MERGED, F3's base/head checks pass, and
  `_is_ancestor(item.sha, pr.head)` holds. Audit: `QUEUE-MERGED manual=1`.
- CLAUDE.md, integration-queue invariant: "An item becomes merged only
  through Approve merge or the user's Mark merged, and only when GitHub says
  MERGED."

**Proof:**
- P1, `test_integration_core`: at the integrator's turn end, the fake PR is
  MERGED. The item goes to NEEDS_YOU `merged-outside`, and the next queued
  item is **not** delivered (no `deliver_task` to the integrator). Red on
  `277cd2e`, where the item is MERGED and the next one goes out.
- P1, Mark merged: a MERGED PR that doesn't contain `item.sha` stays
  NEEDS_YOU. One that does becomes MERGED, with `QUEUE-MERGED manual=1`.
- P2: put the MERGED return back in `read_outcome`, and the check fails.
- P5: F35 scenario 4.

### F2. A resubmitted lane is marked merged by its old PR (#38)

**Where:** `integration.py:194` (`integrate_branch` per lane), `:490`
(`read_outcome`), `tests/smoke/lanes.py:1682` (`_FakeGh.prs` keyed by
branch).

**Fix:**
- The integrate branch is per item: `integrate/<lane tail>-<item.id[:6]>`.
  It is derived, not stored, so no persisted shape changes. SESSION_VERSION 6
  is unreleased, so no migration is needed.
- Every path that accepts a PR checks `headRefName == item.integrate_branch`
  (F3) and `_is_ancestor(item.sha, pr.head)`. AWAITING already checks
  ancestry; the MERGED paths now go through F1.
- `_FakeGh` keeps a list of PRs per head branch. `pr view <branch>` returns
  the open one, otherwise the newest, like gh.

**Proof:**
- P1, new check "submit, merge, submit the same lane again, integrator ends
  a turn with no new PR": item 2 is NEEDS_YOU "No pull request from
  integrate/...-<id6> yet", **not** MERGED. Update the fake first, then run
  against the old `integration.py`: red on `277cd2e` (item 2 is MERGED).
- P1: two items of one lane get different integrate branches, and the brief
  names the per-item branch.
- P5: F35 scenario 2.

### F3. The PR's base and head are never checked (#38)

**Where:** `integration.py:258` (`pr_view` reads only
`number,state,headRefOid,url`), plus `read_outcome` and `approve_merge`.

**Fix:** `pr_view` also reads `baseRefName,headRefName,isCrossRepository`.
`read_outcome`, `approve_merge` and Mark merged return NEEDS_YOU when the
base is not `item.base`, the head branch is not `item.integrate_branch`, or
the PR comes from a fork. `approve_merge` looks the PR up by number, so the
head-branch check also stops a swapped PR number.

**Proof:**
- P1: a PR with base `develop` never reaches AWAITING.
- P1: an AWAITING item whose PR the fake retargets to another base: Approve
  refuses, and `_FakeGh.merges() == []`. Red on `277cd2e` (it merges).
- P2: drop the base check, and the check fails.
- P5: F35 scenario 5.

### F4. Lane branches track `origin/main` (#36)

**Where (on #36):** `app/lanes.py` `create_lane` and `repair_lane`
(`worktree add --track -b`), and `remove_lane` (`branch -d`).

**Fix:** see "What was verified", items 2 and 3.
- `create_lane` and `repair_lane` use `worktree add --no-track -b ...`.
  Removing `--track` alone is not enough.
- `remove_lane` keeps its clean + `_in_base` check. It then deletes the
  branch with `git update-ref -d refs/heads/<branch> <st.head>`, a
  compare-and-delete that refuses if the ref moved. Never `-D`. Update the
  docstring and the CLAUDE.md lanes invariant ("branch deletion is
  `update-ref -d` with the verified head").
- `repair_lane` on an existing branch whose upstream is the base runs
  `git branch --unset-upstream`. This only matters for lanes made by
  pre-fix builds during testing.
- Lane prompt: "Never push your lane branch to `<base>`. The integrator
  opens the pull request." (see F30)

**Proof:**
- P1, `test_lanes_core`, real repo with an origin: after `create_lane`
  there is no `branch.<b>.merge` config, and `git status` in the lane does
  not mention `origin/`. Red on `344a96b`.
- P1: `remove_lane` removes a merged lane while the main checkout is on
  another branch (the case where `branch -d` would refuse), and the branch
  is gone.
- P1, P2: a runner that moves the branch between the status read and the
  delete. `LaneError` is raised and the branch is kept. Drop the expected
  sha, and the check fails.
- P1: static check that no argument list in `app/lanes.py` contains
  `--track`, `-D` or `--force`.

### F5. "Tested" is not what the suite tested (#38)

**Where:** the `docs/agents/integration.md` checklist (suite in step 3,
then code-review fixes, then version/CHANGELOG/README), `DEFAULT_CHECKLIST`
(`integration.py:279`), and `read_outcome:516`, which records `pr.head`.

**Fix:**
- New checklist order:
  1. branch;
  2. merge the base and resolve conflicts;
  3. version and CHANGELOG;
  4. `/code-review` and its fixes;
  5. commit;
  6. the FULL suite on that commit;
  7. the README count only;
  8. push, and open the PR with a `Tested-commit: <full sha>` line and the
     `RESULT` line in the body;
  9. never merge.

  If anything besides `README.md` changes after step 6, rerun step 6 and
  update the line.
- `pr_view` also reads `body`. `read_outcome` reaches AWAITING only when
  `Tested-commit` names a commit we have, `item.sha` is its ancestor, and
  the PR head either equals it or descends from it through a diff that
  touches only `README.md`. Otherwise it returns NEEDS_YOU with reason
  `untested-head`, listing the files that changed after the test.
  `tested_sha` stays the PR head (what `--match-head-commit` merges). The
  panel and the audit line also show the suite commit.
- The user's Recheck applies the same rule. It does not bypass it.
- Say the remaining trust plainly in `integration.md`: `Tested-commit` is
  the integrator's claim. AI Hive can't prove that the suite ran. The
  `RESULT` line is in the PR body for the user, who approves.

**Proof:**
- P1, three cases. No `Tested-commit` gives NEEDS_YOU. `Tested-commit` =
  head~1 with `app/x.py` changed after it gives NEEDS_YOU naming `app/x.py`.
  Only `README.md` changed gives AWAITING with `tested_sha` = head. Red on
  `277cd2e` (all three are AWAITING).
- P1: the rendered brief puts the suite after version/CHANGELOG and code
  review, and mentions `Tested-commit`.
- P5: F35 scenarios 1 and 3.

### F6. NEEDS_YOU re-approves itself (#38)

**Where:** `app/widgets/main_window.py:4404` (`_on_integrator_turn_ended`
rechecks INTEGRATING **and** NEEDS_YOU heads).

**Fix:**
- `QueueItem` gains `reason` (`no-pr`, `head-moved`, `merged-outside`,
  `untested-head`, `base-unknown`, ...). It is an additive key with a
  default, restored by `from_dict`, and saved through
  `update_queue_item`'s `_touch`. SESSION_VERSION 6 is unreleased, so there
  is no bump; the CLAUDE.md three-part rule is still met.
- A turn end auto-rechecks only INTEGRATING items and NEEDS_YOU with reason
  `no-pr`. Every other NEEDS_YOU waits for the user's Recheck.

**Proof:**
- P1: AWAITING, then Approve refuses "changed after it was tested", so the
  item is NEEDS_YOU `head-moved`. An integrator turn end leaves it
  NEEDS_YOU and writes no `QUEUE-AWAITING` line. Red on `277cd2e` (it goes
  back to AWAITING).
- P1: a NEEDS_YOU `no-pr` item still advances on a turn end (the existing
  check stays).
- P1: `reason` survives a real close and reopen, and an item saved without
  `reason` loads as `""`.

### F7. The red suite: the `pty width` regression (PR A)

**Facts:** see "What was verified", item 1.

**Where to look first:** `babeb51`'s header changes in
`app/widgets/workspace_page.py`: the trash icon on `delete_btn`, and the
new `repo_actions` host with "Open repo" and the dropdown button. These
widen the header row, which probably changes the cards' width after
`settle_layout` has already measured it. This is a lead, not a diagnosis.

**Fix:** whatever the diagnosis shows (`mattpocock-skills:diagnosing-bugs`).
The test stays as it is: no tolerance, no retry.

**Proof:** `-k pty_width` gives 7/0. The bisect method from above: fails on
`391fb13`, passes on the fix commit. Full suite 0 failed. Timebox: D2.

---

## Medium

### F8. Approve merges on a stale base when the fetch fails (#38)

**Where:** `integration.py:547` ignores `lanes.fetch_base(...)`'s result.

**Fix:**
- A failed fetch returns NEEDS_YOU `base-unknown` ("Could not fetch
  `<base>`; nothing was merged").
- Then read the base head from GitHub
  (`gh api repos/{owner}/{repo}/branches/<base> --jq .commit.sha`) and
  require it to equal the fetched `origin/<base>`.
- In `integration.md`, describe the window that remains between the check
  and the merge, and the optional "Require branches to be up to date"
  protection. `main` has no protection today.

**Proof:**
- P1: a real temp repo whose origin points at a missing path. Approve
  returns NEEDS_YOU and `_FakeGh.merges() == []`. Red on `277cd2e` (it
  merges).
- P1: the fake gh reports a base sha that differs from the local one, and
  Approve refuses.

### F9. A lane with no folder to start in (#36)

**Where:** `lanes.plan_lane` / `_mapped_cwd`, `_on_lane_created` (sets a
cwd it never checks), and `find_repo_root` (walks up to any ancestor
`.git`).

**Fix:**
- `create_lane` (worker): after `worktree add`, if the mapped cwd is not a
  directory, remove the fresh lane (unlink junctions, `worktree remove`, F4's
  `update-ref -d`; it has no commits) and raise
  `LaneError("location", "<rel> is not tracked in the repository, so a lane
  would not contain it")`. The caller already falls back to the workspace
  folder and writes `LANE-FAIL`.
- Dialog: when `find_repo_root(project) != project`, the checkbox defaults
  to OFF, with the tooltip "This folder is inside the repository at
  `<root>`. A lane copies the whole repository." This needs no git on the
  GUI thread, and it covers the home-directory dotfiles repo case.

**Proof:**
- P1: a workspace in an ignored subfolder. The agent starts in the
  workspace folder, `LANE-FAIL location` is audited, and no lane folder or
  branch is left behind. Red on `344a96b` (the agent's cwd doesn't exist).
- P1: a tracked subfolder gets a lane whose mapped cwd exists.
- P1: a workspace nested under a parent repo gets the checkbox, unchecked.

### F10. Retire removes after a timed-out wait (#36)

**Where:** `lanes.retire_lane` / `wait_for_exit` (`lanes.py:591-631`).

**Fix:**
- `wait_for_exit` returns True only when every process exited. On False,
  `retire_lane` keeps the lane: `RetireResult(False, st, "busy")`,
  `LANE-KEEP reason=busy`.
- `MainWindow` retries the retire up to 3 times, 30 s apart, through
  `lane_ops`, then shows the "lane kept" notice.
- If `git worktree remove` itself fails (a process outside the job, such as
  Explorer or VS Code, holds the folder), write `LANE-FAIL` with git's
  stderr and show the kept notice. Never retry with `--force`.

**Proof:**
- P1: a harmless sleeper (`python -c "import time; time.sleep(30)"`) with
  its cwd in the lane, its pid passed with `wait_s=0.3`. The lane folder and
  branch still exist, and the reason is `busy`. Kill the sleeper, and the
  retry removes the lane. Red on `344a96b`.
- P2: ignore the return value, and the check fails.

### F11. An overlap is reported once per lane lifetime (#37)

**Where:** `session_hook.py:442` (key `path|peer_uid|level`) and
`lane_service.py:96,356-360` (`_noticed` never forgets).

**Fix:** put the round of work in the key:
`path|peer_uid|level|<my fork>|<peer fork>`, where a fork is
`git merge-base HEAD <base_ref>`.
- `lane_snap` computes the fork (cached by head and base sha) and
  `lanes.json` publishes it per lane. The hook and `Overlap.key` build the
  key the same way.
- A lane that merged its work and moved to the new base has a new fork, so
  a repeat overlap is news again.
- The key comes from git state, so a restart doesn't repeat old news, and
  no new persisted state is needed.

**Proof:**
- P1, `test_lane_awareness_core`. An overlap gives 1 notice. The same
  overlap on the next poll gives 0. Then the peer's work lands on the base,
  the peer lane is refreshed, and the peer edits the same file again: 1 new
  notice. A new `LaneService` with the same seen file gives 0. Red on
  `f7ea8e8` (the third step gives 0).
- P1: the hook subprocess test, with the same steps through `lanes.json`.

### F12. Switching off is not reliable on Windows (#37)

**Where:** `LaneService.stop()` (`lane_service.py:139-160`) and
`session_hook._write_json_atomic` (`:332`).

**Fix:**
- `stop()` keeps a pending set of failed writes and retries them on a
  250 ms `QTimer` up to 20 times, then writes `LANE-FAIL`.
- Hooks treat a `lanes.json` whose `ts` is older than 45 s (3 polls) as
  disabled. While running, the service refreshes `ts` on every poll, even
  when nothing else changed.
- `_write_json_atomic` removes its `.tmp` when the replace fails.

**Proof:**
- P1: hold `lanes.json` open (a plain Python `open()` blocks `os.replace`
  on Windows), then call `stop()`. The file still says enabled. Close it,
  pump 1 s, and it says `enabled: false`. Red on `f7ea8e8` (it is never
  rewritten).
- P1: a `lanes.json` with `ts = now - 120` makes the hook print nothing on
  a real overlap.
- P1: no `.tmp` is left after a failed replace.

### F13. "Skim the roster" still reads the whole board (#37, after #35)

**Fix:**
- `update_roster` also writes `.aihive/roster.md`, by absolute path under
  `ws.board.dir`, in the same serialized call.
- The laned-agent prompt points at `roster.md` only. Agents without a lane
  keep today's board instruction.
- #35 merges first, so the task column has content.

**Proof:**
- P1: the laned prompt names the absolute `roster.md` path and doesn't tell
  the agent to read `board.md`. `roster.md` follows roster changes.
- P1: with 10 agents, `roster.md` stays under 3 KB.
- The existing "no relative `.aihive`" check covers the new path.

### F14. The integrator's lane is an overlap peer (#38)

**Fix:**
- `snapshot_repo` entries carry a role. The integrator's lane is left out
  of the pairwise overlaps and the `lanes.json` file index.
- Overlaps where one head is an ancestor of the other are dropped too.

**Proof:**
- P1: an integrator lane on `integrate/x`, built from lane A's head. A gets
  no notice and there is no event-log row. Red on `277cd2e`.
- P1: A and B overlapping are still reported.

### F15. A revive that recreates the branch tells only the card log (#37)

**Where:** the revive callback in `main_window.py` (around `:3984` on #38)
calls `agent.notice(...)` only.

**Fix:** also call `session_hook.append_notice(notices_path(ws, uid),
"revive|<branch>|<ts>", text)`, so the model reads it with its next prompt.
With the switch off, the hooks aren't armed, so the card log is all there
is. Document that.

**Proof:** P1: revive with a deleted branch. The notices file has one entry,
and the hook subprocess on a `UserPromptSubmit` payload outputs it as
`additionalContext`. Red on `f7ea8e8`.

### F16. A clean lane is removed with its ignored files (#36 + #37)

A50 verified this: an ignored `.env` holding `SECRET` was deleted with
rc 0.

**Fix:**
- #36: before `worktree remove`, list the lane's ignored files with
  `ls-files -z -o -i --exclude-standard --directory`, using F24's pathspec
  excludes for the junction names. Put them in the `LANE-REMOVE` audit
  line (first 20, plus a count). Nothing is removed silently.
- #37: `lane_status` counts as dirty a `.worktreeinclude` copy that differs
  from the repo-root original, and an ignored file matching
  `.worktreeinclude` that doesn't exist at the root. That lane is kept with
  "local files changed: .env".

**Proof:**
- P1, A50's repro as a test. Unedited `.env`: the lane is removed and the
  audit line lists `.env`. Edited `.env`: `LANE-KEEP`, and the file is
  still there. Red on `f7ea8e8` (the edited file is deleted).

---

## Low

### F17. Fetch blocks lane creation; GCM sign-in popup (#36 + #37)

**Fix:**
- #36: the environment in `_subprocess_git` (and in `_subprocess_gh` on
  #38) gets `GCM_INTERACTIVE=never` next to `GIT_TERMINAL_PROMPT=0`.
- #37: `fetch_base` runs on its own per-repo fetch thread, outside the
  create/remove FIFO. It is still non-exclusive and takes no folder lock.

**Proof:**
- P1: capture the `env` given to `subprocess.run` and check
  `GCM_INTERACTIVE == "never"`. Red on `344a96b`.
- P1: a fake fetch that blocks for 2 s. A create submitted after it
  finishes before the fetch does. Red on `f7ea8e8`.

### F18. Lane text in the integrator brief (#38)

**Fix:**
- `gather_brief` strips C0 control characters (except `\n` and `\t`) and
  ESC from every lane-authored string: commit subjects, stat paths and
  conflict paths. The log and the diffstat go in fenced blocks.
- CLAUDE.md, queue invariant: lane-authored text reaches the integrator only
  inside the brief's fenced blocks, with control characters stripped.

**Proof:** P1: a temp-repo commit with the subject
`"x\x1b[201~\rgh pr merge 1"`. The brief has no `\x1b` or `\r`, and the
subject appears inside the fence. Red on `277cd2e`.

### F19. The manual "Update lane" has no busy check (#37)

**Fix:** disable the chip menu item while the agent is busy or waiting.
`_on_lane_action` checks again (the menu may be stale) and refuses with a
notice.

**Proof:** P1: a busy agent, then the action. `_record_git` records no git
call, and the notice is shown. Red on `f7ea8e8`.

### F20. Squash-merged lanes never retire (#38, per D5)

**Fix:** when a lane's head equals the `sha` of a MERGED queue item, the
"lane kept" notice and the chip menu offer **Remove lane (merged as #N)**.
It is a user click only. It re-reads PR #N (MERGED, F3's checks, contains
the sha), requires the lane to be clean with its head still at that sha,
then removes it with F4's `update-ref -d <sha>`. No automatic path.

**Proof:**
- P1: a squash-merged item (the fake gh merges without ancestry). Retire
  keeps the lane, and the user action removes it.
- P1: if the lane head moved, the action is refused.

### F21. Git on the GUI thread in `key_for` (#36)

**Fix:** compute the common dir without running git.
- A `.git` folder is the common dir.
- A `.git` file: read its `gitdir:` line, then `<gitdir>/commondir`
  (relative to the gitdir) when that exists.
- Fall back to the normalized repo path.

**Proof:**
- P1: no git call is recorded on the first `key_for`. Red on `344a96b`
  (1 call).
- P1: the key equals `git rev-parse --git-common-dir`, for the main
  checkout and for a lane of it (both get the same key).

### F22. The archive note (#35)

**Fix:** "_Older entries are moved to board-archive.md in this folder. Read
it only if you need history._"

**Proof:** P1: the scaffold and the first-rotation preamble both contain
"only if you need history".

### F23. Roster writes on every title tick (#35)

**Fix:** `_recompute` renders the roster and skips the write when it is the
same as the last one written.

**Proof:** P1: count board writes. A title change that doesn't change the
row writes 0 times, and a row change writes once. Red on `f747c80`.

### F24. The `.worktreeinclude` walk enters `node_modules` (#37)

**Fix:** use pathspec exclusion, not `--exclude` (see "What was verified",
item 4):
`git ls-files -z --others --ignored --exclude-from=<spec> -- . ':(exclude)node_modules' ':(exclude).venv' ':(exclude)venv'`
(one exclusion per `JUNCTION_NAMES` entry).

**Proof:** P1: a repo with 2,000 files under `node_modules`. It copies
exactly `[".env"]`, and the recorded git argv contains the pathspec
excludes. Red on `f7ea8e8` (argv check).

### F25. The board log is dropped when a marker is missing (#35)

This bug predates #35. Agent Smith found it, and A50 confirmed it. With
rotation, it could still lose the last 40 entries.

**Fix:**
- `update_roster` re-inserts missing markers at the top and keeps every
  other line.
- `append_activity` adds a fresh `## Activity log` heading at the end
  instead of replacing the file.

**Proof:**
- P1: a board with its markers removed and 5 entries. After
  `update_roster`, all 5 are still there.
- P1: a board without the heading. After an append, the old content is
  intact, followed by the new heading and the entry. Red on `f747c80`.

---

## Docs, process and gates

- **F26, #35 e2e.** It is part of #35's P4 gate. Merge nothing without it.
- **F27, roster precedence.** Implement as D3 decides, in #35, and test
  both orders' fallback.
- **F28, spec v3 (#38's last commit).** Write `spec-v3.md` with the
  comments folded into the body:
  - check 1's rationale (`--resume` works from any folder; the lane path
    stays fixed for AI Hive's own readers);
  - check 3 made mandatory;
  - Agent 49's delivery cause, replacing Phase 2's (F31);
  - "Clean up lane" is now "Update lane";
  - other providers are deferred (F33);
  - the fixes in this plan: `--no-track`, a branch per item,
    `Tested-commit`, `merged-outside`.

  `spec-v2.md` stays as history with a "Superseded by spec-v3.md" line.
  Until then, **nobody edits `spec-v2.md` on #36 or #37**: the copies only
  differ by appended comments, so in-order merges resolve them with no
  conflict. Commit this plan and both review files as they are. From now
  on, reviews go in their own `review-<agent>.md` files.
  Proof: the merged tree has one `spec-v2.md`, and #38's PR body lists
  each correction.
- **F29.** CLAUDE.md, lanes invariant: "A lane operation in flight owns its
  lane. If the card closes meanwhile, the operation's callback retires the
  lane, never the close." (#36)
- **F30.** The lane prompt gains two lines: "`.venv` and `node_modules`
  here are links to the main checkout's copies. An install changes them for
  every lane." and F4's push line. P1: a check on the prompt text. (#36)
- **F32.** No code change. The warning is right: a laned agent should not
  edit the main checkout. The process rule in F28 (reviews in their own
  files, committed through a lane) removes the legitimate cases.
- **F33.** Write `.scratch/agent-lanes/issues/01-other-providers.md`
  (Codex, Gemini and Grok: their Step 0 runs and a provider-specific
  revive), and fix the Decisions text in v3.
- **F34.** `docs/agents/integration.md` and the README "Agent lanes"
  section get two lines: the integrator needs `PowerShell(git:*)` as well
  as `Bash(git:*)` allowed, and Claude Code keeps auto-memory per main
  checkout, so all lanes of a repo share it. (#38)
- **F35, live GitHub check (#38 gate, D4).** Use a private throwaway repo
  under `MolnarMario`, cloned into a scratch folder (never a real project
  folder). Drive it the way Agent 49's live check did (real Claude, haiku,
  `real_profile()` like the e2e test), with the real `gh`. Scenarios:
  1. submit, AWAITING, Approve, MERGED, with `--match-head-commit` seen in
     the audit line;
  2. submit the same lane again: a new `integrate/...-<id6>` branch (F2);
  3. a non-README commit pushed after `Tested-commit`: NEEDS_YOU (F5);
  4. merge a PR by hand on github.com: NEEDS_YOU `merged-outside` (F1);
  5. retarget a PR's base on github.com: Approve refuses (F3).

  Put a transcript excerpt and the audit lines in the PR body, then delete
  the repo.
- **F36.** Accepted for v1. Record it in spec v3 under Decisions.
- **F37.** PR B, per D1.
- **F38.** No CI. The real-claude e2e can't run on hosted CI anyway. Proof
  is local, by the P1-P5 rules. Write
  `.scratch/agent-lanes/issues/02-ci-quick-suite.md` (`--quick` on a
  Windows runner) as a follow-up.
- **F39.** See the merge plan.
- **F40.** #37: the switch tooltip gains "Lane warnings stop at once;
  existing lanes stay." Issues `03-trust-lanes-folder-once.md` (it writes to
  the user's Claude config, so it needs its own design) and
  `04-onedrive-warning.md` are enhancements, not defects.

---

## Merge plan

### Versions (monotonic, nothing below the current main)

| Order | PR | Branch | Version |
|---|---|---|---|
| 1 | A, new | `fix/pty-width` | 0.23.1 |
| 2 | #35 | `feat/board-rotation` | 0.23.1 -> **0.23.2** (renumbered) |
| 3 | B, new (D1) | `feat/count-and-clone` | 0.24.0 |
| 4 | #36 | `feat/agent-lanes` | 0.25.0 |
| 5 | #37 | `feat/lane-awareness` | 0.26.0 |
| 6 | #38 | `feat/integration-queue` | 0.26.1 + 0.27.0 |

If D2 leaves PR A unfinished, #35 merges first and keeps 0.23.1, and A takes
the next free number when it lands.

### Rules for every step

- One writer per worktree. The shared checkout stays untouched until
  step 7.
- Bring a branch up to date with `git merge origin/main` (or with its stack
  parent). Never rebase or force-push: the PRs are public and stacked.
- Merge with `gh pr merge <n> --merge`. **Never squash**: a squash orphans
  the stacked branches, and the queue's lane cleanup relies on ancestry.
- Merge gate:
  - every F of that PR meets P1-P3;
  - the full suite including e2e gives `0 failed` on the tip after the main
    merge (P4);
  - `__version__` equals the top CHANGELOG section, sections are in
    descending order, and the README count comes from the RESULT line;
  - the PR body is filled from the template.
- The user merges, or an agent merges at the user's explicit request.
- Retarget before you delete. GitHub closes a PR whose base branch is
  deleted, and `delete_branch_on_merge` is off. Delete branches only in
  step 7.
- Log each start, merge and finding with `log_activity`.

### Step 0. Prep (now)

- Copy the shared checkout's `.scratch/agent-lanes/*.md` (including both
  reviews and this plan) and `docs/ISOLATED-AGENT-TASKS-PLAN.md` to
  `C:\Users\Mario\ai-hive\.aihive\backup-agent-lanes-20261003\`. That folder
  is gitignored. They are the only copies of the reviews and this plan.
- Post on the board: the `spec-v2.md` freeze, and which agent owns which
  worktree.

### Step 1. PR A, the `pty width` fix

- Worktree `..\ai-hive-ptywidth`, new branch `fix/pty-width` off
  `origin/main`. Fix F7. Version 0.23.1 with a CHANGELOG entry ("Terminals
  restored after a reopen keep the width they started at").
- Gate, then merge.

### Step 2. #35

- In `..\ai-hive-board`: merge `origin/main`, renumber to 0.23.2 (its
  CHANGELOG section goes on top), then F22, F23, F25 and F27 (D3).
- Run the full suite with e2e (F26). Gate, then merge.
- After it ships, the first `log_activity` moves about 220 board entries to
  `board-archive.md`. That is expected.

### Step 3. PR B, Count stepper + GitHub clone (D1)

- `feat/new-agent-count` is checked out in the shared checkout, so use a
  new branch: worktree `..\ai-hive-count`, branch `feat/count-and-clone` at
  `bd2707d` (it already holds `8bb4321`).
- `git cherry-pick 9e9f435`. Resolve the README count conflict.
- `git merge origin/main`. CHANGELOG order: 0.24.0, 0.23.2, 0.23.1, 0.23.0.
- Full suite. Open the PR, gate, merge. Local `main` (`8bb4321`, 1 ahead)
  can then fast-forward.

### Step 4. #36

- Fixes in `..\ai-hive-lanes` can start now: F4, F9, F10, F16 (audit
  part), F17 (env), F21, F29, F30.
- Before the gate, merge `origin/main`. `8bb4321` and `bd2707d` are the
  same commits, so they merge with no conflict. `9e9f435` and its
  cherry-pick conflict only on the README count. CHANGELOG order: 0.25.0
  on top.
- Update the PR body: the extra features are gone now that PR B merged,
  and the fix table is in. Gate, merge. Keep the branch.

### Step 5. #37

- Run `gh pr edit 37 --base main`.
- Merge `feat/agent-lanes` into `feat/lane-awareness` as soon as #36's
  fixes land, so the #37 fixes build on them. After #36 merges, merge
  `origin/main` too.
- Expected conflicts:
  - `lanes.py`, the `worktree add` lines: keep `--no-track` and
    `copy_worktree_includes`;
  - `roster_row` / `_render_roster`: keep #35's whitespace collapse and the
    D3 fallback, then add the lane columns;
  - `tests/smoke_test.py` `MODULES`: keep both `board` and `lanes`;
  - CHANGELOG: 0.26.0 on top.
- Fixes: F11, F12, F13, F15, F16 (the `.worktreeinclude` part), F17 (fetch
  thread), F19, F24, F40 (tooltip).
- Rerun Step 0 check 2 live (P5): PostToolUse and UserPromptSubmit context
  still reach the model, and the first task is still delivered.
- Gate, merge.

### Step 6. #38

- Run `gh pr edit 38 --base main`.
- The #38-only fixes can start now in `..\ai-hive-lanes3`, since they touch
  code only #38 has: F1, F2, F3, F5, F6, F8, F14, F18, F20, F34. Merge
  `feat/lane-awareness` into it, then `origin/main` after #37 merges.
- Last commit: F28 (spec v3, plus this plan and both reviews), F31, F33,
  and the issue files.
- Run F35 live. Gate, merge.

### Step 7. Land it on the user's machine

1. Confirm that the shared checkout's dirty files still equal `9e9f435`
   (the check from "What was verified", item 5). Then run
   `git stash push -m "clone fix, identical to 9e9f435"`. That is
   reversible; nothing is discarded.
2. Move the untracked `.scratch/agent-lanes/` and
   `docs/ISOLATED-AGENT-TASKS-PLAN.md` aside (they are now tracked, and git
   refuses a checkout over untracked files), into the step 0 backup folder.
3. `git switch main` and `git pull --ff-only`. Check that
   `app/__init__.py` says 0.27.0 and `git status` is clean, so the in-app
   updater works again.
4. Restart AI Hive.
5. Remove the worktrees `..\ai-hive-board`, `-lanes`, `-lanes2`, `-lanes3`,
   `-ptywidth` and `-count`. First list any junctions in each one
   (`cmd /c dir /AL /S <dir>`) and unlink them with `rmdir`. Never use
   `--force` (the CLAUDE.md invariant).
6. Delete the merged branches, local and remote.
7. Acceptance run in the real app (the spec's manual check):
   - turn on Agent lanes, and have two agents edit `main_window.py` in this
     repo: both chips go amber, and each agent gets exactly one overlap
     notice;
   - submit both lanes and approve the first PR: the second is integrated
     against the new `main` before it reaches AWAITING;
   - switch off: the hooks go quiet within one poll (F12).

### What can run in parallel

| Now | After #36's fixes land | After each merge |
|---|---|---|
| PR A, #35 round, PR B, #36 fixes, #38-only fixes | #37 fixes | merge `origin/main` up the stack, rerun P4 |

Merges themselves stay strictly serial, in the order of the version table.
