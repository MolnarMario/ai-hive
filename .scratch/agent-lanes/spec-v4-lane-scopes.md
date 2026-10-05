# Agent lanes v4: lane scopes, per workspace and per agent

Status: ready-for-human (Phase A and Phase B built; "Ship it
yourself" is a follow-up)

Supersedes the "Master switch" section of `spec-v3.md`; the rest of
v3 stays current. This is the approved plan as written. What changed
while building it is under "Implementation notes" at the bottom.

## Context

Today "Agent lanes" is one switch in Options. ON means every new Claude agent in
every git workspace gets its own worktree, the lane service polls, hooks fire and
the integration queue runs. OFF pauses all of it. You want a finer grain:

- a lanes toggle per workspace, OFF for every new workspace;
- a per-agent choice, OFF by default, that can be turned on for one agent;
- the Options switch, when ON, forces it ON for every workspace and agent.

Your two answers set the rest:

- Turning a workspace ON gives lanes to new agents. Existing agents stay in the
  main folder, and their card gets "Restart in own lane (new conversation)".
- Turning anything OFF never breaks a lane that exists. A laned agent keeps its
  warnings and its way to the integrator. The whole flow should look like what a
  developer did by hand before AI: a branch per person, merge main in, run the
  tests, open a PR, someone approves the merge.

After approval, step 1 copies this plan into the repo as
`.scratch/agent-lanes/spec-v4-lane-scopes.md` (the issue tracker's folder for this
feature), so it lives next to spec-v3.

## How the feature works today, and what changes downstream

### What a lane is, in pre-AI terms

| AI Hive term | What a developer did by hand |
|---|---|
| Lane | A feature branch, checked out in its own folder (`git worktree add`). Folder `<repo>.lanes\<name>-<uid6>`, branch `hive/<name>-<uid6>`. |
| Lane chip `⎇ ↑2 ±3`, amber/red | Glancing at `git status` and asking a colleague "are you touching main_window.py too?" |
| Overlap hook and lane notices | The colleague answering "yes, I changed it an hour ago". AI Hive reads every lane every 15 s and runs `git merge-tree` to know before anyone merges. |
| Integrator agent | The teammate who owns the merge: takes a finished branch, merges current main into it, fixes conflicts, bumps the version, runs the full suite, opens the PR. |
| Integration queue | The order PRs land in. One at a time, so PR 2 is tested against main after PR 1 merged, not against the main PR 1 started from. |
| Approve merge | The human reviewer pressing Merge on GitHub. AI Hive only merges the exact commit the integrator tested (`--match-head-commit`). |

### Is the integrator redundant?

It's an ordinary Claude agent you pick with "Make integrator" on its card, one
per workspace. It is not a special agent type. It gets a typed brief per
submitted lane, built from `docs/agents/integration.md`.

My opinion: it's the step a careful developer does for their own branch before
a PR, moved into one agent. Here's why that agent pays off with parallel agents:

- Two lanes can each pass the suite alone and break main together. A green
  suite proves nothing unless it ran on top of the main the PR will land on.
  The queue guarantees that by going one item at a time.
- Version, CHANGELOG and README check count collide whenever two branches
  bump them. The board shows two branches both claiming 0.22.3. Only the
  integrator touches those, once per PR.
- Lane agents stay focused on their task and don't each spend tokens on a
  2 minute e2e run plus merge resolution.

When it's overkill: one agent at a time, or agents in different repos. Then a
lane is just a branch, and you can open the PR from the lane yourself.

So it isn't redundant, but nothing in the app explains it today. Phase B below
adds that guidance. A "ship it yourself" path (the lane agent runs the
checklist on its own branch, no integrator) is listed as a follow-up for you to
decide on. It's closer to how most small teams worked before AI, at the cost of
losing the one-at-a-time guarantee.

### What the new scopes change

1. "Switch off" stops meaning "pause everything". Lane machinery (poller,
   hooks, notices, queue, Make integrator) now follows the lanes themselves:
   it runs for any workspace that has a laned agent, whatever the toggles say.
   With no laned agent anywhere, AI Hive starts no lane git subprocess, same
   as today's OFF. The New Agent dialog's repo check is a Python folder walk
   for `.git`, not a process, and stays the one lane-related read.
2. The toggles only decide the default for new agents. Effective default for a
   workspace = Options switch OR the workspace toggle. The New Agent dialog's
   "Own lane" box starts ticked when that is ON, and the user can untick it.
   The box is offered in any git workspace, toggle on or off, so one agent can
   opt in alone.
3. The kill switch goes away. Today, flipping Options OFF silences every
   lane's warnings within one poll (`lanes.json` "enabled": false). After this
   change the way to quiet a lane is to close its card, which retires the lane
   (removed if clean and merged, kept with a notice otherwise). I think that's
   right, because a lane without overlap warnings is worse than both no lane
   and a full lane.
4. Workspace toggle ON with existing agents: they keep working in the main
   folder next to the new laned agents. That's the same mixed state today's
   per-agent opt-out allows, and it has a blind spot worth knowing. The
   overlap check compares lanes with each other and with the base REF
   (`origin/main`, `lanes.base_paths` at `lanes.py:955`), not with the main
   checkout's working tree. An unlaned agent's edits in the main folder are
   invisible to the lanes until they are committed and pushed. The
   Restart in own lane tooltip should say so in one line.
5. Two workspaces on one repo already share each other's lanes in `lanes.json`.
   With per-workspace toggles one can be ON and the other OFF. Lanes still see
   each other because the machinery follows lanes, not toggles.
6. Restart in own lane starts a fresh conversation in a new worktree. The old
   one stays on disk under the main folder and the New Agent picker can resume
   it. The agent keeps its name, uid and card.
7. CLAUDE.md invariant "only `lanes_enabled()` gates creating one" becomes
   "a lane is created only by the user's tick in the New Agent dialog or the
   card's Restart in own lane; `lane_default(ws)` only sets the tick's start
   value". Spec-v3's "Master switch" table gets replaced.

## Implementation

### Phase A: scopes (one PR)

**Model** (`app/workspace_manager.py`)
- `Workspace.lanes: bool = False`. Persist as `"lanes"` in `to_session_dict`
  (written directly, next to `base_branch`; a bool can't throw, so it needs no
  `_*_safe` wrapper) and restore with default False in `load_session_dict`.
  It's an additive key read with a default, so no `SESSION_VERSION` bump and
  no migration.
- Downgrade behavior, stated plainly: an older build loads fine and ignores
  the key, but its next save drops it, so every workspace toggle resets to
  OFF. Existing lanes and the Options switch survive. Accepted: a reset
  toggle only changes the default tick for new agents.
- `set_workspace_lanes(ws_id, on)`: sets it and goes through `_touch` (dirty,
  save), like the queue mutations.
- `lane_awareness` becomes per agent: `_apply_coordination`
  (`workspace_manager.py:964`) passes `aware=bool(agent.spec.lane)` (laned
  agents read `roster.md`, others the board). Delete the manager-wide flag and
  `reapply_coordination()` if nothing else calls it. The update paths are
  already covered: `set_agent_lane` and `clear_agent_lane` (`:323`, `:338`)
  and `set_integrator` (`:409`) each call `_apply_coordination` and `_rearm`;
  a repair never changes whether an agent has a lane; a load computes it
  right from the restored record. Add a check for each path anyway (see
  Verification), since a stale prompt only shows at the next launch.

**Gates** (`app/widgets/main_window.py`)

Every `lanes_enabled()` call gets classified. Only one kind of question is
left for a switch: what should a NEW agent start with. Everything else drops
the switch and keeps its own preconditions, which already make it a no-op
without lanes (an integrator must have a lane, `can_integrate` at
`workspace_manager.py:398`; the lane service only sees laned agents).

| Call site | Today | After |
|---|---|---|
| `_on_add_terminal_clicked` 5726, 5733 | dialog box shown, creation allowed | box always shown in a git workspace; initial tick `lane_default(ws)`; creation = user's tick and a repo |
| `_arm_lane_hooks` 2399 | switch AND lane | lane only |
| `_integration_info` 4241 (Make integrator) | switch | no gate; `can_integrate` already refuses an unlaned agent |
| `_submit_state` 4274, `_toggle_integrator` 4368 | switch | no gate; drop the "Turn on Agent lanes" reason |
| `_advance_queue` 4440, 4478 | paused while off | no gate; still needs an integrator, which needs a lane |
| `_on_integrator_turn_ended` 4501 | switch | no gate |
| `_approve_item` 4551, `_on_queue_action` 4639 | switch | no gate. Important: an AWAITING item must stay approvable after the user closes every laned card in the workspace |
| `_queue_panel_state` 4781, 4784 (`paused`) | switch | drop `paused`; remove the "Paused while Agent lanes is off" text in `activity_panel.py:345` |
| `_refresh_idle_lanes` 4795 | switch | no gate; its `views` come from the lane service, so they only hold lanes |
| `_apply_lane_machinery` 3848 | the switch | replaced by `_sync_lane_service()` below |

Two functions replace `lanes_enabled()`:
- `lane_default(ws) -> bool`: `self._agent_lanes or ws.lanes`. Used for the
  dialog's initial tick, the header toggle display and whether the card shows
  Restart in own lane.
- `_any_lanes() -> bool`: any agent has `spec.lane`, or a uid is in
  `_lane_pending`. Used only by `_sync_lane_service()`.

Lane service lifecycle, exact:
- `_sync_lane_service()` starts the service when `_any_lanes()` is true and
  stops it when false. Call it at the end of startup (after
  `_start_lane_repairs`), on `terminalAdded`, `terminalRemoved`, and after
  `set_agent_lane`/`clear_agent_lane` (wire it in `_on_lane_created`, the
  revive callback and the restart-in-lane path).
- `start()` and `stop()` are already idempotent (`lane_service.py:142`,
  `:155`). `stop()` writes `"enabled": false` to every `lanes.json` it wrote,
  which is the right thing when the last lane goes.
- When workspace A loses its last lane but workspace B still has lanes, the
  service keeps running and A's `lanes.json` stops being rewritten. Its hooks
  treat it as off after `LANES_STALE_S` (45 s), and A has no laned agent left
  to arm a hook anyway. No extra code; add a comment in `lane_service.py`.
- The gh readiness check that `_apply_lane_machinery` ran per integrator
  moves to `set_integrator` success and to startup for a restored integrator.

The "no git" guarantee, stated precisely: with no laned agent anywhere, AI
Hive starts no lane git subprocess (no poll, no fetch, no gh). Opening the New
Agent dialog still calls `lanes.find_repo_root`, which walks parent folders for
a `.git` entry in Python and starts no process. That is the one intentional
lane-related read, and it's what decides whether the box appears. Keep the
existing comment at 5720 and update its wording ("with no lane anywhere"
instead of "with the lanes switch off").

Tooltips that say "Turn on Agent lanes in Options first." (4275, 4369,
`terminal_card.py:1086`, `:1143`, `activity_panel.py:376`, `:408`) go away or
become "This agent has no lane. Restart it in its own lane from the card
menu." where an action exists.

**Options switch** (`main_window.py` around 586 and 900)
- Label "⎇  Lanes in every workspace". Tooltip: ON forces every workspace's
  toggle on and ticks Own lane for every new Claude agent. OFF leaves each
  workspace's own toggle in charge. Existing lanes are never touched.
- Persisted key stays `ui.agent_lanes`, so a user who has it ON today sees no
  change.

**Workspace toggle** (`app/widgets/workspace_page.py` header, near Map/Activity)
- A `ToggleSwitch` with label "⎇ Lanes". Checked and disabled while the
  Options switch is on (tooltip says why). Hidden when
  `lanes.find_repo_root(ws.project_path)` is empty (the same process-free
  folder walk as the dialog). Recompute that on `workspacePathChanged`.
- Flipping it starts no git and touches no existing agent. It only changes
  the dialog's default tick and whether Restart in own lane is offered.
- New signal `lanesToggled(ws_id, bool)` → `MainWindow` →
  `manager.set_workspace_lanes`. Refresh every page's toggle when the Options
  switch flips.

**New Agent dialog** (`AddTerminalDialog`, `main_window.py:1286`)
- Always build `lane_check` for a Claude agent when `repo_root` is set (no more
  `lanes_on` gate). Initial tick = `lane_default(ws)`, except a nested
  workspace stays unticked as today (`_nested_in`).
- `_on_add_terminal_clicked` (5733): `new_lanes = dialog.wants_lane() and
  bool(repo)`.

**Restart in own lane** (card header menu, offered through the dict
`MainWindow._integration_info` already builds for the card)
- Shown when `lane_default(ws)` is on, on an unlaned Claude PTY agent in a git
  workspace. Enabled only when the agent isn't busy or waiting and its uid is
  not in `_lane_pending`. Confirm dialog: "Agent 3 starts a new conversation
  in its own lane. The current conversation stays in this workspace's folder,
  and New Agent can resume it."
- The order matters. Each step names the race it closes:
  1. `agent.hold_start("[moving this agent into its own lane...]")` first.
     Nothing (a keystroke, a scheduled send, the autostart) may relaunch it
     in the main folder meanwhile.
  2. `agent.stop()` (stdin EOF; the worker's job object does the hard kill,
     never `terminate()`). Continue only from the worker's finished/state
     signal once `not agent.is_running()`, never from a timer. A stopped
     agent is out of session sync's reach (`sync_live_sessions` only looks at
     running agents, `workspace_manager.py:908`).
  3. Set `spec.session_id = ""` and `spec.resume = False` BEFORE the lane is
     recorded. Otherwise a crash between the save and the new start would
     restore the agent with the old id and the lane cwd, and `--resume` would
     continue the main-folder conversation inside the lane, the exact hazard
     this action exists to avoid. Verify a restored Claude agent with an
     empty `session_id` starts fresh; `start()` mints a new id for a
     non-resume launch (`terminal_agent.py:615`).
  4. `lanes.plan_lane(...)`, then `manager.set_agent_lane(ws_id, agent_id,
     plan.lane_dict(), plan.cwd)`. That saves, re-applies the prompt and
     re-arms the hooks (`workspace_manager.py:323`). Then `_create_lane`
     (`main_window.py:3886`). Its callback releases the hold. On `LANE-FAIL`
     it clears the lane and starts the agent fresh in the workspace folder
     with a notice. Then `_sync_lane_service()`.
  5. Live-map guard. `session_hook.read_live_map` keeps the agent's last
     SessionStart record (old session id, main-folder cwd) until the new
     child writes its own. If a sync runs in that gap, step 1 of
     `sync_live_sessions` (`workspace_manager.py:921`) would pin the OLD id
     back onto the agent. Ignore a live-map record whose `ts` predates
     `agent._session_started`, or whose `cwd` differs from `spec.cwd`. Check
     first whether plain `restart()` already has this gap. If it does, the
     guard fixes that too and gets its own regression check.
- How the old conversation stays reachable: the New Agent picker lists every
  transcript under the workspace folder (`session_sync.conversation_previews`
  in `_ensure_resume_loaded`, `main_window.py:1585`) except ids a running
  agent holds. After step 3 no agent holds the old id, so it shows up and a
  new agent can resume it in the main folder.
- Audit line `LANE-ADOPT agent=... root=... branch=...` and an event-log row.
- No reverse action. Leaving a lane is closing the card, which keeps today's
  retire rules.

**Docs and version**
- Replace spec-v3 "Master switch" with the scopes in the new spec-v4 file.
- Update the CLAUDE.md lane invariant (item 7 above), `lane_service.py` and
  `coordination.py` docstrings, README feature list and check count. The
  integrator does the version bump and CHANGELOG once per PR.

### Phase B: guidance (same PR or right after)

- A "What are lanes?" link in the workspace toggle tooltip and the New Agent
  dialog that opens a short in-app explainer built from the pre-AI table above.
- First time a workspace gets two laned agents with no integrator: a one-line
  card notice "Two lanes in this workspace. Pick an integrator to merge them
  one at a time, or open a PR from each lane yourself."
- The Submit to integrator tooltip names the steps the integrator will run.

### Follow-up, needs your decision later

"Ship it yourself": a lane agent runs the checklist on its own branch (merge
main, test, PR) without an integrator. Smaller teams worked this way. It loses
the one-at-a-time guarantee, so two PRs can each be green and break main
together.

## Review response (AGENT-LANES-SCOPES-PLAN-REVIEW.md, 2026-10-05)

Each finding was checked against the code before the plan changed.

1. Service lifecycle and the "no git" claim. Valid. The plan said "start it
   at launch", which contradicted "no machinery without lanes". It now has an
   exact `_sync_lane_service()` with its call sites. The repo check is a
   process-free folder walk, now stated as the one exception.
2. Restart in own lane. Valid, and the most important finding. The plan now
   orders the steps (hold, stop, wait for the worker, clear the id, record the
   lane, create). It shows from `_ensure_resume_loaded` why the old
   conversation stays listed. Checking this turned up a gap the review didn't
   name: the session hook's live map still holds the old id and could pin it
   back. The plan adds a guard for it.
3. Downgrade. Valid. An older build's save drops the key and every workspace
   toggle resets to OFF. Accepted and documented. Lanes and the Options
   switch survive.
4. Awareness update paths. Mostly covered already: `set_agent_lane`,
   `clear_agent_lane` and `set_integrator` each call `_apply_coordination`,
   and a repair never changes whether an agent has a lane. No new code, but
   each path gets a check.
5. Make the real-claude e2e manual or release-only. Rejected. CLAUDE.md and
   step 6 of the integration checklist require the full suite before a merge.
   It stays.
6. "DEGRADED workspace record". Valid. Only agents and queue items have safe
   paths, and a bool can't throw. The sentence is gone.
7. Classify every former gate. Done in the gates table. While doing it I
   dropped the `lanes_active(ws)` gate the first draft had. Every queue gate
   already has its own precondition, and gating Approve merge on "workspace
   has a laned card" would strand an AWAITING item once its cards close.

Also fixed in the first draft: item 4 under "What the new scopes change"
claimed that an unlaned agent's edits show up in the overlap check. They
don't. The check reads `origin/main`, not the main folder's working tree.

## Verification

- New checks in `tests/smoke/lanes.py`, stubbed workers, temp `SessionStore`:
  - workspace toggle persists, survives save/load, defaults False on a record
    without the key, and a new workspace starts False;
  - Options ON makes `lane_default` True for every workspace and disables the
    header toggles; OFF restores each workspace's own value;
  - dialog: box present in a git workspace with every switch off, unticked;
    ticked when the workspace is on; nested repo still unticked;
  - laned agent in an OFF workspace gets hooks armed, a chip, Make integrator
    and Submit; a workspace with no laned agent runs zero lane git calls
    (recording `RUNNER`);
  - Restart in own lane: refused while busy, waiting or pending; holds the
    start before stopping; mutates nothing until the stub worker reports
    finished; the save written after `set_agent_lane` has an empty
    `session_id` and the lane cwd; creates the lane, keeps name and uid; a
    failed create leaves the agent in the workspace folder with a notice;
    afterwards the New Agent picker lists the old conversation;
  - live-map guard: a record older than `_session_started`, or with another
    cwd, never re-pins a session id;
  - awareness, one check per path: `set_agent_lane`, `clear_agent_lane`,
    `set_integrator`, a load with a laned agent, a load without. Each
    asserts the system prompt's roster/board line;
  - lane service lifecycle: not running at startup with no lanes; starts on
    the first lane create; stops (and writes `"enabled": false`) when the
    last laned card closes; keeps running while another workspace has one;
  - queue without a switch: an AWAITING item is still approvable after every
    laned card in its workspace is closed;
  - mutation checks: removing the `lanes` key from save, ignoring `ws.lanes`
    in `lane_default`, or moving step 3 after step 4 of the restart each
    fail a check.
- `.venv\Scripts\python.exe tests\smoke_test.py --quick` while iterating, the
  full suite (with the real-claude e2e) before merge. CLAUDE.md and step 6 of
  `docs/agents/integration.md` require it, so it stays.
- Manual in the real app: Options OFF; turn one workspace ON, add two agents,
  both get lanes and chips; a second workspace stays OFF and its New Agent box
  is unticked; tick it for one agent, it gets a lane and warnings; flip Options
  ON and every header toggle shows on and locked; Restart in own lane on an
  existing agent gives it a fresh conversation in a new worktree.

## Implementation notes

What the build did differently from the plan above, or added to it:

- Make integrator is offered only in a workspace that has at least one laned
  agent (enabled only on a laned one). The plan said "no gate", which would
  have put a disabled Make integrator on every Claude card in every
  workspace, lanes or not. A workspace without lanes keeps the menu it had.
- The live-map guard closes the gap for a plain `restart()` too, as the plan
  suspected: `sync_live_sessions` used to pin the old run's SessionStart id
  back onto a restarted agent until its new child wrote a record. Checked in
  `test_lane_scopes_model`.
- A restored laned agent with no pinned conversation starts fresh. `main.py`
  sets `spec.resume` on every restored Claude agent, and an empty pin then
  meant `claude --continue`. In a new lane folder that finds nothing and
  falls back to a fresh start, so it was safe but cost a failed launch;
  it's now skipped (`test_restored_laned_agent_without_pin_starts_fresh`).
- Restart in own lane waits for an ENDED status (exited, crashed, failed,
  idle), not for `not is_running()`: `is_running()` is already false while
  the worker is STOPPING, before the child is gone.
- A tooltip can't hold a link, so the workspace header has a small "?"
  beside the toggle that opens the explainer; the New Agent dialog has a
  "What are lanes?" link beside Own lane. The explainer is
  `app/widgets/lanes_help.py`.
- The two-lanes hint fires once per workspace per run, on the card of the
  lane that made it two, and only while the workspace has no integrator.
- The Submit tooltip names the integrator's steps only when Submit is
  enabled; a disabled Submit keeps its reason.
- Audit lines: `LANE-ADOPT agent=... root=... branch=... left=<old id>` when
  the lane is recorded, then the usual `LANE-CREATE ... adopt=1`, or
  `LANE-FAIL <code> adopt ...`.
- The workspace header's path elision ignores hidden header widgets, so the
  hidden toggle outside git costs the path no width.
