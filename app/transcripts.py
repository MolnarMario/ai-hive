"""Transcript backups: snapshot each pinned agent's Claude conversation.

Claude Code stores a conversation as a single JSONL file under
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl — one file, no backup,
owned by another program. That file has been lost twice (a deleted transcript
and a concurrent-resume truncation), which is why AI Hive keeps its own
snapshots: on every app START (before any agent launches — a bad resume can
never destroy the only copy) and on every graceful CLOSE (captures the day's
conversation).

Layout under <backup_dir> per session id:
  <id>.jsonl       newest snapshot
  <id>.jsonl.1     previous snapshot (one rotation)
  <id>.max.jsonl   HIGH-WATER copy — the largest version ever seen, never
                   overwritten by anything smaller. A truncated transcript
                   (the exact incident this guards against) can rotate through
                   the snapshots, but it can never displace the full copy.

Qt-free and best-effort throughout: backups must never block or crash the app.
"""

import os
import re
import shutil

_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")


def encode_project_dir(cwd: str) -> str:
    """Claude Code's project-directory encoding: every character of the
    absolute path that is not alphanumeric becomes '-'.
    C:\\Users\\Mario\\Downloads\\hive-test2 -> C--Users-Mario-Downloads-hive-test2
    (verified against the real ~/.claude/projects layout)."""
    return _ENCODE_RE.sub("-", os.path.normpath(cwd))


def transcript_path(cwd: str, session_id: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        encode_project_dir(cwd), f"{session_id}.jsonl")


def _same_file(a: str, b: str) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
        return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)
    except OSError:
        return False


def _backup_one(src: str, backup_dir: str, session_id: str) -> bool:
    newest = os.path.join(backup_dir, f"{session_id}.jsonl")
    prev = newest + ".1"
    high = os.path.join(backup_dir, f"{session_id}.max.jsonl")
    if _same_file(src, newest):
        return False  # unchanged since the last snapshot
    if os.path.isfile(newest):
        try:
            os.replace(newest, prev)
        except OSError:
            pass
    shutil.copy2(src, newest)
    try:  # high-water copy: only ever replaced by something LARGER
        if (not os.path.isfile(high)
                or os.path.getsize(newest) > os.path.getsize(high)):
            shutil.copy2(newest, high)
    except OSError:
        pass
    return True


def backup_for_agents(agents, backup_dir: str) -> int:
    """Snapshot the transcript of every pinned Claude agent. `agents` is any
    iterable of objects with .spec (provider/cwd/session_id). Returns how many
    transcripts were snapshotted; never raises."""
    done = 0
    for agent in agents:
        try:
            spec = agent.spec
            if spec.provider != "claude" or not spec.session_id:
                continue
            src = transcript_path(spec.cwd, spec.session_id)
            if not os.path.isfile(src):
                continue
            os.makedirs(backup_dir, exist_ok=True)
            if _backup_one(src, backup_dir, spec.session_id):
                done += 1
        except Exception:
            continue  # best-effort: one bad transcript never stops the rest
    return done
