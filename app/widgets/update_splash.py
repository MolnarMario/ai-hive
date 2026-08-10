"""The startup CLI-update splash, and the thread that keeps it responsive.

Shown BEFORE `create_main_window()` (see `main.py`), which is the whole point:
it paints in milliseconds instead of after the entire window has built, and it
makes an agent starting mid-update structurally impossible, because no agent
exists yet. The decisions all live in the Qt-free `app.cli_update`; this module
only shows them and pumps the loop.

THE THREAD IS MANDATORY, NOT STYLISTIC. CLAUDE.md records what happened the
last time this app shelled out on the GUI thread: `gemini_usage.fetch()` costs
~3.0s per call and froze the whole app for ~6s a minute, with an idle CPU,
reported as constant stuttering. So the gate runs on a `threading.Thread` and
the splash drains a `queue.Queue` on a `QTimer`, held open by a local
`QEventLoop`.

Skip closes the loop and launches AI Hive; it never kills anything. An install
killed halfway leaves a half written multi-hundred-MB binary, which is strictly
worse than the update banner it was trying to silence. The one consequence is
handled rather than prevented: if the user skips while an install is still
running, the providers being installed are held out of the autostart (see
`GateResult.installing`) so no agent can execute a half written file.

Everything is themed from the live `Palette` at paint time, like
`ornaments.BootVeil`, so the splash follows every skin and introduces no QSS
tokens of its own.
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEventLoop,
                            QRectF, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QWidget

from .. import cli_update
from ..ui_theme import Palette

# how long the finished splash stays up so "nothing to do" reads as a glance
# rather than a flash. Short enough that a fully current machine is not a wait.
AUTO_CLOSE_MS = 700
POLL_MS = 60

_STATE_TEXT = {
    cli_update.Status.UP_TO_DATE: "up to date",
    cli_update.Status.UPDATED: "updated",
    cli_update.Status.BLOCKED_PROCESSES: "skipped, the CLI was in use",
    cli_update.Status.DB_STALE: "needs a manual reinstall",
    cli_update.Status.REPORTED_BUT_UNCHANGED: "unchanged, see the top bar",
    cli_update.Status.FAILED: "check failed",
    cli_update.Status.TIMEOUT: "took too long, skipped",
    cli_update.Status.NOT_INSTALLED: "not installed",
    cli_update.Status.DISABLED: "off",
}


def state_text(outcome) -> str:
    """One short phrase per finished target. Never an em dash: this is read."""
    base = _STATE_TEXT.get(outcome.status, str(outcome.status))
    if outcome.status is cli_update.Status.UPDATED:
        return f"updated {outcome.before or '?'} to {outcome.after}"
    if outcome.status is cli_update.Status.UP_TO_DATE and outcome.before:
        return f"up to date ({outcome.before})"
    return base


class UpdateSplash(QWidget):
    """A frameless panel with one row per CLI, a sweeping arc on whichever row
    is busy, and a Skip button.

    Deliberately paints its own contents rather than composing labels: the
    colours are read from the live `Palette` inside `paintEvent`, which is what
    lets it follow a skin without a stylesheet, and it is the same sweeping-arc
    motif the rest of the app already uses for "working"."""

    skipRequested = Signal()

    _SPAN = 270 * 16     # arc sweep, in 1/16-degree units (QPainter.drawArc)
    _RING = 15
    _ROW_H = 30
    _PAD = 18

    def __init__(self, targets, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("AI Hive")
        self._rows = [[t.key, t.label, "waiting", False] for t in targets]
        self._angle = 0.0
        self.setFixedSize(420, self._PAD * 2 + 34 + self._ROW_H * len(self._rows)
                          + 40)

        self.skip_btn = QPushButton("Skip", self)
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.setToolTip(
            "Start AI Hive now. Any update already installing keeps going.")
        self.skip_btn.clicked.connect(self.skipRequested)
        self.skip_btn.adjustSize()
        self.skip_btn.move(self.width() - self._PAD - self.skip_btn.width(),
                           self.height() - self._PAD - self.skip_btn.height())

        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1100)
        self._spin.setLoopCount(-1)
        self._spin.setEasingCurve(QEasingCurve.Type.Linear)
        self._spin.valueChanged.connect(self._on_spin)

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            centre = screen.availableGeometry().center()
            self.move(centre.x() - self.width() // 2,
                      centre.y() - self.height() // 2)

    # ------------------------------------------------------------ control ---

    def set_state(self, key: str, text: str, spinning: bool = False) -> None:
        for row in self._rows:
            if row[0] == key:
                row[2] = text
                row[3] = bool(spinning)
        self._sync_spin()
        self.update()

    def row_state(self, key: str) -> str:
        """The state text currently shown for a target (used by the tests)."""
        for row in self._rows:
            if row[0] == key:
                return row[2]
        return ""

    def _sync_spin(self) -> None:
        # the animation runs ONLY while something is actually busy, so a
        # finished splash waiting out its auto-close costs nothing
        busy = any(row[3] for row in self._rows)
        running = self._spin.state() == QAbstractAnimation.State.Running
        if busy and not running:
            self._spin.start()
        elif not busy and running:
            self._spin.stop()

    def closeEvent(self, event):
        self._spin.stop()
        super().closeEvent(event)

    # ----------------------------------------------------------- painting ---

    def _on_spin(self, value):
        self._angle = float(value or 0.0)
        self.update()

    def paintEvent(self, event):
        from .terminal_view import legible_color  # avoids an import cycle

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(Palette.BG_PANEL)
        p.setBrush(bg)
        pen = QPen(QColor(Palette.ACCENT_GOLD_DIM))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                          8, 8)

        title = QFont()
        title.setPixelSize(13)
        title.setBold(True)
        p.setFont(title)
        p.setPen(legible_color(QColor(Palette.TEXT), bg))
        p.drawText(QRectF(self._PAD, self._PAD, self.width() - self._PAD * 2, 18),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Checking for CLI updates")

        sub = QFont()
        sub.setPixelSize(11)
        p.setFont(sub)
        p.setPen(legible_color(QColor(Palette.TEXT_DIM), bg))
        p.drawText(QRectF(self._PAD, self._PAD + 17,
                          self.width() - self._PAD * 2, 16),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Now is the only moment these files are not in use.")

        row_font = QFont()
        row_font.setPixelSize(12)
        top = self._PAD + 34
        for index, (_key, label, text, spinning) in enumerate(self._rows):
            y = top + index * self._ROW_H
            if spinning:
                self._paint_arc(p, self._PAD + 1, y + self._ROW_H / 2.0)
            p.setFont(row_font)
            p.setPen(legible_color(QColor(Palette.TEXT), bg))
            p.drawText(QRectF(self._PAD + self._RING + 10, y, 150, self._ROW_H),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter), label)
            p.setPen(legible_color(QColor(Palette.TEXT_DIM), bg))
            p.drawText(QRectF(self._PAD + self._RING + 160, y,
                              self.width() - self._PAD * 2 - self._RING - 160,
                              self._ROW_H),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter), text)
        p.end()

    def _paint_arc(self, p: QPainter, x: float, cy: float) -> None:
        colour = QColor(Palette.ACCENT_GOLD)
        rect = QRectF(x, cy - self._RING / 2.0, self._RING, self._RING)
        track = QColor(colour)
        track.setAlpha(46)
        pen = QPen(track)
        pen.setWidthF(1.8)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        arc = QPen(colour)
        arc.setWidthF(2.0)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc)
        p.drawArc(rect, int(-self._angle * 16), -self._SPAN)


def run_update_gate(store=None, targets=None, runner=None,
                    budget: float = cli_update.CHECK_TIMEOUT_S,
                    install_timeout: float = cli_update.INSTALL_TIMEOUT_S,
                    auto_close_ms: int = AUTO_CLOSE_MS,
                    show: bool = True) -> cli_update.GateResult:
    """Run the gate with the splash up, and return what happened.

    `store` only receives audit lines, and they are written from the GUI thread
    as they arrive rather than from the worker: `SessionStore.audit` appends to
    the same `session.log` everything else uses, and there is no reason to make
    that file concurrent for the sake of a handful of lines."""
    targets = list(targets) if targets is not None else \
        cli_update.default_targets()
    if not targets:
        return cli_update.GateResult()
    runner = runner or cli_update.subprocess_runner

    events: queue.Queue = queue.Queue()

    def work():
        try:
            cli_update.run_gate(
                targets, runner, enabled=True,
                on_event=lambda kind, payload: events.put((kind, payload)),
                budget=budget, install_timeout=install_timeout)
        finally:
            # the loop is waiting on this: a crashed gate must still launch
            events.put(("done", None))

    splash = UpdateSplash(targets) if show else None
    outcomes: list = []
    installing: set = set()
    finished = {"done": False}
    loop = QEventLoop()

    poll = QTimer()
    poll.setInterval(POLL_MS)

    def drain():
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == "start" and splash is not None:
                splash.set_state(payload.key, "checking", True)
            elif kind == "install":
                installing.add(payload.key)
                if splash is not None:
                    splash.set_state(payload.key,
                                     "installing, this can take a while", True)
            elif kind == "outcome":
                installing.discard(payload.target)
                outcomes.append(payload)
                if splash is not None:
                    splash.set_state(payload.target, state_text(payload), False)
            elif kind == "audit":
                if store is not None:
                    try:
                        store.audit(payload)
                    except Exception:  # noqa: BLE001 - forensics, never fatal
                        pass
            elif kind == "done":
                finished["done"] = True
        if finished["done"]:
            poll.stop()
            QTimer.singleShot(max(0, auto_close_ms), loop.quit)

    poll.timeout.connect(drain)
    if splash is not None:
        splash.skipRequested.connect(loop.quit)
        splash.show()
        splash.raise_()

    thread = threading.Thread(target=work, name="ai-hive-cli-update",
                              daemon=True)
    thread.start()
    poll.start()
    loop.exec()
    poll.stop()
    drain_leftovers(events, outcomes, installing, store)
    if splash is not None:
        splash.close()
        splash.deleteLater()
    # `installing` is non-empty only when Skip beat an install to the finish:
    # every completed target discards its own key on the outcome event
    return cli_update.GateResult(tuple(outcomes), tuple(sorted(installing)))


def drain_leftovers(events: queue.Queue, outcomes: list, installing: set,
                    store) -> None:
    """Take whatever the worker managed to post between the last poll and the
    loop quitting. Without this, a target that finished in that window would be
    dropped from the report (and its audit line lost) purely because of when
    the timer last fired."""
    while True:
        try:
            kind, payload = events.get_nowait()
        except queue.Empty:
            return
        if kind == "outcome":
            installing.discard(payload.target)
            outcomes.append(payload)
        elif kind == "install":
            installing.add(payload.key)
        elif kind == "audit" and store is not None:
            try:
                store.audit(payload)
            except Exception:  # noqa: BLE001 - forensics, never fatal
                pass
