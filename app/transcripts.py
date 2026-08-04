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

import json
import os
import re
import shutil
from datetime import datetime

from . import limit_banner

_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")

# cache for latest_ai_title: transcript path -> (mtime, size, title). Claude
# rewrites the title as a conversation evolves; we re-read only when the file
# actually changed so polling several agents stays cheap.
_TITLE_CACHE: dict[str, tuple[float, int, str]] = {}

# cache for latest_token_usage: transcript path -> (mtime, size, used, window).
_USAGE_CACHE: dict[str, tuple[float, int, int, int]] = {}

# cache for ended_on_limit: path -> (mtime, size, (cut_off, written, resets)).
_LIMIT_CACHE: dict[str, tuple[float, int, tuple[bool, float, float]]] = {}


def context_window_for(model: str) -> int:
    """The context-window size (in tokens) a model runs with in AI Hive, used
    only to turn a raw token count into a percentage for the card badge. Opus /
    Sonnet 4.x+ agents run with the 1M-token window here; anything else (and the
    unknown case) falls back to the classic 200K. Never the source of truth for
    billing — just a denominator for the on-card 'context full' indicator."""
    m = (model or "").lower()
    if any(k in m for k in ("opus-4", "opus-5", "sonnet-4", "sonnet-5", "-1m")):
        return 1_000_000
    return 200_000


def encode_project_dir(cwd: str) -> str:
    """Claude Code's project-directory encoding: every character of the
    absolute path that is not alphanumeric becomes '-'.
    C:\\Users\\Mario\\Downloads\\hive-test2 -> C--Users-Mario-Downloads-hive-test2
    (verified against the real ~/.claude/projects layout)."""
    return _ENCODE_RE.sub("-", os.path.normpath(cwd))


def transcript_path(cwd: str, session_id: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        encode_project_dir(cwd), f"{session_id}.jsonl")


def latest_ai_title(cwd: str, session_id: str) -> str:
    """The most recent AI-generated conversation title Claude Code wrote into
    this session's transcript — the same short summary shown in `/resume`.
    Claude appends `{"type":"ai-title","aiTitle":"...","sessionId":...}` records
    as the conversation evolves; we return the LAST one. "" if none / no file.
    Cached by (mtime,size) so repeated polling only re-reads on a real change.
    Never raises."""
    if not session_id or not cwd:
        return ""
    return _read_latest_ai_title(transcript_path(cwd, session_id))


def _read_latest_ai_title(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cached = _TITLE_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    title = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                # cheap prefilter before the JSON parse (most lines aren't titles)
                if '"ai-title"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a partial last line while Claude is writing
                if rec.get("type") == "ai-title" and rec.get("aiTitle"):
                    title = rec["aiTitle"]
    except OSError:
        return cached[2] if cached else ""
    _TITLE_CACHE[path] = (st.st_mtime, st.st_size, title)
    return title


def ended_on_limit(cwd: str, session_id: str) -> tuple[bool, float, float]:
    """Did this conversation STOP because the plan limit ran out?

    Returns (cut_off, written_at_epoch, resets_at_epoch) — `resets_at` is 0.0
    when the banner named no time. The durable record of a cut-off: after a
    restart the live screen shows a REPLAYED conversation, but the transcript
    still ends exactly where the limit stopped it, which is how both live
    failures of this feature were ultimately diagnosed.

    The test is that the LAST assistant message is the limit banner. "Last" is
    what makes it safe: if the agent said anything afterwards it plainly
    carried on, so it is not sitting cut off and must be left alone.

    The reset time is resolved HERE, against the record's own timestamp, since
    this is the only place both halves are in hand — the banner's clock is bare
    ("resets 3am") and anchoring it to the current time instead would land a
    day late. Cached by (mtime,size); never raises.
    """
    if not session_id or not cwd:
        return (False, 0.0, 0.0)
    return _read_ended_on_limit(transcript_path(cwd, session_id))


def _read_ended_on_limit(path: str) -> tuple[bool, float, float]:
    empty = (False, 0.0, 0.0)
    try:
        st = os.stat(path)
    except OSError:
        return empty
    cached = _LIMIT_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    hit, when, resets = False, 0.0, 0.0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"assistant"' not in line:   # cheap prefilter
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a partial last line while Claude is writing
                if rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                text = _message_text(rec)
                # every assistant turn overwrites the verdict, so only the LAST
                # one counts -- a banner followed by real output is history
                # `banner_in`, not a bare regex search: an agent that merely
                # WROTE ABOUT the limit would otherwise be armed for a resume
                # it never needed (observed live on an agent working on this
                # feature). A real cut-off is a short injected line.
                hit = limit_banner.banner_in(text)
                if hit:
                    when = _record_epoch(rec)
                    resets = limit_banner.banner_reset_at(text, when) or 0.0
                else:
                    when, resets = 0.0, 0.0
    except OSError:
        return cached[2] if cached else empty
    result = (hit, when, resets)
    _LIMIT_CACHE[path] = (st.st_mtime, st.st_size, result)
    return result


def _message_text(rec: dict) -> str:
    """Flatten an assistant record's content to plain text."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("text"))
    return ""


def _record_epoch(rec: dict) -> float:
    """A transcript record's ISO-8601 UTC timestamp as epoch seconds."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str):
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def latest_token_usage(cwd: str, session_id: str) -> tuple[int, int]:
    """Context-window occupancy of this session's LAST main-conversation turn,
    as (used_tokens, window_tokens). Claude writes a `message.usage` block on
    every assistant record; the tokens occupying the window after a turn are
    input + cache_creation + cache_read + output. We take the last such record
    (skipping sub-agent sidechains, whose usage is a separate context) and size
    the window from that record's model. Returns (0, 0) when there is no file /
    no usage yet. Cached by (mtime,size) so polling several agents stays cheap.
    Never raises."""
    if not session_id or not cwd:
        return (0, 0)
    return _read_latest_token_usage(transcript_path(cwd, session_id))


def _read_latest_token_usage(path: str) -> tuple[int, int]:
    try:
        st = os.stat(path)
    except OSError:
        return (0, 0)
    cached = _USAGE_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return (cached[2], cached[3])
    used, window = 0, 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"usage"' not in line:  # cheap prefilter before JSON parse
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a partial last line while Claude is writing
                if rec.get("isSidechain"):
                    continue  # a sub-agent's context, not this conversation's
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                total = (usage.get("input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)
                         + usage.get("output_tokens", 0))
                if total <= 0:
                    continue
                used = total
                window = context_window_for(msg.get("model", ""))
    except OSError:
        return (cached[2], cached[3]) if cached else (0, 0)
    # a turn that overflowed the sized window means the larger beta is active
    if used > window:
        window = 1_000_000
    _USAGE_CACHE[path] = (st.st_mtime, st.st_size, used, window)
    return (used, window)


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
