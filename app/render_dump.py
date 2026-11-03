"""The raw pty bytes of a Claude card whose scrollback picked up doubled
rows, saved the moment it happened so the case can be replayed offline.

Doubled text has come back in a new shape after several Claude Code
releases: a repaint from the top row, a misplaced redraw after the idle
recap, a reflow, a one-column resize. Each time the cause had to be pieced
together from a screenshot, because a running card's stream lives only in
AI Hive's memory and the snapshot written at close records no sizes. So
TerminalView watches what reaches a Claude card's scrollback
(`_scan_for_copies`), and when a row repeats the row before it, or a run of
rows repeats an earlier run, TerminalAgent.note_render_dup writes a
`RENDER-DUP` line to session.log and saves one of these.

A dump holds the agent's replay buffer (the last PTY_BUFFER_CAP of the
stream), the view sizes it was drawn at with their offsets, and the Claude
version. `tools/render_replay.py <dump>` feeds those bytes through a fresh
TerminalView at the same sizes, which reproduces the card. See
docs/agents/terminal-rendering.md for what to do with one.

Not session state: plain files under `<session-dir>/render_dumps/`, the
newest KEEP of them kept. Qt-free and stdlib-only, like screen_snapshot.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid

DIR_NAME = "render_dumps"
KEEP = 5
FORMAT = 1


def save(dir_path: str, label: str, payload: dict) -> str:
    """Write one dump under `dir_path`/render_dumps and drop all but the
    newest KEEP. Returns the file's path, or "" on any failure. Never raises:
    this is forensics, and it runs on the GUI thread beside live output."""
    if not dir_path:
        return ""
    try:
        folder = os.path.join(dir_path, DIR_NAME)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", label or "").strip("-")[:40]
        # names repeat across workspaces ("Agent 1"), so two agents reporting
        # in the same second must not share a file
        path = os.path.join(folder, f"{stamp}-{safe or 'agent'}-"
                                    f"{uuid.uuid4().hex[:8]}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(payload, format=FORMAT), fh)
        os.replace(tmp, path)
        prune(folder)
        return path
    except Exception:
        return ""


def prune(folder: str, keep: int = KEEP) -> int:
    """Delete all but the newest `keep` dumps in `folder`. Returns how many
    went. Never raises."""
    try:
        dumps = [os.path.join(folder, n) for n in os.listdir(folder)
                 if n.endswith(".json")]
        dumps.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        return 0
    gone = 0
    for path in dumps[keep:]:
        try:
            os.remove(path)
            gone += 1
        except OSError:
            pass
    return gone


def load(path: str) -> dict:
    """A dump as written by `save`. Raises on a missing or broken file: the
    only caller is the replay tool, which should say so."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
