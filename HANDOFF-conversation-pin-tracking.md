# HANDOFF: Make conversation pin-tracking airtight

**Status:** partial fix shipped; one real gap remains. This document is a
brief for an ultracode agent to close it "once and for all."

> Author's honesty note: the previous work (mine) over-claimed success three
> times. Treat every "VERIFIED" below as something YOU should re-confirm, and
> treat every "UNVERIFIED" as an open question to settle EMPIRICALLY against
> the real Claude CLI before writing code. Do not repeat the mistake of
> reasoning your way to "this works" — test it. (This doc was itself re-audited
> against the code, git history, and `session.log` by independent agents; the
> log-line narrative in §4c/§10 and the hook assumptions in §6/§7 were
> corrected as a result — see the notes inline.)

---

## 1. The product goal (what the user actually wants)

> "I just want the conversations that are open on app start-up to be the exact
> same ones, on each workspace, as when I closed the app. Nothing hardcoded."

So: **faithful, dynamic restore of the live conversation on every card, in
every workspace, across close/reopen — including if the user switched a
card's conversation from inside the terminal.**

## 2. System context

- **App:** AI Hive — Windows-first PySide6 desktop app that runs many terminal
  agents (incl. interactive **Claude Code**) tiled across workspaces. Repo:
  `C:/Users/Mario/ai-hive`. Read `CLAUDE.md` (invariants) and `README.md`.
- **How a Claude conversation is pinned:** each Claude agent (`AgentSpec`)
  carries a `session_id`. Fresh launch → `--session-id <uuid>`; restore →
  `--resume <uuid>` (NEVER `--continue` for a pinned agent). See
  `AgentSpec.effective_args` in `app/process_worker.py`.
- **Where Claude stores a conversation:** exactly one file
  `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. Encoding: every
  non-alphanumeric char of the abs cwd → `-` (`transcripts.encode_project_dir`).
- **Session persistence:** `session.json` in
  `C:/Users/Mario/AppData/Roaming/AIHive/AI Hive/`; audited in `session.log`
  (SAVE / SESSION-SYNC / SAVE-FAIL lines). NEVER hand-edit it while the app is
  running (it gets clobbered on the next save) and NEVER with PowerShell
  `Set-Content` (writes a BOM).
- **The workspace that kept breaking:** "HiraKataQuest", folder
  `C:/Users/Mario/Downloads/HiraKataQuest-` (encoded
  `e8c214f54d624221a0d3289bb852776f`), which has **two** Claude agents sharing
  one folder. Every incident happened here — the multi-agent case is the hard
  case.

## 3. The core difficulty (why this is not trivial)

AI Hive only knows the session id it put on the command line. The child's
**live** conversation id can drift out from under that pin:
- the user runs `/resume` **inside the Claude TUI** and switches conversations
  (Claude then appends to a *different* `<id>.jsonl`);
- a conversation forks (usage-limit recovery, `--fork-session`);
- a fresh id is minted that never gets a transcript.

AI Hive cannot directly observe the child's current session id. That is the
whole problem.

## 4. What was tried, what worked, what failed

### 4a. Filesystem drift-tracking (commit `1d9896d`) — PARTIALLY WORKS
New module `app/session_sync.py`, plus wiring:
- `WorkspaceManager.sync_live_sessions()` — reconciles each running agent's pin
  to the transcript it's actually writing, correlated by file mtime vs. the
  agent's process-start time (`TerminalAgent._session_started`). Run on a 5 s
  timer (`MainWindow._sync_live_sessions`, `SESSION_SYNC_MS`) and once in
  `closeEvent` before the final save. Emits `dirty` only on a real change.
- `TerminalAgent._recover_missing_resume_target()` — before `--resume`, verify
  the pinned transcript exists; if not, resume the folder's most recent real
  conversation. Gated by one-shot `_verify_resume_target` (set only on RESTORE
  in `main.py`), so a plain unit-test `start()` / manual restart never touches
  the filesystem. Excludes sibling agents' pins (`sibling_session_ids`).

**VERIFIED WORKING:** the missing-pin recovery. It correctly recovered a real
9 MB conversation (`d98a6af0`) when the pin pointed at an id that never had a
transcript.

> Log-quote correction: an earlier draft named the missing precursor id as
> `4ac5d4b0`. That string does NOT appear anywhere in `session.log`. The actual
> recovery line into `d98a6af0` is, verbatim:
> ```
> 2026-07-07 00:08:42 pid=... SESSION-SYNC agent=e5dc621d25a5429c9e7a37976cb5178f bad54ed3-0f4b-4a1c-a519-179773473309 -> d98a6af0-3132-44bf-9753-2dbacd8fbed5
> ```
> i.e. the precursor was `bad54ed3-0f4b-4a1c-a519-179773473309`, not
> `4ac5d4b0`. The recovery-into-`d98a6af0` behavior is real; the id was
> misremembered. Re-confirm from the log before quoting.

### 4b. Stub guard (part of the session_sync work) — WORKS
Symptom: the sync followed a card onto a brand-new **empty** `/recap` session
(`5e1a00d5`, ~2 KB) and abandoned the real 10 MB conversation (`eb24cc7f`).
Fix: `_STUB_BYTES = 4096` — never let a pin drift from a real conversation onto
a near-empty stub; recovery also passes over stubs unless only stubs exist.
**VERIFIED** by test and by the current good state.

### 4c. Multi-agent mtime correlation — FAILED, now DISABLED (commit `1bf3719`)
Symptom (happened in the 2-agent HiraKataQuest folder): the sync moved pins onto
the wrong transcripts, and a real conversation was left un-pinned onto an empty
`/recap` stub while its real chat sat elsewhere.

The log lines that were cited as "proof of a swap" are real and verbatim, but
they do NOT show a single agent's pin bouncing between two conversations. Each
`SESSION-SYNC` line carries a distinct `agent=<id>` field, and the two most
often quoted lines belong to **two different agents** reconciling independently:
```
2026-07-06 23:30:58 pid=8744 SESSION-SYNC agent=f41c50acdd6f4ec688b6298c41bcbb61 5e1a00d5-564d-4460-8afd-32b778152ed1 -> eb24cc7f-4279-4e11-93d4-0e21203b0b46
2026-07-06 23:31:03 pid=8744 SESSION-SYNC agent=78b36e75ee4c46348c3fe6f4ad0d2303 d98a6af0-3132-44bf-9753-2dbacd8fbed5 -> 5e1a00d5-564d-4460-8afd-32b778152ed1
```
Note the different `agent=` prefixes. Across the log, ids `5e1a00d5` and
`eb24cc7f` each get pinned by **three distinct agent ids** over a >2.5-hour span
(e.g. `eb24cc7f -> 5e1a00d5` at 20:48:11 by agent `bbd831a78db54556a211f1f91200be0c`).
That is exactly the "the filesystem cannot say which agent owns which
transcript" ambiguity — but the honest reading is: mtime correlation moved
different agents onto the same/overlapping transcripts, not a clean two-agent
"swap." The corruption was real; the tidy "A↔B swap" story was not what the log
shows. Do not re-quote these lines as a same-agent swap.

Root cause: a `--resume` touches ALL of a folder's transcripts at launch, so
file mtime cannot say WHICH of two same-folder agents owns WHICH transcript.
mtime correlation therefore GUESSES, and wrong guesses move pins onto the wrong
conversations.

Fix shipped: `resolve_live_ids()` now tracks **ONLY single-agent folders**
(`session_sync.py:211-212` groups by encoded cwd and skips any group whose
length != 1). Multi-agent folders are left exactly as launched/restored — never
auto-reshuffled. Missing-pin recovery (4a) still runs for genuinely broken
pins. This STOPPED the corruption. See the "CRITICAL" note in `CLAUDE.md` under
the "The pin must TRACK the live conversation" invariant: **do not re-add
mtime-based reassignment for multi-agent folders** — the filesystem lacks the
signal.

## 5. The remaining gap (what still needs fixing)

**In a MULTI-agent folder, an in-terminal conversation switch is not
remembered.** If the user `/resume`s a card to a different conversation from
inside the Claude TUI (or the conversation forks), that switch is not captured;
on reopen the card returns to its launch/last-set pin, not the switched-to
conversation.

- Single-agent folders: fully tracked (safe, unambiguous). No gap.
- Multi-agent folders, if each card stays on its own conversation: fine.
- Multi-agent folders + in-terminal switch/fork: **NOT tracked.** ← the gap.

This gap is the direct, deliberate cost of disabling the unreliable mtime
correlation. Closing it requires a signal the filesystem cannot provide.

## 6. Proposed fix (hook-based — research done, live confirmation still required)

Stop guessing; have the child Claude **report its real current session id** to
AI Hive authoritatively, via a per-agent **`SessionStart` hook**:
- AI Hive sets a unique per-agent env var (e.g. `AIHIVE_AGENT_ID`) when
  launching each agent (see `pty_worker.agent_environment`, which already
  sanitizes the env).
- AI Hive injects a `SessionStart` hook (per-agent settings file passed to the
  CLI) whose command reads the hook JSON on stdin and appends
  `{AIHIVE_AGENT_ID, session_id, transcript_path, cwd, source, ts}` to a
  known mapping file under the session dir.
- AI Hive reads that mapping on the existing timer + in `closeEvent` and updates
  `spec.session_id` for the matching agent (emit `dirty` only on change). This
  removes ALL filesystem guessing and works for any number of agents per folder.

### THE make-or-break unknown — what the docs say, and what you must still confirm
**Does `SessionStart` fire on an in-TUI `/resume` (a conversation switch), not
just at process launch — and does it carry the NEW session id?**

Documentation research (Claude Code hook docs, HIGH confidence from the docs
themselves) says **YES**:
- The [SessionStart hook docs](https://code.claude.com/docs/en/hooks.md) list
  four `source` values: `startup`, `resume`, `clear`, `compact`, and the docs
  explicitly map `resume` to "`--resume`, `--continue`, or `/resume`" — i.e.
  an in-TUI `/resume` fires SessionStart.
- The payload carries `session_id`, `transcript_path`, `cwd`,
  `hook_event_name`, `source`, `model` (and optionally `agent_type`,
  `session_title`). On a switch, `session_id`/`transcript_path` reflect the
  conversation now in effect.
- A fork (`/branch`) prints a NEW session id and a `/clear` emits
  `source: "clear"` with a fresh id, so both should surface as new-id
  SessionStart events.

**So the hook path is promising and worth building — but the docs being right
is NOT the same as it working in AI Hive's exact launch configuration.** Do a
live confirmation FIRST (see §7 step 3) before writing feature code. Two things
the docs do NOT settle and that have burned this project before:
- Whether AI Hive's specific launch (interactive PTY, sanitized env, MCP config,
  `--append-system-prompt`) actually delivers the `resume` event on a `/resume`
  performed *inside an already-running* session — as opposed to on process
  launch. Docs describe the feature; only a probe proves it in-situ.
- Whether hooks can be injected per-invocation via a settings file WITHOUT
  editing the user's global `~/.claude/settings.json` (see unknown #1 below) —
  the docs confirm a `--settings <file>` flag exists but are **SILENT on
  whether `--settings` can carry a `hooks` section**. This must be tested
  empirically.

If the live probe shows it fires with the new id → build the hook path.
If it does NOT → the hook does not fix the user's case; document the true
ceiling and evaluate fallbacks (§8).

### Other unknowns to confirm against Claude CLI (verified 2.1.197 elsewhere)
1. Exact hooks config schema and whether a per-invocation settings file can
   inject hooks WITHOUT touching the user's global `~/.claude/settings.json`.
   A `--settings <file>` flag exists (per CLI-reference docs), but the docs do
   NOT state whether a `hooks` section inside that file is honored — **test
   this directly** (`claude --help` + a probe).
2. `SessionStart` payload fields actually delivered in AI Hive's launch
   (docs say `session_id`, `transcript_path`, `cwd`, `hook_event_name`,
   `source ∈ {startup,resume,clear,compact}`, `model`; confirm live).
3. Whether a fork (usage-limit / `--fork-session`) and `/clear` also emit an
   event with the new id (docs imply yes; confirm live).
4. Per-agent attribution: two agents in one folder each fire their own hook and
   the `AIHIVE_AGENT_ID` correctly distinguishes them.
5. Env var visibility to the hook process, and that adding a hook does not
   break the existing MCP config, `--append-system-prompt`, `--add-dir`, and
   the transcript-persistence behavior (nested-session env markers must stay
   stripped — see the `agent_environment` invariant).

Note: there is NO documented CLI flag that prints the current session id at
runtime (per the CLI reference). The hook payload (or reading the transcript
filename on disk) is the only way to learn the live id — which is exactly why
the hook approach is the proposal.

## 7. Concrete verification plan (do this before writing feature code)

1. Read `claude --help` and current hook docs; confirm the config format and a
   per-invocation injection path (in particular: does `--settings <file>` honor
   a `hooks` section, or must hooks live in `~/.claude/settings.json`?).
2. Minimal probe: launch a real `claude` in a scratch folder with a
   `SessionStart` hook that logs the payload + `AIHIVE_AGENT_ID` to a file.
   Confirm it fires at startup with the right session_id.
3. **CRITICAL PROBE — DO THIS FIRST, before any feature code.** The docs say
   `/resume` fires SessionStart with the new id, but that must be proven in
   AI Hive's actual launch config. Inside a running session run `/resume` and
   pick a different conversation. Check whether the hook fires again with the
   NEW session id and NEW transcript_path. Repeat for a fork and `/clear`. If
   this fails, the entire hook approach is off the table — go to §8. Everything
   else in this plan is downstream of this result.
4. Two-agent probe: two claude processes in ONE folder, distinct
   `AIHIVE_AGENT_ID`s; confirm each hook line is attributable.
5. Only then wire it into AI Hive; keep single-agent tracking (§4a) and
   missing-pin recovery as-is; the hook augments/replaces the multi-agent path.

## 8. Fallbacks if the hook does NOT fire on in-TUI /resume

- Accept and clearly document the gap; make it easy to set a card's
  conversation via the New-Agent **Conversation** dropdown (already exists —
  launches `--resume <id>` and pins correctly).
- Content-correlation as a LAST resort (match a prompt AI Hive delivered to a
  transcript to attribute it) — fragile; only with heavy safeguards and never
  in a way that can swap/orphan (the failure mode of §4c).
- Do NOT re-introduce mtime reassignment for multi-agent folders.

## 9. Invariants you MUST respect (from CLAUDE.md)

- Model owns processes; never parent an agent to a widget.
- Never `QProcess.terminate()`; stop = stdin EOF, kill = Windows Job Object.
- Persistence is sacred: structural changes save immediately; persisted-field
  changes emit `dirty`; TRANSIENT signals (busy/standby, output activity) must
  NEVER mark `dirty` (would thrash `session.json`). A save must never fail
  silently — audit SAVE / SAVE-FAIL / SAVE-SKIP.
- `--resume <id>`, never `--continue`, for a pinned agent.
- Agents launch with a SANITIZED env (`CLAUDECODE`/`CLAUDE_CODE_*` stripped) or
  transcript persistence silently breaks.
- Do NOT re-add mtime-based reassignment for multi-agent folders.
- Never hand-edit `session.json` while the app runs; copy-aside + re-read +
  atomic if you ever must, app closed.

## 10. Current state (VERIFIED at handoff time)

- Branch `session-live-conversation-tracking`, commits `1d9896d` (v1) and
  `1bf3719` (single-agent-only fix). Both confirmed present with the exact
  messages: `1d9896d` "Live session tracking + recovery, Agent/File Map,
  terminal shortcuts"; `1bf3719` "Session sync: only track single-agent folders
  (never reshuffle peers)".
- HiraKataQuest (`e8c214f54d624221a0d3289bb852776f`) pins are correct right now,
  confirmed in `session.json`:
  - Agent 3: `session_id = eb24cc7f-4279-4e11-93d4-0e21203b0b46` (~10 MB).
  - Agent 4: `session_id = d98a6af0-3132-44bf-9753-2dbacd8fbed5` (~9.2 MB, the
    "audio-icon" conversation).
  No stub pinned.
- All orphaned transcripts remain safe on disk; only pins were ever at risk.

> Verification caveat carried over from §4c: `session.log` shows these two ids
> being (re)pinned by SEVERAL distinct `agent=` ids over the session's history.
> The current `session.json` state above is the ground truth for what is pinned
> now; do not infer a clean per-agent history from the log's SESSION-SYNC lines,
> which reflect multiple agents converging on overlapping transcripts.

## 11. Key files

- `app/session_sync.py` — reconciliation + recovery (the heart).
  `_STUB_BYTES = 4096` (line 47), `_START_SLACK = 3.0` (line 36), single-agent
  guard at lines 211-212, `best_recovery_id` stub-skip at lines 159-174.
- `app/terminal_agent.py` — `start()`, `_recover_missing_resume_target`
  (lines ~175-177 build the sibling+self `exclude`), `_verify_resume_target`
  (one-shot gate at lines 151-159), `_session_started`, `_sibling_sessions`.
- `app/workspace_manager.py` — `sync_live_sessions` (dirty only on real change,
  lines ~384-391), `sibling_session_ids` (lines ~352-363), `_wire_agent` (sets
  the sibling resolver at line ~339).
- `app/widgets/main_window.py` — `SESSION_SYNC_MS = 5000` (line 40),
  `_sync_live_sessions` (guarded by `_closing`/`_ready`, lines ~1083-1084),
  `_session_sync_timer` (lines ~442-444), `closeEvent` sync-before-save
  (lines ~1092-1102).
- `main.py` — restore loop sets `resume` + `_verify_resume_target = True`
  (line ~135, inside the `provider in RESUME_PROVIDERS` branch).
- `app/pty_worker.py` — `agent_environment` (where a per-agent env marker goes).
- `app/process_worker.py` — `AgentSpec.effective_args` (where a `--settings`/
  hook flag would go).
- `app/transcripts.py` — `encode_project_dir`, `transcript_path`.
- `tests/smoke_test.py` — `test_session_recovery` (the regression suite).

## 12. How to test

```
.venv\Scripts\python.exe tests\smoke_test.py
```
All must pass. Add a new regression check for whatever you fix.

(Historical note: this section used to wave off `pty: Ctrl+C stopped the loop`
as a pre-existing timing/locale flake. It was not flaky — it was reporting a
real bug, that Ctrl+C did nothing to a running command in a pty card. Both the
bug and the check are fixed; see the ignore-Ctrl+C invariant in `CLAUDE.md`.)
