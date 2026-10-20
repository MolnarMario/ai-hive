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


def test_terminal_input_method():
    """Win+V and Win+. panels read keys through the input context, which Qt
    detaches from a focused widget that has no input method. The terminal
    must keep it, place the panel at its cursor and type committed text."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    view = TerminalView(rows=6, cols=40)
    sent = []
    view.keyInput.connect(sent.append)

    check("ime: terminal enables the input method",
          view.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled))
    check("ime: query reports enabled",
          view.inputMethodQuery(Qt.InputMethodQuery.ImEnabled) is True)

    view.feed("one\r\ntwo> ab")
    x0, y0, x1, y1 = view.cell_bounds(view.screen.cursor.x,
                                      view.screen.cursor.y)
    got = view.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
    check("ime: popup anchors at the cursor cell",
          got == QRect(x0, y0, x1 - x0, y1 - y0), f"got {got}")

    view.inputMethodEvent(QInputMethodEvent("に", []))  # preedit only
    check("ime: preedit sends nothing", sent == [], f"sent {sent!r}")
    check("ime: the composition is kept for drawing", view._preedit == "に",
          repr(view._preedit))
    ev = QInputMethodEvent()
    ev.setCommitString("\U0001F600")
    view.inputMethodEvent(ev)
    check("ime: committed text is typed", sent == ["\U0001F600"],
          f"sent {sent!r}")
    check("ime: a commit ends the composition", view._preedit == "",
          repr(view._preedit))

    # a commit with a line break must not reach Claude as Enter
    sent.clear()
    view.feed("\x1b[?2004h")
    ev = QInputMethodEvent()
    ev.setCommitString("one\ntwo")
    view.inputMethodEvent(ev)
    check("ime: a multi-line commit goes as a bracketed paste",
          sent == ["\x1b[200~one\rtwo\x1b[201~"], f"sent {sent!r}")

    # Qt keeps the rectangle it read at focus-in; a repaint after the cursor
    # moved must hand it the new one
    view.hasFocus = lambda: True
    view.grab()
    first = view._im_rect
    view.feed("\r\nthree")
    view.grab()
    check("ime: a repaint tells Qt the cursor moved",
          first is not None and view._im_rect != first
          and view._im_rect == view.inputMethodQuery(
              Qt.InputMethodQuery.ImCursorRectangle),
          f"{first} -> {view._im_rect}")

    # the composition is drawn at the cursor while it is in progress
    plain = view.grab().toImage()
    view.inputMethodEvent(QInputMethodEvent("にほん", []))
    drawn = view.grab().toImage()
    check("ime: a composition in progress is drawn", drawn != plain)


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


def test_replay_reads_each_part_at_its_drawn_size():
    """A card rebuild or a width change must not double a Claude reply.

    Live-reported: a reply's text showed up several times over, each copy a
    little longer. Claude's classic renderer erases its streaming frame by
    moving up one row per line it drew, at the width it was told. The card
    replayed the raw stream at its CURRENT width on every width change and
    rebuild, so the erase missed rows and every older frame stayed stacked
    above the next. The agent now records the view's size at its stream
    offset and the replay feeds each part at the size it was drawn at."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])

    def ink_reply(word, width):
        # the classic renderer's stream: erase the frame's rows, redraw longer
        out, prev = "", 0
        for n in range(4, 40, 6):
            text = word + ": " + " ".join(f"w{i}" for i in range(n))
            if prev:
                out += "\x1b[2K\x1b[1A" * (prev - 1) + "\x1b[2K\r"
            rows = [text[i:i + width] for i in range(0, len(text), width)]
            out += "\r\n".join(rows)
            prev = len(rows)
        return out + "\r\n> \r\n"

    def size(card, rows, cols):        # what TerminalView._apply_resize does
        card.terminal.screen.resize(rows, cols)
        card.terminal.sizeChanged.emit(rows, cols)

    def starts(view, word):
        scr = view.screen
        lines = list(scr.history.top) + [scr.buffer[r] for r in range(scr.lines)]
        return sum(1 for ln in lines
                   if "".join(ln[c].data for c in range(80)).startswith(word))

    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Geometry",
                                     cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    card._proj_cols = card.terminal.screen.columns
    size(card, 20, 60)
    agent._on_pty_output("pty", "header\r\n" + ink_reply("Alpha", 60))
    check("geometry: the live view shows the reply once",
          starts(card.terminal, "Alpha:") == 1, starts(card.terminal, "Alpha:"))
    flat = TerminalView(rows=20, cols=40)
    flat.feed(agent.pty_replay())
    check("geometry: (fixture) fed flat at 40 cols the reply doubles",
          starts(flat, "Alpha:") > 1, starts(flat, "Alpha:"))

    fed = []
    real_feed = card.terminal.feed
    card.terminal.feed = lambda d: (fed.append(d), real_feed(d))[1]
    size(card, 20, 40)                 # the card narrows
    check("geometry: a width change does not re-feed a live stream",
          fed == [] and starts(card.terminal, "Alpha:") == 1,
          (len(fed), starts(card.terminal, "Alpha:")))
    del card.terminal.feed
    off40 = agent._pty_total
    agent._on_pty_output("pty", ink_reply("Beta", 40))
    check("geometry: the sizes are recorded at their stream offsets",
          [(off, g.cols) for off, g in agent.replay_geometry()][-2:]
          == [(0, 60), (off40, 40)],
          [(off, g.rows, g.cols) for off, g in agent.replay_geometry()])

    card2 = TerminalCard(agent)        # a retile / workspace switch
    size(card2, 20, 40)
    check("geometry: a rebuilt card shows each reply once",
          starts(card2.terminal, "Alpha:") == 1
          and starts(card2.terminal, "Beta:") == 1,
          (starts(card2.terminal, "Alpha:"), starts(card2.terminal, "Beta:")))
    check("geometry: ...and is back at its own size afterwards",
          (card2.terminal.screen.lines, card2.terminal.screen.columns)
          == (20, 40))

    # a capped replay that starts after a recorded size starts AT that size
    sizes = []
    real_ps = card2.terminal.project_size
    card2.terminal.project_size = lambda r, c: (sizes.append((r, c)),
                                                real_ps(r, c))[1]
    replay = agent.pty_replay()
    card2._clear_for_projection()
    card2._replay_with_marks(replay, cap=len(replay) - off40 - 5,
                             recover=False)
    check("geometry: a capped replay starts at the size in force there",
          sizes[:1] == [(20, 40)] and sizes[-1] == (20, 40), sizes)
    del card2.terminal.project_size

    # a size and a prompt mark at one offset replay in the order they came
    log = []
    agent.note_prompt_submitted("first")
    agent.resize(20, 50)
    agent.note_prompt_submitted("second")
    card2.terminal.project_size = lambda r, c: (log.append(("size", c)),
                                                real_ps(r, c))[1]
    real_anchor = card2.terminal.anchor_line
    card2.terminal.anchor_line = lambda: (log.append(("mark",)),
                                          real_anchor())[1]
    card2._clear_for_projection()
    card2._replay_with_marks(agent.pty_replay(), recover=False)
    tail = [e for e in log if e == ("mark",) or e == ("size", 50)]
    check("geometry: a mark and a resize at one offset keep their order",
          tail == [("mark",), ("size", 50), ("mark",)], log)
    del card2.terminal.project_size, card2.terminal.anchor_line

    # a restart starts a new stream at the view's current size
    agent.begin_fresh_screen()
    check("geometry: a restart keeps the view's size for the new stream",
          [(off, g.cols) for off, g in agent.replay_geometry()] == [(0, 50)],
          [(off, g.cols) for off, g in agent.replay_geometry()])
    card.detach(); card2.detach(); agent.dispose()

    # dropping a restored seed must not leave buffer offsets counting it
    seeded = TerminalAgent(build_spec(AgentKind.CLAUDE, "Seeded",
                                      cwd=SCRATCH_CWD, pty=True))
    seeded.seed_pty_replay("PREVIOUS-RUN " * 50)
    seeded.drop_seeded_screen()
    seeded._on_pty_output("pty", "hello\r\n")
    seeded.note_prompt_submitted("typed")
    check("geometry: offsets after a dropped seed point into the buffer",
          [off for off, _ in seeded.replay_marks()]
          == [len(seeded.pty_replay())],
          ([off for off, _ in seeded.replay_marks()], len(seeded.pty_replay())))
    seeded.dispose()

    # a pristine restored seed records no sizes: the card projects it again
    # at every width, and sizes at its end cut its lines to the narrowest
    restored = TerminalAgent(build_spec(AgentKind.CLAUDE, "Restored",
                                        cwd=SCRATCH_CWD, pty=True))
    seed = "PREVIOUS-RUN " * 50
    restored.seed_pty_replay(seed)
    restored.resize(30, 60)
    restored.resize(30, 120)
    check("geometry: resizes over a pristine seed record no size",
          restored.replay_geometry() == [], restored.replay_geometry())
    restored._on_pty_output("pty", "drawn over\r\n")
    check("geometry: the first bytes after a seed record the view's size",
          [(off, g.cols) for off, g in restored.replay_geometry()]
          == [(len(seed), 120)],
          [(off, g.cols) for off, g in restored.replay_geometry()])
    restored.dispose()

    # a card that never resized (a workspace not shown yet): the first bytes
    # are read at the size the child was spawned at
    unseen = TerminalAgent(build_spec(AgentKind.CLAUDE, "Unseen",
                                      cwd=SCRATCH_CWD, pty=True))
    unseen._on_pty_output("pty", "hello\r\n")
    check("geometry: with no view size the spawn size is recorded",
          [(off, g.rows, g.cols) for off, g in unseen.replay_geometry()]
          == [(0, unseen.worker.rows, unseen.worker.cols)],
          [(off, g.rows, g.cols) for off, g in unseen.replay_geometry()])
    unseen.dispose()


def _reflow_render(width):
    """The rows Claude's classic renderer paints for one conversation at
    `width`: banner, the prompt's echo, a reply whose items wrap with a
    hanging indent, the input box. Every word is unique, so a row that shows
    up twice, or at another width, is the bug and not a coincidence."""
    import textwrap
    rows = ["Claude Code v9", "", "> Explain the sorting algorithms please",
            ""]
    for n in range(1, 9):
        words = " ".join(f"w{n}x{i}" for i in range(30))
        rows += textwrap.wrap(f"{n}. Item {n} {words}", width,
                              subsequent_indent="   ")
    return rows + ["", "-" * width, "> ", "-" * width, "  ? for shortcuts"]


def _conhost_paint(rows, viewport):
    """What conhost sends after a resize, as captured from claude 2.1.296:
    erase each of its rows, home, then the frame row by row, ending on the
    blank row the renderer's cursor sits on, and the caret back up in the
    input box."""
    return ("\x1b[?25l\x1b[H" + "\x1b[2K\x1b[1B" * viewport + "\x1b[H"
            + "\r\n".join(rows) + "\r\n\x1b[2C\x1b[3A\x1b[?25h")


def test_reflow_reprints_scrollback_at_new_size():
    """A resized Claude card shows its whole scrollback at the new width.

    Live-reported: widen an agent's card and only the bottom screenful of a
    reply re-wrapped; everything above kept the old width, and the old
    screen's last rows repeated at the top of the new one. Claude reprints
    the whole conversation on a resize, but ConPTY forwards only one
    screenful of it. The agent now gives the child REFLOW_EXTRA_ROWS more
    rows once it is idle, so the reprint arrives whole, and marks the stream
    so the view drops the old scrollback and lines its screen up with
    conhost's before the child gets its own rows back."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import (REFLOW_BEGIN, REFLOW_END,
                                    REFLOW_EXTRA_ROWS, AgentStatus,
                                    TerminalAgent)
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])
    rows_n = 10

    class Worker:
        """A running child: records each size it is given."""
        def __init__(self):
            self.rows, self.cols, self.sizes = rows_n, 40, []
            self.exit_info = None

        def is_running(self):
            return True

        def resize(self, rows, cols):
            if (rows, cols) == (self.rows, self.cols):
                return False
            self.rows, self.cols = rows, cols
            self.sizes.append((rows, cols))
            return True

        def write(self, data):
            return True

        def flush(self):
            pass

        def dispose(self):
            pass

    def size(card, rows, cols):        # what TerminalView._apply_resize does
        card.terminal.screen.resize(rows, cols)
        card.terminal.sizeChanged.emit(rows, cols)

    def screen_rows(view):
        scr = view.screen
        lines = list(scr.history.top) + [scr.buffer[r]
                                         for r in range(scr.lines)]
        return ["".join(ln[c].data for c in range(scr.columns)).rstrip()
                for ln in lines]

    def mark_row(card, uid):
        view = card.terminal
        line = card._mark_lines.get(uid)
        if line is None:
            return None
        oldest = view.history_pushed() - len(view.screen.history.top)
        return screen_rows(view)[line - oldest]

    narrow, wide = _reflow_render(40), _reflow_render(80)
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Reflow",
                                     cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    card._proj_cols = card.terminal.screen.columns
    agent.worker = worker = Worker()
    size(card, rows_n, 40)
    agent._set_status(AgentStatus.RUNNING)
    agent._prompt_ready = True
    # the conversation streams in at 40 columns, the prompt's Enter on its echo
    agent._on_pty_output("pty", "\r\n".join(narrow[:3]))
    card._on_prompt_submitted("Explain the sorting algorithms please",
                              card.terminal.anchor_line())
    mark = agent.prompt_marks()[-1]
    agent._on_pty_output("pty", "\r\n" + "\r\n".join(narrow[3:])
                         + "\r\n\x1b[2C\x1b[3A")
    check("reflow: (fixture) the prompt's milestone sits on its echo",
          mark_row(card, mark.uid) == narrow[2], mark_row(card, mark.uid))

    size(card, rows_n, 80)             # the card widens
    agent._on_pty_output("pty", _conhost_paint(wide[-rows_n:], rows_n))
    check("reflow: (fixture) one screenful re-wraps, the rows above do not",
          any(len(t) <= 40 and t.startswith("   w1x")
              for t in screen_rows(card.terminal)))
    check("reflow: a resized Claude agent owes a reflow",
          agent._reflow_due and agent._reflow_timer.isActive())

    # still busy: the extra rows wait, or a streaming reply would erase each
    # frame by more rows than the card has
    agent._busy = True
    agent._reflow_tick()
    check("reflow: nothing starts while the agent is busy",
          worker.sizes[-1] == (rows_n, 80) and agent._reflow_due
          and agent._reflow_timer.isActive(), worker.sizes)
    agent._busy = False

    agent._reflow_tick()
    tall = rows_n + REFLOW_EXTRA_ROWS
    check("reflow: idle, the child gets the extra rows",
          worker.sizes[-1] == (tall, 80) and agent._reflow_step == "tall",
          worker.sizes)
    check("reflow: a repaint wiggle keeps out of it",
          agent.request_repaint() is False)
    agent._on_pty_output("pty", _conhost_paint(wide, tall))
    check("reflow: the reprint's first byte marks where the old scrollback "
          "ends", agent.pty_replay().count(REFLOW_BEGIN) == 1
          and agent.reflowed_at > mark.pos)
    agent._end_reflow()
    check("reflow: then the child gets the card's own rows back",
          worker.sizes[-1] == (rows_n, 80)
          and agent.pty_replay().endswith(REFLOW_END), worker.sizes)
    agent._on_pty_output("pty", _conhost_paint(wide[-rows_n:], rows_n))

    got = screen_rows(card.terminal)
    want = [t.rstrip() for t in wide] + [""]
    check("reflow: the whole scrollback is at the new width, each row once",
          got == want,
          [t for t in got if t not in want][:3] or (len(got), len(want)))
    check("reflow: the milestone kept by the agent, found on the reprint",
          mark in agent.prompt_marks() and mark_row(card, mark.uid) == wide[2],
          mark_row(card, mark.uid))

    # a card rebuild replays the stream, markers included, to the same screen
    # (the full projection _rerender_restored runs once the card is sized)
    card2 = TerminalCard(agent)
    size(card2, rows_n, 80)
    card2._clear_for_projection()
    card2._replay_with_marks(agent.pty_replay())
    check("reflow: a rebuilt card replays to the same scrollback",
          screen_rows(card2.terminal) == got,
          (len(screen_rows(card2.terminal)), len(got)))
    check("reflow: ...with the milestone on its echo",
          mark_row(card2, mark.uid) == wide[2], mark_row(card2, mark.uid))
    check("reflow: a same-size rebuild owes no reflow",
          not agent._reflow_due, worker.sizes)

    # a submit while the child has the extra rows gives the card's back first
    size(card, rows_n, 60)
    agent._reflow_tick()
    agent._on_pty_output("pty", _conhost_paint(_reflow_render(60),
                                               rows_n + REFLOW_EXTRA_ROWS))
    agent.write("next question\r")
    check("reflow: a submit ends the reflow before its reply streams",
          agent._reflow_step == "" and worker.sizes[-1] == (rows_n, 60),
          worker.sizes)

    # a child that never answers costs no scrollback
    size(card, rows_n, 70)
    agent._awaiting_reply = False
    before = screen_rows(card.terminal)
    agent._reflow_tick()
    agent._end_reflow()
    check("reflow: no reprint, no wipe",
          screen_rows(card.terminal) == before
          and agent.pty_replay().count(REFLOW_BEGIN) == 2,
          agent.pty_replay().count(REFLOW_BEGIN))

    # a card that read the transcript after the prompt was typed (a rebuilt
    # one) placed that prompt already: the live milestone leaves it there
    # and never binds to a later line that quotes it
    import time
    spec = agent.spec
    card._recover_key = (spec.provider, spec.cwd, spec.session_id)
    card._recover_prompts = [mark.text]
    card._recover_prompts_ts = time.time()
    quoted = _reflow_render(50)
    quoted.insert(-5, "> " + mark.text + ", again")
    size(card, rows_n, 50)
    agent._reflow_tick()
    agent._on_pty_output("pty", _conhost_paint(quoted,
                                               rows_n + REFLOW_EXTRA_ROWS))
    agent._end_reflow()
    agent._on_pty_output("pty", _conhost_paint(quoted[-rows_n:], rows_n))
    view = card.terminal
    oldest = view.history_pushed() - len(view.screen.history.top)
    found = [screen_rows(view)[line - oldest] for line, _ in card._recovered]
    check("reflow: a prompt the transcript holds is placed on its echo",
          found == [quoted[2]], found)
    check("reflow: ...and its live milestone does not take the quoting line",
          mark_row(card, mark.uid) is None, mark_row(card, mark.uid))

    # a task typed while the child has the extra rows gives the card's back
    # first: the whole-conversation reprint could hold the task's words and
    # release its Enter before Claude drew it
    size(card, rows_n, 65)
    agent._reflow_tick()
    agent._on_pty_output("pty", _conhost_paint(_reflow_render(65),
                                               rows_n + REFLOW_EXTRA_ROWS))
    agent._write_task_to_pty("Explain the sorting algorithms please")
    check("reflow: a typed task ends the reflow before its text goes in",
          agent._reflow_step == "" and worker.sizes[-1] == (rows_n, 65),
          worker.sizes)
    agent._pending_submits.clear()

    # other providers and stopped children are left alone
    codex = TerminalAgent(build_spec(AgentKind.OPENAI, "Codex",
                                     cwd=SCRATCH_CWD, pty=True))
    codex.worker = Worker()
    codex.resize(rows_n, 90)
    check("reflow: only Claude reprints on a resize, so only Claude reflows",
          not codex._reflow_due)
    agent._set_status(AgentStatus.EXITED_OK)
    check("reflow: a child that stops owes nothing",
          not agent._reflow_due and agent._reflow_step == "")
    card.detach(); card2.detach(); agent.dispose(); codex.dispose()


def test_redraw_from_the_wrong_row_leaves_no_copy_in_scrollback():
    """A ConPTY frame that scrolls rows off but leaves the screen as it was
    pushed copies, not conversation, and the view takes them back.

    The bytes copy the shape of two real saved screens. After Claude's idle
    recap, it redrew its frame starting 4 rows too low: ConPTY sent a repaint
    from row 4 down plus newlines on the last row, so 4 rows scrolled off
    while the screen ended identical. Those rows were a reply paragraph
    already on screen, and the scrollback showed it twice."""
    import pyte
    from PySide6.QtWidgets import QApplication
    from app.widgets import terminal_view as tvm
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    rows, cols = 10, 40

    def rt(line):
        return "".join(line[x].data for x in range(cols)).rstrip()

    def hist(v):
        return [rt(r) for r in v.screen.history.top]

    def screen(v):
        return [rt(v.screen.buffer[y]) for y in range(rows)]

    def conversation(v, n=20):
        for i in range(n):
            v.feed(f"reply paragraph line {i:02d}\r\n")

    def wrong_row_redraw(texts, k=4):
        # cursor sits on the bottom row; go up to row k-1, erase it, then
        # paint the same screen one row lower per newline, running k rows
        # past the bottom (what ConPTY sent for Claude's redraw)
        out = ["\x1b[?25h\x1b[?25l\r", f"\x1b[{rows - k}A\x1b[K"]
        for t in texts:
            out.append("\r\n" + t + "\x1b[K")
        return "".join(out) + "\x1b[?25h"

    v = TerminalView(rows=rows, cols=cols)
    conversation(v)
    hist0, screen0, pushed0 = hist(v), screen(v), v.history_pushed()
    frame = wrong_row_redraw(screen0)

    raw = tvm._new_history_screen(cols, rows)
    st = pyte.Stream(raw)
    for i in range(20):
        st.feed(f"reply paragraph line {i:02d}\r\n")
    st.feed(frame)
    check("redraw: the bytes really push 4 copied rows (else this proves "
          "nothing)", len(raw.history.top) == len(hist0) + 4,
          len(raw.history.top))

    v.feed(frame)
    check("redraw: the screen is unchanged", screen(v) == screen0, screen(v))
    check("redraw: no copied rows reach the scrollback", hist(v) == hist0,
          hist(v)[-6:])
    check("redraw: line ids stay where they were",
          v.history_pushed() == pushed0, v.history_pushed())

    # a frame split across two chunks is still one frame
    v2 = TerminalView(rows=rows, cols=cols)
    conversation(v2)
    cut = len(frame) // 2
    v2.feed(frame[:cut])
    v2.feed(frame[cut:])
    check("redraw: a frame split over two chunks drops its copies too",
          hist(v2) == hist0 and screen(v2) == screen0, hist(v2)[-6:])

    # a replay feeds the whole stream at once and must come out the same
    v3 = TerminalView(rows=rows, cols=cols)
    v3.feed("".join(f"reply paragraph line {i:02d}\r\n" for i in range(20))
            + frame)
    check("redraw: a replay in one feed drops the same rows",
          hist(v3) == hist0 and screen(v3) == screen0, hist(v3)[-6:])

    # scrolled back while it happens: the view stays on the same text
    v4 = TerminalView(rows=rows, cols=cols)
    conversation(v4)
    v4.scroll_by(5)
    seen = v4.visible_text()
    v4.feed(frame)
    check("redraw: a scrolled-back view stays on the same text",
          v4.scroll_offset() == 5 and v4.visible_text() == seen,
          (v4.scroll_offset(), v4.visible_text()))
    # ...also when the frame is split after some of its pushes: the first
    # chunk grows the offset for them, the second takes them back
    v11 = TerminalView(rows=rows, cols=cols)
    conversation(v11)
    v11.scroll_by(5)
    seen = v11.visible_text()
    cut = frame.rindex("\r\n")
    v11.feed(frame[:cut])
    check("redraw: the split frame's first chunk pushed rows (else this "
          "proves nothing)", v11.scroll_offset() > 5, v11.scroll_offset())
    v11.feed(frame[cut:])
    check("redraw: a frame split after its pushes keeps the view in place",
          hist(v11) == hist0 and v11.scroll_offset() == 5
          and v11.visible_text() == seen,
          (v11.scroll_offset(), v11.visible_text()))

    # a full history: the pushes evicted rows, which is not a wipe
    v5 = TerminalView(rows=rows, cols=cols)
    conversation(v5, tvm.HISTORY_LINES + 30)
    wiped = []
    v5.historyCleared.connect(lambda: wiped.append(1))
    full = hist(v5)
    v5.feed(wrong_row_redraw(screen(v5)))
    check("redraw: with history full it is not read as a wipe",
          not wiped, wiped)
    check("redraw: ...and the rows its pushes evicted come back",
          hist(v5) == full, (len(hist(v5)), len(full), hist(v5)[:2]))

    # ordinary output keeps every row it scrolls off
    v6 = TerminalView(rows=rows, cols=cols)
    conversation(v6)
    v6.feed("\x1b[?25lnew line A\r\nnew line B\r\n\x1b[?25h")
    check("redraw: ordinary output keeps its scrolled-off rows",
          hist(v6) == hist0 + screen0[:2], hist(v6)[-3:])

    # the same text in new colors changed what the screen shows
    v10 = TerminalView(rows=rows, cols=cols)
    conversation(v10)
    v10.feed(wrong_row_redraw(["\x1b[31m" + t + "\x1b[0m" for t in screen0]))
    check("redraw: a recolored screen keeps its scrolled-off rows",
          len(hist(v10)) == len(hist0) + 4, len(hist(v10)))

    # a screen that repeats every k rows can't be told apart from real
    # output scrolling, so its rows stay
    v7 = TerminalView(rows=rows, cols=cols)
    for _ in range(30):
        v7.feed("y\r\n")
    before = len(v7.screen.history.top)
    v7.feed("\x1b[?25ly\r\ny\r\ny\r\n\x1b[?25h")
    check("redraw: a repeating screen keeps its scrolled-off rows",
          len(v7.screen.history.top) == before + 3,
          (before, len(v7.screen.history.top)))

    # printing the same text twice is real output, even though the screen
    # ends the same: it scrolled at least a full screen
    v8 = TerminalView(rows=rows, cols=cols)
    conversation(v8)
    conversation(v8)
    check("redraw: the same text printed twice keeps both copies",
          len(hist(v8)) == len(hist0) + 20, len(hist(v8)))
    v9 = TerminalView(rows=rows, cols=cols)
    conversation(v9)
    v9.feed("".join(f"reply paragraph line {i:02d}\r\n" for i in range(20)))
    check("redraw: ...also when it arrives in one chunk",
          len(hist(v9)) == len(hist0) + 20, len(hist(v9)))


def test_repaint_from_the_top_leaves_no_copy_in_scrollback():
    """Claude repainting a frame taller than the card from the top row pushes
    the frame's own top into the scrollback a second time, and the view
    takes those rows back even when the repaint changed a row on screen.

    The bytes copy the shape of a live capture (claude 2.1.303 at 25x76,
    Ctrl+O twice over a long reply) and of six agents' saved screens: ConPTY
    moves the cursor up to the top row, writes the whole frame, and the rows
    past the bottom scroll off. Those rows equal the ones that end the
    scrollback. The repaint here also changes the footer, which is what let
    a reply paragraph show up three times over in a card: the older rule
    only takes copies back when the screen ends exactly as it was."""
    import pyte
    from PySide6.QtWidgets import QApplication
    from app.widgets import terminal_view as tvm
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    rows, cols = 10, 40
    lines = [f"reply paragraph line {i:02d}" for i in range(20)]
    printed = "".join(t + "\r\n" for t in lines) + "-- footer --\r\n"

    def rt(line):
        return "".join(line[x].data for x in range(cols)).rstrip()

    def hist(v):
        return [rt(r) for r in v.screen.history.top]

    def screen(v):
        return [rt(v.screen.buffer[y]) for y in range(rows)]

    def repaint(frame):
        # the cursor sits on the blank bottom row; up to the top row, then
        # the whole frame and the row the cursor parks on
        return ("\x1b[?25l\r\x1b[%dA" % (rows - 1)
                + "\r\n".join(frame) + "\r\n\x1b[?25h")

    # the frame: the reply from line 08 down, 4 rows taller than the card
    v = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v.feed(printed)
    hist0, screen0, pushed0 = hist(v), screen(v), v.history_pushed()
    check("repaint: the reply's top is in the scrollback (else this proves "
          "nothing)", hist0[-4:] == lines[8:12], hist0[-4:])
    frame = lines[8:] + ["-- footer, new hint --"]
    bytes_ = repaint(frame)

    raw = tvm._new_history_screen(cols, rows)
    pyte.Stream(raw).feed(printed + bytes_)
    check("repaint: the bytes really push 4 copied rows",
          [rt(r) for r in raw.history.top] == hist0 + lines[8:12],
          [rt(r) for r in raw.history.top][-6:])

    v.feed(bytes_)
    want_screen = lines[12:] + ["-- footer, new hint --", ""]
    check("repaint: the screen shows the repainted frame",
          screen(v) == want_screen and screen(v) != screen0, screen(v))
    check("repaint: no copied rows reach the scrollback", hist(v) == hist0,
          hist(v)[-6:])
    check("repaint: line ids stay where they were",
          v.history_pushed() == pushed0, v.history_pushed())

    # a frame split anywhere is judged whole, and a replay in one feed
    # drops the same rows
    for cut in (5, len(bytes_) // 2, bytes_.rindex("\r\n") + 1,
                len(bytes_) - 3):
        v2 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
        v2.feed(printed)
        v2.feed(bytes_[:cut])
        v2.feed(bytes_[cut:])
        check(f"repaint: a frame cut at {cut} drops its copies too",
              hist(v2) == hist0 and screen(v2) == want_screen, hist(v2)[-6:])
    v3 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v3.feed(printed + bytes_)
    check("repaint: a replay in one feed drops the same rows",
          hist(v3) == hist0 and screen(v3) == want_screen, hist(v3)[-6:])

    # the recap grew the frame by 2 rows: the 4 copies go, and the 2 rows
    # it scrolled off for real stay
    v4 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v4.feed(printed)
    v4.feed(repaint(lines[8:] + ["recap one", "recap two", "-- footer --"]))
    check("repaint: a grown frame keeps the rows it scrolled off",
          hist(v4) == hist0 + lines[12:14], hist(v4)[-6:])
    check("repaint: ...and shows its new rows",
          screen(v4) == lines[14:] + ["recap one", "recap two",
                                      "-- footer --", ""], screen(v4))

    # scrolled back while it happens: the view stays on the same text
    v5 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v5.feed(printed)
    v5.scroll_by(3)
    seen = v5.visible_text()
    v5.feed(bytes_)
    check("repaint: a scrolled-back view stays on the same text",
          v5.scroll_offset() == 3 and v5.visible_text() == seen,
          (v5.scroll_offset(), v5.visible_text()))

    # a full history: the rows the copies evicted come back
    v6 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    many = [f"older line {i:04d}" for i in range(tvm.HISTORY_LINES)]
    v6.feed("".join(t + "\r\n" for t in many) + printed)
    wiped = []
    v6.historyCleared.connect(lambda: wiped.append(1))
    full = hist(v6)
    check("repaint: the history is full (else this proves nothing)",
          len(full) == tvm.HISTORY_LINES, len(full))
    v6.feed(bytes_)
    check("repaint: with history full it is not read as a wipe",
          not wiped, wiped)
    check("repaint: ...and the rows the copies evicted come back",
          hist(v6) == full, (hist(v6)[:2], hist(v6)[-2:]))
    # ...also when the frame grew and keeps 2 of its 6 pushes: history is a
    # window, so it holds the newest rows as if only those 2 were pushed
    v6b = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v6b.feed("".join(t + "\r\n" for t in many) + printed)
    v6b.feed(repaint(lines[8:] + ["recap one", "recap two", "-- footer --"]))
    want = (many + lines[:14])[-tvm.HISTORY_LINES:]
    check("repaint: a grown frame on a full history keeps the right window",
          hist(v6b) == want, (hist(v6b)[:2], hist(v6b)[-3:]))

    # output that never went to the top row keeps a repeated line
    v7 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v7.feed("".join("same line\r\n" for _ in range(rows + 5)))
    before = hist(v7)
    v7.feed("\x1b[?25lsame line\r\n\x1b[?25h")
    check("repaint: a line printed twice without a jump stays",
          hist(v7) == before + ["same line"], len(hist(v7)) - len(before))

    # a jump to the top followed by new text keeps every row it scrolls
    v8 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v8.feed(printed)
    fresh = [f"fresh text {i:02d}" for i in range(rows + 2)]
    v8.feed("\x1b[?25l\x1b[%dA\r" % (rows - 1)
            + "\x1b[K\r\n".join(fresh) + "\x1b[K\r\n\x1b[?25h")
    check("repaint: new text after a jump keeps its scrolled-off rows",
          hist(v8) == hist0 + fresh[:3], hist(v8)[-4:])

    # the same text in new colors is new output, so it stays (a new footer
    # keeps the older rule, which reads an unchanged screen, out of it)
    v11 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v11.feed(printed)
    v11.feed(repaint(["\x1b[31m" + t + "\x1b[0m" for t in lines[8:12]]
                     + lines[12:] + ["-- footer, new hint --"]))
    check("repaint: recolored rows after a jump stay",
          hist(v11) == hist0 + lines[8:12], hist(v11)[-5:])

    # clearing the screen and printing the same text again is real output:
    # the cursor goes home with ESC[H, not up, so both prints stay
    v10 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v10.feed(printed)
    v10.feed("\x1b[?25l\x1b[H\x1b[2J" + printed + "\x1b[?25h")
    check("repaint: clear and print the same text again keeps both",
          hist(v10) == hist0 + hist0, (len(hist(v10)), hist(v10)[-3:]))

    # a blank row after a blank row proves nothing, so it stays
    v9 = TerminalView(rows=rows, cols=cols, claude_repaints=True)
    v9.feed(printed + "\r\n" * rows)
    before = hist(v9)
    check("repaint: the history ends on a blank row (else this proves "
          "nothing)", before[-1] == "", before[-2:])
    v9.feed("\x1b[?25l\x1b[%dA\r\x1b[2K" % (rows - 1)
            + "\r\n" * rows + "\x1b[?25h")
    check("repaint: a blank copy stays", hist(v9) == before + [""],
          hist(v9)[-3:])

    # only a Claude card takes copies back: another program's bytes could
    # mean a reprint on purpose
    v12 = TerminalView(rows=rows, cols=cols)
    v12.feed(printed + bytes_)
    check("repaint: a view without claude_repaints keeps the rows",
          hist(v12) == hist0 + lines[8:12], hist(v12)[-5:])
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    seen = {}
    for kind in (AgentKind.CLAUDE, AgentKind.OPENAI):
        agent = TerminalAgent(build_spec(kind, "Repaint", cwd=SCRATCH_CWD))
        card = TerminalCard(agent)
        seen[kind.value] = (card.terminal is not None
                            and card.terminal._claude_repaints)
        card.detach()
        card.close()
        agent.deleteLater()
    check("repaint: a Claude card turns the rule on, a Codex card doesn't",
          seen == {"claude": True, "openai": False}, seen)


def test_repaint_take_back_edges():
    """The repaint rule (_drop_repaint_copies) where the stream gets awkward:
    two repaints in one frame, a frame split around a paint while scrolled
    back, a mode switch in the same chunk as the frame, and a reprint the
    stream never closed."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import REFLOW_BEGIN
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    rows, cols = 10, 40
    lines = [f"reply paragraph line {i:02d}" for i in range(20)]
    printed = "".join(t + "\r\n" for t in lines) + "-- footer --\r\n"

    def rt(line):
        return "".join(line[x].data for x in range(cols)).rstrip()

    def hist(v):
        return [rt(r) for r in v.screen.history.top]

    def frame(rows_):
        return ("\r\x1b[%dA" % (rows - 1) + "\r\n".join(rows_) + "\r\n")

    def view(scrolled=0):
        v = TerminalView(rows=rows, cols=cols, claude_repaints=True)
        v.feed(printed)
        if scrolled:
            v.scroll_by(scrolled)
        return v

    hist0 = hist(view())
    once = frame(lines[8:] + ["-- footer, new hint --"])

    # two repaints before the frame ends: both sets of copies go
    v = view()
    v.feed("\x1b[?25l" + once + once + "\x1b[?25h")
    check("repaint-edge: two repaints in one frame leave no copy",
          hist(v) == hist0, hist(v)[-6:])

    # a frame that grew, cut after its first 2 pushes, with a paint in
    # between: the take-back brings (pushed, len) back to the value the
    # paint cached for other rows
    grown = ("\x1b[?25l" + frame(lines[8:] + ["recap one", "recap two",
                                              "-- footer --"]) + "\x1b[?25h")
    cut = 0
    for _ in range(rows + 1):
        cut = grown.index("\r\n", cut) + 2
    v = view(scrolled=3)
    p0 = v.history_pushed()
    v.feed(grown[:cut])
    check("repaint-edge: (fixture) the cut lands after 2 pushes",
          v.history_pushed() == p0 + 2, v.history_pushed() - p0)
    v.visible_text()                       # a paint caches the history
    v.feed(grown[cut:])
    check("repaint-edge: (fixture) the grown frame keeps 2 of its rows",
          hist(v) == hist0 + lines[12:14], hist(v)[-4:])
    cached = v._view_state()[0]
    check("repaint-edge: a scrolled-back view paints the rows history holds",
          [rt(r) for r in cached] == hist(v), [rt(r) for r in cached][-4:])

    # a mode switch after the frame, in the same chunk: the frame was on the
    # main screen, and a feed cut anywhere agrees
    v = view()
    v.feed("\x1b[?25l" + once + "\x1b[?25h\x1b[?1049h")
    check("repaint-edge: a frame before ESC[?1049h in its chunk drops copies",
          hist(v) == hist0, hist(v)[-6:])
    alt = "\x1b[?1049h\x1b[?25l" + once + "\x1b[?25h\x1b[?1049l"
    v, v2 = view(), view()
    v.feed(alt)
    for part in alt.split("\x1b[?25h"):
        v2.feed(part if part.startswith("\x1b[?1049l") else part + "\x1b[?25h")
    check("repaint-edge: a frame on the alternate screen keeps its rows, "
          "however the chunk is cut",
          hist(v) == hist(v2) == hist0 + lines[8:12], hist(v)[-6:])

    # a reprint the stream never closed (the app quit mid-reflow) ends when
    # the card drops that screen for the next child
    v = view()
    v.feed(REFLOW_BEGIN + "reprinted\r\n")
    check("repaint-edge: (fixture) an open reprint turns the rule off",
          v._reflowing)
    v.note_history_cleared()
    v.screen.reset()
    check("repaint-edge: a dropped screen ends the reprint", not v._reflowing)
    v.feed(printed + "\x1b[?25l" + once + "\x1b[?25h")
    check("repaint-edge: ...and the next repaint's copies go",
          hist(v) == hist0, hist(v)[-6:])


def test_cancelled_reflow_still_ends_in_the_stream():
    """A child that stops in the middle of a reflow's reprint still gets its
    REFLOW_END. Without it the view took everything after for reprint and
    kept every Claude repaint's copies, live and in a rebuilt card."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import (REFLOW_BEGIN, REFLOW_END,
                                    REFLOW_EXTRA_ROWS, AgentStatus,
                                    TerminalAgent)
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])
    rows_n = 10

    class Worker:
        def __init__(self):
            self.rows, self.cols, self.exit_info = rows_n, 40, None
            self.flushed = 0

        def is_running(self):
            return True

        def resize(self, rows, cols):
            if (rows, cols) == (self.rows, self.cols):
                return False
            self.rows, self.cols = rows, cols
            return True

        def write(self, data):
            return True

        def flush(self):
            self.flushed += 1

        def dispose(self):
            pass

    def size(card, rows, cols):
        card.terminal.screen.resize(rows, cols)
        card.terminal.sizeChanged.emit(rows, cols)

    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Reflow cancel",
                                     cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    card._proj_cols = card.terminal.screen.columns
    agent.worker = worker = Worker()
    size(card, rows_n, 40)
    agent._set_status(AgentStatus.RUNNING)
    agent._prompt_ready = True
    agent._on_pty_output("pty", "\r\n".join(_reflow_render(40)))
    size(card, rows_n, 80)
    wide = _reflow_render(80)
    agent._on_pty_output("pty", _conhost_paint(wide[-rows_n:], rows_n))
    agent._busy = False                # idle at its prompt
    agent._reflow_tick()
    agent._on_pty_output("pty", _conhost_paint(wide,
                                               rows_n + REFLOW_EXTRA_ROWS))
    check("reflow-cancel: (fixture) the reprint is under way",
          agent._reflow_step == "reprint" and card.terminal._reflowing,
          agent._reflow_step)
    agent._set_status(AgentStatus.EXITED_OK)
    replay = agent.pty_replay()
    check("reflow-cancel: the reprint ends in the stream",
          replay.endswith(REFLOW_END) and replay.count(REFLOW_BEGIN) == 1
          and agent._reflow_step == "", agent._reflow_step)
    check("reflow-cancel: ...after the bytes the worker still held",
          worker.flushed == 1, worker.flushed)
    check("reflow-cancel: the live view is out of the reprint",
          not card.terminal._reflowing)
    card2 = TerminalCard(agent)        # the full projection, markers in
    size(card2, rows_n, 80)
    card2._clear_for_projection()
    card2._replay_with_marks(agent.pty_replay())
    check("reflow-cancel: so is a rebuilt card",
          not card2.terminal._reflowing)
    # nothing more to end: a second stop and the teardown add nothing
    agent._set_status(AgentStatus.IDLE)
    agent.dispose()
    check("reflow-cancel: one REFLOW_END, however often it is cancelled",
          agent.pty_replay().count(REFLOW_END) == 1)
    for c in (card, card2):
        c.detach()
        c.close()
    agent.deleteLater()


def test_reflow_keeps_scrollback_until_the_reprint_starts():
    """A tall Claude card keeps its scrollback through a reflow.

    Live-reported: every Claude card showed its last screenful and nothing
    to scroll back to. Claude answers a resize to at most 2048 rows with the
    whole conversation and ignores a taller one (measured on claude 2.1.302
    and 2.1.304), so a card over 48 rows got no reprint for its rows +
    REFLOW_EXTRA_ROWS. The first byte read meanwhile counted as the reprint
    and dropped the scrollback, and that byte was a footer hint Claude
    cleared in place. The tall size now stops at REFLOW_MAX_ROWS, and only
    a frame that homes the cursor starts the reprint."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import (REFLOW_BEGIN, REFLOW_END,
                                    REFLOW_EXTRA_ROWS, REFLOW_MAX_ROWS,
                                    AgentStatus, TerminalAgent)
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])
    rows_n = 60

    class Worker:
        def __init__(self):
            self.rows, self.cols, self.sizes = rows_n, 40, []
            self.exit_info = None

        def is_running(self):
            return True

        def resize(self, rows, cols):
            if (rows, cols) == (self.rows, self.cols):
                return False
            self.rows, self.cols = rows, cols
            self.sizes.append((rows, cols))
            return True

        def write(self, data):
            return True

        def flush(self):
            pass

        def dispose(self):
            pass

    def size(card, rows, cols):        # what TerminalView._apply_resize does
        card.terminal.screen.resize(rows, cols)
        card.terminal.sizeChanged.emit(rows, cols)

    def screen_rows(view):
        scr = view.screen
        lines = list(scr.history.top) + [scr.buffer[r]
                                         for r in range(scr.lines)]
        return ["".join(ln[c].data for c in range(scr.columns)).rstrip()
                for ln in lines]

    conv = [f"row {i}" for i in range(1, 151)]

    def each_row_once(view):
        got = [t for t in screen_rows(view) if t.startswith("row ")]
        return got == conv

    # the bytes claude 2.1.304 sent while it ignored a 2050-row resize: the
    # footer hint after a reply, erased in place
    hint = ("\x1b[?25l\x1b[2D\x1b[3B\r\x1b[61C\x1b[1A\x1b[K\r\n"
            "\x1b[2C\x1b[3A\x1b[?25h")

    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Tall reflow",
                                     cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    card._proj_cols = card.terminal.screen.columns
    agent.worker = worker = Worker()
    size(card, rows_n, 40)
    agent._set_status(AgentStatus.RUNNING)
    agent._prompt_ready = True
    agent._on_pty_output("pty", "\r\n".join(conv) + "\r\n")
    size(card, rows_n, 80)
    agent._on_pty_output("pty", _conhost_paint(conv[-rows_n:], rows_n))
    view = card.terminal
    hist = len(view.screen.history.top)
    check("tall-reflow: (fixture) the conversation is in the scrollback",
          hist > 80 and each_row_once(view), hist)

    agent._busy = False
    agent._reflow_tick()
    check("tall-reflow: the child is never made taller than Claude answers",
          rows_n + REFLOW_EXTRA_ROWS > REFLOW_MAX_ROWS
          and worker.sizes[-1] == (REFLOW_MAX_ROWS, 80)
          and agent._reflow_step == "tall", worker.sizes)

    agent._on_pty_output("pty", hint)
    check("tall-reflow: a hint redrawn meanwhile is not the reprint",
          REFLOW_BEGIN not in agent.pty_replay()
          and agent._reflow_step == "tall"
          and len(view.screen.history.top) == hist and each_row_once(view),
          (agent._reflow_step, len(view.screen.history.top)))
    check("tall-reflow: ...and does not end the wait for it",
          not agent._reflow_quiet.isActive()
          and agent._reflow_cap.isActive())

    paint = _conhost_paint(conv, REFLOW_MAX_ROWS)
    agent._on_pty_output("pty", hint + paint)
    replay = agent.pty_replay()
    check("tall-reflow: the reprint starts at its frame, after the hint",
          replay.count(REFLOW_BEGIN) == 1
          and replay.endswith(hint + REFLOW_BEGIN + paint)
          and agent._reflow_step == "reprint", agent._reflow_step)
    agent._end_reflow()
    agent._on_pty_output("pty", _conhost_paint(conv[-rows_n:], rows_n))
    check("tall-reflow: the reprint replaced the scrollback, each row once",
          each_row_once(view) and agent.pty_replay().count(REFLOW_END) == 1,
          len(view.screen.history.top))

    # a read that cuts the home sequence in two still starts the reprint
    size(card, rows_n, 70)
    agent._busy = False
    agent._reflow_tick()
    at = paint.index("\x1b[H") + 2
    agent._on_pty_output("pty", paint[:at])
    check("tall-reflow: (fixture) half a home sequence starts nothing",
          agent._reflow_step == "tall")
    agent._on_pty_output("pty", paint[at:])
    check("tall-reflow: the rest of it does",
          agent._reflow_step == "reprint"
          and agent.pty_replay().count(REFLOW_BEGIN) == 2)
    agent._end_reflow()
    agent._on_pty_output("pty", _conhost_paint(conv[-rows_n:], rows_n))
    check("tall-reflow: ...and the scrollback holds each row once",
          each_row_once(view), len(view.screen.history.top))

    # the stream replays to the same scrollback
    card2 = TerminalCard(agent)
    size(card2, rows_n, 70)
    card2._clear_for_projection()
    card2._replay_with_marks(agent.pty_replay())
    check("tall-reflow: a rebuilt card replays to the same scrollback",
          screen_rows(card2.terminal) == screen_rows(view))

    # a child that ignores the resize and redraws a hint keeps everything
    size(card, rows_n, 75)
    agent._busy = False
    agent._reflow_tick()
    before = screen_rows(view)
    agent._on_pty_output("pty", hint)
    agent._end_reflow()
    check("tall-reflow: no reprint, no wipe, even with a hint meanwhile",
          screen_rows(view) == before
          and agent.pty_replay().count(REFLOW_BEGIN) == 2,
          agent.pty_replay().count(REFLOW_BEGIN))
    card.detach(); card2.detach(); agent.dispose()


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
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap, QWheelEvent
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
    check("scrollbar: write/nudge record no milestones",
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

    # a wheel over the bar reaches a mouse-tracking agent at the cell under
    # the pointer (the right edge), not at the bar's own x mapped onto col 0
    sent = []
    t.keyInput.connect(sent.append)
    t.feed("\x1b[?1000h\x1b[?1006h")
    local = QPointF(WIDTH / 2, 100)
    QApplication.sendEvent(card.scroll_bar, QWheelEvent(
        local, QPointF(card.scroll_bar.mapToGlobal(local)), QPoint(),
        QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False))
    row, col = t._cell_at(QPointF(card.scroll_bar.mapToParent(local)))
    check("scrollbar: a wheel over it is reported at the cell under it",
          sent and sent[-1].startswith(f"\x1b[<64;{col + 1};{row + 1}M")
          and col > 5, (sent, row, col))
    t.feed("\x1b[?1006l\x1b[?1000l")
    t.keyInput.disconnect(sent.append)

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

    # ---- width change re-projects a restored snapshot's scrollback ---------
    # (a stream drawn this run is replayed at its recorded sizes instead, see
    # test_replay_reads_each_part_at_its_drawn_size)
    wide = TerminalAgent(build_spec(AgentKind.CLAUDE, "Reflow", cwd=SCRATCH_CWD,
                                    pty=True))
    wcard = TerminalCard(wide)
    wt = wcard.terminal
    wt.screen.resize(20, 30)                   # a pre-layout narrow card
    wcard._proj_cols = 30
    snap = ("A very long line of conversation text that "
            "must wrap at thirty columns\r\n") * 20
    wide.seed_pty_replay(snap)
    wt.feed(snap)                              # the seed projection's paint
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
    (restart, scheduled send, the lane actions) are
    icon buttons in the hover tray, each with a tooltip naming it. The user
    had forgotten the right-click menu existed. A terminal card has no Stop:
    Ctrl+C in the terminal does it, and the user found the button useless.
    A line-console card keeps it, having no Ctrl+C."""
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
    lifecycle = [card.btn_restart, card.btn_sched]
    check("actions: restart/schedule are in the tray",
          all(b in shown() for b in lifecycle))
    check("actions: no Start or Assign button (restart and any keystroke "
          "start an agent)",
          not hasattr(card, "btn_start") and not hasattr(card, "btn_assign")
          and not hasattr(card, "reassignRequested"))
    check("actions: no Stop button (Ctrl+C in the terminal stops it)",
          card.btn_stop is None
          and not any("Stop" in b.toolTip() for b in ht._buttons))
    check("actions: every tray button has a tooltip",
          all(b.toolTip().strip() for b in shown()),
          [(b.text(), b.toolTip()) for b in shown()])
    check("actions: lane buttons stay out with nothing to offer",
          card.btn_adopt not in shown()
          and card.btn_integrator not in shown())
    ht._set_open(False)

    # a line console has no per-command Ctrl+C, so it keeps its Stop
    # (graceful, stdin EOF): without it only Restart could end its child
    lc = TerminalAgent(build_spec(AgentKind.POWERSHELL, "Line",
                                  cwd=SCRATCH_CWD, pty=False))
    stops = []
    lc.stop = lambda: stops.append(1)
    lc_card = TerminalCard(lc)
    lc_card.resize(900, 300); lc_card.show(); pump(60)
    check("actions: a line-console card keeps its Stop in the tray",
          not lc_card.is_pty and lc_card.btn_stop is not None
          and lc_card.btn_stop in lc_card.header_tools._buttons)
    lc_card.header_tools._set_open(True); pump(20)
    check("actions: a line console's Stop is off while the child is idle",
          not lc_card.btn_stop.isEnabled())
    lc_card.btn_stop.setEnabled(True)
    lc_card.btn_stop.click(); pump(10)
    check("actions: a line console's Stop calls agent.stop", stops == [1])
    lc_card.header_tools._set_open(False)
    lc_card.detach(); lc_card.deleteLater(); pump(10)

    # what MainWindow offers decides the lane buttons, and its label and
    # reason land in their tooltips
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

    card.detach(); card.close()
    a.deleteLater()


def test_restart_resets_the_agent_to_its_launch_defaults():
    """Restart reads like closing the card and opening a new agent of the same
    kind. The permission mode the user shifted into (which the manager writes
    back to the spec), a /model override, the AI title, the context badge and
    the task all belonged to the old conversation and must not carry over.
    The name, folder and launch model/effort stay."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AssignmentState, TerminalAgent

    def make():
        return TerminalAgent(build_spec(AgentKind.CLAUDE, "Keep me",
                                        cwd=SCRATCH_CWD, effort="high"))

    fresh = make()
    a = make()
    a.set_live_model("Sonnet 5", "low", "plan")
    a.spec.set_permission_mode("plan")
    a.set_ai_title("Old conversation")
    a.set_token_usage(200_000, 1_000_000)
    a.set_task("old task")
    a.set_assignment(AssignmentState.WORKING)
    badges = []
    a.model_changed.connect(badges.append)
    # a stub: from IDLE, PtyWorker.restart() STARTS a real claude
    a.worker.restart = lambda: None
    a.restart()

    check("restart-reset: the permission mode is back to the CLI default",
          a.spec.permission_mode == ""
          and "--permission-mode" not in a.spec.effective_args()
          and a.permission_mode() == "", a.spec.effective_args())
    check("restart-reset: the header chip reads like a new agent's",
          a.model_badge() == fresh.model_badge()
          and badges == [fresh.model_badge()],
          (a.model_badge(), fresh.model_badge(), badges))
    check("restart-reset: no old title, context badge or task",
          a.summary() == "" and a.token_badge() == ""
          and a.current_task == ""
          and a.assignment is AssignmentState.IDLE,
          (a.summary(), a.token_badge(), a.current_task))
    check("restart-reset: the name and launch effort stay",
          a.spec.name == "Keep me" and a.spec.effort == "high")
    fresh.dispose()
    a.dispose()


def test_restart_blanks_the_card_screen():
    """Restart gives the new child a blank screen, as a fresh terminal would.

    The regression: restart() dropped the agent's raw buffer but the card's
    view kept the old frame and its cursor, so the new Claude painted its
    banner and input box over the old one from wherever the old cursor sat.
    The dying child's last output also arrives after restart() (its EOF
    comes later), and none of that may survive into the new screen."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY, WorkerState
    from app.terminal_agent import TerminalAgent
    QApplication.instance() or QApplication([])
    if not HAS_CONPTY:
        return skip("restart-blank", "no ConPTY")
    from app.widgets.terminal_card import TerminalCard

    agent = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Restart blank", cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    worker = agent.worker
    # stub the worker side: a real restart kills a child and starts another
    worker.restart = lambda: worker._set_state(WorkerState.STOPPING)
    worker._set_state(WorkerState.RUNNING)
    agent._on_pty_output("pty", "old banner\r\nold prompt\r\n\x1b[?2004h> ")
    agent._set_prompt_ready(True)
    check("restart-blank: the old frame is on screen first",
          "old banner" in card.terminal.screen_text()
          and card.terminal.screen.cursor.y > 0)

    agent.restart()
    agent._on_pty_output("pty", "old tail")   # the dying child's last frame
    worker._set_state(WorkerState.DEAD)      # PtyWorker._on_eof
    worker._set_state(WorkerState.IDLE)
    worker._set_state(WorkerState.STARTING)  # ...and the new child
    term = card.terminal
    check("restart-blank: the new child gets an empty screen at the top left",
          not term.screen_text().strip()
          and (term.screen.cursor.x, term.screen.cursor.y) == (0, 0)
          and not term._bracketed_paste, repr(term.screen_text()))
    check("restart-blank: nothing of the old child is kept for replay",
          agent.pty_replay() == "" and not agent.prompt_ready())
    agent._on_pty_output("pty", "new banner")
    check("restart-blank: the new banner draws on the first row",
          term.screen_text().splitlines()[0].startswith("new banner"))

    # a plain start is not a restart: what is on screen stays
    worker._set_state(WorkerState.IDLE)
    worker._set_state(WorkerState.STARTING)
    check("restart-blank: a later start keeps the screen",
          "new banner" in term.screen_text())
    card.detach()
    worker._set_state(WorkerState.IDLE)
    agent.dispose()


def test_restart_drops_the_dying_childs_frames():
    """What a Restart's old child draws while it dies never reaches the new
    conversation: its footer must not make the new child read as ready, and
    a plan-limit banner on it must not park the new one. The view drops a
    selection made on the old frame, a restored screen's pending projection
    is cancelled, a restart whose new child never starts stops dropping
    output, and Restart in own lane gets the same blank screen."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY, WorkerState
    from app.terminal_agent import TerminalAgent
    QApplication.instance() or QApplication([])
    if not HAS_CONPTY:
        return skip("restart-tail", "no ConPTY")
    from app.widgets.terminal_card import TerminalCard

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    agent = TerminalAgent(build_spec(
        AgentKind.CLAUDE, "Restart tail", cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(agent)
    term = card.terminal
    worker = agent.worker
    worker.restart = lambda: worker._set_state(WorkerState.STOPPING)
    worker._set_state(WorkerState.RUNNING)
    agent._on_pty_output("pty", "old line\r\n" * 60)
    pushed = term.screen.history.top.pushed
    term._sel_anchor, term._sel_end = (0, 0), (0, 3)

    agent.restart()
    # the dying child's last frames: the footer, then a limit banner
    agent._on_pty_output("pty", "? for shortcuts\r\n")
    agent._on_pty_output("pty", "You've hit your session limit \xb7 resets "
                                "4:40am (Europe/Budapest)\r\n")
    check("restart-tail: the old child's footer does not make the new one "
          "ready", not agent.prompt_ready())
    check("restart-tail: a limit banner on the old frame parks nothing",
          not agent._limit_blocked)
    worker._set_state(WorkerState.DEAD)
    worker._set_state(WorkerState.IDLE)
    worker._set_state(WorkerState.STARTING)
    check("restart-tail: the old frame's selection is gone",
          term._sel_anchor is None and term._sel_end is None)
    check("restart-tail: absolute line ids keep counting up",
          pushed > 0 and term.screen.history.top.pushed == pushed,
          (pushed, term.screen.history.top.pushed))
    agent._on_pty_output("pty", "new banner")
    check("restart-tail: the new child's output is kept",
          "new banner" in agent.pty_replay()
          and "old line" not in agent.pty_replay())
    card.detach()
    worker._set_state(WorkerState.IDLE)
    agent.dispose()

    # a restored screen waiting for its settled-size projection
    seeded = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Seeded", cwd=SCRATCH_CWD, pty=True))
    seeded.seed_pty_replay("PREVIOUS-RUN-SCREEN\r\n")
    scard = TerminalCard(seeded)
    check("restart-tail: the restored screen's projection is pending",
          scard._settle_timer.isActive() and scard._boot_seed)
    seeded.worker.restart = lambda: seeded.worker._set_state(
        WorkerState.STARTING)
    seeded.restart()
    check("restart-tail: Restart cancels the restored screen's projection",
          not scard._settle_timer.isActive() and not scard._restored_hooked
          and not scard._boot_seed)
    pump(400)
    check("restart-tail: ...so it never replays over the new child",
          "PREVIOUS-RUN-SCREEN" not in scard.terminal.screen_text())
    scard.detach()
    seeded.worker._set_state(WorkerState.IDLE)
    seeded.dispose()

    # a restart whose new child never starts
    failed = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Failed", cwd=SCRATCH_CWD, pty=True))
    failed.worker.restart = lambda: failed.worker._set_state(
        WorkerState.STOPPING)
    failed.worker._set_state(WorkerState.RUNNING)
    failed.restart()
    failed.worker._set_state(WorkerState.DEAD)
    pump(30)
    failed._on_pty_output("pty", "after the failed start")
    check("restart-tail: a restart that never started stops dropping output",
          "after the failed start" in failed.pty_replay())
    failed.dispose()

    # Restart in own lane: the worker already stopped, the lane's first
    # child starts on a blank screen
    adopt = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Adopt", cwd=SCRATCH_CWD, pty=True))
    resets = []
    adopt.screen_reset.connect(lambda: resets.append(1))
    adopt._on_pty_output("pty", "main folder frame")
    adopt.begin_fresh_screen()
    adopt.worker._set_state(WorkerState.STARTING)
    check("restart-tail: Restart in own lane starts on a blank screen",
          resets == [1] and adopt.pty_replay() == "")
    adopt.worker._set_state(WorkerState.IDLE)
    adopt.dispose()


def test_restart_keeps_the_permission_mode_the_agent_was_created_with():
    """Restart drops a mode the user shifted into, but not the one they
    picked in the New Agent dialog. The manager writes the live mode back to
    spec.permission_mode, so the launch pick lives in its own field and has
    to survive a save and load. A file written before that field existed
    keeps its stored mode, which is what Restart did before."""
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.terminal_agent import TerminalAgent

    spec = build_spec(AgentKind.CLAUDE, "Edits", cwd=SCRATCH_CWD,
                      permission_mode="acceptEdits")
    spec.set_permission_mode("plan")          # the user shifted into plan
    spec = AgentSpec.from_dict(spec.to_dict())  # and AI Hive restarted
    check("launch-mode: a save and load keep the launch pick apart",
          spec.permission_mode == "plan"
          and spec.launch_permission_mode == "acceptEdits",
          (spec.permission_mode, spec.launch_permission_mode))
    a = TerminalAgent(spec)
    # a stub: from IDLE, PtyWorker.restart() STARTS a real claude
    a.worker.restart = lambda: None
    a.restart()
    args = a.spec.effective_args()
    check("launch-mode: restart comes back in the mode it was created with",
          a.spec.permission_mode == "acceptEdits"
          and "plan" not in args
          and args[args.index("--permission-mode") + 1] == "acceptEdits",
          args)
    a.dispose()

    legacy = build_spec(AgentKind.CLAUDE, "Old", cwd=SCRATCH_CWD,
                        permission_mode="plan").to_dict()
    del legacy["launch_permission_mode"]
    old = AgentSpec.from_dict(legacy)
    check("launch-mode: an older session file keeps its stored mode",
          old.launch_permission_mode == "plan", old.launch_permission_mode)


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


def test_terminal_find():
    """Ctrl+F: the Chrome-style find bar over a terminal. Matches cover the
    scrollback and the live screen, the active one is the nearest to the
    view, stepping wraps and scrolls, new output keeps the active match, and
    no key typed into the bar ever reaches the agent."""
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
    from PySide6.QtGui import QColor, QKeyEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.terminal_view import (FIND_ACTIVE, FIND_HIT,
                                           TerminalView)

    app = QApplication.instance() or QApplication([])
    K, M = Qt.Key, Qt.KeyboardModifier

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def abs_text(view, abs_line):
        """The text of an absolute line, wherever the view is scrolled."""
        top = view.screen.history.top
        idx = abs_line - (view.history_pushed() - len(top))
        line = top[idx] if idx < len(top) \
            else view.screen.buffer[idx - len(top)]
        return "".join(line[c].data
                       for c in range(view.screen.columns)).rstrip()

    def visible(view, abs_line):
        return 0 <= abs_line - view.abs_line_at_row(0) < view.screen.lines

    def press(view, key, mods=M.NoModifier):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods, ""))

    v = TerminalView(rows=10, cols=40)
    v.resize(400, 200)
    for i in range(60):
        v.feed(f"line {i}" + (" Needle here" if i % 10 == 3 else "") + "\r\n")
    v.feed("> prompt")
    changes = []
    v.findChanged.connect(lambda a, t: changes.append((a, t)))

    v.set_find_query("needle")
    lines = [m[0] for m in v.find_matches()]
    check("find: case-insensitive, one match per row that has one",
          len(lines) == 6, v.find_matches())
    check("find: matches are in reading order", lines == sorted(lines))
    check("find: a match's line is the line that says it",
          [abs_text(v, ln) for ln in lines]
          == [f"line {n} Needle here" for n in (3, 13, 23, 33, 43, 53)],
          [abs_text(v, ln) for ln in lines])
    # the view sat at the bottom, so the active match is the newest one
    # above it, scrolled into view, not the oldest line in history
    check("find: the active match is the one nearest the view",
          v.find_state() == (6, 6), v.find_state())
    check("find: findChanged carries the count",
          changes and changes[-1] == (6, 6), changes)
    check("find: the active match is scrolled into view",
          visible(v, lines[-1]), v.scroll_offset())
    m = v.find_matches()[-1]
    check("find: the match spans the word's columns",
          (m[1], m[2]) == (len("line 53 "), len("line 53 Needle") - 1), m)

    v.find_step(1)
    check("find: next wraps from the last match to the first",
          v.find_state() == (1, 6), v.find_state())
    check("find: stepping scrolls the match into view",
          visible(v, lines[0]), v.scroll_offset())
    v.find_step(-1)
    check("find: previous wraps from the first match to the last",
          v.find_state() == (6, 6), v.find_state())
    v.find_step(-1)
    check("find: previous steps back one", v.find_state() == (5, 6))

    # refining keeps the active match while it still matches
    active_line = v.find_matches()[4][0]
    v.set_find_query("needle h")
    check("find: a refined query keeps the active match",
          v.find_state()[1] == 6
          and v.find_matches()[v.find_state()[0] - 1][0] == active_line,
          v.find_state())
    v.set_find_query("needle hx")
    check("find: no match reads 0/0", v.find_state() == (0, 0))
    v.set_find_query("needle")

    # new output: the matches catch up, the active match and the view stay
    keep = v.find_matches()[v.find_state()[0] - 1]
    top_before = v.abs_line_at_row(0)
    check("find: the test starts scrolled back", v.scroll_offset() > 0)
    v.feed("\r\nfresh needle one\r\nfresh needle two\r\n")
    check("find: new output arms the rescan", v._find_timer.isActive())
    v._find_timer.stop()
    v._find_refresh()
    check("find: the rescan counts the new matches",
          v.find_state()[1] == 8, v.find_state())
    check("find: the active match survives the rescan",
          v.find_matches()[v.find_state()[0] - 1] == keep,
          (v.find_matches(), keep))
    check("find: the rescan does not move a scrolled-back view",
          v.abs_line_at_row(0) == top_before,
          (v.abs_line_at_row(0), top_before))

    # paint: the active match on orange, another on yellow
    p = TerminalView(rows=6, cols=40)
    p.resize(400, 150)
    p.feed("a needle\r\nb needle\r\n")
    p.set_find_query("needle")
    check("find: on a fresh screen the first match is active",
          p.find_state() == (1, 2), p.find_state())
    img = p.grab().toImage()

    def fill_at(match):
        r = match[0] - p.abs_line_at_row(0)
        x0, _y0, _x1, y1 = p.cell_bounds(match[1], r)
        return img.pixelColor(x0 + 1, y1 - 2)
    check("find: the active match is painted orange",
          fill_at(p.find_matches()[0]) == QColor(FIND_ACTIVE),
          fill_at(p.find_matches()[0]).name())
    check("find: another match is painted yellow",
          fill_at(p.find_matches()[1]) == QColor(FIND_HIT),
          fill_at(p.find_matches()[1]).name())
    # a selection dragged over a match stays visible on top of its box
    p._sel_anchor, p._sel_end = (1, 2), (1, 7)
    img = p.grab().toImage()
    check("find: a selection over a match still shows",
          fill_at(p.find_matches()[1]) != QColor(FIND_HIT),
          fill_at(p.find_matches()[1]).name())

    v.clear_find()
    check("find: closing drops every match",
          v.find_matches() == [] and v.find_state() == (0, 0))
    check("find: closing tells the bar", changes[-1] == (0, 0))
    check("find: closing keeps the row text for the next search",
          len(v._find_cache) > 0, len(v._find_cache))
    v.feed("more needle\r\n")
    check("find: a closed search does not rescan", not v._find_timer.isActive())

    # a search that found nothing yet: when matches arrive, the active one
    # is the newest above the view, as for a fresh query, not the oldest
    z = TerminalView(rows=5, cols=40)
    z.set_find_query("zebra")
    check("find: nothing to find reads 0/0", z.find_state() == (0, 0))
    z.feed("".join(f"zebra {i}\r\n" for i in range(20)) + "plain\r\n" * 6)
    z._find_timer.stop()
    z._find_refresh()
    check("find: matches that arrive later start at the newest",
          z.find_state() == (20, 20), z.find_state())
    check("find: ...without scrolling the view", z.scroll_offset() == 0)

    # wide glyphs: string index is not column
    w = TerminalView(rows=4, cols=30)
    w.feed("中文 needle 中\r\n")
    w.set_find_query("needle")
    check("find: a match after wide glyphs sits on its real columns",
          w.find_matches() and w.find_matches()[0][1:] == (5, 10),
          w.find_matches())
    w.set_find_query("文")
    check("find: a wide glyph match covers both of its cells",
          w.find_matches() and w.find_matches()[0][1:] == (2, 3),
          w.find_matches())

    # a history wipe (a reflow's reprint, ED 3) gives every reprinted line
    # a new id past the old active match: the rescan picks from the view
    # rather than jumping to the oldest match
    h = TerminalView(rows=10, cols=40)
    h.resize(400, 200)
    body = "".join(f"line {i}" + (" needle" if i % 10 == 3 else "")
                   + "\r\n" for i in range(60))
    h.feed(body)
    h.set_find_query("needle")
    h.find_step(-1)
    h.feed("\x1b[3J\x1b[2J\x1b[H")      # the wipe, then the reprint
    h.feed(body)
    h._find_timer.stop()
    h._find_refresh()
    check("find: after a history wipe the active match is the one by "
          "the view, not the oldest",
          h.find_state() == (6, 6), h.find_state())

    # a new query starts from what is on screen, not from an active match
    # the user scrolled away from
    a = TerminalView(rows=10, cols=40)
    a.resize(400, 200)
    a.feed("".join(f"row {i} " + ("needle" if i % 10 == 3 else "")
                   + (" pick" if i in (12, 55) else "") + "\r\n"
                   for i in range(60)))
    a.set_find_query("needle")
    a.find_step(1)              # wraps to the oldest match, far up
    a.scroll_by(-10 ** 6)       # the user wheels back to the bottom
    a.set_find_query("pick")
    check("find: a new query picks the match on screen, not one past "
          "the old active match",
          a.find_state() == (2, 2) and a.scroll_offset() == 0,
          (a.find_state(), a.scroll_offset()))

    # Enter right after the agent printed the first match, before the
    # rescan ran, still finds it
    e = TerminalView(rows=6, cols=40)
    e.set_find_query("error")
    e.feed("an error\r\n")
    e.find_step(1)
    check("find: stepping runs a pending rescan even from 0/0",
          e.find_state() == (1, 1), e.find_state())

    # the scrollbar's ticks: one entry per line, however often it matches
    f = TerminalView(rows=6, cols=40)
    f.feed("e e e\r\nxyz\r\ne\r\n")
    f.set_find_query("e")
    tick_lines, tick_active = f.find_lines()
    check("find: the scrollbar gets one line per row, not one per match",
          len(f.find_matches()) == 4
          and tick_lines == sorted({m[0] for m in f.find_matches()})
          and len(tick_lines) == 2, (f.find_matches(), tick_lines))
    check("find: the scrollbar knows the active match's line",
          tick_active == f.find_matches()[f.find_state()[0] - 1][0],
          tick_active)

    # keys: Ctrl+F asks for the bar and types nothing; F3 is the child's
    # key until a search is open
    k = TerminalView(rows=6, cols=40)
    sent, asked = [], []
    k.keyInput.connect(sent.append)
    k.findRequested.connect(lambda: asked.append(1))
    press(k, K.Key_F, M.ControlModifier)
    check("find: Ctrl+F asks for the find bar", asked == [1], asked)
    check("find: Ctrl+F sends nothing to the agent", sent == [], sent)
    press(k, K.Key_F3)
    check("find: F3 with no search goes to the agent",
          sent == ["\x1bOR"], sent)
    sent.clear()
    k.feed("alpha beta alpha\r\n")
    k.set_find_query("alpha")
    press(k, K.Key_F3)
    check("find: F3 steps while a search is open",
          k.find_state() == (2, 2) and sent == [], (k.find_state(), sent))
    press(k, K.Key_F3, M.ShiftModifier)
    check("find: Shift+F3 steps back", k.find_state() == (1, 2))
    # less and vim page forward with Ctrl+F on the alternate screen
    sent.clear()
    asked.clear()
    k.feed("\x1b[?1049h")
    press(k, K.Key_F, M.ControlModifier)
    check("find: on the alternate screen Ctrl+F goes to the program",
          asked == [] and sent == ["\x06"], (asked, sent))
    k.feed("\x1b[?1049l")

    # the card: Ctrl+F opens the bar, the bar drives the view
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Find", cwd=SCRATCH_CWD,
                                     pty=True))
    agent.worker.start = lambda: None       # a keypress wakes a stopped card
    card = TerminalCard(agent)
    card.resize(700, 360)
    card.show()
    pump(60)
    t, bar = card.terminal, card.find_bar
    typed = []
    t.keyInput.connect(typed.append)
    for i in range(40):
        t.feed(f"row {i} {'Target' if i in (5, 30) else ''}\r\n")
    check("find: the bar starts hidden", not bar.isVisible())
    press(t, K.Key_F, M.ControlModifier)
    check("find: Ctrl+F in the terminal opens the bar", bar.isVisible())
    check("find: the bar sits at the top right, left of the scrollbar",
          bar.y() == 0 and bar.geometry().right() < card.scroll_bar.x(),
          (bar.geometry(), card.scroll_bar.geometry()))
    check("find: rows under the bar count as hidden for scrolling to",
          t._find_top_row() * t._cell_h + 4 >= bar.height() > 0,
          (t._find_top_row(), t._cell_h, bar.height()))
    bar.edit.setText("target")
    check("find: typing searches as you go",
          t.find_state() == (2, 2) and bar.count.text() == "2/2",
          (t.find_state(), bar.count.text()))

    def key_in_bar(key, mods=M.NoModifier):
        QApplication.sendEvent(bar.edit, QKeyEvent(QEvent.Type.KeyPress,
                                                   key, mods, ""))
    key_in_bar(K.Key_Return)
    check("find: Enter in the bar goes to the next match",
          t.find_state() == (1, 2) and bar.count.text() == "1/2",
          bar.count.text())
    key_in_bar(K.Key_Return, M.ShiftModifier)
    check("find: Shift+Enter goes to the previous one",
          t.find_state() == (2, 2), t.find_state())
    key_in_bar(K.Key_G, M.ControlModifier)
    check("find: Ctrl+G goes to the next one too", t.find_state() == (1, 2))
    for key in (K.Key_Up, K.Key_Down, K.Key_PageUp, K.Key_F5, K.Key_Tab):
        key_in_bar(key)
    check("find: keys the query box ignores never reach the agent",
          typed == [], typed)
    bar.next_btn.click()
    check("find: the next arrow steps", t.find_state() == (2, 2))

    # a wheel over the bar reaches a mouse-tracking agent at the cell under
    # the pointer, not at the bar's own top left mapped onto cell (0, 0)
    t.feed("\x1b[?1000h\x1b[?1006h")
    local = QPointF(20, 10)
    glob = QPointF(bar.mapToGlobal(local))
    QApplication.sendEvent(bar, QWheelEvent(
        local, glob, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
        M.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
    row, col = t._cell_at(QPointF(bar.mapToParent(local)))
    check("find: a wheel over the bar is reported at the cell under it",
          typed and typed[-1].startswith(f"\x1b[<64;{col + 1};{row + 1}M")
          and col > 10, (typed, row, col))
    t.feed("\x1b[?1006l\x1b[?1000l")
    typed.clear()
    t.grab()
    card.scroll_bar.grab()     # the find ticks paint without error
    key_in_bar(K.Key_Escape)
    check("find: Esc closes the bar", not bar.isVisible())
    check("find: closing clears the highlights", t.find_matches() == [])
    check("find: Esc in the bar is not sent to the agent", typed == [], typed)

    # Ctrl+F again brings the last query back and searches it
    press(t, K.Key_F, M.ControlModifier)
    check("find: reopening keeps the last query",
          bar.isVisible() and bar.edit.text() == "target"
          and bar.edit.selectedText() == "target" and t.find_state()[1] == 2,
          (bar.edit.text(), t.find_state()))
    key_in_bar(K.Key_Escape)
    # a one-line selection becomes the query
    t.feed("pick this word\r\n")
    r = t.screen.cursor.y - 1
    t._sel_anchor, t._sel_end = (r, 5), (r, 8)
    press(t, K.Key_F, M.ControlModifier)
    check("find: the terminal's selection becomes the query",
          bar.edit.text() == "this", bar.edit.text())
    key_in_bar(K.Key_Escape)
    t._sel_anchor, t._sel_end = (0, 0), (2, 3)
    press(t, K.Key_F, M.ControlModifier)
    check("find: a multi-line selection does not replace the query",
          bar.edit.text() == "this", bar.edit.text())
    key_in_bar(K.Key_Escape)
    check("find: nothing typed in the bar reached the agent",
          typed == [], typed)

    # a boot veil over an open bar would leave the focus in a box nobody
    # can see: the veil closes the bar
    press(t, K.Key_F, M.ControlModifier)
    check("find: the bar is open before the veil", bar.isVisible())
    card._begin_boot_veil()
    check("find: a starting agent's veil closes the find bar",
          not bar.isVisible())
    card._dismiss_boot_veil()

    card.detach()
    card.close()
    agent.deleteLater()
    app.processEvents()
