"""Workspace activity panel: live roster + shared-board log + changed files.

A collapsible side panel giving the human a workspace-wide view of every
agent (status, current task) plus the shared coordination board's activity
log and a best-effort git changed-files list. This is the human-facing half
of the shared-awareness feature; the agent-facing half is the board file
(.aihive/board.md) that Claude agents read and write via their appended
system prompt.
"""

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, Qt, Signal)
from PySide6.QtWidgets import (QFrame, QLabel, QLineEdit, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import coordination
from ..terminal_agent import AgentStatus

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


class ActivityPanel(QFrame):
    taskEdited = Signal(str, str)  # agent_id, task

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ActivityPanel")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(0)
        self.setMaximumWidth(0)  # starts concealed; reveal() animates it open
        self._items: dict[str, AgentRosterItem] = {}
        self._project_path = ""
        self._git_cache: list[str] = []  # last git result (git is blocking)
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
        self.files_label.setText("\n".join(self._git_cache) if self._git_cache
                                 else "(not a git repo, or no changes)")
