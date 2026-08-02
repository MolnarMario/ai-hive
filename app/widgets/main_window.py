"""MainWindow: the shell — top bar, sidebar, page stack, and ALL wiring.

This is the only module that imports both the model and the view layers.
Every dialog lives here so the model API stays headless-testable.
"""

import os

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSplitter, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from .. import chime
from .. import fsopen
from .. import providers
from .. import session_hook
from .. import ui_theme
from ..process_worker import (AI_KINDS, PTY_ONLY_KINDS, AgentKind, build_spec)
from ..pty_worker import HAS_CONPTY
from ..session_store import SessionStore
from ..workspace_manager import Workspace, WorkspaceManager
from .. import coordination
from ..orchestrator_bridge import OrchestratorBridge
from .activity_panel import ActivityPanel
from .agent_file_map import AgentFileMapWindow
from .ornaments import LogoRoundel, PageBorder
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
        import time

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
        unless they've muted it. Non-blocking; a silent no-op if unavailable."""
        if self._sound_enabled:
            chime.play()

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
