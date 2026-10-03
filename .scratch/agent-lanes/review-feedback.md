# Agent Lanes v2 PR Review

Reviewed 2026-10-03 against PRs [#35](https://github.com/MolnarMario/ai-hive/pull/35), [#36](https://github.com/MolnarMario/ai-hive/pull/36), [#37](https://github.com/MolnarMario/ai-hive/pull/37), and [#38](https://github.com/MolnarMario/ai-hive/pull/38), plus the implementation notes in `spec-v2.md`.

## Findings

### High: PR #38 can advance the queue after a merge without AI Hive approval

In `app/integration.py`, `read_outcome()` returns `MERGED` when the PR is already merged (`read_outcome`, around lines 490–502). This check runs after the integrator's turn, before the user clicks **Approve merge**. Because the integrator can run `gh`, it could merge the PR itself; AI Hive would accept that state and advance the queue, contrary to the user-approval rule.

**Recommendation:** Treat a PR found already merged outside the AI Hive approval action as `needs-you` and pause queue advancement for user resolution.

### High: PR #38 does not verify the PR target branch

`pr_view()` requests `number,state,headRefOid,url` (`app/integration.py`, around line 258). Neither `read_outcome()` nor `approve_merge()` verifies that the PR's `baseRefName` matches the queue item's expected base. A PR with the correct head but the wrong target branch could be approved and merged into the wrong branch.

**Recommendation:** Include `baseRefName` in the PR lookup and refuse approval when it differs from the queue item's base branch.

## Merge order and scope

- The PRs are stacked: #36 → #37 → #38. PR #37 targets `feat/agent-lanes`; PR #38 targets `feat/lane-awareness`. Merge in dependency order, retargeting each stacked PR to `main` as its parent lands.
- PR #36 also contains GitHub cloning and the New Agent Count stepper, as its description notes. Confirm those additions are intended in this feature PR.
- PR #35 is independent, but it overlaps with lane changes in roster rendering and smoke-test registration. The spec notes these conflicts; retain both sets of changes when resolving them.
- Lane support for Codex, Gemini, and Grok remains deferred, as does optional auto-merge (Phase 4). This matches the implementation notes, but provider-wide lane support is still follow-up work.

## Verification and review coverage

- The PRs have no submitted GitHub reviews, inline comments, or conversation comments.
- GitHub reported no status checks for PRs #35–#38 at review time.
- PR #35's description says its full suite with real-Claude e2e had not yet been run. Later PR descriptions report e2e passing, with the known `pty width` failure also present on clean `main`.
- I reviewed the implementation and available PR metadata but did not run the test suites.

The lane isolation, switch-off behavior, junction-safe cleanup, serialized Git operations, and hook gating have specific implementation notes and reported test coverage. The two high-severity integration-queue findings above are the merge blockers identified in this review.
