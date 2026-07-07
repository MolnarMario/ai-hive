# AI Hive — Multi-Agent Control Center

A desktop app for orchestrating many terminal agents side by side, styled as
"a medieval scribe's workshop for software engineers" — warm parchment-toned
chrome with illuminated gold initials over clean, dark, high-density terminals.
Workspaces live in a collapsible sidebar; each is tied to a project folder and
tiles its agent cards into a chosen grid. Every card runs a **real child
process** (PowerShell, cmd, a Python script, any custom command, or an
interactive **Claude Code** session) with live streaming output. Hidden
workspaces keep executing — switching never pauses anything.

## What's here (v2 highlights)

- **AI-agent creation** — pick a provider (Claude fully wired with **model** +
  **effort** dropdowns → real `--model`/`--effort` flags; **Gemini via the
  Antigravity CLI** (`agy`) with its full model list incl. Gemini 3.x, and
  `--continue` resume + shared-board `--add-dir`, verified against agy 1.0.16;
  OpenAI as an editable command template until its CLI is installed). Shells
  and Python scripts are also first-class agent types.
- **One-click grid layouts** — a visual selector (Auto, 1×1 … 4×3, including
  the 3×1 strip and 1×3 stack) applies instantly and preserves agent state;
  empty cells show clickable "＋ New agent" slots. Layout names read
  **width × height** like screen resolutions ("3×1" = three side by side),
  and each swatch's diagram is exactly the shape you get. Optimized for
  wide monitors.
- **Workspace status badge** — each sidebar row leads with a badge showing its
  agent count that doubles as a status light: it **pulses green** while an agent
  is actually **working** (streaming output — thinking, generating, running a
  command), not merely alive, so an interactive agent idling at its prompt reads
  as **amber** standby rather than a false green. It turns **red** on error and
  dims when empty (the working/running/error breakdown is in the row's hover
  tooltip). The folder/delete controls stay hidden until you hover the row, so
  the workspace name keeps the space.
- **First-class folders** — every workspace header shows its path with
  **Open folder** and **Change…** buttons; agents launch rooted there.
- **Font controls** — per-agent A−/A+ (and `Ctrl+±`) plus a global A−/A+ in the
  top bar; sizes persist.
- **Shared agent awareness** — agents in a workspace coordinate through a
  shared board (`.aihive/board.md`): Claude agents launch with `--add-dir` +
  an appended system prompt so they can read peers' status and post their own,
  and a live **Activity** panel shows the roster, shared log, and git changes.
  Scoped per workspace; workspaces stay isolated.
- **Per-workspace agent numbering** — each workspace counts Agent 1, 2, 3…
  independently.
- **Agent/File Map** — a **◆ Map** button in the workspace header opens a
  separate, resizable window that draws a bubble diagram of the workspace:
  each agent is a round node and every file it has touched is a square node,
  with **solid gold edges for files it edited** and **thin dashed edges for
  files it only read**. Files touched by more than one agent are drawn once in
  a shared band linked to each owner, and Claude **sub-agents** (spawned via the
  Task tool) appear as small satellites ringing their parent. Attribution comes
  from parsing each Claude agent's own conversation transcript, so it is exact
  per-agent — the first per-terminal file view AI Hive has had. It live-refreshes
  while open, and it's **interactive**: drag any node to rearrange (placement
  persists across refreshes), drag empty space to pan, Ctrl+wheel (or +/−/0) to
  zoom, **single-click an agent to jump to its terminal card**, and double-click
  a file to open it (right-click for **Open with…** / Reveal in folder / Copy
  path). A header toggle switches between the **Bubble** view and a **Tree**
  view — a VSCode-style file hierarchy (folders/subfolders) on the left with
  curved connectors from each agent to the files it touched. Agent bubbles are
  opaque and each Task sub-agent is labeled with its type. (Sub-agent file work
  and non-Claude agents can't be attributed — those nodes show without file
  edges; see below.)
- **Themes (Winamp-style skins)** — a dropdown in the top bar swaps the whole
  chrome palette live: **Scriptorium (Dark)** (the shipped warm-parchment/gold
  look), **Illuminated Manuscript** (light vellum, ultramarine running-heads,
  a full **painted foliate border with gilt corner medallions**, a gilt logo
  roundel, historiated gilt drop-cap initials, parchment terminals — with
  bundled Cinzel / EB Garamond / Spectral fonts), **Adeptus Mechanicus** (a
  Warhammer 40K cogitator — green-phosphor CRT on a brushed-gunmetal casing
  with corner bolts, an aquila crest, a radiant green cog logo, servo-initial
  drop-caps, amber accents, and Share Tech Mono terminals), and **Obsidian**
  (clean modern dark). Each skin picks its own terminal treatment and ANSI
  palette; the choice persists. Ornaments are painted with QPainter +
  QSvgRenderer (`app/widgets/ornaments.py`), keyed per theme, and each shows
  only for its skin. Adding a skin is one `Theme(...)` in `ui_theme.THEMES`
  (+ optional painters keyed on its `ornament`).

Each card runs in one of two modes:

- **Line console** — a lightweight piped shell for line-oriented commands,
  with ANSI-color rendering and a command input box.
- **Full terminal** — a real Windows pseudo-console (ConPTY): the child sees
  a true TTY, so interactive TUIs (Claude Code, PSReadLine, vim, spinners,
  in-place redraws) work and **Ctrl+C** is a real interrupt. Tick "Full
  terminal" when adding an agent; the "Claude Code (interactive)" type turns
  it on automatically.

Built with Python + PySide6 (Qt 6), pywinpty, and pyte. Windows-first.

## Run it

Double-click **`AI Hive.bat`** in this folder — it launches through the
project's `.venv` with `pythonw` (no console window). From a terminal:

```powershell
python -m venv .venv                                          # first time
.venv\Scripts\python.exe -m pip install -r requirements.txt   # first time
.venv\Scripts\python.exe main.py
```

Don't open `main.py` itself with the system Python (that's what a plain
double-click on the .py file does) — PySide6 lives in the venv, not in the
system interpreter. If that happens anyway, the app now explains it in an
error dialog instead of silently closing. Packaging to a distributable
.exe (PyInstaller etc.) is intentionally not covered in v1.

## Using it

- **Workspaces** — `+` in the sidebar (or `Ctrl+Shift+N`). Every workspace is
  tied to a project folder you pick at creation; terminals open there.
  Double-click a row to rename, `✕` deletes (confirmation appears only when
  terminals are running). `☰` / `Ctrl+Shift+B` collapses the sidebar.
- **Agents** — `+ Terminal` (or `Ctrl+Shift+T`), or click any empty grid slot.
  The dialog groups **AI agents** (Claude with model + effort dropdowns;
  Gemini/Antigravity with its model list; OpenAI as an editable command
  template), **shells** (PowerShell, cmd), and **scripts** (Python, custom);
  tick "Full terminal" for a ConPTY-backed interactive session (auto-on for
  AI agents). For a Claude agent, a **Conversation** dropdown lists the
  workspace folder's past conversations (newest first, with a preview) so you
  can **resume one** instead of starting fresh — it launches with `--resume
  <id>`. Conversations a running agent already holds are omitted (resuming one
  twice would race/truncate it).
- **Grid layouts** — the **Layout** button in the workspace header opens a
  visual picker (Auto, 1×1 … 4×3; names read width × height, and the diagram
  on each swatch is the exact shape applied). A fixed layout fills agents left→right /
  top→bottom and shows clickable "＋ New agent" slots in the rest; changing
  layout is instant and never restarts an agent. Auto tiles to the count
  (1 full, 2 side-by-side, 3–4 = 2×2, 5–6 = 3×2, …). Line and full-terminal
  cards share the same grid.
- **Folder & activity** — the workspace header shows the project path with
  **Open folder** / **Change…**; **Activity** opens a panel with the live
  agent roster, the shared coordination log, and git-changed files.
- **Font size** — per-agent `A−`/`A+` in each card header (or `Ctrl+±` while
  focused), and global `A−`/`A+` in the top bar; both persist.
- **App shortcuts use `Ctrl+Shift+…`** (T = new terminal, N = new workspace,
  B = toggle sidebar) so every plain `Ctrl`/`Alt` key, `Tab`, and `Shift+Tab`
  goes straight to the focused terminal — click a full-terminal card and
  `Shift+Tab` cycles Claude Code's modes, `Ctrl+C` interrupts (when nothing is
  selected; otherwise it copies), `Ctrl+R` reverse-searches, arrows/`Tab`
  complete, exactly as in a real terminal.
- **Card controls** — `▶` start, `■` graceful stop (stdin EOF), `⟳` restart
  (fresh session), `✕` close. Type into the bottom input line to send a
  command to that terminal; `↑`/`↓` recall history; `cls`/`clear` clears
  the console locally.
- **PowerShell quirks to know** — there is no prompt line by design, and
  bare `cd` prints nothing in PowerShell (use `pwd` to see where you are).
  Typing a TTY-only program (`claude`, `vim`, `htop`, …) prints a hint
  instead of leaving you with a cryptic error.
- **Sessions** — workspaces and terminals are saved automatically and
  restored on launch (including provider/model/effort, per-agent font, grid
  layout, current task, and assignment badges). Everything that was running
  comes back **in every workspace**: previously-running agents autostart, and
  each Claude agent resumes **its own pinned conversation** (`--resume
  <session-id>` — so two agents sharing a project folder can never race for
  or swap each other's conversations) — falling back to a fresh launch
  instead of a dead card if there's nothing to resume. The pin **tracks the
  live conversation**: if you switch conversations inside a terminal (`/resume`
  or `/clear` in the TUI, a fork), the agent *reports its own new conversation
  id back to AI Hive* through a `SessionStart` hook, so reopen brings back
  *exactly* the conversation that was on each card — reliably, even when two
  agents share one folder (which the filesystem alone can't disambiguate). A
  pinned id whose transcript has gone missing still recovers the folder's most
  recent one instead of erroring. No more manually hunting for a lost chat.
  Agents that were *stopped* stay stopped, but never as a black screen: the
  card shows a **wake banner** and the first keystroke (or `▶`) starts it —
  a woken Claude agent also reclaims its conversation.
- **Scrolling** — in a full terminal the wheel does what a real terminal does:
  fullscreen apps that request mouse events (Claude Code) receive the wheel
  and scroll their own transcript; other fullscreen apps (vim, less) get
  arrow keys; plain shell output scrolls a 2000-line history view (a `▲ n`
  badge shows how far back you are; any keystroke snaps back live).
  `Shift+PgUp/PgDn` page; `Ctrl+wheel` zooms the font.

## Orchestration (v3)

Turn a workspace into a coordinated team, not just parallel terminals:

- **Shared activity log** — every agent (orchestrator AND workers) can post a
  one-line note to the workspace board via a `log_activity` MCP tool; AI Hive
  serializes those writes through the GUI so concurrent agents can't clobber
  each other's entries. Workers are scoped to `log_activity` only (an
  `AIHIVE_ROLE` guardrail refuses them spawn/retask/close), so peer awareness
  is safe to hand every agent.
- **Orchestrator agent** — when adding a Claude agent, tick **"Orchestrator"**.
  It launches with the AI Hive MCP tools (`--mcp-config` + `--strict-mcp-config`
  + pre-approved `mcp__aihive` tools, so no permission prompts) and can:
  `spawn_agent` (create a worker with a role + task — it appears in the grid),
  `assign_task` / `reassign_agent` (task a new or existing idle/completed
  worker — typed into its live session, preserving context), `list_agents`,
  `get_agent_output`, `set_agent_state`, and a soft `close_agent`. The channel
  is a Windows named pipe (QLocalServer, zero new deps); the MCP server
  (`app/mcp_server.py`) is stdlib-only. If it can't start, the app runs exactly
  as before.
- **Workspace-scoped tools** — every orchestrator is *bound to the workspace it
  was created in*: its mcp config carries the workspace id (`AIHIVE_WS`), which
  is echoed with every tool call and enforced GUI-side. It cannot list, retask,
  or close agents in any other workspace, and name lookups (`"Agent 1"`) can
  never match a same-named agent elsewhere. Every orchestrator mutation also
  saves the session immediately.
- **Dynamic role names** — agents are auto-named for their task (Backend
  Architect, Database Engineer, Testing Agent, …) and rename as tasks change.
- **Intelligent model/effort** — each task auto-selects a model + effort
  (trivial → Haiku/low, mid → Sonnet/medium, architecture → Opus/high); never
  auto-uses top-tier/max. Explicit picks always override. **Ultracode** appears
  in the effort dropdown but greyed out — it's an in-session mode you enable
  with `/effort ultracode` in a supporting model's terminal.
- **Persistent agents (#9)** — auto-created agents **never** auto-close. A
  completed worker stays in the grid with a **Completed / Working / Awaiting
  Assignment** badge and a **⇄ Reassign** button, so you can review its work,
  continue the conversation, or retask it. Only you close an agent.
- **Reassign anywhere** — the ⇄ button on any card assigns a fresh task
  (role/model adapt) without losing the session.

## Shared agent awareness

Agents in the same workspace coordinate through a shared board at
`<project>/.aihive/board.md`. AI Hive maintains an app-owned roster block
(each agent's name, model, status, and current task); agents append to an
`## Activity log` section below it. Claude agents launch with `--add-dir
<.aihive>` and an appended system prompt instructing them to read the board
for peer awareness and post their own updates — so they can see what others
are doing and avoid duplicate work. The **Activity** panel shows the roster,
the log tail, and a best-effort `git status` view. Awareness is **scoped to
the workspace** (the board lives in its folder); different workspaces are
isolated. File-modification attribution is self-reported by agents in the log
plus the repo-wide git view — the app does not attribute individual OS file
writes to a specific terminal. For a precise per-agent view, the **Agent/File
Map** (the ◆ Map button) parses each Claude agent's own transcript to show
exactly which files that agent read and edited; this is transcript-derived, not
OS-level, so it covers Claude agents (not Gemini/OpenAI/shells) and cannot see
inside a Claude Task sub-agent (those files roll up to the parent).

## Reliability & persistence guarantees

Hard-won rules, each with a regression test:

- **Nothing structural is ever only-in-memory.** Adding or removing an agent
  or workspace saves the session *immediately* — a crash or force-kill cannot
  lose a just-created agent. Metadata changes (task, assignment, role, run
  state, fonts) mark the session dirty and save on a short debounce; every
  orchestrator mutation saves immediately. A periodic **safety-net autosave**
  re-writes only when the live state has diverged from disk, so even a missed
  signal or a suppressed save can strand work for at most a few seconds.
- **A save is never silent.** Suppressed saves (e.g. during close) and errors
  raised while *building* the payload — both upstream of the atomic write's own
  logging — append their own forensic line, and one un-serializable agent can
  never abort the whole session's save (it degrades to a minimal entry that
  still restores). Silent no-saves are how agents once vanished without a
  trace; every path now leaves one.
- **One instance, enforced by the kernel.** A named mutex (not a probe with a
  timeout) guards startup and *fails closed* — a second launch refuses to run
  rather than risk last-writer-wins clobbering of `session.json`.
- **Resume, never a black card.** A restored Claude agent relaunches with
  `--resume <session-id>` (its own pinned conversation — legacy unpinned agents
  fall back to `--continue`); if the CLI reports nothing to resume (it exits
  immediately in that case), the agent auto-relaunches fresh — same folder, MCP
  tools and board — instead of showing a dead terminal.
- **A session file is precious.** Loads tolerate a UTF-8 BOM; a failed load
  preserves the file as `.json.bak` (never deletes); saves are atomic
  (tmp + rename). Every save and load-failure appends a forensic line to
  `session.log` (timestamp, pid, per-workspace terminal counts) so any future
  loss is attributable.
- **Conversations are backed up.** Claude Code keeps each conversation as a
  single file it owns (`~/.claude/projects/...`) — AI Hive snapshots every
  pinned agent's transcript into `%APPDATA%\AIHive\AI Hive\transcripts\` on
  every app **start** (before any agent launches, so no resume mishap can
  destroy the only copy) and on every graceful **close**. Per conversation:
  the newest snapshot, one rotation (`.jsonl.1`), and a **high-water copy**
  (`<id>.max.jsonl`) that is never replaced by anything smaller — a truncated
  transcript can never displace the full one.
- **The whole close→reopen journey is tested against a real Claude.** The
  suite's lifecycle test talks to a live agent, closes the app gracefully,
  reopens, and asserts the SAME conversation returns on the same card (and
  that the backup exists) — the exact path where data was historically lost.

## Engine notes (what's under the hood)

- One `QProcess` per terminal — output arrives as event-loop signals, batched
  to ~30 fps; the GUI never blocks, no threads needed.
- Graceful stop is stdin EOF (`cmd` and PowerShell 5.1 both exit 0 on it —
  verified). Hard kill is a Windows **Job Object** tree kill, so shells AND
  their grandchildren (`ping -t`, spawned scripts) die with the card, and
  `KILL_ON_JOB_CLOSE` reaps everything even if the app crashes. No zombies.
- Output decoding is UTF-8-first with cp437 fallback per chunk (cmd/PS 5.1
  builtins emit OEM cp437 on pipes; modern tools emit UTF-8 — sometimes in
  the same session). PowerShell sessions are switched to UTF-8 end-to-end at
  startup. `chcp 65001` is deliberately NOT used: it corrupts non-ASCII stdin.
- ANSI SGR (16/256/truecolor + bold/italic/underline/reverse) renders in the
  console; all other escape sequences are stripped. A lone `\r` overwrites
  the current line, so pip/npm-style progress bars update in place.

## Modes & scope

- **Full-terminal (ConPTY) cards** render a real terminal via a pyte screen:
  interactive Claude Code, vim, PSReadLine, spinners, and Ctrl+C all work.
  Keystrokes go straight to the child; the input box is replaced by the live
  screen. **Clipboard** (Windows-editor style): `Ctrl+C` copies the selection
  or, with nothing selected, sends the interrupt (0x03) — copying clears the
  selection so the next `Ctrl+C` interrupts; `Ctrl+Shift+A` selects all painted
  text (screen + scrollback); `Ctrl+V` / `Ctrl+Shift+V` paste (bracketed-paste
  aware for multi-line); `Ctrl+Shift+C` also copies; and a right-click
  Copy/Paste/Select-all menu. **Mouse**: double-click selects the
  whitespace-delimited word under the pointer (then `Ctrl+C` copies it);
  **Ctrl+click** (or a middle/scroll-wheel click) opens a URL or an existing
  absolute local file path under the pointer with the OS default handler —
  hovering such a link underlines it and shows a hand cursor so it's obviously
  clickable. (`Ctrl`+left-click is primary — the left button always registers,
  while the middle button is often eaten by the OS autoscroll.) **Image
  paste**: a `Ctrl+V` with an image on the
  clipboard is spilled to a temp PNG and its path pasted, because Claude Code
  reads images by path and a native-Windows child can't take a raw clipboard
  image (that's WSL-only, via Claude's `Alt+V`). `Ctrl+A` highlights the text
  you're typing (best-effort — from Claude's `>` prompt row down to the cursor,
  so a wrapped/multi-line prompt highlights in full, like Cursor or the Claude
  desktop input box, without climbing into the transcript above) and
  `Backspace`/`Del` on that highlight clears the child's entire input via
  double-Escape (`0x1b 0x1b`, Claude Code's clear-prompt gesture, which —
  unlike its line-local `Ctrl+A`/`Ctrl+K` — empties multi-line input too). The
  terminal can't see the child's real input buffer, so it's inference from
  painted rows: no `>` found means it falls back to the cursor row (`Home`
  still jumps to line start). `Ctrl+Z`/`Ctrl+Y` are not
  undo/redo — a terminal keeps no local edit buffer, so they forward to the
  child, which owns line editing. Keyboard-protocol escapes that pyte mis-parses
  (e.g. xterm modifyOtherKeys) are filtered so text renders clean, not
  underlined.
- **Line-console cards** stay line-oriented: full-screen TUIs won't render in
  them, and there's no per-command Ctrl+C (use `■` Stop / `⟳` Restart, or
  switch that agent to full-terminal mode). Bare `cd` prints nothing in
  PowerShell — that's PowerShell, not the app.
- Windows-first: developed and tested on Windows 11. On non-Windows,
  full-terminal mode is unavailable (pywinpty is Windows-only) and Job-Object
  tree-kill is skipped; line mode still works but is untested there.

## Verify

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

441 checks drive the real app headlessly (offscreen Qt platform) with real
child processes: tiling math + applied grid geometry, live streaming, stdin
round-trip, workspace-cwd inheritance, background retention while hidden,
card close terminating the process, zero-orphan shutdown, save/restore round
trips, the ConPTY path (interactive prompt, Ctrl+C, retention), every v2
feature (provider flags, per-workspace numbering, the agent-count badge,
explicit grids, folder changes, fonts, the shared board), v3 orchestration
(role naming, model/effort selection, the named-pipe MCP round-trip,
workspace scoping, immediate-save-on-mutation), the sidebar status badge
(output-activity busy detection, pulse/colour state machine, hover-only
controls), the Agent/File Map visualizer (transcript parsing for edited-vs-read
attribution, sub-agent detection, shared-file grouping, headless paint, header-
button wiring, and the drag/zoom/hit-test/click-to-focus interactions) and the
Layout popup
staying on-screen when the window is at a monitor edge, and the reliability
set — immediate structural saves, the safety-net heartbeat, saves that are
never silent (suppressed/payload-error logging, one bad agent can't abort the
save), resume + fresh-fallback, BOM tolerance, the single-instance mutex (fail
closed), the save-audit log, and scrollback in all three wheel regimes.
Screenshots land in `tests/screenshots/`.

## Layout

```
main.py                    entry point + create_main_window() factory
app/
  tiling.py                pure grid math: compute_grid + explicit_grid
  ansi_parser.py           stateful SGR parser (line-console rendering)
  providers.py             AI provider registry (Claude + Gemini/agy wired; OpenAI template)
  coordination.py          per-workspace shared board (.aihive/board.md)
  orchestration.py         task → role + task → model/effort heuristics (Qt-free)
  process_worker.py        QProcess engine, HybridDecoder, WinJob (line mode)
  pty_worker.py            ConPTY engine via pywinpty (full-terminal mode)
  terminal_agent.py        per-terminal model (worker + log/buffer + lifecycle)
  workspace_manager.py     model layer: workspaces, agents, spawn/assign/reassign
  orchestrator_bridge.py   named-pipe RPC server (GUI side), workspace-scoped
  mcp_server.py            stdlib MCP stdio server the Claude CLI spawns
  session_store.py         atomic JSON persistence (AppData) + save-audit log
  transcripts.py           Claude-transcript snapshots (start/close, high-water)
  session_sync.py          reconcile a pinned id with the transcript on disk (fallback)
  session_hook.py          SessionStart hook: the child reports its live conversation id
  file_activity.py         per-agent file attribution from transcripts (Qt-free)
  ui_theme.py              theme registry (skins) + apply_theme + the QSS stylesheet
  assets/fonts/            bundled OFL manuscript fonts (Cinzel/EB Garamond/Spectral)
  widgets/                 main_window, sidebar, workspace_page, terminal_card,
                           terminal_view (pyte grid), grid_selector (on-screen
                           popup), activity_panel, agent_file_map (bubble
                           diagram), ornaments (drop-caps / dividers / the
                           workspace count-badge)
tests/smoke_test.py        headless end-to-end suite (441 checks)
```

Model/view rule: widgets subscribe to model signals and never own processes —
that's what guarantees hidden workspaces keep running.
