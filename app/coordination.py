"""Per-workspace shared coordination board.

Each workspace gets `<project>/.aihive/board.md`. AI Hive owns and regenerates
the `## Agents` roster block from live agent state; agents append to the
`## Activity log` section below a marker. Claude agents are launched with
`--add-dir <.aihive>` and an appended system prompt (system_prompt_text) that
tells them to read the board for peer awareness and post their own updates —
so agents in the same workspace coordinate and avoid duplicate work. The
board lives inside the workspace folder, so different workspaces are isolated.

Qt-free (pure filesystem) so the model layer and tests can use it headlessly.
"""

import datetime
import os
import subprocess

BOARD_DIRNAME = ".aihive"
BOARD_FILENAME = "board.md"
ROSTER_BEGIN = "<!-- AIHIVE:ROSTER:BEGIN -->"
ROSTER_END = "<!-- AIHIVE:ROSTER:END -->"
LOG_HEADER = "## Activity log"

_STATUS_ICON = {
    "running": "🟢", "starting": "🟡", "idle": "⚪", "stopping": "🟡",
    "exited": "⚪", "exited-err": "🔴", "crashed": "🔴", "failed": "🔴",
}


def system_prompt_text(workspace_name: str, agent_name: str,
                       board_path: str, lane: dict | None = None,
                       aware: bool = False, integrator: bool = False) -> str:
    """The board etiquette every Claude agent gets, plus a lane section for
    an agent with its own git worktree (app/lanes.py). The board path is
    always the workspace's own absolute one: a lane has no board of its own.

    `aware`: the lane machinery runs for this agent (the Agent lanes switch
    is on, so its overlap hooks are armed). Only then does a laned agent
    swap "read the board first" for "skim the roster": AI Hive tells it
    about a real overlap when one exists, and reading every note is the
    token cost the feature exists to remove. Every other agent keeps the
    full instruction, because the board is its only awareness.

    `integrator`: this laned agent is the workspace's integrator
    (app/integration.py). It owns the version, CHANGELOG and README count
    that every other lane is told to leave alone, and it never merges."""
    laned = bool(lane and lane.get("root") and lane.get("branch"))
    if laned and aware:
        read = ("Before starting substantial work, skim the roster at the "
                "top of it to see who is working on what. You do not need "
                "to read the whole log: AI Hive tells you when another "
                "agent's lane or the base branch changes a file you changed "
                "(an \"AI Hive lane notice\" in your context), so act on "
                "those notices.")
    else:
        read = ("BEFORE starting substantial work, read it to see what the "
                "other agents are doing and what is already done, so you "
                "avoid duplicating their work.")
    text = (
        f"You are the agent \"{agent_name}\", one of several AI Hive agents "
        f"working together in the \"{workspace_name}\" workspace. A shared "
        f"coordination board is at {board_path}. {read} When you start a "
        f"task, finish one, or change an important file, record it by calling "
        f"the `log_activity` MCP tool with a terse one-line message (e.g. "
        f"\"implementing auth in login.py\"). Do NOT edit board.md directly; "
        f"AI Hive serializes those writes through the tool so concurrent agents "
        f"can't clobber each other's entries.")
    if laned and integrator:
        base = lane.get("base") or "the base branch"
        text += (
            f" You are this workspace's INTEGRATOR, working in your own git "
            f"worktree at {lane['root']}. When the user submits another "
            f"agent's lane, AI Hive types an integration brief into your "
            f"prompt. For each brief: create the integrate/ branch it names "
            f"from the pinned commit, merge {base} into it, resolve "
            f"conflicts, run the full test suite, bump the version and the "
            f"CHANGELOG and README count once for that pull request, push, "
            f"and open the pull request with gh pr create, then end your "
            f"turn. Never rebase or force-push, never edit other worktrees or "
            f"the main checkout at {lane.get('repo', '')}, never commit to "
            f"{base} directly, and NEVER run gh pr merge: the user approves "
            f"every merge in AI Hive.")
    elif laned:
        base = lane.get("base") or "the base branch"
        text += (
            f" You work in your own git worktree (your lane) at {lane['root']}, "
            f"on the branch {lane['branch']}, which starts from {base}. Commit "
            f"your work there, on that branch. Never edit files in other "
            f"worktrees or in the main checkout at {lane.get('repo', '')} "
            f"(reading its board is fine), and never commit to {base}. Do not "
            f"bump the version or edit the CHANGELOG or the README check "
            f"count: that is done once per pull request, when your work is "
            f"integrated.")
    return text


def _with_lane_columns(lines: list, rows: list) -> list:
    """Add Lane, Ahead/Dirty and Touching to a rendered roster table, for a
    workspace with laned agents (app/lanes.py). A laned agent is told to
    skim this roster instead of reading the log, so the columns say who is
    changing what. Without lanes the table keeps its old shape."""
    head, sep, body = lines[:2], lines[2:4], lines[4:]
    out = head + [sep[0] + " Lane | Ahead/Dirty | Touching |",
                  sep[1] + "---|---|---|"]
    for line, r in zip(body, rows):
        cells = [(r.get(k) or "").replace("|", "/").strip() or "-"
                 for k in ("lane", "ahead_dirty", "touching")]
        out.append(line + " " + " | ".join(cells) + " |")
    return out


def sanitize_text(text: str) -> str:
    """Drop lone UTF-16 surrogates from a string. JSON (session files, MCP
    tool calls) legally carries them, but strict UTF-8 files and pipes REFUSE
    to encode them — an unsanitized task string once crashed the app at
    startup while rewriting the board roster."""
    try:
        return text.encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return text


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
                f"{LOG_HEADER}\n\n_Agents append their activity below._\n")

    def update_roster(self, rows: list[dict]) -> bool:
        """rows: [{name, role, provider, model, status, task}]. Rewrites only
        the roster block, preserving the agent-written activity log."""
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
        if ROSTER_BEGIN in text and ROSTER_END in text:
            head, rest = text.split(ROSTER_BEGIN, 1)
            _, tail = rest.split(ROSTER_END, 1)
            text = f"{head}{ROSTER_BEGIN}\n{block}\n{ROSTER_END}{tail}"
        else:  # board lost its markers; rebuild scaffold, keep any log
            text = self._scaffold().replace(
                f"{ROSTER_BEGIN}\n{ROSTER_END}",
                f"{ROSTER_BEGIN}\n{block}\n{ROSTER_END}")
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
            task = (r.get("task") or "").replace("|", "/") or "-"
            lines.append(f"| {r.get('name','?')} | {r.get('role','')} | "
                         f"{model} | {icon} {r.get('status','')} | {task} |")
        if any(r.get("lane") for r in rows):
            lines = _with_lane_columns(lines, rows)
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
        end of the file, after all prior log content."""
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
        if LOG_HEADER not in text:            # board lost its log section
            text = self._scaffold().rstrip("\n") + "\n"
        text = text.rstrip("\n") + "\n" + entry + "\n"
        return self._write(text)

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
