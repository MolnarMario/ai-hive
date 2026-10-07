"""MainWindow: the shell — top bar, sidebar, page stack, and ALL wiring.

This is the only module that imports both the model and the view layers.
Every dialog lives here so the model API stays headless-testable.
"""

import os
import json
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

from PySide6.QtCore import QEvent, QPoint, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QProgressDialog, QScrollArea, QSplitter, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from .. import chime
from .. import claude_usage
from .. import codex_usage
from .. import fsopen
from .. import lanes
from .. import limit_ledger
from .. import providers
from .. import repo_activity
from ..limit_banner import LIMIT_PROVIDERS, SEVEN_DAY_WINDOWS
from .. import scheduled_send
from .. import session_hook
from .. import transcripts
from .. import ui_theme
from .. import usage_poll
from ..process_worker import (AI_KINDS, PTY_ONLY_KINDS, AgentKind, build_spec)
from ..pty_worker import HAS_CONPTY
from ..session_store import SessionStore
from ..terminal_agent import AgentStatus
from ..workspace_manager import (MAX_AGENTS_PER_WORKSPACE, Workspace,
                                 WorkspaceManager)
from .. import coordination
from .. import event_log
from ..event_hub import EventHub
from ..lane_ops import LaneOps
from .. import lane_service as lane_svc
from ..lane_service import LaneService
from ..orchestrator_bridge import OrchestratorBridge
from .activity_panel import PANEL_WIDTH as ACTIVITY_PANEL_WIDTH, ActivityPanel
from .agent_file_map import AgentFileMapWindow
from .event_log_window import EventLogWindow
from .lanes_help import show_lanes_explainer
from . import ornaments
from .ornaments import (DropDownComboBox, LogoRoundel,
                        PageBorder, PlanUsageBadge, RefreshGlyphButton,
                        ToggleSwitch, close_on_anchor_press)
from .options_panel import OptionsPanel
from .sidebar import SIDEBAR_WIDTH, Sidebar, WorkspaceRail

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

# how often to poll each running agent's Job Object process count, to notice
# a background command still running after the agent itself has gone quiet
# (drives the gear badge). A single cheap syscall per agent, so a short
# interval is fine.
BG_SHELL_POLL_MS = 2000

# Every usage pill polls on ONE shared policy, in `app.usage_poll`: 90 s while
# that provider's agents are working, 30 s from 90% used, 6 minutes while none
# are. The cadence, the quick retry, the 429 backoff, the wake-from-sleep poll
# and the reset poll all live there, so a pill added later cannot get its own.
# how often the pills re-render their countdowns from the clock alone (no
# network). Repaints only when the displayed string changes. The pollers ride
# the same tick to follow agent activity and to notice the machine waking.
USAGE_TICK_MS = 20000

# The usage readouts the top bar can show, and the order they sit in. PER PILL
# rather than per provider: each window (Claude 5h/7d, Gemini 5h/7d) is its own
# pill on the bar, so anything coarser would leave the X on one of them closing
# another. Persisted per key under ui.usage_trackers, so a user who runs only
# Claude (or only Gemini) is not made to look at a readout that can never say
# anything.
USAGE_TRACKER_KEYS = ("claude_five_hour", "claude_weekly",
                      "gemini_five_hour", "gemini_weekly", "codex_five_hour")
USAGE_TRACKER_LABELS = {
    "claude_five_hour": "Claude 5 hour usage",
    "claude_weekly": "Claude weekly usage",
    "gemini_five_hour": "Gemini 5 hour usage",
    "gemini_weekly": "Gemini weekly usage",
    "codex_five_hour": "Codex 5 hour usage",
}
DEFAULT_USAGE_TRACKERS = {k: True for k in USAGE_TRACKER_KEYS}

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

# The transcript sweep (`_sweep_transcript_cut_offs`) only adopts cut-offs that
# happened while THIS process was running. Anything older belongs to startup
# recovery, which deliberately arms only the newest window; a sweep that
# ignored the boundary would revive exactly the conversations it skipped. The
# slack covers a cut-off written in the seconds before the window came up.
LIMIT_SWEEP_SLACK_S = 30

# How many consecutive "TUI not ready" watchdog ticks (LIMIT_WATCH_MS apart) a
# latched agent may spend before we stop waiting for a frame and ask for one —
# see TerminalAgent.request_repaint. Three minutes is well past any real TUI
# launch, so a genuinely booting agent is never poked, while an agent whose
# card has never been on screen no longer waits for the user to click it.
LIMIT_REPAINT_AFTER_WAITS = 3

# --- taskbar working-count overlay ---
# `workspaceStatsChanged` fires every couple of seconds PER BUSY AGENT, and each
# push costs a COM round trip plus a fresh HICON, so the recompute is coalesced
# behind a single-shot timer and then edge-guarded on the rendered key. Short
# enough that "everything went quiet" still reaches the taskbar promptly, which
# is half the point of the feature.
TASKBAR_BADGE_MS = 400

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


# A CAP on the width the top bar's extras row may demand of the window, not a
# floor: whatever QAbstractScrollArea asks for is used when it is smaller
# (measured 54px), and this only stops a future style with a chunkier
# scrollbar or frame from quietly raising the whole window's minimum width -
# which is the property the scroll area exists to protect.
_EXTRAS_MIN_W = 120


class _AutoSizingScrollContent(QWidget):
    """A QScrollArea content widget that keeps itself sized to its own
    layout's sizeHint, for a QScrollArea built with `setWidgetResizable(False)`.

    Verified live: Qt does NOT do this automatically for such a widget. A
    usage pill starts hidden and only gains its real (much wider) size once a
    reading arrives; without this, `_extras` stayed frozen at the narrower
    size it happened to have when it was last laid out, and its own QHBoxLayout
    crammed the newly-widened pill into that stale rect, overlapping its
    neighbour (reported live - two usage pills drawing on top of each other).
    Catching `QEvent.LayoutRequest` - the event Qt already sends this widget
    whenever ITS OWN layout invalidates - covers every cause of a size change
    (a pill's reading, a pill closed or reopened, a theme's font swap)
    without having to remember each call site."""

    def event(self, e):
        if e.type() == QEvent.Type.LayoutRequest:
            # The MINIMUM is the belt to `adjustSize`'s braces. Resizing to
            # the sizeHint is a one-shot: anything that sizes this widget
            # afterwards (a scroll area rebuilding its layout, a future caller
            # that means well) can still leave it narrower than the row it
            # holds, and then the pills at its right-hand end are simply cut
            # off by its edge - the shape of the overlap bug this class was
            # written for. `resize()` and `setGeometry()` are both clamped to
            # `minimumWidth`, so pinning it to the row's real width takes that
            # state off the table instead of relying on nobody reaching for
            # it. The WINDOW stays free to be narrower than the row, because
            # the scroll area's own minimum is capped separately (see
            # `_HWheelScrollArea.minimumSizeHint`) and the row just scrolls.
            lay = self.layout()
            if lay is not None:
                self.setMinimumWidth(lay.sizeHint().width())
            self.adjustSize()
            # ...and tell the scroll area, because the area's own sizeHint is
            # a function of THIS widget's (see `_HWheelScrollArea.sizeHint`).
            # Without this the row would keep whatever width the outer layout
            # gave it before the pill arrived and scroll instead of growing.
            host = self.parentWidget()
            while host is not None and not isinstance(host, QScrollArea):
                host = host.parentWidget()
            if host is not None:
                host.updateGeometry()
        return super().event(e)


class _HWheelScrollArea(QScrollArea):
    """A QScrollArea whose ordinary (vertical) mouse wheel scrolls this row
    HORIZONTALLY. There is no vertical scrollbar here - the row is one line
    tall - so the plain wheel gesture would otherwise do nothing at all,
    leaving a 10px scrollbar handle as the ONLY way to reach whatever scrolled
    out of view (reported live as controls simply "disappearing"). Just
    hovering the row and scrolling reaches them instead, no precision
    drag-and-hunt required.

    It also ASKS FOR ITS CONTENT'S FULL WIDTH, which is the whole point of
    putting the row in a scroll area rather than a plain widget and is the one
    thing the first cut of this got wrong. `QAbstractScrollArea.sizeHint()` is
    a SMALL CONSTANT that has nothing to do with what is inside it (measured:
    468px whatever the row's real 1860px content), so with a bare stretch
    holding the layout's slack, EVERY pixel of a wider window went to that
    stretch and this row stayed frozen at 468px - identically on a 1280px
    laptop and a 3440px ultrawide. Reported live on a 1440p monitor as most of
    the top bar's controls simply being gone, behind a permanent scrollbar
    that looked like a stray divider. (The row carried every settings control
    then; it holds only the usage pills and their picker now, which is what
    the Options popup was for - but four pills at their real widths still
    outrun a laptop's spare bar width, so all of this still applies.)

    So `sizeHint` reports the content's natural width and `minimumSizeHint`
    stays small. A QHBoxLayout satisfies size HINTS before handing anything to
    a stretch, so the row now gets its full width whenever the window can
    afford it and the bar's own `addStretch(1)` absorbs the slack, keeping the
    cluster right-aligned exactly as it was before the scroll area existed.
    When the window cannot afford it, the layout shrinks towards minimums -
    the stretch to nothing first, then this row, which grows its scrollbar.
    That is what keeps the WINDOW's minimum width a small constant instead of
    the sum of everything the bar can show."""

    def sizeHint(self):
        hint = super().sizeHint()
        content = self.widget()
        if content is not None:
            hint.setWidth(max(content.sizeHint().width(),
                              content.minimumSizeHint().width()))
        return hint

    def minimumSizeHint(self):
        # deliberately NOT the content's: this is the number that decides how
        # narrow the whole window may be
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), _EXTRAS_MIN_W))
        return hint

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
        else:
            super().wheelEvent(event)


class PageStack(QStackedWidget):
    """A page stack that gives EVERY workspace a real size, not just the one on
    screen.

    Qt's `QStackedLayout` lays out the CURRENT page only, so a workspace the
    user has not opened yet never gets a geometry: its cards keep
    `TerminalView`'s pre-layout default column count (100), and since the
    launch autostart brings back EVERY workspace's agents, each of those
    children paints its whole resumed conversation at a width its card does
    not have. Nothing downstream can repair that — Claude hard-wraps its own
    text, so re-projecting the raw stream at the real width (what
    `TerminalCard._reproject_on_size` does) cannot re-flow lines the child
    already broke. Opening that workspace a minute later therefore revealed a
    conversation wrapped for a screen that never existed, using a fraction of
    the card's width, with the live tail below it in full width.

    Laying a hidden page out needs Qt to consider it visible, and
    `WA_DontShowOnScreen` grants exactly that without the page ever reaching
    the screen — it is shown and hidden again inside one call, so nothing
    paints and the stack's own idea of which page is current is untouched.
    Debounced, because the trigger is a window drag."""

    LAYOUT_DEBOUNCE_MS = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hidden_layout_timer = QTimer(self)
        self._hidden_layout_timer.setSingleShot(True)
        self._hidden_layout_timer.setInterval(self.LAYOUT_DEBOUNCE_MS)
        self._hidden_layout_timer.timeout.connect(self.layout_hidden_pages)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._hidden_layout_timer.start()

    def layout_hidden_pages(self) -> None:
        """Hand every off-screen page the current page's rect."""
        self._hidden_layout_timer.stop()
        rect = self.contentsRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return          # not laid out yet; the next resize does it
        current = self.currentWidget()
        for i in range(self.count()):
            page = self.widget(i)
            if page is current or page.isVisible():
                continue
            page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            page.setGeometry(rect)
            page.show()     # the layout pass — never reaches the screen
            page.hide()
            page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)


def usage_sources() -> tuple:
    """Every polled usage readout, and the only place one is declared.

    A NEW PILL GOES HERE: its fetch, the agent providers whose work moves its
    number, and its tracker keys. `usage_poll.UsagePoller` then gives it the
    same cadence, retries, backoff, wake poll, reset poll and grey rule as
    every other pill. `MainWindow.__init__` refuses to build if a key in
    USAGE_TRACKER_KEYS has no source here, so a pill cannot reach the bar
    with no poll behind it.

    The fetches are looked up at call time, so a test that swaps a module's
    `fetch` sees its stub used."""
    from .. import gemini_usage
    return (
        usage_poll.UsageSource(
            name="claude", fetch=lambda: claude_usage.fetch(),
            agent_providers=("claude",),
            tracker_keys=("claude_five_hour", "claude_weekly"),
            always=True),
        usage_poll.UsageSource(
            name="gemini", fetch=lambda: gemini_usage.fetch(),
            agent_providers=("gemini",),
            tracker_keys=("gemini_five_hour", "gemini_weekly")),
        usage_poll.UsageSource(
            name="codex", fetch=lambda: codex_usage.fetch(),
            agent_providers=("openai",),
            tracker_keys=("codex_five_hour",)),
    )


class TopBar(QFrame):
    addTerminalClicked = Signal()
    sidebarToggleClicked = Signal()
    globalFontDelta = Signal(int)
    themeChanged = Signal(str)   # theme id
    soundToggled = Signal(bool)  # question chime enabled/muted
    replySoundToggled = Signal(bool)  # reply-finished chime enabled/muted
    # the chime rows' note-button menu, each carrying the chime kind
    # (chime.QUESTION / chime.REPLY). TopBar only asks; MainWindow opens the
    # file dialog, copies the file and persists it.
    chimeSoundChooseRequested = Signal(str)
    chimeSoundResetRequested = Signal(str)
    chimePreviewRequested = Signal(str)
    # show/hide ONE usage readout: (tracker key, wanted). One signal for both
    # affordances - the X on a pill and the + menu - so the two controls of the
    # same preference can never disagree.
    usageTrackerToggled = Signal(str, bool)
    # should Claude agents run the classic renderer, so their scrollback (and
    # therefore the terminal scrollbar and its prompt milestones) is ours?
    terminalScrollbackToggled = Signal(bool)
    taskbarBadgeToggled = Signal(bool)     # show/hide the taskbar count overlay
    usageLeftToggled = Signal(bool)        # usage pills say "% left", not "% used"
    # install newer Claude Code / agy CLIs at the NEXT startup, before any
    # agent launches (the only moment those binaries are not locked)
    autoUpdateToggled = Signal(bool)
    # "Lanes in every workspace": does every new agent start with Own lane
    agentLanesToggled = Signal(bool)
    # the down-arrow button: open the Updates panel (the install-method control
    # and the startup-check checkbox live there, so the bar gains no button)
    updatesPanelRequested = Signal()
    autoContinueToggled = Signal(bool)     # resume cut-off agents at the reset
    startupRecoveryToggled = Signal(bool)  # recover cut-off agents on startup
    usageRefreshRequested = Signal()       # user clicked the readout
    eventLogClicked = Signal()             # open the event log window
    appUpdateClicked = Signal()            # check for / show an AI Hive update

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

        self._logo = LogoRoundel(self)  # gilt roundel in illuminated themes, ⬡ else
        self._logo.setObjectName("Logo")
        self._name = QLabel("AI Hive", self)
        self._name.setObjectName("AppName")
        self._version = QLabel(f"v{__version__}", self)
        self._version.setObjectName("VersionBadge")
        self.app_update_btn = RefreshGlyphButton(self)
        self.app_update_btn.setObjectName("AppUpdateBtn")
        self.app_update_btn.setAccessibleName("Check for AI Hive updates")
        # as tall as the 42px bar allows while keeping 4px clear above and
        # below, so the hover frame never touches the bar's gilt bottom rule
        self.app_update_btn.setFixedHeight(self.APP_UPDATE_BTN_SIDE)
        self.app_update_btn.setMinimumWidth(self.APP_UPDATE_BTN_SIDE)
        self.app_update_btn.clicked.connect(self.appUpdateClicked)
        self.set_app_update_state()

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

        self.font_dec_btn = font_btn("A−", "Decrease font size (all agents)",
                                     -1)
        self.font_inc_btn = font_btn("A+", "Increase font size (all agents)", +1)

        # Every switch below is one row of the Options panel: a plain-words
        # label (icon + name) on the left, an actual track-and-thumb
        # `ToggleSwitch` on the right — green/slid-right when armed, grey/
        # slid-left when off. This replaced a whole-row button carrying an
        # LED glyph in its own text: it read as a clickable link rather than
        # a switch (reported live), and a real switch says on/off in a shape
        # everyone already recognizes without reading the row at all.
        # `ToggleSwitch` mirrors `QAbstractButton`'s API (`isChecked`,
        # `setChecked`, `click`, `clicked`), so every caller below is
        # unchanged beyond the type.

        def toggle_label(text: str) -> QLabel:
            label = QLabel(text, self)
            label.setObjectName("OptionsRowLabel")
            return label

        # notification-chime mute toggle: rings when an agent raises "?"
        # (settles on a question). The bell glyph carries the state too.
        self._sound_on = True
        self.sound_label = toggle_label("")
        self.sound_btn = ToggleSwitch(self)
        self.sound_btn.clicked.connect(self._on_sound_clicked)
        self._refresh_sound_btn()

        # reply-finished chime: the second, rising-then-held sound, for "an
        # agent finished what you asked". OFF by default, since a busy hive
        # finishes replies far more often than it asks questions.
        self._reply_sound_on = False
        self.reply_sound_label = toggle_label("")
        self.reply_sound_btn = ToggleSwitch(self)
        self.reply_sound_btn.clicked.connect(self._on_reply_sound_clicked)
        self._refresh_reply_sound_btn()

        # ...and a note button beside each of those two switches, for picking
        # the user's own sound. The file name behind each (empty = built-in)
        # is only for the tooltips; MainWindow owns the real path.
        self._custom_sound_names = {chime.QUESTION: "", chime.REPLY: ""}
        self.chime_sound_btns = {}
        for kind in chime.KINDS:
            btn = QToolButton(self)
            btn.setObjectName("OptionsAction")
            btn.setProperty("compact", True)
            btn.setFixedHeight(18)
            btn.setText("♪")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _c=False, k=kind: self._open_chime_sound_menu(k))
            self.chime_sound_btns[kind] = btn
        self._refresh_chime_sound_btns()

        # taskbar working-count overlay toggle. Sits next to the chime because
        # both are the same kind of thing: a signal that reaches the user when
        # the window is NOT the one they are looking at.
        self._taskbar_badge = True
        self.taskbar_label = toggle_label("")
        self.taskbar_btn = ToggleSwitch(self)
        self.taskbar_btn.clicked.connect(self._on_taskbar_clicked)
        self._refresh_taskbar_btn()

        # startup CLI auto-update. Default OFF and one click to arm, like the
        # two recovery switches: it mutates installed software unattended, so
        # it is the user's decision, made once and persisted.
        self._auto_update = False
        self._install_state = ""
        self.auto_update_label = toggle_label("")
        self.auto_update_btn = ToggleSwitch(self)
        self.auto_update_btn.clicked.connect(self._on_auto_update_clicked)
        self._refresh_auto_update_btn()

        # "Lanes in every workspace". Default OFF: armed, it ticks "Own lane" for every new
        # Claude agent in every git workspace, so it is the user's decision,
        # made once and persisted. Each workspace has its own toggle too.
        self._agent_lanes = False
        self.agent_lanes_label = toggle_label("")
        self.agent_lanes_btn = ToggleSwitch(self)
        self.agent_lanes_btn.clicked.connect(self._on_agent_lanes_clicked)
        self._refresh_agent_lanes_btn()

        # The detected Claude Code install method, under the switch it explains.
        # It used to be reachable only by hovering the down-arrow; the panel has
        # room to state it.
        self.install_label = QLabel("", self)
        self.install_label.setObjectName("RecoveryLabel")
        self.install_label.setWordWrap(True)
        self.install_label.setVisible(False)

        # ...and the everything-about-updates door. Separate from the switch
        # above because the two do different things: one arms the startup gate,
        # the other opens the install-method control.
        self.updates_manage_btn = QToolButton(self)
        self.updates_manage_btn.setObjectName("OptionsAction")
        self.updates_manage_btn.setText("Manage...")
        self.updates_manage_btn.setToolTip(
            "Open the Updates panel: how Claude Code is installed, and whether "
            "it may update itself in the background.")
        self.updates_manage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.updates_manage_btn.clicked.connect(self.updatesPanelRequested)

        # ...and the quiet report from the LAST gated startup. Hidden unless
        # something needs saying (an update that could not apply), never a
        # chime: it answers "why am I still seeing the update banner?" without
        # interrupting, the same principle as the usage badge's can't-read pill.
        # It lives inside the panel, so `note_update_pending` also lights the
        # Options button: a warning nobody can see is not a warning.
        self.update_pill = QLabel("", self)
        self.update_pill.setObjectName("UpdatePill")
        self.update_pill.setVisible(False)

        # The four usage readouts: "21% used, resets in 1h20m at 14:49". All
        # start hidden and EMPTY - no cached figure is ever painted (see
        # `MainWindow.start_usage_polling`), so a pill appears when polling puts
        # it into its loading state and stays only while it has something to
        # say and the user wants it.
        self.usage_badge = PlanUsageBadge(self, window="five_hour")
        self.usage_weekly_badge = PlanUsageBadge(self, window="weekly")
        self._scrollback_wanted = True  # AI Hive owns the terminal scrollback

        # Gemini rate-limit usage readout (5-hour limit and weekly limit pills)
        from .gemini_usage_badge import GeminiUsageBadge
        self.gemini_badge = GeminiUsageBadge(self, window="five_hour")
        self.gemini_weekly_badge = GeminiUsageBadge(self, window="weekly")
        from .codex_usage_badge import CodexUsageBadge
        self.codex_badge = CodexUsageBadge(self)

        self._usage_pills = {
            "claude_five_hour": self.usage_badge,
            "claude_weekly": self.usage_weekly_badge,
            "gemini_five_hour": self.gemini_badge,
            "gemini_weekly": self.gemini_weekly_badge,
            "codex_five_hour": self.codex_badge,
        }
        self._trackers = dict(DEFAULT_USAGE_TRACKERS)
        # "Show usage left": every pill states what remains instead of what
        # is spent. Default OFF, the wording the pills always had.
        self._usage_left = False
        self.usage_left_label = toggle_label("")
        self.usage_left_btn = ToggleSwitch(self)
        self.usage_left_btn.clicked.connect(self._on_usage_left_clicked)
        self._refresh_usage_left_btn()
        for key, pill in self._usage_pills.items():
            pill.setVisible(False)
            pill.refreshRequested.connect(self.usageRefreshRequested)
            pill.closeRequested.connect(
                lambda k=key: self.usageTrackerToggled.emit(k, False))

        # The pill picker, immediately RIGHT of the readouts it governs. It is
        # the ONLY way back once a pill has been closed, so it is never hidden:
        # not by a missing reading, not by every tracker being off, and (unlike
        # the two recovery switches) not by `set_recovery_available` either - a
        # machine with no Claude login still has Gemini pills to manage.
        self.usage_add_btn = QToolButton(self)
        self.usage_add_btn.setObjectName("UsageTrackerAdd")
        self.usage_add_btn.setText("+")
        self.usage_add_btn.setToolTip("Choose which usage readouts to show")
        self.usage_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.usage_add_btn.clicked.connect(self._open_tracker_menu)

        # The two auto-recovery switches. Both act on agents the plan limit cut
        # off; they differ only in WHEN the cut-off is discovered - on opening
        # the app, or while it runs. They decide whether unattended work
        # resumes, which is why "will my work resume by itself?" has to be
        # answerable at a glance from the switch alone, not a border colour.
        self._startup_recovery = True
        self._auto_continue = True
        self.recover_label = toggle_label("")
        self.recover_btn = ToggleSwitch(self)
        self.recover_btn.clicked.connect(self._on_recover_clicked)
        self.resume_label = toggle_label("")
        self.resume_btn = ToggleSwitch(self)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self._refresh_recovery_btns()

        # skin selector (Winamp-style): swaps the whole chrome palette live
        self.theme_select = DropDownComboBox(self)
        self.theme_select.setObjectName("ThemeSelect")
        self.theme_select.setToolTip("Theme")
        self.theme_select.setCursor(Qt.CursorShape.PointingHandCursor)
        for tid, theme in ui_theme.THEMES.items():
            self.theme_select.addItem(theme.name, tid)
        self.theme_select.currentIndexChanged.connect(
            lambda _i: self.themeChanged.emit(self.theme_select.currentData()))

        # ---- the Options panel -------------------------------------------
        # Every setting that used to compete for room on the bar, stacked with
        # a label each. `TopBar` still constructs and drives all of these; the
        # panel supplies only the rows (see options_panel.py).
        self.options_panel = OptionsPanel(self)
        self.options_panel.add_section("Automation")
        self.options_panel.add_switch_row(self.recover_label, self.recover_btn)
        self.options_panel.add_switch_row(self.resume_label, self.resume_btn)
        self.options_panel.add_switch_row(
            self.sound_label, self.sound_btn,
            self.chime_sound_btns[chime.QUESTION])
        self.options_panel.add_switch_row(
            self.reply_sound_label, self.reply_sound_btn,
            self.chime_sound_btns[chime.REPLY])
        self.options_panel.add_switch_row(self.taskbar_label, self.taskbar_btn)
        self.options_panel.add_switch_row(self.auto_update_label,
                                          self.auto_update_btn)
        self.options_panel.add_widget(self.install_label)
        self.options_panel.add_row("", self.update_pill,
                                   self.updates_manage_btn)
        self.options_panel.add_separator()
        self.options_panel.add_section("Agents")
        self.options_panel.add_switch_row(self.agent_lanes_label,
                                          self.agent_lanes_btn)
        self.options_panel.add_separator()
        self.options_panel.add_section("Appearance")
        self.options_panel.add_row("Theme", self.theme_select)
        self.options_panel.add_row("Font size", self.font_dec_btn,
                                   self.font_inc_btn)
        self.options_panel.add_switch_row(self.usage_left_label,
                                          self.usage_left_btn)

        self.options_btn = QToolButton(self)
        self.options_btn.setObjectName("OptionsBtn")
        # deliberately NOT the hamburger: `toggle_btn` at the far left already
        # uses that glyph, and two identical ones meaning different things is
        # worse than either.
        self.options_btn.setText("⚙ Options")
        self.options_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.options_btn.clicked.connect(self._open_options)
        self._refresh_options_btn()

        # the event log: every agent's prompts, questions, replies and limit
        # cut-offs across all workspaces. It counts the questions nobody has
        # answered yet, so a "?" in a workspace you are not looking at still
        # shows up here.
        self.log_btn = QToolButton(self)
        self.log_btn.setObjectName("EventLogBtn")
        self.log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_btn.clicked.connect(self.eventLogClicked)
        self.set_log_attention(0)

        # The usage readouts are the one thing left on the bar that keeps
        # changing and is worth glancing at, so they stay - inside a scroll
        # area, because four pills at their real widths still outrun a laptop's
        # spare bar width. `QAbstractScrollArea.minimumSizeHint()` is a small
        # constant regardless of what is inside it (unlike a plain
        # QWidget-with-layout, whose minimum is the sum of every child's own
        # minimum), so the WINDOW is free to shrink; the row just grows a thin
        # horizontal scrollbar instead of forcing the window wider. Nothing is
        # reparented out of the visible tree: an earlier overflow popup did
        # that and both crashed (a QWidgetAction deletes its widget once
        # released from a menu) and broke every `pill.isVisible()` check in the
        # smoke suite. The Options panel sidesteps that trap by holding its
        # widgets permanently rather than borrowing them per click.
        self._extras = _AutoSizingScrollContent(self)
        self._extras.setObjectName("TopBarExtras")
        extras_lay = QHBoxLayout(self._extras)
        extras_lay.setContentsMargins(0, 0, 0, 0)
        extras_lay.setSpacing(8)
        # The pills and their picker are ONE group and sit on the layout's own
        # spacing with nothing added between them, which reads as one readout
        # with a control rather than unrelated widgets.
        extras_lay.addWidget(self.usage_badge)
        extras_lay.addWidget(self.usage_weekly_badge)
        extras_lay.addWidget(self.gemini_badge)
        extras_lay.addWidget(self.gemini_weekly_badge)
        extras_lay.addWidget(self.codex_badge)
        extras_lay.addWidget(self.usage_add_btn)

        self._extras_scroll = _HWheelScrollArea(self)
        self._extras_scroll.setObjectName("TopBarExtrasScroll")
        self._extras_scroll.setWidget(self._extras)
        self._extras_scroll.setWidgetResizable(False)
        self._extras_scroll.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._extras_scroll.setFixedHeight(42)
        self._extras_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._extras_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._extras_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Transparent so it reads as part of the bar, not a separate panel.
        # Scoped by object name rather than left selector-less so it cannot
        # reach the row's own scrollbar as this grows; the themed groove and
        # handle come from `build_qss` and the row must not fight them.
        self._extras_scroll.setStyleSheet(
            "#TopBarExtrasScroll { background: transparent; border: none; }")
        self._extras_scroll.viewport().setStyleSheet("background: transparent;")

        lay.addWidget(self.toggle_btn)
        lay.addWidget(self._logo)
        lay.addWidget(self._name)
        lay.addWidget(self._version)
        lay.addWidget(self.app_update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(12)
        # A bare stretch keeps the identity block left-anchored and the extras
        # row does NOT get one, which only works because
        # `_HWheelScrollArea.sizeHint()` reports the row's real content width:
        # a QHBoxLayout satisfies size hints first and only then hands the
        # leftover to the stretch, so the row gets every pill it can afford
        # and the stretch absorbs what remains. Giving the stretch to the row
        # instead would left-anchor the whole cluster against the identity
        # block and leave the gap on the right, beside Add Terminal.
        lay.addStretch(1)
        lay.addWidget(self._extras_scroll)
        lay.addSpacing(8)
        lay.addWidget(self.log_btn)
        lay.addWidget(self.options_btn)
        lay.addWidget(self.add_terminal_btn)

    def _open_options(self) -> None:
        self.options_panel.toggle_under(self.options_btn)

    APP_UPDATE_BTN_SIDE = 34
    APP_UPDATE_IDLE_TIP = "Check GitHub for a newer AI Hive"

    def set_app_update_state(self, text: str = "", tooltip: str = "",
                             attention: bool = False,
                             interactive: bool = True) -> None:
        """The button beside the version badge. No arguments is the idle
        refresh glyph (painted by `RefreshGlyphButton` when the text is
        empty); anything else is what the last check found. A
        non-interactive state is a notice, not a control: disabled, no hand
        cursor, and only the tooltip the caller passes."""
        btn = self.app_update_btn
        idle = not text
        btn.setText(text)
        btn.setToolTip(tooltip or (self.APP_UPDATE_IDLE_TIP if idle else ""))
        btn.setProperty("attention", bool(attention))
        btn.setEnabled(interactive)
        btn.setCursor(Qt.CursorShape.PointingHandCursor if interactive
                      else Qt.CursorShape.ArrowCursor)
        ui_theme.repolish(btn)

    def set_log_attention(self, open_questions: int) -> None:
        """The log button reads "Log", or "Log ? 2" while two agents are
        waiting on an answer."""
        n = max(0, int(open_questions))
        self.log_btn.setText("\u2630 Log" + (f"  ? {n}" if n else ""))
        tip = ("Event log: what every agent in every workspace did "
               "(Ctrl+Shift+L)")
        if n:
            tip += f"\n{n} question{'s' if n != 1 else ''} waiting for you"
        self.log_btn.setToolTip(tip)
        self.log_btn.setProperty("attention", bool(n))
        ui_theme.repolish(self.log_btn)

    def _refresh_options_btn(self) -> None:
        """The button names what is behind it, and LIGHTS UP when the update
        pill inside has something to report. That pill used to sit on the bar
        where it could not be missed; moving it into a panel without this would
        turn a warning into a secret."""
        pending = self.update_pill.text()
        self.options_btn.setToolTip(
            "Settings: auto-recovery, notifications, updates, theme and font "
            "size." + ("\n" + pending if pending else ""))
        self.options_btn.setProperty("attention", bool(pending))
        ui_theme.repolish(self.options_btn)

    def _on_sound_clicked(self) -> None:
        self.set_sound_enabled(not self._sound_on)
        self.soundToggled.emit(self._sound_on)

    def _on_reply_sound_clicked(self) -> None:
        self.set_reply_sound_enabled(not self._reply_sound_on)
        self.replySoundToggled.emit(self._reply_sound_on)

    def _on_taskbar_clicked(self) -> None:
        self.set_taskbar_badge(not self._taskbar_badge)
        self.taskbarBadgeToggled.emit(self._taskbar_badge)

    def set_taskbar_badge(self, on: bool) -> None:
        """Reflect the taskbar-overlay preference (no signal emitted)."""
        self._taskbar_badge = bool(on)
        self._refresh_taskbar_btn()

    def _refresh_taskbar_btn(self) -> None:
        self.taskbar_btn.setChecked(self._taskbar_badge)
        self.taskbar_label.setText("\U0001FA9F  Taskbar count")
        tip = (
            "Taskbar count: ON. The taskbar icon carries a badge with the "
            "number of agents working, blue when one of them is waiting on a "
            "question, and nothing at all when the hive is idle.\n"
            "Click to turn off."
            if self._taskbar_badge else
            "Taskbar count: OFF. The taskbar icon stays plain, so you cannot "
            "tell from other apps whether agents are still working.\n"
            "Click to turn on.")
        self.taskbar_btn.setToolTip(tip)
        self.taskbar_label.setToolTip(tip)

    def _on_usage_left_clicked(self) -> None:
        self.set_usage_left(not self._usage_left)
        self.usageLeftToggled.emit(self._usage_left)

    def set_usage_left(self, on: bool) -> None:
        """Reflect the "Show usage left" switch onto itself and every usage
        pill (no signal emitted)."""
        self._usage_left = bool(on)
        self._refresh_usage_left_btn()
        for pill in self._usage_pills.values():
            pill.set_show_left(self._usage_left)

    def _refresh_usage_left_btn(self) -> None:
        self.usage_left_btn.setChecked(self._usage_left)
        self.usage_left_label.setText("◔  Show usage left")
        tip = (
            "Show usage left: ON. Each usage pill counts down what remains of "
            "its window, \"Claude 79% left\", and its ring empties as you "
            "use it.\nClick to show the share used instead."
            if self._usage_left else
            "Show usage left: OFF. Each usage pill counts up what you have "
            "used of its window, \"Claude 21% used\".\n"
            "Click to show what is left instead.")
        self.usage_left_btn.setToolTip(tip)
        self.usage_left_label.setToolTip(tip)

    def _on_agent_lanes_clicked(self) -> None:
        self.set_agent_lanes(not self._agent_lanes)
        self.agentLanesToggled.emit(self._agent_lanes)

    def set_agent_lanes(self, on: bool) -> None:
        """Reflect the "Lanes in every workspace" switch (no signal)."""
        self._agent_lanes = bool(on)
        self._refresh_agent_lanes_btn()

    def _refresh_agent_lanes_btn(self) -> None:
        self.agent_lanes_btn.setChecked(self._agent_lanes)
        self.agent_lanes_label.setText("⎇  Lanes in every workspace")
        # "off" must read as safe for agents that already have a lane: the
        # switch sets the default for NEW agents only, and never moves,
        # deletes or silences an existing lane
        tip = (
            "Lanes in every workspace: ON. Every workspace's lanes toggle is "
            "forced on, and the New Agent dialog ticks Own lane for every new "
            "Claude agent in a git workspace: its own git worktree and "
            "branch, so agents cannot overwrite or reset each other's work. "
            "The dialog can still untick it.\n"
            "Click to turn off. Each workspace's own toggle decides again; "
            "existing lanes are never touched."
            if self._agent_lanes else
            "Lanes in every workspace: OFF. Each workspace's ⎇ Lanes toggle "
            "(in its header) decides whether its new agents get a lane. "
            "Existing lanes are never touched.\nClick to turn on.")
        self.agent_lanes_btn.setToolTip(tip)
        self.agent_lanes_label.setToolTip(tip)

    def _on_auto_update_clicked(self) -> None:
        """Arm or disarm the startup update gate, like every other row here.

        On the bar this button could not be a toggle: there was room for ONE
        update control, so it had to be the door to the Updates panel and the
        preference lived on a checkbox inside. The panel has room for both, so
        the switch is a switch again and `updates_manage_btn` is the door.

        This is not a second control for one setting. `open_updates_panel`
        already wires the Updates panel's checkbox to BOTH `set_auto_update`
        and the window's handler, so the two are driven from the same signal
        and cannot disagree."""
        self.set_auto_update(not self._auto_update)
        self.autoUpdateToggled.emit(self._auto_update)

    def set_auto_update(self, on: bool) -> None:
        """Reflect the CLI auto-update preference (no signal emitted)."""
        self._auto_update = bool(on)
        self._refresh_auto_update_btn()

    def auto_update(self) -> bool:
        return self._auto_update

    def note_install_state(self, text: str) -> None:
        """Name the detected Claude Code install state under the switch it
        explains. It used to be tooltip-only, which meant the answer to "how is
        my Claude Code installed?" was reachable only by hovering."""
        self._install_state = text or ""
        self.install_label.setText(self._install_state)
        self.install_label.setVisible(bool(self._install_state))
        self._refresh_auto_update_btn()

    def _refresh_auto_update_btn(self) -> None:
        self.auto_update_btn.setChecked(self._auto_update)
        self.auto_update_label.setText("⬇  Check for CLI updates at start-up")
        # the tooltip states plainly what the armed switch does, because it
        # changes software on the user's machine rather than anything inside
        # the app. The detected install state is no longer crammed in here:
        # `install_label` says it in full, right under this row.
        tip = (
            "Startup check: ON. Next time AI Hive starts, it checks for a "
            "newer Claude Code and Gemini (agy) CLI and installs it BEFORE "
            "any agent launches, which is the only moment those files are "
            "not locked.\nClick to turn off."
            if self._auto_update else
            "Startup check: OFF. Startup is untouched, so you keep whatever "
            "CLI version is installed and may keep seeing Claude's own "
            "'update available' banner. Turning it on lets AI Hive install "
            "CLI updates at startup, which changes installed software on "
            "your machine.\nClick to turn on.")
        self.auto_update_btn.setToolTip(tip)
        self.auto_update_label.setToolTip(tip)

    def note_update_pending(self, text: str, tooltip: str = "") -> None:
        """Show (or hide, on an empty text) the last gate's report.

        The pill lives inside the Options panel now, so this also lights the
        Options button. Without that, an update that could not apply would be
        reported somewhere nobody has open, which is the same as not reporting
        it at all."""
        self.update_pill.setText(text or "")
        self.update_pill.setToolTip(tooltip or text or "")
        self.update_pill.setVisible(bool(text))
        self._refresh_options_btn()

    def set_sound_enabled(self, on: bool) -> None:
        """Reflect the chime on/off state in the button (no signal emitted)."""
        self._sound_on = bool(on)
        self._refresh_sound_btn()

    def _refresh_sound_btn(self) -> None:
        # the switch itself says on/off; the bell/muted-bell glyph in the
        # label is still a second, always-visible tell.
        self.sound_btn.setChecked(self._sound_on)
        bell = "🔔" if self._sound_on else "🔕"
        self.sound_label.setText(f"{bell}  Question chime")
        tip = (
            "Question chime: ON. A rising \"bweep?\" plays when an agent "
            "settles on a question and needs you, even from another "
            "workspace.\nClick to mute."
            if self._sound_on else
            "Question chime: OFF. An agent waiting on a question raises "
            "its \"?\" silently.\nClick to turn on.")
        tip += self._sound_tip_line(chime.QUESTION)
        self.sound_btn.setToolTip(tip)
        self.sound_label.setToolTip(tip)

    def set_reply_sound_enabled(self, on: bool) -> None:
        """Reflect the reply chime on/off state in the button (no signal)."""
        self._reply_sound_on = bool(on)
        self._refresh_reply_sound_btn()

    def _refresh_reply_sound_btn(self) -> None:
        self.reply_sound_btn.setChecked(self._reply_sound_on)
        bell = "🔔" if self._reply_sound_on else "🔕"
        self.reply_sound_label.setText(f"{bell}  Reply finished chime")
        tip = (
            "Reply finished chime: ON. A short \"ta-da!\" plays when an agent "
            "finishes a reply you asked for, even from another workspace.\n"
            "Click to mute."
            if self._reply_sound_on else
            "Reply finished chime: OFF. Agents finish replies silently.\n"
            "Click to turn on.")
        tip += self._sound_tip_line(chime.REPLY)
        self.reply_sound_btn.setToolTip(tip)
        self.reply_sound_label.setToolTip(tip)

    def set_custom_sound(self, kind: str, name: str) -> None:
        """Reflect which sound a chime plays: a custom file's name, or "" for
        the built-in one (no signal emitted)."""
        if kind in self._custom_sound_names:
            self._custom_sound_names[kind] = name or ""
            self._refresh_chime_sound_btns()
            self._refresh_sound_btn()
            self._refresh_reply_sound_btn()

    def custom_sound_name(self, kind: str) -> str:
        """The custom file name shown for this chime, "" when built-in."""
        return self._custom_sound_names.get(kind, "")

    def _sound_tip_line(self, kind: str) -> str:
        name = getattr(self, "_custom_sound_names", {}).get(kind, "")
        return (f"\nSound: {name} (your own)." if name
                else "\nSound: the built-in one.")

    def _refresh_chime_sound_btns(self) -> None:
        for kind, btn in self.chime_sound_btns.items():
            name = self._custom_sound_names.get(kind, "")
            btn.setToolTip(
                (f"Playing your own sound: {name}" if name
                 else "Playing the built-in sound")
                + "\nClick to preview it, choose a WAV or MP3 of your own, "
                "or go back to the built-in one.")

    def build_chime_sound_menu(self, kind: str) -> QMenu:
        """The note button's menu for one chime. Built fresh per click, like
        the tracker menu, so "Use built-in sound" is never stale. Public so
        the smoke suite can trigger the entries without a modal popup."""
        menu = QMenu(self)
        menu.addAction("Play preview").triggered.connect(
            lambda: self.chimePreviewRequested.emit(kind))
        menu.addAction("Choose sound file...").triggered.connect(
            lambda: self.chimeSoundChooseRequested.emit(kind))
        reset = menu.addAction("Use built-in sound")
        reset.setEnabled(bool(self._custom_sound_names.get(kind)))
        reset.triggered.connect(
            lambda: self.chimeSoundResetRequested.emit(kind))
        return menu

    def _open_chime_sound_menu(self, kind: str) -> None:
        btn = self.chime_sound_btns[kind]
        menu = self.build_chime_sound_menu(kind)
        close_on_anchor_press(menu, btn)
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))

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
        # the switch itself (green/slid-right when armed, grey/slid-left when
        # off) is the whole tell now - "will my work resume by itself?" is
        # answerable at a glance without reading a border colour or an LED.
        self.recover_btn.setChecked(self._startup_recovery)
        self.recover_label.setText("⏻  Recover at start-up")
        recover_tip = (
            "Recover at startup: ON. When AI Hive opens, agents whose work "
            "stopped because the plan limit ran out are continued "
            "automatically.\nClick to turn off."
            if self._startup_recovery else
            "Recover at startup: OFF. Agents left stuck on a spent plan "
            "limit stay stopped when AI Hive opens.\nClick to turn on.")
        self.recover_btn.setToolTip(recover_tip)
        self.recover_label.setToolTip(recover_tip)
        self.resume_btn.setChecked(self._auto_continue)
        self.resume_label.setText("\U0001F504  Resume on usage reset")
        resume_tip = (
            "Resume on limit reset: ON. While AI Hive is running, agents cut "
            "off mid-work by the plan limit are continued the moment the "
            "limit resets.\nClick to turn off."
            if self._auto_continue else
            "Resume on limit reset: OFF. Agents cut off by the plan limit "
            "wait for you.\nClick to turn on.")
        self.resume_btn.setToolTip(resume_tip)
        self.resume_label.setToolTip(resume_tip)

    def build_tracker_menu(self) -> QMenu:
        """The pill picker's menu: one checkable entry per usage readout.

        Built fresh on every click (rather than kept as an `InstantPopup` menu)
        so its checkmarks can never be stale. Public because the smoke suite
        asserts the entries without opening a modal popup.
        """
        menu = QMenu(self)
        for key in USAGE_TRACKER_KEYS:
            act = menu.addAction(USAGE_TRACKER_LABELS[key])
            act.setCheckable(True)
            act.setChecked(bool(self._trackers.get(key, True)))
            act.toggled.connect(
                lambda on, k=key: self.usageTrackerToggled.emit(k, on))
        return menu

    def _open_tracker_menu(self) -> None:
        menu = self.build_tracker_menu()
        close_on_anchor_press(menu, self.usage_add_btn)
        menu.exec(self.usage_add_btn.mapToGlobal(
            QPoint(0, self.usage_add_btn.height())))

    def usage_pills_for(self, keys) -> list:
        """The pills behind these tracker keys, for the poller that feeds
        them (`usage_poll.UsagePoller` reports poll health to them directly)."""
        return [self._usage_pills[k] for k in keys if k in self._usage_pills]

    def set_usage_reading(self, keys, reading) -> None:
        """Push a reading into the pills behind these tracker keys. Each pill
        picks its own window out of it."""
        for pill in self.usage_pills_for(keys):
            pill.set_usage(reading)
        self._sync_usage_pills()

    def set_usage(self, usage) -> None:
        """Push a plan-usage reading into both Claude pills (5h and 7d)."""
        self.set_usage_reading(("claude_five_hour", "claude_weekly"), usage)

    def sync_usage_pills(self) -> None:
        """Re-decide visibility after a poll changed what a pill has to say
        (a can't-read pill appearing, or a readout going absent)."""
        self._sync_usage_pills()

    def tick_usage(self) -> None:
        """Re-render every pill's countdown from the clock alone (no network)."""
        for pill in self._usage_pills.values():
            pill.tick()

    def set_usage_trackers(self, trackers: dict) -> None:
        """Reflect the per-pill show/hide preference (no signal emitted)."""
        self._trackers = {k: bool(trackers.get(k, True))
                          for k in USAGE_TRACKER_KEYS}
        self._sync_usage_pills()

    def usage_trackers(self) -> dict:
        return dict(self._trackers)

    def _sync_usage_pills(self) -> None:
        """The ONE place a usage pill's visibility is decided, and it is the
        product of TWO independent facts: does the user want this pill (the
        tracker preference), and does it have anything to say (`has_content` —
        a reading, the can't-read pill, or the loading state).

        Keeping them separate is what lets a CLOSED pill vanish, which is the
        point of the X, without regressing the rule that a FAILED read must
        never make the readout look deleted. Neither state can be mistaken for
        the other, and no badge class calls `setVisible` on itself.
        """
        for key, pill in self._usage_pills.items():
            pill.setVisible(bool(self._trackers.get(key, True))
                            and pill.has_content())

    def mark_usage_loading(self, key: str | None = None) -> None:
        """Put the enabled pills (or just one) into their loading state and show
        them. This is what fills the bar at startup and the moment a tracker is
        switched back on, so a fetch in flight is never a blank gap."""
        for k, pill in self._usage_pills.items():
            if (key is None or k == key) and self._trackers.get(k, True):
                pill.mark_loading()
        self._sync_usage_pills()

    def mark_usage_absent(self, key: str) -> None:
        """This readout has nothing to show and never will this run (no login).
        Clears its content, so no later re-sync can resurrect it."""
        pill = self._usage_pills.get(key)
        if pill is not None:
            pill.mark_absent()
        self._sync_usage_pills()

    def set_recovery_available(self, on: bool) -> None:
        """Show/hide both recovery toggles. They act only on Claude agents cut
        off by a plan limit, so with no Claude login there is nothing for them
        to do — hide them with the readout rather than offer dead switches.

        `usage_add_btn` is deliberately NOT hidden with them: it is the only way
        to bring a closed pill back, and a Gemini-only user (who by definition
        has no Claude login) would otherwise be left with no control at all."""
        self.recover_label.setVisible(bool(on))
        self.recover_btn.setVisible(bool(on))
        self.resume_label.setVisible(bool(on))
        self.resume_btn.setVisible(bool(on))

    def set_terminal_scrollback(self, on: bool) -> None:
        """Reflect the scrollback-ownership preference (no signal emitted)."""
        self._scrollback_wanted = bool(on)

    def terminal_scrollback(self) -> bool:
        return self._scrollback_wanted

    def contextMenuEvent(self, event):
        """Right-click anywhere on the bar: the set-once display preferences.

        Context-menu items rather than more buttons — the bar is already busy,
        and these are set-once preferences. The two RECOVERY switches are
        deliberately NOT here: they decide whether unattended work resumes, so
        they get visible buttons instead (and each setting has exactly one
        control, never two that can disagree).

        The usage readouts used to live here too, as a single "Show plan usage"
        that hid all three at once. They moved to the + button, per pill: the
        same one-setting-one-control rule, applied the other way round — a
        master toggle sitting on top of three per-pill checkboxes is two
        controls for the same preference, and a pill checked in one but hidden
        by the other is not explainable.
        """
        menu = QMenu(self)
        act2 = menu.addAction("Scrollback lives in AI Hive")
        act2.setCheckable(True)
        act2.setChecked(self._scrollback_wanted)
        act2.setToolTip(
            "Claude agents run the classic renderer, so their conversation "
            "scrolls into AI Hive's own scrollbar. Applies to agents started "
            "from now on.")
        act2.toggled.connect(self.terminalScrollbackToggled)
        menu.exec(event.globalPos())

    def set_theme(self, theme_id: str) -> None:
        """Reflect the active theme in the dropdown without re-emitting."""
        i = self.theme_select.findData(theme_id)
        if i >= 0:
            self.theme_select.blockSignals(True)
            self.theme_select.setCurrentIndex(i)
            self.theme_select.blockSignals(False)


class AddTerminalDialog(QDialog):
    """Configure a new agent: type, and for AI agents provider/model/effort.

    Model stays dialog-free — the dialog only produces AgentSpecs via
    result_spec() / result_specs(); headless tests build specs directly.

    The Count stepper launches several identical agents in one go. It is
    capped at the workspace's free slots (max_count) and pinned to 1 while a
    past conversation is picked: two agents resuming one transcript race for
    it and one of them destroys it.

    Agent lanes (app/lanes.py): the "Own lane (git
    worktree)" checkbox exists in any git workspace (`repo_root`), whatever
    the lanes toggles say, so one agent can opt in alone. It starts ticked
    for Claude when `lane_default` (the Options switch or the workspace's
    own toggle) is on. The Conversation picker also lists conversations
    that started in one of the repo's lanes: picking one revives that lane
    at its identical path (`revive_lane`). Lanes of agents that still have
    a card (`live_lane_roots`) are left out.
    """

    def __init__(self, default_name: str, parent=None,
                 cwd: str = "", busy_ids=(),
                 max_count: int = MAX_AGENTS_PER_WORKSPACE,
                 lane_default: bool = False, repo_root: str = "",
                 live_lane_roots=()):
        super().__init__(parent)
        self.setWindowTitle("New Agent")
        self.setMinimumWidth(420)
        self._cwd = cwd
        self._busy_ids = set(busy_ids)  # conversations a running agent holds
        self._resume_loaded = False
        self._count = 1
        self._max_count = max(1, max_count)
        self._repo_root = repo_root
        self._live_lane_roots = set(live_lane_roots)
        # resumable lane conversations by session id (lanes.LaneConversation)
        self._lane_convs: dict = {}
        # the user's own tick, kept while Type is flipped away and back. It
        # starts at the workspace's lane default. A workspace inside a larger
        # repository starts unticked whatever the default: its lane would
        # copy the whole repository (a dotfiles repo in the home folder,
        # say), which the user should choose knowingly.
        self._nested_in = ("" if not repo_root or lanes.same_path(repo_root, cwd)
                           else repo_root)
        self._lane_wanted = bool(lane_default) and not self._nested_in

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

        # outside a git repository there is no checkbox at all: there is
        # nothing a lane could be made from
        self.lane_check = None
        self.lanes_help = None
        if repo_root:
            self.lane_check = QCheckBox("Own lane (git worktree)", self)
            self.lane_check.toggled.connect(self._on_lane_toggled)
            self.lanes_help = QLabel("<a href='lanes'>What are lanes?</a>",
                                     self)
            self.lanes_help.setObjectName("LanesHelpLink")
            self.lanes_help.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.lanes_help.linkActivated.connect(
                lambda _href: show_lanes_explainer(self))

        # how many agents to open: [-] n [+]
        self.count_minus = QPushButton("-", self)
        self.count_value = QLabel(self)
        self.count_plus = QPushButton("+", self)
        for btn in (self.count_minus, self.count_plus):
            btn.setObjectName("CountStepBtn")
            # a dialog's QPushButtons are autoDefault, so Enter would step
            # the count instead of pressing OK
            btn.setAutoDefault(False)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.count_value.setObjectName("CountValue")
        self.count_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_value.setMinimumWidth(28)
        count_row = QHBoxLayout()
        count_row.setSpacing(6)
        count_row.addWidget(self.count_minus)
        count_row.addWidget(self.count_value)
        count_row.addWidget(self.count_plus)
        count_row.addStretch(1)
        self.count_minus.clicked.connect(lambda: self._set_count(self._count - 1))
        self.count_plus.clicked.connect(lambda: self._set_count(self._count + 1))

        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.kind_combo)
        self._note_row = self.provider_note
        form.addRow("", self.provider_note)
        form.addRow("Count", count_row)
        self._model_label = QLabel("Model", self)
        form.addRow(self._model_label, self.model_combo)
        self._effort_label = QLabel("Effort", self)
        form.addRow(self._effort_label, self.effort_combo)
        self._mode_label = QLabel("Mode", self)
        form.addRow(self._mode_label, self.mode_combo)
        self._resume_label = QLabel("Conversation", self)
        form.addRow(self._resume_label, self.resume_combo)
        if self.lane_check is not None:
            lane_row = QHBoxLayout()
            lane_row.addWidget(self.lane_check)
            lane_row.addStretch(1)
            lane_row.addWidget(self.lanes_help)
            form.addRow("", lane_row)
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
        self.resume_combo.currentIndexChanged.connect(
            lambda _i: self._set_count(self._count))
        self.resume_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_lane_check())
        self._on_kind_changed(0)

    def count(self) -> int:
        """How many agents OK opens (1 unless the stepper was used)."""
        return self._count

    def _count_cap(self) -> int:
        # a resumed conversation can only be held by one agent
        if not self.resume_combo.isHidden() and self.resume_combo.currentData():
            return 1
        return self._max_count

    def _set_count(self, n: int) -> None:
        cap = self._count_cap()
        self._count = max(1, min(n, cap))
        self.count_value.setText(str(self._count))
        self.count_minus.setEnabled(self._count > 1)
        self.count_plus.setEnabled(self._count < cap)
        if cap == 1 and self._max_count > 1:
            self.count_plus.setToolTip(
                "A past conversation can only be resumed by one agent.")
        elif self._count >= cap:
            self.count_plus.setToolTip(
                f"This workspace has room for {cap} more agent"
                f"{'' if cap == 1 else 's'}.")
        else:
            self.count_plus.setToolTip("")

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
            if AI_KINDS[kind] == "gemini" and providers._GEMINI_MODELS_CACHE is None:
                import threading
                threading.Thread(target=providers.fetch_gemini_models, daemon=True).start()
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
        self._set_count(self._count)  # the resume picker may have come or gone
        self._refresh_lane_check()

    def _populate_models(self, prov) -> None:
        self.model_combo.clear()
        models = (providers.gemini_available_models()
                  if prov and prov.key == "gemini" else
                  (prov.models if prov else ()))
        for label, value in models:
            self.model_combo.addItem(label, value)

    def _populate_efforts(self, prov) -> None:
        self.effort_combo.clear()
        for tok in prov.efforts:
            self.effort_combo.addItem(tok.capitalize() if tok else "Default", tok)
        # Ultracode isn't a launch flag (claude --effort only takes
        # low/medium/high/xhigh/max); it's an in-session mode. Show it, greyed
        # out, so users know to enable it manually in the terminal.
        if prov.native_flags and prov.key == "claude":
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
        conversation in this workspace's folder and in the repo's lanes
        (newest first), skipping any a running agent still holds (resuming
        that would race/truncate it) and any lane that still has a card."""
        if self._resume_loaded:
            return
        self._resume_loaded = True
        self.resume_combo.clear()
        self.resume_combo.addItem("New conversation", "")

        from app import session_sync
        rows = []                      # (mtime, preview, session id, lane)
        if self._cwd:
            for conv in session_sync.conversation_previews(self._cwd):
                if conv.session_id in self._busy_ids:
                    continue  # in use by a running agent — unsafe to double-resume
                rows.append((conv.mtime, conv.preview, conv.session_id, ""))
        if self._repo_root:
            for conv in lanes.lane_conversations(
                    self._repo_root, exclude_roots=self._live_lane_roots,
                    exclude_ids=self._busy_ids):
                self._lane_convs[conv.session_id] = conv
                rows.append((conv.mtime, conv.preview, conv.session_id,
                             conv.lane_name))
        rows.sort(key=lambda r: r[0], reverse=True)
        for mtime, preview, sid, lane_name in rows:
            when = time.strftime("%b %d %H:%M", time.localtime(mtime))
            preview = preview or "(empty session)"
            if len(preview) > 48:
                preview = preview[:47] + "…"
            where = f"lane {lane_name}  ·  " if lane_name else ""
            self.resume_combo.addItem(f"{when}  ·  {where}{preview}", sid)

    def _resume_id(self) -> str:
        return (self.resume_combo.currentData() or ""
                if not self.resume_combo.isHidden() else "")

    def revive_lane(self):
        """The lane conversation picked under Conversation
        (lanes.LaneConversation), or None. Works whatever the lanes toggles
        say: it restores existing work, it creates no new isolation."""
        return self._lane_convs.get(self._resume_id())

    def wants_lane(self) -> bool:
        """True when OK should give each new agent its own lane."""
        return (self.lane_check is not None and self.lane_check.isEnabled()
                and self.lane_check.isChecked() and self.revive_lane() is None)

    def _on_lane_toggled(self, on: bool) -> None:
        if self.lane_check is not None and self.lane_check.isEnabled():
            self._lane_wanted = bool(on)

    def _refresh_lane_check(self) -> None:
        cb = self.lane_check
        if cb is None:
            return
        resume_id = self._resume_id()
        conv = self._lane_convs.get(resume_id) if resume_id else None
        cb.blockSignals(True)
        if conv is not None:
            cb.setText("Resumes in its own lane")
            cb.setChecked(True)
            cb.setEnabled(False)
            tip = (f"This conversation started in the lane {conv.root} "
                   f"(branch {conv.branch}) and resumes there.")
        else:
            cb.setText("Own lane (git worktree)")
            if self._kind() != AgentKind.CLAUDE:
                reason = "Lanes are for Claude agents for now."
            elif resume_id:
                reason = ("A past conversation from the workspace folder "
                          "resumes there, without a lane.")
            else:
                reason = ""
            cb.setChecked(not reason and self._lane_wanted)
            cb.setEnabled(not reason)
            tip = reason or (
                "Give this agent its own git worktree and branch beside the "
                "repository, so agents cannot overwrite or reset each other's "
                "work. Each agent opened at once gets its own lane.")
            if not reason and self._nested_in:
                tip = (f"This folder is inside the repository at "
                       f"{self._nested_in}. A lane copies the whole "
                       f"repository.\n\n{tip}")
        cb.setToolTip(tip)
        cb.blockSignals(False)

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
                conv = self._lane_convs.get(resume_id)
                if conv is not None:
                    # a lane conversation resumes in its lane, at the path it
                    # started in, never in the workspace folder
                    spec.lane = conv.lane_dict()
                    spec.cwd = conv.cwd
            return spec
        program = self.program_edit.text().strip()
        args = QProcess.splitCommand(self.args_edit.text().strip())
        pty = self.pty_check.isChecked() and HAS_CONPTY
        return build_spec(kind, name, cwd=cwd, program=program, args=args, pty=pty)

    def result_specs(self, cwd: str) -> list:
        """One fresh spec per agent the stepper asked for. The first keeps the
        typed name; the rest count on from it ("Agent 4" -> "Agent 5", 6...;
        "Reviewer" -> "Reviewer 2", 3...)."""
        specs = [self.result_spec(cwd=cwd) for _ in range(self._count)]
        m = re.fullmatch(r"(.*\S)\s+(\d+)", specs[0].name)
        base, first = (m.group(1), int(m.group(2))) if m else (specs[0].name, 1)
        for i, spec in enumerate(specs[1:], start=1):
            spec.name = f"{base} {first + i}"
        return specs


class ScheduleMessageDialog(QDialog):
    """Compose a message now, choose when it is typed in.

    Opened by Ctrl+Shift+Enter in an agent's terminal (prefilled with whatever
    was typed) or from the card's ⏱ tray button (empty). It doubles as the
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


def clone_default_branch(remote: str, destination: str) -> str:
    """Clone `remote`'s default branch into `destination`; return git's error
    text, or "" on success. A failed clone leaves no folder behind.

    No `--branch`: git then checks out whatever the remote's HEAD points at,
    so `main`, `master`, `trunk` or any other default name all work. Naming
    `main` here made every repo whose default is `master` fail with "Remote
    branch main not found". `--single-branch` still fetches only that branch.
    """
    error = ""
    try:
        result = subprocess.run(
            ["git", "clone", "--single-branch", remote, destination],
            capture_output=True, text=True, timeout=900,
            creationflags=0x08000000)
        if result.returncode:
            error = (result.stderr or result.stdout or
                     "Git could not clone the repository.").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)
    if error and os.path.isdir(destination):
        shutil.rmtree(destination, ignore_errors=True)
    return error


class NewWorkspaceDialog(QDialog):
    """Choose an existing project folder or clone a GitHub repository's
    default branch (see `clone_default_branch`)."""

    def __init__(self, start_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Workspace")
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.source_combo = QComboBox(self)
        self.source_combo.addItems(["Existing folder", "GitHub repository"])
        form.addRow("Source", self.source_combo)
        root.addLayout(form)

        self.pages = QStackedWidget(self)
        folder_page = QWidget(self.pages)
        folder_form = QFormLayout(folder_page)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(start_dir, folder_page)
        folder_browse = QPushButton("Browse…", folder_page)
        folder_browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(folder_browse)
        folder_form.addRow("Project folder", folder_row)
        self.pages.addWidget(folder_page)

        repo_page = QWidget(self.pages)
        repo_form = QFormLayout(repo_page)
        self.repo_edit = QLineEdit(repo_page)
        self.repo_edit.setPlaceholderText("https://github.com/owner/repository.git")
        repo_form.addRow("GitHub URL", self.repo_edit)
        self.repo_name_edit = QLineEdit(repo_page)
        self.repo_name_edit.setPlaceholderText("Repository name")
        self._suggested_name = ""
        repo_form.addRow("Workspace name", self.repo_name_edit)
        parent_row = QHBoxLayout()
        self.parent_edit = QLineEdit(start_dir, repo_page)
        parent_browse = QPushButton("Browse…", repo_page)
        parent_browse.clicked.connect(self._browse_parent)
        parent_row.addWidget(self.parent_edit, 1)
        parent_row.addWidget(parent_browse)
        repo_form.addRow("Clone into", parent_row)
        note = QLabel("AI Hive will clone the repository's default branch "
                      "(main, master or whatever it is called) into a new folder.",
                      repo_page)
        self.repo_note = note
        note.setWordWrap(True)
        repo_form.addRow("", note)
        self.pages.addWidget(repo_page)
        root.addWidget(self.pages)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.Ok, parent=self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create workspace")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.source_combo.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.repo_edit.textChanged.connect(self._suggest_repo_name)

    def _browse_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose the project folder", self.folder_edit.text())
        if path:
            self.folder_edit.setText(path)

    def _browse_parent(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose where to clone the repository", self.parent_edit.text())
        if path:
            self.parent_edit.setText(path)

    def _suggest_repo_name(self, value: str):
        if (self.repo_name_edit.text().strip()
                and self.repo_name_edit.text() != self._suggested_name):
            return
        path = urlparse(value.strip()).path.rstrip("/")
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            self._suggested_name = name
            self.repo_name_edit.setText(name)

    def result_data(self) -> dict:
        if self.source_combo.currentIndex() == 0:
            path = self.folder_edit.text().strip()
            return {"mode": "folder", "path": path,
                    "name": os.path.basename(os.path.normpath(path)) or "Workspace"}
        return {"mode": "github", "url": self.repo_edit.text().strip(),
                "name": self.repo_name_edit.text().strip(),
                "parent": self.parent_edit.text().strip()}


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
    repoActivityLoaded = Signal(str, str, object)  # ws_id, path, result
    workspaceCloneFinished = Signal(str, str, str, str)

    def __init__(self, manager: WorkspaceManager, store: SessionStore,
                 session: dict | None = None):
        super().__init__()
        self.manager = manager
        self.store = store
        self.setWindowTitle("AI Hive")
        self.resize(1440, 900)

        self._pages: dict[str, WorkspacePage] = {}
        # the Open repo dropdown's GitHub activity (app/repo_activity.py):
        # ws_id -> {"path", "at" (monotonic), "result"}, plus the workspaces
        # with a fetch in flight. The fetcher is an attribute for tests.
        self._repo_activity: dict[str, dict] = {}
        self._repo_activity_busy: set[str] = set()
        self._repo_activity_fetch = repo_activity.fetch
        self.repoActivityLoaded.connect(self._on_repo_activity_loaded)
        self.workspaceCloneFinished.connect(self._on_workspace_clone_finished)
        self._clone_progress: QProgressDialog | None = None
        self._map_window: AgentFileMapWindow | None = None  # lazy, reused
        self._event_log_window: EventLogWindow | None = None  # lazy, reused
        self._event_log_state: dict = {}  # its filters/size (persisted)
        self.event_hub: EventHub | None = None
        self._focused_card: TerminalCard | None = None
        self._closing = False
        self._ready = False  # suppress save-storms during initial load
        self._last_saved_json = None  # what last reached disk (heartbeat guard)
        self._theme_id = ui_theme.ACTIVE_THEME.id  # active skin (persisted)
        self._sound_enabled = True  # question chime on "?" (persisted)
        self._reply_sound_enabled = False  # reply-finished chime (persisted)
        # chime kind -> the user's own sound file, copied into sounds_dir()
        # (persisted; a kind that is absent plays the built-in sound)
        self._custom_sounds: dict[str, str] = {}

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

        # notice a background command still running after the agent itself
        # has gone quiet (transient: this never saves)
        self._bg_shell_timer = QTimer(self)
        self._bg_shell_timer.setInterval(BG_SHELL_POLL_MS)
        self._bg_shell_timer.timeout.connect(self.manager.poll_bg_shell_activity)

        # ---- Claude plan usage (top-bar readout + limit-reached edges) ----
        # PURELY TRANSIENT: a reading refreshes the badge and may emit the
        # plan-limit edges, but it must NEVER mark the session dirty — the same
        # rule as activity_changed/waiting_changed. This polls every 30 s to
        # 6 min forever; wiring it to a save would rewrite session.json each time.
        self._usage = None            # latest claude_usage.Usage
        # which usage readouts the user wants, per pill (persisted under
        # ui.usage_trackers). The default MUST be assigned here, above
        # _restore_ui_state, or the restored preference is clobbered back to on.
        self._usage_trackers = dict(DEFAULT_USAGE_TRACKERS)
        # did main.py opt this window into polling? Guards the runtime re-arm in
        # _on_usage_tracker_toggled: a tracker switched on inside the offscreen
        # suite must never start shelling out to the user's real agy CLI.
        self._polling = False
        # taskbar working-count overlay. The default MUST be set here, above
        # _restore_ui_state, or the restored preference is clobbered back to on.
        self._taskbar_badge = True    # user preference (persisted)
        self._taskbar_key = None      # last key actually pushed to the shell
        # "Show usage left". Default OFF, assigned above _restore_ui_state
        # for the same reason as the taskbar badge.
        self._usage_left = False      # user preference (persisted)
        # startup CLI auto-update. Default OFF (it changes installed software),
        # and like every other preference here the default MUST be assigned
        # above _restore_ui_state or the restored value is clobbered.
        self._auto_update = False     # user preference (persisted)
        # "Lanes in every workspace". Default OFF, assigned here for the same
        # reason. Read it only through lane_default(ws).
        self._agent_lanes = False     # user preference (persisted)
        # every mutating lane git operation runs here, one at a time per repo
        # (app/lane_ops.py). Created whatever the toggles say: repairing,
        # retiring and reviving an existing lane never ask them.
        self.lane_ops = LaneOps(self, audit=self._store_audit)
        # uids of agents with a lane create/repair in flight. A card closed
        # meanwhile is retired by that operation's callback, never by the
        # close itself (a failed create must not touch a folder it doesn't own)
        self._lane_pending: set[str] = set()
        # uids of agents Restart in own lane is stopping, before their lane
        # is planned: {uid: the status slot waiting for the worker to end}
        self._lane_adopting: dict = {}
        # workspaces already told, this run, that two lanes want an
        # integrator (_hint_integrator). Transient by design.
        self._integrator_hinted: set[str] = set()
        # roots of closed agents' lanes whose retire is still being retried
        # (a program held the folder). Hidden from the picker meanwhile, so a
        # revive can't race the retry for the same lane.
        self._lane_retiring: set[str] = set()
        self._lane_boxes: list = []   # open "lane kept" notices
        # what every lane holds and where lanes overlap (app/lane_service.py):
        # card chips, Activity panel, roster columns, lanes.json for the
        # overlap hook, lane notices. Runs only while some agent has a lane
        # (_sync_lane_service starts and stops it).
        self.lane_service = LaneService(
            self.manager, self.lane_ops,
            skip=lambda uid: uid in self._lane_pending,
            audit=self._store_audit, parent=self)
        # "Task done" lane commits already logged, as {(lane uid, sha)}.
        # Transient: after a restart an unmerged flag is logged once more.
        self._done_seen: set = set()
        # lanes being fast-forwarded on their own (_refresh_idle_lanes)
        self._auto_refreshing: set[str] = set()
        # providers whose CLI was STILL INSTALLING when the user skipped the
        # update splash. Their agents are held out of the autostart, because
        # launching one now could execute a half written binary. Transient by
        # construction: it describes this launch only.
        self._update_installing: tuple = ()
        # every outcome from this launch's gate, kept so the Updates panel can
        # show what happened after the splash has closed. Transient exactly
        # like `_update_installing`: it describes this launch only and is never
        # persisted (a stored version goes stale the moment anything installs).
        self._update_outcomes: tuple = ()
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
        # consecutive "TUI not ready" watchdog ticks per agent id, so a latched
        # agent that never gets a frame is eventually asked for one rather than
        # waited on forever (see LIMIT_REPAINT_AFTER_WAITS)
        self._limit_wait_ticks: dict[str, int] = {}
        # ledger keys already filed this run, so one cut-off is written once
        # however many times its latch is (re)raised
        self._ledger_seen: set[tuple] = set()
        # when this window came up: the line between the transcript sweep's
        # cut-offs and startup recovery's (see LIMIT_SWEEP_SLACK_S)
        self._launched_at = time.time()
        self._auto_continue = True    # user preference (persisted)
        self._startup_recovery = True  # user preference (persisted)
        self._usage_tick_timer = QTimer(self)
        self._usage_tick_timer.setInterval(USAGE_TICK_MS)
        self._usage_tick_timer.timeout.connect(self._tick_usage)
        # One poller per usage source, all on the shared policy in
        # app.usage_poll. None of them fetches until `start_usage_polling`,
        # which main.py alone calls: the smoke suite builds many windows and
        # must never touch the network, the user's account or the agy CLI.
        self._gemini_usage = None     # last Gemini reading with a number in it
        self._codex_usage = None      # last GPT reading with a number in it
        self._usage_pollers: dict[str, usage_poll.UsagePoller] = {}
        sources = usage_sources()
        covered = [k for src in sources for k in src.tracker_keys]
        if sorted(covered) != sorted(USAGE_TRACKER_KEYS):
            raise RuntimeError(
                "every usage pill needs exactly one entry in usage_sources(): "
                f"pills {sorted(USAGE_TRACKER_KEYS)}, sources {sorted(covered)}")
        for source in sources:
            poller = usage_poll.UsagePoller(
                source,
                pills=lambda keys=source.tracker_keys:
                    self.top_bar.usage_pills_for(keys),
                active=lambda provs=source.agent_providers:
                    usage_poll.provider_active(self.manager.all_agents(), provs),
                wanted=lambda keys=source.tracker_keys:
                    any(self._usage_trackers.get(k) for k in keys),
                parent=self)
            # the poller feeds its own pills; the bar only re-decides which
            # of them have something to show
            poller.succeeded.connect(lambda _r: self.top_bar.sync_usage_pills())
            poller.failed.connect(lambda _r: self.top_bar.sync_usage_pills())
            self._usage_pollers[source.name] = poller
        self._usage_pollers["claude"].succeeded.connect(self._apply_usage)
        self._usage_pollers["claude"].absent.connect(self._on_claude_usage_absent)
        self._usage_pollers["gemini"].succeeded.connect(
            lambda r: setattr(self, "_gemini_usage", r))
        self._usage_pollers["codex"].succeeded.connect(
            lambda r: setattr(self, "_codex_usage", r))
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
        # the same hooks plus the agent-lanes family (overlap warnings, lane
        # notices). A separate file, given only to laned Claude agents, so
        # nobody else pays for those hooks.
        self._lane_settings_path = str(session_dir / "aihive_lane_hook.json")
        self._session_map_path = str(session_dir / "live_sessions.jsonl")
        self._prompt_events_path = str(session_dir / "prompt_events.jsonl")
        # Seeded here rather than in _restore_ui_state because the settings
        # file is written NOW, before any agent is armed, and the renderer it
        # names is what those agents launch with.
        self._terminal_scrollback = bool(
            (session or {}).get("ui", {}).get("terminal_scrollback", True))
        try:
            self._write_hook_settings()
            session_hook.reset_map(self._session_map_path)
            session_hook.reset_events(self._prompt_events_path)
        except OSError as e:
            self.store.audit(f"HOOK-SETUP-FAIL {type(e).__name__}: {e}")
            self._hook_settings_path = ""  # degrade: fall back to fs correlation
            self._lane_settings_path = ""
        self.manager.session_map_path = self._session_map_path
        self.manager.prompt_events_path = self._prompt_events_path
        manager.arm_agent = self._arm_agent_mcp  # arm new agents before they start
        manager.audit = self._store_audit   # so a degraded save leaves a trace
        self._rearm_agent_configs()  # restored claude agents re-acquire MCP tools

        self._build_ui()
        self._adopt_existing_model()
        self._wire_model()
        self.lane_service.lanesChanged.connect(self._on_lanes_changed)
        self.lane_service.overlapFound.connect(self._on_lane_overlap)
        # restored laned agents whose folder is missing: repaired in place
        # before they may start (they are holding their start until then)
        self._start_lane_repairs()
        # the lane machinery follows the lanes: running if the session came
        # back with one, and not a single lane git process if it did not
        self._sync_lane_service()
        # after the model is wired: the agents restored with the session are
        # adopted silently, not logged as "added"
        self.event_hub = EventHub(self.manager,
                                  event_log.log_dir(str(session_dir)), self)
        self.event_hub.openChanged.connect(self.top_bar.set_log_attention)
        self._restore_ui_state(session or {})
        self._apply_page_border()   # frame matches the restored skin
        self._ready = True  # from here on, structural changes save immediately
        self._heartbeat_timer.start()
        self._session_sync_timer.start()
        self._prompt_sync_timer.start()
        self._model_sync_timer.start()
        self._bg_shell_timer.start()
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
        self._arm_lane_hooks(ws, agent)
        if not self.bridge.enabled:
            return
        agent.spec.mcp_config_path = self.bridge.mcp_config_path_for(ws.id)

    def _arm_lane_hooks(self, ws, agent) -> None:
        """The agent-lanes hooks, per run: a laned Claude agent gets the lane
        settings file and the env vars that point its hooks at this
        workspace's lanes.json, its notices and its seen file (all absolute,
        all in the workspace's own .aihive), whatever the lanes toggles say.
        Anyone else has them removed, so an installed lane hook exits
        silently. Never persisted (spec.env and settings_path are per run)."""
        env = agent.spec.env
        for key in session_hook.LANE_ENV_KEYS:
            env.pop(key, None)
        lane = agent.spec.lane
        if (not lane or ws.board is None
                or not self._lane_settings_path or not self._hook_settings_path):
            return
        uid = agent.spec.uid
        agent.spec.settings_path = self._lane_settings_path
        env[session_hook.LANE_UID_ENV] = uid
        env[session_hook.LANE_ROOT_ENV] = lane["root"]
        env[session_hook.LANE_REPO_ENV] = lane["repo"]
        env[session_hook.LANES_INDEX_ENV] = lane_svc.index_path(ws)
        env[session_hook.LANE_NOTICES_ENV] = lane_svc.notices_path(ws, uid)
        env[session_hook.LANE_SEEN_ENV] = lane_svc.seen_path(ws, uid)

    def _write_hook_settings(self) -> None:
        """(Re)write the shared `--settings` file every Claude agent launches
        with. Carries the hooks, plus the renderer choice when AI Hive is
        owning the scrollback. Cheap, and re-run whenever the preference
        changes so the NEXT launch picks it up."""
        if not self._hook_settings_path:
            return
        session_hook.write_settings_file(
            self._hook_settings_path, self._session_map_path,
            self._prompt_events_path,
            tui="default" if self._terminal_scrollback else "")
        if self._lane_settings_path:
            session_hook.write_settings_file(
                self._lane_settings_path, self._session_map_path,
                self._prompt_events_path,
                tui="default" if self._terminal_scrollback else "",
                lanes=True)

    def _on_terminal_scrollback(self, on: bool) -> None:
        """Toggle: should Claude agents run the classic renderer so their
        conversation scrolls into AI Hive's own scrollbar?

        Only affects agents started from here on -- a running agent keeps the
        renderer it launched with, and restarting someone's working agent to
        change a display preference is exactly the kind of surprise the
        nudge rules exist to avoid. An ordinary UI preference:
        _schedule_save, never _touch."""
        on = bool(on)
        if on == self._terminal_scrollback:
            return
        self._terminal_scrollback = on
        self._write_hook_settings()
        self._schedule_save()

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
        self.stack = PageStack(center)
        self.stack.setObjectName("PageStack")
        self.activity_panel = ActivityPanel(center)
        self.activity_panel.hide()
        self.activity_panel.taskEdited.connect(self._on_task_edited)
        center_lay.addWidget(self.stack, 1)
        center_lay.addWidget(self.activity_panel)
        # Beside the terminals when they keep their minimum width, floating
        # over them otherwise (see _place_activity).
        self._center_body = center
        self._center_lay = center_lay
        self._activity_floating = False
        center.installEventFilter(self)
        self.activity_panel.widthAnimated.connect(self._place_activity)

        self.body_split.addWidget(self.sidebar)
        self.body_split.addWidget(center)
        self.body_split.setCollapsible(0, True)   # sidebar can drag closed
        self.body_split.setCollapsible(1, False)  # never collapse the terminals
        self.body_split.setStretchFactor(0, 0)
        self.body_split.setStretchFactor(1, 1)
        self.body_split.setSizes([SIDEBAR_WIDTH, 1000])
        self.body_split.splitterMoved.connect(self._on_sidebar_resized)

        # the collapsed sidebar leaves a rail of workspace strips behind,
        # left of the splitter so the splitter handle still drags it open.
        # A sub-layout, not a wrapper widget: one more widget between the
        # splitter and the window delays the window's growth to its minimum
        # width past settle_layout, and every pty child spawns too narrow.
        body_lay = QHBoxLayout()
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        self.ws_rail = WorkspaceRail(central)
        self.ws_rail.hide()
        self.sidebar.set_rail(self.ws_rail)
        body_lay.addWidget(self.ws_rail)
        body_lay.addWidget(self.body_split, 1)

        root.addWidget(self.top_bar)
        root.addLayout(body_lay, 1)
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

        # taskbar working-count overlay: coalesce a burst of per-workspace stat
        # recomputes into one push (see TASKBAR_BADGE_MS)
        self._taskbar_timer = QTimer(self)
        self._taskbar_timer.setSingleShot(True)
        self._taskbar_timer.setInterval(TASKBAR_BADGE_MS)
        self._taskbar_timer.timeout.connect(self._push_taskbar_badge)

        self.top_bar.addTerminalClicked.connect(self._on_add_terminal_clicked)
        self.top_bar.sidebarToggleClicked.connect(self._toggle_sidebar)
        self.top_bar.globalFontDelta.connect(self._change_global_font)
        self.top_bar.themeChanged.connect(self._change_theme)
        self.top_bar.soundToggled.connect(self._on_sound_toggled)
        self.top_bar.replySoundToggled.connect(self._on_reply_sound_toggled)
        self.top_bar.chimeSoundChooseRequested.connect(
            self._on_chime_sound_choose)
        self.top_bar.chimeSoundResetRequested.connect(
            self._on_chime_sound_reset)
        self.top_bar.chimePreviewRequested.connect(self._play_chime)
        self.top_bar.usageTrackerToggled.connect(self._on_usage_tracker_toggled)
        self.top_bar.terminalScrollbackToggled.connect(
            self._on_terminal_scrollback)
        self.top_bar.taskbarBadgeToggled.connect(self._on_taskbar_badge_toggled)
        self.top_bar.usageLeftToggled.connect(self._on_usage_left_toggled)
        self.top_bar.autoUpdateToggled.connect(self._on_auto_update_toggled)
        self.top_bar.agentLanesToggled.connect(self._on_agent_lanes_toggled)
        self.top_bar.updatesPanelRequested.connect(self.open_updates_panel)
        self.top_bar.autoContinueToggled.connect(self._on_auto_continue)
        self.top_bar.startupRecoveryToggled.connect(self._on_startup_recovery)
        # resume whoever the limit cut off, the moment the window reopens
        self.planLimitCleared.connect(self._resume_blocked_agents)
        self.manager.agentLimitBlocked.connect(self._on_agent_limit_blocked)
        self.top_bar.usageRefreshRequested.connect(self._on_usage_refresh)
        self.sidebar.addRequested.connect(self._on_add_workspace_clicked)
        self.sidebar.workspaceSelected.connect(self.manager.set_active)
        self.sidebar.renameRequested.connect(self.manager.rename_workspace)
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
        QShortcut(QKeySequence("Ctrl+Shift+L"), self, self.open_event_log)
        self.top_bar.eventLogClicked.connect(self.open_event_log)
        self.top_bar.appUpdateClicked.connect(self.check_for_app_update)

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
        self._sync_ws_rail()
        self._schedule_save()

    def _on_sidebar_resized(self, _pos: int, _index: int) -> None:
        w = self.body_split.sizes()[0]
        if w > 0:
            self._sidebar_saved_width = w
        self._sync_ws_rail()
        self._schedule_save()

    def _sync_ws_rail(self) -> None:
        """Show the workspace rail exactly while the sidebar is collapsed,
        whether the toggle, Ctrl+Shift+B, a drag or a restore closed it."""
        self.ws_rail.setVisible(self.body_split.sizes()[0] == 0)

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
        # ...and the same edge drives the taskbar badge: busy and waiting both
        # recompute stats, which is exactly the pair the overlay encodes. It is
        # coalesced rather than pushed here, since this fires per workspace.
        # A bound method, not a lambda: the manager outlives a deleted window
        # (the smoke runner deletes each test's windows), and only a slot on
        # this object is disconnected when it goes.
        mgr.workspaceStatsChanged.connect(self._on_stats_for_timers)
        mgr.workspacePathChanged.connect(self._on_workspace_path_changed)
        mgr.workspaceLanesChanged.connect(
            lambda ws_id, _on: self._refresh_workspace_lane_toggles(ws_id))
        mgr.layoutChanged.connect(self._on_layout_changed)
        # an agent just settled on a question ("?" appeared) -> sound the chime
        mgr.agentWaiting.connect(self._on_agent_waiting)
        # ...and one finished a reply the user asked for -> the reply chime
        mgr.agentReplied.connect(self._on_agent_replied)
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
        self._play_chime(chime.QUESTION)

    def _on_agent_replied(self, ws_id: str, agent_id: str) -> None:
        """An agent finished a reply the user asked for (the agent already
        filtered out questions, limit cut-offs and plain shells; see
        TerminalAgent._announce_reply). Ring the reply chime if it is on."""
        if self._reply_sound_enabled:
            self._play_chime(chime.REPLY)

    # ---------------------------------------------------- plan usage ------
    def plan_usage(self):
        """The latest plan-usage reading (`claude_usage.Usage`), or None before
        the first one lands. Public: this plus `planLimitReached` /
        `planLimitCleared` is the API other features hook into — `.blocked`
        says whether the account is cut off, `.resets_at` says until when."""
        return self._usage

    def start_usage_polling(self) -> None:
        """Begin polling every usage source a pill (or, for Claude, the
        plan-limit machinery) wants. OPT-IN, called by main.py only, exactly
        like `quit_on_close`, and for the same reason: the smoke suite
        constructs many windows per process and must never touch the network
        (or the user's real account). Tests feed synthetic readings through
        `_on_usage_ready` and friends instead.

        THERE IS NO CACHED SEED, on purpose. A stored reading goes stale
        exactly where it matters most: a 5-hour window is routinely spent and
        reopened between one launch and the next, so a restored figure is not
        merely old, it is wrong in the direction that misleads. Every enabled
        pill therefore opens in its LOADING state and only ever shows a number
        fetched THIS run.

        The Claude poller runs even with both Claude pills closed:
        planLimitReached, planLimitCleared and plan_usage() hang off its
        reading, and auto-continue depends on them. Closing a pill hides a
        readout; it does not switch a feature off. The other pollers run only
        while one of their pills is on, since nothing else reads them."""
        self._polling = True
        self.top_bar.mark_usage_loading()   # every enabled pill, from t=0
        self._usage_tick_timer.start()
        for poller in self._usage_pollers.values():
            poller.start()                  # a poller nothing wants stays off

    def usage_poller(self, name: str) -> usage_poll.UsagePoller:
        """The poller behind one usage source ("claude", "gemini", "codex")."""
        return self._usage_pollers[name]

    def _poller_for(self, key: str) -> usage_poll.UsagePoller | None:
        return next((p for p in self._usage_pollers.values()
                     if key in p.source.tracker_keys), None)

    def _on_usage_refresh(self) -> None:
        """The user clicked a readout. Every poller clears its backoff and
        polls now: they are asking now, and leaving a timer parked sixteen
        minutes out behind a run of 429s would make a successful manual refresh
        look like it fixed nothing."""
        for poller in self._usage_pollers.values():
            poller.refresh()

    # the three entry points the smoke suite feeds synthetic readings through,
    # exactly as a finished fetch would arrive
    def _on_usage_ready(self, reading) -> None:
        self._usage_pollers["claude"].deliver(reading)

    def _on_gemini_usage_ready(self, reading) -> None:
        self._usage_pollers["gemini"].deliver(reading)

    def _on_codex_usage_ready(self, reading) -> None:
        self._usage_pollers["codex"].deliver(reading)

    def _on_claude_usage_absent(self) -> None:
        """No Claude login at all: the poller has cleared both pills and
        stopped. No Claude account also means no plan limit to recover from,
        so don't leave two switches on the bar that can never do anything."""
        self.top_bar.set_recovery_available(False)
        self.top_bar.sync_usage_pills()

    def _apply_usage(self, reading) -> None:
        """Adopt a Claude reading and fire the plan-limit edges. The poller
        has already put it into both Claude pills.

        NEVER marks the session dirty. See the transient-signal rule in
        CLAUDE.md: this runs every 90 seconds for the life of the process.
        """
        if self._closing:
            return      # a fetch that lands mid-shutdown must not fire edges
        self._usage = reading
        blocked = reading.blocked
        if blocked is not None and not self._plan_blocked:
            self._plan_blocked = True
            self.planLimitReached.emit(blocked)
        elif blocked is None and self._plan_blocked:
            self._plan_blocked = False
            self.planLimitCleared.emit()

    def _tick_usage(self) -> None:
        """Re-render the countdowns from the clock alone, no network. Also
        where each poller follows the work (whether agents are busy changes
        constantly, so a cheap 20 s sweep retunes rather than every
        activity_changed) and notices the machine waking from sleep."""
        self.top_bar.tick_usage()
        for poller in self._usage_pollers.values():
            poller.tick()

    def _on_usage_tracker_toggled(self, key: str, on: bool) -> None:
        """The X on a pill, or an entry in the + menu. One handler for both, so
        the two controls of the same preference can never disagree."""
        if key not in USAGE_TRACKER_KEYS:
            return
        self._usage_trackers[key] = bool(on)
        self.top_bar.set_usage_trackers(self._usage_trackers)
        # a UI PREFERENCE, like sound_enabled. The READING it governs stays
        # transient and still never reaches a save.
        self._schedule_save()
        poller = self._poller_for(key)
        if poller is None:
            return
        if not on:
            if not poller.wanted():
                poller.stop()                # nothing consumes it now
            return
        # Switched back on: load it NOW rather than leave a gap until the next
        # poll. The loading state goes up first, so the pill is on the bar
        # before a fetch that can take ~3s comes back.
        self.top_bar.mark_usage_loading(key)
        if not self._polling:
            return              # this window never opted in; never fetch
        if poller.is_running():
            poller.poll()
        else:
            poller.start()

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
        found: list[tuple] = []           # (agent, record, info)
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
            found.append((agent, record, info))
        if not found:
            return 0

        # 2. ACT ON THE MOST RECENT WINDOW ONLY. Agents stopped by one window
        #    all state the same reset clock, so they group; anything from an
        #    earlier window is history that has already had its chance.
        newest = limit_ledger.latest_window([r for _a, r, _i in found])
        newest_keys = {tuple(r["key"]) for r in newest}
        closed = limit_ledger.closed_keys(
            limit_ledger.read_all(self._ledger_dir()))

        armed = 0
        for agent, record, info in found:
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
                                     banner=record["banner"],
                                     exact=info.get("exact", False))
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
        self._log_limit_outcome(agent, outcome, agent.limit_attempts(), detail)

    def _log_limit_outcome(self, agent, outcome: str, tries: int = 0,
                           detail: str = "") -> None:
        """Mirror a ledger outcome into the event log, beside every
        `record_outcome` call, so the log's cut-off row closes the moment the
        ledger's record does."""
        if self.event_hub is not None:
            self.event_hub.limit_outcome(agent, outcome, tries, detail)

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
        # when the screen doesn't, so borrow it. CLAUDE's account, and therefore
        # only for a Claude agent: filling a Gemini latch with the time the
        # Claude plan reopens would nudge it at an unrelated moment.
        if agent.spec.provider == "claude" and agent.limit_resets_at() is None:
            usage = self.plan_usage()
            if usage is not None:
                agent.set_limit_reset(usage.resets_at)
        at = agent.limit_resets_at()
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at
                else "unknown")
        exact = "" if agent.limit_reset_exact() else " (bare clock)"
        self._limit_audit(f"BLOCKED agent={agent.spec.name} resets={when}"
                          f"{exact if at else ''} "
                          f"window={agent.limit_window() or '-'}")
        # a fresh cut-off gets the full patience budget again
        self._limit_wait_ticks.pop(agent_id, None)
        # File it durably. Skipped when this cut-off is already on record: the
        # startup scan files before it arms, and a failed verify re-latches the
        # same cut-off, so this signal fires more than once per episode.
        key = tuple(self._ledger_key(agent))
        if key not in self._ledger_seen:
            self._ledger_cut_off(agent, agent.limit_cut_off_at(), at or 0.0,
                                 agent.limit_window(),
                                 agent.limit_banner_text(), source="live")
        # The phantom check reads the interrupted turn out of the conversation
        # on disk; only Claude writes one, so a Gemini latch has nothing to
        # re-examine and simply stands.
        if agent.spec.provider == "claude":
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
        # The second detector runs first, so a cut-off it adopts is judged by
        # the very same `due` below on this same tick.
        self._sweep_transcript_cut_offs()
        self._clear_carried_on_latches()
        now = time.time()
        # Late-fill a due time for anything still lacking one (the account
        # reading may only have arrived after the cut-off was latched).
        usage = self.plan_usage()
        if usage is not None and usage.resets_at:
            for a in self.manager.all_agents():
                # Claude's window, so only Claude's agents — see the
                # cross-provider rule in `_resume_blocked_agents`. A Gemini
                # latch always carries its own countdown anyway.
                if (a.spec.provider == "claude" and a.is_limit_blocked()
                        and a.limit_resets_at() is None):
                    a.set_limit_reset(usage.resets_at)

        account_clear = usage is not None and usage.blocked is None

        def due(a):
            # A 7-DAY window (weekly, and the Opus/Sonnet/Fable ones) is not
            # readable off a BARE clock: "resets 8pm" can be days away, and
            # `parse_reset_clock` can only ever resolve that to the next 8pm.
            # Acting on it would nudge days early, every day. So unless the
            # reset is exact (a dated clock, which the CLI prints for anything
            # >24 h out, or the transcript's quotaLimits epoch), such a
            # cut-off waits for the account reading instead.
            if (a.limit_window() in SEVEN_DAY_WINDOWS
                    and not a.limit_reset_exact()):
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

    def _sweep_transcript_cut_offs(self) -> int:
        """The SECOND detector: latch any idle Claude agent whose conversation
        on disk ends on a cut-off the live screen never latched. Returns how
        many it adopted.

        The screen scrape is fast but fragile. It has missed real cut-offs
        three separate ways (a banner painted under the tool-result gutter, a
        banner drawn with cursor jumps instead of spaces, and wordings the CLI
        added later), and each miss was silent: the agent simply sat on a
        spent limit. The transcript is written by the CLI itself in a stable
        JSON shape, so reading it once a minute turns the next such change
        into a late resume instead of a lost night. Cheap: `limit_cut_off` is
        cached by (mtime, size), so an unchanged conversation costs one stat.

        Scoped to cut-offs from THIS run (see LIMIT_SWEEP_SLACK_S), to idle
        agents (a busy one is plainly not parked), and to cut-offs the ledger
        has not already closed, so a conversation this app gave up on or
        dismissed is never re-armed.
        """
        if self._closing or not self._ready:
            return 0
        closed = None
        adopted = 0
        for agent in self.manager.all_agents():
            spec = agent.spec
            if (spec.provider != "claude" or not agent.is_pty
                    or not agent.is_running() or agent.is_busy()
                    or agent.is_limit_blocked() or not spec.session_id):
                continue
            info = transcripts.limit_cut_off(spec.cwd, spec.session_id)
            if not info or not info["cut_off"] or not info["at"]:
                continue
            if info["at"] < self._launched_at - LIMIT_SWEEP_SLACK_S:
                continue        # startup recovery's call, already made
            key = tuple(limit_ledger.key_of(spec.cwd, spec.session_id,
                                            info["resets_at"] or 0.0,
                                            info["at"]))
            if closed is None:
                closed = limit_ledger.closed_keys(
                    limit_ledger.read_all(self._ledger_dir()))
            if key in closed:
                continue
            if key not in self._ledger_seen:
                self._ledger_cut_off(agent, info["at"], info["resets_at"],
                                     info["window"], info["banner"],
                                     source="sweep")
            self._limit_audit(
                f"LATE-LATCH agent={spec.name} (the screen never latched it; "
                f"found in the transcript) banner={info['banner'][:60]!r}")
            agent.mark_limit_blocked(info["resets_at"] or None,
                                     from_startup=False,
                                     cut_off_at=info["at"],
                                     window=info["window"],
                                     banner=info["banner"],
                                     exact=info.get("exact", False))
            adopted += 1
        return adopted

    def _clear_carried_on_latches(self) -> int:
        """Drop any Claude latch the conversation on disk has already moved
        past. Returns how many it cleared.

        Before this, a stale latch was only noticed at its own reset time, by
        the pre-nudge transcript check. That is fine for a genuine cut-off and
        bad for a false one: an echo latch is dated a DAY out (its clock has
        just passed), so the hourglass sat on a working agent and muted its "?"
        chime for 24 h. It happened on 2026-08-04, 08-07 and 09-25, each time
        by a different route past the screen's echo guard.

        The evidence is strictly positive: a record that closed the cut-off
        (ordinary output, or the CLI's own reset notice) written AFTER the
        latch. "Not cut off" alone is not enough, because a banner Claude has
        drawn but not flushed yet reads that way too (see
        `transcripts.limit_cut_off`). An agent with a nudge in flight or
        already sent is left to its verify, which owns that outcome.
        """
        cleared = 0
        for agent in self.manager.all_agents():
            spec = agent.spec
            if (spec.provider != "claude" or not agent.is_limit_blocked()
                    or agent.limit_attempts() or not spec.session_id
                    or agent.id in self._resume_pending):
                continue
            info = transcripts.limit_cut_off(spec.cwd, spec.session_id)
            if (not info or info["cut_off"]
                    or info.get("carried_on_at", 0.0)
                    <= agent.limit_latched_at()):
                continue
            if info.get("self_resumed"):
                self._limit_audit(f"SELF-RESUMED agent={spec.name} (the CLI "
                                  f"continued on its own after the latch)")
                self._ledger_outcome(agent, limit_ledger.RESUMED,
                                     detail="the CLI continued on its own")
            else:
                self._limit_audit(f"CARRIED-ON agent={spec.name} (the "
                                  f"conversation has new work after the "
                                  f"latch)")
                self._ledger_outcome(
                    agent, limit_ledger.DISMISSED,
                    detail="the conversation carried on after the latch")
            agent.clear_limit_block()
            cleared += 1
        return cleared

    def _resume_blocked_agents(self, due=None) -> None:
        """The plan limit reset — put the agents it cut off back to work.

        Two triggers land here. `planLimitCleared` (no `due` filter) means the
        ACCOUNT is provably clear, so every latched agent goes; the usage
        poller's reset poll makes that land within seconds of the window
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

        CROSS-PROVIDER RULE: the account reading behind the no-`due` trigger is
        CLAUDE's, and it says nothing whatsoever about a Gemini quota. So the
        "the account is provably clear, resume everyone" shortcut is Claude-only
        by construction, and a Gemini latch resumes solely on the countdown its
        own message stated (the `due` path). Since agy prints a duration rather
        than a wall clock, that clock is never the ambiguous kind, so nothing is
        lost by leaving it as the only trigger.
        """
        if self._closing or not self._ready:
            return
        blocked = [a for a in self.manager.all_agents()
                   if a.spec.provider in LIMIT_PROVIDERS and a.is_pty
                   and (due is not None or a.spec.provider == "claude")
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
            waits = self._limit_wait_ticks.get(agent.id, 0) + 1
            self._limit_wait_ticks[agent.id] = waits
            self._limit_audit(f"WAIT agent={agent.spec.name} (TUI not ready)")
            # Waiting is right for a booting TUI; waiting FOREVER is how this
            # silently does nothing. A card in a workspace the user has not
            # opened never receives a resizeEvent, so its child is never asked
            # to redraw, so a prompt footer missed on the way past is never
            # seen again and this branch is taken every minute for as long as
            # the app runs (observed live 2026-08-07: nine ticks, ending only
            # when the user clicked that workspace). After a few minutes'
            # patience, ask for the frame instead of hoping for it. Once only,
            # and never during a normal launch — this path is reached only by
            # an agent already latched on a cut-off whose reset has passed.
            if waits == LIMIT_REPAINT_AFTER_WAITS and agent.request_repaint():
                self._limit_audit(f"REPAINT agent={agent.spec.name} "
                                  f"(asked the TUI to redraw after {waits} "
                                  f"quiet ticks)")
            return
        self._limit_wait_ticks.pop(agent.id, None)
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
        #
        # CLAUDE ONLY, and unavoidably so: agy keeps no conversation on disk
        # (only its binary and an `argv.json`), so there is no second source to
        # agree with and the screen latch stands alone. Its identity guard is
        # doing more work here than Claude's as a result — which is why the
        # Gemini reading keys on a per-message Error ID rather than a clock.
        if agent.spec.provider == "claude":
            info = transcripts.limit_cut_off(agent.spec.cwd,
                                             agent.spec.session_id)
            if info is not None and not info["cut_off"]:
                if info.get("self_resumed"):
                    # Claude Code's own auto-continue got there first ("Usage
                    # limit reset \xb7 continuing automatically"), which is the
                    # outcome this feature wants, just not by our hand.
                    self._limit_audit(f"SELF-RESUMED agent={agent.spec.name} "
                                      f"(the CLI continued on its own)")
                    self._ledger_outcome(agent, limit_ledger.RESUMED,
                                         detail="the CLI continued on its own")
                else:
                    self._limit_audit(f"PHANTOM agent={agent.spec.name} (the "
                                      f"conversation carried on, or the "
                                      f"interrupted turn wasn't real work)")
                    self._ledger_outcome(
                        agent, limit_ledger.DISMISSED,
                        detail="transcript shows no real work lost")
                agent.clear_limit_block()
                self._resume_pending.discard(agent.id)
                return
            # The Esc closes the OLD limit options menu ("Stop and wait for
            # limit to reset") when one is actually sitting over the prompt,
            # and ONLY then. Current CLIs draw no menu, and there an Esc does
            # harm: "continuing automatically at ... \xb7 esc or type to
            # cancel" means it CANCELS Claude's own auto-continue, and on a
            # plain prompt it clears whatever the user had half-typed. agy
            # never has a menu. The beat before the text goes out is kept
            # regardless: it costs nothing and staggers the send.
            if agent.limit_menu_visible():
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
                # Same asymmetry as the pre-nudge check: there is no Gemini
                # conversation on disk to consult, so `recheck_limit` is the
                # only evidence there — and it is stronger there than here,
                # because agy answers a still-spent quota with a NEW message
                # rather than leaving the old menu standing.
                stuck = False
                if agent.spec.provider == "claude":
                    stuck, _, _ = transcripts.ended_on_limit(
                        agent.spec.cwd, agent.spec.session_id)
                if menu_gone and not stuck:
                    self._limit_audit(f"RESUMED agent={agent.spec.name}")
                    limit_ledger.record_outcome(
                        self._ledger_dir(), key, limit_ledger.RESUMED,
                        tries=tries)
                    self._log_limit_outcome(agent, limit_ledger.RESUMED,
                                            tries)
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
                    self._log_limit_outcome(agent, limit_ledger.FAILED, tries)
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

    def _on_stats_for_timers(self, *_) -> None:
        self._sync_schedule_timer()
        self._schedule_taskbar_badge()

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

        Delivery is `nudge`: the user pressed a deferred Enter, they did not
        assign a task, so `current_task` must be left exactly as it is.
        `nudge` also
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
            if self.event_hub is not None:
                self.event_hub.scheduled(agent, True, msg.text)
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
            if self.event_hub is not None:
                self.event_hub.scheduled(agent, False, msg.text, why)
            self._schedule_audit(f"MISSED agent={agent.spec.name} "
                                 f"tries={msg.attempts} ({why})")

    def _on_sound_toggled(self, enabled: bool) -> None:
        """User flipped the top-bar chime toggle. Persist the preference (via
        the debounced save) so it survives a restart."""
        self._sound_enabled = bool(enabled)
        self._schedule_save()

    # ------------------------------------------------ custom chime sounds ---
    # The user's own WAV/MP3 per chime. The picked file is COPIED into
    # sounds_dir() under a fresh name, so moving or deleting the original
    # breaks nothing, and a replacement never has to overwrite a file the MCI
    # player may still hold open. chime.play falls back to the built-in sound
    # whenever the copy is missing or unplayable.

    def sounds_dir(self) -> str:
        """Where custom chime files live: beside session.json."""
        return os.path.join(str(self.store.path.parent), "sounds")

    def _play_chime(self, kind: str) -> None:
        chime.play(kind, self._custom_sounds.get(kind))

    def _on_chime_sound_choose(self, kind: str) -> None:
        # deferred: the request arrives from inside the note button's
        # QMenu.exec, and a modal dialog nested in there fights the menu and
        # the Options popup for the mouse grab
        QTimer.singleShot(0, lambda: self._choose_chime_sound(kind))

    def _choose_chime_sound(self, kind: str) -> None:
        self.top_bar.options_panel.hide()
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a chime sound", "",
            "Sounds (*.wav *.mp3);;All files (*)")
        if path:
            self.set_custom_chime(kind, path)

    def set_custom_chime(self, kind: str, source: str) -> str | None:
        """Validate `source`, copy it in, make it this chime's sound and play
        it once. Returns None on success, else the reason (also shown to the
        user in a message box)."""
        reason = chime.validate(source)
        target = ""
        if reason is None:
            ext = os.path.splitext(source)[1].lower()
            # a name no file has yet: a replacement in the same millisecond
            # copied over the current copy, then _drop_custom_chime_file
            # deleted it, leaving the chime pointing at nothing
            stamp = int(time.time() * 1000)
            target = os.path.join(self.sounds_dir(), f"{kind}-{stamp}{ext}")
            while os.path.exists(target):
                stamp += 1
                target = os.path.join(self.sounds_dir(),
                                      f"{kind}-{stamp}{ext}")
            try:
                os.makedirs(self.sounds_dir(), exist_ok=True)
                shutil.copyfile(source, target)
            except OSError as e:
                reason = f"The file could not be copied: {e.strerror or e}"
        if reason is not None:
            QMessageBox.warning(self, "Chime sound not changed", reason)
            return reason
        self._drop_custom_chime_file(kind)
        self._custom_sounds[kind] = target
        self.top_bar.set_custom_sound(kind, os.path.basename(source))
        self._schedule_save()
        self._play_chime(kind)
        return None

    def _on_chime_sound_reset(self, kind: str) -> None:
        self._drop_custom_chime_file(kind)
        self._custom_sounds.pop(kind, None)
        self.top_bar.set_custom_sound(kind, "")
        self._schedule_save()

    def _drop_custom_chime_file(self, kind: str) -> None:
        """Delete this chime's current custom copy, if it has one. chime.stop
        first, because the MCI player keeps an MP3 open (locked) after it."""
        old = self._custom_sounds.get(kind)
        if not old:
            return
        chime.stop()
        try:
            os.remove(old)
        except OSError:
            pass   # already gone, or still locked: harmless leftover

    def _on_reply_sound_toggled(self, enabled: bool) -> None:
        """User flipped the reply-finished chime toggle. Persisted like the
        question chime's."""
        self._reply_sound_enabled = bool(enabled)
        self._schedule_save()

    # ------------------------------------------- taskbar working-count badge ---
    # The one signal AI Hive has that reaches the user in ANOTHER application.
    # Everything else that says "an agent is working" lives inside the window
    # (the sidebar's pulsing badge, the WorkspaceSpinner), which is no use at
    # all while the user is waiting in some other app for the hive to finish.
    #
    # Windows allows exactly ONE overlay icon and fixes it to the corner of the
    # taskbar button, so both facts have to share one ~16px square: the DIGIT is
    # how many agents are working, the COLOUR is whether any of them is stuck on
    # a question. An idle hive gets no overlay at all, which is what makes "is
    # anything still running?" answerable from across the room.

    def _taskbar_state(self) -> tuple[int, bool, int]:
        """(agents working, does any agent need the user, agents idle but
        waiting on a background shell to finish).

        Deliberately the SAME predicates the sidebar reads (`is_busy` /
        `is_waiting` / `is_bg_shell_busy`) rather than a parallel notion of
        activity, so the taskbar and the sidebar can never disagree about
        what the hive is doing.
        """
        agents = self.manager.all_agents()
        return (sum(1 for a in agents if a.is_busy()),
                any(a.is_waiting() for a in agents),
                sum(1 for a in agents if a.is_bg_shell_busy()))

    def _schedule_taskbar_badge(self) -> None:
        """Coalesce a burst of stat recomputes into one push.

        (An Explorer restart destroys the taskbar button and with it the
        overlay. No re-arm is wired for that on purpose: a working hive changes
        busy state within seconds, so the very next push repaints it, and an
        idle hive is meant to have no overlay anyway. The message hook that
        would cover the remaining case runs on EVERY window message, which is
        not a Python callback this app wants in its terminals' path.)
        """
        if self._closing:
            return
        self._taskbar_timer.start()   # single-shot: restarting is the debounce

    def _taskbar_badge_spec(self) -> tuple[str, str | None, str | None, str]:
        """What the overlay should be right now: `(key, text, fill, note)`.

        `text is None` means NO overlay - an idle hive shows a plain icon, and
        that absence is itself the readout ("nothing is running, go and look").
        The `key` is what the push is edge-guarded on, so it must collapse every
        state that renders identically: past 9 the disc only ever says "9+".

        Kept separate from the push so the whole state table is decidable
        without a shell, a window handle or a COM apartment.

        Priority when several states hold at once: asking wins outright (it
        needs the user NOW); a genuinely busy count is next; a background
        shell only surfaces when NEITHER of those is true -- it is exactly
        the case that used to leave the taskbar silent even though a
        background command was still running.
        """
        count, asking, bg = self._taskbar_state()
        if not self._taskbar_badge:
            count, asking, bg = 0, False, 0   # switched off reads as "nothing"
        key = f"{min(count, 10)}|{int(asking)}|{min(bg, 10)}"
        if count <= 0 and not asking and bg <= 0:
            return (key, None, None, "")
        # >9 stops being a number anyone reads at a glance, and stops fitting
        # the disc; with nothing working the glyph carries the meaning instead.
        if asking:
            text = "?" if count <= 0 else ("9+" if count > 9 else str(count))
            note = (f"{count} working, one is waiting for you" if count > 0
                     else "an agent is waiting for you")
            return (key, text, ornaments.TASKBAR_ASKING, note)
        if count > 0:
            text = "9+" if count > 9 else str(count)
            return (key, text, ornaments.TASKBAR_WORKING, f"{count} working")
        text = "9+" if bg > 9 else str(bg)
        note = f"{bg} agent(s) idle, waiting on a background command to finish"
        return (key, text, ornaments.TASKBAR_BG_SHELL, note)

    def _push_taskbar_badge(self) -> None:
        """Render the overlay and hand it to the shell, if it actually changed.

        CRITICAL, the same rule as `activity_changed` and the plan-usage
        reading: this is TRANSIENT. It runs for the life of the process every
        time an agent's output starts or stops, so it must never touch `_touch`
        or `_schedule_save` - only the on/off preference saves. And the edge
        guard is not an optimisation: `workspaceStatsChanged` fires every couple
        of seconds per busy agent, and each push builds an HICON and crosses a
        COM boundary, so pushing unconditionally would have the shell repainting
        an identical badge forever.

        `_taskbar_key` advances only on a SUCCESSFUL push, so a shell that
        refused one attempt is retried on the next state change rather than
        being remembered as up to date.
        """
        if self._closing:
            return
        if QGuiApplication.platformName() != "windows":
            return   # offscreen (the smoke suite) has no taskbar to decorate
        from .. import taskbar_overlay
        if not taskbar_overlay.available():
            return   # the shell refused the interface; it will not start later
        key, text, fill, note = self._taskbar_badge_spec()
        if key == self._taskbar_key:
            return
        hwnd = int(self.winId())
        if text is None:
            ok = taskbar_overlay.clear(hwnd)
        else:
            ok = taskbar_overlay.set_overlay(
                hwnd,
                ornaments.taskbar_badge_bgra(
                    text, fill, taskbar_overlay.overlay_size()),
                note)
        if ok:
            self._taskbar_key = key

    def _on_taskbar_badge_toggled(self, enabled: bool) -> None:
        """User flipped the top-bar taskbar-count toggle. An ordinary UI
        preference, so it persists on the debounced save like `sound_enabled`;
        the COUNT it controls never does."""
        self._taskbar_badge = bool(enabled)
        self._schedule_save()
        self._push_taskbar_badge()   # apply now, don't wait for an agent event

    def _on_usage_left_toggled(self, enabled: bool) -> None:
        """User flipped "Show usage left". The top bar has already reworded
        its pills; this only persists the choice, as an additive optional
        key under "ui" like `taskbar_badge` (no SESSION_VERSION bump)."""
        self._usage_left = bool(enabled)
        self._schedule_save()

    def _on_auto_update_toggled(self, enabled: bool) -> None:
        """User flipped the startup CLI auto-update switch. An ordinary UI
        preference: additive optional key under "ui", saved on the debounced
        timer, no SESSION_VERSION bump (identical to `taskbar_badge` and the
        two recovery toggles). It takes effect on the NEXT launch, since the
        gate runs before the window exists."""
        self._auto_update = bool(enabled)
        self._schedule_save()

    # ------------------------------------------------------- agent lanes ---
    # Lane scopes. Two
    # toggles, the Options switch "Lanes in every workspace" and each
    # workspace's own, decide ONE thing: whether "Own lane" starts ticked
    # for a new agent (lane_default). A lane is created only by the user's
    # tick in the New Agent dialog or the card's Restart in own lane. The
    # lane machinery (poller, hooks, notices, Make integrator)
    # follows the lanes themselves, never the toggles: it runs for any lane
    # that exists, and with no lane anywhere no lane git process starts. An
    # agent that has a lane keeps it until its card closes: its conversation
    # is keyed by the lane folder, so moving it back would lose it.

    def _on_agent_lanes_toggled(self, enabled: bool) -> None:
        """User flipped "Lanes in every workspace". An ordinary UI
        preference (additive key under "ui", debounced save, no
        SESSION_VERSION bump, like `auto_update`). It starts no git and
        touches no agent: it changes the dialog's default tick, the
        workspace toggles' display and where Restart in own lane is offered."""
        self._agent_lanes = bool(enabled)
        self._schedule_save()
        self._refresh_workspace_lane_toggles()

    def lane_default(self, ws) -> bool:
        """Should a new Claude agent in `ws` start with "Own lane" ticked?
        The Options switch forces it on everywhere; otherwise the
        workspace's own toggle decides. The ONLY question a toggle answers."""
        return bool(self._agent_lanes or (ws is not None and ws.lanes))

    def _any_lanes(self) -> bool:
        """Does any agent have a lane, or one being made? What the lane
        service's lifetime follows (_sync_lane_service)."""
        return bool(self._lane_pending) or any(
            a.spec.lane for a in self.manager.all_agents())

    def _sync_lane_service(self) -> None:
        """Run the lane service exactly while some lane exists. start() and
        stop() are idempotent; stop() writes "enabled": false into every
        lanes.json it wrote, which is right when the last lane goes."""
        if getattr(self, "_closing", False):
            return          # shutdown() owns the service now, writing nothing
        if self._any_lanes():
            if not self.lane_service.is_running():
                self.lane_service.start()
        elif self.lane_service.is_running():
            self.lane_service.stop()

    def _on_workspace_lanes_toggled(self, ws_id: str, on: bool) -> None:
        """A workspace header's ⎇ Lanes toggle. Persisted on the workspace.
        Starts no git and changes no existing agent."""
        if self._agent_lanes:
            # forced on: the toggle is disabled, so this only resyncs it
            self._refresh_workspace_lane_toggles()
            return
        self.manager.set_workspace_lanes(ws_id, on)

    def _refresh_workspace_lane_toggles(self, ws_id: str = "") -> None:
        for wid, page in self._pages.items():
            if ws_id and wid != ws_id:
                continue
            ws = self.manager.workspace(wid)
            if ws is None:
                continue
            # a folder walk for .git, not a git process (lanes.find_repo_root)
            page.set_lanes_state(self.lane_default(ws),
                                 forced=self._agent_lanes,
                                 available=bool(lanes.find_repo_root(
                                     ws.project_path)))

    # Lane lifetime (spec "Lane lifetime"). LIVE from creation until the card
    # closes: the path never changes, a missing folder is repaired in place.
    # RETIRED on close: removed when it holds nothing, otherwise kept and the
    # user told so. REVIVED by resuming one of its conversations from the New
    # Agent dialog. No toggle is asked by any of them: repair, retire and
    # revive protect lanes that already exist.
    # Every git mutation goes through self.lane_ops, never inline.

    # a retire that found a program still running in the lane tries again
    LANE_RETIRE_TRIES = 3
    LANE_RETIRE_RETRY_MS = 30_000

    def _live_lane_roots(self) -> set:
        return ({a.spec.lane.get("root", "") for a in self.manager.all_agents()
                 if a.spec.lane} | self._lane_retiring)

    def _create_lane(self, ws_id: str, agent, plan,
                     adopt: bool = False) -> None:
        """Create a new agent's lane, then start it. The agent was added
        unstarted with its lane already recorded (or, `adopt`, stopped and
        moved into it by Restart in own lane); it holds its start (a waking
        keystroke included) until the worktree exists."""
        agent.hold_start("[creating this agent's lane (git worktree)...]")
        agent.notice("[creating this agent's lane (git worktree)...]")
        self._lane_pending.add(agent.spec.uid)
        lane, cwd, t0 = plan.lane_dict(), plan.cwd, time.monotonic()
        self.lane_ops.submit(
            plan.repo, lanes.create_lane, lane, cwd, label="create",
            callback=lambda res, err, a=agent.id, u=agent.spec.uid,
            n=agent.spec.name: self._on_lane_created(
                ws_id, a, u, n, lane, cwd, t0, res, err, adopt=adopt))

    def _on_lane_created(self, ws_id, agent_id, uid, name, lane, cwd, t0,
                         result, error, adopt: bool = False) -> None:
        self._lane_pending.discard(uid)
        ms = int((time.monotonic() - t0) * 1000)
        agent = self.manager.agent(ws_id, agent_id)
        if error is not None:
            self._store_audit(
                f"LANE-FAIL {getattr(error, 'code', 'error')} "
                f"{'adopt' if adopt else 'create'} "
                f"agent={name!r} root={lane['root']} branch={lane['branch']}: "
                f"{error}")
            if agent is not None:
                # a lane problem never blocks the agent itself
                self.manager.clear_agent_lane(ws_id, agent_id)
                agent.notice(f"[no lane: {error}. This agent works in the "
                             f"workspace folder.]")
                self._push_lane_view(ws_id, agent)
                agent.release_start(run=True)
            self._sync_lane_service()
            return
        self._store_audit(
            f"LANE-CREATE agent={name!r} root={result['root']} "
            f"branch={result['branch']} base={result['base']} ms={ms}"
            + (" adopt=1" if adopt else ""))
        if agent is None:
            # closed while its lane was being made: the new lane holds nothing
            self._retire_lane(result, (), name)
            self._sync_lane_service()
            return
        self.manager.set_agent_lane(ws_id, agent_id, result, cwd)
        self._sync_lane_service()
        self._hint_integrator(ws_id, agent)
        if self.event_hub is not None:
            self.event_hub.lane_event(
                agent, f"{'restarted in' if adopt else 'got'} its own lane "
                       f"{result['branch']} (from "
                       f"{result['base'] or 'the base branch'})"
                       + (", in a new conversation" if adopt else ""),
                {"root": result["root"], "branch": result["branch"]})
        self._push_lane_view(ws_id, agent)
        agent.release_start(run=True)

    TWO_LANES_HINT = ("[Two lanes in this workspace. Pick an integrator (the "
                      "flag in a laned Claude agent's tray). When you ask "
                      "it to, it ships the lanes whose agent committed "
                      "\"Task done\".]")

    def _hint_integrator(self, ws_id: str, agent) -> None:
        """The first time this run a workspace has two lanes and no
        integrator, say on the new lane's card what the integrator is for.
        Once per workspace per run: a hint, not a nag."""
        ws = self.manager.workspace(ws_id)
        if (ws is None or ws_id in self._integrator_hinted
                or self.manager.integrator(ws_id) is not None
                or sum(1 for a in ws.agents if a.spec.lane) < 2):
            return
        self._integrator_hinted.add(ws_id)
        agent.notice(self.TWO_LANES_HINT)

    def _start_lane_repairs(self) -> None:
        """Repair, at the same path, every restored lane whose folder was
        missing (WorkspaceManager.load_session_dict queued them). Never moves
        the agent to the workspace folder."""
        for ws_id, agent_id in self.manager.take_lane_repairs():
            agent = self.manager.agent(ws_id, agent_id)
            if agent is not None:
                self._repair_lane(ws_id, agent, revive=False)

    def _revive_lane(self, ws_id: str, agent) -> None:
        """A past lane conversation picked in the New Agent dialog: bring its
        lane back at the identical path, then resume it there."""
        agent.hold_start("[restoring this conversation's lane...]")
        agent.notice("[restoring this conversation's lane...]")
        self._repair_lane(ws_id, agent, revive=True)

    def _repair_lane(self, ws_id: str, agent, revive: bool) -> None:
        lane = dict(agent.spec.lane)
        self._lane_pending.add(agent.spec.uid)
        self.lane_ops.submit(
            lane["repo"], lanes.repair_lane, lane,
            label="revive" if revive else "repair",
            callback=lambda res, err, a=agent.id, u=agent.spec.uid,
            n=agent.spec.name: self._on_lane_repaired(
                ws_id, a, u, n, lane, revive, res, err))

    def _on_lane_repaired(self, ws_id, agent_id, uid, name, lane, revive,
                          result, error) -> None:
        self._lane_pending.discard(uid)
        try:
            self._finish_lane_repair(ws_id, agent_id, uid, name, lane, revive,
                                     result, error)
        finally:
            # a failed revive drops the card; a closed one retires the lane
            self._sync_lane_service()

    def _finish_lane_repair(self, ws_id, agent_id, uid, name, lane, revive,
                            result, error) -> None:
        what = "revive" if revive else "repair"
        agent = self.manager.agent(ws_id, agent_id)
        if error is not None:
            self._store_audit(
                f"LANE-FAIL {getattr(error, 'code', 'error')} {what} "
                f"agent={name!r} root={lane['root']} branch={lane['branch']}: "
                f"{error}")
            if agent is None:
                return
            if revive:
                # never resumed in the workspace folder instead: the agent
                # would work in the main checkout while its conversation says
                # it is in its lane. The card goes, and the user is told why.
                agent.spec.lane = {}            # nothing of ours to retire
                self.manager.remove_terminal(ws_id, agent_id)
                self._lane_message(
                    "Conversation not resumed",
                    f"The lane this conversation started in could not be "
                    f"restored, so it was not resumed.\n\n{lane['root']}\n"
                    f"{error}")
                return
            reason = (f"[this agent's lane could not be restored: {error}. It "
                      f"stays stopped rather than work in another folder. "
                      f"Close the card to retire it.]")
            agent.notice(reason)
            agent.hold_start(reason)
            return
        new_lane, recreated = result
        self._store_audit(
            f"LANE-{'REVIVE' if revive else 'REPAIR'} agent={name!r} "
            f"root={new_lane['root']} branch={new_lane['branch']} "
            f"recreated_branch={recreated}")
        if agent is None:
            self._retire_lane(new_lane, (), name)
            return
        cwd = agent.spec.cwd if os.path.isdir(agent.spec.cwd) \
            else new_lane["root"]
        self.manager.set_agent_lane(ws_id, agent_id, new_lane, cwd)
        self._push_lane_view(ws_id, agent)
        if recreated:
            agent.notice(f"[its branch {new_lane['branch']} was merged and "
                         f"deleted, so it was recreated from "
                         f"{new_lane['base']}: this lane starts fresh]")
            # the card log is for the user; the model still believes its old
            # commits are on its branch. A lane notice reaches it with its
            # next prompt (a laned agent's hooks are always armed).
            ws = self.manager.workspace(ws_id)
            if ws is not None and ws.board is not None:
                try:
                    session_hook.append_notice(
                        lane_svc.notices_path(ws, uid),
                        f"revive|{new_lane['branch']}|{time.time():.3f}",
                        f"Your lane's branch {new_lane['branch']} was merged "
                        f"into {new_lane['base'] or 'the base branch'} and "
                        f"deleted while you were away, so it was recreated "
                        f"from {new_lane['base'] or 'the base branch'}. Your "
                        f"earlier commits are not on it any more: they are "
                        f"in the base branch. Read files again before you "
                        f"edit them.")
                except OSError:
                    pass
        agent.release_start(run=revive)

    # Restart in own lane: an existing agent without
    # a lane starts a NEW conversation in a new worktree, keeping its name,
    # uid and card. The order is the point; each step closes a race:
    #   1. hold its start, so nothing (a keystroke, a scheduled send, the
    #      autostart) relaunches it in the main folder meanwhile;
    #   2. stop it, and go on only once the worker has ended (a stopped agent
    #      is out of session sync's reach);
    #   3. clear its session id BEFORE the lane is recorded, so a crash in
    #      between can't --resume the main-folder conversation inside the
    #      lane;
    #   4. record the lane (saves, re-applies the prompt, re-arms the hooks),
    #      then create it; its callback releases the hold.
    # The old conversation stays on disk under the main folder. No running
    # agent holds its id any more, so the New Agent picker lists it.
    # There is no way back: leaving a lane is closing the card.

    def _adopt_state(self, ws, agent):
        """(enabled, tooltip) for Restart in own lane, or None when the card
        does not offer it: only an interactive Claude agent with no lane, in
        a git workspace whose lane default is on."""
        if (agent.spec.provider != "claude" or not agent.is_pty
                or agent.spec.lane or not self.lane_default(ws)
                or not lanes.find_repo_root(ws.project_path)):
            return None
        uid = agent.spec.uid
        if uid in self._lane_pending or uid in self._lane_adopting:
            return False, "This agent is already moving into its own lane."
        if agent.is_busy() or agent.is_waiting():
            return False, ("Wait until this agent has finished its turn and "
                           "answered you.")
        return True, (
            "Stops this agent and starts a new conversation in its own git "
            "worktree and branch. Its current conversation stays in the "
            "workspace folder, and New Agent can resume it.\n\nAgents left "
            "in the workspace folder are invisible to the lanes' overlap "
            "checks until their work is committed and pushed.")

    def _confirm_adopt(self, agent) -> bool:
        """The user's go-ahead for Restart in own lane (tests replace it)."""
        answer = QMessageBox.question(
            self, "Restart in own lane",
            f"{agent.spec.name} starts a new conversation in its own lane. "
            f"The current conversation stays in this workspace's folder, and "
            f"New Agent can resume it.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Ok

    @staticmethod
    def _adopt_stopped(agent) -> bool:
        return (not agent.is_running()
                and agent.status not in (AgentStatus.STARTING,
                                         AgentStatus.RUNNING,
                                         AgentStatus.STOPPING))

    def _restart_in_lane(self, ws_id: str, agent) -> None:
        ws = self.manager.workspace(ws_id)
        state = self._adopt_state(ws, agent) if ws is not None else None
        if not state or not state[0]:
            if state:
                self._lane_message("Not moved", state[1])
            return
        uid, agent_id = agent.spec.uid, agent.id
        # 1. nothing may relaunch it in the main folder from here on
        agent.hold_start("[moving this agent into its own lane...]")
        agent.notice("[moving this agent into its own lane...]")
        if self._adopt_stopped(agent):
            self._lane_adopting[uid] = None
            self._continue_adopt(ws_id, agent_id, uid)
            return

        # 2. stop (Ctrl+C, then the job object's hard kill), and go on only
        # from the status the worker's end produces, never from a timer
        def on_status(_status, a=agent):
            if self._adopt_stopped(a):
                self._continue_adopt(ws_id, agent_id, uid)
        self._lane_adopting[uid] = (agent, on_status)
        agent.status_changed.connect(on_status)
        agent.stop()

    def _continue_adopt(self, ws_id: str, agent_id: str, uid: str) -> None:
        wait = self._lane_adopting.pop(uid, None)
        if wait:
            try:
                wait[0].status_changed.disconnect(wait[1])
            except (RuntimeError, TypeError):
                pass
        ws = self.manager.workspace(ws_id)
        agent = self.manager.agent(ws_id, agent_id)
        if (ws is None or agent is None or agent.spec.lane
                or getattr(agent, "_disposing", False)):
            return              # its card closed while it stopped
        name = agent.spec.name
        old_sid = agent.spec.session_id
        # 3. the main-folder conversation is let go of BEFORE the lane is
        # recorded: the lane's first start is a fresh one, never a --resume
        agent.spec.session_id = ""
        agent.spec.resume = False
        agent.note_conversation_replaced()
        repo = lanes.find_repo_root(ws.project_path)
        try:
            plan = lanes.plan_lane(repo, ws.project_path, name, uid)
        except lanes.LaneError as exc:
            self._store_audit(f"LANE-FAIL {exc.code} adopt agent={name!r}: "
                              f"{exc}")
            agent.notice(f"[no lane: {exc}. This agent starts a new "
                         f"conversation in the workspace folder.]")
            agent.release_start(run=True)
            return
        # 4. record the lane, then make it
        self.manager.set_agent_lane(ws_id, agent_id, plan.lane_dict(),
                                    plan.cwd)
        self._store_audit(f"LANE-ADOPT agent={name!r} root={plan.root} "
                          f"branch={plan.branch} left={old_sid or '-'}")
        self._create_lane(ws_id, agent, plan, adopt=True)
        self._sync_lane_service()
        self._push_lane_view(ws_id, agent)

    def _lane_retire_args(self, agent) -> tuple:
        """(lane, pids, name) for retiring `agent`'s lane, read BEFORE the
        agent is disposed: the retire waits for those processes to exit."""
        pids = []
        try:
            worker = agent.worker
            pid = worker.pid() if hasattr(worker, "pid") else None
            pids = list(getattr(worker, "job_process_ids", lambda: [])())
            if pid and pid not in pids:
                pids.append(pid)
        except Exception:
            pass
        return dict(agent.spec.lane), pids, agent.spec.name

    def _close_agent(self, ws_id: str, agent_id: str) -> None:
        """A card's close button. A laned agent's lane is retired after the
        agent is gone, whatever the lanes toggles say. Closing the card is
        also the one way to quiet a lane's warnings."""
        agent = self.manager.agent(ws_id, agent_id)
        retire = None
        if (agent is not None and agent.spec.lane
                and agent.spec.uid not in self._lane_pending):
            retire = self._lane_retire_args(agent)
        self.manager.remove_terminal(ws_id, agent_id)
        if retire is not None:
            self._retire_lane(*retire)

    def _retire_lane(self, lane: dict, pids, name: str,
                     attempt: int = 1) -> None:
        self.lane_ops.submit(
            lane["repo"], lanes.retire_lane, lane, tuple(pids), label="retire",
            callback=lambda res, err: self._on_lane_retired(
                lane, name, res, err, pids, attempt))

    def _on_lane_retired(self, lane, name, result, error, pids=(),
                         attempt: int = 1) -> None:
        where = f"agent={name!r} root={lane['root']} branch={lane['branch']}"
        if error is not None or result is None:
            self._store_audit(f"LANE-FAIL retire {where}: {error}")
            self._show_lane_kept(name, lane, None, str(error))
            return
        if result.removed:
            ignored = ""
            if result.ignored:
                more = len(result.ignored) - 20
                extra = f" +{more} more" if more > 0 else ""
                ignored = (f" ignored={len(result.ignored)} "
                           f"[{' '.join(result.ignored[:20])}{extra}]")
            self._store_audit(f"LANE-REMOVE {where}{ignored}")
            return
        st = result.status
        detail = (f" ahead={st.ahead} dirty={len(st.dirty)} "
                  f"local={len(st.local)} merged={st.merged}"
                  if st is not None else "")
        if result.code == "busy":
            self._store_audit(f"LANE-KEEP {where}{detail} reason=busy "
                              f"attempt={attempt}")
            if attempt < self.LANE_RETIRE_TRIES:
                self._lane_retiring.add(lane["root"])
                QTimer.singleShot(
                    self.LANE_RETIRE_RETRY_MS, self,
                    lambda: self._retry_retire(lane, pids, name, attempt + 1))
                return
        elif result.code in ("git", "moved"):
            # git itself refused: a process outside the agent (Explorer, an
            # editor, a terminal) holds the folder. Never retried with --force.
            self._store_audit(f"LANE-FAIL retire {where}: {result.reason}")
        else:
            why = f" reason={result.reason!r}" if result.reason else ""
            self._store_audit(f"LANE-KEEP {where}{detail}{why}")
        self._show_lane_kept(name, lane, st, result.reason)

    def _retry_retire(self, lane, pids, name, attempt: int) -> None:
        self._lane_retiring.discard(lane["root"])
        # revived meanwhile: the lane is live again and not ours to retire
        if any(lanes.same_path(a.spec.lane.get("root", ""), lane["root"])
               for a in self.manager.all_agents() if a.spec.lane):
            return
        self._retire_lane(lane, pids, name, attempt)

    # Lane awareness (spec Phase 2). LaneService polls; these put what it saw
    # on the cards, the Activity panel, the roster and the event log, and run
    # the chip's actions. All transient: nothing here reaches a save.

    def _on_lanes_changed(self, ws_id: str, views: dict) -> None:
        self.manager.set_lane_views(ws_id, views)
        page = self._pages.get(ws_id)
        if page is not None:
            for card in page.cards:
                card.set_lane_view(views.get(card.agent.spec.uid)
                                   if card.agent.spec.lane else None)
        if self.activity_panel.is_open() and ws_id == self.manager.active_id:
            self.activity_panel.set_lanes(views)
        self._refresh_idle_lanes(ws_id, views)
        self._log_done_lanes(ws_id, views)

    def _push_lane_view(self, ws_id: str, agent) -> None:
        """One agent's lane record changed (created, failed, revived): repaint
        its chip now and poll soon, rather than wait for the next tick."""
        page = self._pages.get(ws_id)
        card = page.card_for(agent.id) if page is not None else None
        if card is not None:
            card.set_lane_view(self.lane_service.view(agent.spec.uid)
                               if agent.spec.lane else None)
        self.lane_service.poke()

    def _on_lane_overlap(self, ws_id: str, uid: str, info: dict) -> None:
        """First sighting of an overlap: one event-log row per pair."""
        ws = self.manager.workspace(ws_id)
        agent = next((a for a in (ws.agents if ws is not None else [])
                      if a.spec.uid == uid), None)
        if agent is None or self.event_hub is None:
            return
        paths = list(info.get("paths") or [])
        files = ", ".join(paths[:3])
        if len(paths) > 3:
            files += f" (+{len(paths) - 3} more)"
        peer = info.get("peer", "")
        if not info.get("peer_uid"):
            text = (f"{peer} changed files its lane also changed, in a way "
                    f"that conflicts: {files}"
                    if info.get("level") == lanes.CONFLICTS else
                    f"{peer} changed files its lane also changed: {files}")
        elif info.get("level") == lanes.CONFLICTS:
            text = f"would conflict with {peer} on {files}"
        else:
            text = f"changed the same files as {peer}: {files}"
        self.event_hub.lane_event(agent, text, dict(info))

    def _on_lane_action(self, ws_id: str, agent_id: str, action: str) -> None:
        """The card chip's menu. Opening is read-only; updating writes refs
        and goes through lane_ops like every other lane mutation."""
        agent = self.manager.agent(ws_id, agent_id)
        if action == "adopt":
            if agent is not None and self._confirm_adopt(agent):
                self._restart_in_lane(ws_id, agent)
            return
        lane = dict(agent.spec.lane) if agent is not None else {}
        if not lane:
            return
        if action == "open":
            if os.path.isdir(lane["root"]):
                fsopen.open_path(lane["root"])
            else:
                self._lane_message("Lane folder missing",
                                   f"The lane folder is missing:\n"
                                   f"{lane['root']}")
        elif action == "refresh":
            # the chip's menu may have opened before the agent started a turn
            if agent.is_busy() or agent.is_waiting():
                agent.notice("[lane not updated: this agent is working. "
                             "Update it once its turn is over.]")
                return
            uid, name = agent.spec.uid, agent.spec.name
            self.lane_ops.submit(
                lane["repo"], lanes.refresh_lane, lane, label="refresh",
                callback=lambda res, err: self._on_lane_refreshed(
                    ws_id, agent_id, uid, name, lane, res, err))
        elif action == "integrator":
            self._toggle_integrator(ws_id, agent)
        elif action == "ship":
            self._ship_lanes(ws_id, agent)

    def _on_lane_refreshed(self, ws_id, agent_id, uid, name, lane, result,
                           error) -> None:
        where = f"agent={name!r} root={lane['root']} branch={lane['branch']}"
        agent = self.manager.agent(ws_id, agent_id)
        if error is not None:
            self._store_audit(f"LANE-FAIL {getattr(error, 'code', 'error')} "
                              f"refresh {where}: {error}")
            if agent is not None:
                agent.notice(f"[lane not updated: {error}]")
            return
        self._store_audit(f"LANE-REFRESH {where} to={result or 'unchanged'}")
        ws = self.manager.workspace(ws_id)
        if agent is not None and result:
            agent.notice(f"[this lane was fast-forwarded to {result}]")
            if ws is not None and ws.board is not None:
                # the agent's files changed under it: say so on its next prompt
                try:
                    session_hook.append_notice(
                        lane_svc.notices_path(ws, uid),
                        f"refresh|{time.time():.3f}",
                        f"The user fast-forwarded your lane to {result}. "
                        f"Files in your lane may have changed since you read "
                        f"them; read a file again before you edit it.")
                except OSError:
                    pass
        self.lane_service.poke()

    # The integrator (docs/agents/integration.md). The USER picks it. A lane
    # agent flags finished work with a "Task done" commit line, the lane
    # service sees it (LaneView.done), and AI Hive shows it on the chip and
    # logs it, nothing more. The user decides when to ship and asks the
    # integrator. Everything after that (combining the lanes, the pull
    # request, the review loop, the merge) is the integrator's own work in
    # its own lane. AI Hive never merges anything and never tells the
    # integrator to start on its own. The one way AI Hive types into it is
    # the integrator chip's Ship finished lanes, a click by the user
    # (_ship_lanes).

    def _integration_info(self, agent) -> dict:
        """What a card's action tray shows for the integrator role and for
        Restart in own lane (see TerminalCard.integration_info)."""
        ws = self.manager.workspace_of(agent.id)
        if ws is None:
            return {}
        is_integrator = self.manager.is_integrator(agent)
        info = {"integrator": is_integrator}
        adopt = self._adopt_state(ws, agent)
        if adopt is not None:
            info["adopt"] = ("Restart in own lane (new conversation)",) + adopt
        if is_integrator:
            info["role"] = ("Stop being the integrator", True,
                            "This agent stops being the one that ships "
                            "finished lanes when you ask.")
            info["ship"] = self._ship_offer(ws, agent)
        elif (agent.spec.provider == "claude" and agent.is_pty
              and (agent.spec.lane or any(a.spec.lane for a in ws.agents))):
            # offered where lanes are: in a workspace without any, the tray
            # is exactly what it was before lanes
            why = self.manager.can_integrate(agent)
            if why and not agent.spec.lane and adopt is not None:
                why = ("This agent has no lane. Restart it in its own lane "
                       "first (the ⎇ button in this tray).")
            current = self.manager.integrator(ws.id)
            tip = why or (
                (f"Replaces {current.spec.name} as this workspace's "
                 f"integrator. " if current is not None else "")
                + "When you ask it to, it combines the lanes whose agent "
                  "committed \"Task done\" into one pull request, gets it "
                  "reviewed and tested, and merges it.")
            info["role"] = ("Make integrator", not why, tip)
        return info

    @staticmethod
    def _finished_lanes(ws, integrator) -> list:
        """The lanes with a "Task done" commit the base lacks, as the lane
        service last read them: (agent, LaneView) pairs, integrator left
        out (its branch carries the flags it merged in)."""
        found = []
        for agent in ws.agents:
            view = getattr(agent, "lane_view", None)
            if (agent is not integrator and agent.spec.lane
                    and view is not None and view.done):
                found.append((agent, view))
        return found

    def _ship_offer(self, ws, agent) -> tuple:
        """(label, enabled, tooltip) for the integrator chip's Ship finished
        lanes."""
        done = self._finished_lanes(ws, agent)
        label = (f"Ship finished lanes ({len(done)})" if done
                 else "Ship finished lanes")
        if not done:
            why = "No lane has a \"Task done\" commit that the base lacks."
        elif not agent.is_running():
            why = "The integrator is stopped. Restart it first."
        elif agent.is_busy() or agent.is_waiting():
            why = "Wait until the integrator has finished its turn."
        elif not agent.prompt_ready():
            why = "The integrator is still starting."
        else:
            names = ", ".join(a.spec.name for a, _ in done)
            return (label, True,
                    f"Asks the integrator to ship {names}: one pull request, "
                    f"reviewed and tested, then merged.")
        return (label, False, why)

    def _ship_lanes(self, ws_id: str, agent) -> None:
        """The integrator chip's Ship finished lanes: the user asking the
        integrator to ship, typed in for them. It names the lanes the user
        saw flagged, so a lane flagged after the click waits for the next
        ask. Only this click types into the integrator; a flag never does
        (_log_done_lanes)."""
        ws = self.manager.workspace(ws_id)
        if ws is None or agent is None or not self.manager.is_integrator(agent):
            return
        _label, ok, why = self._ship_offer(ws, agent)
        if not ok:
            self._lane_message("Nothing shipped", why)
            return
        done = self._finished_lanes(ws, agent)
        if not self._confirm_ship(agent, done):
            return
        branches = ", ".join(view.branch for _a, view in done)
        text = (f"Ship the finished lanes now: {branches}. Merge each lane's "
                f"newest \"Task done\" commit into one integrate/ branch and "
                f"follow your integrator instructions through to the merge.")
        if not agent.nudge(text):
            self._lane_message("Nothing shipped",
                               f"{agent.spec.name} could not take the "
                               f"request. Try again once it is at its prompt.")
            return
        self._store_audit(f"LANE-SHIP agent={agent.spec.name!r} "
                          f"lanes={branches}")

    def _confirm_ship(self, agent, done: list) -> bool:
        """The user's go-ahead for Ship finished lanes (tests replace it)."""
        lines = "\n".join(f"  {a.spec.name}: {view.branch} at "
                          f"{view.done[:7]}" for a, view in done)
        answer = QMessageBox.question(
            self, "Ship finished lanes",
            f"{agent.spec.name} combines these lanes into one pull request, "
            f"gets it reviewed and tested, and merges it:\n\n{lines}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Ok

    def _toggle_integrator(self, ws_id: str, agent) -> None:
        """The card header's Make integrator / Stop being the integrator."""
        if self.manager.is_integrator(agent):
            self.manager.set_integrator(ws_id, "")
            self._store_audit(f"INTEGRATOR ws={ws_id} none "
                              f"(was {agent.spec.name!r})")
        else:
            why = self.manager.can_integrate(agent)
            if why:
                self._lane_message("Not the integrator", why)
                return
            self.manager.set_integrator(ws_id, agent.id)
            self._store_audit(f"INTEGRATOR ws={ws_id} "
                              f"agent={agent.spec.name!r}")
            if self.event_hub is not None:
                self.event_hub.lane_event(
                    agent, "is now this workspace's integrator",
                    {"integrator": agent.spec.uid})
        page = self._pages.get(ws_id)
        for card in (page.cards if page is not None else []):
            card.refresh_lane()

    def _log_done_lanes(self, ws_id: str, views: dict) -> None:
        """Log each lane commit that says "Task done", once per commit. Past
        the chip's check mark, that is all AI Hive does with the flag. The
        user decides when there is enough to ship and asks the integrator,
        in their own words or with the chip's Ship finished lanes. A flag
        never types into the integrator: told about every flag as it
        landed, the integrator shipped work before the user asked."""
        ws = self.manager.workspace(ws_id)
        integrator = self.manager.integrator(ws_id)
        for agent in (ws.agents if ws is not None else []):
            view = views.get(agent.spec.uid) if agent.spec.lane else None
            # the integrator's branch carries the flags it merged in
            if view is None or not view.done or agent is integrator:
                continue
            key = (agent.spec.uid, view.done)
            if key in self._done_seen:
                continue
            self._done_seen.add(key)
            self._store_audit(f"LANE-DONE agent={agent.spec.name!r} "
                              f"branch={view.branch} sha={view.done[:7]}")
            if self.event_hub is not None:
                self.event_hub.lane_event(
                    agent, f"marked its work done at {view.done[:7]}",
                    {"branch": view.branch, "sha": view.done})

    def _refresh_idle_lanes(self, ws_id: str, views: dict) -> None:
        """Keep idle lanes fresh (spec Phase 3): a lane with nothing of its
        own (clean, no commits) whose agent is not working is fast-forwarded
        once its base moves. Only --ff-only, through lane_ops, so nothing can
        be lost; the agent hears about it on its next prompt. A lane with
        commits of its own is never touched, only told (Phase 2 notices).
        `views` come from the lane service, so they only ever hold lanes."""
        ws = self.manager.workspace(ws_id)
        for agent in (ws.agents if ws is not None else []):
            uid = agent.spec.uid
            view = views.get(uid) if agent.spec.lane else None
            if (view is None or not view.can_refresh()
                    or uid in self._auto_refreshing
                    or uid in self._lane_pending
                    or agent.is_busy() or agent.is_waiting()):
                continue
            self._auto_refreshing.add(uid)
            lane = dict(agent.spec.lane)
            self.lane_ops.submit(
                lane["repo"], lanes.refresh_lane, lane, label="auto-refresh",
                callback=lambda res, err, u=uid, n=agent.spec.name, ln=lane:
                self._on_lane_auto_refreshed(ws_id, u, n, ln, res, err))

    def _on_lane_auto_refreshed(self, ws_id, uid, name, lane, result,
                                error) -> None:
        self._auto_refreshing.discard(uid)
        where = f"agent={name!r} root={lane['root']} branch={lane['branch']}"
        if error is not None:
            # it gained work of its own since the poll: then it stays put
            if getattr(error, "code", "") not in ("dirty", "unmerged"):
                self._store_audit(f"LANE-FAIL {getattr(error, 'code', 'error')}"
                                  f" auto-refresh {where}: {error}")
            return
        if not result:
            return
        self._store_audit(f"LANE-REFRESH auto {where} to={result}")
        ws = self.manager.workspace(ws_id)
        if ws is not None and ws.board is not None:
            try:
                session_hook.append_notice(
                    lane_svc.notices_path(ws, uid), f"refresh|{time.time():.3f}",
                    f"AI Hive fast-forwarded your lane to {result}, because it "
                    f"had no work of its own and {result} moved on. Files in "
                    f"your lane may have changed since you read them; read a "
                    f"file again before you edit it.")
            except OSError:
                pass
        self.lane_service.poke()

    @staticmethod
    def _file_names(paths: list, shown: int = 3) -> str:
        """"a.py, b.py, c.py (+2 more)" for a notice. Control characters
        are dropped: a file name is not AI Hive's text."""
        names = [session_hook.printable(p) for p in paths]
        more = len(names) - shown
        return ", ".join(names[:shown]) + (f" (+{more} more)" if more > 0
                                            else "")

    def _show_lane_kept(self, name: str, lane: dict, status,
                        reason: str) -> None:
        """Non-blocking notice that a closed agent's lane was kept, because
        it holds work (or could not be checked). There is deliberately no
        delete button: unmerged work is only ever removed by the user."""
        held = status.describe() if status is not None else ""
        kept = ("The lane folder is kept." if os.path.isdir(lane["root"])
                else "Its branch is kept.")
        if (status is not None and status.merged and status.dirty
                and not status.local):
            # kept only for uncommitted files: name them, so the user can
            # judge whether they matter now that the commits are in
            n = len(status.dirty)
            base = lane.get("base") or status.base_ref or "the base branch"
            text = (f"{name}'s lane has {n} uncommitted "
                    f"file{'' if n == 1 else 's'} "
                    f"({self._file_names(status.dirty_paths())}). Its "
                    f"commits are all on {base}. {kept}")
        elif held:
            text = f"{name}'s lane has {held}. {kept}"
        else:
            text = f"{name}'s lane could not be removed ({reason}). {kept}"
        box = QMessageBox(QMessageBox.Icon.Information, "Lane kept", text,
                          parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)    # it holds file names
        box.setInformativeText(
            f"Folder: {lane['root']}\nBranch: {lane['branch']}\n\nTo keep "
            f"working on it, pick its conversation under Conversation in "
            f"the New Agent dialog.")
        keep = box.addButton("Keep", QMessageBox.ButtonRole.AcceptRole)
        open_btn = box.addButton("Open folder",
                                 QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(keep)
        box.setModal(False)
        open_btn.clicked.connect(
            lambda _=False, p=lane["root"]: fsopen.open_path(p))
        self._track_lane_box(box)

    def _lane_message(self, title: str, text: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, parent=self)
        box.setModal(False)
        self._track_lane_box(box)

    def _track_lane_box(self, box) -> None:
        self._lane_boxes.append(box)
        box.finished.connect(
            lambda _r, b=box: self._lane_boxes.remove(b)
            if b in self._lane_boxes else None)
        box.show()

    # ------------------------------------------------ updating AI Hive ---
    # `app/self_update.py` decides and `self_update_dialog.py` shows. Nothing
    # here is persisted (the state is re-derived by every check) and nothing
    # here closes the window: after an update lands, the user restarts when
    # their agents are at a good point.

    APP_UPDATE_FLASH_MS = 6000     # how long "Check failed" stays
    APP_UPDATE_QUIET_MS = 4000     # how long the "Up to date" notice stays

    def arm_self_update(self, runner=None, repo: str = "") -> None:
        """Let the version button reach GitHub. Called from `main.py` alone:
        the offscreen suite shares `create_main_window` and must never fetch
        or merge into the clone it runs from. `repo` is for tests."""
        from app import self_update
        self._self_update_runner = runner or self_update.subprocess_runner
        self._self_update_repo = repo or self_update.REPO_DIR

    def check_for_app_update(self) -> None:
        """The button beside the version: fetch main off the GUI thread, then
        show what was found. A click while the dialog is open raises it."""
        from app import self_update
        from .self_update_dialog import Job

        dialog = getattr(self, "_self_update_dialog", None)
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        runner = getattr(self, "_self_update_runner", None)
        if runner is None or getattr(self, "_app_update_job", None) is not None:
            return
        self._app_update_flash().stop()
        self.top_bar.set_app_update_state(
            "Checking...", "Asking GitHub for the latest AI Hive",
            interactive=False)
        repo = self._self_update_repo
        job = Job(lambda: self_update.check(runner, repo, __version__), self)
        job.finished.connect(self._on_app_update_checked)
        self._app_update_job = job
        job.start()

    def _app_update_flash(self) -> QTimer:
        timer = getattr(self, "_app_update_flash_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.top_bar.set_app_update_state)
            self._app_update_flash_timer = timer
        return timer

    def _on_app_update_checked(self, check) -> None:
        from app import self_update
        from app.self_update import Status

        self._app_update_job = None
        if not isinstance(check, self_update.Check):
            check = self_update.Check(
                Status.ERROR, running=__version__,
                detail=f"The check stopped unexpectedly: {check}")
        self._audit_install(self_update.audit_line(check))
        bar = self.top_bar
        if check.status is Status.UPDATE_AVAILABLE:
            bar.set_app_update_state(
                f"⬆ v{check.remote}",
                f"AI Hive v{check.remote} is available. Click to see what "
                "changed and update.", attention=True)
        elif check.status is Status.RESTART_PENDING:
            self._note_app_restart_pending(check.installed)
        elif check.status is Status.UP_TO_DATE:
            # a notice, not a control: nothing to click or hover for 4s,
            # then the glyph comes back on its own
            bar.set_app_update_state("✓ Up to date", interactive=False)
            self._app_update_flash().start(self.APP_UPDATE_QUIET_MS)
            return
        else:
            bar.set_app_update_state("Check failed", check.detail)
            self._app_update_flash().start(self.APP_UPDATE_FLASH_MS)
        self._open_self_update_dialog(check)

    def _note_app_restart_pending(self, version: str) -> None:
        self.top_bar.set_app_update_state(
            "Restart to update",
            (f"AI Hive v{version} is installed. " if version else "")
            + "Close the window and open AI Hive again to use it.",
            attention=True)

    def _open_self_update_dialog(self, check) -> None:
        from .self_update_dialog import SelfUpdateDialog

        dialog = SelfUpdateDialog(check, runner=self._self_update_runner,
                                  repo=self._self_update_repo, parent=self)
        dialog.auditRequested.connect(self._audit_install)
        dialog.applied.connect(self._on_app_update_applied)
        dialog.finished.connect(self._on_self_update_dialog_closed)
        self._self_update_dialog = dialog
        dialog.open()

    def _on_self_update_dialog_closed(self, _code=0) -> None:
        dialog = getattr(self, "_self_update_dialog", None)
        self._self_update_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    def _on_app_update_applied(self, result) -> None:
        if result.ok:
            self._note_app_restart_pending(result.version)

    # ------------------------------------------------- the Updates panel ---
    # The install-method control (`app/cli_install.py`). Everything here is
    # DERIVED and TRANSIENT: nothing is added to `session.json`, no
    # SESSION_VERSION bump, and none of it may `_touch`/`_schedule_save`. The
    # only persisted key is still `ui.auto_update`, written by the checkbox
    # inside the panel through the unchanged `_on_auto_update_toggled`.

    def _cli_install_runner(self):
        """The runner the panel acts through, or None.

        OPT-IN exactly like `start_usage_polling()` and the startup gate: the
        real subprocess runner is armed only from `main.py`, because the
        offscreen suite shares `create_main_window` and must never install
        software or rewrite the user's `~/.claude/settings.json`."""
        return getattr(self, "_install_runner", None)

    def arm_cli_install(self, runner=None) -> None:
        """Let the Updates panel actually act. Called from `main.py` alone."""
        from app import cli_update
        self._install_runner = runner or cli_update.subprocess_runner

    def claude_install_situation(self):
        """Read the machine now. Never cached: a remembered install method is
        wrong the moment the user installs something by hand."""
        from app import cli_install, providers
        return cli_install.detect(providers.resolve_claude(),
                                  runner=self._cli_install_runner())

    def refresh_install_state(self) -> None:
        """Put the detected state on the down-arrow's tooltip, and audit it."""
        from app import cli_install
        try:
            situation = self.claude_install_situation()
        except Exception:  # noqa: BLE001 - a tooltip must never break a launch
            return
        self.top_bar.note_install_state(situation.detail)
        self._audit_install(cli_install.state_line(situation))

    def open_updates_panel(self) -> None:
        from app import cli_install, cli_update
        from .update_panel import UpdatePanel

        situation = self.claude_install_situation()
        self.top_bar.note_install_state(situation.detail)
        self._audit_install(cli_install.state_line(situation))
        panel = UpdatePanel(situation, auto_update=self._auto_update,
                            runner=self._cli_install_runner(),
                            winget_exe=cli_install.winget_exe_path(),
                            last_check=cli_update.last_check_summary(
                                self._update_outcomes, self._update_installing),
                            parent=self)
        panel.autoUpdateToggled.connect(self.top_bar.set_auto_update)
        panel.autoUpdateToggled.connect(self._on_auto_update_toggled)
        panel.auditRequested.connect(self._audit_install)
        panel.migrationApplied.connect(self.rebind_claude_specs)
        panel.exec()
        self.top_bar.note_install_state(panel.situation().detail)

    def _audit_install(self, line: str) -> None:
        store = getattr(self, "store", None)
        if store is None:
            return
        try:
            store.audit(line)
        except Exception:  # noqa: BLE001 - forensics, never fatal
            pass

    def rebind_claude_specs(self) -> int:
        """Repoint every LIVE Claude spec at whatever `resolve_claude()` now
        answers, and audit how many moved.

        `providers.resolve_claude()` learning to prefer the native launcher
        fixes what that function ANSWERS; it does not touch a `spec.program`
        already baked by `build_spec`. So without this pass, every card that
        existed before a migration would go on relaunching the WinGet binary
        for the rest of the process, and the migration would genuinely appear
        to have done nothing. `AgentSpec.set_permission_mode` is the precedent
        for the rebuild.

        Three rules. It must NOT restart, stop or otherwise disturb a RUNNING
        agent (the new path applies at that agent's next launch, which is what
        the migration being lock-free bought us). It must NOT emit `dirty`:
        `program`/`args` are derived and are not persisted at all (`to_dict`
        stores `user_program`). And it is idempotent, so a second migration
        attempt is harmless."""
        from app import providers

        moved, exe = 0, ""
        for agent in self.manager.all_agents():
            spec = getattr(agent, "spec", None)
            if spec is None or spec.provider != "claude":
                continue
            program, args = providers.build_invocation(
                spec.provider, model=spec.model, effort=spec.effort,
                custom_command=spec.custom_command,
                extra_args=list(spec.user_args),
                permission_mode=spec.permission_mode)
            if not program:
                continue
            if program == spec.program and list(args) == list(spec.args):
                continue
            spec.program, spec.args = program, list(args)
            exe, moved = program, moved + 1
        if moved:
            from app import cli_install
            self._audit_install(cli_install.audit_lines(
                cli_install.Result(True, "rebind", before=str(moved),
                                   after=exe))[0])
        return moved

    def note_update_outcomes(self, outcomes, installing=()) -> None:
        """Report what the startup update gate did (called from `main.py`,
        which is the only place that runs it).

        Purely transient, like the plan-usage reading and the taskbar count:
        this must never mark the session dirty. Only the four reporting
        statuses say anything at all, so a clean gate leaves the top bar
        exactly as it was."""
        from app import cli_update
        self._update_installing = tuple(installing or ())
        self._update_outcomes = tuple(outcomes or ())
        text = cli_update.pill_text(outcomes, self._update_installing)
        self.top_bar.note_update_pending(
            text, cli_update.pill_tooltip(outcomes, self._update_installing))

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
        # reply-finished chime preference (default OFF if never saved)
        self._reply_sound_enabled = bool(ui.get("reply_sound_enabled", False))
        saved_log = ui.get("event_log")
        self._event_log_state = saved_log if isinstance(saved_log, dict) else {}
        # custom chime sounds: keep only kinds we know whose copy still exists
        saved_sounds = ui.get("custom_sounds")
        self._custom_sounds = {
            k: v for k, v in (saved_sounds.items()
                              if isinstance(saved_sounds, dict) else ())
            if k in chime.KINDS and isinstance(v, str) and os.path.isfile(v)}
        names = ui.get("custom_sound_names")
        names = names if isinstance(names, dict) else {}
        for kind in chime.KINDS:
            path = self._custom_sounds.get(kind, "")
            self.top_bar.set_custom_sound(
                kind, (str(names.get(kind) or os.path.basename(path)))
                if path else "")
        self.top_bar.set_reply_sound_enabled(self._reply_sound_enabled)
        # Which usage readouts to show (default: all of them). A dict rather
        # than a list of enabled keys, so a missing entry defaults ON per key
        # and a fourth tracker added later needs no migration. A new key inside
        # "ui" read with a default is backward compatible, so this needs no
        # SESSION_VERSION bump — same as sound_enabled before it.
        saved_trackers = ui.get("usage_trackers")
        if isinstance(saved_trackers, dict):
            self._usage_trackers = {k: bool(saved_trackers.get(k, True))
                                    for k in USAGE_TRACKER_KEYS}
        elif ui.get("usage_visible", True) is False:
            # MIGRATION off the older single boolean: this user hid the whole
            # readout, so honour that as "every tracker off". Reopening must
            # never put a bar back that they deliberately cleared.
            self._usage_trackers = {k: False for k in USAGE_TRACKER_KEYS}
        else:
            self._usage_trackers = dict(DEFAULT_USAGE_TRACKERS)
        self.top_bar.set_usage_trackers(self._usage_trackers)
        # taskbar working-count overlay (default ON: it is self-silencing, an
        # idle hive shows no badge at all, so it never nags)
        self._taskbar_badge = bool(ui.get("taskbar_badge", True))
        self.top_bar.set_taskbar_badge(self._taskbar_badge)
        # usage pills say "% left" instead of "% used" (default OFF)
        self._usage_left = bool(ui.get("usage_left", False))
        self.top_bar.set_usage_left(self._usage_left)
        # startup CLI auto-update (default OFF: it installs software, so it is
        # armed deliberately, once, exactly like the recovery switches were)
        self._auto_update = bool(ui.get("auto_update", False))
        self.top_bar.set_auto_update(self._auto_update)
        # "Lanes in every workspace" (default OFF: it ticks Own lane for
        # every new agent, so it is armed deliberately). The key keeps its
        # old name, so a user who had the old Agent lanes switch on sees no
        # change in what new agents get.
        self._agent_lanes = bool(ui.get("agent_lanes", False))
        self.top_bar.set_agent_lanes(self._agent_lanes)
        self._refresh_workspace_lane_toggles()
        self._auto_continue = bool(ui.get("auto_continue", True))
        self.top_bar.set_auto_continue(self._auto_continue)
        self._startup_recovery = bool(ui.get("startup_recovery", True))
        self.top_bar.set_startup_recovery(self._startup_recovery)
        # AI Hive owns the terminal scrollback (Claude launches with the
        # classic renderer). Already seeded in __init__ -- the settings file is
        # written before any agent is armed -- so this only mirrors it to the
        # menu; re-reading keeps the two in step if a restore lands later.
        self._terminal_scrollback = bool(ui.get("terminal_scrollback", True))
        self.top_bar.set_terminal_scrollback(self._terminal_scrollback)
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
        self._sync_ws_rail()
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
            lambda agent_id, ws_id=ws.id: self._close_agent(ws_id, agent_id))
        page.focusGained.connect(self._set_focused_card)
        page.addRequested.connect(self._on_add_terminal_clicked)  # empty slot
        page.layoutChosen.connect(self.manager.set_layout)        # persist
        page.deleteRequested.connect(self._confirm_delete_workspace)
        page.openFolderRequested.connect(self._open_workspace_folder)
        page.openRepoRequested.connect(self._open_workspace_repo)
        page.repoActivityRequested.connect(self._load_repo_activity)
        page.changePathRequested.connect(self._change_workspace_folder)
        page.activityToggled.connect(self._toggle_activity)
        page.mapRequested.connect(self._open_agent_map)
        page.scheduleRequested.connect(self._on_schedule_message)
        page.fileActivated.connect(self._reveal_file_in_tree)
        page.reorderCommitted.connect(self.manager.reorder_agents)
        page.laneActionRequested.connect(self._on_lane_action)
        page.lanesToggled.connect(self._on_workspace_lanes_toggled)
        page.integration_info = self._integration_info
        self._pages[ws.id] = page
        self._refresh_workspace_lane_toggles(ws.id)
        self.stack.addWidget(page)
        # a page added while another one is current is added HIDDEN, and Qt
        # never lays a hidden page out — its cards would keep the terminal's
        # pre-layout default width (see PageStack)
        self.stack.layout_hidden_pages()
        self.sidebar.add_row(ws.id, ws.name, os.path.basename(ws.project_path)
                             or ws.project_path)
        self.sidebar.set_stats(ws.id, self.manager.workspace_stats(ws.id))

    def _on_workspace_removed(self, ws_id: str) -> None:
        if (self._map_window is not None
                and getattr(self._map_window._workspace, "id", None) == ws_id):
            self._map_window.close()   # don't keep mapping a deleted workspace
        self._repo_activity.pop(ws_id, None)
        page = self._pages.pop(ws_id, None)
        if page is not None:
            for card in list(page.cards):
                if card is self._focused_card:
                    self._focused_card = None
                card.detach()
            self.stack.removeWidget(page)
            page.deleteLater()
        self.sidebar.remove_row(ws_id)
        # its agents went without terminalRemoved: it may have held the
        # last lane
        self._sync_lane_service()
        if not self.manager.workspaces and not self._closing:
            ws = self.manager.create_workspace("Workspace 1")
            self.manager.set_active(ws.id)

    def _on_workspace_renamed(self, ws_id: str, name: str) -> None:
        self.sidebar.set_row_name(ws_id, name)

    def _on_active_changed(self, ws_id: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            self.stack.setCurrentWidget(page)
        self.sidebar.set_active_row(ws_id)
        ws = self.manager.workspace(ws_id)
        # keep the activity panel following the active workspace
        if self.activity_panel.is_open() and ws is not None:
            self.activity_panel.set_lanes(self.lane_service.views(ws.id))
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

    def eventFilter(self, obj, event):
        if (obj is getattr(self, "_center_body", None)
                and event.type() == QEvent.Type.Resize):
            self._place_activity()
        return super().eventFilter(obj, event)

    def _place_activity(self) -> None:
        """Put the Activity panel beside the terminals, or over them.

        Inside the layout the panel adds its width to the window's minimum
        width. On a scaled 1080p screen that minimum passes the screen, so Qt
        grew the window off-screen and kept it that size after the panel
        closed. When the terminals would drop below their own minimum, the
        panel leaves the layout and floats over the right edge instead, which
        costs the window nothing."""
        panel = self.activity_panel
        if not panel.isVisible():
            return
        center = self._center_body
        fits = (center.width() - ACTIVITY_PANEL_WIDTH
                >= self.stack.minimumSizeHint().width())
        if self._activity_floating and fits:
            self._activity_floating = False
            self._center_lay.addWidget(panel)
        elif not self._activity_floating and not fits:
            self._activity_floating = True
            self._center_lay.removeWidget(panel)
        if self._activity_floating:
            w = panel.maximumWidth()
            panel.setGeometry(center.width() - w, 0, w, center.height())
            panel.raise_()

    def _toggle_activity(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        if self.activity_panel.is_open():
            self.activity_panel.conceal()
            self._activity_timer.stop()
        else:
            self.activity_panel.set_lanes(self.lane_service.views(ws.id))
            self.activity_panel.set_workspace(ws)
            self.activity_panel.reveal()
            self._place_activity()
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

    # ----------------------------------------------------------- event log ---

    def open_event_log(self) -> None:
        """Show the event log window (created on first use, then reused).
        Its links go through `_reveal_agent`, like the map's and the
        sidebar's."""
        if self.event_hub is None:
            return
        if self._event_log_window is None:
            win = EventLogWindow(self.event_hub, self,
                                 state=self._event_log_state)
            win.agentActivated.connect(self._reveal_agent)
            win.workspaceActivated.connect(self._reveal_workspace)
            win.stateChanged.connect(self._schedule_save)
            self._event_log_window = win
        self._event_log_window.show()
        self._event_log_window.raise_()
        self._event_log_window.activateWindow()

    def _reveal_workspace(self, ws_id: str) -> None:
        if self.manager.workspace(ws_id) is None:
            return
        self.manager.set_active(ws_id)
        self.raise_()
        self.activateWindow()

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
        target = card.terminal or card.input or card
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

    def _on_terminal_added(self, ws_id: str, agent) -> None:
        page = self._pages.get(ws_id)
        if page is not None and page.card_for(agent.id) is None:
            card = page.add_agent(agent)
            if agent.spec.lane:
                card.set_lane_view(self.lane_service.view(agent.spec.uid))
            if not page.isVisible():
                # the retile changed every card's width on a page Qt will not
                # lay out; without this the new card (and its siblings) keep a
                # width their children would then paint at (see PageStack)
                self.stack.layout_hidden_pages()
        if agent.spec.lane:
            # after the add returns: by then the dialog has marked a lane
            # being created as pending, so the first poll skips its folder
            QTimer.singleShot(0, self, self._sync_lane_service)

    def _on_terminal_removed(self, ws_id: str, agent_id: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            card = page.card_for(agent_id)
            if card is not None and card is self._focused_card:
                self._focused_card = None
            page.remove_agent(agent_id)
            if not page.isVisible():
                self.stack.layout_hidden_pages()   # the retile widened the rest
        # the last laned card closed: the service stops and says so
        self._sync_lane_service()

    def _on_workspace_path_changed(self, ws_id: str, path: str) -> None:
        page = self._pages.get(ws_id)
        if page is not None:
            page.set_path_text(path)
            # the old folder's GitHub activity is not this one's
            self._repo_activity.pop(ws_id, None)
            page.reset_repo_activity()
        # the new folder may or may not be in a git repository
        self._refresh_workspace_lane_toggles(ws_id)
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

    def _open_workspace_repo(self, ws_id: str) -> None:
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        if not os.path.isdir(ws.project_path):
            QMessageBox.warning(self, "AI Hive",
                                f"Folder not found:\n{ws.project_path}")
            return
        url = repo_activity.origin_url(ws.project_path)
        if url:
            if not fsopen.open_url(url):
                QMessageBox.warning(self, "AI Hive",
                                    f"Could not open repository in a browser:\n{url}")
        else:
            QMessageBox.information(
                self, "AI Hive",
                f"No GitHub origin remote found for:\n{ws.project_path}")

    def _load_repo_activity(self, ws_id: str) -> None:
        """The dropdown opened. It already shows the cached list (or the
        loading line); fetch only when there is no good copy for this folder
        or it is older than repo_activity.STALE_AFTER_S. A fresh result
        rebuilds the menu in place, even while it is open."""
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        entry = self._repo_activity.get(ws_id)
        if (entry is not None and entry["path"] == ws.project_path
                and not entry["result"]["error"]
                and time.monotonic() - entry["at"]
                < repo_activity.STALE_AFTER_S):
            return
        self._fetch_repo_activity([ws_id])

    def start_repo_activity_prefetch(self) -> None:
        """Fill every workspace's Open repo dropdown once, in the background,
        after the restored agents have had the machine for a few seconds.
        OPT-IN, called by main.py only, like start_usage_polling: the smoke
        suite must never touch the network."""
        QTimer.singleShot(repo_activity.PREFETCH_DELAY_MS, self,
                          self._prefetch_repo_activity)

    def _prefetch_repo_activity(self) -> None:
        if self._closing:
            return
        active = self.manager.active_id
        # the workspace on screen first: it's the dropdown most likely opened
        ids = sorted((w.id for w in self.manager.workspaces),
                     key=lambda ws_id: ws_id != active)
        self._fetch_repo_activity(
            [ws_id for ws_id in ids if ws_id not in self._repo_activity])

    def _fetch_repo_activity(self, ws_ids: list[str]) -> None:
        """Fetch each workspace's activity in turn on ONE worker thread, so a
        prefetch of many workspaces never opens many connections at once."""
        jobs = []
        for ws_id in ws_ids:
            ws = self.manager.workspace(ws_id)
            if ws is None or ws_id in self._repo_activity_busy:
                continue
            self._repo_activity_busy.add(ws_id)
            jobs.append((ws_id, ws.project_path))
        if not jobs:
            return
        fetch = self._repo_activity_fetch

        def run() -> None:
            for ws_id, path in jobs:
                try:
                    result = fetch(path)
                except Exception as exc:  # never leave a workspace "busy"
                    result = {"pull_requests": [], "commits": [],
                              "repo_url": "",
                              "error": f"Could not load GitHub activity: {exc}"}
                try:
                    self.repoActivityLoaded.emit(ws_id, path, result)
                except RuntimeError:   # the window is gone
                    return

        threading.Thread(target=run, name="github-repo-activity",
                         daemon=True).start()

    def _on_repo_activity_loaded(self, ws_id: str, path: str,
                                 result: dict) -> None:
        self._repo_activity_busy.discard(ws_id)
        ws = self.manager.workspace(ws_id)
        if ws is None:
            return
        if ws.project_path != path:
            # the folder changed while this was in flight: fetch the new one
            self._fetch_repo_activity([ws_id])
            return
        old = self._repo_activity.get(ws_id)
        if (result["error"] and old is not None and old["path"] == path
                and not old["result"]["error"]):
            # a failed refresh (offline, rate limited) keeps the last good
            # list, still stale, so the next click tries again
            return
        self._repo_activity[ws_id] = {"path": path, "at": time.monotonic(),
                                      "result": result}
        page = self._pages.get(ws_id)
        if page is not None:
            page.show_repo_activity(result["pull_requests"], result["commits"],
                                    result["error"], result["repo_url"])

    @staticmethod
    def _repo_remote_url(remote: str) -> str:
        """Convert a GitHub origin URL (HTTPS or SSH) to its browser URL."""
        return repo_activity.browser_url(remote)

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
        dialog = NewWorkspaceDialog(start_dir, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.result_data()
        if data["mode"] == "folder":
            path = data["path"]
            if not os.path.isdir(path):
                QMessageBox.warning(self, "AI Hive",
                                    "Choose an existing project folder.")
                return
            ws = self.manager.create_workspace(data["name"], path)
            self.manager.set_active(ws.id)
            self.sidebar.begin_rename(ws.id)
            return

        remote = data["url"]
        repo_url = self._repo_remote_url(remote)
        parts = [part for part in urlparse(repo_url).path.split("/") if part]
        name = data["name"].strip()
        parent = data["parent"].strip()
        if not repo_url or len(parts) != 2:
            QMessageBox.warning(self, "AI Hive",
                                "Enter a GitHub repository URL with an owner and repository name.")
            return
        if not name or not parent or not os.path.isdir(parent):
            QMessageBox.warning(self, "AI Hive",
                                "Enter a workspace name and choose an existing clone folder.")
            return
        folder_name = re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(" .")
        if not folder_name or folder_name in {".", ".."}:
            QMessageBox.warning(self, "AI Hive", "Enter a valid workspace name.")
            return
        destination = os.path.abspath(os.path.join(parent, folder_name))
        if os.path.exists(destination):
            QMessageBox.warning(
                self, "AI Hive",
                f"The clone destination already exists:\n{destination}")
            return

        progress = QProgressDialog(
            f"Cloning {repo_url}…", "", 0, 0, self)
        progress.setWindowTitle("Creating GitHub workspace")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        self._clone_progress = progress

        def clone() -> None:
            error = clone_default_branch(remote, destination)
            self.workspaceCloneFinished.emit(name, destination, repo_url, error)

        threading.Thread(target=clone, name="github-workspace-clone",
                         daemon=True).start()

    def _on_workspace_clone_finished(self, name: str, path: str,
                                     repo_url: str, error: str) -> None:
        if self._clone_progress is not None:
            self._clone_progress.close()
            self._clone_progress.deleteLater()
            self._clone_progress = None
        if error:
            QMessageBox.warning(self, "GitHub clone failed", error)
            return
        if not os.path.isdir(path):
            QMessageBox.warning(self, "GitHub clone failed",
                                "Git reported success, but the cloned folder was not found.")
            return
        ws = self.manager.create_workspace(name, path)
        self.manager.set_active(ws.id)

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
        free = MAX_AGENTS_PER_WORKSPACE - len(ws.agents)
        # found by a folder walk, not git: with no lane anywhere this dialog
        # must start no git process at all. It is the one lane-related read
        # that runs without a lane, and it decides whether the box appears.
        repo = lanes.find_repo_root(ws.project_path)
        dialog = AddTerminalDialog(self.manager.next_agent_name(ws.id), self,
                                   cwd=ws.project_path, busy_ids=busy_ids,
                                   max_count=free,
                                   lane_default=self.lane_default(ws),
                                   repo_root=repo,
                                   live_lane_roots=self._live_lane_roots())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        revive = dialog.revive_lane()
        # the user's tick decides; the toggles only set where it started
        new_lanes = dialog.wants_lane() and bool(repo)
        first = None
        for spec in dialog.result_specs(cwd=ws.project_path):
            plan = None
            if new_lanes:
                try:
                    plan = lanes.plan_lane(repo, ws.project_path, spec.name,
                                           spec.uid)
                except lanes.LaneError as exc:
                    self._store_audit(f"LANE-FAIL {exc.code} agent="
                                      f"{spec.name!r}: {exc}")
                if plan is not None:
                    # set BEFORE the add, so the add's own save carries it
                    spec.lane = plan.lane_dict()
                    spec.cwd = plan.cwd
            agent = self.manager.add_terminal(ws.id, spec,
                                              autostart=not spec.lane)
            if agent is None:
                QMessageBox.warning(self, "AI Hive",
                                    "This workspace is at its agent limit.")
                break
            if plan is not None:
                self._create_lane(ws.id, agent, plan)
            elif revive is not None and spec.lane:
                self._revive_lane(ws.id, agent)
            elif new_lanes:
                agent.notice("[no lane: this agent works in the workspace "
                             "folder]")
            first = first or agent
        # opening a terminal is for typing into it right away
        if first is not None:
            self._reveal_agent(ws.id, first.id)

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
        # the workspace's laned agents retire their lanes like a closed card
        retiring = [self._lane_retire_args(a) for a in ws.agents
                    if a.spec.lane and a.spec.uid not in self._lane_pending]
        self.manager.remove_workspace(ws_id)
        for lane, pids, name in retiring:
            self._retire_lane(lane, pids, name)

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
        a side effect of launch. (Name kept for the smoke-test API.)

        The one exception is a provider whose CLI was still installing when the
        user skipped the update splash: its binary is being rewritten right
        now, so starting an agent could execute a half written file. Those wait
        for the user (the top-bar pill says so) rather than being started."""
        self.settle_layout()
        for ws in self.manager.workspaces:
            page = self._pages.get(ws.id)
            for agent in ws.agents:
                if agent.autostart_on_restore and not agent.is_running():
                    if agent.spec.provider in self._update_installing:
                        agent.notice("[not started: a CLI update is still "
                                     "installing]")
                        continue
                    # this start is the LAUNCH restore, so the card's snapshot
                    # of last night's screen goes: the child paints its own
                    # within seconds, and a snapshot wrapped for the width that
                    # card had yesterday can never be reflowed to today's
                    card = page.card_for(agent.id) if page is not None else None
                    if card is not None:
                        card.drop_restored_screen()
                    agent.start()

    def settle_layout(self) -> None:
        """Give every card its FINAL width before any pty child is spawned.

        A terminal's width is not a cosmetic detail that can be corrected
        later: the child WRAPS ITS OWN TEXT to whatever the pseudo-console
        reports, and a `--resume` launch dumps the entire past conversation the
        moment it boots. Lines broken for the wrong width stay broken for it
        forever — pyte cannot re-flow them, and neither can
        `TerminalCard._reproject_on_size`, which only re-projects the raw
        stream the child already hard-wrapped. That is the half-width
        scrollback bug: scroll up in a restored conversation and the text uses
        a fraction of the card, while everything below it is full width.

        Three things conspire to make launch the worst moment for it, and this
        closes all three:

        1. `main()` calls `autostart_active_workspace()` the instant `show()`
           returns, and a window restoring MAXIMIZED still reports its
           restore-down geometry there (measured: 1249x662 after `show()`,
           1536x793 one `processEvents` later). Pumping the queue first is what
           makes the width honest.
        2. `TerminalView` debounces its resize by 120ms, so even a correctly
           sized card has not told its pty anything yet — `flush_resize`
           applies it now.
        3. A workspace the user has not opened is never laid out at all
           (`PageStack.layout_hidden_pages`), and the autostart brings back
           EVERY workspace's agents, not just the visible one's.
        4. A window whose minimum width is wider than its screen (a narrow or
           heavily scaled display: the workspace header alone needs about
           720px) is maximized to the screen first and only grows to its
           minimum on a LATER layout pass, which the single pump above does
           not reach. Measured on an 800px screen: every card spawned at 40
           columns and was widened to 46 a moment later. So the hidden pages
           are laid out and the window pumped until its size stops changing.

        Cheap and idempotent: `_apply_resize` returns early when nothing
        changed, so calling this again costs a queue pump."""
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            # let the window reach its real (possibly maximized) geometry, and
            # the visible page tile into it
            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        # Hidden pages first, then the layout pass that may grow the window to
        # its minimum (point 4), and again if it did: a never-laid-out page
        # reports a much wider minimum than its real one, so the window only
        # learns its true minimum from the pages' first layout. Bounded: a
        # pass either changes the window's size or ends the loop.
        for _ in range(5):
            before = self.size()
            self.stack.layout_hidden_pages()
            if app is None:
                break
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            if self.size() == before:
                break
        for page in self._pages.values():
            for card in page.cards:
                if card.is_pty and card.terminal is not None:
                    card.terminal.flush_resize()

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
            "reply_sound_enabled": self._reply_sound_enabled,
            "custom_sounds": dict(self._custom_sounds),
            # the ORIGINAL file names, for the tooltips (the copies are named
            # by kind and time)
            "custom_sound_names": {
                k: self.top_bar.custom_sound_name(k)
                for k in self._custom_sounds},
            "usage_trackers": dict(self._usage_trackers),
            "event_log": (self._event_log_window.state()
                          if self._event_log_window is not None
                          else dict(self._event_log_state)),
            # Legacy mirror, DERIVED, kept for one release so a downgrade to a
            # build that only understands this key does not resurrect three
            # pills the user closed. Restore prefers usage_trackers, so the two
            # can never fight on the way back in.
            "usage_visible": any(self._usage_trackers.values()),
            "taskbar_badge": self._taskbar_badge,
            "usage_left": self._usage_left,
            "auto_update": self._auto_update,
            "agent_lanes": self._agent_lanes,
            "auto_continue": self._auto_continue,
            "startup_recovery": self._startup_recovery,
            "terminal_scrollback": self._terminal_scrollback,
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
        # every agent is about to be stopped, and none of that is news
        if self.event_hub is not None:
            self.event_hub.shutdown()
        self.lane_service.shutdown()
        # take the badge off the button before the window goes: a stale "3
        # working" left on a taskbar icon during the seconds Windows keeps the
        # button alive says the opposite of the truth
        try:
            from .. import taskbar_overlay
            taskbar_overlay.shutdown(int(self.winId()))
        except Exception:
            pass
        self._taskbar_timer.stop()
        self._save_timer.stop()
        self._heartbeat_timer.stop()
        self._session_sync_timer.stop()
        self._prompt_sync_timer.stop()
        self._model_sync_timer.stop()
        self._bg_shell_timer.stop()
        self._limit_watch_timer.stop()
        self._usage_tick_timer.stop()
        for poller in self._usage_pollers.values():
            poller.stop()
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
