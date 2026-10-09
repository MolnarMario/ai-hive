"""Tooltips: one width cap for every tip, and the Options switch that turns
them all off."""

import shutil
import tempfile
from pathlib import Path

from .harness import check

LONG_TIP = (
    "Lanes: ON. A new Claude agent in this workspace gets its own git "
    "worktree and branch (the New Agent dialog ticks Own lane). Agents that "
    "are already here stay where they are; each card offers Restart in own "
    "lane.\nClick to turn off. Existing lanes are never touched.")


def _tip_label():
    """The live QTipLabel, or None while no tooltip is showing."""
    from PySide6.QtWidgets import QApplication
    for w in QApplication.topLevelWidgets():
        if w.metaObject().className() == "QTipLabel" and w.isVisible():
            return w
    return None


def _gone() -> bool:
    """True once no tip shows. Qt hides a visible tip on a 300 ms timer, so
    give it a moment before judging."""
    import time

    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        QApplication.processEvents()
        if _tip_label() is None:
            return True
        time.sleep(0.02)
    return False


def _hover(widget):
    """Send the ToolTip event Qt sends after the mouse rests on `widget`."""
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication
    ev = QHelpEvent(QEvent.Type.ToolTip, QPoint(3, 3),
                    widget.mapToGlobal(QPoint(3, 3)))
    QApplication.sendEvent(widget, ev)
    QApplication.processEvents()


def test_tooltip_wrap_caps_the_width():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel

    from app import tooltips

    app = QApplication.instance() or QApplication([])
    limit = tooltips.max_width()
    check("tooltip: the cap is a fixed pixel width, never over 360",
          200 <= limit <= tooltips.MAX_WIDTH, limit)
    check("tooltip: a short tip is left alone",
          tooltips.wrap("Theme") == "Theme")
    check("tooltip: nothing in, nothing out", tooltips.wrap("") == "")
    wrapped = tooltips.wrap(LONG_TIP)
    check("tooltip: a long tip goes into a table of the capped width",
          f'width="{limit}"' in wrapped and "<br>" in wrapped, wrapped[:80])
    check("tooltip: wrapping escapes plain text",
          "x &lt; y &amp;" in tooltips.wrap("x < y & " + "word " * 80))

    tooltips.install(app)
    tooltips.set_enabled(True)
    host = QLabel("host")
    host.setToolTip(LONG_TIP)
    # an offscreen window is never the active one, and Qt drops tips for those
    host.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
    host.show()
    app.processEvents()      # let the window expose before hovering
    try:
        _hover(host)
        tip = _tip_label()
        check("tooltip: a default setToolTip tip appears", tip is not None)
        if tip is not None:
            check("tooltip: the shown tip is no wider than the cap + padding",
                  tip.width() <= limit + 40, (tip.width(), limit))
            check("tooltip: ...and wraps onto several lines",
                  tip.height() > 3 * tip.fontMetrics().height(), tip.height())
        tooltips.set_enabled(False)
        check("tooltip: switching off hides a tip that is showing",
              _gone())
        _hover(host)
        check("tooltip: OFF swallows the hover event", _gone())
        tooltips.show_text(host.mapToGlobal(host.rect().center()), "custom", host)
        QApplication.processEvents()
        check("tooltip: OFF also silences widgets that draw their own tips",
              _gone())
        tooltips.set_enabled(True)
        tooltips.show_text(host.mapToGlobal(host.rect().center()), "custom", host)
        QApplication.processEvents()
        check("tooltip: show_text works again once ON", _tip_label() is not None)
    finally:
        tooltips.set_enabled(True)
        from PySide6.QtWidgets import QToolTip
        QToolTip.hideText()
        host.deleteLater()


def test_tooltips_option_toggle():
    """The "Tooltips" switch in the Options panel defaults ON, flips the
    app-wide flag, and persists under ui."""
    from PySide6.QtWidgets import QApplication

    from app import tooltips
    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tiptoggle-"))
    try:
        bar = TopBar()
        check("tooltips toggle: defaults to ON",
              bar._tooltips and bar.tooltips_btn.isChecked())
        emitted = []
        bar.tooltipsToggled.connect(emitted.append)
        bar.tooltips_btn.click()
        check("tooltips toggle: a click turns it off and emits False",
              emitted == [False] and not bar._tooltips, emitted)
        bar.set_tooltips(True)
        check("tooltips toggle: set_tooltips does not re-emit",
              bar._tooltips and emitted == [False])
        check("tooltips toggle: the switch has a row in the Options panel",
              bar.options_panel.isAncestorOf(bar.tooltips_btn))
        bar.deleteLater()

        win = create_main_window(SessionStore(path=tmp / "session.json"))
        check("tooltips toggle: a fresh window has tooltips on",
              win._tooltips and tooltips.enabled())
        win._save_timer.stop()
        win._on_tooltips_toggled(False)
        check("tooltips toggle: OFF flips the app-wide flag",
              not tooltips.enabled())
        check("tooltips toggle: flipping it marks the session for saving",
              win._save_timer.isActive())
        check("tooltips toggle: the preference is persisted under ui",
              win._session_payload()["ui"]["tooltips"] is False)
        win._on_tooltips_toggled(True)

        win._restore_ui_state({"ui": {"tooltips": False}})
        check("tooltips toggle: restored onto the window, bar and flag",
              not win._tooltips and not win.top_bar._tooltips
              and not tooltips.enabled())
        win._restore_ui_state({"ui": {}})
        check("tooltips toggle: a session that predates it defaults ON",
              win._tooltips and tooltips.enabled())

        win._tooltips = False
        win.top_bar.set_tooltips(False)
        win._save_session()
        win.close()
        app.processEvents()
        again = create_main_window(SessionStore(path=tmp / "session.json"))
        check("tooltips toggle: OFF survives a close and reopen",
              not again._tooltips and not again.top_bar._tooltips
              and not tooltips.enabled())
        again._save_timer.stop()
        again.close()
        app.processEvents()
    finally:
        tooltips.set_enabled(True)   # the flag is global
        shutil.rmtree(tmp, ignore_errors=True)
