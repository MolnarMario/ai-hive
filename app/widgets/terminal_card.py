"""TerminalCard: one tiled agent view — header, console, command input.

The card is a pure view over a model-owned TerminalAgent: it renders the
agent's log/output and forwards user input. It never owns the process, so
deleting a card can never kill anything by side effect; teardown goes
through WorkspaceManager. Call detach() before deleting the card so late
agent signals can't fire into a dead widget.
"""

import re

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QDrag, QPainter, QPixmap,
                           QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QToolButton, QVBoxLayout)

from .. import scheduled_send, ui_theme
from ..ansi_parser import AnsiSgrParser, CharStyle
from ..terminal_agent import (STREAM_INPUT, STREAM_SYSTEM, AgentStatus,
                              TerminalAgent)
from ..ui_theme import Palette, repolish
from .ornaments import BootVeil, ElidingLabel

_LINE_BREAKS = re.compile(r"[\r\n]")

# How long the boot veil may cover a launching terminal before it lifts on its
# own. Readiness normally arrives in a second or two; this only exists so a
# child that never emits the ready signal at all (an exotic pty shell) can
# never leave the user looking at a cover instead of their terminal.
BOOT_VEIL_MAX_MS = 25_000

_GLYPH_STATE = {
    AgentStatus.IDLE: "idle",
    AgentStatus.STARTING: "starting",
    AgentStatus.RUNNING: "running",
    AgentStatus.STOPPING: "starting",
    AgentStatus.EXITED_OK: "dead",
    AgentStatus.EXITED_ERR: "dead",
    AgentStatus.CRASHED: "dead",
    AgentStatus.FAILED: "dead",
}

_ENDED = (AgentStatus.EXITED_OK, AgentStatus.EXITED_ERR,
          AgentStatus.CRASHED, AgentStatus.FAILED)

INPUT_STYLE = CharStyle(fg=Palette.INPUT_ECHO, bold=True)
SYSTEM_STYLE = CharStyle(fg=Palette.SYSTEM_MSG, italic=True)

# drag payload for reordering agent cards within a workspace (started by
# _CardHeader, resolved by WorkspacePage's drop handling)
CARD_REORDER_MIME = "application/x-aihive-card-reorder"


def _snippet(text: str, limit: int = 140) -> str:
    """One-line preview of a queued message for a tooltip or a list row."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


class _CardHeader(QFrame):
    """The card's title bar, which doubles as its drag handle and carries the
    card's action menu. A left-drag from EMPTY header space past a small
    threshold starts a reorder drag; the buttons consume their own presses, so
    they never drag, while the labels (name/model/summary/usage) don't consume
    presses, so the whole strip except the buttons is grabbable — exactly the
    area the user asked to drag from. A plain click (no movement) is left
    alone, so double-click-to-rename on the title still works. A RIGHT-click
    opens start/stop/restart/assign: those are rare, deliberate actions, and
    the four buttons they used to occupy were worth more as summary space."""

    _SLOP = 8

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self._card = card
        self._press = None
        # an open-hand over the empty strip hints it's a drag handle; the child
        # buttons/title set their own cursors, so only the grabbable area shows it
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
            event.accept()   # take the implicit grab so we see the moves
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press is not None
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength()
                > self._SLOP):
            self._press = None
            self._card._begin_reorder_drag()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self._card.show_actions_menu(event.globalPos())
        event.accept()


class TerminalCard(QFrame):
    closeRequested = Signal(str)     # agent id
    focusGained = Signal(object)     # self
    reassignRequested = Signal(str)  # agent id (retask a completed/idle agent)
    maximizeRequested = Signal(object)  # self (toggle solo view of this card)
    fileActivated = Signal(str)      # abs path Ctrl+clicked in the conversation
    scheduleRequested = Signal(str, str)  # agent id, text to prefill (may be "")

    def __init__(self, agent: TerminalAgent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.is_pty = agent.is_pty
        self.setObjectName("TerminalCard")
        self.setProperty("focused", False)
        self.setMinimumSize(300, 180)

        # one parser per stream: a partial escape carried from a stdout
        # chunk must never be glued onto the next stderr chunk
        self._parsers: dict[str, AnsiSgrParser] = {}
        self._fmt_cache: dict = {}
        self._cr_pending = False
        self._renaming = False  # inline title-edit in progress
        self._task_full = ""    # untruncated current-task (the label elides it)
        self._pending_replay = ""  # restored screen, re-rendered once at size
        self._overlay_compact = False
        # single-shot backstop for the boot veil (see _begin_boot_veil)
        self._boot_timer = QTimer(self)
        self._boot_timer.setSingleShot(True)
        self._boot_timer.timeout.connect(self._dismiss_boot_veil)
        self._follow = True  # sticky auto-scroll (survives resizes/retiles)
        self._history: list[str] = []
        self._hist_idx = 0
        self._draft = ""
        self.terminal = None
        self.console = None
        self.input = None

        self._build_ui()
        self._wire()
        if self.is_pty:
            replay = self.agent.pty_replay()
            if replay:
                self.terminal.feed(replay)
                # A RESTORED screen (app/screen_snapshot.py) is fed here, in
                # the constructor, which is BEFORE the tiling grid hands the
                # card its real size — and pyte neither reflows on resize nor
                # keeps the lines it drops off the TOP when it shrinks, so the
                # newest part of the conversation is exactly what would
                # vanish. Re-render once at the settled size instead.
                # This is deliberately NOT gated on the agent being stopped.
                # The launch autostart starts agents SYNCHRONOUSLY right after
                # show(), while TerminalView debounces its resize by 120ms, so
                # "is running" is already true by the time the first real size
                # arrives — the gate that used to be here therefore skipped
                # precisely the cards that needed it, and every autostarted
                # agent came back showing a mangled 24-column fragment in the
                # top-left of a full-width terminal until its child finished
                # launching. `_rerender_restored` guards the live case the
                # only way that is actually true: the agent's own buffer.
                self._pending_replay = replay
                self.terminal.sizeChanged.connect(self._rerender_restored)
        else:
            self._replay_log()
        self._on_status(agent.status)
        self._on_assignment(agent.assignment)
        self._on_task()
        self._on_tokens(agent.token_badge())
        self._on_model(agent.model_badge())

    # ----------------------------------------------------------------- ui ---

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = self.header = _CardHeader(self, self)
        header.setObjectName("CardHeader")
        header.setToolTip("Drag to reorder this agent, right-click for actions")
        header.setFixedHeight(38)   # room for the larger 14px glyph buttons
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 0, 6, 0)
        hl.setSpacing(7)

        self.glyph = QLabel("●", header)
        self.glyph.setObjectName("StatusGlyph")
        self.title = QLabel(self.agent.spec.name, header)
        self.title.setObjectName("CardTitle")
        self.title.setToolTip("Double-click to rename")
        self.title.setCursor(Qt.CursorShape.PointingHandCursor)
        # hidden inline editor swapped in on double-click (see start_rename);
        # mirrors the workspace-row rename UX in sidebar.py
        self.title_edit = QLineEdit(self.agent.spec.name, header)
        self.title_edit.setObjectName("CardTitleEdit")
        self.title_edit.hide()
        self.role = QLabel(self.agent.spec.role or "", header)
        self.role.setObjectName("CardRole")
        # what the agent is RUNNING ON right now. The user can change both from
        # inside the terminal (/model, /effort), so this follows the live
        # conversation rather than the launch flags; hidden when unknown, which
        # is every non-AI shell.
        self.model_label = QLabel("", header)
        self.model_label.setObjectName("CardModel")
        self.model_label.hide()
        # "the usage limit stopped this agent" marker. Visible for as long as
        # the cut-off is latched, so an interrupted agent is identifiable at a
        # glance instead of by reading its terminal — and so an auto-continue
        # that is about to happen is visible BEFORE it happens. Transient, like
        # the latch behind it.
        self.limit_mark = QLabel("⏳", header)   # hourglass
        self.limit_mark.setObjectName("CardLimitMark")
        self.limit_mark.hide()
        # "idle, but a background command it started is still running" — same
        # transient-marker treatment as limit_mark above, but clickable: a
        # QToolButton (not a QLabel) so a click hard-kills whatever is still
        # running (see TerminalAgent.kill_bg_shell_extras) without also
        # triggering anything the marker sits inside.
        self.bg_mark = QToolButton(header)
        self.bg_mark.setObjectName("CardBgShell")
        self.bg_mark.setText("⚙")
        self.bg_mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bg_mark.hide()
        # "a message is queued to be typed in at N" — the countdown for a
        # deferred submit (Ctrl+Shift+Enter). Visible for as long as something
        # is held, so a scheduled send is never a surprise: the user can see it
        # coming and click to change or cancel it. Ticked by MainWindow, which
        # updates the LABEL only and never the model.
        self.sched_mark = QToolButton(header)
        self.sched_mark.setObjectName("CardSchedule")
        self.sched_mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sched_mark.hide()
        # one-line summary of what this agent is working on (its current task),
        # so several agents in a workspace are tellable apart at a glance
        # without reading each terminal. It takes every pixel the fixed chrome
        # beside it leaves and elides only what genuinely doesn't fit; the full
        # text is always on hover.
        self.task_summary = ElidingLabel(header)
        self.task_summary.setObjectName("CardTaskSummary")
        # compact context-window usage (e.g. "20% of 1M"), right after the
        # summary snippet — hidden until the transcript reports usage (Claude
        # only). Fixed content, so no stretch: the summary keeps the slack.
        self.token_label = QLabel("", header)
        self.token_label.setObjectName("CardTokens")
        self.token_label.hide()
        hl.addWidget(self.glyph)
        hl.addWidget(self.title)
        hl.addWidget(self.title_edit)
        hl.addWidget(self.role)
        hl.addWidget(self.model_label)
        hl.addSpacing(6)
        hl.addWidget(self.limit_mark)
        hl.addWidget(self.bg_mark)
        hl.addWidget(self.sched_mark)
        hl.addWidget(self.task_summary, 1)  # takes the middle space, elides
        hl.addWidget(self.token_label)

        def tool(text, obj_name, tip):
            b = QToolButton(header)
            b.setText(text)
            b.setObjectName(obj_name)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            hl.addWidget(b)
            return b

        # Only the buttons worth their width live here. Start / Stop / Restart /
        # Assign moved to the header's right-click menu: the terminal itself is
        # how this app is driven (any keystroke wakes a stopped card), so those
        # four were spending ~130px of every header on actions nobody clicks.
        self.btn_font_dec = tool("A−", "CardFontDec", "Smaller font (Ctrl+-)")
        self.btn_font_inc = tool("A+", "CardFontInc", "Larger font (Ctrl+=)")
        # solo/restore this card in the workspace grid — a pure view toggle;
        # never touches sibling processes (see WorkspacePage.toggle_solo)
        self.btn_max = tool("⤢", "CardMaximize", "Maximize (focus this agent)")
        self.btn_close = tool("✕", "CardClose", "Close terminal")

        root.addWidget(header)
        if self.is_pty:
            from .terminal_view import TerminalView
            self.terminal = TerminalView(rows=self.agent.worker.rows,
                                         cols=self.agent.worker.cols,
                                         parent=self,
                                         font_px=self.agent.spec.font_px)
            root.addWidget(self.terminal, 1)
            # a stopped terminal must NEVER read as a dead black screen: a
            # visible banner says so, and any keystroke starts the session.
            # It takes TWO shapes, because the banner is only the whole story
            # when there is nothing else to look at. A card restored with its
            # previous conversation on screen (see app/screen_snapshot.py) gets
            # a slim footer instead: covering that conversation with a centred
            # box is what made a reopened hive read as a wall of dead
            # terminals, which is the thing this was supposed to prevent.
            self.overlay = QLabel(self.terminal)
            self.overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._overlay_compact = False
            self.overlay.hide()
            # ...and a LAUNCHING terminal shows a quiet loader rather than the
            # child's half-drawn boot frame, which at app launch is drawn at
            # the pre-layout width and cannot reflow (see BootVeil).
            self.boot = BootVeil(self.terminal)
        else:
            self.console = QPlainTextEdit(self)
            self.console.setObjectName("Console")
            self.console.setReadOnly(True)
            self.console.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.console.document().setMaximumBlockCount(5000)
            self.console.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.input = QLineEdit(self)
            self.input.setObjectName("CardInput")
            self.input.setPlaceholderText("type a command and press Enter…")
            self.input.setClearButtonEnabled(False)
            root.addWidget(self.console, 1)
            root.addWidget(self.input)
            if self.agent.spec.font_px:  # per-agent override of the QSS default
                self._apply_font(self.agent.spec.font_px)

    def _wire(self) -> None:
        self.agent.status_changed.connect(self._on_status)
        self.agent.assignment_changed.connect(self._on_assignment)
        self.agent.role_changed.connect(self._on_role)
        self.agent.name_changed.connect(self._on_name)
        self.agent.task_changed.connect(self._on_task)
        self.agent.summary_changed.connect(self._on_task)  # incl. live AI title
        self.agent.tokens_changed.connect(self._on_tokens)
        self.agent.model_changed.connect(self._on_model)
        self.agent.limit_blocked_changed.connect(self._on_limit_blocked)
        self._on_limit_blocked(self.agent.is_limit_blocked())
        self.agent.bg_shell_changed.connect(self._on_bg_shell)
        self._on_bg_shell(self.agent.is_bg_shell_busy())
        self.bg_mark.clicked.connect(self.agent.kill_bg_shell_extras)
        self.agent.scheduled_changed.connect(self.refresh_schedule)
        self.sched_mark.clicked.connect(
            lambda: self.scheduleRequested.emit(self.agent.id, ""))
        self.refresh_schedule()
        self.title.installEventFilter(self)        # double-click to rename
        self.title_edit.installEventFilter(self)   # Esc cancels, focus-out commits
        self.title_edit.returnPressed.connect(self._commit_rename)
        self.btn_max.clicked.connect(lambda: self.maximizeRequested.emit(self))
        self.btn_close.clicked.connect(self._on_close_clicked)
        self.btn_font_dec.clicked.connect(lambda: self._font_delta(-1))
        self.btn_font_inc.clicked.connect(lambda: self._font_delta(+1))

        if self.is_pty:
            self.agent.pty_output.connect(self._on_pty_output)
            self.agent.prompt_ready_changed.connect(self._on_prompt_ready)
            self.terminal.keyInput.connect(self._on_key_input)
            self.terminal.sizeChanged.connect(self.agent.resize)
            # relative paths in the output resolve against the agent's cwd, and
            # Ctrl+clicking a file bubbles up so the app can reveal it
            self.terminal.set_base_dir(getattr(self.agent.spec, "cwd", "") or "")
            self.terminal.fileActivated.connect(self.fileActivated)
            # Ctrl+Shift+Enter in the terminal: "send this, but later". The view
            # hands up what is currently typed; the window turns it into the
            # countdown dialog and, only on confirm, clears the input box.
            self.terminal.scheduleRequested.connect(
                lambda text: self.scheduleRequested.emit(self.agent.id, text))
            self.terminal.installEventFilter(self)
            return

        self.agent.output_segment.connect(self._on_segment)
        self.agent.cleared.connect(self._on_cleared)
        self.input.returnPressed.connect(self._submit)
        self.console.installEventFilter(self)
        self.input.installEventFilter(self)
        sb = self.console.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_value)
        sb.rangeChanged.connect(self._on_scroll_range)

    def show_actions_menu(self, global_pos) -> None:
        """The card's lifecycle actions, opened by right-clicking the header.
        These used to be four permanent header buttons; the menu keeps every one
        of them reachable while giving the row back to the summary. Enablement
        follows the same status rules the buttons used."""
        status = self.agent.status
        running = status in (AgentStatus.STARTING, AgentStatus.RUNNING)
        menu = QMenu(self)
        act_start = QAction("Start", menu)
        act_start.triggered.connect(self.agent.start)
        act_start.setEnabled(status is AgentStatus.IDLE or status in _ENDED)
        act_stop = QAction("Stop (Ctrl+C, then terminate)" if self.is_pty
                           else "Stop (graceful, stdin EOF)", menu)
        act_stop.triggered.connect(self.agent.stop)
        act_stop.setEnabled(running)
        act_restart = QAction("Restart (kill + fresh session)", menu)
        act_restart.triggered.connect(self.agent.restart)
        act_assign = QAction("Assign / reassign a task…", menu)
        act_assign.triggered.connect(
            lambda: self.reassignRequested.emit(self.agent.id))
        for act in (act_start, act_stop, act_restart, act_assign):
            menu.addAction(act)
        menu.addSeparator()
        # the discoverable half of Ctrl+Shift+Enter (which needs the terminal
        # focused and something typed); this opens the same dialog empty
        act_sched = QAction("Send a message on a countdown…", menu)
        act_sched.triggered.connect(
            lambda: self.scheduleRequested.emit(self.agent.id, ""))
        act_sched.setEnabled(self.is_pty)
        menu.addAction(act_sched)
        menu.addSeparator()
        act_max = QAction("Maximize (focus this agent)", menu)
        act_max.triggered.connect(lambda: self.maximizeRequested.emit(self))
        menu.addAction(act_max)
        act_close = QAction("Close terminal", menu)
        act_close.triggered.connect(self._on_close_clicked)
        menu.addAction(act_close)
        menu.exec(global_pos)

    def _begin_reorder_drag(self) -> None:
        """Start a drag the WorkspacePage turns into a card reorder. Carries the
        agent id and a translucent snapshot of the card as the drag pixmap."""
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(CARD_REORDER_MIME, self.agent.id.encode("utf-8"))
        drag.setMimeData(mime)
        pm = self.grab()
        if not pm.isNull():
            pm = pm.scaledToWidth(min(pm.width(), 280),
                                  Qt.TransformationMode.SmoothTransformation)
            ghost = QPixmap(pm.size())
            ghost.fill(Qt.GlobalColor.transparent)
            p = QPainter(ghost)
            p.setOpacity(0.72)
            p.drawPixmap(0, 0, pm)
            p.end()
            drag.setPixmap(ghost)
            drag.setHotSpot(QPoint(pm.width() // 2, 16))
        drag.exec(Qt.DropAction.MoveAction)

    def detach(self) -> None:
        """Unhook from the agent before the card widget is deleted."""
        pairs = [(self.agent.status_changed, self._on_status),
                 (self.agent.assignment_changed, self._on_assignment),
                 (self.agent.role_changed, self._on_role),
                 (self.agent.name_changed, self._on_name),
                 (self.agent.task_changed, self._on_task),
                 (self.agent.summary_changed, self._on_task),
                 (self.agent.tokens_changed, self._on_tokens),
                 (self.agent.model_changed, self._on_model),
                 (self.agent.limit_blocked_changed, self._on_limit_blocked),
                 (self.agent.bg_shell_changed, self._on_bg_shell),
                 (self.agent.scheduled_changed, self.refresh_schedule)]
        if self.is_pty:
            pairs.append((self.agent.pty_output, self._on_pty_output))
            pairs.append((self.agent.prompt_ready_changed,
                          self._on_prompt_ready))
            # a card on its way out must not leave a throbber animating
            self._dismiss_boot_veil()
        else:
            pairs.append((self.agent.output_segment, self._on_segment))
            pairs.append((self.agent.cleared, self._on_cleared))
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _on_pty_output(self, text: str) -> None:
        self.terminal.feed(text)

    def _on_key_input(self, seq: str) -> None:
        """Keystrokes reach the process — and a stopped terminal is never a
        dead end: the first keypress starts (or resumes) the session."""
        if self.agent.is_running():
            # typing into a still-booting child means the user wants the
            # terminal, not the loader: the veil gets out of the way at once
            self._dismiss_boot_veil()
            self.agent.write(seq)
        else:
            self.agent.start()  # the waking keystroke is deliberately eaten

    def _on_assignment(self, state) -> None:
        # The lifecycle state is no longer painted as a header badge: what an
        # agent is doing is plain from its terminal, and the badge cost the
        # summary its width. The state itself still drives the model (and the
        # sidebar), so nothing is lost from the record.
        self.btn_close.setToolTip(
            "Close terminal (agents never close on their own, you decide)")

    def _on_role(self, _name: str) -> None:
        # the title tracks spec.name (which set_role leaves alone once the user
        # has manually renamed); only the role sublabel follows the emitted role
        self.title.setText(self.agent.spec.name)
        self.role.setText(self.agent.spec.role or "")

    def _on_name(self, name: str) -> None:
        self.title.setText(name)

    def _on_task(self, *_ignore) -> None:
        # show the agent's summary — its assigned task, else Claude's live AI
        # conversation title. The label collapses it to one line, fits it to
        # whatever width the header leaves and keeps the full text on hover.
        # It stays IN the layout even when empty (it carries the header's
        # stretch): hidden, the stretch vanishes and the status glyph absorbs
        # the slack, shoving the agent name to the middle.
        text = self.agent.summary() if hasattr(self.agent, "summary") \
            else (self.agent.current_task or "")
        self.task_summary.set_full_text(text or "")
        self._task_full = self.task_summary.full_text()

    def _on_limit_blocked(self, blocked: bool) -> None:
        """Show/hide the 'the usage limit stopped this agent' marker. The
        tooltip is rebuilt each time because the agent's state moves under it
        (a reset time arrives from the account reading; retries accumulate)."""
        self.limit_mark.setVisible(bool(blocked))
        if blocked:
            self.limit_mark.setToolTip(self.agent.limit_summary())

    def _on_bg_shell(self, active: bool) -> None:
        """Show/hide the 'idle, but a background command it started is still
        running' marker."""
        self.bg_mark.setVisible(bool(active))
        if active:
            self.bg_mark.setToolTip(
                "Idle, but a background command it started is still "
                "running. Click to stop it")

    def refresh_schedule(self) -> None:
        """Repaint the deferred-message chip: the countdown to the soonest one,
        or a warning that one was missed.

        Called both on `scheduled_changed` (the queue changed) and once a second
        from MainWindow's tick while anything is pending. It touches nothing but
        this label, which is what keeps a per-second countdown off the session
        file - see the `scheduled_changed` wiring in WorkspaceManager.
        """
        msg = self.agent.next_scheduled()
        missed = self.agent.missed_scheduled()
        if msg is not None:
            count = len(self.agent.pending_scheduled())
            self.sched_mark.setText(
                f"⏱ {scheduled_send.format_countdown(msg.seconds_left())}")
            more = f" (+{count - 1} more)" if count > 1 else ""
            self.sched_mark.setToolTip(
                f"Sending {scheduled_send.format_clock(msg.due_ts)}{more}:\n"
                f"{_snippet(msg.text)}\n\nClick to change or cancel")
            self._set_sched_missed(False)
        elif missed:
            self.sched_mark.setText(f"⏱ missed ({len(missed)})")
            self.sched_mark.setToolTip(
                f"{len(missed)} scheduled message(s) came due while this agent "
                f"was unreachable and were NOT sent.\n"
                f"Click to send one now or dismiss it.")
            self._set_sched_missed(True)
        self.sched_mark.setVisible(msg is not None or bool(missed))

    def _set_sched_missed(self, missed: bool) -> None:
        """Flip the chip's warning state, restyling ONLY on a real change.
        `refresh_schedule` runs once a second while a countdown is live, and an
        unconditional repolish would re-run the stylesheet on every tick."""
        if self.sched_mark.property("missed") is missed:
            return
        self.sched_mark.setProperty("missed", missed)
        repolish(self.sched_mark)

    def _on_tokens(self, badge: str = "") -> None:
        # context-window usage badge beside the summary; hidden when empty so a
        # fresh or non-Claude agent shows nothing (never a misleading "0%")
        if badge:
            self.token_label.setText(badge)
            self.token_label.setToolTip(
                f"Context window: {badge} used by this conversation")
        else:
            self.token_label.clear()
        self.token_label.setVisible(bool(badge))

    def _on_model(self, badge: str = "") -> None:
        # "Opus 5 · high · plan", tracking /model, /effort and the Shift+Tab
        # permission mode inside the terminal. Hidden when empty so a plain
        # shell shows nothing (never a guess).
        if badge:
            model, effort = self.agent.live_model()
            tip = f"Model: {model}"
            if effort:
                tip += f"\nEffort: {effort}"
            mode = self.agent.permission_mode_label()
            if mode:
                tip += (f"\nPermission mode: {mode}"
                        "\nKept for the next launch, so this agent reopens"
                        " in the same mode")
            self.model_label.setText(badge)
            self.model_label.setToolTip(
                tip + "\nFollows /model, /effort and Shift+Tab in this terminal")
        else:
            self.model_label.clear()
        self.model_label.setVisible(bool(badge))

    # -------------------------------------------------------- inline rename ---

    def start_rename(self) -> None:
        self._renaming = True
        self.title_edit.setText(self.title.text())
        self.title.hide()
        self.title_edit.show()
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _commit_rename(self) -> None:
        # re-entrancy guard: _end_rename()'s hide() delivers a synchronous
        # FocusOut that routes right back here (see WorkspaceRow). Without it,
        # Escape would commit instead of cancel and Enter would commit twice.
        if not self._renaming:
            return
        name = self.title_edit.text().strip()
        self._end_rename()
        if name and name != self.title.text():
            self.agent.set_name(name)  # model persists via name_changed -> _touch

    def _end_rename(self) -> None:
        self._renaming = False
        self.title_edit.hide()
        self.title.show()

    # ------------------------------------------------------- per-agent font ---

    def current_font_px(self) -> int:
        if self.is_pty:
            return self.terminal.font_size()
        # per-agent override wins; otherwise the app-wide QSS default renders
        return self.agent.spec.font_px or ui_theme.CONSOLE_FONT_PX

    def _apply_font(self, px: int) -> None:
        px = max(7, min(40, int(px)))
        if self.is_pty:
            self.terminal.set_font_size(px)
        else:
            # a per-widget stylesheet outranks the app-wide #Console/#CardInput
            # font-size rule (plain setFont() does NOT), so the override sticks
            # even after a global setStyleSheet re-polish
            self.console.setStyleSheet(f"#Console {{ font-size: {px}px; }}")
            self.input.setStyleSheet(f"#CardInput {{ font-size: {px}px; }}")

    def reapply_font(self) -> None:
        """Re-assert this card's per-agent font (after a global QSS change)."""
        if self.agent.spec.font_px:
            self._apply_font(self.agent.spec.font_px)

    def _font_delta(self, delta: int) -> None:
        px = max(7, min(40, self.current_font_px() + delta))
        self._apply_font(px)
        self.agent.set_font(px)  # persist per-agent override

    def _on_cleared(self) -> None:
        self.console.clear()
        self._parsers = {}
        self._cr_pending = False
        self._follow = True

    # ------------------------------------------------------------- events ---

    def eventFilter(self, obj, event):
        if obj is self.title:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self.start_rename()
                return True
            return super().eventFilter(obj, event)
        if obj is self.title_edit:
            if (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key.Key_Escape):
                self._end_rename()  # true cancel
                return True
            if event.type() == QEvent.Type.FocusOut:
                # a context-menu popup is not a real focus loss
                if event.reason() != Qt.FocusReason.PopupFocusReason:
                    self._commit_rename()
            return super().eventFilter(obj, event)
        if event.type() == QEvent.Type.FocusIn:
            self.focusGained.emit(self)
        elif event.type() == QEvent.Type.KeyPress:
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl and event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._font_delta(+1)
                return True
            if ctrl and event.key() in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                self._font_delta(-1)
                return True
            if obj is self.input and event.key() == Qt.Key.Key_Up:
                self._history_step(-1)
                return True
            if obj is self.input and event.key() == Qt.Key.Key_Down:
                self._history_step(1)
                return True
        elif (event.type() == QEvent.Type.Resize and self.is_pty
                and obj is self.terminal):
            self._place_overlay()
        return super().eventFilter(obj, event)

    def _drop_restored_screen(self) -> None:
        """Give the launching child a clean terminal.

        `_pending_replay` still being set means this card has NEVER re-rendered
        its restored screen at a settled size, so what is on the terminal was
        drawn at the pre-layout width and pyte cannot reflow it. Both launch
        paths that start an agent (`autostart_active_workspace` and
        `recover_blocked_at_startup`) run SYNCHRONOUSLY right after `show()`,
        while `TerminalView` debounces its resize by 120ms, so this is every
        agent that comes back running: their cards would sit on a mangled
        narrow fragment of the old conversation until the TUI finished
        booting. An empty terminal that fills in a few seconds is what a
        restored hive looked like before snapshots existed, and snapshots were
        never meant to change it.

        A card WOKEN by a keystroke is untouched: it has long since
        re-rendered (`_pending_replay` is empty by then), so its conversation
        still scrolls up out of the way as a real terminal's would."""
        self._pending_replay = ""
        try:
            self.terminal.sizeChanged.disconnect(self._rerender_restored)
        except (RuntimeError, TypeError):
            pass
        # drop it on the AGENT too, or a card rebuilt later (a retile, a
        # workspace switch) replays the same stale seed under the child
        if self.agent.drop_seeded_screen():
            self.terminal.screen.reset()
            self.terminal.update()

    def _rerender_restored(self, *_) -> None:
        """One-shot: repaint a restored screen at the card's settled size.

        Consumes `_pending_replay` first, so a resize storm (a retile, a
        sidebar toggle, a window drag) can only ever re-render once."""
        replay, self._pending_replay = self._pending_replay, ""
        try:
            self.terminal.sizeChanged.disconnect(self._rerender_restored)
        except (RuntimeError, TypeError):
            pass
        if not replay:
            return
        if self.agent.pty_replay() != replay:
            # The child has written (or a restart cleared the buffer) since
            # this card was built, so what is on screen is no longer the
            # restored snapshot. Whoever owns it now redraws on the SIGWINCH
            # that `agent.resize` just sent — never fight that with a stale
            # re-feed. Note the test is the agent's BUFFER, not `is_running`:
            # an agent can be running for a good fraction of a second before
            # its child emits its first byte, and that gap is the whole window
            # this re-render exists to cover.
            return
        self.terminal.screen.reset()
        self.terminal.feed(replay)
        self._refresh_overlay()

    def _refresh_overlay(self) -> None:
        """Pick the banner's shape from what is already on the screen.

        Deciding here (on a status change) rather than in `_place_overlay`
        keeps `screen_text()` off the resize path, which fires per pixel
        while a card is dragged or a workspace retiles."""
        if not self.is_pty:
            return
        compact = bool(self.terminal.screen_text().strip())
        self._overlay_compact = compact
        if compact:
            self.overlay.setText("not running · press any key to resume")
            self.overlay.setStyleSheet(
                "QLabel { background: rgba(10, 10, 12, 225);"
                f" color: {Palette.ACCENT_ORANGE}; font-size: 12px;"
                " font-style: italic; border-top: 1px dashed #6b5b28;"
                " padding: 4px; }")
        else:
            self.overlay.setText("terminal not running\n"
                                 "press any key to start")
            self.overlay.setStyleSheet(
                "QLabel { background: rgba(10, 10, 12, 200);"
                f" color: {Palette.ACCENT_ORANGE}; font-size: 13px;"
                " font-style: italic; border: 1px dashed #6b5b28;"
                " border-radius: 6px; padding: 10px; }")
        self._place_overlay()

    def _begin_boot_veil(self) -> None:
        """Cover a launching terminal until its conversation is on screen.

        The veil lifts on the FIRST of: the child reporting an interactive
        prompt (`prompt_ready_changed`, which for Claude is the input-box
        footer, i.e. after any trust dialog AND after a `--resume` replay has
        finished drawing), the user typing, the agent stopping, or
        `BOOT_VEIL_MAX_MS`. A terminal is never covered for good."""
        if not self.is_pty or self.agent.prompt_ready():
            return
        # `spec.resume` is cleared by `start()` right after the worker is
        # launched, so at STARTING it still says whether this launch is
        # reopening a conversation or beginning one.
        self.boot.begin("restoring conversation…" if self.agent.spec.resume
                        else "starting…")
        self._place_overlay()
        self._boot_timer.start(BOOT_VEIL_MAX_MS)

    def _end_boot_veil(self) -> None:
        """Ready: dissolve, so the conversation appears rather than snaps in."""
        self._boot_timer.stop()
        if self.is_pty:
            self.boot.finish()

    def _dismiss_boot_veil(self) -> None:
        """Drop it immediately (stopped, typed into, or timed out)."""
        self._boot_timer.stop()
        if self.is_pty:
            self.boot.dismiss()

    def _on_prompt_ready(self, ready: bool) -> None:
        if ready:
            self._end_boot_veil()

    def _place_overlay(self) -> None:
        if not self.is_pty:
            return
        self.boot.setGeometry(self.terminal.rect())
        if self._overlay_compact:  # a full-width strip along the bottom edge,
            h = 24                 # so the conversation above stays readable
            self.overlay.setGeometry(0, max(0, self.terminal.height() - h),
                                     self.terminal.width(), h)
            return
        w = min(320, max(220, self.terminal.width() - 40))
        h = 64
        self.overlay.setGeometry((self.terminal.width() - w) // 2,
                                 (self.terminal.height() - h) // 2, w, h)

    def _on_scroll_value(self, value: int) -> None:
        self._follow = value >= self.console.verticalScrollBar().maximum()

    def _on_scroll_range(self, _min: int, _max: int) -> None:
        # geometry changes (retile, resize, sidebar toggle) grow the range
        # without moving the value — keep following if we were following
        if self._follow:
            sb = self.console.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_focused(self, focused: bool) -> None:
        if self.property("focused") != focused:
            self.setProperty("focused", focused)
            repolish(self)

    def set_maximized(self, on: bool) -> None:
        # the SAME button toggles between Maximize and Restore down — the page
        # owns the actual solo state; this only reflects it in the glyph/tooltip
        self.btn_max.setText("⤡" if on else "⤢")
        self.btn_max.setToolTip("Restore down" if on
                                else "Maximize (focus this agent)")

    def _on_close_clicked(self) -> None:
        self.closeRequested.emit(self.agent.id)

    def _submit(self) -> None:
        text = self.input.text()
        if not text.strip():
            return
        self._history.append(text)
        self._hist_idx = len(self._history)
        self.input.clear()
        self.agent.send_command(text)

    def _history_step(self, delta: int) -> None:
        if not self._history:
            return
        if self._hist_idx == len(self._history) and delta < 0:
            self._draft = self.input.text()  # stash the in-progress command
        self._hist_idx = max(0, min(len(self._history), self._hist_idx + delta))
        self.input.setText(self._history[self._hist_idx]
                           if self._hist_idx < len(self._history)
                           else self._draft)

    # ------------------------------------------------------------- status ---

    def _on_status(self, status: AgentStatus) -> None:
        self.glyph.setProperty("state", _GLYPH_STATE.get(status, "idle"))
        repolish(self.glyph)
        running = status in (AgentStatus.STARTING, AgentStatus.RUNNING)
        exit_info = self.agent.worker.exit_info
        tip = f"{status.value}"
        if exit_info and not running:
            tip += f" (code {exit_info[0]})"
        self.glyph.setToolTip(tip)

        if self.is_pty and running and self._pending_replay:
            self._drop_restored_screen()

        if self.is_pty:  # stopped terminal shows the wake banner, never black
            self.overlay.setVisible(not running
                                    and status is not AgentStatus.STOPPING)
            if not running:
                self._refresh_overlay()
                self.overlay.raise_()
            # a booting child and a stopped one are mutually exclusive states,
            # and the wake banner owns the stopped one
            if running and not self.agent.prompt_ready():
                self._begin_boot_veil()
            elif not running:
                self._dismiss_boot_veil()

        if status is AgentStatus.STARTING:
            self._parsers = {}  # fresh session: never inherit stale carry
        elif status in _ENDED:
            for stream, parser in self._parsers.items():
                segments = parser.flush()  # render text held mid-escape
                if segments:
                    self._insert_segments(segments,
                                          stderr=(stream == "stderr"))

    # ---------------------------------------------------------- rendering ---

    def _replay_log(self) -> None:
        for stream, text in list(self.agent.log):
            self._on_segment(stream, text)

    def _on_segment(self, stream: str, text: str) -> None:
        if stream == STREAM_INPUT:
            segments = [(INPUT_STYLE, text)]
        elif stream == STREAM_SYSTEM:
            segments = [(SYSTEM_STYLE, text)]
        else:
            parser = self._parsers.setdefault(stream, AnsiSgrParser())
            segments = parser.feed(text)
        if segments:
            self._insert_segments(segments, stderr=(stream == "stderr"))

    def _fmt(self, style: CharStyle, stderr: bool) -> QTextCharFormat:
        key = (style, stderr)
        fmt = self._fmt_cache.get(key)
        if fmt is not None:
            return fmt
        fmt = QTextCharFormat()
        default_fg = Palette.RED if stderr else Palette.CONSOLE_FG
        fg = style.fg or default_fg
        bg = style.bg
        if style.reverse:
            fg, bg = (bg or Palette.BG_CONSOLE), (style.fg or default_fg)
        fmt.setForeground(QColor(fg))
        if bg:
            fmt.setBackground(QColor(bg))
        if style.bold:
            fmt.setFontWeight(700)
        if style.italic:
            fmt.setFontItalic(True)
        if style.underline:
            fmt.setFontUnderline(True)
        self._fmt_cache[key] = fmt
        return fmt

    def _insert_segments(self, segments, stderr: bool = False) -> None:
        cursor = QTextCursor(self.console.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try:
            newline_fmt = QTextCharFormat()
            for style, text in segments:
                fmt = self._fmt(style, stderr)
                pos = 0
                for m in _LINE_BREAKS.finditer(text):
                    chunk = text[pos:m.start()]
                    if chunk:
                        self._insert_chunk(cursor, chunk, fmt)
                    if m.group(0) == "\n":
                        self._cr_pending = False
                        cursor.insertText("\n", newline_fmt)
                    else:  # lone \r: next text overwrites the current line
                        self._cr_pending = True
                    pos = m.end()
                rest = text[pos:]
                if rest:
                    self._insert_chunk(cursor, rest, fmt)
        finally:
            cursor.endEditBlock()

        # log-viewer convention: follow output unless the user scrolled up
        if self._follow:
            sb = self.console.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _insert_chunk(self, cursor: QTextCursor, chunk: str,
                      fmt: QTextCharFormat) -> None:
        if self._cr_pending:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            self._cr_pending = False
        cursor.insertText(chunk, fmt)
