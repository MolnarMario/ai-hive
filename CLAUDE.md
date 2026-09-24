# AI Hive rules for working on this codebase

Windows-first PySide6 desktop app. `README.md` covers features and layout.
Design rationale lives in module docstrings, next to the code it explains.
This file holds only the rules that protect user data or fail silently.

## Invariants

- Agents belong to the model. `TerminalAgent` objects are parented to
  `WorkspaceManager`, never to a widget. Cards `detach()` before deletion and
  teardown goes through the manager. That is what keeps hidden workspaces
  running.
- Never call `QProcess.terminate()`. Graceful stop is stdin EOF, hard kill is
  the Windows Job Object (`KILL_ON_JOB_CLOSE`) so grandchildren die too.
- A new persisted field needs three things: its mutation path emits `dirty` or
  saves, `load_session_dict` restores it, and `SESSION_VERSION` gets a bump and
  a migration if the shape changes. Never remove the `SAVE-SKIP`, `SAVE-FAIL`
  and `SAVE-DEGRADE` audit lines or `_agent_dict_safe`. A silent save failure
  is how agents vanished before.
- The session file is never deleted. Failed loads rename to `.json.bak`, saves
  are atomic and keep `session.json.1`. Never edit `session.json` with
  PowerShell `Set-Content` (it writes a BOM). Use Python, and copy the live
  file aside and re-read it right before writing. A hand edit from stale data
  once destroyed a user's new agents.
- Single instance is a kernel mutex (`main._single_instance_guard`) that fails
  closed. Don't replace it with anything that has a probe timeout.
- `main.py` sets `quit_on_close = True`. A process that outlives its window
  holds the mutex and can never save again. Tests must NOT set it.
- `log_activity` is the only agent-to-GUI operation, scoped by `AIHIVE_WS`.
  There is no orchestrator and agents cannot spawn or retask each other. A
  new bridge op must be scoped by workspace too.
- Agents launch with `CLAUDECODE` and `CLAUDE_CODE_*` stripped
  (`pty_worker.agent_environment`). A claude that inherits them thinks it is
  nested and stops writing transcripts. This bites whenever AI Hive runs from
  a Claude Code terminal, which is how the suite runs.
- The spawn clears the inherited ignore-Ctrl+C flag
  (`process_worker.enable_ctrl_c_for_children`), or Ctrl+C in a card reaches
  nobody. The self-protect console handler it installs is required, or a
  Ctrl+C kills AI Hive itself without a final save.
- Pinned agents resume with `--resume <id>`, never `--continue`. `--continue`
  picks the newest conversation in the folder, so two agents in one folder
  raced for the same one and a transcript got destroyed.
- Claude task delivery sends Enter 350 ms after the text
  (`_write_task_to_pty`). A CR in the same burst counts as part of the paste
  and the task never runs.
- Nothing renames an agent except the user.
- No em dash in any string the user sees. `test_no_em_dashes_in_visible_text`
  enforces it for non-docstring literals.

## Verifying changes

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

Every bug fix gets a regression check in `tests/smoke_test.py`. The suite is
headless and uses only temp `SessionStore` paths. A test that touches the real
`%APPDATA%` session wipes the user's workspaces. Keep check names ASCII
(cp1252 console). The final e2e test launches a real claude for about two
minutes. Don't shorten its timeouts, it guards close/reopen data loss.

Never run test Claude sessions inside the user's real project folders. They
pollute resume ordering. Use a scratch cwd.

## Conventions

- Every PR merged to `main` bumps `__version__` in `app/__init__.py`. Nothing
  enforces it, so it is part of done.
- Keep README.md's check count and feature list current.
- `app/orchestration.py`, `app/providers.py` and `app/mcp_server.py` stay
  Qt-free. `mcp_server` must not import PySide6 at all.
- Comments explain constraints the code can't show. Update module docstrings
  when behavior changes.

## Agent docs

Issues live as markdown under `.scratch/<feature-slug>/`, see
`docs/agents/issue-tracker.md`. Triage labels are in
`docs/agents/triage-labels.md`, domain docs in `docs/agents/domain.md`.
