"""The durable record of plan-limit cut-offs: who was stopped, when, and how
it ended.

Everything else in this feature is TRANSIENT by design — the latch on a
`TerminalAgent`, the usage reading, the "?" state — because a persisted flag
goes stale and the transcript is the real evidence. But a cut-off itself is a
historical fact worth keeping: which workspace, which agent, which task, at
what local time, until when, and whether the resume actually took. Nothing in
the app could answer that after a restart, so a night's unattended recovery
could only be reconstructed by hand from transcript timestamps hours later.

Qt-free and stdlib-only, like `limit_banner` / `claude_usage` / `chime`: this
is written from the GUI thread and read at startup, and it must never be able
to take the app down. Every function swallows its own IO errors and degrades to
"no record" rather than raising.

The file is `<session-dir>/limit_events.jsonl`, append-only, one JSON object per
line, never rewritten in place — an append cannot corrupt what is already there,
which matters because this is written at exactly the moments things are going
wrong.

    {"event":"cut_off","at":...,"at_local":"2026-08-04 03:32:29",
     "ws_id":"07a5...","ws_name":"AI Hive","agent_name":"Agent 5",
     "cwd":"...","session_id":"51d0...","task":"fix the tiling math",
     "window":"session","banner":"You've hit your session limit ...",
     "reset_at":...,"reset_local":"2026-08-04 05:30","source":"transcript"}
    {"event":"resumed","at":...,"key":[...],"tries":1}

IDENTITY IS THE SUBTLE PART. `TerminalAgent.id` is a fresh uuid on every load,
so it cannot name a cut-off across the restart this file exists to survive; the
agent's DISPLAY NAME is not unique either. The stable triple is the folder, the
pinned conversation, and the window that stopped it — see `key_of`.
"""

from __future__ import annotations

import json
import os
import time

LEDGER_NAME = "limit_events.jsonl"

CUT_OFF = "cut_off"
RESUMED = "resumed"       # verified back at work
FAILED = "failed"         # retries exhausted
DISMISSED = "dismissed"   # not a real cut-off after all (phantom latch)

# outcomes that CLOSE a cut-off: it is never acted on again
TERMINAL = (RESUMED, FAILED, DISMISSED)

# Two cut-offs belong to the same window when their stated resets agree this
# closely. Every agent stopped by one window prints the SAME clock (verified in
# session.log: four agents cut off within a minute of each other all read
# "resets 5:30am"), so this only has to absorb rounding.
WINDOW_TOLERANCE_S = 60

# ...and when a banner named no clock at all, fall back to how far apart the
# cut-offs were: a window is five hours, so nothing older can belong to the same
# one.
WINDOW_SPAN_S = 5 * 3600


def path_for(session_dir: str) -> str:
    return os.path.join(session_dir or ".", LEDGER_NAME)


def local_stamp(epoch: float) -> str:
    """An epoch as a readable LOCAL date+time. Local on purpose: the whole
    point of the record is to answer "when did this happen to me", and Claude's
    own banner states its clock in the user's timezone too."""
    if not epoch:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    except (ValueError, OSError):
        return ""


def key_of(cwd: str, session_id: str, reset_at: float,
           cut_off_at: float = 0.0) -> list:
    """The stable identity of one cut-off, as a JSON-round-trippable list.

    (folder, pinned conversation, window). The first two survive a restart
    where the agent's own id does not, and the third separates successive
    cut-offs of the SAME conversation — an agent resumed at 05:30 and stopped
    again at 10:50 must be two records, not one.

    The window is named by its RESET time rather than by when the cut-off was
    noticed, because that is the one number both sources agree on: the live
    screen parses it from the banner as it is drawn, and the startup scan
    parses the same banner off disk anchored to its own timestamp — both land
    on the same instant. The notice times do NOT agree (one is "when the app
    saw it", the other "when Claude wrote it"), so keying on those would file
    one cut-off twice. A banner with no clock at all falls back to the hour it
    happened in, which is coarse enough to survive a restart and fine enough to
    separate two genuine cut-offs.
    """
    if reset_at:
        stamp = int(reset_at)
    else:
        stamp = int(cut_off_at // 3600 * 3600)
    return [cwd or "", session_id or "", stamp]


def _write(session_dir: str, record: dict) -> bool:
    try:
        with open(path_for(session_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except (OSError, ValueError, TypeError):
        # A ledger that cannot be written must not break the recovery it is
        # only observing; the caller audits the failure to session.log.
        return False


def record_cut_off(session_dir: str, *, cwd: str, session_id: str,
                   at: float, reset_at: float = 0.0, ws_id: str = "",
                   ws_name: str = "", agent_name: str = "", task: str = "",
                   window: str = "", banner: str = "",
                   source: str = "live") -> dict | None:
    """File one cut-off. Returns the record (with its `key`) or None if the
    write failed. `source` is "live" (seen on screen) or "transcript"
    (reconstructed at startup)."""
    record = {
        "event": CUT_OFF,
        "at": float(at or 0.0),
        "at_local": local_stamp(at),
        "ws_id": ws_id, "ws_name": ws_name, "agent_name": agent_name,
        "cwd": cwd, "session_id": session_id, "task": task,
        "window": window, "banner": banner,
        "reset_at": float(reset_at or 0.0),
        "reset_local": local_stamp(reset_at),
        "source": source,
        "key": key_of(cwd, session_id, reset_at, at),
    }
    return record if _write(session_dir, record) else None


def record_outcome(session_dir: str, key, outcome: str, tries: int = 0,
                   detail: str = "") -> bool:
    """Close a cut-off: it resumed, gave up, or turned out not to be one."""
    return _write(session_dir, {
        "event": outcome, "at": time.time(), "at_local": local_stamp(time.time()),
        "key": list(key), "tries": int(tries), "detail": detail})


def read_all(session_dir: str) -> list[dict]:
    """Every well-formed record, oldest first. A truncated or garbled line is
    skipped rather than fatal — this file is appended to at the moment the app
    may be killed, so a partial last line is expected, not exceptional."""
    out: list[dict] = []
    try:
        with open(path_for(session_dir), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("event"):
                    out.append(rec)
    except OSError:
        return out
    return out


def closed_keys(records: list[dict]) -> set:
    """Keys that already have a terminal outcome — never act on these again."""
    return {tuple(r.get("key") or ()) for r in records
            if r.get("event") in TERMINAL}


def open_cut_offs(session_dir: str) -> list[dict]:
    """Cut-offs still awaiting an outcome, newest last, one per key.

    De-duplicated because the same cut-off is legitimately filed more than
    once: the live screen sees it when it happens, and a later startup scan
    reconstructs the very same banner off disk. `key_of` is what makes those
    collapse into one.
    """
    records = read_all(session_dir)
    done = closed_keys(records)
    seen: set = set()
    out: list[dict] = []
    for rec in records:
        if rec.get("event") != CUT_OFF:
            continue
        key = tuple(rec.get("key") or ())
        if key in done or key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def latest_window(records: list[dict]) -> list[dict]:
    """Of these cut-offs, the ones belonging to the MOST RECENT window.

    This is what replaced a fixed age bound. An age bound answers the wrong
    question — it asks how old a cut-off is, when what matters is whether it
    was the last thing that happened. Several agents stopped by one window all
    state the same reset clock, so they group cleanly; anything from an earlier
    window is history and is left recorded but untouched.

    Anchored on when each cut-off HAPPENED (always known) rather than on the
    reset it stated (which a banner may omit), so a clockless cut-off can still
    be the newest one.
    """
    cuts = [r for r in records if r.get("event", CUT_OFF) == CUT_OFF]
    if not cuts:
        return []
    anchor = max(cuts, key=lambda r: float(r.get("at") or 0.0))
    a_at = float(anchor.get("at") or 0.0)
    a_reset = float(anchor.get("reset_at") or 0.0)
    out = []
    for rec in cuts:
        reset = float(rec.get("reset_at") or 0.0)
        at = float(rec.get("at") or 0.0)
        if a_reset and reset:
            same = abs(reset - a_reset) <= WINDOW_TOLERANCE_S
        else:
            same = abs(at - a_at) <= WINDOW_SPAN_S
        if same:
            out.append(rec)
    return out
