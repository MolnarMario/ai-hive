# Agent Lanes v3: a private worktree per agent, plus an approved merge queue

Status: ready-for-human (Phases 0 to 3 built; Phase 4 and other providers are
follow-ups)

Author: Agent 52, 2026-10-03, from Agent 47's v2. Supersedes `spec-v2.md`,
which stays as history: its `## Comments` hold each phase's implementation
notes, and `review-feedback.md` (R1), `review-agent50.md` (A50) and
`fix-and-merge-plan.md` hold the reviews and the fix plan. v3 folds all of
those into the body, so the body is current again. See "Changes from v2" at
the bottom.

Code: Phase 0 shipped in 0.23.2 (#35), Phase 1 in 0.25.0 (#36), Phase 2 in
0.26.0 (#37), the launch-delivery fix in 0.26.1 and Phase 3 in 0.27.0 (#38).

Reviews go in their own `review-<agent>.md` file next to this one, never
appended here: a lane workflow appending to one shared file conflicts on
every merge (that is how v2 forked into three copies).

## The ask, verbatim

> Having multiple agents inside the same workspace working at the same time is
> one of the main advantages of the app. BUT sometimes this means that they
> will step on "eachother's toes" and have to be cognizant of what the other
> agent(s) are working on, so they have synergy. I think they also use the
> coordination board, which helps, but i think this also takes up tokens and
> time to account for. What kind of feature would you plan for them to work on
> their things without stepping on each other's toes while they implement
> their things and commit their work. and for each PR of course, i would have
> another agent scan the commits and check/fix commit conflicts before making
> PRs. note that this feature should be one that increases code quality
> instead of decreasing it.

Follow-up, 2026-10-02: "add a toggle in the Options dropdown where this
feature can be turned on and off reliably. by default it should be off".

## Decisions

Made by the user:
- The whole feature sits behind an **"Agent lanes" switch in Options, OFF by
  default**. While it is off, AI Hive behaves as it did before lanes.
- While the switch is on, lanes are **on by default** for new Claude agents
  in a git workspace whose folder is the repository root. A workspace inside
  a larger repository starts with the box unticked (its lane would copy the
  whole repository). The New Agent dialog's checkbox opts out per agent.
- **The integrator owns the version, CHANGELOG and README count**, once per PR.
  Lanes don't touch them.
- 2026-10-03, on the fix plan: the roster's task column shows the live AI
  title first, like the card (D3); squash-merged lanes get a user-only
  removal (D5); a private throwaway GitHub repo may be used for the live
  queue check (D4).

Made by the authors, on the user's instruction to pick the best practice:
- **Merging needs the user's approval.** The integrator opens the PR and
  reports. It never merges. The user clicks "Approve merge", and AI Hive
  itself runs a guarded merge. A green suite is necessary, not sufficient:
  the base can move after the test run, flaky failures are easy to explain
  away, and an agent should not judge its own exceptions. Auto-merge stays
  possible later as its own opt-in switch (Phase 4).
- **Lanes are Claude only.** Claude is the only provider whose resume path,
  system prompt and hooks have passed the Step 0 checks. Codex, Gemini and
  Grok keep running in the workspace folder until their own checks pass:
  `issues/01-other-providers.md`.
- **Strictly one queue item at a time.** An item awaiting approval or needing
  the user holds the line. Accepted for v1, knowing the cost: the integrator
  sits idle while an item waits, so the user is the bottleneck. In exchange,
  no PR is ever built on a base that is about to move.
- **Board rotation shipped first, on its own (Phase 0).** It is the cheapest
  token win and needs no lanes.

## Context

Before lanes every agent in a workspace shared **one checkout, one index and
one branch**. The board's own history showed the cost: about 20 entries read
"only my hunks staged" or "built through a PRIVATE index"; "a peer ran
`git reset --hard` mid-edit and wiped my working-tree changes"; version,
CHANGELOG and README-count collisions (two branches both claimed 0.22.3); and
a 79 KB board every agent was told to read before substantial work.

What lanes change:
- Each agent works in its own git worktree ("lane"), so agents can't share an
  index or working tree, or reset each other's work.
- AI Hive tells an agent about an overlap only when one actually exists,
  instead of every agent reading the whole board.
- Lanes merge one at a time through an integrator agent. Each change is
  merged and tested against the base it actually lands on, and the user
  approves each merge.

Why not file claims or locks: they rely on agents remembering to declare
things, they go stale, they cost tokens on every task, and they still leave
everyone on one index. Worktrees separate the agents physically.

## Master switch

**Replaced by `spec-v4-lane-scopes.md`.** The switch became "Lanes in every
workspace", each workspace got its own toggle, and both only set the New
Agent dialog's starting tick. The lane machinery now follows the lanes
themselves, so no switch pauses the queue or silences a lane. What follows
is history.

- A row in Options > Agents: "⎇  Agent lanes", a `ToggleSwitch` with a
  tooltip saying what ON and OFF mean, ending "Lane warnings stop at once;
  existing lanes stay." OFF by default.
- Persisted as `ui.agent_lanes`, an additive key read with a default (no
  `SESSION_VERSION` bump).
- `MainWindow.lanes_enabled()` is **the only gate**.

| | Switch OFF | Switch ON |
|---|---|---|
| New Agent dialog | No lane checkbox, and no git command runs at all. | "Own lane" checkbox (see Decisions). |
| `git worktree add` | Never runs. | Runs for each new laned agent. |
| Lane service, overlap hooks, notices | Stopped. `lanes.json` says disabled, retried until the write lands; hooks also treat a `lanes.json` older than 45 s as off. | Running. |
| Integration queue | Paused. Items are kept, nothing is delivered, read or merged. | Delivers to an idle integrator. |
| Agents that already have a lane | Keep running and resuming in their lane. | Same. |
| Repair on load, retire on close, revive | Still work. | Same. |

The switch controls **new** lanes and the optional machinery, never the
safety of lanes that already exist. It takes effect at once; a running agent
gets or loses its lane hooks at its next launch (Claude snapshots hooks at
start), but the `lanes.json` gate silences them immediately.

## Where shared state lives

All AI Hive state for a workspace lives in **`ws.board.dir`**, the `.aihive\`
folder of the workspace's own folder (the main checkout), always addressed by
**absolute path**: `board.md`, `board-archive.md`, `roster.md`, `lanes.json`,
`notices\<agent_uid>.jsonl`, `seen\<agent_uid>.json`. Nothing resolves
`.aihive` against an agent's working folder, and nothing is written inside a
lane. `create_lane` adds `.aihive/` to the shared `info/exclude` as a safety
net for repos that don't gitignore it.

## Lane identity

- **Folder:** `<parent>\<repo>.lanes\<slug>-<uid6>\` (`<uid6>`: the first 6
  hex chars of `spec.uid`). **Branch:** `hive/<slug>-<uid6>`.
- Both are recorded in `AgentSpec.lane` (`{root, branch, base, repo}`) when the
  lane is created and **never recomputed** from the agent's name.
- **Collisions fail, they never reuse** (`LANE-FAIL exists`).
- **No upstream.** `worktree add --no-track -b`. Leaving out `--track` is not
  enough: from a remote-tracking start point git sets the upstream itself
  (`branch.autoSetupMerge`), and with `origin/main` as upstream `git status`
  and a bare `git push` in the lane suggest `git push origin HEAD:main`, a
  push that skips every review. A repair unsets a base upstream left by an
  older build. The lane prompt says never to push the lane branch to the
  base.
- **Only where the folder exists.** If the worktree doesn't contain the
  workspace folder (it is untracked or ignored), the fresh lane is removed
  again and the agent starts in the workspace folder (`LANE-FAIL location`).

## Lane lifetime

- **Live:** from creation until the card is closed (running, stopped,
  crashed, across restarts). The lane path is fixed; a missing folder is
  repaired in place on load, never swapped for the workspace folder.
  Why the path is fixed: `claude --resume <id>` would find the conversation
  from any folder (Step 0 check 1), but every AI Hive transcript reader
  (titles, context usage, the model chip, session sync, the picker) looks a
  conversation up by the agent's cwd, and an agent resumed elsewhere would
  edit that checkout while its history says it is in its lane.
- **Retired:** closing the card retires the agent:
  - it waits for the agent's processes to exit; if one is still running in
    the lane, nothing is touched (`LANE-KEEP reason=busy`) and the retire is
    retried 3 times, 30 s apart;
  - clean, its head and its branch in the base, and no edited
    `.worktreeinclude` copy: the lane is removed (`LANE-REMOVE`, listing the
    ignored files it took);
  - otherwise it is kept, with a non-modal "Agent 5's lane has 3 unmerged
    commits and 2 modified files" (or "local files changed: .env") and
    **Keep** / **Open folder**. No delete button for unmerged work.
  - A lane whose pinned commit was squash- or rebase-merged through the
    queue is "unmerged" by ancestry forever. Its notice (and its live chip)
    offers **Remove lane (merged as #N)**, a user click only: AI Hive re-reads
    PR #N, requires it MERGED, the item's own, containing the commit, and the
    lane clean with its head still at that commit, then removes it.
- **Revived:** resuming a retired lane's conversation from the New Agent
  dialog's picker repairs the lane at the identical path (recreating the
  branch from the base if it was merged and deleted) and resumes there. A
  recreated branch is told to the AGENT through a lane notice, not only the
  card log. A failed revive never starts the agent in the workspace folder.
- **An operation in flight owns its lane.** If the card closes while its
  lane is being created or repaired, the operation's callback retires it,
  never the close.

## Serial git operations per repo

- **One `LaneOps`**, owned by `MainWindow`, runs every mutating lane
  operation: one FIFO and one worker per repo, keyed by the git common dir,
  read from the `.git` entries (no git on the GUI thread). Different repos
  run in parallel.
- **The base fetch** (non-exclusive) runs in the repo's second FIFO, so a
  fetch hanging on the network never delays a lane create.
- **The folder lock** (`lock_for(repo)`): exclusive jobs hold it, and the
  poller takes it around each lane read, because Windows won't delete a
  folder that is any process's working directory.
- Read-only polling runs off the queue with `--no-optional-locks`.
- Lane git runs with `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`.

## Phase 0: Smaller board (shipped, 0.23.2)

- `append_activity` keeps the newest 40 entries in `board.md` and moves older
  ones to `board-archive.md` (append-only, written first; a failed board
  write truncates it back). The note says to read the archive "only if you
  need history".
- The roster's task column shows the live AI title, else the assigned task.
- An unchanged roster is not rewritten. A board that lost its roster markers
  or its log heading keeps every line.

## Phase 1: Lanes, isolation (shipped, 0.25.0)

`app/lanes.py` (Qt-free) and `app/lane_ops.py`: create, repair, status,
remove, retire, revive, as in "Lane identity" and "Lane lifetime". Junctions
for the ignored `.venv`, `venv` and `node_modules`, unlinked before any
removal (Step 0 check 3). `AgentSpec.lane` is persisted (`SESSION_VERSION`
5). The lane prompt: commit in your lane, never edit other worktrees or the
main checkout, never commit or push to the base, the junction folders are
shared with every lane, and leave the version, CHANGELOG and README count to
the integrator.

## Phase 2: Awareness without the token cost (shipped, 0.26.0)

- **`app/lane_service.py`** polls every lane about every 15 s and after each
  turn end, and fetches the base every 5 minutes. It finds overlaps (two lanes
  changed one file; a lane and its base changed one file) and real conflicts
  (`git merge-tree --write-tree`, git 2.38+). The integrator's lane is
  nobody's peer, and a committed overlap between two lanes where one contains
  the other's commits is not reported.
- **`lanes.json`** is rewritten on every poll with a fresh `ts`.
- **Card chip** `⎇ ↑2 ±3`: neutral, amber on an overlap, red on a conflict.
  Its menu: Open lane folder; **Update lane to origin/main (fast-forward)**
  for a lane with nothing of its own, never while the agent is working (this
  replaces v2's "Clean up lane": a live lane can't be removed under its
  agent); Submit to integrator (Phase 3).
- **Overlap hook** (PostToolUse on the edit tools) and **lane notices**
  (UserPromptSubmit), in a separate settings file given only to laned Claude
  agents while the switch is on. Each is said once per round of work: the
  key is `(file, peer, level, my fork, peer's fork)`, the forks being each
  lane's merge-base with its base, so a peer that landed its work and moved
  on overlapping the same file again is news again. The hook also warns on
  edits outside the agent's own lane.
- **Laned agents read `.aihive/roster.md`** (the roster table alone) instead
  of the board; the Read tool returns a whole file, so "skim the top of the
  board" saved nothing. The roster gains Lane, Ahead/Dirty and Touching
  columns. Agents without a lane keep reading the board.
- **`.worktreeinclude`** copies ignored local files into new lanes, walking
  with pathspec `:(exclude)` for the junction folders (`--exclude=` would
  list every file under them).
- Activity panel Lanes section, File Map rooted at the repo, event-log rows.

## Launch delivery fix (shipped, 0.26.1)

The finding during Phase 2 was right, the suspected cause was not. Readiness
is fine: the task text is typed into a live box. What broke is the Enter: in
a fresh git folder Claude's event loop stalls right after its first frame
(570 ms measured), so the text and the Enter sent 350 ms later reached Claude
as one stdin chunk, and a CR inside a chunk counts as part of the paste.
`_write_task_to_pty` now sends the Enter once Claude has drawn the typed
text, never sooner than 350 ms, with a 5 s fallback.

## Phase 3: Approved integration queue (shipped, 0.27.0)

Requires a GitHub remote and a logged-in `gh`.

- **Integrator:** "Make integrator" on a laned Claude agent's card, one per
  workspace (`Workspace.integrator_uid`). It needs `Bash(git:*)` and
  `PowerShell(git:*)` allowed. Every lane of a repo, the integrator's
  included, shares the main checkout's Claude auto-memory.
- **Queue** (`Workspace.integration_queue`, `SESSION_VERSION` 6): only a user
  click submits a lane, pinning its current commit. One open item per lane.
  Each item has a `reason` when it needs the user and a `suite_sha`.
- **Brief:** pinned commit, `git log` and diffstat (inside fenced blocks,
  control characters stripped: lane-authored text must not end the brief's
  bracketed paste or submit anything), merge checks against the base and the
  other queued lanes, the test command and the checklist from the BASE
  branch's `docs/agents/integration.md`.
- **Integrate branch per item:** `integrate/<lane tail>-<item id6>`. One per
  lane let a resubmitted lane's turn end find the previous item's merged PR.
- **Checklist order:** branch, merge the base, version and CHANGELOG, review,
  commit, the full suite on that commit, the README count only, push and
  open the PR with `Tested-commit: <sha>` and the RESULT line, never merge.
- **Reading the PR** after the integrator's turn: AWAITING only when the PR
  is from the item's own branch (not a fork), into its base, contains the
  pinned commit, and its head is the Tested-commit or adds README.md only on
  top. Never MERGED from reading: a PR merged on github.com is NEEDS_YOU
  `merged-outside` until the user's **Mark merged**, which checks with GitHub.
- **Turn ends recheck** only INTEGRATING items and NEEDS_YOU `no-pr`; every
  other NEEDS_YOU waits for the user's Recheck.
- **Approve merge** (user only), run by AI Hive on LaneOps: the PR is still
  this item's and open; its head is the tested head; the base fetch worked
  and GitHub's base head (`gh api`) equals the fetched one; the base is an
  ancestor of the tested head (else back to the integrator with the short
  re-merge brief, same item); then `gh pr merge --merge --match-head-commit`;
  MERGED only when GitHub then says so. A window remains between the base
  check and the merge; branch protection's "Require branches to be up to
  date" closes it.
- **Keeping idle lanes fresh:** a clean lane with no commits of its own whose
  agent is neither busy nor waiting is fast-forwarded when its base moves,
  and the agent gets a lane notice.

What AI Hive can't prove: that the suite ran on the Tested-commit. That line
is the integrator's claim; the RESULT line is there for the user.

## Phase 4 (optional, later): auto-merge

A per-workspace switch, off by default, that lets AI Hive press "Approve
merge" itself when the suite had 0 failures. Only worth building once the
approval log shows how often the user approved without changes.

## Step 0 checks, with results (git 2.51.2, Claude Code 2.1.287/288, gh 2.89.0)

1. **Resume from a lane folder:** works. `--resume <id>` also works from any
   other folder, appending to the same transcript; the lane path stays fixed
   for AI Hive's own readers (see "Lane lifetime"). A fresh lane shows
   Claude's trust prompt once. `git worktree add` took about 70 ms.
2. **Hook context:** PostToolUse and UserPromptSubmit `additionalContext`
   both reach the model (transcript `hook_additional_context` attachments),
   and a UserPromptSubmit hook does not drop the first delivered task.
   Rerun live after the Phase 2 fixes: 8 of 8 checks.
3. **`git worktree remove` FOLLOWS junctions** and empties their targets (a
   lane's `.venv` junction wiped the real `.venv`). The pre-unlink is
   mandatory, and removal refuses while any directory link is left.
   CLAUDE.md invariant.
4. **`gh pr merge --match-head-commit`** exists.

## Verification

`tests/smoke/lanes.py`, `tests/smoke/board.py`: real temp repos in the sandbox
profile, stubbed workers, a fake `gh` that holds several PRs per branch. Every
review fix has a check that fails on the pre-fix code and a mutation check for
its guard (the PR bodies of #35 to #38 list them). The full suite, including
the real-claude e2e, runs before every merge.

Manual check in the real app (fix plan step 7): turn on Agent lanes; two
agents edit `main_window.py` in this repo, both chips go amber and each agent
gets exactly one overlap notice; submit both lanes, approve the first PR, and
the second is integrated against the new main before it reaches
awaiting-approval; switch off, and the hooks go quiet within one poll.

## Changes from v2

From the implementation notes (spec-v2 `## Comments`):
- Step 0 check 1's rationale corrected; check 3 failed and made the
  pre-unlink mandatory; check 2 and 4 passed.
- Phase 2's "lost first task" cause replaced by Agent 49's (the Enter, not
  readiness).
- "Clean up lane" became "Update lane (fast-forward)".
- Other providers deferred out of Phase 2 into `issues/01-other-providers.md`
  (v2's Decisions said Phase 2 would do them).
- The integrator's `PowerShell(git:*)` permission and the shared auto-memory
  moved from a comment into `docs/agents/integration.md` and the README.

From the reviews and the fix plan (F numbers from `fix-and-merge-plan.md`):
- F1 `merged-outside` and Mark merged; F2 an integrate branch per item; F3
  PR base, head and fork checks; F4 `--no-track` and `update-ref -d`; F5
  `Tested-commit` and the checklist order; F6 `reason`, and turn-end
  rechecks only for `no-pr`; F8 base fetch and `gh api` check; F9 no lane
  without a folder, nested workspaces unticked; F10 busy retire; F11 fork in
  the overlap key; F12 reliable switch-off and stale `lanes.json`; F13
  `roster.md`; F14 the integrator is nobody's peer; F15 revive notice to the
  agent; F16 ignored files listed, edited local files keep the lane; F17 GCM
  never prompts, fetch in its own FIFO; F18 brief sanitizing; F19 no Update
  lane while working; F20 squash-merged removal; F21 no git on the GUI
  thread; F22 to F25 and F27 the Phase 0 fixes; F29, F30, F34 docs and
  prompt lines; F36 one item at a time accepted.
- Follow-up issues: `issues/01-other-providers.md`,
  `issues/02-ci-quick-suite.md`, `issues/03-trust-lanes-folder-once.md`,
  `issues/04-onedrive-warning.md`.
