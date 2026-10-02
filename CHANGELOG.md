# Changelog

What changed in each AI Hive version, newest first. The app shows these
sections in its update dialog, so write them for the person using AI Hive:
one line per change they would notice.

Every PR merged to `main` bumps `__version__` in `app/__init__.py` and adds a
`## x.y.z` section here with the same number (a smoke check enforces it). No
em dashes: this text is shown in the app.

## 0.24.0

- The New Agent dialog has a Count stepper: pick Claude Code (or any type),
  press + to choose how many, and that many agents open at once, numbered on
  from the name you gave. It starts at 1 and can't go past the workspace's
  free slots. Resuming a past conversation keeps it at 1.
- New Workspace can start from a GitHub repository: paste its URL and AI Hive
  clones its default branch (main, master or any other name) into a new folder
  and opens a workspace there.

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
