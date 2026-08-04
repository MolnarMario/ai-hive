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
                               QMessageBox, QPushButton, QSplitter,
                               QStackedWidget, QToolButton, QVBoxLayout,
                               QWidget)

from .. import __version__
from .. import chime
from .. import claude_usage
from .. import fsopen
from .. import providers
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
from .terminal_card import TerminalCard
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

# how often to re-read the Claude account's plan usage from the API. One small
# HTTPS GET; a minute is well inside the resolution of a 5-hour window.
USAGE_POLL_MS = 60000
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
# What gets typed. Short on purpose: the agent still holds the whole
# conversation, so it needs a go-ahead, not a restatement of the work.
AUTO_CONTINUE_TEXT = "Continue"
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
# A resume can land while the window is still shut, so one attempt is not
# enough — but it must not become a Continue every minute forever either.
LIMIT_RETRY_S = 300
LIMIT_MAX_TRIES = 4
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
STARTUP_RECOVERY_MAX_AGE_S = 36 * 3600

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
        # a glance. Glyph carries the state, tooltip carries the meaning.
        self._startup_recovery = True
        self._auto_continue = True
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
        lay.addSpacing(4)
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
            "Notification chime: ON — click to mute" if self._sound_on
            else "Notification chime: OFF — click to enable")

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
        # off, so "will my work resume by itself?" is answerable at a glance.
        # NOT a power symbol on the first one — that reads as "shut down".
        self.recover_btn.setCheckable(True)
        self.recover_btn.setChecked(self._startup_recovery)
        self.recover_btn.setText("⏯")
        self.recover_btn.setToolTip(
            "Recover at startup: ON — when AI Hive opens, agents whose work "
            "stopped because the plan limit ran out are continued "
            "automatically.\nClick to turn off."
            if self._startup_recovery else
            "Recover at startup: OFF — agents left stuck on a spent plan "
            "limit stay stopped when AI Hive opens.\nClick to turn on.")
        self.resume_btn.setCheckable(True)
        self.resume_btn.setChecked(self._auto_continue)
        self.resume_btn.setText("⏰")
        self.resume_btn.setToolTip(
            "Resume on limit reset: ON — while AI Hive is running, agents cut "
            "off mid-work by the plan limit are continued the moment the "
            "limit resets.\nClick to turn off."
            if self._auto_continue else
            "Resume on limit reset: OFF — agents cut off by the plan limit "
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
        """Show/hide both recovery toggles. They act only on Claude agents cut
        off by a plan limit, so with no Claude login there is nothing for them
        to do — hide them with the readout rather than offer dead switches."""
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
            "Which permission mode this Claude agent starts in — the same modes "
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
            "Full terminal (interactive — TUIs, colors, Ctrl+C)", self)
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
            status = "✓ detected" if detected else "⚠ CLI not detected — install it or edit the command"
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
                "Ultracode (activate manually in terminal — model-dependent)", None)
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

        # ---- Claude plan usage (top-bar readout + limit-reached edges) ----
        # PURELY TRANSIENT: a reading refreshes the badge and may emit the
        # plan-limit edges, but it must NEVER mark the session dirty — the same
        # rule as activity_changed/waiting_changed. This polls every minute
        # forever; wiring it to a save would rewrite session.json 60x an hour.
        self._usage = None            # latest claude_usage.Usage
        self._usage_visible = True    # user preference (persisted)
        self._usage_inflight = False  # one request at a time, never stack
        self._plan_blocked = False    # edge state for planLimitReached/Cleared
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
        self._limit_watch_timer.start()

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

    def _on_usage_refresh(self) -> None:
        """The user clicked the readout. Clear any 429 backoff first: they are
        asking now, and leaving the timer parked at sixteen minutes would make
        a successful manual refresh look like it fixed nothing when the next
        automatic poll failed to arrive."""
        self._usage_backoff = 0
        self._usage_timer.setInterval(USAGE_POLL_MS)
        if self._usage_timer.isActive():
            self._usage_timer.start()      # restart the interval from now
        self._poll_usage()

    def _on_usage_ready(self, reading) -> None:
        self._usage_inflight = False
        if self._closing:
            return
        if reading is not None and reading.ok:
            self._usage_backoff = 0
            self._usage_timer.setInterval(USAGE_POLL_MS)
            self._apply_usage(reading)
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
            self._usage_timer.setInterval(USAGE_POLL_MS * (2 ** self._usage_backoff))
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
        """Re-render the countdown from the clock alone — no network."""
        self.top_bar.usage_badge.tick()

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
        now = time.time()
        armed = 0
        for agent in self.manager.all_agents():
            spec = agent.spec
            if spec.provider != "claude" or not agent.is_pty:
                continue
            # A card the user left stopped stays stopped: this resumes work, it
            # does not launch processes they didn't ask for (and each one would
            # spend quota).
            if not agent.is_running() or agent.is_limit_blocked():
                continue
            cut_off, written_at, resets_at = transcripts.ended_on_limit(
                spec.cwd, spec.session_id)
            # The gate the user asked for: no stoppage message, no action.
            if not cut_off or not written_at:
                continue
            if now - written_at > STARTUP_RECOVERY_MAX_AGE_S:
                self._limit_audit(f"STARTUP-SKIP agent={spec.name} (stale, "
                                  f"{int((now - written_at) / 3600)}h old)")
                continue
            agent.mark_limit_blocked(resets_at or None, from_startup=True)
            armed += 1
        if armed:
            self._limit_audit(f"STARTUP-SCAN armed={armed}")
        return armed

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

        def due(a):
            at = a.limit_resets_at()
            if at is not None:
                return at <= now
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
                   and (due is None or due(a))]
        for i, agent in enumerate(blocked):
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
            return
        # An agent recovered at startup may still be booting its TUI. Don't
        # poke a launching terminal with stray Esc keys every minute — just
        # wait. `nudge` would refuse anyway, and a refusal costs no attempt, so
        # the watchdog picks it up as soon as the prompt is live.
        if not agent.prompt_ready():
            self._limit_audit(f"WAIT agent={agent.spec.name} (TUI not ready)")
            return
        agent.write("\x1b")

        def send():
            if self._closing or not agent.is_running():
                return
            if not agent.nudge(AUTO_CONTINUE_TEXT):
                self._limit_audit(f"NUDGE-SKIP agent={agent.spec.name} "
                                  f"(prompt not ready)")
                return
            agent.note_limit_attempt()
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
                    return
                # keep the latch so the watchdog tries again
                agent.mark_limit_blocked(agent.limit_resets_at(),
                                         from_startup=agent.limit_from_startup())
                self._limit_audit(
                    f"STILL-BLOCKED agent={agent.spec.name} "
                    f"try={agent.limit_attempts()} "
                    f"(menu_gone={menu_gone} transcript_stuck={stuck})")
            QTimer.singleShot(AUTO_CONTINUE_VERIFY_MS, verify)
            # audit trail: on the card, and on the workspace board. The board
            # write goes through the same serialized append the log_activity
            # tool uses, so it can't interleave with an agent's own note.
            agent.notice("— plan limit reset; auto-continued —")
            ws = self.manager.workspace_of(agent.id)
            if ws is not None and ws.board is not None:
                ws.board.append_activity(
                    "AI Hive",
                    f"auto-continued {agent.spec.name} after the plan limit "
                    f"reset")

        QTimer.singleShot(AUTO_CONTINUE_ESC_MS, send)

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
        self._save_session()  # persist FIRST: teardown can never lose state
        from .. import transcripts  # snapshot the day's conversations
        transcripts.backup_for_agents(
            self.manager.all_agents(),
            str(self.store.path.parent / "transcripts"))
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
