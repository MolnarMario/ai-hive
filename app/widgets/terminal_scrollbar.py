"""The terminal's scrollbar, and the prompt milestones painted on it.

A slim bar overlaid on a TerminalView's right edge, showing how far back the
conversation reaches and marking every point where the USER submitted a typed
prompt, so a long session can be jumped through by milestone rather than
scrubbed through by hand.

Three decisions worth keeping:

* It is a real `QScrollBar` subclass, not a hand-painted widget. `ui_theme`
  already styles `QScrollBar:vertical` at exactly the width this wants, with
  the theme's own handle and hover colours, so the look follows every skin for
  free -- and drag, click-to-page, minimum handle size and keyboard handling
  come with it. Only the markers are painted on top.

* It is a CHILD of the terminal, positioned in `TerminalCard._place_overlay`
  like the boot veil and the wake banner, rather than a sibling in the card's
  layout. A sibling would take real estate out of `TerminalView.width()`, which
  is what `_apply_resize` derives the child's column count from -- so the bar
  would silently re-wrap every agent's output. Overlaying costs the rightmost
  sliver of glyph space instead, and only while there is anything to scroll.

* It HIDES ITSELF whenever history is empty. That is not just tidiness: a
  terminal with no scrollback has nothing to say here, and a hidden widget
  accepts no mouse events at all, so the terminal underneath keeps every pixel
  in the common case. It is also what makes the bar vanish on `/clear`, which
  is exactly what a cleared conversation should look like.

The value space is ABSOLUTE SCROLLBACK LINES, mapped so that `maximum` is live
(offset 0) and `0` is the oldest line still held. See TerminalView's coordinate
block for the `pushed - offset + row` identity everything here rests on.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QScrollBar, QToolTip

from ..ui_theme import Palette

WIDTH = 10          # matches the QScrollBar:vertical width in ui_theme's QSS
MARK_HIT_PX = 5     # click/hover slop around a marker
_MARK_W = 6         # marker dash, px
_MARK_H = 3
_INSET = 3          # keeps the first/last marker off the rounded ends


class TerminalScrollBar(QScrollBar):
    """Overview ruler for a TerminalView, with prompt milestones."""

    markActivated = Signal(int)     # absolute line of the clicked milestone

    def __init__(self, view, parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self._view = view
        self._syncing = False
        self.setObjectName("TerminalScrollBar")
        # never pull focus off the terminal on a drag, and don't inherit its
        # I-beam cursor -- this is chrome, not text
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # Enforce the width here rather than inheriting it from the style: the
        # QSS says 10px, but a widget built before the stylesheet is applied
        # (or in a test) would otherwise take the platform default and sit
        # noticeably wider over the conversation.
        self.setFixedWidth(WIDTH)
        self.setMouseTracking(True)     # for the marker tooltip
        self.setVisible(False)
        self.valueChanged.connect(self._on_value)

    # ------------------------------------------------------------- model ---

    def refresh(self) -> None:
        """Re-read the view's scroll state. Cheap and idempotent: this is wired
        to viewChanged, which a busy agent emits a few times a second."""
        hist = len(self._view.screen.history.top)
        if hist <= 0:
            self.setVisible(False)
            return
        self._syncing = True
        self.setRange(0, hist)
        self.setPageStep(max(1, self._view.screen.lines))
        self.setSingleStep(1)
        # value counts DOWN into history: maximum == live == offset 0
        self.setValue(hist - self._view.scroll_offset())
        self._syncing = False
        self.setVisible(True)
        self.update()

    def _on_value(self, value: int) -> None:
        if self._syncing:
            return
        hist = len(self._view.screen.history.top)
        self._view.scroll_by((hist - value) - self._view.scroll_offset())

    # -------------------------------------------------------- geometry ---

    def _span(self) -> tuple[int, int]:
        """(oldest absolute line, line count) the TRACK represents.

        This spans history PLUS the live screen, which is wider than the
        scrollable range (you can only scroll back as far as history goes).
        That matters: a prompt just submitted is still on the live screen, so
        bounding the track at `pushed` would leave its milestone undrawable
        until it happened to scroll off -- i.e. you type a prompt and no dot
        appears, which reads as the feature being broken."""
        hist = len(self._view.screen.history.top)
        return (self._view.history_pushed() - hist,
                hist + self._view.screen.lines)

    def _y_for(self, abs_line: int) -> float:
        oldest, span = self._span()
        frac = (abs_line - oldest) / float(max(1, span))
        frac = max(0.0, min(1.0, frac))
        return _INSET + frac * max(1, self.height() - 2 * _INSET)

    def _mark_at(self, y: float):
        """The nearest marker within MARK_HIT_PX of `y`, or None."""
        best = None
        best_d = MARK_HIT_PX + 1
        for abs_line, text in self._view.marks():
            d = abs(self._y_for(abs_line) - y)
            if d < best_d:
                best, best_d = (abs_line, text), d
        return best

    # ----------------------------------------------------------- events ---

    def wheelEvent(self, event) -> None:
        # The wheel has three regimes (forward to the child / alternate scroll
        # / local history) and they must stay decided in ONE place, so a wheel
        # over this 10px strip behaves exactly like one over the terminal.
        self._view.wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        hit = self._mark_at(event.position().y())
        if hit is not None:
            # consume it: falling through would ALSO page the bar, so a click
            # meant for a milestone would jump somewhere else entirely
            self.markActivated.emit(hit[0])
            event.accept()
            return
        super().mousePressEvent(event)

    def event(self, ev):
        if ev.type() == ev.Type.ToolTip:
            hit = self._mark_at(ev.pos().y())
            if hit is not None:
                QToolTip.showText(ev.globalPos(), _snippet(hit[1]), self)
            else:
                QToolTip.hideText()
            ev.accept()
            return True
        return super().event(ev)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)       # groove + handle, straight from the QSS
        marks = self._view.marks()
        if not marks:
            return
        oldest, span = self._span()
        newest = oldest + span
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # read at paint time so the marker follows a live theme switch, exactly
        # like BootVeil -- there is no QSS token for a custom-painted dash
        colour = QColor(Palette.ACCENT_ORANGE)
        colour.setAlpha(210)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        x = (self.width() - _MARK_W) / 2.0
        for abs_line, _text in marks:
            if not (oldest <= abs_line <= newest):
                continue    # scrolled out of history: no honest place to draw it
            y = self._y_for(abs_line) - _MARK_H / 2.0
            painter.drawRoundedRect(x, y, _MARK_W, _MARK_H, 1.5, 1.5)
        painter.end()


def _snippet(text: str, limit: int = 90) -> str:
    """One-line preview of a submitted prompt for the marker tooltip."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
