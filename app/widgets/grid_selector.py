"""One-click grid layout selector.

A toolbar button that pops up a palette of clickable mini-diagrams. Choosing
one applies a fixed layout (or Auto) to the active workspace instantly.
Labels and layout strings read WIDTH × HEIGHT ("3×1" = three side by side),
and each swatch draws exactly the shape that will be applied — the smoke
suite asserts swatch == parse_layout for every entry (WYSIWYG). Wide
arrangements are offered generously for ultrawide monitors.
"""

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QGridLayout, QToolButton, QWidget)

from ..ui_theme import Palette

# (label "W×H", layout string, rows, cols). "auto" draws a special glyph.
LAYOUTS = [
    ("Auto", "auto", 0, 0),
    ("1×1", "1x1", 1, 1), ("2×1", "2x1", 1, 2), ("1×2", "1x2", 2, 1),
    ("3×1", "3x1", 1, 3), ("1×3", "1x3", 3, 1), ("2×2", "2x2", 2, 2),
    ("3×2", "3x2", 2, 3), ("2×3", "2x3", 3, 2), ("3×3", "3x3", 3, 3),
    ("4×2", "4x2", 2, 4), ("5×2", "5x2", 2, 5), ("4×3", "4x3", 3, 4),
]


class LayoutSwatch(QToolButton):
    chosen = Signal(str)

    def __init__(self, label, layout, rows, cols, parent=None):
        super().__init__(parent)
        self.layout_str = layout
        self._rows, self._cols = rows, cols
        self.setToolTip(f"{label} layout")
        self.setText(label)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setFixedSize(60, 58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: self.chosen.emit(self.layout_str))

    def sizeHint(self):
        return QSize(60, 58)

    def paintEvent(self, event):
        super().paintEvent(event)  # draws the frame + label text
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(Palette.ACCENT_GOLD if hasattr(Palette, "ACCENT_GOLD")
                        else Palette.ACCENT_BLUE)
        pen = QPen(accent)
        pen.setWidthF(1.2)
        p.setPen(pen)
        area = QRectF(14, 6, self.width() - 28, 30)
        if self.layout_str == "auto":
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "◆")
            p.end()
            return
        gap = 2.0
        cw = (area.width() - gap * (self._cols - 1)) / self._cols
        ch = (area.height() - gap * (self._rows - 1)) / self._rows
        for r in range(self._rows):
            for c in range(self._cols):
                x = area.left() + c * (cw + gap)
                y = area.top() + r * (ch + gap)
                p.drawRect(QRectF(x, y, cw, ch))
        p.end()


class GridSelectorPopup(QWidget):
    selected = Signal(str)

    def __init__(self, current="auto", parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("GridSelectorPopup")
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(4)
        per_row = 4
        for i, (label, layout, rows, cols) in enumerate(LAYOUTS):
            sw = LayoutSwatch(label, layout, rows, cols, self)
            sw.setChecked(layout == current)
            sw.chosen.connect(self._pick)
            grid.addWidget(sw, i // per_row, i % per_row)

    def _pick(self, layout):
        self.selected.emit(layout)
        self.close()


class GridButton(QToolButton):
    """Header button that opens the layout palette and reports the choice."""

    layoutChosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GridButton")
        self._current = "auto"
        self.setText("▦ Layout")
        self.setToolTip("Choose the agent grid layout")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open)

    def set_current(self, layout: str) -> None:
        self._current = layout or "auto"

    def _open(self):
        popup = GridSelectorPopup(self._current, self)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # no leak
        popup.selected.connect(self._on_selected)
        popup.adjustSize()
        popup.move(self._popup_position(popup.size()))
        popup.show()

    def _popup_position(self, size):
        """Anchor the palette under the button but keep it inside the app
        window (and the screen). The Layout button sits at the right end of the
        header, so left-anchoring the wide palette under it spills past the
        window's right edge; instead clamp to the tighter of the window and
        screen — overflow right → right-align to the button (palette grows
        leftward into the window), overflow bottom → flip above it."""
        w, h = size.width(), size.height()
        bl = self.mapToGlobal(self.rect().bottomLeft())
        br = self.mapToGlobal(self.rect().bottomRight())
        tl = self.mapToGlobal(self.rect().topLeft())
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        win = self.window().geometry()  # client area in global coords
        # bound to the intersection of the window and the screen
        left = max(avail.x(), win.x())
        top = max(avail.y(), win.y())
        right = min(avail.x() + avail.width(), win.x() + win.width())
        bottom = min(avail.y() + avail.height(), win.y() + win.height())
        x = bl.x()
        if x + w > right:
            x = br.x() - w                       # right-align under the button
        x = max(left, min(x, right - w))
        y = bl.y()
        if y + h > bottom:
            y = tl.y() - h                        # flip above the button
        y = max(top, min(y, bottom - h))
        return QPoint(x, y)

    def _on_selected(self, layout):
        self._current = layout
        self.layoutChosen.emit(layout)
