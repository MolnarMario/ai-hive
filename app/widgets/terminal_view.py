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
"""

import re

import pyte
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication,
                           QPainter)
from PySide6.QtWidgets import QMenu, QWidget

from .. import ui_theme
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

HISTORY_LINES = 2000
CELL_PAD_X = 6   # left/right inner padding (real terminals aren't flush)
CELL_PAD_Y = 4   # top/bottom inner padding

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
# DECSET/DECRST private modes (CSI ? Pm h/l). pyte ignores these, but they
# decide how the wheel must behave (altscreen / mouse tracking / paste mode).
_PRIVATE_MODE_RE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")


class TerminalView(QWidget):
    keyInput = Signal(str)        # VT byte sequence for the PTY
    sizeChanged = Signal(int, int)  # rows, cols

    def __init__(self, rows: int = 30, cols: int = 100, parent=None,
                 font_px: int = 0):
        super().__init__(parent)
        self.setObjectName("TerminalView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._esc_carry = ""  # trailing partial escape between feed() calls
        self._scroll_offset = 0  # lines scrolled back into history (0 = live)
        self._sel_anchor = None  # (row, col) selection start, in screen coords
        self._sel_end = None     # (row, col) selection end
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

        self.screen = pyte.HistoryScreen(cols, rows, history=HISTORY_LINES,
                                         ratio=0.25)
        self.stream = pyte.Stream(self.screen)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._apply_resize)

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
        if m and m.group(0):
            self._esc_carry = m.group(0)
            data = data[: m.start()]
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
                elif tok in ("1000", "1002", "1003"):
                    self._mouse_tracking = on
                elif tok == "1006":
                    self._mouse_sgr = on
                elif tok == "1":
                    self._app_cursor_keys = on
                elif tok == "1004":
                    self._focus_reporting = on
        hist_before = len(self.screen.history.top)
        self.stream.feed(data)
        # while scrolled back, stay anchored to the CONTENT being read: new
        # lines entering history grow the offset so the view doesn't slide
        if self._scroll_offset:
            grown = len(self.screen.history.top) - hist_before
            if grown > 0:
                self._scroll_offset = min(self._scroll_offset + grown,
                                          len(self.screen.history.top))
        self.update()

    def reset(self) -> None:
        rows, cols = self.screen.lines, self.screen.columns
        self.screen = pyte.HistoryScreen(cols, rows, history=HISTORY_LINES,
                                         ratio=0.25)
        self.stream = pyte.Stream(self.screen)
        self._scroll_offset = 0
        self._bracketed_paste = False
        self._alt_screen = False
        self._mouse_tracking = False
        self._mouse_sgr = False
        self._app_cursor_keys = False
        self._focus_reporting = False
        self.update()

    def screen_text(self) -> str:
        """Plain text of the live screen (used by tests)."""
        return "\n".join(self.screen.display)

    def _view_state(self):
        """(history_list, clamped_offset) for composite rendering, or
        (None, 0) when live. Clamping guards a history wiped underneath us
        (e.g. a full reset) while scrolled back."""
        if not self._scroll_offset:
            return None, 0
        hist = list(self.screen.history.top)
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
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) \
                    or (ctrl and not shift) or (alt and not shift):
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

        # clipboard: Ctrl+V or Ctrl+Shift+V paste; Ctrl+Shift+C copies the
        # selection (plain Ctrl+C stays the interrupt signal, as in a terminal)
        if ctrl and key == Qt.Key.Key_V:
            self.paste_clipboard()
            event.accept()
            return
        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy_selection()
            event.accept()
            return

        seq = self._sequence_for(key, ctrl, shift, alt, event.text())
        if seq:
            self._snap_to_bottom()
            self.keyInput.emit(seq)
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

    def _snap_to_bottom(self) -> None:
        if self._scroll_offset:
            self._scroll_offset = 0
            self.update()

    # -------------------------------------------------- mouse / clipboard ---

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int((pos.x() - CELL_PAD_X) / self._cell_w)
        row = int((pos.y() - CELL_PAD_Y) / self._cell_h)
        col = max(0, min(self.screen.columns - 1, col))
        row = max(0, min(self.screen.lines - 1, row))
        return row, col

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._sel_anchor = self._cell_at(event.position())
            self._sel_end = self._sel_anchor
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._sel_anchor:
            self._sel_end = self._cell_at(event.position())
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # a plain click (no drag) clears the selection
        if self._sel_anchor == self._sel_end:
            self._sel_anchor = self._sel_end = None
            self.update()
        super().mouseReleaseEvent(event)

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
        self._sel_anchor = (0, 0)
        self._sel_end = (self.screen.lines - 1, self.screen.columns - 1)
        self.update()

    def paste_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        self._snap_to_bottom()
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        # bracketed paste (if the app enabled mode 2004, e.g. Claude Code) so a
        # multi-line/large paste is delivered as one block, not line-by-line
        if self._bracketed_paste:
            self.keyInput.emit("\x1b[200~" + text + "\x1b[201~")
        else:
            self.keyInput.emit(text)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        has_sel = self._selection_range() is not None
        can_paste = bool(QGuiApplication.clipboard().text())
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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(Palette.BG_CONSOLE))
        painter.setFont(self._font)

        hist, off = self._view_state()
        cw, ch = self._cell_w, self._cell_h

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
            line = self._visible_line(row, hist, off)
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

                x = CELL_PAD_X + col * cw
                width = (run_end - col) * cw
                if bg != QColor(Palette.BG_CONSOLE):
                    painter.fillRect(int(x), int(y), int(width) + 1,
                                     int(ch) + 1, bg)
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
                    painter.setPen(legible_color(fg, bg))
                    painter.drawText(int(x), int(y + self._ascent), text)
                col = run_end

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

        if off:  # scrolled-back badge, top-right
            painter.setFont(self._font)
            label = f"▲ {off}"
            metrics = QFontMetricsF(self._font)
            tw = metrics.horizontalAdvance(label)
            bx = self.width() - tw - 20
            bh = metrics.height() + 6
            painter.fillRect(int(bx), 5, int(tw + 14), int(bh),
                             QColor(0, 0, 0, 170))
            painter.setPen(QColor(Palette.ACCENT_ORANGE))
            painter.drawText(int(bx + 7), int(5 + 3 + metrics.ascent()), label)
        painter.end()
