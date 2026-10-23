# Changelog

What changed in each AI Hive version, newest first. The app shows these
sections in its update dialog, so write them for the person using AI Hive:
one line per change they would notice.

Every PR merged to `main` bumps `__version__` in `app/__init__.py` and adds a
`## x.y.z` section here with the same number (a smoke check enforces it). No
em dashes: this text is shown in the app.

## 0.51.0

- Bold word starts is now called Bionic reading, and its tooltip explains
  how a bold start helps some readers keep their place in a line.
- Boldness is now called Contrast, and Options shows it only while Bionic
  reading is on.
- With Bionic reading on, the line you are typing no longer turns bold
  when its text fills a row and the caret moves to the row below.

## 0.50.0

- Changing the terminal font or letter spacing no longer flickers through
  the old screen in the new font. Each terminal on screen shows "applying
  the new font" until the agent has redrawn, at most 3 seconds.
- Options > Terminal font ends with "Load a custom font...". It takes
  .ttf, .otf and .ttc files, explains why it refused anything else, and
  keeps the font across restarts.
- Bold word starts has a Boldness setting from 1 to 5 under the switch.
  Level 1 is the old plain bold, level 2 draws a heavier bold, and levels
  3 to 5 also fade the rest of each word. It starts at 3, and each
  terminal font remembers its own level.
- Dropdowns open below their box instead of covering it.
- The New Agent dialog keeps one width while its conversation list loads.
  Long conversation previews are cut short in the list and show in full
  in their tooltip.

## 0.49.0

- Options > Appearance has a Terminal font picker and Letter spacing for
  the agent terminals. Pick Cascadia Mono, JetBrains Mono NL, IBM Plex
  Mono, Atkinson Hyperlegible Mono or OpenDyslexic Mono, or Theme font for
  the skin's own (Share Tech Mono in Adeptus Mechanicus). Default keeps
  Consolas, so nothing changes until you pick. Letter spacing adds 0 to 3
  px between letters, and box-drawing lines stay solid at any spacing.
- A new Bold word starts switch in Options > Appearance draws the first
  half of each word in bold in every terminal. Code, paths, links and the
  line you are typing keep their regular weight. It starts off.
- The New Agent model list matches Claude's own /model menu, older models
  included (Opus 4.8, Sonnet 4.6 and the rest). The "[1m]" entries are
  gone: Opus, Sonnet and Fable already run with a 1M context window.
  Agents saved on one still launch as before.
- The Default model and effort entries in New Agent say what Claude will
  run, for example "Default (high, saved by /effort)". Default uses the
  last pick /model or /effort saved in any terminal, which looked like
  the previous agent's settings carrying over.
- Answering one of Claude's question forms puts a dot on the terminal
  scrollbar, a yellower shade of your prompt dots, so you can scroll back
  to what you answered.

## 0.48.1

- An agent you closed before typing anything to it comes back as a fresh
  session. It used to show "No conversation found with session ID" above
  its prompt, and in a folder with other conversations it could come back
  holding another agent's chat.
- When the window stops responding for 3 seconds or more, AI Hive writes
  where it was stuck to hang.log, next to your session file. The next
  launch moves it to hang.log.1, so a freeze you had to close AI Hive in
  keeps its record. Attach both to a report of a freeze.

## 0.48.0

- At launch AI Hive starts the agents of the workspace it opens on first.
  Agents in your other workspaces wait until those have their prompt, then
  start two at a time, and opening one of those workspaces starts its
  waiting agents straight away. Starting all of them together used to
  freeze the window for up to 1.2 s while the agents in front of you
  loaded. A card still waiting shows its loader, and closing AI Hive
  before it starts brings it back next launch.

## 0.47.0

- Opening AI Hive no longer waits on GitHub. It used to fetch and install
  updates before showing a window, which took about a second on every
  launch. Now a new Options switch, Update AI Hive in the background (on
  by default), checks a minute after the window opens and then every hour.
  It installs a newer version while you work, the button by the version
  number reads "Restart to update", and the next launch runs it. An update
  that needs new Python packages waits for your click, and so does a
  folder that is off `main` or has local changes. Switched off, only the
  button checks.
- The launch no longer copies your agents' conversations or scans them
  for plan-limit stops on the window's thread. Both run in the background
  while the window builds, which took about 0.6 s of freezing off the
  launch. Agents still wait for the backup copy before they start.

## 0.46.1

- The New Agent dialog opens without waiting for its list of past
  conversations. The list loads in the background while the resume picker
  says "Loading conversations", and OK stays off for a Claude agent until
  it arrives, so Enter can't start a fresh conversation you meant to
  resume. Over a folder with 131 conversations, the first opening went
  from about 400 ms to about 80 ms.

## 0.46.0

- AI Hive opens much faster when you have many workspaces and agents. The
  saved theme and console font now apply before the window is built,
  instead of restyling every widget and redrawing every terminal after it,
  and the sidebar builds once instead of once per workspace. On a copy of
  a 15 workspace, 18 agent session, building the window went from 14 to
  18 seconds down to 2 to 3.
- A new agent starts at its card's real size. It used to start at 100
  columns by 30 rows and redraw when the card's size caught up, and a
  conversation resumed from the New Agent dialog stayed wrapped at 100
  columns in the scrollback.
- Each launch writes a `PERF startup` line to session.log with how long
  every step took, and a `PERF stall` line whenever the window freezes for
  more than 150 ms, naming the terminal output or redraw behind it. Nothing
  changes on screen.
- Claude's install is looked up once per launch instead of once per agent,
  and the tooltip handler starts after the window appears, which trims a
  little more off the launch.

## 0.45.3

- The Adeptus Mechanicus theme has a new aquila crest: a double-headed
  eagle in phosphor green on a dark glass plate with a green edge. The old
  grey crest was hard to make out. The new one stays sharp at 150%
  display scaling.

## 0.45.2

- A Claude card taller than 48 rows keeps its scrollback when the card
  changes size. It used to lose everything above the last screenful, in
  resumed conversations and open ones alike, so the scrollbar went away
  and the mouse wheel did nothing.

## 0.45.1

- A long Claude reply no longer shows up two or three times over in a
  card's scrollback. When Claude redraws a reply taller than the card
  (after the idle recap, closing Ctrl+O, or coming back to the
  terminal), the rows that scrolled off a second time are copies, and
  AI Hive now drops them.

## 0.45.0

- Ctrl+F in a terminal opens a find box in its top right corner, like
  Chrome's page search. It searches the agent's whole history as you
  type, ignoring case, paints every match yellow and the current one
  orange, shows how many there are and marks each one on the scrollbar.
  Enter or F3 goes to the next match, Shift+Enter or Shift+F3 to the
  previous one, and Esc closes the box. Text you selected becomes the
  search. A phrase the agent wrapped onto two rows is not found. In a
  full-screen program such as less or vim, Ctrl+F still goes to the
  program.
- A mouse wheel over a terminal's scrollbar reaches an agent that tracks
  the mouse at the spot under the pointer. It used to reach it at the
  terminal's left edge.

## 0.44.3

- In the Illuminated Manuscript and Adeptus Mechanicus themes, the top
  bar's buttons and pills sit in the middle of the header. They used to
  sit closer to its bottom edge.
- The Adeptus Mechanicus aquila plaque is smaller and stays on the top
  border, so it no longer hangs over the usage pills.

## 0.44.2

- The + that picks which usage pills to show sits in the middle of its
  square. It used to sit low and to one side.
- The sidebar collapse and expand button in the top bar is bigger, so
  its panel and arrow are easier to make out.

## 0.44.1

- The usage pills say "checking..." at launch instead of "unreadable"
  when the Claude login expired during a long break. AI Hive checks again
  every 2 seconds and fills the pill in as soon as the first Claude agent
  renews the login, instead of waiting up to 6 minutes.
- The gear on an agent card no longer lights up from launch on for nearly
  every agent. It counted AI Hive's own activity log helper, which Claude
  starts a little later, as a background process.
- With a terminal focused, the Win+V clipboard panel and the Win+. emoji
  panel now take the arrow keys, Enter and Esc. Before, those keys went to
  the agent and the panel couldn't be driven or closed from the keyboard.
  An emoji picked in the panel is typed into the agent. The panels open
  at the terminal's cursor, and a Japanese, Chinese or Korean input method
  works in a terminal, showing what you compose at the cursor until you
  commit it.

## 0.44.0

- Ctrl+Tab moves to the next agent in the workspace, left to right along
  a row and then down to the next row, wrapping back to the first.
  Ctrl+Shift+Tab goes the other way. If an agent is maximized, the next
  one takes its place.
- Options > Help > Keyboard shortcuts lists every shortcut AI Hive has.
- The button at the top left that hides the sidebar now shows which way
  it will go: an arrow pointing left while the sidebar is open, right
  while it is hidden. Its tooltip names the action and the shortcut.
- Fullscreen (F11) no longer leaves a thin strip of wallpaper at the
  window's edges, and neither does the maximized window you return to.

## 0.43.3

- Hovering a category in the sidebar now shows plainly which one the
  pointer is on: the header gets a visible tint, its arrow lights up in
  the accent colour and its count brightens. The category's outline
  stays visible under the tint instead of disappearing.
- An agent no longer shows a "?" when it hasn't asked anything. Claude
  sometimes signals a question it never asks, and the "?" then stayed
  lit on an agent that was done. AI Hive now waits until the question is
  actually in the conversation before it shows the "?".

## 0.43.2

- A narrow card header no longer cuts its chips off mid-word, such as
  "Opus 5.5 · high ·" or "10% of 1". As the card narrows, the header
  drops whole words in a fixed order: the task summary shrinks to a
  clipboard icon, then the word integrator goes, then the model's mode and effort,
  then the " of 1M" of the context badge, then the lane counts.

## 0.43.1

- A crowded card header no longer shortens the integrator's lane chip to
  "int...tor". The chip drops whole words instead, the word integrator
  first since its icon already says it, then the counts. The chip's
  tooltip still lists the role and every count.

## 0.43.0

- Giving an agent its own lane, from the New Agent dialog or Restart in
  own lane, now turns the workspace's Lanes toggle on, so the header no
  longer says Lanes is off beside a laned card.
- Make integrator works on an agent without a lane once the workspace has
  a lane. After you confirm, the agent restarts in its own lane in a new
  conversation and becomes the integrator.
- The + that brings back closed usage readouts is now a square button with
  the same bold + as the add workspace button.

## 0.42.1

- After a usage limit, Claude Code's own "continuing automatically" gets
  to go first. AI Hive used to type Continue a few seconds after the
  reset, which cancelled Claude's own continuation. Now it waits until 7.5
  minutes past the reset and steps in only if Claude hasn't continued.
- Pressing Esc on Claude's automatic continue now also tells AI Hive not
  to resume that agent.
- Before it continues a Claude agent whose reset time has passed, AI Hive
  checks the account's usage reading and keeps waiting while the limit is
  still spent. With no fresh reading, it waits up to 10 minutes, then goes
  by the clock as before.

## 0.42.0

- The Close merged agents dialog has a "Don't ask again" box, with a
  switch under Options > Agents (Confirm Close merged agents) that brings
  the dialog back.
- Closing several merged agents at once fades their cards out in turn and
  rearranges the grid once, instead of once per agent.
- Pressing F11 to leave fullscreen goes straight back to a maximized
  window, without flashing at half size first.

## 0.41.2

- The usage pills keep loading at startup when the first read fails and
  say "retrying..." while they try again within seconds. They only show
  that usage could not be read after 3 minutes of failures, or right away
  for an expired login.
- A new agent in its own lane shows the loading animation while AI Hive
  creates its git worktree, instead of "terminal not running".

## 0.41.1

- An agent that writes about the usage-limit menu in its own reply no
  longer gets the hourglass. Only the real menu counts.

## 0.41.0

- Ctrl+Up maximizes the selected agent and Ctrl+Down shows every agent
  again. A focused terminal no longer receives these two keys.
- A Claude card's usage-limit hourglass no longer comes back after a
  resume when Claude redraws the old limit message. A limit the
  conversation never recorded clears after 10 idle minutes.

## 0.40.0

- Resizing a Claude terminal re-wraps its whole scrollback at the new size,
  not just the bottom screenful. The reflow waits until the agent is idle.
- A reply paragraph no longer shows up twice in a Claude card's scrollback
  after Claude redraws its screen while idle.
- Closing a Codex agent no longer kills the Codex daemon that your other
  Codex windows share.
- Codex's own daemon and MCP servers no longer light the gear badge, and
  the gear's kill menu no longer offers them.
- Clicking a workspace in the sidebar or in the event log puts the cursor
  in its top-left agent (or the maximized one), so you can type right away.
- The New Agent dialog shows the Full terminal box only for shells, scripts
  and custom commands. It was a forced, greyed-out tick for AI agents.
- A task summary squeezed down to a lone ellipsis in a card's header turns
  into a clipboard icon that shows the whole task on hover. The close button
  is square and the usage text next to it is larger.

## 0.39.1

- A Codex card now shows the "?" when Codex asks for approval or asks a
  question, and stops pulsing as busy while it waits for your answer.

## 0.39.0

- Before a laned agent marks its work Task done, it merges the newest main
  into its lane and gets a GPT-6-Luna code review, fixing what the review
  finds. Lanes reach the integrator already up to date and reviewed.
- Agents already running keep the old instructions until they restart.
- After a restart, the newest reply of a reopened conversation gets its
  time stamp again, even when the reply was reprinted slowly or the card
  was cleared.

## 0.38.1

- Opening or closing the sidebar no longer lights every idle agent as
  working for two seconds.
- The README explains the lane chip's counts, untracked files and its
  hover tooltip.

## 0.38.0

- Tooltips wrap at one readable width instead of stretching into a thin
  strip across the window. A new Tooltips switch in Options turns them
  all off.
- Options has a Fullscreen switch that does the same as F11.
- F11 no longer opens a black console window when pressed right after
  AI Hive starts.
- In fullscreen, resting the cursor on the top or left edge brings the
  bars back in 0.75 s instead of 1.5 s, and the floating top bar leaves
  out the logo, name and version.
- A Claude reply no longer shows up several times over in a card after
  the card changes width.
- Close merged agents closes every merged agent in one click, not one
  per click.
- A finished lane's chip judges conflicts by the commit it flagged as
  done, so later unfinished work no longer turns a shippable lane red.
- The Claude model list says Haiku 5.5 instead of Haiku 4.5.

## 0.37.2

- The icons in a card's hover tray are now one size, the maximize
  button included, and the "⋯" that opens the tray is bigger.
- Make integrator is no longer offered while the workspace already has
  an integrator. Stop the current one first to hand the role on.
- The lane chip on a card shows a drawn branch icon instead of the small
  "⎇" glyph. The integrator's chip shows two lanes merging into one.
- The folder and trash beside the workspace path are line icons that
  match Lanes, Layout, Map and Activity, and the path is easier to read.

## 0.37.1

- The new terminal shortcut is now Ctrl+T instead of Ctrl+N, so Ctrl+N
  goes back to the program running in the terminal.
- The maximize and restore button on a card is now four plain arrows,
  a little larger than before.

## 0.37.0

- F11 goes fullscreen with only the agent grid on screen. Rest the
  pointer on the top edge for a moment to float the top bar and the
  workspace header over the grid, or on the left edge for the sidebar.
  Terminals never resize when they appear. F11 again puts the window
  back as it was.
- Ctrl+N opens a new terminal, also while a terminal has focus.
- Reply time stamps no longer vanish when a card is resized, retiled or
  rebuilt. They now work for Codex and Gemini agents, and a reopened
  Codex conversation gets its stamps back. Options > Reply times hides
  them.
- The card's maximize button draws corner arrows, pointing out to
  maximize and in to restore, in the theme's header colors.

## 0.36.1

- Restart now gives the new agent a blank screen. The new Claude used to
  draw its banner and input box over the old one's last frame.
- The card's maximize button uses the Windows caption icons: one square
  to maximize, two stacked squares to restore. They are larger than the
  old arrows, which looked alike.

## 0.36.0

- Close merged agents on the integrator's lane chip closes, after you
  confirm, every agent whose lane work is now on main and that holds
  nothing else: no commit ahead, no uncommitted file, not mid-turn. Their
  lanes go too. The integrator and every other agent stay open.
- The Ship finished lanes confirmation has a Don't ask again box. Options
  > Agents > Confirm Ship finished lanes turns the question back on.
- Stop is gone from a terminal card's hover tray. Ctrl+C in the terminal
  does the same. Line-console cards, which have no Ctrl+C, keep it. Restart on a running agent now shows a restarting veil until
  the new prompt is live, instead of a frozen old frame.
- A finished lane that shares a file with another lane keeps its green
  chip and gets a yellow border, instead of turning amber. Lane chips
  have squarer corners.
- The model badge no longer shows backticks or a cut-off name like
  "Sonnet 5" after a /model pick.
- The agent map's hub colors match the sidebar: green when idle, yellow
  while working.
- Show usage moved to the left column in Options, under the chime rows.

## 0.35.0

- Click the integrator's lane chip and pick Ship finished lanes to ask it
  to ship. The item counts the lanes with a Task done commit, shows you
  the list to confirm, then types the request into the integrator. It
  stays greyed out while the integrator works or no lane is finished.
- The test suite runs in parallel worker processes and finishes in about
  a minute and a half instead of six. It also stops leaving test folders
  behind in your Temp folder.
- Old plan and review documents are gone from the project folder.

## 0.34.1

- Removes the old task hand-off code that 0.34.0 left without a button.
  Nothing you can see changes: scheduled sends and the auto-continue
  after a plan limit still type into agents the same way.
- The collapsed sidebar rail and the workspace count badges now share
  one "working" pulse, so they breathe in step.

## 0.34.0

- Restart now gives you a fresh agent, as if you closed the card and
  opened a new one of the same kind. It drops a permission mode you
  switched into, a /model or /effort override, the AI title, the
  context badge and the assigned task. Name, folder, lane, font and
  the model, effort and permission mode you created it with stay.
- The Start and Assign buttons are gone from the card's hover tray.
  Restart or any keystroke starts a stopped agent.
- A collapsed sidebar now leaves a thin rail with one strip per
  workspace, colored like its count badge: amber while an agent
  works, green when idle, red on an error. Hover a strip for the name
  and counts, click it to open that workspace.
- New "Show usage left" switch under Options, Appearance. Usage pills
  then read "Claude 79% left" instead of "Claude 21% used", and the
  ring empties as you use up the window. Red still means 85% used.

## 0.33.0

- The integrator no longer pulls main into your project's own folder
  after it merges. That folder is your working copy, and a pull could
  change files under your editor or dev server. It tells you main moved
  instead, so you pull when you're ready.
- The integrator no longer bumps a version or edits a CHANGELOG in
  every project. It does both only when the project's
  docs/agents/integration.md asks for it, the way AI Hive's own does.

## 0.32.0

- You decide when finished work ships. AI Hive no longer types a note
  into the integrator each time a lane's agent commits "Task done".
  The lane chip still gets its ✓ and turns green. When you think
  there is enough for a pull request, ask the integrator to ship it,
  naming the lanes you want or none for everything marked done.
- The integrator starts only when you ask it to, never on its own.

## 0.31.0

- A lane's chip on its card turns green once its agent commits "Task
  done". Amber or red for files another lane also changed still wins
  over green.
- The workspace header's buttons are evenly spaced again. Change and
  Layout had a 20px gap between them while every other pair sat 6px
  apart.
- The header's Change… button is now called Change folder.

## 0.30.0

- Finished work now ships without any clicks. When a laned agent finishes
  its task, it commits with a last line of just "Task done". AI Hive sees
  that commit, puts a ✓ on the lane's chip and tells the integrator. The
  integrator combines every finished lane into one pull request, runs the
  tests, gets a GPT-6-Luna code review, fixes what it finds and reviews
  again until the review is clean. Then it merges and updates your main
  folder, so the next launch runs the new version.
- The integration queue is gone: no more Submit to integrator, Approve
  merge, Mark merged or the Activity panel's Integration section. Your
  integrator stays the integrator. Queue items saved by an older version
  are dropped when the session loads.
- A laned agent now commits each finished task on its own, without asking.
  If it ends a turn with uncommitted changes anyway, it gets one reminder to
  commit them.
- When a closed card's lane is kept only because of uncommitted files, the
  notice names up to three of them.

## 0.29.0

- The card actions that lived in the header's right-click menu (start, stop,
  restart, assign a task, scheduled send, Restart in own lane, Make
  integrator) are now buttons in the tray that opens when you hover the ⋯ in
  a card header, next to A- / A+ / maximize. Each has a tooltip naming it.
  The right-click menu is gone. A lane action AI Hive can't do right now
  shows disabled, and its tooltip says why.
- The hover tray closes when you move the pointer slowly off it, up or down.
  Before, it could stay stuck open on a display with fractional scaling.
- A second click on a dropdown's button now closes the dropdown. Before, it
  closed and opened again at once. This affects the repo activity arrow, the
  usage "+" picker, the chime rows' note button in Options, and the
  background-shell and lane chips.
- A click anywhere on a sidebar category row expands or collapses it, not
  only on the arrow. A double-click on the name still renames it.
- The Activity panel's "No integrator yet" hint points at the ⚑ button in
  the card's hover tray instead of the right-click menu that is gone.

## 0.28.5

- Closing an agent no longer says "Lane kept" with an unmerged commit when
  that work is already on main as a different commit. This happened when the
  integrator cherry-picked a lane into a batch, and after a squash or rebase
  merge. AI Hive now removes the lane once every change in it is on main. A
  lane with even one change main lacks is still kept.

## 0.28.4

- AI Hive pulls what merged on GitHub every time it starts, so a restart
  always runs the latest approved changes. Before, nothing moved your folder
  after a merge, and a folder left on an old branch never updated at all.
  It still never touches local changes or commits that are not on GitHub.
- The integrator now gets a second code review from GPT-6-Luna (through
  your Codex CLI) before it opens a pull request, and lists each finding
  and what it did about it in the PR.

## 0.28.3

- No change to the app itself. The release test that drives a real Claude
  Code agent works again with Claude Code 2.1.290, which changed how its
  "do you trust this folder" question marks the selected answer.

## 0.28.2

- The A- / A+ / maximize buttons that appear when you hover the ⋯ in a card
  header now touch each other and the tray edge, with no dead gaps, and fill
  the full header height.
- Moving the pointer above or below those buttons now hides them, instead of
  leaving them open until you leave sideways.
- Layout, Map, Activity and Lanes in the workspace header have line icons
  instead of font symbols, which some systems drew as colour emoji.
- Open folder and Change... look like the other header buttons, and the
  Lanes toggle sits before them.
- On a window too narrow for the terminals plus the Activity panel, the panel
  now slides over the terminals instead of pushing the window wider than the
  screen.
- The model dropdown labels Sonnet as 5.5.

## 0.28.1

- The ⚙ badge in a card header (a background command is still running) is
  now as narrow as the gear itself, instead of a wide pill that pushed the
  task summary aside.
- The workspace header's delete button is now a 🗑 trash can, and it turns
  red on hover like the other delete buttons.
- Hovering Open repo now outlines the whole button, right edge included.
- The Open repo ▾ list of recent pull requests and commits loads in the
  background a few seconds after AI Hive starts, so it opens with the list
  already there. Opening it refreshes the list in place when it is more than
  3 minutes old, and a refresh that fails keeps the list you had.

## 0.28.0

- Lanes can now be turned on per workspace. Each git workspace's header has
  a ⎇ Lanes toggle (off for new workspaces): on, the New Agent dialog ticks
  Own lane for that workspace's new Claude agents.
- The Options switch is now "Lanes in every workspace". On, it turns every
  workspace's toggle on and locks it. Off, each workspace decides.
- The New Agent dialog offers Own lane in every git workspace, so one agent
  can have a lane while the rest of the workspace shares the folder.
- An existing agent can move into a lane with Restart in own lane in its
  card's right-click menu. It starts a new conversation in its own worktree,
  and the old conversation stays in the New Agent picker.
- Turning lanes off no longer pauses anything. A lane keeps its warnings,
  its chip and its way to the integrator until you close its card, and a
  pull request awaiting approval can be merged even after its lane is gone.
- New "What are lanes?" explainer, from the ? beside the header toggle and
  from the New Agent dialog, and a one-time hint when a workspace gets two
  lanes without an integrator.
- Fixed: right after a restart, an agent could briefly be pinned back to its
  previous conversation.

## 0.27.1

- Closing a laned agent now keeps its lane when the agent deleted one of the
  local files that `.worktreeinclude` copied in (a `.env`), the same as when
  it edited one. Before, the lane was removed and the deletion with it.
- When AI Hive can't finish checking a lane's local files, it now keeps the
  lane and says so, instead of taking the lane for clean and removing it.

## 0.27.0

- Agent lanes get an integration queue. Right-click a laned Claude agent's
  header and pick Make integrator, then choose Submit to integrator on
  another lane's chip. One lane at a time, AI Hive sends the integrator a
  short brief: it merges the newest base branch into that lane's work, bumps
  the version, runs the tests on exactly that commit and opens a pull request
  that names it. It never merges.
- The Activity panel's new Integration section lists every submitted lane.
  Approve merge there is the only way a pull request gets merged, and AI Hive
  does it itself: it refuses if the pull request changed since it was tested
  or the base branch can't be confirmed with GitHub, and sends it back to the
  integrator if the base branch moved since then. Anything unexpected waits
  for you with the reason and the "?" chime, and is never approved again by
  itself.
- A pull request merged on GitHub instead of with Approve merge waits for you:
  Mark merged checks with GitHub, then moves the queue on. A lane whose work
  was squash-merged can be removed from its "lane kept" notice.
- A lane with no work of its own now catches up with the base branch by
  itself while its agent is idle, and the agent is told.
- The integration queue needs a GitHub remote and a logged-in GitHub CLI
  (gh). It pauses while Agent lanes is off and survives a restart.

## 0.26.1

- A task given to a Claude agent as it starts is now always submitted. In a
  fresh git folder (every new agent lane is one) Claude could take the task's
  text and its Enter as one paste, so the task sat typed in the input box and
  never ran. AI Hive now presses Enter only once Claude shows the text.

## 0.26.0

- Agent lanes now keep agents aware of each other without reading the whole
  board. Each laned agent's card shows its branch, commits ahead and
  uncommitted files, and turns amber when another agent's lane (or the base
  branch) changed one of the same files, red when the two would conflict.
- A laned agent is warned the moment it edits a file another lane changed,
  or edits outside its own lane, and is told on its next prompt when the base
  branch changed a file it changed. Each warning comes once per round of
  work. Laned agents read a small roster file instead of the whole board.
- The board roster shows each agent's lane, how far ahead it is and which
  files it is touching. The Activity panel lists lanes and their changed
  files, the Agent/File Map shows one tree for the whole repository, and new
  overlaps and conflicts appear in the event log.
- The lane chip's menu opens the lane folder, and updates a lane with no work
  of its own to the newest base branch.
- A `.worktreeinclude` file in the repository names ignored local files (like
  a `.env`) to copy into every new lane. A lane whose copy you edited is kept
  when its agent closes, not deleted with it.
- Turning Agent lanes off now always reaches agents that are running, even
  when one of them is reading the lane file at that moment.

## 0.25.0

- New in Options: Agent lanes, off by default. Turned on, every new Claude
  agent in a git workspace works in its own copy of the repository (a git
  worktree on its own branch, next to your project folder), so agents can no
  longer overwrite, reset or accidentally commit each other's changes. Untick
  "Own lane" in the New Agent dialog to keep an agent in the workspace folder.
- Closing a laned agent cleans its lane up only when nothing in it is
  unsaved or unmerged, and only once no program is still running in it (AI
  Hive tries again for a minute). Otherwise the lane is kept and AI Hive tells
  you what is in it; its conversation stays in the New Agent dialog's
  Conversation list, and picking it brings the lane back where it was.
- A workspace inside a larger repository starts with "Own lane" unticked,
  since its lane would copy the whole repository. A workspace folder that is
  not part of the repository's files gets no lane at all.
- Turning Agent lanes off stops new lanes. Agents that already have one keep
  working in it.

## 0.24.0

- The New Agent dialog has a Count stepper: pick Claude Code (or any type),
  press + to choose how many, and that many agents open at once, numbered on
  from the name you gave. It starts at 1 and can't go past the workspace's
  free slots. Resuming a past conversation keeps it at 1.
- New Workspace can start from a GitHub repository: paste its URL and AI Hive
  clones its default branch (main, master or any other name) into a new folder
  and opens a workspace there.

## 0.23.2

- The shared board now keeps only its newest 40 activity notes. Older ones
  move to board-archive.md next to it, so agents spend fewer tokens reading
  the board before each task. Nothing is deleted.
- The board's agent list shows what each agent is working on (its
  conversation title) instead of "-" when it has no assigned task.

## 0.23.1

- Terminals restored after a reopen keep the width they started at, also on a
  small or heavily scaled screen. Before, a resumed conversation there could
  be wrapped narrower than its card.

## 0.23.0

- Codex cards now resume their own conversation when you reopen AI Hive, and
  show the model, reasoning level and context-window usage live in the header.
- A card's summary is now the conversation title you see in the CLI's resume
  list. A task assigned in AI Hive shows only until that title exists.
- A Codex card's summary stays on the conversation's opening prompt instead of
  jumping to a later message once the conversation gets long.

## 0.22.6

- When Claude Code continues on its own after a usage limit resets, the
  hourglass no longer comes back and stays for a day. Any leftover hourglass
  also clears within a minute once the conversation shows new work.
- An agent stopped by a usage limit is now resumed even if a background task
  hit the same limit again later. Before, AI Hive could decide there was
  nothing to resume and leave it idle.

## 0.22.5

- A reply's date and time stamp now shows when Claude actually finished the
  reply. It used to jump to whenever the terminal was next redrawn, for
  example when you switched to its workspace hours later.
- Reply stamps no longer go missing in terminals where a Stop hook prints a
  line under every reply (such as a hook that echoes the time).

## 0.22.4

- The update button beside the version is a cleaner refresh icon.
- "Up to date" now shows for 4 seconds and then the button returns on its
  own. While it shows it is just a message, not something to click.

## 0.22.3

- Internal: the test suite is split into one file per area of the app, and a
  test can no longer be left out of the run by mistake.

## 0.22.2

- The model list in the New Terminal dialog is current: Fable 5.1, Opus 5.5,
  Sonnet 5 and Haiku 4.5, plus the 1M-context versions and Opus Plan (Opus
  plans, Sonnet executes).

## 0.22.1

- Conversations are found when Claude Code keeps its data somewhere other
  than your user folder (the CLAUDE_CONFIG_DIR setting).
- Fixed an error that could be logged when a label was removed right after
  its text changed.
- Internal: the test suite runs in a throwaway profile, can never start a
  real Claude in your project folders, and finishes in about a third of the
  time. Unused code for spawning agents automatically was removed.

## 0.22.0

- Update AI Hive from inside the app. The button beside the version badge
  checks GitHub, shows what changed, and updates this folder when you click
  Update. You restart AI Hive yourself when your agents are at a good point.
- This changelog.

## 0.21.1

- Internal: shorter project notes for the agents working on AI Hive. No
  change to the app.

## 0.21.0

- Event log window (Log button or Ctrl+Shift+L): what every agent in every
  workspace did, with unanswered questions highlighted.
- Usage pills share one poller: faster while agents work, slower when idle,
  and a pill only greys out when its reading is really stale.
- Pick your own WAV or MP3 for each chime in Options, preview it, or go back
  to the built-in sound.
- A second click on Options now closes the panel.

## 0.19.7

- New question chime, and an optional chime when a reply finishes.
- Usage pills: per-agent colours, red from 85%, Claude and GPT labels, and a
  new Codex / ChatGPT usage pill.
- Gemini agents get the full card header: model, effort, mode, summary and
  context.
- Usage-limit cut-offs are caught more reliably, and the reset time is read
  correctly across time zones and daylight saving.
- Card font and maximize buttons collapse into a hover tray.
- Right-click menu on links, and the input box no longer gets stranded after
  a menu closes.
- Reveal in folder opens the file's own folder.
