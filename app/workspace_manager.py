"""The model layer: workspaces and their agents. No widget imports.

Single source of truth for sidebar rows, badges, breadcrumbs, pages and
persistence. All mutation goes through WorkspaceManager methods, which
emit signals the UI reacts to — the API is dialog-free so headless tests
can drive the real app.
"""

import os
import re
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from . import coordination
from . import orchestration
from .process_worker import AgentKind, AgentSpec, build_spec
from .pty_worker import HAS_CONPTY
from .terminal_agent import AgentStatus, AssignmentState, TerminalAgent

MAX_AGENTS_PER_WORKSPACE = 12
SESSION_VERSION = 3  # v3: provider/model/effort, per-agent font, grid layout
DEFAULT_LAYOUT = "auto"

# Shell kinds whose pre-v2 (line-mode-default) instances are upgraded to
# interactive on load, so old sessions get the same working terminal as new
# ones. Custom/Python/Claude agents are never touched.
_MIGRATE_KINDS = {"powershell", "pwsh", "cmd"}

_AGENT_NAME_RE = re.compile(r"Agent (\d+)$")

_ACTIVE = {AgentStatus.RUNNING, AgentStatus.STARTING}
_ERROR = {AgentStatus.EXITED_ERR, AgentStatus.CRASHED, AgentStatus.FAILED}


@dataclass
class Workspace:
    id: str
    name: str
    project_path: str
    agents: list = field(default_factory=list)  # list[TerminalAgent]
    layout: str = DEFAULT_LAYOUT   # "auto" or "RxC" (e.g. "2x2")
    board: object = None           # coordination.WorkspaceBoard


class WorkspaceManager(QObject):
    workspaceAdded = Signal(object)          # Workspace
    workspaceRemoved = Signal(str)           # ws_id
    workspaceRenamed = Signal(str, str)      # ws_id, new name
    activeChanged = Signal(str)              # ws_id
    terminalAdded = Signal(str, object)      # ws_id, TerminalAgent
    terminalRemoved = Signal(str, str)       # ws_id, agent_id
    terminalCountChanged = Signal(str, int)  # ws_id, count
    workspaceStatsChanged = Signal(str, dict)  # ws_id, {total,active,idle,error}
    workspacePathChanged = Signal(str, str)  # ws_id, new project_path
    layoutChanged = Signal(str, str)         # ws_id, layout
    dirty = Signal()                         # any persistable mutation

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._workspaces: list[Workspace] = []
        self._active_id: str = ""
        # optional hook (set by MainWindow): arm an agent's MCP config before
        # it starts, so workers launch able to log_activity. callable(ws, agent)
        self.arm_agent = None

    # ------------------------------------------------------------- reads ---

    @property
    def workspaces(self) -> list[Workspace]:
        return list(self._workspaces)

    @property
    def active_id(self) -> str:
        return self._active_id

    def workspace(self, ws_id: str) -> Workspace | None:
        return next((w for w in self._workspaces if w.id == ws_id), None)

    def agent(self, ws_id: str, agent_id: str) -> TerminalAgent | None:
        ws = self.workspace(ws_id)
        if ws is None:
            return None
        return next((a for a in ws.agents if a.id == agent_id), None)

    def all_agents(self) -> list[TerminalAgent]:
        return [a for w in self._workspaces for a in w.agents]

    def workspace_stats(self, ws_id: str) -> dict:
        ws = self.workspace(ws_id)
        agents = ws.agents if ws else []
        active = sum(1 for a in agents if a.status in _ACTIVE)
        error = sum(1 for a in agents if a.status in _ERROR)
        # "busy" = actively streaming output (truly working), a subset of the
        # RUNNING agents — this is what the sidebar badge pulses green on, so an
        # agent merely idling at its prompt reads as standby, not working
        busy = sum(1 for a in agents if a.is_busy())
        return {"total": len(agents), "active": active, "error": error,
                "busy": busy, "idle": len(agents) - active - error}

    def next_agent_name(self, ws_id: str) -> str:
        """Per-workspace numbering: each workspace counts Agent 1, 2, 3…"""
        return self._next_numbered(ws_id, "Agent")

    def _next_numbered(self, ws_id: str, base: str) -> str:
        """A free name for 'base' in the workspace. 'Agent' keeps its historic
        monotonic 'Agent N' = max+1 numbering; role names go bare then ' 2'."""
        ws = self.workspace(ws_id)
        agents = ws.agents if ws else []
        taken = {a.spec.name for a in agents}
        if base == "Agent":
            highest = 0
            for name in taken:
                m = _AGENT_NAME_RE.fullmatch(name)
                if m:
                    highest = max(highest, int(m.group(1)))
            return f"Agent {highest + 1}"
        if base not in taken:
            return base
        n = 2
        while f"{base} {n}" in taken:
            n += 1
        return f"{base} {n}"

    def assign_role_name(self, ws_id: str, role: str) -> str:
        return self._next_numbered(ws_id, role or "Worker")

    def resolve_agent(self, ident: str):
        """Find an agent by id or (case-insensitive) display name, any ws."""
        for a in self.all_agents():
            if a.id == ident:
                return a
        low = (ident or "").strip().lower()
        for a in self.all_agents():
            if a.spec.name.lower() == low:
                return a
        return None

    def workspace_of(self, agent_id: str):
        for w in self._workspaces:
            if any(a.id == agent_id for a in w.agents):
                return w
        return None

    # ----------------------------------------------------------- mutation ---

    def create_workspace(self, name: str, project_path: str = "",
                         layout: str = DEFAULT_LAYOUT) -> Workspace:
        path = project_path or os.path.expanduser("~")
        ws = Workspace(id=uuid.uuid4().hex, name=name, project_path=path,
                       layout=layout or DEFAULT_LAYOUT,
                       board=coordination.WorkspaceBoard(path))
        self._workspaces.append(ws)
        self.workspaceAdded.emit(ws)
        self.dirty.emit()
        if not self._active_id:
            self.set_active(ws.id)
        return ws

    def set_workspace_path(self, ws_id: str, path: str) -> None:
        ws = self.workspace(ws_id)
        if ws is None or not path or ws.project_path == path:
            return
        old_path = ws.project_path
        ws.project_path = path
        ws.board = coordination.WorkspaceBoard(path)  # new folder, new board
        # keep existing agents pointed at the new folder + board: update the
        # cwd of agents that still followed the workspace (preserve custom
        # cwds), and re-inject coordination so Claude flags/system-prompt track
        # the new board. Takes effect on the next (re)start of each agent.
        for agent in ws.agents:
            if agent.spec.cwd == old_path:
                agent.spec.cwd = path
            self._apply_coordination(ws, agent)
        self.workspacePathChanged.emit(ws_id, path)
        self._recompute(ws_id)  # scaffold + roster the new board immediately
        self.dirty.emit()

    def set_layout(self, ws_id: str, layout: str) -> None:
        ws = self.workspace(ws_id)
        if ws is None or ws.layout == layout:
            return
        ws.layout = layout
        self.layoutChanged.emit(ws_id, layout)
        self.dirty.emit()

    def rename_workspace(self, ws_id: str, name: str) -> None:
        ws = self.workspace(ws_id)
        name = name.strip()
        if ws is None or not name or ws.name == name:
            return
        ws.name = name
        self.workspaceRenamed.emit(ws_id, name)
        self.dirty.emit()

    def remove_workspace(self, ws_id: str) -> None:
        ws = self.workspace(ws_id)
        if ws is None:
            return
        for agent in list(ws.agents):
            agent.dispose()
            agent.deleteLater()
        ws.agents.clear()
        self._workspaces.remove(ws)
        self.workspaceRemoved.emit(ws_id)
        if self._active_id == ws_id:
            self._active_id = ""
            if self._workspaces:
                self.set_active(self._workspaces[0].id)
        self.dirty.emit()

    def set_active(self, ws_id: str) -> None:
        if ws_id == self._active_id or self.workspace(ws_id) is None:
            return
        self._active_id = ws_id
        self.activeChanged.emit(ws_id)
        self.dirty.emit()

    def add_terminal(self, ws_id: str, spec: AgentSpec,
                     autostart: bool = True) -> TerminalAgent | None:
        ws = self.workspace(ws_id)
        if ws is None or len(ws.agents) >= MAX_AGENTS_PER_WORKSPACE:
            return None
        if not spec.cwd:
            spec.cwd = ws.project_path
        agent = TerminalAgent(spec, parent=self)  # model-owned, never a widget
        ws.agents.append(agent)
        self._wire_agent(ws, agent)
        self._apply_coordination(ws, agent)
        # arm MCP tools (worker log_activity / orchestrator full) BEFORE the
        # agent starts — a worker must launch WITH its config to log at all
        if self.arm_agent is not None:
            self.arm_agent(ws, agent)
        self.terminalAdded.emit(ws_id, agent)
        self.terminalCountChanged.emit(ws_id, len(ws.agents))
        self._recompute(ws_id)
        self.dirty.emit()
        if autostart:
            agent.start()
        return agent

    def remove_terminal(self, ws_id: str, agent_id: str) -> None:
        ws = self.workspace(ws_id)
        agent = self.agent(ws_id, agent_id)
        if ws is None or agent is None:
            return
        agent.dispose()
        ws.agents.remove(agent)
        self.terminalRemoved.emit(ws_id, agent_id)
        self.terminalCountChanged.emit(ws_id, len(ws.agents))
        self._recompute(ws_id)
        self.dirty.emit()
        agent.deleteLater()

    # ----------------------------------------------------- orchestration ---
    # Both the UI (card buttons / dialogs) and the MCP orchestrator call these
    # same methods, so behavior is identical with or without the orchestrator.

    def spawn_worker(self, ws_id: str, task: str, role: str = "",
                     model: str = "", effort: str = "",
                     auto_created: bool = True) -> TerminalAgent | None:
        """Create a NEW Claude worker with a role-based name + selected model,
        and give it a task. Returns None if the workspace is at its cap."""
        ws = self.workspace(ws_id)
        if ws is None:
            return None
        role = role or orchestration.infer_role(task)
        name = self.assign_role_name(ws_id, role)
        model, effort = orchestration.resolve_model_effort(task, role, model, effort)
        spec = build_spec(AgentKind.CLAUDE, name, role=role, cwd=ws.project_path,
                          model=model, effort=effort)
        agent = self.add_terminal(ws_id, spec, autostart=True)
        if agent is None:
            return None  # limit_reached — caller should reassign a completed one
        agent.auto_created = auto_created
        agent.set_assignment(AssignmentState.AWAITING)
        agent.deliver_task(task)  # queued until the TUI is prompt-ready
        # add_terminal's immediate save fired BEFORE auto_created/assignment/
        # task were set above — mark dirty so they persist too
        self.dirty.emit()
        return agent

    def assign_task(self, ws_id: str, agent_id: str, task: str,
                    role: str = "") -> bool:
        agent = self.agent(ws_id, agent_id) or self.resolve_agent(agent_id)
        if agent is None:
            return False
        wid = self.workspace_of(agent.id)
        wid = wid.id if wid else ws_id
        # rename to reflect the (new) role unless the user gave a custom name
        role = role or orchestration.infer_role(task)
        if role and (agent.auto_created or agent.spec.role):
            agent.set_role(self.assign_role_name(wid, role))
        agent.deliver_task(task)
        return True

    def reassign_agent(self, agent_id: str, task: str, role: str = "") -> bool:
        """Retask an idle/completed agent, preserving its session (no restart —
        the task is typed into the existing pty)."""
        agent = self.resolve_agent(agent_id)
        if agent is None:
            return False
        wid = self.workspace_of(agent.id)
        if wid is None:
            return False
        role = role or orchestration.infer_role(task)
        agent.set_role(self.assign_role_name(wid.id, role))
        if not agent.is_running():
            agent.start()
        agent.deliver_task(task)  # queued if (re)starting, else typed now
        return True

    def set_assignment_state(self, agent_id: str, state) -> bool:
        agent = self.resolve_agent(agent_id)
        if agent is None:
            return False
        agent.set_assignment(state)
        return True

    # ------------------------------------------------ stats + coordination ---

    def _wire_agent(self, ws: Workspace, agent: TerminalAgent) -> None:
        # model-side connections; agent is deleteLater'd on removal so these
        # auto-disconnect. ws.id (a str) is safe to capture. Every one of
        # these mutations is PERSISTED (running/task/assignment/role), so each
        # must also mark the session dirty — otherwise a reassignment or
        # completion is lost if the process dies before a graceful close.
        wid = ws.id
        agent.status_changed.connect(lambda *_: self._touch(wid))
        agent.task_changed.connect(lambda *_: self._touch(wid))
        agent.assignment_changed.connect(lambda *_: self._touch(wid))
        agent.role_changed.connect(lambda *_: self._touch(wid))
        agent.font_changed.connect(lambda *_: self.dirty.emit())
        # busy/standby is TRANSIENT (not persisted): refresh the badge only,
        # never mark dirty — otherwise every output burst would thrash saves
        agent.activity_changed.connect(lambda *_: self._recompute(wid))

    def _touch(self, ws_id: str) -> None:
        """Recompute derived state AND mark the session dirty (persisted
        fields changed)."""
        self._recompute(ws_id)
        self.dirty.emit()

    def _apply_coordination(self, ws: Workspace, agent: TerminalAgent) -> None:
        """Give AI agents access to the shared workspace board (peer
        awareness) via --add-dir; Claude additionally gets the board etiquette
        appended to its system prompt (agy has no system-prompt flag — Gemini
        agents rely on the project's context files to mention the board)."""
        if agent.spec.provider not in ("claude", "gemini") or ws.board is None:
            return
        if not ws.board.ensure():  # project dir missing / read-only
            return
        agent.spec.extra_dirs = [ws.board.dir]
        if agent.spec.provider == "claude":
            # role-aware: orchestrators get the team-management prompt, workers
            # the peer-etiquette prompt (must not clobber an orchestrator's
            # prompt with the worker one)
            if agent.spec.is_orchestrator:
                agent.spec.system_prompt = coordination.orchestrator_prompt_text(
                    ws.name, ws.board.path)
            else:
                agent.spec.system_prompt = coordination.system_prompt_text(
                    ws.name, agent.spec.name, ws.board.path)

    def _recompute(self, ws_id: str) -> None:
        ws = self.workspace(ws_id)
        if ws is None:
            return
        self.workspaceStatsChanged.emit(ws_id, self.workspace_stats(ws_id))
        # refresh the shared board roster only when the board is in use.
        # Best-effort by definition: NO board problem (encoding, locks,
        # permissions) may ever propagate — _recompute runs during session
        # load, and an exception here once killed the app at startup.
        board = ws.board
        if board is not None and (
                os.path.isfile(board.path)
                or any(a.spec.provider in ("claude", "gemini")
                       for a in ws.agents)):
            try:
                board.update_roster([a.roster_row() for a in ws.agents])
            except Exception:
                pass

    # -------------------------------------------------------- persistence ---

    def _agent_dict(self, a) -> dict:
        # auto-created agents (orchestrator/workers) keep their "resume on next
        # open" intent even if the process momentarily died — otherwise a
        # crashed/failed auto-agent silently goes dormant and never comes back.
        # User-created agents persist their live running state as before.
        return {**a.spec.to_dict(),
                "running": (a.is_running()
                            or (a.auto_created and a.autostart_on_restore)),
                "task": a.current_task,
                "assignment": a.assignment.value,
                "auto_created": a.auto_created}

    def _agent_dict_safe(self, a) -> dict | None:
        """Serialize one agent, but NEVER let a single malformed agent abort the
        whole session save: one un-serializable agent must not take every other
        workspace's agents down with it (that is how a folder's agents once
        vanished from disk with nothing in the log). Fall back to a minimal
        entry that still preserves identity + resume so the agent survives; if
        even that fails, drop just this one and keep going."""
        try:
            return self._agent_dict(a)
        except Exception:
            try:
                spec = a.spec
                return {"kind": getattr(spec, "kind", ""),
                        "name": getattr(spec, "name", "Agent"),
                        "cwd": getattr(spec, "cwd", ""),
                        "provider": getattr(spec, "provider", ""),
                        "session_id": getattr(spec, "session_id", ""),
                        "running": True}
            except Exception:
                return None

    def to_session_dict(self) -> dict:
        return {
            "version": SESSION_VERSION,
            "active": self._active_id,
            "workspaces": [
                {
                    "id": w.id,
                    "name": w.name,
                    "project_path": w.project_path,
                    "layout": w.layout,
                    "terminals": [d for d in (self._agent_dict_safe(a)
                                              for a in w.agents)
                                  if d is not None],
                }
                for w in self._workspaces
            ],
        }

    def load_session_dict(self, data: dict) -> None:
        """Rebuild workspaces/agents from a session dict. Agents are created
        idle (lazy restore) — the caller decides what to auto-start."""
        if not data or not isinstance(data, dict):
            return
        # one-time upgrade of pre-v2 shell agents (saved as line-mode when
        # that was the default) to interactive terminals
        migrate_shells = data.get("version", 1) < SESSION_VERSION and HAS_CONPTY
        for wd in data.get("workspaces", []):
            path = wd.get("project_path", "")
            if not path or not os.path.isdir(path):
                path = os.path.expanduser("~")
            ws = Workspace(id=wd.get("id") or uuid.uuid4().hex,
                           name=wd.get("name", "Workspace"),
                           project_path=path,
                           layout=wd.get("layout", DEFAULT_LAYOUT),
                           board=coordination.WorkspaceBoard(path))
            self._workspaces.append(ws)
            self.workspaceAdded.emit(ws)
            for td in wd.get("terminals", [])[:MAX_AGENTS_PER_WORKSPACE]:
                if migrate_shells and td.get("kind") in _MIGRATE_KINDS:
                    td = {**td, "pty": True}
                try:
                    spec = AgentSpec.from_dict(td)
                except Exception:
                    continue
                spec.cwd = spec.cwd if os.path.isdir(spec.cwd) else ws.project_path
                agent = TerminalAgent(spec, parent=self)
                agent.current_task = td.get("task", "")
                agent.auto_created = bool(td.get("auto_created", False))
                try:
                    agent.assignment = AssignmentState(td.get("assignment", "idle"))
                except ValueError:
                    agent.assignment = AssignmentState.IDLE
                agent.autostart_on_restore = bool(td.get("running", False))
                ws.agents.append(agent)
                self._wire_agent(ws, agent)
                self._apply_coordination(ws, agent)
                self.terminalAdded.emit(ws.id, agent)
            self.terminalCountChanged.emit(ws.id, len(ws.agents))
            self._recompute(ws.id)
        active = data.get("active", "")
        if self.workspace(active) is not None:
            self.set_active(active)
        elif self._workspaces:
            self.set_active(self._workspaces[0].id)
