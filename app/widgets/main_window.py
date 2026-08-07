"""MainWindow: the shell — top bar, sidebar, page stack, and ALL wiring.

This is the only module that imports both the model and the view layers.
Every dialog lives here so the model API stays headless-testable.
"""

import os
import threading
import time

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import __version__
from .. import chime
from .. import claude_usage
from .. import fsopen
from .. import limit_ledger
from .. import providers
from .. import scheduled_send
from .. import session_hook
from .. import transcripts
from .. import ui_theme
from ..process_worker import (AI_KINDS, PTY_ONLY_KINDS, AgentKind, build_spec)
from ..pty_worker import HAS_CONPTY
from ..session_store import SessionStore
from ..workspace_manager import Workspace, WorkspaceManager
from .. import coordination
from ..orchestrator_bridge import OrchestratorBridge
from .activity_panel import ActivityPanel
from .agent_file_map import AgentFileMapWindow
from .ornaments import LogoRoundel, PageBorder, PlanUsageBadge
from .sidebar import SIDEBAR_WIDTH, Sidebar

SIDEBAR_MIN, SIDEBAR_MAX = 170, 700  # drag bounds (ultrawide-friendly)
from .terminal_card import TerminalCard, _snippet
from .workspace_page import WorkspacePage

SAVE_DEBOUNCE_MS = 800
HEARTBEAT_SAVE_MS = 20000  # safety-net autosave: caps worst-case loss to ~20s
# how often to reconcile each Claude agent's pinned session id with the
# transcript it is really writing, so a conversation the user switched to
# (via /resume, a fork) is what comes back on reopen — not a stale pin
SESSION_SYNC_MS = 5000

# how often to apply the Claude hooks' "needs the user" edges (drives the "?"
# badge + chime). Faster than the session sync so a chime feels prompt; the poll
# is a cheap incremental read of a small append-only file.
PROMPT_SYNC_MS = 750

# how often to re-read each running Claude agent's current model/effort from its
# transcript, so the card header follows a /model or /effort typed in the
# terminal. Deliberately its own timer rather than the 5s session sync: that
# tick does full-file title/usage scans, while this one is a stat per agent
# while nothing changed, and a tail-only read when it did.
MODEL_SYNC_MS = 1500

# how often to re-read the Claude account's plan usage from the API. One small
# HTTPS GET; a minute is well inside the resolution of a 5-hour window.
USAGE_POLL_MS = 60000
# ...except in the danger zone, where a minute is NOT fine: from this
# utilization on, agents that are actively streaming can spend the rest of the
# window in well under a poll interval, and everything that reacts to being cut
# off (planLimitReached, the reset poll it arms, the ledger) only starts once a
# reading reports it. So poll faster - but ONLY while both halves hold (nearly
# spent AND at least one agent working), which keeps the fast rate to short
# bursts at the end of a window instead of a permanently tripled request rate.
# 20s is three requests a minute at the very worst; the ordinary rate is one.
USAGE_URGENT_POLL_MS = 20000
USAGE_URGENT_PCT = 90.0
# how often the readout re-renders its countdown from the clock alone (no
# network). Repaints only when the displayed string changes.
USAGE_TICK_MS = 20000
# once a limit is spent, re-poll this soon after its stated reset so the
# "limit cleared" edge fires promptly (an unattended relaunch shouldn't wait
# out a whole poll interval at 4am), plus a small cushion for clock skew.
USAGE_RESET_GRACE_MS = 8000

# --- auto-continue after a plan-limit reset ---
# Beat between closing the limit's options menu and typing into the prompt
# underneath it: the menu tears down on the next render, and typing into the
# frame before that would land in the dying menu instead of the input box.
AUTO_CONTINUE_ESC_MS = 400
# Gap between successive agents. They all unblock on the same edge, so without
# this they would submit simultaneously into a window that just reopened.
AUTO_CONTINUE_STAGGER_MS = 2000
# What gets typed. Still one short line — the agent holds the whole
# conversation, so it needs a go-ahead, not a restatement of the work — but it
# NAMES the reason: a bare "Continue" landing hours after the last exchange is
# ambiguous, and saying the limit reset removes the ambiguity for free.
AUTO_CONTINUE_TEXT = ("The usage limit has reset. Continue the task you were "
                      "working on.")
# How often to check whether a cut-off agent's OWN stated reset time has
# passed. This is the network-free trigger and the one that actually has to be
# reliable: the usage endpoint 429s intermittently and its "cleared" edge can
# be missed entirely, which is exactly how a night's work was lost. Pure
# in-memory comparison, so a minute costs nothing.
LIMIT_WATCH_MS = 60000
# How long after a nudge to check whether the agent actually got going. Long
# enough for the TUI to redraw without the banner, short enough that a failed
# attempt is retried while it still matters.
AUTO_CONTINUE_VERIFY_MS = 20000
# How long after a LIVE latch to cross-check it against the transcript, to
# catch a cut-off that was Claude Code's own background-task-notification
# auto-continuing rather than real work (see `_dismiss_if_phantom`). The
# screen shows the identical menu either way, so this is the earliest point
# the two can be told apart. Short: the transcript record behind the banner
# is normally flushed within a second or two of it being drawn, and a miss
# here is never fatal -- the identical check runs again at nudge time.
LIMIT_PHANTOM_CHECK_MS = 3000
# A resume can land while the window is still shut, so one attempt is not
# enough — but it must not become a Continue every minute forever either.
LIMIT_RETRY_S = 300
LIMIT_MAX_TRIES = 4

# --- deferred ("send later") messages ---
# The countdown is shown to the second, so the tick is a second. It runs ONLY
# while something is actually queued (see `_sync_schedule_timer`), the same way
# a workspace row's spinner stops at zero, so a hive with nothing scheduled
# pays nothing for this feature.
SCHEDULE_TICK_MS = 1000
# How long past its time a message keeps trying before it is given up on as
# MISSED. It is retried rather than dropped because the usual reason for a
# refusal is temporary (the agent is stopped, or its TUI is still booting), and
# it is bounded because a message that lands far from its intended moment is a
# surprise, not a hand-off. The user still sees the missed entry and can send it
# by hand.
SCHEDULE_GIVE_UP_S = 900
# Slack past a reset time READ OFF THE SCREEN before acting on it. The account
# does not free up on the exact second its banner named — the clock is printed
# to the minute, the server rounds, and a nudge into a still-shut window costs
# one of the few retries above. The account READING needs no such slack: when
# it reports headroom the window is provably open already.
LIMIT_RESET_GRACE_S = 120
# Backstop for a cut-off whose reset time nothing could supply — neither the
# screen nor the usage API. A 5-hour window cannot outlast this, so waiting it
# out is always eventually right, and it guarantees a latch can never become
# permanent for want of a timestamp.
LIMIT_UNKNOWN_WAIT_S = 5 * 3600 + 600
# How far back startup recovery will reach. Sized for the real pattern it
# serves: work started during one day, the limit spent, and the machine not
# touched again until well into the NEXT day. Still finite, so a conversation
# abandoned last week isn't revived just because the app was opened to look at
# something else.
# (There is deliberately NO age bound on startup recovery. One asks the wrong
# question — how OLD a cut-off is — when what decides whether to act is whether
# it was the LAST thing that happened. `limit_ledger.latest_window` answers
# that instead, and a fixed bound had already begun silently skipping real
# cut-offs.)

# Grouped agent types for the creation dialog.
KIND_GROUPS = [
    ("AI agents", [
        ("Claude Code", AgentKind.CLAUDE),
        ("OpenAI (Codex CLI)", AgentKind.OPENAI),
        ("Gemini CLI", AgentKind.GEMINI),
        ("Grok CLI", AgentKind.GROK),
    ]),
    ("Shells", [
        ("PowerShell", AgentKind.POWERSHELL),
        ("Command Prompt (cmd)", AgentKind.CMD),
    ]),
    ("Scripts", [
        ("Python script", AgentKind.PYTHON_SCRIPT),
        ("Custom command", AgentKind.CUSTOM),
    ]),
]


class TopBar(QFrame):
    addTerminalClicked = Signal()
    sidebarToggleClicked = Signal()
    globalFontDelta = Signal(int)
    themeChanged = Signal(str)   # theme id
    soundToggled = Signal(bool)  # notification chime enabled/muted
    usageVisibilityToggled = Signal(bool)  # show/hide the plan-usage readout
    autoContinueToggled = Signal(bool)     # resume cut-off agents at the reset
    startupRecoveryToggled = Signal(bool)  # recover cut-off agents on startup
    usageRefreshRequested = Signal()       # user clicked the readout

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(42)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(8)

        self.toggle_btn = QToolButton(self)
        self.toggle_btn.setObjectName("SidebarToggle")
        self.toggle_btn.setText("☰")
        self.toggle_btn.setToolTip("Toggle sidebar")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.sidebarToggleClicked)

        logo = LogoRoundel(self)   # gilt roundel in illuminated themes, ⬡ else
        logo.setObjectName("Logo")
        name = QLabel("AI Hive", self)
        name.setObjectName("AppName")
        version = QLabel(f"v{__version__}", self)
        version.setObjectName("VersionBadge")
        self.breadcrumb = QLabel("", self)
        self.breadcrumb.setObjectName("Breadcrumb")

        self.add_terminal_btn = QToolButton(self)
        self.add_terminal_btn.setObjectName("AddTerminalBtn")
        self.add_terminal_btn.setText("+ Terminal")
        self.add_terminal_btn.setToolTip("Add a terminal agent to this workspace")
        self.add_terminal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_terminal_btn.clicked.connect(self.addTerminalClicked)

        def font_btn(text, tip, delta):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setObjectName("GlobalFontBtn")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda: self.globalFontDelta.emit(delta))
            return b

        self.font_dec_btn = font_btn("A−", "Decrease font size (all agents)", -1)
        self.font_inc_btn = font_btn("A+", "Increase font size (all agents)", +1)

        # notification-chime mute toggle: rings when an agent raises "?"
        # (settles on a question). Reflects state via its glyph (🔔/🔕).
        self._sound_on = True
        self.sound_btn = QToolButton(self)
        self.sound_btn.setObjectName("SoundToggle")
        self.sound_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sound_btn.clicked.connect(self._on_sound_clicked)
        self._refresh_sound_btn()

        # Claude plan usage: "21% used, resets in 1h20m at 14:49". Hidden until
        # a reading arrives (and permanently when there's no Claude login), and
        # hideable from the top bar's context menu.
        self.usage_badge = PlanUsageBadge(self)
        self.usage_badge.setVisible(False)
        self.usage_badge.refreshRequested.connect(self.usageRefreshRequested)
        self._usage_wanted = True   # the user's show/hide preference

        # The two auto-recovery switches, beside the readout they belong to.
        # Both act on agents the plan limit cut off; they differ only in WHEN
        # the cut-off is discovered — on opening the app, or while it runs.
        # Deliberately buttons rather than context-menu items: these decide
        # whether unattended work resumes, so their state has to be visible at
        # a glance. A caption names what they're for (a bare pair of icon
        # buttons reads as decoration), and each carries its own LED
        # (🟢 armed / ⚫ off) alongside the accent-lit checked state, so
        # "will my work resume by itself?" survives even a glance too quick
        # to register border color.
        self._startup_recovery = True
        self._auto_continue = True
        self.recovery_label = QLabel(
            "Auto-restart agents who ran out of usage on:", self)
        self.recovery_label.setObjectName("RecoveryLabel")
        self.recover_btn = QToolButton(self)
        self.recover_btn.setObjectName("RecoveryToggle")
        self.recover_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recover_btn.clicked.connect(self._on_recover_clicked)
        self.resume_btn = QToolButton(self)
        self.resume_btn.setObjectName("RecoveryToggle")
        self.resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self._refresh_recovery_btns()

        # skin selector (Winamp-style): swaps the whole chrome palette live
        self.theme_select = QComboBox(self)
        self.theme_select.setObjectName("ThemeSelect")
        self.theme_select.setToolTip("Theme")
        self.theme_select.setCursor(Qt.CursorShape.PointingHandCursor)
        for tid, theme in ui_theme.THEMES.items():
            self.theme_select.addItem(theme.name, tid)
        self.theme_select.currentIndexChanged.connect(
            lambda _i: self.themeChanged.emit(self.theme_select.currentData()))

        lay.addWidget(self.toggle_btn)
        lay.addWidget(logo)
        lay.addWidget(name)
        lay.addWidget(version)
        lay.addSpacing(12)
        lay.addWidget(self.breadcrumb)
        lay.addStretch(1)
        lay.addWidget(self.usage_badge)
        lay.addSpacing(10)
        lay.addWidget(self.recovery_label)
        lay.addSpacing(6)
        lay.addWidget(self.recover_btn)
        lay.addWidget(self.resume_btn)
        lay.addSpacing(8)
        lay.addWidget(self.theme_select)
        lay.addSpacing(8)
        lay.addWidget(self.font_dec_btn)
        lay.addWidget(self.font_inc_btn)
        lay.addSpacing(8)
        lay.addWidget(self.sound_btn)
        lay.addSpacing(8)
        lay.addWidget(self.add_terminal_btn)

    def _on_sound_clicked(self) -> None:
        self.set_sound_enabled(not self._sound_on)
        self.soundToggled.emit(self._sound_on)

    def set_sound_enabled(self, on: bool) -> None:
        """Reflect the chime on/off state in the button (no signal emitted)."""
        self._sound_on = bool(on)
        self._refresh_sound_btn()

    def _refresh_sound_btn(self) -> None:
        self.sound_btn.setText("🔔" if self._sound_on else "🔕")
        self.sound_btn.setToolTip(
            "Notification chime: ON, click to mute" if self._sound_on
            else "Notification chime: OFF, click to enable")

    def _on_recover_clicked(self) -> None:
        self.set_startup_recovery(not self._startup_recovery)
        self.startupRecoveryToggled.emit(self._startup_recovery)

    def _on_resume_clicked(self) -> None:
        self.set_auto_continue(not self._auto_continue)
        self.autoContinueToggled.emit(self._auto_continue)

    def set_startup_recovery(self, on: bool) -> None:
        """Reflect the startup-recovery preference (no signal emitted)."""
        self._startup_recovery = bool(on)
        self._refresh_recovery_btns()

    def startup_recovery(self) -> bool:
        return self._startup_recovery

    def set_auto_continue(self, on: bool) -> None:
        """Reflect the resume-on-reset preference (no signal emitted)."""
        self._auto_continue = bool(on)
        self._refresh_recovery_btns()

    def auto_continue(self) -> bool:
        return self._auto_continue

    def _refresh_recovery_btns(self) -> None:
        # `checked` drives the QSS: lit in the accent when armed, dimmed when
        # off. The LED (🟢/⚫) repeats that same state as its own glyph, so
        # "will my work resume by itself?" is answerable even without
        # registering the border color.
        self.recover_btn.setCheckable(True)
        self.recover_btn.setChecked(self._startup_recovery)
        led = "\U0001F7E2" if self._startup_recovery else "⚫"
        self.recover_btn.setText(f"{led} App start-up ⏻")
        self.recover_btn.setToolTip(
            "Recover at startup: ON. When AI Hive opens, agents whose work "
            "stopped because the plan limit ran out are continued "
            "automatically.\nClick to turn off."
            if self._startup_recovery else
            "Recover at startup: OFF. Agents left stuck on a spent plan "
            "limit stay stopped when AI Hive opens.\nClick to turn on.")
        self.resume_btn.setCheckable(True)
        self.resume_btn.setChecked(self._auto_continue)
        led = "\U0001F7E2" if self._auto_continue else "⚫"
        self.resume_btn.setText(f"{led} Usage reset \U0001F504")
        self.resume_btn.setToolTip(
            "Resume on limit reset: ON. While AI Hive is running, agents cut "
            "off mid-work by the plan limit are continued the moment the "
            "limit resets.\nClick to turn off."
            if self._auto_continue else
            "Resume on limit reset: OFF. Agents cut off by the plan limit "
            "wait for you.\nClick to turn on.")

    def set_usage(self, usage) -> None:
        """Push a plan-usage reading into the badge (no-op while hidden by the
        user, so a poll can't resurrect a readout they switched off)."""
        self.usage_badge.set_usage(usage)
        if not self._usage_wanted:
            self.usage_badge.setVisible(False)

    def note_usage_error(self, error: str) -> None:
        """A poll failed with no earlier reading to fall back on: show the
        can't-read pill rather than nothing at all. Still honours the user's
        show/hide preference — an error is not a reason to force the readout
        back onto a bar they cleared."""
        self.usage_badge.mark_unreadable(error)
        if not self._usage_wanted:
            self.usage_badge.setVisible(False)

    def set_usage_visible(self, on: bool) -> None:
        """Reflect the show/hide preference (no signal emitted)."""
        self._usage_wanted = bool(on)
        self.usage_badge.setVisible(self._usage_wanted
                                    and self.usage_badge.has_content())

    def usage_visible(self) -> bool:
        return self._usage_wanted

    def set_recovery_available(self, on: bool) -> None:
        """Show/hide both recovery toggles (and their caption). They act only
        on Claude agents cut off by a plan limit, so with no Claude login
        there is nothing for them to do — hide them with the readout rather
        than offer dead switches."""
        self.recovery_label.setVisible(bool(on))
        self.recover_btn.setVisible(bool(on))
        self.resume_btn.setVisible(bool(on))

    def contextMenuEvent(self, event):
        """Right-click anywhere on the bar: toggle the plan-usage readout.

        A context-menu item rather than another button — the bar is already
        busy, and this is a set-once preference. The two RECOVERY switches are
        deliberately NOT here: they decide whether unattended work resumes, so
        they get visible buttons instead (and each setting has exactly one
        control, never two that can disagree).
        """
        menu = QMenu(self)
        act = menu.addAction("Show plan usage")
        act.setCheckable(True)
        act.setChecked(self._usage_wanted)
        act.toggled.connect(self.usageVisibilityToggled)
        menu.exec(event.globalPos())

    def set_theme(self, theme_id: str) -> None:
        """Reflect the active theme in the dropdown without re-emitting."""
        i = self.theme_select.findData(theme_id)
        if i >= 0:
            self.theme_select.blockSignals(True)
            self.theme_select.setCurrentIndex(i)
            self.theme_select.blockSignals(False)

    def set_breadcrumb(self, workspace_name: str) -> None:
        self.breadcrumb.setText(f"AI Hive  ›  {workspace_name}"
                                if workspace_name else "")


class AddTerminalDialog(QDialog):
    """Configure a new agent: type, and for AI agents provider/model/effort.

    Model stays dialog-free — the dialog only produces an AgentSpec via
    result_spec(); headless tests build specs directly.
    """

    def __init__(self, default_name: str, parent=None,
                 cwd: str = "", busy_ids=()):
        super().__init__(parent)
        self.setWindowTitle("New Agent")
        self.setMinimumWidth(420)
        self._cwd = cwd
        self._busy_ids = set(busy_ids)  # conversations a running agent holds
        self._resume_loaded = False

        form = QFormLayout()
        self.name_edit = QLineEdit(default_name, self)

        self.kind_combo = QComboBox(self)
        first = True
        for group_name, entries in KIND_GROUPS:
            if not first:
                self.kind_combo.insertSeparator(self.kind_combo.count())
            first = False
            for label, kind in entries:
                self.kind_combo.addItem(label, kind)
        self.kind_combo.setCurrentIndex(0)  # Claude by default

        # AI provider fields
        self.provider_note = QLabel(self)
        self.provider_note.setObjectName("ProviderNote")
        self.provider_note.setWordWrap(True)
        self.model_combo = QComboBox(self)
        self.effort_combo = QComboBox(self)
        # startup permission mode — the modes the Claude TUI cycles through
        # with Shift+Tab (Claude only). Default omits the flag = today's behavior.
        self.mode_combo = QComboBox(self)
        self.mode_combo.setToolTip(
            "Which permission mode this Claude agent starts in: the same modes "
            "you flip through with Shift+Tab in the terminal. 'Normal' is the "
            "current default; the agent can still switch modes once running.")
        # resume an existing conversation from this workspace folder (Claude)
        self.resume_combo = QComboBox(self)
        self.resume_combo.setToolTip(
            "Start this Claude agent by resuming a past conversation from this "
            "workspace's folder, instead of a fresh one.")
        self.command_edit = QLineEdit(self)
        self.command_edit.setPlaceholderText("full launch command (editable)")

        # script/shell fields
        self.program_edit = QLineEdit(self)
        self.program_edit.setPlaceholderText("script path / executable")
        self.browse_btn = QPushButton("Browse…", self)
        prog_row = QHBoxLayout()
        prog_row.addWidget(self.program_edit, 1)
        prog_row.addWidget(self.browse_btn)
        self.args_edit = QLineEdit(self)
        self.args_edit.setPlaceholderText("extra arguments (optional)")

        self.pty_check = QCheckBox(
            "Full terminal (interactive: TUIs, colors, Ctrl+C)", self)
        self.pty_check.setToolTip(
            "Run inside a real pseudo-console (ConPTY). Uncheck for a "
            "lightweight line-only console.")
        self.pty_check.setChecked(HAS_CONPTY)
        self.pty_check.setEnabled(HAS_CONPTY)

        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.kind_combo)
        self._note_row = self.provider_note
        form.addRow("", self.provider_note)
        self._model_label = QLabel("Model", self)
        form.addRow(self._model_label, self.model_combo)
        self._effort_label = QLabel("Effort", self)
        form.addRow(self._effort_label, self.effort_combo)
        self._mode_label = QLabel("Mode", self)
        form.addRow(self._mode_label, self.mode_combo)
        self._resume_label = QLabel("Conversation", self)
        form.addRow(self._resume_label, self.resume_combo)
        self._cmd_label = QLabel("Command", self)
        form.addRow(self._cmd_label, self.command_edit)
        self._prog_label = QLabel("Program", self)
        form.addRow(self._prog_label, prog_row)
        self._args_label = QLabel("Args", self)
        form.addRow(self._args_label, self.args_edit)
        form.addRow("", self.pty_check)

        self._ai_widgets = (self._model_label, self.model_combo,
                            self._effort_label, self.effort_combo)
        # Claude-only, so kept out of _ai_widgets (which is every AI provider)
        self._mode_widgets = (self._mode_label, self.mode_combo)
        self._resume_widgets = (self._resume_label, self.resume_combo)
        self._script_widgets = (self._prog_label, self.program_edit,
                                self.browse_btn, self._args_label, self.args_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel,
                                   parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.program_edit.textChanged.connect(self._validate)
        self.command_edit.textChanged.connect(self._validate)
        self.browse_btn.clicked.connect(self._browse)
        self._on_kind_changed(0)

    def _kind(self) -> AgentKind:
        return self.kind_combo.currentData()

    def _is_ai(self) -> bool:
        return self._kind() in AI_KINDS

    def _needs_program(self) -> bool:
        return self._kind() in (AgentKind.PYTHON_SCRIPT, AgentKind.CUSTOM)

    def _on_kind_changed(self, _index: int) -> None:
        kind = self._kind()
        if kind is None:  # a separator got selected — snap to next real item
            self.kind_combo.setCurrentIndex(self.kind_combo.currentIndex() + 1)
            return
        is_ai = self._is_ai()
        needs_program = self._needs_program()

        for w in self._script_widgets:
            w.setVisible(needs_program)

        # AI provider fields
        self.provider_note.setVisible(is_ai)
        self._cmd_label.setVisible(False)
        self.command_edit.setVisible(False)
        for w in self._ai_widgets + self._mode_widgets + self._resume_widgets:
            w.setVisible(False)

        # resume picker: Claude-only, shown only when the folder has a
        # resumable conversation that isn't already held by a running agent
        if kind == AgentKind.CLAUDE:
            # startup permission mode (Shift+Tab modes) — Claude only
            self._populate_modes(providers.get("claude"))
            for w in self._mode_widgets:
                w.setVisible(True)
            self._ensure_resume_loaded()
            if self.resume_combo.count() > 1:
                for w in self._resume_widgets:
                    w.setVisible(True)

        if is_ai:
            prov = providers.get(AI_KINDS[kind])
            detected = providers.detected(AI_KINDS[kind])
            self._populate_models(prov)
            self._model_label.setVisible(True)
            self.model_combo.setVisible(True)
            if prov.efforts:
                self._populate_efforts(prov)
                self._effort_label.setVisible(True)
                self.effort_combo.setVisible(True)
            # template providers (and any undetected AI) expose an editable command
            if not prov.native_flags or not detected:
                self._cmd_label.setVisible(True)
                self.command_edit.setVisible(True)
                if not self.command_edit.text().strip() and prov.base_cmd:
                    tmpl = prov.base_cmd + (" " + prov.model_flag if prov.model_flag else "")
                    self.command_edit.setPlaceholderText(tmpl + "  (edit to taste)")
            status = "✓ detected" if detected else "⚠ CLI not detected; install it or edit the command"
            self.provider_note.setText(f"{prov.note}\n{status}")

        # AI agents are always interactive (ConPTY); shells/scripts choose
        if kind in PTY_ONLY_KINDS:
            self.pty_check.setChecked(True)
            self.pty_check.setEnabled(False)
        else:
            self.pty_check.setEnabled(HAS_CONPTY)
        self._validate()

    def _populate_models(self, prov) -> None:
        self.model_combo.clear()
        for label, value in prov.models:
            self.model_combo.addItem(label, value)

    def _populate_efforts(self, prov) -> None:
        self.effort_combo.clear()
        for tok in prov.efforts:
            self.effort_combo.addItem(tok.capitalize() if tok else "Default", tok)
        # Ultracode isn't a launch flag (claude --effort only takes
        # low/medium/high/xhigh/max); it's an in-session mode. Show it, greyed
        # out, so users know to enable it manually in the terminal.
        if prov.native_flags:
            self.effort_combo.addItem(
                "Ultracode (activate manually in terminal, model-dependent)", None)
            model = self.effort_combo.model()
            item = model.item(self.effort_combo.count() - 1)
            item.setEnabled(False)  # visible but non-selectable

    def _populate_modes(self, prov) -> None:
        # populate once so a user's choice survives toggling Type away and back
        if self.mode_combo.count() or prov is None:
            return
        for label, value in prov.permission_modes:
            self.mode_combo.addItem(label, value)  # index 0 = "" = default

    def _ensure_resume_loaded(self) -> None:
        """Populate the resume picker once: 'New conversation' plus every past
        conversation in this workspace's folder (newest first), skipping any a
        running agent still holds (resuming that would race/truncate it)."""
        if self._resume_loaded:
            return
        self._resume_loaded = True
        self.resume_combo.clear()
        self.resume_combo.addItem("New conversation", "")
        if not self._cwd:
            return

        from app import session_sync
        for conv in session_sync.conversation_previews(self._cwd):
            if conv.session_id in self._busy_ids:
                continue  # in use by a running agent — unsafe to double-resume
            when = time.strftime("%b %d %H:%M", time.localtime(conv.mtime))
            preview = conv.preview or "(empty session)"
            if len(preview) > 48:
                preview = preview[:47] + "…"
            self.resume_combo.addItem(f"{when}  ·  {preview}", conv.session_id)

    def _validate(self) -> None:
        ok = True
        if self._needs_program():
            ok = bool(self.program_edit.text().strip())
        elif self._is_ai() and not self.command_edit.isHidden():
            # undetected AI needs a runnable command; detected native (Claude) is fine
            prov = providers.get(AI_KINDS[self._kind()])
            if not providers.detected(prov.key):
                ok = bool(self.command_edit.text().strip())
        self._ok_btn.setEnabled(ok)

    def _browse(self) -> None:
        pattern = ("Python scripts (*.py);;All files (*)"
                   if self._kind() == AgentKind.PYTHON_SCRIPT else
                   "Programs (*.exe *.bat *.cmd);;All files (*)")
        path, _ = QFileDialog.getOpenFileName(self, "Choose program", "", pattern)
        if path:
            self.program_edit.setText(path)

    def result_spec(self, cwd: str):
        kind = self._kind()
        name = self.name_edit.text().strip() or "Agent"
        if self._is_ai():
            model = self.model_combo.currentData() or ""
            effort = (self.effort_combo.currentData() or ""
                      if not self.effort_combo.isHidden() else "")
            mode = (self.mode_combo.currentData() or ""
                    if not self.mode_combo.isHidden() else "")
            custom = (self.command_edit.text().strip()
                      if not self.command_edit.isHidden() else "")
            extra = QProcess.splitCommand(self.args_edit.text().strip()) \
                if not self.args_edit.isHidden() else []
            spec = build_spec(kind, name, cwd=cwd, model=model, effort=effort,
                              permission_mode=mode,
                              custom_command=custom, args=extra)
            # resume a chosen past conversation: pin it and launch --resume <id>
            resume_id = (self.resume_combo.currentData()
                         if not self.resume_combo.isHidden() else "")
            if resume_id:
                spec.session_id = resume_id
                spec.resume = True
            return spec
        program = self.program_edit.text().strip()
        args = QProcess.splitCommand(self.args_edit.text().strip())
        pty = self.pty_check.isChecked() and HAS_CONPTY
        return build_spec(kind, name, cwd=cwd, program=program, args=args, pty=pty)


class ScheduleMessageDialog(QDialog):
    """Compose a message now, choose when it is typed in.

    Opened by Ctrl+Shift+Enter in an agent's terminal (prefilled with whatever
    was typed) or from the card's right-click menu (empty). It doubles as the
    manage view: everything already queued for this agent is listed with a
    cancel button, and a MISSED entry can be sent immediately or dismissed.

    The prefill is INFERRED from the painted input box, which can come up short
    on a very long horizontally-scrolled line, so it is shown in an editable box
    rather than scheduled behind the user's back. That is the whole reason this
    is a dialog and not a silent hotkey.

    Model-free like AddTerminalDialog: it produces a (text, due_ts) pair and the
    window does the scheduling, so headless tests never need it.
    """

    PRESETS = [("5m", 300), ("15m", 900), ("30m", 1800),
               ("1h", 3600), ("2h", 7200)]

    def __init__(self, agent, parent=None, prefill: str = ""):
        super().__init__(parent)
        self.agent = agent
        self._due_ts = None
        self.editing_id: str | None = None
        self.setWindowTitle(f"Send later to {agent.spec.name}")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Message", self))
        self.text_edit = QPlainTextEdit(prefill or "", self)
        self.text_edit.setPlaceholderText(
            "what to type into this agent when the countdown ends")
        self.text_edit.setMinimumHeight(90)
        root.addWidget(self.text_edit)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Send in:", self))
        for label, seconds in self.PRESETS:
            btn = QPushButton(label, self)
            btn.setObjectName("SchedulePreset")
            btn.clicked.connect(
                lambda _checked=False, s=seconds: self._set_preset(s))
            presets.addWidget(btn)
        presets.addStretch(1)
        root.addLayout(presets)

        custom = QHBoxLayout()
        self.delay_edit = QLineEdit(self)
        self.delay_edit.setPlaceholderText("45m, 1h30, 2:15")
        self.delay_edit.setToolTip(
            "A delay from now. A bare number means minutes.")
        self.clock_edit = QLineEdit(self)
        self.clock_edit.setPlaceholderText("03:30")
        self.clock_edit.setToolTip(
            "A wall-clock time. A time already past today means tomorrow.")
        custom.addWidget(QLabel("or in", self))
        custom.addWidget(self.delay_edit, 1)
        custom.addWidget(QLabel("or at", self))
        custom.addWidget(self.clock_edit, 1)
        root.addLayout(custom)

        # says exactly when this will fire, so "at 3" is never ambiguous
        self.when_label = QLabel("", self)
        self.when_label.setObjectName("ScheduleWhen")
        self.when_label.setWordWrap(True)
        root.addWidget(self.when_label)

        self.pending_box = QVBoxLayout()
        root.addLayout(self.pending_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText("Schedule")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self.text_edit.textChanged.connect(self._revalidate)
        # the two time fields are alternatives: typing in one clears the other,
        # so there is never a hidden second answer deciding the fire time
        self.delay_edit.textEdited.connect(lambda *_: self._on_time_edited(True))
        self.clock_edit.textEdited.connect(lambda *_: self._on_time_edited(False))
        self._set_preset(self.PRESETS[0][1])
        self.refresh_pending()

    # ------------------------------------------------------------ helpers ---

    def _set_preset(self, seconds: int) -> None:
        # Write the preset in the DELAY vocabulary ("5m", "1h"), never the
        # countdown one: `format_countdown(300)` is "5:00", and "H:MM" means
        # five HOURS to `parse_delay`. The two formats look alike and mean
        # different things, so a preset must never round-trip through the
        # display format.
        self.delay_edit.setText(f"{seconds // 3600}h" if seconds % 3600 == 0
                                else f"{seconds // 60}m")
        self.clock_edit.clear()
        self._revalidate()

    def _on_time_edited(self, from_delay: bool) -> None:
        if from_delay:
            self.clock_edit.clear()
        else:
            self.delay_edit.clear()
        self._revalidate()

    def _resolve_due(self) -> float | None:
        """The chosen fire time, or None when neither field parses. The clock
        wins only when the delay field is empty, which the mutual clearing
        above guarantees."""
        delay = scheduled_send.parse_delay(self.delay_edit.text())
        if delay is not None:
            return time.time() + delay
        return scheduled_send.parse_clock(self.clock_edit.text())

    def _revalidate(self) -> None:
        self._due_ts = self._resolve_due()
        has_text = bool(self.text_edit.toPlainText().strip())
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(has_text and self._due_ts is not None)
        if self._due_ts is None:
            self.when_label.setText("Enter a delay (45m, 1h30, 2:15) or a "
                                    "time (03:30).")
            return
        left = scheduled_send.format_countdown(self._due_ts - time.time())
        self.when_label.setText(
            f"Sends {scheduled_send.format_clock(self._due_ts)}, in {left}.")

    def refresh_pending(self) -> None:
        """(Re)build the list of what this agent is already holding."""
        while self.pending_box.count():
            item = self.pending_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        held = self.agent.scheduled_messages()
        if not held:
            return
        self.pending_box.addWidget(QLabel(f"Already queued ({len(held)})", self))
        for msg in held:
            self.pending_box.addLayout(self._pending_row(msg))

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        layout.deleteLater()

    def _pending_row(self, msg):
        row = QHBoxLayout()
        missed = msg.state == scheduled_send.MISSED
        when = ("missed" if missed
                else scheduled_send.format_countdown(msg.seconds_left()))
        label = QLabel(f"{when}  {_snippet(msg.text, 60)}", self)
        label.setToolTip(f"{scheduled_send.format_clock(msg.due_ts)}\n{msg.text}")
        row.addWidget(label, 1)
        edit = QToolButton(self)
        edit.setText("✏")
        edit.setToolTip("Edit this message or its time")
        edit.clicked.connect(lambda _checked=False, m=msg: self._start_edit(m))
        row.addWidget(edit)
        if missed:
            send_now = QToolButton(self)
            send_now.setText("send now")
            send_now.setToolTip("Type this into the agent right away")
            send_now.clicked.connect(
                lambda _checked=False, m=msg: self._send_now(m))
            row.addWidget(send_now)
        drop = QToolButton(self)
        drop.setText("✕")
        drop.setToolTip("Cancel this message")
        drop.clicked.connect(lambda _checked=False, m=msg: self._cancel(m))
        row.addWidget(drop)
        return row

    def _start_edit(self, msg) -> None:
        """Load an already-queued message back into the compose form so its
        text and/or fire time can be changed in place, instead of
        cancel-and-recreate (which would silently lose its spot in the
        queue)."""
        self.editing_id = msg.id
        self.text_edit.setPlainText(msg.text)
        self.text_edit.setFocus()
        self.text_edit.selectAll()
        remaining = msg.due_ts - time.time()
        if remaining > 0:
            self.delay_edit.setText(f"{max(1, round(remaining / 60))}m")
            self.clock_edit.clear()
            self._revalidate()
        else:
            self._set_preset(self.PRESETS[0][1])  # missed: the old time is gone
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        self.setWindowTitle(f"Edit message for {self.agent.spec.name}")

    def _cancel_edit(self) -> None:
        self.editing_id = None
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Schedule")
        self.setWindowTitle(f"Send later to {self.agent.spec.name}")

    def _cancel(self, msg) -> None:
        self.agent.cancel_scheduled(msg.id)
        if msg.id == self.editing_id:
            self._cancel_edit()
        self.refresh_pending()

    def _send_now(self, msg) -> None:
        """Deliver a missed message immediately, on the user's say-so.

        Goes through `nudge` exactly as the timed path does, so this can never
        become an assignment. A refusal (the agent is stopped) leaves the entry
        alone so it is still there to try again."""
        if self.agent.nudge(msg.text):
            self.agent.mark_scheduled_sent(msg.id)
        self.refresh_pending()

    # -------------------------------------------------------------- result ---

    def result_message(self) -> tuple[str, float] | None:
        """(text, due_ts), or None when the dialog produced nothing usable."""
        text = self.text_edit.toPlainText().strip()
        if not text or self._due_ts is None:
            return None
        return text, self._due_ts


class MainWindow(QMainWindow):
    # Plan-usage edges, for features that need to ACT on the account being cut
    # off rather than just display it (e.g. relaunching agents that died on a
    # limit, unattended, once it resets). Both are EDGE-triggered and
    # level-correct, like the chime: `planLimitReached` fires once on
    # headroom -> spent and carries the claude_usage.Limit (so `.resets_at`
    # says when it frees up); `planLimitCleared` fires once on the way back.
    # `plan_usage()` exposes the latest full reading for polling-style callers.
    planLimitReached = Signal(object)   # claude_usage.Limit
    planLimitCleared = Signal()
    # private: carries a reading from the fetch thread back to the GUI thread.
    # Qt marshals a cross-thread emit through the event loop, so everything the
    # slot touches (widgets, timers) stays on the main thread.
    _usageReady = Signal(object)

    def __init__(self, manager: WorkspaceManager, store: SessionStore,
                 session: dict | None = None):
        super().__init__()
        self.manager = manager
        self.store = store
        self.setWindowTitle("AI Hive")
        self.resize(1440, 900)

        self._pages: dict[str, WorkspacePage] = {}
        self._map_window: AgentFileMapWindow | None = None  # lazy, reused
        self._focused_card: TerminalCard | None = None
        self._closing = False
        self._ready = False  # suppress save-storms during initial load
        self._last_saved_json = None  # what last reached disk (heartbeat guard)
        self._theme_id = ui_theme.ACTIVE_THEME.id  # active skin (persisted)
        self._sound_enabled = True  # notification chime on "?" (persisted)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_session)

        # safety-net heartbeat: re-saves only when live state diverges from
        # disk, so a missed/suppressed signal can't silently strand work
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(HEARTBEAT_SAVE_MS)
        self._heartbeat_timer.timeout.connect(self._heartbeat_save)

        # keep pinned session ids tracking the live conversations
        self._session_sync_timer = QTimer(self)
        self._session_sync_timer.setInterval(SESSION_SYNC_MS)
        self._session_sync_timer.timeout.connect(self._sync_live_sessions)

        # apply the Claude hooks' "needs the user" edges (the "?"/chime signal)
        self._prompt_sync_timer = QTimer(self)
        self._prompt_sync_timer.setInterval(PROMPT_SYNC_MS)
        self._prompt_sync_timer.timeout.connect(self.manager.sync_prompt_events)

        # keep the header's model/effort label on what the agent is really
        # running (transient: this never saves)
        self._model_sync_timer = QTimer(self)
        self._model_sync_timer.setInterval(MODEL_SYNC_MS)
        self._model_sync_timer.timeout.connect(self.manager.refresh_model_effort)

        # ---- Claude plan usage (top-bar readout + limit-reached edges) ----
        # PURELY TRANSIENT: a reading refreshes the badge and may emit the
        # plan-limit edges, but it must NEVER mark the session dirty — the same
        # rule as activity_changed/waiting_changed. This polls every minute
        # forever; wiring it to a save would rewrite session.json 60x an hour.
        self._usage = None            # latest claude_usage.Usage
        self._usage_visible = True    # user preference (persisted)
        self._usage_inflight = False  # one request at a time, never stack
        self._plan_blocked = False    # edge state for planLimitReached/Cleared
        # agent ids with a resume SCHEDULED but not yet delivered. The attempt
        # counter only advances when the nudge actually goes out, up to
        # (stagger * index + esc) ms later, so without this a second trigger
        # arriving inside that window sees everyone downstream of the first
        # agent as un-attempted and schedules them all over again — observed
        # live on 2026-08-04, when the API's cleared edge and the minute
        # watchdog both fired at 05:30 and typed Continue twice into three of
        # four agents (burning half their retry budget and dropping a stray
        # message into freshly started work).
        self._resume_pending: set[str] = set()
        # ledger keys already filed this run, so one cut-off is written once
        # however many times its latch is (re)raised
        self._ledger_seen: set[tuple] = set()
        self._auto_continue = True    # user preference (persisted)
        self._startup_recovery = True  # user preference (persisted)
        self._usage_backoff = 0       # consecutive 429s -> exponential poll gap
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(USAGE_POLL_MS)
        self._usage_timer.timeout.connect(self._poll_usage)
        self._usage_tick_timer = QTimer(self)
        self._usage_tick_timer.setInterval(USAGE_TICK_MS)
        self._usage_tick_timer.timeout.connect(self._tick_usage)
        # fires just after a spent limit's stated reset, so the "cleared" edge
        # doesn't wait out a full poll interval
        self._usage_reset_timer = QTimer(self)
        self._usage_reset_timer.setSingleShot(True)
        self._usage_reset_timer.timeout.connect(self._poll_usage)
        # the network-free auto-continue trigger: resume a cut-off agent once
        # the reset time ITS OWN banner stated has passed, whatever the API is
        # doing (or not doing)
        self._limit_watch_timer = QTimer(self)
        self._limit_watch_timer.setInterval(LIMIT_WATCH_MS)
        self._limit_watch_timer.timeout.connect(self._check_limit_resets)
        # deferred-message countdown + delivery. Started and stopped by
        # _sync_schedule_timer so it only runs while something is queued.
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(SCHEDULE_TICK_MS)
        self._schedule_timer.timeout.connect(self._tick_schedules)

        # shared-board control channel (named-pipe RPC → this GUI): relays each
        # agent's log_activity note onto its workspace board. Additive and
        # guarded: if it can't listen, the app runs exactly as before (agents
        # simply can't post board notes).
        self.bridge = OrchestratorBridge(
            manager, active_ws=lambda: self.manager.active_id, parent=self)
        self.bridge.start()
        # shared SessionStart-hook plumbing: ONE settings file (injected into
        # every Claude agent via --settings) + ONE mapping file the child hooks
        # append their live conversation id to, keyed by AIHIVE_AGENT_ID. This
        # is what lets an in-TUI /resume or /clear be captured authoritatively,
        # including in multi-agent folders. Written BEFORE any agent is armed or
        # started; reset each run because agent ids are minted fresh per run.
        session_dir = self.store.path.parent
        self._hook_settings_path = str(session_dir / "aihive_session_hook.json")
        self._session_map_path = str(session_dir / "live_sessions.jsonl")
        self._prompt_events_path = str(session_dir / "prompt_events.jsonl")
        try:
            session_hook.write_settings_file(self._hook_settings_path,
                                             self._session_map_path,
                                             self._prompt_events_path)
            session_hook.reset_map(self._session_map_path)
            session_hook.reset_events(self._prompt_events_path)
        except OSError as e:
            self.store.audit(f"HOOK-SETUP-FAIL {type(e).__name__}: {e}")
            self._hook_settings_path = ""  # degrade: fall back to fs correlation
        self.manager.session_map_path = self._session_map_path
        self.manager.prompt_events_path = self._prompt_events_path
        manager.save_now = self._save_now  # immediate persistence for spawn_worker
        manager.arm_agent = self._arm_agent_mcp  # arm new agents before they start
        manager.audit = self._store_audit   # so a degraded save leaves a trace
        self._rearm_agent_configs()  # restored claude agents re-acquire MCP tools

        self._build_ui()
        self._adopt_existing_model()
        self._wire_model()
        self._restore_ui_state(session or {})
        self._apply_page_border()   # frame matches the restored skin
        self._ready = True  # from here on, structural changes save immediately
        self._heartbeat_timer.start()
        self._session_sync_timer.start()
        self._prompt_sync_timer.start()
        self._model_sync_timer.start()
        self._limit_watch_timer.start()
        # a restored session can bring back queued messages, so the tick may
        # need to be running before anything else happens
        self._sync_schedule_timer()

    def _arm_agent_mcp(self, ws, agent) -> None:
        """Arm a Claude agent's per-run launch config before it starts (and
        again on restore, via _rearm_agent_configs — none of this is persisted).

        Two things, both keyed off the agent being Claude:
          * the SessionStart hook that reports the agent's LIVE conversation id
            back to AI Hive (via --settings + a per-agent AIHIVE_AGENT_ID). This
            is INDEPENDENT of the board bridge — every Claude agent gets it, so
            conversation tracking works even with the bridge disabled.
          * the per-workspace MCP config giving the agent the board's
            log_activity tool (nothing more)."""
        if agent.spec.provider != "claude":
            return
        if self._hook_settings_path:
            agent.spec.settings_path = self._hook_settings_path
            # AIHIVE_AGENT_ID == TerminalAgent.id, the same key sync_live_sessions
            # matches on, so a hook line maps straight back to this agent.
            agent.spec.env["AIHIVE_AGENT_ID"] = agent.id
        if not self.bridge.enabled:
            return
        agent.spec.mcp_config_path = self.bridge.mcp_config_path_for(ws.id)

    def _rearm_agent_configs(self) -> None:
        # Re-arm EVERY restored Claude agent. Must NOT bail when the bridge is
        # disabled: the SessionStart hook (settings_path + AIHIVE_AGENT_ID) is
        # independent of the board bridge, and _arm_agent_mcp already
        # self-gates the MCP-config part on bridge.enabled. Bailing here would
        # leave restored agents with no live-conversation tracking exactly when
        # the bridge is unavailable — the case the hook most needs to cover.
        for ws in self.manager.workspaces:
            for agent in ws.agents:
                self._arm_agent_mcp(ws, agent)

    # ----------------------------------------------------------------- ui ---

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.top_bar = TopBar(central)

        # sidebar | center, split by a draggable handle (resizable + persisted)
        self.body_split = QSplitter(Qt.Orientation.Horizontal, central)
        self.body_split.setObjectName("BodySplit")
        self.body_split.setHandleWidth(4)
        self.body_split.setChildrenCollapsible(True)

        self.sidebar = Sidebar(self.body_split)
        # let the splitter own the width (override the sidebar's own cap)
        self.sidebar.setMinimumWidth(SIDEBAR_MIN)
        self.sidebar.setMaximumWidth(SIDEBAR_MAX)
        self._sidebar_saved_width = SIDEBAR_WIDTH
        center = QWidget(self.body_split)
        center.setObjectName("CentralBody")
        center_lay = QHBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)
        self.stack = QStackedWidget(center)
        self.stack.setObjectName("PageStack")
        self.activity_panel = ActivityPanel(center)
        self.activity_panel.hide()
        self.activity_panel.taskEdited.connect(self._on_task_edited)
        center_lay.addWidget(self.stack, 1)
        center_lay.addWidget(self.activity_panel)

        self.body_split.addWidget(self.sidebar)
        self.body_split.addWidget(center)
        self.body_split.setCollapsible(0, True)   # sidebar can drag closed
        self.body_split.setCollapsible(1, False)  # never collapse the terminals
        self.body_split.setStretchFactor(0, 0)
        self.body_split.setStretchFactor(1, 1)
        self.body_split.setSizes([SIDEBAR_WIDTH, 1000])
        self.body_split.splitterMoved.connect(self._on_sidebar_resized)

        root.addWidget(self.top_bar)
        root.addWidget(self.body_split, 1)
        self.setCentralWidget(central)

        # illuminated-manuscript page border: a mouse-transparent overlay over
        # the whole central widget; it paints only when the active theme is
        # illuminated, and the root margins open up to seat content inside it
        self._central = central
        self._root_layout = root
        self._page_border = PageBorder(central)
        self._apply_page_border()   # geometry + margins + visibility for theme

        # refresh the activity panel's board log + git changes while visible
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(3000)
        self._activity_timer.timeout.connect(self._refresh_activity)

        self.top_bar.addTerminalClicked.connect(self._on_add_terminal_clicked)
        self.top_bar.sidebarToggleClicked.connect(self._toggle_sidebar)
        self.top_bar.globalFontDelta.connect(self._change_global_font)
        self.top_bar.themeChanged.connect(self._change_theme)
        self.top_bar.soundToggled.connect(self._on_sound_toggled)
        self.top_bar.usageVisibilityToggled.connect(self._on_usage_visibility)
        self.top_bar.autoContinueToggled.connect(self._on_auto_continue)
        self.top_bar.startupRecoveryToggled.connect(self._on_startup_recovery)
        # resume whoever the limit cut off, the moment the window reopens
        self.planLimitCleared.connect(self._resume_blocked_agents)
        self.manager.agentLimitBlocked.connect(self._on_agent_limit_blocked)
        self.top_bar.usageRefreshRequested.connect(self._on_usage_refresh)
        # QueuedConnection is the point: the fetch thread emits, and the slot
        # runs on the GUI thread where touching widgets/timers is legal
        self._usageReady.connect(self._on_usage_ready,
                                 Qt.ConnectionType.QueuedConnection)
        self.sidebar.addRequested.connect(self._on_add_workspace_clicked)
        self.sidebar.workspaceSelected.connect(self.manager.set_active)
        self.sidebar.renameRequested.connect(self.manager.rename_workspace)
        self.sidebar.deleteRequested.connect(self._confirm_delete_workspace)
        self.sidebar.openFolderRequested.connect(self._open_workspace_folder)
        # inline agent list: the sidebar expands agents under a workspace and
        # reveals a clicked agent's card (no overlapping popup)
        self.sidebar.agents_provider = self._agents_for_ws
        self.sidebar.agentActivated.connect(self._reveal_agent)
        # the sidebar's own "⏱" clock opens the same view/edit/cancel popup
        # as the card's clock chip, always empty-prefill (manage, not compose)
        self.sidebar.agentScheduleRequested.connect(
            lambda ws_id, agent_id: self._on_schedule_message(agent_id))
        # inline file explorer: the sidebar resolves a ws to its root folder,
        # opens files with the OS default app, and its open/closed set persists
        self.sidebar.files_root_provider = self._project_path_for_ws
        self.sidebar.fileActivated.connect(self._on_sidebar_file_activated)
        self.sidebar.filesToggled.connect(self._schedule_save)
        # the sidebar owns the live layout; the manager persists whatever it
        # reports (order + categories) and re-sequences its workspace list
        self.sidebar.layoutChanged.connect(self.manager.apply_sidebar_layout)

        # Ctrl+Shift+* so plain Ctrl/Alt keys stay free for the focused
        # terminal (Claude Code, shells, readline all need them).
        QShortcut(QKeySequence("Ctrl+Shift+T"), self,
                  self._on_add_terminal_clicked)
        QShortcut(QKeySequence("Ctrl+Shift+N"), self,
                  self._on_add_workspace_clicked)
        QShortcut(QKeySequence("Ctrl+Shift+B"), self, self._toggle_sidebar)

    # ------------------------------------------------------------- sidebar ---

    def _toggle_sidebar(self) -> None:
        sizes = self.body_split.sizes()
        total = sizes[0] + sizes[1]
        if sizes[0] > 0:  # collapse, remembering the current width
            self._sidebar_saved_width = sizes[0]
            self.body_split.setSizes([0, total])
        else:  # restore
            w = self._sidebar_saved_width or SIDEBAR_WIDTH
            self.body_split.setSizes([w, total - w])
        self._schedule_save()

    def _on_sidebar_resized(self, _pos: int, _index: int) -> None:
        w = self.body_split.sizes()[0]
        if w > 0:
            self._sidebar_saved_width = w
        self._schedule_save()

    def _adopt_existing_model(self) -> None:
        """Build pages/rows for workspaces created before this window existed
        (session restore happens before the window is constructed)."""
        for ws in self.manager.workspaces:
            self._on_workspace_added(ws)
            for agent in ws.agents:
                self._pages[ws.id].add_agent(agent)
            self.sidebar.set_stats(ws.id, self.manager.workspace_stats(ws.id))
        # arrange rows into the restored order + categories (rows were appended
        # top-level as each workspace was added above)
        self.sidebar.apply_layout(self.manager.sidebar_layout())
        for ws in self.manager.workspaces:   # re-apply stats after the rebuild
            self.sidebar.set_stats(ws.id, self.manager.workspace_stats(ws.id))
        active = self.manager.active_id
        if active:
            self._on_active_changed(active)

    def _wire_model(self) -> None:
        mgr = self.manager
        mgr.workspaceAdded.connect(self._on_workspace_added)
        mgr.workspaceRemoved.connect(self._on_workspace_removed)
        mgr.workspaceRenamed.connect(self._on_workspace_renamed)
        mgr.activeChanged.connect(self._on_active_changed)
        mgr.terminalAdded.connect(self._on_terminal_added)
        mgr.terminalRemoved.connect(self._on_terminal_removed)
        mgr.workspaceStatsChanged.connect(self.sidebar.set_stats)
        mgr.workspaceStatsChanged.connect(self._on_stats_for_activity)
        # start/stop the countdown tick as messages are queued and drained.
        # Stats are recomputed on every `scheduled_changed`, so this is the
        # one edge that always covers it; _sync_schedule_timer is written to
        # be safe under this signal's high firing rate.
        mgr.workspaceStatsChanged.connect(
            lambda *_: self._sync_schedule_timer())
        mgr.workspacePathChanged.connect(self._on_workspace_path_changed)
        mgr.layoutChanged.connect(self._on_layout_changed)
        # an agent just settled on a question ("?" appeared) -> sound the chime
        mgr.agentWaiting.connect(self._on_agent_waiting)
        mgr.dirty.connect(self._schedule_save)
        # structural changes (add/remove agent or workspace) save IMMEDIATELY,
        # not on the 800 ms debounce — so an abrupt process kill can never lose
        # an agent that the user created
        mgr.terminalAdded.connect(lambda *_: self._save_now())
        mgr.terminalRemoved.connect(lambda *_: self._save_now())
        mgr.workspaceAdded.connect(lambda *_: self._save_now())
        mgr.workspaceRemoved.connect(lambda *_: self._save_now())
        # reordering/categorizing is a structural layout change -> save now
        mgr.sidebarLayoutChanged.connect(lambda *_: self._save_now())

    def _on_agent_waiting(self, ws_id: str, agent_id: str) -> None:
        """An agent just raised its "?" (settled on a prompt/question). Ring the
        notification chime so the user notices even from another workspace —
        unless they've muted it. Non-blocking; a silent no-op if unavailable.

        One exception: an agent parked on the plan-limit banner raises "?" too
        (that banner comes with a numbered options menu, which is exactly what
        the scrape looks for), but it is not a question the user can answer —
        the only cure is time. Ringing for it would wake someone at 4am for
        nothing, and auto-continue is about to handle it unattended anyway, so
        the badge still lights but the bell stays quiet.
        """
        if not self._sound_enabled:
            return
        agent = self.manager.agent(ws_id, agent_id)
        if agent is not None and agent.is_limit_blocked():
            return
        chime.play()

    # ---------------------------------------------------- plan usage ------
    def plan_usage(self):
        """The latest plan-usage reading (`claude_usage.Usage`), or None before
        the first one lands. Public: this plus `planLimitReached` /
        `planLimitCleared` is the API other features hook into — `.blocked`
        says whether the account is cut off, `.resets_at` says until when."""
        return self._usage

    def start_usage_polling(self) -> None:
        """Begin polling the account's plan usage. OPT-IN, called by main.py
        only — exactly like `quit_on_close`, and for the same reason: the smoke
        suite constructs many windows per process and must never touch the
        network (or the user's real account). Tests drive `_on_usage_ready`
        with synthetic readings instead.

        Seeds from Claude's own on-disk cache for an instant first paint, then
        goes and gets a live reading. The cache is often stale (observed 1.5
        days out of date), so it is only ever a placeholder until the fetch
        lands."""
        cached = claude_usage.read_cached()
        if cached is not None:
            self._apply_usage(cached)
        self._usage_timer.start()
        self._usage_tick_timer.start()
        self._poll_usage()

    def _poll_usage(self) -> None:
        """Kick a fetch on a daemon thread (the pty_worker/mcp_server pattern).

        `_usage_inflight` is the guard that matters: a hung request must never
        let the minute timer stack threads behind it."""
        if self._closing or self._usage_inflight:
            return
        self._usage_inflight = True

        def worker():
            reading = claude_usage.fetch()
            self._usageReady.emit(reading)   # queued -> GUI thread

        threading.Thread(target=worker, daemon=True,
                         name="aihive-usage").start()

    def _usage_poll_interval(self) -> int:
        """The poll gap the current situation asks for, BEFORE any 429 backoff.

        A minute is the right ordinary rate: a 5-hour window moves slowly and
        the readout is a number on a bar. It is the wrong rate for the end of a
        window with work under way - several agents streaming can burn the last
        few percent in far less than a minute, and nothing in the app learns it
        is cut off until a READING says so (the plan-limit edge, the reset poll
        it arms, the ledger entry all hang off `_apply_usage`). A minute of
        blindness there is a minute of agents parked on a banner nobody noticed.

        Both halves are required, which is what keeps this cheap. Under
        URGENT_PCT there is nothing imminent to catch; with every agent idle
        the number is not moving at all, so a faster poll would only ask the
        same question more often. And once a window is actually SPENT the fast
        rate stops again: `_arm_reset_poll` already schedules a poll for the
        moment it reopens, so hammering the endpoint through the outage adds
        nothing. Worst case is therefore three requests a minute, only in the
        last stretch of a window, only while agents are working.
        """
        limit = claude_usage.headline(self._usage)
        if limit is None or not (USAGE_URGENT_PCT <= limit.percent
                                 < claude_usage.EXHAUSTED_PCT):
            return USAGE_POLL_MS
        if not any(a.is_busy() for a in self.manager.all_agents()):
            return USAGE_POLL_MS
        return USAGE_URGENT_POLL_MS

    def _retune_usage_poll(self) -> None:
        """Apply `_usage_poll_interval` (times any backoff) to the timer.

        Only when it CHANGES: `QTimer.setInterval` restarts a running timer, so
        calling this unconditionally from the tick would keep resetting the
        countdown and the poll would never come round at all.
        """
        want = self._usage_poll_interval() * (2 ** self._usage_backoff)
        if want != self._usage_timer.interval():
            self._usage_timer.setInterval(want)

    def _on_usage_refresh(self) -> None:
        """The user clicked the readout. Clear any 429 backoff first: they are
        asking now, and leaving the timer parked at sixteen minutes would make
        a successful manual refresh look like it fixed nothing when the next
        automatic poll failed to arrive."""
        self._usage_backoff = 0
        self._retune_usage_poll()
        if self._usage_timer.isActive():
            self._usage_timer.start()      # restart the interval from now
        self._poll_usage()

    def _on_usage_ready(self, reading) -> None:
        self._usage_inflight = False
        if self._closing:
            return
        if reading is not None and reading.ok:
            self._usage_backoff = 0
            self._apply_usage(reading)
            # after adopting it, so the new percentage decides the next gap
            self._retune_usage_poll()
            return
        # A failed poll keeps the last good number on screen, greyed, rather
        # than blanking a figure the user is watching. Only a machine with no
        # Claude login at all (no-auth) has nothing to show, ever.
        if reading is not None and reading.error == "no-auth" and self._usage is None:
            self.top_bar.usage_badge.setVisible(False)
            # no Claude account => no plan limit to recover from; don't leave
            # two switches on the bar that can never do anything
            self.top_bar.set_recovery_available(False)
            self._usage_timer.stop()
            self._usage_tick_timer.stop()
            return
        # The endpoint rate-limits (observed: two 429s in a row, then a 200).
        # Backing off matters beyond politeness — a minute timer that keeps
        # firing into a 429 is how the app can go hours without ever seeing the
        # blocked state, which is what the plan-limit edges are derived from.
        # The auto-continue watchdog is deliberately independent of all this.
        if reading is not None and reading.error == "http 429":
            self._usage_backoff = min(self._usage_backoff + 1, 4)
            # the backoff multiplies whatever the situation asks for, so it
            # still wins over the urgent rate: being rate-limited is precisely
            # when polling harder is counterproductive
            self._retune_usage_poll()
        if self._usage is None:
            # Nothing to grey out: there has never been a reading this run, and
            # since CLI 2.1.220 stopped writing `cachedUsageUtilization` there
            # is no on-disk seed to cover the gap either. Say the number is
            # unreadable instead of leaving a hole in the bar — a readout that
            # silently vanishes is indistinguishable from a deleted feature
            # (reported as exactly that), and the backoff can keep it away for
            # sixteen minutes at a stretch.
            self.top_bar.note_usage_error(
                reading.error if reading is not None else "unknown")
            return
        self.top_bar.usage_badge.mark_stale(True)

    def _apply_usage(self, reading) -> None:
        """Adopt a reading: refresh the badge and fire the plan-limit edges.

        NEVER marks the session dirty — see the transient-signal rule in
        CLAUDE.md. This runs every minute for the life of the process.
        """
        self._usage = reading
        self.top_bar.set_usage(reading)
        blocked = reading.blocked
        if blocked is not None and not self._plan_blocked:
            self._plan_blocked = True
            self.planLimitReached.emit(blocked)
        elif blocked is None and self._plan_blocked:
            self._plan_blocked = False
            self.planLimitCleared.emit()
        self._arm_reset_poll(blocked)

    def _arm_reset_poll(self, blocked) -> None:
        """While cut off, schedule one extra poll just after the stated reset
        so `planLimitCleared` fires within seconds of the window reopening —
        an unattended relaunch at 4am shouldn't wait out the minute timer."""
        if blocked is None or blocked.resets_at is None:
            self._usage_reset_timer.stop()
            return
        delay = int((blocked.resets_at - time.time()) * 1000) + USAGE_RESET_GRACE_MS
        # QTimer takes a 32-bit interval; a far-future reset is covered by the
        # ordinary minute poll, so only arm when it's genuinely near.
        if 0 < delay <= 6 * 3600 * 1000:
            self._usage_reset_timer.start(delay)
        else:
            self._usage_reset_timer.stop()

    def _tick_usage(self) -> None:
        """Re-render the countdown from the clock alone — no network.

        Also the place the poll gap follows the WORK: whether agents are busy
        changes constantly, and `activity_changed` fires every couple of
        seconds per agent, so this cheap 20s sweep is where that half of
        `_usage_poll_interval` is re-evaluated rather than off a signal that
        would retune (and so restart the poll timer) far more often than the
        number can move.
        """
        self.top_bar.usage_badge.tick()
        self._retune_usage_poll()

    def _on_usage_visibility(self, on: bool) -> None:
        """User toggled the readout from the top bar's context menu."""
        self._usage_visible = bool(on)
        self.top_bar.set_usage_visible(self._usage_visible)
        self._schedule_save()   # a UI preference, like sound_enabled

    def _on_auto_continue(self, on: bool) -> None:
        """User toggled resume-on-limit-reset from the top bar."""
        self._auto_continue = bool(on)
        self._schedule_save()   # a UI preference, like sound_enabled

    def _on_startup_recovery(self, on: bool) -> None:
        """User toggled recover-at-startup from the top bar."""
        self._startup_recovery = bool(on)
        self._schedule_save()   # a UI preference, like sound_enabled

    def recover_blocked_at_startup(self) -> int:
        """Find agents the plan limit stopped BEFORE this run, and arm them to
        be resumed. Returns how many were armed.

        OPT-IN and called from main.py only, exactly like `start_usage_polling`
        and `quit_on_close`: the smoke suite shares `create_main_window` and
        must never type into a real agent or read the user's real transcripts.

        The live latch cannot see these — after a restart each agent's screen
        shows a REPLAYED conversation, which `_scrape_limit` deliberately
        ignores as history. The transcript is the durable record instead: it
        still ends exactly where the limit stopped it.

        This only ARMS them. Delivery stays with the one watchdog
        (`_check_limit_resets`), so there is a single resume path to get right
        — including waiting for a freshly launched TUI to become prompt-ready.
        """
        if not self._startup_recovery:
            return 0
        # 1. LOOK EVERYWHERE. Every agent in every workspace is examined,
        #    running or not, and every cut-off found is filed — the record is
        #    supposed to be complete even where the action is selective.
        found: list[tuple] = []           # (agent, record)
        for agent in self.manager.all_agents():
            spec = agent.spec
            if spec.provider != "claude" or not agent.is_pty:
                continue
            if agent.is_limit_blocked():
                continue        # already latched live; nothing to reconstruct
            info = transcripts.limit_cut_off(spec.cwd, spec.session_id)
            # The gate: no stoppage message, no action.
            if not info or not info["cut_off"] or not info["at"]:
                continue
            record = self._ledger_cut_off(agent, info["at"],
                                          info["resets_at"], info["window"],
                                          info["banner"], source="transcript")
            found.append((agent, record))
        if not found:
            return 0

        # 2. ACT ON THE MOST RECENT WINDOW ONLY. Agents stopped by one window
        #    all state the same reset clock, so they group; anything from an
        #    earlier window is history that has already had its chance.
        newest = limit_ledger.latest_window([r for _a, r in found])
        newest_keys = {tuple(r["key"]) for r in newest}
        closed = limit_ledger.closed_keys(
            limit_ledger.read_all(self._ledger_dir()))

        armed = 0
        for agent, record in found:
            key = tuple(record["key"])
            name = agent.spec.name
            if key not in newest_keys:
                self._limit_audit(f"STARTUP-SKIP agent={name} (an earlier "
                                  f"window, cut off {record['at_local']})")
                continue
            # Already resolved in an earlier run — resumed, or given up on.
            # Without this the same abandoned conversation would be revived on
            # every launch for as long as its transcript ends on the banner.
            if key in closed:
                self._limit_audit(f"STARTUP-SKIP agent={name} "
                                  f"(already resolved)")
                continue
            # A card the user left stopped IS started here: it was stopped BY
            # THE LIMIT, not by the user, so leaving it alone would silently
            # drop exactly the work this feature exists to rescue. Only agents
            # with a corroborated cut-off get this — never a blanket autostart.
            if not agent.is_running():
                # It MUST come back as a resume. `start()` mints a fresh
                # session id for a non-resume launch, which would open an empty
                # conversation and abandon the very transcript that proved the
                # cut-off — and `spec.resume` has already been consumed if the
                # user stopped this card during an earlier run. We have just
                # read that transcript, so resuming it is known-good.
                agent.spec.resume = True
                self._limit_audit(f"STARTUP-START agent={name}")
                agent.start()
            agent.mark_limit_blocked(record["reset_at"] or None,
                                     from_startup=True,
                                     cut_off_at=record["at"],
                                     window=record["window"],
                                     banner=record["banner"])
            armed += 1
        self._limit_audit(f"STARTUP-SCAN found={len(found)} armed={armed}")
        return armed

    # ------------------------------------------------- the cut-off ledger ---
    def _ledger_dir(self) -> str:
        """Where `limit_events.jsonl` lives — beside session.json, so a cut-off
        record travels with the session it belongs to."""
        try:
            return str(self.store.path.parent)
        except Exception:
            return ""

    def _ledger_cut_off(self, agent, at: float, reset_at: float,
                        window: str, banner: str, source: str) -> dict:
        """File one cut-off in the durable ledger and return the record.

        Always returns a usable record even when the write fails, because the
        caller navigates by its `key`; a failed write is audited, never raised
        (the ledger observes the recovery, it must not be able to break it).
        """
        ws = self.manager.workspace_of(agent.id)
        spec = agent.spec
        record = limit_ledger.record_cut_off(
            self._ledger_dir(), cwd=spec.cwd, session_id=spec.session_id,
            at=at, reset_at=reset_at or 0.0,
            ws_id=(ws.id if ws else ""), ws_name=(ws.name if ws else ""),
            agent_name=spec.name, task=agent.current_task or "",
            window=window, banner=banner, source=source)
        if record is not None:
            self._ledger_seen.add(tuple(record["key"]))
        if record is None:
            self._limit_audit(f"LEDGER-FAIL agent={spec.name}")
            record = {"key": limit_ledger.key_of(spec.cwd, spec.session_id,
                                                 reset_at or 0.0, at),
                      "at": at, "at_local": limit_ledger.local_stamp(at),
                      "reset_at": reset_at or 0.0, "window": window,
                      "banner": banner}
        return record

    def _ledger_key(self, agent) -> list:
        """The ledger identity of the cut-off this agent is currently latched
        on — see `limit_ledger.key_of` for why it is not the agent's id."""
        return limit_ledger.key_of(agent.spec.cwd, agent.spec.session_id,
                                   agent.limit_resets_at() or 0.0,
                                   agent.limit_cut_off_at())

    def _ledger_outcome(self, agent, outcome: str, detail: str = "") -> None:
        limit_ledger.record_outcome(self._ledger_dir(), self._ledger_key(agent),
                                    outcome, tries=agent.limit_attempts(),
                                    detail=detail)

    def _snapshot_screens(self, agents=None) -> int:
        """Persist every pty agent's screen and drop the ones nothing claims.

        Never raises: this runs inside `closeEvent`, after the authoritative
        save, and a cosmetic feature must not be able to interfere with a
        clean shutdown."""
        from .. import screen_snapshot
        try:
            agents = list(self.manager.all_agents() if agents is None
                          else agents)
            root = str(self.store.path.parent)
            written = screen_snapshot.save_for_agents(agents, root)
            # keys go stale on their own: every /clear or fork mints a new
            # conversation, so without this the directory only ever grows
            screen_snapshot.prune(root, screen_snapshot.keys_for_agents(agents))
            return written
        except Exception:
            return 0

    def _store_audit(self, message: str) -> None:
        """Plain forensic line in session.log, for callers that have no store
        of their own (the manager's degraded-save path). Same never-raise rule
        as `_limit_audit`."""
        try:
            self.store.audit(message)
        except Exception:
            pass

    def _limit_audit(self, message: str) -> None:
        """Forensic line in session.log for the auto-continue path.

        Not decoration: this feature has now failed silently twice, and both
        times the cause had to be reconstructed hours later from transcript
        timestamps and inference. A latch that never fired and a latch that
        fired into a still-closed window look identical from outside; these
        lines tell them apart in seconds.
        """
        try:
            self.store.audit(f"LIMIT {message}")
        except Exception:
            pass    # forensics must never break the feature they observe

    def _on_agent_limit_blocked(self, ws_id: str, agent_id: str) -> None:
        agent = self.manager.agent(ws_id, agent_id)
        if agent is None:
            return
        # The menu carries no clock and the banner that does may have scrolled
        # out of the searched region, so a latch can arrive with no due time —
        # which strands the network-free watchdog and leaves only the flaky
        # usage API. The ACCOUNT reading knows when the window reopens even
        # when the screen doesn't, so borrow it.
        if agent.limit_resets_at() is None:
            usage = self.plan_usage()
            if usage is not None:
                agent.set_limit_reset(usage.resets_at)
        at = agent.limit_resets_at()
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at
                else "unknown")
        self._limit_audit(f"BLOCKED agent={agent.spec.name} resets={when}")
        # File it durably. Skipped when this cut-off is already on record: the
        # startup scan files before it arms, and a failed verify re-latches the
        # same cut-off, so this signal fires more than once per episode.
        key = tuple(self._ledger_key(agent))
        if key not in self._ledger_seen:
            self._ledger_cut_off(agent, agent.limit_cut_off_at(), at or 0.0,
                                 agent.limit_window(),
                                 agent.limit_banner_text(), source="live")
        QTimer.singleShot(LIMIT_PHANTOM_CHECK_MS,
                          lambda: self._dismiss_if_phantom(ws_id, agent_id,
                                                            key))

    def _dismiss_if_phantom(self, ws_id: str, agent_id: str, key: tuple) -> None:
        """Shortly after a live latch: was the interrupted turn something the
        user or AI Hive actually asked for, or Claude Code's own background-
        task-notification auto-continuing on its own? Both render the
        identical "Stop and wait for limit to reset" menu, so the screen
        genuinely cannot tell them apart — the transcript can, because it
        carries the raw record behind the banner (see
        `transcripts._is_synthetic_user_turn`).

        Gated on POSITIVE evidence (`info["synthetic"]`), NOT on the absence
        of a cut-off, and that difference is the whole safety of running this
        early. `_auto_continue_agent` may dismiss on a plain `not cut_off`
        because it runs at reset time, hours later, when the transcript is
        certainly written; three seconds after the banner was DRAWN it may
        not be, and a running agent's transcript always exists, so an
        unflushed banner reads as `cut_off False` rather than None (see
        `transcripts.limit_cut_off`). Dismissing on that would clear a
        genuine latch during the race — the exact inversion the tri-state
        exists to prevent, and unrecoverable unless the parked TUI happens to
        redraw and re-latch. So only a transcript that positively SHOWS the
        interrupted turn was Claude Code's own plumbing clears anything here;
        every other reading leaves the latch alone and the nudge-time check
        remains the backstop.
        """
        if self._closing:
            return
        agent = self.manager.agent(ws_id, agent_id)
        if agent is None or not agent.is_limit_blocked():
            return
        if tuple(self._ledger_key(agent)) != key:
            return   # a newer cut-off has since taken this one's place
        info = transcripts.limit_cut_off(agent.spec.cwd, agent.spec.session_id)
        if info is None or not info.get("synthetic"):
            return
        self._limit_audit(
            f"PHANTOM agent={agent.spec.name} (the interrupted turn was a "
            f"background/system notification, not real work)")
        self._ledger_outcome(agent, limit_ledger.DISMISSED,
                             detail="trivial background-task cut-off")
        agent.clear_limit_block()

    def _check_limit_resets(self) -> None:
        """Network-free trigger: resume any cut-off agent whose OWN banner said
        the limit would be back by now.

        This is the RELIABLE half, and it exists because the API half isn't:
        the usage endpoint 429s intermittently, and its "cleared" edge only
        fires if this same process also observed the blocked state first — so a
        restart, a bad poll, or a missed edge silently costs a whole night. The
        banner states its reset time in local clock time, latched when it was
        drawn, so this needs neither the network nor process continuity.
        """
        if self._closing or not self._ready:
            return
        now = time.time()
        # Late-fill a due time for anything still lacking one (the account
        # reading may only have arrived after the cut-off was latched).
        usage = self.plan_usage()
        if usage is not None and usage.resets_at:
            for a in self.manager.all_agents():
                if a.is_limit_blocked() and a.limit_resets_at() is None:
                    a.set_limit_reset(usage.resets_at)

        account_clear = usage is not None and usage.blocked is None

        def due(a):
            # A WEEKLY window is not readable off the screen: its banner prints
            # a bare wall clock ("resets 8pm") for a reset that can be days
            # away, and `parse_reset_clock` can only ever resolve that to the
            # next 8pm. Acting on it would nudge days early, every day. The
            # account reading is the only thing that knows, so a weekly cut-off
            # waits for it and ignores the clock entirely.
            if a.limit_window() == "weekly":
                return account_clear
            at = a.limit_resets_at()
            if at is not None:
                # ...and even a session clock gets slack: the banner names a
                # minute, not an instant, and a nudge into a window that is
                # still shut spends one of very few retries.
                return at + LIMIT_RESET_GRACE_S <= now
            # Still no clock from anywhere — screen silent, API unreachable.
            # A latch must never become permanent for want of a timestamp
            # (observed live: `resets=unknown` at 05:10 sat untouched for five
            # hours), so fall back to the longest a window can possibly last.
            return (a.limit_latched_at()
                    and now - a.limit_latched_at() >= LIMIT_UNKNOWN_WAIT_S)

        self._resume_blocked_agents(due=due)

    def _resume_blocked_agents(self, due=None) -> None:
        """The plan limit reset — put the agents it cut off back to work.

        Two triggers land here. `planLimitCleared` (no `due` filter) means the
        ACCOUNT is provably clear, so every latched agent goes; its
        `_arm_reset_poll` cushion makes that land within seconds of the window
        reopening. `_check_limit_resets` passes a `due` predicate so only agents
        whose own stated reset has passed are touched. Promptness is the point
        either way: the first message after a window expires is what STARTS the
        next 5-hour window, so resuming at 4am also means the clock has rolled
        over by morning.

        Only agents LATCHED as cut off are touched (`is_limit_blocked`) — the
        account-wide reading cannot say who was mid-turn. An agent that is busy
        again, or was never cut off, is left alone.

        Each latch is gated by the toggle that OWNS it: one recovered from disk
        at startup answers to `startup_recovery`, one seen live answers to
        `auto_continue`. That keeps the two switches genuinely independent —
        turning one off can never strand a latch the other created.
        """
        if self._closing or not self._ready:
            return
        blocked = [a for a in self.manager.all_agents()
                   if a.spec.provider == "claude" and a.is_pty
                   and a.is_running() and not a.is_busy()
                   and a.is_limit_blocked()
                   and (self._startup_recovery if a.limit_from_startup()
                        else self._auto_continue)
                   and a.limit_retry_ready(LIMIT_RETRY_S, LIMIT_MAX_TRIES)
                   and a.id not in self._resume_pending
                   and (due is None or due(a))]
        for i, agent in enumerate(blocked):
            # claimed BEFORE the stagger, because the attempt itself is only
            # recorded when the nudge goes out — see `_resume_pending`
            self._resume_pending.add(agent.id)
            # Stagger, then Esc, then type. The Esc closes the limit's options
            # menu (upgrade / extra usage / cancel) that is sitting over the
            # prompt; `write` is right for it (it stamps _last_input_ts, so the
            # pty's echo of the keystroke doesn't fake a "working" pulse),
            # whereas the text itself goes through `nudge` -> _write_task_to_pty,
            # which deliberately does NOT stamp, so the resumed work DOES pulse.
            QTimer.singleShot(i * AUTO_CONTINUE_STAGGER_MS,
                              lambda a=agent: self._auto_continue_agent(a))

    def _auto_continue_agent(self, agent) -> None:
        """Dismiss the limit prompt, then submit the go-ahead a beat later."""
        if self._closing or not agent.is_running():
            self._resume_pending.discard(agent.id)
            return
        # An agent recovered at startup may still be booting its TUI. Don't
        # poke a launching terminal with stray Esc keys every minute — just
        # wait. `nudge` would refuse anyway, and a refusal costs no attempt, so
        # the watchdog picks it up as soon as the prompt is live.
        if not agent.prompt_ready():
            self._resume_pending.discard(agent.id)
            self._limit_audit(f"WAIT agent={agent.spec.name} (TUI not ready)")
            return
        # TWO AGREEING SOURCES before anything is typed. The screen said this
        # agent was cut off; the conversation on disk has to still end there.
        # A banner stays in view (and is redrawn) long after the agent moved
        # on, so the screen alone can be describing history — and a stray
        # "Continue" into an agent that is working fine is both an
        # interruption and real quota spent. TRI-STATE on purpose: only a
        # transcript that demonstrably CARRIED ON, OR shows the interrupted
        # turn was Claude's own background-task-notification rather than real
        # work (see `transcripts._is_synthetic_user_turn` — normally caught
        # already, promptly, by `_dismiss_if_phantom`; this is the backstop
        # for a transcript that hadn't flushed yet at latch time), refutes the
        # latch; one that cannot be read (a drifted pin, a conversation Claude
        # hasn't flushed) is no evidence either way and must not strand a
        # genuine cut-off.
        info = transcripts.limit_cut_off(agent.spec.cwd, agent.spec.session_id)
        if info is not None and not info["cut_off"]:
            self._limit_audit(f"PHANTOM agent={agent.spec.name} (the "
                              f"conversation carried on, or the interrupted "
                              f"turn wasn't real work)")
            self._ledger_outcome(agent, limit_ledger.DISMISSED,
                                 detail="transcript shows no real work lost")
            agent.clear_limit_block()
            self._resume_pending.discard(agent.id)
            return
        agent.write("\x1b")

        def send():
            if self._closing or not agent.is_running():
                self._resume_pending.discard(agent.id)
                return
            if not agent.nudge(AUTO_CONTINUE_TEXT):
                self._resume_pending.discard(agent.id)
                self._limit_audit(f"NUDGE-SKIP agent={agent.spec.name} "
                                  f"(prompt not ready)")
                return
            agent.note_limit_attempt()
            # released only now: from here the attempt counter is what keeps a
            # second trigger away (`limit_retry_ready`), so the claim and the
            # count are never both absent
            self._resume_pending.discard(agent.id)
            self._limit_audit(f"NUDGE agent={agent.spec.name} "
                              f"try={agent.limit_attempts()}")
            # Verify rather than assume. A nudge can land while the window is
            # still shut (clock skew, or a reset not exactly on the stated
            # minute); clearing the latch here — as this first did — burned the
            # single attempt and left the agent parked for good. The banner is
            # gone once it is genuinely going again.
            def verify():
                if self._closing:
                    return
                # capture the cut-off's identity before recheck_limit can clear
                # it — the ledger entry has to close the SAME record the cut-off
                # opened, whichever way this goes
                key = self._ledger_key(agent)
                cut_at, window = agent.limit_cut_off_at(), agent.limit_window()
                banner, resets = agent.limit_banner_text(), agent.limit_resets_at()
                from_startup = agent.limit_from_startup()
                tries = agent.limit_attempts()
                # Two independent proofs, because the menu alone is NOT one:
                # Esc removes it whether or not the agent went anywhere, so
                # "menu gone" once reported RESUMED for an agent that never
                # produced another line. The conversation on disk is the real
                # evidence — if it STILL ends on the banner, nothing happened.
                menu_gone = not agent.recheck_limit()
                stuck, _, _ = transcripts.ended_on_limit(
                    agent.spec.cwd, agent.spec.session_id)
                if menu_gone and not stuck:
                    self._limit_audit(f"RESUMED agent={agent.spec.name}")
                    limit_ledger.record_outcome(
                        self._ledger_dir(), key, limit_ledger.RESUMED,
                        tries=tries)
                    return
                # keep the latch so the watchdog tries again, carrying the
                # cut-off's identity so the retry stays the SAME episode
                agent.mark_limit_blocked(resets, from_startup=from_startup,
                                         cut_off_at=cut_at, window=window,
                                         banner=banner)
                self._limit_audit(
                    f"STILL-BLOCKED agent={agent.spec.name} "
                    f"try={tries} "
                    f"(menu_gone={menu_gone} transcript_stuck={stuck})")
                if tries >= LIMIT_MAX_TRIES:
                    # out of retries: close the record so a later run doesn't
                    # inherit a cut-off this one already gave up on
                    self._limit_audit(f"GAVE-UP agent={agent.spec.name} "
                                      f"after {tries} tries")
                    limit_ledger.record_outcome(
                        self._ledger_dir(), key, limit_ledger.FAILED,
                        tries=tries)
            QTimer.singleShot(AUTO_CONTINUE_VERIFY_MS, verify)
            # audit trail: on the card, and on the workspace board. The board
            # write goes through the same serialized append the log_activity
            # tool uses, so it can't interleave with an agent's own note.
            agent.notice("[plan limit reset; auto-continued]")
            ws = self.manager.workspace_of(agent.id)
            if ws is not None and ws.board is not None:
                ws.board.append_activity(
                    "AI Hive",
                    f"auto-continued {agent.spec.name} after the plan limit "
                    f"reset")

        QTimer.singleShot(AUTO_CONTINUE_ESC_MS, send)

    # ---------------------------------------------- deferred ("send later") ---

    def _schedule_audit(self, message: str) -> None:
        """Forensic line in session.log for the deferred-message path. Same
        reasoning as `_limit_audit`: this fires while nobody is watching, so a
        message that did not go out has to leave a trace saying why."""
        try:
            self.store.audit(f"SCHEDULE {message}")
        except Exception:
            pass    # forensics must never break the feature they observe

    def _card_for_agent(self, agent_id: str):
        for page in self._pages.values():
            card = page.card_for(agent_id)
            if card is not None:
                return card
        return None

    def _on_schedule_message(self, agent_id: str, prefill: str = "") -> None:
        """Open the countdown composer for an agent.

        `prefill` is what the terminal had typed when Ctrl+Shift+Enter was
        pressed. It is only INFERRED from the painted input box, so the dialog
        shows it for confirmation rather than scheduling it blind - and the
        child's input box is cleared only once something is actually queued, so
        a cancelled dialog leaves the user's typing exactly where it was.
        """
        agent = self.manager.resolve_agent(agent_id)
        if agent is None:
            return
        dialog = ScheduleMessageDialog(agent, parent=self, prefill=prefill)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = dialog.result_message() if accepted else None
        if result is None:
            self._sync_schedule_timer()   # cancels made in the manage list
            return
        text, due_ts = result
        if dialog.editing_id:
            # the tick could have delivered or dropped it while the dialog was
            # open, so this can legitimately no-op rather than error
            if agent.reschedule(dialog.editing_id, text, due_ts):
                self._schedule_audit(
                    f"RESCHEDULED agent={agent.spec.name} "
                    f"at={scheduled_send.format_clock(due_ts)} "
                    f"in={scheduled_send.format_countdown(due_ts - time.time())}")
            self._sync_schedule_timer()
            return
        msg = agent.schedule_message(text, due_ts)
        if msg is None:
            QMessageBox.information(
                self, "Not scheduled",
                f"“{agent.spec.name}” is already holding the maximum number of "
                f"scheduled messages. Cancel one first.")
            return
        if prefill.strip():
            # The text now lives in AI Hive, so leaving a copy in the child's
            # input box would submit it twice the moment the user hits Enter.
            # Double-Escape is Claude Code's clear-prompt gesture (the same one
            # TerminalView uses for Backspace over a Ctrl+A highlight), and
            # `write` is right for it: it stamps _last_input_ts, so the pty's
            # echo of the Escapes is not mistaken for the agent working.
            agent.write("\x1b\x1b")
        self._schedule_audit(
            f"QUEUED agent={agent.spec.name} "
            f"at={scheduled_send.format_clock(due_ts)} "
            f"in={scheduled_send.format_countdown(due_ts - time.time())}")
        self._sync_schedule_timer()

    def _schedule_pending(self) -> bool:
        return any(a.pending_scheduled() for a in self.manager.all_agents())

    def _sync_schedule_timer(self) -> None:
        """Run the countdown tick only while something is actually queued.

        CRITICAL: only touch the timer when the desired state DIFFERS from the
        current one. `QTimer.start()` RESTARTS a running timer, and this is
        called from `workspaceStatsChanged`, which fires every couple of seconds
        per busy agent - so an unconditional start would reset the countdown
        forever and the tick would never fire at all. Exactly the trap
        `_retune_usage_poll` documents.
        """
        want = self._schedule_pending() and not self._closing
        if want and not self._schedule_timer.isActive():
            self._schedule_timer.start()
        elif not want and self._schedule_timer.isActive():
            self._schedule_timer.stop()

    def _tick_schedules(self) -> None:
        """Repaint every countdown, and deliver anything that has come due.

        The repaint half touches ONLY the card's label - no model state, no
        `dirty`. A per-second tick wired to the session file would rewrite it
        3600 times an hour, the same rule `activity_changed` and the plan-usage
        reading follow.
        """
        if self._closing or not self._ready:
            return
        now = time.time()
        for agent in self.manager.all_agents():
            if not agent.scheduled_messages():
                continue
            card = self._card_for_agent(agent.id)
            if card is not None:
                card.refresh_schedule()
            for msg in agent.due_scheduled(now):
                self._deliver_scheduled(agent, msg, now)
        self._sync_schedule_timer()

    def _deliver_scheduled(self, agent, msg, now: float) -> None:
        """Type one due message into its agent, or decide it can't be.

        Delivery is `nudge`, NEVER `deliver_task`: the user pressed a deferred
        Enter, they did not assign a task, so `current_task`, the assignment
        state and the role must all be left exactly as they are. `nudge` also
        does not stamp `_last_input_ts`, so the work it kicks off still pulses
        the sidebar rather than reading as the user's own typing.

        A refusal is not a failure. The usual reasons are temporary (the agent
        is stopped, its TUI is still booting, or it is parked on a plan-limit
        menu where the text would land in the menu instead of the prompt), so
        the message stays queued and is retried on the next tick. Only after
        `SCHEDULE_GIVE_UP_S` is it given up on - and even then it is kept,
        MISSED, rather than dropped: the user chose a moment and it did not
        happen, which they need to be able to see.
        """
        blocked = agent.is_limit_blocked()
        if not blocked and agent.nudge(msg.text):
            agent.mark_scheduled_sent(msg.id)
            agent.notice("[scheduled message sent]")
            self._schedule_audit(f"SENT agent={agent.spec.name} "
                                 f"late={int(now - msg.due_ts)}s")
            return
        agent.note_scheduled_attempt(msg.id)
        if now - msg.due_ts < SCHEDULE_GIVE_UP_S:
            return
        if agent.mark_scheduled_missed(msg.id):
            why = ("the agent is stopped" if not agent.is_running()
                   else "the plan limit has it parked" if blocked
                   else "its prompt never became ready")
            agent.notice(f"[scheduled message NOT sent: {why}]")
            self._schedule_audit(f"MISSED agent={agent.spec.name} "
                                 f"tries={msg.attempts} ({why})")

    def _on_sound_toggled(self, enabled: bool) -> None:
        """User flipped the top-bar chime toggle. Persist the preference (via
        the debounced save) so it survives a restart."""
        self._sound_enabled = bool(enabled)
        self._schedule_save()

    def _restore_ui_state(self, session: dict) -> None:
        ui = session.get("ui", {})
        # restore the saved skin FIRST so the stylesheet below is built once
        # with the right palette (and the dropdown reflects it silently)
        tid = ui.get("theme")
        if tid in ui_theme.THEMES and tid != ui_theme.ACTIVE_THEME.id:
            from PySide6.QtWidgets import QApplication
            ui_theme.apply_theme(tid)
            self._theme_id = tid
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(ui_theme.build_qss(
                    app.property("chromeFamily") or "Segoe UI",
                    ui_theme.CONSOLE_FONT_PX))
        self.top_bar.set_theme(self._theme_id)
        # notification chime preference (default ON if never saved)
        self._sound_enabled = bool(ui.get("sound_enabled", True))
        self.top_bar.set_sound_enabled(self._sound_enabled)
        # plan-usage readout preference (default ON if never saved). A new key
        # inside "ui" read with a default is backward compatible, so this needs
        # no SESSION_VERSION bump — same as sound_enabled before it.
        self._usage_visible = bool(ui.get("usage_visible", True))
        self.top_bar.set_usage_visible(self._usage_visible)
        self._auto_continue = bool(ui.get("auto_continue", True))
        self.top_bar.set_auto_continue(self._auto_continue)
        self._startup_recovery = bool(ui.get("startup_recovery", True))
        self.top_bar.set_startup_recovery(self._startup_recovery)
        win = ui.get("window", {})
        if win.get("w") and win.get("h"):
            self.resize(int(win["w"]), int(win["h"]))
        if win.get("maximized"):
            self.setWindowState(Qt.WindowState.WindowMaximized)
        # restore sidebar width / collapsed state onto the splitter
        w = int(ui.get("sidebar_width", 0) or 0)
        if SIDEBAR_MIN <= w <= SIDEBAR_MAX:
            self._sidebar_saved_width = w
        if ui.get("sidebar_collapsed"):
            self.body_split.setSizes([0, 1000])
        else:
            self.body_split.setSizes([self._sidebar_saved_width, 1000])
        # restore the global console font (pages already built → refresh them)
        px = int(ui.get("console_font_px", 0) or 0)
        if 7 <= px <= 40 and px != ui_theme.CONSOLE_FONT_PX:
            from PySide6.QtWidgets import QApplication
            ui_theme.CONSOLE_FONT_PX = px
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(ui_theme.build_qss(
                    app.property("chromeFamily") or "Segoe UI", px))
            for page in self._pages.values():
                for card in page.cards:
                    if card.is_pty and not card.agent.spec.font_px:
                        card.terminal.set_font_size(px)
                    elif not card.is_pty and card.agent.spec.font_px:
                        card.reapply_font()
        # re-open the file trees that were open last session (sidebar rows are
        # already built by _adopt_existing_model; unknown ids are dropped)
        self.sidebar.set_open_file_trees(ui.get("file_trees_open", []))

    # ------------------------------------------------------- model events ---

    def _on_workspace_added(self, ws: Workspace) -> None:
        if ws.id in self._pages:
            return
        page = WorkspacePage(ws)
        page.closeRequested.connect(
            lambda agent_id, ws_id=ws.id:
            self.manager.remove_terminal(ws_id, agent_id))
        page.focusGained.connect(self._set_focused_card)
        page.addRequested.connect(self._on_add_terminal_clicked)  # empty slot
        page.layoutChosen.connect(self.manager.set_layout)        # persist
        page.openFolderRequested.connect(self._open_workspace_folder)
        page.changePathRequested.connect(self._change_workspace_folder)
        page.activityToggled.connect(self._toggle_activity)
        page.mapRequested.connect(self._open_agent_map)
        page.reassignRequested.connect(self._on_reassign_agent)
        page.scheduleRequested.connect(self._on_schedule_message)
        page.fileActivated.connect(self._reveal_file_in_tree)
        page.reorderCommitted.connect(self.manager.reorder_agents)
        self._pages[ws.id] = page
        self.stack.addWidget(page)
        self.sidebar.add_row(ws.id, ws.name, os.path.basename(ws.project_path)
                             or ws.project_path)
        self.sidebar.set_stats(ws.id, self.manager.workspace_stats(ws.id))

    def _on_workspace_removed(self, ws_id: str) -> None:
        if (self._map_window is not None
                and getattr(self._map_window._workspace, "id", None) == ws_id):
            self._map_window.close()   # don't keep mapping a deleted workspace
        page = self._pages.pop(ws_id, None)
        if page is not None:
            for card in list(page.cards):
                if card is self._focused_card:
                    self._focused_card = None
                card.detach()
            self.stack.removeWidget(page)
            page.deleteLater()
        self.sidebar.remove_row(ws_id)
        if not self.manager.workspaces and not self._closing:
            ws = self.manager.create_workspace("Workspace 1")
            self.manager.set_active(ws.id)

    def _on_workspace_renamed(self, ws_id: str, name: str) -> None:
        self.sidebar.set_row_name(ws_id, name)
        if ws_id == self.manager.active_id:
            self.top_bar.set_breadcrumb(name)

    def _on_active_changed(self, ws_id: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            self.stack.setCurrentWidget(page)
        self.sidebar.set_active_row(ws_id)
        ws = self.manager.workspace(ws_id)
        self.top_bar.set_breadcrumb(ws.name if ws else "")
        # keep the activity panel following the active workspace
        if self.activity_panel.is_open() and ws is not None:
            self.activity_panel.set_workspace(ws)
        # the agent/file map (if open) tracks the active workspace too
        if (self._map_window is not None and self._map_window.isVisible()
                and ws is not None):
            self._map_window.set_workspace(ws)
        self._sync_activity_buttons()

    # ------------------------------------------------------- activity panel ---

    def _sync_activity_buttons(self) -> None:
        # one shared panel, one button per page: only the active page's button
        # is lit, and only when the panel is actually open
        open_id = self.manager.active_id if self.activity_panel.is_open() else None
        for wid, page in self._pages.items():
            page.activity_btn.setChecked(wid == open_id)

    def _toggle_activity(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        if self.activity_panel.is_open():
            self.activity_panel.conceal()
            self._activity_timer.stop()
        else:
            self.activity_panel.set_workspace(ws)
            self.activity_panel.reveal()
            self._activity_timer.start()
        self._sync_activity_buttons()

    # --------------------------------------------------------- agent map ---

    def _open_agent_map(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        if self._map_window is None:   # lazy, parented so theme switch repaints it
            self._map_window = AgentFileMapWindow(self)
            self._map_window.agentActivated.connect(self._focus_agent_from_map)
        self._map_window.set_workspace(ws)
        self._map_window.show()
        self._map_window.raise_()
        self._map_window.activateWindow()

    def _focus_agent_from_map(self, ws_id: str, agent_id: str) -> None:
        self._reveal_agent(ws_id, agent_id)

    def _reveal_agent(self, ws_id: str, agent_id: str) -> None:
        """The single 'click an agent -> reveal its card' primitive (used by the
        Agent/File Map and the sidebar agent dropdown): switch to the agent's
        workspace, scroll its terminal card into view, and focus it."""
        if not agent_id:
            return
        self.manager.set_active(ws_id)
        page = self._pages.get(ws_id)
        if page is None:
            return
        card = page.card_for(agent_id)
        if card is None:
            return
        page.scroll.ensureWidgetVisible(card)
        target = card.terminal or card
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.raise_()
        self.activateWindow()

    def _agents_for_ws(self, ws_id: str) -> list:
        """Live agents for a workspace — the sidebar's provider for its inline
        agent expansion (clicking the count badge lists them under the row)."""
        ws = self.manager.workspace(ws_id)
        return list(ws.agents) if ws is not None else []

    def _project_path_for_ws(self, ws_id: str) -> str:
        """Root folder for a workspace — the sidebar file explorer's provider
        (it scandirs this to build the inline tree)."""
        ws = self.manager.workspace(ws_id)
        return ws.project_path if ws is not None else ""

    def _on_sidebar_file_activated(self, ws_id: str, path: str) -> None:
        """A file row was clicked in the sidebar tree: open it with the OS
        default program and highlight it in place."""
        fsopen.open_path(path)
        self.sidebar.reveal_file(ws_id, path)

    def _reveal_file_in_tree(self, ws_id: str, path: str) -> None:
        """A file path was Ctrl+clicked in a conversation (already opened by the
        terminal). If that workspace's file tree is open, scroll to + highlight
        the file — the file<->conversation bridge. No-ops when the tree is
        closed (reveal only when the explorer is showing)."""
        self.sidebar.reveal_file(ws_id, path)

    def _on_stats_for_activity(self, ws_id: str, _stats: dict) -> None:
        # cheap refresh only (roster + log); the blocking git scan stays on the
        # slow poll timer, never on this high-frequency status-change path
        if self.activity_panel.is_open() and ws_id == self.manager.active_id:
            ws = self.manager.workspace(ws_id)
            if ws is not None:
                self.activity_panel.refresh(ws, with_git=False)

    def _refresh_activity(self) -> None:
        if not self.activity_panel.is_open():
            return
        ws = self.manager.workspace(self.manager.active_id)
        if ws is not None:
            self.activity_panel.refresh(ws, with_git=True)  # slow path

    def _on_layout_changed(self, ws_id: str, layout: str) -> None:
        # keep the page's grid in sync when set_layout is driven from the model
        # (the page's own GridButton already applied it; set_layout is a no-op
        # if unchanged, so this can't loop)
        page = self._pages.get(ws_id)
        if page is not None and page._layout != layout:
            page.set_layout(layout)

    def _on_task_edited(self, agent_id: str, task: str) -> None:
        ws = self.manager.workspace(self.manager.active_id)
        if ws is None:
            return
        agent = self.manager.agent(ws.id, agent_id)
        if agent is not None:
            agent.set_task(task)

    def _on_reassign_agent(self, agent_id: str) -> None:
        from PySide6.QtWidgets import QInputDialog
        agent = self.manager.resolve_agent(agent_id)
        if agent is None:
            return
        task, ok = QInputDialog.getMultiLineText(
            self, "Assign task",
            f"Task for “{agent.spec.name}” (its role/model adapt to the task):",
            agent.current_task)
        if ok and task.strip():
            self.manager.reassign_agent(agent_id, task.strip())

    def _on_terminal_added(self, ws_id: str, agent) -> None:
        page = self._pages.get(ws_id)
        if page is not None and page.card_for(agent.id) is None:
            page.add_agent(agent)

    def _on_terminal_removed(self, ws_id: str, agent_id: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            card = page.card_for(agent_id)
            if card is not None and card is self._focused_card:
                self._focused_card = None
            page.remove_agent(agent_id)

    def _on_workspace_path_changed(self, ws_id: str, path: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            page.set_path_text(path)
        self.sidebar.set_row_folder(ws_id, os.path.basename(path) or path)
        # the file explorer re-roots at the new folder (drop stale sub-expansion)
        self.sidebar.reset_file_tree(ws_id)
        for card in (page.cards if page is not None else []):
            if card.is_pty:
                card.terminal.set_base_dir(getattr(card.agent.spec, "cwd", "")
                                           or path)
        # the board moved folders — repoint the panel if it's showing this ws
        if self.activity_panel.is_open() and ws_id == self.manager.active_id:
            ws = self.manager.workspace(ws_id)
            if ws is not None:
                self.activity_panel.set_workspace(ws)

    # --------------------------------------------------------- folder access ---

    def _open_workspace_folder(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws and os.path.isdir(ws.project_path):
            fsopen.open_path(ws.project_path)
        elif ws:
            QMessageBox.warning(self, "AI Hive",
                                f"Folder not found:\n{ws.project_path}")

    def _change_workspace_folder(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        start = ws.project_path if os.path.isdir(ws.project_path) \
            else os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, "Choose the workspace folder", start)
        if path:
            self.manager.set_workspace_path(ws_id, path)

    # --------------------------------------------------------- user flows ---

    def _on_add_workspace_clicked(self) -> None:
        start_dir = os.path.expanduser("~")
        active = self.manager.workspace(self.manager.active_id)
        if active and os.path.isdir(active.project_path):
            start_dir = active.project_path
        path = QFileDialog.getExistingDirectory(
            self, "Choose the project folder for the new workspace", start_dir)
        if not path:
            return  # mandatory: cancel aborts creation
        name = os.path.basename(os.path.normpath(path)) or "Workspace"
        ws = self.manager.create_workspace(name, path)
        self.manager.set_active(ws.id)
        self.sidebar.begin_rename(ws.id)

    def _on_add_terminal_clicked(self, ws_id: str = "") -> None:
        # ws_id lets an empty grid slot request an agent for its own workspace;
        # default is the active workspace.
        ws = self.manager.workspace(ws_id) if ws_id else \
            self.manager.workspace(self.manager.active_id)
        if ws is None:
            return
        # per-workspace numbering: each workspace counts Agent 1, 2, 3…
        # busy_ids: conversations a running agent already holds — never offer
        # them for resume (two agents on one transcript race/truncate it)
        busy_ids = {a.spec.session_id for a in ws.agents
                    if a.is_running() and a.spec.session_id}
        dialog = AddTerminalDialog(self.manager.next_agent_name(ws.id), self,
                                   cwd=ws.project_path, busy_ids=busy_ids)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dialog.result_spec(cwd=ws.project_path)
        if self.manager.add_terminal(ws.id, spec) is None:
            QMessageBox.warning(self, "AI Hive",
                                "This workspace is at its agent limit.")

    def _confirm_delete_workspace(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        page = self._pages.get(ws_id)
        if ws is None:
            return
        running = page.running_count() if page else 0
        if running:
            answer = QMessageBox.question(
                self, "Delete workspace",
                f"Workspace “{ws.name}” has {running} running terminal(s).\n"
                "Deleting it will terminate them. Delete anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.manager.remove_workspace(ws_id)

    def _change_global_font(self, delta: int) -> None:
        from PySide6.QtWidgets import QApplication
        px = max(7, min(40, ui_theme.CONSOLE_FONT_PX + delta))
        if px == ui_theme.CONSOLE_FONT_PX:
            return
        ui_theme.CONSOLE_FONT_PX = px
        app = QApplication.instance()
        chrome = app.property("chromeFamily") or "Segoe UI"
        app.setStyleSheet(ui_theme.build_qss(chrome, px))  # affects chrome + line consoles
        for page in self._pages.values():
            for card in page.cards:
                override = card.agent.spec.font_px
                if card.is_pty:
                    if not override:  # follow the new global default
                        card.terminal.set_font_size(px)
                elif override:  # re-assert the per-agent line-mode override
                    card.reapply_font()
        self._schedule_save()

    def _change_theme(self, theme_id: str) -> None:
        """Swap the active skin live: repaint chrome, terminals, and ornaments,
        then persist. Safe to call with the current id (no-op-ish)."""
        from PySide6.QtWidgets import QApplication
        ui_theme.apply_theme(theme_id)
        self._theme_id = theme_id
        self.top_bar.set_theme(theme_id)  # keep the dropdown in sync (no re-emit)
        app = QApplication.instance()
        chrome = app.property("chromeFamily") or "Segoe UI"
        app.setStyleSheet(ui_theme.build_qss(chrome, ui_theme.CONSOLE_FONT_PX))
        # QSS handles chrome; repolish forces re-eval of dynamic-property rules
        # ([state=…]/[active=…]) that a plain setStyleSheet doesn't rescan
        ui_theme.repolish(self)
        for w in self.findChildren(QWidget):
            ui_theme.repolish(w)
        # terminals paint themselves (pyte reads Palette/ANSI at paint time) —
        # nudge every view + ornament to redraw with the new palette
        for page in self._pages.values():
            for card in page.cards:
                if card.is_pty and card.terminal is not None:
                    card.terminal.update()
                card.update()
        self._apply_page_border()   # show/hide the foliate frame for this skin
        self._schedule_save()

    def _apply_page_border(self) -> None:
        """Seat content inside the illuminated border (open root margins) and
        show the overlay — only for an illuminated theme; a no-op frame
        otherwise, so other skins fill the window edge-to-edge."""
        if not hasattr(self, "_page_border"):
            return
        lit = bool(getattr(ui_theme.ACTIVE_THEME, "ornament", ""))
        m = (PageBorder.INSET + PageBorder.THICK + 6) if lit else 0
        self._root_layout.setContentsMargins(m, m, m, m)
        self._page_border.setGeometry(self._central.rect())
        self._page_border.setVisible(lit)
        self._page_border.raise_()
        self._page_border.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_page_border") and self._page_border.isVisible():
            self._page_border.setGeometry(self._central.rect())

    def _set_focused_card(self, card: TerminalCard) -> None:
        if card is self._focused_card:
            return
        if self._focused_card is not None:
            self._focused_card.set_focused(False)
        self._focused_card = card
        card.set_focused(True)

    def autostart_active_workspace(self) -> None:
        """Restore what was running: every agent that was RUNNING at save
        time — in EVERY workspace — starts (and resumes) automatically, so
        opening the app brings the whole hive back without manual steps.
        Agents that were stopped, and one-shot scripts, never re-execute as
        a side effect of launch. (Name kept for the smoke-test API.)"""
        for ws in self.manager.workspaces:
            for agent in ws.agents:
                if agent.autostart_on_restore and not agent.is_running():
                    agent.start()

    # -------------------------------------------------------- persistence ---

    def _schedule_save(self) -> None:
        if not self._closing:
            self._save_timer.start()

    def _save_now(self) -> None:
        """Immediate (non-debounced) save for structural changes, so a kill
        can't lose a just-created agent/workspace."""
        if self._closing:
            # closeEvent already wrote the authoritative snapshot; teardown
            # mutations must not overwrite it. But NEVER do this silently: a
            # window stuck in _closing (a failed-quit zombie) would otherwise
            # accept new work forever while logging nothing — the exact
            # invisible-loss incident this whole subsystem exists to prevent.
            self.store.audit("SAVE-SKIP closing")
            return
        if not self._ready:
            return  # mid-load; the post-load state is saved once _ready flips
        self._save_session()

    def _session_payload(self) -> dict:
        data = self.manager.to_session_dict()
        if self.isMaximized():  # remember the restore-down size, not the
            g = self.normalGeometry()  # screen-sized maximized geometry
            w, h = g.width(), g.height()
        else:
            w, h = self.width(), self.height()
        sizes = self.body_split.sizes()
        data["ui"] = {
            "sidebar_collapsed": sizes[0] == 0,
            "sidebar_width": self._sidebar_saved_width,
            "console_font_px": ui_theme.CONSOLE_FONT_PX,
            "theme": self._theme_id,
            "sound_enabled": self._sound_enabled,
            "usage_visible": self._usage_visible,
            "auto_continue": self._auto_continue,
            "startup_recovery": self._startup_recovery,
            "window": {"w": w, "h": h, "maximized": self.isMaximized()},
            # which workspaces have their inline file tree open (per-folder
            # expansion + highlight are transient, not persisted)
            "file_trees_open": self.sidebar.open_file_trees(),
        }
        return data

    def _save_session(self) -> None:
        # Build the payload inside the try: to_session_dict / geometry reads
        # run BEFORE store.save(), so an exception here would bypass save()'s
        # own SAVE-FAIL logging and vanish without a trace.
        try:
            payload = self._session_payload()
        except Exception as e:  # noqa: BLE001 - a lost save must never be silent
            self.store.audit(f"SAVE-FAIL payload {type(e).__name__}: {e}")
            return
        if self.store.save(payload):
            # remember what actually reached disk so the heartbeat can skip
            # redundant writes and only re-save genuine divergence
            import json
            self._last_saved_json = json.dumps(payload, sort_keys=True)

    def _heartbeat_save(self) -> None:
        """Safety-net autosave: persistence is otherwise edge-triggered on
        signals + graceful close, so a single missed/suppressed save (or a
        future refactor that drops a `dirty` emit) would silently diverge from
        disk until close — and a hard kill would lose it. This bounds
        worst-case loss to one interval by re-saving whenever the live state
        no longer matches what last reached disk."""
        if self._closing or not self._ready:
            return
        try:
            payload = self._session_payload()
            import json
            current = json.dumps(payload, sort_keys=True)
        except Exception as e:  # noqa: BLE001
            self.store.audit(f"SAVE-FAIL payload {type(e).__name__}: {e}")
            return
        if current != getattr(self, "_last_saved_json", None):
            if self.store.save(payload):
                self._last_saved_json = current

    def _sync_live_sessions(self) -> None:
        """Reconcile pinned session ids with the transcripts agents are really
        writing, then persist any change (debounced). This is what makes the
        RIGHT conversation come back on reopen even after the user switched
        conversations inside a terminal."""
        if self._closing or not self._ready:
            return
        # a pin change emits `dirty`, which schedules the (debounced) save;
        # when nothing drifted this is a cheap no-op that never touches disk
        for agent_id, old, new in self.manager.sync_live_sessions():
            self.store.audit(f"SESSION-SYNC agent={agent_id} {old} -> {new}")
        # same tick: refresh each agent's summary from Claude's live AI title
        # (transient; drives the card header + sidebar agent list, never saves)
        self.manager.refresh_ai_titles()

    # -------------------------------------------------------------- close ---

    def closeEvent(self, event) -> None:
        self._closing = True
        self._save_timer.stop()
        self._heartbeat_timer.stop()
        self._session_sync_timer.stop()
        self._prompt_sync_timer.stop()
        self._model_sync_timer.stop()
        self._limit_watch_timer.stop()
        self._usage_timer.stop()
        self._usage_tick_timer.stop()
        self._usage_reset_timer.stop()
        # capture any last-moment conversation switch BEFORE the final save, so
        # reopen resumes what was actually on screen — not a stale pin. Agents
        # are still alive here (processes are killed further down), so their
        # transcripts on disk are current.
        for agent_id, old, new in self.manager.sync_live_sessions():
            self.store.audit(f"SESSION-SYNC agent={agent_id} {old} -> {new}")
        # same reason, for the OTHER thing a user changes inside the terminal:
        # a Shift+Tab in the last second before closing must reopen in that mode
        self.manager.refresh_model_effort()
        self._save_session()  # persist FIRST: teardown can never lose state
        from .. import transcripts
        agents = self.manager.all_agents()
        transcripts.backup_for_agents(  # snapshot the day's conversations
            agents, str(self.store.path.parent / "transcripts"))
        # ...and the SCREENS, so a card left stopped reopens showing its
        # conversation rather than a black rectangle. Must run while the
        # agents are still alive (dispose() below drops their pty buffers),
        # and after sync_live_sessions above so a last-moment conversation
        # switch is keyed on the pin that will actually be restored.
        self._snapshot_screens(agents)
        self.bridge.stop()    # stop the RPC server + remove the endpoint file
        for page in self._pages.values():
            for card in list(page.cards):
                card.detach()
        for agent in self.manager.all_agents():
            agent.dispose()  # Job Object tree kill: instant, no blocking waits
        event.accept()
        if getattr(self, "quit_on_close", False):
            # a closed AI Hive must NEVER linger as a zombie process: a
            # survivor keeps the single-instance mutex AND, with _closing
            # latched, can never save again — anything the user does in a
            # half-dead window silently never reaches disk (this happened:
            # an instance logged nothing for over an hour of real use).
            # Set only by main.py; the offscreen test suite opens and closes
            # many windows in one process and must not have its app quit.
            from PySide6.QtWidgets import QApplication
            QTimer.singleShot(0, QApplication.instance().quit)
