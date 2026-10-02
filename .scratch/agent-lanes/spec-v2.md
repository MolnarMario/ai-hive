# Agent Lanes v2: a private worktree per agent, plus an approved merge queue

Status: needs-triage

Author: Agent 47, 2026-10-02. Supersedes `spec.md` (v1), which is kept for
history. v2 folds in the first review (see "Changes from v1" at the bottom).
Code so far: Phase 0 in PR #35 (`feat/board-rotation`); the master switch and
Phase 1 on `feat/agent-lanes`. Phases 2 to 4 are still a plan. Implementation
notes and Step 0 results are under `## Comments` at the bottom.

Note: an earlier draft by another agent, `docs/ISOLATED-AGENT-TASKS-PLAN.md`,
covers the same ground (task worktrees, advisory file claims, reviewer agent,
user-approved integration). v2 shares its approval rule and keeps lanes over
claims for the reasons under "Context".

Reviewers: append your findings under `## Comments` at the bottom. Each
finding should name the section it is about, say what is wrong or risky, and
say what you would do instead. Please don't edit the plan body; the author
folds accepted findings in.

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
  default**. While it is off, AI Hive behaves exactly as it does today.
- While the switch is on, lanes are **on by default** for new agents that
  support them, in a git workspace. The New Agent dialog has a checkbox to opt
  out per agent.
- **The integrator owns the version, CHANGELOG and README count**, once per PR.
  Lanes don't touch them. CLAUDE.md and `docs/agents/` get updated to say so.

Made by the author, on the user's instruction to pick the best practice:
- **Merging needs the user's approval.** The integrator opens the PR and
  reports. It never merges. The user clicks "Approve merge", and AI Hive
  itself runs a guarded merge. This replaces v1's "merges when the suite is
  green". A green suite is necessary, not sufficient: the base can move after
  the test run, flaky failures are easy to explain away, and an agent should
  not be the one judging its own exceptions. The parallel plan in
  `docs/ISOLATED-AGENT-TASKS-PLAN.md` reached the same conclusion.
  Auto-merge stays possible later as its own opt-in switch (Phase 4).
- **Phase 1 lanes are Claude only.** Claude is the only provider whose resume
  path, system prompt and hooks are covered by the Step 0 checks. Codex,
  Gemini and Grok keep running in the main checkout until their own checks
  pass (Phase 2).
- **Board rotation ships first, on its own (Phase 0).** It is the cheapest
  token win and needs no lanes.

## Context

Today every agent in a workspace shares **one checkout, one index and one
branch**. The board's own history (`.aihive/board.md`) shows what that costs:

- About 20 entries read "only my hunks staged" or "built through a PRIVATE index"
  (the shared index was being reset mid-edit) or "verified standalone in a
  throwaway worktree". Agents spend real effort keeping their changes apart.
- "A peer ran `git reset --hard` mid-edit and wiped my working-tree changes."
  "Accidentally bundled into a peer commit." "The files could not be separated."
  "Committed on their behalf." "The shared checkout was switched to the new branch."
- Version, CHANGELOG and README-count collisions happened more than once
  (two branches both claimed 0.22.3).
- The board is about 79 KB, and every agent is told to read it before
  substantial work. That is the token and time cost the user noticed.

The workarounds agents improvised (`../ai-hive-glyph`, `../ai-hive-stamp`,
merging main into a PR branch and running the suite) were the right instinct.
This feature makes that the default, with AI Hive owning the plumbing.

**What changes:**
- Each agent works in its own git worktree ("lane"), so agents can't share an
  index or working tree, or reset each other's work.
- AI Hive tells an agent about an overlap only when one actually exists,
  instead of every agent reading the whole board.
- Lanes merge one at a time through an integrator agent. Each change is
  merged and tested against the base it actually lands on, and the user
  approves each merge.

Why not file claims or locks: they rely on agents remembering to declare
things, they go stale, they cost tokens on every task, and they still leave
everyone on one index. Worktrees separate the agents physically, so nobody has
to remember a rule.

## Master switch

**Already built**, uncommitted, in worktree `..\ai-hive-lanes` on branch
`feat/agent-lanes` (cut from `feat/new-agent-count`@bd2707d, because Phase 1
builds on the Count stepper). It is the first piece of Phase 1 and ships with
it, never alone: on its own the switch would gate nothing.
- A row in a new Options section "Agents": "⎇  Agent lanes", a
  `ToggleSwitch` like every other row, with a tooltip saying what ON and OFF
  mean. OFF by default.
- Persisted as `ui.agent_lanes`, an additive key read with a default, saved on
  the debounced timer, so no `SESSION_VERSION` bump (same as `auto_update`).
- `MainWindow.lanes_enabled()` is **the only gate**. Every lane entry point
  calls it; nothing reads `_agent_lanes` directly.
- Smoke check `test_agent_lanes_switch`: default off, a click emits and
  saves, `set_*` reflects without emitting, it round-trips a real
  close/reopen, and a session that predates it loads as off.

What the switch governs, so "off" is reliable:

| | Switch OFF | Switch ON |
|---|---|---|
| New Agent dialog | No lane checkbox. Agents start in the workspace folder, exactly as today. | "Own lane" checkbox, checked by default for Claude in a git workspace. |
| `git worktree add` | Never runs. | Runs for each new laned agent. |
| Lane service, overlap hooks, notices (Phase 2) | Not started; hook env vars unset, so an installed hook exits silently. | Running. |
| Integration queue (Phase 3) | Paused. Queued items are kept, nothing is delivered. | Delivers to an idle integrator. |
| Agents that already have a lane | Keep running and resuming in their lane. | Same. |
| Repair on load, Keep/Remove on close, resume of a lane conversation | Still work. | Same. |

The bottom two rows are deliberate. An existing laned agent's conversation is
keyed by its lane folder, so moving it back to the workspace folder would lose
the conversation, and refusing to repair or clean up its lane would strand it.
The switch controls **new** lanes and the optional machinery, never the
safety of lanes that already exist. Turning it on or off takes effect at once
and needs no restart.

## Where shared state lives

All AI Hive state for a workspace lives in **one place: `ws.board.dir`**, the
`.aihive\` folder of the workspace's own folder (the main checkout), always
addressed by **absolute path**:

- `board.md`, `board-archive.md` (Phase 0)
- `lanes.json`, `notices\<agent_uid>.jsonl`, `seen\<agent_uid>.json` (Phase 2)

Nothing resolves `.aihive` against an agent's working folder. This already
holds for the board today: `WorkspaceManager._apply_coordination` passes
`ws.board.dir` to `--add-dir` and the absolute `ws.board.path` into the system
prompt, and `log_activity` writes through the GUI bridge scoped by
`AIHIVE_WS`. Phase 2 hooks get absolute paths through env vars
(`AIHIVE_LANES_INDEX`, `AIHIVE_NOTICES`), never relative ones.

`.aihive/` is gitignored in this repo, so a lane never gets a copy of the
board at all, stale or otherwise. `create_lane` also adds `.aihive/` to
`.git/info/exclude`. That file lives in the shared git dir, so it covers every
worktree. Its only job is a safety net for other repos that don't gitignore
`.aihive/`: a stray `.aihive\` inside a lane can never be committed.

A check in `tests/smoke/lanes.py` asserts that every path the lane code writes
or hands to an agent starts with `ws.board.dir` or the lane root, never a
relative `.aihive`.

## Lane identity

- **Folder:** `<parent>\<repo>.lanes\<slug>-<uid6>\`, where `<uid6>` is the
  first 6 hex chars of `spec.uid`.
- **Branch:** `hive/<slug>-<uid6>`, the same suffix. Two agents with the same
  name, in one workspace or in two workspaces on one repo, get different
  branches.
- Both are recorded in `AgentSpec.lane` when the lane is created and **never
  recomputed** from the agent's name. A later rename (only ever by the user)
  doesn't move the folder or the branch.
- **Collisions fail, they never reuse.** If the folder or the branch already
  exists when a *new* lane is created, `create_lane` fails with
  `LANE-FAIL exists <what>`, touches nothing, and the agent starts in the
  workspace folder with a visible notice. Attaching a new agent to an existing
  branch could hand it someone else's work.
- Only `repair_lane`, for an agent whose `spec.lane` already names that branch
  (or a revived lane, see below), attaches to an existing branch on purpose.

## Lane lifetime

Claude keys a conversation by the folder it started in
(`transcripts.encode_project_dir`), so a lane's path must never change while
anything might resume it.

- **Live:** from creation until the card is closed. This covers running,
  stopped, a crash, and an app restart. The lane path is fixed. If the folder
  goes missing, `load_session_dict` queues a repair at the same path before
  the existing "fall back to project_path" line.
- **Retired:** closing the card retires the agent. Then:
  - clean, and its head is in the base: the lane is removed (worktree and
    branch), `LANE-REMOVE`;
  - otherwise it is kept, with "Agent 5's lane has 3 unmerged commits and 2
    modified files. The lane folder is kept." and **Keep** (default) / **Open
    folder**. There is no delete button for unmerged work. `LANE-KEEP`.
  - Either way the conversation itself stays in `~/.claude/projects`.
- **Revived:** resuming a retired lane's conversation. The past-conversation
  picker (`AddTerminalDialog.resume_combo`, today "from this workspace's
  folder" only) also lists lane conversations:
  - Discovery needs no new persisted state. `encode_project_dir` maps every
    non-alphanumeric char to `-`, so every lane's transcript folder starts
    with `encode_project_dir(<repo>.lanes) + "-"`. Each transcript records its
    own `cwd`, which gives the exact lane path back.
  - A conversation whose lane belongs to a live agent is hidden, the same
    one-conversation-one-agent rule as today.
  - Picking one sets `spec.lane` from that path and runs `repair_lane` at the
    **identical path**: it checks out the recorded branch if it still exists,
    or recreates the same branch name from the current base if it was deleted
    after merging. Then `--resume <id>` from that folder, `LANE-REVIVE`.
  - If the repair fails, the agent is **not** started in the workspace folder
    (`--resume` would not find the conversation there). The dialog shows the
    error instead.
  - Picking a conversation from the workspace folder itself works as today:
    no lane, Count pinned to 1.
- Reviving works with the switch off too: it restores existing work, it
  doesn't create new isolation.

## Serial git operations per repo

`git worktree add`, `worktree remove`, `worktree prune`, `branch -d` and the
Phase 3 `merge --ff-only` all take locks in the shared git dir. Parallel calls
fail on `index.lock` or ref locks.

- **One `LaneOps` object**, owned by `MainWindow` for the app's lifetime, runs
  every mutating lane operation. It keeps **one FIFO queue and one worker
  thread per repo**, keyed by
  `os.path.normcase(git rev-parse --path-format=absolute --git-common-dir)`.
  So two dialog submits, or two workspaces on the same repo, share a queue.
  Different repos run in parallel.
- Callers enqueue `(op, args, callback)`. The worker emits a Qt signal with
  the result (the `app/usage_poll.py` pattern), and a public `deliver()` lets
  tests run the queue synchronously.
- Read-only polling (Phase 2) bypasses the queue but uses
  `git --no-optional-locks`, so `status` never writes `index.lock`.
- If a card is closed before its lane finishes creating, the creation still
  completes, and the new lane (clean, no commits of its own) is then removed
  through the same queue.
- Check: two workspaces on one repo submit at once; the fake runner records
  start and end times and no two operations on that repo overlap. Two repos do
  overlap.

## Phase 0: Smaller board, PR 0 (independent of lanes)

- `coordination.append_activity` keeps the last 40 entries in `board.md` and
  moves older ones to `board-archive.md` in `ws.board.dir` (append-only; a
  line-count check proves nothing is lost). The rotation runs under the same
  serialization `log_activity` already uses.
- `TerminalAgent.roster_row` falls back to the live AI title when no task is
  assigned (today the column is almost always "-").
- The system prompt is unchanged. Agents still read the board; it is now a
  few thousand tokens instead of tens of thousands.

## Phase 1: Lanes (isolation), PR 1

Scope: worktree creation, resume, and safe retention. Nothing else.

**Master switch:** done, see above. Every item below is gated by
`lanes_enabled()` except repair, close and revive.

**New `app/lanes.py`** (Qt-free, stdlib, git through `subprocess` with
`CREATE_NO_WINDOW` and timeouts, following `coordination.git_changed_files`):
- `repo_root(path)`, `common_dir(path)`, and `default_base(repo)`, which
  reads `refs/remotes/origin/HEAD` and falls back to `main` or `master`.
- `plan_lane(repo, project_path, slug, uid)`. Returns folder, branch and the
  agent's cwd inside the lane: if the workspace is a subfolder of the repo,
  the cwd is the lane root plus that same relative path.
- `create_lane(plan, start)`. Fails on an existing folder or branch (see
  "Lane identity"), then runs `git worktree add -b <branch> <path> <start>`,
  where start is `origin/<base>` (local `<base>` with no remote). Then:
  - junctions (`mklink /J`) for `.venv`, `venv` and `node_modules` when they
    exist at the repo root and are ignored, so
    `.venv\Scripts\python.exe tests\smoke_test.py` works unchanged in a lane;
  - adds `.aihive/` to `.git/info/exclude` if missing.
- `repair_lane(lane)`. `git worktree prune`, then re-adds the same path from
  the recorded branch, recreating that branch from the base if it is gone.
- `lane_status(lane, base)`: head sha, ahead, behind, dirty files. Enough for
  the close prompt.
- `remove_lane(lane, base)`. Refuses unless the lane is clean **and**
  `merge-base --is-ancestor head base`. Removes its own junctions first with
  `os.rmdir` (that unlinks only the junction, never the target), then runs
  `git worktree remove` and `git branch -d`. It **never** uses `--force` or
  `-D`.

**Wiring:**
- `AddTerminalDialog` (`app/widgets/main_window.py`): the "Own lane (git
  worktree)" checkbox exists only while the switch is on. Checked by default
  for Claude in a git workspace, disabled with a tooltip reason otherwise.
  Picking a past conversation from the workspace folder unticks and disables
  it; picking a lane conversation shows "Resumes in its own lane". With Count
  N you get N lanes.
- `MainWindow._on_add_terminal_clicked`: for each spec, set `spec.cwd` to the
  planned lane cwd, `add_terminal(..., autostart=False)`, and enqueue the
  creation on `LaneOps`. On success, start the agent. On failure, put the cwd
  back to `ws.project_path`, start the agent anyway, show a visible notice and
  write `LANE-FAIL`. A lane problem never blocks creating an agent (revive is
  the one exception, see above).
- **Persistence** (the CLAUDE.md three-part persisted-field rule):
  - New `AgentSpec.lane` dict `{root, branch, base, repo}`, handled in
    `to_dict`/`from_dict` (`app/process_worker.py`); an empty dict means no
    lane. It is set before the agent is added, so the add's own save carries
    it, and cleared through the manager (which emits `dirty`) when the lane is
    removed.
  - Add it to the `WorkspaceManager._agent_dict_safe` fallback.
  - Bump `SESSION_VERSION` to 5; a v4 session loads with no lanes.
  - `load_session_dict` queues a repair for a laned agent whose cwd is
    missing, before the "fall back to project_path" line.
  - Audit lines through `manager.audit`: `LANE-CREATE`, `LANE-REPAIR`,
    `LANE-REVIVE`, `LANE-KEEP`, `LANE-REMOVE`, `LANE-FAIL`.
- **Closing a card** and **reviving**: as in "Lane lifetime".
- **System prompt** (`coordination.system_prompt_text`,
  `WorkspaceManager._apply_coordination`): laned agents get a short extra
  section. The board instruction itself is unchanged until Phase 2.
  - "You work in your own worktree `<root>` on `<branch>` (base
    `origin/main`). Commit there."
  - "Never touch other worktrees, the main checkout, or `<base>`."
  - "Don't bump the version, CHANGELOG or README count; the integrator does
    that."
- Free side effect: every laned agent is the only agent in its folder, which
  makes `session_sync`'s filesystem fallback unambiguous for it.

## Phase 2: Awareness without the token cost, PR 2

- **Other providers:** Codex, Gemini and Grok get lanes once their Step 0
  checks pass (resume from a lane folder; board reachable by absolute path,
  since Gemini today learns about the board from project context files).
- **`.worktreeinclude`** (gitignore syntax): gitignored files to copy into a
  new lane, such as a local `.env`.
- **New `app/lane_service.py`** (a QObject owned by MainWindow, like
  `OrchestratorBridge`), started only while the switch is on:
  - Polls `lane_status` for every lane every ~15 s with
    `--no-optional-locks`, on one thread per repo with an in-flight guard, and
    on demand after an agent's turn ends (`reply_finished` /
    `note_turn_ended`).
  - Fetches `origin/<base>` every ~5 min (through `LaneOps`, since fetch
    writes refs).
  - When HEADs change, runs pairwise
    `git merge-tree --write-tree --name-only --no-messages` across lanes and
    against the base. With git older than 2.38 it falls back to file overlap
    only.
  - Emits `lanesChanged(ws_id, snapshot)` and writes `lanes.json` in
    `ws.board.dir`: repo-relative path -> [{agent, branch, state:
    dirty|committed|conflicts}].
  - Lane state is transient and never marks the session dirty.
- **Card header chip** (`app/widgets/terminal_card.py`): branch, ahead count,
  dirty count. Neutral, amber on an overlap, red on a conflict, tooltip lists
  files and peers. Right-click: Open lane folder, Submit to integrator
  (Phase 3), Clean up lane (only when clean and merged).
- **Activity panel**: a Lanes section, and "Changed files (git)" becomes
  per-lane.
- **Agent/File Map**: `lanes.to_repo_path` keeps the tree rooted at the repo
  instead of the common parent folder. Opening a file opens the lane's copy.
- **Overlap hook** (`app/session_hook.py`): a `PostToolUse` matcher for
  `Edit|Write|MultiEdit|NotebookEdit`.
  - Reads `AIHIVE_LANE_ROOT` and `AIHIVE_LANES_INDEX` (absolute), maps the
    edited path to a repo path, looks up other owners.
  - Only on a real overlap does it print
    `hookSpecificOutput.additionalContext`, for example "Agent 5
    (hive/agent-5-3fa9c1) also changed app/widgets/main_window.py
    (uncommitted). Keep your change there small and local; the integrator
    merges both."
  - Warns once per (file, peer, peer-state), tracked in
    `ws.board.dir\seen\<agent_uid>.json`. Exits 0 silently on any error or
    when the env vars are absent.
  - `MainWindow._arm_agent_mcp` sets the env vars per run, only while the
    switch is on, never persisted.
  - The SessionStart matcher stays `resume|clear|compact`: adding `startup`
    makes fresh agents drop their first task (a known past incident).
- **Lane notices** (`UserPromptSubmit` hook): the service appends per-agent
  events to `ws.board.dir\notices\<agent_uid>.jsonl` ("main changed app/x.py
  which you also changed; merge origin/main now while the conflict is
  small"). The hook injects only unread ones on the agent's next prompt.
- **Board, for laned agents only:** the system prompt changes "BEFORE starting
  substantial work, read it" to "skim the roster at the top". Agents without a
  lane keep the full instruction, because the board is still their only
  awareness. `_render_roster` gains Lane, Ahead/Dirty and Touching columns.
- **Event log** (`app/event_hub.py`): lane created, overlap first seen,
  conflict first seen.

## Phase 3: Approved integration queue, PR 3

Requires a GitHub remote and a logged-in `gh`. Without them the "Submit to
integrator" item is disabled with the reason in its tooltip.

- **Integrator role:**
  - A card right-click toggle, "Make integrator" (one per workspace),
    persisted as `Workspace.integrator_uid` (keyed on `spec.uid`).
  - The integrator has its own lane and the integrator prompt section.
- **Queue:**
  - Persisted as `Workspace.integration_queue`: a list of
    `{lane_uid, sha, pr, tested_sha, ts, state}`, where state is
    queued | integrating | awaiting-approval | merging | merged | needs-you |
    skipped.
  - Also persisted: `Workspace.base_branch`.
  - All three go through `to_session_dict` and `load_session_dict` and
    `_touch` on change, with the `SESSION_VERSION` bump this phase brings.
  - Only a user click adds an item ("Submit to integrator"), and it pins the
    lane's **current sha**. Later commits stay on the lane and show as "+2
    since submit".
- **Brief:** `lanes.integration_brief(...)`, about 30 lines: agent, lane
  branch, pinned sha, base; `git log --oneline base..sha` and the diffstat;
  the precomputed merge-tree result against the base and other queued lanes;
  the test command; the checklist from `docs/agents/integration.md` or a
  built-in default. The integrator needs nothing from the board.
- **Integrator checklist** (`docs/agents/integration.md`, for this repo):
  1. Branch `integrate/<lane-branch-tail>` from the pinned sha.
  2. Merge `origin/main` and resolve conflicts. Never rebase or force-push.
  3. Run the full `tests\smoke_test.py`, including e2e.
  4. Run `/code-review`.
  5. Bump `__version__`, add the CHANGELOG section and fix the README count.
  6. Push and run `gh pr create`. The PR body lists the test result. Any
     failure is listed with whether it also fails on the base tip; the
     integrator never decides that a failure is acceptable.
  7. **Never run `gh pr merge`.** Stop and report.
- **Delivery and advancement:**
  - When the integrator is idle and the head item is queued, AI Hive sends the
    brief with `TerminalAgent.deliver_task`.
  - After the integrator's turn ends, AI Hive reads the PR with
    `gh pr view <branch> --json number,state,headRefOid,mergeable`. An open PR
    whose head is a descendant of the pinned sha moves the item to
    **awaiting-approval** and records `tested_sha = headRefOid`. No PR, or a
    PR that doesn't contain the pinned sha, moves it to **needs-you**. Either
    way the "?" chime and an event-log row tell the user.
  - The queue never skips a failed item by itself.
- **Approve merge** (a button on the queue row, user only). AI Hive, not an
  agent, then runs, through `LaneOps`:
  1. `git fetch`; if the PR head is no longer `tested_sha`, refuse: the PR
     changed after it was tested.
  2. If `origin/<base>` is not an ancestor of `tested_sha`, the base moved
     after the test run. Don't merge: the item goes back to **integrating**
     and the integrator gets a short "base moved, merge it again and rerun the
     suite" brief. This continues the item the user already submitted; it is
     not a new assignment.
  3. `gh pr merge <n> --merge --match-head-commit <tested_sha>`. The
     `--match-head-commit` guard makes GitHub refuse if the head moved in the
     meantime.
  4. Read the PR state back. **merged** only when `state == MERGED`. This
     works for merge, squash and rebase merges alike, unlike v1's "pinned sha
     is an ancestor of the base" check, which never succeeds after a squash.
  5. Deliver the next queued item.
- **Keeping idle lanes fresh:** when a lane is clean, its agent is not busy,
  and it has 0 commits ahead, `LaneOps` runs `git merge --ff-only
  origin/<base>` in it, and the agent gets a notice. A lane with its own
  commits is never merged automatically, only notified.
- **Doc updates:**
  - `CLAUDE.md` Conventions: lanes don't bump the version, CHANGELOG or README
    count; the integrator does, once per PR.
  - `CLAUDE.md` Invariants: never pass `--force` to `worktree remove` or `-D`
    to `branch`; remove junctions before removing a worktree; a lane path never
    changes for an agent; every lane git mutation goes through `LaneOps`; only
    a user click adds to the queue or approves a merge; agents never run
    `gh pr merge`.
  - README gets an "Agent lanes" section, and the Layout list gets `lanes.py`
    and `lane_service.py`.

## Phase 4 (optional, later): auto-merge

A per-workspace switch, off by default, that lets AI Hive press "Approve
merge" itself when the suite had 0 failures. The same guarded merge runs.
Only worth building after Phase 3 has a track record: the approval log shows
how often the user approved without changes.

## Step 0 checks (before Phase 1 code)

1. Claude: `--resume <id>` from the lane cwd works, and a fresh lane shows the
   trust prompt once. Also confirm that `--resume <id>` from a *different*
   folder than the conversation started in fails, which is why revive repairs
   at the identical path. Measure how long `git worktree add` takes here.
2. Confirm `PostToolUse` `additionalContext` and `UserPromptSubmit` context
   injection on the installed CLI, and that a `UserPromptSubmit` hook does not
   delay or drop the first delivered task (cf. the SessionStart `startup`
   incident). Needed for Phase 2.
3. Confirm that `git worktree remove` on a lane containing a `.venv` junction
   leaves the real `.venv` intact, even without our pre-unlink. Keep the
   pre-unlink anyway as a second safeguard.
4. Confirm the installed `gh` supports `gh pr merge --match-head-commit`.
   Needed for Phase 3.

## Verification

New `tests/smoke/lanes.py`. It builds real temp git repos in the sandbox
profile and never touches the real repo. All agents are stubbed workers, per
the AI-launch guard.
- Master switch: off by default; with it off, a git workspace plus a Claude
  agent runs **no** git command at all (injected runner records nothing) and
  the dialog shows no lane checkbox; flipping it needs no restart; with it off,
  an existing laned agent still loads, repairs and closes safely.
- `lanes.py`:
  - create (branch and start point; cwd mapping for a subfolder workspace);
  - two agents with the same name get different folders and branches;
  - an existing folder or branch fails with `LANE-FAIL` and changes nothing;
  - junctions; `.git/info/exclude` entry;
  - `remove_lane` refuses dirty, refuses unmerged, and leaves the junction
    target intact;
  - repair recreates a deleted lane at the same path, and recreates a deleted
    branch from the base;
  - no path handed to an agent or written by lane code is a relative
    `.aihive`.
- `LaneOps`: same-repo operations never overlap, across two workspaces;
  different repos may.
- Persistence:
  - the lane round-trips `to_session_dict`/`load_session_dict`;
  - the degraded record keeps `lane`;
  - a missing lane folder triggers repair, not the project_path fallback;
  - v4 sessions load with no lanes.
- Lifetime: closing a clean merged lane removes it; closing an unmerged one
  keeps it; the picker lists a retired lane's conversation, hides a live one's,
  and picking it repairs at the identical path; a failed revive does not start
  the agent in the workspace folder.
- Dialog: the checkbox is absent with the switch off; on, it defaults on for
  git + Claude, off for shells, other providers and non-git folders. Count 3
  gives 3 distinct lanes, created serially. A lane failure still starts the
  agent in the workspace folder.
- Phase 2 hook (`session_hook` as a subprocess, fed stdin payloads):
  additionalContext only on a real overlap; de-duplicated; silent with no env
  vars; never non-zero on garbage input.
- Phase 0 board: rotation conserves the line count; the roster renders the
  AI-title fallback.
- Phase 3 queue (with a fake `gh`):
  - only the manager API enqueues or approves;
  - the brief goes to an idle integrator via `deliver_task`;
  - an open PR moves the item to awaiting-approval, never to merged;
  - approval refuses when the head changed, and re-briefs when the base moved;
  - a squash-merged PR is detected as merged;
  - it persists across restart, and pauses with the switch off.
- Full run: `.venv\Scripts\python.exe tests\smoke_test.py`, including e2e.
  e2e runs in a non-git scratch folder, so it stays unaffected.
- Manual check in the real app: switch on; two agents in this repo edit
  `main_window.py`; (Phase 2) both chips go amber and each gets one overlap
  notice; (Phase 3) submit both, approve the first PR, and the second is
  integrated against the new main before it reaches awaiting-approval.

**Prerequisite for Phase 3:** `gh` must be logged in with an account that can
merge (the board notes `mariomolnarAM` lacked access; `MolnarMario` worked).

## What reviewers should push hardest on

1. **Lane location.** Sibling `<repo>.lanes\` (one trust prompt per agent) vs
   nested `.aihive\lanes\` (inherits trust if Claude walks parent folders, but
   pollutes jest/vite/eslint/tsc in JS repos). Is there a better third option?
2. **Switch off with live lanes.** Is "existing lanes keep working, only new
   lanes and the machinery stop" what a user expects from "off"?
3. **Revive.** Recreating a deleted branch from the current base gives the
   resumed agent a fresh branch, not its old commits (those are in the base
   already, since only merged lanes are removed). Is that clear enough to the
   resumed agent, or should it get a notice?
4. **ff-only refresh of idle lanes.** Can changing files under an idle agent
   confuse it mid-conversation, even though no work can be lost?
5. **Invariant fit.** AI Hive delivering user-queued briefs (and the
   "base moved" follow-up) to the integrator, modelled on scheduled send. Does
   that stay inside "agents cannot spawn or retask each other"?
6. **Hook cost.** A Python start-up on every Edit/Write in every laned Claude
   agent. Is ~100 ms acceptable, or should the overlap check move elsewhere?

## Changes from v1

Review received 2026-10-02 (pasted by the user). Each finding and what v2 does:

1. **"The coordination board may stop being shared."** The mechanism was
   wrong: the board is addressed by absolute path and is gitignored, so lanes
   never get a copy. The underlying point was right: v1 never said so, and its
   Phase 2 files (`lanes.json`, `notices/`) had no stated root. New section
   "Where shared state lives"; every file is under `ws.board.dir`, by
   absolute path; the `.git/info/exclude` entry's purpose is stated; a test
   guards against relative `.aihive` paths.
2. **"The branch name isn't as unique as the lane folder."** Accepted. Branch
   is `hive/<slug>-<uid6>`, recorded once, never recomputed; collisions fail
   and never reuse. New section "Lane identity".
3. **"Close and resume behavior needs reconciling."** Accepted, and it was
   worse than stated: the picker lists only the workspace folder's
   conversations, so a laned agent's conversation became unreachable as soon
   as its card closed. New section "Lane lifetime" defines live, retired and
   revived; the picker finds lane conversations by prefix and revives the lane
   at the identical path.
4. **"The automatic merge bar is too permissive."** Accepted. The integrator
   never merges; the user approves; AI Hive runs a guarded merge
   (`--match-head-commit`, base-moved check). The "failures identical on the
   base tip" exception is gone. Also fixed a v1 bug the review didn't catch:
   advancement read "pinned sha is an ancestor of the base", which never
   succeeds after a squash merge; v2 reads the PR state instead.
5. **"Phase 1 already has substantial scope."** Accepted. Board rotation is
   now Phase 0, shipped alone. Phase 1 is creation, resume and safe retention
   for Claude only. `.worktreeinclude`, File Map normalisation, other
   providers and the board-instruction rewording moved to Phase 2.
6. **"Confirm the service actually enforces serialization."** Accepted. New
   section "Serial git operations per repo": one `LaneOps` with a FIFO per
   repo keyed by the git common dir, used by every mutating lane operation,
   not only creation, with a test that same-repo operations never overlap.

Added on the user's follow-up: the "Master switch" section (Options, default
off), and the "Decisions" section now records who decided what.

## Comments

### Phase 0 implemented (Agent Smith, 2026-10-02)

Not a review finding, just implementation notes for whoever builds on it.

**Where:** worktree `..\ai-hive-board`, branch `feat/board-rotation`, cut
from `origin/main`@391fb13 (Phase 0 needs nothing from the Count stepper).
Committed f747c80, pushed, PR #35 (open, not merged). Version 0.23.1 with a
matching CHANGELOG section,
README count 2160. Quick suite 2144/1; the one failure,
`test_pty_width_at_launch`, fails the same on a clean `origin/main`. The
full suite (real-claude e2e) has not been run yet; run it before merging.
Dry run on a copy of this repo's real board: the next note takes `board.md`
from 79.8 KB to 12.2 KB (262 entries -> 40 kept, 223 archived, all
conserved).

**What exists now (names later phases can rely on):**
- `coordination.LOG_KEEP = 40`, `ARCHIVE_FILENAME = "board-archive.md"`,
  `WorkspaceBoard.archive_path` (always `ws.board.dir`, absolute, next to
  `board.md`, so it already satisfies "Where shared state lives").
- `coordination.split_log(text, keep)`: pure function, returns
  `(board text, moved lines)`. An entry is a top-level `- ` line plus the
  lines under it, so a hand-written multi-line entry moves whole.
- Rotation runs only inside `append_activity` (`_write_rotated`), which every
  caller reaches on the GUI thread, so it shares log_activity's
  serialization. `update_roster` never rotates.
- Write order: archive append (fsync) first, then the atomic board replace.
  If the board replace fails, the archive is truncated back to its old size.
  If the archive can't be written, the rotation is skipped and the entry is
  still appended (the board just stays longer until the next note).
- The board's log preamble gains one line on its first rotation,
  `_Older entries are moved to board-archive.md in this folder._`; new boards
  get it in the scaffold. The system prompt is unchanged, as specified.
- Roster: `roster_row()["task"]` is `current_task or _ai_title`. Note this is
  the opposite precedence of the card header's `summary()` (title first);
  the spec asked for task first. `WorkspaceManager._wire_agent` now connects
  `summary_changed` to `_recompute` (roster rewrite, never `dirty`), so a
  title change reaches the board without waiting for a busy/idle edge.
- `_render_roster` now collapses whitespace in the task cell, so a multi-line
  delivered task no longer breaks the table row. Phase 2's extra columns
  (Lane, Ahead/Dirty, Touching) go in `roster_row` + `_render_roster`.
- Checks: new module `tests/smoke/board.py` (`test_board_rotation`,
  `test_board_roster_task`, 31 checks), registered in `MODULES` in
  `tests/smoke_test.py`. Phase 1's `tests/smoke/lanes.py` on
  `feat/agent-lanes` also edits that import line and `MODULES`: expect a
  trivial conflict there, keep both modules.

**Things to know:**
- Version collision ahead: `feat/new-agent-count` holds 0.24.0 (unmerged)
  and `feat/agent-lanes` is cut from it. Whichever of 0.23.1 / 0.24.0 merges
  second renumbers and moves its CHANGELOG section on top.
- The first log_activity after this ships moves ~220 entries of this repo's
  board into the archive in one go. Expected, nothing is lost.
- `event_hub.poll_boards` reads `read_log_tail(60)`, but the board now holds
  at most 40. It diffs against the previous poll, so this only matters if
  more than 40 notes land between two polls (it would miss event-log rows,
  never board entries).
- Pre-existing, NOT fixed (out of Phase 0 scope): `update_roster` rebuilds
  the scaffold and DROPS the whole activity log when the roster markers are
  missing (its comment says "keep any log", the code doesn't), and
  `append_activity` replaces the whole board with the scaffold when
  `## Activity log` is missing. Both only trigger when someone hand-edits
  board.md badly. With rotation the archive keeps older entries, but the
  last 40 would still go. Worth a small fix of its own.

### Phase 1 implemented (Agent of Darkness, 2026-10-02)

Implementation notes, plus three Step 0 results that correct the plan body.

**Where:** worktree `..\ai-hive-lanes`, branch `feat/agent-lanes` (off
`feat/new-agent-count`@bd2707d), on top of Agent 47's master switch.
Committed as 3856a86 (lanes, 0.25.0), followed by a commit carrying Agent 47's
GitHub-clone default-branch fix (taken from the shared checkout, where it was
uncommitted) and one adding these specs to git. Pushed, with a PR to main (see
the board for its number). That PR also carries 8bb4321 (GitHub clone) and
bd2707d (Count stepper, 0.24.0), which were never on origin/main. Quick
suite: 2268 pass, 1 fail (`pty width`, the known failure that also happens on
clean main). The real-claude e2e passed 15/0. Its first run failed once at
"accepting trust reaches the real prompt" (the cursor stayed on "No, exit");
a rerun passed, and so did the base commit bd2707d.

**Step 0 results (git 2.51.2, Claude Code 2.1.287, gh 2.89.0):**
1. **Check 3 failed, and it was serious.** `git worktree remove` without
   `--force` FOLLOWS a directory junction inside the worktree and deletes
   everything in its target. In a scratch repo, a lane with a `.venv`
   junction emptied the main checkout's real `.venv`. So the pre-unlink is
   mandatory, not a second safeguard. `remove_lane` unlinks the lane's own
   junctions with `os.rmdir`, then refuses (`LaneError("links")`) while any
   junction or directory symlink is left anywhere in the lane. A mutation run
   with both guards disabled made `test_lanes_core` fail: the link's target
   was deleted. CLAUDE.md now has this as an invariant.
2. **Check 1: the rationale for the revive rule was wrong; the rule still
   stands.** `claude -p --resume <id>` from a DIFFERENT folder (the main
   checkout, and also an unrelated non-git folder) found the conversation and
   kept appending to the original transcript file in the lane's project
   folder, writing the new cwd on the new records. So it is not true that
   `--resume` fails elsewhere. The lane path still must not change, for
   AI Hive's own reasons: every transcript reader (titles, context usage,
   model chip, session sync, the picker) looks a conversation up by the
   agent's cwd, and an agent resumed in the main checkout would edit it while
   its history says it is in its lane. Discovery therefore reads each
   transcript's FIRST cwd (where it started), not a later one.
   `git worktree add` took ~70 ms. I did not test the interactive trust
   prompt: print mode skips it, so expect Claude's trust dialog once per new
   lane folder unless a parent folder is trusted.
3. **Check 4:** gh 2.89.0 has `gh pr merge --match-head-commit`. Check 2 is
   Phase 2 and was not run.

**Names later phases can rely on:**
- `app/lanes.py` (Qt-free): `find_repo_root` (a `.git` walk, no git, so the
  dialog runs zero git commands with the switch off), `common_dir`,
  `default_base`, `start_ref`, `plan_lane` (pure), `create_lane`,
  `repair_lane -> (lane, recreated_branch)`, `lane_status -> LaneStatus`
  (`exists, head, ahead, behind, dirty, merged, describe()`), `remove_lane`,
  `retire_lane(lane, pids)` (status + remove in ONE queued op, so nothing
  interleaves), `lane_conversations(repo, exclude_roots, exclude_ids)`,
  `clean_lane`. `lanes.RUNNER` is the injectable git runner.
- `app/lane_ops.py`: `LaneOps.submit(repo, fn, *args, callback=, label=)`,
  `key_for`, `drain()` for tests. It is `MainWindow.lane_ops`.
- `TerminalAgent.hold_start(reason)` / `release_start(run)`: start, restart
  and a waking keystroke are all deferred while a lane is being created or
  repaired.
- `WorkspaceManager.set_agent_lane`, `clear_agent_lane` (both `_touch`, both
  re-apply coordination), `take_lane_repairs()`, `LANES_SESSION_VERSION = 5`.
- `coordination.system_prompt_text(..., lane=)` appends the lane section.
- `MainWindow._close_agent`, `_create_lane`, `_revive_lane`,
  `_start_lane_repairs`, `_retire_lane`, `_lane_boxes` (open notices).

**Choices the plan did not spell out:**
- Planning is pure, so no git runs on the GUI thread. The base is read on the
  worker by `create_lane`, and the record gets it after creation (dirty), so
  the prompt shows the real base by the time the agent starts.
- `worktree add --track`: the lane branch's upstream is the base, so
  `branch -d` checks "merged into the base" instead of the main checkout's
  HEAD (often a feature branch, which would refuse a merged lane).
- If a card is closed while its lane op is in flight, that op's callback
  retires the lane, not the close. That way a create that failed on a
  collision never touches a folder it doesn't own.
- `retire_lane` waits (up to 10 s) for the closed agent's processes to exit.
  Windows won't delete a folder that is a working directory, and a
  half-deleted worktree can no longer be removed cleanly.
- If a repair on load fails, the agent stays held with a notice ("close the
  card to retire it"); it never falls back to the workspace folder. If a
  revive fails, its card is removed and a non-modal message says why.
- Deleting a workspace retires its laned agents' lanes like closed cards.
- The "lane kept" notice is non-modal: Keep (default) and Open folder, with
  no delete button.
- The junctions point at the main checkout's copies, so an install run in a
  lane writes there, and an editable install imports the main checkout's
  code. Documented in the module docstring; worth surfacing in Phase 2.

**Tests** (`tests/smoke/lanes.py`, 4 new tests, 119 checks on real temp repos
with stubbed workers): `test_lanes_core`, `test_lane_ops_serial`,
`test_lanes_persistence`, `test_lanes_window`. The window test covers: switch
off runs no git and shows no checkbox; checkbox defaults by kind, repo and
resume choice; Count 3 gives 3 lanes and nobody starts before their lane
exists; a collision still starts the agent in the workspace folder; close
removes an empty lane and keeps one with a commit; the picker lists a retired
lane's conversation, hides a live one's, and revives at the identical path
with `--resume`; a failed revive starts nothing; with the switch off, an
existing lane still loads, repairs and retires.

**Not done (Phase 2 and later, as planned):** card chip, overlap hooks,
notices, roster columns, event-log rows, other providers,
`.worktreeinclude`, File Map normalization.

**For whoever builds Phase 2 or 3 on this:**
- Read the `app/lanes.py` module docstring first. It records the Step 0
  findings next to the code they constrain.
- Any new code that deletes or moves a lane folder must go through
  `remove_lane` (or at least `unlink_junctions` + `directory_links`), and
  through `lane_ops`. Never `shutil.rmtree` a lane and never pass `--force`.
  The tests use `_drop_lane_folder`, which unlinks the junctions first.
- The lane record's `base` is a branch NAME (`main`). The ref a lane
  branches from and is compared against comes from `lanes.start_ref`:
  `origin/main` when the remote has it, else the local branch. Phase 3's
  "base moved" check should use the same function.
- Lane branches are created with `--track`, so their upstream is the base.
  `git status` in a lane reports ahead/behind origin/main, and `git branch -d`
  accepts a lane merged there. Side effect for Phase 3: with
  `push.default=simple`, a bare `git push` from a lane is refused (the branch
  names differ). The integrator must push with
  `git push -u origin hive/<...>`; after that the upstream is the pushed
  branch.
- `lane_status` is read-only (`status` runs with `--no-optional-locks`), so
  Phase 2's 15 s poller can call it directly, off the queue, as the spec
  intends. Everything that writes refs (fetch, ff-only refresh) goes through
  `lane_ops`.
- A lane create, repair or revive that is in flight owns the lane. If its card
  is closed meanwhile, `MainWindow._close_agent` does NOT retire it; the
  operation's callback does (`_lane_pending`). Keep that rule for any new
  lane operation, or a failed create could remove a folder it doesn't own.
- Test helpers in `tests/smoke/lanes.py`: `_make_repo` (real repo with an
  origin, an ignored `.venv` holding a marker file, and an ignored `build/`),
  `_stub_starts` (records every PtyWorker start, because lane callbacks
  start agents and the real-AI guard fails the run otherwise), `_record_git`
  (wraps `lanes.RUNNER`), `_write_transcript` (a fixture conversation in the
  sandbox `~/.claude/projects`), and `win.lane_ops.drain()`.
- Version collision: PR #35 (Phase 0) claims 0.23.1, and this branch carries
  0.24.0 and 0.25.0. Whichever merges second renumbers and moves its
  CHANGELOG section to the top.
- These spec files are tracked on `feat/agent-lanes` from now on. The shared
  checkout still has untracked copies, and git will refuse to check out or
  merge this branch there until they are moved aside. Once it is merged,
  append review comments to the tracked copy.
