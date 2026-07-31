"""Sidebar: the WORKSPACES panel — rows, badges, inline rename, collapse,
drag-reorder, (M2) collapsible categories, and an inline file explorer.

The panel is a model-driven `QTreeWidget`: `Sidebar._nodes` is the ordered list
of top-level nodes (workspaces and, in M2, categories with workspace children),
and `rebuild()` recreates the tree's items + custom row widgets from that model.
Drag-and-drop never moves Qt items directly (that would strand the rich
`setItemWidget` row widgets); instead a drop mutates `_nodes` and rebuilds, which
keeps ordering, category membership, and persistence driven by a single source
of truth. Rows themselves initiate the `QDrag` (they cover the viewport, so the
view can't), and the tree resolves the drop target + position.

Two things expand INLINE under a workspace row as child items (folder-tree
style), each toggled by a hover control and both rebuilt from state — never by
moving items: the AGENT list (count-badge / "?" click → `_expanded_ws`) and the
FILE explorer (the ▸ toggle → `_expanded_files`). The file explorer is a lazy,
VS Code-style tree: `_add_file_rows`/`_add_dir_children` scandir the workspace
root (`files_root_provider`) and recurse only into directories the user has
opened (`_expanded_dirs`, relative paths). A `QFileSystemWatcher` on the visible
dirs refreshes it when files change on disk. `reveal_file` scrolls to +
highlights a file (used when a path is Ctrl+clicked in a conversation). The
coarse per-workspace open/closed set persists (`filesToggled` → the session
"ui" blob); per-directory expansion + the reveal highlight are transient like
the agent expansion — in-memory, rebuild-only, never marking the session dirty.

The header also has a SEARCH toggle (magnifier next to "add category"): clicking
it reveals an overlay `QLineEdit` positioned by hand over the WORKSPACES title +
count (`_position_search`), and each keystroke recomputes highlight sets
(`_apply_search`) — a workspace matches on its name or on any agent's
name/summary (`_agent_haystack`), a matching agent's workspace is auto-expanded
to reveal it, and matches get a `search_hit` tint (`set_search_hit`). Search is
transient too: it saves/restores the pre-search expansion and never persists.
"""

import os
import uuid

from PySide6.QtCore import (QEasingCurve, QEvent, QFileSystemWatcher, QMimeData,
                            QPoint, QPropertyAnimation, QRect, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QDrag, QFont, QFontMetrics, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..filetypes import EMOJI_FONT, FOLDER_ICON, FOLDER_OPEN_ICON, file_icon
from ..terminal_agent import AgentStatus
from ..ui_theme import Palette, repolish
from .activity_panel import _ICON
from .ornaments import AgentCountBadge, OrnamentDivider, WorkspaceSpinner

SIDEBAR_WIDTH = 230
ROW_HEIGHT = 44
CAT_HEIGHT = 32
AGENT_HEIGHT = 34
TREE_ROW_HEIGHT = 24
# soft cap on entries shown per directory — a huge folder (node_modules) would
# otherwise stall paint; the overflow collapses to a muted "… N more" row
FILE_TREE_CAP = 800


def _agent_haystack(agent) -> str:
    """Lowercased 'name + summary' text for sidebar search matching. The summary
    is the agent's live one-liner (assigned task, else its AI conversation
    title) — the same text shown beside the name in the inline agent list."""
    name = getattr(getattr(agent, "spec", None), "name", "") or ""
    get = getattr(agent, "summary", None)
    summary = (get() if callable(get) else getattr(agent, "current_task", "")) or ""
    return f"{name} {summary}".lower()

# drag payload: b"workspace:<id>" or b"category:<id>"
NODE_MIME = "application/x-aihive-sidebar-node"


class WorkspaceRow(QFrame):
    """One sidebar workspace row: status badge, name (+ inline rename), folder,
    delete, working-count spinner. Initiates a drag past the drag threshold."""

    selected = Signal(str)              # ws_id
    renameCommitted = Signal(str, str)  # ws_id, new name
    deleteRequested = Signal(str)       # ws_id
    openFolderRequested = Signal(str)   # ws_id
    agentsRequested = Signal(str)       # ws_id (count-badge clicked; M3)
    filesRequested = Signal(str)        # ws_id (file-tree toggle clicked)

    def __init__(self, ws_id: str, name: str, folder: str, parent=None):
        super().__init__(parent)
        self.ws_id = ws_id
        self._renaming = False
        self._folder = folder
        self._press_pos: QPoint | None = None
        self._last_stats = {"total": 0, "active": 0, "idle": 0, "error": 0}
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ROW_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)  # right margin mirrors the left
        lay.setSpacing(8)

        # the agent tally doubles as the workspace's status light
        self.count_badge = AgentCountBadge(self)
        self.count_badge.setObjectName("WsCount")
        self.count_badge.clicked.connect(
            lambda: self.agentsRequested.emit(self.ws_id))

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        self.name_label = QLabel(name, self)
        self.name_label.setObjectName("WsName")
        self.rename_edit = QLineEdit(self)
        self.rename_edit.setObjectName("WsRenameEdit")
        self.rename_edit.hide()
        text_col.addWidget(self.name_label)
        text_col.addWidget(self.rename_edit)

        # inline file-tree toggle (a caret that turns as the tree opens) — a
        # SEPARATE control from folder_btn: this expands the file explorer under
        # the row, folder_btn opens the folder in the OS file manager. Unlike the
        # hover-only folder/delete buttons, this caret is ALWAYS visible (it sits
        # between the count badge and the name) so the file explorer is a
        # first-class, discoverable affordance rather than a hover surprise.
        self.tree_btn = QToolButton(self)
        self.tree_btn.setObjectName("WsTreeBtn")
        self.tree_btn.setText("▸")
        self.tree_btn.setToolTip("Show files")
        self.tree_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tree_btn.clicked.connect(
            lambda: self.filesRequested.emit(self.ws_id))

        self.folder_btn = QToolButton(self)
        self.folder_btn.setObjectName("WsFolderBtn")
        self.folder_btn.setText("📁")  # filled folder reads far better than 🗀
        self.folder_btn.setToolTip("Open workspace folder")
        self.folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_btn.hide()
        self.folder_btn.clicked.connect(
            lambda: self.openFolderRequested.emit(self.ws_id))

        self.delete_btn = QToolButton(self)
        self.delete_btn.setObjectName("WsDelete")
        self.delete_btn.setText("✖")  # heavy multiplication x — thicker than ✕
        self.delete_btn.setToolTip("Delete workspace")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.hide()
        self.delete_btn.clicked.connect(
            lambda: self.deleteRequested.emit(self.ws_id))

        # "?" notification: an agent here is waiting for the user (a permission
        # prompt or a question). Clicking it opens the agent dropdown so the
        # user can see WHICH agent and jump to it.
        self.q_badge = QToolButton(self)
        self.q_badge.setObjectName("WsQ")
        self.q_badge.setText("?")
        self.q_badge.setToolTip("An agent is waiting for your input")
        self.q_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.q_badge.hide()
        self.q_badge.clicked.connect(
            lambda: self.agentsRequested.emit(self.ws_id))

        # a sweeping-arc throbber with the WORKING count; pinned far-right, so
        # the hover folder/delete buttons appear to its LEFT (see layout order)
        self.work_spinner = WorkspaceSpinner(self)
        self.work_spinner.setObjectName("WsSpinner")

        lay.addWidget(self.count_badge)
        lay.addWidget(self.tree_btn)      # always-visible file-explorer caret
        lay.addLayout(text_col, 1)
        lay.addWidget(self.folder_btn)
        lay.addWidget(self.delete_btn)
        lay.addWidget(self.q_badge)
        lay.addWidget(self.work_spinner)

        self.rename_edit.returnPressed.connect(self._commit_rename)
        self.rename_edit.installEventFilter(self)

    # ------------------------------------------------------------- state ---

    def set_active(self, active: bool) -> None:
        if self.property("active") != active:
            self.setProperty("active", active)
            repolish(self)
        self._update_hover_buttons(hovered=self.underMouse())

    def set_search_hit(self, hit: bool) -> None:
        """Tint the row when it matches the active sidebar search."""
        if bool(self.property("search_hit")) != bool(hit):
            self.setProperty("search_hit", bool(hit))
            repolish(self)

    def set_name(self, name: str) -> None:
        self.name_label.setText(name)

    def set_folder(self, folder: str) -> None:
        self._folder = folder
        self.set_stats(self._last_stats)  # folder lives in the tooltip now

    def set_stats(self, stats: dict) -> None:
        self._last_stats = dict(stats)
        busy = stats.get("busy", 0)      # actively streaming output = working
        running = stats.get("active", 0)  # process alive (may be idle at prompt)
        e = stats.get("error", 0)
        total = stats.get("total", 0)
        # amber pulses ONLY when an agent is actually working; a running-but-
        # quiet agent (standby, no errors) reads green. Working wins over a
        # stale error, so an active row is amber even if a sibling errored.
        state = ("empty" if total == 0
                 else "working" if busy > 0
                 else "error" if e > 0
                 else "idle")
        self.count_badge.set_state(total, state)
        # the right-edge spinner mirrors just the working count (hidden at 0)
        self.work_spinner.set_count(busy)
        # the "?" shows when any agent is waiting for the user (hidden at 0)
        waiting = stats.get("waiting", 0)
        self.q_badge.setVisible(waiting > 0)
        if waiting > 0:
            self.q_badge.setToolTip(
                f"{waiting} agent(s) waiting for your input — click to see who")
        tip = (f"{total} agent(s): {busy} working, {running} running, "
               f"{e} error, {waiting} waiting")
        self.setToolTip(f"{tip}\n{self._folder}" if self._folder else tip)

    # ------------------------------------------------------------ rename ---

    def start_rename(self) -> None:
        self._renaming = True
        self.rename_edit.setText(self.name_label.text())
        self.name_label.hide()
        self.rename_edit.show()
        self.rename_edit.setFocus()
        self.rename_edit.selectAll()

    def _commit_rename(self) -> None:
        # Guard against re-entrancy: _end_rename()'s hide() delivers a
        # synchronous FocusOut which routes right back here. Without the
        # flag, Escape would commit instead of cancel and Enter would
        # commit twice.
        if not self._renaming:
            return
        name = self.rename_edit.text().strip()
        self._end_rename()
        if name and name != self.name_label.text():
            self.renameCommitted.emit(self.ws_id, name)

    def _end_rename(self) -> None:
        self._renaming = False
        self.rename_edit.hide()
        self.name_label.show()

    # ------------------------------------------------------------ events ---

    def eventFilter(self, obj, event):
        if obj is self.rename_edit:
            if (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key.Key_Escape):
                self._end_rename()  # true cancel
                return True
            if event.type() == QEvent.Type.FocusOut:
                # a context-menu popup is not a real focus loss
                if event.reason() != Qt.FocusReason.PopupFocusReason:
                    self._commit_rename()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        self.selected.emit(self.ws_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # start a drag once the pointer leaves the click threshold (but never
        # while inline-renaming, which owns the mouse for text selection)
        if (self._press_pos is not None and not self._renaming
                and event.buttons() & Qt.MouseButton.LeftButton):
            moved = (event.position().toPoint()
                     - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                start_node_drag(self, "workspace", self.ws_id)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.start_rename()
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event):
        self._update_hover_buttons(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._update_hover_buttons(hovered=False)
        super().leaveEvent(event)

    def set_files_open(self, is_open: bool) -> None:
        """Reflect the file-tree open state in the toggle caret (▾ open / ▸)."""
        self.tree_btn.setText("▾" if is_open else "▸")
        self.tree_btn.setToolTip("Hide files" if is_open else "Show files")

    def _update_hover_buttons(self, hovered: bool) -> None:
        # folder/delete appear ONLY while hovering the row (not on the active
        # row) so the workspace name keeps the full width the rest of the time;
        # the count badge carries the workspace's status at all times, and the
        # file-explorer caret (tree_btn) is always visible up front by the name
        self.delete_btn.setVisible(hovered)
        self.folder_btn.setVisible(hovered)


class CategoryRow(QFrame):
    """A collapsible category header: disclosure caret + editable name + a
    child-count chip + hover delete. Draggable (categories reorder at the top
    level only — they never nest). Toggling the caret expands/collapses its
    workspaces."""

    toggled = Signal(str)               # cat_id (caret clicked)
    renameCommitted = Signal(str, str)  # cat_id, new name
    deleteRequested = Signal(str)       # cat_id

    def __init__(self, cat_id: str, name: str, collapsed: bool,
                 count: int = 0, parent=None):
        super().__init__(parent)
        self.cat_id = cat_id
        self._renaming = False
        self._collapsed = collapsed
        self._press_pos: QPoint | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("WsCategory")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(CAT_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 10, 2)
        lay.setSpacing(6)

        self.caret = QToolButton(self)
        self.caret.setObjectName("CatCaret")
        self.caret.setCursor(Qt.CursorShape.PointingHandCursor)
        self.caret.setText("▸" if collapsed else "▾")
        self.caret.clicked.connect(lambda: self.toggled.emit(self.cat_id))

        self.name_label = QLabel(name, self)
        self.name_label.setObjectName("CatName")
        self.rename_edit = QLineEdit(self)
        self.rename_edit.setObjectName("WsRenameEdit")
        self.rename_edit.hide()
        self.rename_edit.returnPressed.connect(self._commit_rename)
        self.rename_edit.installEventFilter(self)

        self.count_label = QLabel(str(count), self)
        self.count_label.setObjectName("CatCount")

        self.delete_btn = QToolButton(self)
        self.delete_btn.setObjectName("WsDelete")
        self.delete_btn.setText("✖")  # heavy multiplication x — thicker than ✕
        self.delete_btn.setToolTip("Delete category (its workspaces move out)")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.hide()
        self.delete_btn.clicked.connect(
            lambda: self.deleteRequested.emit(self.cat_id))

        lay.addWidget(self.caret)
        lay.addWidget(self.name_label, 1)
        lay.addWidget(self.rename_edit, 1)
        lay.addWidget(self.count_label)
        lay.addWidget(self.delete_btn)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.caret.setText("▸" if collapsed else "▾")

    def set_count(self, count: int) -> None:
        self.count_label.setText(str(count))

    # ------------------------------------------------------------ rename ---

    def start_rename(self) -> None:
        self._renaming = True
        self.rename_edit.setText(self.name_label.text())
        self.name_label.hide()
        self.rename_edit.show()
        self.rename_edit.setFocus()
        self.rename_edit.selectAll()

    def _commit_rename(self) -> None:
        if not self._renaming:
            return
        name = self.rename_edit.text().strip()
        self._end_rename()
        if name and name != self.name_label.text():
            self.renameCommitted.emit(self.cat_id, name)

    def _end_rename(self) -> None:
        self._renaming = False
        self.rename_edit.hide()
        self.name_label.show()

    def eventFilter(self, obj, event):
        if obj is self.rename_edit:
            if (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key.Key_Escape):
                self._end_rename()
                return True
            if event.type() == QEvent.Type.FocusOut:
                if event.reason() != Qt.FocusReason.PopupFocusReason:
                    self._commit_rename()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------ events ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press_pos is not None and not self._renaming
                and event.buttons() & Qt.MouseButton.LeftButton):
            moved = (event.position().toPoint()
                     - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                start_node_drag(self, "category", self.cat_id)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.start_rename()
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event):
        self.delete_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.delete_btn.setVisible(False)
        super().leaveEvent(event)


def start_node_drag(widget: QWidget, kind: str, node_id: str) -> None:
    """Begin a sidebar drag from a row/header widget. The payload is the node's
    kind+id; the sidebar tree resolves the drop target and rebuilds its model."""
    drag = QDrag(widget)
    mime = QMimeData()
    mime.setData(NODE_MIME, f"{kind}:{node_id}".encode("utf-8"))
    drag.setMimeData(mime)
    pm = QPixmap(widget.size())
    widget.render(pm)
    drag.setPixmap(pm)
    drag.setHotSpot(QPoint(16, widget.height() // 2))
    drag.exec(Qt.DropAction.MoveAction)


class _SidebarTree(QTreeWidget):
    """The workspace/category tree. Rows initiate drags; this view only handles
    drops: it resolves the target node + relative position and emits
    `nodeDropped` for the Sidebar to reorder its model and rebuild."""

    # kind, node_id (dragged), target_id ("" = empty space), position
    nodeDropped = Signal(str, str, str, str)  # position: above|below|on

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setRootIsDecorated(False)  # category rows draw their own caret
        self.setIndentation(14)
        self.setUniformRowHeights(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        # accept drops + show the built-in indicator, but never start view drags
        # (the row widgets start them) — DragDrop mode + drag disabled does that
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(NODE_MIME):
            super().dragMoveEvent(event)   # positions the drop indicator
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        data = event.mimeData().data(NODE_MIME)
        if not data:
            super().dropEvent(event)
            return
        kind, _, node_id = bytes(data.data()).decode("utf-8").partition(":")
        target = self.itemAt(event.position().toPoint())
        ind = self.dropIndicatorPosition()
        Ind = QAbstractItemView.DropIndicatorPosition
        if ind == Ind.AboveItem:
            position = "above"
        elif ind == Ind.OnItem:
            position = "on"
        else:                              # BelowItem / OnViewport
            position = "below"
        target_id = ""
        if target is not None:
            info = target.data(0, Qt.ItemDataRole.UserRole) or {}
            target_id = info.get("id", "")
        event.acceptProposedAction()
        self.nodeDropped.emit(kind, node_id, target_id, position)

    # ---- category container: a tinted box behind a category + its visible ----
    # descendants (workspaces, and their agents when expanded). Composed per
    # row in drawRow, so it grows/shrinks automatically with any expand/collapse.

    def _row_category(self, index) -> str:
        """The id of the top-level category this row belongs to, or "" if the
        row isn't inside a category."""
        item = self.itemFromIndex(index)
        if item is None:
            return ""
        top = item
        while top.parent() is not None:
            top = top.parent()
        info = top.data(0, Qt.ItemDataRole.UserRole) or {}
        return info.get("id", "") if info.get("type") == "category" else ""

    def _is_category_header(self, index) -> bool:
        item = self.itemFromIndex(index)
        return (item is not None and item.parent() is None
                and (item.data(0, Qt.ItemDataRole.UserRole)
                     or {}).get("type") == "category")

    def drawRow(self, painter, option, index):
        cat = self._row_category(index)
        if cat:
            r = option.rect
            x0, x1 = 3, self.viewport().width() - 3
            # The group box is NEUTRAL, not accent-tinted: the theme accent is
            # reserved for the SELECTED workspace (see ui_theme's active-row
            # QSS), so a categorized row can never be mistaken for the current
            # one. TEXT_DIM is the theme's muted neutral, so the box still fits
            # each skin (warm gray on parchment, cool slate on Obsidian, ...).
            group = QColor(Palette.TEXT_DIM)
            fill = QColor(group)
            fill.setAlpha(16)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.fillRect(QRect(x0, r.top(), x1 - x0, r.height()), fill)
            bar = QColor(group)
            bar.setAlpha(130)
            painter.fillRect(QRect(x0, r.top(), 2, r.height()), bar)
            edge = QColor(group)
            edge.setAlpha(80)
            painter.setPen(QPen(edge, 1))
            painter.drawLine(x1, r.top(), x1, r.bottom())
            if self._is_category_header(index):
                painter.drawLine(x0, r.top(), x1, r.top())
            below = self.indexBelow(index)          # last visible row of group?
            if not below.isValid() or self._row_category(below) != cat:
                painter.drawLine(x0, r.bottom(), x1, r.bottom())
            painter.restore()
        super().drawRow(painter, option, index)


class AgentRow(QFrame):
    """One agent shown inline under its workspace (folder-tree style): a status
    dot, the agent's NAME on the left, its current-task SUMMARY beside the name,
    and a "?" when it's waiting for the user. Clicking it reveals the card."""

    activated = Signal(str, str)   # ws_id, agent_id

    def __init__(self, ws_id: str, agent, parent=None):
        super().__init__(parent)
        self.setObjectName("WsAgentRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ws_id = ws_id
        self.agent_id = agent.id
        self._full = ""     # untruncated summary, re-elided to the live width
        self.setFixedHeight(AGENT_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 10, 3)
        lay.setSpacing(6)
        self.dot = QLabel(self)
        self.dot.setObjectName("WsAgentDot")
        self.name = QLabel(self)
        self.name.setObjectName("WsAgentName")
        self.summary = QLabel(self)
        self.summary.setObjectName("WsAgentTask")
        self.q = QLabel("?", self)
        self.q.setObjectName("WsAgentQ")
        self.q.setToolTip("Waiting for your input")
        self.q.hide()
        lay.addWidget(self.dot)
        lay.addWidget(self.name)
        lay.addWidget(self.summary, 1)
        lay.addWidget(self.q)
        self.refresh(agent)

    def refresh(self, agent) -> None:
        # WORK, not liveness (mirrors the sidebar workspace badge): a RUNNING
        # agent that is actively producing output (is_busy) shows amber; a
        # RUNNING-but-quiet agent stays green. Other statuses map as usual.
        busy = bool(getattr(agent, "is_busy", lambda: False)())
        icon = "🟡" if (busy and agent.status == AgentStatus.RUNNING) \
            else _ICON.get(agent.status, "⚪")
        self.dot.setText(icon)
        self.name.setText(agent.spec.name)
        waiting = bool(getattr(agent, "is_waiting", lambda: False)())
        self.q.setVisible(waiting)
        self.setProperty("waiting", waiting)
        # summary = assigned task, else Claude's live AI conversation title
        get = getattr(agent, "summary", None)
        self._full = (get() if callable(get) else agent.current_task or "").strip()
        self.summary.setToolTip(self._full)
        self._elide()

    def set_search_hit(self, hit: bool) -> None:
        """Tint the agent row when it matches the active sidebar search."""
        if bool(self.property("search_hit")) != bool(hit):
            self.setProperty("search_hit", bool(hit))
            repolish(self)

    def _elide(self) -> None:
        # fit to the summary label's ACTUAL width (it has the layout's stretch,
        # so it fills whatever the row/sidebar width allows — no wasted space);
        # re-runs on resize so it always spans to the true right edge
        w = self.summary.contentsRect().width()
        if not self._full:
            self.summary.clear()
            return
        if w > 8:
            fm = QFontMetrics(self.summary.font())
            self.summary.setText(
                fm.elidedText(self._full, Qt.TextElideMode.ElideRight, w))
        else:   # width not settled yet (pre-layout) — show something meaningful
            self.summary.setText(self._full)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.ws_id, self.agent_id)
            event.accept()
            return
        super().mousePressEvent(event)


class TreeEntryRow(QFrame):
    """One entry in a workspace's inline file explorer (VS Code style): a small
    type icon (folder / file-by-extension) and the name. A directory row shows a
    disclosure caret and toggles its children on click; a file row opens the
    file on click. Modeled on AgentRow — same folder-tree look, same click-to-act
    interaction. Nesting/indentation comes from the QTreeWidget item depth."""

    dirToggled = Signal(str, str)      # ws_id, relpath (expand/collapse)
    fileActivated = Signal(str, str)   # ws_id, abspath (open it)

    def __init__(self, ws_id, rel, name, abspath, is_dir, is_open, parent=None):
        super().__init__(parent)
        self.setObjectName("WsTreeRow")
        self.setProperty("dir", bool(is_dir))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ws_id = ws_id
        self.rel = rel
        self.abspath = abspath
        self.is_dir = is_dir
        self._full = name
        self.setFixedHeight(TREE_ROW_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 8, 1)
        lay.setSpacing(4)
        # caret column: a fixed width kept even for files so names line up
        self.caret = QLabel(self)
        self.caret.setObjectName("WsTreeCaret")
        self.caret.setFixedWidth(10)
        if is_dir:
            self.caret.setText("▾" if is_open else "▸")  # ▾ / ▸
        self.glyph = QLabel(self)
        self.glyph.setObjectName("WsTreeIcon")
        ef = QFont(EMOJI_FONT)
        ef.setPixelSize(11)
        self.glyph.setFont(ef)
        self.glyph.setText((FOLDER_OPEN_ICON if is_open else FOLDER_ICON)
                           if is_dir else file_icon(name))
        self.name = QLabel(self)
        self.name.setObjectName("WsTreeName")
        lay.addWidget(self.caret)
        lay.addWidget(self.glyph)
        lay.addWidget(self.name, 1)
        self._elide()

    def _elide(self) -> None:
        w = self.name.contentsRect().width()
        if w > 8:
            fm = QFontMetrics(self.name.font())
            self.name.setText(
                fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle, w))
        else:
            self.name.setText(self._full)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.is_dir:
                self.dirToggled.emit(self.ws_id, self.rel)
            else:
                self.fileActivated.emit(self.ws_id, self.abspath)
            event.accept()
            return
        super().mousePressEvent(event)


class Sidebar(QFrame):
    addRequested = Signal()
    addCategoryRequested = Signal()          # M2
    workspaceSelected = Signal(str)
    renameRequested = Signal(str, str)
    deleteRequested = Signal(str)
    openFolderRequested = Signal(str)
    agentsRequested = Signal(str)            # ws_id (count badge clicked; M3)
    agentActivated = Signal(str, str)        # ws_id, agent_id (reveal its card)
    reordered = Signal(list)                 # flattened ws-id order (M1)
    layoutChanged = Signal(list)             # full node model: order+categories
    filesRequested = Signal(str)             # ws_id (file-tree toggle clicked)
    filesToggled = Signal()                  # a file tree opened/closed (persist)
    fileActivated = Signal(str, str)         # ws_id, abs path (open the file)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(0)
        self.setMaximumWidth(SIDEBAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._collapsed = False
        # model: ordered top-level nodes. M1 nodes are all workspaces:
        #   {"type": "workspace", "id": ws_id}
        # (M2 adds {"type": "category", "id", "name", "collapsed", "children"})
        self._nodes: list[dict] = []
        self._ws_data: dict[str, dict] = {}   # ws_id -> {name, folder, stats}
        self._active_id = ""
        # rebuilt each rebuild(): ws_id -> row widget / tree item
        self._ws_widgets: dict[str, WorkspaceRow] = {}
        self._ws_items: dict[str, QTreeWidgetItem] = {}
        self._cat_widgets: dict[str, CategoryRow] = {}
        self._cat_items: dict[str, QTreeWidgetItem] = {}
        # inline agent expansion: which workspaces show their agents as child
        # rows, and the live agent-row widgets (rebuilt). agents_provider is set
        # by MainWindow to fetch a workspace's live agents on demand.
        self.agents_provider = None
        self._expanded_ws: set[str] = set()
        self._agent_rows: dict[str, AgentRow] = {}
        self._rendered_agents: dict[str, list] = {}   # ws_id -> agent ids shown
        self._agent_timer = QTimer(self)
        self._agent_timer.setInterval(600)
        self._agent_timer.timeout.connect(self._sync_expanded)
        # inline FILE explorer (VS Code style, transient like agent expansion):
        # which workspaces show their file tree, and which directories within
        # each are expanded (workspace-relative paths). files_root_provider is
        # set by MainWindow to resolve a ws_id -> its project_path root. The
        # per-directory expansion + the reveal highlight are purely in-memory
        # (never persisted, never dirty); only the coarse open/closed set is
        # persisted (via filesToggled -> the "ui" blob).
        self.files_root_provider = None
        self._expanded_files: set[str] = set()
        self._expanded_dirs: dict[str, set] = {}      # ws_id -> {relpath, ...}
        self._file_items: dict[tuple, QTreeWidgetItem] = {}  # (ws_id, abs)->item
        self._file_rows: dict[tuple, TreeEntryRow] = {}
        self._revealed: tuple | None = None           # (ws_id, abs) highlighted
        self._watched_dirs: list[str] = []            # visible dirs, per rebuild
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(lambda _p: self._fs_debounce.start())
        self._fs_debounce = QTimer(self)              # coalesce burst FS events
        self._fs_debounce.setInterval(300)
        self._fs_debounce.setSingleShot(True)
        self._fs_debounce.timeout.connect(self.rebuild)
        # search: transient filter/highlight over workspaces + their agents.
        # While active it may auto-expand workspaces that have agent matches so
        # the hits are visible; the prior expansion is saved and restored on
        # close. None of this persists or marks the session dirty.
        self._search_active = False
        self._search_query = ""
        self._search_saved_expanded: set | None = None
        self._search_ws_hits: set = set()      # ws_ids to highlight
        self._search_agent_hits: set = set()   # agent ids to highlight

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(4)

        header = QWidget(self)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 8, 0)
        self.header = header
        self.title = QLabel("WORKSPACES", header)
        self.title.setObjectName("SidebarTitle")
        self.count_label = QLabel("0", header)
        self.count_label.setObjectName("WsCount")
        # search: a magnifier next to "add category"; clicking it expands an
        # input that OVERLAYS everything to its left (the title + count) and
        # live-highlights matching workspaces, agents, and agent summaries
        self.search_btn = QToolButton(header)
        self.search_btn.setObjectName("SearchBtn")
        self.search_btn.setText("🔍")
        self.search_btn.setToolTip("Search workspaces & agents")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.clicked.connect(lambda: self._toggle_search())
        self.add_cat_btn = QToolButton(header)
        self.add_cat_btn.setObjectName("AddCatBtn")
        self.add_cat_btn.setText("📦")
        self.add_cat_btn.setToolTip("Add category (group related workspaces)")
        self.add_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # lambda: clicked() passes a `checked` bool that must not shadow `name`
        self.add_cat_btn.clicked.connect(lambda: self._add_category())
        self.add_btn = QToolButton(header)
        self.add_btn.setObjectName("AddWsBtn")
        self.add_btn.setText("+")
        self.add_btn.setToolTip("Add workspace (choose a project folder)")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(self.addRequested)
        hl.addWidget(self.title)
        hl.addWidget(self.count_label)
        hl.addStretch(1)
        hl.addWidget(self.search_btn)
        hl.addWidget(self.add_cat_btn)
        hl.addWidget(self.add_btn)
        # overlay search field: a child of the header positioned by hand (NOT in
        # the layout) so it can cover the title/count region exactly when shown
        self.search_edit = QLineEdit(header)
        self.search_edit.setObjectName("SidebarSearch")
        self.search_edit.setPlaceholderText("Search workspaces & agents…")
        self.search_edit.hide()
        self.search_edit.textChanged.connect(self._on_search_text)
        self.search_edit.installEventFilter(self)

        self.tree = _SidebarTree(self)
        self.tree.setObjectName("WsList")
        self.tree.nodeDropped.connect(self._on_node_dropped)

        lay.addWidget(header)
        lay.addWidget(OrnamentDivider(parent=self))  # gilt section rule
        lay.addWidget(self.tree, 1)

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -------------------------------------------------------------- search ---

    def _toggle_search(self) -> None:
        self._close_search() if self._search_active else self._open_search()

    def _open_search(self) -> None:
        """Reveal the overlay search field over the title/count region."""
        self._search_active = True
        self._search_saved_expanded = set(self._expanded_ws)
        self.title.hide()
        self.count_label.hide()
        self.search_edit.show()
        self._position_search()
        self.search_edit.raise_()
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _close_search(self) -> None:
        """Hide the search field, clear the filter, and restore the header +
        the workspace expansion that was in effect before searching."""
        if not self._search_active:
            return
        self._search_active = False        # so the clear() below no-ops
        self.search_edit.hide()
        self.search_edit.clear()
        self.title.show()
        self.count_label.show()
        self._search_query = ""
        self._search_ws_hits = set()
        self._search_agent_hits = set()
        self._expanded_ws = self._search_saved_expanded or set()
        self._search_saved_expanded = None
        self.rebuild()
        self._agent_timer.start() if self._expanded_ws else self._agent_timer.stop()

    def _position_search(self) -> None:
        """Size the overlay to span from the left edge to the search button —
        covering the WORKSPACES title and count exactly."""
        if not self._search_active:
            return
        left = 10
        right = self.search_btn.x() - 4
        h = self.header.height()
        self.search_edit.setGeometry(left, 3, max(40, right - left), max(18, h - 6))

    def _on_search_text(self, text: str) -> None:
        if not self._search_active:
            return
        self._search_query = text.strip().lower()
        self._apply_search()

    def _apply_search(self) -> None:
        """Recompute the highlight sets from the query and rebuild. A workspace
        matches on its name OR on any of its agents (name/summary); a matching
        agent's workspace is auto-expanded so the hit is visible. Empty query
        clears highlights but keeps the field open."""
        q = self._search_query
        ws_hits, agent_hits = set(), set()
        expand = set(self._search_saved_expanded or set())
        if q:
            for ws_id in self._ws_data:
                name = (self._ws_data[ws_id].get("name") or "").lower()
                hit_here = q in name
                agents = self.agents_provider(ws_id) if self.agents_provider else []
                for a in (agents or []):
                    if q in _agent_haystack(a):
                        agent_hits.add(a.id)
                        expand.add(ws_id)     # reveal the matching agent
                        hit_here = True
                if hit_here:
                    ws_hits.add(ws_id)
        self._search_ws_hits = ws_hits
        self._search_agent_hits = agent_hits
        self._expanded_ws = expand
        self.rebuild()
        self._agent_timer.start() if self._expanded_ws else self._agent_timer.stop()

    def eventFilter(self, obj, event):
        # Esc in the search box closes it and clears the filter
        if obj is self.search_edit and event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Escape:
            self._close_search()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_search()   # keep the overlay aligned as the rail resizes

    # ------------------------------------------------------------- model ---

    @property
    def _rows(self) -> dict:
        """Back-compat view: ws_id -> (tree item, row widget). The model moved
        to `_nodes`/`_ws_widgets`; this keeps the historic accessor working."""
        return {wid: (self._ws_items.get(wid), w)
                for wid, w in self._ws_widgets.items()}

    def _ws_node_ids(self) -> list[str]:
        """All workspace ids in display order (top-level + category children)."""
        ids: list[str] = []
        for n in self._nodes:
            if n["type"] == "workspace":
                ids.append(n["id"])
            elif n["type"] == "category":
                ids.extend(n.get("children", []))
        return ids

    def add_row(self, ws_id: str, name: str, folder: str) -> None:
        if ws_id in self._ws_data:
            return
        self._ws_data[ws_id] = {"name": name, "folder": folder, "stats": {}}
        self._nodes.append({"type": "workspace", "id": ws_id})
        self.rebuild()

    def remove_row(self, ws_id: str) -> None:
        if ws_id not in self._ws_data:
            return
        self._ws_data.pop(ws_id, None)
        self._expanded_ws.discard(ws_id)
        # drop from top level and from any category's children
        self._nodes = [n for n in self._nodes
                       if not (n["type"] == "workspace" and n["id"] == ws_id)]
        for n in self._nodes:
            if n["type"] == "category":
                n["children"] = [c for c in n.get("children", [])
                                 if c != ws_id]
        self.rebuild()

    def set_row_name(self, ws_id: str, name: str) -> None:
        if ws_id in self._ws_data:
            self._ws_data[ws_id]["name"] = name
        w = self._ws_widgets.get(ws_id)
        if w is not None:
            w.set_name(name)

    def set_row_folder(self, ws_id: str, folder: str) -> None:
        if ws_id in self._ws_data:
            self._ws_data[ws_id]["folder"] = folder
        w = self._ws_widgets.get(ws_id)
        if w is not None:
            w.set_folder(folder)

    def set_stats(self, ws_id: str, stats: dict) -> None:
        if ws_id in self._ws_data:
            self._ws_data[ws_id]["stats"] = dict(stats)
        w = self._ws_widgets.get(ws_id)
        if w is not None:
            w.set_stats(stats)
        # if this workspace is expanded, keep its inline agent list in sync the
        # instant an agent is added/removed (stats fire on terminal add/remove)
        if (ws_id in self._expanded_ws
                and self._agent_ids(ws_id) != self._rendered_agents.get(ws_id, [])):
            self.rebuild()

    def set_active_row(self, ws_id: str) -> None:
        self._active_id = ws_id
        for wid, w in self._ws_widgets.items():
            w.set_active(wid == ws_id)

    def begin_rename(self, ws_id: str) -> None:
        w = self._ws_widgets.get(ws_id)
        if w is not None:
            w.start_rename()

    def badge_for(self, ws_id: str):
        """The workspace row's count badge widget (dropdown anchor), or None."""
        w = self._ws_widgets.get(ws_id)
        return w.count_badge if w is not None else None

    # -------------------------------------------------------------- build ---

    def rebuild(self) -> None:
        """Recreate every tree item + row widget from `self._nodes`. Cheap (a
        handful of rows) and the single path that reflects model changes."""
        self.tree.clear()
        self._ws_widgets = {}
        self._ws_items = {}
        self._cat_widgets = {}
        self._cat_items = {}
        self._agent_rows = {}
        self._rendered_agents = {}
        self._file_items = {}
        self._file_rows = {}
        self._watched_dirs = []
        root = self.tree.invisibleRootItem()
        for node in self._nodes:
            if node["type"] == "workspace":
                self._add_ws_item(node["id"], root)
            elif node["type"] == "category":
                self._add_cat_item(node)
        self._update_count()
        self._rewatch()
        self._reapply_reveal()

    def _add_cat_item(self, node: dict) -> None:
        cid = node["id"]
        children = [c for c in node.get("children", []) if c in self._ws_data]
        item = QTreeWidgetItem(self.tree.invisibleRootItem())
        item.setData(0, Qt.ItemDataRole.UserRole,
                     {"type": "category", "id": cid})
        item.setSizeHint(0, QSize(SIDEBAR_WIDTH, CAT_HEIGHT))
        row = CategoryRow(cid, node.get("name", "Category"),
                          bool(node.get("collapsed", False)), len(children))
        row.toggled.connect(self._on_cat_toggled)
        row.renameCommitted.connect(self._on_cat_renamed)
        row.deleteRequested.connect(self._on_cat_deleted)
        self.tree.setItemWidget(item, 0, row)
        self._cat_widgets[cid] = row
        self._cat_items[cid] = item
        for ws_id in children:
            self._add_ws_item(ws_id, item)
        item.setExpanded(not node.get("collapsed", False))

    def _add_ws_item(self, ws_id: str, parent) -> None:
        data = self._ws_data.get(ws_id)
        if data is None:
            return
        item = QTreeWidgetItem(parent)
        item.setData(0, Qt.ItemDataRole.UserRole,
                     {"type": "workspace", "id": ws_id})
        item.setSizeHint(0, QSize(SIDEBAR_WIDTH, ROW_HEIGHT))
        row = WorkspaceRow(ws_id, data["name"], data["folder"])
        row.selected.connect(self.workspaceSelected)
        row.renameCommitted.connect(self.renameRequested)
        row.deleteRequested.connect(self.deleteRequested)
        row.openFolderRequested.connect(self.openFolderRequested)
        row.agentsRequested.connect(self._toggle_agents)  # inline expand/close
        row.filesRequested.connect(self._toggle_files)    # inline file tree
        row.set_files_open(ws_id in self._expanded_files)
        self.tree.setItemWidget(item, 0, row)
        self._ws_widgets[ws_id] = row
        self._ws_items[ws_id] = item
        if data.get("stats"):
            row.set_stats(data["stats"])
        row.set_active(ws_id == self._active_id)
        row.set_search_hit(ws_id in self._search_ws_hits)
        if ws_id in self._expanded_ws:      # show its agents as child rows
            self._add_agent_rows(ws_id, item)
            item.setExpanded(True)
        if ws_id in self._expanded_files:   # show its file tree as child rows
            self._add_file_rows(ws_id, item)
            item.setExpanded(True)

    def _add_agent_rows(self, ws_id: str, ws_item: QTreeWidgetItem) -> None:
        agents = self.agents_provider(ws_id) if self.agents_provider else []
        for agent in agents:
            child = QTreeWidgetItem(ws_item)
            child.setData(0, Qt.ItemDataRole.UserRole,
                          {"type": "agent", "id": agent.id})
            child.setSizeHint(0, QSize(SIDEBAR_WIDTH, AGENT_HEIGHT))
            arow = AgentRow(ws_id, agent)
            arow.activated.connect(self.agentActivated)
            arow.set_search_hit(agent.id in self._search_agent_hits)
            self.tree.setItemWidget(child, 0, arow)
            self._agent_rows[agent.id] = arow
        self._rendered_agents[ws_id] = [a.id for a in agents]

    def _agent_ids(self, ws_id: str) -> list:
        agents = self.agents_provider(ws_id) if self.agents_provider else []
        return [a.id for a in agents]

    def _toggle_agents(self, ws_id: str) -> None:
        """Expand/collapse a workspace's agent list inline (folder-tree style).
        Also emitted for external listeners via agentsRequested."""
        self.agentsRequested.emit(ws_id)
        if ws_id in self._expanded_ws:
            self._expanded_ws.discard(ws_id)
        else:
            self._expanded_ws.add(ws_id)
        self.rebuild()
        if self._expanded_ws:
            self._agent_timer.start()
        else:
            self._agent_timer.stop()

    # -------------------------------------------------- inline file explorer ---

    def _toggle_files(self, ws_id: str) -> None:
        """Expand/collapse a workspace's inline file tree. This is a deliberate,
        low-frequency user action, so it persists (via filesToggled). The
        per-directory expansion below it is transient and never persisted."""
        self.filesRequested.emit(ws_id)
        if ws_id in self._expanded_files:
            self._expanded_files.discard(ws_id)
            self._expanded_dirs.pop(ws_id, None)
        else:
            self._expanded_files.add(ws_id)
        self.rebuild()
        self.filesToggled.emit()

    def _on_dir_toggled(self, ws_id: str, rel: str) -> None:
        """Expand/collapse one directory inside a file tree (transient)."""
        dirs = self._expanded_dirs.setdefault(ws_id, set())
        dirs.discard(rel) if rel in dirs else dirs.add(rel)
        self.rebuild()

    def _add_file_rows(self, ws_id: str, ws_item: QTreeWidgetItem) -> None:
        root = (self.files_root_provider(ws_id)
                if self.files_root_provider else None)
        if not root or not os.path.isdir(root):
            return
        self._watched_dirs.append(root)
        self._add_dir_children(ws_id, ws_item, root, "")

    def _add_dir_children(self, ws_id, parent_item, dir_path, rel_prefix) -> None:
        """Populate one directory level; recurse only into expanded subdirs
        (lazy, VS Code style). Guarded so a permission error never breaks the
        rebuild path. Directories sort before files, case-insensitive."""
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except OSError:
            return

        def is_dir(e):
            try:
                return e.is_dir()
            except OSError:
                return False

        dirs = sorted((e for e in entries if is_dir(e)),
                      key=lambda e: e.name.lower())
        files = sorted((e for e in entries if not is_dir(e)),
                       key=lambda e: e.name.lower())
        ordered = dirs + files
        expanded = self._expanded_dirs.get(ws_id, set())
        for e in ordered[:FILE_TREE_CAP]:
            entry_is_dir = is_dir(e)
            rel = f"{rel_prefix}/{e.name}" if rel_prefix else e.name
            abspath = os.path.abspath(e.path)
            is_open = entry_is_dir and rel in expanded
            child = QTreeWidgetItem(parent_item)
            child.setData(0, Qt.ItemDataRole.UserRole,
                          {"type": "dir" if entry_is_dir else "file",
                           "path": abspath})
            child.setSizeHint(0, QSize(SIDEBAR_WIDTH, TREE_ROW_HEIGHT))
            trow = TreeEntryRow(ws_id, rel, e.name, abspath, entry_is_dir, is_open)
            if entry_is_dir:
                trow.dirToggled.connect(self._on_dir_toggled)
            else:
                trow.fileActivated.connect(self.fileActivated)
            self.tree.setItemWidget(child, 0, trow)
            self._file_items[(ws_id, abspath)] = child
            self._file_rows[(ws_id, abspath)] = trow
            if is_open:
                self._watched_dirs.append(abspath)
                self._add_dir_children(ws_id, child, abspath, rel)
                child.setExpanded(True)
        if len(ordered) > FILE_TREE_CAP:
            more = QTreeWidgetItem(parent_item)
            more.setSizeHint(0, QSize(SIDEBAR_WIDTH, TREE_ROW_HEIGHT))
            lbl = QLabel(f"… {len(ordered) - FILE_TREE_CAP} more")
            lbl.setObjectName("WsTreeMore")
            self.tree.setItemWidget(more, 0, lbl)

    def _rewatch(self) -> None:
        """Point the filesystem watcher at exactly the directories currently
        visible in open file trees, so external add/delete refreshes the view."""
        existing = self._fs_watcher.directories()
        if existing:
            self._fs_watcher.removePaths(existing)
        if self._watched_dirs:
            self._fs_watcher.addPaths(self._watched_dirs)

    def reveal_file(self, ws_id: str, abspath: str) -> None:
        """Scroll to + highlight a file in its workspace's tree. No-ops unless
        that tree is already open (respecting 'reveal only when the explorer is
        showing'); expands every ancestor directory to bring it into view. All
        transient — never marks the session dirty."""
        if ws_id not in self._expanded_files:
            return
        root = (self.files_root_provider(ws_id)
                if self.files_root_provider else None)
        if not root:
            return
        try:
            rel = os.path.relpath(abspath, root)
        except ValueError:            # different drive — not under this root
            return
        rel = rel.replace("\\", "/")
        if rel.startswith("../") or rel == "..":
            return                    # outside the workspace root
        parts = rel.split("/")
        dirs = self._expanded_dirs.setdefault(ws_id, set())
        acc = ""
        for p in parts[:-1]:
            acc = f"{acc}/{p}" if acc else p
            dirs.add(acc)
        self._revealed = (ws_id, os.path.abspath(abspath))
        self.rebuild()               # reapplies the highlight for _revealed
        item = self._file_items.get(self._revealed)
        if item is not None:
            self.tree.scrollToItem(item)

    def _reapply_reveal(self) -> None:
        """Re-mark the revealed row after a rebuild (the widgets are recreated).
        A revealed file that is no longer visible simply isn't highlighted."""
        if not self._revealed:
            return
        row = self._file_rows.get(self._revealed)
        if row is not None:
            row.setProperty("revealed", True)
            repolish(row)

    def reset_file_tree(self, ws_id: str) -> None:
        """Drop a workspace's per-directory expansion (e.g. its root folder
        changed) and rebuild if its tree is currently open."""
        self._expanded_dirs.pop(ws_id, None)
        if ws_id in self._expanded_files:
            self.rebuild()

    def open_file_trees(self) -> list:
        """The ws-ids whose file tree is open (for persistence)."""
        return sorted(self._expanded_files)

    def set_open_file_trees(self, ids) -> None:
        """Restore which workspaces show their file tree (session restore).
        Intersected with known workspaces so a stale id is dropped."""
        self._expanded_files = {i for i in (ids or []) if i in self._ws_data}
        self.rebuild()

    def _sync_expanded(self) -> None:
        """Keep expanded agent rows live (status / summary / "?"); rebuild only
        if an expanded workspace's agent set changed (add/remove/reorder)."""
        if not self._expanded_ws or self.agents_provider is None:
            self._agent_timer.stop()
            return
        for ws_id in list(self._expanded_ws):
            agents = self.agents_provider(ws_id) or []
            # compare to what is CURRENTLY rendered for this ws (catches both
            # additions and removals — an earlier check missed removals)
            if [a.id for a in agents] != self._rendered_agents.get(ws_id, []):
                self.rebuild()
                return
            for agent in agents:
                row = self._agent_rows.get(agent.id)
                if row is None:
                    continue
                try:
                    row.refresh(agent)
                    row.style().unpolish(row)
                    row.style().polish(row)
                except RuntimeError:
                    self.rebuild()
                    return

    def _update_count(self) -> None:
        self.count_label.setText(str(len(self._ws_data)))

    # --------------------------------------------------------- reordering ---

    def _snapshot(self) -> list:
        return [{"type": "workspace", "id": n["id"]} if n["type"] == "workspace"
                else {"type": "category", "id": n["id"],
                      "name": n.get("name", ""),
                      "collapsed": n.get("collapsed", False),
                      "children": list(n.get("children", []))}
                for n in self._nodes]

    def _emit_layout(self) -> None:
        self.layoutChanged.emit(self._snapshot())
        self.reordered.emit(self._ws_node_ids())

    def apply_layout(self, nodes: list) -> None:
        """Install a full layout (order + categories), e.g. on session restore.
        Normalized against the workspaces currently known to the sidebar."""
        self._nodes = self._normalize(nodes)
        self.rebuild()

    def _normalize(self, nodes: list) -> list:
        seen: set[str] = set()
        out: list[dict] = []
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            if n.get("type") == "workspace":
                wid = n.get("id")
                if wid in self._ws_data and wid not in seen:
                    seen.add(wid)
                    out.append({"type": "workspace", "id": wid})
            elif n.get("type") == "category":
                kids = [c for c in n.get("children", [])
                        if c in self._ws_data and c not in seen]
                seen.update(kids)
                out.append({"type": "category",
                            "id": n.get("id") or uuid.uuid4().hex,
                            "name": str(n.get("name", "Category")),
                            "collapsed": bool(n.get("collapsed", False)),
                            "children": kids})
        for wid in self._ws_data:        # any unplaced workspace -> top level
            if wid not in seen:
                out.append({"type": "workspace", "id": wid})
        return out

    # ---- drag/drop node placement (single-level categories) --------------

    def _top_index(self, kind: str, node_id: str):
        for i, n in enumerate(self._nodes):
            if n["type"] == kind and n["id"] == node_id:
                return i
        return None

    def _locate_ws(self, ws_id: str):
        """('top', i) or ('cat', cat_index, child_index) or None."""
        for i, n in enumerate(self._nodes):
            if n["type"] == "workspace" and n["id"] == ws_id:
                return ("top", i)
            if n["type"] == "category" and ws_id in n.get("children", []):
                return ("cat", i, n["children"].index(ws_id))
        return None

    def _detach_node(self, kind: str, node_id: str):
        for i, n in enumerate(self._nodes):
            if n["type"] == kind and n["id"] == node_id:
                return self._nodes.pop(i)
            if n["type"] == "category" and kind == "workspace":
                if node_id in n.get("children", []):
                    n["children"].remove(node_id)
                    return {"type": "workspace", "id": node_id}
        return None

    def _on_node_dropped(self, kind: str, node_id: str,
                         target_id: str, position: str) -> None:
        if node_id == target_id:
            return                            # dropped on itself -> no-op
        before = self._snapshot()
        node = self._detach_node(kind, node_id)
        if node is None:
            return
        if kind == "workspace":
            self._place_workspace(node, target_id, position)
        else:
            self._place_category(node, target_id, position)
        if self._snapshot() == before:
            return                            # no-op drop
        self.rebuild()
        self._emit_layout()

    def _place_workspace(self, node, target_id, position) -> None:
        if not target_id:
            self._nodes.append(node)
            return
        cat_idx = self._top_index("category", target_id)
        if cat_idx is not None:
            if position == "above":
                self._nodes.insert(cat_idx, node)          # out, before cat
            else:                                          # on/below -> into
                self._nodes[cat_idx].setdefault(
                    "children", []).append(node["id"])
            return
        loc = self._locate_ws(target_id)
        if loc is None:
            self._nodes.append(node)
        elif loc[0] == "top":
            self._nodes.insert(loc[1] + (0 if position == "above" else 1), node)
        else:                                              # into that category
            _, cat_i, child_i = loc
            ins = child_i + (0 if position == "above" else 1)
            self._nodes[cat_i]["children"].insert(ins, node["id"])

    def _place_category(self, node, target_id, position) -> None:
        # categories live only at the top level (never nest)
        if not target_id:
            self._nodes.append(node)
            return
        cat_idx = self._top_index("category", target_id)
        if cat_idx is not None:
            self._nodes.insert(cat_idx + (0 if position == "above" else 1), node)
            return
        loc = self._locate_ws(target_id)
        if loc is None:
            self._nodes.append(node)
        else:                              # adjacent to the ws's top-level slot
            top_i = loc[1]
            self._nodes.insert(top_i + (0 if position == "above" else 1), node)

    # ---- category CRUD ---------------------------------------------------

    def _add_category(self, name: str = "New category") -> str:
        cid = uuid.uuid4().hex
        self._nodes.append({"type": "category", "id": cid, "name": name,
                            "collapsed": False, "children": []})
        self.rebuild()
        self._emit_layout()
        row = self._cat_widgets.get(cid)   # let the user name it right away
        if row is not None:
            row.start_rename()
        return cid

    def _cat_node(self, cid: str):
        return next((n for n in self._nodes
                     if n["type"] == "category" and n["id"] == cid), None)

    def _on_cat_toggled(self, cid: str) -> None:
        node = self._cat_node(cid)
        if node is None:
            return
        node["collapsed"] = not node.get("collapsed", False)
        item = self._cat_items.get(cid)
        row = self._cat_widgets.get(cid)
        if item is not None:
            item.setExpanded(not node["collapsed"])
        if row is not None:
            row.set_collapsed(node["collapsed"])
        self._emit_layout()

    def _on_cat_renamed(self, cid: str, name: str) -> None:
        node = self._cat_node(cid)
        if node is None:
            return
        node["name"] = name
        row = self._cat_widgets.get(cid)
        if row is not None:
            row.name_label.setText(name)
        self._emit_layout()

    def _on_cat_deleted(self, cid: str) -> None:
        idx = self._top_index("category", cid)
        if idx is None:
            return
        node = self._nodes.pop(idx)
        kids = [{"type": "workspace", "id": c}
                for c in node.get("children", []) if c in self._ws_data]
        for j, k in enumerate(kids):        # children spill out in place
            self._nodes.insert(idx + j, k)
        self.rebuild()
        self._emit_layout()

    # ---------------------------------------------------------- collapse ---

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool, animate: bool = True) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            # the panel shrinks to width 0 but its children stay alive:
            # pull focus out (an invisible rename editor must not keep
            # eating keystrokes) and drop the panel from the tab order
            focus = QApplication.focusWidget()
            if focus is not None and self.isAncestorOf(focus):
                focus.clearFocus()
            for w in self._ws_widgets.values():
                w._end_rename()
            for c in self._cat_widgets.values():
                c._end_rename()
        self.setEnabled(not collapsed)
        end = 0 if collapsed else SIDEBAR_WIDTH
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self.maximumWidth())
            self._anim.setEndValue(end)
            self._anim.start()
        else:
            self.setMaximumWidth(end)
