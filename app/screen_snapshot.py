"""Persisted terminal screens, so a stopped agent reopens showing its
conversation instead of a black rectangle.

A restored agent that was left stopped stays stopped on purpose (starting it
would relaunch a real `claude.exe` and let a resumed session act on stale
context). But "stopped" used to mean the card came back as a dead black
screen with a "terminal not running" banner over it, which loses the one
thing the user actually wants back on reopen: the conversation that was
there. This module keeps the RAW VT TAIL each pty agent had on screen at
close and hands it back at launch, so the card is repainted exactly as it was
left, with no process behind it.

Why the raw VT stream rather than the Claude transcript on disk: the
transcript is a semantic record (`transcripts.py` already backs those up),
and re-rendering it would produce something that does not look like the TUI.
The bytes the child actually wrote replay through the same pyte screen that
drew them the first time, so what comes back IS what was there, mid-frame
artwork and all.

The snapshot is NOT session state. Nothing here goes in `session.json` (a
half-megabyte of escape codes per agent would dwarf it and put binary-ish
noise in a file whose integrity everything else depends on) — snapshots are
plain files under `<session-dir>/screens/`, the same shape as the transcript
backups next door.

Identity is (cwd, pinned session id), NOT the agent id — `TerminalAgent.id`
is minted fresh on every load, so it cannot key anything that must survive a
restart (this is the same rule `limit_ledger.key_of` follows). Keying on the
conversation also makes staleness self-correcting: if the pin moved on (the
user switched conversations in the TUI, a fork), the old snapshot simply no
longer matches and the card comes back empty rather than showing a screen
belonging to a different chat.

Qt-free and stdlib-only, like `chime.py` / `limit_banner.py` / `limit_ledger.py`.
"""

from __future__ import annotations

import hashlib
import os

# Matches TerminalAgent.PTY_BUFFER_CAP: the tail we keep is the tail that gets
# replayed, so writing more than the agent would ever hold back is pointless.
MAX_BYTES = 512 * 1024

DIR_NAME = "screens"


def key_of(cwd: str, session_id: str) -> str:
    """Filesystem-safe identity for one agent's screen.

    Hashed rather than composed from the path because a cwd is arbitrary text
    (drive letters, spaces, non-ASCII) and this becomes a file name.
    """
    raw = f"{os.path.normcase(os.path.abspath(cwd or ''))}\x00{session_id or ''}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _path(dir_path: str, key: str) -> str:
    return os.path.join(dir_path, DIR_NAME, f"{key}.vt")


def save(dir_path: str, cwd: str, session_id: str, text: str) -> bool:
    """Write one agent's screen tail. Never raises: a failed snapshot is a
    cosmetic loss on the next launch, and this runs during close, right
    alongside the save path that must not be disturbed."""
    if not session_id or not text:
        return False
    try:
        target = _path(dir_path, key_of(cwd, session_id))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tail = text[-MAX_BYTES:]
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="replace",
                  newline="") as fh:
            fh.write(tail)
        os.replace(tmp, target)  # atomic: never leave a half-written screen
        return True
    except Exception:
        return False


def load(dir_path: str, cwd: str, session_id: str) -> str:
    """Return the saved screen for this conversation, or "" if there is none.
    Never raises — a missing or unreadable snapshot just means an empty card,
    which is exactly the old behavior."""
    if not session_id:
        return ""
    try:
        with open(_path(dir_path, key_of(cwd, session_id)),
                  encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()
    except Exception:
        return ""


def save_for_agents(agents, dir_path: str) -> int:
    """Snapshot every pty agent that has something on screen. Returns the
    count written. Called at close, while the agents are still alive."""
    written = 0
    for agent in agents or ():
        try:
            if not getattr(agent, "is_pty", False):
                continue
            spec = agent.spec
            if save(dir_path, getattr(spec, "cwd", ""),
                    getattr(spec, "session_id", ""), agent.pty_replay()):
                written += 1
        except Exception:
            continue  # one bad agent must never cost the others their screens
    return written


def prune(dir_path: str, keep_keys) -> int:
    """Delete snapshots no live agent claims any more (closed cards, and
    conversations an agent has since switched away from). Returns the count
    removed. Without this the directory would grow without bound, since every
    `/clear` in the TUI mints a new conversation and therefore a new key."""
    keep = set(keep_keys or ())
    removed = 0
    try:
        root = os.path.join(dir_path, DIR_NAME)
        for name in os.listdir(root):
            if not name.endswith(".vt"):
                continue
            if name[:-3] in keep:
                continue
            try:
                os.remove(os.path.join(root, name))
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def keys_for_agents(agents) -> set:
    """The keys the given agents currently claim (for `prune`)."""
    keys = set()
    for agent in agents or ():
        try:
            spec = agent.spec
            sid = getattr(spec, "session_id", "")
            if sid:
                keys.add(key_of(getattr(spec, "cwd", ""), sid))
        except Exception:
            continue
    return keys
