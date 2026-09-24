"""Turns agent and workspace signals into event-log records (see event_log).

`EventHub` is owned by MainWindow and created AFTER the session has loaded, so
the agents that already exist are wired silently: restoring a session is not
"Agent 3 was added". Everything it hears it writes to disk at once and keeps
in memory for the window, which listens to `appended`.

Most events are already announced somewhere; the hub only collects them:

    prompt / task      TerminalAgent.user_prompted / task_delivered
    question/answered  TerminalAgent.waiting_changed (both edges)
    reply              TerminalAgent.reply_finished (turns the user asked for)
    limit              TerminalAgent.limit_blocked_changed (rising edge)
    limit outcome      MainWindow calls limit_outcome() beside the ledger write
    scheduled          MainWindow calls scheduled() from _deliver_scheduled
    crashed/lifecycle  TerminalAgent.status_changed, name_changed, and the
                       manager's structural signals
    board              a poll of each workspace's board.md activity section

Two edges are SETTLED before they are recorded (QUESTION_SETTLE_MS). An agent
parked on the plan-limit menu raises "?" too, and which of the two latches
first depends on the screen; waiting a moment lets the limit win, exactly as
MainWindow._on_agent_waiting keeps the question chime quiet for it. The limit
record waits for the same reason: MainWindow borrows the reset time from the
usage reading when the banner had none, and that happens in its own slot.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import event_log as el
from .terminal_agent import AgentStatus

QUESTION_SETTLE_MS = 1500
BOARD_POLL_MS = 20000
# How long a limit row may stay open after the latch dropped with no outcome
# written: long enough for MainWindow's verify pass (AUTO_CONTINUE_VERIFY_MS)
# to report the real one first.
LIMIT_CLOSE_GRACE_MS = 45000
# a reply's "took" is measured from the last prompt, if it is this recent
REPLY_SPAN_S = 24 * 3600
# a question this close before a cut-off latched was the limit's own menu
LIMIT_MENU_S = 30

_ERROR_STATES = (AgentStatus.CRASHED, AgentStatus.EXITED_ERR,
                 AgentStatus.FAILED)
_LIFECYCLE_WORDS = {AgentStatus.RUNNING: "started",
                    AgentStatus.EXITED_OK: "exited",
                    AgentStatus.IDLE: "stopped"}


class EventHub(QObject):
    appended = Signal(dict)      # a new record (already on disk)
    openChanged = Signal(int)    # number of unanswered questions this run

    def __init__(self, manager, directory: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.directory = directory
        self.session_start = time.time()
        el.prune(directory)
        self.records: list[dict] = el.read_since(
            directory, self.session_start - el.KEEP_DAYS * 86400)
        self._by_id = {r["id"]: r for r in self.records if r.get("id")}
        self.closers: dict[str, dict] = {}     # opened record id -> closer
        for r in self.records:
            if r.get("ref") and r.get("kind") in (el.ANSWERED,
                                                  *el.LIMIT_OUTCOMES):
                self.closers.setdefault(r["ref"], r)
        self._closed = False
        self._info: dict[str, dict] = {}       # agent id -> identity snapshot
        self._prev_status: dict[str, AgentStatus] = {}
        self._open_q: dict[str, str] = {}      # agent uid -> question id
        self._open_limit: dict[str, str] = {}  # agent uid -> limit id
        self._last_prompt: dict[str, float] = {}
        self._pending: dict[tuple, float] = {}  # (kind, agent id) -> since
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(QUESTION_SETTLE_MS)
        self._settle.timeout.connect(self.flush_pending)
        self._board_seen: dict[str, set] = {}
        self._board_timer = QTimer(self)
        self._board_timer.setInterval(BOARD_POLL_MS)
        self._board_timer.timeout.connect(self.poll_boards)

        self._ws_names = {ws.id: ws.name for ws in manager.workspaces}
        for ws in manager.workspaces:
            for agent in ws.agents:
                self._wire(ws.id, agent)
        manager.terminalAdded.connect(self._on_terminal_added)
        manager.terminalRemoved.connect(self._on_terminal_removed)
        manager.workspaceAdded.connect(self._on_workspace_added)
        manager.workspaceRemoved.connect(self._on_workspace_removed)
        manager.workspaceRenamed.connect(self._on_workspace_renamed)
        self.poll_boards()          # seeds what is already on each board
        self._board_timer.start()

    # ------------------------------------------------------------ public ---

    def shutdown(self) -> None:
        """The app is closing: every agent is about to be stopped, and none of
        that is news. Stop recording."""
        self._closed = True
        self._settle.stop()
        self._board_timer.stop()

    def open_questions(self) -> int:
        return len(self._open_q)

    def closer_of(self, rec: dict) -> dict | None:
        return self.closers.get(rec.get("id"))

    def resolve(self, ws_id: str, agent_uid: str):
        """The live TerminalAgent a record points at, or None if it is gone."""
        ws = self.manager.workspace(ws_id)
        if ws is None or not agent_uid:
            return None
        return next((a for a in ws.agents
                     if getattr(a.spec, "uid", "") == agent_uid), None)

    def limit_outcome(self, agent, outcome: str, tries: int = 0,
                      detail: str = "") -> None:
        """MainWindow wrote a ledger outcome for this agent's cut-off."""
        kind = {"resumed": el.LIMIT_RESUMED, "failed": el.LIMIT_FAILED,
                "dismissed": el.LIMIT_DISMISSED}.get(outcome)
        if kind is None:
            return
        uid = agent.spec.uid
        ref = self._open_limit.pop(uid, None)
        self._record(kind, agent, ref=ref,
                     data={"tries": tries, "detail": detail})

    def scheduled(self, agent, sent: bool, text: str, why: str = "") -> None:
        """MainWindow delivered (or gave up on) a scheduled message."""
        self._record(el.SCHED_SENT if sent else el.SCHED_MISSED, agent,
                     text=text, data={"why": why} if why else None)

    # ------------------------------------------------------------ wiring ---

    def _snapshot(self, ws_id: str, agent) -> dict:
        info = {"ws_id": ws_id, "agent_uid": agent.spec.uid,
                "agent_name": agent.spec.name,
                "provider": agent.spec.provider or agent.spec.kind.value}
        self._info[agent.id] = info
        return info

    def _wire(self, ws_id: str, agent) -> None:
        self._snapshot(ws_id, agent)
        self._prev_status[agent.id] = agent.status
        agent.user_prompted.connect(
            lambda text, a=agent: self._on_prompt(a, el.PROMPT, text))
        agent.task_delivered.connect(
            lambda text, a=agent: self._on_prompt(a, el.TASK, text))
        agent.waiting_changed.connect(
            lambda waiting, a=agent: self._on_waiting(a, waiting))
        agent.reply_finished.connect(lambda a=agent: self._on_reply(a))
        agent.limit_blocked_changed.connect(
            lambda blocked, a=agent: self._on_limit(a, blocked))
        agent.status_changed.connect(
            lambda status, a=agent: self._on_status(a, status))
        agent.name_changed.connect(
            lambda name, a=agent: self._on_renamed(a, name))

    def _on_terminal_added(self, ws_id: str, agent) -> None:
        self._wire(ws_id, agent)
        self._record(el.LIFECYCLE, agent, text="added")

    def _on_terminal_removed(self, ws_id: str, agent_id: str) -> None:
        info = self._info.pop(agent_id, None)
        self._prev_status.pop(agent_id, None)
        if info is not None:
            self._open_q.pop(info["agent_uid"], None)
            self.openChanged.emit(len(self._open_q))
            self._write(el.make_record(el.LIFECYCLE, ws_name=self._ws_name(
                ws_id), text="removed", **info))

    def _on_workspace_added(self, ws) -> None:
        self._ws_names[ws.id] = ws.name
        self._write(el.make_record(el.LIFECYCLE, ws_id=ws.id, ws_name=ws.name,
                                   text="workspace added"))

    def _on_workspace_removed(self, ws_id: str) -> None:
        name = self._ws_names.pop(ws_id, "")
        for aid in [k for k, v in self._info.items() if v["ws_id"] == ws_id]:
            info = self._info.pop(aid)
            self._open_q.pop(info["agent_uid"], None)
        self.openChanged.emit(len(self._open_q))
        self._write(el.make_record(el.LIFECYCLE, ws_id=ws_id, ws_name=name,
                                   text="workspace removed"))

    def _on_workspace_renamed(self, ws_id: str, name: str) -> None:
        old = self._ws_names.get(ws_id, "")
        self._ws_names[ws_id] = name
        self._write(el.make_record(
            el.LIFECYCLE, ws_id=ws_id, ws_name=name,
            text=f'workspace renamed from "{old}"' if old
            else "workspace renamed"))

    # ------------------------------------------------------------ events ---

    def _on_prompt(self, agent, kind: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._last_prompt[agent.spec.uid] = time.time()
        self._record(kind, agent, text=text)

    def _on_waiting(self, agent, waiting: bool) -> None:
        if waiting:
            self._pending[(el.QUESTION, agent.id)] = time.time()
            self._settle.start()
            return
        self._pending.pop((el.QUESTION, agent.id), None)
        ref = self._open_q.pop(agent.spec.uid, None)
        if ref is not None:
            self._record(el.ANSWERED, agent, ref=ref)
            self.openChanged.emit(len(self._open_q))

    def _on_reply(self, agent) -> None:
        since = self._last_prompt.pop(agent.spec.uid, None)
        took = (time.time() - since
                if since and time.time() - since < REPLY_SPAN_S else None)
        self._record(el.REPLY, agent,
                     data={"took": round(took, 1)} if took is not None
                     else None)

    def _on_limit(self, agent, blocked: bool) -> None:
        if blocked:
            # a failed verify re-latches the same cut-off; that is one row
            if agent.spec.uid not in self._open_limit:
                self._pending[(el.LIMIT, agent.id)] = time.time()
                self._settle.start()
            return
        self._pending.pop((el.LIMIT, agent.id), None)
        if agent.spec.uid in self._open_limit:
            QTimer.singleShot(LIMIT_CLOSE_GRACE_MS,
                              lambda a=agent: self._limit_grace(a))

    def _limit_grace(self, agent) -> None:
        """The latch dropped and no outcome followed (the user cleared it, or
        the agent carried on by itself). Close the row as resumed."""
        try:
            blocked = agent.is_limit_blocked()
        except RuntimeError:       # the agent was deleted meanwhile
            return
        if not blocked and agent.spec.uid in self._open_limit:
            self.limit_outcome(agent, "resumed", detail="the limit cleared")

    def flush_pending(self) -> None:
        """Record every settled question / cut-off that still stands."""
        pending, self._pending = self._pending, {}
        for (kind, agent_id), _since in pending.items():
            info = self._info.get(agent_id)
            agent = (self.manager.agent(info["ws_id"], agent_id)
                     if info else None)
            if agent is None:
                continue
            if kind == el.QUESTION:
                if (not agent.is_waiting() or agent.is_limit_blocked()
                        or agent.spec.uid in self._open_q):
                    continue
                rec = self._record(el.QUESTION, agent)
                if rec is not None:
                    self._open_q[agent.spec.uid] = rec["id"]
                    self.openChanged.emit(len(self._open_q))
            elif kind == el.LIMIT:
                if (not agent.is_limit_blocked()
                        or agent.spec.uid in self._open_limit):
                    continue
                # a question recorded a moment before the limit latched was
                # the limit menu, not a question: close it as such
                qid = self._open_q.get(agent.spec.uid)
                q = self._by_id.get(qid) if qid else None
                if q is not None and time.time() - q["at"] < LIMIT_MENU_S:
                    del self._open_q[agent.spec.uid]
                    self._record(el.ANSWERED, agent, ref=qid,
                                 data={"limit_menu": True})
                    self.openChanged.emit(len(self._open_q))
                rec = self._record(el.LIMIT, agent, data={
                    "reset_at": agent.limit_resets_at(),
                    "window": agent.limit_window()})
                if rec is not None:
                    self._open_limit[agent.spec.uid] = rec["id"]

    def _on_status(self, agent, status: AgentStatus) -> None:
        prev = self._prev_status.get(agent.id)
        self._prev_status[agent.id] = status
        if getattr(agent, "_disposing", False):
            return
        if prev is AgentStatus.STOPPING and status in (
                AgentStatus.IDLE, AgentStatus.EXITED_OK, *_ERROR_STATES):
            self._record(el.LIFECYCLE, agent, text="stopped")   # the user did
            return
        if status in _ERROR_STATES:
            self._record(el.CRASHED, agent, text={
                AgentStatus.CRASHED: "crashed",
                AgentStatus.EXITED_ERR: "exited with an error",
                AgentStatus.FAILED: "failed to start"}[status])
            return
        word = _LIFECYCLE_WORDS.get(status)
        if word == "stopped" and prev is not AgentStatus.RUNNING:
            return      # idle -> idle noise, or the initial arm
        if word:
            self._record(el.LIFECYCLE, agent, text=word)

    def _on_renamed(self, agent, name: str) -> None:
        info = self._info.get(agent.id)
        old = info["agent_name"] if info else ""
        if info is not None:
            info["agent_name"] = name
        self._record(el.LIFECYCLE, agent,
                     text=f'renamed from "{old}"' if old else "renamed")

    # ------------------------------------------------------------- board ---

    def poll_boards(self) -> None:
        """Log board lines that appeared since the last poll. The first read of
        a workspace only seeds what is already there: the board's own history
        is on the board, and replaying it on every launch would bury today."""
        for ws in self.manager.workspaces:
            board = getattr(ws, "board", None)
            if board is None or not os.path.isfile(board.path):
                continue
            lines = board.read_log_tail(60)
            seen = self._board_seen.get(ws.id)
            self._board_seen[ws.id] = set(lines)
            if seen is None:
                continue
            for line in lines:
                if line in seen:
                    continue
                parsed = el.parse_board_line(line)
                if parsed is None:
                    continue
                name, _stamp, message = parsed
                agent = next((a for a in ws.agents
                              if a.spec.name.lower() == name.lower()), None)
                if agent is not None:
                    self._record(el.BOARD, agent, text=message)
                else:
                    self._write(el.make_record(
                        el.BOARD, ws_id=ws.id, ws_name=ws.name,
                        agent_name=name, text=message))

    # ----------------------------------------------------------- writing ---

    def _ws_name(self, ws_id: str) -> str:
        ws = self.manager.workspace(ws_id)
        return ws.name if ws is not None else self._ws_names.get(ws_id, "")

    def _record(self, kind: str, agent, *, text: str = "",
                data: dict | None = None, ref: str | None = None):
        info = self._info.get(agent.id)
        if info is None:
            ws = self.manager.workspace_of(agent.id)
            if ws is None:
                return None
            info = self._snapshot(ws.id, agent)
        info["agent_name"] = agent.spec.name
        rec = el.make_record(kind, ws_name=self._ws_name(info["ws_id"]),
                             text=text, data=data, ref=ref, **info)
        return self._write(rec)

    def _write(self, rec: dict):
        if self._closed:
            return None
        el.append(self.directory, rec)
        self.records.append(rec)
        self._by_id[rec["id"]] = rec
        if rec.get("ref") and rec["kind"] in (el.ANSWERED, *el.LIMIT_OUTCOMES):
            self.closers.setdefault(rec["ref"], rec)
        self.appended.emit(rec)
        return rec
