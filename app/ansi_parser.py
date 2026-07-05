"""Minimal stateful ANSI parser: renders SGR (color/style), strips the rest.

QPlainTextEdit is not a VT emulator, so cursor-movement CSI, OSC titles,
and other escapes are dropped. SGR state persists across feed() calls and
partial escape sequences split across chunks are carried to the next call.
"""

import re
from dataclasses import dataclass, replace
from typing import Optional

from .ui_theme import ANSI_16

# Complete escape sequences: CSI ... final, OSC ... (BEL | ST), or a
# single-char escape. OSC may legitimately contain printable text.
_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[@-~]"                 # CSI
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC terminated by BEL or ST
    r"|[@-Z\\^_]"                      # two-char escapes (ESC + one)
    r")"
)
# A trailing prefix that *could* grow into a complete escape sequence.
_PARTIAL_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*"          # unterminated CSI
    r"|\][^\x07\x1b]*\x1b?"  # unterminated OSC (maybe mid-ST)
    r")?\Z"
)


@dataclass(frozen=True)
class CharStyle:
    fg: Optional[str] = None
    bg: Optional[str] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False


DEFAULT_STYLE = CharStyle()

# No real escape sequence is anywhere near this long. An unterminated OSC
# (e.g. from catting a binary file, or a flood-cap splice) would otherwise
# match "partial" forever and swallow the rest of the stream unboundedly.
MAX_CARRY = 4096


def _xterm_256(n: int) -> str:
    if n < 16:
        return ANSI_16[n]
    if n < 232:  # 6x6x6 color cube
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        conv = [0, 95, 135, 175, 215, 255]
        return f"#{conv[r]:02x}{conv[g]:02x}{conv[b]:02x}"
    v = 8 + (n - 232) * 10  # grayscale ramp
    return f"#{v:02x}{v:02x}{v:02x}"


class AnsiSgrParser:
    """feed(text) -> [(CharStyle, text)] segments; stateful across calls."""

    def __init__(self):
        self._style = DEFAULT_STYLE
        self._carry = ""

    def feed(self, text: str) -> list[tuple[CharStyle, str]]:
        text = self._carry + text
        self._carry = ""

        # Hold back a trailing partial escape for the next chunk.
        m = _PARTIAL_RE.search(text)
        if m and m.group(0):
            self._carry = m.group(0)
            text = text[: m.start()]
            if len(self._carry) > MAX_CARRY:
                self._carry = ""  # pathological unterminated sequence: drop it

        segments: list[tuple[CharStyle, str]] = []
        pos = 0
        for esc in _ESCAPE_RE.finditer(text):
            if esc.start() > pos:
                segments.append((self._style, text[pos:esc.start()]))
            seq = esc.group(0)
            if seq.startswith("\x1b[") and seq.endswith("m"):
                self._apply_sgr(seq[2:-1])
            pos = esc.end()
        if pos < len(text):
            segments.append((self._style, text[pos:]))
        return segments

    def flush(self) -> list[tuple[CharStyle, str]]:
        """Emit any held partial escape as literal text (process ended)."""
        if not self._carry:
            return []
        out = [(self._style, self._carry)]
        self._carry = ""
        return out

    # ------------------------------------------------------------- SGR ---

    def _apply_sgr(self, params: str) -> None:
        if not params:
            self._style = DEFAULT_STYLE
            return
        codes = []
        for part in params.split(";"):
            try:
                codes.append(int(part or "0"))
            except ValueError:
                return  # private-mode junk; ignore whole sequence
        i = 0
        style = self._style
        while i < len(codes):
            c = codes[i]
            if c == 0:
                style = DEFAULT_STYLE
            elif c == 1:
                style = replace(style, bold=True)
            elif c == 3:
                style = replace(style, italic=True)
            elif c == 4:
                style = replace(style, underline=True)
            elif c == 7:
                style = replace(style, reverse=True)
            elif c == 22:
                style = replace(style, bold=False)
            elif c == 23:
                style = replace(style, italic=False)
            elif c == 24:
                style = replace(style, underline=False)
            elif c == 27:
                style = replace(style, reverse=False)
            elif 30 <= c <= 37:
                style = replace(style, fg=ANSI_16[c - 30])
            elif 90 <= c <= 97:
                style = replace(style, fg=ANSI_16[c - 90 + 8])
            elif 40 <= c <= 47:
                style = replace(style, bg=ANSI_16[c - 40])
            elif 100 <= c <= 107:
                style = replace(style, bg=ANSI_16[c - 100 + 8])
            elif c == 39:
                style = replace(style, fg=None)
            elif c == 49:
                style = replace(style, bg=None)
            elif c in (38, 48):
                target = "fg" if c == 38 else "bg"
                if i + 1 < len(codes) and codes[i + 1] == 5 and i + 2 < len(codes):
                    style = replace(style, **{target: _xterm_256(codes[i + 2] & 0xFF)})
                    i += 2
                elif i + 1 < len(codes) and codes[i + 1] == 2 and i + 4 < len(codes):
                    r, g, b = (codes[i + 2] & 0xFF, codes[i + 3] & 0xFF,
                               codes[i + 4] & 0xFF)
                    style = replace(style, **{target: f"#{r:02x}{g:02x}{b:02x}"})
                    i += 4
            i += 1
        self._style = style
