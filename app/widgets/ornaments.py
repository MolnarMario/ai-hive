"""Manuscript ornaments, painted with QPainter + QSvgRenderer (no raster
assets). The elaborate illuminated pieces (gilt drop-cap, logo roundel, the
foliate page border with gilt corner medallions) render only when the active
theme sets `illuminated`; otherwise the simple gold-on-dark versions are used,
so the other skins stay clean and light. SVG geometry/colours are lifted from
the design handoff's inline data-URIs.
"""

import os
from functools import lru_cache

from PySide6.QtCore import (QAbstractAnimation, QByteArray, QEasingCurve,
                            QRectF, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QComboBox, QLabel, QSizePolicy,
                               QToolButton, QWidget)

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


_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "assets", "icons", "app_logo.png")


@lru_cache(maxsize=None)
def _logo_pixmap(size: int) -> QPixmap:
    """The bundled hexagon/hive artwork (same source as the window icon,
    see generate_app_icon.py), rasterised once per requested size."""
    src = QPixmap(_LOGO_PATH)
    if src.isNull():
        return src
    return src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


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


# The two states the Windows taskbar overlay can be in. These are FIXED
# constants rather than `Palette` reads, unlike every other badge in this file,
# and deliberately so: the overlay is painted onto the OS taskbar, over whatever
# accent colour the user has chosen there, not onto our own chrome. Following
# the active skin would buy no visual coherence (nothing of ours is next to it)
# while risking a disc that vanishes into the taskbar on a light theme.
# Amber is the app's "working" hue so the two surfaces still read as related;
# blue is a hue nothing else in AI Hive uses, because at 16 pixels colour is the
# only channel that reliably carries a second meaning.
TASKBAR_WORKING = "#d9b24a"   # agents working, none of them asking
TASKBAR_ASKING = "#3b82f6"    # at least one agent is waiting on the user
# no agent is busy or asking, but one or more are idle with a background
# command still running (see TerminalAgent.poll_bg_shell) -- the case that
# used to leave the taskbar silent even though real work was still in
# flight. Violet, not a warm amber/orange shade: WORKING is already warm and
# ASKING is cool blue, and an orange tried here first read as too close to
# WORKING at 16px. Violet sits apart from both on the wheel (roughly equal
# hue distance from amber and blue) and isn't otherwise a "meaning" color in
# this app (no green-for-fine or red-for-error overtone), so it reads as its
# own distinct third signal rather than a shade of either existing one.
TASKBAR_BG_SHELL = "#a855f7"


def taskbar_badge_bgra(text: str, fill: str, size: int):
    """Paint the taskbar overlay disc and return `(w, h, premultiplied BGRA)`.

    Returns exactly what `taskbar_overlay.set_overlay` wants, so the Qt half of
    this feature stops here and the ctypes half never imports Qt.

    Everything about the drawing is in service of legibility at 16 pixels: a
    filled disc rather than an outline (an outline's interior shows the taskbar
    through it), a dark rim so the disc still has an edge when the user's
    taskbar happens to be the same hue, contrast-picked text, and a glyph
    scaled by how many characters it has, since "9+" needs materially more room
    than "3".
    """
    from .terminal_view import contrast_ratio

    size = max(8, int(size))
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    body = QColor(fill)
    rim = QColor(body.darker(190))
    rim.setAlpha(215)
    inset = max(0.5, size * 0.045)
    disc = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    pen = QPen(rim)
    pen.setWidthF(max(1.0, size * 0.07))
    p.setPen(pen)
    p.setBrush(body)
    p.drawEllipse(disc)

    text = str(text or "")
    if text:
        ink = QColor("#101010")
        if contrast_ratio(ink, body) < contrast_ratio(QColor("#ffffff"), body):
            ink = QColor("#ffffff")
        # Segoe UI, not the skin's body font, for the same reason the colours
        # are fixed: this glyph is drawn into Windows' furniture, not ours.
        f = QFont("Segoe UI")
        f.setPixelSize(max(6, int(size * (0.70 if len(text) == 1 else 0.52))))
        f.setBold(True)
        p.setFont(f)
        p.setPen(ink)
        # Centre on the glyph's own INK, not the font's line box: at this size
        # the box's ascent/descent padding visibly drops a digit off-centre.
        # tightBoundingRect is relative to the baseline origin, so the ink runs
        # from baseline+top to baseline+top+height.
        box = QFontMetrics(f).tightBoundingRect(text)
        p.drawText(round(disc.center().x() - box.left() - box.width() / 2.0),
                   round(disc.center().y() - box.top() - box.height() / 2.0),
                   text)
    p.end()

    stride = img.bytesPerLine()
    raw = bytes(img.constBits())
    want = size * 4
    if stride != want:   # 32bpp is already 4-byte aligned, but never assume it
        raw = b"".join(raw[y * stride:y * stride + want] for y in range(size))
    return (size, size, raw)


class BootVeil(QWidget):
    """A quiet cover over a terminal while its child TUI is booting.

    A launching agent is not a blank terminal for the second or two before its
    prompt is live: it paints a half-drawn frame, and at app launch it paints
    that frame at the pre-layout width (the tiling grid sizes the card AFTER
    the child is started, and pyte cannot reflow), so what the user actually
    saw on every reopen was a mangled narrow fragment in the top-left corner
    until the conversation finished replaying. The veil covers exactly that
    window, from the (re)start to `prompt_ready`, and then dissolves.

    It is deliberately the app's existing throbber motif (the sidebar's
    sweeping arc) rather than a new one, colours read from the live `Palette`
    at paint time so it follows every skin. It never takes focus and is
    transparent to the mouse, so the terminal underneath keeps every
    keystroke, and the animation runs ONLY while the veil is up.
    """

    _SPAN = 270 * 16   # arc sweep, in 1/16-degree units (QPainter.drawArc)
    _RING = 30         # ring diameter, px
    _FADE_MS = 260

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVisible(False)
        self._caption = ""
        self._angle = 0.0
        self._fade = 1.0
        # whether the veil is UP, which is not the same question as
        # `isVisible()`: a card in a hidden workspace is not on screen, yet its
        # launching agent is still booting and must be covered when the user
        # switches to it
        self._up = False
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1100)
        self._spin.setLoopCount(-1)
        self._spin.setEasingCurve(QEasingCurve.Type.Linear)
        self._spin.valueChanged.connect(self._on_spin)
        # revealing the conversation is a fade, never a cut: the terminal is
        # already correct underneath by the time this runs
        self._fader = QVariantAnimation(self)
        self._fader.setStartValue(1.0)
        self._fader.setEndValue(0.0)
        self._fader.setDuration(self._FADE_MS)
        self._fader.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fader.valueChanged.connect(self._on_fade)
        self._fader.finished.connect(self.dismiss)

    # ------------------------------------------------------------- control ---

    def begin(self, caption: str = "") -> None:
        """Cover the terminal now. Instant, never a fade in: the whole point is
        that the child's first frame is never seen."""
        self._caption = caption
        self._fader.stop()
        self._fade = 1.0
        self._up = True
        # cover the parent NOW rather than trusting a resize to have arrived:
        # a veil that comes up an inch too small leaves the child's fragment
        # showing round its edges, which is the whole thing it exists to hide
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.setVisible(True)
        self.raise_()
        if self._spin.state() != QAbstractAnimation.State.Running:
            self._spin.start()
        self.update()

    def finish(self) -> None:
        """The conversation is ready: dissolve."""
        if not self._up or self._fader.state() == \
                QAbstractAnimation.State.Running:
            return
        self._fader.start()

    def dismiss(self) -> None:
        """Drop the veil at once (the agent stopped, the user typed, or the
        fade finished). Stopping the spin here is what keeps a card that is
        merely sitting there from animating forever."""
        self._fader.stop()
        self._spin.stop()
        self._fade = 1.0
        self._up = False
        self.setVisible(False)

    def is_active(self) -> bool:
        """Is the veil up? (See `_up`: not the same as being on screen.)"""
        return self._up

    # ------------------------------------------------------------- painting ---

    def _on_spin(self, value):
        self._angle = float(value or 0.0)
        self.update()

    def _on_fade(self, value):
        self._fade = float(value if value is not None else 1.0)
        self.update()

    def paintEvent(self, event):
        from .terminal_view import legible_color  # avoids an import cycle
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, self._fade)))
        bg = QColor(Palette.BG_CONSOLE)
        p.fillRect(self.rect(), bg)
        color = QColor(Palette.ACCENT_GOLD)
        cx = self.width() / 2.0
        cy = self.height() / 2.0 - (9 if self._caption else 0)
        rect = QRectF(cx - self._RING / 2.0, cy - self._RING / 2.0,
                      self._RING, self._RING)
        track = QColor(color)
        track.setAlpha(46)          # faint full ring, so the sweep reads as motion
        pen = QPen(track)
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        arc = QPen(color)
        arc.setWidthF(2.4)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc)
        p.drawArc(rect, int(-self._angle * 16), -self._SPAN)
        if self._caption:
            f = QFont()
            f.setPixelSize(12)
            f.setItalic(True)
            p.setFont(f)
            p.setPen(legible_color(QColor(Palette.TEXT_DIM), bg))
            p.drawText(QRectF(0, cy + self._RING / 2.0 + 10,
                              self.width(), 20),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       self._caption)
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


class UsagePillBadge(QWidget):
    """The shared body of every top-bar usage readout (Claude plan, Gemini
    5-hour, Gemini weekly): a percent ring, one line of text, and an X that
    appears on hover to close the pill.

    Painted rather than styled, for the same reason as `AgentCountBadge`: the
    colour has to switch on utilization (green -> amber -> red) AND track the
    active skin, and per-state QSS would fight the theme registry. Reading
    `Palette` at paint time gives both for free.

    THE WIDTH IS THE TEXT'S WIDTH, measured with the font `paintEvent` actually
    draws with (never the widget's QSS font, or the pill is sized for text of a
    different size). One formula, in `_measure_width`, for every pill: the
    Gemini readout used to carry a hardcoded `_FIXED_WIDTH = 315` and elide into
    it, which reserved 630px of the bar for two pills whose real content is
    ~215px each, and which truncated anything longer. The two pills sit side by
    side, so a second hand-copy of the formula is the same bug waiting to
    happen; subclasses supply only the TEXT.

    The X costs NO layout width: it FLOATS over the tail of the text, which
    fades out under it for the moment the pointer is inside the pill. Reserving
    a permanent slot for it (the first cut of this) left ~20px of every pill
    blank for the 99% of the time nobody is hovering. What must NOT change is
    the width: the pills sit after the layout's stretch, so a pill that grew on
    hover would shove the whole right-hand cluster (recovery caption, LED
    toggles, theme combo, font steppers, Add Terminal) sideways as the pointer
    crossed it. Overlaying keeps that property for free - `_measure_width`
    depends on the text alone, and hovering paints, it never re-measures.

    The widget is a pure VIEW - it never fetches, and it never decides its own
    visibility. `MainWindow` polls off-thread and pushes readings in; a click on
    the body emits `refreshRequested`, a click on the X emits `closeRequested`,
    and `TopBar._sync_usage_pills` is the ONE place `setVisible` is called (see
    the two-axis rule there).
    """

    refreshRequested = Signal()
    closeRequested = Signal()

    _RING = 15          # ring diameter
    _PAD = 8            # horizontal padding inside the pill
    _GAP = 7            # ring -> text gap
    _CLOSE_W = 14       # the hover X, OVERLAID (see the class docstring)
    _FADE_W = 14        # how far the text fades out ahead of the hovered X
    _RADIUS = 6         # pill corner radius
    _BORDER_W = 1.2
    _FILL_ALPHA, _BORDER_ALPHA = 30, 140          # a live reading
    _FILL_ALPHA_DIM, _BORDER_ALPHA_DIM = 18, 90   # loading or stale

    # utilization thresholds. Deliberately generous: amber is a nudge, red is
    # "wrap up", because being cut off mid-task is the thing we're avoiding.
    _AMBER, _RED = 60.0, 85.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._usage = None          # the provider's Usage reading | None
        self._limit = None          # the headline Limit
        self._text = ""
        self._sized_for = None      # the text the current width was measured for
        self._stale = False         # showing an older reading than we'd like
        self._label = False         # prefix the window name (multi-limit plans)
        self._unreadable = ""       # last error, when we have NO reading at all
        self._loading = False       # a fetch is in flight and we have nothing yet
        self._hovering = False      # paint the X's scrim over the text tail
        # A child QToolButton rather than a rect hit-tested in mousePressEvent:
        # it consumes its own press, so closing can never be mistaken for the
        # click-to-refresh affordance, and it gets the hover cursor, hover
        # colour and QSS treatment of the other close buttons for free.
        self.close_btn = QToolButton(self)
        self.close_btn.setObjectName("UsagePillClose")
        self.close_btn.setText("✕")
        self.close_btn.setFixedSize(self._CLOSE_W, self._CLOSE_W)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setToolTip(
            "Hide this usage readout. The + button beside the auto-restart "
            "label brings it back.")
        self.close_btn.hide()
        self.close_btn.clicked.connect(self.closeRequested)
        self._refresh_text()

    # -- state -----------------------------------------------------------
    def has_reading(self) -> bool:
        """True once a usable reading has arrived."""
        return self._limit is not None

    def has_content(self) -> bool:
        """True when there is anything worth showing - a reading, the can't-read
        pill, OR the loading state. This is one of the TWO factors that decide
        visibility (the other is the user's per-pill preference); the error
        state is content too, and hiding it is what made the readout look
        deleted."""
        return self._limit is not None or bool(self._unreadable) or self._loading

    def mark_loading(self) -> None:
        """A fetch is in flight: say so in place of a number. This is what fills
        the bar at startup, where there is deliberately no cached figure to
        paint (a stored reading goes stale exactly where it matters most)."""
        self._loading = True
        self._unreadable = ""
        self._stale = False
        self._refresh_text()

    def mark_unreadable(self, error: str = "") -> None:
        """No reading at all and the last poll failed: say so, in place, rather
        than leave a gap in the bar."""
        self._loading = False
        self._unreadable = str(error) or "unavailable"
        self._stale = True
        self._refresh_text()

    def mark_stale(self, stale: bool = True) -> None:
        """A poll failed but we still have a previous reading: keep showing it,
        greyed, rather than blanking a number the user is watching."""
        if stale != self._stale:
            self._stale = bool(stale)
            self.update()

    def mark_absent(self) -> None:
        """This readout has nothing to show and never will this run (no login).
        Clears every kind of content, so no later re-sync can resurrect it."""
        self._loading = False
        self._unreadable = ""
        self._limit = None
        self._usage = None
        self._refresh_text()

    def tick(self) -> None:
        """Re-render the countdown from the clock alone (no network). Repaints
        only when the visible string actually changes, so the tick costs nothing
        while the minute digit is unchanged."""
        self._refresh_text()

    # -- rendering -------------------------------------------------------
    def _refresh_text(self) -> None:
        if self._loading:
            text = self._loading_text()
        elif self._limit is not None:
            text = self._format_limit()
        elif self._unreadable:
            text = self._unreadable_text()
        else:
            text = ""
        self._set_text(text)

    def _set_text(self, text: str) -> None:
        tip = self._build_tooltip()
        if text == self._text and self._sized_for == text:
            self.setToolTip(tip)   # age keeps moving even when the line doesn't
            return
        self._text = text
        self._sized_for = text
        self.setToolTip(tip)
        self.setFixedWidth(self._measure_width(text))
        self.update()

    def _measure_width(self, text: str) -> int:
        """[pad][ring][gap][text][pad]. The one width formula, for every pill.

        The X is deliberately NOT a term here: it floats over the text's tail,
        so it costs no width and hovering can never resize the pill.

        `QFontMetrics` MUST be bound to this widget (`self` as the paint
        device), never a bare `QFontMetrics(font)`: unbound, Qt resolves the
        font against the PRIMARY screen's DPI, while `paintEvent` draws with
        `p.fontMetrics()`, bound to whatever screen this widget is actually
        on. On a single-monitor 100%-scale machine the two agree and nothing
        looks wrong; on a mixed-DPI multi-monitor setup they diverge, so the
        width reserved here undershoots what painting needs and `paintEvent`'s
        `elidedText` safety net - meant only as insurance against a subclass
        handing us an unmeasured string - fires for real and truncates a pill
        that was sized "correctly". Same reason `ElidingLabel` measures with
        `self.font()` rather than a fresh `QFont`.
        """
        fm = QFontMetrics(self._text_font(), self)
        return (self._PAD * 2 + self._RING + self._GAP
                + fm.horizontalAdvance(text))

    @staticmethod
    def _text_font() -> QFont:
        f = QFont()
        f.setPixelSize(11)
        return f

    # -- subclass hooks --------------------------------------------------
    def _loading_text(self) -> str:
        return "usage, reading..."

    def _format_limit(self) -> str:
        raise NotImplementedError

    def _unreadable_text(self) -> str:
        return "usage limit unreadable, click to refresh"

    def _build_tooltip(self) -> str:
        return ""

    # -- interaction -----------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.close_btn.move(int(self.width() - self._PAD - self._CLOSE_W),
                            int((self.height() - self._CLOSE_W) / 2))

    def enterEvent(self, event):
        self._hovering = True
        self.close_btn.setVisible(True)
        self.update()          # repaint so the text fades under the X
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self.close_btn.setVisible(False)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # show the fetch immediately: a click with no visible response reads
            # as a dead pill, and the Gemini fetch takes ~3s
            self.mark_loading()
            self.refreshRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _color(self) -> QColor:
        if self._loading or self._stale or self._limit is None:
            return QColor(Palette.TEXT_DIM)
        pct = self._limit.percent
        if pct >= self._RED:
            return QColor(Palette.RED)
        if pct >= self._AMBER:
            return QColor(Palette.YELLOW)
        return QColor(Palette.GREEN)

    def paintEvent(self, event):
        if self._limit is None and not self._unreadable and not self._loading:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self._color()
        dim = self._loading or self._stale
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        fill = QColor(color)
        fill.setAlpha(self._FILL_ALPHA_DIM if dim else self._FILL_ALPHA)
        p.setBrush(fill)
        border = QColor(color)
        border.setAlpha(self._BORDER_ALPHA_DIM if dim else self._BORDER_ALPHA)
        pen = QPen(border)
        pen.setWidthF(self._BORDER_W)
        p.setPen(pen)
        p.drawRoundedRect(rect, self._RADIUS, self._RADIUS)
        # percent ring: faint full track + a filled sweep from 12 o'clock
        top = (self.height() - self._RING) / 2.0
        ring = QRectF(self._PAD, top, self._RING, self._RING)
        track = QColor(color)
        track.setAlpha(55)
        tp = QPen(track)
        tp.setWidthF(2.2)
        p.setPen(tp)
        p.drawArc(ring, 0, 360 * 16)
        span = 0
        if self._loading:
            # a bare track already reads as "waiting"; drawing a sweep here
            # would be a number, and there is deliberately no number yet
            pass
        elif self._limit is None:
            # can't-read state: an empty track with a "!" where the sweep goes,
            # so the pill reads as a warning at a glance and not as 0% used.
            bang = self._text_font()
            bang.setBold(True)
            p.setFont(bang)
            p.setPen(color)
            p.drawText(ring, int(Qt.AlignmentFlag.AlignCenter), "!")
        else:
            span = int(max(0.0, min(100.0, self._limit.percent))
                       / 100.0 * 360 * 16)
        if span:
            ap = QPen(color)
            ap.setWidthF(2.2)
            ap.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(ap)
            p.drawArc(ring, 90 * 16, -span)   # clockwise from 12 o'clock
        p.setFont(self._text_font())
        p.setPen(color)
        text_x = self._PAD + self._RING + self._GAP
        # the pill is sized for this exact string, so the elide is insurance
        # only (a subclass could yet hand us something longer than it measured)
        avail = self.width() - text_x - self._PAD
        elided = p.fontMetrics().elidedText(self._text,
                                            Qt.TextElideMode.ElideRight,
                                            int(max(0, avail)))
        p.drawText(QRectF(text_x, 0, max(0, avail), self.height()),
                   int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter),
                   elided)
        if self._hovering:
            self._paint_close_scrim(p, color, dim)
        p.end()

    def _paint_close_scrim(self, p, color, dim) -> None:
        """Fade the text out under the hovered X.

        The X owns no layout width, so without this it would sit on top of live
        glyphs and neither would be readable. The scrim is the pill's OWN
        background rebuilt opaque - the flat bar colour with the same tint the
        fill uses - so the covered tail reads as empty pill rather than as a
        patch of some other colour. It is clipped to the rounded outline, or it
        would square off the right-hand corners it paints over.
        """
        ground = QColor(Palette.BG_PANEL)
        a = (self._FILL_ALPHA_DIM if dim else self._FILL_ALPHA) / 255.0
        blend = QColor(
            int(round(ground.red() * (1 - a) + color.red() * a)),
            int(round(ground.green() * (1 - a) + color.green() * a)),
            int(round(ground.blue() * (1 - a) + color.blue() * a)))
        x1 = self.width() - self._PAD - self._CLOSE_W
        x0 = max(0.0, x1 - self._FADE_W)
        clear = QColor(blend)
        clear.setAlpha(0)
        grad = QLinearGradient(x0, 0.0, float(x1), 0.0)
        grad.setColorAt(0.0, clear)
        grad.setColorAt(1.0, blend)     # PadSpread keeps it solid past x1
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1,
                                      self.height() - 1),
                               self._RADIUS, self._RADIUS)
        p.save()
        p.setClipPath(outline)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRect(QRectF(x0, 0.0, self.width() - x0, self.height()))
        p.restore()


class PlanUsageBadge(UsagePillBadge):
    """The top-bar readout of one Claude account plan-usage window (5-hour
    session, or 7-day):

        (o) 5h 21% used, resets in 1h20m at 14:49
        (o) 7d 40% used, resets in 3d14h at 09:00

    A percent ring plus one line, in the order the user asked for — countdown
    first ("how long have I got"), wall-clock second, both in LOCAL time. The
    7-day pill's countdown is DAYS+HOURS only (no minutes,
    `claude_usage.format_countdown_dh`) — a week-long window doesn't need
    to-the-minute precision, and "167h23m" (what the ordinary countdown gives a
    multi-day duration) is unreadable at a glance.

    Painted rather than styled, for the same reason as `AgentCountBadge`: the
    colour has to switch on utilization (green -> amber -> red) AND track the
    active skin, and per-state QSS would fight the theme registry. Reading
    `Palette` at paint time gives both for free.

    The widget is a pure VIEW — it never fetches. `MainWindow` polls off-thread
    and pushes readings in via `set_usage`; a click emits `refreshRequested`,
    which is the whole refresh affordance (no extra button in the chrome).

    When a poll fails and there is NO earlier number to grey out, the pill says
    so (`mark_unreadable`) instead of vanishing. Disappearing silently reads as
    "the feature was removed" — it was reported as exactly that after a restart
    that hit an `http 429` with no seed on disk to fall back on. There is now
    NO on-disk seed at all, by design (see `start_usage_polling`): a stored
    number goes stale exactly where it matters most, so the pill opens in the
    LOADING state instead and only ever shows a figure fetched this run. The
    error pill keeps the click-to-refresh affordance, which is the one useful
    thing a user can do about it.
    """

    def __init__(self, parent=None, window: str = "five_hour"):
        super().__init__(parent)
        self.window = window
        # always name the window: two Claude pills (5h and 7d) sit side by
        # side on the bar now, and "which one is this" is the whole point of
        # the label, exactly like the two Gemini pills beside them
        self._label = True

    # -- data in ---------------------------------------------------------
    def set_usage(self, usage) -> None:
        """Adopt a reading. `None`, or a reading with no limits, leaves the pill
        with no content — an API-key user or a logged-out machine has nothing to
        show, and `TopBar._sync_usage_pills` hides it for that reason. Note the
        badge never calls `setVisible` itself: which pills are on the bar is the
        product of a reading AND the user's per-pill preference, and only the
        top bar knows both."""
        from .. import claude_usage

        self._usage = usage
        self._limit = (claude_usage.weekly(usage) if self.window == "weekly"
                       else claude_usage.five_hour(usage))
        self._unreadable = ""       # a real number supersedes the error pill
        self._loading = False
        self._stale = bool(usage.error) or usage.source == "cache" if usage else False
        self._refresh_text()

    def _loading_text(self) -> str:
        # deliberately SHORTER than the finished line, so the pill only ever
        # grows when the reading lands (a long loading string makes it snap
        # narrower, which reads as a glitch), and it names its own tracker so
        # four grey pills side by side are still tellable apart
        return ("Claude 7d usage, reading..." if self.window == "weekly"
                else "Claude 5h usage, reading...")

    def _format_limit(self) -> str:
        from .. import claude_usage

        return claude_usage.format_limit(self._limit, with_label=self._label,
                                         days_only=self.window == "weekly")

    def _unreadable_text(self) -> str:
        return ("Claude 7d usage unreadable, click to refresh"
                if self.window == "weekly" else
                "Claude 5h usage unreadable, click to refresh")

    def _build_tooltip(self) -> str:
        from .. import claude_usage
        import time as _time

        if self._loading:
            return "Reading the Claude plan usage..."
        if self._limit is None and self._unreadable:
            return ("Claude plan usage could not be read.\n"
                    f"Last attempt failed: {self._unreadable}\n"
                    "The endpoint rate-limits; retries back off automatically.\n"
                    "Click to try again now.")
        if self._usage is None or self._limit is None:
            return "Claude plan usage"
        lines = []
        if self._usage.plan:
            lines.append(f"Claude {self._usage.plan.capitalize()} plan")
        for lim in self._usage.limits:
            lines.append(f"{lim.label}: "
                         + claude_usage.format_limit(
                             lim, days_only=lim.key.startswith("seven_day")))
        if self._usage.fetched_at:
            age = claude_usage.format_since(_time.time() - self._usage.fetched_at)
            src = " (cached by Claude)" if self._usage.source == "cache" else ""
            lines.append(f"Updated {age}{src}")
        if self._usage.error:
            lines.append(f"Last refresh failed: {self._usage.error}")
        lines.append("Click to refresh")
        return "\n".join(lines)


class LogoRoundel(QWidget):
    """The gilt logo roundel: gold-leaf boss, ultramarine inner disc, white
    vine tendrils, gold 'A'. Falls back to the bundled hive-hexagon artwork
    (app/assets/icons/app_logo.png, same source as the window icon) off the
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
            side = round(rect.width())
            logo = _logo_pixmap(side)
            if logo.isNull():
                f = QFont("Segoe UI Symbol")
                f.setPixelSize(17)
                f.setBold(True)
                p.setFont(f)
                p.setPen(QColor(Palette.ACCENT_GOLD))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "⬡")
            else:
                x = rect.left() + (rect.width() - logo.width()) / 2
                y = rect.top() + (rect.height() - logo.height()) / 2
                p.drawPixmap(round(x), round(y), logo)
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


class ElidingLabel(QLabel):
    """A one-line label that always shows as much of its text as the space it
    was actually given allows, and keeps the whole text in its tooltip.

    Two things make it reliable where hand-rolled elision was not. It re-fits in
    its OWN resizeEvent, so it never has to guess whether the parent has laid
    out yet: setting the text before the first layout used to leave a card
    stranded on a short character-count fallback for the rest of its life, in a
    header with hundreds of free pixels. And it reports a minimal width hint
    (the policy below), so its own natural width can never push its neighbours
    around: with a layout stretch it simply receives every pixel the fixed
    chrome beside it did not take."""

    def __init__(self, parent=None, min_chars: int = 0):
        super().__init__(parent)
        self._full = ""
        self._min_chars = min_chars   # >0 keeps a floor under the shrink
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        """Set the text to display and elide it to the current width. The
        tooltip carries the untruncated string."""
        text = " ".join((text or "").split())   # a newline would grow the row
        self._full = text
        self.setToolTip(text)
        self._refit()
        if text:
            # width may still be settling (first layout, a retile in flight):
            # re-fit once the event loop has caught up, which is cheap and
            # makes the truncation match the final geometry
            QTimer.singleShot(0, self._refit)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        if self._min_chars:
            fm = QFontMetrics(self.font())
            hint.setWidth(min(hint.width(), fm.averageCharWidth() * self._min_chars))
        else:
            hint.setWidth(0)
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()

    def _refit(self) -> None:
        if not self._full:
            self.clear()
            return
        avail = self.contentsRect().width()
        if avail <= 8:
            self.setText("")   # no room at all; the tooltip still has the text
            return
        fm = QFontMetrics(self.font())
        self.setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight,
                                   avail))


class ToggleSwitch(QWidget):
    """A track-and-thumb switch for the Options panel: green track with the
    thumb slid right when armed, grey track with the thumb at the left when
    off — the ordinary mobile-settings convention. Replaces the older
    "whole-row button with an LED glyph" rows, which read as a clickable link
    rather than a switch (reported live). `isChecked`/`setChecked`/`click`
    mirror `QAbstractButton`'s so every existing caller (`.click()` in tests,
    `setChecked` in `_refresh_*`) needed no change beyond the type; `clicked`
    (no args) and `toggled(bool)` both fire on every click, matching
    `QAbstractButton.clicked` and `.toggled` respectively."""

    toggled = Signal(bool)
    clicked = Signal()

    _W, _H, _PAD = 34, 18, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = False
        self._pos = 0.0  # 0..1 thumb travel, animated toward `_checked`
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, on: bool) -> None:
        on = bool(on)
        if on == self._checked:
            return
        self._checked = on
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._pos = float(value or 0.0)
        self.update()

    def click(self) -> None:
        self.setChecked(not self._checked)
        self.clicked.emit()
        self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.click()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        off = QColor(Palette.BORDER)
        on = QColor(Palette.GREEN)
        t = self._pos
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        rect = QRectF(0, 0, self.width(), self.height())
        radius = rect.height() / 2
        p.drawRoundedRect(rect, radius, radius)
        d = self.height() - self._PAD * 2
        x = self._PAD + (self.width() - d - self._PAD * 2) * t
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, self._PAD, d, d))
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


def anchored_popup_pos(anchor, size):
    """Where to put a popup dropped under `anchor`, kept inside the app window
    AND the screen.

    Extracted from `GridButton._popup_position` so the top bar's Options panel
    reuses the same rules rather than growing a second, subtly different copy.
    Both callers share the shape of the problem: the button sits at the RIGHT
    end of a wide strip, so left-anchoring a panel under it spills past the
    window's right edge on a narrow window and off the display on a wide one.

    Overflow right -> right-align to the button (the panel grows leftward, back
    into the window); overflow bottom -> flip above it. The bound is the
    INTERSECTION of the window and the screen's available geometry, so neither
    a window pushed off-screen nor a taskbar can strand the panel.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    w, h = size.width(), size.height()
    bl = anchor.mapToGlobal(anchor.rect().bottomLeft())
    br = anchor.mapToGlobal(anchor.rect().bottomRight())
    tl = anchor.mapToGlobal(anchor.rect().topLeft())
    screen = anchor.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    win = anchor.window().geometry()  # client area in global coords
    left = max(avail.x(), win.x())
    top = max(avail.y(), win.y())
    right = min(avail.x() + avail.width(), win.x() + win.width())
    bottom = min(avail.y() + avail.height(), win.y() + win.height())
    x = bl.x()
    if x + w > right:
        x = br.x() - w                        # right-align under the button
    x = max(left, min(x, right - w))
    y = bl.y()
    if y + h > bottom:
        y = tl.y() - h                        # flip above the button
    y = max(top, min(y, bottom - h))
    return QPoint(x, y)


class DropDownComboBox(QComboBox):
    """A `QComboBox` whose popup always opens directly under it.

    Qt's native combo popup aligns the CURRENTLY SELECTED row with the box
    (so the list can land above, below, or straddling it depending on which
    item happens to be picked) - normal for a native OS combo, but every combo
    here is styled to read as an ordinary list-style dropdown, where that
    reads as the menu jumping around each time the selection changes (live-
    reported on the theme selector). `showPopup` lets Qt do its own layout and
    sizing, then repositions just the popup window's top-left corner under the
    box afterwards, so nothing about the list itself (size, scroll position)
    changes. Clamped to the SCREEN only, not `anchored_popup_pos`'s window
    bound: a combo living inside another `Qt.Popup` (the Options panel) has a
    tiny host window, and bounding the list to it would truncate a dropdown
    taller than that panel.
    """

    def showPopup(self) -> None:
        super().showPopup()
        popup = self.view().window()
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        bl = self.mapToGlobal(self.rect().bottomLeft())
        tl = self.mapToGlobal(self.rect().topLeft())
        w, h = popup.width(), popup.height()
        x = min(bl.x(), avail.x() + avail.width() - w)
        x = max(avail.x(), x)
        y = bl.y()
        if y + h > avail.y() + avail.height():
            y = tl.y() - h                    # flip above when short on room
        y = max(avail.y(), y)
        popup.move(x, y)
