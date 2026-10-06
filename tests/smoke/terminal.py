"""The terminal view: ANSI rendering, keys, mouse, links, selection, the
input editor, scrollback and the scrollbar."""

import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check, skip


def test_terminal_relative_link():
    """Ctrl+click a path in a conversation opens it: absolute paths work, and a
    RELATIVE path resolves against the agent cwd set via set_base_dir (the
    common case, since Claude prints repo-relative paths). Opening a file emits
    fileActivated(abspath) so the app can reveal it in the sidebar tree."""
    import os
    import tempfile
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y, TerminalView)
    QApplication.instance() or QApplication([])

    base = tempfile.mkdtemp(prefix="aihive_link_")
    os.makedirs(os.path.join(base, "app", "widgets"), exist_ok=True)
    rel = os.path.join("app", "widgets", "sidebar.py")
    absf = os.path.join(base, rel)
    with open(absf, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")

    tv = TerminalView(rows=6, cols=80)
    tv.resize(700, 200)
    # no base dir yet -> a relative path is NOT openable
    check("link: relative path is not classified without a base dir",
          tv._classify_link("app/widgets/sidebar.py") is None)
    tv.set_base_dir(base)
    check("link: relative path resolves against the base dir",
          tv._classify_link("app/widgets/sidebar.py")
          == ("file", os.path.abspath(absf)))
    check("link: relative path + :line[:col] suffix still resolves",
          tv._classify_link("app/widgets/sidebar.py:42:7")
          == ("file", os.path.abspath(absf)))
    check("link: absolute existing path still classifies as a file",
          tv._classify_link(absf) == ("file", os.path.abspath(absf)))
    check("link: a URL still classifies as a url",
          tv._classify_link("https://example.com")[0] == "url")
    check("link: a non-existent relative path is not openable",
          tv._classify_link("does/not/exist.py") is None)

    # Ctrl+left-click over the path emits fileActivated (open suppressed so the
    # test never launches a program)
    tv._open_target = lambda *_a, **_k: None
    tv.feed("app/widgets/sidebar.py\r\n")
    got = []
    tv.fileActivated.connect(got.append)
    x = CELL_PAD_X + int(3 * tv._cell_w)   # a cell inside the path token
    y = CELL_PAD_Y + int(0 * tv._cell_h) + 1
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    tv.mousePressEvent(ev)
    check("link: Ctrl+click a path emits fileActivated(abspath)",
          got == [os.path.abspath(absf)], got)
    tv.deleteLater()


def test_terminal_link_context_menu():
    """Right-clicking a path in the output offers Reveal in folder / Copy path,
    resolved to the ABSOLUTE path (Explorer needs an absolute path, and the
    token Claude prints is usually repo-relative). Drives _build_context_menu
    directly: contextMenuEvent's exec() would block the headless suite.

    Only the COPY actions are triggered here -- triggering Open, Open with or
    Reveal would launch a real program out of the test run."""
    import os
    import tempfile
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import TerminalView
    QApplication.instance() or QApplication([])

    base = tempfile.mkdtemp(prefix="aihive_menu_")
    os.makedirs(os.path.join(base, "app", "widgets"), exist_ok=True)
    absf = os.path.join(base, "app", "widgets", "sidebar.py")
    with open(absf, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")

    tv = TerminalView(rows=6, cols=80)
    tv.resize(700, 200)
    tv.set_base_dir(base)
    tv.feed("app/widgets/sidebar.py plainword https://example.com/a\r\n")

    def texts(col):
        return [a.text() for a in tv._build_context_menu(0, col).actions()
                if not a.isSeparator()]

    over_path = texts(3)
    check("link menu: a path offers the file map's four actions first",
          over_path[:4] == ["Open", "Open with...", "Reveal in folder",
                            "Copy path"], over_path)
    check("link menu: the clipboard items survive under the link items",
          over_path[4:] == ["Copy", "Paste", "Select all"], over_path)

    QGuiApplication.clipboard().setText("")
    menu = tv._build_context_menu(0, 3)
    [a for a in menu.actions() if a.text() == "Copy path"][0].trigger()
    check("link menu: Copy path copies the RESOLVED absolute path",
          QGuiApplication.clipboard().text() == os.path.abspath(absf),
          QGuiApplication.clipboard().text())

    # a plain word carries no link items at all -- no greyed-out placeholders
    plain = texts(26)
    check("link menu: a plain word offers only the clipboard items",
          plain == ["Copy", "Paste", "Select all"], plain)

    url_col = len("app/widgets/sidebar.py plainword ") + 4
    over_url = texts(url_col)
    check("link menu: a URL offers open + copy, not the four file actions",
          over_url[:2] == ["Open link", "Copy link address"], over_url)
    QGuiApplication.clipboard().setText("")
    menu = tv._build_context_menu(0, url_col)
    [a for a in menu.actions() if a.text() == "Copy link address"][0].trigger()
    check("link menu: Copy link address copies the URL",
          QGuiApplication.clipboard().text() == "https://example.com/a",
          QGuiApplication.clipboard().text())
    tv.deleteLater()


def test_terminal_block_glyphs():
    """Block Elements (U+2580-U+259F) are painted GEOMETRICALLY on the cell
    grid, not handed to the font.

    Regression for Claude Code's welcome mascot rendering visibly broken. The
    mascot is full blocks plus QUADRANTS (U+2598/259B/259C/259D), and Consolas
    has no quadrant glyphs: Qt fell back to Segoe UI Symbol, whose advance is
    12px where the cell is 7. Because paintEvent batches a run of cells into
    one drawText, that single glyph dragged the rest of the row ~5px right and
    the mascot's rows sheared apart; the fallback ink was also the wrong shape
    for the cell, notching every corner. Separately, Consolas' own full block
    is 6.7px of ink in a 7.0px advance, leaving a hairline of background at
    each cell boundary that striped solid artwork.

    So: exact fills on a shared rounded grid (no seam, no overlap, no drift),
    and any glyph the font lacks is drawn ALONE so it cannot shear its row."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import (CELL_PAD_X, _BLOCK_RECTS,
                                           _BLOCK_SHADES, TerminalView)
    QApplication.instance() or QApplication([])

    tv = TerminalView(rows=8, cols=40)
    tv.resize(500, 220)

    # --- the grid: adjacent cells must share an edge exactly ---------------
    check("blocks: cells tile horizontally with no seam or overlap",
          all(tv.cell_bounds(c, 0)[2] == tv.cell_bounds(c + 1, 0)[0]
              for c in range(39)))
    check("blocks: cells tile vertically with no seam or overlap",
          all(tv.cell_bounds(0, r)[3] == tv.cell_bounds(0, r + 1)[1]
              for r in range(7)))
    # a fractional cell width is the case that breaks a width-per-cell scheme
    # (rounding error accumulates across the row) - deriving both edges from
    # the same expression cannot drift
    tv._cell_w, tv._cell_h = 7.4, 15.6
    check("blocks: fractional cell size tiles seamlessly and never drifts",
          all(tv.cell_bounds(c, 0)[2] == tv.cell_bounds(c + 1, 0)[0]
              for c in range(39))
          and tv.cell_bounds(39, 0)[2] == round(CELL_PAD_X + 40 * 7.4))
    tv.deleteLater()

    # --- pixel coverage ----------------------------------------------------
    FG = QColor(255, 255, 255).rgb()   # white: max contrast, never re-tinted

    def render(text, cols=12, rows=4):
        v = TerminalView(rows=rows, cols=cols)
        v.resize(260, 120)
        v.feed("\x1b[38;2;255;255;255m" + text)
        return v, v.grab().toImage()

    def lit(img, v, col, row, u, w):
        """Is the cell's fractional point (u, w) painted foreground?"""
        x0, y0, x1, y1 = v.cell_bounds(col, row)
        return QColor(img.pixel(x0 + int((x1 - x0) * u),
                                y0 + int((y1 - y0) * w))).rgb() == FG

    # full block: every pixel of the cell, and no gap where two blocks meet
    v, img = render("███")
    x0, y0, _, y1 = v.cell_bounds(0, 0)
    x1 = v.cell_bounds(2, 0)[2]
    check("blocks: full block fills its cell edge to edge (no hairline seam)",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(x0, x1) for y in range(y0, y1)))
    v.deleteLater()

    # halves and quadrants: boundaries at 1/2 round cleanly, so assert the
    # exact quarter-by-quarter mask the Unicode chart specifies
    QUARTERS = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    exact = []
    for data in ("▀", "▄", "▌", "▐", "▖", "▗",
                 "▘", "▙", "▚", "▛", "▜", "▝",
                 "▞", "▟", "█"):
        v, img = render(data)
        for (u, w) in QUARTERS:
            want = any(fx0 <= u < fx1 and fy0 <= w < fy1
                       for (fx0, fy0, fx1, fy1) in _BLOCK_RECTS[data])
            if lit(img, v, 0, 0, u, w) != want:
                exact.append((hex(ord(data)), u, w))
        v.deleteLater()
    check("blocks: every half/quadrant covers exactly its quarters of the cell",
          not exact, exact)

    # eighth blocks: ink on the correct side, nothing on the opposite half
    eighths = []
    for data, inside, outside in (("▁", (0.5, 0.97), (0.5, 0.25)),
                                  ("▔", (0.5, 0.02), (0.5, 0.75)),
                                  ("▏", (0.02, 0.5), (0.75, 0.5)),
                                  ("▕", (0.97, 0.5), (0.25, 0.5))):
        v, img = render(data)
        if not lit(img, v, 0, 0, *inside) or lit(img, v, 0, 0, *outside):
            eighths.append(hex(ord(data)))
        v.deleteLater()
    check("blocks: eighth blocks hug their own edge of the cell", not eighths,
          eighths)

    # shades are the full cell at partial opacity - present but not solid
    v, img = render("░▒▓")
    shades = [QColor(img.pixel(*[(v.cell_bounds(c, 0)[0]
                                  + v.cell_bounds(c, 0)[2]) // 2,
                                 (v.cell_bounds(c, 0)[1]
                                  + v.cell_bounds(c, 0)[3]) // 2])).lightness()
              for c in range(3)]
    check("blocks: the three shades render at increasing density",
          shades[0] < shades[1] < shades[2] < QColor(FG).lightness(), shades)
    check("blocks: shade table covers exactly the three shade codepoints",
          set(_BLOCK_SHADES) == {"░", "▒", "▓"})
    check("blocks: geometry table covers the rest of U+2580-U+259F",
          set(_BLOCK_RECTS) | set(_BLOCK_SHADES)
          == {chr(c) for c in range(0x2580, 0x25A0)})
    v.deleteLater()

    # vertical neighbours must meet: lower half over upper half is solid
    v, img = render("▄\r\n▀")
    bx0, _, bx1, seam = v.cell_bounds(0, 0)
    check("blocks: a lower half over an upper half leaves no horizontal seam",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(bx0, bx1) for y in range(seam - 2, seam + 2)))
    v.deleteLater()

    # --- shear guard: an off-grid glyph must not move its neighbours -------
    v = TerminalView(rows=4, cols=12)
    check("blocks: an ASCII glyph is grid-safe", v._is_grid_glyph("M"))
    # Which glyphs fall back depends on the installed fonts (and on font state
    # earlier tests leave behind), so pin the advance instead of probing real
    # coverage: a glyph whose advance is not the cell width is off-grid.
    from PySide6.QtGui import QFontMetricsF
    adv = QFontMetricsF(v._font).horizontalAdvance("▘")
    v._grid_glyph_cache.clear()
    v._cell_w = adv
    check("blocks: a glyph advancing exactly one cell is grid-safe",
          v._is_grid_glyph("▘"))
    v._grid_glyph_cache.clear()
    v._cell_w = adv + 5
    check("blocks: a glyph advancing more than one cell is flagged off-grid",
          not v._is_grid_glyph("▘") and v._is_grid_glyph("M"))
    v.deleteLater()

    # the mascot itself: legs sit on exact cell boundaries, body is solid
    v, img = render("▝▜█████▛▘"
                    "\r\n  ▘▘ ▝▝", cols=14)
    body_x0, body_y0, _, body_y1 = v.cell_bounds(2, 0)
    body_x1 = v.cell_bounds(6, 0)[2]
    check("blocks: mascot body renders solid (no shear gaps, no stripes)",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(body_x0, body_x1)
              for y in range(body_y0, body_y1)))
    legs = [c for c in range(14)
            if lit(img, v, c, 1, 0.25, 0.25) or lit(img, v, c, 1, 0.75, 0.25)]
    check("blocks: mascot legs land on their own cells (no fallback drift)",
          legs == [2, 3, 5, 6], legs)
    v.deleteLater()


def test_terminal_link_underline():
    """Every clickable URL/path on the visible screen is scanned + underlined
    (not just the hovered one), so links stand out in the body text; the scan
    is content-guarded (no rescan when nothing changed); hover reads the cached
    spans; plain words are pre-filtered out (no filesystem stat)."""
    import os
    import tempfile
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import TerminalView
    QApplication.instance() or QApplication([])

    base = tempfile.mkdtemp(prefix="aihive_ul_")
    os.makedirs(os.path.join(base, "app", "widgets"), exist_ok=True)
    open(os.path.join(base, "app", "widgets", "sidebar.py"), "w").close()

    tv = TerminalView(rows=6, cols=90)
    tv.resize(820, 220)
    tv.set_base_dir(base)
    tv.feed("See https://example.com and app/widgets/sidebar.py here\r\n")
    tv.grab()   # force a paint -> content-guarded rescan

    hist, off = tv._view_state()
    tokens = ["".join(tv._visible_line(r, hist, off)[i].data
                      for i in range(c0, c1 + 1))
              for (r, c0, c1) in tv._link_spans]
    check("underline: both a URL and a relative path are scanned as links",
          any(t.startswith("https://example.com") for t in tokens)
          and any(t.endswith("sidebar.py") for t in tokens)
          and len(tv._link_spans) == 2)
    check("underline: plain words are pre-filtered (no fs stat)",
          not tv._maybe_link("here") and not tv._maybe_link("and"))

    # the scan is content-guarded: same content -> same cached span object
    sig_before = tv._link_sig
    spans_obj = tv._link_spans
    tv.grab()
    check("underline: no rescan when the screen content is unchanged",
          tv._link_sig == sig_before and tv._link_spans is spans_obj)

    # hover reads the cached spans (no filesystem work): a cell inside a link
    # resolves to its span; a blank cell resolves to nothing
    r, c0, c1 = tv._link_spans[0]
    check("underline: _span_at finds the link under a cell", tv._span_at(r, c0) == (c0, c1))
    check("underline: _span_at is None off any link", tv._span_at(r, c1 + 1) is None)

    # new content re-scans (the URL is gone, so no links remain)
    tv.feed("\x1b[2J\x1b[Hjust plain text now\r\n")
    tv.grab()
    check("underline: rescans when content changes (links cleared)",
          tv._link_spans == [])
    tv.deleteLater()


def test_ansi():
    from app.ansi_parser import AnsiSgrParser

    p = AnsiSgrParser()
    segs = p.feed("\x1b[31mred\x1b[0m plain")
    check("ansi: SGR color applied",
          len(segs) == 2 and segs[0][0].fg is not None and segs[1][0].fg is None,
          segs)

    p = AnsiSgrParser()
    a = p.feed("start\x1b[3")      # escape split across chunks
    b = p.feed("2mgreen")
    check("ansi: partial escape carried across chunks",
          "".join(t for _, t in a) == "start"
          and "".join(t for _, t in b) == "green"
          and b[0][0].fg is not None, (a, b))

    p = AnsiSgrParser()
    segs = p.feed("\x1b]0;window title\x07visible")
    check("ansi: OSC stripped", "".join(t for _, t in segs) == "visible", segs)

    p = AnsiSgrParser()
    p.feed("\x1b[38;5;196m")
    segs = p.feed("bright")
    check("ansi: 256-color mode", segs and segs[0][0].fg == "#ff0000", segs)

    # regression: an unterminated OSC must not swallow the stream forever
    p = AnsiSgrParser()
    p.feed("\x1b]0;title-that-never-terminates")
    for _ in range(30):
        p.feed("x" * 500)  # would grow the carry unboundedly if uncapped
    segs = p.feed("recovered")
    check("ansi: unterminated OSC recovers (carry capped)",
          any("recovered" in t for _, t in segs), segs[-1:])


def test_terminal_keys():
    """Focused terminal maps keys to the right VT sequences (Shift+Tab etc.)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    view = TerminalView(rows=24, cols=80)

    K = Qt.Key
    cases = {
        # (key, ctrl, shift, alt, text) -> expected VT bytes
        "Shift+Tab cycles modes (CSI Z)": ((K.Key_Backtab, False, True, False, ""), "\x1b[Z"),
        "Tab completes": ((K.Key_Tab, False, False, False, "\t"), "\t"),
        "Ctrl+C interrupts": ((K.Key_C, True, False, False, ""), "\x03"),
        "Ctrl+R reverse-search": ((K.Key_R, True, False, False, ""), "\x12"),
        "Ctrl+D EOF": ((K.Key_D, True, False, False, ""), "\x04"),
        "Escape": ((K.Key_Escape, False, False, False, ""), "\x1b"),
        "Enter submits": ((K.Key_Return, False, False, False, "\r"), "\r"),
        "Shift+Enter newlines": ((K.Key_Return, False, True, False, "\r"), "\x1b\r"),
        "Ctrl+Enter newlines": ((K.Key_Return, True, False, False, ""), "\n"),
        "Up arrow": ((K.Key_Up, False, False, False, ""), "\x1b[A"),
        "Ctrl+Left word-jump": ((K.Key_Left, True, False, False, ""), "\x1b[1;5D"),
        "Alt+key sends meta": ((K.Key_B, False, False, True, "b"), "\x1bb"),
        "plain letter": ((K.Key_A, False, False, False, "a"), "a"),
    }
    for name, (args, expected) in cases.items():
        got = view._sequence_for(*args)
        check(f"keys: {name}", got == expected, f"got {got!r}, want {expected!r}")

    check("keys: terminal refuses focus traversal (owns Tab)",
          view.focusNextPrevChild(True) is False)

    # Windows-editor shortcuts: Ctrl+A highlights the input line (Backspace/Del
    # clears it), Ctrl+Shift+A selects all, Ctrl+C smart-copies, and Ctrl+C with
    # no selection still sends the interrupt.
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QGuiApplication, QKeyEvent

    def press(k, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, ""))

    # Claude's input box opens with a '>' prompt, and transcript can butt right
    # against it (no blank line). Ctrl+A must stop AT the prompt row, never
    # climb into the transcript above -- the over-highlight regression.
    view.feed("recap line one\r\nrecap line two\r\n> my prompt")
    sent = []
    view.keyInput.connect(sent.append)
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A highlights the input line",
          view.selected_text() == "> my prompt", view.selected_text())
    check("keys: Ctrl+A stops at the '>' prompt, not the transcript above",
          "recap" not in view.selected_text(), view.selected_text())
    check("keys: Ctrl+A is visual-only (nothing to the pty)", sent == [], sent)
    press(K.Key_Backspace)
    check("keys: Backspace on the highlight clears input via double-Esc",
          sent == ["\x1b\x1b"], sent)
    check("keys: clearing drops the highlight", view.selected_text() == "")
    sent.clear()
    press(K.Key_Backspace)
    check("keys: plain Backspace (no highlight) forwards to the child (0x7f)",
          sent == ["\x7f"], sent)

    # Typing a printable character over the Ctrl+A highlight REPLACES the input,
    # like any editor: the child's prompt is cleared (double-Esc) THEN the typed
    # character is sent, so it becomes the fresh input rather than appending.
    def type_char(ch):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, K.Key_X,
                                     Qt.KeyboardModifier.NoModifier, ch))
    view.feed("recap line one\r\n> my prompt")
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A re-highlights the input", view.selected_text() == "> my prompt")
    sent.clear()
    type_char("x")
    check("keys: typing over the highlight clears then sends the char",
          sent == ["\x1b\x1b", "x"], sent)
    check("keys: typing over the highlight drops the highlight",
          view.selected_text() == "")

    # a navigation key over the highlight only collapses it -- no clear, no char
    press(K.Key_A, ctrl=True)
    sent.clear()
    press(K.Key_Left)
    check("keys: arrow over the highlight collapses it without clearing",
          "\x1b\x1b" not in sent and view.selected_text() == "", sent)

    # Ctrl+C over the Ctrl+A highlight COPIES the input rather than falling
    # through to the 0x03 interrupt (which cleared the child's input -- the
    # reported "Ctrl+C deletes my text instead of copying" bug).
    view.feed("recap\r\n> copy me")
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A re-highlights for the copy case",
          view.selected_text() == "> copy me", view.selected_text())
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C over the highlight copies it",
          QGuiApplication.clipboard().text() == "> copy me",
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+C over the highlight sends NO interrupt (no 0x03)",
          "\x03" not in sent, sent)
    check("keys: Ctrl+C over the highlight drops the highlight",
          view.selected_text() == "")

    # Ctrl+X over the highlight copies too, THEN clears the child's input (the
    # double-Esc clear-prompt gesture) -- a cut.
    view.feed("recap\r\n> cut me")
    press(K.Key_A, ctrl=True)
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_X, ctrl=True)
    check("keys: Ctrl+X over the highlight copies it",
          QGuiApplication.clipboard().text() == "> cut me",
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+X over the highlight clears input (double-Esc)",
          sent == ["\x1b\x1b"], sent)

    # a multi-line prompt (the '>' first line + continuation) highlights in full
    # but still excludes the output above it
    mv = TerminalView(rows=24, cols=80)
    mv.feed("output above\r\n> line one\r\nline two")
    mv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, K.Key_A,
                               Qt.KeyboardModifier.ControlModifier, ""))
    msel = mv.selected_text()
    check("keys: Ctrl+A spans a multi-line prompt",
          "line one" in msel and "line two" in msel and "\n" in msel, msel)
    check("keys: multi-line Ctrl+A excludes the output above",
          "output above" not in msel, msel)

    # Ctrl+Shift+A selects all; Ctrl+C then copies it and sends nothing to pty
    sent.clear()
    press(K.Key_A, ctrl=True, shift=True)
    check("keys: Ctrl+Shift+A selects all (incl. transcript)",
          "recap line one" in view.selected_text(), view.selected_text())
    check("keys: Ctrl+Shift+A sends nothing to the pty", sent == [], sent)
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C copies the selection",
          "recap line one" in QGuiApplication.clipboard().text(),
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+C-copy sends nothing to the pty", sent == [], sent)
    check("keys: Ctrl+C clears selection so next Ctrl+C interrupts",
          view.selected_text() == "")

    # with the selection gone, Ctrl+C is the interrupt again
    sent.clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C with no selection interrupts (0x03)",
          sent == ["\x03"], sent)


def test_terminal_image_paste():
    """A clipboard image is spilled to a temp PNG and its path is pasted.

    Native-Windows Claude can't take a raw clipboard image; it reads images by
    path. So pasting an image must write a file and paste the path, while a
    text clipboard still pastes text unchanged."""
    import os

    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=40)

    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFFFF0000)
    QGuiApplication.clipboard().setImage(img)
    if not QGuiApplication.clipboard().mimeData().hasImage():
        skip('image-paste', 'offscreen clipboard has no image support')
        return

    sent = []
    v.keyInput.connect(sent.append)
    v.paste_clipboard()
    check("image-paste: emitted exactly one paste", len(sent) == 1, sent)
    pasted = sent[0].strip('"') if sent else ""
    check("image-paste: pasted a .png path", pasted.lower().endswith(".png"), pasted)
    check("image-paste: the PNG was actually written on disk",
          os.path.isfile(pasted), pasted)
    check("image-paste: file lands in the aihive-paste temp dir",
          "aihive-paste" in pasted, pasted)

    # a text clipboard still pastes text, never a file path
    QGuiApplication.clipboard().setText("plain text")
    sent.clear()
    v.paste_clipboard()
    check("image-paste: text clipboard still pastes text",
          sent == ["plain text"], sent)

    try:
        os.remove(pasted)
    except OSError:
        pass


def test_terminal_mouse_words_links():
    """Double-click selects a word; middle-click classifies URLs / files."""
    import os

    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=80)
    v.feed("the quick brown fox")

    # word run is whitespace-delimited; a blank cell has no word
    check("mouse: _word_at finds the word range", v._word_at(0, 11) == (10, 14),
          v._word_at(0, 11))
    check("mouse: _word_at on a space is None", v._word_at(0, 9) is None)

    # a real double-click event selects that word, ready for Ctrl+C
    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * v._cell_w,
                       CELL_PAD_Y + (row + 0.5) * v._cell_h)
    v.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, pos(0, 11),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("mouse: double-click selects the word", v.selected_text() == "brown",
          v.selected_text())

    # link classification
    check("mouse: https URL classified", v._classify_link("https://example.com/x")
          == ("url", "https://example.com/x"))
    check("mouse: www URL gets an http scheme",
          v._classify_link("www.example.com/y") == ("url", "http://www.example.com/y"))
    check("mouse: wrapping paren/comma stripped from a URL",
          v._classify_link("(https://example.com),")[1] == "https://example.com")
    check("mouse: a bare word is not a link", v._classify_link("brown") is None)
    check("mouse: a relative path is not opened (no base dir)",
          v._classify_link("app/foo.py") is None)

    this = os.path.abspath(__file__)
    check("mouse: an existing absolute file is classified",
          v._classify_link(this) == ("file", this), v._classify_link(this))
    check("mouse: a file:line[:col] suffix is stripped",
          v._classify_link(this + ":42:7") == ("file", this))
    check("mouse: a nonexistent absolute path is None",
          v._classify_link(os.path.join(os.path.dirname(this), "nope_xyz.zzz")) is None)

    # _link_at reads the token straight off the painted line
    v2 = TerminalView(rows=6, cols=80)
    v2.feed("see https://example.com/docs for details")
    v2.grab()   # paint once so the full-screen link scan caches the spans
    check("mouse: _link_at picks up the URL under the pointer",
          v2._link_at(0, 8) == ("url", "https://example.com/docs"), v2._link_at(0, 8))

    # every link is underlined (scanned up front); hover reads the cached span
    check("mouse: _span_at spans the scanned URL",
          v2._span_at(0, 8) == (4, 27), v2._span_at(0, 8))
    check("mouse: _span_at is None over plain text",
          v2._span_at(0, 30) is None, v2._span_at(0, 30))

    def move(view, row, col):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        view.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, p, Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    move(v2, 0, 8)  # over the URL
    check("mouse: hovering a link sets the hover span",
          v2._hover_link == (0, 4, 27), v2._hover_link)
    check("mouse: hovering a link shows the hand cursor",
          v2.cursor().shape() == Qt.CursorShape.PointingHandCursor)
    move(v2, 0, 30)  # over plain text
    check("mouse: leaving a link clears the hover", v2._hover_link is None)
    check("mouse: cursor reverts to I-beam off a link",
          v2.cursor().shape() == Qt.CursorShape.IBeamCursor)

    # Ctrl+left-click a link opens it (route to _open_target, without actually
    # launching a browser); a plain left-click selects instead of opening
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    opened = []
    v2._open_target = opened.append

    def click(row, col, ctrl=False, button=Qt.MouseButton.LeftButton):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v2._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v2._cell_h)
        mods = (Qt.KeyboardModifier.ControlModifier if ctrl
                else Qt.KeyboardModifier.NoModifier)
        v2.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, p,
                                       button, button, mods))

    click(0, 8, ctrl=True)  # Ctrl+left-click ON the URL
    check("mouse: Ctrl+click opens the link under the pointer",
          opened == [("url", "https://example.com/docs")], opened)
    opened.clear()
    click(0, 8, ctrl=False)  # plain left-click does NOT open
    check("mouse: plain left-click does not open a link", opened == [], opened)
    opened.clear()
    click(0, 30, ctrl=True)  # Ctrl+click on plain text -> nothing to open
    check("mouse: Ctrl+click off a link opens nothing", opened == [], opened)
    opened.clear()
    click(0, 8, button=Qt.MouseButton.MiddleButton)  # middle-click still works
    check("mouse: middle-click still opens the link",
          opened == [("url", "https://example.com/docs")], opened)

    # plain left-click places the caret: send Left/Right arrows to the child so
    # its input cursor lands on the clicked column (exact on the caret's line)
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("hello")                      # child caret ends at column 5, row 0
    moves = []
    v3.keyInput.connect(moves.append)

    def caret_click(row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v3._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v3._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        v3.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v3.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    caret_click(0, 2)   # 3 columns left of the caret
    check("mouse: click left of the caret sends Left arrows",
          moves == ["\x1b[D" * 3], moves)
    moves.clear()
    caret_click(0, 7)   # 2 columns right of the caret (pty is inert in-test)
    check("mouse: click right of the caret sends Right arrows",
          moves == ["\x1b[C" * 2], moves)
    moves.clear()
    caret_click(3, 4)   # a row that isn't the caret's line
    check("mouse: click off the caret's line never nudges it", moves == [], moves)
    moves.clear()
    caret_click(0, 5)   # exactly on the caret -> nothing to send
    check("mouse: click on the caret sends nothing", moves == [], moves)

    # Multi-line prompt: clicking a DIFFERENT line of the input box repositions
    # the caret best-effort -- Up/Down to the row, then Left/Right to the
    # predicted landing column. Previously any off-caret-row click did nothing.
    v4 = TerminalView(rows=8, cols=80)
    v4.feed("> line one\r\nline two")   # prompt row 0, caret row 1 col 8
    m2 = []
    v4.keyInput.connect(m2.append)

    def caret_click4(row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v4._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v4._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        v4.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v4.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    caret_click4(0, 2)   # up one row (caret col 8 -> land col 8), left to col 2
    check("mouse: click a line above moves up then left",
          m2 == ["\x1b[A", "\x1b[D" * 6], m2)
    m2.clear()
    caret_click4(0, 40)  # past the row's end -> clamp to line end (col 10)
    check("mouse: click past a line's end clamps to its content",
          m2 == ["\x1b[A", "\x1b[C" * 2], m2)
    m2.clear()
    caret_click4(6, 4)   # a row outside the input box -> untouched
    check("mouse: click outside the input box never nudges the caret",
          m2 == [], m2)
    m2.clear()
    caret_click4(1, 3)   # the caret's own row is still exact (col 8 -> 3)
    check("mouse: click on the caret's own multi-line row stays exact",
          m2 == ["\x1b[D" * 5], m2)

    # moving DOWN a row: put the child's caret on the top line (feed real CUU),
    # then click a lower line of a 3-line prompt.
    v5 = TerminalView(rows=8, cols=80)
    v5.feed("> a\r\nbb\r\nccc\x1b[2A")   # 3 lines, caret pushed up to row 0
    m3 = []
    v5.keyInput.connect(m3.append)
    p = QPointF(CELL_PAD_X + 1.5 * v5._cell_w, CELL_PAD_Y + 2.5 * v5._cell_h)
    a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
         Qt.KeyboardModifier.NoModifier)
    v5.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
    v5.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))
    check("mouse: click a line below moves down then corrects the column",
          m3 == ["\x1b[B" * 2, "\x1b[D" * 2], m3)


def test_terminal_mouse_tracking_click():
    """When the child app requested mouse tracking (Claude Code holds it on for
    its whole session), a stationary left CLICK is forwarded as a mouse report so
    the app selects the option under the pointer -- NOT swallowed for local caret
    repositioning, which injected stray arrows into modal menus (AskUserQuestion
    / plan approval) and made the question vanish unanswered. But a DRAG (or a
    double-click) is a local text SELECTION and is never forwarded, so the
    transcript stays copyable even while the child holds mouse tracking on."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=8, cols=80)
    # the app enables mouse tracking + SGR encoding (exactly what Claude does)
    v.feed("\x1b[?1000h\x1b[?1006h")
    # put selectable text on screen row 3 (1-based row 4): "hello world"
    v.feed("\x1b[4;1Hhello world")
    check("mouse-track: DECSET turned on tracking", v._mouse_tracking)
    check("mouse-track: DECSET turned on SGR encoding", v._mouse_sgr)

    out = []
    v.keyInput.connect(out.append)

    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * v._cell_w,
                       CELL_PAD_Y + (row + 0.5) * v._cell_h)

    def click(row, col, mods=Qt.KeyboardModifier.NoModifier):
        a = (pos(row, col), Qt.MouseButton.LeftButton,
             Qt.MouseButton.LeftButton, mods)
        v.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    # a stationary click at row 3, col 5 -> deferred, then forwarded on release
    # as SGR press+release (1-based coords), no arrows
    click(3, 5)
    check("mouse-track: stationary left-click forwards SGR press+release",
          out == ["\x1b[<0;6;4M", "\x1b[<0;6;4m"], out)
    check("mouse-track: forwarding clears the pending-forward state",
          v._pending_fwd is None)
    out.clear()

    # a DRAG selects text locally and forwards NOTHING -- this is the regression
    # guard: forwarding on press used to make every drag a mouse report so the
    # transcript never selected. Press at "hello", drag across it, release.
    a0 = (pos(3, 0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
          Qt.KeyboardModifier.NoModifier)
    v.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a0))
    v.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, pos(3, 4), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos(3, 4), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    check("mouse-track: drag selects locally, forwards no report", out == [], out)
    check("mouse-track: drag while tracking still yields a copyable selection",
          v.selected_text() == "hello", repr(v.selected_text()))
    check("mouse-track: drag left no pending forward", v._pending_fwd is None)
    out.clear()

    # a double-click word-selects locally (a selection gesture), forwards nothing
    v.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, pos(3, 8),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos(3, 8),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("mouse-track: double-click word-selects locally, forwards nothing",
          out == [] and v.selected_text() == "world", (out, v.selected_text()))
    out.clear()

    # Shift is the escape hatch: Shift+click selects locally, forwards nothing
    click(3, 5, mods=Qt.KeyboardModifier.ShiftModifier)
    check("mouse-track: Shift+click selects locally, forwards no report",
          out == [], out)
    out.clear()

    # legacy X10 encoding (no ?1006): press byte = 32+button, release = 32+3
    v2 = TerminalView(rows=8, cols=80)
    v2.feed("\x1b[?1000h")
    check("mouse-track: X10 tracking on, SGR off",
          v2._mouse_tracking and not v2._mouse_sgr)
    out2 = []
    v2.keyInput.connect(out2.append)
    a = (QPointF(CELL_PAD_X + 5.5 * v2._cell_w, CELL_PAD_Y + 3.5 * v2._cell_h),
         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
         Qt.KeyboardModifier.NoModifier)
    v2.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
    v2.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))
    check("mouse-track: X10 stationary click forwards press(0) then release(3)",
          out2 == ["\x1b[M" + chr(32) + chr(38) + chr(36),
                   "\x1b[M" + chr(35) + chr(38) + chr(36)], out2)


def test_terminal_click_menu_guard():
    """Regression guard for the "AskUserQuestion vanishes on click" bug,
    reopened once the classic/"default" TUI renderer became the default
    (ui.terminal_scrollback=True): that renderer never negotiates mouse
    tracking, so a click on a menu falls through to local caret-repositioning
    (_reposition_cursor), which sends raw arrow/backspace bytes and corrupts
    an interactive menu that has no readline caret for them to land on. The
    fix is set_waiting_probe(agent.is_waiting) -- while it reports True, a
    click (or a selection edit) must send NOTHING to the child."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])

    def click(view, row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        view.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    # no mouse tracking (the classic-renderer case): a click 3 columns off the
    # caret normally sends Right arrows -- confirm that still works by default
    # (waiting_probe defaults to "never waiting"), then confirm it stops dead
    # the moment the probe reports an interactive menu is open.
    v = TerminalView(rows=6, cols=80)
    v.feed("hello")   # caret at row 0, col 5
    moves = []
    v.keyInput.connect(moves.append)
    click(v, 0, 2)
    check("click-guard: default probe still allows normal caret placement",
          moves == ["\x1b[D" * 3], moves)
    moves.clear()

    v.set_waiting_probe(lambda: True)
    click(v, 0, 2)
    check("click-guard: a click sends nothing while waiting_probe is True",
          moves == [], moves)
    moves.clear()
    # clicking the exact caret cell (the row Claude parks its cursor on while
    # showing a highlighted menu option) is the scenario that actually
    # corrupted the menu -- must be inert too, not just off-caret clicks
    click(v, 0, 7)
    check("click-guard: a click on the caret's own row is inert while waiting",
          moves == [], moves)
    moves.clear()

    v.set_waiting_probe(lambda: False)
    click(v, 0, 2)
    check("click-guard: caret placement resumes once the probe clears",
          moves == ["\x1b[D" * 3], moves)

    # independent, narrower fix: _input_block_span() now runs before the
    # same-row fast path, so a click on the cursor's own row is rejected when
    # that row is itself BLANK (previously it fired arrows regardless).
    v2 = TerminalView(rows=8, cols=80)
    v2.feed("hello\r\n")   # caret moves to row 1 col 0 -- a blank row
    blanks = []
    v2.keyInput.connect(blanks.append)
    click(v2, 1, 5)
    check("click-guard: a click on the caret's own blank row sends nothing",
          blanks == [], blanks)

    # _delete_selection carries its own copy of the guard (it sends real
    # Backspace bytes unconditionally after repositioning, so suppressing only
    # the reposition would still corrupt the menu with stray deletes).
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("hello")
    v3._sel_anchor, v3._sel_end = (0, 0), (0, 4)  # select "hell"
    v3.set_waiting_probe(lambda: True)
    check("click-guard: _delete_selection refuses while waiting_probe is True",
          v3._delete_selection() is False)
    dels = []
    v3.keyInput.connect(dels.append)
    check("click-guard: _delete_selection sent nothing while waiting",
          dels == [], dels)
    v3.set_waiting_probe(lambda: False)
    check("click-guard: _delete_selection works again once the probe clears",
          v3._delete_selection() is True)


def test_terminal_selection_edit():
    """A mouse selection (double-click word / drag) is editable like an editor
    selection: Backspace/Del deletes it, Ctrl+X cuts it, Ctrl+C copies it.
    Delete/cut drive the child's caret + Backspace, so they act ONLY on a
    single-row selection on the caret's live-screen line; off the input line
    the key is swallowed and only the selection is dropped."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    K = Qt.Key

    def dbl(view, row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        view.mouseDoubleClickEvent(QMouseEvent(
            QEvent.Type.MouseButtonDblClick, p, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

    def press(view, k, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, ""))

    # "hello world": child caret ends at column 11. Double-click "hello".
    v = TerminalView(rows=6, cols=80)
    v.feed("hello world")
    sent = []
    v.keyInput.connect(sent.append)
    dbl(v, 0, 2)
    check("seledit: double-click selects the word", v.selected_text() == "hello",
          v.selected_text())
    press(v, K.Key_Backspace)
    # caret parks just past the span (col 5): 6 Left arrows from col 11, then
    # Backspace over the 5 selected chars
    check("seledit: Backspace deletes the selection (arrows + backspaces)",
          sent == ["\x1b[D" * 6, "\x7f" * 5], sent)
    check("seledit: deleting drops the selection", v.selected_text() == "")

    # Ctrl+X cuts: the word lands on the clipboard AND is deleted from input
    v2 = TerminalView(rows=6, cols=80)
    v2.feed("hello world")
    sent2 = []
    v2.keyInput.connect(sent2.append)
    QGuiApplication.clipboard().clear()
    dbl(v2, 0, 8)   # "world" (cols 6-10)
    press(v2, K.Key_X, ctrl=True)
    check("seledit: Ctrl+X copies the selection to the clipboard",
          QGuiApplication.clipboard().text() == "world",
          QGuiApplication.clipboard().text())
    # caret parks just past "world" (col 11 == caret): 0 arrows, 5 backspaces
    check("seledit: Ctrl+X deletes the cut span", sent2 == ["\x7f" * 5], sent2)
    check("seledit: Ctrl+X drops the selection", v2.selected_text() == "")

    # Ctrl+X with NOTHING selected falls through to the 0x18 control byte
    sent2.clear()
    press(v2, K.Key_X, ctrl=True)
    check("seledit: Ctrl+X with no selection forwards 0x18",
          sent2 == ["\x18"], sent2)

    # a selection OFF the caret's line can't be edited: the key is swallowed
    # (no child bytes, no caret nudge) and only the selection is dropped
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("aaa\r\nbbb")            # caret now on row 1
    sent3 = []
    v3.keyInput.connect(sent3.append)
    dbl(v3, 0, 1)                    # "aaa" on row 0, not the caret's row
    check("seledit: word on another row is selected", v3.selected_text() == "aaa",
          v3.selected_text())
    press(v3, K.Key_Backspace)
    check("seledit: Backspace off the input line sends nothing", sent3 == [], sent3)
    check("seledit: off-line Backspace still clears the selection",
          v3.selected_text() == "")


def test_terminal_input_editor():
    """Desktop-text-area conveniences layered on the passthrough terminal:
    keyboard text-selection (Shift+Arrow/Home/End, Ctrl for word), Shift+click
    extend, triple-click line-select, multi-row selection delete inside the
    input box, and an approximate whole-input undo/redo on Ctrl+Z/Y."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    K = Qt.Key

    def press(view, k, ctrl=False, shift=False, text=""):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, text))

    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                       CELL_PAD_Y + (row + 0.5) * view._cell_h)

    def mpress(view, row, col, shift=False):
        mods = (Qt.KeyboardModifier.ShiftModifier if shift
                else Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, pos(row, col),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods))

    def mdbl(view, row, col):
        view.mouseDoubleClickEvent(QMouseEvent(
            QEvent.Type.MouseButtonDblClick, pos(row, col),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    # --- keyboard selection: Ctrl+Shift+Left word-selects "world", no bytes ---
    view = TerminalView(rows=6, cols=80)
    view.feed("hello world")            # child caret at col 11
    sent = []
    view.keyInput.connect(sent.append)
    press(view, K.Key_Left, ctrl=True, shift=True)
    check("kbdsel: Ctrl+Shift+Left selects the word 'world'",
          view.selected_text() == "world", view.selected_text())
    check("kbdsel: building a selection sends nothing to the child",
          sent == [], sent)
    # a plain (unshifted) arrow collapses the selection, then forwards
    press(view, K.Key_Left)
    check("kbdsel: plain arrow collapses the selection",
          view._selection_range() is None)
    check("kbdsel: the collapsing arrow still forwards to the child",
          sent == ["\x1b[D"], sent)

    # --- Shift+click extends; triple-click selects the whole line -------------
    view = TerminalView(rows=6, cols=80)
    view.feed("hello world")
    mpress(view, 0, 0)                  # anchor at col 0
    mpress(view, 0, 5, shift=True)      # Shift+click extends to col 5
    check("kbdsel: Shift+click extends the selection",
          view.selected_text() == "hello", view.selected_text())

    view = TerminalView(rows=6, cols=80)
    view.feed("aaa bbb ccc")
    mdbl(view, 0, 1)                    # double-click primes the triple
    mpress(view, 0, 1)                  # third press at the same cell
    check("kbdsel: triple-click selects the whole line",
          view.selected_text() == "aaa bbb ccc", view.selected_text())

    # --- multi-row delete inside the input box -------------------------------
    view = TerminalView(rows=6, cols=80)
    view.feed("> line one\r\nline two")  # 2-row input box, caret row1 col8
    out = []
    view.keyInput.connect(out.append)
    view._sel_anchor = (0, 2)            # just after the '> ' prompt
    view._sel_end = (1, 7)              # last char of "line two"
    view._delete_selection()
    # "line one"(8) + one newline + "line two"(8) = 17 backspaces; caret already
    # sits at the selection end (row1 col8) so no arrows are emitted
    check("multidel: multi-row input selection backspaces the whole span",
          out == ["\x7f" * 17], out)

    # a selection that escapes the input box (output row) touches nothing
    view = TerminalView(rows=6, cols=80)
    view.feed("output line\r\n> prompt")  # row0 output, row1 input box
    out = []
    view.keyInput.connect(out.append)
    view._sel_anchor = (0, 0)
    view._sel_end = (0, 5)
    check("multidel: off-input-box selection returns False",
          view._delete_selection() is False)
    check("multidel: off-input-box delete sends nothing", out == [], out)

    # --- approximate undo / redo --------------------------------------------
    view = TerminalView(rows=6, cols=80)
    view.feed("> ")                     # empty prompt
    out = []
    view.keyInput.connect(out.append)
    view.feed("hi")                     # child echoes the typed input -> "> hi"
    view._snapshot_input()              # the debounced burst snapshot fires
    check("undo: a typed burst snapshots the prior (empty) input",
          view._undo_stack == [""] and view._undo_last == "hi",
          (view._undo_stack, view._undo_last))
    press(view, K.Key_Z, ctrl=True)     # undo -> restore ""
    check("undo: Ctrl+Z clears the prompt (empty restore = just the clear)",
          out == ["\x1b\x1b"], out)
    check("undo: undo pushes the current input onto the redo stack",
          view._redo_stack == ["hi"], view._redo_stack)
    out.clear()
    press(view, K.Key_Y, ctrl=True)     # redo -> restore "hi"
    check("undo: Ctrl+Y re-applies the undone input (clear + paste)",
          out == ["\x1b\x1b", "hi"], out)

    # empty stacks fall through to the legacy control bytes
    view = TerminalView(rows=6, cols=80)
    view.feed("> ")
    out = []
    view.keyInput.connect(out.append)
    press(view, K.Key_Z, ctrl=True)
    check("undo: Ctrl+Z with nothing to undo forwards 0x1a", out == ["\x1a"], out)
    out.clear()
    press(view, K.Key_Y, ctrl=True)
    check("undo: Ctrl+Y with nothing to redo forwards 0x19", out == ["\x19"], out)

    # a submit (bare Enter) forgets the history so undo never crosses messages
    view = TerminalView(rows=6, cols=80)
    view.feed("> hi")
    view._snapshot_input()
    view._undo_stack.append("stale")   # pretend prior history exists
    press(view, K.Key_Return)
    check("undo: submit resets the undo/redo history",
          view._undo_stack == [] and view._undo_last == "",
          (view._undo_stack, view._undo_last))


def test_input_gap_self_heal():
    """Claude's classic renderer can scroll the screen for a transient
    dropdown or a slash-command menu and never scroll back on dismissal,
    stranding the input box above a dead run of blank rows (see
    TerminalView._check_input_gap).

    Every fixture here draws the box Claude REALLY draws -- a top border, the
    '> ' row, a bottom border, and a footer hint that wraps onto a second row
    -- because the shipped check never fired once on a real screen: its
    blank-row scan aborted on that border, and the old fixture ('> ' plus a
    one-row footer, no border) drew a screen Claude never paints. Same
    fixture blind spot that hid the reply_anchor_line bug.

    _check_input_gap is called directly rather than pumping the real
    _gap_timer / _snap_timer debounces, matching the suite's existing
    settled-handler pattern."""
    from app.widgets.terminal_view import TerminalView

    RULE = "─" * 30
    BOX = f"{RULE}\r\n> \r\n{RULE}\r\n? for shortcuts\r\n  • high · /effort"

    def stranded(rows=20, history=True, trailer=""):
        """A view whose box sits near the top with nothing below it -- the
        shape left once a menu's rows are erased but the viewport is never
        scrolled back down."""
        view = TerminalView(rows=rows, cols=40)
        if history:
            view.feed("x\r\n" * (rows + 10))   # push lines into history
        # home + erase-down leaves the history alone and repaints the box at
        # the TOP of the screen: what a menu teardown leaves behind, since
        # nothing re-scrolls the viewport back down
        # ...and park the caret back on the '> ' row (row 2, 1-based), where
        # the real renderer leaves it once the footer is painted -- every
        # input-box reading starts from the caret
        view.feed("\x1b[H\x1b[J" + BOX + trailer + "\x1b[2;3H")
        fired = []
        view.staleLayoutDetected.connect(lambda: fired.append(1))
        return view, fired

    view, fired = stranded()
    view._check_input_gap()
    check("input-gap: a real box (border + wrapped footer) stranded high fires",
          fired == [1], fired)
    # the border and the wrapped hint row are exactly what the old blank scan
    # aborted on, so assert the fixture really contains them
    top, bottom = view._input_block_span()
    check("input-gap: the fixture draws the box border the old scan died on",
          view._row_is_rule(bottom + 1), bottom)
    check("input-gap: the fixture's footer wraps past _CLAUDE_READY_HINTS",
          not view._row_is_input_footer(view._last_content_row()),
          view._last_content_row())

    # checking again with nothing changed must NOT refire -- this is what
    # keeps a legitimately short conversation free of a repeated resize blip
    view._check_input_gap()
    check("input-gap: an unchanged gap does not refire", fired == [1], fired)

    # the typing path still works and shares the same edge guard
    view._on_input_settled()
    check("input-gap: the keystroke path does not re-fire the same gap",
          fired == [1], fired)

    # a fresh/cleared conversation legitimately sits high with blank space
    # under it, and its footer walks down a row every turn -- nothing has
    # scrolled off, so nothing can have been scrolled away
    view2, fired2 = stranded(history=False)
    view2._check_input_gap()
    check("input-gap: an empty history never fires",
          fired2 == [] and view2._input_gap_row is None,
          (fired2, view2._input_gap_row))

    # a menu is still open below the box: real content down there means the
    # layout is not stranded, it is just busy
    view3, fired3 = stranded(trailer="\r\n\r\n  1. Opus 5\r\n  2. Sonnet 5")
    view3._check_input_gap()
    check("input-gap: content below the box (an open menu) never fires",
          fired3 == [] and view3._input_gap_row is None,
          (fired3, view3._input_gap_row))

    # An OPEN /model menu, transcribed from a live 30x100 capture: it REPLACES
    # the box, its highlighted row starts with the same '❯' the prompt does,
    # and it ends close enough to the last content row to clear the chrome
    # bound -- so the only thing telling it apart from a real box is that the
    # row under the selection is BLANK rather than the box's border/footer.
    view3b = TerminalView(rows=30, cols=100)
    view3b.feed("x\r\n" * 40)
    view3b.feed("\x1b[H\x1b[J" + RULE + "\r\n  Select model\r\n\r\n"
                "    1. Default\r\n    2. Sonnet\r\n    3. Fable\r\n"
                "  ❯ 4. Opus\r\n    5. Haiku\r\n\r\n"
                "  ● High effort (default)\r\n\r\n"
                "  Enter to set as default · Esc to cancel"
                "\x1b[7;5H")
    fired3b = []
    view3b.staleLayoutDetected.connect(lambda: fired3b.append(1))
    span3b = view3b._input_block_span()
    view3b._check_input_gap()
    check("input-gap: an open menu's selection caret is not the input box",
          fired3b == [] and span3b is not None
          and view3b._row_content(span3b[1] + 1) == (-1, -1),
          (fired3b, span3b))

    # the box sits right at the bottom, within tolerance: nothing is wrong
    view4, fired4 = stranded(rows=6)
    view4._check_input_gap()
    check("input-gap: a box within tolerance of the bottom never fires",
          fired4 == [] and view4._input_gap_row is None,
          (fired4, view4._input_gap_row))

    # mid-reply: the caret is parked on plain output with no '>' above it and
    # blank rows below. _input_block_span keeps top = cy in that case, so
    # without the prompt-glyph guard this reads as a stranded box.
    view5 = TerminalView(rows=20, cols=40)
    view5.feed("x\r\n" * 30)
    view5.feed("thinking about it")
    fired5 = []
    view5.staleLayoutDetected.connect(lambda: fired5.append(1))
    view5._check_input_gap()
    check("input-gap: the caret on plain output is not a stranded box",
          fired5 == [] and view5._input_gap_row is None,
          (fired5, view5._input_gap_row))

    # the output path: a feed arms _gap_timer, so a menu closing with no
    # keystroke at all still gets checked
    view6, _ = stranded()
    check("input-gap: output re-arms the settle timer with no keystroke",
          view6._gap_timer.isActive(), view6._gap_timer.isActive())


def test_scrollback():
    """Wheel scrollback must survive continuous TUI repaints. The old code used
    pyte's prev_page/next_page, but HistoryScreen snaps to the bottom on ANY
    subsequent screen event — Claude Code repaints many times a second, so the
    view was yanked back instantly ('scroll doesn't work'). Now scrollback is a
    view offset that pyte never sees."""
    import re
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=40)
    for i in range(50):
        v.feed(f"scroll-{i}\r\n")
    check("scroll: history accumulates", len(v.screen.history.top) >= 30,
          len(v.screen.history.top))
    check("scroll: live view shows the tail", "scroll-49" in v.visible_text())

    v.scroll_by(15)
    seen = v.visible_text()
    check("scroll: wheel-up reveals older lines", "scroll-26" in seen, seen)
    check("scroll: tail off-screen while scrolled", "scroll-49" not in seen)

    # the regression: new output must NOT snap the view back (old prev_page
    # behavior); the view stays anchored to the content being read
    for i in range(50, 56):
        v.feed(f"scroll-{i}\r\n")
    after = v.visible_text()
    check("scroll: new output does not snap view back", "scroll-26" in after,
          after)
    check("scroll: offset grew to stay content-anchored", v.scroll_offset() > 15,
          v.scroll_offset())

    # copying while scrolled back copies what's on screen (history), not live
    v.select_all()
    check("scroll: copy sees scrolled-back content",
          "scroll-26" in v.selected_text())
    v._sel_anchor = v._sel_end = None

    # typing any key snaps back to live
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                              Qt.KeyboardModifier.NoModifier, "a"))
    check("scroll: keystroke snaps back to live", v.scroll_offset() == 0)
    check("scroll: live tail visible again", "scroll-55" in v.visible_text())

    # Shift+PageUp/PageDown page the view without touching the app
    got = []
    v.keyInput.connect(got.append)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_PageUp,
                              Qt.KeyboardModifier.ShiftModifier, ""))
    check("scroll: Shift+PageUp pages back", v.scroll_offset() == 9,
          v.scroll_offset())
    check("scroll: paging sends nothing to the pty", got == [], got)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_PageDown,
                              Qt.KeyboardModifier.ShiftModifier, ""))
    check("scroll: Shift+PageDown pages forward", v.scroll_offset() == 0)

    # bounds: can't scroll past the oldest line or below the live screen
    v.scroll_by(99999)
    check("scroll: clamped at oldest history line",
          v.scroll_offset() == len(v.screen.history.top))
    v.scroll_by(-99999)
    check("scroll: clamped at live bottom", v.scroll_offset() == 0)

    # ---- wheel routing for fullscreen apps (the Claude Code case) ----
    # Claude Code v2 enters the ALTERNATE SCREEN and enables mouse tracking
    # (?1049h ?1000/1002/1003h ?1006h, verified against v2.1.197): there is no
    # terminal scrollback there — the wheel must be FORWARDED so the app
    # scrolls its own transcript. That's why history-offset scrolling alone
    # read as 'scroll still not working' in Claude terminals.
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    def wheel(view, dy):
        return QWheelEvent(QPointF(50, 30), QPointF(50, 30), QPoint(0, 0),
                           QPoint(0, dy), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier,
                           Qt.ScrollPhase.NoScrollPhase, False)

    sent = []
    v2 = TerminalView(rows=10, cols=40)
    v2.keyInput.connect(sent.append)

    # altscreen WITHOUT mouse tracking (vim/less): wheel -> arrow keys
    v2.feed("\x1b[?1049h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: altscreen wheel-up sends arrow keys",
          sent and sent[-1] == "\x1b[A" * 3, sent[-1:])
    v2.wheelEvent(wheel(v2, -120))
    check("scroll: altscreen wheel-down sends down arrows",
          sent[-1] == "\x1b[B" * 3, sent[-1:])

    # DECCKM application cursor keys switch the arrow encoding
    v2.feed("\x1b[?1h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: DECCKM arrows use SS3 form", sent[-1] == "\x1bOA" * 3,
          sent[-1:])
    v2.feed("\x1b[?1l")

    # mouse tracking + SGR (Claude Code): wheel -> SGR mouse reports
    v2.feed("\x1b[?1000;1006h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: tracked wheel sends SGR mouse reports",
          re.fullmatch(r"(?:\x1b\[<64;\d+;\d+M){3}", sent[-1]) is not None,
          repr(sent[-1]))
    v2.wheelEvent(wheel(v2, -120))
    check("scroll: SGR wheel-down uses button 65",
          sent[-1].startswith("\x1b[<65;"), repr(sent[-1]))

    # leaving altscreen + tracking restores history-offset scrolling
    v2.feed("\x1b[?1000;1006l\x1b[?1049l")
    for i in range(30):
        v2.feed(f"back-{i}\r\n")
    before = len(sent)
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: normal buffer scrolls locally again (nothing sent)",
          len(sent) == before and v2.scroll_offset() == 3,
          (len(sent) - before, v2.scroll_offset()))

    # focus reporting (?1004): Claude Code wants focus in/out events
    v2.feed("\x1b[?1004h")
    sent.clear()
    from PySide6.QtGui import QFocusEvent
    v2.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    v2.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    check("scroll: focus reporting forwards ESC[I / ESC[O",
          sent == ["\x1b[I", "\x1b[O"], sent)


def test_projection_happens_once():
    """The scrollback is projected ONCE, at the card's settled width.

    pyte does not reflow, so a card built before the tiling grid sizes it has
    to project again at the real width. The cost of that is paid on the GUI
    thread per card, and the card used to do the FULL projection twice -- once
    at a width that never reached the screen -- plus a full transcript read
    each time. Measured on real captures: ~80ms + 90-165ms per card, per
    projection, which is what made launch and every retile visibly freeze."""
    import pathlib
    import tempfile

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets import terminal_card as tc

    if not HAS_CONPTY:
        return
    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aihive-project-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    check("project: the seed cap is far smaller than the full cap",
          tc.REPLAY_SEED_CAP < tc.REPLAY_PROJECT_CAP // 4,
          (tc.REPLAY_SEED_CAP, tc.REPLAY_PROJECT_CAP))

    # a buffer much larger than the seed cap, with a marker line right at the
    # start so we can tell a seed-sized projection from a full one
    head = "HEADLINE-" + "h" * 40
    body = "".join(f"body line {i} " + "b" * 40 + "\r\n" for i in range(4000))
    big = head + "\r\n" + body

    # -- an ordinary rebuild (retile / workspace switch): the buffer IS the
    #    live conversation, so the settled projection must project it in full
    live = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Live", cwd=str(tmp), pty=True))
    live._pty_buffer.append(big)
    live._pty_bytes = len(big)
    live._pty_total = len(big)
    card = tc.TerminalCard(live)
    seeded_hist = len(card.terminal.screen.history.top)
    check("project: the constructor projects only a screenful, not the cap",
          0 < seeded_hist < 400, seeded_hist)
    check("project: ...and defers the real one", bool(card._pending_replay))

    card.terminal.resize(900, 500)
    card._rerender_restored()
    full_hist = len(card.terminal.screen.history.top)
    check("project: the settled projection is the FULL one",
          full_hist > seeded_hist * 3, (seeded_hist, full_hist))
    check("project: ...and it projects the LIVE buffer, so a busy agent's "
          "rebuilt card is not left holding only the seed",
          full_hist > 1000, full_hist)
    check("project: it records the width it projected at, so the very next "
          "resize check is not a redundant third projection",
          card._proj_cols == card.terminal.screen.columns,
          (card._proj_cols, card.terminal.screen.columns))
    check("project: the pending marker is consumed, so a resize storm "
          "cannot project again", card._pending_replay == "")
    card.detach(); live.dispose(); pump(30)

    # -- a restored snapshot a child has since drawn over is NOT replayed
    #    under it (an agent that comes back running gets a clean terminal)
    seed = "PREVIOUS-RUN-SCREEN\r\n"
    over = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Over", cwd=str(tmp), pty=True))
    over.seed_pty_replay(seed)
    card2 = tc.TerminalCard(over)
    check("project: a seed no child has touched is not 'written over'",
          not over.seed_written_over())
    over._pty_buffer.append("the child's own output\r\n")
    check("project: ...but it is once the child writes",
          over.seed_written_over())
    card2.terminal.screen.reset()
    card2._rerender_restored()
    check("project: the previous run's screen is never projected under a "
          "live child", "PREVIOUS-RUN-SCREEN" not in card2.terminal.screen_text())
    card2.detach(); over.dispose(); pump(30)

    # -- the backstop: TerminalView._apply_resize returns EARLY when rows/cols
    #    are unchanged, so sizeChanged is not guaranteed to arrive
    quiet = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Quiet", cwd=str(tmp), pty=True))
    quiet._pty_buffer.append(big)
    quiet._pty_bytes = quiet._pty_total = len(big)
    card3 = tc.TerminalCard(quiet)
    check("project: a backstop timer is armed for the settled projection",
          card3._settle_timer.isActive())
    pump(tc.REPLAY_SETTLE_MS + 250)
    check("project: ...and it projects even though sizeChanged never fired",
          card3._pending_replay == ""
          and len(card3.terminal.screen.history.top) > 1000,
          len(card3.terminal.screen.history.top))
    card3.detach(); quiet.dispose(); pump(30)

    # -- dropping the restored screen must cancel the backstop too, or it
    #    fires a moment later and puts back what was just dropped
    dropped = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Dropped", cwd=str(tmp), pty=True))
    dropped.seed_pty_replay(seed)
    card4 = tc.TerminalCard(dropped)
    card4.drop_restored_screen()
    check("project: dropping the restored screen stops the backstop",
          not card4._settle_timer.isActive())
    pump(tc.REPLAY_SETTLE_MS + 150)
    check("project: ...so the dropped screen stays dropped",
          "PREVIOUS-RUN-SCREEN" not in card4.terminal.screen_text())
    card4.detach(); dropped.dispose(); pump(30)


def test_history_screen_wrapper_removed():
    """_FastHistoryScreen drops pyte's per-event wrapper without changing what
    is rendered.

    pyte routes every attribute access on a HistoryScreen through a Python
    __getattribute__ that re-wraps each event in before_event/after_event, both
    of which only serve prev_page/next_page. AI Hive never pages, so they are
    no-ops -- but not free ones: taking the scrollback back made pyte's scroll
    path hot (it shuffles every buffer row per scrolled line), and the tax was
    43% of feed time. This asserts the wrapper is gone AND that its removal is
    invisible, which is the only thing that makes the speedup safe."""
    import pyte
    from app.widgets import terminal_view as tv

    fast = tv._new_history_screen(40, 12)
    check("pyte: the view's screen bypasses HistoryScreen.__getattribute__",
          type(fast).__getattribute__ is object.__getattribute__,
          type(fast).__getattribute__)

    # a stream that scrolls well past the screen, hides/shows the cursor
    # (DECTCEM -- what after_event also maintained), wraps, uses SGR, and
    # finally wipes history with ED 3
    parts = ["\x1b[?25l"]
    for i in range(60):
        parts.append(f"\x1b[3{i % 8}mline {i} " + "x" * (i % 50) + "\r\n")
        if i % 7 == 0:
            parts.append("\x1b[?25h" if i % 14 == 0 else "\x1b[?25l")
    parts.append("\x1b[2;5Hmid-screen\x1b[0m")
    data = "".join(parts)

    def drive(screen):
        stream = pyte.Stream(screen)
        for i in range(0, len(data), 64):     # split mid-sequence deliberately
            stream.feed(data[i:i + 64])
        return screen

    stock = drive(pyte.HistoryScreen(40, 12, history=tv.HISTORY_LINES,
                                     ratio=0.25))
    drive(fast)

    def htext(sc):
        return ["".join(l[x].data for x in sorted(l)) for l in sc.history.top]

    check("pyte: unwrapped screen renders an identical live screen",
          list(fast.display) == list(stock.display),
          (list(fast.display)[:2], list(stock.display)[:2]))
    check("pyte: ...and identical scrollback history",
          htext(fast) == htext(stock),
          (len(htext(fast)), len(htext(stock))))
    check("pyte: ...and identical cursor, incl. DECTCEM hidden state",
          (fast.cursor.x, fast.cursor.y, fast.cursor.hidden)
          == (stock.cursor.x, stock.cursor.y, stock.cursor.hidden),
          (fast.cursor.hidden, stock.cursor.hidden))
    check("pyte: history actually filled (otherwise this proves nothing)",
          len(htext(fast)) > 40, len(htext(fast)))
    check("pyte: pushed still counts every scrolled-off line",
          getattr(fast.history.top, "pushed", 0) == len(htext(stock)),
          getattr(fast.history.top, "pushed", None))

    # ED 3 must still reach _reset_history through the unwrapped call
    pyte.Stream(fast).feed("\x1b[3J")
    check("pyte: ED 3 still wipes history without the wrapper",
          len(fast.history.top) == 0, len(fast.history.top))

    # paging is the one thing the wrapper made safe, so it must fail loudly
    for name in ("prev_page", "next_page"):
        try:
            getattr(fast, name)()
            raised = False
        except NotImplementedError:
            raised = True
        check(f"pyte: {name} raises rather than silently paging away",
              raised, raised)


def test_terminal_scrollbar():
    """The terminal scrollbar and its prompt milestones.

    Covers the coordinate identity everything rests on, the capture point (a
    bare Enter and nothing else), the overlay's no-stolen-columns contract, and
    every path that has to wipe a milestone."""
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap
    from PySide6.QtWidgets import QApplication

    import json

    from app import session_hook, ui_theme
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import PROMPT_MARK_CAP, TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.terminal_scrollbar import WIDTH, TerminalScrollBar
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])

    def enter(view, mods=Qt.KeyboardModifier.NoModifier):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                     Qt.Key.Key_Return, mods))

    # ---- viewChanged: fires on real changes, silent on no-ops -----------
    v = TerminalView(rows=10, cols=40)
    hits = []
    v.viewChanged.connect(lambda: hits.append(1))
    for i in range(30):
        v.feed(f"line {i}\r\n")
    v._view_notify_timer.timeout.emit()      # drive the coalescer directly
    check("scrollbar: viewChanged fires as history grows", hits, hits)
    n = len(hits)
    v.scroll_by(-1)                          # already live: clamped no-op
    check("scrollbar: a clamped no-op scroll emits nothing", len(hits) == n)
    v.scroll_by(5)
    check("scrollbar: scrolling back emits", len(hits) > n)
    n = len(hits)
    v.feed("")                               # nothing pushed, nothing changed
    check("scrollbar: an empty feed emits nothing", len(hits) == n)

    # ---- the absolute-line identity -------------------------------------
    def row_text(view, r):
        hist, off = view._view_state()
        return "".join(view._visible_line(r, hist, off)[c].data or " "
                       for c in range(view.screen.columns)).strip()
    check("scrollbar: abs id identifies the line under it, scrolled back",
          row_text(v, 0) == f"line {v.abs_line_at_row(0)}",
          (row_text(v, 0), v.abs_line_at_row(0)))
    target = v.abs_line_at_row(0)
    v.scroll_to_abs(target, lead=0)
    check("scrollbar: scroll_to_abs(lead=0) puts the line on row 0",
          row_text(v, 0) == f"line {target}", row_text(v, 0))
    oldest, newest = v.history_span()
    check("scrollbar: history_span brackets every live id",
          oldest <= v.abs_line_at_row(0) <= newest, (oldest, newest))

    # ---- history wipe: pushed SURVIVES, ids stay consistent -------------
    cleared = []
    v.historyCleared.connect(lambda: cleared.append(1))
    before = v.history_pushed()
    v.feed("\x1b[3J")
    check("scrollbar: ED 3 fires historyCleared", cleared == [1], cleared)
    check("scrollbar: ...and empties history", len(v.screen.history.top) == 0)
    check("scrollbar: ...and snaps back to live", v.scroll_offset() == 0)
    check("scrollbar: ...but `pushed` is deliberately NOT reset",
          v.history_pushed() == before, (v.history_pushed(), before))
    v.feed("after the wipe\r\n" * 12)
    check("scrollbar: ids stay self-consistent across a wipe",
          row_text(v, 0) == "after the wipe"
          or v.abs_line_at_row(0) >= before, v.abs_line_at_row(0))

    # ---- capture point: a bare Enter, and nothing else -------------------
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Marks", cwd=SCRATCH_CWD,
                                     pty=True))
    agent.worker.start = lambda: None  # Enter on a stopped card wakes it
    card = TerminalCard(agent)
    card.resize(640, 420)
    t = card.terminal
    cols_without_bar = t.screen.columns

    t.feed("> refactor the parser")
    enter(t)
    check("scrollbar: a bare Enter records a milestone",
          [m.text for m in agent.prompt_marks()] == ["refactor the parser"],
          agent.prompt_marks())
    check("scrollbar: ...and the view is given it in view coordinates",
          len(t.marks()) == 1 and t.marks()[0][0] == card._mark_lines[
              agent.prompt_marks()[0].uid], t.marks())

    # the classic renderer parks the caret on a BLANK row below the box, which
    # is what made the span reading come back empty and record nothing
    below = TerminalView(rows=20, cols=40)
    below.feed("output\r\n" * 14 + "─" * 30 + "\r\n> find the leak\x1b[19;3H")
    check("scrollbar: input is still found when the caret sits below the box",
          below._submitted_input() == (15, "find the leak"),
          below._submitted_input())
    below2 = TerminalView(rows=20, cols=40)
    below2.feed("output\r\n" * 14 + "─" * 30 + "\r\n\x1b[19;3H")
    check("scrollbar: ...but an empty box behind that rule records nothing",
          below2._submitted_input() is None, below2._submitted_input())
    below3 = TerminalView(rows=20, cols=40)
    below3.feed("conversation output\r\n\r\n")
    check("scrollbar: ...and a caret up in the conversation records nothing",
          below3._submitted_input() is None, below3._submitted_input())

    for mods, label in ((Qt.KeyboardModifier.ShiftModifier, "Shift"),
                        (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
                        (Qt.KeyboardModifier.AltModifier, "Alt")):
        t.feed("\r\n> a newline, not a submit")
        enter(t, mods)
        check(f"scrollbar: {label}+Enter is a newline, never a milestone",
              len(agent.prompt_marks()) == 1, agent.prompt_marks())

    t.feed("\r\n> ")
    enter(t)
    check("scrollbar: an empty input records nothing",
          len(agent.prompt_marks()) == 1)

    t.feed("\r\n> 1. Yes, proceed")
    enter(t)
    check("scrollbar: a numbered menu row is an ANSWER, not a milestone",
          len(agent.prompt_marks()) == 1, agent.prompt_marks())

    # AI Hive's own writes must never look like the user typing
    agent.write("\r")
    agent.nudge("Continue")
    agent.deliver_task("go and do the thing")
    check("scrollbar: write/nudge/deliver_task record no milestones",
          len(agent.prompt_marks()) == 1, agent.prompt_marks())

    # ---- uid is never an id() ------------------------------------------
    evicted = agent.prompt_marks()[0].uid
    evicted_line = card._mark_lines.get(evicted)
    for i in range(PROMPT_MARK_CAP + 5):
        t.feed(f"\r\n> prompt {i}")
        enter(t)
    uids = [m.uid for m in agent.prompt_marks()]
    check("scrollbar: the milestone list is FIFO-capped",
          len(uids) == PROMPT_MARK_CAP, len(uids))
    check("scrollbar: a surviving uid never reuses an evicted one",
          evicted not in uids and len(set(uids)) == len(uids))
    card._refresh_marks()
    painted = sorted(line for line, _ in card.terminal.marks())
    check("scrollbar: an evicted milestone is not still painted",
          painted == sorted(card._mark_lines[u] for u in uids)
          and evicted_line not in painted, (evicted_line, painted[:3]))

    # ---- re-anchoring across a card rebuild ----------------------------
    agent2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Replay", cwd=SCRATCH_CWD,
                                      pty=True))
    agent2.worker.start = lambda: None  # Enter on a stopped card wakes it
    c1 = TerminalCard(agent2)
    c1.resize(640, 420)
    for k in range(4):
        agent2._on_pty_output("pty", f"> prompt {k}\r\n")
        c1.terminal.feed(f"> prompt {k}")
        enter(c1.terminal)
        for i in range(20):
            agent2._on_pty_output("pty", f"  reply {k} line {i}\r\n")
    c2 = TerminalCard(agent2)          # what a retile does
    c2.resize(640, 420)
    check("scrollbar: a rebuilt card re-derives the SAME milestone lines",
          c1._mark_lines == c2._mark_lines,
          (c1._mark_lines, c2._mark_lines))
    check("scrollbar: every milestone is anchored inside the new history",
          all(c2.terminal.history_span()[0] <= line
              <= c2.terminal.history_span()[1]
              for line in c2._mark_lines.values()))
    agent2._pty_dropped = 10 ** 9      # everything has aged out of the buffer
    check("scrollbar: a milestone whose bytes aged out is dropped, not clamped",
          agent2.replay_marks() == [], agent2.replay_marks())

    # ---- the overlay contract ------------------------------------------
    check("scrollbar: it is a child of the terminal, not a layout sibling",
          card.scroll_bar.parent() is t)
    check("scrollbar: it never takes focus off the terminal",
          card.scroll_bar.focusPolicy() == Qt.FocusPolicy.NoFocus)
    check("scrollbar: it steals no terminal columns",
          t.screen.columns == cols_without_bar,
          (t.screen.columns, cols_without_bar))
    bare = TerminalView(rows=10, cols=40)
    bar = TerminalScrollBar(bare, bare)
    bar.refresh()
    check("scrollbar: hidden while there is no scrollback",
          not bar.isVisibleTo(bare))
    for i in range(40):
        bare.feed(f"line {i}\r\n")
    bar.refresh()
    check("scrollbar: visible once there is", bar.isVisibleTo(bare))
    check("scrollbar: its range is the history depth",
          bar.maximum() == len(bare.screen.history.top), bar.maximum())
    check("scrollbar: maximum means live (offset 0)",
          bar.value() == bar.maximum() - bare.scroll_offset())
    card._place_overlay()
    geo = card.scroll_bar.geometry()
    check("scrollbar: pinned to the right edge, full height",
          geo.right() >= t.width() - 2 and geo.height() == t.height()
          and geo.width() == WIDTH, geo)

    # ---- markers paint, and a click on one jumps -------------------------
    bare.resize(WIDTH, 200)
    bar.resize(WIDTH, 200)
    oldest, newest = bare.history_span()
    marks = [(oldest + 2, "first prompt"), (oldest + 20, "second prompt")]
    bare.set_marks(marks)
    bar.refresh()
    pm = QPixmap(bar.size())
    pm.fill()
    bar.render(pm)
    img = pm.toImage()
    accent = ui_theme.Palette.ACCENT_ORANGE
    want = (int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16))

    def band_has_accent(y):
        for dy in range(-3, 4):
            yy = y + dy
            if not (0 <= yy < img.height()):
                continue
            for x in range(img.width()):
                c = img.pixelColor(x, yy)
                if (abs(c.red() - want[0]) < 60 and abs(c.green() - want[1]) < 60
                        and abs(c.blue() - want[2]) < 60):
                    return True
        return False
    check("scrollbar: a milestone is painted at its own position",
          band_has_accent(int(bar._y_for(marks[0][0]))))
    check("scrollbar: ...and so is the second one",
          band_has_accent(int(bar._y_for(marks[1][0]))))

    jumped = []
    bar.markActivated.connect(jumped.append)
    bar.markActivated.connect(bare.scroll_to_abs)
    y = bar._y_for(marks[1][0])
    bar.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPoint(int(WIDTH / 2), int(y)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("scrollbar: clicking a milestone activates THAT milestone",
          jumped == [marks[1][0]], jumped)
    check("scrollbar: ...and the view lands on it",
          bare.abs_line_at_row(2) == marks[1][0], bare.abs_line_at_row(2))

    # ---- a fresh milestone is drawable BEFORE it scrolls off ------------
    live = TerminalView(rows=10, cols=40)
    livebar = TerminalScrollBar(live, live)
    livebar.resize(WIDTH, 200)
    for i in range(30):
        live.feed(f"line {i}\r\n")
    livebar.refresh()
    fresh = live.history_pushed() + 3          # still on the live screen
    live.set_marks([(fresh, "just typed")])
    oldest, span = livebar._span()
    check("scrollbar: a milestone on the LIVE screen is inside the track",
          oldest <= fresh <= oldest + span, (oldest, span, fresh))
    pm2 = QPixmap(livebar.size())
    pm2.fill()
    livebar.render(pm2)
    img2 = pm2.toImage()
    found = False
    for dy in range(-4, 5):
        yy = int(livebar._y_for(fresh)) + dy
        if not (0 <= yy < img2.height()):
            continue
        for x in range(img2.width()):
            c = img2.pixelColor(x, yy)
            if (abs(c.red() - want[0]) < 60 and abs(c.green() - want[1]) < 60
                    and abs(c.blue() - want[2]) < 60):
                found = True
    check("scrollbar: ...and is actually painted, not waiting to scroll off",
          found)

    # ---- width change re-projects the scrollback -------------------------
    wide = TerminalAgent(build_spec(AgentKind.CLAUDE, "Reflow", cwd=SCRATCH_CWD,
                                    pty=True))
    wcard = TerminalCard(wide)
    wt = wcard.terminal
    wt.screen.resize(20, 30)                   # a pre-layout narrow card
    wcard._proj_cols = 30
    wide._on_pty_output("pty", ("A very long line of conversation text that "
                                "must wrap at thirty columns\r\n") * 20)
    narrow_lines = len(wt.screen.history.top)
    wt.screen.resize(20, 100)                  # the tiling grid widens it
    wcard._reproject_on_size(20, 100)
    check("scrollbar: widening re-projects the scrollback instead of "
          "leaving it wrapped for a screen that is gone",
          len(wt.screen.history.top) < narrow_lines,
          (narrow_lines, len(wt.screen.history.top)))
    joined = "".join(
        "".join(ln[c].data or " " for c in range(100)).rstrip() + "\n"
        for ln in wt.screen.history.top)
    check("scrollbar: ...and the re-projected lines use the full width",
          any(len(l) > 40 for l in joined.splitlines()),
          max((len(l) for l in joined.splitlines()), default=0))
    before_cols = len(wt.screen.history.top)
    wcard._reproject_on_size(30, 100)          # height-only change
    check("scrollbar: a height-only change re-projects nothing",
          len(wt.screen.history.top) == before_cols)
    wcard.deleteLater()

    # ---- milestones recovered for a conversation we did not watch --------
    from app import transcripts
    rtmp = Path(tempfile.mkdtemp(prefix="ai-hive-marks-"))
    conv = rtmp / "conv.jsonl"
    typed = ["refactor the session pinning logic",
             "now write the regression tests for it",
             "explain why the anchor drifts by one line"]
    recs = []
    for i, text in enumerate(typed):
        recs.append(json.dumps({
            "type": "user", "promptSource": "typed",
            "origin": {"kind": "human"}, "isSidechain": False,
            "timestamp": f"2026-08-09T1{i}:00:00.000Z",
            "message": {"role": "user", "content": text}}))
    # noise that must NOT become a milestone
    recs.append(json.dumps({
        "type": "user", "promptSource": "typed", "isSidechain": True,
        "message": {"role": "user", "content": "a sub-agent turn"}}))
    recs.append(json.dumps({
        "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x"}]}}))
    recs.append(json.dumps({
        "type": "user", "promptSource": "typed", "message": {
            "role": "user",
            "content": "<local-command-stdout>Set model to Opus</local-command-stdout>"}}))
    conv.write_text("\n".join(recs) + "\n", encoding="utf-8")

    real_tp = transcripts.transcript_path
    transcripts.transcript_path = lambda cwd, sid: str(conv)
    try:
        got = transcripts.typed_prompts(str(rtmp), "sid")
        check("scrollbar: the transcript yields exactly the typed prompts",
              got == typed, got)

        rag = TerminalAgent(build_spec(AgentKind.CLAUDE, "Recover",
                                       cwd=str(rtmp), pty=True))
        rag.spec.session_id = "sid"
        rcard = TerminalCard(rag)
        rcard.terminal.screen.resize(24, 80)
        # a conversation replayed from disk: the prompts are echoed in the
        # scrollback, but no keystroke was ever seen by this process
        stream = ""
        for text in typed:
            stream += f"> {text}\r\n"
            stream += "".join(f"  reply line {i}\r\n" for i in range(9))
        rag._on_pty_output("pty", stream)
        rcard._recover_marks()
        rcard._refresh_marks()
        found = [t for _line, t in rcard._recovered]
        check("scrollbar: every earlier prompt is recovered from the "
              "scrollback", found == typed, found)
        check("scrollbar: ...and none of them came from a live keystroke",
              rag.prompt_marks() == [])
        rv = rcard.terminal
        hist_r = list(rv.screen.history.top)
        all_rows = hist_r + [rv.screen.buffer[r] for r in range(rv.screen.lines)]
        oldest_r = rv.history_pushed() - len(hist_r)
        ok = True
        for line, text in rcard._recovered:
            idx = line - oldest_r
            if not (0 <= idx < len(all_rows)):
                ok = False
                continue
            row = "".join(all_rows[idx][c].data or " " for c in range(80))
            if text[:20] not in row:
                ok = False
        check("scrollbar: ...and each dot sits on that prompt's own line", ok)
        check("scrollbar: recovered milestones reach the view",
              len(rcard.terminal.marks()) == len(typed),
              rcard.terminal.marks())

        # a prompt the transcript does not contain is never marked
        rag2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "NoMatch",
                                        cwd=str(rtmp), pty=True))
        rag2.spec.session_id = "sid"
        rcard2 = TerminalCard(rag2)
        rcard2.terminal.screen.resize(24, 80)
        rag2._on_pty_output("pty", "> something nobody ever typed\r\n" * 30)
        rcard2._recover_marks()
        check("scrollbar: a line that matches no typed prompt gets no dot",
              rcard2._recovered == [], rcard2._recovered)
        rcard.deleteLater()
        rcard2.deleteLater()
    finally:
        transcripts.transcript_path = real_tp

    # ---- resets ---------------------------------------------------------
    replaced = []
    agent2.conversation_replaced.connect(lambda: replaced.append(1))
    agent2.note_conversation_replaced()
    check("scrollbar: a replaced conversation drops every milestone",
          agent2.prompt_marks() == [] and replaced == [1])
    c2.terminal.clear_history()
    check("scrollbar: ...and its scrollback, so the bar goes away",
          len(c2.terminal.screen.history.top) == 0
          and c2._mark_lines == {} and c2.terminal.marks() == [])
    # a stub: from IDLE, PtyWorker.restart() STARTS a real claude
    agent.worker.restart = lambda: None
    agent.restart()
    check("scrollbar: a restart clears milestones and stream coordinates",
          agent.prompt_marks() == [] and agent._pty_total == 0
          and agent._pty_dropped == 0)

    # ---- the tui renderer setting --------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tui-"))
    sp, mp, ep = (str(tmp / "s.json"), str(tmp / "m.jsonl"),
                  str(tmp / "e.jsonl"))
    session_hook.write_settings_file(sp, mp, ep, tui="default")
    data = json.loads(Path(sp).read_text(encoding="utf-8"))
    check("scrollbar: tui='default' selects the classic renderer",
          data.get("tui") == "default", data.get("tui"))
    check("scrollbar: the renderer key never disturbs the hook matcher",
          data["hooks"]["SessionStart"][0]["matcher"] == "resume|clear|compact",
          data["hooks"]["SessionStart"][0]["matcher"])
    session_hook.write_settings_file(sp, mp, ep)
    data = json.loads(Path(sp).read_text(encoding="utf-8"))
    check("scrollbar: omitted when AI Hive is not owning the scrollback",
          "tui" not in data, data)
    check("scrollbar: ...and the hooks are still intact",
          data["hooks"]["SessionStart"][0]["matcher"] == "resume|clear|compact")

    card.deleteLater()
    c1.deleteLater()
    c2.deleteLater()


def test_header_tools_tray_has_no_dead_space():
    """The hover tray (A- / A+ / maximize) must be solid buttons edge to edge,
    as tall as its header strip, and must close when the pointer is above or
    below it, not only when it leaves the strip sideways."""
    from PySide6.QtCore import QEventLoop, QPoint, QTimer
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Tray", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    ht = card.header_tools
    ht._set_open(True); pump(30)
    tray = ht.tray
    check("tray: no spacing between the buttons", tray.layout().spacing() == 0)
    check("tray: only the 1px border around the buttons",
          tuple(tray.layout().contentsMargins().__getattribute__(n)()
                for n in ("left", "top", "right", "bottom")) == (1, 1, 1, 1))
    check("tray: as tall as the strip it opens from",
          tray.height() >= ht.height(), (tray.height(), ht.height()))
    # pointer just above the tray, still over the strip's column
    above = tray.mapToGlobal(QPoint(tray.width() // 2, -3))
    QCursor.setPos(above); pump(30)
    ht._recheck()
    check("tray: closes when the pointer is above it",
          not tray.isVisible())

    card.detach(); card.close()
    a.deleteLater()


def test_header_tray_carries_the_card_actions():
    """The actions that used to hide behind a right-click on the header
    (start, stop, restart, assign, scheduled send, the lane actions) are
    icon buttons in the hover tray, each with a tooltip naming it. The user
    had forgotten the right-click menu existed."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import AgentStatus, TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard, _CardHeader

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Acts", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    ht = card.header_tools

    def shown():
        return [b for b in ht._buttons if not b.isHidden()]

    check("actions: the header has no right-click menu any more",
          "contextMenuEvent" not in _CardHeader.__dict__
          and not hasattr(card, "show_actions_menu"))
    ht._set_open(True); pump(20)
    lifecycle = [card.btn_start, card.btn_stop, card.btn_restart,
                 card.btn_assign, card.btn_sched]
    check("actions: start/stop/restart/assign/schedule are in the tray",
          all(b in shown() for b in lifecycle))
    check("actions: every tray button has a tooltip",
          all(b.toolTip().strip() for b in shown()),
          [(b.text(), b.toolTip()) for b in shown()])
    check("actions: lane buttons stay out with nothing to offer",
          card.btn_adopt not in shown()
          and card.btn_integrator not in shown())
    check("actions: an idle agent can start but not stop",
          card.btn_start.isEnabled() and not card.btn_stop.isEnabled())
    # a status change while the tray is open updates what is clickable
    a.status = AgentStatus.RUNNING
    card._on_status(AgentStatus.RUNNING)
    check("actions: a running agent can stop but not start",
          card.btn_stop.isEnabled() and not card.btn_start.isEnabled())
    a.status = AgentStatus.IDLE
    card._on_status(AgentStatus.IDLE)
    ht._set_open(False)

    # what the integration queue offers decides the lane buttons, and its
    # label and reason land in their tooltips
    card.integration_info = lambda _agent: {
        "adopt": ("Restart in own lane (new conversation)", False,
                  "This agent is working."),
        "role": ("Make integrator", True, "Types each lane into it.")}
    ht._set_open(True); pump(20)
    check("actions: offered lane buttons appear",
          card.btn_adopt in shown() and card.btn_integrator in shown())
    check("actions: a refused lane action is disabled, with its reason",
          not card.btn_adopt.isEnabled()
          and card.btn_adopt.toolTip().startswith("Restart in own lane")
          and "This agent is working." in card.btn_adopt.toolTip(),
          card.btn_adopt.toolTip())
    got = []
    card.laneActionRequested.connect(lambda aid, act: got.append(act))
    card.btn_integrator.click()
    check("actions: the integrator button asks for the integrator action",
          got == ["integrator"], got)
    ht._set_open(False)

    # the Activity panel's no-integrator hint points at the tray, not at the
    # right-click menu that is gone
    from app.widgets.activity_panel import ActivityPanel
    panel = ActivityPanel()
    panel.set_integration({"integrator": "", "laned": True, "items": []})
    hint = panel.queue_info.text()
    check("actions: the no-integrator hint names the tray, not a right-click",
          "right-click" not in hint.lower() and "⋯" in hint
          and "Make integrator" in hint, hint)
    panel.deleteLater()

    card.detach(); card.close()
    a.deleteLater()


def test_header_tools_tray_closes_without_a_leave_event():
    """A slow exit up or down can fire Leave while QCursor.pos() still rounds
    inside the tray (fractional DPI), and then no further event reaches it.
    The tray must notice the pointer is gone by itself, with no Leave and no
    explicit _recheck call."""
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QTimer
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Tray", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    ht = card.header_tools
    tray = ht.tray
    ht._set_open(True); pump(30)
    QCursor.setPos(tray.mapToGlobal(QPoint(tray.width() // 2,
                                           tray.height() // 2)))
    pump(ht._WATCH_MS * 2)
    check("tray: stays open while the pointer is on it", tray.isVisible())
    # just below the tray, over the terminal, with every Enter/Leave eaten:
    # offscreen Qt turns setPos into real crossing events, which would hide
    # the very case this guards
    class _NoCrossing(QObject):
        def eventFilter(self, obj, ev):
            return ev.type() in (QEvent.Type.Enter, QEvent.Type.Leave)
    eat = _NoCrossing()
    QApplication.instance().installEventFilter(eat)
    try:
        QCursor.setPos(tray.mapToGlobal(QPoint(tray.width() // 2,
                                               tray.height() + 2)))
        pump(ht._WATCH_MS * 3)
    finally:
        QApplication.instance().removeEventFilter(eat)
    check("tray: closes on its own once the pointer is below it",
          not tray.isVisible())
    check("tray: the watch timer stops when the tray closes",
          not ht._watch.isActive())

    card.detach(); card.close()
    a.deleteLater()
