"""Per-workspace shared coordination board.

Each workspace gets `<project>/.aihive/board.md`. AI Hive owns and regenerates
the `## Agents` roster block from live agent state; agents append to the
`## Activity log` section below a marker. Claude agents are launched with
`--add-dir <.aihive>` and an appended system prompt (system_prompt_text) that
tells them to read the board for peer awareness and post their own updates —
so agents in the same workspace coordinate and avoid duplicate work. The
board lives inside the workspace folder, so different workspaces are isolated.

The board stays small. Every agent reads it before substantial work, so its
size is paid in tokens on every task: an unrotated board reached ~80 KB. Each
append keeps the newest LOG_KEEP entries in board.md and moves older ones to
board-archive.md next to it. The archive is append-only and written BEFORE the
board, so a rotation can never lose an entry; a failed board write truncates
the archive back, so it doesn't leave a copy in both files either.

Qt-free (pure filesystem) so the model layer and tests can use it headlessly.
"""

import datetime
import os
import subprocess

BOARD_DIRNAME = ".aihive"
BOARD_FILENAME = "board.md"
ARCHIVE_FILENAME = "board-archive.md"
ROSTER_BEGIN = "<!-- AIHIVE:ROSTER:BEGIN -->"
ROSTER_END = "<!-- AIHIVE:ROSTER:END -->"
LOG_HEADER = "## Activity log"
LOG_KEEP = 40   # activity-log entries kept in board.md, newest last
# "only if": every agent reads the board before a task, and an agent told
# where the history is tends to go and read it, paying the tokens the archive
# exists to save
ARCHIVE_NOTE = (f"_Older entries are moved to {ARCHIVE_FILENAME} in this "
                f"folder. Read it only if you need history._")
ARCHIVE_HEADER = ("# AI Hive: board archive\n\n"
                  "Activity-log entries moved out of board.md, oldest first. "
                  "AI Hive only ever appends to this file.\n\n")

_STATUS_ICON = {
    "running": "🟢", "starting": "🟡", "idle": "⚪", "stopping": "🟡",
    "exited": "⚪", "exited-err": "🔴", "crashed": "🔴", "failed": "🔴",
}


def system_prompt_text(workspace_name: str, agent_name: str,
                       board_path: str) -> str:
    return (
        f"You are the agent \"{agent_name}\", one of several AI Hive agents "
        f"working together in the \"{workspace_name}\" workspace. A shared "
        f"coordination board is at {board_path}. BEFORE starting substantial "
        f"work, read it to see what the other agents are doing and what is "
        f"already done, so you avoid duplicating their work. When you start a "
        f"task, finish one, or change an important file, record it by calling "
        f"the `log_activity` MCP tool with a terse one-line message (e.g. "
        f"\"implementing auth in login.py\"). Do NOT edit board.md directly; "
        f"AI Hive serializes those writes through the tool so concurrent agents "
        f"can't clobber each other's entries.")


def sanitize_text(text: str) -> str:
    """Drop lone UTF-16 surrogates from a string. JSON (session files, MCP
    tool calls) legally carries them, but strict UTF-8 files and pipes REFUSE
    to encode them — an unsanitized task string once crashed the app at
    startup while rewriting the board roster."""
    try:
        return text.encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return text


def split_log(text: str, keep: int = LOG_KEEP) -> tuple[str, list[str]]:
    """Split board text into (the board keeping its newest `keep` log
    entries, the lines moved out of it, oldest first). Nothing to move gives
    (text, []).

    An entry is a top-level "- " line plus every line under it up to the next
    one, so an entry an agent wrote by hand over several lines moves whole.
    The roster and the log's preamble (the lines above its first entry) stay.
    Every input line lands in exactly one output; the only line added is
    ARCHIVE_NOTE, put in the preamble the first time anything moves."""
    if LOG_HEADER not in text:
        return text, []
    head, log = text.split(LOG_HEADER, 1)
    # lines[0] is the rest of the header's own line, never an entry
    lines = log.split("\n")
    starts = [i for i, ln in enumerate(lines) if i and ln.startswith("- ")]
    keep = max(1, keep)
    if len(starts) <= keep:
        return text, []
    first, cut = starts[0], starts[-keep]
    preamble = lines[:first]
    if not any(ARCHIVE_FILENAME in ln for ln in preamble):
        last = max((i for i, ln in enumerate(preamble) if i and ln.strip()),
                   default=0)
        preamble.insert(last + 1, ARCHIVE_NOTE)
    kept = head + LOG_HEADER + "\n".join(preamble + lines[cut:])
    return kept, lines[first:cut]


class WorkspaceBoard:
    """Owns board.md for one workspace: roster (app-written) + log (agents)."""

    def __init__(self, project_path: str):
        self.project_path = project_path

    @property
    def dir(self) -> str:
        return os.path.join(self.project_path, BOARD_DIRNAME)

    @property
    def path(self) -> str:
        return os.path.join(self.dir, BOARD_FILENAME)

    @property
    def archive_path(self) -> str:
        return os.path.join(self.dir, ARCHIVE_FILENAME)

    def ensure(self) -> bool:
        """Create the board scaffold if missing. Returns False on failure
        (e.g. read-only or non-existent project dir)."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            if not os.path.isfile(self.path):
                self._write(self._scaffold())
            return True
        except OSError:
            return False

    def _scaffold(self) -> str:
        return (f"# AI Hive: workspace coordination board\n\n"
                f"{ROSTER_BEGIN}\n{ROSTER_END}\n\n"
                f"{LOG_HEADER}\n\n_Agents append their activity below._\n"
                f"{ARCHIVE_NOTE}\n")

    def update_roster(self, rows: list[dict]) -> bool:
        """rows: [{name, role, provider, model, status, task}]. Rewrites only
        the roster block, preserving the agent-written activity log.

        The manager calls this on every status or title tick, so an unchanged
        roster is not written again: board.md is replaced only when its
        roster block actually differs.

        A board whose markers were lost (an agent edited it by hand) gets the
        roster back at the top, and every other line is kept. Rebuilding the
        scaffold here once dropped the whole activity log."""
        if not self.ensure():
            return False
        try:
            # tolerant read: a foreign agent writing mangled bytes to the
            # board must degrade its own entry, never break the roster
            with open(self.path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, UnicodeError):
            return False
        block = self._render_roster(rows)
        fresh = f"{ROSTER_BEGIN}\n{block}\n{ROSTER_END}"
        if ROSTER_BEGIN in text and ROSTER_END in text.split(ROSTER_BEGIN, 1)[1]:
            head, rest = text.split(ROSTER_BEGIN, 1)
            current, tail = rest.split(ROSTER_END, 1)
            if current == f"\n{block}\n":
                return True
            text = f"{head}{fresh}{tail}"
        elif not text.strip():
            text = self._scaffold().replace(f"{ROSTER_BEGIN}\n{ROSTER_END}",
                                            fresh)
        else:
            # a lone marker is AI Hive's own line, not content: drop it, or
            # the next split would pair it with the wrong partner
            body = "\n".join(ln for ln in text.split("\n")
                             if ln.strip() not in (ROSTER_BEGIN, ROSTER_END))
            if body.startswith("# "):     # keep the title line on top
                title, _, rest = body.partition("\n")
                rest = rest.lstrip("\n")
                text = f"{title}\n\n{fresh}\n\n{rest}"
            else:
                text = f"{fresh}\n\n{body}"
        return self._write(text)

    def _render_roster(self, rows: list[dict]) -> str:
        if not rows:
            return "## Agents\n\n_No agents yet._"
        lines = ["## Agents", "",
                 "| Agent | Role | Model | Status | Current task |",
                 "|---|---|---|---|---|"]
        for r in rows:
            icon = _STATUS_ICON.get(r.get("status", ""), "⚪")
            model = r.get("model") or r.get("provider") or ""
            # one line: a delivered task can span several, which would end
            # the table row early
            task = " ".join((r.get("task") or "").split()).replace("|", "/") or "-"
            lines.append(f"| {r.get('name','?')} | {r.get('role','')} | "
                         f"{model} | {icon} {r.get('status','')} | {task} |")
        return "\n".join(lines)

    def _write(self, text: str) -> bool:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", errors="replace") as f:
                f.write(sanitize_text(text))
            os.replace(tmp, self.path)
            return True
        except (OSError, UnicodeError):
            return False

    def append_activity(self, agent_name: str, message: str) -> bool:
        """Append one dated activity line to the ## Activity log section, via
        the app's serialized atomic write. Agents call this through the
        log_activity MCP tool instead of editing board.md directly, so
        concurrent writers can't interleave or clobber each other. The roster
        block (above the log) is never touched — the entry always lands at the
        end of the file, after all prior log content.

        The rotation to the archive happens here too, so it is serialized by
        the same thing that serializes the append: every caller (the bridge's
        log_activity executor, the auto-continue note) runs on the GUI
        thread."""
        message = " ".join(sanitize_text(message or "").split())  # one clean line
        name = sanitize_text(agent_name or "agent").strip() or "agent"
        if not message:
            return False
        if not self.ensure():
            return False
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, UnicodeError):
            return False
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{name}] {stamp} {message}"
        if not text.strip():
            text = self._scaffold()
        elif LOG_HEADER not in text:
            # the board lost its log heading (edited by hand): start a fresh
            # log at the end and keep everything above it. Replacing the file
            # with the scaffold here once dropped the whole board.
            text = text.rstrip("\n") + f"\n\n{LOG_HEADER}\n"
        text = text.rstrip("\n") + "\n" + entry + "\n"
        return self._write_rotated(text)

    def _write_rotated(self, text: str) -> bool:
        """Write the board keeping only its newest LOG_KEEP entries, after the
        older ones are safely in the archive. A rotation that can't reach the
        archive is skipped, never the append: the board just stays longer
        until the next one."""
        kept, moved = split_log(text)
        if not moved:
            return self._write(text)
        size = self._archive_append(moved)
        if size is None:
            return self._write(text)
        if self._write(kept):
            return True
        self._archive_truncate(size)   # the board still holds those entries
        return False

    def _archive_append(self, lines: list[str]) -> int | None:
        """Append `lines` to the archive, with its header when the file is new.
        Returns the archive's size before the append (what _archive_truncate
        restores), or None when nothing was appended."""
        path = self.archive_path
        try:
            size = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            return None
        body = "\n".join(lines) + "\n"
        if size == 0:
            body = ARCHIVE_HEADER + body
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(sanitize_text(body))
                f.flush()
                # on disk before board.md is replaced without these lines
                os.fsync(f.fileno())
            return size
        except (OSError, UnicodeError):
            self._archive_truncate(size)   # never leave half an append behind
            return None

    def _archive_truncate(self, size: int) -> None:
        try:
            with open(self.archive_path, "r+b") as f:
                f.truncate(size)
        except OSError:
            pass   # the entries are then in both files: duplicated, not lost

    def read_log_tail(self, max_lines: int = 40) -> list[str]:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, UnicodeError):
            return []
        if LOG_HEADER not in text:
            return []
        log = text.split(LOG_HEADER, 1)[1]
        entries = [ln.strip() for ln in log.splitlines()
                   if ln.strip().startswith("- ")]
        return entries[-max_lines:]


def git_changed_files(project_path: str, limit: int = 30,
                      timeout: float = 1.5) -> list[str]:
    """Best-effort repo-wide changed-file list (not per-agent attribution).

    NOTE: this spawns `git` and blocks — call it off the GUI thread's hot
    path (e.g. only from a slow timer, never on every status change).
    """
    from itertools import islice
    try:
        out = subprocess.run(
            ["git", "-C", project_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        if out.returncode != 0:
            return []
        # cap before materializing (avoid holding a huge untracked tree in a list)
        rows = (ln.rstrip() for ln in out.stdout.splitlines() if ln.strip())
        return list(islice(rows, limit))
    except (OSError, subprocess.SubprocessError):
        return []
