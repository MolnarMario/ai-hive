"""TerminalView: a VT terminal screen widget backed by pyte.

Renders the emulated screen grid (colors, bold/underline/reverse, cursor)
and translates keyboard input into VT sequences. The PTY output stream is
fed in via feed(); keystrokes come out of the keyInput signal; size changes
come out of sizeChanged so the worker can setwinsize the pseudo-console.

Scrolling has TWO regimes, decided by what the running app asked for (tracked
from DECSET private modes in the stream, which pyte itself ignores):

1. Fullscreen apps (Claude Code v2, vim, less) enter the ALTERNATE SCREEN
   (?1049h) — there is no terminal scrollback there by design; the app owns
   scrolling. If it enabled mouse tracking (?1000/1002/1003, SGR ?1006 — Claude
   Code enables all of these) the wheel is forwarded as mouse reports and the
   app scrolls its own transcript; otherwise the wheel falls back to arrow
   keys ("alternate scroll", as Windows Terminal does).
2. Normal-buffer output (PowerShell, cmd) scrolls a VIEW OFFSET over pyte's
   history — never pyte's own prev_page/next_page paging, because
   HistoryScreen snaps to the bottom on ANY subsequent screen event and
   constantly-repainting apps would yank the view back instantly. The offset
   stays anchored to content while new output streams in; any keystroke snaps
   back live. Shift+PageUp/PageDown page; Ctrl+wheel zooms the font.

Editing shortcuts follow Windows conventions rather than pure terminal
semantics: Ctrl+C copies the selection (falling back to the 0x03 interrupt
when nothing is selected), Ctrl+V pastes (a clipboard IMAGE is spilled to a
temp PNG and its path pasted, since Claude Code reads images by path and a
native-Windows child can't take a raw clipboard image), Ctrl+Shift+A selects
all painted text. Ctrl+A highlights the text you're typing (best-effort: the
contiguous block of non-blank rows around the cursor, so wrapped/multi-line
input highlights in full, like Cursor / the Claude desktop input box) and
Backspace/Del on that highlight clears the child's ENTIRE input via
double-Escape (0x1b 0x1b, Claude Code's clear-prompt gesture, which unlike its
line-local Ctrl+A/Ctrl+K empties multi-line input too). Typing a printable
character over the highlight REPLACES the input the same way: the prompt is
cleared first, then the character is sent as the fresh input (navigation keys
just drop the highlight, as an editor collapses a selection). Ctrl+C over the
highlight COPIES it (Ctrl+X copies then clears) -- the highlight is a real
selection, so it must reach the clipboard rather than fall through to the 0x03
interrupt, which would clear the child's input instead. The highlight is
anchored at Claude's '>' prompt row so it never climbs into the transcript;
lacking the child's real buffer it's still inference from painted rows, so it
falls back to the cursor row when no prompt glyph is found. (Home still jumps
to line start via 0x1b[H.)
Ctrl+Z/Ctrl+Y (and Ctrl+Shift+Z) are an APPROXIMATE local undo/redo. A terminal
keeps no local edit buffer, so this can't be a real per-keystroke history; it is
a coarse "restore previous input" built from snapshots of the INFERRED input
text (the painted input-box rows, debounced into one step per typing burst).
Undo clears the prompt (double-Escape) and re-pastes the prior snapshot. Its
limits are inherent: granularity is the whole prompt, not a character; a very
long horizontally-scrolled input truncates in the snapshot; restore leaves the
caret at the end; and a submit (bare Enter) forgets the history so it never
bleeds a sent message into a fresh prompt. When the undo/redo stack is empty,
Ctrl+Z/Ctrl+Y fall through to their old control bytes (0x1a/0x19) so nothing is
lost for the child. (See _undo/_redo; the user chose this over a local composer,
which would be exact but would bypass Claude's own /slash, @-mention, and history
UI.)

Mouse: when the child app requested mouse tracking (?1000/1002/1003 -- Claude
Code enables all of these for its WHOLE session, not just while a menu is up),
a press is DEFERRED and the CLICK-vs-DRAG split decides what it means, because
the DECSET modes alone can't tell a menu from a scrollback of prose. A plain
left CLICK (press and release on the same cell, no motion) is FORWARDED to the
child as a mouse report (SGR 1006 if negotiated, else legacy X10), so its
clickable menus (AskUserQuestion, plan approval, permission prompts) respond to
the pointer just as in a real terminal -- essential, because swallowing the
click for local caret-repositioning injected stray arrow keys into those modal
menus and made the question VANISH with no way to answer it. A DRAG (the pointer
leaves the press cell) is instead a local text SELECTION and is never forwarded,
so you can drag-select and copy the transcript even while the child holds mouse
tracking on. (Forwarding on press, as the first fix did, made every drag a mouse
report to the child, so a plain drag never selected anything -- only Shift+drag
did.) Shift stays the xterm escape hatch -- Shift+click/drag selects text
locally regardless. A double-click is a selection gesture too (word-select),
local even under mouse tracking. When mouse tracking is OFF (a normal
shell prompt), a plain left-click places the input caret where you clicked, by sending
the child arrow keys (a terminal can't set the child's cursor directly): exact
on the caret's own line (Left/Right by the column delta), and best-effort on
another line of a multi-line prompt (Up/Down to the row, then Left/Right to the
predicted landing column -- see _reposition_cursor). Only rows inside the input
box are touched, so clicking the transcript never nudges the prompt. A drag
selects text instead and never moves the caret. Double-click selects the whitespace-delimited word
under the pointer (then Ctrl+C copies it); Ctrl+click (or middle/scroll-wheel
click) opens a URL or an existing local file path under the pointer via the OS
default handler (_link_at/_classify_link/_open_target). File paths may be
ABSOLUTE, or RELATIVE to the agent's working directory when that base dir has
been supplied via set_base_dir() -- so a repo-relative path Claude prints
(app/widgets/sidebar.py) is clickable too; opening a file also emits
fileActivated(abspath) so the app can reveal it in the sidebar file tree.
Ctrl+LEFT-click is primary -- the left button always delivers, while the middle
button is often eaten by the OS (autoscroll). EVERY such link on the visible
screen is underlined (a soft accent line) so URLs/paths stand out in the body
text without hovering; hovering one emphasizes it (solid line) and switches to a
hand cursor so it reads as the one you'd open. The full-screen scan
(_rescan_links) can stat the filesystem, so it runs only when the visible
content changes (guarded by a per-row-text signature in paintEvent), never per
repaint; hover then reads the cached spans (_span_at) with no filesystem work.
A selection can also be built from the KEYBOARD, like a desktop text area:
Shift+Arrow/Home/End grows a local copy selection (Ctrl adds word granularity),
sending NOTHING to the child -- it's a visual overlay that feeds copy/cut, so it
works regardless of wrapping (_extend_kbd_selection). A plain (unshifted) arrow
collapses it, then forwards to move the child's caret. Shift+click EXTENDS the
current selection to the clicked cell; a TRIPLE-click selects the whole line.
A selection (mouse OR keyboard) is EDITABLE like any editor selection: Ctrl+C
copies it, Ctrl+X cuts it, and Backspace/Del deletes it. Deletion/cut drive the
child's caret to the selection end + Backspace over it, so they act on any
selection INSIDE the live input box -- single OR multi-row/wrapped (the box maps
to the child's logical line, one Backspace per row transition; the leading '> '
prompt is never counted). Fail-closed: a selection that escapes the input box
(output/scrollback) or a scrolled-back view drops the selection and touches
nothing -- inference from painted rows must never corrupt what it can't be sure
of. To empty the whole prompt at once, use Ctrl+A then Backspace (the
clear-prompt gesture above).
"""

import collections
import re
import time

import pyte
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication,
                           QPainter)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from .. import ui_theme
from ..terminal_agent import _CLAUDE_READY_HINTS, _NUM_OPTION_RE
from ..ui_theme import ANSI_16, Palette

_NAMED = {
    "black": ANSI_16[0], "red": ANSI_16[1], "green": ANSI_16[2],
    "brown": ANSI_16[3], "yellow": ANSI_16[3], "blue": ANSI_16[4],
    "magenta": ANSI_16[5], "cyan": ANSI_16[6], "white": ANSI_16[7],
    "brightblack": ANSI_16[8], "brightred": ANSI_16[9],
    "brightgreen": ANSI_16[10], "brightbrown": ANSI_16[11],
    "brightyellow": ANSI_16[11], "brightblue": ANSI_16[12],
    "brightmagenta": ANSI_16[13], "brightcyan": ANSI_16[14],
    "brightwhite": ANSI_16[15],
}

# First glyph of an input-box line, used by Ctrl+A to find where the prompt
# you're typing into begins (so the highlight stops there, not up in the
# transcript). Claude Code draws '>'; '❯' covers common shell/other prompts.
_INPUT_PROMPTS = (">", "❯")

# The Unicode Box Drawing block. Claude Code paints a horizontal rule (plain
# dashes, or a rounded-corner box border) directly between the input box and
# its footer hint, with no blank line either side -- a row built ENTIRELY
# from these glyphs is that rule, never typed content, since none of them
# show up in ordinary prose.
_RULE_CHARS = frozenset(chr(c) for c in range(0x2500, 0x2580))

# Rows of blank padding allowed below the input box's footer before
# _check_input_gap treats it as a stranded layout rather than ordinary
# spacing. See _check_input_gap for why this exists at all.
_INPUT_GAP_TOLERANCE = 1

_KEY_SEQUENCES = {
    Qt.Key.Key_Return: "\r", Qt.Key.Key_Enter: "\r",
    Qt.Key.Key_Backspace: "\x7f", Qt.Key.Key_Tab: "\t",
    Qt.Key.Key_Backtab: "\x1b[Z",  # Shift+Tab — Claude Code cycles modes
    Qt.Key.Key_Escape: "\x1b",
    Qt.Key.Key_Up: "\x1b[A", Qt.Key.Key_Down: "\x1b[B",
    Qt.Key.Key_Right: "\x1b[C", Qt.Key.Key_Left: "\x1b[D",
    Qt.Key.Key_Home: "\x1b[H", Qt.Key.Key_End: "\x1b[F",
    Qt.Key.Key_PageUp: "\x1b[5~", Qt.Key.Key_PageDown: "\x1b[6~",
    Qt.Key.Key_Delete: "\x1b[3~", Qt.Key.Key_Insert: "\x1b[2~",
    Qt.Key.Key_F1: "\x1bOP", Qt.Key.Key_F2: "\x1bOQ",
    Qt.Key.Key_F3: "\x1bOR", Qt.Key.Key_F4: "\x1bOS",
    Qt.Key.Key_F5: "\x1b[15~", Qt.Key.Key_F6: "\x1b[17~",
    Qt.Key.Key_F7: "\x1b[18~", Qt.Key.Key_F8: "\x1b[19~",
    Qt.Key.Key_F9: "\x1b[20~", Qt.Key.Key_F10: "\x1b[21~",
    Qt.Key.Key_F11: "\x1b[23~", Qt.Key.Key_F12: "\x1b[24~",
}

# Arrows with modifiers use the CSI-1;<mod><final> form (xterm).
_MODIFIED_ARROWS = {
    Qt.Key.Key_Up: "A", Qt.Key.Key_Down: "B",
    Qt.Key.Key_Right: "C", Qt.Key.Key_Left: "D",
    Qt.Key.Key_Home: "H", Qt.Key.Key_End: "F",
}

# Caret-movement keys. With Shift they drive a LOCAL copy selection instead of
# reaching the child (keyboard text-selection, like a desktop text area); a
# plain (unshifted) one collapses any such selection first, then forwards.
_NAV_KEYS = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up,
             Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End)

# Scrollback depth, and therefore how far back the scrollbar can reach and how
# long a prompt milestone stays jumpable. Only reachable at all once AI Hive
# owns the scrollback (Claude's classic renderer -- see MainWindow's tui
# preference); under the alt-screen renderer history never fills.
# MEASURED on a live classic-renderer session: ~12 lines pushed per turn with
# NO frame churn (every repeated frame seen exactly once), so 2000 lines is
# roughly 160 turns. That is the trade: raising it buys reach into older
# conversation at a real memory cost (pyte keeps each line as a sparse dict of
# Char namedtuples, and a hive routinely runs six agents), and lowering it
# silently drops the oldest milestones. Do NOT try to stretch it by filtering
# duplicate lines out of the history push: every coordinate here is
# `pushed - offset + row`, which holds only while EVERY line leaving the screen
# is counted, so a suppressed append drifts every live-anchored mark by one.
HISTORY_LINES = 2000
CELL_PAD_X = 6   # left/right inner padding (real terminals aren't flush)
CELL_PAD_Y = 4   # top/bottom inner padding
VIEW_NOTIFY_MS = 50   # coalescing window for viewChanged
# how far above the caret to look for the submitted input when the caret
# itself is parked on a blank row (see TerminalView._submitted_input)
_INPUT_SCAN_ROWS = 6
# the caret must be this far down the screen for that fallback to apply at all:
# Claude's classic renderer keeps its input box at the bottom, so a caret up in
# the conversation means the user is not typing a prompt
_INPUT_ZONE_ROWS = 12
_WIDE_GAP_RE = re.compile(r"\s{8,}")


class _CountingDeque(collections.deque):
    """A deque that remembers how many items were ever appended. pyte pushes
    each scrolled-off line to history.top via append; once the deque saturates
    (append-then-evict), len() stops growing, so a scrolled-back view would
    drift because feed() can't see that lines still scrolled by. `pushed` keeps
    counting past saturation, giving feed() the true growth to anchor against.
    (AI Hive never triggers pyte paging — scrollback is a view offset — so only
    `append` matters; appendleft/pop from prev_page never occur.)"""

    def __init__(self, iterable=(), maxlen=None):
        super().__init__(iterable, maxlen=maxlen)
        self.pushed = 0

    def append(self, x):
        self.pushed += 1
        super().append(x)


class _FastHistoryScreen(pyte.HistoryScreen):
    """HistoryScreen with pyte's per-event wrapper removed.

    pyte routes EVERY attribute access on a HistoryScreen through a Python-level
    `__getattribute__` that re-wraps each stream event in before_event/
    after_event. Those two hooks exist solely to serve prev_page/next_page: the
    "before" hook pages back to the bottom of the history buffer, and the
    "after" hook re-clips line widths. AI Hive NEVER pages -- scrollback is a
    view offset (see the module docstring), so `history.position` never leaves
    `history.size` and both hooks are no-ops on every call.

    They are not free no-ops. MEASURED, replaying 1.7 MiB of real captured
    agent output: 4.2 MILLION __getattribute__ calls, 43% of all feed time, and
    removing the wrapper renders byte-identical screen, history AND cursor state
    2.87x faster. The cost only became visible when AI Hive took the scrollback
    back: pyte's scroll path (Screen.index at the bottom margin) shuffles every
    row of the buffer one dict entry at a time, so it touches ~2*rows attributes
    per scrolled line -- and under Claude's old alt-screen renderer that path was
    never entered at all (the spike measured `pushed == 0` for 4 of 5 sessions,
    i.e. nothing ever scrolled). The classic renderer scrolls thousands of times
    per session, which multiplied a per-attribute tax nobody had ever paid.

    after_event ALSO maintained `cursor.hidden`, but only as
    `position == size and DECTCEM in mode` -- and the first term is always true
    here, leaving exactly what the base Screen's own set_mode/reset_mode already
    does. Verified against a capture exercising DECTCEM 3982 times.

    paging is therefore made LOUD rather than silently wrong: without the hook a
    prev_page would leave the screen paged away with nothing to snap it back."""

    __getattribute__ = object.__getattribute__

    def prev_page(self) -> None:
        raise NotImplementedError(
            "AI Hive scrolls by view offset, not pyte paging; "
            "_FastHistoryScreen removes the hook that would snap the page back")

    def next_page(self) -> None:
        raise NotImplementedError(
            "AI Hive scrolls by view offset, not pyte paging; "
            "_FastHistoryScreen removes the hook that would snap the page back")


def _new_history_screen(cols: int, rows: int):
    """A pyte HistoryScreen whose history.top counts total pushes, so feed()
    can anchor a scrolled-back view even after history saturates. Best-effort:
    on any pyte-shape mismatch, return a plain HistoryScreen and let feed() fall
    back to the length delta."""
    screen = _FastHistoryScreen(cols, rows, history=HISTORY_LINES, ratio=0.25)
    try:
        top = screen.history.top
        screen.history = screen.history._replace(
            top=_CountingDeque(top, maxlen=top.maxlen))
    except Exception:
        pass
    return screen

# legibility guarantee: a child process (Claude Code, etc.) emits fg colors
# tuned for a DARK terminal. On a light-background theme those land near-
# invisible, and we can't remap 256-/true-color values the way we remap the 16
# ANSI slots. So at paint time any glyph whose contrast against its actual
# background falls below LEGIBLE_FLOOR is darkened/lightened (hue preserved)
# up to LEGIBLE_TARGET — genuinely unreadable text is rescued while text that
# is merely dim (a deliberate hierarchy) is left alone. Works for every theme.
LEGIBLE_FLOOR = 3.0    # leave text alone at/above this contrast ratio
LEGIBLE_TARGET = 4.5   # rescue below-floor text up to this (WCAG AA)
_LEGIBLE_CACHE: dict = {}


def _rel_lum(c: QColor) -> float:
    def lin(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c.red()) + 0.7152 * lin(c.green()) + 0.0722 * lin(c.blue())


def contrast_ratio(a: QColor, b: QColor) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def legible_color(fg: QColor, bg: QColor) -> QColor:
    """fg unchanged if already readable on bg; otherwise nudged toward the
    high-contrast end (hue kept) until it clears LEGIBLE_TARGET."""
    if contrast_ratio(fg, bg) >= LEGIBLE_FLOOR:
        return fg
    key = (fg.rgb(), bg.rgb())
    hit = _LEGIBLE_CACHE.get(key)
    if hit is not None:
        return hit
    bg_light = _rel_lum(bg) >= 0.5
    h, s, lightness, a = fg.getHslF()
    if h < 0:            # achromatic (grey) — hue is undefined
        h = 0.0
    step = -0.05 if bg_light else 0.05
    out = QColor(0, 0, 0) if bg_light else QColor(255, 255, 255)
    for _ in range(20):
        lightness = min(1.0, max(0.0, lightness + step))
        cand = QColor.fromHslF(h, max(0.0, s), lightness, a)
        if contrast_ratio(cand, bg) >= LEGIBLE_TARGET:
            out = cand
            break
        if lightness in (0.0, 1.0):
            break
    if len(_LEGIBLE_CACHE) > 4096:
        _LEGIBLE_CACHE.clear()
    _LEGIBLE_CACHE[key] = out
    return out

# CSI sequences with a <, >, or = private marker (keyboard-protocol / xterm
# modifyOtherKeys negotiation) that pyte mis-parses as SGR — stripped.
_PRIVATE_CSI_RE = re.compile(r"\x1b\[[<>=][0-9;:]*[@-~]")
# A trailing, not-yet-complete escape held until the next chunk completes it.
_TRAILING_PARTIAL_RE = re.compile(
    r"\x1b(?:\[[0-9;:<>=?]*|\][^\x07\x1b]*)?\Z")
# Upper bound on a carried partial escape. A real escape (even a long OSC title)
# is well under this; a "partial" past it is an unterminated/binary run (e.g. an
# OSC with no BEL/ST while a child dumps binary) — carrying it would swallow the
# whole stream into _esc_carry forever and freeze the screen. Mirrors
# ansi_parser.MAX_CARRY.
_MAX_ESC_CARRY = 4096
# DECSET/DECRST private modes (CSI ? Pm h/l). pyte ignores these, but they
# decide how the wheel must behave (altscreen / mouse tracking / paste mode).
_PRIVATE_MODE_RE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")

# Block Elements (U+2580-U+259F) are drawn GEOMETRICALLY rather than handed to
# the font, the way every real terminal (Windows Terminal, kitty, alacritty)
# does it. Three reasons, all observed live on Claude Code's welcome mascot —
# which is built from full blocks plus QUADRANTS (U+2598/259B/259C/259D):
#   1. Consolas has no quadrant glyphs at all. Qt silently falls back to
#      Segoe UI Symbol, whose advance is 12px where the cell is 7 — and since
#      paintEvent batches a run of cells into ONE drawText, that one glyph
#      shoves the whole rest of the row ~5px right. The mascot's rows sheared
#      against each other.
#   2. The fallback glyph is also the wrong SHAPE for the cell (12.3x12.5 ink
#      in a 7x15 box, different baseline), so a "quarter" never meets the
#      neighbouring full block's edge — corners came out notched.
#   3. Even where Consolas DOES have the glyph, its full-block ink is 6.7px
#      wide inside a 7.0px advance, leaving a ~0.3px unpainted hairline at
#      every cell boundary that antialiased into visible stripes through
#      solid artwork.
# Filling exact grid rects fixes all three and is font-independent, so it
# survives a theme font change. Values are fractions of the cell (x0,y0,x1,y1)
# with the origin top-left; see the Unicode chart for the eighths.
_BLOCK_RECTS = {
    "▀": ((0, 0, 1, 1 / 2),),            # upper half
    "▁": ((0, 7 / 8, 1, 1),),            # lower one eighth
    "▂": ((0, 3 / 4, 1, 1),),            # lower one quarter
    "▃": ((0, 5 / 8, 1, 1),),            # lower three eighths
    "▄": ((0, 1 / 2, 1, 1),),            # lower half
    "▅": ((0, 3 / 8, 1, 1),),            # lower five eighths
    "▆": ((0, 1 / 4, 1, 1),),            # lower three quarters
    "▇": ((0, 1 / 8, 1, 1),),            # lower seven eighths
    "█": ((0, 0, 1, 1),),                # full block
    "▉": ((0, 0, 7 / 8, 1),),            # left seven eighths
    "▊": ((0, 0, 3 / 4, 1),),            # left three quarters
    "▋": ((0, 0, 5 / 8, 1),),            # left five eighths
    "▌": ((0, 0, 1 / 2, 1),),            # left half
    "▍": ((0, 0, 3 / 8, 1),),            # left three eighths
    "▎": ((0, 0, 1 / 4, 1),),            # left one quarter
    "▏": ((0, 0, 1 / 8, 1),),            # left one eighth
    "▐": ((1 / 2, 0, 1, 1),),            # right half
    "▔": ((0, 0, 1, 1 / 8),),            # upper one eighth
    "▕": ((7 / 8, 0, 1, 1),),            # right one eighth
    "▖": ((0, 1 / 2, 1 / 2, 1),),        # quadrant lower left
    "▗": ((1 / 2, 1 / 2, 1, 1),),        # quadrant lower right
    "▘": ((0, 0, 1 / 2, 1 / 2),),        # quadrant upper left
    "▙": ((0, 0, 1 / 2, 1 / 2), (0, 1 / 2, 1, 1)),      # UL+LL+LR
    "▚": ((0, 0, 1 / 2, 1 / 2), (1 / 2, 1 / 2, 1, 1)),  # UL+LR
    "▛": ((0, 0, 1, 1 / 2), (0, 1 / 2, 1 / 2, 1)),      # UL+UR+LL
    "▜": ((0, 0, 1, 1 / 2), (1 / 2, 1 / 2, 1, 1)),      # UL+UR+LR
    "▝": ((1 / 2, 0, 1, 1 / 2),),        # quadrant upper right
    "▞": ((1 / 2, 0, 1, 1 / 2), (0, 1 / 2, 1 / 2, 1)),  # UR+LL
    "▟": ((1 / 2, 0, 1, 1 / 2), (0, 1 / 2, 1, 1)),      # UR+LL+LR
}
# The three shade blocks are the same full cell at partial opacity — drawing
# them as a stipple would moire against the cell grid at small font sizes.
_BLOCK_SHADES = {"░": 0.25, "▒": 0.50, "▓": 0.75}


class TerminalView(QWidget):
    keyInput = Signal(str)        # VT byte sequence for the PTY
    sizeChanged = Signal(int, int)  # rows, cols
    fileActivated = Signal(str)   # absolute path Ctrl+clicked in the conversation
    # Ctrl+Shift+Enter: "submit this, but later". Carries the INFERRED input
    # text so the card can prefill the countdown dialog; nothing is sent to the
    # child, and nothing is cleared, until the user confirms.
    scheduleRequested = Signal(str)
    # --- scrollbar / prompt-milestone surface (all TRANSIENT, view-only) ---
    # The scroll offset, history length or page size changed, so an attached
    # TerminalScrollBar should re-read them. Coalesced (see _notify_view) and
    # guarded on a signature, because feed() runs several times a second per
    # busy agent. NEVER wire this to a save -- same rule as activity_changed.
    viewChanged = Signal()
    # history.top was wiped (ED 3 / RIS / screen.reset()), so every marker
    # anchored into it is meaningless and the bar has nothing left to show.
    historyCleared = Signal()
    # the user submitted a TYPED prompt: (inferred text, absolute line).
    # Emitted from the bare-Enter branch of keyPressEvent and nowhere else --
    # see _note_prompt_submit for why every other candidate source is wrong.
    promptSubmitted = Signal(str, int)
    # Claude's classic renderer scrolled the screen to fit a transient
    # dropdown (autocomplete/@-mention) and, on dismissal, left the input box
    # stranded above a dead run of blank rows instead of pinned to the
    # bottom -- see _check_input_gap. The card turns this into a silent
    # TerminalAgent.request_repaint() so the layout self-heals with no user
    # action.
    staleLayoutDetected = Signal()

    def __init__(self, rows: int = 30, cols: int = 100, parent=None,
                 font_px: int = 0):
        super().__init__(parent)
        self.setObjectName("TerminalView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setMouseTracking(True)  # hover (no button) to highlight links

        # base directory for resolving RELATIVE paths clicked in the output
        # (the agent's cwd == its workspace project_path); set via set_base_dir
        self._base_dir = ""
        self._esc_carry = ""  # trailing partial escape between feed() calls
        self._scroll_offset = 0  # lines scrolled back into history (0 = live)
        # footer row _check_input_gap last asked for a repaint at, so a
        # persistent (legitimately short) gap is checked once and left alone
        # rather than re-triggering a repaint on every settle
        self._input_gap_row = None
        self._sel_anchor = None  # (row, col) selection start, in screen coords
        self._sel_end = None     # (row, col) selection end
        self._input_selected = False  # Ctrl+A input-line highlight is active
        # Keyboard-driven selection (Shift+Arrow/Home/End, Ctrl for word) reuses
        # _sel_anchor/_sel_end -- purely a local copy overlay, no child bytes.
        # Triple-click (whole-line select) needs the last double-click's
        # time+cell to recognise the third press.
        self._last_dbl_ts = 0.0
        self._last_dbl_cell = None
        # Approximate whole-input undo/redo: a bounded stack of snapshots of the
        # INFERRED input text (the painted input-box rows). Coarse by nature --
        # granularity is the whole prompt, not per-keystroke; it only sees what
        # is painted; and restore re-pastes, leaving the caret at the end. See
        # _undo/_redo and the module docstring.
        self._undo_stack: list[str] = []
        self._redo_stack: list[str] = []
        self._undo_last = ""
        self._pending_fwd = None       # (row, col) of a press on a mouse-tracking
        #   app that is NOT YET forwarded: held until release so we can tell a
        #   click (forward it) from a drag (select locally). None = not pending.
        self._hover_link = None  # (row, c0, c1) of a link under the pointer
        self._hover_cell = None  # last hovered (row, col), to skip re-scans
        # every clickable URL/path on the visible screen, so they read as links
        # (underlined) even before you hover. Rescanned only when the visible
        # content changes (a cheap signature guards it) — never per repaint,
        # because classifying a token can stat the filesystem.
        self._link_spans: list[tuple[int, int, int]] = []  # (row, c0, c1)
        self._link_sig = None
        self._bracketed_paste = False  # tracked from the stream (pyte ignores 2004)
        self._alt_screen = False       # ?1049/?1047/?47 — app owns the screen
        self._mouse_tracking = False   # ?1000/?1002/?1003 — app wants mouse
        self._mouse_sgr = False        # ?1006 — SGR mouse encoding
        self._app_cursor_keys = False  # ?1 DECCKM — arrows send SS3 form
        self._focus_reporting = False  # ?1004 — app wants focus in/out events
        self._font = QFont("Consolas")
        self._font.setPixelSize(font_px or ui_theme.CONSOLE_FONT_PX)
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self._font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        metrics = QFontMetricsF(self._font)
        self._cell_w = metrics.horizontalAdvance("M")
        self._cell_h = metrics.height()
        self._ascent = metrics.ascent()
        # char -> "does this glyph advance exactly one cell?", memoized per
        # font size (see _is_grid_glyph). Cleared whenever the font changes.
        self._grid_glyph_cache: dict[str, bool] = {}

        self.screen = _new_history_screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._apply_resize)

        # debounce that coalesces a typing burst into ONE undo snapshot (see
        # _snapshot_input / _kick_snapshot)
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(600)
        self._snap_timer.timeout.connect(self._on_input_settled)

        # prompt milestones painted on the scrollbar: (absolute line, tooltip).
        # Pure VIEW data -- the durable copy lives on the agent, because a card
        # rebuild throws this widget away (see TerminalCard._replay_with_marks).
        self._marks: list[tuple[int, str]] = []
        # cached list(history.top) for _view_state, keyed on (pushed, len) --
        # without it, a user parked in scrollback copies up to HISTORY_LINES
        # items on EVERY repaint, which is precisely the state this feature
        # invites people into.
        self._vs_cache: list | None = None
        self._vs_sig = None
        self._view_sig = None    # last (pushed, hist, offset, rows) announced
        self._view_notify_timer = QTimer(self)
        self._view_notify_timer.setSingleShot(True)
        self._view_notify_timer.setInterval(VIEW_NOTIFY_MS)
        self._view_notify_timer.timeout.connect(self._emit_view_changed)

    # -------------------------------------------------------------- data ---

    def feed(self, data: str) -> None:
        # pyte mis-parses CSI sequences with a <, >, or = private-parameter
        # marker (e.g. modifyOtherKeys "ESC[>4;2m" / Kitty keyboard "ESC[>1u")
        # as ordinary SGR — "ESC[>4;2m" becomes SGR-4 = underline and turns it
        # on globally. These are keyboard-protocol negotiation, irrelevant to
        # the screen, so strip them before feeding pyte. A trailing partial
        # escape is carried to the next chunk so a split sequence still matches.
        data = self._esc_carry + data
        self._esc_carry = ""
        m = _TRAILING_PARTIAL_RE.search(data)
        if m and m.group(0) and len(m.group(0)) <= _MAX_ESC_CARRY:
            self._esc_carry = m.group(0)
            data = data[: m.start()]
        # else: an over-long "partial" is an unterminated/binary run — feed it
        # to pyte as-is rather than re-prepending it every chunk (unbounded).
        data = _PRIVATE_CSI_RE.sub("", data)
        # pyte ignores DECSET/DECRST private modes; track the ones that change
        # OUR behavior (paste wrapping, wheel routing, arrow-key encoding)
        for params, hl in _PRIVATE_MODE_RE.findall(data):
            on = hl == "h"
            for tok in params.split(";"):
                if tok == "2004":
                    self._bracketed_paste = on
                elif tok in ("1049", "1047", "47"):
                    self._alt_screen = on
                    if on:  # the app owns the screen now — leave scrollback
                        self._scroll_offset = 0
                        self._notify_view()
                elif tok in ("1000", "1002", "1003"):
                    self._mouse_tracking = on
                elif tok == "1006":
                    self._mouse_sgr = on
                elif tok == "1":
                    self._app_cursor_keys = on
                elif tok == "1004":
                    self._focus_reporting = on
        top_before = self.screen.history.top
        pushed_before = getattr(top_before, "pushed", None)
        hist_before = len(top_before)
        self.stream.feed(data)
        # while scrolled back, stay anchored to the CONTENT being read: new
        # lines entering history grow the offset so the view doesn't slide.
        # Use the true push count (not the deque length) so the anchor holds
        # even after history saturates — once full, append-then-evict pins len()
        # and the naive delta reads 0, which let the view drift a line per line.
        if self._scroll_offset:
            top_after = self.screen.history.top
            pushed_after = getattr(top_after, "pushed", None)
            if pushed_before is not None and pushed_after is not None:
                grown = pushed_after - pushed_before
            else:
                grown = len(top_after) - hist_before
            if grown > 0:
                self._scroll_offset = min(self._scroll_offset + grown,
                                          len(top_after))
        # A SHRINK can only be _reset_history (pyte's ED 3 or screen.reset()):
        # history otherwise just grows toward maxlen, evicting from the front
        # without changing len() once saturated. That is the cheapest reliable
        # "the conversation was wiped" edge available from the stream itself.
        if len(self.screen.history.top) < hist_before:
            self._on_history_wiped()
        self._notify_view()
        self.update()

    def reset(self) -> None:
        rows, cols = self.screen.lines, self.screen.columns
        self.screen = _new_history_screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self._scroll_offset = 0
        self._bracketed_paste = False
        self._alt_screen = False
        self._mouse_tracking = False
        self._mouse_sgr = False
        self._app_cursor_keys = False
        self._focus_reporting = False
        self._reset_undo()
        self._on_history_wiped()
        self._notify_view(immediate=True)
        self.update()

    # ------------------------------------------------ scrollback coordinates ---
    #
    # ONE identity underpins the scrollbar and every prompt marker:
    #
    #     abs_line(visual row r) == history.top.pushed - scroll_offset + r
    #
    # Both branches of _visible_line reduce to it. A live row r is buffer[r],
    # whose absolute id is pushed + r. A history row at
    # idx = len(hist) - off + r has absolute id pushed - len(hist) + idx, which
    # is the same expression. No special cases, and it stays true after history
    # saturates because `pushed` keeps counting past eviction.
    #
    # It also stays true across a WIPE, which is why _CountingDeque.pushed is
    # deliberately NOT reset by pyte's _reset_history (it clears the same deque
    # object). After a clear, pushed == P and len(top) == 0, so the next line
    # pushed is P and history index i is P + i: still monotonic, still correct.
    # "Fixing" that by zeroing `pushed` would silently corrupt every id.

    def history_pushed(self) -> int:
        """Total lines ever scrolled off, counting past history saturation."""
        return getattr(self.screen.history.top, "pushed", 0)

    def abs_line_at_row(self, row: int) -> int:
        """Absolute scrollback id of the line currently painted at `row`."""
        _hist, off = self._view_state()
        return self.history_pushed() - off + row

    def history_span(self) -> tuple[int, int]:
        """(oldest, newest) absolute ids still reachable. Anything outside is
        gone from history and must not be drawn as a marker."""
        pushed = self.history_pushed()
        return pushed - len(self.screen.history.top), pushed + self.screen.lines

    def anchor_line(self) -> int:
        """Absolute line of the input box the user is typing in: the '>' row
        when there is one, else the caret's own row. Used at BOTH prompt-submit
        capture and replay re-anchoring -- one function over identical screen
        state is what makes the two agree.

        NOTE the coordinate space: _input_block_span and cursor.y are LIVE
        BUFFER rows, so the id is `pushed + row` and the scroll offset does not
        enter into it. Only a VISUAL row (what the scrollbar hit-tests) needs
        abs_line_at_row's `- offset` term. The two agree at offset 0 and
        nowhere else, so mixing them up is silent by construction."""
        span = self._input_block_span()
        row = span[0] if span is not None else self.screen.cursor.y
        return self.history_pushed() + row

    def scroll_to_abs(self, abs_line: int, lead: int = 2) -> None:
        """Put `abs_line` `lead` rows below the top of the view.

        `lead` is slack, not decoration: a submitted prompt is re-printed as
        committed output a line or two from where the input box stood when it
        was captured (measured at -1 on a live classic-renderer session), so
        landing the target slightly inside the view shows it either way."""
        pushed = self.history_pushed()
        target = max(0, min(len(self.screen.history.top),
                            pushed - abs_line + lead))
        self.scroll_by(target - self._scroll_offset)

    def set_marks(self, marks) -> None:
        self._marks = list(marks)
        self._notify_view(immediate=True)

    def marks(self) -> list[tuple[int, str]]:
        return list(self._marks)

    def clear_history(self) -> None:
        """Drop the scrollback but leave the LIVE screen alone.

        For "this is a different conversation now" (a /clear or a /resume
        elsewhere): the child has already repainted its own screen, and what
        is stale is everything behind it. pyte has no API for this, but
        clearing the deque is exactly what its own _reset_history does -- and
        `pushed` deliberately survives, so absolute ids stay monotonic across
        the boundary (see the coordinate block above)."""
        try:
            self.screen.history.top.clear()
        except Exception:
            pass
        self._on_history_wiped()
        self._notify_view(immediate=True)
        self.update()

    def note_history_cleared(self) -> None:
        """Public entry for the card paths that call screen.reset() DIRECTLY
        and so never pass through feed()'s shrink check."""
        self._on_history_wiped()
        self._notify_view(immediate=True)

    def _on_history_wiped(self) -> None:
        self._scroll_offset = 0
        self._marks = []
        self._vs_cache = None
        self._vs_sig = None
        self.historyCleared.emit()

    def _notify_view(self, immediate: bool = False) -> None:
        """Announce a scroll/extent change, coalesced to one emit per 50ms.

        The signature guard skips a genuine no-op, but it cannot do the
        coalescing on its own: once AI Hive owns the scrollback, `pushed` moves
        on nearly EVERY feed, so the guard almost always passes while an agent
        streams. That is exactly when the timer must not be restarted -- a
        single-shot QTimer.start() re-arms from zero, so a burst arriving inside
        the window pushes the emit back again, and a continuously streaming
        agent (bursts every ~10-50ms) can starve it indefinitely: the scrollbar
        would freeze for the whole reply and only catch up once output paused.
        Arming only when the timer is idle turns the window into a real 50ms
        ceiling. This is the QTimer.start trap that bites the usage poll and the
        schedule tick, reached from the opposite direction."""
        sig = (self.history_pushed(), len(self.screen.history.top),
               self._scroll_offset, self.screen.lines)
        if sig == self._view_sig:
            return
        self._view_sig = sig
        if immediate:
            self._view_notify_timer.stop()
            self.viewChanged.emit()
        elif not self._view_notify_timer.isActive():
            self._view_notify_timer.start()

    def _emit_view_changed(self) -> None:
        self.viewChanged.emit()

    def screen_text(self) -> str:
        """Plain text of the live screen (used by tests).

        pyte's own `display` property can raise on a malformed buffer cell
        (observed live: a wide CJK character combined with an absolute
        cursor-column jump left a cell with empty `.data`, which crashed
        `wcwidth(char[0])` with an IndexError). That cell is reachable from a
        REPLAYED snapshot (`screen_snapshot.py`) fed straight into a fresh
        screen in `TerminalCard.__init__`, before the window is even shown --
        so an unguarded call here doesn't just blank one card, it takes down
        the whole app on startup with pythonw giving no console to see why.
        Degrade to empty text instead: the caller only uses this to decide
        compact-vs-full overlay styling, so losing it is cosmetic."""
        try:
            return "\n".join(self.screen.display)
        except Exception:
            return ""

    def _view_state(self):
        """(history_list, clamped_offset) for composite rendering, or
        (None, 0) when live. Clamping guards a history wiped underneath us
        (e.g. a full reset) while scrolled back."""
        if not self._scroll_offset:
            return None, 0
        top = self.screen.history.top
        # The copy is cached on (pushed, len): paintEvent, visible_text,
        # selected_text and every _visible_line call land here, so a user simply
        # parked in scrollback with a quiet child was copying up to
        # HISTORY_LINES entries per repaint. New output still costs one copy per
        # burst, which is what the uncached version cost per PAINT.
        sig = (getattr(top, "pushed", 0), len(top))
        if sig != self._vs_sig or self._vs_cache is None:
            self._vs_sig, self._vs_cache = sig, list(top)
        hist = self._vs_cache
        return hist, min(self._scroll_offset, len(hist))

    def _visible_line(self, r: int, hist, off: int):
        """Line shown at visual row r: history tail first, then the live
        screen shifted down by the offset."""
        if not off or hist is None:
            return self.screen.buffer[r]
        idx = len(hist) - off + r
        if idx < len(hist):
            return hist[idx]
        return self.screen.buffer[idx - len(hist)]

    def visible_text(self) -> str:
        """Plain text of what is actually painted (honors scrollback)."""
        hist, off = self._view_state()
        return "\n".join(
            "".join(self._visible_line(r, hist, off)[c].data
                    for c in range(self.screen.columns)).rstrip()
            for r in range(self.screen.lines))

    def scroll_by(self, lines: int) -> None:
        """Scroll the view: positive = back into history, negative = toward
        live; clamped to [0, history]."""
        new = max(0, min(len(self.screen.history.top),
                         self._scroll_offset + lines))
        if new != self._scroll_offset:
            self._scroll_offset = new
            self._notify_view(immediate=True)
            self.update()

    def scroll_offset(self) -> int:
        return self._scroll_offset

    def font_size(self) -> int:
        return self._font.pixelSize()

    def set_font_size(self, px: int) -> None:
        px = max(7, min(40, int(px)))
        if px == self._font.pixelSize():
            return
        self._font.setPixelSize(px)
        metrics = QFontMetricsF(self._font)
        self._cell_w = metrics.horizontalAdvance("M")
        self._cell_h = metrics.height()
        self._ascent = metrics.ascent()
        self._grid_glyph_cache.clear()  # advances are per font size
        # recompute rows/cols for the new cell size and resize the pty
        self._apply_resize()
        self.update()

    # ------------------------------------------------------------ sizing ---

    def sizeHint(self) -> QSize:
        return QSize(int(self._cell_w * 80), int(self._cell_h * 24))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_timer.start()  # debounce retile storms

    def _apply_resize(self) -> None:
        cols = max(10, int((self.width() - 2 * CELL_PAD_X) / self._cell_w))
        rows = max(2, int((self.height() - 2 * CELL_PAD_Y) / self._cell_h))
        if (rows, cols) == (self.screen.lines, self.screen.columns):
            return
        self.screen.resize(rows, cols)  # pyte order: (lines, columns)
        self.sizeChanged.emit(rows, cols)
        self._notify_view(immediate=True)   # page size changed
        self.update()

    # ------------------------------------------------------------- input ---

    def focusNextPrevChild(self, _next):
        # A terminal owns Tab and Shift+Tab (completion / Claude Code mode
        # cycling); never let Qt consume them for focus traversal.
        return False

    def focusInEvent(self, event) -> None:
        if self._focus_reporting:  # app asked for focus events (?1004)
            self.keyInput.emit("\x1b[I")
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        if self._focus_reporting:
            self.keyInput.emit("\x1b[O")
        super().focusOutEvent(event)

    def event(self, e):
        # When this terminal is focused it must win any key an app-level
        # QShortcut would otherwise swallow. Accepting ShortcutOverride tells
        # Qt to deliver the combo as a normal key press to us instead.
        # We claim plain Ctrl/Alt combos and Tab/Backtab, but deliberately
        # leave Ctrl+Shift+* to the app (its menu/window shortcuts live there
        # and shells/Claude Code don't use Ctrl+Shift+letter).
        if e.type() == QEvent.Type.ShortcutOverride:
            mods = e.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            alt = bool(mods & Qt.KeyboardModifier.AltModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            key = e.key()
            # Shift+navigation (and Ctrl+Shift+arrow for word) is our keyboard
            # text-selection gesture; Ctrl+Shift+Z is redo -- claim them so an
            # app-level QShortcut can't swallow them before keyPressEvent.
            nav = key in _NAV_KEYS
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) \
                    or (ctrl and not shift) or (alt and not shift) \
                    or (shift and nav) \
                    or (ctrl and shift and key == Qt.Key.Key_Z):
                e.accept()
                return True
        return super().event(e)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # scrollback paging, as in Windows Terminal: Shift+PageUp/PageDown
        # move the VIEW through history without sending anything to the app
        if shift and key == Qt.Key.Key_PageUp:
            self.scroll_by(max(1, self.screen.lines - 1))
            event.accept()
            return
        if shift and key == Qt.Key.Key_PageDown:
            self.scroll_by(-max(1, self.screen.lines - 1))
            event.accept()
            return

        # Ctrl+Shift+Enter is "Enter, but on a countdown": hand the card what is
        # currently typed so it can offer to send it later. NOTHING goes to the
        # child here — not the text, not a submit, not a clear. The card only
        # clears the input if the user actually schedules something, so an
        # accidental chord or a cancelled dialog leaves the prompt untouched.
        #
        # Ctrl+Enter (and Shift/Alt+Enter) are NOT available for this: they
        # insert a newline, which is how multi-line input works in Claude Code.
        # Hence the third modifier, the same move that put select-all on
        # Ctrl+Shift+A when Ctrl+A was needed for the input line.
        if ctrl and shift and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._input_selected:
                self._clear_input_selection(send=False)  # visual mark only
            self.scheduleRequested.emit(self._input_text())
            event.accept()
            return

        # Ctrl+A highlights the text you're typing (best-effort: from Claude's
        # '>' prompt row down to the cursor, so wrapped/multi-line input
        # highlights in full but the transcript above is never grabbed). It
        # sends NOTHING to the child -- it's a visual mark that pairs with
        # Backspace/Del below. A terminal can't know the child's true buffer.
        if ctrl and not shift and key == Qt.Key.Key_A:
            self._select_input_line()
            event.accept()
            return
        # With that highlight active, Backspace/Del clears the child's entire
        # input via double-Escape (Claude Code's "clear prompt" gesture; see
        # _clear_input_selection). Any other real key just dismisses the
        # highlight, then is handled normally -- as an editor drops its
        # selection on the next keypress. Bare modifiers don't dismiss (so
        # Ctrl+A then a chorded combo still works).
        if self._input_selected and key not in (
                Qt.Key.Key_Control, Qt.Key.Key_Shift,
                Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            # Ctrl+C / Ctrl+X copy the highlighted input FIRST -- the Ctrl+A mark
            # is a real selection, so copying it is what the user means. Without
            # this, Ctrl+C dropped the highlight here and fell through to the
            # 0x03 interrupt below, which CLEARS the child's input: the text was
            # deleted instead of copied (the reported bug). Ctrl+X also empties
            # the prompt (the double-Esc clear, like Backspace); Ctrl+C leaves
            # the text in place, the way any editor does.
            if ctrl and key in (Qt.Key.Key_C, Qt.Key.Key_X):
                self.copy_selection()
                self._clear_input_selection(send=(key == Qt.Key.Key_X))
                event.accept()
                return
            if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                self._clear_input_selection(send=True)
                event.accept()
                return
            # Typing a printable character over the highlight REPLACES the
            # input, like any editor: clear the child's whole prompt (the same
            # double-Escape gesture) first, then FALL THROUGH so the character
            # itself is sent and becomes the fresh input. Control/navigation
            # keys (arrows, Enter, Ctrl-combos) instead just drop the highlight,
            # the way an editor collapses a selection without deleting it.
            if event.text() and event.text().isprintable() and not ctrl and not alt:
                self._clear_input_selection(send=True)
                # fall through -> _sequence_for emits the typed character below
            else:
                self._clear_input_selection(send=False)

        # A MOUSE selection (double-click word / drag) behaves like an editor
        # selection: Backspace/Del deletes it. Deletion only reaches a selection
        # on the input line (see _delete_selection); off the input line we still
        # swallow the key and just drop the selection, so a stray Backspace can
        # never corrupt output/scrollback or nudge the caret. Ctrl+X (cut) is
        # handled with the clipboard shortcuts below.
        if (not self._input_selected and self._selection_range() is not None
                and key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
                and not ctrl and not alt):
            self._delete_selection()
            self._sel_anchor = self._sel_end = None
            self.update()
            event.accept()
            return

        # clipboard / editing shortcuts, Windows-editor style. These win over
        # the terminal control byte the same combo would otherwise send.
        # Ctrl+V / Ctrl+Shift+V paste.
        if ctrl and key == Qt.Key.Key_V:
            self.paste_clipboard()
            event.accept()
            return
        # Ctrl+C copies when there is a selection, else falls through to the
        # interrupt (0x03) so cancelling a running task still works with nothing
        # selected. Copying clears the selection (as Windows Terminal does) so
        # the *next* Ctrl+C reaches the process. Ctrl+Shift+C always copies.
        if ctrl and key == Qt.Key.Key_C:
            if shift or self._selection_range() is not None:
                self.copy_selection()
                self._sel_anchor = self._sel_end = None
                self.update()
                event.accept()
                return
            # no selection: fall through -> _sequence_for emits 0x03 (interrupt)
        # Ctrl+X cuts: copy the selection, then delete it from the input line
        # (deletion no-ops off the input line, but the copy still lands). With
        # nothing selected it falls through to the 0x18 control byte.
        if ctrl and not shift and key == Qt.Key.Key_X \
                and self._selection_range() is not None:
            self.copy_selection()
            self._delete_selection()
            self._sel_anchor = self._sel_end = None
            self.update()
            event.accept()
            return
        # Ctrl+Shift+A selects ALL painted text (screen + scrollback) for
        # copying -- distinct from Ctrl+A above, which highlights only the input
        # line. (Home still jumps to line start: it emits 0x1b[H.)
        if ctrl and shift and key == Qt.Key.Key_A:
            self.select_all()
            event.accept()
            return

        # Ctrl+Z / Ctrl+Y (and Ctrl+Shift+Z) = APPROXIMATE local undo/redo of the
        # inferred input text (see _undo/_redo + the module docstring). When the
        # stack is empty, undo/Ctrl+Y fall THROUGH so the old passthrough control
        # byte (0x1a/0x19) still reaches the child; Ctrl+Shift+Z is a new binding
        # with no legacy meaning, so it is simply swallowed when there is nothing
        # to redo.
        if ctrl and not shift and key == Qt.Key.Key_Z:
            if self._undo():
                event.accept()
                return
        elif ctrl and key == Qt.Key.Key_Y:
            if self._redo():
                event.accept()
                return
        elif ctrl and shift and key == Qt.Key.Key_Z:
            self._redo()
            event.accept()
            return

        # Shift+navigation builds/extends a LOCAL copy selection (no bytes to the
        # child), like a desktop text area; Ctrl adds word granularity. A plain
        # (unshifted) nav key collapses any such selection, then forwards below.
        if shift and not alt and key in _NAV_KEYS:
            self._extend_kbd_selection(key, word=ctrl)
            event.accept()
            return
        if (not shift and key in _NAV_KEYS and not self._input_selected
                and self._selection_range() is not None):
            self._sel_anchor = self._sel_end = None
            self.update()
            # fall through -> the key still forwards to move the child's caret

        seq = self._sequence_for(key, ctrl, shift, alt, event.text())
        if seq:
            self._snap_to_bottom()
            self.keyInput.emit(seq)
            # keep the undo snapshot cadence: a submit (bare Enter) ends this
            # input's history; other edit keys (re)arm the coalescing snapshot.
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                    and not (shift or ctrl or alt):
                self._note_prompt_submit()
                self._reset_undo()
            else:
                self._kick_snapshot(key, event.text())
            event.accept()
        else:
            super().keyPressEvent(event)

    def _sequence_for(self, key, ctrl, shift, alt, text):
        # newline vs submit: Shift/Alt/Ctrl + Enter inserts a newline the way
        # Claude Code and other REPLs expect (ESC-CR / literal LF)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if ctrl:
                return "\n"
            if shift or alt:
                return "\x1b\r"
            return "\r"

        # modified arrows/home/end -> CSI 1;<mod><final> (xterm form)
        if key in _MODIFIED_ARROWS and (ctrl or shift or alt):
            mod = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
            return f"\x1b[1;{mod}{_MODIFIED_ARROWS[key]}"

        if ctrl and key == Qt.Key.Key_Space:
            return "\x00"
        if ctrl and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key - Qt.Key.Key_A + 1)  # Ctrl+A..Z -> 0x01..0x1a
        if key in _KEY_SEQUENCES:
            return _KEY_SEQUENCES[key]
        if text:
            # Alt/Option + key sends the ESC-prefixed byte(s) (meta)
            return ("\x1b" + text) if alt else text
        return None

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+wheel zooms the font, as in every terminal/editor
            delta = 1 if event.angleDelta().y() > 0 else -1
            self.set_font_size(self._font.pixelSize() + delta)
            event.accept()
            return
        dy = event.angleDelta().y()
        if not dy:
            event.accept()
            return
        up = dy > 0
        steps = max(1, abs(dy) // 40)  # ~3 lines per wheel notch
        if self._mouse_tracking:
            # the app asked for mouse events (Claude Code does): forward the
            # wheel as mouse reports — the app scrolls its own transcript
            row, col = self._cell_at(event.position())
            self.keyInput.emit(self._wheel_report(up, row, col) * steps)
        elif self._alt_screen:
            # fullscreen app without mouse tracking (vim, less): "alternate
            # scroll" — the wheel becomes arrow keys, as Windows Terminal does
            arrow = ("\x1bOA" if self._app_cursor_keys else "\x1b[A") if up \
                else ("\x1bOB" if self._app_cursor_keys else "\x1b[B")
            self.keyInput.emit(arrow * steps)
        else:
            # normal buffer: scroll our own history view
            self.scroll_by(steps if up else -steps)
        event.accept()

    def _wheel_report(self, up: bool, row: int, col: int) -> str:
        """One wheel mouse event in the encoding the app negotiated."""
        btn = 64 if up else 65
        if self._mouse_sgr:  # SGR 1006 — modern, unambiguous
            return f"\x1b[<{btn};{col + 1};{row + 1}M"
        # legacy X10 bytes, coordinates capped at its 223-cell limit
        return ("\x1b[M" + chr(32 + btn)
                + chr(32 + min(col + 1, 222)) + chr(32 + min(row + 1, 222)))

    def _mouse_button_report(self, code: int, row: int, col: int,
                             press: bool) -> str:
        """One mouse BUTTON event in the encoding the app negotiated. `code` is
        0/1/2 for left/middle/right. SGR (1006) distinguishes press ('M') from
        release ('m') and carries the real button on both; legacy X10 encodes a
        release as button 3 with no way to say which button came up."""
        if self._mouse_sgr:  # SGR 1006 — modern, unambiguous
            return f"\x1b[<{code};{col + 1};{row + 1}{'M' if press else 'm'}"
        b = code if press else 3
        return ("\x1b[M" + chr(32 + b)
                + chr(32 + min(col + 1, 222)) + chr(32 + min(row + 1, 222)))

    def _snap_to_bottom(self) -> None:
        if self._scroll_offset:
            self._scroll_offset = 0
            self._notify_view(immediate=True)
            self.update()

    # -------------------------------------------------- mouse / clipboard ---

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int((pos.x() - CELL_PAD_X) / self._cell_w)
        row = int((pos.y() - CELL_PAD_Y) / self._cell_h)
        col = max(0, min(self.screen.columns - 1, col))
        row = max(0, min(self.screen.lines - 1, row))
        return row, col

    def mousePressEvent(self, event):
        # Ctrl+click (or a scroll-wheel/middle click) opens a URL / local file
        # under the pointer. Ctrl+LEFT-click is the primary, VS-Code-style
        # gesture: the left button always delivers, whereas the middle button
        # is often swallowed by the OS (autoscroll) and never reaches us.
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        opens_link = (event.button() == Qt.MouseButton.MiddleButton
                      or (event.button() == Qt.MouseButton.LeftButton and ctrl))
        if opens_link:
            row, col = self._cell_at(event.position())
            target = self._link_at(row, col)
            if target:
                self._open_target(target)
                if target[0] == "file":   # let the app reveal it in the sidebar
                    self.fileActivated.emit(target[1])
                event.accept()
                return
        # When the child app asked for mouse tracking (Claude Code's clickable
        # menus — AskUserQuestion, plan approval, permission prompts — do), a
        # plain left press is DEFERRED, not forwarded on the spot: we remember it
        # (_pending_fwd) and decide on RELEASE. If the pointer never moved off the
        # press cell it was a CLICK -> forward it as a mouse report so the menu
        # responds (see mouseReleaseEvent); if it dragged, that's a local text
        # SELECTION and the forward is dropped. This is what lets you drag-select
        # and copy the transcript even though Claude keeps mouse tracking on for
        # its WHOLE session (not just while a menu is up), while a genuine click
        # still reaches the menu — the click-vs-drag split is the disambiguator
        # the DECSET modes can't give us. Forwarding on press instead made every
        # plain drag a mouse report to the child, so nothing local ever selected.
        # (Shift stays the xterm escape hatch: Shift+click/drag selects locally
        # and is never forwarded, even mid-render before the anchor is set.)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if (event.button() == Qt.MouseButton.LeftButton and self._mouse_tracking
                and not ctrl and not shift):
            self._pending_fwd = self._cell_at(event.position())
            # fall through: set the selection anchor so a drag builds a real
            # selection (mouseMoveEvent needs _sel_anchor to track the drag).
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._input_selected = False  # a mouse drag is a copy selection
            cell = self._cell_at(event.position())
            if shift and self._sel_anchor is not None:
                self._sel_end = cell          # Shift+click EXTENDS the selection
            elif (self._last_dbl_cell == cell
                    and time.monotonic() - self._last_dbl_ts
                    <= QApplication.doubleClickInterval() / 1000.0):
                # TRIPLE-click (a press right after a double-click at the same
                # cell) selects the whole line
                first, last = self._row_content(cell[0])
                self._sel_anchor = (cell[0], 0)
                self._sel_end = (cell[0], last if last >= 0 else 0)
                self._last_dbl_cell = None
            else:
                self._sel_anchor = cell
                self._sel_end = cell
            self.update()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # double-click selects the word (non-whitespace run) under the pointer,
        # ready to copy (Ctrl+C) -- like any editor/terminal. This is a SELECTION
        # gesture (never a menu answer), so it word-selects locally even on a
        # mouse-tracking app, the same way a plain drag now selects; the deferred
        # single click still forwards to the menu (see mousePressEvent). Any
        # pending single-click forward from the first press is dropped here.
        self._pending_fwd = None
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            row, col = self._cell_at(event.position())
            # remember this double-click so a following press at the same cell
            # is recognised as a TRIPLE-click (whole-line select, mousePressEvent)
            self._last_dbl_ts = time.monotonic()
            self._last_dbl_cell = (row, col)
            rng = self._word_at(row, col)
            if rng:
                self._input_selected = False  # a copy selection, not the Ctrl+A mark
                self._sel_anchor = (row, rng[0])
                self._sel_end = (row, rng[1])
                self.update()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._sel_anchor:
            self._sel_end = self._cell_at(event.position())
            # moving off the press cell makes this a DRAG (a local selection),
            # so a pending mouse-tracking forward is cancelled — the child never
            # sees the click, and the drag is ours to copy.
            if self._pending_fwd is not None and self._sel_end != self._pending_fwd:
                self._pending_fwd = None
            self.update()
            super().mouseMoveEvent(event)
            return
        # hover (no drag): every link is already underlined; hovering one just
        # emphasizes it and shows the hand cursor. Look it up in the cached scan
        # (no filesystem work here), only re-checking when the cell changes.
        cell = self._cell_at(event.position())
        if cell != self._hover_cell:
            self._hover_cell = cell
            rng = self._span_at(*cell)
            hover = (cell[0], rng[0], rng[1]) if rng else None
            if hover != self._hover_link:
                self._hover_link = hover
                self.setCursor(Qt.CursorShape.PointingHandCursor if hover
                               else Qt.CursorShape.IBeamCursor)
                self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_link is not None:
            self._hover_link = None
            self.setCursor(Qt.CursorShape.IBeamCursor)
            self.update()
        self._hover_cell = None
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        # A DEFERRED mouse-tracking press that never dragged is a CLICK: forward
        # it now (press + release at the press cell) so the child's menu responds
        # under the pointer, exactly as a live report would. A drag would have
        # cleared _pending_fwd in mouseMoveEvent, leaving the local selection to
        # copy; here we drop that (empty) selection since it was really a click.
        if (self._pending_fwd is not None
                and event.button() == Qt.MouseButton.LeftButton):
            row, col = self._pending_fwd
            self._pending_fwd = None
            self._sel_anchor = self._sel_end = None
            self.keyInput.emit(self._mouse_button_report(0, row, col, True))
            self.keyInput.emit(self._mouse_button_report(0, row, col, False))
            self.update()
            event.accept()
            return
        # a plain click (no drag): place the input caret where you clicked, then
        # drop the (empty) selection. A drag leaves a real selection to copy and
        # never moves the caret. Skip on a mouse-tracking app — its clicks are
        # forwarded above, and driving the caret here would inject stray arrows.
        if self._sel_anchor is not None and self._sel_anchor == self._sel_end:
            if event.button() == Qt.MouseButton.LeftButton \
                    and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier) \
                    and not self._mouse_tracking:
                self._reposition_cursor(*self._sel_anchor)
            self._sel_anchor = self._sel_end = None
            self.update()
        super().mouseReleaseEvent(event)

    def _emit_arrows(self, final: str, count: int) -> None:
        """Send `count` copies of one arrow key (final 'A'/'B'/'C'/'D' == Up/
        Down/Right/Left), honouring the child's cursor-key mode (DECCKM)."""
        if count <= 0:
            return
        seq = ("\x1bO" if self._app_cursor_keys else "\x1b[") + final
        self.keyInput.emit(seq * count)

    def _input_block_span(self):
        """(top, bottom) live-screen rows of the contiguous input box, or None.
        `top` is the nearest '>'/'❯' prompt row at/above the caret (as
        _select_input_line finds it -- when there is no prompt glyph it stays on
        the caret's own row, so a promptless transcript above is never absorbed);
        `bottom` is the last non-blank row of the contiguous block at/below the
        caret, stopping BEFORE the box's own footer hint (Claude Code paints
        that directly under the box with no blank line, so a naive non-blank
        scan swept it -- and anything under it -- into the captured input; see
        _row_is_input_footer) or a divider/border row (_row_is_rule) -- Claude
        Code also paints a plain rule or box border between the input and its
        footer with no blank line, and a naive scan swept that in too (it
        showed up literally as a line of box-drawing dashes in a scheduled
        message's prefill). Bounds multi-line click-to-position so a click
        on the transcript or on blank space below the box never drives the
        child's caret."""
        buf = self.screen.buffer
        cy = self.screen.cursor.y
        if self._row_content(cy) == (-1, -1):
            return None
        top = cy
        r = cy
        while r >= 0:
            first, _ = self._row_content(r)
            if first < 0:
                break  # blank line: the input box doesn't extend past it
            if buf[r][first].data in _INPUT_PROMPTS:
                top = r  # the box's first line -- stop, never climb higher
                break
            if self._row_is_rule(r):
                break  # a divider row: never part of typed content
            r -= 1
        bottom = cy
        r = cy + 1
        while r < self.screen.lines:
            if self._row_content(r) == (-1, -1):
                break
            if self._row_is_input_footer(r) or self._row_is_rule(r):
                break  # footer hint or divider, not more typed text -- stop before it
            bottom = r
            r += 1
        return top, bottom

    def _reposition_cursor(self, row: int, col: int) -> None:
        """Move the CHILD's input caret to (row, col) by sending arrow keys --
        a terminal can't set the child's cursor directly. Non-destructive and
        self-clamping (readline stops at the input's start/end), so a
        mispredicted landing is at worst a few columns off, never data loss.

        On the caret's OWN row it is exact: each Left/Right == one column on an
        unwrapped line. On ANOTHER row of a multi-line prompt it is best-effort:
        Up/Down move by one visual row carrying the caret's "goal column" (its
        current column, which the editor clamps to the target line's length), so
        we PREDICT the landing column from the painted row and correct with
        Left/Right. Only rows inside the input box (_input_block_span) are
        touched, so clicking the transcript or a scrolled-back view -- or any row
        outside the box -- never nudges the prompt."""
        if self._scroll_offset:
            return  # caret only meaningful on the live screen
        cy = self.screen.cursor.y
        cx = self.screen.cursor.x
        if row == cy:
            self._emit_arrows("C" if col > cx else "D", abs(col - cx))
            return
        span = self._input_block_span()
        if span is None or not (span[0] <= row <= span[1]):
            return  # off the input box: leave the child's caret alone
        # 1. vertical: Up/Down carry the goal column (cx), clamped to the target
        #    line's end. Consecutive presses keep the same goal, so N moves land
        #    on `row` at min(cx, line-end).
        self._emit_arrows("B" if row > cy else "A", abs(row - cy))
        # 2. predict that landing, then correct horizontally on the target row.
        #    Clamp the requested column into the row's content so the Left/Right
        #    run can't spill onto an adjacent line.
        _, last = self._row_content(row)
        line_end = last + 1 if last >= 0 else 0
        landing = min(cx, line_end)
        target = max(0, min(col, line_end))
        self._emit_arrows("C" if target > landing else "D", abs(target - landing))

    def _delete_selection(self) -> bool:
        """Delete a selection by driving the child's caret to its end, then
        Backspacing over it -- a terminal can't edit a span of the child's
        buffer directly. Works for a selection anywhere INSIDE the live input
        box (single OR multi-row/wrapped): the box's rows map to the child's
        logical line, so the character count is the painted cells across the
        span plus one Backspace per row transition (each soft-wrap / explicit
        newline is one editable char). Fail-closed: a scrolled-back view or a
        selection that escapes the input box (output/scrollback) is left
        untouched and returns False -- inference from painted rows must never
        corrupt what it can't be sure of. The leading prompt run ('> ') on the
        box's first row is never counted as deletable input."""
        rng = self._selection_range()
        if rng is None or self._scroll_offset:
            return False
        (r0, c0), (r1, c1) = rng
        span = self._input_block_span()
        if span is None or not (span[0] <= r0 and r1 <= span[1]):
            return False  # selection escapes the input box
        buf = self.screen.buffer
        count = 0
        for r in range(r0, r1 + 1):
            first, last = self._row_content(r)
            if last >= 0:
                start = c0 if r == r0 else 0
                end = min(c1 if r == r1 else last, last)
                # don't count the box's leading '> ' prompt as input
                if r == span[0] and buf[r][first].data in _INPUT_PROMPTS:
                    pstart = first + 1
                    while pstart <= last and buf[r][pstart].data in ("", " "):
                        pstart += 1
                    start = max(start, pstart)
                if end >= start:
                    count += end - start + 1
            if r < r1:
                count += 1  # the wrap/newline joining this row to the next
        if count <= 0:
            return False
        self._snap_to_bottom()
        self._reposition_cursor(r1, c1 + 1)   # caret to just past the span end
        self.keyInput.emit("\x7f" * count)    # then Backspace over it
        return True

    def _extend_kbd_selection(self, key, word: bool) -> None:
        """Grow the LOCAL copy selection by one cell (or one word with Ctrl)
        from the keyboard, WITHOUT sending anything to the child -- keyboard
        text-selection, exactly like a desktop text area. Anchors at the child
        caret on first use; the moving end is _sel_end. Purely visual, so it
        works regardless of wrapping and feeds copy/cut just like the mouse
        selection."""
        self._snap_to_bottom()
        self._input_selected = False
        rows, cols = self.screen.lines, self.screen.columns
        if self._sel_anchor is None or self._sel_end is None:
            start = (self.screen.cursor.y, self.screen.cursor.x)
            self._sel_anchor = start
            self._sel_end = start
        r, c = self._sel_end
        if key == Qt.Key.Key_Left:
            c = self._word_col(r, c, -1) if word else c - 1
        elif key == Qt.Key.Key_Right:
            c = self._word_col(r, c, +1) if word else c + 1
        elif key == Qt.Key.Key_Up:
            r -= 1
        elif key == Qt.Key.Key_Down:
            r += 1
        elif key == Qt.Key.Key_Home:
            c = 0
        elif key == Qt.Key.Key_End:
            c = self._row_content(r)[1]
            if c < 0:
                c = 0
        self._sel_end = (max(0, min(rows - 1, r)), max(0, min(cols - 1, c)))
        self.update()

    def _word_col(self, r: int, c: int, direction: int) -> int:
        """Column one word away from (r, c) in `direction` (+1 right / -1 left),
        for Ctrl+Shift+Arrow selection. Same whitespace-run notion as the
        double-click word select (_word_at)."""
        hist, off = self._view_state()
        line = self._visible_line(r, hist, off)
        cols = self.screen.columns

        def blank(i):
            return i < 0 or i >= cols or line[i].data in ("", " ")

        i = c
        if direction > 0:
            while i < cols and not blank(i):   # skip the current word
                i += 1
            while i < cols and blank(i):       # then the gap -> next word start
                i += 1
            return min(i, cols - 1)
        i -= 1
        while i >= 0 and blank(i):             # skip the gap to the left
            i -= 1
        while i > 0 and not blank(i - 1):      # then to the word's start
            i -= 1
        return max(i, 0)

    def _word_at(self, row: int, col: int):
        """(c0, c1) inclusive of the non-whitespace run at (row, col), or None
        if that cell is blank. A 'word' here is whitespace-delimited, so a path
        or URL selects whole."""
        hist, off = self._view_state()
        line = self._visible_line(row, hist, off)
        cols = self.screen.columns
        if col >= cols or line[col].data in ("", " "):
            return None
        c0 = col
        while c0 > 0 and line[c0 - 1].data not in ("", " "):
            c0 -= 1
        c1 = col
        while c1 + 1 < cols and line[c1 + 1].data not in ("", " "):
            c1 += 1
        return c0, c1

    def _link_at(self, row: int, col: int):
        """('url'|'file', target) for a clickable token at (row, col), else
        None. Reuses the double-click word run as the token."""
        rng = self._word_at(row, col)
        if not rng:
            return None
        hist, off = self._view_state()
        line = self._visible_line(row, hist, off)
        token = "".join(line[c].data for c in range(rng[0], rng[1] + 1))
        return self._classify_link(token)

    @staticmethod
    def _maybe_link(token: str) -> bool:
        """Cheap pre-filter for the full-screen scan: could this whitespace-run
        possibly be a link? Keeps the scan from stat()ing the filesystem for
        every plain word — only URL-ish or path-ish tokens reach _classify_link."""
        t = token.strip("'\"()[]{}<>,;")
        if not t:
            return False
        if t.lower().startswith(("http://", "https://", "ftp://", "file://",
                                 "www.")):
            return True
        # path-ish: a separator, a home '~', or a name.ext[:line[:col]] tail
        return ("/" in t or "\\" in t or t.startswith("~")
                or bool(re.search(r"\.\w{1,8}(:\d+){0,2}$", t)))

    def _span_at(self, row: int, col: int):
        """(c0, c1) of the clickable span covering (row, col), from the cached
        scan — so hover does no filesystem work and always agrees with the
        underlines that are already painted."""
        for (r, c0, c1) in self._link_spans:
            if r == row and c0 <= col <= c1:
                return (c0, c1)
        return None

    def _rescan_links(self, lines) -> None:
        """Recompute every clickable span on the visible screen. Called only
        when the content signature changed (see paintEvent), never per repaint.
        Tokenizes each row on whitespace, pre-filters, then classifies, recording
        the raw token span so it can be underlined."""
        cols = self.screen.columns
        spans: list[tuple[int, int, int]] = []
        for row in range(min(self.screen.lines, len(lines))):
            line = lines[row]
            col = 0
            while col < cols:
                if line[col].data in ("", " "):
                    col += 1
                    continue
                c0 = col
                while col < cols and line[col].data not in ("", " "):
                    col += 1
                c1 = col - 1
                token = "".join(line[i].data for i in range(c0, c1 + 1))
                if self._maybe_link(token) and self._classify_link(token):
                    spans.append((row, c0, c1))
        self._link_spans = spans

    def _classify_link(self, token: str):
        """Classify a token as an openable URL or an existing local file.
        Trims wrapping quotes/brackets and a trailing :line[:col] ref (Claude
        prints file:line). Absolute paths are checked directly; a RELATIVE path
        is resolved against `self._base_dir` (the agent's cwd) when one is set --
        that is the common case, since Claude prints repo-relative paths like
        'app/widgets/sidebar.py'. Returns ('url'|'file', value) or None."""
        import os

        t = token.strip().strip("'\"()[]{}<>,;")
        if not t:
            return None
        if re.match(r"(?i)^(https?|ftp|file)://\S", t):
            return ("url", t)
        if re.match(r"(?i)^www\.\S+\.\S", t):
            return ("url", "http://" + t)
        path = re.sub(r":\d+(:\d+)?$", "", t)  # drop a file:line[:col] suffix
        path = os.path.expanduser(path)
        try:
            if os.path.isabs(path):
                if os.path.exists(path):
                    return ("file", os.path.abspath(path))
            elif self._base_dir:              # resolve relative to the agent cwd
                cand = os.path.join(self._base_dir, path)
                if os.path.exists(cand):
                    return ("file", os.path.abspath(cand))
        except OSError:
            return None
        return None

    def set_base_dir(self, path: str) -> None:
        """Set the directory relative paths in the output resolve against (the
        agent's working directory). Empty disables relative-path opening."""
        self._base_dir = path or ""

    def _open_target(self, target) -> None:
        """Open a classified link with the OS default handler (user-initiated
        via Ctrl/middle-click, like following a hyperlink). URLs go to the
        browser; files go through the shared fsopen helper (Qt openUrl with an
        os.startfile fallback -- Qt returns False for some file associations,
        which would otherwise make a click look dead)."""
        from .. import fsopen

        kind, value = target
        if kind == "url":
            fsopen.open_url(value)
        else:
            fsopen.open_path(value)

    def _selection_range(self):
        """Normalized ((r0,c0),(r1,c1)) with start <= end, or None."""
        if not self._sel_anchor or not self._sel_end \
                or self._sel_anchor == self._sel_end:
            return None
        a, b = self._sel_anchor, self._sel_end
        return (a, b) if a <= b else (b, a)

    def selected_text(self) -> str:
        rng = self._selection_range()
        if rng is None:
            return ""
        (r0, c0), (r1, c1) = rng
        hist, off = self._view_state()  # copy what is painted, incl. history
        lines = []
        for r in range(r0, r1 + 1):
            start = c0 if r == r0 else 0
            end = c1 if r == r1 else self.screen.columns - 1
            row = self._visible_line(r, hist, off)
            lines.append("".join(row[c].data for c in range(start, end + 1)).rstrip())
        return "\n".join(lines)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def select_all(self) -> None:
        self._input_selected = False
        self._sel_anchor = (0, 0)
        self._sel_end = (self.screen.lines - 1, self.screen.columns - 1)
        self.update()

    def _row_content(self, r: int) -> tuple[int, int]:
        """(first, last) non-blank columns on screen row r, or (-1, -1)."""
        row = self.screen.buffer[r]
        first = last = -1
        for c in range(self.screen.columns):
            if row[c].data not in ("", " "):
                first = c if first < 0 else first
                last = c
        return first, last

    def _row_is_input_footer(self, r: int) -> bool:
        """True when row r is Claude Code's input-box footer hint (e.g. '? for
        shortcuts') -- painted with NO blank line between it and the box, so
        the bottom-scan in `_input_block_span` must stop before it instead of
        folding it into the captured/selected input."""
        first, last = self._row_content(r)
        if first < 0:
            return False
        row = self.screen.buffer[r]
        text = "".join(row[c].data for c in range(first, last + 1)).strip().lower()
        return any(hint in text for hint in _CLAUDE_READY_HINTS)

    def _row_is_rule(self, r: int) -> bool:
        """True when row r is a horizontal divider (box border or plain rule)
        rather than typed content -- see `_RULE_CHARS`."""
        first, last = self._row_content(r)
        if first < 0:
            return False
        row = self.screen.buffer[r]
        return all(row[c].data in _RULE_CHARS for c in range(first, last + 1))

    def _select_input_line(self) -> None:
        """Best-effort highlight of the text you're typing. Claude Code's input
        box opens with a prompt glyph ('>'), so we anchor the TOP of the
        selection at the nearest prompt row at/above the cursor and run down to
        the cursor row -- a wrapped/multi-line prompt highlights in full WITHOUT
        climbing into the transcript above it (the over-reach that happened when
        output butts straight against the box). If no prompt glyph is found
        before a blank line or the top of the screen, we fall back to the
        cursor's row only: better to under-reach than to grab output. A terminal
        can't see the child's real buffer, so this is inference from painted
        rows. Purely a view mark; Backspace/Del acts on it via
        _clear_input_selection. A blank cursor row selects nothing."""
        self._snap_to_bottom()  # cursor.y is a live-screen row; align the view
        buf = self.screen.buffer
        cy = self.screen.cursor.y
        if self._row_content(cy) == (-1, -1):
            self._clear_input_selection(send=False)
            return
        top = cy  # conservative default if we never find a prompt row
        r = cy
        while r >= 0:
            first, _ = self._row_content(r)
            if first < 0:
                break  # blank line: the input box doesn't extend past it
            if buf[r][first].data in _INPUT_PROMPTS:
                top = r  # the input box's first line -- stop, never go higher
                break
            r -= 1
        self._sel_anchor = (top, 0)
        self._sel_end = (cy, self._row_content(cy)[1])
        self._input_selected = True
        self.update()

    def _clear_input_selection(self, send: bool) -> None:
        """Drop the Ctrl+A input highlight. With send=True (Backspace/Del on the
        highlight) also clear the child's ENTIRE input via double-Escape
        (0x1b 0x1b) -- Claude Code's documented "clear the prompt" gesture,
        which empties single, wrapped, AND explicit multi-line input in one go
        (its Ctrl+A/Ctrl+K are only line-local, so they can't). This is tuned
        for Claude Code; a plain shell clears its line differently."""
        had = self._input_selected
        self._input_selected = False
        self._sel_anchor = self._sel_end = None
        if send and had:
            self._snap_to_bottom()
            self.keyInput.emit("\x1b\x1b")
        self.update()

    # ------------------------------------------------- approximate undo ---
    # A terminal keeps no local edit buffer, so this can't be a real per-
    # keystroke undo. It is a COARSE, best-effort "restore previous input"
    # built from snapshots of the INFERRED input text (the painted input-box
    # rows). Limits, by construction: granularity is the whole prompt, not a
    # character; the snapshot only sees what is painted (a very long,
    # horizontally-scrolled input truncates); restore re-pastes and leaves the
    # caret at the end; and it can interact oddly with Claude's own Up-arrow
    # history. The user opted into this approximation over a local composer.

    def _input_text(self) -> str:
        """The inferred current input: the painted input-box rows joined, with
        the leading '> ' prompt stripped. Empty when the caret isn't in an
        input box."""
        span = self._input_block_span()
        if span is None:
            return ""
        top, bottom = span
        buf = self.screen.buffer
        out = []
        for r in range(top, bottom + 1):
            first, last = self._row_content(r)
            if last < 0:
                out.append("")
                continue
            start = first
            if r == top and buf[r][first].data in _INPUT_PROMPTS:
                start = first + 1
                while start <= last and buf[r][start].data in ("", " "):
                    start += 1
            out.append("".join(buf[r][c].data for c in range(start, last + 1)))
        return "\n".join(out).strip("\n")

    def _note_prompt_submit(self) -> None:
        """Record that the user just submitted a TYPED prompt, for the
        scrollbar's milestone dots.

        This is the only correct place in the app to notice that. The
        alternatives all lie:
          * agent.write() / TerminalCard._on_key_input see a bare string, and a
            bracketed PASTE carries '\\r' characters that are not submits.
          * _last_input_ts is stamped by any keystroke, not by a submit.
          * a UserPromptSubmit hook is forbidden -- it perturbs the launch and
            first-task-submit timing the SessionStart `startup` exclusion
            exists to protect.
        It also excludes what the user asked to exclude, by construction:
        deliver_task, nudge (auto-continue's "Continue") and scheduled sends
        all reach worker.write and never pass through a key event.

        The numbered-option reject is the other half of the menu guard (the
        card adds agent.is_waiting()): _INPUT_PROMPTS includes the selection
        caret Claude paints on a highlighted permission row, so _input_text()
        over a live menu returns something like "1. Yes" -- an answer, not a
        prompt, and not a milestone."""
        found = self._submitted_input()
        if found is None:
            return
        row, text = found
        if _NUM_OPTION_RE.search(text):
            return
        self.promptSubmitted.emit(text, self.history_pushed() + row)

    def _submitted_input(self):
        """(live-buffer row, text) of the input just submitted, or None.

        `_input_block_span` is the precise reading and is tried first, but it
        REQUIRES the cursor to be sitting on a row with content, and the
        classic renderer frequently parks the cursor on a blank row below the
        input box after a submit (measured on a real session: text on row 33,
        cursor on row 35, so the span came back None and no milestone was ever
        recorded). Falling back to the nearest non-blank row above the cursor
        recovers it.

        The fallback is bounded two ways so it can never mistake ordinary
        OUTPUT for a prompt: it only runs when the caret is down in the input
        box's own region at the bottom of the screen, and it STOPS at the box's
        rule/footer rather than stepping over it into the conversation. An
        empty input box therefore hits the rule and records nothing, which is
        what a bare Enter at an empty prompt should do."""
        span = self._input_block_span()
        if span is not None:
            text = self._input_text()
            if text.strip():
                return span[0], text
        buf = self.screen.buffer
        lines = self.screen.lines
        cy = max(0, min(lines - 1, self.screen.cursor.y))
        # bottom third, floored, so this behaves on a short test screen as well
        # as a full-height one
        if cy < lines - max(3, min(_INPUT_ZONE_ROWS, lines // 3)):
            return None         # the caret is up in the conversation, not typing
        for r in range(cy, max(-1, cy - _INPUT_SCAN_ROWS), -1):
            if self._row_is_input_footer(r) or self._row_is_rule(r):
                return None     # reached the edge of the box: nothing was typed
            first, last = self._row_content(r)
            if first < 0:
                continue
            text = "".join(buf[r][c].data or "" for c in range(first, last + 1))
            if buf[r][first].data in _INPUT_PROMPTS:
                text = text[1:]
            # the renderer right-aligns footer bits ("high / effort") on the
            # same row by jumping the cursor, which leaves a wide gap behind
            text = _WIDE_GAP_RE.split(text.strip(), 1)[0].strip()
            return (r, text) if len(text) >= 2 else None
        return None

    def _kick_snapshot(self, key, text: str) -> None:
        """(Re)arm the coalescing snapshot after an edit keystroke, so a typing
        burst collapses into ONE undo step."""
        if (text and text.isprintable()) or key in (
                Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._snap_timer.start()

    def _snapshot_input(self) -> None:
        """Push the PREVIOUS input onto the undo stack when the input changed --
        the coalesced unit for one undo step."""
        cur = self._input_text()
        if cur != self._undo_last:
            self._undo_stack.append(self._undo_last)
            if len(self._undo_stack) > 50:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
            self._undo_last = cur

    def _on_input_settled(self) -> None:
        """Fired once, 600ms after the last edit keystroke (see _snap_timer /
        _kick_snapshot) -- the "user stopped touching the input" moment. Does
        the existing undo bookkeeping and then checks whether the box got
        left in a stale spot (_check_input_gap): a burst of typing is exactly
        when Claude's classic renderer can scroll for a dropdown and fail to
        scroll back, and by the time this fires the child's redraw has long
        since arrived and been painted."""
        self._snapshot_input()
        self._check_input_gap()

    def _check_input_gap(self) -> None:
        """Self-heal a classic-renderer glitch: Claude's autocomplete/mention
        dropdown scrolls the whole screen to make room, and on dismissal the
        input box can be left stranded above a dead run of blank rows instead
        of pinned to the bottom of the terminal (the dropdown's rows are
        erased, but nothing re-scrolls the viewport back down). A raw VT100
        terminal fed the same bytes would show the identical gap -- this is
        upstream Claude Code CLI behaviour, not a scroll_offset bug (typing
        already calls _snap_to_bottom, so offset is 0 here) -- so the fix is
        to ask Claude to redraw its whole frame, exactly like a real terminal
        recovers from this when its window is resized (TerminalAgent.
        request_repaint, wired to staleLayoutDetected by TerminalCard).

        Fires ONLY on the EDGE: a footer row that moved further from the
        bottom than the last check. A short conversation that legitimately
        has blank space below its footer gets checked once, finds nothing to
        fix next time (same row), and is never repainted again -- this is
        what keeps normal use free of any repeated resize blip."""
        if self._scroll_offset:
            return  # only the live tail can be "stranded"
        span = self._input_block_span()
        if span is None:
            self._input_gap_row = None
            return
        _, bottom = span
        footer = bottom + 1
        edge = footer if (footer < self.screen.lines
                          and self._row_is_input_footer(footer)) else bottom
        gap = (self.screen.lines - 1) - edge
        if gap <= _INPUT_GAP_TOLERANCE:
            self._input_gap_row = None
            return
        for r in range(edge + 1, self.screen.lines):
            if self._row_content(r) != (-1, -1):
                return  # not actually blank all the way down -- leave it
        if self._input_gap_row == edge:
            return  # already asked for a repaint at this exact position
        self._input_gap_row = edge
        self.staleLayoutDetected.emit()

    def _reset_undo(self) -> None:
        """Forget the edit history -- called on submit (bare Enter) and reset,
        so undo never bleeds a previous message into a fresh prompt."""
        self._snap_timer.stop()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._undo_last = ""

    def _apply_input_text(self, text: str) -> None:
        """Replace the child's whole input with `text`: clear the prompt, then
        paste it (bracketed if the child enabled it). Coarse -- the caret ends
        at the end of the pasted text."""
        self._snap_to_bottom()
        self.keyInput.emit("\x1b\x1b")   # clear-prompt gesture
        if text:
            self._paste_text(text)

    def _undo(self) -> bool:
        """Restore the previous input snapshot. Returns False (so the caller
        forwards the 0x1a control byte instead) when there is nothing to undo."""
        if self._snap_timer.isActive():   # flush a pending coalesced edit first
            self._snap_timer.stop()
            self._snapshot_input()
        if not self._undo_stack:
            return False
        self._redo_stack.append(self._input_text())
        target = self._undo_stack.pop()
        self._apply_input_text(target)
        self._undo_last = target
        return True

    def _redo(self) -> bool:
        """Re-apply the most recently undone snapshot. Returns False when there
        is nothing to redo."""
        if not self._redo_stack:
            return False
        self._snap_timer.stop()
        self._undo_stack.append(self._input_text())
        target = self._redo_stack.pop()
        self._apply_input_text(target)
        self._undo_last = target
        return True

    def paste_clipboard(self) -> None:
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        # An image on the clipboard has no text, so it can't ride the pty as
        # bytes -- and a native-Windows Claude can't read a raw clipboard image
        # either (only WSL's Alt+V does, and it needs WSLg). Claude Code DOES
        # read images by file path on every platform, so we spill the image to
        # a temp PNG and paste its path: this automates the documented
        # "save it to disk, then reference the file" workaround. Prefer text
        # when the clipboard carries both (a copied file can expose both).
        if md is not None and md.hasImage() and not md.hasText():
            path = self._spill_clipboard_image(cb.image())
            if path:
                # quote only when needed -- a bare path is what Claude's
                # image-path detection expects; spaces would split the token
                self._paste_text(f'"{path}"' if " " in path else path)
            return
        text = cb.text()
        if not text:
            return
        self._paste_text(text.replace("\r\n", "\r").replace("\n", "\r"))

    def _paste_text(self, text: str) -> None:
        self._snap_to_bottom()
        # bracketed paste (if the app enabled mode 2004, e.g. Claude Code) so a
        # multi-line/large paste is delivered as one block, not line-by-line
        if self._bracketed_paste:
            self.keyInput.emit("\x1b[200~" + text + "\x1b[201~")
        else:
            self.keyInput.emit(text)

    def _spill_clipboard_image(self, image) -> str:
        """Write a clipboard QImage to a temp PNG and return its path (or "").

        Qt decodes the OS clipboard bitmap (incl. Snipping-Tool DIBs) into a
        QImage, so saving as PNG sidesteps the BMP-format breakage that dogs
        the WSL path. Any failure degrades to "" -> nothing pasted (never an
        exception into the key handler)."""
        if image is None or image.isNull():
            return ""
        import os
        import tempfile
        import uuid
        folder = os.path.join(tempfile.gettempdir(), "aihive-paste")
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, uuid.uuid4().hex + ".png")
            if not image.save(path, "PNG"):
                return ""
        except OSError:
            return ""
        return path

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        has_sel = self._selection_range() is not None
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        can_paste = bool(cb.text()) or (md is not None and md.hasImage())
        act_copy = menu.addAction("Copy")
        act_copy.setEnabled(has_sel)
        act_paste = menu.addAction("Paste")
        act_paste.setEnabled(can_paste)
        menu.addSeparator()
        act_all = menu.addAction("Select all")
        chosen = menu.exec(event.globalPos())
        if chosen == act_copy:
            self.copy_selection()
        elif chosen == act_paste:
            self.paste_clipboard()
        elif chosen == act_all:
            self.select_all()

    # ------------------------------------------------------------- paint ---

    def _color(self, value: str, default: str) -> QColor:
        if not value or value == "default":
            return QColor(default)
        named = _NAMED.get(value)
        if named:
            return QColor(named)
        if len(value) == 6:  # pyte encodes 256/truecolor as bare hex
            return QColor("#" + value)
        return QColor(default)

    def cell_bounds(self, col: int, row: int) -> tuple[int, int, int, int]:
        """Device-pixel (x0, y0, x1, y1) of one cell, ROUNDED on the shared
        grid so cell N's right edge is bit-identical to cell N+1's left edge.
        Deriving both edges from the same rounded expression is what makes
        adjacent block glyphs tile with no seam and no overlap — computing a
        width instead (round(w) per cell) accumulates error across a row."""
        return (round(CELL_PAD_X + col * self._cell_w),
                round(CELL_PAD_Y + row * self._cell_h),
                round(CELL_PAD_X + (col + 1) * self._cell_w),
                round(CELL_PAD_Y + (row + 1) * self._cell_h))

    def _is_grid_glyph(self, data: str) -> bool:
        """True when this character advances exactly one cell in the terminal
        font. A character the font LACKS is resolved by Qt's fallback to some
        other family whose advance is its own (Segoe UI Symbol renders the
        quadrant blocks 12px wide where the cell is 7). Since paintEvent draws
        a whole run with one drawText, letting Qt lay it out, such a glyph
        would drag every following character in the run off the grid — so the
        caller draws it ALONE at its own cell instead."""
        if data.isascii():  # the overwhelmingly common case, always in-font
            return True
        cached = self._grid_glyph_cache.get(data)
        if cached is None:
            adv = QFontMetricsF(self._font).horizontalAdvance(data)
            cached = abs(adv - self._cell_w) < 0.01
            self._grid_glyph_cache[data] = cached
        return cached

    def _paint_block(self, painter, data: str, col: int, row: int,
                     color: QColor) -> None:
        """Draw one Block Element as filled rects on the cell grid."""
        x0, y0, x1, y1 = self.cell_bounds(col, row)
        shade = _BLOCK_SHADES.get(data)
        if shade is not None:
            tint = QColor(color)
            tint.setAlphaF(color.alphaF() * shade)
            painter.fillRect(x0, y0, x1 - x0, y1 - y0, tint)
            return
        w, h = x1 - x0, y1 - y0
        for (fx0, fy0, fx1, fy1) in _BLOCK_RECTS[data]:
            rx0, rx1 = x0 + round(fx0 * w), x0 + round(fx1 * w)
            ry0, ry1 = y0 + round(fy0 * h), y0 + round(fy1 * h)
            painter.fillRect(rx0, ry0, rx1 - rx0, ry1 - ry0, color)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(Palette.BG_CONSOLE))
        painter.setFont(self._font)

        hist, off = self._view_state()
        cw, ch = self._cell_w, self._cell_h

        # snapshot the visible lines once (used for both rendering and the link
        # scan). Rescan clickable spans only when the content actually changed —
        # a cheap per-row-text signature guards the (filesystem-touching) scan.
        lines = [self._visible_line(row, hist, off)
                 for row in range(self.screen.lines)]
        _cols = self.screen.columns
        sig = (off, _cols,
               tuple("".join((ln[c].data or "") for c in range(_cols))
                     for ln in lines))
        if sig != self._link_sig:
            self._link_sig = sig
            self._rescan_links(lines)

        # selection highlight (drawn under the text)
        rng = self._selection_range()
        if rng is not None:
            (r0, c0), (r1, c1) = rng
            sel = QColor(Palette.SELECTION)
            sel.setAlpha(90)
            for r in range(r0, r1 + 1):
                sc = c0 if r == r0 else 0
                ec = c1 if r == r1 else self.screen.columns - 1
                painter.fillRect(
                    int(CELL_PAD_X + sc * cw), int(CELL_PAD_Y + r * ch),
                    int((ec - sc + 1) * cw), int(ch) + 1, sel)
        for row in range(self.screen.lines):
            line = lines[row]
            y = CELL_PAD_Y + row * ch
            col = 0
            while col < self.screen.columns:
                char = line[col]
                # batch a run of visually identical cells
                run_end = col + 1
                while run_end < self.screen.columns:
                    nxt = line[run_end]
                    if (nxt.fg, nxt.bg, nxt.bold, nxt.italics, nxt.underscore,
                            nxt.reverse) != (char.fg, char.bg, char.bold,
                                             char.italics, char.underscore,
                                             char.reverse):
                        break
                    run_end += 1
                text = "".join(line[i].data or " " for i in range(col, run_end))

                fg = self._color(char.fg, Palette.CONSOLE_FG)
                bg = self._color(char.bg, Palette.BG_CONSOLE)
                if char.bold and char.fg in _NAMED and \
                        not char.fg.startswith("bright"):
                    fg = QColor(_NAMED.get("bright" + char.fg, fg.name()))
                if char.reverse:
                    fg, bg = bg, fg

                if bg != QColor(Palette.BG_CONSOLE):
                    # on the SAME rounded grid the block glyphs use, so a
                    # coloured backdrop meets its neighbours exactly (this is
                    # what sits behind Claude's welcome mascot)
                    bx0, by0, _, by1 = self.cell_bounds(col, row)
                    bx1 = self.cell_bounds(run_end - 1, row)[2]
                    painter.fillRect(bx0, by0, bx1 - bx0, by1 - by0, bg)
                if text.strip():
                    font = QFont(self._font)
                    if char.bold:
                        font.setWeight(QFont.Weight.Bold)
                    if char.italics:
                        font.setItalic(True)
                    if char.underscore:
                        font.setUnderline(True)
                    painter.setFont(font)
                    # guarantee the glyph is readable on its real background,
                    # whatever color the child emitted (dark-tuned on a light
                    # theme, or vice versa)
                    ink = legible_color(fg, bg)
                    painter.setPen(ink)
                    # Split the run into grid-safe pieces. Block Elements are
                    # painted geometrically; a glyph the font lacks is drawn on
                    # its own so its fallback advance can't shear the row (see
                    # _BLOCK_RECTS / _is_grid_glyph). Ordinary text — virtually
                    # every run — still goes out in ONE drawText.
                    i = col
                    while i < run_end:
                        data = line[i].data or " "
                        if data in _BLOCK_RECTS or data in _BLOCK_SHADES:
                            self._paint_block(painter, data, i, row, ink)
                            i += 1
                            continue
                        j = i
                        while j < run_end:
                            nd = line[j].data or " "
                            if (nd in _BLOCK_RECTS or nd in _BLOCK_SHADES
                                    or not self._is_grid_glyph(nd)):
                                break
                            j += 1
                        if j == i:  # a lone off-grid glyph, placed by hand
                            j = i + 1
                        painter.drawText(
                            int(CELL_PAD_X + i * cw), int(y + self._ascent),
                            "".join(line[k].data or " " for k in range(i, j)))
                        i = j
                col = run_end

        # every clickable URL/path is underlined so you can spot links in the
        # body text at a glance (a soft 3px accent line); the hovered one is
        # emphasized below with a solid 4px line + the hand cursor.
        if self._link_spans:
            soft = QColor(Palette.ACCENT_ORANGE)
            soft.setAlpha(140)
            for (lr, lc0, lc1) in self._link_spans:
                lx = CELL_PAD_X + lc0 * cw
                ly = CELL_PAD_Y + lr * ch
                painter.fillRect(int(lx), int(ly + ch - 3),
                                 int((lc1 - lc0 + 1) * cw), 3, soft)
        # hover: emphasize the link under the pointer (paired with the hand
        # cursor from mouseMoveEvent) so it reads as the one you'd open
        if self._hover_link is not None:
            hr, hc0, hc1 = self._hover_link
            lx = CELL_PAD_X + hc0 * cw
            ly = CELL_PAD_Y + hr * ch
            painter.fillRect(int(lx), int(ly + ch - 4),
                             int((hc1 - hc0 + 1) * cw), 4,
                             QColor(Palette.ACCENT_ORANGE))

        cursor = self.screen.cursor
        if not cursor.hidden and off == 0:  # cursor lives on the live screen
            cx = CELL_PAD_X + cursor.x * cw
            cy = CELL_PAD_Y + cursor.y * ch
            if self.hasFocus():  # solid block, char inverted underneath
                painter.fillRect(int(cx), int(cy), max(1, int(cw)), int(ch),
                                 QColor(Palette.ACCENT_ORANGE))
                ch_cell = self.screen.buffer[cursor.y][cursor.x]
                if ch_cell.data.strip():
                    painter.setPen(QColor(Palette.BG_CONSOLE))
                    painter.drawText(int(cx), int(cy + self._ascent), ch_cell.data)
            else:  # hollow box when unfocused
                painter.setPen(QColor(Palette.ACCENT_ORANGE))
                painter.drawRect(int(cx), int(cy), max(1, int(cw) - 1), int(ch) - 1)

        # (the old "up-arrow N" scrolled-back badge lived here, at
        # width() - tw - 20 -- exactly where TerminalScrollBar now sits. The
        # thumb says the same thing continuously and in the right place, so the
        # badge would only be a second, worse readout fighting it for pixels.)
        painter.end()
