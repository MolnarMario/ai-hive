"""One look for every tooltip in the app, and one switch to silence them.

Qt sizes a plain-text tooltip to its longest line, up to the width of the
screen. A two-sentence tip then comes out as a thin strip across the window.
Rich text is the only thing Qt wraps, so `wrap` measures the text and, when
it is wider than `MAX_WIDTH`, puts it in a fixed-width table.

Two paths reach `wrap`:

* `TooltipFilter`, an application-wide event filter. It takes over the
  ToolTip event of every widget with a `toolTip()`, every `QMenu` action
  (menus that set `setToolTipsVisible`) and every item view row
  (`ToolTipRole`), so a plain `setToolTip(...)` needs no extra code.
* `show_text`, for widgets that draw their own hit areas (sidebar rail,
  terminal scrollbar, file map) and call `QToolTip.showText` themselves.
  They call this instead.

The switch (`set_enabled(False)`) makes the filter swallow every ToolTip
event and makes `show_text` a no-op, so one flag covers both paths.
"""
from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication, QTextDocument
from PySide6.QtGui import Qt as _GuiQt   # mightBeRichText lives on the QtGui side
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QMenu,
                               QToolTip, QWidget)

# Logical pixels, so it scales with the display. 360 holds about 55
# characters per line at the 13px chrome font: the longest tips in the app
# come out at 5 or 6 lines instead of 2.
MAX_WIDTH = 360
# On a small screen the cap shrinks so a tip never covers more than this
# share of the width.
MAX_SCREEN_FRACTION = 0.28
_enabled = True


def enabled() -> bool:
    return _enabled


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)
    if not _enabled:
        QToolTip.hideText()


def max_width(widget: QWidget | None = None) -> int:
    """The widest a tooltip text may be, in logical pixels."""
    screen = None
    if widget is not None:
        screen = widget.screen()
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return MAX_WIDTH
    share = int(screen.availableGeometry().width() * MAX_SCREEN_FRACTION)
    return max(200, min(MAX_WIDTH, share))


def _natural_width(text: str, rich: bool, widget: QWidget | None) -> float:
    font = widget.font() if widget is not None else QToolTip.font()
    if rich:
        doc = QTextDocument()
        doc.setDefaultFont(font)
        doc.setHtml(text)
        return doc.idealWidth()
    metrics = QFontMetrics(font)
    return max((metrics.horizontalAdvance(line)
                for line in text.split("\n")), default=0)


def wrap(text: str, widget: QWidget | None = None) -> str:
    """`text` ready for `QToolTip.showText`: unchanged when it already fits,
    otherwise rich text held to `max_width`."""
    if not text:
        return text
    limit = max_width(widget)
    rich = _GuiQt.mightBeRichText(text)
    if _natural_width(text, rich, widget) <= limit:
        return text
    body = text if rich else html.escape(text).replace("\n", "<br>")
    return (f'<table width="{limit}" cellspacing="0" cellpadding="0">'
            f"<tr><td>{body}</td></tr></table>")


def show_text(pos: QPoint, text: str, widget: QWidget | None = None,
              rect: QRect | None = None, msec: int = -1) -> None:
    """`QToolTip.showText` with the width cap and the on/off switch."""
    if not _enabled or not text:
        QToolTip.hideText()
        return
    QToolTip.showText(pos, wrap(text, widget), widget,
                      rect if rect is not None else QRect(), msec)


class TooltipFilter(QObject):
    """Application-wide: routes every default tooltip through `show_text`."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.ToolTip or not isinstance(obj, QWidget):
            return False
        if not _enabled:
            QToolTip.hideText()
            event.accept()
            return True
        text, owner = self._text_for(obj, event)
        if not text:
            return False        # a widget with its own handler, or no tip
        show_text(event.globalPos(), text, owner,
                  QRect(), getattr(owner, "toolTipDuration", lambda: -1)())
        event.accept()
        return True

    @staticmethod
    def _text_for(obj: QWidget, event) -> tuple[str, QWidget]:
        if isinstance(obj, QMenu):
            if not obj.toolTipsVisible():
                return "", obj
            action = obj.actionAt(event.pos())
            return (action.toolTip() if action is not None else ""), obj
        view = obj.parentWidget()
        if isinstance(view, QAbstractItemView) and obj is view.viewport():
            index = view.indexAt(event.pos())
            if index.isValid():
                tip = index.data(Qt.ItemDataRole.ToolTipRole)
                return (tip if isinstance(tip, str) else ""), view
            return "", view
        return obj.toolTip(), obj


def install(app: QApplication | None = None) -> None:
    """Install the filter once per QApplication; later calls do nothing."""
    app = app or QApplication.instance()
    if app is None or app.property("aihiveTooltipFilter"):
        return
    flt = TooltipFilter(app)
    app.installEventFilter(flt)
    app.setProperty("aihiveTooltipFilter", True)
