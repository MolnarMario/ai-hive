"""Manuscript ornaments, painted with QPainter + QSvgRenderer (no raster
assets). The elaborate illuminated pieces (gilt drop-cap, logo roundel, the
foliate page border with gilt corner medallions) render only when the active
theme sets `illuminated`; otherwise the simple gold-on-dark versions are used,
so the other skins stay clean and light. SVG geometry/colours are lifted from
the design handoff's inline data-URIs.
"""

from PySide6.QtCore import (QAbstractAnimation, QByteArray, QEasingCurve,
                            QRectF, Qt, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from .. import ui_theme
from ..ui_theme import Palette

# --- lifted SVG ornament tiles (transparent grounds; colours are the design's) ---

_BORDER_H = """<svg xmlns='http://www.w3.org/2000/svg' width='220' height='32'>
<line x1='0' y1='3' x2='220' y2='3' stroke='rgb(224,188,86)' stroke-width='1'/>
<line x1='0' y1='29' x2='220' y2='29' stroke='rgb(224,188,86)' stroke-width='1'/>
<path d='M0 16 Q27.5 5 55 16 Q82.5 27 110 16 Q137.5 5 165 16 Q192.5 27 220 16' fill='none' stroke='rgb(122,86,20)' stroke-width='2.8'/>
<path d='M0 16 Q27.5 5 55 16 Q82.5 27 110 16 Q137.5 5 165 16 Q192.5 27 220 16' fill='none' stroke='rgb(232,196,96)' stroke-width='1.4'/>
<g fill='rgb(230,193,90)' stroke='rgb(122,86,20)' stroke-width='0.7'>
<circle cx='27.5' cy='6' r='3.2'/><circle cx='137.5' cy='6' r='3.2'/>
<circle cx='82.5' cy='26' r='3.2'/><circle cx='192.5' cy='26' r='3.2'/></g>
<g fill='rgb(182,46,44)'>
<circle cx='21.5' cy='6' r='1.6'/><circle cx='33.5' cy='6' r='1.6'/>
<circle cx='131.5' cy='6' r='1.6'/><circle cx='143.5' cy='6' r='1.6'/>
<circle cx='76.5' cy='26' r='1.6'/><circle cx='88.5' cy='26' r='1.6'/>
<circle cx='186.5' cy='26' r='1.6'/><circle cx='198.5' cy='26' r='1.6'/></g>
<g fill='rgb(96,156,86)' stroke='rgb(52,104,58)' stroke-width='0.5'>
<circle cx='110' cy='10.5' r='3.1'/><circle cx='105.8' cy='17.5' r='3.1'/>
<circle cx='114.2' cy='17.5' r='3.1'/></g>
<g>
<circle cx='60' cy='16' r='2.7' fill='rgb(40,86,172)'/><circle cx='57.5' cy='11.5' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='52.5' cy='11.5' r='2.7' fill='rgb(40,86,172)'/><circle cx='50' cy='16' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='52.5' cy='20.5' r='2.7' fill='rgb(40,86,172)'/><circle cx='57.5' cy='20.5' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='55' cy='16' r='2.5' fill='rgb(182,46,44)'/>
<circle cx='170' cy='16' r='2.7' fill='rgb(230,193,90)'/><circle cx='167.5' cy='11.5' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='162.5' cy='11.5' r='2.7' fill='rgb(230,193,90)'/><circle cx='160' cy='16' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='162.5' cy='20.5' r='2.7' fill='rgb(230,193,90)'/><circle cx='167.5' cy='20.5' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='165' cy='16' r='2.5' fill='rgb(182,46,44)'/></g></svg>"""

_BORDER_V = """<svg xmlns='http://www.w3.org/2000/svg' width='32' height='220'>
<line x1='3' y1='0' x2='3' y2='220' stroke='rgb(224,188,86)' stroke-width='1'/>
<line x1='29' y1='0' x2='29' y2='220' stroke='rgb(224,188,86)' stroke-width='1'/>
<path d='M16 0 Q5 27.5 16 55 Q27 82.5 16 110 Q5 137.5 16 165 Q27 192.5 16 220' fill='none' stroke='rgb(122,86,20)' stroke-width='2.8'/>
<path d='M16 0 Q5 27.5 16 55 Q27 82.5 16 110 Q5 137.5 16 165 Q27 192.5 16 220' fill='none' stroke='rgb(232,196,96)' stroke-width='1.4'/>
<g fill='rgb(230,193,90)' stroke='rgb(122,86,20)' stroke-width='0.7'>
<circle cx='6' cy='27.5' r='3.2'/><circle cx='6' cy='137.5' r='3.2'/>
<circle cx='26' cy='82.5' r='3.2'/><circle cx='26' cy='192.5' r='3.2'/></g>
<g fill='rgb(182,46,44)'>
<circle cx='6' cy='21.5' r='1.6'/><circle cx='6' cy='33.5' r='1.6'/>
<circle cx='6' cy='131.5' r='1.6'/><circle cx='6' cy='143.5' r='1.6'/>
<circle cx='26' cy='76.5' r='1.6'/><circle cx='26' cy='88.5' r='1.6'/>
<circle cx='26' cy='186.5' r='1.6'/><circle cx='26' cy='198.5' r='1.6'/></g>
<g fill='rgb(96,156,86)' stroke='rgb(52,104,58)' stroke-width='0.5'>
<circle cx='10.5' cy='110' r='3.1'/><circle cx='17.5' cy='105.8' r='3.1'/>
<circle cx='17.5' cy='114.2' r='3.1'/></g>
<g>
<circle cx='16' cy='60' r='2.7' fill='rgb(40,86,172)'/><circle cx='11.5' cy='57.5' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='11.5' cy='52.5' r='2.7' fill='rgb(40,86,172)'/><circle cx='16' cy='50' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='20.5' cy='52.5' r='2.7' fill='rgb(40,86,172)'/><circle cx='20.5' cy='57.5' r='2.7' fill='rgb(230,193,90)'/>
<circle cx='16' cy='55' r='2.5' fill='rgb(182,46,44)'/>
<circle cx='16' cy='170' r='2.7' fill='rgb(230,193,90)'/><circle cx='11.5' cy='167.5' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='11.5' cy='162.5' r='2.7' fill='rgb(230,193,90)'/><circle cx='16' cy='160' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='20.5' cy='162.5' r='2.7' fill='rgb(230,193,90)'/><circle cx='20.5' cy='167.5' r='2.7' fill='rgb(40,86,172)'/>
<circle cx='16' cy='165' r='2.5' fill='rgb(182,46,44)'/></g></svg>"""

_MEDALLION = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 46 46'>
<ellipse cx='23' cy='8' rx='3.6' ry='7' fill='rgb(40,86,172)'/>
<ellipse cx='23' cy='38' rx='3.6' ry='7' fill='rgb(40,86,172)'/>
<ellipse cx='8' cy='23' rx='7' ry='3.6' fill='rgb(40,86,172)'/>
<ellipse cx='38' cy='23' rx='7' ry='3.6' fill='rgb(40,86,172)'/>
<ellipse cx='12.4' cy='12.4' rx='5.6' ry='3' fill='rgb(182,46,44)' transform='rotate(45 12.4 12.4)'/>
<ellipse cx='33.6' cy='12.4' rx='5.6' ry='3' fill='rgb(182,46,44)' transform='rotate(-45 33.6 12.4)'/>
<ellipse cx='12.4' cy='33.6' rx='5.6' ry='3' fill='rgb(182,46,44)' transform='rotate(-45 12.4 33.6)'/>
<ellipse cx='33.6' cy='33.6' rx='5.6' ry='3' fill='rgb(182,46,44)' transform='rotate(45 33.6 33.6)'/>
<circle cx='23' cy='23' r='6.6' fill='rgb(230,193,90)' stroke='rgb(122,86,20)' stroke-width='1'/>
<circle cx='23' cy='23' r='2.8' fill='rgb(182,46,44)'/></svg>"""

_DROPCAP_VINE = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 56 56'>
<g stroke='rgba(255,255,255,0.78)' stroke-width='1.3' fill='none'>
<path d='M6 6 Q17 9 14 20'/><path d='M50 6 Q39 9 42 20'/>
<path d='M6 50 Q17 47 14 36'/><path d='M50 50 Q39 47 42 36'/></g>
<g fill='rgba(255,255,255,0.72)'>
<circle cx='15' cy='20' r='1.8'/><circle cx='41' cy='20' r='1.8'/>
<circle cx='15' cy='36' r='1.8'/><circle cx='41' cy='36' r='1.8'/></g></svg>"""

_ROUNDEL_VINE = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'>
<g stroke='rgba(255,255,255,0.8)' stroke-width='1.4' fill='none'>
<path d='M8 8 Q18 12 15 22'/><path d='M40 8 Q30 12 33 22'/>
<path d='M8 40 Q18 36 15 26'/><path d='M40 40 Q30 36 33 26'/></g></svg>"""

# --- Adeptus Mechanicus SVG ornaments (from the cogitator handoff) ---

_AQUILA = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 64'>
<g fill='#242b31' stroke='#616b73' stroke-width='0.8'>
<path d='M60 30 C72 22 88 20 104 26 C92 27 82 30 74 36 C86 31 96 31 105 37 C93 37 83 40 75 45 C86 42 95 43 102 49 C90 48 78 47 68 44 C64 42 61 37 60 33 Z'/>
<path d='M60 30 C48 22 32 20 16 26 C28 27 38 30 46 36 C34 31 24 31 15 37 C27 37 37 40 45 45 C34 42 25 43 18 49 C30 48 42 47 52 44 C56 42 59 37 60 33 Z'/>
<path d='M60 26 C63 17 70 15 75 10 C75 16 72 22 66 25 C64 26 62 26 60 27 Z'/>
<path d='M60 26 C57 17 50 15 45 10 C45 16 48 22 54 25 C56 26 58 26 60 27 Z'/>
<path d='M55 42 L60 62 L65 42 C63 46 57 46 55 42 Z'/>
<circle cx='60' cy='31' r='5.4' fill='#2f373d'/>
<circle cx='60' cy='31' r='2' fill='#0c0f11'/></g></svg>"""

_COG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'>
<polygon points='46,24 39.9,28.3 43,35 35.7,35.7 35,43 28.3,39.9 24,46 19.7,39.9 13,43 12.3,35.7 5,35 8.1,28.3 2,24 8.1,19.7 5,13 12.3,12.3 13,5 19.7,8.1 24,2 28.3,8.1 35,5 35.7,12.3 43,13 39.9,19.7' fill='none' stroke='#5fe8ac' stroke-width='2.2'/>
<g stroke='#5fe8ac' stroke-width='1.3'>
<line x1='24' y1='11' x2='24' y2='37'/><line x1='11' y1='24' x2='37' y2='24'/>
<line x1='15' y1='15' x2='33' y2='33'/><line x1='33' y1='15' x2='15' y2='33'/></g></svg>"""

_SERVO_CIRCUIT = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 54 54'>
<g stroke='rgba(90,232,172,0.55)' stroke-width='1' fill='none'>
<path d='M8 8 h9 v-3'/><path d='M46 8 h-9 v-3'/>
<path d='M8 46 h9 v3'/><path d='M46 46 h-9 v3'/></g></svg>"""


def _pixmap(svg: str, w: int, h: int) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    p = QPainter(pm)
    r.render(p, QRectF(0, 0, w, h))
    p.end()
    return pm


def _gilt(rect: QRectF) -> QRadialGradient:
    """The gold-leaf boss gradient (light at ~32%/26%, deep at the rim)."""
    g = QRadialGradient(rect.left() + rect.width() * 0.32,
                        rect.top() + rect.height() * 0.26,
                        max(rect.width(), rect.height()) * 0.85)
    g.setColorAt(0.0, QColor("#f7e18c"))
    g.setColorAt(0.6, QColor("#c39430"))
    g.setColorAt(1.0, QColor("#8a6218"))
    return g


def _ornament() -> str:
    """Active theme's ornament set: 'manuscript' | 'mechanicus' | ''."""
    return getattr(ui_theme.ACTIVE_THEME, "ornament", "") or ""


def _gunmetal(rect: QRectF) -> QLinearGradient:
    """Brushed-steel casing gradient (top-light → bottom-dark)."""
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0.0, QColor("#414c56"))
    g.setColorAt(0.38, QColor("#2b333a"))
    g.setColorAt(0.72, QColor("#1b2126"))
    g.setColorAt(1.0, QColor("#141a1e"))
    return g


class DropCap(QWidget):
    """An illuminated initial. In an illuminated theme it's a gilt tile with a
    painted field, white vine tendrils and a Cinzel capital; otherwise a
    simple gold letter in a thin frame."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._letter = (text.strip()[:1] or "·").upper()
        self.setFixedSize(30, 34)

    def set_text(self, text: str) -> None:
        letter = (text.strip()[:1] or "·").upper()
        if letter != self._letter:
            self._letter = letter
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        orn = _ornament()
        odd = ord(self._letter[0]) % 2
        if orn == "manuscript":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_gilt(rect))
            p.drawRoundedRect(rect, 4, 4)          # gilt boss
            field = rect.adjusted(3, 3, -3, -3)
            # alternate the painted ground per initial (vermillion / ultramarine)
            p.setBrush(QColor("#8a1f22" if odd else "#173571"))
            p.drawRoundedRect(field, 3, 3)
            p.drawPixmap(field.toRect(), _pixmap(_DROPCAP_VINE, 56, 56))
            self._letter_glyph(p, rect, "Cinzel Decorative", "#f6de86")
        elif orn == "mechanicus":
            # servo-initial: dark green/steel field, phosphor border + circuit
            field = rect.adjusted(1, 1, -1, -1)
            p.setBrush(QColor("#0c3a2a" if odd else "#123138"))
            pen = QPen(QColor("#2f9a76"))
            pen.setWidthF(1.1)
            p.setPen(pen)
            p.drawRoundedRect(field, 4, 4)
            p.drawPixmap(field.toRect(), _pixmap(_SERVO_CIRCUIT, 54, 54))
            self._letter_glyph(p, rect, "Cinzel Decorative", "#9dffcf")
        else:
            p.setBrush(QColor(Palette.BG_ACTIVE))
            pen = QPen(QColor(Palette.ACCENT_GOLD_DIM))
            pen.setWidthF(1.2)
            p.setPen(pen)
            p.drawRoundedRect(rect, 4, 4)
            self._letter_glyph(p, rect, "Constantia", Palette.ACCENT_GOLD)
        p.end()

    def _letter_glyph(self, p, rect, family, color):
        f = QFont(family)
        if not f.exactMatch():
            f = QFont("Cambria")
        f.setPixelSize(18)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._letter)


class AgentCountBadge(QWidget):
    """A workspace's agent tally: a rounded badge showing how many agents are
    open, colour-coded by state — pulsing amber while any agent is working,
    green when all are idle (running, no errors), red on error, dim when empty.
    Colours are read from the live `Palette` at paint time so the badge tracks
    every skin (no per-skin QSS needed). The pulse runs ONLY in the working
    state; every other state is static, so idle rows cost nothing. Clicking it
    emits `clicked` (the row opens its agent dropdown; the click is consumed so
    it never bubbles up to select/switch the workspace)."""

    clicked = Signal()

    # resolved per-paint — Palette attrs are rewritten in place on skin switch
    _STATE_COLOR = {
        "working": lambda: Palette.YELLOW,
        "idle": lambda: Palette.GREEN,
        "error": lambda: Palette.RED,
        "empty": lambda: Palette.TEXT_DIM,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Show this workspace's agents")
        self._count = 0
        self._state = "empty"
        self._pulse = 0.0  # 0..1 breathing factor while working
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setKeyValueAt(0.5, 1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(1100)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.valueChanged.connect(self._on_pulse)

    def set_state(self, count: int, state: str) -> None:
        if state not in self._STATE_COLOR:
            state = "empty"
        changed = count != self._count or state != self._state
        self._count, self._state = count, state
        if state == "working":
            if self._anim.state() != QAbstractAnimation.State.Running:
                self._anim.start()
        else:
            self._anim.stop()
            self._pulse = 0.0
        if changed:
            self.update()

    def _on_pulse(self, value):
        self._pulse = float(value or 0.0)
        self.update()

    def mousePressEvent(self, event):
        # consume the press so it never reaches the row (which would select /
        # switch the workspace); a bare click opens the agent dropdown instead
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        color = QColor(self._STATE_COLOR[self._state]())
        fill = QColor(color)
        fill.setAlpha(38)
        p.setBrush(fill)
        border = QColor(color)
        border.setAlpha(150 + int(90 * self._pulse))  # 150..240, breathes
        pen = QPen(border)
        pen.setWidthF(1.4 + 0.8 * self._pulse)
        p.setPen(pen)
        p.drawRoundedRect(rect, 8, 8)
        f = QFont()
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self._count))
        p.end()


class WorkspaceSpinner(QWidget):
    """A sweeping-arc throbber with the live WORKING (busy) count at its centre,
    pinned to the right edge of a workspace row. Hidden whenever nothing is
    working (count 0) so the row's right edge stays clear for the hover
    folder/delete buttons; while working it spins in the amber 'working' colour.
    Colour is read from the live `Palette` at paint time, so it tracks every
    skin, and the animation runs ONLY while count > 0 — a resting row is free."""

    _SPAN = 270 * 16  # arc sweep, in 1/16-degree units (QPainter.drawArc)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setVisible(False)
        self._count = 0
        self._angle = 0.0  # rotating arc start angle (degrees)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(1000)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._anim.valueChanged.connect(self._on_spin)

    def set_count(self, count: int) -> None:
        count = max(0, int(count))
        if count == self._count:
            return
        self._count = count
        if count > 0:
            self.setVisible(True)
            if self._anim.state() != QAbstractAnimation.State.Running:
                self._anim.start()
        else:
            self._anim.stop()
            self.setVisible(False)
        self.update()

    def _on_spin(self, value):
        self._angle = float(value or 0.0)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(Palette.YELLOW)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        # faint full-ring track so the sweep reads as motion against it
        track = QColor(color)
        track.setAlpha(48)
        pen = QPen(track)
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        # the sweeping arc, rotating clockwise from -_angle
        arc = QPen(color)
        arc.setWidthF(2.4)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc)
        p.drawArc(rect, int(-self._angle * 16), -self._SPAN)
        # working count, bold, centred
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.setPen(color)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self._count))
        p.end()


class LogoRoundel(QWidget):
    """The gilt logo roundel: gold-leaf boss, ultramarine inner disc, white
    vine tendrils, gold 'A'. Falls back to a plain gold hexagon glyph off the
    illuminated theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        orn = _ornament()
        if orn == "manuscript":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_gilt(rect))
            p.drawEllipse(rect)                         # gilt boss
            disc = rect.adjusted(4, 4, -4, -4)
            dg = QRadialGradient(disc.left() + disc.width() * 0.34,
                                 disc.top() + disc.height() * 0.28,
                                 disc.width() * 0.8)
            dg.setColorAt(0.0, QColor("#2a56b0"))
            dg.setColorAt(1.0, QColor("#163471"))
            p.setBrush(dg)
            p.drawEllipse(disc)                          # ultramarine disc
            p.drawPixmap(disc.toRect(), _pixmap(_ROUNDEL_VINE, 48, 48))
            f = QFont("Cinzel Decorative")
            if not f.exactMatch():
                f = QFont("Cinzel")
            f.setPixelSize(16)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#f6de86"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "A")
        elif orn == "mechanicus":
            # radiant green cog with a bright pulsing-looking core
            p.drawPixmap(rect.toRect(), _pixmap(_COG, 48, 48))
            core = rect.center()
            r = rect.width() * 0.16
            cg = QRadialGradient(core, r)
            cg.setColorAt(0.0, QColor("#d6ffe9"))
            cg.setColorAt(0.55, QColor("#4fffc0"))
            cg.setColorAt(1.0, QColor(79, 255, 192, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cg)
            p.drawEllipse(core, r, r)
        else:
            f = QFont("Segoe UI Symbol")
            f.setPixelSize(17)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(Palette.ACCENT_GOLD))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "⬡")
        p.end()


class PageBorder(QWidget):
    """A mouse-transparent overlay that frames the whole page with the active
    theme's ornament: the manuscript's foliate vine strips + gilt corner
    medallions, or the Mechanicus brushed-steel casing + corner bolts + aquila
    crest. Paints nothing for skins with no ornament."""

    INSET = 6
    THICK = 32
    MED = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._h_tile = None
        self._v_tile = None
        self._medallion = None
        self._aquila = None

    def _tiles(self):
        if self._h_tile is None:      # built lazily, reused across repaints
            self._h_tile = _pixmap(_BORDER_H, 220, 32)
            self._v_tile = _pixmap(_BORDER_V, 32, 220)
            self._medallion = _pixmap(_MEDALLION, self.MED, self.MED)
        return self._h_tile, self._v_tile, self._medallion

    def paintEvent(self, event):
        orn = _ornament()
        if orn == "manuscript":
            self._paint_manuscript()
        elif orn == "mechanicus":
            self._paint_mechanicus()

    def _paint_manuscript(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, ins, t = self.width(), self.height(), self.INSET, self.THICK
        h_tile, v_tile, med = self._tiles()
        strips = [
            (QRectF(ins, ins, w - 2 * ins, t), "h", False),               # top
            (QRectF(ins, h - ins - t, w - 2 * ins, t), "h", False),       # bottom
            (QRectF(ins, ins, t, h - 2 * ins), "v", False),               # left
            (QRectF(w - ins - t, ins, t, h - 2 * ins), "v", True),        # right
        ]
        for rect, orient, flip in strips:
            grad = QLinearGradient(rect.topLeft(),
                                   rect.bottomLeft() if orient == "h"
                                   else rect.topRight())
            grad.setColorAt(0.0, QColor("#22539c" if not flip else "#12305f"))
            grad.setColorAt(1.0, QColor("#12305f" if not flip else "#22539c"))
            p.fillRect(rect, grad)
            p.save()
            p.setClipRect(rect)
            p.drawTiledPixmap(rect, h_tile if orient == "h" else v_tile)
            p.restore()
            pen = QPen(QColor(232, 196, 96, 140))   # gilt keyline
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
        # gilt corner medallions over the strip joints
        m, off = self.MED, 1
        for cx, cy in [(off, off), (w - m - off, off),
                       (off, h - m - off), (w - m - off, h - m - off)]:
            boss = QRectF(cx, cy, m, m)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_gilt(boss))
            p.drawEllipse(boss)
            p.drawPixmap(boss.toRect(), med)
        p.end()

    def _paint_mechanicus(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, ins, t = self.width(), self.height(), self.INSET, self.THICK
        strips = [
            QRectF(ins, ins, w - 2 * ins, t),               # top
            QRectF(ins, h - ins - t, w - 2 * ins, t),       # bottom
            QRectF(ins, ins, t, h - 2 * ins),               # left
            QRectF(w - ins - t, ins, t, h - 2 * ins),       # right
        ]
        for rect in strips:                                  # brushed gunmetal
            p.fillRect(rect, _gunmetal(rect))
            p.setPen(QPen(QColor(255, 255, 255, 40)))        # top bevel light
            p.drawLine(rect.topLeft(), rect.topRight())
        # green CRT bezel keyline just inside the casing
        bezel = QRectF(ins + t, ins + t, w - 2 * (ins + t), h - 2 * (ins + t))
        pen = QPen(QColor("#2f9a76"))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(bezel, 8, 8)
        # four gunmetal corner bolts
        b = 16
        for bx, by in [(ins + 6, ins + 6), (w - ins - 6 - b, ins + 6),
                       (ins + 6, h - ins - 6 - b), (w - ins - 6 - b, h - ins - 6 - b)]:
            bolt = QRectF(bx, by, b, b)
            bg = QRadialGradient(bolt.left() + b * 0.34, bolt.top() + b * 0.30, b)
            bg.setColorAt(0.0, QColor("#6a747c"))
            bg.setColorAt(1.0, QColor("#1b2024"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawEllipse(bolt)
        # aquila crest plaque straddling the top edge, centered
        pw, ph = 190, 52
        px = (w - pw) / 2
        plaque = QRectF(px, ins - 4, pw, ph)
        pg = QLinearGradient(plaque.topLeft(), plaque.bottomLeft())
        pg.setColorAt(0.0, QColor("#4a555f"))
        pg.setColorAt(1.0, QColor("#232a30"))
        p.setBrush(pg)
        p.setPen(QPen(QColor("#5a656f")))
        p.drawRoundedRect(plaque, 6, 6)
        aq = _pixmap(_AQUILA, 150, 48)
        p.drawPixmap(int(px + (pw - 150) / 2), int(plaque.top() + 3), aq)
        p.end()


class OrnamentDivider(QWidget):
    """A thin gilt rule with a centered fleuron — a manuscript section break."""

    def __init__(self, glyph: str = "❧", parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self.setFixedHeight(16)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        w = self.width()
        gap = 16
        pen = QPen(QColor(Palette.ACCENT_GOLD_DIM))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawLine(10, int(mid), int(w / 2 - gap), int(mid))
        p.drawLine(int(w / 2 + gap), int(mid), w - 10, int(mid))
        f = QFont("Segoe UI Symbol")
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(Palette.ACCENT_GOLD))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._glyph)
        p.end()
