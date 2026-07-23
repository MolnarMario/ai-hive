"""Authoritative Claude-hook reporting: live conversation ids AND "needs user".

This one stdlib-only script backs two shared-`--settings` hook families that AI
Hive injects into every Claude agent. The original purpose (below) is live-
conversation tracking via `SessionStart`. It ALSO carries the authoritative
"the agent is waiting for the user" signal that drives the sidebar "?" and the
notification chime: PreToolUse on AskUserQuestion/ExitPlanMode (the only
reliable way to catch those — they render no numbered menu the screen scrape
can see and fire no Notification hook, per claude-code#59908), PostToolUse to
clear it, and Stop to close a lingering prompt and detect a plain free-text
question the agent ended its turn on. These prompt events are EDGES appended to
a separate events file and read incrementally by the manager; SessionStart
records stay in the mapping file. See write_settings_file / read_prompt_events.


AI Hive pins each Claude agent to a conversation with `--session-id`/`--resume`,
but the LIVE conversation can drift out from under that pin: the user runs
`/resume` INSIDE the TUI and switches conversations, a `/clear` starts a new one,
or a session forks. AI Hive never sees the child's real current id — and in a
folder with two-plus agents the filesystem CANNOT say which agent owns which
transcript (a resume touches them all), so mtime correlation can only guess and
a wrong guess swaps or strands live conversations (a real, repeated incident).

The fix is to stop guessing and have the child report the truth. Claude fires a
`SessionStart` hook on launch AND on every in-TUI conversation switch
(`source` in {startup, resume, clear, compact}), and the payload carries the id
and transcript_path now in effect (verified live against Claude 2.1.197 in
AI Hive's exact launch config: interactive PTY, sanitized env, `--settings`
injection). AI Hive:

  * writes ONE shared settings file with this SessionStart hook and passes it to
    every Claude agent via `--settings` (verified: `--settings` honors a `hooks`
    section, and injected hooks are ADDITIVE with the user's own — they are
    never clobbered);
  * sets a per-agent `AIHIVE_AGENT_ID` env var (the TerminalAgent.id) so each
    hook line is attributable to exactly one agent — the signal the filesystem
    could not give, which is what makes multi-agent folders safe;
  * reads the resulting mapping file on the session-sync timer and in
    `closeEvent` (before the final save) to reconcile each agent's pin to its
    real live conversation.

This module is deliberately Qt-free and stdlib-only: when Claude runs the hook
it executes `python session_hook.py <mapping_path>`, and that process must start
instantly with no heavy imports. The same module provides the writer/reader
helpers AI Hive uses, so the record format lives in ONE place.
"""

import json
import os
import sys
import time

# env var carrying the AI Hive agent id (== TerminalAgent.id) into the hook
AGENT_ID_ENV = "AIHIVE_AGENT_ID"

# The interactive tools that pause a turn awaiting the user but render NO
# numbered+caret menu the screen scrape can detect — and (confirmed against
# claude-code#59908) fire NO Notification hook either. A PreToolUse hook on
# these tool names is the ONLY reliable "the agent is now asking you" signal.
WAITING_TOOLS = "AskUserQuestion|ExitPlanMode"
_WAITING_TOOL_SET = set(WAITING_TOOLS.split("|"))


def _hook_command(mapping_path: str, events_path: str | None = None,
                  python_exe: str | None = None) -> str:
    """The shell command Claude runs for our hooks: this script, told where to
    append. argv[1] is the SessionStart mapping file; argv[2] (optional) is the
    prompt-events file the PreToolUse/PostToolUse/Stop hooks append to. The
    single script dispatches on the event name in the stdin payload, so one
    command string serves every hook. Every path is quoted so spaces in the
    venv/app/session paths (the common case on Windows) survive the shell
    split."""
    py = python_exe or sys.executable
    script = os.path.abspath(__file__)
    cmd = f'"{py}" "{script}" "{mapping_path}"'
    if events_path is not None:
        cmd += f' "{events_path}"'
    return cmd


def write_settings_file(settings_path: str, mapping_path: str,
                        events_path: str | None = None,
                        python_exe: str | None = None) -> None:
    """Write the shared `--settings` file carrying ONLY our hooks.

    Only the `hooks` key is present, so passing it to `--settings` adds our
    hooks without disturbing any other setting; and because Claude merges hooks
    across sources, the user's own hooks still fire too (verified). Best-effort.

    Two families of hooks, all pointing at this one script:
      * SessionStart -> live-conversation tracking (the original purpose).
      * PreToolUse/PostToolUse/Stop -> the "agent needs the user" signal that
        drives the sidebar "?" and the notification chime. These fire mid-
        session (never at launch, never on prompt-submit), so they don't touch
        the timing-sensitive first-task-submit path the SessionStart `startup`
        matcher famously broke.
    """
    cmd = _hook_command(mapping_path, events_path, python_exe)
    hooks: dict = {
        # Match only the IN-SESSION conversation switches (resume/clear/
        # compact), NOT `startup`. At launch the live id already equals the
        # id AI Hive put on the command line, so a startup firing adds no
        # information -- and firing during startup perturbs the TUI's settle
        # just enough that the first task-submit Enter is dropped and the
        # task never runs (verified live: with `startup` included, a freshly
        # spawned agent silently ignored its first task). Excluding startup
        # keeps exactly the signal we need and leaves launch timing pristine.
        "SessionStart": [
            {"matcher": "resume|clear|compact",
             "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
        ]
    }
    if events_path is not None:
        hooks.update({
            # the instant the agent opens an interactive question/plan prompt
            "PreToolUse": [
                {"matcher": WAITING_TOOLS,
                 "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
            # the user answered it -> the prompt is closed
            "PostToolUse": [
                {"matcher": WAITING_TOOLS,
                 "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
            # turn ended: closes any lingering tool prompt AND lights up if the
            # agent's last message was a plain free-text question
            "Stop": [
                {"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
        })
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"hooks": hooks}, fh)
    os.replace(tmp, settings_path)


def reset_map(mapping_path: str) -> None:
    """Truncate the mapping file at app start. Agent ids are minted fresh every
    run (TerminalAgent.id), so lines from a previous run can never match a live
    agent — but starting clean keeps the file small and the read unambiguous."""
    try:
        open(mapping_path, "w", encoding="utf-8").close()
    except OSError:
        pass


def read_live_map(mapping_path: str) -> dict:
    """{agent_id: latest_record} from the mapping file. Each record is
    {session_id, transcript_path, cwd, source, ts}. Later lines win, so the
    result is each agent's CURRENT live conversation. Never raises -> {}."""
    out: dict = {}
    try:
        with open(mapping_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                aid = rec.get("agent_id")
                if aid:
                    out[aid] = rec  # last write for this agent wins
    except OSError:
        pass
    return out


def reset_events(events_path: str) -> None:
    """Truncate the prompt-events file at app start (see reset_map)."""
    try:
        open(events_path, "w", encoding="utf-8").close()
    except OSError:
        pass


# prompt-event kinds appended to the events file. Two targets, each a sticky
# boolean on the agent: "tool" (an AskUserQuestion/ExitPlanMode prompt is open)
# and "turn" (the agent ended a turn on a free-text question).
EV_TOOL_SET = "tool_set"
EV_TOOL_CLEAR = "tool_clear"
EV_TURN_SET = "turn_set"
EV_TURN_CLEAR = "turn_clear"


def read_prompt_events(events_path: str, offset: int = 0):
    """Read NEW prompt-event lines appended since `offset`. Returns
    (records, new_offset). Events are EDGES applied once (not a level to
    reconcile), so reading incrementally is essential — a stale line must never
    be re-applied after a local output-clear. Only complete (newline-
    terminated) lines are consumed; a half-written trailing line is left for the
    next read. Never raises."""
    records: list = []
    try:
        size = os.path.getsize(events_path)
    except OSError:
        return records, offset
    if size < offset:      # file was truncated/rotated -> start over
        offset = 0
    try:
        with open(events_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return records, offset
    # consume only up to the last newline; keep the tail for next time
    nl = data.rfind(b"\n")
    if nl < 0:
        return records, offset
    consumed = data[:nl + 1]
    new_offset = offset + len(consumed)
    for line in consumed.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("agent_id") and rec.get("kind"):
            records.append(rec)
    return records, new_offset


def _ends_with_question(text) -> bool:
    """True if `text` (an assistant message) ends on a question. Trailing
    whitespace and common wrapping punctuation (quotes/backticks/asterisks/
    parens) are stripped first so `...want that?"` or `...right?)` still count.
    Requiring the FINAL char to be '?' keeps ordinary prose (which merely
    CONTAINS questions) from lighting the indicator."""
    if not isinstance(text, str):
        return False
    return text.rstrip(" \t\r\n\"'`*)]}").endswith("?")


def _append_event(events_path: str, kind: str, extra: dict | None = None) -> None:
    record = {"agent_id": os.environ.get(AGENT_ID_ENV, ""),
              "kind": kind, "ts": time.time()}
    if extra:
        record.update(extra)
    line = json.dumps(record) + "\n"
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _append_record(mapping_path: str, payload: dict) -> None:
    record = {
        "agent_id": os.environ.get(AGENT_ID_ENV, ""),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"),
        "source": payload.get("source"),
        "ts": time.time(),
    }
    # single pre-built line + append mode: concurrent hooks from sibling agents
    # interleave at line granularity at worst, which the reader tolerates.
    line = json.dumps(record) + "\n"
    with open(mapping_path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _dispatch(mapping_path, events_path, payload: dict) -> None:
    """Route one hook payload to the right file/record by its event name."""
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        if mapping_path:
            _append_record(mapping_path, payload)
        return
    if not events_path:
        return
    if event == "PreToolUse":
        if payload.get("tool_name") in _WAITING_TOOL_SET:
            _append_event(events_path, EV_TOOL_SET,
                          {"tool": payload.get("tool_name")})
    elif event == "PostToolUse":
        if payload.get("tool_name") in _WAITING_TOOL_SET:
            _append_event(events_path, EV_TOOL_CLEAR,
                          {"tool": payload.get("tool_name")})
    elif event == "Stop":
        # a turn ended: any in-flight tool prompt is resolved (covers the user
        # cancelling an AskUserQuestion, where no PostToolUse fires), and the
        # agent is now waiting iff its last message was a plain question.
        _append_event(events_path, EV_TOOL_CLEAR, {"tool": "stop"})
        if _ends_with_question(payload.get("last_assistant_message")):
            _append_event(events_path, EV_TURN_SET)
        else:
            _append_event(events_path, EV_TURN_CLEAR)


def main(argv) -> int:
    """Hook entrypoint: read the hook JSON from stdin, stamp it with the
    per-agent env id, append the matching record(s). argv[1] is the SessionStart
    mapping file; argv[2] (optional) is the prompt-events file. Silent + best-
    effort: a hook must NEVER break the Claude session it is attached to."""
    mapping_path = argv[1] if len(argv) > 1 else None
    events_path = argv[2] if len(argv) > 2 else None
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            _dispatch(mapping_path, events_path, payload)
    except Exception:
        pass
    # hooks may print context for the model on stdout; we add none.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
