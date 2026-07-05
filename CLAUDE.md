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
  font) must emit `dirty`; orchestrator mutations save immediately via the
  bridge's `on_mutation` hook. If you add a persisted field, wire its mutation
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
  audit trail.
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
- **Orchestrator tools are workspace-scoped AND role-scoped.** The mcp config
  binds `AIHIVE_WS` + `AIHIVE_ROLE`; the bridge enforces both in `_dispatch`.
  Workers get a config exposing only `log_activity` (`--allowedTools
  mcp__aihive__log_activity`) AND the bridge refuses `_ORCHESTRATOR_ONLY_OPS`
  for role=="worker" — never rely on the tool list alone; keep the bridge
  guard. Any new bridge op must resolve agents via `_resolve_scoped`, be added
  to `_MUTATING_OPS` if it writes SESSION state (board writes don't), and be
  added to `_ORCHESTRATOR_ONLY_OPS` if only orchestrators may call it. Agents
  are armed with their config in `add_terminal` (via `manager.arm_agent`,
  set by MainWindow) BEFORE they start, and re-armed on restore
  (`_rearm_agent_configs`) — `mcp_config_path` is never persisted.
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
- **Gemini rides the Antigravity CLI** (`agy`, verified 1.0.16; installed at
  `%LOCALAPPDATA%\agy\bin\agy.exe`, which providers.py falls back to when the
  app's PATH predates the install). `--model` takes the MULTIWORD display
  strings from `agy models` (e.g. "Gemini 3.1 Pro (High)") — they must stay
  one argv entry; `--continue` resumes and `--add-dir` is repeatable, like
  Claude; there is NO system-prompt or MCP-config flag, so Gemini agents
  can't be orchestrators.

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
