"""TerminalCard: one tiled agent view — header, console, command input.

The card is a pure view over a model-owned TerminalAgent: it renders the
agent's log/output and forwards user input. It never owns the process, so
deleting a card can never kill anything by side effect; teardown goes
through WorkspaceManager. Call detach() before deleting the card so late
agent signals can't fire into a dead widget.
"""

import re

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QToolButton, QVBoxLayout)

from .. import ui_theme
from ..ansi_parser import AnsiSgrParser, CharStyle
from ..terminal_agent import (ASSIGNMENT_LABEL, STREAM_INPUT, STREAM_SYSTEM,
                              AgentStatus, AssignmentState, TerminalAgent)
from ..ui_theme import Palette, repolish

_LINE_BREAKS = re.compile(r"[\r\n]")

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


class TerminalCard(QFrame):
    closeRequested = Signal(str)     # agent id
    focusGained = Signal(object)     # self
    reassignRequested = Signal(str)  # agent id (retask a completed/idle agent)
    maximizeRequested = Signal(object)  # self (toggle solo view of this card)
    fileActivated = Signal(str)      # abs path Ctrl+clicked in the conversation

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
        self._task_full = ""    # untruncated current-task, for the elided summary
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
        else:
            self._replay_log()
        self._on_status(agent.status)
        self._on_assignment(agent.assignment)
        self._on_task()
        self._on_tokens(agent.token_badge())

    # ----------------------------------------------------------------- ui ---

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("CardHeader")
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
        self.role = QLabel(f"— {self.agent.spec.role}" if self.agent.spec.role
                           else "", header)
        self.role.setObjectName("CardRole")
        self.badge = QLabel("", header)   # assignment lifecycle badge
        self.badge.setObjectName("CardBadge")
        self.badge.hide()
        # one-line summary of what this agent is working on (its current task),
        # so several agents in a workspace are tellable apart at a glance
        # without reading each terminal. Elided to fit; full text on hover.
        self.task_summary = QLabel("", header)
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
        hl.addSpacing(6)
        hl.addWidget(self.badge)
        hl.addSpacing(6)
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

        self.btn_font_dec = tool("A−", "CardFontDec", "Smaller font (Ctrl+-)")
        self.btn_font_inc = tool("A+", "CardFontInc", "Larger font (Ctrl+=)")
        self.btn_start = tool("▶", "CardStart", "Start")
        stop_tip = ("Stop (Ctrl+C, then terminate)" if self.is_pty
                    else "Stop (graceful, stdin EOF)")
        self.btn_stop = tool("■", "CardStop", stop_tip)
        self.btn_restart = tool("⟳", "CardRestart", "Restart (kill + fresh session)")
        self.btn_reassign = tool("⇄", "CardReassign",
                                 "Assign / reassign a task to this agent")
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
            # visible banner says so, and any keystroke starts the session
            self.overlay = QLabel("terminal not running\n"
                                  "press any key — or ▶ — to start",
                                  self.terminal)
            self.overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.overlay.setStyleSheet(
                "QLabel { background: rgba(10, 10, 12, 200);"
                f" color: {Palette.ACCENT_ORANGE}; font-size: 13px;"
                " font-style: italic; border: 1px dashed #6b5b28;"
                " border-radius: 6px; padding: 10px; }")
            self.overlay.hide()
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
        self.title.installEventFilter(self)        # double-click to rename
        self.title_edit.installEventFilter(self)   # Esc cancels, focus-out commits
        self.title_edit.returnPressed.connect(self._commit_rename)
        self.btn_start.clicked.connect(self.agent.start)
        self.btn_stop.clicked.connect(self.agent.stop)
        self.btn_restart.clicked.connect(self.agent.restart)
        self.btn_reassign.clicked.connect(
            lambda: self.reassignRequested.emit(self.agent.id))
        self.btn_max.clicked.connect(lambda: self.maximizeRequested.emit(self))
        self.btn_close.clicked.connect(self._on_close_clicked)
        self.btn_font_dec.clicked.connect(lambda: self._font_delta(-1))
        self.btn_font_inc.clicked.connect(lambda: self._font_delta(+1))

        if self.is_pty:
            self.agent.pty_output.connect(self._on_pty_output)
            self.terminal.keyInput.connect(self._on_key_input)
            self.terminal.sizeChanged.connect(self.agent.resize)
            # relative paths in the output resolve against the agent's cwd, and
            # Ctrl+clicking a file bubbles up so the app can reveal it
            self.terminal.set_base_dir(getattr(self.agent.spec, "cwd", "") or "")
            self.terminal.fileActivated.connect(self.fileActivated)
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

    def detach(self) -> None:
        """Unhook from the agent before the card widget is deleted."""
        pairs = [(self.agent.status_changed, self._on_status),
                 (self.agent.assignment_changed, self._on_assignment),
                 (self.agent.role_changed, self._on_role),
                 (self.agent.name_changed, self._on_name),
                 (self.agent.task_changed, self._on_task)]
        if self.is_pty:
            pairs.append((self.agent.pty_output, self._on_pty_output))
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
            self.agent.write(seq)
        else:
            self.agent.start()  # the waking keystroke is deliberately eaten

    def _on_assignment(self, state) -> None:
        # persistent lifecycle badge; auto-created agents never auto-close, so
        # a completed agent stays visible with a clear "Completed" badge
        label = ASSIGNMENT_LABEL.get(state, "")
        show = state in (AssignmentState.WORKING, AssignmentState.COMPLETED,
                         AssignmentState.AWAITING)
        self.badge.setText(label)
        self.badge.setVisible(show)
        key = {AssignmentState.WORKING: "working",
               AssignmentState.COMPLETED: "completed",
               AssignmentState.AWAITING: "awaiting"}.get(state, "")
        self.badge.setProperty("state", key)
        repolish(self.badge)
        self.btn_close.setToolTip(
            "Close terminal (agents never close on their own — you decide)")

    def _on_role(self, _name: str) -> None:
        # the title tracks spec.name (which set_role leaves alone once the user
        # has manually renamed); only the role sublabel follows the emitted role
        self.title.setText(self.agent.spec.name)
        self.role.setText(f"— {self.agent.spec.role}" if self.agent.spec.role
                          else "")

    def _on_name(self, name: str) -> None:
        self.title.setText(name)

    def _on_task(self, *_ignore) -> None:
        # show the agent's summary — its assigned task, else Claude's live AI
        # conversation title. Collapse to a single line: the summary shares the
        # fixed-height header row, so a newline would blow it up.
        text = self.agent.summary() if hasattr(self.agent, "summary") \
            else (self.agent.current_task or "")
        self._task_full = " ".join((text or "").split())
        self.task_summary.setToolTip(self._task_full)
        # keep the summary label ALWAYS in the layout (it carries the header's
        # stretch): if it's hidden when empty, the stretch vanishes and the
        # status glyph absorbs the slack, shoving the agent name to the middle.
        self._elide_task()

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

    def _elide_task(self) -> None:
        if not self._task_full:
            self.task_summary.clear()
            return
        avail = self.task_summary.width() - 4
        if avail > 8:
            fm = QFontMetrics(self.task_summary.font())
            self.task_summary.setText(
                fm.elidedText(self._task_full, Qt.TextElideMode.ElideRight, avail))
        else:
            # width not settled yet (e.g. before first layout): char fallback so
            # the summary is never blank when there IS a task
            self.task_summary.setText(
                self._task_full[:48] + ("…" if len(self._task_full) > 48 else ""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_task()  # re-fit the summary to the new header width

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

    def _place_overlay(self) -> None:
        if not self.is_pty:
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
        self.btn_start.setEnabled(status is AgentStatus.IDLE
                                  or status in _ENDED)
        self.btn_stop.setEnabled(running)
        exit_info = self.agent.worker.exit_info
        tip = f"{status.value}"
        if exit_info and not running:
            tip += f" (code {exit_info[0]})"
        self.glyph.setToolTip(tip)

        if self.is_pty:  # stopped terminal shows the wake banner, never black
            self.overlay.setVisible(not running
                                    and status is not AgentStatus.STOPPING)
            if not running:
                self._place_overlay()
                self.overlay.raise_()

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
