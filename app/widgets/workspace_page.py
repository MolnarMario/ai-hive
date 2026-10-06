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

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent,
                            QParallelAnimationGroup, QPropertyAnimation, QRect,
                            Qt, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QMenu, QScrollArea, QToolButton, QVBoxLayout,
                               QWidget)

from ..terminal_agent import TerminalAgent
from ..tiling import compute_grid, explicit_grid, parse_layout
from ..ui_theme import Palette, repolish
from ..workspace_manager import Workspace
from .grid_selector import GridButton
from .header_icons import IconLabel, IconToolButton
from .lanes_help import show_lanes_explainer
from .ornaments import (ToggleSwitch, anchored_popup_pos,
                        close_on_anchor_press)
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
    scheduleRequested = Signal(str, str)  # agent id, text to prefill
    addRequested = Signal(str)          # ws_id (empty slot clicked)
    layoutChosen = Signal(str, str)     # ws_id, layout
    deleteRequested = Signal(str)       # ws_id
    openFolderRequested = Signal(str)   # ws_id
    openRepoRequested = Signal(str)     # ws_id
    repoActivityRequested = Signal(str)  # ws_id
    changePathRequested = Signal(str)   # ws_id
    activityToggled = Signal(str)       # ws_id (wired in Phase 6)
    mapRequested = Signal(str)          # ws_id (open the Agent/File Map window)
    fileActivated = Signal(str, str)    # ws_id, abs path (Ctrl+clicked in a card)
    reorderCommitted = Signal(str, list)  # ws_id, new ordered agent ids
    laneActionRequested = Signal(str, str, str)  # ws_id, agent id, action
    lanesToggled = Signal(str, bool)    # ws_id, the header's ⎇ Lanes toggle

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
        # MainWindow's view of an agent's lane roles, for the cards'
        # menus: callable(agent) -> dict (see TerminalCard.integration_info)
        self.integration_info = None

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
        # a QLabel's minimum size is its whole text, which would hold the
        # window at least as wide as the full path plus every button and
        # leave _elide_path nothing to elide. 60 is _path_budget's floor.
        self.path_label.setMinimumWidth(60)

        def tool(text, tip, obj="", icon=""):
            b = IconToolButton(icon, header) if icon else QToolButton(header)
            b.setText(text)
            b.setToolTip(tip)
            if obj:
                b.setObjectName(obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        # delete sits LEFT of open/change — the sidebar used to duplicate both
        # open-folder and delete as hover buttons on the workspace row; both
        # actions now live here, once, next to the folder they act on.
        # a text glyph, not Qt's stock trash pixmap: text takes the QSS color,
        # so it matches the header and turns red on hover like every delete
        self.delete_btn = tool("🗑", "Delete workspace", "WsTrash")
        self.delete_btn.setAccessibleName("Delete workspace")
        self.repo_btn = tool("Open repo", "Open this workspace's GitHub repository in a browser",
                             "RepoOpenButton")
        self.repo_activity_btn = tool("▾", "Show recent GitHub pull requests and commits",
                                      "RepoActivityMenuButton")
        # Treat the activity menu as the dropdown half of the repository
        # action. A zero-spacing host keeps the two controls visually joined
        # while preserving separate click targets.
        repo_actions = QWidget(header)
        repo_actions_lay = QHBoxLayout(repo_actions)
        repo_actions_lay.setContentsMargins(0, 0, 0, 0)
        repo_actions_lay.setSpacing(0)
        repo_actions_lay.addWidget(self.repo_btn)
        repo_actions_lay.addWidget(self.repo_activity_btn)
        # Open repo has no right border (the ▾ draws the shared edge), so its
        # hover lights that edge on the ▾ or the hover box reads open
        self.repo_btn.installEventFilter(self)
        self.repo_activity_menu = QMenu(self.repo_activity_btn)
        close_on_anchor_press(self.repo_activity_menu, self.repo_activity_btn)
        # MainWindow fills it in the background (app/repo_activity.py); a
        # click shows whatever is here at once
        self._repo_activity_shown = None
        self.reset_repo_activity()
        self.open_btn = tool("Open folder", "Open this workspace's folder",
                             "HeaderOpenFolder")
        self.change_btn = tool("Change…", "Change the workspace folder",
                               "HeaderChangeFolder")
        self.grid_button = GridButton(header)
        self.map_btn = tool("Map", "Show the agent / file map (who is "
                                   "working on which files)", "MapToggle", "map")
        self.activity_btn = tool("Activity", "Show the workspace activity board",
                                 "ActivityToggle", "activity")
        self.activity_btn.setCheckable(True)
        # the workspace's lanes toggle (spec-v4-lane-scopes.md): the default
        # "Own lane" tick for its new agents. Hidden outside a git
        # repository; MainWindow sets its state (set_lanes_state).
        self.lanes_box = QWidget(header)
        lanes_lay = QHBoxLayout(self.lanes_box)
        lanes_lay.setContentsMargins(0, 0, 0, 0)
        lanes_lay.setSpacing(5)
        self.lanes_label = IconLabel("lanes", "Lanes", self.lanes_box)
        self.lanes_label.setObjectName("HeaderLanesLabel")
        self.lanes_switch = ToggleSwitch(self.lanes_box)
        self.lanes_switch.setObjectName("HeaderLanesSwitch")
        # a tooltip can't hold a link, so the explainer has its own button
        self.lanes_help_btn = QToolButton(self.lanes_box)
        self.lanes_help_btn.setText("?")
        self.lanes_help_btn.setObjectName("HeaderLanesHelp")
        self.lanes_help_btn.setToolTip("What are lanes?")
        self.lanes_help_btn.setAccessibleName("What are lanes?")
        self.lanes_help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lanes_lay.addWidget(self.lanes_label)
        lanes_lay.addWidget(self.lanes_switch)
        lanes_lay.addWidget(self.lanes_help_btn)
        self.lanes_box.hide()

        hl.addWidget(folder_icon)
        hl.addWidget(self.path_label, 1)
        hl.addWidget(self.delete_btn)
        hl.addWidget(repo_actions)
        hl.addWidget(self.lanes_box)
        hl.addWidget(self.open_btn)
        hl.addWidget(self.change_btn)
        hl.addWidget(self.grid_button)
        hl.addWidget(self.map_btn)
        hl.addWidget(self.activity_btn)
        # kept for `_elide_path`: the space the path label may claim is the
        # header's width minus every OTHER item in this same row
        self._header_lay = hl

        self.delete_btn.clicked.connect(
            lambda: self.deleteRequested.emit(self.workspace.id))
        self.open_btn.clicked.connect(
            lambda: self.openFolderRequested.emit(self.workspace.id))
        self.repo_btn.clicked.connect(
            lambda: self.openRepoRequested.emit(self.workspace.id))
        self.repo_activity_btn.clicked.connect(self._show_repo_activity_menu)
        self.change_btn.clicked.connect(
            lambda: self.changePathRequested.emit(self.workspace.id))
        self.grid_button.layoutChosen.connect(self._on_layout_chosen)
        self.map_btn.clicked.connect(
            lambda: self.mapRequested.emit(self.workspace.id))
        # clicked (not toggled) so programmatic setChecked never re-fires
        self.activity_btn.clicked.connect(
            lambda: self.activityToggled.emit(self.workspace.id))
        self.lanes_switch.clicked.connect(self._on_lanes_clicked)
        self.lanes_help_btn.clicked.connect(
            lambda: show_lanes_explainer(self.window()))
        return header

    def _on_lanes_clicked(self) -> None:
        # emitted even while forced: MainWindow then puts the switch back
        self.lanesToggled.emit(self.workspace.id, self.lanes_switch.isChecked())

    def set_lanes_state(self, on: bool, forced: bool = False,
                        available: bool = True) -> None:
        """Reflect the workspace's lane default (no signal). `forced`: the
        Options switch "Lanes in every workspace" is on, so the toggle shows
        on and can't be changed here. `available`: the folder is in a git
        repository; outside one there is nothing to make a lane from."""
        self.lanes_switch.setChecked(bool(on))
        self.lanes_switch.setEnabled(not forced)
        if self.lanes_label.property("forced") is not bool(forced):
            self.lanes_label.setProperty("forced", bool(forced))
            repolish(self.lanes_label)
        if forced:
            tip = ("Lanes: ON for every workspace (the Options switch "
                   "\"Lanes in every workspace\" is on). Turn that off to "
                   "choose per workspace.")
        elif on:
            tip = ("Lanes: ON. A new Claude agent in this workspace gets its "
                   "own git worktree and branch (the New Agent dialog ticks "
                   "Own lane). Agents that are already here stay where they "
                   "are; each card offers Restart in own lane.\nClick to turn "
                   "off. Existing lanes are never touched.")
        else:
            tip = ("Lanes: OFF. New agents in this workspace share the "
                   "workspace folder. The New Agent dialog can still give one "
                   "agent its own lane.\nClick to turn on. Nothing runs and "
                   "no agent moves: it only changes the default for new "
                   "agents.")
        self.lanes_switch.setToolTip(tip)
        self.lanes_label.setToolTip(tip)
        if self.lanes_box.isHidden() == bool(available):
            self.lanes_box.setVisible(bool(available))
            self._elide_path()

    def eventFilter(self, obj, event):
        if obj is self.repo_btn and event.type() in (QEvent.Type.Enter,
                                                     QEvent.Type.Leave):
            lit = event.type() == QEvent.Type.Enter
            seam = self.repo_activity_btn
            if seam.property("seamLit") is not lit:
                seam.setProperty("seamLit", lit)
                repolish(seam)
        return super().eventFilter(obj, event)

    def _show_repo_activity_menu(self) -> None:
        menu = self.repo_activity_menu
        # Measure it before showing so Qt never paints it at the raw anchor
        # first and then visibly shifts it back onto the screen.
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(anchored_popup_pos(self.repo_activity_btn, menu.sizeHint(),
                                      align_right=True))
        self.repoActivityRequested.emit(self.workspace.id)

    def reset_repo_activity(self) -> None:
        """Back to the loading line: nothing fetched yet for this folder."""
        self._repo_activity_shown = None
        self.repo_activity_menu.clear()
        self.repo_activity_menu.addAction(
            "Loading recent GitHub activity…").setEnabled(False)

    def show_repo_activity(self, pull_requests: list, commits: list,
                           error: str = "", repo_url: str = "") -> None:
        """Replace the menu with GitHub activity for this workspace. The same
        data again is a no-op, so a refresh that found nothing new never
        rebuilds a menu the user has open."""
        shown = (pull_requests, commits, error, repo_url)
        if shown == self._repo_activity_shown:
            return
        self._repo_activity_shown = shown
        menu = self.repo_activity_menu
        menu.clear()
        self._fill_repo_activity(menu, pull_requests, commits, error, repo_url)
        if menu.isVisible():
            # rebuilt while open: re-measure and re-anchor, as on popup
            menu.adjustSize()
            menu.move(anchored_popup_pos(self.repo_activity_btn,
                                         menu.sizeHint(), align_right=True))

    def _fill_repo_activity(self, menu, pull_requests: list, commits: list,
                            error: str, repo_url: str) -> None:
        if error:
            menu.addAction(error).setEnabled(False)
            return
        if not repo_url:
            menu.addAction("No recent activity found").setEnabled(False)
            return

        menu.addSection("Pull requests")
        for item in pull_requests:
            label = f"#{item['number']}  {item['title']}"
            if item.get("state") == "closed" and item.get("merged"):
                label += "  (merged)"
            elif item.get("state") == "closed":
                label += "  (closed)"
            menu.addAction(label, lambda url=item["url"]: self._open_activity_url(url))
        if not pull_requests:
            menu.addAction("No pull requests found").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open all pull requests",
                       lambda url=repo_url + "/pulls?q=is%3Apr":
                       self._open_activity_url(url))

        menu.addSection("Commits")
        for item in commits:
            message = item["message"].splitlines()[0]
            label = f"{item['sha'][:7]}  {message}"
            menu.addAction(label, lambda url=item["url"]: self._open_activity_url(url))
        if not commits:
            menu.addAction("No commits found").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open all commits",
                       lambda url=repo_url + "/commits":
                       self._open_activity_url(url))

    @staticmethod
    def _open_activity_url(url: str) -> None:
        from .. import fsopen
        fsopen.open_url(url)

    def set_path_text(self, path: str) -> None:
        self.path_label.setToolTip(path)
        self._full_path = path
        self._elide_path()

    def _elide_path(self) -> None:
        """Show the FULL path whenever it fits; elide only the overflow.

        The available width is computed from the header row itself (this
        widget's own width minus every OTHER item's natural width), never
        from the label's OWN current width — eliding against the label's own
        width is self-referential: once elided down, its sizeHint shrinks to
        match, so it never grows back even when the window widens (reported
        live: a wide window still showed "C:/Users…/ai-hive" with a large gap
        of empty space before the buttons)."""
        path = getattr(self, "_full_path", self.workspace.project_path)
        fm = QFontMetrics(self.path_label.font())
        self.path_label.setText(fm.elidedText(
            path, Qt.TextElideMode.ElideMiddle, self._path_budget()))

    def _path_budget(self) -> int:
        """The width the header row leaves the path label. The row holds
        widgets only, no spacer items. QBoxLayout puts no spacing beside a
        hidden widget, so only the visible ones count: charging the hidden
        lanes box its 6px slot elided the path that much too early."""
        hl = getattr(self, "_header_lay", None)
        if hl is None:
            return max(60, self.path_label.width())
        others = [hl.itemAt(i).widget() for i in range(hl.count())]
        others = [w for w in others if w is not None
                  and w is not self.path_label and not w.isHidden()]
        margins = hl.contentsMargins()
        used = (margins.left() + margins.right()
                + hl.spacing() * len(others)
                + sum(w.sizeHint().width() for w in others))
        return max(60, self.width() - used)

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
        card.scheduleRequested.connect(self.scheduleRequested)
        card.maximizeRequested.connect(self.toggle_solo)
        card.fileActivated.connect(
            lambda p: self.fileActivated.emit(self.workspace.id, p))
        card.laneActionRequested.connect(
            lambda aid, action: self.laneActionRequested.emit(
                self.workspace.id, aid, action))
        card.integration_info = self._integration_info_for
        self.cards.append(card)
        # a freshly added agent must never be born invisible behind a maximized
        # sibling — adding one exits solo so the new card is seen
        self._exit_solo()
        self._retile()
        self._update_empty_state()
        return card

    def _integration_info_for(self, agent):
        fn = self.integration_info
        return fn(agent) if fn is not None else None

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
