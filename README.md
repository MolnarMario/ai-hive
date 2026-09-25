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
  and Python scripts are also first-class agent types. A newly created agent's
  card is scrolled into view and given keyboard focus immediately, so you can
  start typing without an extra click.
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
  vanishes when all are idle. Opening a workspace's folder or deleting it are
  buttons in its own header bar now (see **First-class folders** below), not
  hover controls on the sidebar row.
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
  reading its terminal. The same summary shows in the terminal card header,
  where it fits itself to whatever width the row leaves (full text on hover).
- **Live model, effort and mode on every card** — the header says what the
  agent is *actually* running, e.g. `Opus 5 · high · plan`, right of its name.
  All three are yours to change from inside the terminal (`/model`, `/effort`,
  and `Shift+Tab` for the permission mode), so the readout follows the live
  conversation rather than the flags the agent launched with, and updates a
  second or two after you pick. The **permission mode is remembered**: an agent
  you put in plan or auto mode comes back in that mode the next time you open
  AI Hive, instead of reverting to ask-each-time.
  Click any agent to jump straight
  to its terminal card (switching workspace first if needed). When an agent is
  actually **waiting for you** — a permission prompt or an interactive
  question — a "?" lights up on the row (next to the working count) and beside
  that agent in the expanded list, so you can spot and answer it without hunting
  through terminals. Suppressed for agents launched in bypass-permissions mode
  (which never prompt). A **question chime**, a short droid-style "doo-dee-bweep?"
  whose last note is still sliding up when it stops, plays the moment that "?"
  appears (the standby→waiting rising edge), so you notice an agent needs you
  even while you're heads-down in another workspace — handy when agents you sent
  into plan mode come back with questions. A second **reply finished chime**, a
  "ta-da!" that steps up and holds its top note, plays when an agent finishes a
  reply you asked for. Claude's comes from its Stop hook, so it fires once at
  the real end of the turn; Codex, Gemini and Grok have no such hook and ring
  after 6 s of silence (2 s settle + `REPLY_QUIET_MS`) instead. Each has its own
  switch under **⚙ Options** in the top bar; the question chime starts on, the
  reply chime off, and both persist across restarts. The ♪ button beside each
  switch plays a preview, swaps in your own WAV or MP3 (5 s and 2 MB at most),
  or goes back to the built-in sound. AI Hive copies the file into its app-data
  `sounds` folder, so moving the original breaks nothing, and if the copy ever
  goes missing or won't play, the built-in chime rings instead.
- **Taskbar working count** — the same signal, but from *outside* the app. The
  Windows taskbar icon carries a small badge with the number of agents currently
  working, so you can tell at a glance from any other window whether the hive is
  still churning, whether it dropped from five workers to one (time to start
  reviewing), or whether it has gone completely quiet. **No badge at all** means
  nothing is running, so an idle hive costs you no visual noise. The disc turns
  **blue** when one of the working agents is waiting on a question, and shows a
  blue **?** when nothing is working but something needs you. Past nine it reads
  `9+`. Windows allows an app exactly one overlay icon and places it itself (on
  Windows 11, the icon's top-right corner), which is why the count and the
  question have to share one small square rather than getting a corner each.
  Toggle it with the **🪟 button** beside the bell (persists across restarts).
- **Background-shell indicator** — an agent can look completely idle while a
  command it started (a Claude `Bash` call run in the background, a plain
  shell's `cmd &`) is still going. A **⚙ gear badge** shows on the sidebar
  row, next to the affected agent in the expanded list, and in the terminal's
  card header once that's been true for a few seconds, so you don't close AI
  Hive thinking nothing is happening. It shares the taskbar's one overlay
  square too: when nothing is busy or asking but a shell is still running,
  the disc turns **violet** with the count, instead of showing nothing at
  all.
- **Plan usage readout** — two top-bar badges, one
  per Claude rate-limit window, showing how much you've burned and when it
  comes back: the **5-hour** pill reads `5h Claude 21% used, resets in 1h20m
  at 14:49` — countdown first, then the wall-clock time in your own timezone;
  the **7-day** pill reads `7d Claude 40% used, resets in 3d14h at 09:00` —
  the same shape as the Gemini pills beside it,
  but its countdown is **days+hours only, no minutes**, since a week-long
  window doesn't need to-the-minute precision (`3d14h` / `3d` / `14h` / `<1h`
  rather than an unreadable `86h27m`). Each pill wears its agent's colour
  (Claude terracotta, Gemini blue, GPT in the card header's light title ink)
  and turns red at 85%, so you see a wall coming instead of hitting it
  mid-task, and the red pill tells you which agent is about to hit it.
  **Click either to refresh**; hover for every limit window, your
  plan, and how old the reading is. The numbers come from the same place the
  CLI's `/usage` gets them, read in the background. Nothing is logged or
  persisted; it's a live readout only. **Every pill (Claude, Gemini, GPT)
  follows the same schedule**, set in `app/usage_poll.py`: every 90 seconds
  while one of that provider's agents is working (or finished within the last
  3 minutes), every 30 seconds once a window passes 90% with agents working,
  and every 6 minutes while none are, since the browser or the official apps
  can still move the number. A spent window drops back to 90 seconds, because
  a poll is already armed for a few seconds after its reset. Starting work
  after an idle stretch polls straight away, and so does waking the PC from
  sleep. A failed poll retries after 10 seconds; a `429` backs off instead,
  up to 16 minutes, and a click clears the backoff. **A pill greys only when
  its number stops being trustworthy**: polls have been failing and the
  reading is older than two poll intervals (at least 3 minutes, 12 while
  idle), or the window has reset since it was read. One failed poll leaves
  the number in colour, and the tooltip says what failed, for how long, and
  when the next try is. When a limit is actually
  spent its pill reads **`limit reached, resets in …`**, and the window raises
  `planLimitReached` / `planLimitCleared` signals (with the reset time) so
  other features can act on being cut off — e.g. relaunching blocked agents
  unattended the moment the limit resets. Both hide themselves when there's no
  Claude login. If a number can't be fetched at all — the endpoint
  rate-limits, and there's no longer an on-disk figure to fall back on — the
  pill says **`! usage limit unreadable — click to refresh`** rather than
  quietly disappearing, and clicking it retries immediately instead of waiting
  out the backoff.
- **Pick which usage readouts you want** — four pills can sit on the bar
  (Claude 5-hour, Claude 7-day, Gemini 5-hour, Gemini 7-day — the two Gemini
  pills use the same days+hours-only countdown on their 7-day window), each
  sized to its own text. **Hover one and an ✕ appears at its right edge** to
  close it; the **+ button** left of the auto-restart caption opens a checklist
  to bring any of them back. The choice is remembered per pill. Closing both
  Gemini pills also stops the `agy` usage subprocess entirely, so a
  Claude-only user isn't paying a few seconds a minute for a readout they
  don't want. Closing a Claude pill only hides that readout: the poll keeps
  running for both Claude windows regardless, because auto-recovery is driven
  from that reading.
  Reading the Gemini pills means running `agy`, and `agy` occasionally starts
  a helper that asks Windows for its own console, which Windows 11 grants as a
  real terminal window that flashes over whatever you are doing and closes a
  moment later. No launch flag prevents it (the flags we control don't reach
  that helper). The shared schedule keeps that to one run every 6 minutes
  while no Gemini agent is working; while one is, it runs every 90 seconds
  like the others, so the flash can show up more often then.
  **Nothing is remembered between runs except the choice itself.** Every pill
  opens saying `reading...` and fills in from a fresh fetch at startup, because
  a stored number goes stale exactly where it matters most — a 5-hour window is
  routinely spent and reopened between one launch and the next, and a restored
  figure looks identical to a live one.
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

  Either way AI Hive types `Continue` (pressing Esc first only when an older
  CLI's options menu is actually on screen, since on current versions Esc
  cancels Claude's own "continuing automatically" timer), staggered so agents
  don't all pile into a fresh window, then **verifies** against the
  conversation on disk, and retries if the resume didn't take. When Claude Code
  already continued by itself, the transcript says so and AI Hive stays out of
  the way. Cut-offs are caught two independent ways: off the live screen (every
  wording claude.exe 2.1.281 prints, including rows it paints with cursor jumps
  instead of spaces), and by a once-a-minute sweep of each idle agent's
  conversation file, which catches anything the screen missed. A weekly, Opus,
  Sonnet or Fable cut-off is resumed on its own clock only when that clock is
  exact (a dated reset, or the epoch Claude records with the 429); a bare
  "resets 8pm" on a 7-day window waits for the account reading instead.
  A printed clock is read in the zone Claude names after it ("resets 3am
  (Asia/Tokyo)"), falling back to the machine's zone when there is none, and
  a reset that rolls over to tomorrow keeps its wall time on the night the
  clocks change. Because the first message after a window expires is what STARTS the next
  5-hour window, resuming at 4am also means the clock has already rolled over by
  the time you sit down. The cut-off is recorded the instant it appears, along
  with the reset time the limit itself stated, so recovery doesn't depend on the
  usage API being reachable — it fires from the agent's own stated reset even
  when the account readout is rate-limited. Every step is logged to
  `session.log` (`BLOCKED` / `LATE-LATCH` / `NUDGE` / `RESUMED` /
  `SELF-RESUMED` / `CARRIED-ON` / `STILL-BLOCKED`), each resumed
  card shows a `— plan limit reset; auto-continued —` line, and the workspace
  board gets a note. A stopped agent is also visible at a glance, everywhere,
  live: an **⏳ hourglass** sits in the card header and next to the agent in
  the sidebar's expanded workspace dropdown, and the workspace row itself
  carries an **⏳N** badge counting how many of its agents are currently
  stuck — all three clear the instant that agent resumes, whether that was
  auto-continue or you restarting it yourself. If Claude Code continues on
  its own, the hourglass clears within a minute of the conversation
  writing new work.

  **Gemini agents are covered too**, with the differences their CLI forces.
  agy refuses a turn with `⚠ Individual quota reached … Resets in 1h40m21s`
  and then returns to its prompt, so there is no menu to close (none is sent)
  and no menu disappearing to prove the resume took — instead a quota that is
  still spent answers with a *fresh* message, which is what AI Hive watches for.
  The countdown is relative rather than a wall clock, which is if anything
  better: it is resolved to a real time the moment the message appears and needs
  no guessing about which side of midnight the reset falls on. **⏯ Recover at
  startup** works too, by a different route: agy keeps no conversation on disk,
  so instead of reading a transcript AI Hive uses the fact that `agy --continue`
  redraws the conversation it ended on — a quota message still sitting there
  when the agent comes back up is a cut-off that was never resolved. Its printed
  countdown is stale by then (the text is frozen at the moment it was written),
  so rather than trust it the agent is simply tried straight away: if the quota
  is back it carries on, and if it isn't, agy says so with a fresh countdown that
  *is* accurate and the retry waits for that instead. Claude's account readout is
  never used to resume a Gemini agent (it knows nothing about a Google quota).
- **Let Claude Code update itself** — the **⬇ button** beside the taskbar toggle
  opens a small **Updates** panel, and what it offers depends on how Claude Code
  is actually installed on your machine (it re-reads that every time, so it is
  never out of date):

  A package-manager install *does not update itself*, and there is no setting
  that fixes it: Claude Code knows a package manager owns its file and refuses
  to replace it. Meanwhile its own "Update available!" banner compares against
  Anthropic rather than against `winget`, so a fully upgraded winget install
  still nags in every terminal — and because model aliases resolve *inside* the
  binary, a stale one silently can't launch newer models however many times you
  upgrade. The install method *is* the behaviour, so the panel offers to change
  the install: it runs Anthropic's documented installer
  (`irm https://claude.ai/install.ps1 | iex`) behind a consent dialog that shows
  you the exact command, what changes, what doesn't, and both ways back, with an
  action button that stays disabled until you tick "I understand and agree".

  It installs *alongside* what you have (under your user folder, no
  Administrator rights, a different directory from the winget copy), which means
  **it never touches the running file, so it works with every agent alive** —
  unlike the startup gate below, which only ever gets one moment. AI Hive picks
  the new binary up straight away: it looks for the native launcher before it
  looks at `PATH`, and it repoints the agents already on screen too, so nothing
  needs a restart or a fresh terminal.

  Afterwards the same panel is the way back. **Pause** is one setting, instant,
  no download, and freezes you on exactly the version you're running ("I don't
  trust anything newer than this"). **Follow the stable channel** takes builds
  about a week old that skip releases with major regressions, and always pins a
  floor at your current version so switching can never walk you *backwards*.
  **Revert** reinstalls the winget package and removes the native one. And
  **remove the old copy** is an optional tidy-up, refused (never forced, never
  killing anything) while a session is still using it. Every one of these is
  written to `session.log` as a `CLI-MIGRATE-*` line.
- **Check for CLI updates at startup** — a checkbox inside that same panel,
  **off by default** because turning it on lets AI Hive install software on your
  machine. Armed, it checks for a newer Claude Code and Gemini (`agy`)
  before the window is even built, and installs what it finds.

  Startup isn't just convenient here, it's the only moment that works. Every
  agent is a `claude.exe` child, Windows can't overwrite a running `.exe`, and
  `winget` records the new version in its database anyway — which is exactly how
  you end up with the CLI's own "Update available!" banner printing forever
  while `winget upgrade` insists there's nothing to do, and (because model
  aliases resolve inside the binary) no new model in `/model` however many times
  you upgrade. So the gate runs above everything: no agent exists yet, nothing
  holds a file. Two rules follow from that:
  - **If anything is already running the CLI, the update is skipped without
    running the installer at all.** Those are your own sessions or another
    app's, and asking an installer to replace a locked file is precisely what
    writes the false record. They're never killed either. "Running the CLI" is
    matched on the executable's full path, not its name: the Claude desktop
    app's binary is also called `Claude.exe`, and while it was counted the gate
    skipped every launch forever (19 processes by name on this machine, 11 of
    them actually the CLI).
  - **Success is decided by running `--version` on the binary itself**, before
    and after, never by what the installer said about itself.

  A small splash names each CLI and what's happening, with a **Skip** button
  that launches AI Hive immediately (an install already running is left alone,
  never killed halfway through a 285 MB file — if you skip mid-install, agents
  for that CLI simply wait rather than risk running a half-written program).
  When everything is current the whole thing is about a second and a half. If an
  update *couldn't* apply, a quiet pill appears under **⚙ Options** saying so
  and the Options button itself lights up, with the reason on hover, so "why am
  I still seeing the nag?" has an answer instead of being a mystery. Every version transition, skip and failure is written to
  `session.log` (`UPDATE-CHECK` / `UPDATE` / `UPDATE-SKIP` / `UPDATE-FAIL`), so
  "it broke this morning" is a lookup rather than an investigation.
- **Send a message on a countdown** — type into an agent's terminal as usual,
  then press **`Ctrl+Shift+Enter`** instead of Enter. A small composer opens
  with what you typed, you pick when it should go in (**5m / 15m / 30m / 1h /
  2h**, a custom delay like `45m`, `1h30` or `2:15`, or a wall-clock time like
  `03:30`), and AI Hive holds the message and types it in when the countdown
  ends. This is what makes **chaining agents while you're away** possible: send
  the first one off now, queue the second for twenty minutes' time and the third
  for forty five, then leave. An **⏱ countdown** sits in the card header for as
  long as something is queued (click it to change or cancel, or to send a
  queued message right away), with an **⏱N** badge on the workspace row and a
  marker beside the agent in the sidebar's expanded list. Same dialog from the
  header's right-click menu if you'd rather not learn the chord.
  A scheduled message is a *message*, not an assignment: it never overwrites
  what the card says the agent is working on. Queued messages survive a
  restart, but one whose moment passed while the app was closed comes back
  marked **missed** rather than firing hours late into a conversation that has
  moved on: you decide whether it still applies.
- **Scrollbar with prompt milestones** — every terminal has a slim scrollbar on
  its right edge, with a dot at each point where *you* submitted a prompt. Hover
  a dot for the prompt text, click it to jump straight back to that moment. It
  is the conversation's table of contents: the dots are the milestones, and the
  stretch between two of them is one exchange. Nothing AI Hive types itself gets
  a dot (delivered tasks, scheduled messages, auto-continue nudges), and neither
  does answering a permission menu or a question, so the marks are only ever
  your own instructions. The bar hides itself when there is nothing to scroll,
  so a fresh terminal shows none, and a **`/clear`** takes the scrollback and its
  dots with it.
  Dots are **not lost when you reopen AI Hive**: milestones are only minted from
  a live keystroke, so a restored conversation would otherwise come back with
  none, and that is exactly the conversation long enough to want to jump around
  in. The prompts you typed are read back from Claude's own transcript and
  matched against the restored scrollback, so the dots come back where they
  belong. Only genuinely typed prompts are used, so nothing can invent a dot you
  did not create.
  This works because AI Hive asks Claude for its **classic renderer**, which
  prints the conversation into the terminal proper instead of keeping it inside
  the alternate screen where the app can never see it. The trade is that the
  classic renderer is not the flicker-free one, and Claude's menus stop being
  clickable (keyboard still works). Right-click the top bar and untick
  **Scrollback lives in AI Hive** if you would rather have the flicker-free
  renderer back; agents already running keep whatever they launched with.
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
  **highlighted** in it, so you can see where it lives. **Right-click** the same
  path for Open / Open with... / Reveal in folder / Copy path (a URL gets Open
  link / Copy link address) — "Reveal in folder" opens Explorer with the file
  already selected, and "Copy path" copies the resolved **absolute** path, which
  is what pastes into an address bar.
- **Search the sidebar** — a 🔍 next to the category button expands an input
  that covers the WORKSPACES title/count; as you type it **highlights every
  match** across workspace names, agent names, and agent summaries, and
  auto-expands a workspace to reveal a matching agent. Esc (or clicking 🔍
  again) closes it and restores what was expanded before.
- **First-class folders** — every workspace header shows its full path
  (eliding only what its own buttons don't leave room for) with **Delete**,
  **Open folder** and **Change…** buttons; agents launch rooted there.
- **Font controls** — per-agent A−/A+ (and `Ctrl+±`) plus a global A−/A+ under
  **⚙ Options**; sizes persist.
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
- **Event log** (the **Log** button in the top bar, or Ctrl+Shift+L) opens a
  separate window with one timeline for every agent in every workspace: the
  prompts you sent, tasks AI Hive handed over, questions, finished replies
  with how long they took, limit cut-offs with a countdown to the reset,
  resumes, crashes and missed scheduled messages. Click a workspace name to
  switch to it, or an agent name to jump to its card. A question row keeps
  counting ("waiting 12m") until you answer it, then reads "answered after
  12m"; the Log button shows how many are still waiting. Filter by workspace
  and agent (a checkable tree, or right-click a row for "Show only this
  agent"), by event type, by text, or to **Needs you** only. Lifecycle events
  (started, stopped, added, renamed) and board posts are recorded too but
  hidden until you switch them on. The log lives in
  `%APPDATA%/AIHive/AI Hive/event_log/`, one JSONL file per day, pruned after 30
  days, so what ran overnight is still there after a restart.
  (Sub-agent file work and non-Claude agents can't be attributed — those nodes
  show without file edges; see below.)
- **⚙ Options** — one button on the top bar opens a panel with everything that
  used to compete for room up there: **Recover at start-up**, **Resume on usage
  reset**, **Question chime**, **Reply finished chime**, **Taskbar count** and **Check for CLI
  updates at start-up** as labelled switches (each with a green/dark LED, so
  "will my work resume by itself?" is answerable at a glance), the detected
  Claude Code install method with a **Manage…** door to the Updates panel, and
  the theme and global font size below. Click outside or press `Esc` to close.
  The bar itself keeps only what you actually glance at: the usage pills and
  their `+`, and `+ Terminal`, so it fits on a laptop instead of hiding
  controls behind a scrollbar.
- **Themes (Winamp-style skins)** — a dropdown under **⚙ Options** swaps the whole
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
  (1 full, 2 side-by-side, 3 = 3×1, 4 = 2×2, 5–6 = 3×2, …). Line and full-terminal
  cards share the same grid.
- **Folder & activity** — the workspace header shows the full project path
  with **Delete** / **Open folder** / **Change…**; **Activity** opens a panel
  with the live agent roster, the shared coordination log, and git-changed
  files.
- **Font size** — per-agent `A−`/`A+` in each card header (or `Ctrl+±` while
  focused), and global `A−`/`A+` under **⚙ Options**; both persist.
- **App shortcuts use `Ctrl+Shift+…`** (T = new terminal, N = new workspace,
  B = toggle sidebar) so every plain `Ctrl`/`Alt` key, `Tab`, and `Shift+Tab`
  goes straight to the focused terminal — click a full-terminal card and
  `Shift+Tab` cycles Claude Code's modes, `Ctrl+C` interrupts (when nothing is
  selected; otherwise it copies), `Ctrl+R` reverse-searches, arrows/`Tab`
  complete, exactly as in a real terminal.
- **Card controls** — the header keeps only what you reach for: `A−`/`A+`
  font, `⤢` maximize, `✕` close. **Right-click the header** for Start,
  Stop (stdin EOF), Restart (fresh session) and Assign / reassign; those four
  are rare and deliberate, and the width they used to hold now goes to the
  summary. Type into the bottom input line to send a command to that terminal;
  `↑`/`↓` recall history; `cls`/`clear` clears the console locally.
- **PowerShell quirks to know** — there is no prompt line by design, and
  bare `cd` prints nothing in PowerShell (use `pwd` to see where you are).
  Typing a TTY-only program (`claude`, `vim`, `htop`, …) prints a hint
  instead of leaving you with a cryptic error.
- **Sessions** — workspaces and terminals are saved automatically and
  restored on launch (including provider/model/effort, the Claude permission
  mode each agent was last in, per-agent font, grid layout, current task, and
  assignment badges). Everything that was running
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
  Agents that were *stopped* stay stopped, but the card is **not** blank:
  it comes back showing **the conversation that was on it when you closed**,
  exactly as it looked, with a slim `not running · press any key to resume`
  strip along the bottom. So reopening the app shows you your work rather
  than a wall of dead terminals, and nothing relaunches behind your back;
  the first keystroke starts it, and a woken Claude agent also reclaims its
  conversation. While an agent that *is* coming back up launches, its card
  shows a **quiet loader** (a sweeping arc over `restoring conversation…`)
  instead of the CLI's half-drawn boot frames, which used to appear as a
  mangled narrow fragment in the terminal's top-left corner until the
  conversation finished replaying. It lifts with a short fade the moment the
  prompt is live, and gets out of the way at once if you start typing.
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
- **Your names stay yours** — assigning a task never renames an agent. Earlier
  versions ran the task through a keyword-to-role heuristic (Backend Architect,
  Testing Agent, …) and renamed the card to the guess, which also printed the
  same string twice in the header. Both are gone; only you rename an agent.
- **Intelligent model/effort** — a new spawned worker auto-selects a model + effort
  (trivial → Haiku/low, mid → Sonnet/medium, architecture → Opus/high); never
  auto-uses top-tier/max. Explicit picks always override. **Ultracode** appears
  in the effort dropdown but greyed out — it's an in-session mode you enable
  with `/effort ultracode` in a supporting model's terminal.
- **Persistent agents** — agents **never** auto-close. A completed agent stays
  in the grid so you can review its work, continue the conversation, or retask
  it. Only you close an agent.
- **Reassign anywhere** — right-click any card header and pick **Assign /
  reassign a task** to hand it fresh work without losing the session. Its name,
  model and effort are left exactly as you set them.

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
  lose a just-created agent. Metadata changes (task, assignment, name, run
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
  while the middle button is often eaten by the OS autoscroll.) **Right-click**
  a link and the menu gains Open / Open with... / Reveal in folder / Copy path
  above the usual Copy / Paste / Select all — same wording as the Agent/File
  Map's menu, and Copy path gives you the absolute path. **Image
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
  them, and there's no per-command Ctrl+C (right-click the header for Stop or
  Restart, or
  switch that agent to full-terminal mode). Bare `cd` prints nothing in
  PowerShell — that's PowerShell, not the app.
- Windows-first: developed and tested on Windows 11. On non-Windows,
  full-terminal mode is unavailable (pywinpty is Windows-only) and Job-Object
  tree-kill is skipped; line mode still works but is untested there.

## Verify

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

2090 checks drive the real app headlessly (offscreen Qt platform) with real
child processes: tiling math + applied grid geometry, live streaming, stdin
round-trip, workspace-cwd inheritance, background retention while hidden,
card close terminating the process, zero-orphan shutdown, save/restore round
trips, the ConPTY path (interactive prompt, Ctrl+C, retention), every v2
feature (provider flags, per-workspace numbering, the agent-count badge,
explicit grids, folder changes, fonts, the shared board), task assignment
(model/effort selection, a retask never renaming the agent, the named-pipe
`log_activity` MCP round-trip, workspace-scoped board notes), inline agent
rename in the card header (double-click) plus
a per-agent task summary beside the name, the per-card maximize/restore toggle
(solo one agent full-area without touching any sibling's process, then restore
the exact prior tiling), the context-window usage badge beside the summary
("N% of 1M/200K", read from the transcript's last usage record — transient,
never persisted), and a date-and-time stamp under every finished reply,
drawn inline in the terminal directly beneath Claude's own "✻ Worked for
16m 36s" footer (a second FIFO-capped milestone type mirroring the
scrollbar's prompt marks, captured live on the busy-to-idle settle and
otherwise READ FROM THE CONVERSATION ON DISK by matching each transcript
reply's closing line into the scrollback — so reopening the app shows when
an answer was really generated rather than nothing at all; re-anchored
across a card rebuild from the pty replay, skipped rather than guessed when
a reply has scrolled away or a row runs out of room),
deferred "send later" messages (delay/clock parsing, the
Ctrl+Shift+Enter gesture sending nothing to the child, delivery by nudge so an
assignment is never overwritten, a refusal retried then given up on as missed,
the countdown tick never marking the session dirty, and a message that came due
while the app was closed coming back missed rather than firing late), the live
model/effort/permission-mode readout beside the agent's name
(normalising `claude-opus-5` to "Opus 5", merging the last turn's model with a
mid-session `/model` or `/effort` pick, reading the Shift+Tab mode off the
transcript's permission-mode records and writing it back so a reopened agent
returns in the same mode, translating the CLI's internal "default" into an
omitted flag) and the
self-fitting header summary that uses every free pixel, the sidebar status badge
(output-activity busy detection, pulse/colour state machine, hover-only
controls), sidebar drag-reorder + collapsible categories (create/rename/delete,
drag workspaces in/out, single-level membership persisted across a v4 session
round-trip with v3 migration), the count-badge inline agent list +
click-to-reveal, and the waiting-for-input "?" detection (settled-screen
prompt/question scrape, gated on the idle timer, suppressed under
bypassPermissions) plus the question chime it triggers (WAV synthesis,
the manager's waiting rising-edge `agentWaiting` signal, and the top-bar
mute toggle persisted in the ui state), the reply finished chime (Claude's
Stop-hook edge, the hookless providers' quiet timer, once per submitted turn,
never for a shell or a waiting agent), custom chime sounds (WAV/MP3
validation, the MCI player thread, fallback to the built-in sound, the copy
into app data and its session round-trip), the event log (per-day storage,
torn lines, 30-day pruning, the stable agent uid its links key on, the
collector's question settle and limit-menu exclusion, answers and resumes
closing their rows, the window's filters, live rows and link hit-testing),
the Agent/File Map
visualizer (transcript parsing for edited-vs-read
attribution, sub-agent detection, shared-file grouping, headless paint, header-
button wiring, and the drag/zoom/hit-test/click-to-focus interactions) and the
Layout popup
staying on-screen when the window is at a monitor edge, the startup CLI-update
gate (version parsing on all three real output shapes, a parse failure never
being the reason to install, the switch off meaning not one command runs, a
live `claude.exe` blocking the update with no installer command issued at all,
an installer that reports success while the binary is unchanged, a package
database ahead of the file being reported and never forced, a check that
outruns its budget failing open to launch, agy self-updating serially after
Claude with no interleaving, the preference defaulting off and surviving a
close/reopen with no `SESSION_VERSION` bump, outcomes never marking the session
dirty, and the factory the suite shares doing no update work at all), the
install-method control (each install path classifying by its full path through
a symlink shim and regardless of case, one detected state per machine including
a policy-locked one, `resolve_claude` preferring the native launcher over what
`PATH` would answer, a migration whose file didn't change counting as a failure,
a settings write preserving every unknown key and re-reading first so a key an
agent saved meanwhile survives, an unparseable settings file being refused and
left byte-identical, the stable channel refusing to be written without its
version floor, `DB_STALE` being unreachable on a native install, cleanup
counting the recorded winget path rather than the resolved binary and issuing no
kill, a revert that leaves the native install in place when the winget copy
doesn't come back, the live specs being repointed without disturbing a running
agent or marking the session dirty, a command that never returns not blocking
the GUI thread, and the consent action staying disabled until the box is
ticked), and the reliability
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
  orchestration.py         task → model/effort heuristic (Qt-free)
  process_worker.py        QProcess engine, HybridDecoder, WinJob (line mode)
  pty_worker.py            ConPTY engine via pywinpty (full-terminal mode)
  terminal_agent.py        per-terminal model (worker + log/buffer + lifecycle)
  workspace_manager.py     model layer: workspaces, agents, spawn/assign/reassign
  orchestrator_bridge.py   named-pipe RPC server (GUI side) for board log_activity
  mcp_server.py            stdlib MCP stdio server (log_activity) the Claude CLI spawns
  session_store.py         atomic JSON persistence (AppData) + save-audit log
  transcripts.py           Claude-transcript snapshots (start/close, high-water)
  screen_snapshot.py       a stopped card's last screen, so reopen shows the conversation (Qt-free)
  session_sync.py          reconcile a pinned id with the transcript on disk (fallback)
  session_hook.py          SessionStart hook: the child reports its live conversation id
  chime.py                 question + reply finished chimes (WAV synth, custom WAV/MP3, async play, Qt-free)
  taskbar_overlay.py       the Windows taskbar corner badge (ITaskbarList3 via ctypes, Qt-free)
  claude_usage.py          live plan-usage reading (/api/oauth/usage) + limit edges (Qt-free)
  limit_banner.py          recognising a usage cut-off + when it resets (Qt-free, shared)
  limit_ledger.py          durable record of cut-offs: who, when, and how it ended (Qt-free)
  event_log.py             the event log's storage + wording: per-day JSONL, prune (Qt-free)
  event_hub.py             collects agent/workspace signals into event-log records
  scheduled_send.py        deferred messages: parse a delay/clock, hold it, format the countdown (Qt-free)
  cli_update.py            startup CLI update gate: decide from the FILE's own version (Qt-free)
  cli_install.py           how Claude Code is installed + the switch onto the self-updating build (Qt-free)
  file_activity.py         per-agent file attribution from transcripts (Qt-free)
  ui_theme.py              theme registry (skins) + apply_theme + the QSS stylesheet
  assets/fonts/            bundled OFL manuscript fonts (Cinzel/EB Garamond/Spectral)
  widgets/                 main_window, sidebar (drag-reorder + categories +
                           inline agent list w/ "?" + inline file explorer),
                           workspace_page, terminal_card, terminal_view (pyte grid;
                           Ctrl+click paths open + reveal), grid_selector
                           (on-screen popup), activity_panel, agent_file_map (tree
                           diagram), ornaments (drop-caps / dividers / count-badge
                           + working-count spinner + taskbar-badge painter),
                           event_log_window (the event log timeline),
                           update_splash (the startup CLI-update panel + its
                           worker thread), update_panel (the Updates panel +
                           its consent modal + its worker thread)
  fsopen.py                shared OS-open helpers (open_path/open_with/reveal)
  filetypes.py             file-type icon map (shared by map + file explorer)
tests/smoke_test.py        headless end-to-end suite (2090 checks)
```

Model/view rule: widgets subscribe to model signals and never own processes —
that's what guarantees hidden workspaces keep running.
