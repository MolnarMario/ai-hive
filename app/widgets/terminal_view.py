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
The view keeps Qt's input method enabled even though it has no text buffer.
Windows feeds the Win+V clipboard and Win+. emoji panels their keys through
the input context, so without it Up/Down/Enter/Esc went to the child while a
panel sat open. A Win+V pick then arrives as Ctrl+V; emoji and finished IME
compositions arrive as committed text (inputMethodEvent). That also turns on
a CJK IME in every terminal, so the view draws the composition at the cursor
itself (the child sees nothing until it's committed), and tells Qt after a
repaint that moved the cursor, or panels open where the cursor was at
focus-in.
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
shell prompt, OR Claude Code's own classic/"default" TUI renderer -- see
CLAUDE.md's terminal-scrollback invariant -- which never negotiates mouse
tracking at all, even while showing a clickable-looking AskUserQuestion/plan/
permission menu), a plain left-click places the input caret where you clicked,
by sending the child arrow keys (a terminal can't set the child's cursor
directly): exact on the caret's own line (Left/Right by the column delta), and
best-effort on another line of a multi-line prompt (Up/Down to the row, then
Left/Right to the predicted landing column -- see _reposition_cursor). Only
rows inside the input box are touched, so clicking the transcript never nudges
the prompt -- and while the agent is parked on an interactive menu
(waiting_probe, wired to agent.is_waiting()) a click is inert instead, because
that menu has no readline caret for arrow keys to land on and sending them
into it is what corrupts/dismisses it (the classic-renderer path onto the
exact bug the mouse-tracking forward above exists to prevent). A drag
selects text instead and never moves the caret. Double-click selects the whitespace-delimited word
under the pointer (then Ctrl+C copies it); Ctrl+click (or middle/scroll-wheel
click) opens a URL or an existing local file path under the pointer via the OS
default handler (_link_at/_classify_link/_open_target). File paths may be
ABSOLUTE, or RELATIVE to the agent's working directory when that base dir has
been supplied via set_base_dir() -- so a repo-relative path Claude prints
(app/widgets/sidebar.py) is clickable too; opening a file also emits
fileActivated(abspath) so the app can reveal it in the sidebar file tree.
Ctrl+LEFT-click is primary -- the left button always delivers, while the middle
button is often eaten by the OS (autoscroll). RIGHT-clicking that same token
adds Open / Open with... / Reveal in folder / Copy path to the context menu
(Open link / Copy link address for a URL), worded as the Agent/File Map's menu
words them and carrying the RESOLVED absolute path -- 'Reveal in folder' is
explorer /select, i.e. the folder opens with the file highlighted.
EVERY such link on the visible screen is underlined (a soft accent line) so
URLs/paths stand out in the body text without hovering; hovering one
emphasizes it (solid line) and switches to a hand cursor so it reads as the one you'd open. The full-screen scan
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
A drag held above or below the view scrolls history under it, faster the
further past the edge, and selects whole rows to that edge. A held pointer
sends no moves, so a timer drives it (_drag_scroll_tick). The selection is
kept in visual rows, and every scroll of the view (wheel, scrollbar,
Shift+PageUp, a keystroke's snap to live) moves it with its text
(_set_offset), so its ends can sit off screen: selected_text reads those
rows from history, paint draws only the part on screen, and deletion fails
closed on them. Output arriving while live doesn't move it. The alternate
screen never auto-scrolls, for the same reason the wheel doesn't scroll
history there.
Options > Bionic reading (off by default, one class flag for every view)
draws the first half of each prose word in bold. app/word_starts.py picks
the cells from a whole row; _word_start_rows runs it over the visible rows
once per paint, skipping the alternate screen, the live input rows and the
link spans, and caches each row's result by its cells (text and bold). The
main pass, the find overlay and the focused cursor all draw those cells
with _run_font's bold, so the effect never blinks off under an overlay.
Options > Contrast (_word_strength, a level of WORD_STRENGTHS) sets how hard
the heads contrast. Most terminal fonts draw Bold only a little heavier
than Regular, and the bundled ones have nothing heavier, so the higher
levels draw the bold head twice, a fraction of a pixel apart, and paint
the rest of each word at a lower opacity. Level 1 is the plain bold.
Ctrl+F finds text in the conversation the way Chrome's find bar does: the
view emits findRequested and the card opens its FindBar (terminal_find.py),
which feeds the query back through set_find_query. The view owns the search
because only it can read the rows: every scrollback line plus the live
screen, matched case-insensitively within one row at a time (a phrase the
child wrapped onto the next row is not found). Every match is painted on a
yellow box, the active one orange, and stepping to a match off screen
scrolls the view to center it. Typing a query makes the match nearest the
view active (the first at or below the view's top row, else the last one
above it) rather than wrapping to the oldest line, since a terminal is read
from the bottom up. New output rescans at most every FIND_RESCAN_MS and
keeps the active match by its absolute line, so a streaming agent never
yanks the view. Ctrl+F no longer reaches the child (readline's forward-char,
which Right arrow also does) except on the alternate screen, where less and
vim page forward with it. F3/Shift+F3 step through matches while the find
box holds a query and reach the child otherwise.

Fonts: the family and letter spacing come from Options > Appearance
(ui_theme.TERMINAL_FONT and TERMINAL_LETTER_PX, the same for every view),
the pixel size from the view (a per-agent font_px wins). Column N sits at
CELL_PAD_X + N * _cell_w, where _cell_w is the advance of "M" with the
spacing in it, so every style has to advance exactly one cell or a run
shears the rest of its row: _run_font holds each style's font to the cell
(synthetic bold is wider), _is_grid_glyph sends a glyph that still isn't
one cell to its own drawText, and _paint_block / _paint_box draw block
elements and box-drawing lines as geometry so they meet at cell edges.
"""

import collections
import math
import re
import time

import pyte
from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication,
                           QPainter, QPainterPath, QPen, QStaticText,
                           QWheelEvent)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from .. import ui_theme
from ..terminal_agent import (REFLOW_BEGIN, REFLOW_END, _CLAUDE_READY_HINTS,
                              _NUM_OPTION_RE)
from ..ui_theme import ANSI_16, Palette
from ..word_starts import word_spans

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

# Options > Contrast for Bionic reading, level 1 to 5. Per level: how
# far right the bold head of each word is drawn a second time, as a share
# of the font's pixel size, and the opacity of the rest of the word. Level
# 1 is plain Bold with nothing faded, which most terminal fonts draw only a
# little heavier than Regular. Bionic reading reads by contrast between
# the head and the rest, so the default fades the rest as well.
WORD_STRENGTHS = ((0.0, 1.0), (0.05, 1.0), (0.06, 0.78), (0.07, 0.64),
                  (0.07, 0.5))
DEFAULT_WORD_STRENGTH = 3
# runs of one value in a _word_start_rows mask
_MASK_RUNS = re.compile(rb"\x00+|\x01+|\x02+")

# First glyph of an input-box line, used by Ctrl+A to find where the prompt
# you're typing into begins (so the highlight stops there, not up in the
# transcript). Claude Code draws '>'; '❯' covers common shell/other prompts.
_INPUT_PROMPTS = (">", "❯")

# How many rule and hint rows reply_anchor_line() skips above the freshly
# redrawn input box on its way to the finished reply's own footer row
# ("Crunched for Ns" and its many spinner-verb siblings). Blank rows don't
# count: Codex leaves the whole gap above its bottom-pinned composer blank.
# The scan never leaves the live screen, and a screen that is nothing but
# chrome gets no stamp rather than one on older content.
_REPLY_ANCHOR_SCAN = 6

# Claude Code's settled-turn footer: a spinner glyph, a verb, and how long the
# turn took -- "✻ Worked for 16m 36s", "✻ Cooked for 8m 2s · 1 shell still
# running". The reply stamp is drawn on the blank row BELOW this rather than
# beside the reply text above it (see reply_anchor_line). The leading-glyph
# slot must be followed by a WORD (any letters, "Sautéed" is one) and then
# " for <duration>", which is what keeps the input box's own footer hints
# ("? for shortcuts", "← for agents") out: neither has a second "for" after
# its first word.
_REPLY_FOOTER_RE = re.compile(
    r"^\s*[^\w\s]?\s*[^\W\d_](?:[^\W\d_]|['\-])*\s+for\s+"
    r"(?:\d+h\s*)?(?:\d+m\s*)?\d+(?:\.\d+)?s\b", re.I)


def is_reply_footer(text: str) -> bool:
    """True when a row is Claude Code's "<verb> for <duration>" turn footer.
    Shared with TerminalCard._reply_end_row so the live anchor and the
    transcript-recovered one put a stamp in the SAME place."""
    return bool(_REPLY_FOOTER_RE.match(text.strip()))


# The line Claude Code prints once an AskUserQuestion form is answered:
# "● User answered Claude's questions:" (claude 2.1, read off a saved screen).
# Anchored at the row start, past at most one bullet glyph, so a prompt the
# user typed that happens to quote it ("> User answered...") never matches.
_ANSWER_ECHO_RE = re.compile(r"^\s*(?:[^\w\s>❯]\s*)?User answered Claude\b")
# How far back from the bottom of the live screen answer_echo_line()
# looks for that line. The hook edge that mints the mark lands up to a poll
# (PROMPT_SYNC_MS) after the answer, and a fast reply can stream a screen or
# two past it by then.
_ANSWER_ANCHOR_SCAN = 300


def is_answer_echo(text: str) -> bool:
    """True when a row is Claude's "User answered Claude's questions" line.
    Shared with TerminalCard._recover_marks so a live answer dot and a
    recovered one land on the same row."""
    return bool(_ANSWER_ECHO_RE.match(text))


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

# Rows of Claude's own box chrome allowed between the last typed line and the
# bottom of the picture: the box's bottom border plus a footer hint that wraps
# onto a second or third row on a narrow card. More than this below the box
# means real content is down there, so the layout is not stranded.
_INPUT_CHROME_ROWS = 4

# Debounce for the output-driven stranded-layout check. Long enough that a
# menu teardown's whole redraw collapses into one check, short enough that the
# box comes back within about a second of the menu closing.
_GAP_CHECK_MS = 500

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
# rows a full history keeps after evicting them, so a take-back
# (_drop_repeated_push, _drop_repaint_copies) can put back the rows its
# copies pushed out. On a full history, a repaint that copied more rows than
# this keeps its copies.
EVICTED_KEEP = 512
CELL_PAD_X = 6   # left/right inner padding (real terminals aren't flush)
CELL_PAD_Y = 4   # top/bottom inner padding
VIEW_NOTIFY_MS = 50   # coalescing window for viewChanged
# A drag held above or below the view scrolls history one step per
# DRAG_SCROLL_MS: 1 line just past the edge, ramping to DRAG_SCROLL_MAX_LINES
# at DRAG_SCROLL_RAMP_PX past it (xterm.js uses the same shape). At the
# fastest, HISTORY_LINES takes about 10 s to cross.
DRAG_SCROLL_MS = 50
DRAG_SCROLL_MAX_LINES = 10
DRAG_SCROLL_RAMP_PX = 60
# how far above the caret to look for the submitted input when the caret
# itself is parked on a blank row (see TerminalView._submitted_input)
_INPUT_SCAN_ROWS = 6
# the caret must be this far down the screen for that fallback to apply at all:
# Claude's classic renderer keeps its input box at the bottom, so a caret up in
# the conversation means the user is not typing a prompt
_INPUT_ZONE_ROWS = 12
_WIDE_GAP_RE = re.compile(r"\s{8,}")
# Ctrl+F: the longest a streaming agent waits for its matches to catch up
# with new output. A ceiling, not a debounce (see _find_dirty).
FIND_RESCAN_MS = 120
# Chrome's own find colors. Fixed rather than Palette reads: a match has to
# stand out on every skin's console background, the light one included, and
# the text on it is redrawn in FIND_INK so it stays legible whatever color
# the child printed it in.
FIND_HIT = "#ffdf4a"
FIND_ACTIVE = "#ff9632"
FIND_INK = "#141414"


def _find_row_text(line, cols: int):
    """(text, colmap) of one row for find, trailing blanks dropped. colmap is
    None when string index == column; otherwise it maps each index to its
    column, because a wide glyph leaves an empty second cell and a combining
    mark joins the cell before it."""
    datas = [line[c].data for c in range(cols)]
    joined = "".join(datas)
    if len(joined) == cols and "" not in datas:
        return joined.rstrip(), None
    return joined.rstrip(), [c for c, d in enumerate(datas) for _ in d]


def _row_text(row, cols: int, blank) -> str:
    """A row's text with trailing blanks dropped, for the repaint rules."""
    return "".join(row.get(x, blank).data for x in range(cols)).rstrip()


def _row_cells(row, cols: int, blank) -> tuple:
    """What each cell of a row shows, for telling a copied row from new
    output. An erased cell and one never written look alike, and so do
    blanks in two foreground colors: ConPTY's repaint writes spaces the
    first paint skipped, in whatever color is current (measured on saved
    screens)."""
    out = []
    for x in range(cols):
        ch = row.get(x, blank)
        if ch.data.strip():
            out.append(ch)
        else:
            out.append((ch.bg, ch.reverse, ch.underscore, ch.strikethrough))
    return tuple(out)


class _CountingDeque(collections.deque):
    """A deque that remembers how many items were ever appended. pyte pushes
    each scrolled-off line to history.top via append; once the deque saturates
    (append-then-evict), len() stops growing, so a scrolled-back view would
    drift because feed() can't see that lines still scrolled by. `pushed` keeps
    counting past saturation, giving feed() the true growth to anchor against.
    (AI Hive never triggers pyte paging — scrollback is a view offset — so only
    `append` counts.)

    It also keeps the last rows a full deque evicted, so a push that
    TerminalView._drop_repeated_push takes back can return the row it pushed
    out of the front (`evicted` counts them all, like `pushed`)."""

    def __init__(self, iterable=(), maxlen=None):
        super().__init__(iterable, maxlen=maxlen)
        self.pushed = 0
        self.evicted = 0
        self.recent_evicted = collections.deque(maxlen=EVICTED_KEEP)

    def append(self, x):
        self.pushed += 1
        if self.maxlen is not None and len(self) == self.maxlen:
            self.evicted += 1
            self.recent_evicted.append(self[0])
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

    # (pushed, evicted) of history.top each time a cursor-up reached the top
    # row since TerminalView last looked, oldest first. A full-frame repaint
    # starts there (see TerminalView._drop_repaint_copies).
    top_jumps: list

    def __init__(self, *args, **kwargs) -> None:
        self.top_jumps = []
        super().__init__(*args, **kwargs)

    def cursor_up(self, count=None) -> None:
        # only a move UP counts: a renderer erasing what it drew climbs with
        # CUU (measured: ESC[24A, ESC[36A), while clear-and-print homes the
        # cursor with CUP, and a second print after it is real output
        super().cursor_up(count)
        if self.cursor.y or (self.margins and self.margins.top):
            return
        top = self.history.top
        mark = (getattr(top, "pushed", 0), getattr(top, "evicted", 0))
        jumps = self.top_jumps
        if not jumps or jumps[-1] != mark:
            jumps.append(mark)
            if len(jumps) > 8:
                del jumps[0]

    def _reset_history(self) -> None:
        super()._reset_history()
        self.top_jumps = []

    def resize(self, lines=None, columns=None) -> None:
        # a jump from before a resize describes another layout
        super().resize(lines, columns)
        self.top_jumps = []

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
# TerminalAgent's reflow markers, split out of a chunk so each part of it
# is fed on its own side of the marker (see _apply_reflow_marker)
_REFLOW_RE = re.compile("(" + re.escape(REFLOW_BEGIN) + "|"
                        + re.escape(REFLOW_END) + ")")
_REFLOW_TAG = REFLOW_BEGIN[:7]
# ConPTY opens every frame it paints with a visible cursor by hiding it, and
# closes it by showing the cursor again (see TerminalView._feed_frames)
_FRAME_START = "\x1b[?25l"
_FRAME_END = "\x1b[?25h"
# pyte acts on none of these but keeps them in Screen.mode, in stream order,
# each private mode shifted left by 5
_ALT_SCREEN_MODES = frozenset(m << 5 for m in (1049, 1047, 47))
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

# Box Drawing lines (U+2500-257F) as arms from the cell's middle to its edges:
# (left, right, up, down), each 0 none, 1 light, 2 heavy, 3 double. The font
# draws these while its glyph fills the cell, which Consolas's do, so the
# default look is the font's own. Geometry takes over when a glyph can't meet
# its neighbour (_draws_as_box). Letter spacing is added after every glyph,
# box drawing included, so a rule of "─" came out dashed. A family with no
# box drawing at all (OpenDyslexic Mono, Atkinson Hyperlegible Mono) gets
# Consolas's narrower glyph and the same gaps. Dashed lines, diagonals and
# the double-line corners and tees stay with the font: they're drawn alone
# at their own cell, so the columns hold, but they can show a seam.
_BOX_ARMS = {
    "─": (1, 1, 0, 0), "━": (2, 2, 0, 0), "│": (0, 0, 1, 1), "┃": (0, 0, 2, 2),
    "┌": (0, 1, 0, 1), "┍": (0, 2, 0, 1), "┎": (0, 1, 0, 2), "┏": (0, 2, 0, 2),
    "┐": (1, 0, 0, 1), "┑": (2, 0, 0, 1), "┒": (1, 0, 0, 2), "┓": (2, 0, 0, 2),
    "└": (0, 1, 1, 0), "┕": (0, 2, 1, 0), "┖": (0, 1, 2, 0), "┗": (0, 2, 2, 0),
    "┘": (1, 0, 1, 0), "┙": (2, 0, 1, 0), "┚": (1, 0, 2, 0), "┛": (2, 0, 2, 0),
    "├": (0, 1, 1, 1), "┝": (0, 2, 1, 1), "┞": (0, 1, 2, 1), "┟": (0, 1, 1, 2),
    "┠": (0, 1, 2, 2), "┡": (0, 2, 2, 1), "┢": (0, 2, 1, 2), "┣": (0, 2, 2, 2),
    "┤": (1, 0, 1, 1), "┥": (2, 0, 1, 1), "┦": (1, 0, 2, 1), "┧": (1, 0, 1, 2),
    "┨": (1, 0, 2, 2), "┩": (2, 0, 2, 1), "┪": (2, 0, 1, 2), "┫": (2, 0, 2, 2),
    "┬": (1, 1, 0, 1), "┭": (2, 1, 0, 1), "┮": (1, 2, 0, 1), "┯": (2, 2, 0, 1),
    "┰": (1, 1, 0, 2), "┱": (2, 1, 0, 2), "┲": (1, 2, 0, 2), "┳": (2, 2, 0, 2),
    "┴": (1, 1, 1, 0), "┵": (2, 1, 1, 0), "┶": (1, 2, 1, 0), "┷": (2, 2, 1, 0),
    "┸": (1, 1, 2, 0), "┹": (2, 1, 2, 0), "┺": (1, 2, 2, 0), "┻": (2, 2, 2, 0),
    "┼": (1, 1, 1, 1), "┽": (2, 1, 1, 1), "┾": (1, 2, 1, 1), "┿": (2, 2, 1, 1),
    "╀": (1, 1, 2, 1), "╁": (1, 1, 1, 2), "╂": (1, 1, 2, 2), "╃": (2, 1, 2, 1),
    "╄": (1, 2, 2, 1), "╅": (2, 1, 1, 2), "╆": (1, 2, 1, 2), "╇": (2, 2, 2, 1),
    "╈": (2, 2, 1, 2), "╉": (2, 1, 2, 2), "╊": (1, 2, 2, 2), "╋": (2, 2, 2, 2),
    "═": (3, 3, 0, 0), "║": (0, 0, 3, 3),
    "╭": (0, 1, 0, 1), "╮": (1, 0, 0, 1), "╯": (1, 0, 1, 0), "╰": (0, 1, 1, 0),
    "╴": (1, 0, 0, 0), "╵": (0, 0, 1, 0), "╶": (0, 1, 0, 0), "╷": (0, 0, 0, 1),
    "╸": (2, 0, 0, 0), "╹": (0, 0, 2, 0), "╺": (0, 2, 0, 0), "╻": (0, 0, 0, 2),
    "╼": (1, 2, 0, 0), "╽": (0, 0, 1, 2), "╾": (2, 1, 0, 0), "╿": (0, 0, 2, 1),
}
# the rounded corners: a quarter circle joining their two arms
_BOX_ARCS = frozenset("╭╮╯╰")
# every character paintEvent may draw as geometry instead of text
_CELL_SHAPES = frozenset(_BLOCK_RECTS) | frozenset(_BLOCK_SHADES) \
    | frozenset(_BOX_ARMS)

# The longest feed and paint since the stall probe last looked
# (app/perf_audit.py reads and resets them every tick). feed and paintEvent
# are the hottest code in the app, so each pays one perf_counter pair and a
# compare here, and stores a reference only on a new longest.
_perf_feed_s = 0.0
_perf_feed_len = 0
_perf_feed_view = None
_perf_paint_s = 0.0
_perf_paint_view = None


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
    # TerminalAgent's REFLOW_BEGIN marker dropped the scrollback because the
    # child is reprinting the same conversation at the new size, and
    # REFLOW_END says the reprint is in. Not historyCleared: the conversation
    # is unchanged, so the agent's milestones stay and only move.
    reflowStarted = Signal()
    reflowEnded = Signal()
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
    # Ctrl+F: the card opens (or refocuses) its find bar
    findRequested = Signal()
    # the find bar's "3/17": (1-based active match, or 0 with none; total)
    findChanged = Signal(int, int)

    def __init__(self, rows: int = 30, cols: int = 100, parent=None,
                 font_px: int = 0, claude_repaints: bool = False):
        super().__init__(parent)
        # take back the copies a Claude repaint from the top row scrolls
        # into history (see _drop_repaint_copies). Only Claude cards: the
        # bytes can't tell that repaint from a program reprinting rows on
        # purpose, and Claude's classic renderer is the one measured.
        self._claude_repaints = claude_repaints
        self.setObjectName("TerminalView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # Windows routes keys to its Win+V clipboard and Win+. emoji panels
        # through the window's input context. Without this attribute Qt
        # detaches that context while we have focus, so Up/Down/Enter/Esc
        # reached the child instead and the open panel couldn't be driven.
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setMouseTracking(True)  # hover (no button) to highlight links

        # base directory for resolving RELATIVE paths clicked in the output
        # (the agent's cwd == its workspace project_path); set via set_base_dir
        self._base_dir = ""
        self._esc_carry = ""  # trailing partial escape between feed() calls
        self._scroll_offset = 0  # lines scrolled back into history (0 = live)
        self._preedit = ""  # IME composition in progress, drawn at the cursor
        self._im_rect = None  # cursor rectangle Qt last heard about
        # footer row _check_input_gap last asked for a repaint at, so a
        # persistent (legitimately short) gap is checked once and left alone
        # rather than re-triggering a repaint on every settle
        self._input_gap_row = None
        # (row, col) selection start and end, in VISUAL rows. A scroll moves
        # both with the text (_set_offset), so either can sit off screen:
        # below the view (row >= lines) or above it (row < 0).
        self._sel_anchor = None
        self._sel_end = None
        # pointer of a drag held past the top or bottom edge, in view
        # coordinates, while _drag_timer scrolls history (_drag_scroll_tick)
        self._drag_pos = None
        # the left button went down in the view and is still down: only then
        # does a wheel grow the selection. A press held on the scrollbar or
        # the find bar forwards its wheel here with the left button down too.
        self._mouse_selecting = False
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
        # Authoritative "an interactive menu the mouse can't drive is open"
        # probe, set by the card to agent.is_waiting() (see set_waiting_probe).
        # Defaults to "never waiting" so the view works standalone/in tests.
        self._waiting_probe = lambda: False
        self._hover_link = None  # (row, c0, c1) of a link under the pointer
        self._hover_cell = None  # last hovered (row, col), to skip re-scans
        # every clickable URL/path on the visible screen, so they read as links
        # (underlined) even before you hover. Rescanned only when the visible
        # content changes (a cheap signature guards it) — never per repaint,
        # because classifying a token can stat the filesystem.
        self._link_spans: list[tuple[int, int, int]] = []  # (row, c0, c1)
        self._link_sig = None
        # Bionic reading (_word_start_rows): each visible row's bold mask
        # keyed by its cells, the input rows keyed by what they read, and
        # the bold pass's laid-out text for the font it was laid out in
        self._ws_rows: dict = {}
        self._ws_input = (None, None)
        self._ws_static: dict = {}
        self._ws_font = None
        self._bracketed_paste = False  # tracked from the stream (pyte ignores 2004)
        self._alt_screen = False       # ?1049/?1047/?47 — app owns the screen
        self._mouse_tracking = False   # ?1000/?1002/?1003 — app wants mouse
        self._mouse_sgr = False        # ?1006 — SGR mouse encoding
        self._app_cursor_keys = False  # ?1 DECCKM — arrows send SS3 form
        self._focus_reporting = False  # ?1004 — app wants focus in/out events
        # Options > Terminal font and Letter spacing are app-wide (ui_theme
        # holds them); the pixel size is this view's own, so a card with a
        # per-agent font_px keeps it whatever the family
        self._letter_px = ui_theme.TERMINAL_LETTER_PX
        self._font = self._base_font(ui_theme.terminal_font_families(),
                                     font_px or ui_theme.CONSOLE_FONT_PX,
                                     self._letter_px)
        # (char, bold, italic) -> "does this glyph advance exactly one
        # cell?" (see _is_grid_glyph), and (bold, italic, underline) -> the
        # font a run in that style paints with (see _run_font). Both are
        # cleared whenever the metrics change (_apply_metrics).
        self._grid_glyph_cache: dict[tuple[str, bool, bool], bool] = {}
        self._style_fonts: dict[tuple[bool, bool, bool], QFont] = {}
        self._apply_metrics(resize="")

        self.screen = _new_history_screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        # the screen as the current ConPTY frame found it (see _feed_frames)
        self._frame_ref = None
        # between REFLOW_BEGIN and REFLOW_END: the tall reprint is fresh
        # history, not a repaint over it (see _drop_repaint_copies)
        self._reflowing = False

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._apply_resize)

        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(DRAG_SCROLL_MS)
        self._drag_timer.timeout.connect(self._drag_scroll_tick)

        # debounce that coalesces a typing burst into ONE undo snapshot (see
        # _snapshot_input / _kick_snapshot)
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(600)
        self._snap_timer.timeout.connect(self._on_input_settled)

        # The stranded-layout check also has to run when the user has typed
        # NOTHING: closing a slash-command menu is pure child output, and
        # _snap_timer is armed only by an edit keystroke (see _check_input_gap).
        # Restarting on every burst means a long redraw collapses into one
        # check once the screen is quiet, and the _input_gap_row edge guard
        # keeps it to one repair per occurrence.
        self._gap_timer = QTimer(self)
        self._gap_timer.setSingleShot(True)
        self._gap_timer.setInterval(_GAP_CHECK_MS)
        self._gap_timer.timeout.connect(self._check_input_gap)

        # prompt milestones painted on the scrollbar: (absolute line, tooltip).
        # Pure VIEW data -- the durable copy lives on the agent, because a card
        # rebuild throws this widget away (see TerminalCard._replay_with_marks).
        self._marks: list[tuple[int, str]] = []
        # answers to Claude's AskUserQuestion forms, the same shape and the
        # same contract as _marks, painted in their own colour
        self._answer_marks: list[tuple[int, str]] = []
        # reply-finished milestones painted INLINE in the terminal content
        # itself (a small date/time stamp above the input box, next to
        # Claude's own "for Ns" footer) rather than on the scrollbar. Same
        # pure-VIEW-data contract as _marks. (absolute line, stamp text).
        self._reply_marks: list[tuple[int, str]] = []
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

        # Ctrl+F find, pure view state like the marks. Matches are
        # (absolute line, first col, last col) in reading order; _find_rows
        # indexes them by line for paintEvent as (first col, last col, index).
        self._find_re = None
        self._find_matches: list[tuple[int, int, int]] = []
        self._find_rows: dict[int, list[tuple[int, int, int]]] = {}
        self._find_active = -1
        # id(history line) -> (line, cols, text, colmap). A line never changes
        # while it sits in history, so a rescan only reads the live screen and
        # lines new since the last one. The entry holds the line itself, so
        # its id can't be reused by another object while cached. Cleared
        # wherever rows leave history for the screen (_apply_reflow_marker).
        self._find_cache: dict[int, tuple] = {}
        # px from the top a match must sit below to count as on screen:
        # the find bar covers the rows above it (set_find_inset)
        self._find_inset = 0
        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(FIND_RESCAN_MS)
        self._find_timer.timeout.connect(self._find_refresh)

    # -------------------------------------------------------------- data ---

    def feed(self, data: str) -> None:
        global _perf_feed_s, _perf_feed_len, _perf_feed_view
        t0 = time.perf_counter()
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
                        self._shift_selection(-self._scroll_offset)
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
        if _REFLOW_TAG in data:
            for i, part in enumerate(_REFLOW_RE.split(data)):
                if i % 2:
                    self._apply_reflow_marker(part)
                elif part:
                    self._feed_screen(part)
        else:
            self._feed_screen(data)
        self._notify_view()
        self.update()
        self._gap_timer.start()
        self._find_dirty()
        t0 = time.perf_counter() - t0
        if t0 > _perf_feed_s:
            _perf_feed_s, _perf_feed_len, _perf_feed_view = t0, len(data), self

    def _feed_screen(self, data: str) -> None:
        top_before = self.screen.history.top
        pushed_before = getattr(top_before, "pushed", None)
        hist_before = len(top_before)
        dropped = self._feed_frames(data)
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
            # a drop (_feed_frames) that takes back rows an earlier feed
            # pushed lowers the count, and the offset that grew for them
            # shrinks back with it
            if grown:
                self._scroll_offset = max(0, min(self._scroll_offset + grown,
                                                 len(top_after)))
        # A SHRINK can only be _reset_history (pyte's ED 3 or screen.reset()):
        # history otherwise just grows toward maxlen, evicting from the front
        # without changing len() once saturated. That is the cheapest reliable
        # "the conversation was wiped" edge available from the stream itself.
        # Rows _feed_frames took back are not a wipe: once history is full,
        # their pushes evicted as many rows from the front.
        if len(self.screen.history.top) + dropped < hist_before:
            self._on_history_wiped()

    def _apply_reflow_marker(self, marker: str) -> None:
        """Act on a marker TerminalAgent wrote around a reflow (see
        REFLOW_EXTRA_ROWS). Both are in the stream, so a replay of it does
        exactly what the live view did.

        BEGIN: the child is about to reprint the whole conversation at the new
        size, so the scrollback drawn at the old one goes. The screen stays:
        the reprint paints over it a moment later, and a blank card in between
        would flash.

        END: the reprint is in, and the child is about to get the card's rows
        back. Shrinking its buffer, conhost keeps the rows that end at the
        later of its cursor row and its last row with text, then repaints
        them from the top. This screen is the bottom of what conhost painted
        into the taller pty, which ends on the blank row the renderer's cursor
        sits on, so it can sit lower than conhost's rows. Measured: one row
        lower, and conhost's repaint scrolled once more. Pulling the missing
        rows back out of history puts this screen on conhost's rows, so the
        repaint lands on the same content and no row reaches history twice."""
        if marker == REFLOW_BEGIN:
            self.screen._reset_history()
            self._forget_view_history()
            self._reflowing = True
            self.reflowStarted.emit()
            return
        self._reflowing = False
        scr = self.screen
        scr.top_jumps = []
        top = scr.history.top
        bottom = scr.cursor.y
        for y in range(scr.lines - 1, bottom, -1):
            row = scr.buffer.get(y)
            if row and any(ch.data.strip() for ch in row.values()):
                bottom = y
                break
        n = min(scr.lines - 1 - bottom, len(top))
        if n > 0:
            buf = scr.buffer
            for y in range(scr.lines - 1, n - 1, -1):
                buf[y] = buf[y - n]
            for y in range(n - 1, -1, -1):
                buf[y] = top.pop()
            # those rows are screen rows again and keep their ids (see the
            # coordinate block below: a screen row's id is pushed + row)
            if hasattr(top, "pushed"):
                top.pushed -= n
            scr.cursor.y += n
            scr.dirty.update(range(scr.lines))
            # every line keeps its place in history-plus-screen, so a row
            # moves down by the n rows minus what the offset gave back
            off = max(0, self._scroll_offset - n)
            self._shift_selection(n - (self._scroll_offset - off))
            self._scroll_offset = off
            self._vs_cache = None
            self._vs_sig = None
            # the popped rows are screen rows again and will be drawn over,
            # so their cached find text can go stale
            self._find_cache = {}
            # the frame's screen and push count moved under it, so it no
            # longer describes this screen (see _drop_repeated_push)
            self._frame_ref = None
        self.reflowEnded.emit()

    def _feed_frames(self, data: str) -> int:
        """Feed pyte, then take back the rows a frame pushed into history
        that are copies of rows already there. Returns how many.

        Two rules do it. _drop_repaint_copies handles a frame that repaints
        from the top row, which is how every copy measured so far got in.
        _drop_repeated_push is the older rule, for a frame that pushes rows
        and leaves the screen exactly as it found it.

        The older rule came from two saved screens read as a 40-row card:
        after the idle "recap" block printed, Claude redrew from 36 rows up
        and its writes ran 4 rows past the bottom, leaving the screen as it
        was and 4 copied rows in history. Replayed at 37 rows, the height
        where that 36-row jump lands on the top row, the same bytes are a
        repaint from the top whose 4 pushed rows equal the 4 at the end of
        history, and at 40 rows the replay ends with screen rows repeated in
        history. So those were the newer rule's case too. The older rule
        stays as a second net: it fires only when it can't be wrong. Output
        written at the cursor and scrolled k rows shifts every row above the
        cursor up by k. If the screen still ends unchanged, each of those rows
        equals the row k below it: they repeat every k rows (a run of "y"
        lines, say). Rows above the cursor that don't repeat can only end
        unchanged when the frame redrew rows it had already shown, and the k
        pushed rows are leftovers. Repeating rows stay ambiguous and are left
        alone, and so is k >= the screen height, where a program reprinting
        a screen it printed before is real output.

        Both run per ConPTY frame. ConPTY starts each frame by hiding the
        cursor and ends it by showing it again. The older rule cuts the
        stream before every ESC[?25l and keeps the screen at that cut across
        feeds for a frame split between two chunks; each feed's start is a
        checkpoint too. The newer rule looks after each ESC[?25h and before
        each ESC[?25l, never at a feed's end, so a frame split between two
        chunks is judged whole. A replay of the same stream cuts it the same
        way, so a rebuilt card drops the same rows."""
        top = self.screen.history.top
        if not hasattr(top, "pushed"):     # plain deque: no push count to use
            self.stream.feed(data)
            return 0
        dropped = 0
        for i, part in enumerate(data.split(_FRAME_START)):
            if i:
                dropped += self._drop_repaint_copies()   # the last frame ended
                part = _FRAME_START + part
                self._frame_ref = self._screen_ref()
                ref = None                 # same screen as the frame's
            else:
                ref = self._screen_ref()
            pieces = part.split(_FRAME_END)
            for piece in pieces[:-1]:
                self.stream.feed(piece + _FRAME_END)
                dropped += self._drop_repaint_copies()
            if pieces[-1]:
                self.stream.feed(pieces[-1])
            for r in (ref, self._frame_ref):
                if r is not None:
                    dropped += self._drop_repeated_push(r)
        return dropped

    def _drop_repaint_copies(self) -> int:
        """At the end of a ConPTY frame: take back the rows it pushed after
        the cursor moved to the top row when they repeat the rows that end
        the history. Returns how many.

        Claude's classic renderer keeps its last reply in the frame it
        redraws, and a long reply makes that frame taller than the card. A
        full repaint (after the idle recap, closing the Ctrl+O transcript,
        or taking the terminal back) moves the cursor up as far as it goes,
        which ConPTY clamps to the top row, and writes the whole frame from
        there. Every row past the card's height scrolls the screen, and what
        scrolls off is the frame's own top: rows that went into history the
        first time the frame was drawn. Measured on a live claude at 25 rows
        (Ctrl+O twice: one copied row) and on the saved screens of six
        agents at 37 rows (2 to 13 copied rows per repaint): the pushed rows
        equal the rows at the end of history, in order. A repaint that also
        changed a row on screen, a footer hint or the recap growing the
        frame, is the case the older rule let through, and how one reply
        paragraph showed up three times over in a card's scrollback.

        So the rule: of the rows pushed since a cursor-up last reached the
        top row in this frame, the longest leading run that equals, cell
        for cell, the same number of rows ending the history before them
        goes. Pushes
        after that run stay, since a frame that grew (the recap) scrolls
        its new rows off legitimately. A run must hold a row with text: a
        blank row after a blank row proves nothing. Plain output never
        moves the cursor up to the top row before scrolling, which keeps a
        program printing the same line twice out of this. Only a move up
        counts: a renderer erasing what it drew climbs with ESC[nA, while
        clearing the screen to print the same text again homes the cursor
        with ESC[H, and that second print is real output. Even so, a
        program could climb to the top row on purpose and print rows equal
        to the scrollback's last ones, and its bytes would read the same,
        so the rule runs only in a Claude card (`claude_repaints`). The
        alternate screen keeps no scrollback, and a reflow's reprint is
        fresh history rather than a repaint over it, so both are left
        alone."""
        scr = self.screen
        jumps = scr.top_jumps
        if not jumps:
            return 0
        scr.top_jumps = []
        # the alternate screen as of this frame's end, not _alt_screen:
        # feed() sets that from every mode switch in a chunk before feeding
        # any of it, and a replay cuts the stream into other chunks
        if (not self._claude_repaints or self._reflowing
                or scr.mode & _ALT_SCREEN_MODES):
            return 0
        top = scr.history.top
        cols = scr.columns
        blank = scr.default_char
        texts: dict = {}
        looks: dict = {}

        def text(i: int) -> str:
            # history row i, cached: a long frame compares the same rows
            # for every candidate run
            t = texts.get(i)
            if t is None:
                t = texts[i] = _row_text(top[i], cols, blank)
            return t

        def look(i: int) -> tuple:
            c = looks.get(i)
            if c is None:
                c = looks[i] = _row_cells(top[i], cols, blank)
            return c

        # the newest jump first: rows pushed before it were not this
        # repaint's. An older one still counts when the cursor went back up
        # after the copies were in, and it gets its turn after a take-back
        # too: two repaints in one frame push two sets of copies.
        dropped = 0
        for pushed_then, evicted_then in reversed(jumps):
            n = top.pushed - pushed_then
            size = len(top)
            if n <= 0 or n >= size:
                continue
            first = size - n                 # P = top[first:], T = top[:first]
            lead = text(first)
            run = 0
            for h in range(min(n, first), 0, -1):
                if text(first - h) != lead:
                    continue
                pairs = range(h)
                # text first, it rules out most runs cheaply; then colors,
                # since the same text in new colors is new output. Every
                # measured copy matched its original cell for cell.
                if not all(text(first + i) == text(first - h + i)
                           for i in pairs):
                    continue
                if not all(look(first + i) == look(first - h + i)
                           for i in pairs):
                    continue
                if any(text(first + i) for i in pairs):
                    run = h
                break
            if run:
                taken = self._take_back(run, n, evicted_then)
                if not taken:
                    break
                dropped += taken
                texts.clear()               # the history indexes moved
                looks.clear()
        return dropped

    def _take_back(self, run: int, n: int, evicted_then: int) -> int:
        """Remove the leading `run` of the last `n` history rows, and give
        back the rows those pushes evicted from a full history (see
        _CountingDeque). Returns `run`, or 0 when the evicted rows are no
        longer kept: copies are better than losing rows that can't come
        back."""
        top = self.screen.history.top
        gone = min(run, getattr(top, "evicted", 0) - evicted_then)
        kept = getattr(top, "recent_evicted", ())
        if gone > len(kept):
            return 0
        tail = [top.pop() for _ in range(n)]
        tail.reverse()
        for row in tail[run:]:
            collections.deque.append(top, row)   # not a push: no count
        top.pushed -= run
        for _ in range(gone):
            top.appendleft(kept.pop())
            top.evicted -= 1
        # rows went from the middle, so (pushed, len) can come back to a
        # value _view_state cached for other rows
        self._vs_cache = None
        self._vs_sig = None
        return run

    def _screen_ref(self):
        """The visible rows (as copies), the cursor row and the push count,
        for _drop_repeated_push to compare against."""
        scr = self.screen
        rows = [dict(r) if r else {}
                for r in map(scr.buffer.get, range(scr.lines))]
        top = scr.history.top
        return (rows, scr.cursor.y, top.pushed, getattr(top, "evicted", 0),
                (scr.lines, scr.columns))

    def _drop_repeated_push(self, ref) -> int:
        """Pop the rows pushed since `ref` when the screen still reads as it
        did then and its rows above the cursor don't repeat every k rows
        (see _feed_frames)."""
        rows_then, cursor_then, pushed_then, evicted_then, size = ref
        scr = self.screen
        top = scr.history.top
        k = top.pushed - pushed_then
        if (k <= 0 or k >= scr.lines or k > len(top)
                or size != (scr.lines, scr.columns)):
            return 0
        cols = scr.columns
        buf = scr.buffer
        blank = scr.default_char
        then = []
        for y, old in enumerate(rows_then):
            now = buf[y] if y in buf else {}
            old_text = _row_text(old, cols, blank)
            if now != old:
                # text first, it rules out ordinary scrolling cheaply; then
                # colors, since the same text in new colors is a change too
                if (_row_text(now, cols, blank) != old_text
                        or _row_cells(now, cols, blank)
                        != _row_cells(old, cols, blank)):
                    return 0
            then.append(old_text)
        if all(then[y] == then[y + k] for y in range(cursor_then - k)):
            return 0
        # a full history evicted a row per push, and those rows go back in
        # front (_take_back)
        return self._take_back(k, k, evicted_then)

    def reset(self) -> None:
        """A fresh terminal: blank screen, no scrollback, default modes. For
        a Restart's new child. Nothing of the old screen survives, a
        selection or a hovered link included, but `pushed` carries over so
        absolute line ids stay monotonic (see the coordinate block below)."""
        rows, cols = self.screen.lines, self.screen.columns
        pushed = getattr(self.screen.history.top, "pushed", 0)
        self.screen = _new_history_screen(cols, rows)
        try:
            self.screen.history.top.pushed = pushed
        except Exception:
            pass
        self.stream = pyte.Stream(self.screen)
        self._frame_ref = None
        self._reflowing = False
        self._esc_carry = ""
        self._input_gap_row = None
        self._sel_anchor = self._sel_end = None
        self._stop_drag_scroll()
        self._input_selected = False
        self._pending_fwd = None
        self._hover_link = None
        self._hover_cell = None
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

    def reply_anchor_line(self) -> int | None:
        """Absolute line of the row a just-finished reply's stamp goes on: the
        blank row directly UNDER Claude's own "<spinner verb> for Ns" footer,
        which is left in place once a turn settles. Used at BOTH the live
        capture (TerminalCard._on_reply_mark_added) and replay re-anchoring
        (_replay_with_marks), exactly like anchor_line() -- one function over
        identical screen state on both sides is what keeps them agreeing.

        Found by scanning up from the input box the child redraws at settle
        (`_input_block_span`'s top row), skipping the chrome between the two --
        blank rows (any number: Codex leaves a gap above its bottom-pinned
        composer) AND rules and hint rows, such as the box's own top border,
        which sits directly above the prompt row -- to the nearest real
        content row. At most _REPLY_ANCHOR_SCAN rule/hint rows are skipped, so
        a screen that is nothing but chrome can never walk into unrelated older
        history and mislabel it. None when there is no live input box to scan
        from at all (e.g. the agent settled parked on a menu) or no content is
        found; the mark is then simply skipped, exactly like an un-anchored
        PromptMark."""
        span = self._input_block_span()
        if span is None:
            return None
        chrome = 0
        r = span[0] - 1
        while r >= 0:
            first, _ = self._row_content(r)
            if first < 0:
                # A blank row costs nothing. Codex pins its composer to the
                # bottom of the window and leaves the whole gap above it
                # blank until the conversation fills the screen, so a reply
                # sits 20+ rows above the box. The old fixed row bound ended
                # that scan in the gap and Codex never got a stamp.
                r -= 1
                continue
            # Rules and the box's hint line are chrome between the box and the
            # reply. The box's own top BORDER sits directly above the prompt
            # row, so a scan that stopped at the first rule stopped before it
            # had looked at anything -- that bail-out is why the live stamp
            # silently never appeared on the real screen shape, while a
            # fixture without that border passed.
            if self._row_is_rule(r) or self._row_is_input_footer(r):
                chrome += 1
                if chrome > _REPLY_ANCHOR_SCAN:
                    return None     # a stack of chrome, not a settled reply
                r -= 1
                continue
            below = r + 1
            if (below < self.screen.lines
                    and self._row_content(below) == (-1, -1)):
                return self.history_pushed() + below  # UNDER the footer
            return self.history_pushed() + r    # nothing below to use
        return None

    def answer_echo_line(self) -> int | None:
        """Absolute line of the newest "User answered Claude's questions"
        row within _ANSWER_ANCHOR_SCAN rows of the bottom, or None. Used at
        BOTH the live capture (TerminalCard._mark_anchor) and replay
        re-anchoring, like anchor_line(): the mark is minted after Claude
        printed that row, so the same screen state gives the same row on
        both sides."""
        pushed = self.history_pushed()
        cols, blank = self.screen.columns, self.screen.default_char
        for r in range(self.screen.lines - 1, -1, -1):
            if is_answer_echo(_row_text(self.screen.buffer[r], cols, blank)):
                return pushed + r
        hist = self.screen.history.top
        left = _ANSWER_ANCHOR_SCAN - self.screen.lines
        for i in range(len(hist) - 1, max(-1, len(hist) - 1 - left), -1):
            if is_answer_echo(_row_text(hist[i], cols, blank)):
                return pushed - len(hist) + i
        return None

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
        # update() is NOT optional here, and _notify_view cannot stand in for
        # it: that signature is (pushed, history, offset, lines), which marks
        # are not part of, so it returns at its own guard on an idle screen --
        # and even when it does emit, viewChanged goes to the SCROLLBAR, never
        # to this widget's paint queue. See set_reply_marks for what that cost.
        self._marks = list(marks)
        self._notify_view(immediate=True)
        self.update()

    def marks(self) -> list[tuple[int, str]]:
        return list(self._marks)

    def set_answer_marks(self, marks) -> None:
        # update() for the same reason as set_marks
        self._answer_marks = list(marks)
        self._notify_view(immediate=True)
        self.update()

    def answer_marks(self) -> list[tuple[int, str]]:
        return list(self._answer_marks)

    def set_reply_marks(self, marks) -> None:
        # A reply stamp is minted 2 s after the last output (BUSY_IDLE_MS),
        # by which time the repaint that last feed() scheduled has long since
        # run -- so without this the stamp sat in _reply_marks unpainted until
        # something ELSE happened to repaint the widget (the next burst, a
        # resize, a focus change, a scroll). On an agent that has just gone
        # quiet, which is exactly the moment the stamp is for, that can be a
        # long wait, and it reads as the feature being flaky rather than as a
        # bug with an address.
        self._reply_marks = list(marks)
        self._notify_view(immediate=True)
        self.update()

    def reply_marks(self) -> list[tuple[int, str]]:
        return list(self._reply_marks)

    # The "Reply times" switch in Options. One flag for every terminal, read
    # at paint time: the marks are always collected, so flipping it back on
    # shows the whole history again instead of only the replies finished
    # since.
    _reply_stamps_enabled = True

    @classmethod
    def set_reply_stamps_enabled(cls, on: bool) -> None:
        on = bool(on)
        if on == cls._reply_stamps_enabled:
            return
        cls._reply_stamps_enabled = on
        for w in QApplication.allWidgets():
            if isinstance(w, TerminalView):
                w.update()

    @classmethod
    def reply_stamps_enabled(cls) -> bool:
        return cls._reply_stamps_enabled

    # The "Bionic reading" switch in Options. One flag for every terminal,
    # read at paint time like the one above. Default OFF.
    _word_starts_enabled = False

    @classmethod
    def set_word_starts_enabled(cls, on: bool) -> None:
        on = bool(on)
        if on == cls._word_starts_enabled:
            return
        cls._word_starts_enabled = on
        for w in QApplication.allWidgets():
            if isinstance(w, TerminalView):
                w.update()

    @classmethod
    def word_starts_enabled(cls) -> bool:
        return cls._word_starts_enabled

    # Options > Contrast, how hard Bionic reading contrasts, the same for
    # every terminal (MainWindow keeps one level per terminal font). Read
    # at paint time, so a change only repaints.
    _word_strength = DEFAULT_WORD_STRENGTH

    @classmethod
    def set_word_strength(cls, level: int) -> None:
        level = max(1, min(len(WORD_STRENGTHS), int(level)))
        if level == cls._word_strength:
            return
        cls._word_strength = level
        if cls._word_starts_enabled:
            for w in QApplication.allWidgets():
                if isinstance(w, TerminalView):
                    w.update()

    @classmethod
    def word_strength(cls) -> int:
        return cls._word_strength

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
        # both start the stream over, so a reprint the old stream left open
        # ends here: a projection meets its REFLOW_BEGIN again, a dropped
        # seed never will
        self._reflowing = False
        self._on_history_wiped()
        self._notify_view(immediate=True)

    def _on_history_wiped(self) -> None:
        self._forget_view_history()
        self.historyCleared.emit()

    def _forget_view_history(self) -> None:
        self._frame_ref = None
        # a selection that reaches into history lost its text with it; one
        # wholly on the live screen keeps it, since the screen stays
        if self._scroll_offset or any(
                p is not None and p[0] < 0
                for p in (self._sel_anchor, self._sel_end)):
            self._sel_anchor = self._sel_end = None
        self._scroll_offset = 0
        self._marks = []
        self._answer_marks = []
        self._reply_marks = []
        self._vs_cache = None
        self._vs_sig = None
        self._find_cache = {}
        # The reprinted lines get new ids, all past the active match's, so
        # anchoring on it would make the oldest match active. Pick from the
        # view instead.
        self._find_active = -1
        self._find_dirty()

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
        hist = self._history_list()
        return hist, min(self._scroll_offset, len(hist))

    def _history_list(self) -> list:
        """history.top as a list, for indexing."""
        top = self.screen.history.top
        # The copy is cached on (pushed, len): paintEvent, visible_text,
        # selected_text and every _visible_line call land here, so a user simply
        # parked in scrollback with a quiet child was copying up to
        # HISTORY_LINES entries per repaint. New output still costs one copy per
        # burst, which is what the uncached version cost per PAINT.
        sig = (getattr(top, "pushed", 0), len(top))
        if sig != self._vs_sig or self._vs_cache is None:
            self._vs_sig, self._vs_cache = sig, list(top)
        return self._vs_cache

    def _visible_line(self, r: int, hist, off: int):
        """Line shown at visual row r: history tail first, then the live
        screen shifted down by the offset."""
        if not off or hist is None:
            return self.screen.buffer[r]
        idx = len(hist) - off + r
        if idx < len(hist):
            return hist[idx]
        return self.screen.buffer[idx - len(hist)]

    def _line_at_row(self, r: int, hist: list, off: int):
        """_visible_line for any row, on screen or not: a selection carried
        off screen by a scroll still has its rows. None past either end of
        history plus the live screen, rather than the blank line pyte's
        buffer would make up for a row it doesn't have."""
        idx = len(hist) - off + r
        if idx < 0:
            return None
        if idx < len(hist):
            return hist[idx]
        idx -= len(hist)
        return self.screen.buffer[idx] if idx < self.screen.lines else None

    def _row_line(self, r: int):
        """_line_at_row at the current offset. History is copied only when
        row r or the view is in it, so a live selection read while an agent
        streams doesn't rebuild the history list on every call."""
        hist = (self._history_list() if r < 0 or self._scroll_offset
                else [])
        return self._line_at_row(r, hist, min(self._scroll_offset, len(hist)))

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
        self._set_offset(max(0, min(len(self.screen.history.top),
                                    self._scroll_offset + lines)))

    def _set_offset(self, new: int) -> None:
        """Move the view to scroll offset `new`. The selection moves with
        the text under it, a row down per line scrolled back, the way an
        editor's does. A drag that auto-scrolls carries its start off screen
        this way and still copies it, and a selection survives a wheel."""
        delta = new - self._scroll_offset
        if not delta:
            return
        self._scroll_offset = new
        self._shift_selection(delta)
        self._notify_view(immediate=True)
        self.update()

    def _shift_selection(self, delta: int) -> None:
        """Move the selection `delta` visual rows down, to follow its text
        when the view moves. Every write of _scroll_offset that moves the
        text under the view goes through here."""
        if not delta:
            return
        if self._sel_anchor is not None:
            self._sel_anchor = (self._sel_anchor[0] + delta,
                                self._sel_anchor[1])
        if self._sel_end is not None:
            self._sel_end = (self._sel_end[0] + delta, self._sel_end[1])

    def scroll_offset(self) -> int:
        return self._scroll_offset

    def font_size(self) -> int:
        return self._font.pixelSize()

    def set_font_size(self, px: int, now: bool = False) -> None:
        """`now` resizes the pty at once (Ctrl+wheel zoom). Everything else
        waits out the resize debounce, so three clicks on A+ make the child
        redraw once."""
        px = max(7, min(40, int(px)))
        if px == self._font.pixelSize():
            return
        self._font.setPixelSize(px)
        self._apply_metrics("now" if now else "soon")

    def font_families(self) -> list[str]:
        return list(self._font.families())

    def set_font_families(self, families) -> None:
        """Options > Terminal font: the families to draw with, most wanted
        first. Qt walks the list for a glyph the first one lacks."""
        families = list(families) or ["Consolas"]
        if families == self._font.families():
            return
        self._font = self._base_font(families, self._font.pixelSize(),
                                     self._letter_px)
        self._apply_metrics("soon")

    def letter_spacing(self) -> int:
        return self._letter_px

    def set_letter_spacing(self, px: int) -> None:
        """Options > Letter spacing: extra px added to every cell."""
        px = max(0, min(ui_theme.MAX_LETTER_PX, int(px)))
        if px == self._letter_px:
            return
        self._letter_px = px
        self._font = self._base_font(self._font.families(),
                                     self._font.pixelSize(), px)
        self._apply_metrics("soon")

    @staticmethod
    def _base_font(families, px: int, letter_px: int) -> QFont:
        font = QFont(list(families))
        font.setPixelSize(px)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        # Qt adds absolute spacing to every glyph's advance, single
        # characters included, so the "M" _apply_metrics measures grows by
        # exactly `letter_px` and a drawText run still lands on the grid.
        # Zero is left unset rather than set to 0, so the default font is
        # the one this view always had.
        if letter_px:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_px)
        return font

    def _apply_metrics(self, resize: str = "soon") -> None:
        """Measure the cell from the base font and drop everything measured
        with the old one. `resize` is "now" (the pty learns its new size
        before this returns), "soon" (through the 120 ms debounce, for the
        setters: every rows/cols change resizes the pty and reprojects the
        restored screen) or "" (the constructor, before a screen exists)."""
        metrics = QFontMetricsF(self._font)
        self._cell_w = metrics.horizontalAdvance("M")
        self._cell_h = metrics.height()
        self._ascent = metrics.ascent()
        # stroke of a light box-drawing line: Consolas draws 1 px at 13 px
        self._box_px = max(1, round(self._font.pixelSize() * 0.08))
        self._grid_glyph_cache.clear()
        self._style_fonts.clear()
        if not resize:
            return
        if resize == "now":
            self._resize_timer.stop()
            self._apply_resize()
        else:
            self._resize_timer.start()
        self.update()

    # -------------------------------------------------------------- find ---

    def set_find_query(self, text: str) -> None:
        """Search the scrollback and the live screen for `text` and make the
        match nearest the view active, scrolling to it when it is off screen.
        A refined query keeps the active match when it still matches, as
        Chrome does, unless the user scrolled it out of sight. An empty
        `text` is the same as clear_find."""
        if not text:
            self.clear_find()
            return
        anchor = self._find_anchor(visible_only=True)
        self._find_re = re.compile(re.escape(text), re.IGNORECASE)
        self._find_timer.stop()
        self._find_scan()
        self._find_pick(anchor)
        self._reveal_find()
        self.update()
        self._emit_find()

    def find_step(self, delta: int) -> None:
        """Make the next (delta 1) or previous (-1) match active, wrapping
        at either end, and scroll to it."""
        if self._find_timer.isActive():
            # stepping on matches that are up to FIND_RESCAN_MS stale could
            # land on a row the output has already rewritten, or miss the
            # first match the agent just printed
            self._find_timer.stop()
            self._find_refresh()
        if not self._find_matches:
            return
        self._find_active = (self._find_active + delta) % len(self._find_matches)
        self._reveal_find()
        self.update()
        self._emit_find()

    def clear_find(self) -> None:
        """Close the search: no highlights, no rescans. The view stays
        wherever the last match put it, as Chrome's page does. The row text
        cache stays, so the next Ctrl+F doesn't read all of history again."""
        self._find_re = None
        self._find_timer.stop()
        self._find_matches = []
        self._find_rows = {}
        self._find_active = -1
        self.update()
        self._emit_find()

    def find_state(self) -> tuple[int, int]:
        """(1-based active match or 0, match count)."""
        if not self._find_matches:
            return 0, 0
        return self._find_active + 1, len(self._find_matches)

    def find_matches(self) -> list[tuple[int, int, int]]:
        """Every match as (absolute line, first col, last col), oldest
        first."""
        return list(self._find_matches)

    def find_lines(self) -> tuple[list[int], int]:
        """(every absolute line holding a match, oldest first; the active
        match's line, or -1). The scrollbar's ticks: one per line, since a
        one-letter query can match 30000 times in a full history."""
        active = -1
        if 0 <= self._find_active < len(self._find_matches):
            active = self._find_matches[self._find_active][0]
        return list(self._find_rows), active

    def set_find_inset(self, px: int) -> None:
        """Rows whose top is less than `px` down count as hidden when
        deciding whether a match needs scrolling to (the find bar covers
        them)."""
        self._find_inset = max(0, int(px))

    def _find_dirty(self) -> None:
        # Armed only when idle, like _notify_view: a restart per chunk would
        # starve the rescan for as long as the agent streams.
        if self._find_re is not None and not self._find_timer.isActive():
            self._find_timer.start()

    def _find_refresh(self) -> None:
        """Rescan after the content changed, keeping the active match by
        position. Never scrolls: the user may be reading somewhere else."""
        if self._find_re is None:
            return
        anchor = self._find_anchor()
        self._find_scan()
        self._find_pick(anchor)
        self.update()
        self._emit_find()

    def _find_anchor(self, visible_only: bool = False) -> tuple[int, int]:
        """(line, col) the next pick starts from: the active match, or with
        none the first row the find bar leaves uncovered. With
        `visible_only` an active match the user scrolled away from doesn't
        count: a new query starts from what is on screen."""
        if 0 <= self._find_active < len(self._find_matches):
            line, c0, _c1 = self._find_matches[self._find_active]
            if not visible_only or self._find_on_screen(line):
                return line, c0
        return self.abs_line_at_row(self._find_top_row()), 0

    def _find_on_screen(self, abs_line: int) -> bool:
        """True when the line shows on screen and the find bar doesn't
        cover it."""
        row = abs_line - self.abs_line_at_row(0)
        return self._find_top_row() <= row < self.screen.lines

    def _find_top_row(self) -> int:
        """First visual row the find bar leaves uncovered."""
        if self._find_inset <= CELL_PAD_Y:
            return 0
        hidden = math.ceil((self._find_inset - CELL_PAD_Y)
                           / max(1.0, self._cell_h))
        return min(self.screen.lines - 1, hidden)

    def _find_scan(self) -> None:
        rx = self._find_re
        cols = self.screen.columns
        top = self.screen.history.top
        pushed = self.history_pushed()
        first = pushed - len(top)
        old, cache = self._find_cache, {}
        matches: list[tuple[int, int, int]] = []
        rows: dict[int, list[tuple[int, int, int]]] = {}

        def spans(line, text, colmap):
            out = []
            for m in rx.finditer(text):
                if m.end() <= m.start():
                    continue
                c0 = colmap[m.start()] if colmap else m.start()
                c1 = colmap[m.end() - 1] if colmap else m.end() - 1
                if c1 + 1 < cols and line[c1 + 1].data == "":
                    c1 += 1         # the wide glyph's second cell
                out.append((c0, c1))
            return out

        def add(abs_line, found):
            if found:
                n = len(matches)
                rows[abs_line] = [(c0, c1, n + k)
                                  for k, (c0, c1) in enumerate(found)]
                matches.extend((abs_line, c0, c1) for c0, c1 in found)

        for i, line in enumerate(top):
            # [line, cols, text, colmap, query, spans]: a history line's
            # spans are kept per query too, so a streaming agent's rescans
            # only run the regex over new lines and the live screen
            hit = old.get(id(line))
            if hit is None or hit[0] is not line or hit[1] != cols:
                hit = [line, cols, *_find_row_text(line, cols), None, ()]
            if hit[4] is not rx:
                hit[4], hit[5] = rx, spans(line, hit[2], hit[3])
            cache[id(line)] = hit
            add(first + i, hit[5])
        buf = self.screen.buffer
        for r in range(self.screen.lines):
            line = buf[r]
            add(pushed + r, spans(line, *_find_row_text(line, cols)))
        self._find_cache = cache
        self._find_matches = matches
        self._find_rows = rows

    def _find_pick(self, anchor) -> None:
        """Active = the first match at or after `anchor` (line, col), else
        the last one before it."""
        matches = self._find_matches
        if not matches:
            self._find_active = -1
            return
        for i, (line, c0, _c1) in enumerate(matches):
            if (line, c0) >= anchor:
                self._find_active = i
                return
        self._find_active = len(matches) - 1

    def _reveal_find(self) -> None:
        """Center the active match when it is off screen or under the bar."""
        if not 0 <= self._find_active < len(self._find_matches):
            return
        line = self._find_matches[self._find_active][0]
        if not self._find_on_screen(line):
            self.scroll_to_abs(line, lead=self.screen.lines // 2)

    def _emit_find(self) -> None:
        self.findChanged.emit(*self.find_state())

    # ------------------------------------------------------------ sizing ---

    def sizeHint(self) -> QSize:
        return QSize(int(self._cell_w * 80), int(self._cell_h * 24))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_timer.start()  # debounce retile storms

    def resize_pending(self) -> bool:
        """Is a debounced resize still waiting to reach the screen?"""
        return self._resize_timer.isActive()

    def flush_resize(self) -> None:
        """Apply a pending debounced resize NOW.

        The 120ms debounce exists so a retile storm costs one reprojection, but
        there is one moment where waiting is wrong: just before a pty child is
        SPAWNED. A child told its width 120ms late has already painted at the
        wrong one, and nothing downstream can undo that — the child hard-wraps
        its own text, so those lines keep the width they were written for no
        matter how the raw stream is projected afterwards. The callers are
        `MainWindow.settle_layout` at launch,
        `MainWindow._on_terminal_added` for a card added later, and
        `MainWindow._start_launch_queued` for an agent the launch queued
        behind the active workspace (it starts only once
        `PageStack.layout_pending` is clear)."""
        self._resize_timer.stop()
        self._apply_resize()

    def _apply_resize(self) -> None:
        cols = max(10, int((self.width() - 2 * CELL_PAD_X) / self._cell_w))
        rows = max(2, int((self.height() - 2 * CELL_PAD_Y) / self._cell_h))
        if (rows, cols) == (self.screen.lines, self.screen.columns):
            return
        self.screen.resize(rows, cols)  # pyte order: (lines, columns)
        self.sizeChanged.emit(rows, cols)
        self._notify_view(immediate=True)   # page size changed
        self.update()
        self._find_dirty()

    def project_size(self, rows: int, cols: int) -> None:
        """Resize the emulated screen only, for a replay that has to read the
        stream at the size each part was drawn for (see
        TerminalAgent.replay_geometry). No sizeChanged: the child and the
        agent's size record must not hear about it, and the replay puts the
        widget's own size back when it ends."""
        if (rows, cols) != (self.screen.lines, self.screen.columns):
            self.screen.resize(rows, cols)
            self._find_dirty()

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
        # whatever took focus may keep the release
        self._mouse_selecting = False
        self._stop_drag_scroll()
        self._preedit = ""
        self._im_rect = None
        super().focusOutEvent(event)

    def hideEvent(self, event) -> None:
        # a workspace switch can hide the view mid-drag without taking its
        # focus, and the release then never reaches it
        self._mouse_selecting = False
        self._stop_drag_scroll()
        super().hideEvent(event)

    def inputMethodQuery(self, query):
        # The panels and IME candidate windows open next to this rectangle.
        # The view keeps the live screen's cursor `off` rows lower while
        # scrolled back, clamped so the popup stays over the card.
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            cur = self.screen.cursor
            col = max(0, min(self.screen.columns - 1, cur.x))
            row = max(0, min(self.screen.lines - 1,
                             cur.y + self._scroll_offset))
            x0, y0, x1, y1 = self.cell_bounds(col, row)
            return QRect(x0, y0, x1 - x0, y1 - y0)
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        if query == Qt.InputMethodQuery.ImHints:
            return (Qt.InputMethodHint.ImhNoPredictiveText
                    | Qt.InputMethodHint.ImhNoAutoUppercase)
        return super().inputMethodQuery(query)

    def inputMethodEvent(self, event) -> None:
        # Committed text (an emoji from Win+., a finished IME composition)
        # is typed input. A Win+V pick never comes here: the panel sends
        # Ctrl+V, which keyPressEvent already pastes. The preedit string is
        # only drawn (paintEvent), the child sees the text once it's
        # committed. A commit with a line break goes as a paste, or its
        # newline would reach Claude as Enter.
        text = event.commitString()
        if text:
            if self._input_selected:
                self._clear_input_selection(send=True)  # replace, as typing does
            if "\r" in text or "\n" in text:
                self._paste_text(
                    text.replace("\r\n", "\r").replace("\n", "\r"))
            else:
                self._snap_to_bottom()
                self.keyInput.emit(text)
                self._kick_snapshot(None, text)
        preedit = event.preeditString()
        if preedit != self._preedit:
            self._preedit = preedit
            self.update()
        event.accept()

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
            # Ctrl+T is MainWindow's new-terminal shortcut, the user's
            # pick over readline's transpose-chars and Claude's task list
            if ctrl and not shift and not alt and key == Qt.Key.Key_T:
                return super().event(e)
            # Ctrl+Up / Ctrl+Down maximize and restore the selected agent
            if ctrl and not shift and not alt \
                    and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                return super().event(e)
            # Ctrl+Tab / Ctrl+Shift+Tab move to the next / previous agent
            if ctrl and not alt \
                    and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                return super().event(e)
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

        # Ctrl+F opens the find bar, as in Chrome, except on the alternate
        # screen, where less and vim page forward with it. F3 / Shift+F3 step
        # through the matches, but only while a search is open: otherwise F3
        # is the child's key like any other F-key.
        if ctrl and not shift and not alt and key == Qt.Key.Key_F \
                and not self._alt_screen:
            self.findRequested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_F3 and not ctrl and not alt \
                and self._find_re is not None:
            self.find_step(-1 if shift else 1)
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
            self.set_font_size(self._font.pixelSize() + delta, now=True)
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
            # a wheel mid-drag grows the selection to the text that scrolled
            # under the pointer, as a browser does
            if (self._mouse_selecting
                    and event.buttons() & Qt.MouseButton.LeftButton
                    and self._sel_anchor is not None):
                self._sel_end = self._drag_cell(event.position())
        event.accept()

    def overlay_wheel(self, event) -> None:
        """A wheel that landed on a child laid over the view (the scrollbar,
        the find bar), handled as if it hit the view. Its position is moved
        into the view's coordinates first: with mouse tracking on, the agent
        hears the wheel at the cell under the pointer, and the child's own
        (0, 0) is not the view's."""
        self.wheelEvent(QWheelEvent(
            self.mapFromGlobal(event.globalPosition()),
            event.globalPosition(), event.pixelDelta(), event.angleDelta(),
            event.buttons(), event.modifiers(), event.phase(),
            event.inverted()))
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
        self._set_offset(0)

    # -------------------------------------------------- mouse / clipboard ---

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int((pos.x() - CELL_PAD_X) / self._cell_w)
        row = int((pos.y() - CELL_PAD_Y) / self._cell_h)
        col = max(0, min(self.screen.columns - 1, col))
        row = max(0, min(self.screen.lines - 1, row))
        return row, col

    def _drag_cell(self, pos) -> tuple[int, int]:
        """The cell a drag at `pos` selects to. Above the view that is the
        start of the top row and below it the end of the bottom row, so a
        drag that leaves the view takes whole rows whatever its x."""
        if pos.y() < 0:
            return 0, 0
        if pos.y() >= self.height():
            return self.screen.lines - 1, self.screen.columns - 1
        return self._cell_at(pos)

    def _drag_scroll_step(self, pos) -> int:
        """Lines one auto-scroll tick moves for a drag at `pos`: positive
        (back into history) above the view, negative (toward live) below
        it, 0 inside it. Also 0 on the alternate screen, where the app
        owns scrolling and the wheel doesn't scroll our history either."""
        if self._alt_screen:
            return 0
        y = pos.y()
        if y < 0:
            past, sign = -y, 1
        elif y >= self.height():
            past, sign = y - self.height() + 1, -1
        else:
            return 0
        ramp = min(past, DRAG_SCROLL_RAMP_PX) / DRAG_SCROLL_RAMP_PX
        return sign * (1 + round(ramp * (DRAG_SCROLL_MAX_LINES - 1)))

    def _drag_scroll_tick(self) -> None:
        """One step of a drag held past an edge: scroll, then pull the
        selection's end to the row that just scrolled in. A held pointer
        sends no moves, so the timer drives this, not mouseMoveEvent."""
        pos = self._drag_pos
        step = self._drag_scroll_step(pos) if pos is not None else 0
        if not step or self._sel_anchor is None:
            self._stop_drag_scroll()
            return
        before = self._scroll_offset
        self.scroll_by(step)
        if self._scroll_offset == before:   # oldest line or live reached
            self._stop_drag_scroll()
            return
        self._sel_end = self._drag_cell(pos)   # scroll_by queued the repaint

    def _stop_drag_scroll(self) -> None:
        self._drag_timer.stop()
        self._drag_pos = None

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
            self._mouse_selecting = True
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
            pos = event.position()
            self._sel_end = self._drag_cell(pos)
            # moving off the press cell makes this a DRAG (a local selection),
            # so a pending mouse-tracking forward is cancelled — the child never
            # sees the click, and the drag is ours to copy.
            if self._pending_fwd is not None and self._sel_end != self._pending_fwd:
                self._pending_fwd = None
            # past the top or bottom edge, history scrolls under the drag
            # until the pointer comes back or the button comes up
            if self._drag_scroll_step(pos):
                self._pending_fwd = None
                self._drag_pos = pos
                if not self._drag_timer.isActive():
                    self._drag_timer.start()
            else:
                self._stop_drag_scroll()
            self.update()
            super().mouseMoveEvent(event)
            return
        # no button: a release that never reached us (a popup took the
        # grab) must not leave the drag scrolling
        self._mouse_selecting = False
        self._stop_drag_scroll()
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
        if event.button() == Qt.MouseButton.LeftButton:
            self._mouse_selecting = False
            self._stop_drag_scroll()
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

    def _typing_span(self):
        """_input_block_span for Bionic reading, which has to cover one more
        case. Typed text that fills its last row to the edge leaves the
        caret alone at the start of the blank row below, and
        _input_block_span returns None for a blank caret row. The prompt
        then lost its exemption and the typed words went bold as the
        caret wrapped. Here a blank caret row takes the box of the row
        above it, when that box opens with a prompt glyph."""
        span = self._input_block_span()
        cy = self.screen.cursor.y
        if span is not None or cy < 1 \
                or self._row_content(cy) != (-1, -1):
            return span
        # its bottom is always cy - 1, since row cy is blank
        above = self._input_block_span(cy - 1)
        if above is None:
            return None
        first, _ = self._row_content(above[0])
        if first < 0 or self.screen.buffer[above[0]][first].data \
                not in _INPUT_PROMPTS:
            return None
        return above[0], cy

    def _input_block_span(self, cy=None):
        """(top, bottom) live-screen rows of the contiguous input box, or None.
        `cy` is the caret row, the cursor's own unless a caller asks about
        another row.
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
        if cy is None:
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
        outside the box -- never nudges the prompt.

        THE waiting_probe() guard is what actually keeps this from firing into
        something that isn't a normal readline prompt: while the agent is
        parked on an interactive menu it can't drive via arrow keys
        (AskUserQuestion, ExitPlanMode, a classic permission menu), sending
        arrows there is what corrupts/dismisses it -- see set_waiting_probe.
        This is load-bearing: when Claude's classic/"default" TUI renderer is
        active (the `ui.terminal_scrollback` default), it never negotiates
        mouse tracking, so EVERY click -- including one landing on such a
        menu -- reaches this function; nothing upstream filters it out.

        The span check now also runs BEFORE the same-row fast path rather
        than after. This is a narrower, independent correctness fix, not the
        thing that stops menu corruption: `_input_block_span()`, whenever it
        returns non-None, always spans at least the cursor's own row (`top`/
        `bottom` both start at `cy`), so a clicked row equal to `cy` was
        already going to pass the check in the common case. The one real
        behavior change is a click on `cy`'s row when that row is BLANK
        (`_input_block_span()` returns None then) -- the old ordering still
        fired arrows at it; now it correctly does nothing."""
        if self._scroll_offset:
            return  # caret only meaningful on the live screen
        if self._waiting_probe():
            return  # an interactive menu is open -- never touch its caret
        span = self._input_block_span()
        if span is None or not (span[0] <= row <= span[1]):
            return  # off the input box: leave the child's caret alone
        cy = self.screen.cursor.y
        cx = self.screen.cursor.x
        if row == cy:
            self._emit_arrows("C" if col > cx else "D", abs(col - cx))
            return
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
        box's first row is never counted as deletable input.

        Also fails closed while waiting_probe() is true: _input_block_span's
        heuristics can misjudge a printed interactive menu as part of the
        input box (see _reposition_cursor), and unlike a plain click this
        function sends REAL Backspace bytes to the child regardless of
        whether the caret-repositioning it does first actually moved
        anything -- so this must not rely solely on _reposition_cursor's own
        guard."""
        if self._waiting_probe():
            return False
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
            line = self._row_line(r)
            c = max((i for i in range(cols)
                     if line is not None and line[i].data not in ("", " ")),
                    default=0)
        # the snap above carried an end that a drag scrolled into history
        # off the top (row < 0). It stays there and moves from there, but a
        # key never pushes the end further into history than it already was,
        # where the user can't see what it takes.
        top = min(0, self._sel_end[0])
        self._sel_end = (max(top, min(rows - 1, r)), max(0, min(cols - 1, c)))
        self.update()

    def _word_col(self, r: int, c: int, direction: int) -> int:
        """Column one word away from (r, c) in `direction` (+1 right / -1 left),
        for Ctrl+Shift+Arrow selection. Same whitespace-run notion as the
        double-click word select (_word_at). Row r may sit off screen, where
        a drag's auto-scroll left the selection's end."""
        line = self._row_line(r)
        if line is None:
            return c
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

    def set_waiting_probe(self, probe) -> None:
        """Wire the authoritative "agent is waiting on an interactive menu"
        signal (agent.is_waiting()) into _reposition_cursor's safety gate. See
        that function for why this is needed: it is the same hook-driven
        signal TerminalCard already trusts over screen scraping for the
        analogous Enter-key/milestone guard, because a scrape alone misses
        AskUserQuestion/ExitPlanMode."""
        self._waiting_probe = probe

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
        # copy what is painted, plus any rows a scroll carried off screen
        hist = (self._history_list() if r0 < 0 or self._scroll_offset
                else [])
        off = min(self._scroll_offset, len(hist))
        lines = []
        for r in range(r0, r1 + 1):
            row = self._line_at_row(r, hist, off)
            if row is None:     # history wiped under the selection
                continue
            start = c0 if r == r0 else 0
            end = c1 if r == r1 else self.screen.columns - 1
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

    def _last_content_row(self) -> int:
        """Bottom-most non-blank screen row, or -1 when the screen is empty."""
        for r in range(self.screen.lines - 1, -1, -1):
            if self._row_content(r) != (-1, -1):
                return r
        return -1

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
        nudge (auto-continue's "Continue") and scheduled sends both reach
        worker.write and never pass through a key event.

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
        when Claude's classic renderer can scroll for an autocomplete dropdown
        and fail to scroll back, and by the time this fires the child's redraw
        has long since arrived and been painted. The output-driven _gap_timer
        covers the keystroke-free cases; this call costs nothing on top of it
        (the edge guard makes the second one a no-op) and keeps the typing
        path covered if a child ever strands the box and then emits nothing."""
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

        Runs on a debounced OUTPUT settle (_gap_timer, re-armed by every
        feed) as well as after a typing burst, because the commonest way to
        strand the box is a slash-command menu (/model, /effort) closing on
        Esc -- pure child output, not a keystroke this widget ever sees.

        The gap is measured from the bottom-most row with anything on it,
        NOT by walking down from the box looking for blank rows. The shipped
        version did the latter and therefore never fired once on a real
        screen: directly under the '>' row Claude draws the box's bottom
        BORDER, and the footer hint wraps onto a second row that matches no
        known hint text, so the blank scan aborted on the box's own chrome
        every single time. The suite missed it for the same reason
        reply_anchor_line's bug survived -- the fixture drew a box with no
        border and a one-row footer, a screen Claude never paints.

        Fires ONLY on the EDGE: a bottom row that moved further from the
        bottom of the screen than the last check. A short conversation that
        legitimately has blank space below its footer gets checked once,
        finds nothing to fix next time (same row), and is never repainted
        again -- that edge guard plus the empty-history guard below are what
        keep normal use free of any repeated resize blip."""
        if self._scroll_offset or self._alt_screen:
            return  # only the live tail can be "stranded"
        if not len(self.screen.history.top):
            # A screen that has never scrolled a line off cannot have been
            # scrolled up by a dropdown. A fresh or freshly cleared
            # conversation legitimately sits high with blank space under it,
            # and its footer moves down a row every turn -- without this the
            # output trigger would ask for a resize once per turn.
            self._input_gap_row = None
            return
        span = self._input_block_span()
        if span is None:
            self._input_gap_row = None
            return
        top, bottom = span
        first, _ = self._row_content(top)
        if first < 0 or self.screen.buffer[top][first].data not in _INPUT_PROMPTS:
            # _input_block_span keeps top = cy when there is no prompt glyph,
            # so mid-reply -- caret on output, blank rows below -- this would
            # otherwise read as a stranded box. The keystroke trigger hid
            # that; the output trigger would not.
            self._input_gap_row = None
            return
        below = bottom + 1
        if below >= self.screen.lines or not (self._row_is_rule(below)
                                              or self._row_is_input_footer(below)):
            # Claude paints the box's bottom border (or, borderless, its footer
            # hint) DIRECTLY under the input with no blank line between -- that
            # is what _input_block_span's own scan stops on. An OPEN menu also
            # ends up looking like a box otherwise: its highlighted row starts
            # with the same '❯' the prompt does, and a tall one leaves the span
            # close enough to the last content row to pass the chrome bound
            # below. Measured on a real /model menu: span bottom 23, row 24
            # BLANK, so this is the discriminator.
            self._input_gap_row = None
            return
        edge = self._last_content_row()
        if edge < bottom or edge - bottom > _INPUT_CHROME_ROWS:
            self._input_gap_row = None
            return  # real content below the box -- a menu is still open
        gap = (self.screen.lines - 1) - edge
        if gap <= _INPUT_GAP_TOLERANCE:
            self._input_gap_row = None
            return
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

    def _build_context_menu(self, row: int, col: int) -> QMenu:
        """Assemble the right-click menu for the cell under the pointer, and
        return it UNSHOWN -- contextMenuEvent's exec() blocks, so this split is
        what lets the headless suite read the actions and trigger one.

        A cell carrying a link (the SAME _link_at that Ctrl+click uses, so
        the two can never disagree about what one is) gets the file actions
        prepended, worded exactly as the Agent/File Map's menu words them. The
        path copied is the RESOLVED absolute one _link_at returns, not the token
        as printed: a repo-relative 'app/widgets/sidebar.py' pastes into
        Explorer's address bar only as an absolute path. Nothing here sends the
        child anything, so it needs no waiting_probe() gate."""
        from .. import fsopen

        menu = QMenu(self)
        target = self._link_at(row, col)
        if target:
            kind, value = target
            if kind == "file":
                menu.addAction("Open", lambda: fsopen.open_path(value))
                menu.addAction("Open with...", lambda: fsopen.open_with(value))
                menu.addAction("Reveal in folder",
                               lambda: fsopen.reveal_in_folder(value))
                menu.addAction(
                    "Copy path",
                    lambda: QGuiApplication.clipboard().setText(value))
            else:
                menu.addAction("Open link", lambda: fsopen.open_url(value))
                menu.addAction(
                    "Copy link address",
                    lambda: QGuiApplication.clipboard().setText(value))
            menu.addSeparator()
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        can_paste = bool(cb.text()) or (md is not None and md.hasImage())
        act_copy = menu.addAction("Copy", self.copy_selection)
        act_copy.setEnabled(self._selection_range() is not None)
        act_paste = menu.addAction("Paste", self.paste_clipboard)
        act_paste.setEnabled(can_paste)
        menu.addSeparator()
        menu.addAction("Select all", self.select_all)
        return menu

    def contextMenuEvent(self, event):
        menu = self._build_context_menu(*self._cell_at(event.pos()))
        menu.exec(event.globalPos())

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

    def _word_start_rows(self, lines, hist, off):
        """Per visible row, a mask with a 1 in each column Bionic reading
        draws in bold and a 2 in the rest of each word (word_spans), or
        None for a row with no word. None for the whole
        screen while the switch is off or the alternate screen is up: vim
        and less own every cell there.

        Segmenting a 120x50 screen of prose costs about 5 ms, a third of a
        whole paint, against 0.24 ms to look the rows up. So each row's
        mask is kept, keyed by its cells (text and bold together: a row
        the child re-bolds is a new key) and the cell above it. The cache
        holds only the rows on screen, so scrolling by a line segments one
        row."""
        if not TerminalView._word_starts_enabled or self._alt_screen:
            if self._ws_rows or self._ws_static:
                self._ws_rows, self._ws_static = {}, {}
            return None
        if self._ws_font != self._font:   # A+/A- sets the size in place
            self._ws_static = {}
            self._ws_font = QFont(self._font)
        cols = self.screen.columns
        # the row above the view, so a word wrapped onto the top row gets
        # its prefix only once (word_spans' prev_row_tail)
        above = None
        if hist is not None and off:
            if len(hist) > off:
                above = hist[len(hist) - off - 1]
        elif self.screen.history.top:
            above = self.screen.history.top[-1]
        tail = above[cols - 1].data if above is not None else None
        old, cache = self._ws_rows, {}
        rows = []
        for line in lines:
            cells = tuple(line[c] for c in range(cols))
            key = (cells, tail)
            mask = old.get(key, False)
            if mask is False:
                mask = None
                spans = word_spans(cells, tail)
                if spans:
                    buf = bytearray(cols)
                    for a, b, e in spans:
                        buf[a:b] = b"\x01" * (b - a)
                        buf[b:e] = b"\x02" * (e - b)
                    mask = bytes(buf)
            cache[key] = mask
            rows.append(mask)
            tail = cells[-1].data if cells else None
        self._ws_rows = cache
        # The live input rows, where a prefix would jump about as the user
        # types. _typing_span is a Claude-shaped guess in live-screen rows
        # (visible row = live row + off); when it finds nothing, every
        # row keeps its prefixes. Under a block of output it scans every row
        # above the caret twice, so on the live view its answer is kept
        # until the rows' text (the link signature) or the caret row moves.
        if off < len(rows):
            if off:
                span = self._typing_span()
            else:
                key = (self._link_sig, self.screen.cursor.y)
                if self._ws_input[0] != key:
                    self._ws_input = (key, self._typing_span())
                span = self._ws_input[1]
            if span is not None:
                for r in range(span[0] + off,
                               min(span[1] + off + 1, len(rows))):
                    rows[r] = None
        # what the view treats as a link keeps its regular weight, whatever
        # the token rules made of it
        for r, c0, c1 in self._link_spans:
            if r < len(rows) and rows[r] is not None:
                mask = bytearray(rows[r])
                mask[c0:c1 + 1] = bytes(c1 - c0 + 1)
                rows[r] = bytes(mask)
        return rows

    def _draw_piece(self, painter, line, i: int, j: int, y: float, starts,
                    italic: bool, underline: bool,
                    text: str | None = None) -> None:
        """drawText one grid-safe piece, cells [i, j) of `line`, with the
        painter holding the piece's font and pen. Cells `starts` marks (a
        row of _word_start_rows, or None) with a 1 go out in bold, and at
        a Contrast level that fades the rest of a word, the 2s at that
        opacity. `text` is the piece's text when the caller has already
        joined it.

        One pass per style, never one call per word: the regular text goes
        out with blanks where the other cells are, then the faded text and
        the bold text the same way. A grid-safe piece holds only glyphs
        that advance one cell, and _run_font's bold advances the same,
        blanks included, so every pass lands on the grid. The underline
        comes from the regular pass only, so it runs unbroken at one
        thickness.

        Measured on a 120x50 screen of prose with a find query showing, a
        drawText per stretch made a paint cost about 2x the plain one, a
        second full-width drawText about 1.45x, and the bold pass as a
        QStaticText about 1.25x: the bold strings repeat paint after paint,
        and the static text keeps their layout. At Contrast 1 the bold
        pass paints the same pixels as the child's own bold would
        (tests/smoke/word_starts.py checks it)."""
        x = int(CELL_PAD_X + i * self._cell_w)
        base = int(y + self._ascent)
        stroke, fade = WORD_STRENGTHS[TerminalView._word_strength - 1]
        if starts is None or (starts.find(1, i, j) < 0 and (
                fade >= 1 or starts.find(2, i, j) < 0)):
            if text is None:
                text = "".join(line[k].data or " " for k in range(i, j))
            painter.drawText(x, base, text)
            return
        # one cell per entry: a cell with a combining mark is two code
        # points, so the joined text can't be sliced by column
        cells = [line[k].data or " " for k in range(i, j)]
        # by mask value: 0 regular, 1 bold, 2 faded (None: drawn regular)
        passes = ([], [], [] if fade < 1 else None)
        for m in _MASK_RUNS.finditer(starts, i, j):
            a, b = m.start(), m.end()
            kind = starts[a]
            if passes[kind] is None:
                kind = 0
            for k, out in enumerate(passes):
                if out is None:
                    continue
                if k == kind:
                    out += cells[a - i:b - i]
                else:
                    out.append(" " * (b - a))
        text = "".join(passes[0])
        if underline or text.strip():
            painter.drawText(x, base, text)
        if passes[2] is not None:
            text = "".join(passes[2])
            if text.strip():
                pen = painter.pen()
                ink = QColor(pen.color())
                ink.setAlphaF(ink.alphaF() * fade)
                painter.setPen(ink)
                painter.drawText(x, base, text)
                painter.setPen(pen)
        text = "".join(passes[1])
        if text.strip():
            font = self._run_font(True, italic, False)
            painter.setFont(font)
            entry = self._ws_static.get((text, italic))
            if entry is None:
                if len(self._ws_static) > 8 * self.screen.lines:
                    self._ws_static = {}
                st = QStaticText(text)
                st.setTextFormat(Qt.TextFormat.PlainText)
                # placed by its top edge, so the baseline lands where
                # drawText would put it only with the bold font's own ascent
                entry = (st, QFontMetricsF(font).ascent())
                self._ws_static[(text, italic)] = entry
            top = base - entry[1]
            painter.drawStaticText(QPointF(x, top), entry[0])
            if stroke:
                # the same text again a little to the right thickens every
                # stem, in any font: the bundled ones stop at Bold
                painter.drawStaticText(
                    QPointF(x + stroke * self._font.pixelSize(), top),
                    entry[0])
        painter.setFont(self._run_font(False, italic, underline))

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

    def _run_font(self, bold: bool, italic: bool, underline: bool) -> QFont:
        """The font a run in this style paints with, its advance held to the
        cell.

        A family without a Bold or Italic face gets one Qt synthesizes, and
        that one can be wider. Measured on the real Windows platform with
        full hinting: Share Tech Mono's synthetic bold advances 8 px where
        the cell is 7 at 13 px, so a 40-char bold run painted 5.7 cells too
        wide, and OpenDyslexic Mono drifts at some sizes. So each style
        measures its own "M" and takes the difference back as letter
        spacing, which lands a 40-char run on exactly 40 cells. Bold ink is
        fatter and may end a pixel later, which is fine. The upright,
        regular-weight font is the one the cell was measured from, so it is
        never corrected."""
        key = (bool(bold), bool(italic), bool(underline))
        font = self._style_fonts.get(key)
        if font is None:
            font = QFont(self._font)
            if key[0]:
                font.setWeight(QFont.Weight.Bold)
            if key[1]:
                font.setItalic(True)
            if key[2]:
                font.setUnderline(True)
            if key[0] or key[1]:
                drift = (QFontMetricsF(font).horizontalAdvance("M")
                         - self._cell_w)
                if abs(drift) >= 0.01:
                    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing,
                                          self._letter_px - drift)
            self._style_fonts[key] = font
        return font

    def _is_grid_glyph(self, data: str, bold: bool = False,
                       italic: bool = False) -> bool:
        """True when this character advances exactly one cell in the font of
        its style (_run_font). A character the font LACKS is resolved by Qt's
        fallback to some other family whose advance is its own (Segoe UI
        Symbol renders the quadrant blocks 12px wide where the cell is 7).
        Since paintEvent draws a whole run with one drawText, letting Qt lay
        it out, such a glyph would drag every following character in the run
        off the grid — so the caller draws it ALONE at its own cell instead."""
        if data.isascii():  # the overwhelmingly common case, always in-font
            return True
        key = (data, bool(bold), bool(italic))
        cached = self._grid_glyph_cache.get(key)
        if cached is None:
            adv = QFontMetricsF(
                self._run_font(bold, italic, False)).horizontalAdvance(data)
            cached = abs(adv - self._cell_w) < 0.01
            self._grid_glyph_cache[key] = cached
        return cached

    def _draws_as_box(self, data: str, bold: bool, italic: bool) -> bool:
        """Paint this box-drawing character as geometry (_paint_box) rather
        than text: with letter spacing on, or when its glyph doesn't fill a
        cell, the font's own would leave a gap to the next one."""
        if data not in _BOX_ARMS:
            return False
        return bool(self._letter_px) or not self._is_grid_glyph(
            data, bold, italic)

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

    def _paint_box(self, painter, data: str, col: int, row: int,
                   color: QColor) -> None:
        """Draw one box-drawing character (_BOX_ARMS) as strokes on the cell
        grid. Every arm runs to the cell's edge, so it meets the arm of the
        cell beside it whatever the cell width."""
        x0, y0, x1, y1 = self.cell_bounds(col, row)
        w, h = x1 - x0, y1 - y0
        lw = self._box_px
        left, right, up, down = _BOX_ARMS[data]

        def strokes(lo: int, span: int, weight: int) -> list[tuple[int, int]]:
            """(start, thickness) of the strokes of one arm, centred in
            `span` px from `lo`: one light or heavy, or two light for a
            double line with a light gap between them."""
            if weight == 3:
                start = lo + (span - 3 * lw) // 2
                return [(start, lw), (start + 2 * lw, lw)]
            t = lw * weight
            return [(lo + (span - t) // 2, t)]

        # the junction box where the arms meet: as wide as the vertical
        # strokes (the horizontal ones on a horizontal-only glyph) and as
        # tall as the horizontal ones, so a corner closes with no notch
        vert = strokes(x0, w, max(up, down) or max(left, right))
        horz = strokes(y0, h, max(left, right) or max(up, down))
        jx0, jx1 = vert[0][0], vert[-1][0] + vert[-1][1]
        jy0, jy1 = horz[0][0], horz[-1][0] + horz[-1][1]
        if data in _BOX_ARCS:
            # a quarter circle from the horizontal arm's edge to the
            # vertical arm's, through the junction's middle
            cx, cy = (jx0 + jx1) / 2, (jy0 + jy1) / 2
            ex = x1 if right else x0
            ey = y1 if down else y0
            r = min(abs(ex - cx), abs(ey - cy))
            sx = 1 if right else -1
            sy = 1 if down else -1
            k = 1 - 0.5523   # cubic control offset that traces a circle
            path = QPainterPath(QPointF(ex, cy))
            path.lineTo(cx + sx * r, cy)
            path.cubicTo(QPointF(cx + sx * r * k, cy),
                         QPointF(cx, cy + sy * r * k),
                         QPointF(cx, cy + sy * r))
            path.lineTo(cx, ey)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(color, lw)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()
            return
        if left:
            for start, t in strokes(y0, h, left):
                painter.fillRect(x0, start, jx1 - x0, t, color)
        if right:
            for start, t in strokes(y0, h, right):
                painter.fillRect(jx0, start, x1 - jx0, t, color)
        if up:
            for start, t in strokes(x0, w, up):
                painter.fillRect(start, y0, t, jy1 - y0, color)
        if down:
            for start, t in strokes(x0, w, down):
                painter.fillRect(start, jy0, t, y1 - jy0, color)

    def paintEvent(self, event) -> None:
        global _perf_paint_s, _perf_paint_view
        t0 = time.perf_counter()
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
        # Bionic reading, after the link scan it leaves alone
        starts = self._word_start_rows(lines, hist, off)

        # selection highlight (drawn under the text)
        rng = self._selection_range()
        if rng is not None:
            (r0, c0), (r1, c1) = rng
            sel = QColor(Palette.SELECTION)
            sel.setAlpha(90)
            # only the rows on screen: a drag can reach far off it
            for r in range(max(r0, 0), min(r1, self.screen.lines - 1) + 1):
                sc = c0 if r == r0 else 0
                ec = c1 if r == r1 else self.screen.columns - 1
                painter.fillRect(
                    int(CELL_PAD_X + sc * cw), int(CELL_PAD_Y + r * ch),
                    int((ec - sc + 1) * cw), int(ch) + 1, sel)
        for row in range(self.screen.lines):
            line = lines[row]
            row_starts = starts[row] if starts else None
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
                    bold, italic = char.bold, char.italics
                    painter.setFont(
                        self._run_font(bold, italic, char.underscore))
                    # guarantee the glyph is readable on its real background,
                    # whatever color the child emitted (dark-tuned on a light
                    # theme, or vice versa)
                    ink = legible_color(fg, bg)
                    painter.setPen(ink)
                    # Split the run into grid-safe pieces. Block Elements, and
                    # box drawing whose glyph would leave a gap, are painted
                    # geometrically; a glyph the font lacks is drawn on its own
                    # so its fallback advance can't shear the row (see
                    # _BLOCK_RECTS / _BOX_ARMS / _is_grid_glyph). Ordinary
                    # text — virtually every run — still goes out in ONE
                    # drawText.
                    i = col
                    while i < run_end:
                        data = line[i].data or " "
                        if data in _BLOCK_RECTS or data in _BLOCK_SHADES:
                            self._paint_block(painter, data, i, row, ink)
                            i += 1
                            continue
                        if self._draws_as_box(data, bold, italic):
                            self._paint_box(painter, data, i, row, ink)
                            i += 1
                            continue
                        j = i
                        while j < run_end:
                            nd = line[j].data or " "
                            if nd in _CELL_SHAPES:
                                if (nd in _BLOCK_RECTS or nd in _BLOCK_SHADES
                                        or self._draws_as_box(
                                            nd, bold, italic)):
                                    break
                            elif not self._is_grid_glyph(nd, bold, italic):
                                break
                            j += 1
                        if j == i:  # a lone off-grid glyph, placed by hand
                            j = i + 1
                        self._draw_piece(painter, line, i, j, y, row_starts,
                                         italic, char.underscore)
                        i = j
                col = run_end

        # find matches (Ctrl+F) on Chrome's yellow, the active one orange.
        # Drawn OVER the text with the glyphs redrawn in dark ink, because
        # a pale box under the child's own (often pale) ink is unreadable.
        # A selection over a match is tinted again on top of its box, or
        # nothing would show what Ctrl+C copies.
        if self._find_rows:
            top_line = self.history_pushed() - off
            font_style = None   # the style the painter's font is in
            hit, active = QColor(FIND_HIT), QColor(FIND_ACTIVE)
            ink = QColor(FIND_INK)
            sel = QColor(Palette.SELECTION)
            sel.setAlpha(90)
            for row in range(self.screen.lines):
                spans = self._find_rows.get(top_line + row)
                if not spans:
                    continue
                line = lines[row]
                row_starts = starts[row] if starts else None
                y = CELL_PAD_Y + row * ch
                for c0, c1, idx in spans:
                    if c0 >= _cols:
                        continue
                    c1 = min(c1, _cols - 1)
                    x0, y0, _, y1 = self.cell_bounds(c0, row)
                    x1 = self.cell_bounds(c1, row)[2]
                    painter.fillRect(x0, y0, x1 - x0, y1 - y0,
                                     active if idx == self._find_active
                                     else hit)
                    if rng is not None and rng[0][0] <= row <= rng[1][0]:
                        s0 = max(c0, rng[0][1] if row == rng[0][0] else 0)
                        s1 = min(c1, rng[1][1] if row == rng[1][0]
                                 else _cols - 1)
                        if s0 <= s1:
                            sx0 = self.cell_bounds(s0, row)[0]
                            sx1 = self.cell_bounds(s1, row)[2]
                            painter.fillRect(sx0, y0, sx1 - sx0, y1 - y0, sel)
                    painter.setPen(ink)
                    # grid-safe pieces, one drawText each, as for the text
                    # itself: a glyph per call made a page of one-letter
                    # matches cost 90 ms a paint. A piece also ends where the
                    # cells' style does, so a match keeps its weight, slant
                    # and bold word starts under the box.
                    i = c0
                    while i <= c1:
                        cell = line[i]
                        data = cell.data or " "
                        bold, italic = cell.bold, cell.italics
                        if data in _BLOCK_RECTS or data in _BLOCK_SHADES:
                            self._paint_block(painter, data, i, row, ink)
                            i += 1
                            continue
                        if self._draws_as_box(data, bold, italic):
                            self._paint_box(painter, data, i, row, ink)
                            i += 1
                            continue
                        style = (bold, italic, cell.underscore)
                        j = i
                        while j <= c1:
                            nxt = line[j]
                            nd = nxt.data or " "
                            if ((nxt.bold, nxt.italics, nxt.underscore)
                                    != style or nd in _CELL_SHAPES
                                    or not self._is_grid_glyph(
                                        nd, bold, italic)):
                                break
                            j += 1
                        if j == i:  # a lone off-grid glyph, placed by hand
                            j = i + 1
                        text = "".join(line[k].data or " "
                                       for k in range(i, j))
                        if text.strip():
                            # _draw_piece hands the font back in `style`
                            if style != font_style:
                                painter.setFont(self._run_font(*style))
                                font_style = style
                            self._draw_piece(painter, line, i, j, y,
                                             row_starts, style[1], style[2],
                                             text)
                        i = j

        # reply-finished stamps: a dim date/time drawn into the RIGHT-hand
        # blank space of the row reply_anchor_line() picked out (Claude's own
        # "for Ns" footer), never a new row of our own -- pyte's screen has no
        # room to insert one without reflowing every anchor below it. Right-
        # aligned and skipped outright when the row's own content runs too
        # close to the edge, so a long footer or a narrow terminal never
        # collides with it (better to miss a stamp than draw over real
        # output).
        if self._reply_marks and TerminalView._reply_stamps_enabled:
            stamp_map = {ln: txt for ln, txt in self._reply_marks}
            pushed = self.history_pushed()
            # the run font carries the letter spacing, so the stamp's
            # len(text) columns below are the cells it covers
            stamp_font = self._run_font(False, True, False)
            for row in range(self.screen.lines):
                text = stamp_map.get(pushed - off + row)
                if text is None:
                    continue
                ln = lines[row]
                last = -1
                for c in range(_cols - 1, -1, -1):
                    if (ln[c].data or " ") != " ":
                        last = c
                        break
                start_col = _cols - len(text) - 1
                if start_col <= last + 2:
                    continue
                painter.setFont(stamp_font)
                ink = legible_color(QColor(Palette.CARDHEAD_SUB),
                                    QColor(Palette.BG_CONSOLE))
                painter.setPen(ink)
                painter.drawText(int(CELL_PAD_X + start_col * cw),
                                 int(CELL_PAD_Y + row * ch + self._ascent),
                                 text)

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
                data = ch_cell.data
                under = QColor(Palette.BG_CONSOLE)
                # the cell's own style and word-start bold, drawn as the
                # main pass drew it: the painter still holds whatever font
                # the last pass left
                if data in _BLOCK_RECTS or data in _BLOCK_SHADES:
                    self._paint_block(painter, data, cursor.x, cursor.y, under)
                elif self._draws_as_box(data, ch_cell.bold, ch_cell.italics):
                    self._paint_box(painter, data, cursor.x, cursor.y, under)
                elif data.strip():
                    painter.setPen(under)
                    cur = starts[cursor.y] if starts else None
                    painter.setFont(self._run_font(
                        ch_cell.bold or bool(cur and cursor.x < len(cur)
                                             and cur[cursor.x] == 1),
                        ch_cell.italics, ch_cell.underscore))
                    painter.drawText(int(cx), int(cy + self._ascent), data)
            else:  # hollow box when unfocused
                painter.setPen(QColor(Palette.ACCENT_ORANGE))
                painter.drawRect(int(cx), int(cy), max(1, int(cw) - 1), int(ch) - 1)

        # an IME composition sits over the cursor, underlined, until it is
        # committed; the child has seen none of it yet
        if self._preedit and self.hasFocus():
            prow = cursor.y + off
            if 0 <= prow < self.screen.lines:
                px = int(CELL_PAD_X + cursor.x * cw)
                py = int(CELL_PAD_Y + prow * ch)
                pfont = self._run_font(False, False, True)
                pw = int(QFontMetricsF(pfont).horizontalAdvance(self._preedit))
                painter.fillRect(px, py, max(1, pw), int(ch),
                                 QColor(Palette.BG_CONSOLE))
                painter.setFont(pfont)
                painter.setPen(legible_color(QColor(Palette.CONSOLE_FG),
                                             QColor(Palette.BG_CONSOLE)))
                painter.drawText(px, int(py + self._ascent), self._preedit)

        # (the old "up-arrow N" scrolled-back badge lived here, at
        # width() - tw - 20 -- exactly where TerminalScrollBar now sits. The
        # thumb says the same thing continuously and in the right place, so the
        # badge would only be a second, worse readout fighting it for pixels.)
        painter.end()

        # Qt reads the cursor rectangle at focus-in and keeps it, so tell it
        # whenever a repaint moved the cursor (new output, scrolling).
        if self.hasFocus():
            rect = self.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
            if rect != self._im_rect:
                self._im_rect = rect
                QGuiApplication.inputMethod().update(
                    Qt.InputMethodQuery.ImCursorRectangle)
        t0 = time.perf_counter() - t0
        if t0 > _perf_paint_s:
            _perf_paint_s, _perf_paint_view = t0, self
