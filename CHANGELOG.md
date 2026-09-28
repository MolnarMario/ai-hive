# Changelog

What changed in each AI Hive version, newest first. The app shows these
sections in its update dialog, so write them for the person using AI Hive:
one line per change they would notice.

Every PR merged to `main` bumps `__version__` in `app/__init__.py` and adds a
`## x.y.z` section here with the same number (a smoke check enforces it). No
em dashes: this text is shown in the app.

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
