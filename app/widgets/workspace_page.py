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

Cards are drag-reorderable: dragging from a card's header (see _CardHeader in
terminal_card) starts a QDrag the grid host (_ReorderGrid) accepts. While
dragging, a gold insertion bar (_DropIndicator) shows exactly where the card
will land (_drop_index maps the cursor to an insertion index in reading order,
so it works for any grid shape). On drop the `cards` list is re-sequenced,
_retile() places everything, and _animate_reflow tweens each moved card from
its old cell to its new one for a snap-into-place feel — cosmetic only, the
final positions come from the layout. The new order is emitted via
reorderCommitted → WorkspaceManager.reorder_agents, which re-sequences
ws.agents (persisted by list position) and marks the session dirty.
"""

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve,
                            QParallelAnimationGroup, QPropertyAnimation, QRect,
                            QSize, Qt, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from ..terminal_agent import TerminalAgent
from ..tiling import compute_grid, explicit_grid, parse_layout
from ..ui_theme import Palette
from ..workspace_manager import Workspace
from .grid_selector import GridButton
from .terminal_card import CARD_REORDER_MIME, TerminalCard


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


class _DropIndicator(QWidget):
    """A gold insertion bar painted between cards to show exactly where a
    dragged card will land. Transparent to mouse so it never eats drop events."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(Palette.ACCENT_GOLD))
        p.drawRoundedRect(self.rect(), 2, 2)


class _ReorderGrid(QWidget):
    """The grid host that holds the agent cards; it accepts card-reorder drops
    and forwards the drag events to its WorkspacePage (which owns the card
    order + the drop indicator)."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self._page = page
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        self._page._drag_enter(event)

    def dragMoveEvent(self, event):
        self._page._drag_move(event)

    def dragLeaveEvent(self, event):
        self._page._drag_leave(event)

    def dropEvent(self, event):
        self._page._drop(event)


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
    fileActivated = Signal(str, str)    # ws_id, abs path (Ctrl+clicked in a card)
    reorderCommitted = Signal(str, list)  # ws_id, new ordered agent ids

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
        # drag-to-reorder state (transient): the card being dragged, the point
        # it was dropped at (start of the snap animation), and a handle on the
        # in-flight reflow animation so it isn't garbage-collected mid-flight.
        self._drag_card: "TerminalCard | None" = None
        self._reflow_anim = None

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
            "running; see the workspace list in the sidebar.",
            self.body)
        self.empty.setObjectName("EmptyState")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea(self.body)
        self.scroll.setWidgetResizable(True)
        self.grid_host = _ReorderGrid(self, self.scroll)
        self.grid_host.setObjectName("GridHost")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setContentsMargins(10, 10, 10, 10)
        self.scroll.setWidget(self.grid_host)
        # gold insertion bar shown over the grid while a card is being dragged
        self._drop_indicator = _DropIndicator(self.grid_host)
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
        card.fileActivated.connect(
            lambda p: self.fileActivated.emit(self.workspace.id, p))
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

    # --------------------------------------------------- drag-to-reorder ---

    def _card_by_id(self, agent_id: str) -> "TerminalCard | None":
        return next((c for c in self.cards if c.agent.id == agent_id), None)

    def _reorderable(self, event) -> bool:
        # only our own card-reorder drags, and never while soloed (one visible
        # card) or with fewer than two cards to shuffle
        return (event.mimeData().hasFormat(CARD_REORDER_MIME)
                and self._solo_card is None and len(self.cards) > 1)

    def _drag_enter(self, event) -> None:
        if not self._reorderable(event):
            return
        aid = bytes(event.mimeData().data(CARD_REORDER_MIME)).decode("utf-8")
        self._drag_card = self._card_by_id(aid)
        if self._drag_card is None:      # a drag from another workspace's card
            return
        event.acceptProposedAction()
        self._position_indicator(self._drop_index(event.position().toPoint()))

    def _drag_move(self, event) -> None:
        if self._drag_card is None or not self._reorderable(event):
            return
        event.acceptProposedAction()
        self._position_indicator(self._drop_index(event.position().toPoint()))

    def _drag_leave(self, event) -> None:
        self._drop_indicator.hide()

    def _drop(self, event) -> None:
        if self._drag_card is None:
            self._drop_indicator.hide()
            return
        self._drop_indicator.hide()
        idx = self._drop_index(event.position().toPoint())
        card = self._drag_card
        self._drag_card = None
        event.acceptProposedAction()
        self._reorder_to(card, idx)

    def _drop_index(self, pos) -> int:
        """Insertion index (among the cards OTHER than the dragged one) for a
        cursor at `pos` in grid-host coordinates. A card counts as 'before' the
        cursor when the cursor is below its row, or within its row band and to
        its right — reading order, so it works for any grid shape."""
        idx = 0
        for c in self.cards:
            if c is self._drag_card:
                continue
            g = c.geometry()
            cy, half = g.center().y(), g.height() / 2
            below_row = pos.y() > cy + half
            same_row = abs(pos.y() - cy) <= half
            if below_row or (same_row and pos.x() > g.center().x()):
                idx += 1
        return idx

    def _position_indicator(self, idx: int) -> None:
        """Show the gold insertion bar at the boundary for insertion `idx`
        (left edge of the card that would be pushed right, or after the last)."""
        others = [c for c in self.cards if c is not self._drag_card]
        if not others:
            self._drop_indicator.hide()
            return
        idx = max(0, min(idx, len(others)))
        if idx < len(others):
            g = others[idx].geometry()
            x = g.left() - 5
            y, h = g.top(), g.height()
        else:                                   # after the last card
            g = others[-1].geometry()
            x = g.right() + 1
            y, h = g.top(), g.height()
        self._drop_indicator.setGeometry(int(x), int(y), 4, int(h))
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def _reorder_to(self, card: "TerminalCard", idx: int) -> None:
        """Move `card` to insertion index `idx`, re-tile, animate the reflow,
        and persist the new order. No-ops if the order is unchanged."""
        others = [c for c in self.cards if c is not card]
        idx = max(0, min(idx, len(others)))
        new = others[:idx] + [card] + others[idx:]
        if [c.agent.id for c in new] == [c.agent.id for c in self.cards]:
            return
        old_geoms = {c: QRect(c.geometry()) for c in self.cards}
        self.cards = new
        self._retile()
        self.grid.activate()          # finalize geometries + clear the layout's
        #                               dirty flag, so the queued LayoutRequest
        #                               won't override the animation below
        self._animate_reflow(old_geoms)
        self.reorderCommitted.emit(self.workspace.id,
                                   [c.agent.id for c in self.cards])

    def _animate_reflow(self, old_geoms: dict) -> None:
        """Tween every card that moved from its old cell to its new one — the
        'snap into place' feel. Purely cosmetic: the cards are already at their
        correct final geometries (from _retile); this only eases them there."""
        group = QParallelAnimationGroup(self)
        for c in self.cards:
            new_g = QRect(c.geometry())
            old_g = old_geoms.get(c)
            if old_g is None or old_g == new_g:
                continue
            c.setGeometry(old_g)
            a = QPropertyAnimation(c, b"geometry", group)
            a.setDuration(180)
            a.setStartValue(old_g)
            a.setEndValue(new_g)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
        if group.animationCount() == 0:
            group.deleteLater()
            return
        self._reflow_anim = group     # hold a ref (else GC'd mid-flight)
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

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
