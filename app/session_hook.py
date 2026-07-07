"""Authoritative live-conversation reporting via a Claude `SessionStart` hook.

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


def _hook_command(mapping_path: str, python_exe: str | None = None) -> str:
    """The shell command Claude runs on SessionStart: this script, told where to
    append. Every path is quoted so spaces in the venv/app/session paths (the
    common case on Windows) survive the shell split."""
    py = python_exe or sys.executable
    script = os.path.abspath(__file__)
    return f'"{py}" "{script}" "{mapping_path}"'


def write_settings_file(settings_path: str, mapping_path: str,
                        python_exe: str | None = None) -> None:
    """Write the shared `--settings` file carrying ONLY our SessionStart hook.

    Only the `hooks` key is present, so passing it to `--settings` adds our hook
    without disturbing any other setting; and because Claude merges hooks across
    sources, the user's own hooks still fire too (verified). Best-effort."""
    settings = {
        "hooks": {
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
                 "hooks": [{"type": "command",
                            "command": _hook_command(mapping_path, python_exe),
                            "timeout": 30}]}
            ]
        }
    }
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh)
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


def main(argv) -> int:
    """Hook entrypoint: read the SessionStart JSON from stdin, stamp it with the
    per-agent env id, append one line to the mapping file. Silent + best-effort:
    a hook must NEVER break the Claude session it is attached to."""
    mapping_path = argv[1] if len(argv) > 1 else None
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if mapping_path:
        try:
            payload = json.loads(raw) if raw.strip() else {}
            if isinstance(payload, dict):
                _append_record(mapping_path, payload)
        except Exception:
            pass
    # SessionStart hooks may print context for the model on stdout; we add none.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
