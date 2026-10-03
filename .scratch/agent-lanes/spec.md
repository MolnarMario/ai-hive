# Agent Lanes: a private worktree per agent, plus an integration queue

Status: superseded by `spec-v2.md` (2026-10-02). Kept for history.

Author: Agent 47, 2026-10-02. This is a plan only. No code has been written.
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

## Decisions already made by the user

- Lanes are **on by default** for new AI agents in a git workspace. The New
  Agent dialog has a checkbox to opt out.
- The integrator **opens the PR and merges it when the suite is green**, then
  moves on to the next queued lane.
- **The integrator owns the version, CHANGELOG and README count**, once per PR.
  Lanes don't touch them. CLAUDE.md and `docs/agents/` get updated to say so.

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
- The board is 275 lines (~34k tokens), and every agent is told to read it
  before substantial work. That is the token and time cost the user noticed.

The workarounds agents improvised (`../ai-hive-glyph`, `../ai-hive-stamp`,
merging main into a PR branch and running the suite) were the right instinct.
This feature makes that the default, with AI Hive owning the plumbing.

**What changes:**
- Each agent works in its own git worktree ("lane"), so agents can't share an
  index or working tree, or reset each other's work.
- AI Hive tells an agent about an overlap only when one actually exists,
  instead of every agent reading the whole board.
- Lanes merge one at a time through an integrator agent. Each change is
  merged and tested against the base it actually lands on, so a pair of
  changes that each pass alone but break together gets caught before main.

Why not file claims or locks: they rely on agents remembering to declare
things, they go stale, they cost tokens on every task, and they still leave
everyone on one index. Worktrees separate the agents physically, so nobody has
to remember a rule.

## How it works

1. **Lane = a stable folder + a branch.** A new agent gets
   `<parent>\<repo>.lanes\<agent-slug>-<uid4>\`, a worktree on branch
   `hive/<slug>` cut from `origin/<base>` (local `<base>` if there is no
   remote). The folder never changes for the agent's lifetime, because Claude
   keys its transcripts by cwd (`transcripts.encode_project_dir`) and moving it
   would break `--resume`. The branch moves on from task to task.
2. **Sibling folder, not nested.** A lane inside the repo would be picked up
   by jest, vite, eslint and tsc in the main checkout. The cost: Claude (and
   Codex/agy) show their folder-trust prompt once per new lane. The first task
   already waits behind that prompt (`tests/smoke/e2e.py` covers this), so
   nothing breaks; it's one click per agent. Auto-trust is a possible
   follow-up, not v1.
3. **Agents get facts, not homework.** The roster is built from git: branch,
   commits ahead/behind, dirty files, files touched, and the live AI title as
   the task. A Claude hook speaks up only when a file you just edited is also
   changed in another lane, or when the base moved under files you touched.
   Otherwise it adds nothing to the agent's context.
4. **Integration is a queue you fill.** Only a user click adds a lane to the
   queue ("Submit to integrator"). AI Hive gives the idle integrator the next
   brief and moves on once that lane's submitted commit is in
   `origin/<base>`. No agent can queue or retask another, so the CLAUDE.md "no
   orchestrator" invariant still holds.
5. **Merge, never rebase.** The integrator merges the base into
   `integrate/<slug>`. Lane history is never rewritten, so an agent that keeps
   committing after it submitted never loses anything.

## Phase 1: Lanes (isolation), PR 1

**New `app/lanes.py`** (Qt-free, stdlib, git through `subprocess` with
`CREATE_NO_WINDOW` and timeouts, following `coordination.git_changed_files`):
- `repo_root(path)` and `default_base(repo)`, which reads
  `refs/remotes/origin/HEAD` and falls back to `main` or `master`.
- `plan_lane(repo, project_path, slug)`. Returns the lane root plus the agent's
  cwd inside it: if the workspace is a subfolder of the repo, the cwd is the
  lane root plus that same relative path.
- `create_lane(...)`. Runs `git worktree add -b hive/<slug> <path> <start>`,
  then:
  - creates junctions (`mklink /J`) for `.venv`, `venv` and `node_modules`
    when they exist at the repo root and are ignored, so
    `.venv\Scripts\python.exe tests\smoke_test.py` works unchanged in a lane;
  - copies any gitignored files listed in a `.worktreeinclude` file at the
    repo root (gitignore syntax);
  - adds `.aihive/` to `.git/info/exclude` (local only, never committed).
- `repair_lane(lane)`. Runs `git worktree prune`, then re-adds the same path
  from its branch, so a lane folder that was deleted outside AI Hive comes
  back at the identical path and the conversation resumes.
- `lane_status(lane, base)`: head sha, ahead, behind, dirty files, files
  touched since the merge-base.
- `remove_lane(lane, base)`. Refuses unless the lane is clean **and**
  `merge-base --is-ancestor head base`. Removes its own junctions first with
  `os.rmdir` (that unlinks only the junction, never the target), then runs
  `git worktree remove` and `git branch -d`. It **never** uses `--force` or
  `-D`.
- `to_repo_path(path, lane_root, repo_root)`, for display normalisation.

**Wiring:**
- `AddTerminalDialog` (`app/widgets/main_window.py`): add an "Own lane (git
  worktree)" checkbox. It is checked by default when the workspace is a git
  repo and the kind is an AI kind, and disabled otherwise. Picking a past
  conversation unticks and disables it, because that conversation belongs to
  the root folder (the same reasoning that pins Count to 1 there). With Count N
  you get N lanes.
- `MainWindow._on_add_terminal_clicked`: for each spec, set `spec.cwd` to the
  planned lane cwd and call `add_terminal(..., autostart=False)`. Create the
  lanes **serially on one worker thread per repo**, because parallel
  `git worktree add` contends on ref locks. Use the `app/usage_poll.py`
  pattern: a daemon thread emits a Qt signal, and a public `deliver()` lets
  tests skip the thread. On success, start the agent. On failure, put the cwd
  back to `ws.project_path`, start the agent anyway, show a visible notice and
  write `LANE-FAIL` to the audit log. A lane problem never blocks creating an
  agent.
- **Persistence** (follows the CLAUDE.md three-part persisted-field rule):
  - New `AgentSpec.lane` dict `{root, branch, base, repo}`, handled in
    `to_dict`/`from_dict` (`app/process_worker.py`), with an empty dict
    meaning no lane.
  - Add it to the `WorkspaceManager._agent_dict_safe` fallback.
  - Bump `SESSION_VERSION` to 5; a missing field loads as no lane.
  - In `load_session_dict`, when a laned agent's cwd is missing, **queue a
    repair before** the existing "fall back to project_path" line. Falling
    back silently would run the agent in the shared checkout and lose its
    conversation.
  - Write `LANE-CREATE`, `LANE-REPAIR`, `LANE-KEEP`, `LANE-REMOVE` and
    `LANE-FAIL` lines through `manager.audit`.
- **Closing a card** (`page.closeRequested` → `remove_terminal`): when the
  lane is clean and merged, remove it silently. Otherwise show "Agent 5's lane
  has 3 unmerged commits and 2 modified files. The lane folder is kept." with
  **Keep** (the default) and **Open folder**. There is no delete button for
  unmerged work.
- **System prompt** (`coordination.system_prompt_text`,
  `WorkspaceManager._apply_coordination`): add a short lane section:
  - "You work in your own worktree `<root>` on `hive/<slug>` (base
    `origin/main`). Commit there."
  - "Never touch other worktrees, the main checkout, or `<base>`."
  - "Don't bump the version, CHANGELOG or README count; the integrator does
    that."
  - "AI Hive tells you when a file you edit is also changed in another lane."

  Also replace "BEFORE starting substantial work, read it" with "skim the
  roster at the top".
- **Agent/File Map** (`app/file_activity.py`,
  `app/widgets/agent_file_map.py`): normalise paths inside a lane with
  `to_repo_path`, so the tree stays rooted at the repo instead of the common
  parent folder. Opening a file still opens the lane's own copy.
- Free side effects, with no code: every laned agent is now the only agent in
  its folder. That makes `session_sync`'s filesystem fallback, Grok's
  `--continue` and Gemini's live-session sync unambiguous.

## Phase 2: Awareness without the token cost, PR 2

- **New `app/lane_service.py`** (a QObject owned by MainWindow, like
  `OrchestratorBridge`):
  - Polls `lane_status` for every lane every ~15 s, on one thread per repo
    with an in-flight guard. It also polls on demand after an agent's turn ends
    (`reply_finished` / `note_turn_ended`).
  - Fetches `origin/<base>` every ~5 min and when the integrator's turn ends.
  - When HEADs change, runs pairwise
    `git merge-tree --write-tree --name-only --no-messages` across lanes and
    against the base. Git here is 2.51; with git older than 2.38 it falls back
    to file overlap only.
  - Emits `lanesChanged(ws_id, snapshot)` and writes a small index to
    `.aihive/lanes.json`: repo-relative path → [{agent, branch, state:
    dirty|committed|conflicts}].
  - Lane state is transient and never marks the session dirty.
- **Card header chip** (`app/widgets/terminal_card.py`): `⎇ hive/agent-3 +2 ●3`.
  - Neutral, amber when it overlaps another lane, red when it conflicts with
    the base or another lane. The tooltip lists which files and with whom.
  - Right-click: Open lane folder, Submit to integrator (Phase 3), Clean up
    lane (enabled only when clean and merged).
- **Activity panel** (`app/widgets/activity_panel.py`): add a Lanes section
  (one row per lane, plus an overlap/conflict list). The existing "Changed
  files (git)" section becomes per-lane.
- **Overlap hook** (`app/session_hook.py`): add a `PostToolUse` matcher for
  `Edit|Write|MultiEdit|NotebookEdit`.
  - The handler reads `AIHIVE_LANE_ROOT` and `AIHIVE_LANES_INDEX`, maps the
    edited path to a repo path, and looks up other owners.
  - Only on a real overlap does it print
    `hookSpecificOutput.additionalContext`, for example "Agent 5 (hive/agent-5)
    also changed app/widgets/main_window.py (uncommitted). Keep your change
    there small and local; the integrator merges both."
  - It warns once per (file, peer, peer-state), tracked in a per-agent seen
    file. It exits 0 silently on any error, or when the env vars are absent.
  - `MainWindow._arm_agent_mcp` sets both env vars per run (never persisted),
    next to `AIHIVE_AGENT_ID`.
  - The SessionStart matcher stays `resume|clear|compact`: adding `startup`
    makes fresh agents drop their first task (a known past incident).
- **Lane notices** (`UserPromptSubmit` hook): the lane service appends
  per-agent events to `.aihive/notices/<agent_id>.jsonl` ("lane fast-forwarded
  to origin/main@abc", "main changed app/x.py which you also changed; merge
  origin/main now while the conflict is small"). On the agent's next prompt,
  the hook injects only the unread ones.
- **Smaller board** (`app/coordination.py`):
  - `append_activity` keeps the last 40 entries in `board.md` and moves older
    ones to `.aihive/board-archive.md` (append-only; a line count check proves
    nothing is lost).
  - `_render_roster` gains Lane, Ahead/Dirty and Touching columns.
  - `TerminalAgent.roster_row` falls back to the AI title when no task is
    assigned (today the column is almost always "-").
  - Result: the board drops from ~34k tokens to a few thousand.
- **Event log** (`app/event_hub.py`): record lane created, overlap first seen,
  conflict first seen, and lane fast-forwarded.

## Phase 3: Integration queue, PR 3

- **Integrator role:**
  - A card right-click toggle, "Make integrator" (one per workspace). Persisted
    as `Workspace.integrator_uid` (keyed on `spec.uid`, which is stable across
    restarts).
  - The integrator has its own lane, and its system prompt carries the
    integrator section.
- **Queue:**
  - Persisted as `Workspace.integration_queue`: a list of
    `{lane_uid, sha, ts, state}`, where state is
    queued | integrating | merged | needs-you | skipped.
  - Also persisted: `Workspace.base_branch`.
  - All three go through `to_session_dict` and `load_session_dict` and
    `_touch` on change.
  - "Submit to integrator" pins the lane's **current sha**. Later commits stay
    on the lane and show as "+2 since submit".
- **Brief:** `lanes.integration_brief(...)` generates about 30 lines:
  - agent, lane branch, pinned sha, base;
  - `git log --oneline base..sha` and the diffstat;
  - the precomputed merge-tree result against the base and against other
    queued lanes;
  - the test command;
  - the checklist from `docs/agents/integration.md` when present, otherwise a
    built-in default.

  The integrator needs nothing from the board.
- **Delivery and advancement:**
  - When the integrator is idle and the head item is queued, send the brief
    with `TerminalAgent.deliver_task`. It is an assignment, so `deliver_task`
    is right, not `nudge`.
  - After the integrator's turn ends: fetch, then check whether the pinned
    sha is an ancestor of `origin/<base>`. If yes, mark the item **merged** and
    deliver the next one. If not, mark it **needs-you**, pause the queue, and
    raise the "?" chime plus an event-log row.
  - The queue never skips a failed item by itself.
- **Integrator checklist** (new `docs/agents/integration.md`, for this repo):
  1. Branch `integrate/<slug>` from the sha in the integrator's lane.
  2. Merge `origin/main` and resolve conflicts.
  3. Run the full `tests\smoke_test.py`, including e2e.
  4. Run `/code-review`.
  5. Bump `__version__`, add the CHANGELOG section and fix the README count.
  6. Push and run `gh pr create`.
  7. Run `gh pr merge --merge` **only** if the suite is green. "Green" means 0
     failures, or failures shown to be identical on the base tip, listed in the
     PR body.
  8. Otherwise leave the PR open and say why.
- **Keeping idle lanes fresh:** when a lane is clean, its agent is not busy,
  and it has 0 commits ahead, the service runs `git merge --ff-only
  origin/<base>` in it. A fast-forward can't lose work. Afterwards the agent
  gets a notice. A lane with its own commits is never merged automatically,
  only notified.
- **Doc updates:**
  - `CLAUDE.md` Conventions: lanes don't bump the version, CHANGELOG or README
    count; the integrator does, once per PR.
  - `CLAUDE.md` Invariants: never pass `--force` to `worktree remove` or `-D`
    to `branch`; remove junctions before removing a worktree; a lane path never
    changes for an agent; only a user click adds to the queue.
  - README gets an "Agent lanes" section, and the Layout list gets `lanes.py`
    and `lane_service.py`.

## Step 0 checks (before Phase 1 code)

1. Claude: `--resume <id>` from the lane cwd works, and a fresh lane shows the
   trust prompt once. Measure how long `git worktree add` takes on this repo.
2. Confirm `PostToolUse` `additionalContext` and `UserPromptSubmit` context
   injection on CLI 2.1.287. Confirm a `UserPromptSubmit` hook does not delay
   or drop the first delivered task (cf. the SessionStart `startup` incident).
3. Confirm that `git worktree remove` on a lane containing a `.venv` junction
   leaves the real `.venv` intact, even without our pre-unlink. Then keep the
   pre-unlink anyway as a second safeguard.

## Verification

New `tests/smoke/lanes.py`. It builds real temp git repos in the sandbox
profile and never touches the real repo. All agents are stubbed workers, per
the AI-launch guard.
- `lanes.py`:
  - create (branch and start point; cwd mapping for a subfolder workspace);
  - junction and `.worktreeinclude`;
  - `.git/info/exclude` entry;
  - status counts;
  - merge-tree conflict / no-conflict;
  - `remove_lane` refuses dirty, refuses unmerged, and leaves the junction
    target intact;
  - ff-only refresh;
  - repair recreates a deleted lane at the same path.
- Persistence:
  - the lane round-trips `to_session_dict`/`load_session_dict`;
  - the degraded record keeps `lane`;
  - a missing lane folder triggers repair, not the project_path fallback;
  - v4 sessions load with no lanes.
- Dialog: the checkbox defaults on for git + AI, off for shells and non-git
  folders, and is disabled while resuming. Count 3 gives 3 distinct lanes,
  created serially. A lane failure still starts the agent in the root folder.
- Hook (`session_hook` as a subprocess, fed stdin payloads):
  - additionalContext only on a real overlap;
  - de-duplicated;
  - silent with no env vars;
  - never non-zero on garbage input.
- Board: rotation conserves the line count; the roster renders lane columns
  and the AI-title fallback.
- Queue:
  - only the manager API enqueues;
  - the brief goes to an idle integrator via `deliver_task`;
  - it advances when the pinned sha becomes an ancestor of the base;
  - it pauses on needs-you;
  - it persists across restart.
- Full run: `.venv\Scripts\python.exe tests\smoke_test.py`, including e2e.
  e2e runs in a non-git scratch folder, so it stays unaffected, and it guards
  first-task delivery with the new hooks installed.
- Manual check in the real app: two agents in this repo edit
  `main_window.py`, both chips go amber, and each gets one overlap notice.
  Submit both. The integrator merges the first, the second is integrated
  against the new main, and both PRs merge green.

**Prerequisite:** `gh` must be logged in with an account that can merge (the
board notes `mariomolnarAM` lacked access; `MolnarMario` worked).

## What reviewers should push hardest on

1. **Lane location.** Sibling `<repo>.lanes\` (one trust prompt per agent) vs
   nested `.aihive\lanes\` (inherits trust if Claude walks parent folders, but
   pollutes jest/vite/eslint/tsc in JS repos). Is there a better third option?
2. **Auto-merge authority.** Is "0 failures, or failures proven identical on
   the base tip" a safe enough bar for an agent to merge to main unattended?
3. **ff-only refresh of idle lanes.** Can changing files under an idle agent
   confuse it mid-conversation, even though no work can be lost?
4. **Invariant fit.** Does AI Hive delivering user-queued briefs to the
   integrator stay inside "agents cannot spawn or retask each other"? It is
   modelled on scheduled send.
5. **Hook cost.** A Python start-up on every Edit/Write in every Claude agent.
   Is a ~100 ms PostToolUse hook acceptable, or should the overlap check move
   elsewhere?
6. **Persistence.** Anything missing from the three-part persisted-field rule
   for `AgentSpec.lane` and the three new Workspace fields?
7. **Scope.** Should Phase 2's board rotation ship first, on its own, since it
   is the cheapest token win?

## Comments

### Review 1 (pasted by the user, 2026-10-02), summarized

- Shared board may diverge across worktrees; name one authoritative location
  for AI Hive state (How it works / lane setup).
- Branch `hive/<slug>` is less unique than the folder; include the UID and
  define what happens when the branch exists.
- Lane lifetime vs card close vs resuming a past conversation is undefined.
- Auto-merge bar too permissive; make PR creation automatic and merging a
  user approval, and make the agent's `gh pr merge` authority explicit.
- Phase 1 scope too large; ship creation, resume and safe retention first.
- "Serial per repo" must be enforced across simultaneous add requests.

All six are addressed in `spec-v2.md`, "Changes from v1".
