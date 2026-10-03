# Lanes for Codex, Gemini and Grok

Status: needs-triage

Lanes are Claude only (spec-v3 "Decisions"). v2's Decisions said Codex,
Gemini and Grok would get lanes in Phase 2; Phase 2 shipped without them,
because each needs its own Step 0 run first. This issue holds that work.

## What each provider needs before it can get a lane

- **Step 0 check 1, per provider:** resuming a conversation from a lane folder
  works, and AI Hive's transcript readers find it there (they look a
  conversation up by the agent's cwd). Measure the first-launch prompts a
  fresh lane folder causes.
- **The board, by absolute path:** Gemini learns about the board from project
  context files today, and Codex gets no board at all. A laned agent must
  reach `ws.board.dir` by absolute path, never a copy inside the lane.
- **A revive path:** `lanes.lane_conversations` reads Claude transcripts only.
  Each provider needs its own way to list a retired lane's conversations and
  resume one at the identical path.
- **Hooks, or an honest fallback:** the overlap hook and lane notices are
  Claude hooks. A provider without an equivalent keeps the full "read the
  board" instruction instead of `roster.md`.

## Done when

- The New Agent dialog offers "Own lane" for the provider, with the same
  defaults as Claude.
- `tests/smoke/lanes.py` covers create, revive and the prompt for it, with
  stubbed workers.
- One live run per provider is recorded in its PR.
