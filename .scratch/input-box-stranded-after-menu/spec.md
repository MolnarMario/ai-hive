# Fix: the input box stays stranded mid-screen after a slash-command menu closes

Status: ready-for-agent

## Context

Open `/model` (or any tall slash-command menu) in a card, close it, and the input
box is left sitting a quarter of the way down the terminal with dead blank space
below it and almost none of the conversation visible above it. The user has to
scroll to read the previous replies. Reported with screenshots.

AI Hive already has a self-heal for exactly this shape:
`TerminalView._check_input_gap` (app/widgets/terminal_view.py:2030) is supposed
to notice a footer stranded far from the bottom and emit `staleLayoutDetected`,
which `TerminalCard._wire` (terminal_card.py:677) turns into
`TerminalAgent.request_repaint()`. It has never fired on a real Claude screen.
Two independent reasons:

1. **The blank-run scan aborts on the box's own chrome.** After finding the
   input span it walks every row below `edge` and bails the moment one has
   content (terminal_view.py:2062). On a real classic-renderer screen the row
   directly under the `>` line is the input box's bottom BORDER, and the footer
   hint wraps onto a second row (`• high · /effort`) that matches none of
   `_CLAUDE_READY_HINTS`. Either one aborts the scan, every time. This is the
   same fixture blind spot that hid the `reply_anchor_line` bug: the existing
   test (`tests/smoke_test.py:4834`) feeds `"> \r\n? for shortcuts"`, a screen
   with no border and a one-row footer, which Claude never draws.

2. **The only trigger is a typing burst.** `_check_input_gap` is called from
   `_on_input_settled`, which `_snap_timer` fires 600 ms after an edit
   keystroke, and `_kick_snapshot` arms that timer only for printable text,
   Backspace and Delete. Closing a menu with Esc or Enter arms nothing, so
   nothing checks until the user starts typing their next prompt.

Intended outcome: about a second after the menu closes, the conversation slides
back down and the input box returns to the bottom of the card, with no keystroke
needed and no visible resize blip during ordinary use.

## The fix

All of it is in `app/widgets/terminal_view.py`, plus test updates.

### 1. Measure the gap from the last content row on the screen

Replace the per-row blank scan with a single "where does the picture end"
reading, so the box's border and a wrapped footer stop mattering.

Add a small helper beside `_row_content`:

```python
def _last_content_row(self) -> int:
    """Bottom-most non-blank screen row, or -1 when the screen is empty."""
    for r in range(self.screen.lines - 1, -1, -1):
        if self._row_content(r) != (-1, -1):
            return r
    return -1
```

Add a constant beside `_INPUT_GAP_TOLERANCE`:

```python
# Rows of Claude's own box chrome allowed between the last typed line and the
# bottom of the picture: the box's bottom border plus a footer hint that wraps
# onto a second or third row on a narrow card. More than this below the box
# means real content is down there, so the layout is not stranded.
_INPUT_CHROME_ROWS = 4
```

Rewrite the body of `_check_input_gap` as:

```python
if self._scroll_offset or self._alt_screen:
    return                       # only the live tail can be stranded
if not len(self.screen.history.top):
    self._input_gap_row = None
    return                       # see the note below
span = self._input_block_span()
if span is None:
    self._input_gap_row = None
    return
top, bottom = span
first, _ = self._row_content(top)
if first < 0 or self.screen.buffer[top][first].data not in _INPUT_PROMPTS:
    self._input_gap_row = None
    return                       # not the input box, just the caret on output
edge = self._last_content_row()
if edge < bottom or edge - bottom > _INPUT_CHROME_ROWS:
    self._input_gap_row = None
    return                       # real content below the box
gap = (self.screen.lines - 1) - edge
if gap <= _INPUT_GAP_TOLERANCE:
    self._input_gap_row = None
    return
if self._input_gap_row == edge:
    return                       # already asked for a repaint at this spot
self._input_gap_row = edge
self.staleLayoutDetected.emit()
```

Two of those guards are new and both earn their place:

- **Empty history means nothing is stranded.** A fresh or freshly cleared
  conversation legitimately sits high with blank space under it, and its footer
  row moves down a little every turn, so without this the new trigger below
  would ask for a resize once per turn. A screen that has never scrolled a line
  off cannot have been scrolled up by a dropdown.
- **The span's top row must start with `>` or `❯`.** `_input_block_span`
  happily returns a span with no prompt glyph (it keeps `top = cy`), so mid
  reply, with the caret on output and blank rows below, the old code would have
  read that as a stranded box. The keystroke trigger hid this; the output
  trigger would not.

### 2. Check after the screen settles, not only after typing

Add a second single-shot timer next to `_snap_timer` in `__init__`:

```python
# The stranded-layout check also has to run when the user has typed NOTHING:
# closing a slash-command menu is pure child output (see _check_input_gap).
# Restarting on every burst means a long redraw collapses into one check once
# the screen is quiet, and the _input_gap_row edge guard keeps it to one
# repair per occurrence.
self._gap_timer = QTimer(self)
self._gap_timer.setSingleShot(True)
self._gap_timer.setInterval(_GAP_CHECK_MS)   # 500
self._gap_timer.timeout.connect(self._check_input_gap)
```

and start it at the end of `feed()`, after `self.update()`:

```python
self._gap_timer.start()
```

Keep the existing `_check_input_gap()` call in `_on_input_settled`. It costs
nothing (the edge guard makes a second call a no-op) and keeps the typing path
covered if a child ever strands the box without emitting anything afterwards.

### 3. Docs and version

- Update the `_check_input_gap` docstring: it no longer piggybacks on the undo
  debounce alone, and the blank-run scan is now a last-content-row reading.
- Update the matching CLAUDE.md bullet ("A dropdown that scrolls the classic
  renderer can strand the input box"). It currently states the check
  deliberately rides `_snap_timer` rather than adding a second timer, and that
  it fires only on the footer-row edge. Say instead that it rides a debounced
  output-settle timer, that the edge guard plus the empty-history guard are what
  keep ordinary use free of repaint blips, and record why the shipped version
  never fired (the box border and the wrapped footer aborted the scan, and the
  test fixture drew neither, exactly like the `reply_anchor_line` miss).
- Bump `__version__` in `app/__init__.py` (0.18.4 to 0.18.5).

## Tests

Rework `test_input_gap_self_heal` in `tests/smoke_test.py:4834`. The fixtures
must draw the shape Claude really draws, or they test a screen that cannot
happen:

- Feed enough lines to push history (`view.feed("x\r\n" * 30)` on a 20-row
  view), then the real box: a rule row, `> `, a rule row, the footer hint, and a
  wrapped second footer row. Assert it fires once and that a second settle with
  nothing changed does not refire.
- Same shape with no history: never fires.
- Same shape but with content further down the screen (a menu still open):
  never fires.
- Footer within `_INPUT_GAP_TOLERANCE` of the bottom: never fires (keep the
  existing case, adding the border row).
- Caret on plain output with no `>` above it and blank rows below: never fires.
- New: drive the output path rather than `_on_input_settled`, by calling
  `view._check_input_gap()` directly after a feed (the timer itself is a plain
  Qt debounce, and the suite's existing pattern is to call the settled handler
  directly rather than pump a real timer).

Then run the full suite:

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

Update the check count in README.md's Verify section.

## Live verification, and the one thing that could still be wrong

The repair itself is `TerminalAgent.request_repaint()`, which sends the child
one column narrower and back. This is unverified in practice, because the check
it hangs off has never fired. The mechanism it relies on is ConPTY reflow:
conhost re-lays-out its buffer on a resize and repaints the viewport, which is
what a real terminal gives you for free when you drag its window. Claude itself
cannot fix this (the lines that scrolled away are in our scrollback, not in its
render tree), so if the resize does not bring them back, nothing at this layer
will.

So verify by hand before calling it done:

1. Run the app, open a Claude card, let the conversation be long enough to have
   scrolled (a few replies).
2. Type `/model`, press Enter, then Esc.
3. Within about a second the conversation should slide back down with the input
   box at the bottom of the card.

If the box does not move, stop and report that, rather than stacking more
heuristics on top. The next option is a view-side repair (rendering the window
so it ends at the last content row) and that one needs its own design, because
it fights `_snap_to_bottom` and the meaning of `_scroll_offset`.

## Files

- `app/widgets/terminal_view.py` (`_check_input_gap`, `_last_content_row`,
  `feed`, `__init__`, `_INPUT_CHROME_ROWS`, `_GAP_CHECK_MS`)
- `tests/smoke_test.py` (`test_input_gap_self_heal`)
- `CLAUDE.md`, `README.md`, `app/__init__.py`
