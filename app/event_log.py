"""The app-wide event log: one timeline of what every agent in every workspace
did, kept on disk so a night's work can be read back after a restart.

Qt-free and stdlib-only, like `limit_ledger`: records are written from the GUI
thread at exactly the moments things go wrong (a crash, a cut-off), so every
function here swallows its own IO errors and degrades to "no record" rather
than raising. The collector that turns agent signals into records is
`app/event_hub.py`; the window that shows them is
`app/widgets/event_log_window.py`.

Storage is one append-only JSONL file per local day under
`<session-dir>/event_log/`, named `YYYY-MM-DD.jsonl`. Retention is then a
matter of deleting old files (`prune`), and nothing is ever rewritten in place.

    {"id": "3f0c...", "at": 1790000000.0, "kind": "question",
     "ws_id": "07a5...", "ws_name": "AI Hive", "agent_uid": "9c1e...",
     "agent_name": "Agent 5", "provider": "claude", "text": "",
     "data": {}, "ref": null}

Names are stored as they were when the event happened, so a renamed or deleted
agent still reads correctly in an old row. Links back to a live agent go
through `agent_uid` (`AgentSpec.uid`), never `TerminalAgent.id`, which is minted
fresh on every load. `ref` points a closing event (an answer, a resume) at the
record it closes.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
import uuid

LOG_DIR_NAME = "event_log"
KEEP_DAYS = 30
TEXT_CAP = 2000

# ---------------------------------------------------------------- kinds ---
PROMPT = "prompt"                # the user pressed Enter on their own prompt
TASK = "task"                    # AI Hive handed the agent a task
QUESTION = "question"            # the agent raised "?" and is waiting on you
ANSWERED = "answered"            # ...and stopped waiting (ref -> the question)
REPLY = "reply"                  # a turn the user asked for finished
LIMIT = "limit"                  # cut off by a plan limit
LIMIT_RESUMED = "limit_resumed"  # back at work after a cut-off (ref -> limit)
LIMIT_FAILED = "limit_failed"    # auto-continue gave up (ref -> limit)
LIMIT_DISMISSED = "limit_dismissed"  # not a real cut-off after all (ref)
SCHED_SENT = "sched_sent"        # a scheduled message went out
SCHED_MISSED = "sched_missed"    # ...or could not be delivered in time
CRASHED = "crashed"              # the process crashed or exited with an error
LIFECYCLE = "lifecycle"          # started/stopped/added/removed/renamed
BOARD = "board"                  # a line an agent posted to the shared board

# Closing kinds render into the row they close rather than as rows of their
# own. The limit outcomes are deliberately NOT here: "Agent 3 resumed at 17:31"
# is something people want to see at the moment it happened, so those get a
# row too (the "Resumes" group) as well as closing their cut-off's row.
MERGED_CLOSERS = (ANSWERED,)
LIMIT_OUTCOMES = (LIMIT_RESUMED, LIMIT_FAILED, LIMIT_DISMISSED)

# The window's filter chips: (key, label, kinds, shown by default). Every kind
# is ALWAYS recorded; these only decide what is shown, so switching a group on
# later reveals its history instead of starting empty. The defaults are the
# events that either need the user (questions, problems, failed resumes) or
# answer "is it done yet"; lifecycle and board chatter would bury those.
GROUPS = (
    ("prompts", "Prompts", (PROMPT, TASK), True),
    ("questions", "Questions", (QUESTION,), True),
    ("finished", "Finished", (REPLY,), True),
    ("limits", "Limits", (LIMIT,), True),
    ("resumes", "Resumes", LIMIT_OUTCOMES, True),
    ("problems", "Problems", (CRASHED, SCHED_MISSED), True),
    ("scheduled", "Scheduled", (SCHED_SENT,), False),
    ("lifecycle", "Lifecycle", (LIFECYCLE,), False),
    ("board", "Board", (BOARD,), False),
)
GROUP_OF = {k: g for g, _l, kinds, _d in GROUPS for k in kinds}
DEFAULT_GROUPS = frozenset(g for g, _l, _k, on in GROUPS if on)

WINDOW_LABELS = {"session": "5h", "weekly": "weekly", "opus": "weekly Opus",
                 "sonnet": "weekly Sonnet", "fable": "weekly Fable"}


def log_dir(session_dir: str) -> str:
    return os.path.join(session_dir, LOG_DIR_NAME)


def _day_name(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at)) + ".jsonl"


def make_record(kind: str, *, ws_id: str = "", ws_name: str = "",
                agent_uid: str = "", agent_name: str = "",
                provider: str = "", text: str = "", data: dict | None = None,
                ref: str | None = None, at: float | None = None) -> dict:
    return {"id": uuid.uuid4().hex, "at": time.time() if at is None else at,
            "kind": kind, "ws_id": ws_id, "ws_name": ws_name,
            "agent_uid": agent_uid, "agent_name": agent_name,
            "provider": provider, "text": (text or "")[:TEXT_CAP],
            "data": dict(data or {}), "ref": ref}


def append(directory: str, record: dict) -> bool:
    """Append one record to its day's file. False (never an exception) on any
    IO failure: losing a log line must not be able to take the app down."""
    try:
        os.makedirs(directory, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(os.path.join(directory, _day_name(record["at"])), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _day_files(directory: str) -> list[tuple[_dt.date, str]]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        try:
            day = _dt.date.fromisoformat(name[:-6])
        except ValueError:
            continue
        out.append((day, os.path.join(directory, name)))
    return sorted(out)


def read_since(directory: str, since: float) -> list[dict]:
    """Every readable record at or after `since`, oldest first. A torn or
    corrupt line (a crash mid-append) is skipped, not fatal."""
    first = _dt.date.fromtimestamp(since)
    out = []
    for day, path in _day_files(directory):
        if day < first:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (isinstance(rec, dict) and isinstance(rec.get("at"), (int, float))
                    and rec["at"] >= since and rec.get("kind")):
                out.append(rec)
    out.sort(key=lambda r: r["at"])
    return out


def prune(directory: str, keep_days: int = KEEP_DAYS,
          now: float | None = None) -> int:
    """Delete day files older than `keep_days`. Returns how many went."""
    today = _dt.date.fromtimestamp(time.time() if now is None else now)
    cutoff = today - _dt.timedelta(days=keep_days)
    gone = 0
    for day, path in _day_files(directory):
        if day < cutoff:
            try:
                os.remove(path)
                gone += 1
            except OSError:
                pass
    return gone


# ------------------------------------------------------------ wording ---

def format_duration(seconds: float) -> str:
    """45s / 4m 12s / 1h 12m / 2d 3h."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _clock(at: float, now: float) -> str:
    """HH:MM, date-prefixed when it is not today."""
    lt = time.localtime(at)
    if time.strftime("%Y-%m-%d", lt) == time.strftime(
            "%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", lt)
    return time.strftime("%b %d, %H:%M", lt)


def first_line(text: str, cap: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= cap else line[:cap - 1] + "…"


def describe(rec: dict) -> str:
    """The event's sentence, without the time, workspace or agent."""
    kind, text, data = rec.get("kind"), rec.get("text", ""), rec.get("data") or {}
    if kind == PROMPT:
        return f'you sent: "{first_line(text)}"'
    if kind == TASK:
        return f'AI Hive sent a task: "{first_line(text)}"'
    if kind == QUESTION:
        return "has a question"
    if kind == ANSWERED:
        return "answered a question"
    if kind == REPLY:
        took = data.get("took")
        return (f"finished (took {format_duration(took)})"
                if took is not None else "finished")
    if kind == LIMIT:
        label = WINDOW_LABELS.get(data.get("window") or "", "")
        what = f"hit the {label} limit" if label else "hit a usage limit"
        reset = data.get("reset_at")
        if reset:
            return f"{what}, resumes at {_clock(reset, rec['at'])}"
        return what
    if kind == LIMIT_RESUMED:
        why = f" ({data['detail']})" if data.get("detail") else ""
        return f"resumed after the limit{why}"
    if kind == LIMIT_FAILED:
        tries = data.get("tries")
        return (f"could not resume after the limit ({tries} tries)" if tries
                else "could not resume after the limit")
    if kind == LIMIT_DISMISSED:
        return "limit cut-off dismissed, no work was lost"
    if kind == SCHED_SENT:
        return f'scheduled message sent: "{first_line(text)}"'
    if kind == SCHED_MISSED:
        why = f" ({data['why']})" if data.get("why") else ""
        return f'scheduled message NOT sent{why}: "{first_line(text)}"'
    if kind == CRASHED:
        return text or "crashed"
    if kind in (LIFECYCLE, BOARD):
        return text
    return text or kind


def status(rec: dict, closer: dict | None, now: float,
           session_start: float) -> str:
    """The live right-hand note of an open-able row: "waiting 3m",
    "answered after 6m", "in 1h 12m", "resumed 17:31". "" for rows that
    never stay open."""
    kind = rec.get("kind")
    if kind == QUESTION:
        if closer is not None and (closer.get("data") or {}).get("limit_menu"):
            return "the limit menu, not a question"
        if closer is not None:
            return f"answered after {format_duration(closer['at'] - rec['at'])}"
        if rec["at"] < session_start:
            return "no answer recorded"
        return f"waiting {format_duration(now - rec['at'])}"
    if kind == LIMIT:
        if closer is not None:
            verb = {LIMIT_RESUMED: "resumed", LIMIT_FAILED: "gave up",
                    LIMIT_DISMISSED: "dismissed"}.get(closer.get("kind"), "closed")
            return f"{verb} {_clock(closer['at'], now)}"
        if rec["at"] < session_start:
            return "no outcome recorded"
        reset = (rec.get("data") or {}).get("reset_at")
        if reset:
            left = reset - now
            return (f"in {format_duration(left)}" if left > 0
                    else f"due {format_duration(-left)} ago")
        return "waiting for reset"
    return ""


def needs_attention(rec: dict, closer: dict | None,
                    session_start: float) -> bool:
    """Rows the "Needs you" filter keeps: an unanswered question from this run,
    a crash, a missed scheduled send, a resume that gave up."""
    kind = rec.get("kind")
    if kind == QUESTION:
        return closer is None and rec["at"] >= session_start
    return kind in (CRASHED, SCHED_MISSED, LIMIT_FAILED)


def parse_board_line(line: str) -> tuple[str, str, str] | None:
    """`- [Agent 5] 2026-09-25 01:46 message` -> ("Agent 5", stamp, message).
    None for anything else (the placeholder line, a hand edit)."""
    line = line.strip()
    if not line.startswith("- ["):
        return None
    close = line.find("]", 3)
    if close < 0:
        return None
    name = line[3:close].strip()
    rest = line[close + 1:].strip()
    parts = rest.split(" ", 2)
    if (len(parts) == 3 and len(parts[0]) == 10 and parts[0][4] == "-"
            and ":" in parts[1]):
        return name, f"{parts[0]} {parts[1]}", parts[2]
    return name, "", rest
