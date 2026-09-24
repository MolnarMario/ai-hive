"""The event log window: one timeline of every agent in every workspace.

Reads `EventHub.records` (see app/event_hub.py) and shows them newest first,
grouped under day separators. The workspace and agent names on each row are
links: a click jumps to that workspace, or reveals that agent's card, through
the same `MainWindow._reveal_agent` the sidebar and the Agent/File Map use.

It is a QListView over a flat model with a painted delegate rather than a
widget per row, because a month of a busy hive is thousands of rows. The
delegate lays a row out in one place (`_layout`) so painting and hit-testing
the links can never disagree about where a name is.

Two kinds of row stay open and keep updating in place: a question ("waiting
3m", then "answered after 6m") and a limit cut-off ("in 1h 12m", then "resumed
17:31"). The event that closes one is recorded on its own but rendered into
its row, which is what `EventHub.closers` is for.
"""

from __future__ import annotations

import time

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QRect,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QMenu, QPushButton,
                               QStyledItemDelegate, QStyle, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from .. import event_log as el
from .. import ui_theme
from ..ui_theme import Palette

HEADER, EVENT = 0, 1
RecordRole = Qt.ItemDataRole.UserRole + 1
KindRole = Qt.ItemDataRole.UserRole + 2

TIME_W, WS_W, AGENT_W, GLYPH_W, PAD = 64, 150, 110, 18, 10

_GLYPH = {el.PROMPT: "›", el.TASK: "»", el.QUESTION: "?",
          el.REPLY: "✓", el.LIMIT: "⏳", el.LIMIT_RESUMED: "↻",
          el.LIMIT_FAILED: "✕", el.LIMIT_DISMISSED: "∘",
          el.SCHED_SENT: "⏱", el.SCHED_MISSED: "⏱",
          el.CRASHED: "!", el.LIFECYCLE: "·", el.BOARD: "✎"}


def _kind_color(kind: str) -> str:
    return {el.PROMPT: Palette.ACCENT_GOLD, el.TASK: Palette.ACCENT_GOLD,
            el.QUESTION: Palette.YELLOW, el.REPLY: Palette.GREEN,
            el.LIMIT: Palette.ACCENT_ORANGE, el.LIMIT_RESUMED: Palette.GREEN,
            el.LIMIT_FAILED: Palette.RED, el.SCHED_MISSED: Palette.RED,
            el.CRASHED: Palette.RED}.get(kind, Palette.TEXT_DIM)


def _agent_ink(provider: str) -> str:
    ink = ui_theme.usage_pill_ink("codex" if provider == "openai" else provider)
    return ink or Palette.TEXT


def _day_key(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def _day_label(day: str, now: float) -> str:
    """A header's label, worked out when it is painted, so a window left open
    past midnight relabels yesterday's "Today"."""
    if day == _day_key(now):
        return "Today"
    if day == _day_key(now - 86400):
        return "Yesterday"
    try:
        return time.strftime("%A, %b %d", time.strptime(day, "%Y-%m-%d"))
    except ValueError:
        return day


class LogFilter:
    """What the window shows. Every field's empty value means "no filter", so
    a workspace or agent created later shows up without anyone ticking it."""

    def __init__(self):
        self.groups: set[str] = set(el.DEFAULT_GROUPS)
        self.only_ws: set[str] = set()        # empty = every workspace
        self.hidden_agents: set[str] = set()  # agent uids
        self.only_agents: set[str] = set()    # empty = every agent
        self.needs_you = False
        self.text = ""

    def is_narrowed(self) -> bool:
        return bool(self.only_ws or self.hidden_agents or self.only_agents
                    or self.needs_you or self.text
                    or self.groups != set(el.DEFAULT_GROUPS))

    def scope_only(self) -> bool:
        return bool(self.only_ws or self.hidden_agents or self.only_agents)

    def to_dict(self) -> dict:
        return {"groups": sorted(self.groups), "only_ws": sorted(self.only_ws),
                "hidden_agents": sorted(self.hidden_agents),
                "only_agents": sorted(self.only_agents),
                "needs_you": self.needs_you}

    def load(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        known = {g for g, *_ in el.GROUPS}
        if isinstance(d.get("groups"), list):
            self.groups = {g for g in d["groups"] if g in known}
        for key in ("only_ws", "hidden_agents", "only_agents"):
            if isinstance(d.get(key), list):
                setattr(self, key, {str(v) for v in d[key]})
        self.needs_you = bool(d.get("needs_you", False))

    def passes(self, rec: dict, closer, session_start: float) -> bool:
        kind = rec.get("kind")
        if kind in el.MERGED_CLOSERS:
            return False
        if el.GROUP_OF.get(kind) not in self.groups:
            return False
        if self.only_ws and rec.get("ws_id") not in self.only_ws:
            return False
        uid = rec.get("agent_uid", "")
        if uid and uid in self.hidden_agents:
            return False
        if self.only_agents and uid not in self.only_agents:
            return False
        if self.needs_you and not el.needs_attention(rec, closer,
                                                     session_start):
            return False
        if self.text:
            hay = " ".join((rec.get("agent_name", ""), rec.get("ws_name", ""),
                            el.describe(rec), rec.get("text", ""))).lower()
            if self.text.lower() not in hay:
                return False
        return True


class EventModel(QAbstractListModel):
    """Flat rows, newest first: ("header", "YYYY-MM-DD") and ("event",
    record)."""

    def __init__(self, hub, flt: LogFilter, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.flt = flt
        self.rows: list[tuple] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        kind, payload = self.rows[index.row()]
        if role == KindRole:
            return HEADER if kind == "header" else EVENT
        if role == RecordRole:
            return payload
        if role == Qt.ItemDataRole.DisplayRole:
            return (_day_label(payload, time.time()) if kind == "header"
                    else self.line(payload))
        if role == Qt.ItemDataRole.ToolTipRole and kind == "event":
            return self.tooltip(payload)
        return None

    def line(self, rec: dict) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["at"]))
        who = rec.get("agent_name") or ""
        where = rec.get("ws_name") or ""
        place = f"{who} in {where}" if who and where else (who or where)
        note = el.status(rec, self.hub.closer_of(rec), time.time(),
                         self.hub.session_start)
        return (f"[{stamp}] {place}: {el.describe(rec)}"
                + (f" ({note})" if note else ""))

    def tooltip(self, rec: dict) -> str:
        parts = [self.line(rec)]
        text = rec.get("text") or ""
        if text and text.strip() != el.first_line(text):
            parts.append(text.strip()[:el.TEXT_CAP])
        if rec.get("agent_uid") and self.hub.resolve(
                rec.get("ws_id", ""), rec["agent_uid"]) is None:
            parts.append("This agent is no longer open.")
        elif self.hub.manager.workspace(rec.get("ws_id", "")) is None:
            parts.append("This workspace is no longer open.")
        return "\n\n".join(parts)

    def rebuild(self) -> None:
        self.beginResetModel()
        self.rows = []
        last_day = None
        for rec in reversed(self.hub.records):
            if not self.flt.passes(rec, self.hub.closer_of(rec),
                                   self.hub.session_start):
                continue
            day = _day_key(rec["at"])
            if day != last_day:
                self.rows.append(("header", day))
                last_day = day
            self.rows.append(("event", rec))
        self.endResetModel()

    def add(self, rec: dict) -> int:
        """Insert a fresh record at the top. Returns rows inserted (0 when the
        filter hides it). A closer instead refreshes the row it closes."""
        ref = rec.get("ref")
        if ref:
            for i, (kind, payload) in enumerate(self.rows):
                if kind == "event" and payload.get("id") == ref:
                    idx = self.index(i)
                    self.dataChanged.emit(idx, idx)
                    break
        if not self.flt.passes(rec, self.hub.closer_of(rec),
                               self.hub.session_start):
            return 0
        today = _day_key(rec["at"])
        if self.rows and self.rows[0] == ("header", today):
            self.beginInsertRows(QModelIndex(), 1, 1)
            self.rows.insert(1, ("event", rec))
            self.endInsertRows()
            return 1
        self.beginInsertRows(QModelIndex(), 0, 1)
        self.rows[0:0] = [("header", today), ("event", rec)]
        self.endInsertRows()
        return 2


class EventDelegate(QStyledItemDelegate):

    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hover: tuple[int, str] | None = None   # (row, "ws"|"agent")

    def sizeHint(self, option, index) -> QSize:
        h = QFontMetrics(option.font).height()
        if index.data(KindRole) == HEADER:
            return QSize(200, h + 16)
        return QSize(200, h + 10)

    def _layout(self, rect: QRect, rec: dict, fm: QFontMetrics) -> dict:
        x = rect.left() + PAD
        out = {"time": QRect(x, rect.top(), TIME_W, rect.height())}
        x += TIME_W
        ws_text = fm.elidedText(rec.get("ws_name") or "", Qt.TextElideMode
                                .ElideRight, WS_W - PAD)
        out["ws"] = QRect(x, rect.top(), fm.horizontalAdvance(ws_text),
                          rect.height())
        out["ws_text"] = ws_text
        x += WS_W
        ag_text = fm.elidedText(rec.get("agent_name") or "", Qt.TextElideMode
                                .ElideRight, AGENT_W - PAD)
        out["agent"] = QRect(x, rect.top(), fm.horizontalAdvance(ag_text),
                             rect.height())
        out["agent_text"] = ag_text
        x += AGENT_W
        out["glyph"] = QRect(x, rect.top(), GLYPH_W, rect.height())
        x += GLYPH_W
        out["body"] = QRect(x, rect.top(), max(0, rect.right() - PAD - x),
                            rect.height())
        return out

    def _alive(self, rec: dict) -> tuple[bool, bool]:
        ws_ok = self.hub.manager.workspace(rec.get("ws_id", "")) is not None
        uid = rec.get("agent_uid", "")
        ag_ok = bool(uid) and ws_ok and self.hub.resolve(
            rec["ws_id"], uid) is not None
        return ws_ok, ag_ok

    def hit(self, rect: QRect, rec: dict, pos: QPoint,
            fm: QFontMetrics) -> str | None:
        lay = self._layout(rect, rec, fm)
        ws_ok, ag_ok = self._alive(rec)
        if ag_ok and lay["agent"].contains(pos):
            return "agent"
        if ws_ok and lay["ws"].contains(pos):
            return "ws"
        return None

    def paint(self, painter, option, index) -> None:
        painter.save()
        fm = QFontMetrics(option.font)
        r = option.rect
        if index.data(KindRole) == HEADER:
            f = QFont(option.font)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor(Palette.TEXT_DIM))
            label = index.data(Qt.ItemDataRole.DisplayRole)
            tr = r.adjusted(PAD, 6, -PAD, 0)
            painter.drawText(tr, Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter, label)
            lx = tr.left() + QFontMetrics(f).horizontalAdvance(label) + 10
            painter.setPen(QPen(QColor(Palette.BORDER), 1))
            y = tr.center().y()
            painter.drawLine(lx, y, r.right() - PAD, y)
            painter.restore()
            return
        rec = index.data(RecordRole)
        hub = self.hub
        closer = hub.closer_of(rec)
        now = time.time()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(r, QColor(Palette.BG_ACTIVE))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(r, QColor(Palette.BG_HOVER))
        if el.needs_attention(rec, closer, hub.session_start):
            painter.fillRect(QRect(r.left(), r.top() + 3, 3, r.height() - 6),
                             QColor(_kind_color(rec["kind"])))
        lay = self._layout(r, rec, fm)
        va = Qt.AlignmentFlag.AlignVCenter
        painter.setPen(QColor(Palette.TEXT_FAINT))
        painter.drawText(lay["time"], va | Qt.AlignmentFlag.AlignLeft,
                         time.strftime("%H:%M:%S", time.localtime(rec["at"])))
        ws_ok, ag_ok = self._alive(rec)
        row = index.row()
        for key, ok, color in (("ws", ws_ok, Palette.TEXT),
                               ("agent", ag_ok,
                                _agent_ink(rec.get("provider", "")))):
            f = QFont(option.font)
            f.setUnderline(ok and self.hover == (row, key))
            f.setStrikeOut(not ok and bool(lay[key + "_text"]))
            painter.setFont(f)
            painter.setPen(QColor(color if ok else Palette.TEXT_FAINT))
            painter.drawText(lay[key].adjusted(0, 0, 4, 0),
                             va | Qt.AlignmentFlag.AlignLeft,
                             lay[key + "_text"])
        painter.setFont(option.font)
        painter.setPen(QColor(_kind_color(rec["kind"])))
        painter.drawText(lay["glyph"], va | Qt.AlignmentFlag.AlignLeft,
                         _GLYPH.get(rec["kind"], "·"))
        note = el.status(rec, closer, now, hub.session_start)
        body = lay["body"]
        if note:
            nw = fm.horizontalAdvance(note)
            painter.setPen(QColor(Palette.TEXT_DIM if closer or not
                                  el.needs_attention(rec, closer,
                                                     hub.session_start)
                                  else _kind_color(rec["kind"])))
            painter.drawText(body, va | Qt.AlignmentFlag.AlignRight, note)
            body = body.adjusted(0, 0, -(nw + 16), 0)
        painter.setPen(QColor(Palette.TEXT if rec["kind"] not in (
            el.LIFECYCLE, el.BOARD, el.SCHED_SENT) else Palette.TEXT_DIM))
        painter.drawText(body, va | Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(el.describe(rec),
                                       Qt.TextElideMode.ElideRight,
                                       max(0, body.width())))
        painter.restore()


class EventListView(QListView):
    linkActivated = Signal(str, dict)     # "ws" | "agent", record
    menuRequested = Signal(QPoint, dict)  # global pos, record

    def __init__(self, delegate: EventDelegate, parent=None):
        super().__init__(parent)
        self.setObjectName("EventLogList")
        self._delegate = delegate
        self.setItemDelegate(delegate)
        self.setMouseTracking(True)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu)

    def _hit(self, pos: QPoint):
        idx = self.indexAt(pos)
        if not idx.isValid() or idx.data(KindRole) != EVENT:
            return None, None
        rec = idx.data(RecordRole)
        return idx, (rec, self._delegate.hit(self.visualRect(idx), rec, pos,
                                              self.fontMetrics()))

    def mouseMoveEvent(self, e) -> None:
        super().mouseMoveEvent(e)
        idx, hit = self._hit(e.position().toPoint())
        link = hit[1] if hit else None
        new = (idx.row(), link) if link else None
        if new != self._delegate.hover:
            self._delegate.hover = new
            self.viewport().update()
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if link
                                  else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, e) -> None:
        self._delegate.hover = None
        self.viewport().update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        if e.button() != Qt.MouseButton.LeftButton:
            return
        _idx, hit = self._hit(e.position().toPoint())
        if hit and hit[1]:
            self.linkActivated.emit(hit[1], hit[0])

    def mouseDoubleClickEvent(self, e) -> None:
        _idx, hit = self._hit(e.position().toPoint())
        if hit:
            self.linkActivated.emit("agent" if hit[0].get("agent_uid")
                                    else "ws", hit[0])
            return
        super().mouseDoubleClickEvent(e)

    def _on_menu(self, pos: QPoint) -> None:
        _idx, hit = self._hit(pos)
        if hit:
            self.menuRequested.emit(self.viewport().mapToGlobal(pos), hit[0])


class ScopePopup(QFrame):
    """The Workspaces & agents picker: a checkable tree, each workspace with
    its agents under it. Unticking a workspace hides it; unticking one agent
    hides just that agent."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("EventLogScope")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumSize(280, 320)
        lay.addWidget(self.tree)
        self.tree.itemChanged.connect(self._on_item_changed)
        self._filling = False

    def fill(self, workspaces: list, flt: LogFilter) -> None:
        """`workspaces` is [(ws_id, name, [(uid, name), ...]), ...]."""
        self._filling = True
        self.tree.clear()
        for ws_id, name, agents in workspaces:
            wi = QTreeWidgetItem(self.tree, [name or "(unnamed)"])
            wi.setData(0, Qt.ItemDataRole.UserRole, ("ws", ws_id))
            wi.setFlags(wi.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            ws_on = not flt.only_ws or ws_id in flt.only_ws
            for uid, aname in agents:
                ai = QTreeWidgetItem(wi, [aname])
                ai.setData(0, Qt.ItemDataRole.UserRole, ("agent", uid))
                ai.setFlags(ai.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                on = (ws_on and uid not in flt.hidden_agents
                      and (not flt.only_agents or uid in flt.only_agents))
                ai.setCheckState(0, Qt.CheckState.Checked if on
                                 else Qt.CheckState.Unchecked)
            wi.setCheckState(0, Qt.CheckState.Checked if ws_on
                             else Qt.CheckState.Unchecked)
            wi.setExpanded(True)
        self._filling = False

    def _on_item_changed(self, item, _col) -> None:
        if self._filling:
            return
        self._filling = True
        kind, _key = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "ws":
            state = item.checkState(0)
            for i in range(item.childCount()):
                item.child(i).setCheckState(0, state)
        else:
            parent = item.parent()
            if parent is not None and item.checkState(0) == Qt.CheckState.Checked:
                parent.setCheckState(0, Qt.CheckState.Checked)
        self._filling = False
        self.changed.emit()

    def read_into(self, flt: LogFilter) -> None:
        only_ws, hidden, all_ws = set(), set(), True
        for i in range(self.tree.topLevelItemCount()):
            wi = self.tree.topLevelItem(i)
            _k, ws_id = wi.data(0, Qt.ItemDataRole.UserRole)
            if wi.checkState(0) == Qt.CheckState.Checked:
                only_ws.add(ws_id)
                for j in range(wi.childCount()):
                    ai = wi.child(j)
                    if ai.checkState(0) != Qt.CheckState.Checked:
                        hidden.add(ai.data(0, Qt.ItemDataRole.UserRole)[1])
            else:
                all_ws = False
        flt.only_ws = set() if all_ws else only_ws
        flt.hidden_agents = hidden
        flt.only_agents = set()


class EventLogWindow(QWidget):
    """Top-level, non-modal, reused across opens (like AgentFileMapWindow)."""

    agentActivated = Signal(str, str)      # ws_id, live agent id
    workspaceActivated = Signal(str)       # ws_id
    stateChanged = Signal()                # filters changed (persist)

    TICK_MS = 15000

    def __init__(self, hub, parent=None, state: dict | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("EventLogWindow")
        self.setWindowTitle("AI Hive event log")
        self.resize(980, 620)
        self.hub = hub
        self.flt = LogFilter()
        self.flt.load((state or {}).get("filter") or {})
        self._unseen = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame(self)
        bar.setObjectName("WorkspaceHeader")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(6)
        self.scope_btn = QToolButton(bar)
        self.scope_btn.setObjectName("LogScopeBtn")
        self.scope_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scope_btn.clicked.connect(self._open_scope)
        bl.addWidget(self.scope_btn)
        self.chips: dict[str, QPushButton] = {}
        for key, label, _kinds, _on in el.GROUPS:
            b = QPushButton(label, bar)
            b.setObjectName("LogChip")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setChecked(key in self.flt.groups)
            b.toggled.connect(lambda on, k=key: self._on_chip(k, on))
            self.chips[key] = b
            bl.addWidget(b)
        bl.addStretch(1)
        self.needs_btn = QPushButton("Needs you", bar)
        self.needs_btn.setObjectName("LogNeedsBtn")
        self.needs_btn.setCheckable(True)
        self.needs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.needs_btn.setToolTip("Only unanswered questions, crashes, missed "
                                  "scheduled messages and failed resumes")
        self.needs_btn.setChecked(self.flt.needs_you)
        self.needs_btn.toggled.connect(self._on_needs)
        bl.addWidget(self.needs_btn)
        self.search = QLineEdit(bar)
        self.search.setObjectName("LogSearch")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(170)
        self.search.textChanged.connect(self._on_search)
        bl.addWidget(self.search)
        root.addWidget(bar)

        info = QFrame(self)
        info.setObjectName("LogInfoRow")
        il = QHBoxLayout(info)
        il.setContentsMargins(12, 3, 12, 3)
        self.count_label = QLabel(info)
        self.count_label.setObjectName("HeaderPath")
        il.addWidget(self.count_label)
        self.clear_btn = QPushButton("Clear filters", info)
        self.clear_btn.setObjectName("LogLinkBtn")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_filters)
        il.addWidget(self.clear_btn)
        il.addStretch(1)
        self.new_btn = QPushButton(info)
        self.new_btn.setObjectName("LogLinkBtn")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self._jump_top)
        il.addWidget(self.new_btn)
        root.addWidget(info)

        self.model = EventModel(hub, self.flt, self)
        self.delegate = EventDelegate(hub, self)
        self.view = EventListView(self.delegate, self)
        self.view.setModel(self.model)
        self.view.linkActivated.connect(self._on_link)
        self.view.menuRequested.connect(self._on_menu)
        self.view.verticalScrollBar().valueChanged.connect(
            self._on_scrolled)
        root.addWidget(self.view, 1)

        self.empty = QLabel(self.view.viewport())
        self.empty.setObjectName("HeaderPath")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.scope = ScopePopup(self)
        self.scope.changed.connect(self._on_scope_changed)

        self._tick = QTimer(self)
        self._tick.setInterval(self.TICK_MS)
        self._tick.timeout.connect(self.view.viewport().update)
        self._tick.start()

        hub.appended.connect(self._on_appended)
        geo = (state or {}).get("size")
        if isinstance(geo, list) and len(geo) == 2:
            self.resize(max(600, int(geo[0])), max(300, int(geo[1])))
        self.refresh()

    # ------------------------------------------------------------ state ---

    def state(self) -> dict:
        return {"filter": self.flt.to_dict(),
                "size": [self.width(), self.height()]}

    def refresh(self) -> None:
        self.model.rebuild()
        self._unseen = 0
        self._sync_chrome()

    def clear_filters(self) -> None:
        self.flt.__init__()
        for key, b in self.chips.items():
            b.blockSignals(True)
            b.setChecked(key in self.flt.groups)
            b.blockSignals(False)
        self.needs_btn.blockSignals(True)
        self.needs_btn.setChecked(False)
        self.needs_btn.blockSignals(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._changed()

    def _changed(self) -> None:
        self.refresh()
        self.stateChanged.emit()

    def _sync_chrome(self) -> None:
        shown = sum(1 for k, _p in self.model.rows if k == "event")
        self.count_label.setText(f"{shown} event{'s' if shown != 1 else ''}")
        self.clear_btn.setVisible(self.flt.is_narrowed())
        n = self._unseen
        self.new_btn.setText(f"{n} new ↑")
        self.new_btn.setVisible(n > 0)
        scope = "All workspaces"
        if self.flt.scope_only():
            parts = []
            if self.flt.only_ws:
                parts.append(f"{len(self.flt.only_ws)} workspace"
                             f"{'s' if len(self.flt.only_ws) != 1 else ''}")
            if self.flt.only_agents:
                parts.append(f"{len(self.flt.only_agents)} agent"
                             f"{'s' if len(self.flt.only_agents) != 1 else ''}")
            if self.flt.hidden_agents:
                parts.append(f"{len(self.flt.hidden_agents)} hidden")
            scope = ", ".join(parts) or scope
        self.scope_btn.setText(scope + " ▾")
        self.scope_btn.setProperty("narrowed", self.flt.scope_only())
        ui_theme.repolish(self.scope_btn)
        if shown:
            self.empty.hide()
        else:
            self.empty.setText("Nothing matches these filters."
                               if self.flt.is_narrowed()
                               else "Nothing has happened yet.")
            self.empty.resize(self.view.viewport().size())
            self.empty.show()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.empty.resize(self.view.viewport().size())

    # ------------------------------------------------------------ input ---

    def _on_chip(self, key: str, on: bool) -> None:
        (self.flt.groups.add if on else self.flt.groups.discard)(key)
        self._changed()

    def _on_needs(self, on: bool) -> None:
        self.flt.needs_you = on
        self._changed()

    def _on_search(self, text: str) -> None:
        self.flt.text = text.strip()
        self.refresh()

    def _scope_rows(self) -> list:
        """Every workspace and agent the log knows: the live ones first, in
        sidebar order, then the ones only old records remember."""
        out, seen_ws = [], set()
        by_ws: dict[str, dict] = {}
        for rec in self.hub.records:
            if rec.get("agent_uid"):
                by_ws.setdefault(rec.get("ws_id", ""), {})[
                    rec["agent_uid"]] = rec.get("agent_name", "")
        names = {r.get("ws_id"): r.get("ws_name") for r in self.hub.records}
        for ws in self.hub.manager.workspaces:
            agents = dict(by_ws.get(ws.id, {}))
            for a in ws.agents:
                agents[a.spec.uid] = a.spec.name
            out.append((ws.id, ws.name, sorted(agents.items(),
                                               key=lambda kv: kv[1].lower())))
            seen_ws.add(ws.id)
        for ws_id, agents in by_ws.items():
            if ws_id and ws_id not in seen_ws:
                out.append((ws_id, f"{names.get(ws_id) or ws_id} (closed)",
                            sorted(agents.items(), key=lambda kv: kv[1])))
        return out

    def _open_scope(self) -> None:
        self.scope.fill(self._scope_rows(), self.flt)
        self.scope.move(self.scope_btn.mapToGlobal(
            QPoint(0, self.scope_btn.height() + 2)))
        self.scope.show()

    def _on_scope_changed(self) -> None:
        self.scope.read_into(self.flt)
        self._changed()

    def _on_link(self, which: str, rec: dict) -> None:
        ws_id = rec.get("ws_id", "")
        if which == "agent":
            agent = self.hub.resolve(ws_id, rec.get("agent_uid", ""))
            if agent is not None:
                self.agentActivated.emit(ws_id, agent.id)
                return
        if self.hub.manager.workspace(ws_id) is not None:
            self.workspaceActivated.emit(ws_id)

    def _on_menu(self, gpos: QPoint, rec: dict) -> None:
        menu = QMenu(self)
        ws_id, uid = rec.get("ws_id", ""), rec.get("agent_uid", "")
        if ws_id:
            menu.addAction(f"Show only {rec.get('ws_name') or 'this workspace'}",
                           lambda: self._only_ws(ws_id))
        if uid:
            menu.addAction(f"Show only {rec.get('agent_name') or 'this agent'}",
                           lambda: self._only_agent(ws_id, uid))
            menu.addAction(f"Hide {rec.get('agent_name') or 'this agent'}",
                           lambda: self._hide_agent(uid))
        if self.flt.is_narrowed():
            menu.addAction("Clear filters", self.clear_filters)
        menu.addSeparator()
        menu.addAction("Copy line", lambda: QGuiApplication.clipboard()
                       .setText(self.model.line(rec)))
        menu.exec(gpos)

    def _only_ws(self, ws_id: str) -> None:
        self.flt.only_ws = {ws_id}
        self.flt.only_agents = set()
        self._changed()

    def _only_agent(self, ws_id: str, uid: str) -> None:
        self.flt.only_ws = {ws_id}
        self.flt.only_agents = {uid}
        self.flt.hidden_agents.discard(uid)
        self._changed()

    def _hide_agent(self, uid: str) -> None:
        self.flt.hidden_agents.add(uid)
        self.flt.only_agents.discard(uid)
        self._changed()

    # ------------------------------------------------------------- live ---

    def _on_appended(self, rec: dict) -> None:
        if self.flt.needs_you and rec.get("ref"):
            # a closer can take its row OUT of "Needs you"
            self.refresh()
            return
        bar = self.view.verticalScrollBar()
        before = bar.value()
        added = self.model.add(rec)
        if added and before > 0:
            # keep what the reader is looking at where it is: the rows went in
            # above it, so move down by exactly their height once laid out
            delta = sum(self.view.sizeHintForIndex(self.model.index(i))
                        .height() for i in range(added))
            QTimer.singleShot(0, lambda: bar.setValue(before + delta))
            self._unseen += 1
        self._sync_chrome()

    def _on_scrolled(self, value: int) -> None:
        if value == 0 and self._unseen:
            self._unseen = 0
            self._sync_chrome()

    def _jump_top(self) -> None:
        self.view.verticalScrollBar().setValue(0)
        self._unseen = 0
        self._sync_chrome()
