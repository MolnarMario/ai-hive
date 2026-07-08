"""Agent dropdown: a floating popup anchored under a workspace row's count
badge, listing that workspace's agents with their status glyph, name, current
task, and a pulsing "?" when an agent is waiting for the user (a permission
prompt or an interactive question). Clicking an agent row reveals its terminal
card (switching workspaces first if needed). The popup auto-dismisses on an
outside click (Qt.Popup) and refreshes live while open so status / "?" track
the agents in real time. Status glyphs are reused from the activity roster.
"""

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)

from .activity_panel import _ICON

DROPDOWN_WIDTH = 264
_TASK_WIDTH = 176   # px budget for the elided current-task line


def position_popup(anchor: QWidget, size) -> QPoint:
    """Anchor a popup under `anchor`, clamped to the tighter of the window and
    screen — overflow right → right-align, overflow bottom → flip above. Mirrors
    grid_selector._popup_position so popups behave consistently."""
    w, h = size.width(), size.height()
    bl = anchor.mapToGlobal(anchor.rect().bottomLeft())
    br = anchor.mapToGlobal(anchor.rect().bottomRight())
    tl = anchor.mapToGlobal(anchor.rect().topLeft())
    screen = anchor.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    win = anchor.window().geometry()
    left = max(avail.x(), win.x())
    top = max(avail.y(), win.y())
    right = min(avail.x() + avail.width(), win.x() + win.width())
    bottom = min(avail.y() + avail.height(), win.y() + win.height())
    x = bl.x()
    if x + w > right:
        x = br.x() - w
    x = max(left, min(x, right - w))
    y = bl.y()
    if y + h > bottom:
        y = tl.y() - h
    y = max(top, min(y, bottom - h))
    return QPoint(x, y)


class _AgentDropRow(QFrame):
    """One agent line in the dropdown: glyph + name + elided task + '?'."""

    activated = Signal(str)   # agent_id

    def __init__(self, agent, parent=None):
        super().__init__(parent)
        self.setObjectName("AgentDropRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.agent_id = agent.id

        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 5, 9, 5)
        lay.setSpacing(1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.glyph = QLabel(self)
        self.name = QLabel(self)
        self.name.setObjectName("AgentDropName")
        self.q = QLabel("?", self)          # waiting-for-input marker
        self.q.setObjectName("AgentDropQ")
        self.q.setToolTip("This agent is waiting for your input")
        self.q.hide()
        top.addWidget(self.glyph)
        top.addWidget(self.name, 1)
        top.addWidget(self.q)
        self.task = QLabel(self)
        self.task.setObjectName("AgentDropTask")
        lay.addLayout(top)
        lay.addWidget(self.task)
        self.refresh(agent)

    def refresh(self, agent) -> None:
        self.glyph.setText(_ICON.get(agent.status, "⚪"))
        self.name.setText(agent.spec.name)
        waiting = bool(getattr(agent, "is_waiting", lambda: False)())
        self.q.setVisible(waiting)
        self.setProperty("waiting", waiting)
        task = (agent.current_task or "").strip()
        fm = QFontMetrics(self.task.font())
        self.task.setText(fm.elidedText(task or "—", Qt.TextElideMode.ElideRight,
                                        _TASK_WIDTH))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.agent_id)
            event.accept()
            return
        super().mousePressEvent(event)


class AgentDropdown(QWidget):
    """The floating agent list for one workspace."""

    agentActivated = Signal(str, str)   # ws_id, agent_id

    def __init__(self, ws_id: str, agents: list, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("AgentDropdown")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._ws_id = ws_id
        self._agents = list(agents)
        self.setFixedWidth(DROPDOWN_WIDTH)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        header = QLabel(f"AGENTS · {len(self._agents)}", self)
        header.setObjectName("AgentDropHeader")
        lay.addWidget(header)

        self._rows: list[_AgentDropRow] = []
        if not self._agents:
            empty = QLabel("No agents in this workspace", self)
            empty.setObjectName("AgentDropTask")
            empty.setContentsMargins(9, 6, 9, 8)
            lay.addWidget(empty)
        for agent in self._agents:
            row = _AgentDropRow(agent, self)
            row.activated.connect(self._on_activated)
            lay.addWidget(row)
            self._rows.append(row)

        # keep the list live while it's open (status / "?" / task change)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start()

    def _refresh_all(self) -> None:
        by_id = {a.id: a for a in self._agents}
        for row in self._rows:
            agent = by_id.get(row.agent_id)
            if agent is None:
                continue
            try:                       # an agent may be disposed while open
                row.refresh(agent)
            except RuntimeError:
                continue
            # re-polish so the [waiting] style follows the live state
            row.style().unpolish(row)
            row.style().polish(row)

    def _on_activated(self, agent_id: str) -> None:
        self.agentActivated.emit(self._ws_id, agent_id)
        self.close()
