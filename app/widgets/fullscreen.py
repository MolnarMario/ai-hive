"""F11 fullscreen: the screen holds only the agent grid, and the chrome
floats back over it on demand.

Entering moves the top bar, every workspace header and the sidebar out of
the window's layouts. The top bar and the sidebar go into two floating
overlays (children of the central widget, above the grid). Resting the
cursor on the top edge for REVEAL_MS shows the top overlay: the top bar
plus the active page's header, lent to the overlay while it shows. Resting
it on the left edge shows the left overlay: the sidebar at the width it
had before F11, or the thin workspace rail when the sidebar was collapsed.
Moving the cursor off an overlay hides it again.

The overlays float, never push. Showing chrome inside the layout would
resize every terminal on each reveal, and each resize makes Claude redraw.

Nothing here is persisted. A session saved while fullscreen records the
pre-F11 window and sidebar state (`saved_ui`), so the next start opens the
way the user left it before pressing F11.

The cursor is polled rather than tracked with events: the terminals under
the edge eat the mouse moves, and the poll runs only while fullscreen."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QVBoxLayout

from .sidebar import WorkspaceRail

REVEAL_MS = 1500        # cursor rest on an edge before the chrome shows
EDGE_PX = 2             # how close to the edge counts as "on" it
POLL_MS = 100
CONCEAL_GRACE_MS = 400  # cursor away from an overlay before it hides


class _Overlay(QFrame):
    def __init__(self, parent, vertical: bool):
        super().__init__(parent)
        self.setObjectName("FullscreenOverlay")
        lay = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.hide()


class FullscreenController(QObject):
    """Owns F11 for one MainWindow. The window keeps its widgets; this
    only moves them between the window's layouts and the overlays."""

    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self.active = False
        self._was_maximized = False
        self._normal_geometry = None
        self._sidebar_open = True
        self._left_widget = None    # the sidebar or the rail, while active
        self._lent = None           # (page, header) shown in the top overlay
        self._left_held = False     # shown by ☰ / Ctrl+Shift+B, not the edge
        self._top_dwell = self._left_dwell = 0
        self._top_away = self._left_away = 0
        self.top = _Overlay(win._central, vertical=True)
        self.left = _Overlay(win._central, vertical=False)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------ toggle ---

    def toggle(self) -> None:
        if self.active:
            self.exit()
        else:
            self.enter()

    def enter(self) -> None:
        if self.active:
            return
        w = self._win
        self.active = True
        self._was_maximized = w.isMaximized()
        self._normal_geometry = w.normalGeometry()
        sizes = w.body_split.sizes()
        self._sidebar_open = sizes[0] > 0
        if self._sidebar_open:
            w._sidebar_saved_width = sizes[0]
        w._root_layout.removeWidget(w.top_bar)
        self.top.layout().addWidget(w.top_bar)
        for page in w._pages.values():
            page.header.hide()
        if self._sidebar_open:
            # reparenting takes it out of the splitter
            lw, width = w.sidebar, w._sidebar_saved_width
        else:
            w._body_lay.removeWidget(w.ws_rail)
            lw, width = w.ws_rail, WorkspaceRail.WIDTH
            # the collapsed sidebar stays in the splitter at width 0, so its
            # handle sits on the very edge the poll watches, and a drag there
            # would open the sidebar inside the layout and resize every card.
            # Disabled, not hidden: QSplitter shows its handles again itself
            handle = w.body_split.handle(1)
            handle.setEnabled(False)
            handle.setCursor(Qt.CursorShape.ArrowCursor)
        self.left.layout().addWidget(lw)
        lw.show()
        self.left.setFixedWidth(width)
        self._left_widget = lw
        self._top_dwell = self._left_dwell = 0
        w.showFullScreen()
        self._timer.start()

    def exit(self) -> None:
        if not self.active:
            return
        w = self._win
        self._timer.stop()
        self.conceal()
        self.active = False
        # the window size first, so the sidebar width below is set against
        # the restored splitter rather than a screen-wide one
        if w.isFullScreen():
            if self._was_maximized:
                w.showMaximized()
            else:
                w.showNormal()
                # the offscreen platform (the suite's) restores the screen
                # size instead of the restore-down size
                geo = self._normal_geometry
                if geo is not None and geo.isValid():
                    w.setGeometry(geo)
        self.top.layout().removeWidget(w.top_bar)
        w._root_layout.insertWidget(0, w.top_bar)
        w.top_bar.show()
        for page in w._pages.values():
            page.header.show()
        lw, self._left_widget = self._left_widget, None
        self.left.layout().removeWidget(lw)
        if lw is w.sidebar:
            split = w.body_split
            split.insertWidget(0, w.sidebar)
            # a re-inserted widget gets the splitter's defaults back
            split.setCollapsible(0, True)
            split.setCollapsible(1, False)
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 1)
            width = w._sidebar_saved_width
            split.setSizes([width, max(1, split.width() - width)])
        else:
            w._body_lay.insertWidget(0, w.ws_rail)
            handle = w.body_split.handle(1)
            handle.setEnabled(True)
            handle.setCursor(Qt.CursorShape.SplitHCursor)
        w._sync_ws_rail()

    def saved_ui(self) -> tuple[bool, bool]:
        """(sidebar collapsed, window maximized) as they were before F11."""
        return not self._sidebar_open, self._was_maximized

    # ---------------------------------------------------------- overlays ---

    def reveal_top(self) -> None:
        w = self._win
        page = w._pages.get(w.manager.active_id)
        if page is not None and self._lent is None:
            page.layout().removeWidget(page.header)
            self.top.layout().addWidget(page.header)
            page.header.show()
            self._lent = (page, page.header)
        self._top_away = 0
        self._place()
        self.top.show()
        self.top.raise_()

    def conceal_top(self) -> None:
        self.top.hide()
        if self._lent is not None:
            page, header = self._lent
            self._lent = None
            self.top.layout().removeWidget(header)
            page.layout().insertWidget(0, header)
            header.setVisible(not self.active)

    def reveal_left(self, held: bool = False) -> None:
        self._left_held = held
        self._left_away = 0
        self._place()
        self.left.show()
        self.left.raise_()
        if self.top.isVisible():
            self.top.raise_()

    def conceal_left(self) -> None:
        self.left.hide()
        self._left_held = False

    def toggle_left(self) -> None:
        """☰ and Ctrl+Shift+B while fullscreen: show or hide the sidebar
        overlay, and keep it up until the cursor has been over it."""
        if self.left.isVisible():
            self.conceal_left()
        else:
            self.reveal_left(held=True)

    def conceal(self) -> None:
        self.conceal_top()
        self.conceal_left()

    def page_changed(self) -> None:
        """The active workspace changed: the top overlay lends the new
        page's header instead."""
        if self.top.isVisible():
            self.conceal_top()
            self.reveal_top()

    def adopt_page(self, page) -> None:
        if self.active:
            page.header.hide()

    def release_page(self, page) -> None:
        """Hand a page about to be deleted its header back first."""
        if self._lent is not None and self._lent[0] is page:
            self.conceal_top()

    def _place(self) -> None:
        c = self._win._central
        self.top.setGeometry(0, 0, c.width(), self.top.sizeHint().height())
        self.left.setGeometry(0, 0, self.left.width(), c.height())

    # -------------------------------------------------------------- poll ---

    def _poll(self) -> None:
        w = self._win
        c = w._central
        pos = c.mapFromGlobal(QCursor.pos())
        on_x = 0 <= pos.x() < c.width()
        on_y = 0 <= pos.y() < c.height()
        # a popup or dialog the chrome opened keeps the chrome up
        busy = (QApplication.activePopupWidget() is not None
                or QApplication.activeModalWidget() is not None)
        armed = w.isActiveWindow() and not busy
        step = self._timer.interval()

        at_top = armed and on_x and 0 <= pos.y() <= EDGE_PX
        at_left = armed and on_y and 0 <= pos.x() <= EDGE_PX
        self._top_dwell = self._top_dwell + step if at_top else 0
        self._left_dwell = self._left_dwell + step if at_left else 0
        if not self.top.isVisible() and self._top_dwell >= REVEAL_MS:
            self.reveal_top()
        if not self.left.isVisible() and self._left_dwell >= REVEAL_MS:
            self.reveal_left()

        if self.top.isVisible():
            self._place()
            if busy or self.top.geometry().contains(pos):
                self._top_away = 0
            else:
                self._top_away += step
                if self._top_away >= CONCEAL_GRACE_MS:
                    self.conceal_top()
        if self.left.isVisible():
            self._place()
            over = self.left.geometry().contains(pos)
            if over:
                self._left_held = False
            if busy or over or self._left_held:
                self._left_away = 0
            else:
                self._left_away += step
                if self._left_away >= CONCEAL_GRACE_MS:
                    self.conceal_left()
