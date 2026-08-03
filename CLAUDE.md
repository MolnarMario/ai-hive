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
  `_agent_dict_safe` so one un-serializable agent degrades to a minimal
  (identity + session_id + running) entry instead of aborting the entire
  session's save. Don't remove these guards or let a new save path bypass the
  audit trail. Conversely, TRANSIENT signals must NEVER mark `dirty`:
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
- **pyte quirks are handled in `terminal_view.feed()`** — private-marker CSI
  stripping, partial-escape carry, DECSET mode tracking. Scrollback is a view
  offset, never pyte paging (`prev_page` snaps back on any event). The wheel
  has three regimes (mouse-tracking forward / altscreen arrows / history
  offset) — keep all three working.
- **Claude CLI facts** (verified 2.1.197): interactive `--continue`/`--resume
  <id>` hard-exit when there is no matching conversation (handled by the
  fresh-fallback in `TerminalAgent._on_finished`); headless `-p` silently
  starts fresh instead — never use `-p` to test resume. Effort tokens:
  low|medium|high|xhigh|max. Claude Code enters the alternate screen and
  enables mouse tracking (?1049h, ?1000/1002/1003h, ?1006h, ?1004h, ?2004h).
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
  `parse_utilization` serves both) but is only a cold-start seed / offline
  fallback — the CLI rewrites it opportunistically and it goes stale for days
  (observed 1.5 days and 10 points out of date), so it must never be the primary
  source. TOKEN HANDLING IS READ-ONLY: re-read `.credentials.json` per call
  (running agents keep it rotated for us), short-circuit on a past `expiresAt`
  instead of putting a dead credential on the wire, and NEVER refresh (that
  races the CLI's own refresh), write, log, or persist it. `fetch()` never
  raises — every failure becomes a `Usage` with `error` set, and a failed poll
  KEEPS the last good number on screen (greyed) rather than blanking a figure
  the user is reading; only `no-auth` with no prior reading hides the badge for
  good. CRITICAL, same rule as `activity_changed`/`waiting_changed`: a reading
  is TRANSIENT and must NEVER mark `dirty` — `_apply_usage` runs every minute
  for the life of the process, so wiring it to a save would rewrite
  `session.json` 60x an hour (only the `ui.usage_visible` preference saves, via
  `_schedule_save`). Polling is OPT-IN — `main.py` calls
  `MainWindow.start_usage_polling()` exactly like it sets `quit_on_close`,
  because the smoke suite shares `create_main_window` and must never touch the
  network or the user's real account; tests drive `_on_usage_ready` with
  synthetic readings. The BLOCKED state (`Usage.blocked`, utilization >= 100 —
  derived from the number, NOT from the payload's server-side `severity`
  string) is the machine-readable half: `MainWindow.planLimitReached(Limit)` /
  `planLimitCleared()` are edge-triggered and level-correct like the chime, and
  `plan_usage()` exposes the latest reading, so features that ACT on being cut
  off (e.g. relaunching blocked agents unattended when the limit resets) hook
  those instead of scraping a terminal. While blocked, `_arm_reset_poll`
  schedules one extra poll just after the stated reset so the cleared edge
  fires within seconds at 4am rather than waiting out the minute timer.
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
  they don't all pile into the freshly reopened window. CRITICAL: the text goes
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
  Related: an agent parked on the limit raises the "?" (its menu is exactly
  what `_screen_waiting` looks for) but must NOT ring the chime — it is not a
  question the user can answer, and it would wake them at 4am for something
  auto-continue is about to handle.
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
  out rather than poked. It skips agents that aren't running (a card left
  stopped stays stopped; starting it would spend quota the user didn't ask
  for) and cut-offs older than `STARTUP_RECOVERY_MAX_AGE_S`. Each latch records
  its ORIGIN (`limit_from_startup`) and is gated by the toggle that owns it —
  `ui.startup_recovery` for disk-recovered, `ui.auto_continue` for live — so
  switching one off can never strand a latch the other created. Both are
  ordinary UI preferences that save via `_schedule_save` (like `usage_visible`);
  the latch itself is NEVER persisted — the transcript is the durable record,
  and a persisted flag would go stale. `recover_blocked_at_startup` is OPT-IN
  from `main.py` (after `autostart_active_workspace`, since it only considers
  RUNNING agents) exactly like `start_usage_polling`: it reads the user's real
  transcripts and types into real agents, which the smoke suite must never do.
  The whole path is audited to `session.log` via `_limit_audit`
  (`STARTUP-SCAN`/`STARTUP-SKIP`/`BLOCKED`/`NUDGE`/`WAIT`/`RESUMED`/
  `STILL-BLOCKED`) — this feature failed silently TWICE and both causes had to
  be reconstructed from transcript timestamps hours later; do not remove it.
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
- **Transcripts are backed up by AI Hive** (`app/transcripts.py`): snapshots
  land in `<session-dir>/transcripts/` at app start (in `create_main_window`,
  BEFORE agents launch) and at graceful close (`closeEvent`). The
  `<id>.max.jsonl` high-water copy must never be replaced by a smaller file —
  that rule is the defense against transcript truncation (a real incident).
  If you add a new launch path, call `transcripts.backup_for_agents` before
  any Claude agent starts.
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
