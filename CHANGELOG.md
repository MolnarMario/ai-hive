# Changelog

What changed in each AI Hive version, newest first. The app shows these
sections in its update dialog, so write them for the person using AI Hive:
one line per change they would notice.

Every PR merged to `main` bumps `__version__` in `app/__init__.py` and adds a
`## x.y.z` section here with the same number (a smoke check enforces it). No
em dashes: this text is shown in the app.

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
