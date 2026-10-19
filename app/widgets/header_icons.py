"""Line icons for the workspace header: the folder and trash beside the path,
and Layout, Map, Activity and Lanes, plus the card header's lane chip and the
terminal find bar's arrows and close button.

Font glyphs (the old "▦", "◆", "❦", "⎇") render as colour emoji on some
systems and never match the text weight. These are stroked SVG paths (the
Lucide set, 24px grid) painted in the same ink the stylesheet gives the
button's text, so a hover, checked or dimmed state recolours both together.
Beside text, a header button's icon goes in the room the stylesheet reserves
with `padding-left`. A button with no text gets it centred. The lane chip
lays out its own icon and text (`LaneChip`).
"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QLabel, QStyle, QStyleOptionToolButton,
                               QStylePainter, QToolButton)

from .. import ui_theme
from ..ui_theme import Palette

ICON_SIZE = 14   # px, square
ICON_GAP = 6     # px between the icon and the text

_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' "
        "fill='none' stroke='{ink}' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'>{body}</svg>")

_BODIES = {
    "layout": ("<rect x='3' y='3' width='7' height='7' rx='1'/>"
               "<rect x='14' y='3' width='7' height='7' rx='1'/>"
               "<rect x='14' y='14' width='7' height='7' rx='1'/>"
               "<rect x='3' y='14' width='7' height='7' rx='1'/>"),
    "map": ("<polygon points='3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21'/>"
            "<line x1='9' x2='9' y1='3' y2='18'/>"
            "<line x1='15' x2='15' y1='6' y2='21'/>"),
    "activity": "<path d='M22 12h-4l-3 9L9 3l-3 9H2'/>",
    "lanes": ("<line x1='6' x2='6' y1='3' y2='15'/>"
              "<circle cx='18' cy='6' r='3'/><circle cx='6' cy='18' r='3'/>"
              "<path d='M18 9a9 9 0 0 1-9 9'/>"),
    # two lanes meeting in one: the integrator's chip (the lane chip's own
    # "lanes" icon, one line splitting in two, is every other agent's)
    "merge": ("<circle cx='6' cy='5' r='2.5'/><circle cx='18' cy='5' r='2.5'/>"
              "<circle cx='12' cy='19' r='2.5'/>"
              "<path d='M6 7.5c0 5 6 4 6 9'/><path d='M18 7.5c0 5-6 4-6 9'/>"),
    # the sidebar toggle: a window with its left panel divided off, and a
    # chevron inside that panel pointing where the click sends it (left to
    # fold it away, right to bring it back)
    "sidebar_close": ("<rect x='2.5' y='4.5' width='19' height='15' rx='3'/>"
                      "<path d='M10 4.5v15'/><path d='M7.8 9.2 5 12l2.8 2.8'/>"),
    "sidebar_open": ("<rect x='2.5' y='4.5' width='19' height='15' rx='3'/>"
                     "<path d='M10 4.5v15'/><path d='M5.2 9.2 8 12l-2.8 2.8'/>"),
    # the terminal find bar's previous / next / close
    "chevron_up": "<path d='m18 15-6-6-6 6'/>",
    "chevron_down": "<path d='m6 9 6 6 6-6'/>",
    "close": "<path d='M18 6 6 18'/><path d='m6 6 12 12'/>",
    "folder": ("<path d='M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 "
               "1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 "
               "0 0 2 2Z'/>"),
    "trash": ("<path d='M3 6h18'/>"
              "<path d='M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6'/>"
              "<path d='M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2'/>"
              "<line x1='10' x2='10' y1='11' y2='17'/>"
              "<line x1='14' x2='14' y1='11' y2='17'/>"),
}


@lru_cache(maxsize=64)
def _renderer(name: str, ink: str) -> QSvgRenderer:
    svg = _SVG.format(ink=ink, body=_BODIES[name])
    return QSvgRenderer(QByteArray(svg.encode()))


def _paint(widget, name: str, ink: str, left: float,
           size: int = ICON_SIZE) -> None:
    box = QRectF(left, (widget.height() - size) / 2, size, size)
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name, ink).render(p, box)
    p.end()


class IconToolButton(QToolButton):
    """A header button with a line icon left of its text, or centred when it
    has no text. The icon is blue while the button is checked, like the text,
    else the normal ink. `hover_ink` names the Palette colour the stylesheet's
    :hover rule gives the button (the trash turns RED): a painted icon can't
    read a pseudo-state's colour."""

    def __init__(self, icon_name: str, parent=None, hover_ink: str = ""):
        super().__init__(parent)
        self._icon_name = icon_name
        self._hover_ink = hover_ink

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.isChecked():
            ink = Palette.ACCENT_BLUE
        elif not self.isEnabled():
            ink = Palette.TEXT_FAINT
        elif self._hover_ink and self.underMouse():
            ink = getattr(Palette, self._hover_ink)
        else:
            ink = Palette.TEXT
        # 9px = the stylesheet's 8px horizontal padding plus the 1px border
        left = 9 if self.text() else (self.width() - ICON_SIZE) / 2
        _paint(self, self._icon_name, ink, left)


class SidebarToggleButton(QToolButton):
    """The top bar's sidebar toggle. Its icon says what a click does: a
    collapse icon while the sidebar is open, an expand icon while it is
    folded away. `MainWindow._sync_ws_rail` feeds it the state, so a drag, a
    restore and Ctrl+Shift+B all move it. Dim ink, full ink on hover, like
    the bar's other glyph buttons."""

    # 18px read as a smudge at a glance; 22px shows the panel and chevron
    ICON_SIZE = 22
    SIZE = (34, 32)
    TIP_COLLAPSE = "Collapse sidebar (Ctrl+Shift+B)"
    TIP_EXPAND = "Expand sidebar (Ctrl+Shift+B)"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(*self.SIZE)
        self._open = True
        self.set_sidebar_open(True)

    def sidebar_open(self) -> bool:
        return self._open

    def set_sidebar_open(self, is_open: bool) -> None:
        self._open = is_open
        tip = self.TIP_COLLAPSE if is_open else self.TIP_EXPAND
        self.setToolTip(tip)
        self.setAccessibleName(tip.split(" (")[0])
        self.update()

    def icon_name(self) -> str:
        return "sidebar_close" if self._open else "sidebar_open"

    def paintEvent(self, event):
        super().paintEvent(event)
        ink = Palette.TEXT if self.underMouse() else Palette.TEXT_DIM
        _paint(self, self.icon_name(), ink,
               (self.width() - self.ICON_SIZE) / 2, self.ICON_SIZE)


class PlusButton(QToolButton):
    """The usage pill picker's "+". The stylesheet draws the frame and the
    button paints the cross itself as two filled bars: the font's "+" sits
    off centre (its side bearings and baseline put it a pixel or two right
    and low), and which way depends on the font. Integer bars of even
    thickness in an even-sided button land on whole pixels, so both arms
    are crisp and the cross is centred both ways. Dim ink, full ink on
    hover, the colours `#UsageTrackerAdd` gives its text."""

    ARM = 10    # px, the length of each bar
    THICK = 2   # px

    def paintEvent(self, event):
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.text = ""
        p = QStylePainter(self)
        p.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, opt)
        ink = Palette.TEXT if self.underMouse() else Palette.TEXT_DIM
        x = (self.width() - self.ARM) // 2
        y = (self.height() - self.ARM) // 2
        mid_x = (self.width() - self.THICK) // 2
        mid_y = (self.height() - self.THICK) // 2
        p.fillRect(QRect(x, mid_y, self.ARM, self.THICK), QColor(ink))
        p.fillRect(QRect(mid_x, y, self.THICK, self.ARM), QColor(ink))
        p.end()


class LaneChip(QToolButton):
    """The agent card header's lane chip: a line icon, then the lane's status
    text ("↑2 ±3 ✓", "integrator ↑1"). The icon is the lanes branch, or the
    merge for the workspace's integrator.

    The chip paints its own text, because a crowded header squeezes it below
    its sizeHint (see terminal_card._HeaderTools: an over-subscribed layout
    shrinks every item, minimums included) and QToolButton then elides the
    text in the middle, "int...tor". Instead the chip drops whole words from
    the front until the rest fits: "integrator" goes first, since the merge
    icon already says it, then the counts, down to the icon alone. `text()`
    stays the full text, and the tooltip spells out the role and every count
    in words (`TerminalCard._lane_tooltip`).

    The card header asks the chip to give up words before any squeeze
    (`set_level`, from `TerminalCard._fit_header`). Level n leaves the first
    n words out of the size hint, so the layout is never asked for room it
    doesn't have, and the chip's neighbours don't get clipped either.

    The old chip led with the font glyph "⎇", which has wide side bearings
    (the gap left of it) and is small at 11px."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_name = "lanes"
        self._level = 0     # words left out of the front of the size hint

    def set_icon_name(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def _width_for(self, text: str) -> int:
        w = 2 * (1 + ui_theme.LANE_PAD_PX) + ui_theme.LANE_ICON_PX
        if text:
            w += (ui_theme.LANE_ICON_GAP_PX
                  + self.fontMetrics().horizontalAdvance(text))
        return w

    def levels(self) -> int:
        """How many forms the chip has: each tail of its words, then the icon
        alone."""
        return len(self.text().split()) + 1

    def level(self) -> int:
        return self._level

    def width_at(self, level: int) -> int:
        return self._width_for(" ".join(self.text().split()[level:]))

    def set_level(self, level: int) -> None:
        if level != self._level:
            self._level = level
            self.updateGeometry()
            self.update()

    def shown_text(self) -> str:
        """The longest tail of the text's words that fits the chip's width."""
        words = self.text().split()
        for i in range(len(words)):
            tail = " ".join(words[i:])
            if self._width_for(tail) <= self.width():
                return tail
        return ""

    def sizeHint(self):
        # exact, because QToolButton adds ~10px of its own around the text and
        # that shows as a gap between the icon and the text
        w = self.width_at(self._level)
        # the height of one line of text, with or without text: a bare chip
        # is icon-only to QToolButton and came out 1px taller, so the chip
        # shifted when a quiet lane made its first commit
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.toolButtonStyle = Qt.ToolButtonStyle.ToolButtonTextOnly
        h = self.style().sizeFromContents(
            QStyle.ContentsType.CT_ToolButton, opt,
            QSize(0, self.fontMetrics().height()), self).height()
        return QSize(w, h)

    def paintEvent(self, event):
        # the frame from the stylesheet (#CardLane[lane=...]), then the text
        # left-aligned after the icon, so a chip wider than its shown text
        # keeps the slack at the right instead of between icon and text
        shown = self.shown_text()
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.text = ""
        p = QStylePainter(self)
        p.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, opt)
        size = ui_theme.LANE_ICON_PX
        left = (1 + ui_theme.LANE_PAD_PX if shown
                else (self.width() - size) / 2)
        if shown:
            x = int(left) + size + ui_theme.LANE_ICON_GAP_PX
            p.drawItemText(QRect(x, 0, self.width() - x, self.height()),
                           Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter,
                           opt.palette, self.isEnabled(), shown,
                           QPalette.ColorRole.ButtonText)
        p.end()
        # the stylesheet's own text ink for this state (#CardLane[lane=...])
        ink = (Palette.CARDHEAD_SUB if self.property("lane") == "clean"
               else Palette.TEXT)
        _paint(self, self._icon_name, ink, left, size)


class IconLabel(QLabel):
    """The Lanes label, and the textless folder icon before the path. Its
    `forced` property dims the text, so the icon dims with it."""

    def __init__(self, icon_name: str, text: str, parent=None):
        super().__init__(text, parent)
        self._icon_name = icon_name

    def paintEvent(self, event):
        super().paintEvent(event)
        ink = Palette.TEXT_DIM if self.property("forced") else Palette.TEXT
        _paint(self, self._icon_name, ink, 0)
