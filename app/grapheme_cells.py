"""Grapheme clusters on a card's terminal screen: how many cells a character
takes, and when it joins the cell before it instead of taking its own.

pyte's `Screen.draw` measures every code point on its own and, on reaching a
zero-width one it can't combine (VS16, ZWJ, a zero-width space; their
combining class is 0), stops drawing and drops the rest of the text run. An
emoji written as base + VS16 (`⚔️`, `🛡️`, `⚠️`, `✔️`) therefore came out one
cell narrow and swallowed whatever followed it in the same run, such as the
closing quote after it or the rest of a diff row. Claude's own frames hid
it, because Claude writes each such emoji between absolute column moves, but
ConPTY's repaint from its buffer (after Claude's left-arrow agent view, for
one) writes it plainly with the rest of the row in the same run. A row that
came out short left the next one, which follows on through the wrap, to
start mid-row, so every row after it drew shifted right, and Claude's
relative cursor moves then left copies of the status line behind. conhost,
the emulator Windows Terminal shares, keeps the same bytes in place.

The model here is the one ConPTY's own console (OpenConsole 1.24, pywinpty
3.0.5) applies to the same bytes, measured with
GetConsoleScreenBufferInfo after each write:

- A cluster is one cell or two. A cluster containing VS16 is two, whatever
  its base (`q` + VS16 too). VS15 never narrows (`⌚︎` stays two). Otherwise
  wcwidth's grapheme width applies (`👨‍👩‍👧`, `👍🏻`, `🇺🇸`, `कि`, Hangul jamo).
  A regional indicator on its own is one.
- A zero-width code point always joins the cell before the cursor, even a
  zero-width space or one written after a cursor move. With no cell before it
  (column 0) it takes a cell of its own, two for VS16.
- Anything else joins only where UAX #29 keeps the cluster together: an
  emoji after a ZWJ, the second half of a flag, a skin tone, a Hangul jamo,
  a consonant after a virama.
- A two-cell cluster that doesn't fit in the last column wraps whole, and the
  cell it leaves is blanked. Growing to two cells in the last column (base,
  then VS16) moves the cluster to the next line the same way.
- A control character inside a text run is skipped, never the end of it.

Two conhost quirks are left out, since neither shows up in Claude's output:
a combining mark right after ASCII text that filled the row exactly takes a
cell of its own on the next row, and a character after a prepend mark
(U+0600) joins it. An ASCII run goes through pyte's own draw, which is exact
for it and the fast path. `tests/smoke/terminal.py` keeps the measured cases;
`tools/render_replay.py --conhost` compares a whole capture frame by frame.

Qt-free, so the smoke suite and the replay tools can use it directly.
"""

from __future__ import annotations

import functools
import unicodedata

from pyte import modes as mo
from wcwidth import grapheme_boundary_before, wcswidth, wcwidth

VS15 = "\uFE0E"
VS16 = "\uFE0F"
_RI_FIRST, _RI_LAST = "\U0001F1E6", "\U0001F1FF"
# below this, no code point continues a cluster
_FIRST_JOINER = "\u0300"
# one cluster of each kind a code point with a width can continue: any
# (a spacing mark, a skin tone), a Hangul leading jamo, a consonant with a
# virama, an emoji with a ZWJ, a regional indicator. A prepend mark is left
# out, since anything at all continues it.
_JOIN_PROBES = ("a", "\u1100", "\u0915\u094D", "\U0001F468\u200D",
                "\U0001F1FA")


@functools.lru_cache(maxsize=8192)
def _props(char: str) -> tuple:
    """(code point width, cells it takes when it starts a cluster, whether
    it can continue any cluster at all) of one code point."""
    w = wcwidth(char)
    if w < 0:
        return -1, 0, False
    if w == 0:
        return 0, 2 if char == VS16 else 1, True
    lone = 1 if _RI_FIRST <= char <= _RI_LAST else w
    return w, lone, char >= _FIRST_JOINER and any(
        joins(rep, char) for rep in _JOIN_PROBES)


@functools.lru_cache(maxsize=4096)
def cluster_width(text: str) -> int:
    """Cells a whole cluster takes: 1 or 2."""
    if VS16 in text:
        return 2
    return min(max(wcswidth(text.replace(VS15, "")), 1), 2)


@functools.lru_cache(maxsize=4096)
def joins(cluster: str, char: str) -> bool:
    """True when `char`, which has a width of its own, continues `cluster`
    rather than starting the next one (UAX #29)."""
    text = cluster + char
    return grapheme_boundary_before(text, len(text)) < len(cluster)


def draw(screen, data: str) -> None:
    """pyte's `Screen.draw`, measuring grapheme clusters as conhost does.
    Each cluster lives in its first cell's `data`; a two-cell cluster's
    second cell holds "", as pyte's own wide characters do."""
    data = data.translate(
        screen.g1_charset if screen.charset else screen.g0_charset)
    cursor = screen.cursor
    columns = screen.columns
    buffer = screen.buffer
    dirty = screen.dirty
    autowrap = mo.DECAWM in screen.mode
    insert = mo.IRM in screen.mode
    for char in data:
        code_width, width, may_join = _props(char)
        if code_width < 0:
            continue
        x = cursor.x
        if x and may_join:
            line = buffer[cursor.y]
            start = x - 1
            if start and line[start].data == "":
                start -= 1
            prev = line[start]
            if code_width == 0 or joins(prev.data, char):
                _join(screen, line, start, x - start, prev, char)
                continue
        if x == columns:
            if autowrap:
                dirty.add(cursor.y)
                screen.carriage_return()
                screen.linefeed()
            else:
                cursor.x -= width
        elif width == 2 and x == columns - 1 and autowrap:
            buffer[cursor.y][x] = cursor.attrs._replace(data=" ")
            dirty.add(cursor.y)
            screen.carriage_return()
            screen.linefeed()
        if insert:
            screen.insert_characters(width)
        line = buffer[cursor.y]
        x = cursor.x
        line[x] = cursor.attrs._replace(data=char)
        if width == 2 and x + 1 < columns:
            line[x + 1] = cursor.attrs._replace(data="")
        cursor.x = min(x + width, columns)
    dirty.add(cursor.y)


def _join(screen, line, start: int, span: int, prev, char: str) -> None:
    """Add `char` to the cluster at `start`, which covers `span` cells and
    ends right before the cursor."""
    cursor = screen.cursor
    text = unicodedata.normalize("NFC", prev.data + char)
    line[start] = prev._replace(data=text)
    screen.dirty.add(cursor.y)
    if span >= 2 or cluster_width(text) < 2:
        return
    if start + 2 <= screen.columns:
        line[start + 1] = prev._replace(data="")
        cursor.x = start + 2
        return
    if mo.DECAWM not in screen.mode or screen.columns < 2:
        return
    line[start] = cursor.attrs._replace(data=" ")
    screen.carriage_return()
    screen.linefeed()
    line = screen.buffer[cursor.y]
    line[0] = prev._replace(data=text)
    line[1] = prev._replace(data="")
    cursor.x = 2
    screen.dirty.add(cursor.y)
