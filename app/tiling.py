"""Pure auto-tiling math for terminal card grids.

No Qt imports — this module is the single source of truth for card
placement. The widget layer applies these cells verbatim and the smoke
test asserts against them independently.
"""

from math import ceil, lcm, sqrt
from typing import NamedTuple


class Cell(NamedTuple):
    row: int
    col: int
    row_span: int
    col_span: int


class GridPlan(NamedTuple):
    cells: list[Cell]  # cells[i] positions card i (insertion order)
    rows: int
    vcols: int  # virtual column count the layout must allocate


class ExplicitPlan(NamedTuple):
    agent_cells: list[Cell]  # 1x1 cell per agent, filled left→right/top→bottom
    empty_cells: list[Cell]  # remaining slots (open "+ New agent" placeholders)
    rows: int
    cols: int


def explicit_grid(rows: int, cols: int, n: int) -> ExplicitPlan:
    """A fixed rows×cols grid. Agents fill left→right then top→bottom; unused
    slots stay empty. Auto-grows rows so no agent is ever hidden when n
    exceeds the requested capacity."""
    cols = max(1, cols)
    rows = max(1, rows)
    if n > rows * cols:
        rows = ceil(n / cols)
    total = rows * cols
    cells = [Cell(i // cols, i % cols, 1, 1) for i in range(total)]
    return ExplicitPlan(cells[:n], cells[n:], rows, cols)


def parse_layout(layout: str) -> tuple[int, int] | None:
    """"auto" → None; "WxH" → (rows, cols) for explicit_grid.

    Layout strings read WIDTH × HEIGHT — the convention humans use for
    screens ("3x1" = three cards side by side, "1x3" = three stacked).
    The selector's mini-diagrams draw exactly that shape, so what you click
    is what you get; parsing them as rows×cols made every non-square layout
    apply transposed (vertical when the picture showed horizontal).
    Invalid strings fall back to auto."""
    if not layout or layout == "auto":
        return None
    try:
        w, h = layout.lower().split("x", 1)
        cols, rows = int(w), int(h)
        if rows >= 1 and cols >= 1:
            return rows, cols
    except (ValueError, AttributeError):
        pass
    return None


def compute_grid(n: int) -> GridPlan:
    """Tile n cards into a squarish, wider-than-tall grid.

    cols = ceil(sqrt(n)), rows = ceil(n / cols):
    1 -> 1x1, 2 -> 2x1, 3-4 -> 2x2, 5-6 -> 3x2, 7-9 -> 3x3, 10 -> 4x3 ...

    Cards in a partial final row stretch to share the full width. Even
    stretching with integer spans requires laying out on lcm(cols, last)
    virtual columns: full rows span vcols/cols, the last row vcols/last.
    """
    if n <= 0:
        return GridPlan([], 0, 0)

    cols = ceil(sqrt(n))
    rows = ceil(n / cols)
    last = n - (rows - 1) * cols  # cards in the final row, 1..cols
    vcols = lcm(cols, last)
    full_span = vcols // cols
    last_span = vcols // last

    cells = []
    for i in range(n):
        r = i // cols
        if r < rows - 1 or last == cols:
            cells.append(Cell(r, (i % cols) * full_span, 1, full_span))
        else:
            k = i - (rows - 1) * cols
            cells.append(Cell(r, k * last_span, 1, last_span))
    return GridPlan(cells, rows, vcols)
