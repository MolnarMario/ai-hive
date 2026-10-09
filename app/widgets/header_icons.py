"""Line icons for the workspace header: the folder and trash beside the path,
and Layout, Map, Activity and Lanes.

Font glyphs (the old "▦", "◆", "❦", "⎇") render as colour emoji on some
systems and never match the text weight. These are stroked SVG paths (the
Lucide set, 24px grid) painted in the same ink the stylesheet gives the
button's text, so a hover, checked or dimmed state recolours both together.
The stylesheet reserves the room with `padding-left`; the icon is drawn there.
"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize
from PySide6.QtGui import QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QToolButton

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
        elif self._hover_ink and self.underMouse():
            ink = getattr(Palette, self._hover_ink)
        else:
            ink = Palette.TEXT
        # 9px = the stylesheet's 8px horizontal padding plus the 1px border
        left = 9 if self.text() else (self.width() - ICON_SIZE) / 2
        _paint(self, self._icon_name, ink, left)


class LaneChip(QToolButton):
    """The agent card header's lane chip: a line icon, then the lane's status
    text ("↑2 ±3 ✓", "integrator"). The icon is the lanes branch, or the merge
    for the workspace's integrator.

    The old chip led with the font glyph "⎇", which has wide side bearings
    (the gap left of it) and is small at 11px. The stylesheet reserves the
    icon's room as padding-left (`#CardLane`) and sets `bare` while there is no
    text, so a quiet lane is just the icon, centered."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_name = "lanes"

    def set_icon_name(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def sizeHint(self):
        # exact, because QToolButton adds ~10px of its own around the text and
        # that shows as a gap between the icon and the text
        pad, icon = ui_theme.LANE_PAD_PX, ui_theme.LANE_ICON_PX
        w = 2 * (1 + pad) + icon
        if self.text():
            w += (ui_theme.LANE_ICON_GAP_PX
                  + self.fontMetrics().horizontalAdvance(self.text()))
        return QSize(w, super().sizeHint().height())

    def paintEvent(self, event):
        super().paintEvent(event)
        size = ui_theme.LANE_ICON_PX
        left = (1 + ui_theme.LANE_PAD_PX if self.text()
                else (self.width() - size) / 2)
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
