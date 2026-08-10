"""Top-bar visual readout for Gemini (Antigravity CLI `agy`) rate-limit utilization.

Painted widget with fixed width (no jittering) and live loading state.
"""

from __future__ import annotations

import time
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics, QPen
from PySide6.QtWidgets import QWidget
from app.ui_theme import Palette
from app import gemini_usage


class GeminiUsageBadge(QWidget):
    """The top-bar readout of Gemini rate-limit utilization:
        (o) Gemini 18% used, resets in 2h45m at 17:30
    """

    refreshRequested = Signal()

    _RING = 15
    _PAD = 8
    _GAP = 7
    _FIXED_WIDTH = 315  # Fixed width to fit max possible string without jitter or truncation
    _AMBER, _RED = 60.0, 85.0

    def __init__(self, parent=None, window: str = "five_hour"):
        super().__init__(parent)
        self.window = window
        self.setFixedHeight(24)
        self.setFixedWidth(self._FIXED_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._usage: gemini_usage.GeminiUsage | None = None
        self._limit: gemini_usage.GeminiLimit | None = None
        self._fetching = True
        self._text = "fetching most recent usage data..."
        self._stale = False
        self._label = True
        self._unreadable = ""
        self.setVisible(True)
        self._refresh_text()

    def set_usage(self, usage: gemini_usage.GeminiUsage | None) -> None:
        self._fetching = False
        self._usage = usage
        if self.window == "weekly":
            self._limit = gemini_usage.weekly(usage)
        else:
            self._limit = gemini_usage.headline(usage)
        self._unreadable = ""
        if self._limit is None:
            self._text = "no usage data"
            self.setVisible(False)
            return
        else:
            self._label = True
            self._stale = bool(usage.error) if usage else False
        self.setVisible(True)
        self._refresh_text()

    def mark_fetching(self) -> None:
        self._fetching = True
        self.update()

    def has_reading(self) -> bool:
        return self._limit is not None

    def has_content(self) -> bool:
        return self._limit is not None or bool(self._unreadable)

    def mark_unreadable(self, error: str = "") -> None:
        self._fetching = False
        self._unreadable = str(error) or "unavailable"
        self._stale = True
        self.setVisible(True)
        self._refresh_text()

    def tick(self) -> None:
        self._refresh_text()

    def _refresh_text(self) -> None:
        if self._fetching:
            self._text = "fetching most recent usage data..."
        elif self._limit is not None:
            self._text = gemini_usage.format_limit(self._limit, with_label=self._label)
        elif self._unreadable:
            self._text = "Gemini limit unreadable, click to refresh"
        else:
            self._text = "no usage data"
        self.setToolTip(self._build_tooltip())
        self.update()

    @staticmethod
    def _text_font() -> QFont:
        f = QFont()
        f.setPixelSize(11)
        return f

    def _build_tooltip(self) -> str:
        if self._fetching:
            return "Fetching most recent Gemini rate-limit usage data..."
        if self._limit is None and self._unreadable:
            return ("Gemini rate-limit usage could not be read.\n"
                    f"Last attempt failed: {self._unreadable}\n"
                    "Click to refresh.")
        if self._usage is None or self._limit is None:
            return "Gemini rate-limit usage"
        lines = [f"Gemini ({self._usage.plan})"]
        for lim in self._usage.limits:
            lines.append(f"{lim.label}: " + gemini_usage.format_limit(lim))
        if self._usage.fetched_at:
            age = gemini_usage.format_countdown(time.time() - self._usage.fetched_at)
            lines.append(f"Updated {age} ago")
        if self._usage.error:
            lines.append(f"Last refresh failed: {self._usage.error}")
        lines.append("Click to refresh")
        return "\n".join(lines)

    def _color(self) -> QColor:
        if self._fetching or self._stale or self._limit is None:
            return QColor(Palette.TEXT_DIM)
        pct = self._limit.percent
        if pct >= self._RED:
            return QColor(Palette.RED)
        if pct >= self._AMBER:
            return QColor(Palette.YELLOW)
        return QColor(Palette.GREEN)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._fetching = True
            self._refresh_text()
            self.refreshRequested.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        col = self._color()

        # Background pill with fixed width
        bg_col = QColor(col)
        bg_col.setAlpha(20 if not self._fetching and not self._stale else 10)
        p.setBrush(bg_col)
        border_col = QColor(col)
        border_col.setAlpha(50 if not self._fetching and not self._stale else 25)
        p.setPen(QPen(border_col, 1))
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.drawRoundedRect(rect, 12, 12)

        # Ring meter
        ring_x = self._PAD
        ring_y = (self.height() - self._RING) / 2.0
        ring_rect = QRectF(ring_x, ring_y, self._RING, self._RING)

        # Background track
        track_col = QColor(Palette.BORDER)
        p.setPen(QPen(track_col, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring_rect)

        # Arc for percent utilization
        if not self._fetching and self._limit is not None and self._limit.percent > 0:
            pct = min(100.0, max(0.0, self._limit.percent))
            span = int(-pct / 100.0 * 360 * 16)
            p.setPen(QPen(col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring_rect, 90 * 16, span)

        # Text
        p.setFont(self._text_font())
        txt_col = QColor(Palette.TEXT) if not self._fetching and not self._stale else QColor(Palette.TEXT_DIM)
        p.setPen(txt_col)
        txt_x = ring_x + self._RING + self._GAP
        txt_y = (self.height() + p.fontMetrics().ascent() - p.fontMetrics().descent()) / 2.0 - 1
        
        # Elide text if it ever exceeds pill bounds
        avail_w = int(self.width() - txt_x - self._PAD)
        elided = p.fontMetrics().elidedText(self._text, Qt.TextElideMode.ElideRight, avail_w)
        p.drawText(txt_x, int(txt_y), elided)
        p.end()
