"""Where Claude Code's input caret can stop, read off the painted input box.

A terminal can't set the child's caret, so TerminalView moves it with arrow
keys (click to place the caret) and deletes a selection with Backspaces.
One press per screen column is right only for plain text. Measured on
Claude Code 2.1.316:

A long paste shows as one token: "[Pasted text #1 +4 lines]" for many
lines, "[Pasted text #2]" for one long line, "[Image #3]" for an image.
Left at a token's end jumps to its start, Right at its start jumps to its
end, and Backspace at its end deletes the whole token. Counting columns
sent the caret past a click by the token's width less one: left of the
click when the click was before the token, right of it when after.

Up and Down keep the screen column, so they can park the caret inside a
token. From there Left and Right move one column at a time until they
reach an edge, and typing there edits the token's text ("[Pasted texxt
#3"), which breaks the paste. So no target here is ever inside a token: a
click on one goes to its nearer edge, and a selection that touches one
deletes all of it.

A token near the row end wraps onto the next row and still moves as one.
Rows after the first are indented to line up with the text after the
prompt, and that indent is not text. One press crosses from a row's start
to the end of the row above, for a newline or a wrap. A wide glyph fills
two cells (pyte paints "" in the second) and takes one press.

Positions are (row, col) caret positions, the caret sitting before the
cell at col, and tuple order is reading order.
"""

import re

# \s+ between the words: a token that wraps loses the space at the break.
TOKEN = re.compile(
    r"\[(?:Pasted\s+text\s+#\d+(?:\s+\+\d+\s+lines?)?|Image\s+#\d+)\]")

_BLANK = ("", " ")
# What follows the prompt glyph. Claude paints "❯" and a no-break space, so
# a plain-space test put the text one column early and the walk crossed
# the indent of the rows below as text.
PROMPT_GAP = ("", " ", "\xa0")


def _content(cells) -> tuple[int, int]:
    """(first, last) non-blank columns of a row, or (-1, -1)."""
    first = last = -1
    for c, data in enumerate(cells):
        if data not in _BLANK:
            first = c if first < 0 else first
            last = c
    return first, last


class CaretMap:
    """The caret stops of one input box. `cells` maps each screen row of
    the box, top to bottom, to its cells' text. `caret` is the child's
    (row, col). A top row that opens with one of `prompts` puts the text
    one blank after the glyph, and the rows below are indented to it."""

    def __init__(self, cells: dict, caret: tuple, prompts=(">", "❯")):
        self.cells = cells
        self.top, self.bottom = min(cells), max(cells)
        self.caret = caret
        self._spans = {r: _content(row) for r, row in cells.items()}
        row = cells[self.top]
        first, _ = self._spans[self.top]
        self.indent = 0
        if first >= 0 and row[first] in prompts:
            self.indent = first + 1
            if self.indent < len(row) and row[self.indent] in PROMPT_GAP:
                self.indent += 1
        # (start, end) of each token, and the jumps one press makes
        self.tokens = []
        self._right = {}
        self._left = {}
        self._find_tokens()

    def row_start(self, r: int) -> int:
        """Leftmost caret column of row r. Rows below the first start at
        the indent Claude paints, or at their text when it starts further
        left, as on a TUI that paints no indent. A row whose real start
        lies left of both reads short, so a delete across it takes less
        text, never more."""
        if r == self.top:
            return self.indent
        first, _ = self._spans[r]
        return self.indent if first < 0 else min(self.indent, first)

    def row_end(self, r: int) -> int:
        """Rightmost caret column of row r: after its last glyph, or at the
        caret on its own row, which counts blanks typed at the end."""
        _, last = self._spans[r]
        end = max(last + 1, self.row_start(r))
        if r == self.caret[0]:
            end = max(end, self.caret[1])
        return end

    def _wide_half(self, r: int, c: int) -> bool:
        row = self.cells[r]
        return 0 < c < len(row) and row[c] == ""

    def _find_tokens(self) -> None:
        chars, where = [], []
        for r in range(self.top, self.bottom + 1):
            if r > self.top:
                chars.append("\n")
                where.append(None)
            for c in range(self.row_start(r), self.row_end(r)):
                data = self.cells[r][c] if c < len(self.cells[r]) else " "
                chars.extend(data)
                where.extend([(r, c)] * len(data))
        for m in TOKEN.finditer("".join(chars)):
            start = where[m.start()]
            last = where[m.end() - 1]
            end = (last[0], last[1] + 1)
            self.tokens.append((start, end))
            self._right[start] = end
            self._left[end] = start

    def _inside(self, pos):
        """The token `pos` sits strictly inside, or None."""
        for start, end in self.tokens:
            if start < pos < end:
                return start, end
        return None

    def step(self, pos: tuple, right: bool) -> tuple:
        """Where one Right (or Left) press moves the caret from `pos`. At
        either end of the box it stays put, as Claude's caret does."""
        r, c = pos
        if right:
            if pos in self._right:
                return self._right[pos]
            if c >= self.row_end(r):
                if r < self.bottom:
                    return r + 1, self.row_start(r + 1)
                return pos
            c += 1
            while c < self.row_end(r) and self._wide_half(r, c):
                c += 1
            return r, c
        if pos in self._left:
            return self._left[pos]
        if c <= self.row_start(r):
            if r > self.top:
                return r - 1, self.row_end(r - 1)
            return pos
        c -= 1
        while c > self.row_start(r) and self._wide_half(r, c):
            c -= 1
        return r, c

    def presses(self, src: tuple, dst: tuple):
        """How many Right (dst after src) or Left presses walk the caret
        from `src` to `dst`, or None when no run of them lands on it."""
        right = dst > src
        pos, n = src, 0
        while pos != dst:
            nxt = self.step(pos, right)
            if nxt == pos or (nxt > dst if right else nxt < dst):
                return None
            pos, n = nxt, n + 1
        return n

    def _clamp(self, pos: tuple) -> tuple:
        r, c = pos
        return r, max(self.row_start(r), min(c, self.row_end(r)))

    def snap(self, pos: tuple) -> tuple:
        """The caret stop a click at `pos` means: inside the row's text, out
        of a token to its nearer edge on this row, and before a wide glyph
        whose right half was clicked."""
        pos = self._clamp(pos)
        token = self._inside(pos)
        if token is not None:
            r, c = pos
            ends = [p for p in token if p[0] == r]
            if len(ends) == 2:
                start, end = ends
                return start if c - start[1] <= end[1] - c else end
            return ends[0] if ends else token[0]
        r, c = pos
        while c > self.row_start(r) and self._wide_half(r, c):
            c -= 1
        return r, c

    def widen(self, start: tuple, end: tuple) -> tuple:
        """A selection from caret `start` to caret `end`, inside the text
        and grown to hold whole tokens and glyphs. Backspace can't take
        part of a token, and deleting the whole token is what a click on
        one would select."""
        start, end = self._clamp(start), self._clamp(end)
        token = self._inside(start)
        if token is not None:
            start = token[0]
        else:
            r, c = start
            while c > self.row_start(r) and self._wide_half(r, c):
                c -= 1
            start = r, c
        token = self._inside(end)
        if token is not None:
            end = token[1]
        else:
            r, c = end
            while c < self.row_end(r) and self._wide_half(r, c):
                c += 1
            end = r, c
        return start, end
