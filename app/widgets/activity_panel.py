"""Workspace activity panel: live roster + shared-board log + changed files.

A collapsible side panel giving the human a workspace-wide view of every
agent (status, current task) plus the shared coordination board's activity
log and a best-effort git changed-files list. This is the human-facing half
of the shared-awareness feature; the agent-facing half is the board file
(.aihive/board.md) that Claude agents read and write via their appended
system prompt.

With agent lanes (app/lanes.py) a Lanes section lists each laned agent's
branch, commits ahead/behind, uncommitted files and overlaps, and the
changed-files list is per checkout: the workspace folder's own `git status`,
then each lane's uncommitted files. Lane data comes from MainWindow
(`set_lanes`, fed by app/lane_service.py), so the panel runs no git for it.

The Integration section is the approved integration queue
(app/integration.py): the integrator, then one row per submitted lane with
the buttons its state allows (Approve merge, Recheck, Resend brief, Open PR,
Skip). A button only asks (`queueAction`); MainWindow does the work, so this
is the one place the user approves a merge.
"""

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, Qt, Signal)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import coordination
from .. import integration as integ
from ..terminal_agent import AgentStatus
from ..ui_theme import repolish

_ICON = {
    AgentStatus.RUNNING: "🟢", AgentStatus.STARTING: "🟡",
    AgentStatus.STOPPING: "🟡", AgentStatus.IDLE: "⚪",
    AgentStatus.EXITED_OK: "⚪", AgentStatus.EXITED_ERR: "🔴",
    AgentStatus.CRASHED: "🔴", AgentStatus.FAILED: "🔴",
}

PANEL_WIDTH = 320


class AgentRosterItem(QFrame):
    """One agent row: status glyph, name/model, editable current task."""

    taskEdited = Signal(str, str)  # agent_id, task

    def __init__(self, agent, parent=None):
        super().__init__(parent)
        self.setObjectName("RosterItem")
        self.agent_id = agent.id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        self.head = QLabel(self)
        self.head.setObjectName("RosterHead")
        self.task_edit = QLineEdit(self)
        self.task_edit.setObjectName("RosterTask")
        self.task_edit.setPlaceholderText("current task…")
        self.task_edit.editingFinished.connect(
            lambda: self.taskEdited.emit(self.agent_id, self.task_edit.text()))
        lay.addWidget(self.head)
        lay.addWidget(self.task_edit)
        self.refresh(agent)

    def refresh(self, agent) -> None:
        busy = bool(getattr(agent, "is_busy", lambda: False)())
        icon = "🟡" if (busy and agent.status == AgentStatus.RUNNING) else _ICON.get(agent.status, "⚪")
        model = agent.spec.model or agent.spec.provider or agent.spec.kind.value
        self.head.setText(f"{icon}  <b>{agent.spec.name}</b>  "
                          f"<span style='color:#8a8a8a'>{model}</span>")
        if not self.task_edit.hasFocus():
            self.task_edit.setText(agent.current_task)


# the buttons each queue state offers, in order: (action, label)
_QUEUE_BUTTONS = {
    integ.QUEUED: (("skip", "Skip"),),
    integ.INTEGRATING: (("recheck", "Recheck"), ("skip", "Skip")),
    integ.AWAITING: (("approve", "Approve merge"), ("recheck", "Recheck"),
                     ("open", "Open PR"), ("skip", "Skip")),
    integ.MERGING: (),
    integ.NEEDS_YOU: (("recheck", "Recheck"), ("resend", "Resend brief"),
                      ("open", "Open PR"), ("skip", "Skip")),
    integ.MERGED: (("open", "Open PR"),),
    integ.SKIPPED: (),
}
_QUEUE_TIPS = {
    "approve": ("Merge this pull request now. AI Hive refuses if it changed "
                "since the integrator tested it, and sends it back to the "
                "integrator if the base branch moved since then."),
    "recheck": "Read the pull request's state again.",
    "resend": "Send the integration brief to the integrator again.",
    "open": "Open the pull request in the browser.",
    "skip": ("Take this lane out of the queue. Its branch and commits stay "
             "where they are."),
}


class QueueRow(QFrame):
    """One submitted lane in the Integration section."""

    actionRequested = Signal(str, str)  # item id, action

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("QueueRow")
        self.item_id = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)
        self.text = QLabel(self)
        self.text.setObjectName("QueueRowText")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.text)
        self.buttons_row = QHBoxLayout()
        self.buttons_row.setSpacing(4)
        lay.addLayout(self.buttons_row)
        self.buttons: dict[str, QPushButton] = {}

    def show_item(self, item, position: int, since: int,
                  enabled: bool) -> None:
        self.item_id = item.id
        if self.property("state") != item.state:
            self.setProperty("state", item.state)
            repolish(self)
        where = f"{item.agent}  ⎇ {item.branch} @ {item.short}"
        lines = [f"{position}. {where}" if position else where]
        status = [integ.STATE_LABEL.get(item.state, item.state)]
        if item.pr:
            status.append(f"PR #{item.pr}")
        if since > 0 and item.is_open:
            status.append(f"+{since} since submit")
        lines.append(" · ".join(status))
        if item.note:
            lines.append(item.note)
        self.text.setText("\n".join(lines))
        wanted = [(a, label) for a, label in _QUEUE_BUTTONS.get(item.state, ())
                  if a != "open" or item.pr_url]
        if [a for a, _ in wanted] != list(self.buttons):
            for btn in self.buttons.values():
                btn.setParent(None)
                btn.deleteLater()
            self.buttons = {}
            for action, label in wanted:
                btn = QPushButton(label, self)
                btn.setObjectName("QueueBtn")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(_QUEUE_TIPS.get(action, ""))
                btn.clicked.connect(
                    lambda _=False, a=action: self.actionRequested.emit(
                        self.item_id, a))
                self.buttons_row.addWidget(btn)
                self.buttons[action] = btn
        for action, btn in self.buttons.items():
            # opening the PR is read-only; everything else waits for the
            # Agent lanes switch, like the rest of the queue
            btn.setEnabled(enabled or action == "open")


class ActivityPanel(QFrame):
    taskEdited = Signal(str, str)  # agent_id, task
    # a queue row's button: item id, action (approve | recheck | resend |
    # open | skip). MainWindow acts on the panel's workspace.
    queueAction = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ActivityPanel")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(0)
        self.setMaximumWidth(0)  # starts concealed; reveal() animates it open
        self._items: dict[str, AgentRosterItem] = {}
        self._project_path = ""
        self._git_cache: list[str] = []  # last git result (git is blocking)
        self._lane_views: dict = {}      # agent uid -> lanes.LaneView
        self._workspace = None
        self._open = False  # logical state, correct instantly (anim lags it)
        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title = QLabel("❦  WORKSPACE ACTIVITY", self)
        title.setObjectName("ActivityTitle")
        root.addWidget(title)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setObjectName("ActivityScroll")
        inner = QWidget(scroll)
        self.inner_lay = QVBoxLayout(inner)
        self.inner_lay.setContentsMargins(8, 8, 8, 8)
        self.inner_lay.setSpacing(6)

        self.roster_header = QLabel("Agents", inner)
        self.roster_header.setObjectName("ActivitySection")
        self.inner_lay.addWidget(self.roster_header)
        self.roster_box = QVBoxLayout()
        self.roster_box.setSpacing(4)
        self.inner_lay.addLayout(self.roster_box)

        # agent lanes: hidden until the workspace has a laned agent
        self.lanes_header = QLabel("Lanes", inner)
        self.lanes_header.setObjectName("ActivitySection")
        self.inner_lay.addWidget(self.lanes_header)
        self.lanes_label = QLabel("", inner)
        self.lanes_label.setObjectName("ActivityLanes")
        self.lanes_label.setWordWrap(True)
        self.lanes_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.inner_lay.addWidget(self.lanes_label)
        self.lanes_header.hide()
        self.lanes_label.hide()

        # the integration queue: hidden until the workspace has a lane, an
        # integrator or a queued item
        self.queue_header = QLabel("Integration", inner)
        self.queue_header.setObjectName("ActivitySection")
        self.inner_lay.addWidget(self.queue_header)
        self.queue_info = QLabel("", inner)
        self.queue_info.setObjectName("ActivityLanes")
        self.queue_info.setWordWrap(True)
        self.inner_lay.addWidget(self.queue_info)
        self.queue_box = QVBoxLayout()
        self.queue_box.setSpacing(4)
        self.inner_lay.addLayout(self.queue_box)
        self.queue_rows: list[QueueRow] = []
        self.queue_header.hide()
        self.queue_info.hide()

        self.log_header = QLabel("Shared log", inner)
        self.log_header.setObjectName("ActivitySection")
        self.inner_lay.addWidget(self.log_header)
        self.log_label = QLabel("_No shared board yet._", inner)
        self.log_label.setObjectName("ActivityLog")
        self.log_label.setWordWrap(True)
        self.log_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.inner_lay.addWidget(self.log_label)

        self.files_header = QLabel("Changed files (git)", inner)
        self.files_header.setObjectName("ActivitySection")
        self.inner_lay.addWidget(self.files_header)
        self.files_label = QLabel("-", inner)
        self.files_label.setObjectName("ActivityFiles")
        self.files_label.setWordWrap(True)
        self.inner_lay.addWidget(self.files_label)
        self.inner_lay.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

    def reveal(self) -> None:
        self._open = True
        self.setVisible(True)
        self._anim.stop()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(PANEL_WIDTH)
        self._anim.start()

    def conceal(self) -> None:
        self._open = False
        self._anim.stop()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(0)
        self._anim.start()

    def is_open(self) -> bool:
        return self._open

    def _on_anim_done(self) -> None:
        if not self._open:
            self.setVisible(False)

    def set_workspace(self, workspace) -> None:
        self._project_path = workspace.project_path
        self._git_cache = []  # force a fresh git read for the new workspace
        # rebuild roster items
        for item in self._items.values():
            item.setParent(None)
            item.deleteLater()
        self._items = {}
        for agent in workspace.agents:
            item = AgentRosterItem(agent, self)
            item.taskEdited.connect(self.taskEdited)
            self.roster_box.addWidget(item)
            self._items[agent.id] = item
        self.refresh(workspace, with_git=True)

    def set_lanes(self, views: dict) -> None:
        """The lane poller's latest {agent uid: lanes.LaneView} for the shown
        workspace ({} while Agent lanes is off). Cheap: no git."""
        self._lane_views = dict(views or {})
        if self._workspace is not None:
            self._render_lanes(self._workspace)

    def set_integration(self, state: dict | None) -> None:
        """The workspace's queue, from MainWindow: {"paused", "integrator"
        (a name or ""), "laned" (the workspace has a lane), "items"
        [QueueItem], "since" {item id: commits on the lane since submit},
        "reason" (why a lane can't be submitted now, or "")}. None hides the
        section."""
        state = state or {}
        visible = bool(state.get("items") or state.get("integrator")
                       or state.get("laned"))
        self.queue_header.setVisible(visible)
        self.queue_info.setVisible(visible)
        items = list(state.get("items") or []) if visible else []
        while len(self.queue_rows) > len(items):
            row = self.queue_rows.pop()
            row.setParent(None)
            row.deleteLater()
        while len(self.queue_rows) < len(items):
            row = QueueRow(self)
            row.actionRequested.connect(self.queueAction)
            self.queue_box.addWidget(row)
            self.queue_rows.append(row)
        if not visible:
            return
        paused = bool(state.get("paused"))
        waiting = sum(1 for i in items if i.is_open)
        self.queue_header.setText(
            "Integration: paused" if paused else
            f"Integration: {waiting} in line" if waiting else "Integration")
        who = state.get("integrator") or ""
        info = [f"Integrator: {who}" if who else
                "No integrator yet. Right-click a laned Claude agent's "
                "header and pick Make integrator."]
        if paused:
            info.append("Paused while Agent lanes is off. Nothing is sent "
                        "and nothing is merged; the queue is kept.")
        elif state.get("reason"):
            info.append(state["reason"])
        self.queue_info.setText("\n".join(info))
        since = state.get("since") or {}
        position = 0
        for row, item in zip(self.queue_rows, items):
            position = position + 1 if item.is_open else 0
            row.show_item(item, position, int(since.get(item.id, 0)),
                          not paused)

    def _laned(self, workspace) -> list:
        return [a for a in workspace.agents
                if (getattr(a.spec, "lane", None) or {}).get("branch")]

    def _render_lanes(self, workspace) -> None:
        laned = self._laned(workspace)
        self.lanes_header.setVisible(bool(laned))
        self.lanes_label.setVisible(bool(laned))
        if laned:
            self.lanes_header.setText(f"Lanes: {len(laned)}")
            self.lanes_label.setText("\n".join(
                self._lane_lines(a) for a in laned))
        self._render_files(workspace)

    def _lane_lines(self, agent) -> str:
        lane = agent.spec.lane
        view = self._lane_views.get(agent.spec.uid)
        head = f"{agent.spec.name}  ⎇ {lane['branch']}"
        if view is None:
            return head + "\n  (details while Agent lanes is on)"
        if not view.exists:
            return head + "\n  (lane folder missing)"
        counts = [f"↑{view.ahead}", f"↓{view.behind}",
                  f"{len(view.dirty)} uncommitted"]
        lines = [f"{head}  {' '.join(counts)}"]
        for o in view.overlaps[:6]:
            mark = "✖" if o.level == "conflicts" else "⚠"
            if not o.peer_uid:
                who = f"{o.peer} changed it too"
            elif o.level == "conflicts":
                who = f"conflicts with {o.peer}"
            else:
                who = f"{o.peer} changed it too"
            lines.append(f"  {mark} {o.path}: {who}")
        if len(view.overlaps) > 6:
            lines.append(f"  ...and {len(view.overlaps) - 6} more overlaps")
        return "\n".join(lines)

    def _render_files(self, workspace) -> None:
        main = ("\n".join(self._git_cache) if self._git_cache
                else "(not a git repo, or no changes)")
        laned = self._laned(workspace)
        if not laned:
            self.files_label.setText(main)
            return
        parts = ["Workspace folder:", main]
        for agent in laned:
            view = self._lane_views.get(agent.spec.uid)
            parts.append("")
            parts.append(f"{agent.spec.name}'s lane:")
            if view is None:
                parts.append("(details while Agent lanes is on)")
            elif view.dirty:
                shown = view.dirty[:30]
                parts.extend(shown)
                if len(view.dirty) > 30:
                    parts.append(f"...and {len(view.dirty) - 30} more")
            else:
                parts.append("(no uncommitted changes)")
        self.files_label.setText("\n".join(parts))

    def refresh(self, workspace, with_git: bool = False) -> None:
        """Update the roster + shared-board log (both cheap). The git changed-
        files scan is blocking, so it only runs when with_git=True — i.e. from
        the slow poll timer / workspace switch, never on the high-frequency
        status-change path."""
        live_ids = {a.id for a in workspace.agents}
        # drop removed
        for aid in list(self._items):
            if aid not in live_ids:
                self._items.pop(aid).deleteLater()
        for agent in workspace.agents:
            item = self._items.get(agent.id)
            if item is None:
                item = AgentRosterItem(agent, self)
                item.taskEdited.connect(self.taskEdited)
                self.roster_box.addWidget(item)
                self._items[agent.id] = item
            else:
                item.refresh(agent)
        if not workspace.agents:
            self.roster_header.setText("Agents: none yet")
        else:
            self.roster_header.setText(f"Agents: {len(workspace.agents)}")

        board = getattr(workspace, "board", None)
        tail = board.read_log_tail(30) if board else []
        self.log_label.setText("\n".join(tail) if tail
                               else "_No shared activity logged yet._")
        if with_git:  # blocking git call — only on the slow path
            self._git_cache = coordination.git_changed_files(workspace.project_path)
        self._workspace = workspace
        self._render_lanes(workspace)
