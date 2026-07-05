"""Sidebar: the WORKSPACES panel — rows, badges, inline rename, collapse."""

from PySide6.QtCore import (QEasingCurve, QEvent, QPropertyAnimation, Qt,
                            Signal)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..ui_theme import repolish
from .ornaments import AgentCountBadge, OrnamentDivider

SIDEBAR_WIDTH = 230


class WorkspaceRow(QFrame):
    """One sidebar row: icon, name (+ inline rename), folder, badge, delete."""

    selected = Signal(str)          # ws_id
    renameCommitted = Signal(str, str)  # ws_id, new name
    deleteRequested = Signal(str)   # ws_id
    openFolderRequested = Signal(str)  # ws_id

    def __init__(self, ws_id: str, name: str, folder: str, parent=None):
        super().__init__(parent)
        self.ws_id = ws_id
        self._renaming = False
        self._folder = folder
        self._last_stats = {"total": 0, "active": 0, "idle": 0, "error": 0}
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(44)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 6, 4)
        lay.setSpacing(8)

        # the agent tally doubles as the workspace's status light
        self.count_badge = AgentCountBadge(self)
        self.count_badge.setObjectName("WsCount")

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

        lay.addWidget(self.count_badge)
        lay.addLayout(text_col, 1)
        lay.addWidget(self.folder_btn)
        lay.addWidget(self.delete_btn)

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
        # green pulses ONLY when an agent is actually working; a running-but-
        # quiet agent (standby) reads amber. Green wins over a stale error.
        state = ("empty" if total == 0
                 else "working" if busy > 0
                 else "error" if e > 0
                 else "idle")
        self.count_badge.set_state(total, state)
        tip = f"{total} agent(s): {busy} working, {running} running, {e} error"
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
        self.selected.emit(self.ws_id)
        super().mousePressEvent(event)

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


class Sidebar(QFrame):
    addRequested = Signal()
    workspaceSelected = Signal(str)
    renameRequested = Signal(str, str)
    deleteRequested = Signal(str)
    openFolderRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(0)
        self.setMaximumWidth(SIDEBAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._collapsed = False
        self._rows: dict[str, tuple[QListWidgetItem, WorkspaceRow]] = {}

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
        self.add_btn = QToolButton(header)
        self.add_btn.setObjectName("AddWsBtn")
        self.add_btn.setText("+")
        self.add_btn.setToolTip("Add workspace (choose a project folder)")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(self.addRequested)
        hl.addWidget(title)
        hl.addWidget(self.count_label)
        hl.addStretch(1)
        hl.addWidget(self.add_btn)

        self.list = QListWidget(self)
        self.list.setObjectName("WsList")
        self.list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection)  # row widgets own the visual
        self.list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        lay.addWidget(header)
        lay.addWidget(OrnamentDivider(parent=self))  # gilt section rule
        lay.addWidget(self.list, 1)

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -------------------------------------------------------------- rows ---

    def add_row(self, ws_id: str, name: str, folder: str) -> None:
        row = WorkspaceRow(ws_id, name, folder)
        row.selected.connect(self.workspaceSelected)
        row.renameCommitted.connect(self.renameRequested)
        row.deleteRequested.connect(self.deleteRequested)
        row.openFolderRequested.connect(self.openFolderRequested)
        item = QListWidgetItem(self.list)
        item.setSizeHint(row.sizeHint())
        self.list.setItemWidget(item, row)
        self._rows[ws_id] = (item, row)
        self._update_count()

    def remove_row(self, ws_id: str) -> None:
        entry = self._rows.pop(ws_id, None)
        if entry is None:
            return
        item, _row = entry
        self.list.takeItem(self.list.row(item))
        self._update_count()

    def set_row_name(self, ws_id: str, name: str) -> None:
        if ws_id in self._rows:
            self._rows[ws_id][1].set_name(name)

    def set_row_folder(self, ws_id: str, folder: str) -> None:
        if ws_id in self._rows:
            self._rows[ws_id][1].set_folder(folder)

    def set_stats(self, ws_id: str, stats: dict) -> None:
        if ws_id in self._rows:
            self._rows[ws_id][1].set_stats(stats)

    def set_active_row(self, ws_id: str) -> None:
        for rid, (_item, row) in self._rows.items():
            row.set_active(rid == ws_id)

    def begin_rename(self, ws_id: str) -> None:
        if ws_id in self._rows:
            self._rows[ws_id][1].start_rename()

    def _update_count(self) -> None:
        self.count_label.setText(str(len(self._rows)))

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
            for _item, row in self._rows.values():
                row._end_rename()
        self.setEnabled(not collapsed)
        end = 0 if collapsed else SIDEBAR_WIDTH
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self.maximumWidth())
            self._anim.setEndValue(end)
            self._anim.start()
        else:
            self.setMaximumWidth(end)
