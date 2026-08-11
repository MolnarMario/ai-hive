"""The Updates panel: one control whose label is a function of the machine.

Opened by the top bar's existing down-arrow button (§4.5 of the migration
spec: no NEW top-bar button, because the bar's minimum width is already 2101px
and this is a one-time setup action, and because two adjacent update controls
meaning different things is worse than either). The startup gate's preference
lives inside as a checkbox, so everything about updating a CLI is answerable
from one place.

THE CONTROL IS NEVER A BOOLEAN. Its label, its modal and what it does are all a
function of the detected `cli_install.Situation`, so a user is never offered an
action that does not apply to their machine:

    MANAGED         -> Enable automatic updates (the migration)
    SELF_ACTIVE     -> Pause automatic updates; revert as a SECONDARY action
    SELF_PAUSED     -> Resume automatic updates
    NOT_APPLICABLE  -> nothing (npm already updates itself)
    LOCKED_BY_POLICY-> nothing, with the reason shown
    MISSING         -> nothing

Pause/resume is the PRIMARY undo: one key in `~/.claude/settings.json`, instant,
no download, no reinstall, and it freezes the user on exactly the version they
have. The full revert is deliberately secondary, because it is a second install
operation rather than a toggle, and conflating the two undos in one click is how
a user who wanted to pause ends up reinstalling.

THREADING IS MANDATORY, NOT STYLISTIC, and is copied from `update_splash.py`:
worker thread, `queue.Queue`, drained by a `QTimer`. CLAUDE.md records what
happened the last time this app shelled out on the GUI thread
(`gemini_usage.fetch()`, ~3.0s a call, froze the whole app ~6s a minute with an
idle CPU). An install here can run for minutes.

NOTHING HERE IS PERSISTED. The state is DERIVED from the filesystem and
`~/.claude/settings.json` on every read, exactly as the plan-usage reading is
derived rather than stored: a remembered install method is wrong the moment the
user installs something by hand. The only persisted key remains
`ui.auto_update`, unchanged.

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from .. import cli_install
from ..cli_install import UpdateState

POLL_MS = 80

# What the primary button says in each actionable state, and the one line under
# it. Kept here rather than inline so the copy is reviewable in one place.
_ACTION_TEXT = {
    UpdateState.MANAGED: "Enable automatic updates",
    UpdateState.SELF_ACTIVE: "Pause automatic updates",
    UpdateState.SELF_PAUSED: "Resume automatic updates",
}

_ACTION_HINT = {
    UpdateState.MANAGED:
        "Installs Anthropic's self-updating build alongside the one you have. "
        "The existing copy stays where it is and is your way back.",
    UpdateState.SELF_ACTIVE:
        "Keeps you on this exact version until you turn updates back on. "
        "Nothing is downloaded and nothing is removed.",
    UpdateState.SELF_PAUSED:
        "Lets Claude Code fetch new versions in the background again.",
}


class ConsentDialog(QDialog):
    """The consent modal for the migration, and the reason it exists.

    AI Hive would be downloading and executing a script from the internet and
    changing installed software. The copy states that plainly rather than
    softening it, and names both levels of undo BEFORE the user agrees. The
    action button is disabled until the checkbox is ticked, and Cancel has
    default focus."""

    def __init__(self, winget_exe: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Let Claude Code update itself")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.addWidget(_bold(self, "What runs"))
        command = QLineEdit(cli_install.INSTALL_COMMAND, self)
        command.setReadOnly(True)
        command.setCursorPosition(0)
        command.setToolTip("Select and copy this if you would rather run it "
                           "yourself in a terminal.")
        root.addWidget(command)
        root.addWidget(_body(
            self, "This downloads and runs Anthropic's official installer from "
                  "claude.ai."))

        root.addWidget(_bold(self, "What changes"))
        root.addWidget(_body(
            self, "A second copy of Claude Code is installed under your user "
                  "folder, in .local. From then on Claude Code updates itself "
                  "in the background instead of waiting for someone to publish "
                  "a new package. No Administrator rights are needed."))

        root.addWidget(_bold(self, "What does not change"))
        rollback = winget_exe or "the existing install"
        root.addWidget(_body(
            self, "Your conversations, settings, logins and MCP configuration "
                  "are untouched, and so is the copy you have now (" + rollback
                  + "). It stays until you remove it separately, and it is the "
                    "rollback."))

        root.addWidget(_bold(self, "How to undo"))
        root.addWidget(_body(
            self, "Pause: one setting, instant, no download, and it freezes you "
                  "on whatever version you are running.\n"
                  "Full revert: reinstall the WinGet package and remove the "
                  "native one. Both are offered in this same panel afterwards."))

        self.agree = QCheckBox("I understand and agree", self)
        root.addWidget(self.agree)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.clicked.connect(self.reject)
        self.action_btn = QPushButton("Install and enable updates", self)
        self.action_btn.setEnabled(False)
        self.action_btn.clicked.connect(self.accept)
        self.agree.toggled.connect(self.action_btn.setEnabled)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.action_btn)
        root.addLayout(buttons)
        self.cancel_btn.setDefault(True)
        self.cancel_btn.setFocus()


class UpdatePanel(QDialog):
    """The panel itself.

    `runner` is INJECTED and the real one is only ever passed from the window,
    the same opt-in rule as `run_update_gate`: the offscreen suite must never
    install software or touch the user's real settings file. With no runner the
    panel still shows the detected state; it simply cannot act.

    `settings_file` is likewise a parameter so a test can point the whole panel
    at a temporary directory."""

    autoUpdateToggled = Signal(bool)
    auditRequested = Signal(str)
    # emitted after a migration lands, so the window can repoint every live
    # Claude spec at the new binary (§5 step 5)
    migrationApplied = Signal()

    def __init__(self, situation, auto_update: bool = False, runner=None,
                 winget_exe: str = "", settings_file: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Updates")
        self.setMinimumWidth(520)
        self._situation = situation
        self._runner = runner
        self._settings_file = settings_file or cli_install.settings_path()
        # captured BEFORE any migration: once the native launcher exists,
        # `resolve_claude()` stops naming this file and nothing else can
        self._winget_exe = winget_exe or cli_install.winget_exe_path()
        self._events: queue.Queue = queue.Queue()
        self._busy = False

        root = QVBoxLayout(self)
        self.state_label = QLabel("", self)
        self.state_label.setWordWrap(True)
        root.addWidget(self.state_label)

        self.version_label = QLabel("", self)
        self.version_label.setObjectName("RecoveryLabel")
        self.version_label.setWordWrap(True)
        root.addWidget(self.version_label)

        self.action_btn = QPushButton("", self)
        self.action_btn.clicked.connect(self._on_action)
        root.addWidget(self.action_btn)
        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("RecoveryLabel")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        # the two secondary acts. Both are about the OLD install, so both are
        # kept visually below the primary control rather than beside it.
        secondary = QHBoxLayout()
        self.channel_btn = QPushButton("Follow the stable channel", self)
        self.channel_btn.setToolTip(
            "Stable is about one week old and skips releases with major "
            "regressions. AI Hive also pins a floor at your current version, "
            "so switching can never move you onto an older build.")
        self.channel_btn.clicked.connect(self._on_channel)
        secondary.addWidget(self.channel_btn)
        self.revert_btn = QPushButton("Revert to the WinGet install", self)
        self.revert_btn.setToolTip(
            "Reinstalls the WinGet package, then removes the native one. This "
            "is a second install, not a toggle: to simply stop updating, use "
            "Pause above.")
        self.revert_btn.clicked.connect(self._on_revert)
        secondary.addWidget(self.revert_btn)
        self.cleanup_btn = QPushButton("Remove the old WinGet copy", self)
        self.cleanup_btn.setToolTip(
            "Optional tidy-up. It is refused while any Claude Code session is "
            "still using that copy, and AI Hive never closes one for you.")
        self.cleanup_btn.clicked.connect(self._on_cleanup)
        secondary.addWidget(self.cleanup_btn)
        root.addLayout(secondary)

        self.gate_check = QCheckBox(
            "Check for CLI updates when AI Hive starts", self)
        self.gate_check.setChecked(bool(auto_update))
        self.gate_check.setToolTip(
            "Runs before any agent launches, which is the only moment those "
            "files are not in use. Still worth having on a self-updating "
            "install: it makes sure a downloaded update has landed before "
            "agents start, and the Gemini CLI has no auto-updater at all.")
        self.gate_check.toggled.connect(self.autoUpdateToggled)
        root.addWidget(self.gate_check)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setVisible(False)
        self.log.setMinimumHeight(120)
        root.addWidget(self.log)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_btn = QPushButton("Close", self)
        self.close_btn.clicked.connect(self.reject)
        close_row.addWidget(self.close_btn)
        root.addLayout(close_row)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._drain)
        self._refresh()

    # ------------------------------------------------------------- display ---

    def situation(self):
        return self._situation

    def _refresh(self) -> None:
        s = self._situation
        self.state_label.setText(s.detail)
        bits = []
        if s.version:
            bits.append("Version " + s.version)
        if s.channel:
            bits.append("channel " + s.channel)
        if s.floor:
            bits.append("never below " + s.floor)
        if s.exe:
            bits.append(s.exe)
        self.version_label.setText(", ".join(bits))

        actionable = s.actionable() and self._runner is not None
        self.action_btn.setText(_ACTION_TEXT.get(s.state, "Nothing to do"))
        self.action_btn.setVisible(s.state in _ACTION_TEXT)
        self.action_btn.setEnabled(actionable and not self._busy)
        self.hint_label.setText(_ACTION_HINT.get(s.state, ""))
        self.hint_label.setVisible(bool(_ACTION_HINT.get(s.state)))

        native = s.state in (UpdateState.SELF_ACTIVE, UpdateState.SELF_PAUSED)
        self.channel_btn.setVisible(native)
        self.channel_btn.setEnabled(self._runner is not None and not self._busy)
        self.channel_btn.setText("Follow the stable channel"
                                 if s.channel != "stable"
                                 else "Follow the latest channel")
        self.revert_btn.setVisible(native)
        self.revert_btn.setEnabled(self._runner is not None and not self._busy)
        # cleanup is about the WinGet file, so it is offered only while one is
        # still there AND the app is no longer launching it
        self.cleanup_btn.setVisible(native and bool(self._winget_exe))
        self.cleanup_btn.setEnabled(self._runner is not None and not self._busy)

    def _say(self, text: str) -> None:
        self.log.setVisible(True)
        self.log.appendPlainText(text)

    def _set_busy(self, on: bool) -> None:
        self._busy = bool(on)
        self._refresh()

    # ------------------------------------------------------------- actions ---

    def _on_action(self) -> None:
        state = self._situation.state
        if state is UpdateState.MANAGED:
            self._start_migration()
        elif state is UpdateState.SELF_ACTIVE:
            self._apply(lambda: cli_install.set_paused(self._settings_file,
                                                       True))
        elif state is UpdateState.SELF_PAUSED:
            self._apply(lambda: cli_install.set_paused(self._settings_file,
                                                       False))

    def _start_migration(self) -> None:
        consent = ConsentDialog(self._winget_exe, self)
        if consent.exec() != QDialog.DialogCode.Accepted:
            self._say("Cancelled. Nothing was installed.")
            return
        self.auditRequested.emit("CLI-MIGRATE-START")
        before = self._situation.version
        self._run(lambda: cli_install.migrate(
            self._runner, on_event=self._post_event, before=before))

    def _on_channel(self) -> None:
        channel = "stable" if self._situation.channel != "stable" else "latest"
        self._apply(lambda: cli_install.set_channel(
            self._settings_file, channel, self._situation.version))

    def _on_revert(self) -> None:
        self._run(lambda: cli_install.revert(self._runner,
                                             on_event=self._post_event))

    def _on_cleanup(self) -> None:
        self._run(lambda: cli_install.cleanup(self._runner, self._winget_exe))

    def _apply(self, work) -> None:
        """A settings write: fast, no subprocess, so it runs inline."""
        result = work()
        self._finish(result)

    def _run(self, work) -> None:
        """Anything that shells out. Never on the GUI thread, and never killed
        on a timer: a half-written multi-hundred-MB binary is worse than the
        nag banner it was replacing."""
        if self._runner is None or self._busy:
            return
        self._set_busy(True)
        self._poll.start()

        def body():
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 - must never take the app down
                result = cli_install.Result(False, "migrate",
                                            detail=f"{type(e).__name__}: {e}")
            self._events.put(("result", result))

        threading.Thread(target=body, name="ai-hive-cli-install",
                         daemon=True).start()

    def _post_event(self, kind: str, payload) -> None:
        self._events.put((kind, payload))

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return
            if kind == "install":
                self._say("Running: " + str(payload))
            elif kind == "output":
                text = str(payload or "").strip()
                if text:
                    self._say(text[-4000:])
            elif kind == "result":
                self._poll.stop()
                self._set_busy(False)
                self._finish(payload)

    def _finish(self, result) -> None:
        for line in cli_install.audit_lines(result):
            self.auditRequested.emit(line)
        self._say(_result_text(result))
        if result.ok and result.action in ("migrate", "revert"):
            self.migrationApplied.emit()
        self.redetect()

    def redetect(self) -> None:
        """Re-read the machine. Never cached: this is the same rule the plan
        usage reading follows, and it is what makes the panel correct after an
        action without having to model what the action did."""
        from .. import providers

        self._situation = cli_install.detect(
            providers.resolve_claude(), self._settings_file, runner=self._runner)
        self.auditRequested.emit(cli_install.state_line(self._situation))
        self._refresh()

    def closeEvent(self, event):
        self._poll.stop()
        super().closeEvent(event)


def _result_text(result) -> str:
    """One sentence per outcome. Never an em dash: this is read."""
    if result is None:
        return ""
    if result.action == "migrate":
        return (f"Claude Code {result.after} is installed and will update "
                f"itself from now on." if result.ok
                else "The migration did not complete: " + result.detail)
    if result.action == "pause":
        return ("Automatic updates are paused. You stay on this version until "
                "you turn them back on." if result.ok
                else "Could not pause updates: " + result.detail)
    if result.action == "resume":
        return ("Automatic updates are on again." if result.ok
                else "Could not resume updates: " + result.detail)
    if result.action == "channel":
        if not result.ok:
            return "The release channel was not changed: " + result.detail
        floor = (" Updates will never go below " + result.before + ".") \
            if result.before else ""
        return f"Now following the {result.after} channel.{floor}"
    if result.action == "revert":
        return ("The WinGet install is back and the native one has been "
                "removed." if result.ok
                else "The revert did not complete: " + result.detail)
    if result.action == "cleanup":
        return ("The old WinGet copy has been removed." if result.ok
                else "The old copy was left alone: " + result.detail)
    return result.detail or ""


def _bold(parent, text: str) -> QLabel:
    label = QLabel(text, parent)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _body(parent, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label
