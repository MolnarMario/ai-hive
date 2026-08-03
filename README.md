# AI Hive — Multi-Agent Control Center

A desktop app for running many terminal agents side by side, styled as
"a medieval scribe's workshop for software engineers" — warm parchment-toned
chrome with illuminated gold initials over clean, dark, high-density terminals.
Workspaces live in a collapsible sidebar; each is tied to a project folder and
tiles its agent cards into a chosen grid. Every card runs a **real child
process** (PowerShell, cmd, a Python script, any custom command, or an
interactive **Claude Code** session) with live streaming output. Hidden
workspaces keep executing — switching never pauses anything.

## What's here (v2 highlights)

- **AI-agent creation** — pick a provider (Claude fully wired with **model** +
  **effort** + **mode** dropdowns → real `--model`/`--effort`/`--permission-mode`
  flags; the **mode** picker is the Shift+Tab permission modes — Normal (default),
  Accept edits, Plan, Bypass permissions — so an agent can start pre-set; **Gemini via the
  Antigravity CLI** (`agy`) with its full model list incl. Gemini 3.x, and
  `--continue` resume + shared-board `--add-dir`, verified against agy 1.0.16;
  **Grok via the xAI CLI** (`grok`) with `-m` model selection + `--continue`
  resume, verified against grok 0.2.93; OpenAI as an editable command template
  until its CLI is installed). Shells
  and Python scripts are also first-class agent types.
- **One-click grid layouts** — a visual selector (Auto, 1×1 … 4×3, including
  the 3×1 strip and 1×3 stack) applies instantly and preserves agent state;
  empty cells show clickable "＋ New agent" slots. Layout names read
  **width × height** like screen resolutions ("3×1" = three side by side),
  and each swatch's diagram is exactly the shape you get. Optimized for
  wide monitors.
- **Drag to reorder agents** — grab a card by an empty part of its **header
  bar** (where the name / model / summary / usage sit — not the buttons) and
  drag it; a **gold insertion bar** shows exactly where it will land, and on
  drop the cards **snap into place** with a short animation. The new order
  persists with the workspace.
- **Workspace status badge** — each sidebar row leads with a badge showing its
  agent count that doubles as a status light: it stays **green** while agents
  are alive and healthy (running, no errors) and **pulses amber** while an agent
  is actually **working** (streaming output — thinking, generating, running a
  command), so working reads as motion rather than resting green. It turns
  **red** on error and dims when empty (the working/running/error breakdown is
  in the row's hover tooltip). A matching **sweeping-arc spinner** with the live
  working count appears at the row's right edge while any agent is working and
  vanishes when all are idle. The folder/delete controls stay hidden until you
  hover the row (opening to the left of the spinner), so the workspace name
  keeps the space.
- **Organize the sidebar** — drag workspaces up/down to reorder them, and group
  related ones under **categories** ("Work dev", "Game dev", …) with the 🗂
  button. Categories are collapsible headers you can drag above/below other
  categories or loose workspaces; drop a workspace onto a header to file it in,
  or back out to un-file it. A category and its members are wrapped in a tinted
  container that grows and shrinks with what's expanded (the category's
  workspaces, and a workspace's agents), so membership is unambiguous at a
  glance. The whole layout (order + categories + collapse state) persists across
  restarts.
- **Inline agent list + "?" waiting alerts** — click a row's count badge to
  **expand its agents inline**, folder-tree style — each agent's name on the
  left with a one-line **summary** beside it. The summary is the agent's
  assigned task, or, when none is set, **Claude Code's own AI conversation
  title** read live from the transcript (the same short summary you see in
  `/resume`) — so you can tell at a glance what each agent is working on without
  reading its terminal. The same summary shows in the terminal card header.
  Click any agent to jump straight
  to its terminal card (switching workspace first if needed). When an agent is
  actually **waiting for you** — a permission prompt or an interactive
  question — a "?" lights up on the row (next to the working count) and beside
  that agent in the expanded list, so you can spot and answer it without hunting
  through terminals. Suppressed for agents launched in bypass-permissions mode
  (which never prompt). A soft **notification chime** rings the moment that "?"
  appears (the standby→waiting rising edge), so you notice an agent needs you
  even while you're heads-down in another workspace — handy when agents you sent
  into plan mode come back with questions. Toggle it with the **🔔 button** in
  the top bar (persists across restarts).
- **Plan usage readout** — a top-bar badge, left of the theme picker, showing
  how much of your Claude plan you've burned and when it comes back:
  `21% used, resets in 1h20m at 14:49` — countdown first, then the wall-clock
  time in your own timezone. A percent ring turns amber past 60% and red past
  85%, so you see a wall coming instead of hitting it mid-task. **Click it to
  refresh**; hover for every limit window, your plan, and how old the reading
  is. The number comes from the same place the CLI's `/usage` gets it, read
  once a minute in the background — nothing is logged or persisted, it's a live
  readout only. When a limit is actually spent it reads **`limit reached,
  resets in …`**, and the window raises `planLimitReached` / `planLimitCleared`
  signals (with the reset time) so other features can act on being cut off —
  e.g. relaunching blocked agents unattended the moment the limit resets.
  Right-click the top bar to hide the readout; it hides itself when there's no
  Claude login.
- **Auto-recovery from a spent plan limit** — two switches sit next to the usage
  readout, both on by default, both persisted, each with a tooltip spelling out
  what it does:
  - **⏯ Recover at startup** — when AI Hive opens, it looks across every
    workspace for agents whose work stopped because the limit ran out, and
    continues them. It reads each agent's conversation on disk rather than the
    screen, so it works after a full reboot, not just an app restart. Strictly
    gated: an agent whose conversation doesn't *end* on the limit message is
    left alone (said anything since, and it plainly carried on), a card you left
    stopped stays stopped, and a cut-off older than 36 hours isn't revived — so
    work you left mid-afternoon is still picked up when you get back to the
    machine late the following day.
  - **⏰ Resume on limit reset** — while the hive is running, agents cut off
    mid-work go back to work the moment the window reopens.

  Either way AI Hive closes the limit's options menu and types `Continue`,
  staggered so agents don't all pile into a fresh window, then **verifies** —
  the menu disappearing is how it knows the resume took, and it retries if not.
  Because the first message after a window expires is what STARTS the next
  5-hour window, resuming at 4am also means the clock has already rolled over by
  the time you sit down. The cut-off is recorded the instant it appears, along
  with the reset time the limit itself stated, so recovery doesn't depend on the
  usage API being reachable — it fires from the agent's own stated reset even
  when the account readout is rate-limited. Every step is logged to
  `session.log` (`BLOCKED` / `NUDGE` / `RESUMED` / `STILL-BLOCKED`), each resumed
  card shows a `— plan limit reset; auto-continued —` line, and the workspace
  board gets a note.
- **Inline file explorer** — hover a workspace row and click the **▸ files**
  toggle to expand a **VS Code-style file tree** right under it: folders and
  files of the project root, each with a **type icon**, lazily populated as you
  open folders and live-refreshed as files change on disk. Click a file to open
  it with your OS default program. The tree lives inside the same sidebar (and
  inside a category's tinted container when the workspace is grouped); which
  workspaces have it open persists across restarts.
- **Conversation ↔ files bridge** — URLs and file paths in an agent's output
  are **underlined** so you can spot them in the body text at a glance; hover
  turns the cursor into a hand and emphasizes the link, and **Ctrl+click** opens
  it with the OS default program — absolute paths *and* repo-relative ones like
  `app/widgets/sidebar.py` (resolved against the agent's folder). If that
  workspace's file tree is open, the clicked file is also scrolled to and
  **highlighted** in it, so you can see where it lives.
- **Search the sidebar** — a 🔍 next to the category button expands an input
  that covers the WORKSPACES title/count; as you type it **highlights every
  match** across workspace names, agent names, and agent summaries, and
  auto-expands a workspace to reveal a matching agent. Esc (or clicking 🔍
  again) closes it and restores what was expanded before.
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
  separate, resizable window that draws a **Tree** view of the workspace: a
  VSCode-style file hierarchy (folders/subfolders) on the left, the agents as
  round bubbles on the right **vertically centered** against the tree, and
  curved connectors from each agent to the file rows it touched — **solid gold
  for files it edited** and **thin dashed for files it only read**. A file
  touched by more than one agent is a single row with a connector to each owner,
  and Claude **sub-agents** (spawned via the Task tool) appear as small
  satellites ringing their parent. Attribution comes from parsing each Claude
  agent's own conversation transcript, so it is exact per-agent — the first
  per-terminal file view AI Hive has had. It live-refreshes while open, and it's
  **interactive**: drag an agent to rearrange it (placement persists across
  refreshes; the file tree is structural), drag empty space to pan, Ctrl+wheel
  (or +/−/0) to zoom, **single-click an agent to jump to its terminal card**,
  and **double-click a file to open it with the OS default program** (each file
  row is prefixed with a **type icon** — image / code / config / doc / … — keyed
  on its extension; right-click for **Open with…** / Reveal in folder / Copy
  path). Header **Write** / **Read** toggles hide the edited or
  read-only lines independently, for when you only care about one kind of
  activity (the legend greys the hidden kind). Agent bubbles are opaque and each
  Task sub-agent is labeled with its type.
  (Sub-agent file work and non-Claude agents can't be attributed — those nodes
  show without file edges; see below.)
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
  The dialog groups **AI agents** (Claude with model + effort + mode dropdowns —
  the **mode** picker chooses the Shift+Tab permission mode the agent starts in;
  Gemini/Antigravity with its model list; Grok via the xAI CLI; OpenAI as an
  editable command template), **shells** (PowerShell, cmd), and **scripts**
  (Python, custom);
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

## Task assignment & the shared board

A workspace is a coordinated team, not just parallel terminals — but **every
agent is a full, visible, interactive terminal you drive yourself.** There is no
hidden "orchestrator" agent spawning or directing others behind the scenes;
agents coordinate only by leaving notes on a shared board (see *Shared agent
awareness* below).

- **Shared activity log** — every Claude agent can post a one-line note to the
  workspace board via a single `log_activity` MCP tool; AI Hive serializes those
  writes through the GUI so concurrent agents can't clobber each other's
  entries. That one tool (`--mcp-config` + `--strict-mcp-config` + a
  pre-approved `mcp__aihive__log_activity`, so no permission prompts) is the
  *only* thing an agent can call — agents post notes, they never spawn, retask,
  or close each other. The channel is a Windows named pipe (QLocalServer, zero
  new deps); the MCP server (`app/mcp_server.py`) is stdlib-only. If it can't
  start, the app runs exactly as before (agents just can't post notes).
- **Workspace-scoped** — each agent's mcp config carries its workspace id
  (`AIHIVE_WS`), echoed with every call and enforced GUI-side, so a note always
  lands on the right workspace's board and never crosses into another's.
- **Dynamic role names** — when you reassign a card, the agent is auto-named for
  its task (Backend Architect, Database Engineer, Testing Agent, …) and renames
  as the task changes.
- **Intelligent model/effort** — a reassigned task auto-selects a model + effort
  (trivial → Haiku/low, mid → Sonnet/medium, architecture → Opus/high); never
  auto-uses top-tier/max. Explicit picks always override. **Ultracode** appears
  in the effort dropdown but greyed out — it's an in-session mode you enable
  with `/effort ultracode` in a supporting model's terminal.
- **Persistent agents** — agents **never** auto-close. A completed agent stays
  in the grid with a **Completed / Working / Awaiting Assignment** badge and a
  **⇄ Reassign** button, so you can review its work, continue the conversation,
  or retask it. Only you close an agent.
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
isolated.

**What the board is (and isn't).** The board shares the *roster* and the *terse
one-line notes* agents choose to log — **not** the text of your conversations.
One agent cannot read another's chat; it sees only what landed on the board (or
what changed on disk). So when a second agent seems to "know what you're working
on," it picked that up from the roster line, a logged note, or the shared files
— not from your dialogue with the first agent.

**Versus two plain terminals in one folder.** Two raw CLI sessions in the same
directory already share the *files* on disk (edit a file in one, the other sees
it). What they do **not** get is any shared notes, any live roster, any sense of
what the other is *doing*, or the auto-injected "read the board / log your
activity" convention — and neither reads the other's conversation. AI Hive adds
exactly that layer on top of the filesystem: it creates and maintains
`board.md`, auto-`--add-dir`s it into every Claude agent with the etiquette
system prompt, gives each a race-safe `log_activity` tool, and keeps the roster
reflecting live state. File-modification attribution is self-reported by agents in the log
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
  state, fonts) mark the session dirty and save on a short debounce. A
  periodic **safety-net autosave**
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
  Copy/Paste/Select-all menu. **Mouse**: a plain left-click **places the input
  caret** where you clicked (AI Hive sends the child the right run of
  arrow keys — exact on the caret's own line, and best-effort on another line of
  a multi-line prompt (Up/Down to the row, then Left/Right to the predicted
  landing column) — so you can jump into your typed text without arrow-key
  walking; a drag selects instead and never moves the caret. Double-click selects
  the whitespace-delimited word under the pointer, **Shift+click** extends the
  selection, and a **triple-click** selects the whole line. You can also select
  from the **keyboard** like a desktop text area: **Shift+Arrow/Home/End** grows
  the selection (**Ctrl** adds word granularity) without sending anything to the
  child, and a plain arrow collapses it. Any selection — mouse or keyboard — is
  editable: `Ctrl+C` copies it, `Ctrl+X` cuts it, and `Backspace`/`Del` deletes
  it (cut/delete drive the child's caret + Backspace, so they act on a selection
  anywhere inside the live input box — single **or** multi-row/wrapped, the
  leading `> ` prompt never touched; off the input box the key just drops the
  selection). **Ctrl+click** (or a middle/scroll-wheel
  click) opens a URL or an existing
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
  unlike its line-local `Ctrl+A`/`Ctrl+K` — empties multi-line input too), while
  `Ctrl+C` copies the highlighted input (and `Ctrl+X` copies then clears) rather
  than falling through to the interrupt. The
  terminal can't see the child's real input buffer, so it's inference from
  painted rows: no `>` found means it falls back to the cursor row (`Home`
  still jumps to line start). `Ctrl+Z`/`Ctrl+Y` (and `Ctrl+Shift+Z`) are an
  **approximate undo/redo**: a terminal keeps no local edit buffer, so this is a
  coarse "restore previous input" built from snapshots of the inferred input
  text (whole-prompt granularity, debounced into one step per typing burst) —
  undo clears the prompt and re-pastes the prior snapshot, a submit forgets the
  history, and an empty stack falls through to the old control bytes. (A true
  per-keystroke undo would need a local composer, which would bypass Claude's own
  `/`-slash, `@`-mention, and history UI — so this stays an approximation.)
  Keyboard-protocol escapes that pyte mis-parses
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

829 checks drive the real app headlessly (offscreen Qt platform) with real
child processes: tiling math + applied grid geometry, live streaming, stdin
round-trip, workspace-cwd inheritance, background retention while hidden,
card close terminating the process, zero-orphan shutdown, save/restore round
trips, the ConPTY path (interactive prompt, Ctrl+C, retention), every v2
feature (provider flags, per-workspace numbering, the agent-count badge,
explicit grids, folder changes, fonts, the shared board), task assignment
(role naming, model/effort selection, the named-pipe `log_activity` MCP
round-trip, workspace-scoped board notes), inline agent rename in the
card header (double-click; a custom name survives a retask) plus
a per-agent task summary beside the name, the per-card maximize/restore toggle
(solo one agent full-area without touching any sibling's process, then restore
the exact prior tiling) and the context-window usage badge beside the summary
("N% of 1M/200K", read from the transcript's last usage record — transient,
never persisted), the sidebar status badge
(output-activity busy detection, pulse/colour state machine, hover-only
controls), sidebar drag-reorder + collapsible categories (create/rename/delete,
drag workspaces in/out, single-level membership persisted across a v4 session
round-trip with v3 migration), the count-badge inline agent list +
click-to-reveal, and the waiting-for-input "?" detection (settled-screen
prompt/question scrape, gated on the idle timer, suppressed under
bypassPermissions) plus the notification chime it triggers (WAV synthesis,
the manager's waiting rising-edge `agentWaiting` signal, and the top-bar
mute toggle persisted in the ui state), the Agent/File Map
visualizer (transcript parsing for edited-vs-read
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
  providers.py             AI provider registry (Claude + Gemini/agy + Grok wired; OpenAI template)
  coordination.py          per-workspace shared board (.aihive/board.md)
  orchestration.py         task → role + task → model/effort heuristics (Qt-free)
  process_worker.py        QProcess engine, HybridDecoder, WinJob (line mode)
  pty_worker.py            ConPTY engine via pywinpty (full-terminal mode)
  terminal_agent.py        per-terminal model (worker + log/buffer + lifecycle)
  workspace_manager.py     model layer: workspaces, agents, spawn/assign/reassign
  orchestrator_bridge.py   named-pipe RPC server (GUI side) for board log_activity
  mcp_server.py            stdlib MCP stdio server (log_activity) the Claude CLI spawns
  session_store.py         atomic JSON persistence (AppData) + save-audit log
  transcripts.py           Claude-transcript snapshots (start/close, high-water)
  session_sync.py          reconcile a pinned id with the transcript on disk (fallback)
  session_hook.py          SessionStart hook: the child reports its live conversation id
  chime.py                 notification bell (WAV synth + async play, Qt-free) for the "?" alert
  file_activity.py         per-agent file attribution from transcripts (Qt-free)
  ui_theme.py              theme registry (skins) + apply_theme + the QSS stylesheet
  assets/fonts/            bundled OFL manuscript fonts (Cinzel/EB Garamond/Spectral)
  widgets/                 main_window, sidebar (drag-reorder + categories +
                           inline agent list w/ "?" + inline file explorer),
                           workspace_page, terminal_card, terminal_view (pyte grid;
                           Ctrl+click paths open + reveal), grid_selector
                           (on-screen popup), activity_panel, agent_file_map (tree
                           diagram), ornaments (drop-caps / dividers / count-badge
                           + working-count spinner)
  fsopen.py                shared OS-open helpers (open_path/open_with/reveal)
  filetypes.py             file-type icon map (shared by map + file explorer)
tests/smoke_test.py        headless end-to-end suite (766 checks)
```

Model/view rule: widgets subscribe to model signals and never own processes —
that's what guarantees hidden workspaces keep running.
