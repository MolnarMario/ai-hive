"""The terminal's find bar: Ctrl+F in an agent's terminal, shaped like Chrome's.

A box hangs from the top right corner of the TerminalView: the query, a
"3/17" count, previous and next arrows and a close button. Typing searches
as you go. Enter and F3 go to the next match, Shift+Enter and Shift+F3 to the
previous one, Ctrl+G and Ctrl+Shift+G too, and Esc closes the bar and gives
the terminal its focus back. The view does the searching and the painting
(TerminalView.set_find_query); this widget only collects keys and shows the
count.

It is a CHILD of the terminal, placed by TerminalCard._place_overlay like the
scrollbar, so it costs the terminal no columns. Esc only closes it while the
bar has focus: in the terminal Esc belongs to the agent (it interrupts
Claude), so clicking back into the conversation leaves the bar and its
highlights up, as Chrome's does.

The query survives a close, so Ctrl+F brings the last search back
preselected, unless text is selected in the terminal, which then becomes the
query. Nothing here is saved: a card rebuild starts with an empty bar.
"""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit

from .header_icons import IconToolButton
from .terminal_scrollbar import WIDTH as SCROLLBAR_WIDTH

MAX_WIDTH = 330     # px, about Chrome's
MIN_WIDTH = 150
HEIGHT = 32
EDGE = 4            # px between the bar and the scrollbar
# a selection longer than this is no query anyone meant to type
MAX_PREFILL = 200


class FindBar(QFrame):
    """Chrome-style find box over a TerminalView's top right corner."""

    def __init__(self, view, parent=None):
        super().__init__(parent if parent is not None else view)
        self._view = view
        self.setObjectName("TerminalFind")
        # chrome, not text: don't inherit the terminal's I-beam
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # The parent is the terminal, and an event this bar leaves unaccepted
        # goes on to it: a click on the bar's margin would reach the agent as
        # a click on whatever cell sits under the bar.
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        self.setFixedHeight(HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 3, 2)
        lay.setSpacing(1)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("TerminalFindInput")
        self.edit.setPlaceholderText("Find in conversation")
        self.count = QLabel(self)
        self.count.setObjectName("TerminalFindCount")
        self.prev_btn = self._button("chevron_up",
                                     "Previous match (Shift+Enter)")
        self.next_btn = self._button("chevron_down", "Next match (Enter)")
        self.close_btn = self._button("close", "Close (Esc)")
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.count)
        lay.addSpacing(4)
        lay.addWidget(self.prev_btn)
        lay.addWidget(self.next_btn)
        lay.addWidget(self.close_btn)

        self.edit.textChanged.connect(self._on_text)
        self.edit.installEventFilter(self)
        self.prev_btn.clicked.connect(lambda: self._view.find_step(-1))
        self.next_btn.clicked.connect(lambda: self._view.find_step(1))
        self.close_btn.clicked.connect(self.close_find)
        view.findChanged.connect(self._on_changed)
        self._on_changed(0, 0)
        self.hide()

    def _button(self, icon: str, tip: str) -> IconToolButton:
        b = IconToolButton(icon, self)
        b.setObjectName("TerminalFindButton")
        b.setToolTip(tip)
        b.setAccessibleName(tip.split(" (")[0])
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        # a click must leave the focus in the query box, as Chrome's does
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setFixedSize(26, 24)
        return b

    # ------------------------------------------------------------- api ---

    def is_open(self) -> bool:
        return self.isVisible()

    def open(self, prefill: str = "") -> None:
        """Show the bar (or refocus it) with the query selected. A one-line
        `prefill` (the terminal's selection) replaces the query."""
        prefill = prefill.strip()
        if prefill and "\n" not in prefill and len(prefill) <= MAX_PREFILL:
            self.edit.setText(prefill)      # searches via textChanged
        if not self.isVisible():
            self.show()
            self.raise_()
            self.place()
            if self.edit.text():
                self._view.set_find_query(self.edit.text())
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()

    def close_find(self) -> None:
        """Hide the bar, drop the highlights and hand the focus back to the
        terminal. The query stays for the next Ctrl+F."""
        if not self.isVisible():
            return
        self.hide()
        self._view.set_find_inset(0)
        self._view.clear_find()
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)

    def place(self) -> None:
        """Pin the bar to the view's top right, left of the scrollbar."""
        room = self._view.width() - SCROLLBAR_WIDTH - 2 * EDGE
        w = max(MIN_WIDTH, min(MAX_WIDTH, room))
        x = max(0, self._view.width() - SCROLLBAR_WIDTH - EDGE - w)
        self.setGeometry(x, 0, w, HEIGHT)
        if self.isVisible():
            self._view.set_find_inset(HEIGHT)

    # ---------------------------------------------------------- events ---

    def keyPressEvent(self, event) -> None:
        # A key the query box leaves alone (Up, PageDown, an F-key) would
        # otherwise go on to the terminal and be typed into the agent.
        event.accept()

    def contextMenuEvent(self, event) -> None:
        event.accept()      # not the terminal's menu for the cell underneath

    def wheelEvent(self, event) -> None:
        # the wheel over the bar scrolls the conversation rather than dying here
        self._view.overlay_wheel(event)

    def _on_text(self, text: str) -> None:
        if self.isVisible():
            self._view.set_find_query(text)

    def _on_changed(self, active: int, total: int) -> None:
        has_query = bool(self.edit.text())
        self.count.setText(f"{active}/{total}" if has_query else "")
        self.prev_btn.setEnabled(total > 0)
        self.next_btn.setEnabled(total > 0)

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if key == Qt.Key.Key_Escape:
                self.close_find()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F3) \
                    or (ctrl and key == Qt.Key.Key_G):
                self._view.find_step(-1 if shift else 1)
                return True
            if ctrl and not shift and key == Qt.Key.Key_F:
                self.edit.selectAll()
                return True
        return super().eventFilter(obj, event)
