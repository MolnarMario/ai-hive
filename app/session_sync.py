"""Keep each Claude agent's pinned session id in sync with the transcript it
is ACTUALLY writing — and recover a resume whose pinned conversation is gone.

AI Hive launches every Claude agent with `--session-id <uuid>` and resumes it
with `--resume <uuid>`, so normally the pinned id matches the live
conversation. But the live id can DRIFT out from under AI Hive, because AI
Hive only knows the id it put on the command line — it never sees the child's
real current session id:

  - the user runs `/resume` inside the Claude TUI and switches to a different
    conversation (Claude then appends to a different <id>.jsonl);
  - a conversation forks (usage-limit recovery, `--fork-session`);
  - a card is reused / a fresh id is minted that never gets a transcript.

When that happens the pin goes stale and the NEXT reopen resumes the WRONG
conversation, or dies on `--resume <missing>`. Both have bitten the user (a
morning thread came back instead of the afternoon one; a never-used id errored
on resume). This module recovers the truth from the filesystem: Claude writes
exactly one `<id>.jsonl` per conversation under
`~/.claude/projects/<encoded-cwd>/`, and the file an agent has been appending
to during its current run IS its live conversation.

Qt-free and best-effort throughout, mirroring `transcripts.py`.
"""

import json
import os
import re
from collections import defaultdict, namedtuple

from . import transcripts

# Correlation slack: a transcript touched no earlier than (agent-start - this)
# is treated as written during the agent's current run. A couple of seconds
# absorbs the resume-replay touch racing just ahead of us stamping start time.
_START_SLACK = 3.0

# A transcript smaller than this is a near-empty STUB — a brand-new chat with
# nothing but mode/permission/slash-command lines (a fresh session, a glitched
# resume that started over, an accidental /clear). A pin must NEVER drift from
# a substantial conversation onto such a stub, and recovery must never resume
# one when a real conversation exists: a fresh chat has to EARN the pin by
# accumulating content first, or a stray empty session on reopen would strand
# a 10 MB conversation (a real incident — the left card came back blank while
# its big chat sat un-pinned on disk). One real user turn with tool use is
# tens of KB, well above this floor.
_STUB_BYTES = 4096

# Claude session ids are UUIDs; only accept those as transcript names so a
# stray file in the project dir can never be mistaken for a conversation.
_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# One running Claude agent, reduced to just what reconciliation needs.
AgentInfo = namedtuple("AgentInfo", "key cwd pinned_id started_at")

# One resumable conversation in a folder, for the New-Agent "Resume" picker.
Conversation = namedtuple("Conversation", "session_id mtime preview size")


def is_session_id(value: str) -> bool:
    """True when `value` is a Claude session id (a UUID). Used to sanity-check a
    hook-reported id before pinning to it, so a garbled mapping line can never
    become a bogus --resume target."""
    return bool(value) and bool(_ID_RE.match(value))


def _first_user_text(path: str, max_scan: int = 300) -> str:
    """First human message in a Claude transcript, collapsed to one line, for
    a resume-picker label. Skips system/command wrappers (lines whose text
    starts with '<'). Reads only the head of the file. Best-effort -> ''."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_scan:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                        elif isinstance(block, str):
                            text = block
                        if text:
                            break
                text = " ".join(text.split())
                # skip tool results / injected system reminders / slash commands
                if text and not text.startswith("<"):
                    return text
    except OSError:
        pass
    return ""


def conversation_previews(cwd: str) -> list:
    """Every resumable Claude conversation in `cwd`'s folder as Conversation
    tuples, newest first: session id, mtime, a first-message preview (empty for
    a stub), and byte size. Never raises."""
    out = [
        Conversation(sid, mt,
                     _first_user_text(transcripts.transcript_path(cwd, sid)),
                     transcript_size(cwd, sid))
        for sid, mt in list_transcripts(cwd).items()
    ]
    out.sort(key=lambda c: c.mtime, reverse=True)
    return out


def project_dir(cwd: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        transcripts.encode_project_dir(cwd))


def list_transcripts(cwd: str) -> dict:
    """{session_id: mtime} for every conversation Claude has written for this
    cwd. Empty when the project dir doesn't exist. Never raises."""
    out: dict = {}
    try:
        with os.scandir(project_dir(cwd)) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(".jsonl"):
                    continue
                sid = name[:-6]  # strip ".jsonl"
                if not _ID_RE.match(sid):
                    continue
                try:
                    out[sid] = entry.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        pass
    return out


def transcript_exists(cwd: str, session_id: str) -> bool:
    return bool(session_id) and os.path.isfile(
        transcripts.transcript_path(cwd, session_id))


def transcript_size(cwd: str, session_id: str) -> int:
    """Byte size of a pinned transcript, or 0 if missing/unreadable. Used to
    tell a barely-used STUB launch pin (the mark of an agent that /resumed
    away to another conversation) from a substantial one an agent still owns."""
    if not session_id:
        return 0
    try:
        return os.path.getsize(transcripts.transcript_path(cwd, session_id))
    except OSError:
        return 0


def best_recovery_id(cwd: str, exclude=()) -> str | None:
    """The most recently modified real conversation in this folder, skipping
    ids in `exclude` (a sibling agent's pin — never recover onto that, or two
    agents would resume the same transcript and race/truncate it). Near-empty
    stubs are passed over in favour of substantial conversations, so a stray
    fresh session never wins the recovery; only if EVERY candidate is a stub
    do we fall back to the most recent one. None when there is nothing safe to
    recover."""
    ex = set(exclude)
    cands = [(mt, sid) for sid, mt in list_transcripts(cwd).items()
             if sid not in ex]
    if not cands:
        return None
    cands.sort()  # by mtime, then id
    real = [c for c in cands if transcript_size(cwd, c[1]) >= _STUB_BYTES]
    return (real or cands)[-1][1]


def resolve_live_ids(agents, lister=list_transcripts,
                     sizer=transcript_size) -> dict:
    """Given the currently-running Claude agents, return
    {key: corrected_session_id} for those whose pinned id no longer matches
    the transcript they are really writing.

    ONLY tracks folders with a SINGLE Claude agent, where "the transcript being
    written" is unambiguous. In a folder with two or more agents the filesystem
    cannot say WHICH agent owns WHICH transcript — a resume touches them all at
    launch, so mtime correlation guesses, and a wrong guess SWAPS two live
    conversations or strands one on an empty stub. That happened repeatedly
    (a good conversation got pushed onto a fresh /recap session while its real
    chat sat un-pinned on disk), so multi-agent folders are deliberately left
    exactly as launched/restored. A genuinely BROKEN pin (a missing transcript)
    is still repaired at resume time by `_recover_missing_resume_target`, which
    excludes sibling pins and avoids stubs — the only place multi-agent folders
    are touched, and only when a pin points at nothing.

    For the single-agent case it proposes a transcript that:
      * is NOT the agent's own pin (nothing to do);
      * was modified DURING this agent's current run (mtime >= start - slack),
        so a pre-existing unrelated conversation is never adopted, and a
        freshly-minted-but-unused id is left alone until it actually gets used;
      * is newer than the agent's pinned transcript (or the pin is missing);
      * is not a near-empty STUB while the current pin is a real conversation
        (a fresh chat must earn the pin by accumulating content first).
    Worst case it changes nothing and the app behaves exactly as before.
    """
    groups = defaultdict(list)
    for a in agents:
        groups[transcripts.encode_project_dir(a.cwd)].append(a)

    updates: dict = {}
    for group in groups.values():
        if len(group) != 1:
            continue  # multi-agent folder: correlation is unsafe (see above)
        a = group[0]
        if not a.started_at:
            continue  # unknown start time -> can't correlate safely
        files = lister(a.cwd)
        if not files:
            continue
        best = None  # (mtime, sid)
        for sid, mt in files.items():
            if sid == a.pinned_id:
                continue
            if mt < a.started_at - _START_SLACK:
                continue  # written before this run — not ours
            if best is None or mt > best[0]:
                best = (mt, sid)
        if best is None:
            continue
        pin_mt = files.get(a.pinned_id)
        if pin_mt is None or best[0] > pin_mt:
            # never abandon a real conversation for a near-empty stub
            if (sizer(a.cwd, best[1]) < _STUB_BYTES
                    <= sizer(a.cwd, a.pinned_id)):
                continue
            updates[a.key] = best[1]
    return updates
