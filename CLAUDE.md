# AI Hive — rules for working on this codebase

Windows-first PySide6 desktop app. Read `README.md` for features and layout;
this file is the invariants that must survive every change.

## Non-negotiable invariants

- **Model owns processes, widgets only subscribe.** `TerminalAgent` objects
  are parented to `WorkspaceManager`, never to a widget. Cards `detach()`
  before deletion; teardown goes through the manager. This is what keeps
  hidden workspaces executing — never parent an agent to a view.
- **Never `QProcess.terminate()`.** Graceful stop is stdin EOF; hard kill is
  the Windows Job Object (`KILL_ON_JOB_CLOSE`) so grandchildren die too.
- **Persistence is sacred.** Structural changes (add/remove agent/workspace)
  save immediately; persisted-metadata changes (task/assignment/role/status/
  font) must emit `dirty`. If you add a persisted field, wire its mutation
  path to a save AND add a restore in `load_session_dict` AND bump/migrate
  `SESSION_VERSION` if the shape changes. Backstops (added after a live loss
  where a folder's agents lived only in memory and NOTHING hit `session.log`):
  a change-detecting **heartbeat autosave** (`MainWindow._heartbeat_save`,
  `HEARTBEAT_SAVE_MS`) re-writes when live state diverges from disk; a save
  may NEVER fail silently — `_save_now`/`_save_session` log `SAVE-SKIP` (save
  suppressed while `_closing`) and `SAVE-FAIL payload` (exception while
  BUILDING the payload, which is upstream of `store.save`'s own guard) via
  `SessionStore.audit`; and `to_session_dict` serializes each agent through
  `_agent_dict_safe` so one un-serializable agent degrades to a minimal entry
  instead of aborting the entire session's save. That degrade is itself
  audited (`SAVE-DEGRADE` + the exception, via the manager's `audit` hook,
  which `MainWindow` sets alongside `arm_agent`) and it carries every field it
  can read without risking a second throw (`pty`, `model`, `effort`,
  `permission_mode`, `role`, `font_px`, `task`). Both halves are from a live
  find: two agents were degrading on EVERY save, silently resetting their
  model/effort/task on the next restore, and the cause was NOT recoverable
  from disk — a `kind` that is a plain str and a `user_args` of None throw at
  different lines of `AgentSpec.to_dict` but produce byte-identical output,
  and `AgentKind` is a str-mixin enum so even the serialized `kind` can't tell
  them apart. Don't remove these guards or let a new save path bypass the
  audit trail. The plain-str `kind` half was then found and CLOSED AT SOURCE:
  `QComboBox.currentData()` round-trips a value through QVariant and hands a
  str-mixin enum back as a PLAIN STR, so every agent built from the New Agent
  dialog carried `kind="claude"`. Nothing downstream notices (the str-mixin
  makes every `in AI_KINDS` / `in PTY_ONLY_KINDS` lookup still hit) until
  `to_dict` reaches `.value`, on EVERY save, for the life of the process —
  832 `SAVE-DEGRADE` lines in one observed session, and the agent silently
  losing its model/effort/permission-mode/role/task on the next restore.
  Restarting appeared to "fix" it only because `from_dict` rebuilds the enum.
  `build_spec` now coerces (`kind = AgentKind(kind)`) so the invariant holds by
  construction for every caller; do NOT rely on call sites passing the enum. Conversely, TRANSIENT signals must NEVER mark `dirty`:
  `activity_changed` (busy/standby, derived from output activity — see the
  status-badge invariant) fires every couple of seconds while an agent works,
  so it connects to `_recompute` (refresh derived UI only), never `_touch`;
  wiring it to a save would thrash `session.json`.
- **The sidebar badge shows WORK, not liveness.** Each workspace row's
  `AgentCountBadge` pulses amber only when an agent `is_busy()` — a subset of
  RUNNING derived from live OUTPUT ACTIVITY (`TerminalAgent._mark_busy` on each
  pty/stdout burst; a `BUSY_IDLE_MS` single-shot drops back to standby when
  output falls quiet; exit clears it in `_set_status`). CRITICAL: the pty echoes
  the user's OWN keystrokes back as output, and that echo is NOT work —
  `_mark_busy` IGNORES a burst landing within `INPUT_ECHO_S` of the last
  keystroke the user sent (`TerminalAgent.write` stamps `_last_input_ts`), and
  also skips the idle-timer re-arm for it so a pulse left over from real work
  still drops on schedule rather than being held alive by typing. Without this,
  typing at an idle prompt animated the sidebar row as if the agent were
  thinking. Task delivery (`_write_task_to_pty` → `worker.write`) deliberately
  does NOT stamp `_last_input_ts`, so a delivered task's work still pulses. Never
  regress "working"
  back to `AgentStatus.RUNNING`: an interactive agent idling at its prompt stays
  RUNNING forever, which is exactly the false-badge this replaced.
  `workspace_stats` carries the `busy` count; the badge maps busy>0→amber
  (pulsing), running-but-quiet→green, error→red, empty→dim. The row ALSO carries
  a right-edge sweeping-arc `WorkspaceSpinner` (pinned far-right so hover
  buttons open to its left) whose centre number is the working count; it is
  hidden and its animation stopped whenever `busy==0`, so a resting row is free.
  A "?" badge (`#WsQ`) shows when `waiting>0` — an agent has settled on a
  prompt/question awaiting the user. `waiting` is a THIRD transient signal
  (`TerminalAgent.is_waiting`/`waiting_changed`), wired to
  `WorkspaceManager._on_agent_waiting` (which `_recompute`s like
  `activity_changed` and NEVER saves). `is_waiting()` is the OR of THREE
  sources (`_emit_waiting`), because no single one is complete:
  (1) `_scrape_waiting` — the legacy heuristic scrape of the settled screen
  (`_screen_tail` + `_NUM_OPTION_RE` + `_OPTION_CARET_RE`), evaluated ONLY on the
  idle-timer settle (never mid-render), cleared by any fresh output, suppressed
  under `bypassPermissions`. CRITICAL: it requires BOTH 2+ numbered options AND a
  SELECTION CARET (`❯`/`>`) in front of one — the caret is the discriminator.
  Keying off numbered options alone (or a "do you want/proceed?" phrase)
  false-fired the "?"/chime on an agent's OWN prose, which routinely has numbered
  lists and questions but never a selection caret before a numbered option. Do
  NOT relax the caret requirement back to phrase/plain-list matching. This scrape
  is now the FALLBACK — it catches classic permission menus but is BLIND to
  `AskUserQuestion` (which renders no numbered+caret menu), plain free-text
  questions, and plan approval. Those are covered by two AUTHORITATIVE
  Claude-hook edges (the scrape literally cannot see them, and — verified,
  claude-code#59908 — `AskUserQuestion` fires NO Notification hook either, so a
  hook on the tool NAME is the only signal): (2) `_tool_waiting` — set by a
  `PreToolUse` hook on `AskUserQuestion|ExitPlanMode` (fires BEFORE the UI
  renders, so it never races the output-based busy marker) and cleared by the
  matching `PostToolUse` (or a `Stop`, which covers the user cancelling the
  prompt); it is STICKY against output because the tool draws its own UI, so
  `_mark_busy` must NOT clear it. (3) `_turn_waiting` — set by the `Stop` hook
  when the agent's `last_assistant_message` ends on a question, cleared by the
  next output burst (the user engaged). These edges ride the SAME shared
  `--settings`/`AIHIVE_AGENT_ID` hook plumbing as the SessionStart tracker
  (`app/session_hook.py` now dispatches ALL events); the hook APPENDS edges to a
  separate `prompt_events.jsonl`, and `WorkspaceManager.sync_prompt_events`
  (on `MainWindow._prompt_sync_timer`, `PROMPT_SYNC_MS`) reads it INCREMENTALLY
  (by byte offset) so an edge is applied exactly once and a stale line is never
  re-applied after a local clear. A `turn_set` arriving while the agent
  `is_busy()` is stale (answered before we polled) and is dropped. Do NOT add a
  `UserPromptSubmit` or `startup`-matched hook — both perturb the launch/first-
  task-submit timing the SessionStart invariant guards; the chosen hooks all
  fire mid-session. On the
  RISING edge (standby→waiting) `_on_agent_waiting` also emits
  `WorkspaceManager.agentWaiting(ws_id, agent_id)`, which `MainWindow` turns
  into a soft notification bell (`app/chime.py` — Qt-free WAV synth, async
  `winsound` playback, degrades to a silent no-op off-Windows) so the user
  hears an agent needs them from another workspace. The chime is level-vs-edge
  correct (re-entering waiting rings again; staying waiting does not) and
  mutable via the top-bar 🔔 toggle, persisted transiently in
  `session["ui"]["sound_enabled"]` (never a `dirty` mutation). Clicking the
  count badge OR the "?" toggles an INLINE agent expansion in the sidebar tree
  (`_toggle_agents` → `_expanded_ws`; agent child rows built from
  `sidebar.agents_provider`, folder-tree style — name left, summary beside,
  per-agent "?"), refreshed live by `_sync_expanded` on a timer while any
  workspace is expanded. The per-agent SUMMARY (`TerminalAgent.summary()`, shown
  here AND in the card header) is the assigned `current_task` if set, else
  Claude Code's own AI conversation title — the `{"type":"ai-title","aiTitle"}`
  record Claude appends to the transcript as it evolves (the same text `/resume`
  shows), read via `transcripts.latest_ai_title` (mtime-cached) and adopted by
  `WorkspaceManager.refresh_ai_titles` on the session-sync timer.
  `set_ai_title`/`_ai_title` are TRANSIENT (never persisted) and only emit
  `summary_changed` when the DISPLAYED summary actually changes (task always
  wins), so a title poll never thrashes the UI or a save. NOT a floating popup (the user explicitly wanted it
  part of the sidebar, not overlapping content). Clicking an agent row emits
  `Sidebar.agentActivated` → `MainWindow._reveal_agent` (switch ws + scroll+focus
  its card — the same primitive the Agent/File Map uses). The badge click is
  CONSUMED so it never bubbles to row-select/switch.
- **The taskbar overlay is the ONE signal that leaves the window**
  (`app/taskbar_overlay.py`, Qt-free/stdlib-only like `chime.py`;
  `ornaments.taskbar_badge_bgra` paints it; `MainWindow._taskbar_badge_spec`/
  `_push_taskbar_badge` drive it). Every other "an agent is working" indicator
  lives INSIDE the app (the pulsing `AgentCountBadge`, the `WorkspaceSpinner`),
  which is useless while the user is in another application waiting for the hive
  to finish — the taskbar button is visible from everywhere, so the working count
  goes there. THE CONSTRAINT THAT SHAPES IT: Windows gives an app exactly ONE
  overlay icon and fixes it to the corner of the button
  (`ITaskbarList3::SetOverlayIcon`; Qt 6 dropped `QWinTaskbarButton`, hence the
  ctypes/COM shim beside `main.py`'s existing Win32 work). There is no second
  slot and no choice of corner, so the count and "an agent has a question" SHARE
  one ~16px square: the DIGIT is `sum(is_busy())` over `manager.all_agents()`,
  the FILL COLOUR is `any(is_waiting())`. Deliberately the same two predicates
  the sidebar reads, so the two surfaces can never disagree. Zero and zero means
  NO overlay — that absence is the readout ("nothing is running, go and look"),
  and it is what makes the feature self-silencing enough to leave on. Rules:
  colours are FIXED constants (`ornaments.TASKBAR_WORKING`/`TASKBAR_ASKING`),
  NOT `Palette` reads like every other badge — this is painted onto the OS
  taskbar over the user's own accent colour, not onto our chrome, so following
  the skin buys no coherence while risking a disc that vanishes; the count is
  TRANSIENT exactly like `activity_changed` and the plan-usage reading, so
  `_push_taskbar_badge` must NEVER `_touch`/`_schedule_save` (only the
  `ui.taskbar_badge` preference saves, additively, no `SESSION_VERSION` bump);
  the push is EDGE-GUARDED on a rendered key and coalesced behind
  `TASKBAR_BADGE_MS`, because `workspaceStatsChanged` fires every couple of
  seconds per busy agent and each push builds an HICON and crosses a COM
  boundary; `_taskbar_key` advances only on a SUCCESSFUL push, so a refusal is
  retried rather than remembered as current. `_push_taskbar_badge` returns early
  unless `QGuiApplication.platformName() == "windows"`, which is what keeps the
  offscreen smoke suite out of COM entirely — the whole state table is therefore
  decided in `_taskbar_badge_spec`, with no window handle or apartment, and
  that is what the tests drive. `taskbar_overlay` never raises (every entry
  point returns a bool), and `closeEvent` clears the overlay before the window
  goes so a stale "3 working" can't outlive the hive.
- **The sidebar is a model-driven tree** (`widgets/sidebar.py`): `_nodes` is the
  ordered top-level list (workspaces + single-level `category` nodes with
  workspace children); drag-and-drop never moves Qt items (that strands the rich
  `setItemWidget` rows) — a drop mutates `_nodes` and `rebuild()`s, and every
  structural change emits `layoutChanged` → `WorkspaceManager.apply_sidebar_layout`
  (which re-sequences `_workspaces` + persists). Categories don't nest. The
  layout persists under the session `"sidebar"` key at `SESSION_VERSION = 4`;
  restore applies it in `load_session_dict` and `_adopt_existing_model`
  (`sidebar.apply_layout`). Normalization is the safety net: unknown ids drop,
  unplaced workspaces append uncategorized — so a stale/partial layout never
  loses a workspace. A category + its visible members are wrapped in a tinted
  group box painted in `_SidebarTree.drawRow` (per-row, keyed on
  `_row_category`), so it grows/shrinks automatically with any expand/collapse
  (category→workspaces, workspace→agents) — no separate container widget.
  NOTE the shell-migration gate was pinned to `version < 3`
  (its original threshold) so future `SESSION_VERSION` bumps never re-flip a
  user's line-mode shells.
- **Single instance is enforced by a kernel mutex** (`main.py`,
  `_single_instance_guard`) and fails closed. Don't replace it with anything
  that has a probe timeout.
- **The session file is never deleted.** Failed loads rename to `.json.bak`;
  loads use `utf-8-sig` (BOM tolerance); saves are atomic, keep a rolling
  previous generation (`session.json.1`), and are audited in `session.log` —
  INCLUDING failures (`SAVE-FAIL` lines; a silent save failure is how agents
  vanish without a trace). Never edit `session.json` with PowerShell
  `Set-Content` (it writes a BOM); use Python — and ALWAYS copy the live
  file aside and re-read it immediately before writing: a hand edit based on
  stale forensics once destroyed a user's freshly created agents.
- **A closed window is a dead process.** `main.py` sets
  `window.quit_on_close = True`; `closeEvent` force-quits the event loop. A
  zombie instance that outlives its window holds the single-instance mutex
  and — with `_closing` latched — can never save again, so everything done
  in it is silently lost (this happened for over an hour of real use). The
  test suite must NOT set this flag: it opens/closes many windows per
  process.
- **The board `log_activity` tool is the ONLY agent→GUI op, and it is
  workspace-scoped.** There is NO orchestrator: agents cannot spawn, retask, or
  close each other — they only post one-line notes to their workspace board.
  (This was deliberately removed; a Claude agent already spawns sub-agents only
  in its own HEADLESS `Task` way, and the user wanted visible, self-driven
  terminals, not a middleman agent directing others.) Each agent's mcp config
  binds `AIHIVE_WS` (echoed with every RPC), so a note always lands on the
  right workspace's board; the bridge exposes exactly one op (`log_activity`)
  and rejects anything else as `bad_args`. Every Claude agent gets the same
  config (`--allowedTools mcp__aihive__log_activity`, pre-approved so the call
  never prompts) — there is no per-role config anymore. If you add a bridge op,
  scope it by `ws` and add it to the docstring; board writes never touch SESSION
  state so they do NOT trigger a save. Agents are armed with their config in
  `add_terminal` (via `manager.arm_agent`, set by MainWindow) BEFORE they
  start, and re-armed on restore (`_rearm_agent_configs`) — `mcp_config_path`
  is never persisted. `OrchestratorBridge`/`orchestrator_bridge.py`/
  `mcp_server.py` keep their historical names but are now just the board
  channel.
- **AI Hive OWNS the scrollback, and that is a launch flag, not a widget.**
  The terminal scrollbar (`widgets/terminal_scrollbar.py`) and its prompt
  milestones only exist because Claude is asked for its CLASSIC renderer:
  `tui: "default"`, written into the SAME shared `--settings` file as the hooks
  (`session_hook.write_settings_file`'s `tui` arg, driven by
  `MainWindow._write_hook_settings` / `ui.terminal_scrollback`). The default
  `"fullscreen"` renderer keeps a VIRTUALIZED scrollback inside the alternate
  screen and redraws in place, so nothing ever scrolls off and pyte's
  `history.top` stays EMPTY — measured on real `.vt` snapshots: 0 lines of
  history for 4 of 5 sessions, and `?1049h` sent with `?1049l` never sent. A
  scrollbar over that is permanently blank, which is why the renderer switch is
  the feature. The `tui` key is a SIBLING of `hooks` and must never be folded
  into it: those matchers are the timing-critical `startup`-exclusion ones.
  It is an ordinary UI preference (`_schedule_save`, additive optional key, NO
  `SESSION_VERSION` bump) and it takes effect on the NEXT launch only — never
  restart a working agent to apply it. Two consequences of the classic renderer
  are real and were measured, not guessed: it is not flicker-free, and it does
  NOT enable mouse tracking (so Claude's menus stop being clickable, and
  `wheelEvent` needs no change — with `_mouse_tracking` and `_alt_screen` both
  False it already falls through to `scroll_by`).
- **Readiness is matched WITHOUT WHITESPACE, and that is load-bearing**
  (`TerminalAgent._has_ready_hint`, `_despace`). The classic renderer lays its
  footer out by MOVING THE CURSOR between segments instead of emitting spaces,
  so once `_CSI_RE` strips the escapes, "? for shortcuts" arrives as
  "?forshortcuts". Measured: the hint was on the rendered pyte screen from the
  first frame and matched the escape-stripped stream NEVER. Since
  `prompt_ready` gates task delivery, a spaced match parks every first task in
  `_pending_task` forever and leaves the BootVeil up until its timeout — the
  exact silent failure the `startup`-hook invariant warns about, from a
  different direction. Despacing both sides is a superset, so the alt-screen
  renderer keeps matching exactly as before.
- **pyte's HistoryScreen event wrapper is REMOVED, and that is a performance
  invariant** (`terminal_view._FastHistoryScreen`). pyte routes EVERY attribute
  access on a `HistoryScreen` through a Python-level `__getattribute__` that
  re-wraps each stream event in `before_event`/`after_event`. Both hooks exist
  only to serve `prev_page`/`next_page`: "before" pages back to the bottom of
  the history buffer, "after" re-clips line widths. AI Hive NEVER pages
  (scrollback is a view offset), so `history.position` never leaves
  `history.size` and both are no-ops on every single call. They are not free
  no-ops, and taking the scrollback back is what made them hot: pyte's scroll
  path (`Screen.index` at the bottom margin) shuffles EVERY row of the buffer
  one dict entry at a time, so it touches ~2*rows attributes per scrolled line
  — and under Claude's alt-screen renderer that path was never entered at all
  (the spike measured `pushed == 0` for 4 of 5 sessions: nothing ever
  scrolled). The classic renderer scrolls thousands of times per session, which
  multiplied a per-attribute tax nobody had previously paid, and the app
  visibly stuttered. MEASURED replaying 1.7 MiB of real captured agent output:
  4.2 MILLION `__getattribute__` calls, 43% of all feed time, 8.0M total
  function calls; removing the wrapper renders a byte-identical screen, history
  AND cursor 2.87x faster (924ms → 318ms on the largest capture, 3.1M calls).
  `after_event` also maintained `cursor.hidden`, but only as
  `position == size and DECTCEM in mode` — the first term is always true here,
  leaving exactly what the base `Screen`'s own `set_mode`/`reset_mode` already
  does (verified against a capture exercising DECTCEM 3982 times). Because the
  snap-back hook is gone, `prev_page`/`next_page` RAISE rather than silently
  paging the screen away with nothing to restore it; do not "implement" them
  without restoring the wrapper. This is the terminal's hot loop and it runs on
  the GUI thread for every agent at once, so do not reintroduce a plain
  `pyte.HistoryScreen` — `_new_history_screen` is the one construction site.
- **ONE identity carries the scrollbar and every milestone**:
  `abs_line(visual row r) == history.top.pushed - scroll_offset + r`. Both
  branches of `_visible_line` reduce to it. NOTE the other coordinate space:
  `_input_block_span`/`cursor.y` are LIVE BUFFER rows, so `anchor_line()` is
  `pushed + row` with no offset term; the two agree at offset 0 and nowhere
  else. `_CountingDeque.pushed` is deliberately NOT reset by a history wipe
  (pyte's `_reset_history` clears the same deque object), which is what keeps
  ids monotonic across a `/clear` — "fixing" that would silently corrupt every
  marker. And do NOT try to stretch `HISTORY_LINES` by filtering duplicate
  lines out of the history push: the identity holds only while EVERY line
  leaving the screen is counted, so one suppressed append drifts every
  live-anchored mark by one, permanently.
- **A milestone is the user's own bare Enter, and nothing else**
  (`TerminalView._note_prompt_submit`, the branch that already calls
  `_reset_undo`). Rejected sources, each for a concrete reason: `agent.write`/
  `_on_key_input` see a `\r` inside a bracketed PASTE; `_last_input_ts` is any
  keystroke; a `UserPromptSubmit` hook is forbidden by the `startup` invariant.
  It also excludes `deliver_task`/`nudge`/scheduled sends by construction —
  they reach `worker.write` and never touch a key event. Two guards keep
  ANSWERS out: the view rejects `_NUM_OPTION_RE` shapes (`_INPUT_PROMPTS`
  includes the `❯` caret Claude paints on a highlighted menu row, so
  `_input_text()` over a menu returns "1. Yes"), and the card rejects while
  `agent.is_waiting()`. Marks live on the AGENT (`PromptMark`), keyed by a
  monotonic `uid` and NEVER by `id()` — they are FIFO-capped, and CPython
  reuses an address after collection, so a dropped mark would hand its id to a
  new one and silently donate its position. `pos` is a CHARACTER offset into
  the pty stream, because a card rebuild throws the view away and restarts
  `pushed` at 0; `TerminalCard._replay_with_marks` splits the replay at those
  offsets and re-derives each line with the SAME `anchor_line()` used live.
  Do not extend a segment past the submit echo to "catch" the reprinted
  prompt: that breaks the symmetry. Marks are TRANSIENT and never persisted
  (`prompt_marks_changed` must never reach a save) — the transcript is the
  durable record, and a stored marker list goes stale like a stored limit
  latch. The PRIMARY reset is `note_conversation_replaced()`, called from
  `sync_live_sessions` wherever a pin actually changes, because `/clear` under
  the classic renderer emits NO `ED 3` at all (measured) and simply reprints
  the banner; the `feed()` history-shrink check is the secondary edge.
- **A milestone the app never watched being typed is RECOVERED, not invented**
  (`TerminalCard._recover_marks` + `transcripts.typed_prompts`). Marks are
  minted from a keystroke, so a conversation restored from disk comes back with
  NONE — which is precisely the conversation long enough to want milestones in
  (reported live: reopened the app, saw no dots at all). The prompt TEXTS come
  from the transcript, where Claude tags a genuinely typed prompt with
  `promptSource: "typed"` (absent on tool results, on `isSidechain` sub-agent
  turns, and on the `_SYNTHETIC_USER_TAGS` plumbing), so the worst case is
  failing to LOCATE a prompt, never inventing one. Matching is strict on the
  prompt side and loose on the line side, because the classic renderer can drop
  the leading character of the echo (measured) and wraps long prompts: a
  normalized head is searched anywhere in a line, first hit wins, and the scan
  carries on from the next line so prompts match in order and a repeated prompt
  cannot claim an earlier line twice. It scans history AND the live screen —
  their absolute ids are contiguous (`oldest + len(hist) == pushed`) and a
  short conversation has scrolled nothing off yet, so history alone finds
  nothing. Recovered marks are recomputed on every projection and merged BEHIND
  the live ones (a live capture is exact; a recovered one was matched), and
  like every other mark they are never persisted.
- **A width change RE-PROJECTS the scrollback** (`TerminalCard.
  _reproject_on_size`). pyte does not reflow: a history line keeps the column
  count it had when it was pushed. That was invisible while Claude owned its
  own scrollback (history was always empty), but now every width change would
  leave the old lines wrapped for a screen that no longer exists — rendering as
  a short fragment with the wrapped remainder stranded at the old right edge.
  The launch case is the one that bites and the one that was reported: a card is
  built at its pre-layout width, the child paints into it, those lines scroll
  into history, and only THEN does the tiling grid give the card its real size.
  The raw pty stream is the truth and the screen is only a projection of it at
  one width, so the repair is to project again. Bounded by
  `REPLAY_PROJECT_CAP`, which is a COMPROMISE and not a free bound: a full
  512 KiB buffer takes ~1.25s (which would freeze a retile across a hive),
  while 128 KiB costs ~80ms since the pyte wrapper came off (~250ms before it)
  and reaches only 844-1519 of `HISTORY_LINES`' 2000 lines — so the cap is
  already what truncates reachable scrollback, and LOWERING it to buy speed
  costs milestones. (An earlier note here and in the code claimed 96 KiB filled
  all 2000; re-measurement on real captures disproved that. It reaches
  576-1126.) The cut is moved forward to the next ESC so a projection never
  starts mid-sequence. Height-only changes do nothing.
- **That projection happens ONCE, at the SETTLED width** (`TerminalCard.
  _rerender_restored`). The constructor runs before the tiling grid sizes the
  card, so its projection is thrown away and redone by construction — doing the
  FULL cap there as well meant every card at launch and every retile projected
  128 KiB twice, the first time at a width that never reached the screen, AND
  read the whole transcript twice for milestone recovery. Measured per card:
  ~80ms + 90-165ms per projection (the user's real transcripts run to 50 MB),
  i.e. multiple seconds of frozen window across a six-agent hive, reported as
  the app freezing right after it "paints the picture". So the constructor
  seeds only `REPLAY_SEED_CAP` (a screenful, ~5ms, `recover=False`) — its ONLY
  job is that the terminal is never briefly blank — and `_rerender_restored`
  does the one real projection. THREE things keep that safe. (1) It projects
  the agent's CURRENT buffer, not the constructor's snapshot: for an ordinary
  rebuild the buffer IS the live conversation, and an earlier version that
  compared the two and bailed on any difference would leave a BUSY agent's
  rebuilt card holding nothing but the 8 KiB seed. (2) It still refuses a
  restored snapshot a live child has drawn over (`TerminalAgent.
  seed_written_over`, which compares the buffer to `_pty_seed`) — that seed is
  the previous run's screen and a running agent is deliberately given a clean
  terminal, so replaying it would put back the mangled fragment
  `_drop_restored_screen` exists to remove. `is_running()` is NOT the test, for
  the reason that function documents. (3) A `REPLAY_SETTLE_MS` single-shot
  BACKSTOP, because `TerminalView._apply_resize` returns EARLY when rows/cols
  are unchanged, so `sizeChanged` is not guaranteed to fire at all and a card
  built at exactly its final size would keep the seed forever.
  `_drop_restored_screen` must stop that timer as well as disconnect the
  signal, or it re-projects the very screen that was just dropped.
- **The transcript behind milestone recovery is cached per conversation**
  (`TerminalCard._recover_key`/`_recover_prompts`). `_recover_marks` runs on
  EVERY projection and `transcripts.typed_prompts` is O(whole transcript);
  `transcripts` keeps an (mtime,size) cache, but a LIVE agent rewrites its
  transcript continuously, so that cache misses for precisely the agents being
  used. Re-reading inside one conversation cannot find anything the card does
  not already know — it WATCHED those prompts being typed, so they carry live
  marks, which outrank recovered ones in `_refresh_marks`. A new conversation
  (a `/clear`, a pin change) changes `spec.session_id`, which changes the key
  and re-reads.
- **The scrollbar's TRACK spans history PLUS the live screen**
  (`TerminalScrollBar._span`), which is wider than the scrollable range. A
  prompt just submitted is still on the live screen, so bounding the track at
  `pushed` left its dot undrawable until it happened to scroll off — i.e. you
  type a prompt and no dot appears, which reads as the feature being broken.
- **The submitted input is read from the box, NOT from the caret's row**
  (`TerminalView._submitted_input`). `_input_block_span` is the precise
  reading and is tried first, but it REQUIRES the caret to be on a row with
  content, and the classic renderer parks the caret on a BLANK row below the
  input box after a submit (measured on a real session: text on row 33, caret
  on row 35), so the span came back None and NO milestone was ever recorded.
  The fallback scans up from the caret and is bounded twice so it can never
  mistake output for a prompt: it only runs when the caret is in the box's own
  region at the bottom of the screen, and it STOPS at the box's rule/footer
  rather than stepping over it into the conversation, so an empty box records
  nothing. It also trims a wide run of spaces, because the renderer
  right-aligns footer bits onto the same row by jumping the cursor.
- **pyte quirks are handled in `terminal_view.feed()`** — private-marker CSI
  stripping, partial-escape carry, DECSET mode tracking. Scrollback is a view
  offset, never pyte paging (`prev_page` snaps back on any event). The wheel
  has three regimes (mouse-tracking forward / altscreen arrows / history
  offset) — keep all three working.
- **Block Elements are drawn GEOMETRICALLY, never by the font**
  (`terminal_view._BLOCK_RECTS`/`_BLOCK_SHADES`/`_paint_block`). Consolas has
  no QUADRANT glyphs (U+2596-259F), so Qt silently falls back to Segoe UI
  Symbol at a 12px advance in a 7px cell — and since `paintEvent` batches a
  run of cells into ONE `drawText`, letting Qt lay it out, that single glyph
  drags the whole rest of the row sideways. Claude Code's welcome mascot is
  full blocks plus quadrants, so its rows sheared apart and its corners came
  out notched (the fallback ink is also the wrong shape for the cell). Even
  in-font, Consolas' full block is 6.7px of ink in a 7.0px advance, striping
  solid artwork with background hairlines. So: fill exact rects on the shared
  rounded grid from `cell_bounds`, which derives BOTH edges of a cell from the
  same expression — cell N's right edge is bit-identical to cell N+1's left,
  which is what makes the fills tile at any fractional cell size. The general
  rule behind it: a glyph whose advance isn't the cell width (`_is_grid_glyph`
  — also true of `✳`, `⏸`) is drawn ALONE at its own cell so it can never
  shear its row; ordinary text still goes out in one `drawText`.
- **Claude CLI facts** (verified 2.1.197): interactive `--continue`/`--resume
  <id>` hard-exit when there is no matching conversation (handled by the
  fresh-fallback in `TerminalAgent._on_finished`); headless `-p` silently
  starts fresh instead — never use `-p` to test resume. Effort tokens:
  low|medium|high|xhigh|max. Claude Code enters the alternate screen and
  enables mouse tracking (?1049h, ?1000/1002/1003h, ?1006h, ?1004h, ?2004h).
- **Model aliases resolve CLIENT-SIDE, in the installed CLI binary — not on
  the server.** The app passes bare aliases (`--model opus|sonnet|haiku|fable`,
  `providers.py:CLAUDE_MODELS`), never a dated model id, so it does NOT pin a
  version. But the `claude.exe` binary expands the alias to a concrete model id
  from a table baked into THAT build. So an alias only knows the models its CLI
  version shipped with: a stale CLI made every "Opus" agent launch Opus 4.8
  long after Opus 5 (`claude-opus-5`, released 2026-07-24) was out, because
  2.1.218's binary had no `opus-5` string in it. "Stuck on an old model" is
  therefore a stale-CLI symptom, NOT app hardcoding — the fix is upgrading the
  CLI, after which the same alias resolves to the newer model with zero app
  change. To verify what an alias will resolve to WITHOUT launching, grep the
  binary: `grep -c "opus-5" "<WinGet Packages>\...\claude.exe"` (0 = that build
  doesn't know the model). Adding an explicit-id entry to `CLAUDE_MODELS`
  (e.g. `("Opus 5","claude-opus-5")`) pins past the alias, but a too-old CLI
  may reject an unknown `--model <id>`, so the upgrade is still the real fix.
- **A CLI upgrade CANNOT apply while AI Hive has live agents.** Every running
  agent is a `claude.exe` child, and Windows cannot replace a running `.exe`,
  so `winget upgrade Anthropic.ClaudeCode` silently no-ops (or reports success
  while the binary is unchanged) whenever the app — or a zombie instance
  holding the single-instance mutex — still has agents alive. To update: fully
  close AI Hive (or reboot), confirm `Get-Process claude` returns nothing, THEN
  upgrade. This is a direct consequence of the Job-Object process model (agents
  are kept alive by design); it is expected, not a bug.
- **The startup update gate is the ANSWER to the invariant above, and its two
  rules both come from that one fact** (`app/cli_update.py`, Qt-free/stdlib-only
  like `chime.py`; `app/widgets/update_splash.py` shows it; `main.py` is the
  only caller). Because a running `claude.exe` cannot be replaced, the moment
  ABOVE `create_main_window()` — before a single agent exists — is not merely
  convenient, it is the ONLY unlocked moment, which is why the gate sits between
  `setup_application` and the factory and far above
  `autostart_active_workspace()`. RULE 1: **winget's report is never evidence.**
  The installed version is read off the FILE (`<exe> --version`, 0.09s) before
  AND after, and winget is asked only what the MANIFEST offers (`winget show`, a
  pure read) — never `winget upgrade` for the check. This is the repair for the
  live bug: an upgrade run with agents up cannot replace the file but winget
  records the new version anyway, after which the stale binary nags forever (and
  its baked-in alias table still can't resolve a newer model, per the alias
  invariant above) while `winget upgrade` answers "No available upgrade found".
  Hence `Status.DB_STALE` for "the file is behind the manifest AND winget says
  nothing to do", checked BEFORE the return code because winget reports that
  with a non-zero rc. DB_STALE only REPORTS: the forcing flag is unverified, and
  shipping a guessed `--force` at startup is not acceptable. RULE 2: **a live
  target process skips the target without issuing ANY upgrade command**, which
  is the direct fix for how the database got poisoned; those processes are the
  user's own or another app's, outside our Job Object, and are NEVER killed (one
  kill can destroy a transcript). An unanswerable `tasklist`
  (`UNKNOWN_PROCESSES`) counts as blocked for the same reason. CRITICAL, and a
  live find: **"live" is decided by PATH, never by image name.** `Claude.exe` is
  BOTH the CLI and the unrelated Claude DESKTOP app, which a user leaves open
  all day, so a name-only count read eight desktop windows as a locked CLI and
  skipped the update on EVERY launch, forever — observed as `UPDATE-SKIP claude
  (8 claude.EXE alive)` logged three seconds BEFORE AI Hive started an agent of
  its own, i.e. against a binary nothing was holding (measured on the same
  machine afterwards: 19 by name, 11 by path). `count_processes` therefore runs
  two passes cheapest-first — `tasklist` answers "anything by this name at all",
  and only a non-zero answer pays for the path listing (`process_paths_argv`,
  measured 0.40s) that decides how many are `target.exe`. Comparison goes
  through `_canonical` (`realpath` + `normcase`, not a string compare) because
  winget also installs a symlink shim and a session launched through it reports
  the LINK. EVERY ambiguity leans towards over-counting — an unreadable path, a
  failed path query, or a listing that sees fewer than `tasklist` did all mean
  "assume it is ours" — because over-counting costs a skipped update that the
  pill reports, while under-counting runs an installer against a locked file,
  which is the false database record this whole module exists to prevent. The two SHAPES
  of target diverge in exactly one structural way and it must not be flattened:
  a winget package is checked and installed as separate acts, while a
  self-updating CLI takes NO flags on `agy update`/`claude update`, so
  there is NO dry run and for a `self_update` target checking IS installing
  (`needs_apply` returns True unconditionally, and an unchanged version after a
  clean self-update reads UP_TO_DATE, not the REPORTED_BUT_UNCHANGED the same
  reading means for winget). WHICH SHAPE CLAUDE TAKES IS NOW READ OFF THE
  INSTALL, not hardcoded (`_claude_target`, decided by
  `cli_install.classify_install` on the PATH, because the binary, the version
  string and the process name are identical across install methods): a WinGet
  package keeps the winget shape, a NATIVE install is `self_update`. The old
  note here said Claude deliberately stays on winget because `claude update`
  installs a native build elsewhere and `resolve_claude()` would then launch the
  stale copy. That risk is now HANDLED rather than avoided (`resolve_claude()`
  checks `%USERPROFILE%\.local\bin\claude.exe` FIRST, see the native-migration
  invariant below), so the same build serves either machine and rolling the
  migration back needs no code revert. One real consequence: `Status.DB_STALE`
  is structurally UNREACHABLE for Claude on a native install, since there is no
  package database to go stale. Budgets are
  SPLIT and that is deliberate: the check is bounded (`CHECK_TIMEOUT_S`) and
  fails OPEN to launch (`TIMEOUT`, no pill — a hung network must never cost the
  user the app), while the install is NEVER killed on a timer, because a
  half-written 285 MB binary is worse than the banner. Skip is the escape hatch
  instead, and its one consequence is handled rather than prevented: providers
  still installing at Skip come back in `GateResult.installing` and
  `autostart_active_workspace` holds THOSE agents back, so none can execute a
  half-written file. Threading is mandatory, not stylistic — the gate runs on a
  `threading.Thread` while the splash drains a `queue.Queue` on a `QTimer` in a
  local `QEventLoop` — because `gemini_usage.fetch()` shelling out inline on the
  GUI thread froze the app ~6s a minute (see the Gemini readout invariant). The
  runner is INJECTED and the real one is only ever passed from `main.py`, the
  same opt-in rule as `start_usage_polling()`: the suite shares
  `create_main_window` and must never upgrade the user's CLI, so every check
  drives `run_gate` with a fake `Runner`. `ui.auto_update` is an ordinary UI
  preference (default OFF, since this mutates installed software: additive
  optional key, `_schedule_save`, NO `SESSION_VERSION` bump) and the OUTCOMES
  are TRANSIENT exactly like the plan-usage reading — `note_update_outcomes`
  must never `_touch`/`_schedule_save`. Every version transition is audited to
  `session.log` (`UPDATE-CHECK`/`UPDATE`/`UPDATE-SKIP`/`UPDATE-STALE`/
  `UPDATE-UNCHANGED`/`UPDATE-FAIL`/`UPDATE-TIMEOUT`) with deliberately NO
  patch-versus-minor gate: nothing in the numbering predicts whether a flag
  moved, so a version gate buys false safety while an audit line turns "it broke
  this morning" into a lookup.
  **WHAT A SKIPPED CHECK MEANS DEPENDS ON THE SHAPE, SO THE WORDS DO TOO.**
  `Outcome.self_updating` is stamped by `check()` off `Target.self_update` and
  is what every reporting surface reads. On a WINGET target a TIMEOUT or a
  locked file is a genuinely missed update (nothing else will fetch one, and
  the stale binary keeps its alias table and its banner); on a SELF-UPDATING
  one the CLI fetches its own version in the background regardless, so the same
  statuses are NON EVENTS. Hence three things move together and must stay
  together: `_SELF_UPDATING_TEXT` rewords them ("left to its own updater"),
  `needs_pill` withholds the top-bar nag — every line of `_PILL_LONG` tells the
  user to close programs or run an installer by hand, and none of that applies
  — and `worth_reading` does not hold the splash open. ONE predicate
  (`needs_pill`) decides the pill for both `pill_text` and `pill_tooltip`, so
  the splash and the bar can never call the same outcome different things.
  This is a live report, and the shape of the complaint is the point: after the
  native migration the splash flashed "took too long, skipped" for under a
  second on a launch where nothing was wrong (the CLI updated itself a minute
  later), and because TIMEOUT is deliberately NOT in `PILL_STATUSES` there was
  no surface afterwards that could say so — a message too brief to read and too
  final to check. So the fleeting surface is no longer the only one:
  `last_check_summary` lets EVERY outcome speak (unlike `pill_text`, which
  reports only what needs acting on) and `MainWindow._update_outcomes` carries
  them to the Updates panel, TRANSIENT exactly like `_update_installing`. A
  status with nowhere to be re-read is indistinguishable from one the app never
  produced. `LINGER_CLOSE_MS` buys a `worth_reading` outcome time to be read,
  and is deliberately NARROWER than "not a clean run" — lingering on a non
  event would manufacture the very concern the rewording removes. The splash
  SUBTITLE follows the machine for the same reason (`subtitle_for`): "Now is
  the only moment these files are not in use" is the winget constraint, false
  of a native install that never touches the running file, and it was being
  painted directly above that install's row. It stays while ANY target is
  package managed. The audit lines record the shape (`UPDATE-TIMEOUT` /
  `UPDATE-SKIP` gain "(self-updating, left to the CLI)") because which of the
  two a line describes is read off the install and can change between runs on
  one machine.
- **Letting Claude Code update ITSELF is a MIGRATION, not a preference**
  (`app/cli_install.py`, Qt-free/stdlib-only like `cli_update.py`;
  `app/widgets/update_panel.py` shows it; `MainWindow.open_updates_panel` /
  `arm_cli_install` wire it). A package-manager install does not auto-update and
  NO SETTING FIXES THAT — Claude Code knows a package manager owns its file and
  refuses to replace it (the documented tell: `claude update` on such an install
  replies "Claude is up to date!" regardless of the actual version). The install
  method IS the behaviour, so the only way to change the behaviour is to change
  the install. Hence a consented, reversible switch onto the documented native
  installer (`irm https://claude.ai/install.ps1 | iex`), which writes to
  `%USERPROFILE%\.local\`, needs no Administrator rights, and therefore NEVER
  touches the running winget file — **the migration is lock-free and may run
  with every agent alive**, which is the structural advantage over the startup
  gate above. Only CLEANUP keeps the old constraint (`winget uninstall` cannot
  remove a package whose `.exe` is running), so cleanup is SEPARATE, OPTIONAL
  and DEFERRABLE and never blocks the win. Rules, each from a concrete failure:
  * **THE CONTROL IS NEVER A BOOLEAN.** Label, modal and action are all a
    function of the detected `Situation` (`MANAGED` / `SELF_ACTIVE` /
    `SELF_PAUSED` / `NOT_APPLICABLE` / `LOCKED_BY_POLICY` / `MISSING`), so a
    user is never offered an action that does not apply. `SELF_ACTIVE` ⇄
    `SELF_PAUSED` is the PRIMARY undo (one `env` key, instant, no download,
    freezes them on exactly this version); the full revert to winget is
    deliberately SECONDARY, because conflating the two undos in one click is how
    a user who wanted to pause ends up reinstalling.
  * **`resolve_claude()` CHECKS THE NATIVE LAUNCHER FIRST**, and this is the
    most likely way to ship the whole feature broken. MEASURED: the winget
    package directory is on PATH DIRECTLY (not via a Links shim) and
    `%USERPROFILE%\.local\bin` is NOT on PATH, so `shutil.which` would keep
    answering with the STALE copy after a migration and AI Hive would go on
    launching the old binary. Deliberate consequence worth keeping: AI Hive
    never needs the installer's PATH edit, so no new terminal is required.
  * **...AND THAT IS NOT ENOUGH ON ITS OWN.** `build_spec` bakes `spec.program`
    once, so a card that existed BEFORE the migration would relaunch the winget
    binary for the rest of the process. `MainWindow.rebind_claude_specs` re-runs
    `providers.build_invocation` for every live Claude spec (the precedent is
    `AgentSpec.set_permission_mode`, which rebuilds `args` for exactly this
    reason). It must NOT restart/stop a RUNNING agent (the new path applies at
    its next launch, which is what lock-free bought us), must NOT emit `dirty`
    (`program`/`args` are derived; `to_dict` stores `user_program`), and is
    idempotent.
  * **A CHANNEL IS NEVER WRITTEN WITHOUT ITS FLOOR.** `stable` is a channel, not
    a ceiling, so setting it on a machine AHEAD of stable lets the next update
    move the user BACKWARDS — i.e. straight back into the stale-alias failure
    this feature exists to end. `set_channel` mirrors `/config`: to `stable` it
    writes `autoUpdatesChannel` AND `minimumVersion` = the version read off the
    FILE right then, and REFUSES (`ok=False`, writes nothing) when that version
    is unparseable; back to `latest` it REMOVES `minimumVersion` so the pin
    cannot outlive its reason. `requiredMinimumVersion` is a different key that
    stops Claude Code STARTING at all and is never written.
  * **`~/.claude/settings.json` HAS OTHER WRITERS AND THEY ARE OURS.** Every
    running agent's `/model` and `/config` writes it. `write_settings` therefore
    takes a MUTATION, not a finished document, and RE-READS immediately before
    the replace — atomicity stops a torn file, it does nothing about a LOST
    UPDATE, and the panel can sit open for a minute between the two. Same rule
    `session.json` follows, and for the same reason. A file that does not parse
    is REFUSED and never "repaired", including on the re-read.
  * **CLEANUP COUNTS AGAINST THE RECORDED WINGET PATH, NEVER
    `resolve_claude()`.** By cleanup time that function answers with the NATIVE
    launcher, so counting against it asks a question nobody asked: the user's
    own winget-launched sessions do not match, the count comes back zero, and
    `winget uninstall` runs against a file those sessions hold. That is
    under-counting, the direction `count_processes` documents as unsafe, and
    here it is unsafe twice (it either poisons the package database again or
    removes the rollback out from under a live session). A live CLI blocks and
    is NEVER killed.
  * `CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE=1` IS NOT SUPPORTED ANYWHERE. It
    makes a RUNNING Claude Code invoke `winget upgrade` on itself, the exact act
    that writes the false database record `cli_update.py` exists to prevent.
  * Everything here is DERIVED and TRANSIENT: the state is re-read from the
    filesystem and `~/.claude/settings.json` on every look, never stored (a
    remembered install method is wrong the moment the user installs anything by
    hand). NOTHING new goes in `session.json`; the only persisted key is still
    `ui.auto_update`, no `SESSION_VERSION` bump. The runner is INJECTED and
    armed from `main.py` alone (`arm_cli_install`), the same opt-in rule as
    `start_usage_polling()`. Outcomes are audited to `session.log`
    (`CLI-MIGRATE-STATE`/`-START`/`-OK`/`-FAIL`/`-REBIND`/`-PAUSE`/`-RESUME`/
    `-CHANNEL`/`-REVERT`/`-CLEANUP`); `REBIND` and the `exe=` on `CLEANUP` name
    the thing the line is ABOUT rather than what the app happened to resolve,
    because a cleanup reporting the native path is a cleanup that counted the
    wrong file.
  * The startup gate above is DEMOTED, not deleted. After a migration it no
    longer keeps the user current; it guarantees a downloaded update has LANDED
    before agents launch rather than "the next time you start Claude Code". It
    stays fully load-bearing for `agy`, which has no auto-updater and no native
    option, and remains the single enforcement point for: the binary changes
    only when nothing is holding it.
  * VERIFIED, by READING `https://claude.ai/install.ps1` rather than running it
    (111 lines, inspected 2026-08-11): it contains NO interactive construct at
    all (no `Read-Host`, `PromptForChoice`, `$Host.UI`, `-Confirm`,
    `Get-Credential` or console read), so it cannot park the install thread on a
    prompt with stdin closed. It resolves `latest`, refuses a version string
    that is not `N.N.N` (an HTML error page), downloads the platform binary,
    verifies its SHA256 against the SIGNED `manifest.json`, then shells to
    `<downloaded>.exe install`; every failure path is `Write-Error` + a non-zero
    exit, which `migrate` reads off the FILE anyway. `subprocess_runner` still
    passes `stdin=DEVNULL`, and the panel's Close remains the escape hatch.
  * STILL UNVERIFIED, and only answerable on a machine that has migrated: is the
    Windows native launcher a stub or the whole binary? The docs describe the
    launcher-into-`versions/` symlink indirection for macOS and Linux
    EXPLICITLY and say nothing equivalent for Windows, and the installer above
    delegates that step to `claude install` so the script does not settle it
    either. Run
    `(Get-Item "$env:USERPROFILE\.local\bin\claude.exe").Length` and
    `Get-ChildItem "$env:USERPROFILE\.local\share\claude\versions\"` right after
    the first migration and record the answer here. Hundreds of MB (the winget
    binary is MEASURED at 284,981,920 bytes) means the launcher IS the binary, a
    background update cannot replace it while agents run, and the startup gate
    stays load-bearing; a few KB with the bulk under `versions\` means updates
    apply side by side.
- **Plan usage is a LIVE READOUT and a HOOK POINT, never history**
  (`app/claude_usage.py`, Qt-free/stdlib-only like `chime.py`). The number comes
  from `GET /api/oauth/usage` with the account's OAuth bearer token — the same
  call the TUI's `/usage` makes ("fetchUtilization: GET /api/oauth/usage" is in
  the binary). There is NO CLI path (no `claude usage` subcommand), and the
  `statusLine` route — whose stdin payload also carries
  `rate_limits.five_hour.used_percentage` — is deliberately REJECTED: it would
  run a subprocess inside every agent's TUI render loop, exactly the launch-
  timing perturbation the SessionStart `startup` invariant above is about.
  `~/.claude.json` → `cachedUsageUtilization` carries the IDENTICAL shape (one
  `parse_utilization` serves both), and `claude_usage.read_cached` still parses
  it, but NOTHING IN THE APP CALLS IT ANY MORE and nothing may: a STORED NUMBER
  IS NEVER SHOWN. The cold-start seed was removed, and Gemini's own disk cache
  (`gemini_usage.cache_path`/`read_cached`/`write_cached`) was DELETED outright,
  because a 5-hour window is routinely spent and reopened between one launch and
  the next — so a restored figure is not merely old (observed 1.5 days and 10
  points out of date), it is wrong in the direction that misleads, and nothing
  on the bar distinguishes it from a live one. Every enabled pill instead opens
  in a LOADING state (`UsagePillBadge.mark_loading`, put up by
  `start_usage_polling`) and only ever shows a figure fetched this run;
  `gemini_usage.fetch` returns `error="no-data"` rather than reaching for disk.
  TOKEN HANDLING IS READ-ONLY: re-read `.credentials.json` per call
  (running agents keep it rotated for us), short-circuit on a past `expiresAt`
  instead of putting a dead credential on the wire, and NEVER refresh (that
  races the CLI's own refresh), write, log, or persist it. `fetch()` never
  raises — every failure becomes a `Usage` with `error` set, and a failed poll
  KEEPS the last good number on screen (greyed) rather than blanking a figure
  the user is reading; only `no-auth` with no prior reading hides the badge for
  good. A failure with NO reading to grey out shows the CAN'T-READ PILL
  (`PlanUsageBadge.mark_unreadable`, "usage limit unreadable — click to
  refresh") — the badge must never just disappear, which is indistinguishable
  from the feature having been deleted (reported as exactly that after a
  restart met an `http 429`; there is deliberately no on-disk seed to paint a
  number instantly, so the gap is reachable on any cold start). The GEMINI pill
  obeys this too, and did not before: `set_usage` used to hide the badge itself
  whenever a reading carried no matching window, with a blanket `try/except`
  hiding that it had, so an absent/erroring `agy` made the readout cease to
  exist with no way to ask it to retry. Failures now route through
  `TopBar.note_gemini_usage_error`, which greys an existing number
  (`mark_stale`) and only says "unreadable" when there is nothing to grey.
  VISIBILITY IS A PRODUCT OF TWO INDEPENDENT FACTORS, decided in exactly one
  place (`TopBar._sync_usage_pills`): `has_content()` (a reading, the can't-read
  pill, OR the loading state) AND the user's per-pill preference. NO badge class
  may call `setVisible` on itself. That split is what lets a pill the user
  CLOSED vanish — the point of the hover ✕ — without regressing the rule that a
  FAILED read must never look like a deleted feature; collapsing them back into
  one flag makes the two indistinguishable. A click resets `_usage_backoff`
  (`_on_usage_refresh`) so the user asking now isn't parked behind a 16-minute
  retry gap. CRITICAL, same rule as `activity_changed`/`waiting_changed`: a reading
  is TRANSIENT and must NEVER mark `dirty` — `_apply_usage` runs every minute
  for the life of the process, so wiring it to a save would rewrite
  `session.json` 60x an hour (only the `ui.usage_trackers` preference saves, via
  `_schedule_save`). THAT PREFERENCE IS PER PILL and has exactly ONE control:
  the hover ✕ on a pill and the `+` picker beside the auto-restart caption both
  emit the same `TopBar.usageTrackerToggled(key, on)` into the same
  `_on_usage_tracker_toggled`, so two controls of one setting can never
  disagree. The older single `ui.usage_visible` boolean (a right-click item that
  hid all three) is GONE; it is still WRITTEN as a derived mirror
  (`any(trackers)`) for one release so a downgrade cannot resurrect closed
  pills, and `_restore_ui_state` prefers `usage_trackers` and reads
  `usage_visible: False` as "all trackers off" only when the newer key is
  absent. Additive optional keys inside `"ui"`, so NO `SESSION_VERSION` bump.
  The `+` button must NEVER be hidden — not by `set_recovery_available(False)`
  (a Gemini-only user has no Claude login by definition), not by every tracker
  being off — or there is no way back. Closing both Gemini pills SKIPS the
  Gemini poll entirely (`_gemini_wanted`, checked in `start_usage_polling` and
  again at the top of `_poll_gemini_usage`), which is sound only because
  `_gemini_usage` has no consumer besides those two pills and
  `_retune_gemini_usage_poll`; the CLAUDE poll is NEVER gated this way, because
  `planLimitReached`/`planLimitCleared`/`plan_usage()`/`_arm_reset_poll` all
  hang off that reading and auto-continue depends on them. Re-enabling a tracker
  re-arms and fetches at once, but only when `self._polling` is set — which
  `start_usage_polling` alone does, so the offscreen suite can toggle trackers
  without ever shelling out to the user's real `agy`. Polling is OPT-IN — `main.py` calls
  `MainWindow.start_usage_polling()` exactly like it sets `quit_on_close`,
  because the smoke suite shares `create_main_window` and must never touch the
  network or the user's real account; tests drive `_on_usage_ready` with
  synthetic readings. THE GEMINI READOUT OBEYS BOTH HALVES OF THIS, and every
  one of them was learned the hard way: `gemini_usage.fetch()` shells out to
  `agy --print /usage`, which MEASURES ~3.0s, and it was called INLINE on the
  GUI thread — once in `MainWindow.__init__` and TWICE per tick, because
  `_retune_gemini_usage_poll` fetched its own copy. That froze the entire app
  for ~6s a minute (the urgent rate is 10s, i.e. shorter than one call), with
  the CPU IDLE because it is blocked on a subprocess rather than computing —
  reported as constant stuttering, and the reason "my CPU is not maxed out"
  was the correct observation. It also made the offscreen suite shell out to
  the user's real CLI and added ~3s to EVERY window it builds, which is what
  started breaking elapsed-time-sensitive checks in unrelated sections. So:
  `_poll_gemini_usage` kicks a daemon thread and emits `_geminiUsageReady`
  (queued) exactly like `_poll_usage`/`_usageReady`; `_gemini_usage_inflight`
  guards it because a 6s CLI timeout outlasts the urgent interval and the timer
  would otherwise stack threads; the retune TAKES the reading; and the timer is
  armed in `start_usage_polling`, never in `__init__`. Do not "simplify" any of
  those back to an inline `fetch()`. AND IT RIDES ITS OWN, MUCH SLOWER CLOCK
  (`GEMINI_USAGE_POLL_MS` 5 min / `GEMINI_USAGE_URGENT_POLL_MS` 60s), because
  every Gemini tick SPAWNS A PROCESS where a Claude tick makes a request. On
  some runs `agy` starts a nested helper that asks Windows for its OWN console;
  `CREATE_NO_WINDOW` is passed and is NOT ENOUGH, because spawn flags do not
  reach a GRANDCHILD — MEASURED on a deterministic reproducer, a descendant that
  demands a console gets a VISIBLE one 8/8 times under `CREATE_NO_WINDOW`,
  `CREATE_NEW_CONSOLE`+`STARTUPINFO(SW_HIDE)` and `CREATE_NO_WINDOW`+
  `STARTUPINFO(SW_HIDE)` alike. With Windows 11 delegating to Windows Terminal
  that console appears as a real window flashing over the user's screen (live:
  ~6% of polls, 2 of 32, i.e. every quarter hour at 60s — reported as "a
  terminal keeps popping up and I can't read it"). Since NO flag suppresses it,
  asking less often is the only lever, and it costs nothing: only the two pills
  consume this reading, and a Gemini cut-off recovers on its own printed
  countdown, never on the account reading. Do NOT fold these back onto
  `USAGE_POLL_MS` — that constant answers to `planLimitReached`/
  `planLimitCleared` and the reset poll they arm, none of which exist for
  Gemini. The BLOCKED state (`Usage.blocked`, utilization >= 100 —
  derived from the number, NOT from the payload's server-side `severity`
  string) is the machine-readable half: `MainWindow.planLimitReached(Limit)` /
  `planLimitCleared()` are edge-triggered and level-correct like the chime, and
  `plan_usage()` exposes the latest reading, so features that ACT on being cut
  off (e.g. relaunching blocked agents unattended when the limit resets) hook
  those instead of scraping a terminal. While blocked, `_arm_reset_poll`
  schedules one extra poll just after the stated reset so the cleared edge
  fires within seconds at 4am rather than waiting out the minute timer.
  The POLL GAP IS ADAPTIVE, and both halves of the condition matter
  (`_usage_poll_interval`/`_retune_usage_poll`): the base is
  `USAGE_POLL_MS` (60s), but a headline window at/over `USAGE_URGENT_PCT` (90)
  *while at least one agent `is_busy()`* drops to `USAGE_URGENT_POLL_MS` (20s),
  because several agents streaming can spend the last few percent in far less
  than a minute and NOTHING in the app knows it is cut off until a READING says
  so. Under 90 there is nothing imminent; with every agent idle the number
  isn't moving, so a faster poll would only re-ask the same question; and a
  window already at 100 goes back to the slow rate because `_arm_reset_poll`
  already covers the reopening. So the fast rate is short bursts at the end of
  a window, never a permanently tripled request rate. The 429 backoff
  MULTIPLIES whatever the situation asks for, so it still wins. Retuning
  happens on each reading and on the countdown tick (`_tick_usage`, 20s) rather
  than off `activity_changed` — that fires every couple of seconds per agent,
  and `QTimer.setInterval` RESTARTS a running timer, so a retune per busy
  flicker would reset the countdown forever and the poll would never fire at
  all; `_retune_usage_poll` therefore only touches the timer when the interval
  actually changes.
- **A usage pill is exactly as wide as its text, by ONE formula**
  (`ornaments.UsagePillBadge._measure_width`: `_PAD*2 + _RING + _GAP +
  advance(text)`, measured with `_text_font()` — the font `paintEvent` actually
  draws with, never the widget's QSS font, or the pill is sized for text of a
  different size). `PlanUsageBadge` and
  `GeminiUsageBadge` are both subclasses and supply only the TEXT; the Gemini
  pills previously carried a hardcoded `_FIXED_WIDTH = 315` and elided into it,
  which reserved 630px of the bar for two readouts whose real content is ~215px
  each, truncated anything longer, and drifted from the Claude pill sitting
  beside them. Do not re-copy the formula into a subclass. The ✕ is NOT a term
  in that formula: it FLOATS over the tail of the text, which fades out under
  it (`_paint_close_scrim`, the pill's own background rebuilt opaque and
  clipped to the rounded outline) for as long as the pointer is inside. An
  earlier cut RESERVED a permanent slot for it, which left ~20px of every pill
  blank for the 99% of the time nobody is hovering. THE INVARIANT THE RESERVED
  SLOT WAS PROTECTING STILL HOLDS AND STILL MATTERS: the pills sit after the
  layout's `addStretch(1)`, so a pill that grew on hover would shove the entire
  right-hand cluster (recovery caption, LED toggles, theme combo, font steppers,
  Add Terminal) sideways as the pointer crossed it. Overlaying keeps it for
  free — `_measure_width` reads the text alone and hovering only repaints — so
  do not make the width depend on hover state. The ✕ is a child
  `QToolButton`, NOT a rect hit-tested in `mousePressEvent`, so it consumes its
  own press and closing can never be mistaken for the click-to-refresh
  affordance that same handler owns. Loading strings must stay SHORTER than the
  finished line, so a pill only ever grows when its reading lands.
- **Auto-continue consumes that edge; the SCREEN says who to resume**
  (`MainWindow._resume_blocked_agents`, wired to `planLimitCleared`). The usage
  reading is ACCOUNT-wide — it knows the plan is out and until when, but never
  WHICH agents were mid-turn — so attribution comes from
  `TerminalAgent.is_limit_blocked()`, a LATCH set by `_scrape_limit` on EVERY
  output burst (`_on_pty_output`), gated on `_prompt_ready` so a `--resume`
  replay of an OLD banner is read as history, not a live cut-off. Do NOT move
  this back behind the idle-timer settle like `_screen_waiting`: the banner
  lands right after the user submits a prompt, which is exactly when
  `_mark_busy` treats output as keystroke echo and never arms that timer — and
  a silently parked agent then produces nothing more to arm it, so the settle
  never comes (this cost a second live miss). A half-drawn menu is ambiguous;
  "You've hit your … limit" is not. Do NOT "simplify" this back to scraping at
  the reset edge either
  (it was written that way first and cost a full night's unattended work):
  `_screen_tail` is a 4000-char ROLLING buffer and Claude's TUI keeps redrawing
  its input box while parked, so hours later the banner has been evicted and
  the tail holds only the bottom of a frame — the re-scrape matched nothing and
  resumed nobody. The patterns live in Qt-free `app/limit_banner.py` because
  `transcripts` matches the SAME thing off disk. Two signals, and which one you
  use matters: `LIMIT_MENU_RE` ("Stop and wait for limit to reset") is the
  interactive menu, so it PERSISTS for as long as the agent is stuck —
  `recheck_limit` must key on it ALONE, since the banner is scrollback that
  lingers after a successful resume and would report "still blocked" forever.
  `LIMIT_HIT_RE` matches ONLY the exhausted banner, never `Approaching …` /
  `You've used N% …` (those mean the agent is still WORKING and nudging it
  would interrupt it); it is the weaker live signal but the ONLY one a
  transcript records.
  CRITICAL, the other half of that asymmetry: a BANNER ON ITS OWN IS NOT PROOF
  OF A LIVE CUT-OFF. It is ordinary output that stays in view — and is redrawn
  with every frame — long after the agent has been resumed off it, so once
  `recheck_limit` cleared the latch the very next burst re-latched on the SAME
  line, and `parse_reset_clock` dated it 24 h out because its clock had just
  passed. A phantom latch mutes that agent's "?" chime (limit-blocked agents
  deliberately don't ring) and later types a stray `Continue` into an agent that
  is working fine — twice on 2026-08-04, once 13 s after a verified RESUMED.
  So `_scrape_limit` latches a banner with NO MENU only when the line DIFFERS
  from `_limit_last_banner`, the one that produced the previous latch;
  successive 5-hour windows never end at the same wall time, and a genuine
  cut-off renders its menu directly below the banner (hence in view whenever the
  banner is), so this suppresses only the echo. `_limit_last_banner` therefore
  SURVIVES `clear_limit_block` and is reset by `start`/`restart` alone — and
  `mark_limit_blocked` MUST set it too. That was missed at first, so a latch
  recovered from DISK armed no guard at all: the moment `recheck_limit` cleared
  it on a successful resume, the very next burst re-latched on the same banner
  still on screen and dated it 24 h out (live, 2026-08-07: `RESUMED` 22:29:42,
  `BLOCKED … resets 2026-08-08 21:30` at 22:29:43). Comparing the LINE is
  enough because both sources normalize through the same
  `limit_banner.banner_line`; the transcript record and the live re-latch
  carried byte-identical text.
  THE SCAN WINDOW IS COUNTED IN CONTENT, NOT LINES (`TerminalAgent._tail_lines`,
  the one helper all four screen-scan call sites now share). Claude's TUI pads
  its frame with blank rows, so a raw `[-40:]` slice spans as little as 432
  characters and 5 non-blank lines — measured on real screen snapshots, against
  a median ~900 characters per frame repaint, i.e. less than half a frame. A
  genuine cut-off was therefore invisible to BOTH the per-burst scrape and the
  settle scrape 2 s later, and left no trace at all because `_note_limit_skip`
  reads the same window (2026-08-07, CVsummer2026: zero `LIMIT` lines for an
  agent whose transcript ends on the banner). `_scrape_limit` and
  `_note_limit_skip` therefore pass `skip_blank=True` and MUST stay in
  agreement. The other two callers keep RAW lines deliberately:
  `_screen_waiting`'s 18-line bound plus its caret requirement is the tuning
  that keeps the "?" chime off an agent's own numbered prose, and
  `recheck_limit` wants the menu that is TORN DOWN on a resume — the raw tail
  still holds that menu's earlier renders, so reaching further back would find
  it forever and report "still blocked" on an agent already going again.
  TWO triggers land in `_resume_blocked_agents`, and the second is the one that
  must be reliable: (1) `planLimitCleared` resumes every latched agent (the
  ACCOUNT is provably clear); (2) `_check_limit_resets` on `LIMIT_WATCH_MS`
  resumes an agent once the reset time ITS OWN banner stated
  (`limit_resets_at`, parsed by `parse_reset_clock`) has passed. The API edge
  alone is NOT sufficient — it fires only if the SAME process also observed the
  blocked state first, and `/api/oauth/usage` 429s intermittently (observed:
  two in a row, then a 200), so a restart or a few bad polls silently skips the
  resume entirely. The watchdog needs neither the network nor process
  continuity. Relatedly `_on_usage_ready` backs the poll off exponentially on
  429 (`_usage_backoff`) — a minute timer firing into a rate limit is how the
  app can go hours never seeing `blocked` at all.
  Delivery is Esc (close the limit's options menu) then the text a beat later
  (`AUTO_CONTINUE_ESC_MS`), agents staggered by `AUTO_CONTINUE_STAGGER_MS` so
  they don't all pile into the freshly reopened window. Because BOTH triggers
  can fire within the same second (the reset poll is armed for exactly then),
  a scheduled-but-undelivered resume must be visible to the second pass:
  `MainWindow._resume_pending` claims the agent id at SCHEDULE time and
  releases it only once `note_limit_attempt` has run — `limit_retry_ready`
  alone cannot do this, since the attempt is recorded up to a full stagger
  after the trigger, which is how three of four agents got `Continue` typed
  twice at 05:30 on 2026-08-04. CRITICAL: the text goes
  through `TerminalAgent.nudge`, NEVER `deliver_task` — `deliver_task` is the
  ASSIGN path and would overwrite `current_task` (persisted, shown in the
  sidebar and on the board), flip the assignment to WORKING and re-infer the
  role. `nudge` also deliberately does NOT stamp `_last_input_ts` (unlike
  `write`, which the Esc correctly uses), so the resumed work still pulses the
  sidebar instead of being mistaken for the user's own typing. A nudge is then
  VERIFIED, not assumed (`recheck_limit` after `AUTO_CONTINUE_VERIFY_MS`), with
  bounded retries (`LIMIT_RETRY_S`, `LIMIT_MAX_TRIES`): a resume can land while
  the window is still shut, and clearing the latch on the nudge itself — as
  this first did — burns the only attempt and parks the agent for good. A
  `nudge` refused because the TUI isn't ready must NOT consume an attempt.
  BUT WAITING FOR READINESS MUST BE BOUNDED, because readiness can never
  arrive on its own. Qt gives a `QStackedWidget` page NO `resizeEvent` until it
  is made current (verified), so a card in a workspace the user has not opened
  never fires `TerminalView.sizeChanged`, never calls `agent.resize`, and never
  asks its child to redraw. Combined with readiness being decided ONCE PER
  BURST against a 600-char `_ready_tail` — a footer followed by more than that
  in the same burst is simply missed — an agent could sit un-nudgeable
  indefinitely: `WAIT (TUI not ready)` every minute, ending only when the user
  happened to click that workspace (live, 2026-08-07: nine ticks over ten
  minutes). Two independent repairs, and both are needed because they cover
  different halves: `_on_idle_timeout` RE-CHECKS readiness against the
  4000-char `_screen_tail` (this fires 2 s after output settles, so it can only
  ever flip readiness LATE — it cannot perturb launch or first-task-submit
  timing, which is the whole point of the SessionStart invariant), and after
  `LIMIT_REPAINT_AFTER_WAITS` quiet ticks `MainWindow._auto_continue_agent`
  calls `TerminalAgent.request_repaint()` ONCE, which sends the child one
  column narrower and back (`REPAINT_RESTORE_MS`) to force a full frame. The
  repaint is reachable only by an agent already latched on a cut-off whose
  reset has passed, so it can never poke a normally launching TUI.
  Related: an agent parked on the limit raises the "?" (its menu is exactly
  what `_screen_waiting` looks for) but must NOT ring the chime — it is not a
  question the user can answer, and it would wake them at 4am for something
  auto-continue is about to handle.
- **A cut-off agent is visible everywhere, live, via one ⏳ hourglass motif** —
  the card header (`#CardLimitMark`), the sidebar's inline agent-dropdown row
  (`AgentRow.limit_mark`, `#WsAgentLimit`), and a workspace-row count badge
  (`WorkspaceRow.limit_badge`, `#WsLimit`, text `⏳N`) that mirrors the "?"
  badge's live wiring and is hidden at `N==0`. The three paths read the SAME
  state two different ways: `AgentRow.refresh()` and the workspace badge are
  POLLED (`agent.is_limit_blocked()` / `workspace_stats()["limit_blocked"]`,
  the latter a plain count like `busy`/`waiting`), while the card header is
  purely SIGNAL-driven off `limit_blocked_changed`. That split is exactly why
  `clear_limit_block()` MUST emit the falling edge (`limit_blocked_changed(
  False)`, guarded on `was_blocked` so `start()`/`restart()` calling it
  unconditionally on an already-clear agent stays silent): a poll always
  catches a resume on its next tick, but a signal-only consumer that only ever
  saw `emit(True)` (the pre-fix state) never learns the agent came back, and
  the hourglass sits there stale forever after a real auto-continue or manual
  restart — this was a live-reported bug, not hypothetical. `workspace_stats`'s
  `limit_blocked` count and its `_recompute` refresh on BOTH edges too — do not
  special-case it to the rising edge only. `WorkspaceManager.agentLimitBlocked`
  is the one exception, and deliberately so: it stays rising-edge-only because
  it feeds the ledger/audit trail (`_on_agent_limit_blocked`), which records
  the cut-off happening, not its resolution — firing it on the falling edge
  too would file a phantom second "BLOCKED" entry on every resume.
- **The OTHER half of recovery reads the TRANSCRIPT, because the screen lies
  after a restart** (`MainWindow.recover_blocked_at_startup`, `⏯` toggle). A
  restarted agent redraws a REPLAYED conversation, which `_scrape_limit`
  correctly ignores as history — so the live latch can never see a cut-off that
  happened before this run. `transcripts.ended_on_limit` supplies it instead:
  the LAST assistant record being the banner means the conversation stopped
  there ("last" is the safety — anything said afterwards means it carried on).
  CRITICAL: the banner's clock is BARE ("resets 3am"), so the reset MUST be
  anchored to the record's own timestamp (`limit_banner.banner_reset_at`);
  resolving it against the current clock lands on the NEXT 3am and stalls the
  agent a full day. Startup recovery only ARMS (`mark_limit_blocked`) —
  delivery stays with the single watchdog, so a freshly launched TUI is waited
  out rather than poked. It scans EVERY agent in every workspace, running or
  not, and files every cut-off it finds; two rules then decide what it acts on.
  (1) It STARTS a stopped card whose transcript shows a cut-off — it was the
  LIMIT that stopped that agent, not the user, so leaving it alone drops
  exactly the work this exists to rescue. It must set `spec.resume` first:
  `start()` mints a fresh session id for a non-resume launch, which would open
  an empty conversation and abandon the transcript that proved the cut-off.
  This is a deliberate reversal of the older "a card left stopped stays
  stopped" rule. (2) It acts only on the MOST RECENT window
  (`limit_ledger.latest_window`), never on an age bound — a bound asks how OLD
  a cut-off is when what decides is whether it was the LAST thing that
  happened, and the old 36 h `STARTUP_RECOVERY_MAX_AGE_S` had begun silently
  skipping real cut-offs. Agents stopped by one window all state the same reset
  clock, so they group; a cut-off the ledger already CLOSED is never revived
  (without which an abandoned conversation whose transcript still ends on the
  banner would be resumed afresh on every launch). Each latch records
  its ORIGIN (`limit_from_startup`) and is gated by the toggle that owns it —
  `ui.startup_recovery` for disk-recovered, `ui.auto_continue` for live — so
  switching one off can never strand a latch the other created. Both are
  ordinary UI preferences that save via `_schedule_save` (like `usage_visible`);
  the latch itself is NEVER persisted — the transcript is the durable record,
  and a persisted flag would go stale. `recover_blocked_at_startup` is OPT-IN
  from `main.py` (after `autostart_active_workspace`, so the ordinary restore
  runs first and this only starts the stragglers) exactly like
  `start_usage_polling`: it reads the user's real transcripts and types into
  real agents, which the smoke suite must never do.
  The whole path is audited to `session.log` via `_limit_audit`
  (`STARTUP-SCAN`/`STARTUP-SKIP`/`STARTUP-START`/`BLOCKED`/`NUDGE`/`WAIT`/
  `PHANTOM`/`RESUMED`/`STILL-BLOCKED`/`GAVE-UP`) — this feature failed silently
  TWICE and both causes had to be reconstructed from transcript timestamps
  hours later; do not remove it. `NO-LATCH` (`TerminalAgent._note_limit_skip`,
  routed through `WorkspaceManager._wire_agent` → the manager's `audit` hook)
  completes it from the other end: every line above describes something that
  happened AFTER a latch, so the decision NOT to latch — the one that actually
  strands work — used to leave no trace at all, and a third live miss could
  only be narrowed by elimination, never explained. It is bounded twice over
  because `_scrape_limit` runs on EVERY output burst: it says nothing unless a
  banner is genuinely on screen, and it repeats only when the (reason, banner)
  pair CHANGES, so one frame repainted hundreds of times is recorded once
  (`_limit_last_skip`, reset by `start`/`restart` alongside
  `_limit_last_banner`). Keep both bounds if you add a rejection reason.
- **The cut-off itself is a HISTORICAL FACT and is kept** (`app/limit_ledger.py`,
  Qt-free/stdlib-only, `<session-dir>/limit_events.jsonl`). Every other piece
  of this feature is transient on purpose, which left nothing able to answer
  "what was interrupted last night, and did it recover" after a restart. The
  ledger is append-only (an append cannot corrupt what is already there, and it
  is written exactly when things are going wrong), records one `cut_off` per
  episode with workspace, agent, task, LOCAL timestamp, window and reset time,
  and closes it with a `resumed`/`failed`/`dismissed` outcome. It is NOT
  session state — nothing here goes in `session.json`, so the "a latch is never
  persisted" rule stands. CRITICAL, the identity: `TerminalAgent.id` is a fresh
  uuid on every load and display names aren't unique, so a cut-off is keyed on
  (cwd, session_id, RESET time) — `limit_ledger.key_of`. The reset is the one
  number both sources resolve identically (the live screen parses the banner as
  it is drawn; the startup scan parses the same banner off disk anchored to its
  own timestamp), whereas the two NOTICE times never agree, so keying on those
  would file one cut-off twice and re-resume work already recovered.
- **Two agreeing sources before anything is typed.** The screen latch says an
  agent was cut off; `transcripts.limit_cut_off` must not contradict it at
  nudge time (checked THEN, not at latch time — the banner may not be flushed
  the instant it is drawn, but by reset time it certainly is). That function is
  deliberately TRI-STATE: only a transcript that demonstrably CARRIED ON refutes
  the latch (`PHANTOM` → `dismissed`), while one that cannot be read — a
  drifted pin, an unflushed conversation — is no evidence either way and must
  never strand a genuine cut-off. `ended_on_limit` collapses both to False,
  which is right only for a caller wanting positive evidence.
  A cut-off is also refuted when the turn behind the banner was Claude Code's
  OWN plumbing rather than anything anyone asked for — a background task's
  completion notification can start a brand-new turn with no input from the
  user or AI Hive, and if the account runs out right then it eats the same
  menu a real interruption would, with nothing of substance lost (that
  auto-nudged an agent hours after its work was done). `transcripts.
  _SYNTHETIC_USER_TAGS` is an ALLOWLIST of those injected tags and must stay
  one: keying off a leading `<` alone also swept in `<command-name>`, i.e. a
  slash command the USER typed, whose work is exactly as real as any prompt's.
  There are TWO checks and they are gated differently ON PURPOSE.
  `_auto_continue_agent`'s runs at reset time and may dismiss on a plain
  `not cut_off`. `MainWindow._dismiss_if_phantom` (`LIMIT_PHANTOM_CHECK_MS`
  after the LIVE latch, so a phantom never even shows the hourglass) may NOT:
  a running agent's transcript always exists, so a banner Claude has drawn but
  not yet WRITTEN reads as `cut_off False`, not None — and clearing the latch
  on that races away a genuine cut-off with no way back, since a silently
  parked agent emits nothing to re-latch on. It therefore gates on the
  POSITIVE `synthetic` field (a banner that WAS found, refuted by the turn
  behind it), never on the absence of a cut-off. Any new early consumer must
  do the same.
  A reset read off the SCREEN also gets `LIMIT_RESET_GRACE_S` of slack (the
  banner names a minute, not an instant, and a nudge into a still-shut window
  spends one of very few retries); a reset from the ACCOUNT reading needs none,
  since headroom means the window is provably open. And a `weekly` window
  (`limit_banner.banner_window`) is NOT readable off the screen at all — its
  banner prints a bare wall clock for a reset that can be days out, which
  `parse_reset_clock` can only ever resolve to the next occurrence, so a weekly
  cut-off ignores its clock and waits for the account reading.
- **GEMINI IS CUT OFF IN A DIFFERENT SHAPE, and every difference removes a
  guard the Claude path leans on.** The whole recovery path used to be gated on
  `provider == "claude"`, so an agy agent that ran out of quota simply sat there
  (live, 2026-08-09). Callers now gate on `limit_banner.LIMIT_PROVIDERS`, and
  the ONE place the two diverge is `TerminalAgent._read_limit_screen` — keep it
  that way; everything downstream (`workspace_stats["limit_blocked"]`, the
  hourglass, the ledger, the watchdog) is provider-agnostic already. What agy
  prints is `⚠ Individual quota reached … Resets in 1h40m21s.` + an `Error ID:`
  line, and then it RETURNS TO ITS PROMPT. So: (1) THERE IS NO MENU, which is
  Claude's primary signal precisely because it persists while the agent is stuck
  and is torn down on a resume — the Gemini path therefore always goes through
  the identity guard, and `recheck_limit` asks the question backwards, treating a
  NEW message (a still-spent quota answers a nudge with one) as "still blocked"
  and the latched one merely lingering as "resumed"; that newer message also
  REPLACES the latched reset, or the watchdog retries instantly and burns the
  whole budget. No Esc is sent either — there is no menu, and an Esc would clear
  whatever the user had half-typed. (2) THE CLOCK IS A DURATION, which is
  strictly better (no rollover guess, and it is equally usable for a window days
  out, so there is no `weekly` ambiguity — the window is just "quota"), but it
  MUST be resolved to an epoch the moment the message is read, since the printed
  text says "1h40m21s" forever. (3) THE MESSAGE WRAPS, so the countdown and the
  Error ID land on continuation rows and the identity of the cut-off is NOT on
  the matched line: `gemini_banner_line` returns the matched line JOINED with
  those rows, which makes the identity sharper than Claude's wall clock (the
  Error ID is unique per message) — do not "simplify" it back to one line.
  (4) THERE IS NO CONVERSATION ON DISK (`~/.antigravity` holds `argv.json` and
  `extensions`, nothing else), so the transcript corroboration that refutes a
  phantom Claude latch has no Gemini equivalent. THE REPLAY IS THEREFORE THE
  RECOVERY, not something to be filtered out (`_in_launch_replay` /
  `_replay_cut_off`), and that inverts the Claude rule: `_scrape_limit` rejects a
  replayed banner by requiring `_prompt_ready`, which works for Claude because
  readiness IS the prompt footer, but agy sends bracketed-paste-enable BEFORE
  its trust dialog (verified by driving agy through a pty), so a resumed Gemini
  agent is "ready" while `--continue` is still redrawing the conversation it
  ended on — and since the live latch is never persisted and there is no
  transcript, that redraw is the ONLY surviving evidence of an overnight
  cut-off. So a quota message inside `LIMIT_REPLAY_S` of launch LATCHES, with
  `limit_from_startup=True` (it is a startup recovery, and answers to that
  toggle). TWO corrections make it usable, both from a live miss on 2026-08-09:
  its FROZEN COUNTDOWN IS DISCARDED and the agent probed at once, because "1h39m
  56s" is stale by EXACTLY THE AGE OF THE MESSAGE — printed ~17:25 (reopening
  ~19:05), re-read on a 20:15 restart, dated 21:54, nudged 21:57:58: 2h50m of an
  idle agent — and there is nothing on screen to date it with. That is safe only
  because a refusal SELF-CORRECTS: a still-spent quota answers with a whole new
  message whose countdown is exact, which `recheck_limit` adopts — so guessing
  early costs one message and guessing late costs hours. And it must be the LAST
  thing on the screen (`LIMIT_REPLAY_TAIL_LINES` of content, the screen-side
  equivalent of `transcripts.ended_on_limit`), or a cut-off the conversation
  already recovered from would be "continued" over finished work. An earlier
  attempt to solve this from the other end — `_seed_limit_history`, adopting the
  on-screen message as history at the readiness edge — is REMOVED: it never
  fired, because readiness arrives before the replay is drawn, and it would have
  suppressed the very recovery this needs. Finally, the CLAUDE account reading
  says nothing about a Google quota: `_resume_blocked_agents`'s no-`due`
  shortcut (the `planLimitCleared` edge) is Claude-only by construction, the
  late-fill in `_check_limit_resets` and the borrow in `_on_agent_limit_blocked`
  likewise, and a Gemini latch resumes solely on its own countdown.
- **A scheduled message is a DEFERRED ENTER, not an assignment**
  (`app/scheduled_send.py`, Qt-free/stdlib-only like `limit_banner.py`).
  `Ctrl+Shift+Enter` in a pty terminal hands `TerminalView._input_text()` up
  through `TerminalCard`/`WorkspacePage` to `MainWindow._on_schedule_message`,
  which opens `ScheduleMessageDialog`; the message is held on the AGENT
  (`TerminalAgent._scheduled`) and typed in later by `MainWindow._tick_schedules`
  (`SCHEDULE_TICK_MS`). This exists so agents can be chained while the user is
  AFK. Several rules are load-bearing:
  * **Delivery is `nudge`, NEVER `deliver_task`** — same distinction the
    auto-continue makes, for the same reason: `deliver_task` overwrites the
    persisted `current_task`, flips the assignment to WORKING and re-infers the
    role. The user deferred an Enter; they did not assign anything.
  * **The chord cannot be `Ctrl+Enter`** — that inserts a newline
    (`terminal_view._sequence_for`), which is how multi-line input works in
    Claude Code. Hence the third modifier, as with `Ctrl+Shift+A`. The keypress
    sends NOTHING to the child; the input box is cleared (double-Escape, via
    `write` so the echo isn't mistaken for work) only once something is
    actually queued, so a cancelled dialog leaves the typing alone.
  * **The prefill is INFERRED from the painted input box**, which can come up
    short on a long horizontally-scrolled line — so it is shown back in an
    editable box rather than scheduled blind. That is why this is a dialog and
    not a silent hotkey. Do NOT "streamline" it into an immediate schedule.
  * **An overdue message is MISSED, never sent late.** `restore_scheduled`
    marks anything already past due on load, and `_deliver_scheduled` gives up
    after `SCHEDULE_GIVE_UP_S`. A 3am message firing at 10am into a conversation
    that has moved on is a surprise and real quota spent; the entry is KEPT and
    surfaced (the `missed` chip state) so the loss is visible rather than
    silent. A refusal itself is not a failure — a stopped agent, a booting TUI,
    or one parked on a limit menu (where the text would land IN the menu) is
    retried on the next tick.
  * **The queue IS persisted** (`_agent_dict`'s `"scheduled"`, only the PENDING
    ones; `_agent_dict_safe` carries it too), so `scheduled_changed` is wired to
    `_touch` (dirty) — the one indicator-shaped signal here that is not
    transient. No `SESSION_VERSION` bump: it is an additive optional key like
    `task`. CRITICAL, the other half: the per-second COUNTDOWN must never reach
    the model — `_tick_schedules` repaints `TerminalCard.refresh_schedule` and
    nothing else, or `session.json` would be rewritten 3600 times an hour (the
    `activity_changed` rule). And `_sync_schedule_timer` only touches the timer
    when the desired state DIFFERS from `isActive()`: it is driven by
    `workspaceStatsChanged`, which fires every couple of seconds per busy agent,
    and `QTimer.start()` RESTARTS a running timer — the exact trap
    `_retune_usage_poll` documents.
- **Theming is a skin registry** (`app/ui_theme.py`): each skin is a `Theme`
  in `THEMES`; `apply_theme(id)` rewrites the module-level `Palette` attrs,
  the `ANSI_16` list (IN PLACE — same object), and the font globals, so every
  existing `Palette.X`/`ANSI_16` read across the app follows the active skin
  with no per-call-site change. A live switch = `apply_theme` →
  `app.setStyleSheet(build_qss(...))` → `repolish` the widget tree →
  `terminal.update()`. The active skin persists in `session["ui"]["theme"]`
  and is restored in `_restore_ui_state` BEFORE the stylesheet is built.
  EVERY skin must keep its text legible: a smoke test asserts each theme's
  chrome tokens meet WCAG contrast on their backgrounds, and the terminal
  renderer (`terminal_view.legible_color`) clamps any glyph below
  ~3:1 contrast up to ~4.5:1 (hue preserved) so a child's dark-tuned output
  can never go invisible on a light theme. When adding a skin, run the suite —
  a low-contrast token fails the contrast test.
  Adding a skin = adding one `Theme(...)`; keep `scriptorium-dark` byte-stable
  (it's the shipped look and the regression baseline). Manuscript fonts
  (Cinzel/EB Garamond/Spectral) are bundled OFL TTFs in `app/assets/fonts`,
  registered by `main._load_bundled_fonts`; themes name them with a serif
  fallback chain so a missing file degrades gracefully.
- **A stopped card shows its CONVERSATION, not a black rectangle**
  (`app/screen_snapshot.py`, Qt-free/stdlib-only like `chime.py`). "Restore as
  it was" used to restore only the PROCESS state, so a reopened hive was a
  wall of dead terminals with a centred "terminal not running" box over each
  one. `closeEvent` now writes each pty agent's raw VT tail
  (`TerminalAgent.pty_replay()`) to `<session-dir>/screens/<key>.vt` and
  `create_main_window` seeds it back via `seed_pty_replay` BEFORE building the
  window: `TerminalCard.__init__` replays `pty_replay()` in its constructor,
  so seeding after that leaves the launch cards blank. The RAW STREAM is kept,
  not the transcript (`transcripts.py` already backs those up) — replaying the
  bytes through the same pyte screen that drew them reproduces what was there;
  re-rendering a jsonl transcript would not look like the TUI. It is NOT
  session state: half a megabyte of escape codes per agent has no business in
  `session.json`, so these are plain files beside the transcript backups, and
  the "a latch is never persisted" style rules are unaffected. Identity is
  (cwd, pinned session id) via `key_of` — `TerminalAgent.id` is minted fresh
  every load (same reason `limit_ledger.key_of` avoids it), and keying on the
  conversation makes staleness self-correcting: a pin that moved on simply
  misses, so a card never shows another chat's screen. `prune` drops unclaimed
  keys, because every `/clear` mints a new conversation and the directory
  would otherwise only grow. THE SNAPSHOT SERVES THE STOPPED CARD ONLY: an
  agent that comes back RUNNING gets a CLEAN terminal that its child fills in
  a few seconds, which is what a restored hive looked like before snapshots
  existed. `TerminalCard._on_status` drops the restored screen (its own, and
  the agent's via `TerminalAgent.drop_seeded_screen`, or a card rebuilt by a
  retile would replay the same stale seed under the child) the moment the
  agent starts while `_pending_replay` is STILL SET — i.e. this card has
  never re-rendered it at a settled size. Both launch paths that start an
  agent (`autostart_active_workspace`, `recover_blocked_at_startup`) run
  SYNCHRONOUSLY right after `show()`, ahead of `TerminalView`'s 120 ms resize
  debounce, so that is every agent restored running: leaving the seed there
  parked each of their cards on a mangled ~24-column fragment of last
  session's screen until the TUI finished booting (reported twice, and the
  reason it is not enough to fix the RE-RENDER: a launching child writes
  within milliseconds, so any guard that defers to a live child leaves the
  bad frame up). `_pending_replay` is the right test because a card WOKEN by
  a keystroke has long since consumed it, so the wake path below is
  untouched. TWO subtleties, both live-found: (1) pyte drops
  lines off the TOP when it shrinks, and the tiling grid resizes a card AFTER
  it is built, so the newest part of a restored conversation is exactly what
  vanished — `TerminalCard._rerender_restored` re-renders ONCE on the first
  `sizeChanged` (consuming `_pending_replay` first so a retile storm cannot
  repeat it). It bails when a live child owns the screen, and the test for
  that is the agent's own BUFFER (`pty_replay()` still byte-identical to what
  was fed), NEVER `is_running()`: the launch autostart starts agents
  SYNCHRONOUSLY right after `show()` while `TerminalView` debounces its
  resize by 120 ms, so an `is_running()` gate skipped precisely the cards it
  was meant to serve, and every restored-and-resumed card came back showing a
  mangled ~24-column fragment in the top-left corner of a full-width terminal
  until its child finished launching (pyte does not reflow on resize, so
  nothing else ever repaired it). The buffer test covers `restart()` too,
  which empties the buffer. (2) `seed_pty_replay` refuses to overwrite a buffer
  that already has output, and `restart()` clears the buffer while `start()`
  does not: waking a stopped card resumes its conversation, so the replayed
  screen scrolling up is right, whereas a deliberate restart is a fresh
  session and must drop it. (That `start()` rule is about the WAKE; the
  launch-time drop above is keyed on the card, not on `start()`.) The wake banner still exists but takes TWO shapes
  (`TerminalCard._refresh_overlay`): a slim bottom strip when there IS a
  screen to read, the original centred box only when the terminal is genuinely
  empty. The invariant it serves is unchanged (a stopped terminal must never
  read as a dead black screen); covering the restored conversation with a box
  was defeating the very thing it exists for. `_refresh_overlay` decides the
  shape on a STATUS change, never in `_place_overlay`, which runs per pixel
  during a drag or retile.
- **A LAUNCHING terminal shows a loader, never the child's first frames**
  (`ornaments.BootVeil`, `TerminalCard._begin_boot_veil`). The clean terminal
  above is the right thing to hand a launching child, but it is not what the
  user SEES: the child paints within milliseconds, at the PRE-LAYOUT width
  (the tiling grid sizes the card after the agent is started, and
  `TerminalView` debounces its resize by 120 ms), so every reopen still showed
  a mangled narrow fragment in each terminal's top-left corner until the
  conversation finished replaying. The veil covers exactly the launch-to-
  prompt window: `_on_status` raises it whenever the agent is running and
  `prompt_ready()` is False, and it comes down on the FIRST of four things,
  all of which must keep working — the new `TerminalAgent.prompt_ready_changed`
  edge (a fade, `finish()`), a keystroke, the agent stopping (the wake banner
  owns that state), or `BOOT_VEIL_MAX_MS`. The last two are not optional: a
  cover with only a positive release is a way to lose a terminal for good, and
  readiness is a SCRAPE of the child's output (Claude's footer-hint family,
  `?2004h` elsewhere) that an exotic shell may never produce. `prompt_ready_
  changed` is TRANSIENT like `activity_changed` — a view signal only, edge-only
  (`_set_prompt_ready`), never wired to a save. `is_active()` reads the veil's
  OWN `_up` flag, not `isVisible()`: a card in a hidden workspace is not on
  screen yet its child is still booting. Colours are read from the live
  `Palette` at paint time (so it follows every skin, no QSS token), the widget
  is `WA_TransparentForMouseEvents` + `NoFocus` so the terminal underneath
  keeps every event, and both animations stop on `dismiss()` so a resting card
  is free.
- **Transcripts are backed up by AI Hive** (`app/transcripts.py`): snapshots
  land in `<session-dir>/transcripts/` at app start (in `create_main_window`,
  BEFORE agents launch) and at graceful close (`closeEvent`). The
  `<id>.max.jsonl` high-water copy must never be replaced by a smaller file —
  that rule is the defense against transcript truncation (a real incident).
  If you add a new launch path, call `transcripts.backup_for_agents` before
  any Claude agent starts.
- **The model/effort on the card is a LIVE READING, never the launch flags**
  (`transcripts.latest_model_effort` → `WorkspaceManager.refresh_model_effort`
  → `TerminalAgent.set_live_model` → the header's `#CardModel` chip, polled on
  `MainWindow._model_sync_timer`/`MODEL_SYNC_MS`). `spec.model`/`spec.effort`
  are what the agent LAUNCHED with: they are persisted, they rebuild the
  command line through `providers.build_invocation`, and they go stale the
  moment the user types `/model` or `/effort` in the TUI, which is normal use.
  So they seed the display (`_seed_model`, falling back to the user's own
  `~/.claude/settings.json` model when the agent launched on "Default") and are
  NEVER written back to from a reading. The transcript carries BOTH signals and
  both are needed: every assistant record has `message.model` + a TOP-LEVEL
  `effort` (ground truth, but only as of the last turn), and a `/model`
  or `/effort` pick appends a `<local-command-stdout>Set model to …` user
  record the instant it happens — which is the only thing that shows an IDLE
  agent's switch without waiting for another turn. Merge them in FILE ORDER,
  last one wins; skip `isSidechain` (a sub-agent runs its own model) and the
  `<synthetic>` pseudo-model. Do NOT scrape the screen for this (`_screen_tail`
  is a 4000-char rolling buffer — the same trap the limit-banner invariant
  documents) and do NOT add a hook: a `/model` typed at an idle prompt fires
  none, and the hook plumbing is exactly what the SessionStart `startup`
  invariant says not to perturb. CRITICAL, same rule as `activity_changed` and
  the plan usage reading: this is TRANSIENT. It polls every 1.5s for the life
  of the process, so `set_live_model` must never mark `dirty`, and it emits
  `model_changed` only when the DISPLAYED badge string changes. An EMPTY
  reading is ignored rather than blanking a good label (a fresh conversation
  has no evidence yet). The reader is tail-only (`_MODEL_TAIL_BYTES`) with a
  full-scan fallback and an (mtime,size) cache, because unlike
  `refresh_ai_titles` it runs several times a second.
- **The PERMISSION MODE rides the same reading and is the ONE part of it that
  IS written back.** The chip reads `Opus 5 · high · plan`; the third token is
  the Shift+Tab mode, from the same transcript scan (`latest_model_effort`
  returns `(model, effort, permission_mode)`), shown via
  `TerminalAgent.permission_mode_label`. Two record shapes carry it, and both
  are needed for the same reason `/model` needs two: every user prompt has a
  top-level `permissionMode` (ground truth as of the last turn), and Claude
  appends a bare `{"type":"permission-mode","permissionMode":…}` record the
  instant Shift+Tab changes it, which is what shows an IDLE agent's switch.
  The write-back is the deliberate exception to the invariant above: the CLI
  does NOT carry a permission mode across a `--resume`, so an agent the user
  put in plan/auto came back ask-each-time on EVERY reopen. So
  `refresh_model_effort` calls `AgentSpec.set_permission_mode` and emits
  `dirty` — but ONLY when the mode genuinely changed, exactly like a pin change
  in `sync_live_sessions`, or a 1.5s poll would rewrite `session.json` forever.
  `set_permission_mode` REBUILDS `spec.args`: they are baked once by
  `build_spec`, so mutating the field alone persists the new mode while every
  launch for the rest of the process keeps the old flag. `closeEvent` runs one
  last `refresh_model_effort` before the final save (same reason it runs
  `sync_live_sessions` there): a Shift+Tab in the last second must still
  reopen in that mode. CRITICAL, the vocabularies differ and are NOT
  interchangeable: the transcript writes the CLI's INTERNAL names, and the
  ask-each-time mode is `"default"`, which `--permission-mode` does not accept
  at all (its choices are acceptEdits|auto|bypassPermissions|manual|dontAsk|
  plan, verified 2.1.220). `providers.normalize_permission_mode` translates
  before anything is stored ("default"/"manual" → `""`, i.e. omit the flag;
  an unknown token → `""` rather than a flag that would stop the agent
  launching), and `providers.permission_mode_display` turns it into the card's
  word. Validation in `build_invocation` is against the provider's
  `cli_permission_modes`, NOT the New Agent dropdown: the dropdown is a curated
  subset, and a mode adopted from a live conversation is routinely outside it
  ("auto" is what a current CLI records where an older build said
  "acceptEdits").
- **The card header is summary-first** (`widgets/terminal_card.py`): the
  one-line summary carries the layout stretch and is an `ornaments.ElidingLabel`
  — it re-fits in its OWN `resizeEvent` and reports a zero-width hint
  (`QSizePolicy.Ignored`), so it takes every pixel the fixed chrome leaves and
  can never be pushed around by its neighbours. Do NOT go back to eliding
  against a parent-supplied width with a character-count fallback: `_on_task`
  runs in the CONSTRUCTOR, before any real width exists, which left restored
  cards stranded on a 48-character truncation in a header with hundreds of free
  pixels. The same label serves the sidebar's agent rows. Start/Stop/Restart/
  Assign are NOT header buttons (they cost ~130px of every header for actions
  the terminal itself replaces); they live in the header's right-click menu
  (`show_actions_menu`), which is also where `reassignRequested` is emitted
  from — the signal and its `WorkspacePage`/`MainWindow` wiring are unchanged.
  The assignment badge is gone from the header too (the terminal says what the
  agent is doing); `AssignmentState` still drives the model and the board.
- **No em dash in text the user sees.** Labels, tooltips, dialog copy, terminal
  notices and the board markdown use other punctuation or a rephrase; a smoke
  check (`test_no_em_dashes_in_visible_text`) parses every module and fails on
  an em dash in any NON-docstring string literal. Comments and docstrings are
  prose for us, not for the reader, and are deliberately out of scope.
- **Agents launch with a SANITIZED env** (`pty_worker.agent_environment`):
  `CLAUDECODE`/`CLAUDE_CODE_*` markers are stripped — a claude that inherits
  them believes it is nested inside another claude session and SILENTLY
  disables transcript persistence (verified live; it also breaks
  `--session-id` pinning). This bites whenever AI Hive itself is launched
  from a Claude Code terminal — which is exactly how the test suite runs.
- **Claude task delivery submits with a delayed Enter** (350 ms,
  `_write_task_to_pty`): a CR in the same input burst as the text reads as
  part of a paste and inserts a newline instead of submitting — the task
  never runs. Prompt READINESS for Claude is the input-box footer
  ("? for shortcuts"), not bracketed-paste-enable: the folder-trust dialog
  also enables 2004h (and never sends 2004l on dismissal).
- **Sessions are PINNED per agent** (`AgentSpec.session_id`): fresh starts
  declare `--session-id <uuid>` (minted/rotated in `TerminalAgent.start`/
  `restart`), resumes use `--resume <id>` — NEVER `--continue` for a pinned
  agent. `--continue` means "most recent in this folder", which made two
  agents sharing a folder race for the same conversation on relaunch (one
  opened the wrong one, and concurrent resumes of one session destroyed a
  transcript). `--resume <id>` keeps the same id (no fork) unless
  `--fork-session` is passed.
- **The pin must TRACK the live conversation, not just the launch id**
  (`app/session_sync.py`). AI Hive only knows the id it put on the command
  line, but the live id can DRIFT out from under it: the user runs `/resume`
  in the TUI and switches conversations, a session forks (usage-limit
  recovery), or a fresh id is minted that never gets a transcript. A stale pin
  then resumes the WRONG conversation on reopen, or dies on `--resume
  <missing>` (both happened live — a closed morning thread came back instead
  of the afternoon one; a never-used id errored on a black terminal). Two
  defenses, both reading the filesystem truth (Claude writes exactly one
  `<id>.jsonl` per conversation under `~/.claude/projects/<encoded-cwd>/`):
  (1) `WorkspaceManager.sync_live_sessions` reconciles a RUNNING agent's pin to
  the transcript it is actually writing — run on a timer
  (`MainWindow._sync_live_sessions`, `SESSION_SYNC_MS`) AND once in
  `closeEvent` BEFORE the final save, so the last-moment switch is what
  persists; it emits `dirty` only when a pin genuinely changes (never thrash
  saves). (2) `TerminalAgent._recover_missing_resume_target` (gated by the
  one-shot `_verify_resume_target`, set only on RESTORE in `main.py`) verifies
  the pinned transcript exists before `--resume`; if not, it resumes the
  folder's most recent real conversation instead. Adoption requires the
  transcript to be newer than the agent's process-start (`_session_started`),
  so a pre-existing unrelated conversation or a fresh unused id is never
  wrongly grabbed, and a near-empty STUB (`_STUB_BYTES` — a fresh `/recap`
  session, a glitched resume) can NEVER displace a real conversation (that
  stranded a 9 MB chat on a blank card). CRITICAL: `resolve_live_ids` tracks
  ONLY single-agent folders. In a folder with two-plus agents the filesystem
  cannot say which agent owns which transcript (a resume touches them all at
  launch), so mtime correlation GUESSES — and a wrong guess SWAPS two live
  conversations or orphans one onto a stub. That misfired three times in one
  session (a good conversation pushed onto an empty `/recap` stub while its
  real chat sat un-pinned), so multi-agent folders are left EXACTLY as
  launched/restored — never auto-reshuffled. The only place a multi-agent
  folder is touched is `_recover_missing_resume_target`, and only when a pin
  points at a MISSING transcript; it excludes sibling pins
  (`sibling_session_ids`) so recovery never lands on a peer's conversation
  (the concurrent-resume truncation guard). Do NOT re-add mtime-based
  reassignment for multi-agent folders, however clever the tie-break — the
  filesystem simply lacks the signal.
- **The AUTHORITATIVE live-id signal is a `SessionStart` hook** — the child
  reports its own conversation id, which is the ONLY thing that makes
  multi-agent folders reliable (the filesystem can't attribute a transcript to
  an agent; the child can). `app/session_hook.py` is a Qt-free, stdlib-only
  script Claude runs on `SessionStart`; it appends `{agent_id, session_id,
  transcript_path, source, ts}` to a shared mapping file. AI Hive injects it
  into EVERY Claude agent via `--settings <shared file>` (verified live: a
  `hooks` section in a `--settings` file is honored AND is ADDITIVE with the
  user's own hooks — theirs still fire, never clobbered) plus a per-agent
  `AIHIVE_AGENT_ID` env var (== `TerminalAgent.id`, the SAME key
  `sync_live_sessions` matches on). Both are armed in
  `MainWindow._arm_agent_mcp` (independent of the board bridge — every
  Claude agent gets the hook) and are TRANSIENT like `mcp_config_path`
  (`AgentSpec.settings_path` / `spec.env`, never persisted, re-armed on
  restore). The shared settings + mapping files are written once per run in
  `MainWindow.__init__` (map reset each run — agent ids are minted fresh, so
  cross-run lines can never match). `sync_live_sessions` now consults the hook
  map FIRST (authoritative, per-agent, works for ANY agent count) and only
  falls back to the single-agent mtime path for agents the hook hasn't
  reported. `pty_worker.PtyWorker.start` layers `spec.env` on top of the
  sanitized `agent_environment()` so `AIHIVE_AGENT_ID` reaches the child (the
  PTY path previously ignored `spec.env`). CRITICAL, do NOT undo: the matcher
  is `"resume|clear|compact"` — it deliberately EXCLUDES `startup`. Including
  `startup` perturbs the TUI's launch settle just enough that a freshly-spawned
  agent's FIRST task-submit Enter is dropped and the task silently never runs
  (verified live — cost hours to isolate). The startup id is redundant anyway
  (it always equals the id AI Hive just put on the command line), so excluding
  it loses nothing and keeps launch timing pristine while still capturing every
  IN-TUI switch. Keep the hook synchronous; `async:true` does NOT fix the
  startup perturbation (it isn't a blocking issue) and only adds read-timing
  slop. This is the primary defense; the two filesystem defenses above remain
  as fallbacks (single-agent tracking, missing-pin recovery).
- **Gemini rides the Antigravity CLI** (`agy`, verified 1.0.16; installed at
  `%LOCALAPPDATA%\agy\bin\agy.exe`, which providers.py falls back to when the
  app's PATH predates the install). `--model` takes the MULTIWORD display
  strings from `agy models` (e.g. "Gemini 3.1 Pro (High)") — they must stay
  one argv entry; `--continue` resumes and `--add-dir` is repeatable, like
  Claude; there is NO system-prompt or MCP-config flag, so Gemini agents get
  no board `log_activity` tool (they still see the board via `--add-dir`).

## Verifying changes

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

All checks must pass (the count in README.md's Verify section is the current
total). The suite ends with a REAL end-to-end lifecycle test: it launches a
live claude once (~2 min, network/auth required; auto-skips if the CLI is
missing), talks to it, closes the app, reopens, and asserts the same
conversation returns. Don't remove or blindly shorten its timeouts — it is
the regression test for the entire class of close/reopen data loss. Every bug fix gets a regression check in
`tests/smoke_test.py` (grep `def test_` for the sections). The suite is
headless (offscreen QPA), uses ONLY isolated tmp `SessionStore` paths — keep
it that way: a test touching the real `%APPDATA%` session would wipe the
user's workspaces. Avoid non-ASCII in check names (cp1252 console).

Don't run test Claude sessions inside the user's real project folders
(`~/Downloads/hive-test*`) — they pollute `--continue` resume ordering.
Use a scratch cwd.

## Conventions

- Comments explain constraints the code can't show; module docstrings carry
  the design rationale. Update them when behavior changes — they are the
  documentation of record alongside README.md.
- `app/orchestration.py`, `app/providers.py`, `app/mcp_server.py` stay
  Qt-free (mcp_server must not import PySide6 at all).
- Keep README.md's check count and feature list current when adding tests
  or features.

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/` in this repo.
See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, used verbatim (`needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`).
See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root.
See `docs/agents/domain.md`.
