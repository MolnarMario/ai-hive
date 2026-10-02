# Isolated Agent Tasks and Reviewed Integration

## Summary

Give each assigned task a clean Git worktree and branch, so agents can work and commit independently without changing the workspace checkout. Add lightweight, app-managed file claims and an integration queue with an independent agent review before a user-approved merge.

## Implementation Changes

- When a task is assigned, have its agent propose the files or modules it expects to touch. AI Hive records those claims and warns when another active task claims the same area. Claims guide coordination; they are advisory, since agents can discover new files or change scope.
- Create a task-specific worktree from the workspace’s current base branch. Launch the agent there, and keep its commits separate from the user’s checkout and other agents’ branches.
- When an agent submits its work, run configured project checks and ask a separate reviewer agent to inspect the full diff for correctness, quality, scope, and integration conflicts. If fixes are needed, let the reviewer prepare them in an integration worktree for review.
- Show the task branch, changed files, check results, and reviewer findings in a queue. Require the user to approve integration. Keep the task branch until the change is integrated or discarded.
- Enable this workflow only for clean Git workspaces. Explain when a workspace needs to be committed or cleaned before task isolation is available. Keep the existing shared-folder workflow for workspaces not using isolated tasks.

## Tests and Acceptance

- Verify that each task receives a distinct worktree and branch, and that agent edits and commits do not modify the workspace checkout or another task.
- Verify overlapping claims produce a warning, and that actual changed files are compared with the claims during review.
- Verify failed checks, reviewer findings, and merge conflicts block readiness and appear in the queue; reviewer fixes remain reviewable before integration.
- Verify user approval is required to integrate, and task worktrees remain recoverable until integration or discard.
- Verify dirty and non-Git workspaces are not silently moved into the isolated workflow.

## Assumptions

- “PR-ready” means a locally reviewed branch and diff; AI Hive does not push branches or create GitHub PRs in this version.
- File claims are inferred by the assigned agent as part of starting its task, rather than requiring a separate planning-agent call. Claims help agents coordinate but do not guarantee that overlapping edits are safe.
- Project checks come from workspace configuration; the reviewer reports when no checks are configured.
- The user remains the final decision maker for merge and any reviewer-prepared conflict fix.
