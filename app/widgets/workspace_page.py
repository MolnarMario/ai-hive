"""WorkspacePage: the agent grid for one workspace + its header bar.

Pages live forever inside the MainWindow's QStackedWidget until their
workspace is deleted — switching workspaces only changes which page is
visible, so hidden cards keep receiving agent output.

Two layout modes:
  - "auto": compute_grid(n) tiles exactly the cards (LCM straggler stretch).
  - "RxC": a fixed grid; cards fill left→right/top→bottom and unused slots
    show a clickable "＋ New agent" placeholder.

Retiling never destroys agent cards: the persistent QGridLayout is drained
with takeAt() and cards are re-seated, preserving process/scroll/console
state (no restart). Empty-slot placeholders are stateless and recreated.
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from ..terminal_agent import TerminalAgent
from ..tiling import compute_grid, explicit_grid, parse_layout
from ..workspace_manager import Workspace
from .grid_selector import GridButton
from .terminal_card import TerminalCard


class EmptySlot(QFrame):
    """A clickable placeholder occupying an unused grid cell."""

    addRequested = Signal(str)  # ws_id

    def __init__(self, ws_id: str, parent=None):
        super().__init__(parent)
        self.ws_id = ws_id
        self.setObjectName("EmptySlot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(220, 140)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus = QLabel("＋", self)
        plus.setObjectName("EmptySlotPlus")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("New agent", self)
        label.setObjectName("EmptySlotLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(plus)
        lay.addWidget(label)

    def mousePressEvent(self, event):
        self.addRequested.emit(self.ws_id)
        super().mousePressEvent(event)


class WorkspacePage(QWidget):
    closeRequested = Signal(str)        # agent id
    focusGained = Signal(object)        # TerminalCard
    reassignRequested = Signal(str)     # agent id
    addRequested = Signal(str)          # ws_id (empty slot clicked)
    layoutChosen = Signal(str, str)     # ws_id, layout
    openFolderRequested = Signal(str)   # ws_id
    changePathRequested = Signal(str)   # ws_id
    activityToggled = Signal(str)       # ws_id (wired in Phase 6)
    mapRequested = Signal(str)          # ws_id (open the Agent/File Map window)

    def __init__(self, workspace: Workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.cards: list[TerminalCard] = []
        self._empty_slots: list[EmptySlot] = []
        self._layout = workspace.layout or "auto"
        self._hist_rows = 0
        self._hist_vcols = 0
        # transient solo view: when set, _retile shows ONLY this card full-area
        # and hides its siblings (view only — their processes keep running).
        # Never persisted; restore is free (layout is derived from cards+layout).
        self._solo_card: "TerminalCard | None" = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self.body = QWidget(self)
        body_lay = QVBoxLayout(self.body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        # an empty workspace must never read as data loss: say what's true —
        # THIS workspace has no agents; everything elsewhere is untouched
        self.empty = QLabel(
            "This workspace has no agents.\n\n"
            "＋ Terminal (Ctrl+Shift+T) adds one, or pick a grid layout.\n"
            "Agents in your other workspaces are untouched and still\n"
            "running — see the workspace list in the sidebar.",
            self.body)
        self.empty.setObjectName("EmptyState")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea(self.body)
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget(self.scroll)
        self.grid_host.setObjectName("GridHost")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setContentsMargins(10, 10, 10, 10)
        self.scroll.setWidget(self.grid_host)
        body_lay.addWidget(self.empty, 1)
        body_lay.addWidget(self.scroll, 1)
        root.addWidget(self.body, 1)

        self.grid_button.set_current(self._layout)
        self._retile()
        self._update_empty_state()

    # ------------------------------------------------------------- header ---

    def _build_header(self) -> QFrame:
        header = QFrame(self)
        header.setObjectName("WorkspaceHeader")
        header.setFixedHeight(38)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 0, 8, 0)
        hl.setSpacing(6)

        folder_icon = QLabel("🗀", header)
        folder_icon.setObjectName("HeaderFolderIcon")
        self.path_label = QLabel(self.workspace.project_path, header)
        self.path_label.setObjectName("HeaderPath")
        self.path_label.setToolTip(self.workspace.project_path)

        def tool(text, tip, obj=""):
            b = QToolButton(header)
            b.setText(text)
            b.setToolTip(tip)
            if obj:
                b.setObjectName(obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        self.open_btn = tool("Open folder", "Open this workspace's folder")
        self.change_btn = tool("Change…", "Change the workspace folder")
        self.grid_button = GridButton(header)
        self.map_btn = tool("◆ Map", "Show the agent / file map (who is "
                                     "working on which files)")
        self.map_btn.setObjectName("MapToggle")
        self.activity_btn = tool("❦ Activity", "Show the workspace activity board")
        self.activity_btn.setObjectName("ActivityToggle")
        self.activity_btn.setCheckable(True)

        hl.addWidget(folder_icon)
        hl.addWidget(self.path_label, 1)
        hl.addWidget(self.open_btn)
        hl.addWidget(self.change_btn)
        hl.addSpacing(8)
        hl.addWidget(self.grid_button)
        hl.addWidget(self.map_btn)
        hl.addWidget(self.activity_btn)

        self.open_btn.clicked.connect(
            lambda: self.openFolderRequested.emit(self.workspace.id))
        self.change_btn.clicked.connect(
            lambda: self.changePathRequested.emit(self.workspace.id))
        self.grid_button.layoutChosen.connect(self._on_layout_chosen)
        self.map_btn.clicked.connect(
            lambda: self.mapRequested.emit(self.workspace.id))
        # clicked (not toggled) so programmatic setChecked never re-fires
        self.activity_btn.clicked.connect(
            lambda: self.activityToggled.emit(self.workspace.id))
        return header

    def set_path_text(self, path: str) -> None:
        self.path_label.setToolTip(path)
        self._full_path = path
        self._elide_path()

    def _elide_path(self) -> None:
        path = getattr(self, "_full_path", self.workspace.project_path)
        fm = QFontMetrics(self.path_label.font())
        self.path_label.setText(fm.elidedText(
            path, Qt.TextElideMode.ElideMiddle,
            max(60, self.path_label.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_path()

    # ------------------------------------------------------------- layout ---

    def set_layout(self, layout: str) -> None:
        self._layout = layout or "auto"
        self.grid_button.set_current(self._layout)
        self._retile()
        self._update_empty_state()

    def _on_layout_chosen(self, layout: str) -> None:
        self.set_layout(layout)                      # apply instantly
        self.layoutChosen.emit(self.workspace.id, layout)  # persist

    # ------------------------------------------------------------- cards ---

    def add_agent(self, agent: TerminalAgent) -> TerminalCard:
        card = TerminalCard(agent, parent=self.grid_host)
        card.closeRequested.connect(self.closeRequested)
        card.focusGained.connect(self.focusGained)
        card.reassignRequested.connect(self.reassignRequested)
        card.maximizeRequested.connect(self.toggle_solo)
        self.cards.append(card)
        # a freshly added agent must never be born invisible behind a maximized
        # sibling — adding one exits solo so the new card is seen
        self._exit_solo()
        self._retile()
        self._update_empty_state()
        return card

    def remove_agent(self, agent_id: str) -> None:
        card = next((c for c in self.cards if c.agent.id == agent_id), None)
        if card is None:
            return
        card.detach()
        self.cards.remove(card)
        # closing the maximized card auto-restores the tiling (never strand a
        # blank workspace pinned to a card that no longer exists)
        if card is self._solo_card:
            self._exit_solo()
        card.setParent(None)
        card.deleteLater()
        self._retile()
        self._update_empty_state()

    def card_for(self, agent_id: str) -> TerminalCard | None:
        return next((c for c in self.cards if c.agent.id == agent_id), None)

    def running_count(self) -> int:
        return sum(1 for c in self.cards if c.agent.is_running())

    def toggle_solo(self, card: "TerminalCard") -> None:
        # maximize this card (or restore if it's already the soloed one). Pure
        # view change: siblings are only hidden, their agents keep running.
        self._solo_card = None if self._solo_card is card else card
        for c in self.cards:
            c.set_maximized(c is self._solo_card)
        self._retile()

    def _exit_solo(self) -> None:
        if self._solo_card is None:
            return
        self._solo_card = None
        for c in self.cards:
            c.set_maximized(False)

    # ------------------------------------------------------------ tiling ---

    def _retile(self) -> None:
        for slot in self._empty_slots:  # stateless placeholders: recreate
            slot.setParent(None)
            slot.deleteLater()
        self._empty_slots = []

        n = len(self.cards)
        self.grid_host.setUpdatesEnabled(False)
        try:
            while self.grid.count():
                self.grid.takeAt(0)  # detaches items; agent cards survive
            parsed = parse_layout(self._layout)
            if self._solo_card is not None and self._solo_card in self.cards:
                # SOLO: one card fills the whole area, siblings hidden (their
                # processes are untouched). Wins over both AUTO and FIXED.
                for card in self.cards:
                    card.setVisible(card is self._solo_card)
                self.grid.addWidget(self._solo_card, 0, 0, 1, 1)
                rows, vcols = 1, 1
            elif parsed is None:  # AUTO
                plan = compute_grid(n)
                for card, cell in zip(self.cards, plan.cells):
                    self.grid.addWidget(card, cell.row, cell.col,
                                        cell.row_span, cell.col_span)
                    card.setVisible(True)
                rows, vcols = plan.rows, plan.vcols
            else:  # FIXED R×C with empty slots
                ep = explicit_grid(parsed[0], parsed[1], n)
                for card, cell in zip(self.cards, ep.agent_cells):
                    self.grid.addWidget(card, cell.row, cell.col, 1, 1)
                    card.setVisible(True)
                for cell in ep.empty_cells:
                    slot = EmptySlot(self.workspace.id, self.grid_host)
                    slot.addRequested.connect(self.addRequested)
                    self.grid.addWidget(slot, cell.row, cell.col, 1, 1)
                    self._empty_slots.append(slot)
                rows, vcols = ep.rows, ep.cols

            # reset stretch up to the historical max (QGridLayout never shrinks
            # its row/col count — leftover stretch is the phantom-gap bug)
            self._hist_rows = max(self._hist_rows, rows, self.grid.rowCount())
            self._hist_vcols = max(self._hist_vcols, vcols, self.grid.columnCount())
            for c in range(self._hist_vcols):
                self.grid.setColumnStretch(c, 1 if c < vcols else 0)
            for r in range(self._hist_rows):
                self.grid.setRowStretch(r, 1 if r < rows else 0)
        finally:
            self.grid_host.setUpdatesEnabled(True)
        self.grid.invalidate()

    def _update_empty_state(self) -> None:
        # a fixed layout always shows its grid (empty slots invite adding);
        # auto with zero cards shows the hint label
        has_content = bool(self.cards) or parse_layout(self._layout) is not None
        self.empty.setVisible(not has_content)
        self.scroll.setVisible(has_content)
