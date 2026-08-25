"""TerminalCard: one tiled agent view — header, console, command input.

The card is a pure view over a model-owned TerminalAgent: it renders the
agent's log/output and forwards user input. It never owns the process, so
deleting a card can never kill anything by side effect; teardown goes
through WorkspaceManager. Call detach() before deleting the card so late
agent signals can't fire into a dead widget.
"""

import datetime
import re

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QDrag, QPainter, QPixmap,
                           QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QToolButton, QVBoxLayout,
                               QWidget)

from .. import scheduled_send, transcripts, ui_theme
from ..ansi_parser import AnsiSgrParser, CharStyle
from ..process_worker import AI_KINDS, describe_pid
from ..terminal_agent import (STREAM_INPUT, STREAM_SYSTEM, AgentStatus,
                              TerminalAgent)
from ..ui_theme import Palette, repolish
from .ornaments import BootVeil, ElidingLabel
from .terminal_view import is_reply_footer

_LINE_BREAKS = re.compile(r"[\r\n]")

# How long the boot veil may cover a launching terminal before it lifts on its
# own. Readiness normally arrives in a second or two; this only exists so a
# child that never emits the ready signal at all (an exotic pty shell) can
# never leave the user looking at a cover instead of their terminal.
BOOT_VEIL_MAX_MS = 25_000
# How much of an agent's raw pty tail is replayed when a card is (re)built or
# the terminal changes width. This is a COMPROMISE, not a free bound: re-measured
# on real classic-renderer captures at ~100 cols, 128 KiB projects only 844-1519
# of HISTORY_LINES' 2000 lines, so the cap is already what truncates reachable
# scrollback, and LOWERING it costs milestones (an earlier note here claimed
# 96 KiB filled all 2000; that was wrong on this data -- 96 KiB reaches 576-1126).
# The other side is that this runs on the GUI thread per card: 128 KiB costs
# ~80ms per card since the pyte wrapper came off (~250ms before it), so a retile
# across a six-agent hive spends about half a second projecting.
# See TerminalCard._replay_with_marks and _reproject_on_size.
REPLAY_PROJECT_CAP = 128 * 1024
# How much is projected in the CONSTRUCTOR, before the tiling grid has given the
# card its real width. That first projection is guaranteed to be thrown away and
# redone (see _rerender_restored), so spending the full cap on it was pure waste:
# every card at launch projected 128 KiB twice, once at a width that never
# reaches the screen. A screenful is enough to paint something immediately --
# which is the only reason to feed anything this early -- and costs ~5ms instead
# of ~80ms. Do NOT raise it to "get the scrollback sooner": the settled-size
# projection is what puts scrollback in, and it lands within ~120ms.
REPLAY_SEED_CAP = 8 * 1024
# Backstop for that settled-size projection. TerminalView._apply_resize returns
# EARLY when rows/cols are unchanged, so sizeChanged is NOT guaranteed to fire --
# a card built at exactly its final size would otherwise keep the seed forever
# and show a screenful with no scrollback behind it. Comfortably past the view's
# own 120ms resize debounce, so a real resize wins the race and this stays a
# backstop rather than a second projection.
REPLAY_SETTLE_MS = 300
# how much of a transcript prompt must be found on a scrollback line to call it
# that prompt's echo (see TerminalCard._recover_marks)
_MARK_MATCH_CHARS = 28
_MARK_WS_RE = re.compile(r"\s+")
# markdown syntax and drawing glyphs the renderer paints rather than prints,
# dropped from both sides of a reply match (see _norm_reply_line)
_MARK_MD_RE = re.compile("[*`_#>❯•⏺─-╿]")
# settles a card will spend looking for the milestones of a conversation its
# child reprinted after the card was built (see TerminalCard._rescan_recovery).
# The scan is ~35 ms over a full history, so this is a handful of attempts, not
# a poll: it stops on the first one that finds anything.
_RECOVER_RESCAN_TRIES = 6
# how much of the END of a reply's last line must be found on a scrollback row
# to call it that reply's last row (see _reply_end_row). Shorter than the
# prompt window: a wrapped line's final row holds only what spilled onto it,
# which can be a few words.
_REPLY_TAIL_CHARS = 16
# how far apart a LIVE reply mark and a RECOVERED one may sit and still be the
# same reply (see _refresh_reply_marks). The two anchors normally agree exactly
# -- reply_anchor_line returns the footer row + 1, _reply_end_row returns i + 3
# where i + 2 is that same footer -- so this only covers the shapes where they
# fall back differently: recovery to the blank row directly under the reply
# text, the live path to the footer row itself. Three rows spans that gap and
# nothing else; a reply is never two turns away from itself.
_STAMP_MERGE_SLACK = 3


def _norm_line(text: str) -> str:
    """Lowered, whitespace-collapsed, prompt glyphs dropped -- the form both
    sides of the milestone-recovery match are compared in."""
    return _MARK_WS_RE.sub(" ", text.replace(">", " ").replace("❯", " ")
                           .lower()).strip()


def _norm_reply_line(text: str) -> str:
    """_norm_line, plus the markdown the renderer eats. A reply is written as
    markdown and PAINTED as styled text -- `code`, **bold** and heading hashes
    arrive on screen as SGR runs with the syntax characters gone -- so matching
    a transcript reply against a rendered line has to drop them from both
    sides. Bullet and box glyphs go too: the renderer draws its own."""
    return _MARK_WS_RE.sub(" ", _MARK_MD_RE.sub(" ", text).lower()).strip()


def _last_content_line(text: str) -> str:
    """The last line of a reply that has anything on it once normalized -- the
    line that ends up at the bottom of that reply on screen."""
    for line in reversed(text.splitlines()):
        if _norm_reply_line(line):
            return line
    return ""


def _reply_end_row(lines: list[str], tail: str, start: int) -> int | None:
    """The blank row directly under where a reply ENDED, at or after `start`.

    Two conditions, and each is load-bearing. The row must contain the TAIL of
    the reply's last line -- not its head, because a long final line wraps and
    only its last rendered row carries the tail, which is precisely the row the
    reply ends on. And the row BELOW must be blank, which is what proves the
    reply really ended there.

    Both come from the same measured failure. pyte does not reflow, so a card
    resized mid-session keeps BOTH renders of a reply in its scrollback, the
    older one truncated wherever the redraw overwrote it. Matching the head
    found that stale copy first and stamped 4 rows into it, i.e. the middle of
    a paragraph. The tail is usually missing from a truncated copy, and the
    blank-row test rejects it outright when it is not, so the scan simply walks
    on to the real one.

    A footer directly below that blank row moves the anchor down past it, so a
    recovered stamp and a live one land in the SAME place relative to the
    "<verb> for Ns" line (see TerminalView.reply_anchor_line).

    The blank row is also the right place to draw: the stamp is painted into a
    row's empty right-hand tail and skipped when the row's own content runs too
    close to the edge (see TerminalView.paintEvent), so a full line of prose
    would silently lose the very stamp this feature exists to show. Nothing
    matching means no stamp -- the same contract reply_anchor_line() and the
    paint-time skip already follow."""
    for i in range(start, len(lines) - 1):
        if tail not in lines[i] or lines[i + 1]:
            continue
        # the blank row under the reply text is only the stamp's home when
        # Claude's own turn footer is NOT there. When it is (the newest reply,
        # the one whose footer survived), the stamp belongs UNDER it, beside
        # nothing rather than wedged between the reply and its own footer --
        # which is exactly what the user asked to have moved.
        if (i + 3 < len(lines) and is_reply_footer(lines[i + 2])
                and not lines[i + 3]):
            return i + 3
        return i + 1
    return None


def _format_reply_stamp(ts: float) -> str:
    """The date AND time a reply finished, e.g. "Aug 19, 19:14".

    Never time-only, not even for a reply from today: this is now the ONLY
    surface carrying a reply time (the card header's badge was removed at the
    user's request), and a bare HH:MM on a conversation reopened days later
    reads as "just now". Computed at REFRESH time rather than capture time,
    so a stamp minted today still says so once the day turns over."""
    return datetime.datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")

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


class _HeaderTools(QWidget):
    """The header's secondary buttons (A- / A+ / maximize), collapsed to a
    narrow strip until the pointer is over them.

    They cost ~100px of every header for actions that all have keyboard
    equivalents, and that width comes straight out of the task summary, which
    is the thing that tells two agents apart on a split screen. Collapsing
    them gives it back without hiding them anywhere the user has to go
    looking for.

    The container sits just LEFT of the usage chip, and the summary carries
    the layout stretch, so expanding takes its width from the summary alone:
    nothing to the right of this widget moves as the pointer crosses it.

    Qt sends Leave to a parent when the pointer enters one of its children, so
    a naive leaveEvent would hide the buttons the instant the user reached for
    one. The close is therefore deferred by one turn of the event loop and
    checked against the real cursor position, which is inside this widget's
    rect for as long as the pointer is over any of its children."""

    _HINT_W = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._open = False
        self._buttons = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.hint = QLabel("⋯", self)   # midline horizontal ellipsis
        self.hint.setObjectName("CardToolsHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setFixedWidth(self._HINT_W)
        self.hint.setToolTip("Font size and maximize")
        lay.addWidget(self.hint)

    def add(self, btn) -> None:
        btn.hide()
        self._buttons.append(btn)
        self.layout().addWidget(btn)

    def _set_open(self, on: bool) -> None:
        if on == self._open:
            return
        self._open = on
        self.hint.setVisible(not on)
        for b in self._buttons:
            b.setVisible(on)

    def enterEvent(self, event):
        self._set_open(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        QTimer.singleShot(0, self._recheck)
        super().leaveEvent(event)

    def _recheck(self) -> None:
        try:
            inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        except RuntimeError:      # widget went away under the timer
            return
        if not inside:
            self._set_open(False)


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
        # is the boot veil up because a RESTORED SNAPSHOT has not been
        # projected at a real width yet? Distinct from the veil being up
        # because a child is booting, and it decides who is allowed to lower
        # it (see __init__, _on_status, _rerender_restored).
        self._boot_seed = False
        self._restored_hooked = False  # is _rerender_restored still armed?
        # prompt milestone uid -> absolute line IN THIS VIEW. Per-card because
        # a rebuilt view has a different `pushed` origin; keyed on uid because
        # id() is reused after a FIFO eviction (see PromptMark).
        self._mark_lines: dict[int, int] = {}
        # same shape/reasoning as _mark_lines, for reply-finished milestones
        # (see ReplyMark / TerminalView.reply_anchor_line).
        self._reply_mark_lines: dict[int, int] = {}
        # milestones located by matching the transcript against a scrollback we
        # did not watch being typed (a restored conversation); recomputed on
        # every projection, never persisted
        self._recovered: list[tuple[int, str]] = []
        # same, for reply-finished stamps: (absolute line, epoch)
        self._recovered_replies: list[tuple[int, float]] = []
        # typed-prompt texts for _recover_marks and finished-reply times for
        # _recover_reply_marks, cached per conversation under ONE key. The
        # read is O(transcript) and a live transcript's mtime changes
        # constantly, so transcripts' own mtime cache never hits for exactly
        # the agents that matter. See _recover_marks for why re-reading within
        # one conversation cannot find anything the card doesn't already know.
        self._recover_key: tuple = ()
        # settles left to look for a conversation reprinted after this card was
        # built (see _rescan_recovery)
        self._recover_tries = _RECOVER_RESCAN_TRIES
        self._recover_prompts: list[str] = []
        self._recover_replies: list[tuple[float, str]] = []
        self.scroll_bar = None
        # the column count the scrollback was last projected at; a change means
        # every history line is wrapped for a screen that no longer exists
        self._proj_cols = None
        self._overlay_compact = False
        # single-shot backstop for the settled-size projection (see __init__)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._rerender_restored)
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
                # Only a SCREENFUL here. This projection happens before the
                # tiling grid has sized the card, so it is thrown away and
                # redone at the settled width no matter what -- doing the full
                # cap twice is what made every launch and retile hitch. The
                # seed exists purely so the terminal is never briefly blank.
                self._replay_with_marks(replay, cap=REPLAY_SEED_CAP,
                                        recover=False)
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
                self._restored_hooked = True
                # ...and a backstop, because sizeChanged is not guaranteed:
                # _apply_resize bails when rows/cols are unchanged, so a card
                # built at exactly its final size would keep the seed forever.
                self._settle_timer.start(REPLAY_SETTLE_MS)
        else:
            self._replay_log()
        self._on_status(agent.status)
        # ...and cover that seed, because the user WOULD otherwise see it.
        # main.py shows the window before autostart_active_workspace raises
        # any veil, so this projection is painted first -- and it can only
        # ever paint scrambled: REPLAY_SEED_CAP is a cut through the MIDDLE
        # of a classic-renderer frame (no banner, no known cursor row, column
        # jumps referring to rows that were never drawn) at a width the card
        # does not have yet. Measured on real captures: the same bytes render
        # as clean prose at their capture width and as overlapping fragments
        # at any other. Reported as "gibberish in the top left corner, then
        # the loader, then it looks normal". Deliberately AFTER _on_status,
        # whose not-running branch would dismiss what this raises.
        if self.is_pty and self.agent.has_pristine_seed():
            self._boot_seed = True
            self._begin_boot_veil()
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
        # What this agent RUNS: "PowerShell", "cmd", "python foo.py". Set once
        # by build_spec from the kind and never mutated (the task-to-role
        # heuristic that used to rewrite it, and rename the agent with it, is
        # gone). It is hidden on an AI agent, where it only ever said the
        # provider name ("Claude Code") that #CardModel says better and more
        # precisely one widget to its right -- and where the 128px it cost
        # (121px of text in the chrome serif at 11px, plus the 7px layout gap,
        # MEASURED) came straight out of the summary, which is the thing that
        # tells two agents apart on a split screen.
        self.role = QLabel(self._kind_label(), header)
        self.role.setObjectName("CardRole")
        self.role.setVisible(bool(self.role.text()))
        # what the agent is RUNNING ON right now. The user can change both from
        # inside the terminal (/model, /effort), so this follows the live
        # conversation rather than the launch flags; hidden when unknown, which
        # is every non-AI shell.
        self.model_label = QLabel("", header)
        self.model_label.setObjectName("CardModel")
        # WHOSE agent this is, as a QSS property: the chip is inked in the
        # vendor's own colour (ui_theme.PROVIDER_INK) rather than the skin's,
        # so Claude reads terracotta and Gemini blue under every theme and two
        # providers side by side are tellable apart without reading the model
        # name. The kind never changes for an agent, so this is set once here
        # and never repolished. A kind with no ink keeps the gold-dim default.
        self.model_label.setProperty("provider", AI_KINDS.get(self.agent.spec.kind, ""))
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

        def tool(text, obj_name, tip, owner=None):
            b = QToolButton(owner or header)
            b.setText(text)
            b.setObjectName(obj_name)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        # Only the buttons worth their width live here. Start / Stop / Restart /
        # Assign moved to the header's right-click menu: the terminal itself is
        # how this app is driven (any keystroke wakes a stopped card), so those
        # four were spending ~130px of every header on actions nobody clicks.
        # The three that remain are hover-revealed (see _HeaderTools): each has
        # a keyboard equivalent, so their idle width belongs to the summary.
        self.header_tools = _HeaderTools(header)
        self.btn_font_dec = tool("A−", "CardFontDec", "Smaller font (Ctrl+-)",
                                 self.header_tools)
        self.btn_font_inc = tool("A+", "CardFontInc", "Larger font (Ctrl+=)",
                                 self.header_tools)
        # solo/restore this card in the workspace grid — a pure view toggle;
        # never touches sibling processes (see WorkspacePage.toggle_solo)
        self.btn_max = tool("⤢", "CardMaximize", "Maximize (focus this agent)",
                            self.header_tools)
        for _b in (self.btn_font_dec, self.btn_font_inc, self.btn_max):
            self.header_tools.add(_b)
        hl.addWidget(self.header_tools)
        # the usage chip sits between the collapsed tools and the close button,
        # so the strip that reveals them is the space to the chip's LEFT
        hl.addWidget(self.token_label)
        self.btn_close = tool("✕", "CardClose", "Close terminal")
        hl.addWidget(self.btn_close)

        root.addWidget(header)
        if self.is_pty:
            from .terminal_view import TerminalView
            self.terminal = TerminalView(rows=self.agent.worker.rows,
                                         cols=self.agent.worker.cols,
                                         parent=self,
                                         font_px=self.agent.spec.font_px)
            root.addWidget(self.terminal, 1)
            # Overlay children of the terminal, created in z-order (each new
            # one sits above the last): the scrollbar is chrome the veil and
            # the wake banner are both entitled to cover.
            from .terminal_scrollbar import TerminalScrollBar
            self.scroll_bar = TerminalScrollBar(self.terminal, self.terminal)
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
        self.agent.activity_changed.connect(self._on_activity)
        self.agent.assignment_changed.connect(self._on_assignment)
        self.agent.name_changed.connect(self._on_name)
        self.agent.task_changed.connect(self._on_task)
        self.agent.summary_changed.connect(self._on_task)  # incl. live AI title
        self.agent.tokens_changed.connect(self._on_tokens)
        self.agent.model_changed.connect(self._on_model)
        self.agent.limit_blocked_changed.connect(self._on_limit_blocked)
        self._on_limit_blocked(self.agent.is_limit_blocked())
        self.agent.bg_shell_changed.connect(self._on_bg_shell)
        self._on_bg_shell(self.agent.is_bg_shell_busy())
        self.bg_mark.clicked.connect(self._show_bg_shell_menu)
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
            self.terminal.sizeChanged.connect(self._reproject_on_size)
            # relative paths in the output resolve against the agent's cwd, and
            # Ctrl+clicking a file bubbles up so the app can reveal it
            self.terminal.set_base_dir(getattr(self.agent.spec, "cwd", "") or "")
            self.terminal.fileActivated.connect(self.fileActivated)
            # a click must never drive the child's caret while an interactive
            # menu (AskUserQuestion/ExitPlanMode/permission prompt) is open --
            # see TerminalView._reposition_cursor for why. is_waiting() is the
            # same authoritative signal _on_prompt_submitted already trusts.
            self.terminal.set_waiting_probe(self.agent.is_waiting)
            # Ctrl+Shift+Enter in the terminal: "send this, but later". The view
            # hands up what is currently typed; the window turns it into the
            # countdown dialog and, only on confirm, clears the input box.
            self.terminal.scheduleRequested.connect(
                lambda text: self.scheduleRequested.emit(self.agent.id, text))
            # scrollbar + prompt milestones. All four are transient view wiring:
            # nothing here may reach a save (see prompt_marks_changed).
            self.terminal.viewChanged.connect(self.scroll_bar.refresh)
            self.terminal.promptSubmitted.connect(self._on_prompt_submitted)
            self.terminal.historyCleared.connect(self._on_history_cleared)
            # self-heal: the classic renderer stranded the input box above a
            # dead gap (see TerminalView._check_input_gap) -- silently ask
            # Claude to redraw. request_repaint() is already a no-op when the
            # agent isn't a running pty, so no extra guard is needed here.
            self.terminal.staleLayoutDetected.connect(self.agent.request_repaint)
            self.agent.prompt_marks_changed.connect(self._refresh_marks)
            self.agent.reply_marks_changed.connect(self._on_reply_mark_added)
            self.agent.conversation_replaced.connect(
                self.terminal.clear_history)
            self.scroll_bar.markActivated.connect(self.terminal.scroll_to_abs)
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
                 (self.agent.activity_changed, self._on_activity),
                 (self.agent.assignment_changed, self._on_assignment),
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

    def _kind_label(self) -> str:
        """The header's kind sublabel, or "" when it would say nothing new.

        An AI agent gets "": spec.role holds the provider display name there,
        which is redundant beside the live model chip. Everything else keeps
        its descriptor, since a shell has no model chip to read instead.
        """
        if self.agent.spec.kind in AI_KINDS:
            return ""
        return (self.agent.spec.role or "").replace(" (Antigravity CLI)", "")

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
                "running. Click to choose what to stop")

    def _show_bg_shell_menu(self) -> None:
        """List each process behind the gear badge so the user can kill one
        at a time instead of an all-or-nothing click -- an accidental click
        near the badge must not risk killing something an agent is actually
        waiting on."""
        pids = self.agent.bg_shell_extra_pids()
        if not pids:
            return
        menu = QMenu(self)
        for pid in pids:
            act = QAction(f"Kill {describe_pid(pid)} (pid {pid})", menu)
            act.triggered.connect(
                lambda checked=False, p=pid: self.agent.kill_bg_shell_pid(p))
            menu.addAction(act)
        if len(pids) > 1:
            menu.addSeparator()
            act_all = QAction(f"Kill all {len(pids)}", menu)
            act_all.triggered.connect(self.agent.kill_bg_shell_extras)
            menu.addAction(act_all)
        menu.exec(self.bg_mark.mapToGlobal(self.bg_mark.rect().bottomLeft()))

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

    def _unhook_restored(self) -> None:
        """Disarm the one-shot settled-size projection, once.

        Both teardown paths (`drop_restored_screen` and `_rerender_restored`
        itself) can run for the same card, and PySide warns rather than raises
        on a disconnect that has already happened -- so the flag, not a
        try/except, is what makes the second call silent."""
        if not self._restored_hooked:
            return
        self._restored_hooked = False
        try:
            self.terminal.sizeChanged.disconnect(self._rerender_restored)
        except (RuntimeError, TypeError):
            pass

    def drop_restored_screen(self) -> None:
        """Give the launching child a clean terminal.

        A restored screen (`app/screen_snapshot.py`) exists so a card the user
        left STOPPED reopens showing its conversation instead of a black
        rectangle. The moment a child is launching behind that card the
        snapshot has no job left: the TUI paints its own frame within seconds,
        and the snapshot is the PREVIOUS run's screen — hard-wrapped for
        whatever width that card had then, which nothing can reflow. An empty
        terminal that fills in a few seconds is what a restored hive looked
        like before snapshots existed, and snapshots were never meant to
        change it.

        Called EXPLICITLY by `MainWindow.autostart_active_workspace` for the
        agents it is about to start, because that is the only party that knows
        a start is the launch restore rather than a wake. It used to infer it
        from `_pending_replay` still being set — "this card has never rendered
        at a settled size" — which held only because the autostart ran ahead of
        `TerminalView`'s 120ms resize debounce. `MainWindow.settle_layout` now
        deliberately settles those sizes FIRST (a child must never paint at a
        width its card does not have), so that proxy no longer distinguishes
        anything and the caller says what it means instead. `_on_status` keeps
        it as a backstop for a card whose child starts before any layout.

        A card WOKEN by a keystroke is untouched: its conversation still
        scrolls up out of the way as a real terminal's would.

        Safe to call twice, and safe once the child has drawn: the real guard
        is `TerminalAgent.drop_seeded_screen`, which drops only a buffer that
        is still nothing but the snapshot."""
        if not self.is_pty or self.terminal is None:
            return          # a line-mode card has no screen to restore
        self._pending_replay = ""
        # the seed is gone, so the veil is no longer holding a screen back --
        # from here it belongs to the booting child (_on_status raises it
        # again a moment later and lifts it on prompt_ready)
        self._boot_seed = False
        # the settled-size projection must be cancelled too, not just the
        # signal: its backstop timer would otherwise fire a moment later and
        # re-project the very screen this just decided to drop
        self._settle_timer.stop()
        self._unhook_restored()
        # drop it on the AGENT too, or a card rebuilt later (a retile, a
        # workspace switch) replays the same stale seed under the child
        if self.agent.drop_seeded_screen():
            self.terminal.note_history_cleared()   # bypasses feed()'s check
            self.terminal.screen.reset()
            self.terminal.update()

    def _rerender_restored(self, *_) -> None:
        """One-shot: project the conversation at the card's settled size.

        The constructor only seeds a screenful (REPLAY_SEED_CAP) because it runs
        before the tiling grid sizes the card, and pyte neither reflows on resize
        nor keeps the lines it drops off the TOP when it shrinks -- so the newest
        part of a restored conversation is exactly what a pre-layout projection
        would lose. This is the projection that actually counts, and it is the
        ONLY full one: doing it in the constructor as well meant every card at
        launch and every retile paid the full cap twice, at a width that never
        reached the screen.

        Consumes `_pending_replay` first, so a resize storm (a retile, a sidebar
        toggle, a window drag) and the REPLAY_SETTLE_MS backstop between them can
        only ever project once.

        It projects the agent's CURRENT buffer, not the snapshot the constructor
        took. For an ordinary rebuild (a retile, a workspace switch) the buffer
        IS the live conversation, and projecting it is exactly what
        _reproject_on_size does on every width change; comparing against the
        constructor snapshot and bailing on any difference -- as an earlier
        version did -- would leave a busy agent's card showing nothing but the
        8 KiB seed, because a live child's buffer is different by the time the
        settled size arrives.

        Two cases are left alone. An EMPTY buffer: restart() clears it and the
        child owns the screen from there. And a restored snapshot a child has
        since drawn over (`seed_written_over`): that seed is the PREVIOUS run's
        screen, and an agent that comes back running is deliberately given a
        clean terminal it fills itself, so replaying the seed under it would put
        back the mangled fragment `drop_restored_screen` exists to remove."""
        self._pending_replay = ""
        seeded, self._boot_seed = self._boot_seed, False
        self._settle_timer.stop()
        self._unhook_restored()
        replay = self.agent.pty_replay()
        if not replay or self.agent.seed_written_over():
            return
        # screen.reset() wipes history WITHOUT going through feed(), so the
        # shrink check there never sees it -- tell the view explicitly, then
        # re-derive the milestones from the same replay.
        self.terminal.note_history_cleared()
        self.terminal.screen.reset()
        self._replay_with_marks(replay)
        self._proj_cols = self.terminal.screen.columns
        self._refresh_overlay()
        if seeded and not self.agent.is_running():
            # this projection IS the stopped card's final picture, so it is
            # the moment to dissolve. A card about to autostart passes through
            # here too (settle_layout flushes every resize before any child is
            # spawned), and does NOT flicker: agent.start() re-raises the veil
            # in the same call stack, and BootVeil.begin() stops the fader and
            # resets _fade, so the 260ms fade never paints a frame.
            self._end_boot_veil()

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

    def _reproject_on_size(self, _rows: int, cols: int) -> None:
        """Re-render the scrollback whenever the terminal's WIDTH changes.

        pyte does not reflow: a history line keeps the column count it had when
        it was pushed. That never mattered while Claude owned its own
        scrollback (history was always empty), but now that AI Hive keeps it,
        every width change leaves the old lines wrapped for a screen that no
        longer exists -- rendering as a short fragment with the wrapped
        remainder stranded out at the old right edge. The launch case is the
        one that bites: a card is built at its pre-layout width, the child
        paints into that, those lines scroll into history, and only then does
        the tiling grid give the card its real size.

        The raw pty stream is the truth and the screen is only a projection of
        it at one width, so the honest repair is to project again. Bounded by
        _replay_with_marks' cap, and skipped entirely when there is no
        scrollback to mangle."""
        if not self.is_pty or self._pending_replay:
            return          # the restored-screen path owns the first render
        if cols == self._proj_cols:
            return          # height-only change: wrapping is unaffected
        self._proj_cols = cols
        if not len(self.terminal.screen.history.top):
            return          # nothing captured at the old width yet
        replay = self.agent.pty_replay()
        if not replay:
            return
        self.terminal.note_history_cleared()
        self.terminal.screen.reset()
        self._replay_with_marks(replay)

    def _replay_with_marks(self, replay: str, cap: int | None = None,
                           recover: bool = True) -> None:
        """Feed a rebuilt/restored screen, re-deriving each prompt milestone's
        line as it goes.

        A card rebuild (retile, workspace switch) throws the TerminalView away,
        and the new one starts at `pushed == 0`, so no absolute line stored by
        the old view is portable. What IS portable is how far into the agent's
        pty stream each submit happened. Splitting the replay at those offsets
        reproduces the exact screen state of each submit -- the typed text was
        echoed as it was typed, so the input box is already painted -- and
        `anchor_line()` then returns what it returned live. One function over
        identical state on both sides is the whole trick.

        Do NOT "improve" this by extending a segment past the submit echo to
        catch the reprinted prompt: that breaks the symmetry and re-anchors to
        a different row than the live path used. The reprint lands a line or
        two below the input box, and `scroll_to_abs`'s lead absorbs it.

        Splitting mid-escape is safe -- feed() carries a trailing partial
        escape across calls.

        Only the TAIL is replayed, bounded by `cap`. Feeding a full 512 KiB
        buffer through pyte measures ~1.25s, unaffordable per resize across a
        hive of cards; the default REPLAY_PROJECT_CAP is the compromise
        documented beside the constant (it does NOT saturate HISTORY_LINES --
        an earlier note here claimed 96 KiB filled all 2000 lines, which
        re-measurement disproved). The constructor passes the much smaller
        REPLAY_SEED_CAP because its projection is always redone at the settled
        width. The cut is moved to the next escape so the projection never
        starts mid-sequence and prints an orphan parameter string as text.

        `recover=False` skips the transcript-backed milestone recovery, which
        the seed projection has no use for: it is replaced within ~120ms, and
        the read is O(whole transcript) on the launch path (90-165ms on a real
        50MB transcript). The settled projection does it once, for real."""
        if cap is None:
            cap = REPLAY_PROJECT_CAP
        skip = 0
        if len(replay) > cap:
            skip = len(replay) - cap
            esc = replay.find("\x1b", skip)
            skip = esc if esc >= 0 else skip
        self._mark_lines = {}
        self._reply_mark_lines = {}
        # merged into ONE pass over the (capped) replay text -- a second full
        # feed just for reply marks would double the pyte cost of every card
        # rebuild, exactly what _rerender_restored's single-projection rule
        # exists to avoid.
        tagged = ([(off, "prompt", mark) for off, mark in self.agent.replay_marks()] +
                  [(off, "reply", mark) for off, mark in self.agent.reply_replay_marks()])
        tagged.sort(key=lambda t: t[0])
        pos = skip
        for off, kind, mark in tagged:
            off = max(0, min(len(replay), off))
            if off < skip:
                continue        # its bytes are outside the projected window
            if off > pos:
                self.terminal.feed(replay[pos:off])
                pos = off
            if kind == "prompt":
                self._mark_lines[mark.uid] = self.terminal.anchor_line()
            else:
                line = self.terminal.reply_anchor_line()
                if line is not None:
                    self._reply_mark_lines[mark.uid] = line
        if pos < len(replay):
            self.terminal.feed(replay[pos:])
        if recover:
            rows = self._scrollback_rows()
            self._recover_marks(rows)
            self._recover_reply_marks(rows)
        self._refresh_marks()
        self._refresh_reply_marks()

    def _recover_marks(self, rows=None) -> None:
        """Find the user's earlier prompts in a scrollback we did not watch
        being typed, and mark those lines too.

        Milestones are minted from a keystroke, so they are only ever created
        while this process is watching. A conversation restored from disk (or
        one already going when the feature arrived) therefore has none, which
        is exactly the conversation long enough to want to jump around in.

        The prompt TEXTS come from the transcript, which is authoritative about
        what the user actually typed, so nothing here can invent a milestone --
        the worst case is failing to locate one. Matching is deliberately loose
        on the line side and strict on the prompt side: the classic renderer
        can drop the leading character of the echoed prompt (measured) and
        wraps long ones, so we look for a normalized HEAD of each prompt
        anywhere in a line, take the first hit, and carry on from the next line
        so prompts match in order and a repeated prompt cannot claim an earlier
        line twice.

        The prompt TEXTS are cached per conversation, and that is a correctness
        no-op as well as the difference between a smooth retile and a visible
        freeze. This runs on EVERY projection (card build, and every width
        change via _reproject_on_size), and the read is O(whole transcript):
        transcripts keeps an (mtime,size) cache, but a LIVE agent rewrites its
        transcript continuously, so that cache misses precisely for the agents
        being used -- MEASURED at 90-165ms on the user's real 50MB transcripts,
        on the GUI thread, per card. Re-reading inside one conversation can only
        turn up prompts typed since the card was built, and the card WATCHED
        those being typed, so they already have live marks that outrank a
        recovered one in _refresh_marks. A new conversation changes session_id
        (a pin change or /clear), which changes the key and re-reads."""
        self._recovered = []
        spec = self.agent.spec
        if not self.is_pty or spec.provider not in ("claude", "gemini"):
            return
        key = (spec.provider, spec.cwd, spec.session_id)
        if key != self._recover_key:
            self._recover_key = key
            if spec.provider == "claude":
                self._recover_prompts = transcripts.typed_prompts(
                    spec.cwd, spec.session_id)
                self._recover_replies = transcripts.reply_times(
                    spec.cwd, spec.session_id)
            elif spec.provider == "gemini":
                self._recover_prompts = transcripts.gemini_typed_prompts(
                    spec.session_id)
                self._recover_replies = transcripts.gemini_reply_times(
                    spec.session_id)
            else:
                self._recover_prompts = []
                self._recover_replies = []
        prompts = self._recover_prompts
        if not prompts:
            return
        oldest, raw = rows if rows is not None else self._scrollback_rows()
        if not raw:
            return
        lines = [_norm_line(t) for t in raw]
        heads = []
        for text in prompts:
            head = _norm_line(text)[:_MARK_MATCH_CHARS]
            if len(head) >= 6:      # too short to identify a line safely
                heads.append((head, text))
        at = 0
        for head, text in heads:
            for i in range(at, len(lines)):
                # the renderer can lose the first character of the echo, so a
                # match on the head MINUS its first char counts too
                if head in lines[i] or head[1:] in lines[i]:
                    self._recovered.append((oldest + i, text))
                    at = i + 1
                    break

    def _scrollback_rows(self) -> tuple[int, list[str]]:
        """(absolute id of the first row, raw text of every row) across history
        THEN the live screen. Their ids are contiguous (oldest + len(hist) ==
        pushed) and a short conversation may not have scrolled anything off
        yet, so history alone would find nothing. Shared by both recoveries so
        they can never disagree about which line is which."""
        view = self.terminal
        hist = list(view.screen.history.top)
        cols = view.screen.columns
        rows = hist + [view.screen.buffer[r] for r in range(view.screen.lines)]
        oldest = view.history_pushed() - len(hist)
        return oldest, ["".join(ln[c].data or " " for c in range(cols))
                        for ln in rows]

    def _recover_reply_marks(self, rows=None) -> None:
        """Find where each finished reply ENDED in a scrollback we did not
        watch, and stamp those lines with the time the transcript says that
        reply finished.

        The counterpart of _recover_marks, and it exists for a sharper reason:
        a reply mark is minted from a busy -> idle settle of a turn somebody
        submitted (TerminalAgent._note_submit), so a conversation restored from
        disk comes back with NONE and a reopened hive would show no reply time
        anywhere at all. The transcript is the only record of when those turns
        actually finished.

        MEASURED, and it shapes the anchor: Claude's own "<verb> for Ns" footer
        -- what the LIVE mark anchors to -- does NOT survive per turn into the
        scrollback. Across seven real captured screens at five widths, at most
        ONE footer was still present in 2000 lines of history and usually none:
        the renderer erases that region when the next turn starts. So a
        recovered stamp is anchored to the reply's own last line instead, which
        is committed output and stays put.

        Matching keeps _recover_marks' discipline -- loose on the line side,
        strict on the transcript side, first hit wins, the scan carries on
        below so replies match in file order -- and differs in two ways that
        _reply_end_row explains: it matches the TAIL of a reply's last line
        rather than its head, and markdown syntax is dropped from both sides,
        because the renderer restyles `code`/**bold** rather than printing the
        characters. The worst case is failing to LOCATE a reply, never
        inventing a time for one."""
        self._recovered_replies = []
        spec = self.agent.spec
        if not self.is_pty or spec.provider not in ("claude", "gemini"):
            return
        if not self._recover_replies:
            return
        oldest, raw = rows if rows is not None else self._scrollback_rows()
        if not raw:
            return
        lines = [_norm_reply_line(t) for t in raw]
        at = 0
        for when, text in self._recover_replies:
            tail = _norm_reply_line(_last_content_line(text))[-_REPLY_TAIL_CHARS:]
            if len(tail) < 8:   # too short to identify a line safely
                continue
            row = _reply_end_row(lines, tail, at)
            if row is None:
                continue
            self._recovered_replies.append((oldest + row, when))
            # carry on BELOW this reply, so replies match in file order and a
            # repeated closing line cannot claim an earlier reply's row
            at = row + 1

    def _refresh_marks(self) -> None:
        """Push the agent's milestones to the view in THIS view's coordinates.
        A mark with no line recorded for this card is skipped rather than
        guessed at."""
        if not self.is_pty:
            return
        live = [(self._mark_lines[m.uid], m.text)
                for m in self.agent.prompt_marks() if m.uid in self._mark_lines]
        # A milestone captured live is exact; a recovered one was located by
        # matching text, so where both point at the same line the live one
        # wins and the recovered duplicate is dropped.
        taken = {line for line, _ in live}
        merged = live + [(line, text) for line, text in self._recovered
                         if line not in taken]
        self.terminal.set_marks(sorted(merged))

    def _on_prompt_submitted(self, text: str, abs_line: int) -> None:
        """A bare Enter in the terminal. Record it as a milestone unless the
        agent is parked on a question, where an Enter is an ANSWER (a menu
        selection, a plan approval) rather than a prompt of the user's own.
        `agent.is_waiting()` is the authoritative half of that guard -- it is
        hook-driven and fires before the tool even renders -- and the view adds
        the numbered-option reject for the classic menus."""
        if self.agent.is_waiting():
            return
        mark = self.agent.note_prompt_submitted(text)
        if mark is not None:
            self._mark_lines[mark.uid] = abs_line
            self._refresh_marks()

    def _on_history_cleared(self) -> None:
        """The scrollback was wiped under us (ED 3 / a reset), so every
        milestone anchored into it is meaningless."""
        self._mark_lines = {}
        self._recovered = []
        self.agent.clear_prompt_marks()
        self._reply_mark_lines = {}
        self._recovered_replies = []
        self.agent.clear_reply_marks()

    def _refresh_reply_marks(self) -> None:
        """Push the agent's reply-finished milestones to the view, in THIS
        view's coordinates -- same shape as _refresh_marks. The stamp text is
        formatted at REFRESH time (not capture time), so a reply from
        yesterday keeps reading as date-prefixed today rather than freezing
        whatever "same day" looked like the moment it was captured.

        Live marks and recovered ones are merged by ROW, and where both are
        the same reply the split is deliberate: the live mark keeps the row
        (it anchored the screen it was looking at) and the TRANSCRIPT supplies
        the time. Claude stamps every record it writes, whereas a live mark
        reads the wall clock at the settle -- a couple of seconds late at best,
        and flatly wrong for any settle that was not a reply at all. That
        makes a stray live stamp self-correcting: the next projection recovers
        the same reply and the recorded time replaces the observed one.

        "The same reply" is NEAREST ROW WITHIN _STAMP_MERGE_SLACK, not an exact
        match, and the difference is a stamp that contradicts itself. The two
        anchors agree on the ordinary screen, but they fall back to DIFFERENT
        rows when the row under Claude's turn footer is not blank: recovery
        takes the blank row above the footer, the live path takes the footer
        row itself. Requiring equality left both in the merge, so one reply
        wore two stamps a couple of rows apart reading different times -- and
        the live one, the one whose time is only an observation, is the one
        that renders, since a footer row usually has room at its right edge.
        Matching is greedy over the live marks in row order and each recovered
        reply is claimed at most once, so a recovered stamp can never be
        counted twice or absorb a neighbouring turn's."""
        if not self.is_pty:
            return
        by_uid = {m.uid: m for m in self.agent.reply_marks()}
        recovered = dict(self._recovered_replies)
        claimed: set[int] = set()
        merged: list[tuple[int, float]] = []
        for line, uid in sorted((ln, u) for u, ln in
                                self._reply_mark_lines.items() if u in by_uid):
            near = [r for r in recovered if r not in claimed
                    and abs(r - line) <= _STAMP_MERGE_SLACK]
            if near:
                match = min(near, key=lambda r: (abs(r - line), r))
                claimed.add(match)
                merged.append((line, recovered[match]))
            else:
                merged.append((line, by_uid[uid].ts))
        merged += [(line, when) for line, when in recovered.items()
                   if line not in claimed]
        self.terminal.set_reply_marks(
            sorted((line, _format_reply_stamp(when)) for line, when in merged))

    def _on_reply_mark_added(self) -> None:
        """A reply-finished milestone was recorded on the agent (or the set
        was cleared, e.g. a restart). Only the newest mark can ever have moved
        here, so the CARD only has to re-read the tail; every older mark keeps
        whatever line _replay_with_marks (or an earlier call here) gave it.

        The newest one is re-anchored on EVERY signal rather than only the
        first time it is seen, because a turn that settles again moves its own
        mark forward (see TerminalAgent.note_reply_settled) and the stamp has
        to follow it to where the reply actually ended. When the anchor cannot
        be found the mark keeps the row it already had, so a settle on an
        awkward screen never costs a stamp that was already placed."""
        if not self.is_pty:
            return
        marks = self.agent.reply_marks()
        if not marks:
            self._reply_mark_lines = {}
            self._refresh_reply_marks()
            return
        line = self.terminal.reply_anchor_line()
        if line is not None:
            self._reply_mark_lines[marks[-1].uid] = line
        self._refresh_reply_marks()

    def _place_overlay(self) -> None:
        if not self.is_pty:
            return
        self.boot.setGeometry(self.terminal.rect())
        # pinned to the right edge, full height. Because it is a CHILD of the
        # terminal it costs no columns: _apply_resize still derives the child's
        # width from terminal.width(), so nothing re-wraps.
        sb_w = self.scroll_bar.width()      # fixed by the widget itself
        self.scroll_bar.setGeometry(max(0, self.terminal.width() - sb_w), 0,
                                    sb_w, self.terminal.height())
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

    def _on_activity(self, busy: bool) -> None:
        self._on_status(self.agent.status)
        if not busy:
            self._rescan_recovery()

    def _rescan_recovery(self) -> None:
        """Look for the milestones of a conversation that was reprinted AFTER
        the card was built, once the screen has settled.

        Milestone recovery normally rides a PROJECTION (_rerender_restored on a
        card build, _reproject_on_size on a width change), which is the right
        place for it: the scan has to run against the screen the marks will be
        drawn on. The launch autostart has neither. `drop_restored_screen`
        cancels the settled-size projection for every agent it is about to
        start (that snapshot is the previous run's screen, hard-wrapped for a
        width nothing can reflow), and `_reproject_on_size` bails while the
        history is still empty -- which it is, because `settle_layout` sizes
        the card BEFORE the child is spawned. So the only projection a restored
        RUNNING card gets is the constructor's, and that runs before the child
        has printed a single byte.

        The conversation then arrives seconds later, reprinted by `--resume`,
        with nothing left to scan it. Every stamp and every prompt dot in a
        reopened hive was therefore missing -- which went unnoticed only
        because a phantom live mark used to appear on the last reply instead,
        stamped with the launch time (see TerminalAgent._note_submit). Fixing
        that revealed this.

        Bounded, because the scan is MEASURED at ~35 ms over a full 2000-row
        history and a settle fires every couple of seconds while an agent
        works: it runs only while nothing has been recovered yet, and gives up
        after _RECOVER_RESCAN_TRIES settles. A scan that finds anything found
        everything findable -- the reprint lands in one go -- so success ends
        it for good, and a conversation whose replies have all scrolled out of
        reach stops asking rather than re-scanning forever."""
        if not self.is_pty or self._recover_tries <= 0:
            return
        if self._recovered or self._recovered_replies:
            self._recover_tries = 0     # already anchored: never scan again
            return
        self._recover_tries -= 1
        rows = self._scrollback_rows()
        self._recover_marks(rows)
        self._recover_reply_marks(rows)
        self._refresh_marks()
        self._refresh_reply_marks()

    def _on_status(self, status: AgentStatus) -> None:
        busy = bool(getattr(self.agent, "is_busy", lambda: False)())
        state = "busy" if (busy and status is AgentStatus.RUNNING) else _GLYPH_STATE.get(status, "idle")
        self.glyph.setProperty("state", state)
        repolish(self.glyph)
        running = status in (AgentStatus.STARTING, AgentStatus.RUNNING)
        exit_info = self.agent.worker.exit_info
        tip = "working…" if (busy and status is AgentStatus.RUNNING) else f"{status.value}"
        if exit_info and not running:
            tip += f" (code {exit_info[0]})"
        self.glyph.setToolTip(tip)

        # A start that RESUMES a conversation reprints that whole conversation
        # itself, so the snapshot underneath it is a duplicate — and one
        # hard-wrapped for whatever width the card had in the PREVIOUS session,
        # which nothing downstream can reflow. That is the same half-width
        # scrollback `MainWindow.settle_layout` exists to prevent, arriving by
        # the one door the launch autostart does not cover: a stopped card the
        # user wakes with a keystroke. `spec.resume` is still readable here —
        # `start()` clears it just after the worker launches (see
        # `_begin_boot_veil`). A plain pty shell reprints nothing, so its
        # conversation is kept and scrolls up as a real terminal's would.
        if self.is_pty and running and (self._pending_replay
                                        or self.agent.spec.resume):
            self.drop_restored_screen()

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
            elif not running and not self._boot_seed:
                # a RESTORED card that stays stopped keeps the veil until its
                # settled-width projection lands (_rerender_restored); the
                # wake banner owns the state only once there is something
                # readable under it
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
