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

# cache for Gemini token usage: db path -> (mtime, size, used, window).
_GEMINI_USAGE_CACHE: dict[str, tuple[float, int, int, int]] = {}

# cache for latest_gemini_ai_title: metadata path -> (mtime, size, {session_id: title}).
_GEMINI_TITLE_CACHE: dict[str, tuple[float, int, dict[str, str]]] = {}

# cache for limit_cut_off: path -> (mtime, size, verdict dict).
_LIMIT_CACHE: dict[str, tuple[float, int, dict]] = {}

# cache for typed_prompts: path -> (mtime, size, [prompt text, ...]).
_PROMPT_CACHE: dict[str, tuple[float, int, list]] = {}

# cache for latest_model_effort: path -> (mtime, size, model, effort, mode).
_MODEL_CACHE: dict[str, tuple[float, int, str, str, str]] = {}

# cache for reply_times: path -> (mtime, size, [(epoch, final text), ...]).
_REPLY_CACHE: dict[str, tuple[float, int, list]] = {}

# How much of the tail reply_times reads. Its one consumer can only anchor a
# stamp to a line the scrollback still holds (bounded by REPLAY_PROJECT_CAP,
# i.e. a few turns), so scanning a multi-MB conversation from the top would
# cost a great deal to produce entries nothing can use. A full scan is the fallback for the rare case the tail holds no
# finished reply at all (one enormous turn).
_REPLY_TAIL_BYTES = 256 * 1024

# How much of the tail latest_model_effort reads. It polls far more often than
# the title/usage readers, so it must not re-scan a multi-MB conversation on
# every write: both signals it wants (the last assistant record, the last
# /model or /effort announcement) are at the END of the file. A full scan is
# the fallback for the rare case the tail holds neither.
_MODEL_TAIL_BYTES = 65536

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# `/model` and `/effort` echo their result into the transcript as a
# <local-command-stdout> user record, the instant the user picks. Observed
# shapes (the model name arrives wrapped in a bold SGR run):
#   Set model to <b>Opus 5</b> and saved as your default for new sessions
#   Set model to <b>Opus 4.8 (1M context)</b> and saved ... with <b>high</b> effort
#   Set effort level to max (this session only): Maximum capability with ...
_SET_MODEL_BOLD_RE = re.compile(r"Set model to\s+\x1b\[1m(.+?)\x1b\[")
_SET_MODEL_RE = re.compile(
    r"Set model to\s+(.+?)(?:\s+and saved\b|\s+for this session\b|[.\n]|$)")
_SET_MODEL_EFFORT_RE = re.compile(r"with\s+(\w+)\s+effort")
_SET_EFFORT_RE = re.compile(r"Set effort level to\s+([A-Za-z]+)")
# a concrete model id as Claude writes it on an assistant record, e.g.
# claude-opus-5, claude-opus-4-8, claude-haiku-4-5-20251001, claude-sonnet-5[1m]
_MODEL_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?")


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


def latest_gemini_ai_title(cwd: str, session_id: str) -> str:
    """The most recent AI-generated conversation title or preview Gemini
    (Antigravity CLI) wrote into conversation_metadata.json for this session.
    "" if none / no file.
    Cached by (mtime, size) so repeated polling only re-reads on a real change.
    Never raises."""
    if not session_id:
        return ""
    from . import session_sync
    meta_path = os.path.join(session_sync.gemini_dir(), "cache", "conversation_metadata.json")
    return _read_gemini_ai_title(meta_path, session_id)


def _read_gemini_ai_title(path: str, session_id: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cached = _GEMINI_TITLE_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2].get(session_id, "")
    titles: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        convs = data.get("conversations", {})
        for sid, info in convs.items():
            s = info.get("summary") or {}
            t = s.get("Title") or s.get("Preview") or ""
            if t:
                titles[sid] = t
    except Exception:
        return cached[2].get(session_id, "") if cached else ""
    _GEMINI_TITLE_CACHE[path] = (st.st_mtime, st.st_size, titles)
    return titles.get(session_id, "")


def typed_prompts(cwd: str, session_id: str) -> list[str]:
    """Every prompt the USER typed in this conversation, oldest first.

    Prompt milestones are transient (they live on the agent and are minted
    from a keystroke), so a conversation restored from disk starts with none --
    which is exactly when the user most wants to jump around it. This is the
    authoritative way to get them back: Claude tags a genuinely typed prompt
    with `promptSource: "typed"` and `origin: {"kind": "human"}`, both absent
    on tool results and on the plumbing turns `_is_synthetic_user_turn`
    excludes. Marking only lines that match one of these can therefore never
    invent a milestone the user did not create.

    Cached by (mtime,size) like every other reader here. Never raises."""
    if not session_id or not cwd:
        return []
    return _read_typed_prompts(transcript_path(cwd, session_id))


def _read_typed_prompts(path: str) -> list[str]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _PROMPT_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return list(cached[2])
    out: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                # cheap prefilter: most records are not typed user prompts
                if '"typed"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue    # a partial last line while Claude is writing
                if rec.get("type") != "user" or rec.get("isSidechain"):
                    continue
                if rec.get("promptSource") != "typed":
                    continue
                if _is_synthetic_user_turn(rec):
                    continue
                text = _message_text(rec).strip()
                if text:
                    out.append(text)
    except OSError:
        return list(cached[2]) if cached else []
    _PROMPT_CACHE[path] = (st.st_mtime, st.st_size, list(out))
    return out


def reply_times(cwd: str, session_id: str) -> list[tuple[float, str]]:
    """Every FINISHED reply in this conversation, oldest first, as
    (epoch, final text).

    The durable answer to "when was this answer actually generated". The live
    signal cannot be: a reply mark is minted at the busy -> idle settle, so it
    only exists for turns THIS process watched finish. Reopen the app and every
    past turn has no stamp at all -- which is what the user sees, and stamping
    the current time on them instead (the bug `_settled_once` fixed) was worse.
    Claude timestamps every record it writes, so the transcript knows what no
    live observation can.

    A finished reply is the LAST assistant record carrying text before the next
    real user turn -- not every assistant text record, because a turn narrates
    between its tool calls ("Now bump __version__...") and those are mid-reply,
    not the end of one. Tool RESULTS come back as `user` records and are
    therefore not a boundary; everything else from the user side is (a typed
    prompt, a delivered task, a slash command's stdout), and flushing on those
    is right either way, since the reply had plainly finished before it.

    Sub-agent sidechains are skipped (they are not this conversation's replies)
    and so is a record with no usable timestamp -- a stamp reading 1970 is worse
    than no stamp. Tail-bounded (see _REPLY_TAIL_BYTES) with a full-scan
    fallback, cached by (mtime,size) like every other reader here. Never raises.
    """
    if not session_id or not cwd:
        return []
    return _read_reply_times(transcript_path(cwd, session_id))


def _read_reply_times(path: str) -> list[tuple[float, str]]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _REPLY_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return list(cached[2])
    try:
        out = _scan_reply_times(_tail_lines(path, _REPLY_TAIL_BYTES))
        if not out and st.st_size > _REPLY_TAIL_BYTES:
            # one turn longer than the whole tail: pay for the full scan once,
            # then the cache holds until the file changes
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                out = _scan_reply_times(fh)
    except OSError:
        return list(cached[2]) if cached else []
    _REPLY_CACHE[path] = (st.st_mtime, st.st_size, list(out))
    return out


def _scan_reply_times(lines) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    pending: tuple[float, str] | None = None
    for line in lines:
        if '"assistant"' not in line and '"user"' not in line:
            continue    # cheap prefilter before the JSON parse
        try:
            rec = json.loads(line)
        except ValueError:
            continue    # a partial last line while Claude is writing
        if rec.get("isSidechain"):
            continue    # a sub-agent's turn, not this conversation's
        kind = rec.get("type")
        if kind == "user":
            if _is_tool_result(rec):
                continue        # mid-turn: the reply has not finished yet
            if pending is not None:
                out.append(pending)
                pending = None
        elif kind == "assistant":
            text = _message_text(rec).strip()
            when = _record_epoch(rec)
            if text and when:
                pending = (when, text)   # only the LAST one ends the reply
    if pending is not None:
        out.append(pending)     # the conversation ends on a reply
    return out


def _is_tool_result(rec: dict) -> bool:
    """True when a 'user' record is a tool's REPLY rather than a turn of its
    own. These sit inside a reply (Claude calls a tool, reads the result, keeps
    going), so they must not be read as the boundary that ends one."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(b.get("type") == "tool_result" for b in content
               if isinstance(b, dict))


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
    info = limit_cut_off(cwd, session_id)
    if not info or not info["cut_off"]:
        return (False, 0.0, 0.0)
    return (True, info["at"], info["resets_at"])


def limit_cut_off(cwd: str, session_id: str) -> dict | None:
    """The same verdict as `ended_on_limit`, but TRI-STATE and detailed.

    Returns None when there is no readable transcript at all, and otherwise
    `{"cut_off", "at", "resets_at", "banner", "window", "synthetic"}`.

    The distinction between "the conversation carried on" and "there is no
    conversation to read" is what makes this safe to act on. A caller using it
    to CONTRADICT a live screen latch must only drop the latch on the former:
    a pin that has drifted, or a transcript Claude has not flushed yet, reads
    as absent — and treating that as "not cut off" would strand a genuinely
    parked agent. `ended_on_limit` collapses both to False, which is right for
    a caller that only wants positive evidence.

    CAREFUL, and this is the trap: "not flushed yet" only reads as None while
    the transcript file does not EXIST. A running agent always has one, so a
    banner Claude has drawn but not yet written reads as `cut_off False` —
    indistinguishable, on that field alone, from a conversation that carried
    on. That is fine for a caller running hours later at reset time, and
    wrong for one running seconds after the banner appeared. Hence
    `synthetic`: True only when a banner WAS found and the turn behind it was
    Claude Code's own plumbing (`_is_synthetic_user_turn`) rather than
    anything the user or AI Hive asked for, so nothing of substance was
    interrupted. It is POSITIVE evidence, never the absence of evidence, and
    it is what an early caller must gate on.

    The banner text and its window come back too, because a cut-off's identity
    (which window stopped it, and when that window reopens) cannot be
    reconstructed from a bool. Cached by (mtime,size); never raises.
    """
    if not session_id or not cwd:
        return None
    return _read_limit_cut_off(transcript_path(cwd, session_id))


# Claude Code's own plumbing, as it appears in a bare-string 'user' record.
# An ALLOWLIST, deliberately, not "any <tag>": the tags below are things the
# CLI injects with nobody asking, while `<command-name>`/`<command-message>`
# -- which read identically -- are a slash command the USER typed, i.e. work
# they asked for and would want resumed. Keying off the leading "<" alone
# swept those in and would silently drop a genuine cut-off during `/compact`,
# a custom command, or anything else invoked by name (observed in real
# transcripts: a `<command-name>` record followed directly by the assistant
# turn). Every entry here was seen standing alone in a real transcript;
# `<system-reminder>` is the one defensive addition -- it is injected context,
# never a request.
_SYNTHETIC_USER_TAGS = ("<task-notification>", "<local-command-stdout>",
                        "<local-command-caveat>", "<system-reminder>")


def _is_synthetic_user_turn(rec: dict) -> bool:
    """True when a 'user' record is Claude Code's OWN plumbing -- a background
    command's completion notification, a slash-command's stdout, ... -- rather
    than something a human or AI Hive actually asked for.

    These are a bare `<tag>...` string from `_SYNTHETIC_USER_TAGS` (the same
    family `latest_model_effort` already skips as `<synthetic>`), never how a
    real prompt or a delivered task reads. The distinction matters here
    because Claude Code can turn a background tool's own completion into a
    brand-new turn with NO input from the user or AI Hive at all -- and if the
    account happens to be exhausted right then, that turn eats the SAME
    "You've hit your session limit" menu a real interruption would, with
    nothing of substance actually lost."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, str):
        return False        # a list of blocks is a real prompt or a tool reply
    return content.lstrip().startswith(_SYNTHETIC_USER_TAGS)


def _read_limit_cut_off(path: str) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _LIMIT_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    found = {"cut_off": False, "at": 0.0, "resets_at": 0.0,
             "banner": "", "window": "", "synthetic": False}
    last_user_synthetic = False   # no evidence yet -> assume a real turn
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"assistant"' not in line and '"user"' not in line:
                    continue      # cheap prefilter
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a partial last line while Claude is writing
                rtype = rec.get("type")
                if rtype == "user" and not rec.get("isSidechain"):
                    last_user_synthetic = _is_synthetic_user_turn(rec)
                    continue
                if rtype != "assistant" or rec.get("isSidechain"):
                    continue
                text = _message_text(rec)
                # every assistant turn overwrites the verdict, so only the LAST
                # one counts -- a banner followed by real output is history
                # `banner_line`, not a bare regex search: an agent that merely
                # WROTE ABOUT the limit would otherwise be armed for a resume
                # it never needed (observed live on an agent working on this
                # feature). A real cut-off is a short injected line.
                banner = limit_banner.banner_line(text)
                # A banner is only a genuine interruption when the turn it cut
                # off was one the user (or a delivered task) actually asked
                # for -- see `_is_synthetic_user_turn`. Otherwise this is the
                # SAME class of false alarm as the "quoting the banner in
                # prose" case above: real API exhaustion, but nothing of the
                # agent's assigned work was actually lost.
                if banner and not last_user_synthetic:
                    when = _record_epoch(rec)
                    found = {
                        "cut_off": True, "at": when, "banner": banner,
                        "window": limit_banner.banner_window(banner),
                        "resets_at": (limit_banner.banner_reset_at(text, when)
                                      or 0.0),
                        "synthetic": False}
                else:
                    # `synthetic` is POSITIVE evidence and only that: a banner
                    # we DID see, refuted by the turn behind it. A last turn
                    # with no banner at all leaves it False, which is what
                    # keeps "the record isn't written yet" distinguishable
                    # from "this was never real work" -- see `limit_cut_off`.
                    found = {"cut_off": False, "at": 0.0, "resets_at": 0.0,
                             "banner": "", "window": "",
                             "synthetic": bool(banner)}
    except OSError:
        return cached[2] if cached else None
    _LIMIT_CACHE[path] = (st.st_mtime, st.st_size, found)
    return found


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


def _parse_varint(data: bytes, pos: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        res |= (b & 0x7f) << shift
        shift += 7
        if not (b & 0x80):
            break
    return res, pos


def latest_gemini_token_usage(cwd: str, session_id: str) -> tuple[int, int]:
    """Context-window occupancy of Gemini session, as (used_tokens, window_tokens).
    Reads the latest token count from Gemini's sqlite conversation database.
    Returns (0, 0) when no usage data is found."""
    if not session_id:
        return (0, 0)
    from . import session_sync
    db_path = os.path.join(session_sync.gemini_dir(), "conversations", f"{session_id}.db")
    return _read_gemini_db_token_usage(db_path)


def _read_gemini_db_token_usage(path: str) -> tuple[int, int]:
    try:
        st = os.stat(path)
    except OSError:
        return (0, 0)
    cached = _GEMINI_USAGE_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return (cached[2], cached[3])
    used, window = 0, 1_000_000
    try:
        import sqlite3
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute('SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 1;')
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            data = bytes(row[0])
            matches = re.finditer(rb'[\x22][\x00-\xff]{5,150}\x28', data)
            last_val = 0
            for m in matches:
                val, _ = _parse_varint(data, m.end())
                if 0 < val < 10_000_000:
                    last_val = val
            if last_val > 0:
                used = last_val
    except Exception:
        return (cached[2], cached[3]) if cached else (0, 0)
    _GEMINI_USAGE_CACHE[path] = (st.st_mtime, st.st_size, used, window)
    return (used, window)


def model_display(raw: str) -> str:
    """A concrete model id or alias as a short human label: claude-opus-5 ->
    'Opus 5', claude-opus-4-8 -> 'Opus 4.8', claude-haiku-4-5-20251001 ->
    'Haiku 4.5', claude-sonnet-5[1m] -> 'Sonnet 5 (1M)', 'opus' -> 'Opus'.
    Names Claude already prints in friendly form (from a /model announcement)
    pass through, only trimmed. "" stays ""."""
    text = (raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    big = "[1m]" in lowered or lowered.endswith("-1m")
    core = lowered.replace("[1m]", "").rstrip()
    m = _MODEL_ID_RE.match(core)
    if m:
        family, major, minor = m.group(1).title(), m.group(2), m.group(3)
        # a trailing 8-digit group is a release date (claude-haiku-4-5-20251001),
        # not a version component
        version = f"{major}.{minor}" if minor and len(minor) <= 2 else major
        return f"{family} {version}" + (" (1M)" if big else "")
    if core in ("opus", "sonnet", "haiku", "fable"):
        return core.title() + (" (1M)" if big else "")
    # already friendly ("Opus 4.8 (1M context)"): only shorten the window note
    return text.replace("(1M context)", "(1M)").strip()


def latest_model_effort(cwd: str, session_id: str) -> tuple[str, str, str]:
    """What this conversation is running with RIGHT NOW, as
    (model_display, effort, permission_mode). All three can change mid-session
    (/model, /effort, Shift+Tab), so neither the launch flags nor a single
    record kind is enough; the sources are merged in file order, last one wins:

      * every assistant record carries `message.model` and a top-level `effort`
        (ground truth, but only as of the last turn);
      * `/model` and `/effort` append a <local-command-stdout> user record the
        instant the user picks, which is what makes an idle agent's switch
        visible without waiting for a turn;
      * the permission mode rides on every user prompt record as a top-level
        `permissionMode`, AND on a dedicated `{"type":"permission-mode"}`
        record Claude appends as soon as Shift+Tab changes it (again, the part
        that shows an idle agent's switch). Its token is the CLI's INTERNAL
        name, which is not always a launch flag ("default"); translating that
        is providers.normalize_permission_mode's job, not this reader's.

    Sub-agent sidechains are skipped (they run their own model) and so is the
    `<synthetic>` pseudo-model. ("", "", "") when there is no file / no
    evidence. Cached by (mtime,size); never raises."""
    if not session_id or not cwd:
        return ("", "", "")
    return _read_model_effort(transcript_path(cwd, session_id))


def _read_model_effort(path: str) -> tuple[str, str, str]:
    try:
        st = os.stat(path)
    except OSError:
        return ("", "", "")
    cached = _MODEL_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return (cached[2], cached[3], cached[4])
    try:
        model, effort, mode = _scan_model_effort(
            _tail_lines(path, _MODEL_TAIL_BYTES))
        if not model and st.st_size > _MODEL_TAIL_BYTES:
            # nothing in the tail (a long stretch of tool output, say): pay for
            # the full scan once, then the cache holds until the file changes
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                model, effort, mode = _scan_model_effort(fh)
    except OSError:
        return (cached[2], cached[3], cached[4]) if cached else ("", "", "")
    _MODEL_CACHE[path] = (st.st_mtime, st.st_size, model, effort, mode)
    return (model, effort, mode)


def _tail_lines(path: str, limit: int) -> list[str]:
    """The last `limit` bytes of `path` as whole lines (a leading partial line
    is dropped, since it cannot be parsed anyway)."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - limit)
        fh.seek(start)
        data = fh.read()
    if start:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data.decode("utf-8", "replace").splitlines()


def _scan_model_effort(lines) -> tuple[str, str, str]:
    model = effort = mode = ""
    for line in lines:
        is_turn = '"assistant"' in line
        is_pick = "Set model to" in line or "Set effort level to" in line
        is_mode = '"permissionMode"' in line
        if not (is_turn or is_pick or is_mode):
            continue  # cheap prefilter before the JSON parse
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a partial last line while Claude is writing
        if rec.get("isSidechain"):
            continue  # a sub-agent's model, not this conversation's
        # the permission mode rides on ordinary records rather than having a
        # record kind of its own, so it is read before the type dispatch: a
        # user prompt carries the mode it was submitted under, and a bare
        # {"type":"permission-mode"} record marks a Shift+Tab as it happens
        current = rec.get("permissionMode")
        if isinstance(current, str) and current:
            mode = current
        if rec.get("type") == "assistant":
            raw = ((rec.get("message") or {}).get("model") or "")
            if raw and not raw.startswith("<"):   # skip the <synthetic> model
                model = model_display(raw)
            if rec.get("effort"):
                effort = str(rec["effort"])
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, str):
            continue
        picked, with_effort = _parse_set_model(content)
        if picked:
            model = picked
        if with_effort:
            effort = with_effort
        m = _SET_EFFORT_RE.search(_ANSI_RE.sub("", content))
        if m:
            effort = m.group(1).lower()
    return (model, effort, mode)


def _parse_set_model(content: str) -> tuple[str, str]:
    """(model, effort) announced by a `/model` pick; ("", "") if this isn't one.
    The bold run around the name is the precise delimiter; the plain-text form
    is the fallback for a build that stops emitting the SGR codes."""
    if "Set model to" not in content:
        return ("", "")
    m = _SET_MODEL_BOLD_RE.search(content)
    plain = _ANSI_RE.sub("", content)
    if not m:
        m = _SET_MODEL_RE.search(plain)
    name = model_display(m.group(1)) if m else ""
    eff = _SET_MODEL_EFFORT_RE.search(plain)
    return (name, eff.group(1).lower() if eff else "")


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
