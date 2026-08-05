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


def _balanced_dims(n: int, prefer_wide: bool) -> tuple[int, int]:
    """A squarish rows×cols that holds n cells. `prefer_wide` puts the longer
    side on the columns (wider-than-tall); otherwise on the rows. floor(sqrt)
    is the short side so 3→3x1, 4→2x2, 5→3x2, 7→4x2, 9→3x3 (wide)."""
    short = max(1, int(sqrt(n)))         # floor(sqrt(n))
    long = ceil(n / short)
    return (short, long) if prefer_wide else (long, short)


def explicit_grid(rows: int, cols: int, n: int) -> ExplicitPlan:
    """A fixed rows×cols grid. Agents fill left→right then top→bottom; unused
    slots stay empty.

    When n exceeds the requested capacity the grid REBALANCES to a squarish
    shape instead of just stacking extra rows onto the original column count.
    Growing rows alone made a strip sprout an ugly sparse row: a 2×1 that
    gained a third agent became 2×2 (two over one) rather than the natural
    3×1, and a 3×1 gaining a fourth became 3×2 rather than a tidy 2×2. The
    rebalance is orientation-biased — a grid that was wider-than-tall grows
    wider, a taller one grows taller — so a deliberate vertical stack (1×2,
    1×3) is never flipped horizontal on overflow."""
    cols = max(1, cols)
    rows = max(1, rows)
    if n > rows * cols:
        rows, cols = _balanced_dims(n, prefer_wide=cols >= rows)
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
    1 -> 1x1, 2 -> 2x1, 4 -> 2x2, 5-6 -> 3x2, 7-9 -> 3x3, 10 -> 4x3 ...

    EXCEPT n<=3, which stays a single row (3 -> 3x1). ceil(sqrt(3)) = 2 puts
    two cards on top and stretches the third full-width underneath — a lopsided
    "2 over 1" nobody wants from a third terminal, and the opposite of what the
    same count produces on a FIXED grid, where `explicit_grid`'s overflow
    rebalance already turns 2x1 + a third agent into 3x1. Three side-by-side is
    also still a usable terminal width; four is where a single row stops being
    one, hence the cutoff.

    Cards in a partial final row stretch to share the full width. Even
    stretching with integer spans requires laying out on lcm(cols, last)
    virtual columns: full rows span vcols/cols, the last row vcols/last.
    """
    if n <= 0:
        return GridPlan([], 0, 0)

    cols = n if n <= 3 else ceil(sqrt(n))
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
