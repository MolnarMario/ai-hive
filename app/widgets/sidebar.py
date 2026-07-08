"""Sidebar: the WORKSPACES panel — rows, badges, inline rename, collapse,
drag-reorder, and (M2) collapsible categories.

The panel is a model-driven `QTreeWidget`: `Sidebar._nodes` is the ordered list
of top-level nodes (workspaces and, in M2, categories with workspace children),
and `rebuild()` recreates the tree's items + custom row widgets from that model.
Drag-and-drop never moves Qt items directly (that would strand the rich
`setItemWidget` row widgets); instead a drop mutates `_nodes` and rebuilds, which
keeps ordering, category membership, and persistence driven by a single source
of truth. Rows themselves initiate the `QDrag` (they cover the viewport, so the
view can't), and the tree resolves the drop target + position.
"""

import uuid

from PySide6.QtCore import (QEasingCurve, QEvent, QMimeData, QPoint,
                            QPropertyAnimation, QSize, Qt, Signal)
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..ui_theme import repolish
from .ornaments import AgentCountBadge, OrnamentDivider, WorkspaceSpinner

SIDEBAR_WIDTH = 230
ROW_HEIGHT = 44
CAT_HEIGHT = 32

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

        self.folder_btn = QToolButton(self)
        self.folder_btn.setObjectName("WsFolderBtn")
        self.folder_btn.setText("🗀")
        self.folder_btn.setToolTip("Open workspace folder")
        self.folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_btn.hide()
        self.folder_btn.clicked.connect(
            lambda: self.openFolderRequested.emit(self.ws_id))

        self.delete_btn = QToolButton(self)
        self.delete_btn.setObjectName("WsDelete")
        self.delete_btn.setText("✕")
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

    def _update_hover_buttons(self, hovered: bool) -> None:
        # folder/delete appear ONLY while hovering the row (not on the active
        # row) so the workspace name keeps the full width the rest of the time;
        # the count badge carries the workspace's status at all times
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
        self.delete_btn.setText("✕")
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


class Sidebar(QFrame):
    addRequested = Signal()
    addCategoryRequested = Signal()          # M2
    workspaceSelected = Signal(str)
    renameRequested = Signal(str, str)
    deleteRequested = Signal(str)
    openFolderRequested = Signal(str)
    agentsRequested = Signal(str)            # ws_id (count badge clicked; M3)
    reordered = Signal(list)                 # flattened ws-id order (M1)
    layoutChanged = Signal(list)             # full node model: order+categories

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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(4)

        header = QWidget(self)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 8, 0)
        title = QLabel("WORKSPACES", header)
        title.setObjectName("SidebarTitle")
        self.count_label = QLabel("0", header)
        self.count_label.setObjectName("WsCount")
        self.add_cat_btn = QToolButton(header)
        self.add_cat_btn.setObjectName("AddCatBtn")
        self.add_cat_btn.setText("🗂")
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
        hl.addWidget(title)
        hl.addWidget(self.count_label)
        hl.addStretch(1)
        hl.addWidget(self.add_cat_btn)
        hl.addWidget(self.add_btn)

        self.tree = _SidebarTree(self)
        self.tree.setObjectName("WsList")
        self.tree.nodeDropped.connect(self._on_node_dropped)

        lay.addWidget(header)
        lay.addWidget(OrnamentDivider(parent=self))  # gilt section rule
        lay.addWidget(self.tree, 1)

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

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
        root = self.tree.invisibleRootItem()
        for node in self._nodes:
            if node["type"] == "workspace":
                self._add_ws_item(node["id"], root)
            elif node["type"] == "category":
                self._add_cat_item(node)
        self._update_count()

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
        row.agentsRequested.connect(self.agentsRequested)
        self.tree.setItemWidget(item, 0, row)
        self._ws_widgets[ws_id] = row
        self._ws_items[ws_id] = item
        if data.get("stats"):
            row.set_stats(data["stats"])
        row.set_active(ws_id == self._active_id)

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
