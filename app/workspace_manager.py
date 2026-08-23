"""The model layer: workspaces and their agents. No widget imports.

Single source of truth for sidebar rows, badges, breadcrumbs, pages and
persistence. All mutation goes through WorkspaceManager methods, which
emit signals the UI reacts to — the API is dialog-free so headless tests
can drive the real app.
"""

import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from . import coordination
from . import orchestration
from . import providers
from . import session_hook
from . import session_sync
from . import transcripts
from .process_worker import AgentKind, AgentSpec, build_spec
from .pty_worker import HAS_CONPTY
from .terminal_agent import AgentStatus, AssignmentState, TerminalAgent

MAX_AGENTS_PER_WORKSPACE = 12
SESSION_VERSION = 4  # v4: sidebar layout (workspace order + categories)
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
    sidebarLayoutChanged = Signal()          # workspace order / categories
    # rising edge of an agent's waiting-for-user state (standby -> waiting):
    # the "?" just appeared on its row. Transient, never persisted — the UI
    # uses it purely to sound the notification chime.
    agentWaiting = Signal(str, str)          # ws_id, agent_id
    agentLimitBlocked = Signal(str, str)     # ws_id, agent_id (plan cut-off)
    dirty = Signal()                         # any persistable mutation

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._workspaces: list[Workspace] = []
        # sidebar layout: ordered top-level nodes (workspaces + categories with
        # workspace children). Empty => all-uncategorized in _workspaces order.
        self._sidebar_nodes: list[dict] = []
        self._active_id: str = ""
        # optional hook (set by MainWindow): arm an agent's MCP config before
        # it starts, so workers launch able to log_activity. callable(ws, agent)
        self.arm_agent = None
        # optional hook (set by MainWindow): append a line to session.log.
        # callable(str). The manager has no SessionStore of its own, and
        # _agent_dict_safe's degrade path is exactly the kind of failure that
        # must not happen in silence. Same set-by-MainWindow shape as
        # `arm_agent`, and unset is a no-op so a bare manager (the tests build
        # many) never depends on it.
        self.audit = None
        # path to the shared SessionStart-hook mapping file (set by MainWindow);
        # sync_live_sessions reads it for the authoritative live conversation id
        self.session_map_path = ""
        # path to the shared prompt-events file (set by MainWindow); the Claude
        # PreToolUse/PostToolUse/Stop hooks append "needs the user" EDGES here,
        # which sync_prompt_events reads incrementally to drive each agent's
        # waiting state (the "?" badge + chime). _prompt_offset marks how far we
        # have consumed, so a stale edge is never re-applied after a local clear.
        self.prompt_events_path = ""
        self._prompt_offset = 0
        # optional immediate-save hook (set by MainWindow to _save_now): for
        # mutations that must persist NOW rather than on the dirty debounce
        self.save_now = None

    def _persist_now(self) -> None:
        """Persist immediately if a hook is wired, else fall back to the
        debounced dirty save. Used where a debounce window would be a loss
        window (a hard kill before the heartbeat)."""
        if self.save_now is not None:
            self.save_now()
        else:
            self.dirty.emit()

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
        # "waiting" = settled on a prompt/question awaiting the user (a subset,
        # drives the row's "?" indicator); transient like busy, never persisted
        waiting = sum(1 for a in agents if a.is_waiting())
        # "limit_blocked" = cut off by the plan usage limit and not yet resumed
        # (auto-continue or manual); drives the row's hourglass count, mirroring
        # the "?" indicator above
        limit_blocked = sum(1 for a in agents if a.is_limit_blocked())
        # "scheduled" = holding at least one deferred message (pending, or one
        # that was missed and is still waiting to be dealt with); drives the
        # row's countdown badge, mirroring the two indicators above
        scheduled = sum(1 for a in agents if a.scheduled_messages())
        # "bg_shell" = quiet (not busy) but a background command it started
        # (a Bash run_in_background call, a shell's `cmd &`) is still running;
        # drives the row's gear badge, mirroring the three indicators above
        bg_shell = sum(1 for a in agents if a.is_bg_shell_busy())
        return {"total": len(agents), "active": active, "error": error,
                "busy": busy, "waiting": waiting, "limit_blocked": limit_blocked,
                "scheduled": scheduled, "bg_shell": bg_shell,
                "idle": len(agents) - active - error}

    def next_agent_name(self, ws_id: str) -> str:
        """Per-workspace numbering: each workspace counts Agent 1, 2, 3…,
        monotonically (max + 1), so a name is never reused after a delete.

        This used to be one half of a generic _next_numbered(base); the other
        half minted role names ("Testing Agent 2") for the task-to-role
        heuristic that has since been removed, and "Agent" is now the only base
        there is.
        """
        ws = self.workspace(ws_id)
        agents = ws.agents if ws else []
        highest = 0
        for a in agents:
            m = _AGENT_NAME_RE.fullmatch(a.spec.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return f"Agent {highest + 1}"

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

    def reorder_workspaces(self, ordered_ids: list) -> None:
        """Re-sequence workspaces to match the sidebar's new top-to-bottom
        order. Order is persisted implicitly by list position in
        `to_session_dict`; ids not present keep their relative tail order."""
        rank = {wid: i for i, wid in enumerate(ordered_ids)}
        before = [w.id for w in self._workspaces]
        self._workspaces.sort(key=lambda w: rank.get(w.id, len(rank)))
        if [w.id for w in self._workspaces] != before:
            self.sidebarLayoutChanged.emit()

    def reorder_agents(self, ws_id: str, ordered_ids: list) -> None:
        """Re-sequence a workspace's agents to match a drag-reorder of its
        cards. Agent order is persisted implicitly by list position in
        `to_session_dict` (and restored in that order), so this just re-sorts
        `ws.agents` and marks the session dirty. Ids not present keep their
        relative tail order — a robust no-loss reconcile like reorder_workspaces."""
        ws = self.workspace(ws_id)
        if ws is None:
            return
        rank = {aid: i for i, aid in enumerate(ordered_ids)}
        before = [a.id for a in ws.agents]
        ws.agents.sort(key=lambda a: rank.get(a.id, len(rank)))
        if [a.id for a in ws.agents] != before:
            self.dirty.emit()

    # -------------------------------------------------- sidebar layout ---

    def _normalize_layout(self, nodes: list) -> list:
        """Validate a sidebar layout against live workspaces: drop unknown ids,
        de-dupe, and append any unplaced workspace uncategorized at the end."""
        known = {w.id for w in self._workspaces}
        seen: set[str] = set()
        out: list[dict] = []
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            if n.get("type") == "workspace":
                wid = n.get("id")
                if wid in known and wid not in seen:
                    seen.add(wid)
                    out.append({"type": "workspace", "id": wid})
            elif n.get("type") == "category":
                kids = [c for c in n.get("children", [])
                        if c in known and c not in seen]
                seen.update(kids)
                out.append({"type": "category",
                            "id": n.get("id") or uuid.uuid4().hex,
                            "name": str(n.get("name", "Category")),
                            "collapsed": bool(n.get("collapsed", False)),
                            "children": kids})
        for w in self._workspaces:
            if w.id not in seen:
                out.append({"type": "workspace", "id": w.id})
        return out

    @staticmethod
    def _flatten_ws_ids(nodes: list) -> list:
        ids: list[str] = []
        for n in nodes:
            if n["type"] == "workspace":
                ids.append(n["id"])
            elif n["type"] == "category":
                ids.extend(n.get("children", []))
        return ids

    def sidebar_layout(self) -> list:
        """The effective sidebar layout (normalized). Defaults to all
        workspaces uncategorized in their current order when none is stored."""
        return self._normalize_layout(self._sidebar_nodes)

    def apply_sidebar_layout(self, nodes: list, emit: bool = True) -> None:
        """Adopt a new sidebar layout (order + categories) from the sidebar,
        re-sequencing workspaces to its flattened display order."""
        norm = self._normalize_layout(nodes)
        self._sidebar_nodes = norm
        flat = self._flatten_ws_ids(norm)
        rank = {wid: i for i, wid in enumerate(flat)}
        self._workspaces.sort(key=lambda w: rank.get(w.id, len(rank)))
        if emit:
            self.sidebarLayoutChanged.emit()

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

    # -------------------------------------------------- task assignment ---
    # The UI dialogs drive these (e.g. the card "Assign / reassign a task..."
    # action -> reassign_agent). Only spawn_worker still consults a heuristic,
    # and only for the model/effort of a brand new agent; assigning a task to
    # an EXISTING agent changes nothing about that agent except its task.

    def spawn_worker(self, ws_id: str, task: str,
                     model: str = "", effort: str = "",
                     auto_created: bool = True) -> TerminalAgent | None:
        """Create a NEW Claude worker with a task-appropriate model and give it
        the task. Returns None if the workspace is at its cap."""
        ws = self.workspace(ws_id)
        if ws is None:
            return None
        name = self.next_agent_name(ws_id)
        model, effort = orchestration.resolve_model_effort(task, model, effort)
        spec = build_spec(AgentKind.CLAUDE, name, cwd=ws.project_path,
                          model=model, effort=effort)
        agent = self.add_terminal(ws_id, spec, autostart=True)
        if agent is None:
            return None  # limit_reached — caller should reassign a completed one
        agent.auto_created = auto_created
        agent.set_assignment(AssignmentState.AWAITING)
        agent.deliver_task(task)  # queued until the TUI is prompt-ready
        # add_terminal's immediate structural save fired BEFORE auto_created/
        # assignment/task were set above — persist NOW (not on the debounce) so
        # a hard kill before the heartbeat can't resurrect this worker with no
        # task and let it go dormant.
        self._persist_now()
        return agent

    def assign_task(self, ws_id: str, agent_id: str, task: str) -> bool:
        agent = self.agent(ws_id, agent_id) or self.resolve_agent(agent_id)
        if agent is None:
            return False
        agent.deliver_task(task)
        return True

    def reassign_agent(self, agent_id: str, task: str) -> bool:
        """Retask an idle/completed agent, preserving its session (no restart —
        the task is typed into the existing pty).

        Assigning a task NEVER renames the agent. It used to: the task was run
        through a keyword-matched role heuristic and the result became both the
        role sublabel and (unless the user had renamed it by hand) the display
        name, so one dialog silently relabelled the card twice over with a
        guess."""
        agent = self.resolve_agent(agent_id)
        if agent is None:
            return False
        if self.workspace_of(agent.id) is None:
            return False
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
        # these mutations is PERSISTED (running/task/assignment/name), so each
        # must also mark the session dirty — otherwise a reassignment or
        # completion is lost if the process dies before a graceful close.
        # spec.role is NOT here: build_spec sets it once from the kind and
        # nothing mutates it any more, so there is no signal to listen to.
        wid = ws.id
        agent.status_changed.connect(lambda *_: self._touch(wid))
        agent.task_changed.connect(lambda *_: self._touch(wid))
        agent.assignment_changed.connect(lambda *_: self._touch(wid))
        agent.name_changed.connect(lambda *_: self._touch(wid))
        agent.font_changed.connect(lambda *_: self.dirty.emit())
        # recovery (verify-before-resume) must never land on a peer's
        # conversation, so give the agent a live view of its folder-mates' pins
        agent._sibling_sessions = lambda a=agent: self.sibling_session_ids(a)
        # so the agent can record WHY a limit banner on its screen did not
        # latch (see TerminalAgent._note_limit_skip). Routed through the
        # manager's own hook, so it is a no-op until MainWindow sets one.
        agent.audit = self._audit
        # busy/standby is TRANSIENT (not persisted): refresh the badge only,
        # never mark dirty — otherwise every output burst would thrash saves
        agent.activity_changed.connect(lambda *_: self._recompute(wid))
        # waiting-for-input is likewise transient (drives the "?" indicator):
        # refresh the badge AND announce the rising edge so the UI can chime
        agent.waiting_changed.connect(
            lambda waiting, wid=wid, aid=agent.id:
            self._on_agent_waiting(wid, aid, waiting))
        # cut off by the plan limit — also transient, but announced so the
        # window can record it. Auto-continue depends on this having been seen,
        # so it must leave a forensic trace: twice now the feature failed
        # silently and the cause had to be reconstructed from transcripts.
        agent.limit_blocked_changed.connect(
            lambda blocked, wid=wid, aid=agent.id:
            self._on_agent_limit_blocked_changed(wid, aid, blocked))
        # deferred messages are the one derived-looking signal that IS
        # persisted, so unlike busy/waiting/limit above this one refreshes the
        # badge AND marks dirty. It fires on add/cancel/send/miss only — the
        # per-second countdown never reaches the model, which is what keeps
        # this from behaving like `activity_changed` and thrashing saves.
        agent.scheduled_changed.connect(lambda wid=wid: self._touch(wid))
        # idle-but-a-shell-is-still-running is transient like busy/waiting:
        # refresh the badge only, never mark dirty
        agent.bg_shell_changed.connect(lambda *_, wid=wid: self._recompute(wid))
        # Dis-allocate any duplicate session_id already held by a sibling agent
        # in the same project directory (e.g. from an old un-isolated sync).
        if agent.spec.session_id and agent.spec.session_id in self.sibling_session_ids(agent):
            agent.spec.session_id = ""

        if agent.spec.provider == "gemini" and not agent.spec.session_id:
            exclude = self.sibling_session_ids(agent)
            live_sid = session_sync.get_gemini_live_session(agent.spec.cwd, exclude=exclude)
            if live_sid:
                agent.spec.session_id = live_sid

    def _on_agent_limit_blocked_changed(self, ws_id: str, agent_id: str,
                                        blocked: bool) -> None:
        """Refresh the derived hourglass count on EVERY edge (like
        `_on_agent_waiting`'s `_recompute`) so the sidebar badge disappears the
        instant an agent resumes, not just when it first gets cut off. The
        RISING edge alone is forwarded to `agentLimitBlocked` — that signal
        feeds the ledger/audit trail, which records the cut-off itself, not
        its resolution."""
        self._recompute(ws_id)
        if blocked:
            self.agentLimitBlocked.emit(ws_id, agent_id)

    def _on_agent_waiting(self, ws_id: str, agent_id: str,
                          waiting: bool) -> None:
        """Refresh derived badge state, and on the RISING edge announce that an
        agent just started waiting for the user (so the UI can chime). Never
        marks dirty — waiting is transient, like busy/standby."""
        self._recompute(ws_id)
        if waiting:
            self.agentWaiting.emit(ws_id, agent_id)

    def poll_bg_shell_activity(self) -> None:
        """Tick every agent's background-shell check (see
        TerminalAgent.poll_bg_shell) -- cheap, a single Job Object syscall
        per running agent, so a plain loop on a timer is enough."""
        for agent in self.all_agents():
            agent.poll_bg_shell()

    def sync_prompt_events(self) -> None:
        """Apply new "needs the user" edges the Claude hooks appended since the
        last poll, driving each agent's authoritative waiting state. Read
        incrementally (edges, not a level to reconcile) so a stale line is never
        re-applied after a local output-clear. Best-effort: any error leaves the
        offset untouched and simply retries next tick.

        A turn_set (free-text question) is IGNORED while the agent is actively
        producing output — that means the question was already answered and the
        agent has resumed, so the edge is stale (the user answered faster than
        we polled). Tool prompts have no such guard: they legitimately render
        output while still open."""
        if not self.prompt_events_path:
            return
        records, self._prompt_offset = session_hook.read_prompt_events(
            self.prompt_events_path, self._prompt_offset)
        for rec in records:
            agent = self.resolve_agent(rec.get("agent_id", ""))
            if agent is None:
                continue
            kind = rec.get("kind")
            if kind == session_hook.EV_TOOL_SET:
                agent.set_tool_waiting(True)
            elif kind == session_hook.EV_TOOL_CLEAR:
                agent.set_tool_waiting(False)
            elif kind == session_hook.EV_TURN_SET:
                if not agent.is_busy():
                    agent.set_turn_waiting(True)
            elif kind == session_hook.EV_TURN_CLEAR:
                agent.set_turn_waiting(False)

    def _touch(self, ws_id: str) -> None:
        """Recompute derived state AND mark the session dirty (persisted
        fields changed)."""
        self._recompute(ws_id)
        self.dirty.emit()

    # ------------------------------------------------- live-session sync ---

    def sibling_session_ids(self, agent: TerminalAgent) -> set:
        """Pinned conversation ids of OTHER agents sharing this agent's
        folder. Recovery must never resume onto one of these — two agents on
        one transcript race and can truncate it (a real past incident)."""
        enc = transcripts.encode_project_dir(agent.spec.cwd)
        out = set()
        for a in self.all_agents():
            if (a is not agent and a.spec.provider == agent.spec.provider
                    and a.spec.session_id
                    and transcripts.encode_project_dir(a.spec.cwd) == enc):
                out.add(a.spec.session_id)
        return out

    def refresh_ai_titles(self) -> None:
        """Pull each running Claude/Gemini agent's latest AI conversation title
        AND its context-window occupancy from the live transcript and adopt both
        as transient card state (`set_ai_title` / `set_token_usage` never
        persist). Cheap: both readers re-read only when the transcript changed.
        Reads spec.session_id, which sync keeps pointed at the conversation the
        agent is actually writing."""
        for w in self._workspaces:
            for a in w.agents:
                spec = a.spec
                if not spec.session_id:
                    continue
                if spec.provider == "claude":
                    title = transcripts.latest_ai_title(spec.cwd, spec.session_id)
                    if title:
                        a.set_ai_title(title)
                    if a.is_running():
                        used, window = transcripts.latest_token_usage(
                            spec.cwd, spec.session_id)
                        a.set_token_usage(used, window)
                elif spec.provider == "gemini":
                    title = transcripts.latest_gemini_ai_title(spec.cwd, spec.session_id)
                    if title:
                        a.set_ai_title(title)
                    if a.is_running():
                        used, window = transcripts.latest_gemini_token_usage(
                            spec.cwd, spec.session_id)
                        a.set_token_usage(used, window)

    def refresh_model_effort(self) -> None:
        """Pull each running Claude/Gemini agent's CURRENT model, effort and permission
        mode from its transcript, so the card header follows a `/model`,
        `/effort` or mode change the user did inside the terminal. Polled far more
        often than `refresh_ai_titles`, which is why its reader only touches the
        tail of the file and re-reads nothing while the transcript is unchanged.

        The model and effort are TRANSIENT display state (`set_live_model` never
        persists them; `spec.model`/`spec.effort` stay the launch record). The
        PERMISSION MODE is the deliberate exception and IS written back for Claude, because
        the CLI does not carry a mode across a `--resume`: an agent whose user
        put it in plan or auto mode came back ask-each-time on every reopen,
        which is exactly what the launch flag exists to set. Like a pin change
        in `sync_live_sessions` this marks the session dirty ONLY when the mode
        genuinely changed, so the poll never thrashes saves."""
        changed = False
        for w in self._workspaces:
            for a in w.agents:
                spec = a.spec
                if not a.is_running():
                    continue
                if spec.provider == "claude":
                    model, effort, mode = transcripts.latest_model_effort(
                        spec.cwd, spec.session_id)
                    a.set_live_model(model, effort, mode)
                    # the transcript names modes the command line cannot ("default"
                    # is spelled by omitting the flag), so translate before storing:
                    # spec.permission_mode is a LAUNCH flag, not a reading
                    if mode and spec.set_permission_mode(
                            providers.normalize_permission_mode(mode)):
                        changed = True
                elif spec.provider == "gemini":
                    model, effort, mode = transcripts.latest_gemini_model_effort(
                        spec.cwd, spec.session_id)
                    if model or effort or mode:
                        a.set_live_model(model, effort, mode)
        if changed:
            self.dirty.emit()

    def sync_live_sessions(self) -> list:
        """Reconcile each running Claude/Gemini agent's pinned session id with the
        conversation it is ACTUALLY on, so a conversation the user switched to
        (via /resume or /clear inside the TUI, a fork, usage-limit recovery) is
        what comes back on reopen — not the id AI Hive happened to launch with.
        Marks the session dirty when a pin changes. Returns
        [(agent_id, old_id, new_id)] for the caller to audit."""
        changed = []

        # Gemini session sync (only for single-agent folders; multi-agent folders
        # leave pinned IDs alone because filesystem correlation is ambiguous)
        gemini_agents = [a for a in self.all_agents() if a.spec.provider == "gemini"]
        g_groups = defaultdict(list)
        for a in gemini_agents:
            g_groups[transcripts.encode_project_dir(a.spec.cwd)].append(a)

        for g_group in g_groups.values():
            if len(g_group) != 1:
                continue  # multi-agent folder: correlation is unsafe
            a = g_group[0]
            exclude = self.sibling_session_ids(a)
            live_sid = session_sync.get_gemini_live_session(a.spec.cwd, exclude=exclude)
            if live_sid and live_sid != a.spec.session_id:
                changed.append((a.id, a.spec.session_id, live_sid))
                a.spec.session_id = live_sid
                a.note_conversation_replaced()

        running = [a for a in self.all_agents()
                   if a.spec.provider == "claude" and a.is_running()
                   and a.spec.session_id]
        if not running:
            if changed:
                self.dirty.emit()
            return changed

        covered = set()

        # 1. authoritative hook-reported ids
        live_map = (session_hook.read_live_map(self.session_map_path)
                    if self.session_map_path else {})
        for a in running:
            rec = live_map.get(a.id)
            if not rec:
                continue
            covered.add(a.id)  # the child spoke for itself; trust it, not mtime
            new = rec.get("session_id")
            if (new and new != a.spec.session_id
                    and session_sync.is_session_id(new)):
                changed.append((a.id, a.spec.session_id, new))
                a.spec.session_id = new
                # a DIFFERENT conversation is on screen now (/clear, /resume):
                # the scrollback behind it, and every milestone in it, belong
                # to the old one
                a.note_conversation_replaced()

        # 2. filesystem fallback for agents the hook hasn't reported. Pass ALL
        # running agents (not just the uncovered ones) so resolve_live_ids sees
        # the TRUE folder composition and still refuses to guess in a
        # multi-agent folder: dropping a hook-covered agent from this list would
        # make its folder-mate look solo and get mtime-mis-pinned onto the
        # transcript the covered agent just switched to. We simply don't APPLY a
        # mtime update to an agent the hook already spoke for authoritatively.
        infos = [
            session_sync.AgentInfo(key=a.id, cwd=a.spec.cwd,
                                   pinned_id=a.spec.session_id,
                                   started_at=a._session_started)
            for a in running
        ]
        updates = session_sync.resolve_live_ids(infos)
        by_id = {a.id: a for a in running}
        for aid, new in updates.items():
            if aid in covered:
                continue  # hook is authoritative for this agent; never override
            a = by_id.get(aid)
            if a and new and new != a.spec.session_id:
                changed.append((aid, a.spec.session_id, new))
                a.spec.session_id = new
                a.note_conversation_replaced()

        if changed:
            self.dirty.emit()
        return changed

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
            # every Claude agent gets the peer-etiquette prompt: read the board
            # before starting work, and log_activity as it goes
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
        # auto-created agents (task-spawned workers) keep their "resume on next
        # open" intent even if the process momentarily died — otherwise a
        # crashed/failed auto-agent silently goes dormant and never comes back.
        # User-created agents persist their live running state as before.
        return {**a.spec.to_dict(),
                "running": (a.is_running()
                            or (a.auto_created and a.autostart_on_restore)),
                "task": a.current_task,
                "assignment": a.assignment.value,
                # messages the user deferred (app/scheduled_send). Only the
                # PENDING ones; a restore turns any that came due while we were
                # closed into MISSED rather than firing them late.
                "scheduled": a.scheduled_dicts(),
                "auto_created": a.auto_created}

    def _agent_dict_safe(self, a) -> dict | None:
        """Serialize one agent, but NEVER let a single malformed agent abort the
        whole session save: one un-serializable agent must not take every other
        workspace's agents down with it (that is how a folder's agents once
        vanished from disk with nothing in the log). Fall back to a minimal
        entry that still preserves identity + resume so the agent survives; if
        even that fails, drop just this one and keep going.

        The degrade is AUDITED, with the exception on the line, because it was
        found happening in production and could not be diagnosed: two live
        agents were being written as fallback records on EVERY save, silently,
        and the cause is not recoverable from the file. Two different failures
        in `AgentSpec.to_dict` (a `kind` that is a plain str, so `.value`
        raises; a `user_args` of None, so `list()` raises) produce byte-
        identical output, and `AgentKind` is a str-mixin enum so even the
        serialized `kind` cannot tell them apart. One line here turns that
        into a name and an exception type.

        The fallback also carries every field it can read WITHOUT calling
        anything that might throw again (plain attribute reads with defaults).
        The loss is otherwise real: model, effort, permission mode, role and
        the current task all silently reset to defaults on the next restore.
        (`pty` is included for the same reason but is belt-and-braces for AI
        agents: they are in `PTY_ONLY_KINDS`, so `build_spec` re-forces it
        even when the record has no such field.)
        """
        try:
            return self._agent_dict(a)
        except Exception as exc:
            self._audit(f"SAVE-DEGRADE agent={getattr(a.spec, 'name', '?')!r} "
                        f"{type(exc).__name__}: {exc}")
            try:
                spec = a.spec
                return {"kind": getattr(spec, "kind", ""),
                        "name": getattr(spec, "name", "Agent"),
                        "custom_name": getattr(spec, "custom_name", False),
                        "cwd": getattr(spec, "cwd", ""),
                        "provider": getattr(spec, "provider", ""),
                        "session_id": getattr(spec, "session_id", ""),
                        # everything below is why the degrade is survivable:
                        # pty in particular decides whether the agent comes
                        # back as a terminal at all
                        "pty": bool(getattr(spec, "pty", False)),
                        "model": getattr(spec, "model", ""),
                        "effort": getattr(spec, "effort", ""),
                        "permission_mode": getattr(spec, "permission_mode", ""),
                        "role": getattr(spec, "role", ""),
                        "font_px": getattr(spec, "font_px", 0),
                        "task": getattr(a, "current_task", ""),
                        "scheduled": self._scheduled_safe(a),
                        "running": True}
            except Exception as exc2:
                self._audit(f"SAVE-DROP agent (even the minimal record failed) "
                            f"{type(exc2).__name__}: {exc2}")
                return None

    @staticmethod
    def _scheduled_safe(a) -> list:
        """The deferred-message queue for the DEGRADED record. Everything in
        the fallback is a read that must not throw a second time (the whole
        point of that path), and this one calls a method, so it is guarded."""
        try:
            return a.scheduled_dicts()
        except Exception:
            return []

    def _audit(self, message: str) -> None:
        """Best-effort line into session.log; forensics must never break the
        save they observe (the same rule `MainWindow._limit_audit` follows)."""
        try:
            if self.audit is not None:
                self.audit(message)
        except Exception:
            pass

    def to_session_dict(self) -> dict:
        return {
            "version": SESSION_VERSION,
            "active": self._active_id,
            "sidebar": self.sidebar_layout(),   # order + categories (v4)
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
        # that was the default) to interactive terminals. Pinned to version < 3
        # (its original threshold) so later SESSION_VERSION bumps never
        # re-trigger it and flip a user's deliberately line-mode shell to pty.
        migrate_shells = data.get("version", 1) < 3 and HAS_CONPTY
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
                agent.restore_scheduled(td.get("scheduled") or [])
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
        # restore the sidebar layout (order + categories); absent on <v4 -> all
        # workspaces stay uncategorized in load order (normalize appends them)
        sidebar = data.get("sidebar")
        if isinstance(sidebar, list):
            self.apply_sidebar_layout(sidebar, emit=False)
        active = data.get("active", "")
        if self.workspace(active) is not None:
            self.set_active(active)
        elif self._workspaces:
            self.set_active(self._workspaces[0].id)
