"""The "update AI Hive" dialog, and the off-thread runner both it and the top
bar's check button use.

The decisions live in `app/self_update.py`; this is only the display. Git and
pip shell out, so they never run on the GUI thread (the same rule, and the same
thread + `queue.Queue` + `QTimer` shape, as `update_panel.py`). The dialog is
opened with `open()`, not `exec()`, so the window and its agents keep painting
while it is up.

It never closes the app. After an update lands the dialog says to restart, and
the user closes the window when their agents are at a good point.

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QTextBrowser, QVBoxLayout)

from .. import self_update
from ..self_update import Status

POLL_MS = 80


class Job(QObject):
    """Run `work()` on a daemon thread and hand its result to `done` on the
    GUI thread. `work` must not raise; if it does anyway the exception becomes
    the result, because a crashed thread would leave a button busy forever."""

    finished = Signal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work
        self._events: queue.Queue = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._drain)

    def start(self) -> "Job":
        def body():
            try:
                result = self._work()
            except Exception as e:  # noqa: BLE001 - never take the app down
                result = e
            self._events.put(result)

        self._timer.start()
        threading.Thread(target=body, name="ai-hive-self-update",
                         daemon=True).start()
        return self

    def _drain(self) -> None:
        try:
            result = self._events.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        self.finished.emit(result)
        self.deleteLater()


class SelfUpdateDialog(QDialog):
    """Shows one `self_update.Check`, and applies it on request.

    `runner` is injected; with None the Update button stays disabled, so the
    offscreen suite can open the dialog without any way to touch the repo."""

    applied = Signal(object)        # self_update.Applied
    auditRequested = Signal(str)

    def __init__(self, check, runner=None, repo: str = self_update.REPO_DIR,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update AI Hive")
        self.setMinimumWidth(520)
        self._check = check
        self._runner = runner
        self._repo = repo
        self._busy = False
        self._result = None

        root = QVBoxLayout(self)
        self.title_label = QLabel("", self)
        font = self.title_label.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title_label.setFont(font)
        self.title_label.setWordWrap(True)
        root.addWidget(self.title_label)

        self.body_label = QLabel("", self)
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.body_label)

        self.notes = QTextBrowser(self)
        self.notes.setObjectName("ChangelogView")
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(220)
        root.addWidget(self.notes, 1)

        self.blocked_label = QLabel("", self)
        self.blocked_label.setObjectName("RecoveryLabel")
        self.blocked_label.setWordWrap(True)
        root.addWidget(self.blocked_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.update_btn = QPushButton("Update", self)
        self.update_btn.setDefault(True)
        self.update_btn.clicked.connect(self._on_update)
        buttons.addWidget(self.update_btn)
        self.close_btn = QPushButton("Close", self)
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

        self._refresh()

    # ------------------------------------------------------------- display ---

    def _refresh(self) -> None:
        c = self._check
        result = self._result
        if result is not None and result.ok:
            self.title_label.setText(f"AI Hive v{result.version} is installed"
                                     if result.version else "AI Hive updated")
            self.body_label.setText(result.detail)
        elif c.status is Status.UPDATE_AVAILABLE:
            self.title_label.setText(f"AI Hive v{c.remote} is available")
            self.body_label.setText(f"You are running v{c.running}. Updating "
                                    "downloads it from GitHub; you restart "
                                    "AI Hive when you are ready.")
        elif c.status is Status.RESTART_PENDING:
            self.title_label.setText(f"AI Hive v{c.installed} is installed")
            self.body_label.setText(
                f"You are still running v{c.running}. Restart AI Hive to use "
                "it: close the window and open it again. Your agents keep "
                "running until you do.")
        elif c.status is Status.UP_TO_DATE:
            self.title_label.setText("AI Hive is up to date")
            self.body_label.setText(f"You are running v{c.running}.")
        else:
            self.title_label.setText("Could not check for updates")
            self.body_label.setText(c.detail)

        if result is not None and not result.ok:
            self.body_label.setText(result.detail)
        if self._busy:
            self.body_label.setText("Updating. This can take a minute when "
                                    "new packages are needed.")

        self.notes.setMarkdown(c.notes or "")
        self.notes.setVisible(bool(c.notes))
        self.blocked_label.setText(c.blocked)
        self.blocked_label.setVisible(
            bool(c.blocked) and c.status is Status.UPDATE_AVAILABLE
            and result is None)

        offer = (c.status is Status.UPDATE_AVAILABLE
                 and not (result is not None and result.ok))
        self.update_btn.setVisible(offer)
        self.update_btn.setEnabled(offer and not self._busy and not c.blocked
                                   and self._runner is not None)
        self.update_btn.setText("Updating..." if self._busy
                                else ("Try again" if result is not None
                                      else "Update"))
        self.close_btn.setEnabled(not self._busy)

    # ------------------------------------------------------------- actions ---

    def _on_update(self) -> None:
        if self._busy or self._runner is None:
            return
        self._busy = True
        self._result = None
        self.auditRequested.emit("SELF-UPDATE apply start")
        self._refresh()
        runner, repo = self._runner, self._repo
        job = Job(lambda: self_update.apply(runner, repo), self)
        job.finished.connect(self._on_applied)
        job.start()

    def _on_applied(self, result) -> None:
        if not isinstance(result, self_update.Applied):
            result = self_update.Applied(
                False, detail=f"The update stopped unexpectedly: {result}")
        self._busy = False
        self._result = result
        self.auditRequested.emit(self_update.audit_line(result))
        self._refresh()
        self.applied.emit(result)

    # a running merge must be allowed to finish and report; closing the
    # dialog mid-update would hide whether the folder moved at all
    def reject(self) -> None:
        if self._busy:
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)
