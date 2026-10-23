"""Bold word starts: which cells of one terminal row to draw in bold.

Options > Appearance > Bold word starts draws the first half of each prose
word in bold, "**read**ing **th**is **li**ne". Some readers, dyslexic
readers among them, find a line easier to track that way. The published
tests show no reading-speed gain, so it is an opt-in comfort setting.

The effect works by contrast between a word's head and its rest, and a
terminal font's Bold is often barely heavier than its Regular. So Options
> Boldness can also thicken the head and fade the rest (word_spans hands
TerminalView both parts); WORD_STRENGTHS in widgets/terminal_view.py
has the levels.

This module only decides the cells. It is Qt-free, like providers.py, and
works on anything with `.data` and `.bold` per column (pyte's Char), so its
tests need no display. TerminalView.paintEvent draws the result.

It takes a whole row, before the painter splits it into attribute runs: a
word can cross a colour change, and segmenting each run on its own would
bold two prefixes in one word.

A token is a whitespace-delimited stretch of cells. Leading openers and
trailing punctuation are stripped, and what is left must be letters,
optionally joined by single internal apostrophes or hyphens. That keeps
code and paths (app/widgets/sidebar.py, snake_case, --flag, x=1, 0x1b) in
their regular weight. camelCase is all letters and gets the treatment.
"""

# straight and curly quotes both: Claude's prose uses either
_LEAD = frozenset("([{\"'`“‘")
_TRAIL = frozenset(".,;:!?)]}\"'`”’…")
_JOIN = frozenset("'-’")


def _is_letter(data: str) -> bool:
    # pyte keeps a combining mark in its base letter's cell ("e" + U+0301
    # is one cell), so the first code point decides
    return data[:1].isalpha()


def word_start_spans(cells, prev_row_tail=None) -> list[tuple[int, int]]:
    """Half-open column spans to draw bold on one row (word_spans' heads)."""
    return [(a, b) for a, b, _e in word_spans(cells, prev_row_tail)]


def word_spans(cells, prev_row_tail=None) -> list[tuple[int, int, int]]:
    """(start, mid, end) per letter run of each prose word on one row:
    columns [start, mid) are the bold head, [mid, end) the rest of the
    run, which the higher strengths draw fainter.

    `cells` holds one object per column. `prev_row_tail` is the data of the
    last cell of the row above, None for the first row. When that cell and
    this row's first cell are both letters, the row opens mid-word (the
    terminal wrapped it) and the word got its prefix on the row above, so
    its tail here stays regular.

    A token with any cell the child already drew in bold is left alone:
    the child's own emphasis (Claude's headings, **strong**) wins.
    """
    datas = [c.data or " " for c in cells]
    n = len(datas)
    spans: list[tuple[int, int]] = []
    col = 0
    if (prev_row_tail is not None and n and _is_letter(prev_row_tail)
            and _is_letter(datas[0])):
        while col < n and not datas[col].isspace():
            col += 1
    while col < n:
        if datas[col].isspace():
            col += 1
            continue
        start = col
        while col < n and not datas[col].isspace():
            col += 1
        _token_spans(cells, datas, start, col, spans)
    return spans


def _token_spans(cells, datas, start: int, end: int, out: list) -> None:
    """Append the spans of the token in columns [start, end)."""
    while start < end and datas[start] in _LEAD:
        start += 1
    while end > start and datas[end - 1] in _TRAIL:
        end -= 1
    if end - start < 2 or not _is_letter(datas[start]) \
            or not _is_letter(datas[end - 1]):
        return
    for k in range(start, end):
        if cells[k].bold:
            return
        d = datas[k]
        # start and end - 1 are letters, so k - 1 and k + 1 are in range
        if not _is_letter(d) and (d not in _JOIN
                                  or not _is_letter(datas[k - 1])
                                  or not _is_letter(datas[k + 1])):
            return
    k = start
    while k < end:
        run = k
        while run < end and _is_letter(datas[run]):
            run += 1
        if run - k >= 2:
            out.append((k, k + (run - k + 1) // 2, run))
        k = run + 1   # past the joiner
