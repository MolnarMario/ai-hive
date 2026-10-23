# Bold word starts

Status: ready-for-agent

An Options > Appearance switch, "Bold word starts", off by default. While
it's on, the agent terminals draw the first half of each prose word in
bold ("**rea**ding **th**is **lin**e"), the way bionic reading does. Some
readers, dyslexic readers among them, find it easier to track. The
published tests don't show a reading-speed gain, so it's an opt-in
comfort setting, and the tooltip shouldn't promise more than that.

Don't call it "Bionic Reading" anywhere a user can see it. That's a
trademark.

A second agent is building the terminal font picker and letter spacing at
the same time (`01-terminal-font-picker.md`). Read "Working alongside
ticket 01" before you touch `terminal_view.py`.

## How the terminal draws text today

`TerminalView` (`app/widgets/terminal_view.py`) paints the pyte grid itself.
`paintEvent` (around line 3202) batches each run of same-attribute cells,
then splits the run into grid-safe pieces (block elements and glyphs the
font lacks go alone), and draws each piece with one `drawText`. Columns are
fixed: column N sits at `CELL_PAD_X + N * _cell_w`. A bold glyph that
advances more than one cell shears the rest of the row.

Consolas has a real Bold face with the same advance, so bold is safe there.
A family without one gets Qt's synthetic bold, which on the real Windows
platform adds 0.25 px per glyph. Ticket 01 fixes that inside its style
helper `_run_font`. This feature has to draw bold through that helper, or
it breaks the grid the moment a user picks OpenDyslexic Mono.

## What to build

### 1. A pure segmentation module

Create `app/word_starts.py`, Qt-free (like `app/providers.py`), with one
entry point roughly like:

```python
def word_start_spans(cells, prev_row_tail=None) -> list[tuple[int, int]]:
    """Half-open column spans to draw bold on one row."""
```

`cells` is what pyte gives a row, one object per column with `.data` and
`.bold`. Work on the whole row, before it's split into attribute runs: a
word can cross a colour change, and splitting per run would bold two
prefixes in one word.

Rules:

- Split the row into whitespace-delimited tokens. Strip leading
  `( [ { " ' `` ` and trailing `. , ; : ! ? ) ] } " ' `` ` from each.
- Skip the token unless what's left is letters, optionally joined by an
  internal apostrophe or hyphen. That keeps code and paths
  (`app/widgets/sidebar.py`, `snake_case`, `--flag`, `x=1`, `0x1b`) in
  their regular weight. camelCase is all letters and gets the treatment,
  which is fine.
- A letter is a cell whose `data[:1].isalpha()`. pyte stores combining
  marks in the same cell (`"é"`), so this handles accented text
  without a regex library.
- In each letter run of length n >= 2, bold the first `(n + 1) // 2`
  letters. Single letters stay regular.
- Skip cells that are already bold. The child's own bold (Claude's
  headings and emphasis) wins.
- Wrapped words: if the previous row's last column holds a letter and this
  row's column 0 holds a letter, treat this row's first word as a
  continuation and don't bold it. `prev_row_tail` carries what you need
  from the row above (its last cell's data is enough).

Unit-test this module on plain Python objects, no Qt.

### 2. Where it applies

- Every row of the normal screen, scrollback included.
- Not while the alternate screen is up (`_alt_screen`, vim and less). That
  flag covers the whole screen, so skip the whole screen.
- Not on the live input rows (`_input_block_span()`, around line 2318),
  where the bold would jump around as the user types. That span is a
  Claude-specific guess, so when it finds nothing, apply everywhere.
- Not inside clickable links (`_link_spans`). The token rules already skip
  most paths and URLs. This covers the rest of what the view treats as a
  link.

### 3. Painting

Compute the spans per visible row once per paint, next to where
`paintEvent` already snapshots `lines`. Inside the piece loop, split each
piece at span boundaries and draw the bold parts with
`_run_font(True, char.italics, char.underscore)`. Keep pieces contiguous:
one drawText per regular stretch and one per bold stretch, never per glyph.
The renderer already learned that per-glyph drawing made a page of
one-letter find matches cost 90 ms (comment around line 3347).

Two other paths redraw text over the main pass and must show the same
bold, or the effect blinks off under them:

- The find overlay (Ctrl+F) redraws matched cells in dark ink.
- The focused cursor redraws its cell inverted.

Both are yours either way. Draw them with `_run_font`, using the cell's
own bold, italic and underline, plus the word-start bold. Today both build
fonts by hand: find uses the base font and drops bold and italic, and the
cursor inherits whatever font the painter last held. If ticket 01 has
landed, it has already moved them onto `_run_font`, and you add the spans.

### 4. Performance

`paintEvent` is one of the hottest functions in the app. Measure before
and after with the switch on, on a 120x50 screen of prose plus the find
overlay showing a common word. Paint into a `QImage` 50 times and compare
the means. With the switch off, painting must take the same time as
before. With it on, it may cost at most 1.5x, and report the numbers you
got. If segmentation shows up in the profile, cache spans per row keyed by
the row's text and bold mask together, not text alone. `perf_audit` keeps
only the longest paint per interval, so it can't stand in for this
comparison.

### 5. Options and persistence

Add a switch row to Appearance in `TopBar.__init__` (`main_window.py`
around line 870), directly after "Tooltips". Copy the "Reply times"
pattern end to end:

- a class flag on `TerminalView` with `set_word_starts_enabled(on)`, which
  repaints every view (see `set_reply_stamps_enabled`, around line 1360)
- the TopBar label, switch, tooltip and `...Toggled` signal
- `MainWindow._on_..._toggled` sets the flag and calls `_schedule_save()`
- `ui["bold_word_starts"]` in `_session_payload`, restored in
  `_restore_ui_state` with default `False`

It's an additive key with a safe default, the same as `reply_stamps`, so no
`SESSION_VERSION` bump. Match the neighbouring labels' style for the label
text. No em dash in any visible string
(`test_no_em_dashes_in_visible_text`).

## Tests

Module tests (new `tests/smoke/word_starts.py`, or in `terminal.py`):

- token rules: prose words get spans; paths, flags, snake_case, numbers and
  single letters don't; trailing punctuation doesn't block a word
- a word split across two colour runs gets one prefix
- a word wrapped onto the next row gets one prefix
- already-bold cells are left alone
- accented and combining-mark words

View tests in `tests/smoke/terminal.py` (import the harness before `app`):

- the switch persists through a temp `SessionStore` round trip and
  defaults off
- with it on, a prose row renders bold prefixes: paint to `QImage` and
  compare against the switch off, or assert on the pieces handed to
  drawText if you add a seam for that
- nothing changes on the alternate screen or the input rows
- a find match over a word keeps the bold prefix

Keep check names ASCII. Run `tests\smoke_test.py -m terminal`, then
`--quick`.

## Working alongside ticket 01

Ticket 01 rewrites font setup and adds `_run_font`, and both of you edit
`paintEvent`. To keep the merge small:

- You own `app/word_starts.py`, the switch row after "Tooltips", the
  `ui["bold_word_starts"]` key, the span computation in `paintEvent`, the
  split-at-spans step in the piece loop, and the word-start bold in the
  find overlay and cursor redraws.
- Ticket 01 owns font setup, metrics, `_run_font`, `_is_grid_glyph`, and
  the family and spacing rows and keys.

Build and test the module first. It touches no shared file. Before you
start on `terminal_view.py`, run `git fetch origin` and check whether
`_run_font` exists on origin/main.

- If it does, merge origin/main and use it.
- If it doesn't, add a minimal one with exactly this signature,
  `_run_font(self, bold: bool, italic: bool, underline: bool) -> QFont`,
  returning `QFont(self._font)` with those three set. Use it in your piece
  split and in the find and cursor redraws. Ticket 01 replaces its body
  when it merges and keeps your call sites. Say in your report which
  ticket you found on origin/main.

README: add the feature to the feature list. Leave the check count, the
version and the CHANGELOG to the integrator.
