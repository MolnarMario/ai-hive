"""Copy terminal text without the line breaks that wrapping put in.

Claude Code wraps its own text. It breaks a reply, or the prompt being
typed, to the card's width and moves to the next row itself, so the
terminal gets a plain line break either way and keeps no wrap flag. A copy
of one wrapped paragraph used to paste as a paragraph per row.
TerminalView.selected_text asks `joins` how to join each pair of selected
rows, and it answers from the rows' layout:

- A row that starts at column 0 is a shell's line, wrapped by the terminal
  itself. That only happens when the row above runs to the last column, and
  it can split a word anywhere, so the rows join with nothing between them.
- An indented row is how Claude continues a wrapped line. The break was a
  wrap when the row's first word would not have fit at the end of the row
  above. Claude never moves a word that fits, so a word that would have fit
  means the text itself had a line break there.

Claude's replies run to the last column. Its input box ends two columns
short of it, and the row after an exactly full row has one column less
(measured on claude 2.1.310 at 60 and 140 columns). WRAP_SLACK covers both.

A tool result keeps every line break. Its rows sit under the result's
first row, the one with the result marker, and Claude cuts them at the
edge mid-word, so the next-word test can't tell a wrapped path from two
lines of output. Rows that start with something of their own are never
joined either: a list item, one of Claude's row markers (a reply's dot,
the prompt), a table or box border, a numbered diff or listing row. The
joined row loses its indentation, and the pair joins with a space unless
the break split a word longer than a row.

The module is Qt-free like word_starts.py. It takes each row as one string
per column, pyte's Char.data, where "" is the second cell of a wide glyph.
"""

import re

# a next word that misses the row's end by this many columns still wrapped
WRAP_SLACK = 2
# a tool result's rows start right of "  ⎿  ", and nothing else of Claude's
# that is left of it sits inside the result
RESULT_INDENT = 5
RESULT_MARK = "⎿"

_LIST_ITEM = re.compile(r"(?:[-*+•◦▪]|\d{1,3}[.)])\s")
# what Claude starts a row of its own with: a reply, a tool result, a
# recap, the prompt, the "… +11 lines" fold of a long result
_ROW_GLYPHS = frozenset("●⎿✻※❯⏵…✓✗✔✘")
# a numbered diff or listing row: "  2492 +    code", "  800:code"
_GUTTER = re.compile(r"\s*\d+(?:$| [ +\-]|[:→])")
# a marker the rest of its item lines up under: "● ", "  - ", "1. "
_MARKER = re.compile(r"\s*(?:[●❯>]|[-*+•]|\d{1,3}[.)])\s+")


def _is_box(ch: str) -> bool:
    """Box drawing and block elements: table borders, rules, the logo."""
    return "─" <= ch[:1] <= "▟"


def _extent(cells) -> int:
    """Columns up to the last one with ink, 0 for a blank row."""
    for c in range(len(cells) - 1, -1, -1):
        if not cells[c].isspace():
            return c + 1
    return 0


def _indent(cells) -> int:
    """The first column with ink, len(cells) for a blank row."""
    for c, d in enumerate(cells):
        if not d.isspace():
            return c
    return len(cells)


def _result_state(cells):
    """True for a tool result's first row, False for another row that ends
    a result, None for a blank row or one that may sit inside a result."""
    start = _indent(cells)
    if start >= min(len(cells), RESULT_INDENT):
        return None
    return cells[start] == RESULT_MARK


def joins(rows, cols: int, above=()) -> list:
    """How a copy joins each row of `rows` to the next, one entry per pair:
    None keeps the line break, " " joins two words, "" joins the halves of
    a split word. `above` yields the rows over the first one, nearest
    first; it is read only until a row shows whether the first sits in a
    tool result."""
    if len(rows) < 2:
        return []
    in_result = _result_state(rows[0])
    if in_result is None:
        in_result = next((s for s in map(_result_state, above)
                          if s is not None), False)
    out = []
    for upper, lower in zip(rows, rows[1:]):
        state = _result_state(lower)
        in_result = in_result if state is None else state
        out.append(None if in_result else join_text(upper, lower, cols))
    return out


def join_text(above, below, cols: int):
    """How a copy joins row `above` to row `below` under it, as in `joins`,
    for two rows outside a tool result."""
    end, start = _extent(above), _indent(below)
    if not end or start >= len(below):
        return None
    top, low = "".join(above).rstrip(), "".join(below).strip()
    if (_is_box(top[-1:]) or _is_box(low) or low[:1] in _ROW_GLYPHS
            or _LIST_ITEM.match(low) or _GUTTER.match(low)
            or _GUTTER.match(top)):
        return None
    if not start:
        return "" if end >= cols else None
    head = start
    while head < len(below) and not below[head].isspace():
        head += 1
    head -= start
    if end + 1 + head <= cols - WRAP_SLACK:
        return None
    marker = _MARKER.match(top)
    if start < (marker.end() if marker else _indent(above)):
        return None
    tail = end
    while tail > 0 and not above[tail - 1].isspace():
        tail -= 1
    if (end >= cols - WRAP_SLACK
            and end - tail + head > cols - WRAP_SLACK - start):
        return ""
    return " "
