"""Bionic reading: the Qt-free segmentation in app/word_starts.py, the
Options switch that draws it in every terminal, and Options > Contrast."""

import collections

from .harness import check

_Cell = collections.namedtuple("_Cell", "data bold")


def _row(text: str, bold: str = ""):
    """One cell per character; a "b" in `bold` marks that column bold."""
    return [_Cell(ch, i < len(bold) and bold[i] == "b")
            for i, ch in enumerate(text)]


def _bolded(text: str, bold: str = "", tail=None) -> list[str]:
    """The bold stretches of `text`, as strings, for readable checks."""
    from app.word_starts import word_start_spans
    return [text[a:b] for a, b in word_start_spans(_row(text, bold), tail)]


def test_word_starts_token_rules():
    """Prose words get their first half in bold; code, paths, flags,
    numbers and single letters keep their regular weight."""
    check("word starts: prose words get the first half, rounded up",
          _bolded("reading this line") == ["read", "th", "li"],
          _bolded("reading this line"))
    check("word starts: single letters stay regular",
          _bolded("a b I x") == [], _bolded("a b I x"))
    check("word starts: two letters bold one",
          _bolded("to be") == ["t", "b"], _bolded("to be"))
    for code in ("app/widgets/sidebar.py", "snake_case", "--flag", "x=1",
                 "0x1b", "42", "v2", "C:\\Users", "a.b", "https://x.io"):
        check(f"word starts: {code} is left alone", _bolded(code) == [],
              _bolded(code))
    check("word starts: trailing punctuation doesn't block a word",
          _bolded("Done. Yes, really!") == ["Do", "Ye", "rea"],
          _bolded("Done. Yes, really!"))
    check("word starts: quotes and brackets are stripped",
          _bolded('("quoted") [square]') == ["quo", "squ"],
          _bolded('("quoted") [square]'))
    check("word starts: curly quotes are stripped too",
          _bolded("\u201cnice\u201d \u2018ok\u2019") == ["ni", "o"],
          _bolded("\u201cnice\u201d \u2018ok\u2019"))
    check("word starts: camelCase is all letters and gets the treatment",
          _bolded("camelCase") == ["camel"], _bolded("camelCase"))
    check("word starts: an internal apostrophe or hyphen joins letter runs",
          _bolded("don't well-known") == ["do", "we", "kno"],
          _bolded("don't well-known"))
    check("word starts: a doubled or edge joiner disqualifies the token",
          _bolded("a--b well- -ish") == [], _bolded("a--b well- -ish"))
    check("word starts: columns are the row's own, gaps included",
          __import__("app.word_starts").word_starts.word_start_spans(
              _row("  hi   there")) == [(2, 3), (7, 10)])
    check("word starts: an empty or blank row has no spans",
          _bolded("") == [] and _bolded("      ") == [])


def test_word_starts_whole_row_and_wrap():
    """A word that crosses a colour change, or wraps onto the next row,
    still gets exactly one prefix; the child's own bold wins."""
    from app.word_starts import word_start_spans
    # pyte gives one cell per column whatever the colours, so a colour
    # change inside "reading" is invisible here: one prefix, not two
    check("word starts: a word split across two colour runs gets one prefix",
          word_start_spans(_row("reading")) == [(0, 4)])
    check("word starts: a wrapped word's tail on the next row stays regular",
          _bolded("ng more words", tail="i") == ["mo", "wor"],
          _bolded("ng more words", tail="i"))
    check("word starts: ...and its head on the row above has the prefix",
          _bolded("we are readi") == ["w", "ar", "rea"],
          _bolded("we are readi"))
    check("word starts: a row above ending in a space is no wrap",
          _bolded("next row", tail=" ") == ["ne", "ro"],
          _bolded("next row", tail=" "))
    check("word starts: a row above ending in punctuation is no wrap",
          _bolded("next row", tail=".") == ["ne", "ro"])
    check("word starts: a row starting with a space is no wrap",
          _bolded(" next", tail="a") == ["ne"])
    check("word starts: already-bold cells are left alone",
          _bolded("Heading here", bold="bbbbbbb") == ["he"],
          _bolded("Heading here", bold="bbbbbbb"))
    check("word starts: a partly bold word is the child's, left alone",
          _bolded("importantly", bold="bbbbbbbbb") == [])


def test_word_starts_accents_and_wide():
    """Accented letters count, whether precomposed or a base letter with a
    combining mark in the same cell, as pyte stores them."""
    from app.word_starts import word_start_spans
    check("word starts: precomposed accents are letters",
          _bolded("caf\u00e9 na\u00efve") == ["ca", "na\u00ef"],
          _bolded("caf\u00e9 na\u00efve"))
    combining = [_Cell(d, False) for d in ("c", "a", "f", "e\u0301")]
    check("word starts: a combining mark in its letter's cell counts",
          word_start_spans(combining) == [(0, 2)],
          word_start_spans(combining))
    check("word starts: Cyrillic and Greek words get prefixes",
          _bolded("\u043f\u0440\u0438\u0432\u0435\u0442 \u03b1\u03b2\u03b3")
          == ["\u043f\u0440\u0438", "\u03b1\u03b2"])
    # pyte stores a wide character, then an empty cell for its second
    # column: each CJK character reads as its own one-letter token
    wide = [_Cell("\u4e2d", False), _Cell("", False),
            _Cell("\u6587", False), _Cell("", False)]
    check("word starts: wide CJK characters are left alone",
          word_start_spans(wide) == [], word_start_spans(wide))
    check("word starts: cells with no data count as blanks",
          word_start_spans([_Cell(None, False), _Cell("o", False),
                            _Cell("k", False)]) == [(1, 2)])


def _view(text: str, rows: int = 6, cols: int = 40):
    from app.widgets.terminal_view import TerminalView
    tv = TerminalView(rows=rows, cols=cols)
    # never shown, so size it to its grid
    tv.resize(int(tv._cell_w * cols) + 40, int(tv._cell_h * rows) + 20)
    tv.feed(text)
    return tv


def _band(tv, img, row: int):
    """The pixels of one visible row."""
    from PySide6.QtCore import QRect
    from app.widgets.terminal_view import CELL_PAD_Y
    y0 = int(CELL_PAD_Y + row * tv._cell_h)
    return img.copy(QRect(0, y0, img.width(), int(tv._cell_h)))


def _masks(tv) -> list:
    """_word_start_rows as lists of bold columns, None for a plain row."""
    hist, off = tv._view_state()
    lines = [tv._visible_line(r, hist, off) for r in range(tv.screen.lines)]
    rows = tv._word_start_rows(lines, hist, off)
    if rows is None:
        return None
    return [None if m is None else [c for c, v in enumerate(m) if v == 1]
            for m in rows]


def _with_switch(on: bool, fn, strength: int = 1):
    """Run fn with the switch set, at Contrast `strength`. Level 1 is the
    plain bold the pixel checks compare with the child's own."""
    from app.widgets.terminal_view import DEFAULT_WORD_STRENGTH, TerminalView
    TerminalView.set_word_starts_enabled(on)
    TerminalView.set_word_strength(strength)
    try:
        return fn()
    finally:
        TerminalView.set_word_starts_enabled(False)
        TerminalView.set_word_strength(DEFAULT_WORD_STRENGTH)


# "reading this line" as the child would send it with those prefixes in
# its own bold: SGR 1 on the prefixes, 22 back to normal
_PROSE = "reading this line"
_PROSE_BOLD = ("\x1b[1mread\x1b[22ming \x1b[1mth\x1b[22mis "
               "\x1b[1mli\x1b[22mne")


def test_word_starts_option_toggle():
    """The "Bionic reading" switch in Options defaults OFF, persists under
    ui through a temp SessionStore, and repaints every terminal."""
    import shutil
    import tempfile
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from app.widgets.terminal_view import TerminalView
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-wordstarts-"))
    try:
        bar = TopBar()
        check("word starts toggle: defaults to OFF",
              not bar._bold_word_starts
              and not bar.bold_word_starts_btn.isChecked())
        emitted = []
        bar.boldWordStartsToggled.connect(emitted.append)
        bar.bold_word_starts_btn.click()
        check("word starts toggle: a click turns it on and emits True",
              emitted == [True] and bar._bold_word_starts, emitted)
        bar.set_bold_word_starts(False)
        check("word starts toggle: set_bold_word_starts does not re-emit",
              not bar._bold_word_starts and emitted == [True])
        check("word starts toggle: the switch has a row in the Options panel",
              bar.options_panel.isAncestorOf(bar.bold_word_starts_btn))
        tips = (bar.bold_word_starts_btn.toolTip(),)
        bar.set_bold_word_starts(True)
        tips += (bar.bold_word_starts_btn.toolTip(),
                 bar.word_strength_label.toolTip(),
                 bar.word_strength_dec_btn.toolTip(),
                 bar.word_strength_inc_btn.toolTip())
        check("word starts toggle: the switch is named Bionic reading and "
              "its tooltip says why the bold start can help",
              "Bionic reading" in bar.bold_word_starts_label.text()
              and "anchor" in tips[1] and "anchor" in tips[0], tips)
        bar.set_bold_word_starts(False)
        check("word starts toggle: the Contrast row shows only while the "
              "switch is on",
              not bar.word_strength_inc_btn.isVisibleTo(bar.options_panel)
              and not bar.word_strength_dec_btn.isVisibleTo(bar.options_panel)
              and not bar.word_strength_label.isVisibleTo(bar.options_panel))
        bar.set_bold_word_starts(True)
        check("word starts toggle: ...and comes back when it is turned on",
              bar.word_strength_inc_btn.isVisibleTo(bar.options_panel)
              and bar.word_strength_dec_btn.isVisibleTo(bar.options_panel)
              and bar.word_strength_label.isVisibleTo(bar.options_panel))
        # the switch sits in the panel, so it is flipped with the panel open
        bar.show()
        bar._open_options()
        app.processEvents()
        panel = bar.options_panel
        tall = panel.height()
        bar.bold_word_starts_btn.click()
        app.processEvents()
        short = panel.height()
        check("word starts toggle: turning it off with the panel open "
              "shrinks the panel to fit",
              panel.isVisible() and short < tall
              and short == panel.sizeHint().height(), (tall, short))
        bar.bold_word_starts_btn.click()
        app.processEvents()
        check("word starts toggle: ...and turning it on grows it back",
              panel.height() == tall, (tall, panel.height()))
        panel.hide()
        bar.hide()
        bar.deleteLater()

        win = create_main_window(SessionStore(path=tmp / "session.json"))
        check("word starts toggle: a fresh window has it off",
              not win._bold_word_starts
              and not TerminalView.word_starts_enabled())
        tv = _view(_PROSE + "\r\n")
        off_img = tv.grab().toImage()
        win._save_timer.stop()
        win._on_bold_word_starts_toggled(True)
        check("word starts toggle: ON reaches every terminal",
              TerminalView.word_starts_enabled()
              and tv.grab().toImage() != off_img)
        check("word starts toggle: flipping it marks the session for saving",
              win._save_timer.isActive())
        payload = win._session_payload()
        check("word starts toggle: the preference is persisted under ui",
              payload["ui"]["bold_word_starts"] is True, payload["ui"])
        win._on_bold_word_starts_toggled(False)
        check("word starts toggle: OFF paints the same pixels as before",
              tv.grab().toImage() == off_img)

        win._restore_ui_state({"ui": {"bold_word_starts": True}})
        check("word starts toggle: restored onto the window, bar and views",
              win._bold_word_starts and win.top_bar._bold_word_starts
              and TerminalView.word_starts_enabled())
        win._restore_ui_state({"ui": {}})
        check("word starts toggle: a session that predates it defaults OFF",
              not win._bold_word_starts
              and not TerminalView.word_starts_enabled())

        win._bold_word_starts = True
        win.top_bar.set_bold_word_starts(True)
        win._save_session()
        win.close()
        app.processEvents()
        TerminalView.set_word_starts_enabled(False)
        again = create_main_window(SessionStore(path=tmp / "session.json"))
        check("word starts toggle: ON survives a close and reopen",
              again._bold_word_starts and again.top_bar._bold_word_starts
              and TerminalView.word_starts_enabled())
        again._save_timer.stop()
        again.close()
        app.processEvents()
        tv.deleteLater()
    finally:
        TerminalView.set_word_starts_enabled(False)   # the flag is global
        shutil.rmtree(tmp, ignore_errors=True)


def test_word_starts_paint():
    """With the switch on, a prose row paints exactly as if the child had
    sent the prefixes in bold itself, while the alternate screen and the
    live input rows paint as before."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    plain = _view(_PROSE + "\r\n")
    child = _view(_PROSE_BOLD + "\r\n")
    off_img = plain.grab().toImage()
    on_img = _with_switch(True, lambda: plain.grab().toImage())
    check("word starts paint: ON changes the prose row",
          _band(plain, on_img, 0) != _band(plain, off_img, 0))
    check("word starts paint: ON draws the prefixes as the child's own "
          "bold would", on_img == child.grab().toImage())
    check("word starts paint: OFF leaves the child's text as it was",
          plain.grab().toImage() == off_img)
    # the bold pass is laid out once and kept: a new font size must not
    # reuse the old layout. The reverse-video cell ends the run, so the
    # bold text is the same string at both sizes and would hit the cache
    plain = _view(_PROSE + "\x1b[7m \x1b[27m\r\n")
    child = _view(_PROSE_BOLD + "\x1b[7m \x1b[27m\r\n")
    _with_switch(True, lambda: plain.grab())
    child.grab()
    # fewer rows fit at 18 px and pyte drops the top ones, so the text goes
    # back in after the change
    for tv, text in ((plain, _PROSE), (child, _PROSE_BOLD)):
        tv.set_font_size(18)
        tv.feed("\x1b[2J\x1b[H" + text + "\x1b[7m \x1b[27m\r\n")
    check("word starts paint: after A+ it still matches the child's bold",
          plain.visible_text().startswith(_PROSE)
          and _with_switch(True, lambda: plain.grab().toImage())
          == child.grab().toImage(), plain.visible_text())
    # an indented row: the bold text starts with blanks
    indent = _view("  a reading x\r\n")
    check("word starts paint: an indented row lands on the same columns",
          _with_switch(True, lambda: indent.grab().toImage())
          == _view("  a \x1b[1mread\x1b[22ming x\r\n").grab().toImage())

    alt = _view("\x1b[?1049h" + _PROSE + "\r\n")
    alt_off = alt.grab().toImage()
    check("word starts paint: nothing changes on the alternate screen",
          _with_switch(True, lambda: (alt.grab().toImage(), _masks(alt)))
          == (alt_off, None))

    # Claude's input box: the prompt row under the caret keeps its regular
    # weight, the reply above it still gets prefixes
    box = _view(_PROSE + "\r\n\r\n> typing some words")
    box_off = box.grab().toImage()
    box_on, masks = _with_switch(
        True, lambda: (box.grab().toImage(), _masks(box)))
    check("word starts paint: the input row is left alone",
          masks[2] is None
          and _band(box, box_on, 2) == _band(box, box_off, 2), masks)
    check("word starts paint: ...the reply above it is not",
          masks[0] == [0, 1, 2, 3, 8, 9, 13, 14]
          and _band(box, box_on, 0) != _band(box, box_off, 0), masks)
    # typed text that fills its last row leaves the caret alone on the blank
    # row below; the prompt must keep its regular weight there too
    cols = 24
    full = "> typing words here abcd"     # 24 = cols
    more = "typing words here abcde!"    # 24
    assert len(full) == len(more) == cols
    for name, text in (("one row", full), ("two rows", full + more)):
        wrapped = _view(_PROSE + "\r\n\r\n" + text + "\r\n",
                        rows=8, cols=cols)
        masks = _with_switch(True, lambda: _masks(wrapped))
        top = wrapped.screen.cursor.y
        check(f"word starts paint: a caret on the blank row below a full "
              f"prompt ({name}) leaves the prompt alone",
              masks[0] is not None
              and all(masks[r] is None for r in range(2, top)), masks)
    # a blank caret row under plain output, no prompt glyph, still applies
    plainout = _view(_PROSE + "\r\n" + _PROSE + "\r\n")
    check("word starts paint: a blank caret row under output without a "
          "prompt keeps every row's prefixes",
          _with_switch(True, lambda: _masks(plainout))[1] is not None)
    # when the guess finds no input box, every row keeps its prefixes
    shell = _view(_PROSE + "\r\n\r\n")
    check("word starts paint: no input box found, every row applies",
          _with_switch(True, lambda: _masks(shell))[0]
          == [0, 1, 2, 3, 8, 9, 13, 14])


def test_word_starts_overlays():
    """The find overlay and the focused cursor redraw text over the main
    pass; both keep the bold prefix under them."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    plain = _view(_PROSE + "\r\n")
    child = _view(_PROSE_BOLD + "\r\n")
    for tv in (plain, child):
        tv.set_find_query("reading")
        tv._find_refresh()
    check("word starts overlay: the find query matches",
          len(plain.find_matches()) == 1, plain.find_matches())
    found = _with_switch(True, lambda: plain.grab().toImage())
    check("word starts overlay: a find match over a word keeps the prefix",
          found == child.grab().toImage())

    # the caret over a prefix letter. _input_block_span would claim the
    # caret's row, so it is told to find nothing here
    plain = _view(_PROSE + "\x1b[1;2H")
    child = _view(_PROSE_BOLD + "\x1b[1;2H")
    for tv in (plain, child):
        tv.hasFocus = lambda: True
    plain._input_block_span = lambda cy=None: None
    check("word starts overlay: the focused cursor keeps the prefix",
          _with_switch(True, lambda: plain.grab().toImage())
          == child.grab().toImage())


def test_word_starts_rows():
    """_word_start_rows: wrapped words across rows and across the top of
    the view, link spans, and the per-row cache."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    # cols=12 wraps "readingmore" after "read"
    tv = _view("one two readingmore\r\n", rows=4, cols=12)
    masks = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: a word wrapped onto the next row gets one "
          "prefix", masks[:2] == [[0, 1, 4, 5, 8, 9], None], masks)

    # the head of the wrapped word scrolled into history: the tail on the
    # top row still knows it is a tail (the caret waits on a blank row, so
    # no row is an input row)
    tv = _view("one two readingmore\r\nlast\r\n", rows=3, cols=12)
    masks = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: a wrapped word's head in scrollback still "
          "counts", masks == [None, [0, 1], None], masks)
    tv._scroll_offset = 1
    masks = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: scrolled back, the head row gets its prefix",
          masks == [[0, 1, 4, 5, 8, 9], None, [0, 1]], masks)

    tv = _view("reading words\r\n", rows=3, cols=20)
    tv._link_spans = [(0, 0, 6)]
    masks = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: a link span keeps its regular weight",
          masks[0] == [8, 9, 10], masks)

    tv = _view("\x1b[1mreading\x1b[22m words\r\n", rows=3, cols=20)
    first = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: the cache holds only the rows on screen",
          0 < len(tv._ws_rows) <= tv.screen.lines, len(tv._ws_rows))
    tv.feed("\x1b[1;1H\x1b[2Kreading words\r\n")
    again = _with_switch(True, lambda: _masks(tv))
    check("word starts rows: the same text unbolded is a new cache key",
          first[0] == [8, 9, 10] and again[0] == [0, 1, 2, 3, 8, 9, 10],
          (first, again))
    tv._word_start_rows([], None, 0)
    check("word starts rows: switched off, the cache is dropped",
          tv._ws_rows == {})


def test_word_starts_spans_rest():
    """word_spans gives each letter run its bold head and its rest, the
    part the higher Contrast levels fade."""
    from app.word_starts import word_spans
    check("word spans: head and rest of each word",
          word_spans(_row("reading this")) == [(0, 4, 7), (8, 10, 12)],
          word_spans(_row("reading this")))
    check("word spans: a joined word has a head and rest per run, the "
          "joiner in neither",
          word_spans(_row("well-known")) == [(0, 2, 4), (5, 8, 10)],
          word_spans(_row("well-known")))
    check("word spans: punctuation stays out of the rest",
          word_spans(_row("Done.")) == [(0, 2, 4)],
          word_spans(_row("Done.")))
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    tv = _view(_PROSE + "\r\n")
    hist, off = tv._view_state()
    lines = [tv._visible_line(r, hist, off) for r in range(tv.screen.lines)]
    rows = _with_switch(True, lambda: tv._word_start_rows(lines, hist, off))
    check("word spans: the row mask marks heads 1 and rests 2",
          rows[0][:17] == bytes([1, 1, 1, 1, 2, 2, 2, 0, 1, 1, 2, 2, 0,
                                 1, 1, 2, 2]), rows[0][:17])


def _ink(tv, img, row: int, c0: int, c1: int) -> int:
    """Summed brightness of cells [c0, c1) of one visible row."""
    from app.widgets.terminal_view import CELL_PAD_X, CELL_PAD_Y
    x0 = int(CELL_PAD_X + c0 * tv._cell_w)
    x1 = int(CELL_PAD_X + c1 * tv._cell_w)
    y0 = int(CELL_PAD_Y + row * tv._cell_h)
    total = 0
    for y in range(y0, y0 + int(tv._cell_h)):
        for x in range(x0, x1):
            c = img.pixelColor(x, y)
            total += c.red() + c.green() + c.blue()
    return total


def test_word_starts_strength_paint():
    """Contrast: every level above 1 inks the heads heavier, the fading
    levels paint the rest of each word dimmer, and code stays as it was
    at every level."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import WORD_STRENGTHS
    QApplication.instance() or QApplication([])

    # "reading" head 0-3, rest 4-6; a path after it in columns 8-15
    tv = _view("reading app/x.py\r\n")
    shots = [_with_switch(True, lambda: tv.grab().toImage(), level)
             for level in range(1, len(WORD_STRENGTHS) + 1)]
    heads = [_ink(tv, img, 0, 0, 4) for img in shots]
    rests = [_ink(tv, img, 0, 5, 7) for img in shots]
    code = [_ink(tv, img, 0, 8, 16) for img in shots]
    check("contrast paint: every level above 1 inks the head heavier",
          all(h > heads[0] for h in heads[1:]), heads)
    check("contrast paint: the head never gets lighter going up",
          all(b >= a for a, b in zip(heads, heads[1:])), heads)
    fades = [lvl for lvl, (_s, f) in enumerate(WORD_STRENGTHS) if f < 1]
    check("contrast paint: the fading levels dim the rest of the word, "
          "more at each level",
          fades and all(rests[a] < rests[a - 1] for a in fades), rests)
    check("contrast paint: a level that does not fade leaves the rest as "
          "level 1 paints it",
          all(rests[lvl] == rests[0] for lvl, (_s, f)
              in enumerate(WORD_STRENGTHS) if f >= 1), rests)
    check("contrast paint: code keeps its pixels at every level",
          len(set(code)) == 1, code)
    off = tv.grab().toImage()
    check("contrast paint: with the switch off no level changes a pixel",
          all(_with_switch(False, lambda: tv.grab().toImage(), level)
              == off for level in range(1, len(WORD_STRENGTHS) + 1)))
    # the find overlay redraws the text: it keeps the level's look
    found = _view("reading app/x.py\r\n")
    found.set_find_query("reading")
    found._find_refresh()
    top = len(WORD_STRENGTHS)
    lo, hi = (_with_switch(True, lambda: found.grab().toImage(), level)
              for level in (1, top))
    check("contrast paint: a find match keeps the level under its box",
          _ink(found, hi, 0, 5, 7) != _ink(found, lo, 0, 5, 7))


def test_word_starts_strength_option():
    """Options > Contrast: a -/+ row next to the switch, a level per
    terminal font, persisted under ui and restored."""
    import shutil
    import tempfile
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from app import ui_theme
    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from app.widgets.terminal_view import (DEFAULT_WORD_STRENGTH,
                                           WORD_STRENGTHS, TerminalView)
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    top = len(WORD_STRENGTHS)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-wordstrength-"))
    font0 = ui_theme.TERMINAL_FONT
    try:
        bar = TopBar()
        check("contrast option: the row is in the Options panel",
              bar.options_panel.isAncestorOf(bar.word_strength_inc_btn)
              and bar.options_panel.isAncestorOf(bar.word_strength_label))
        check("contrast option: the label reads the default level",
              bar.word_strength_label.text()
              == f"{DEFAULT_WORD_STRENGTH} of {top}",
              bar.word_strength_label.text())
        emitted = []
        bar.wordStrengthDelta.connect(emitted.append)
        bar.word_strength_inc_btn.click()
        bar.word_strength_dec_btn.click()
        check("contrast option: + and - emit +1 and -1",
              emitted == [1, -1], emitted)
        bar.set_word_strength(1)
        check("contrast option: - is disabled at level 1",
              not bar.word_strength_dec_btn.isEnabled()
              and bar.word_strength_inc_btn.isEnabled())
        bar.set_word_strength(top)
        check("contrast option: + is disabled at the top level",
              bar.word_strength_dec_btn.isEnabled()
              and not bar.word_strength_inc_btn.isEnabled())
        bar.deleteLater()

        win = create_main_window(SessionStore(path=tmp / "session.json"))
        win._save_timer.stop()
        check("contrast option: a fresh window has the default level",
              TerminalView.word_strength() == DEFAULT_WORD_STRENGTH)
        tv = _view(_PROSE + "\r\n")
        TerminalView.set_word_starts_enabled(True)
        before = tv.grab().toImage()
        win._change_word_strength(+1)
        check("contrast option: + raises the level of every terminal",
              TerminalView.word_strength() == DEFAULT_WORD_STRENGTH + 1
              and tv.grab().toImage() != before
              and win.top_bar.word_strength_label.text()
              == f"{DEFAULT_WORD_STRENGTH + 1} of {top}")
        check("contrast option: a change marks the session for saving",
              win._save_timer.isActive())
        for _ in range(top + 2):
            win._change_word_strength(+1)
        check("contrast option: the level stops at the top",
              TerminalView.word_strength() == top)
        for _ in range(top + 2):
            win._change_word_strength(-1)
        check("contrast option: ...and at 1",
              TerminalView.word_strength() == 1)
        first = ui_theme.TERMINAL_FONT
        other = next(f for f in ui_theme.TERMINAL_FONTS if f != first)
        win._change_terminal_font(other)
        check("contrast option: another font starts at its own level",
              TerminalView.word_strength() == DEFAULT_WORD_STRENGTH
              and win.top_bar.word_strength_label.text()
              == f"{DEFAULT_WORD_STRENGTH} of {top}")
        win._change_word_strength(+1)
        win._change_terminal_font(first)
        check("contrast option: switching back brings the font's level back",
              TerminalView.word_strength() == 1)
        payload = win._session_payload()
        check("contrast option: the levels are persisted per font under ui",
              payload["ui"]["bold_word_strength"]
              == {first: 1, other: DEFAULT_WORD_STRENGTH + 1},
              payload["ui"])

        win._restore_ui_state({"ui": {
            "terminal_font": other,
            "bold_word_strength": {first: 2, other: 4, "x": 99,
                                   "y": "3", "z": True}}})
        check("contrast option: restore applies the saved font's level and "
              "drops bad entries",
              TerminalView.word_strength() == 4
              and win._word_strengths == {first: 2, other: 4},
              win._word_strengths)
        win._restore_ui_state({"ui": {"terminal_font": first}})
        check("contrast option: a session that predates it gets the default",
              TerminalView.word_strength() == DEFAULT_WORD_STRENGTH
              and win._word_strengths == {})

        win._change_word_strength(-1)
        win._save_session()
        win.close()
        app.processEvents()
        TerminalView.set_word_strength(DEFAULT_WORD_STRENGTH)
        again = create_main_window(SessionStore(path=tmp / "session.json"))
        check("contrast option: the level survives a close and reopen",
              TerminalView.word_strength() == DEFAULT_WORD_STRENGTH - 1
              and again.top_bar.word_strength_label.text()
              == f"{DEFAULT_WORD_STRENGTH - 1} of {top}")
        again._save_timer.stop()
        again.close()
        app.processEvents()
        tv.deleteLater()
    finally:
        ui_theme.TERMINAL_FONT = font0
        TerminalView.set_word_starts_enabled(False)   # both are global
        TerminalView.set_word_strength(DEFAULT_WORD_STRENGTH)
        shutil.rmtree(tmp, ignore_errors=True)
