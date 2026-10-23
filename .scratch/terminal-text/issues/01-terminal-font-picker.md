# Terminal font picker and letter spacing

Status: ready-for-agent

Options > Appearance gets a "Terminal font" dropdown and a "Letter spacing"
stepper for the agent terminals. The goal is readability, dyslexic readers
in particular: one of the fonts on offer is OpenDyslexic Mono, and wider
letter spacing is the change with the best evidence behind it (Zorzi et
al. 2012).

A second agent is building "Bold word starts" at the same time
(`02-bold-word-starts.md`). Read "Working alongside ticket 02" before you
touch `paintEvent`.

## How the terminal draws text today

`TerminalView` (`app/widgets/terminal_view.py`) paints the pyte grid itself.
`paintEvent` batches each run of same-attribute cells into one
`drawText`. Column N sits at `CELL_PAD_X + N * _cell_w`, where `_cell_w` is
the advance of "M" (around line 717) and `_cell_h` is `metrics.height()`.
Claude's TUI lays everything out in columns, so a glyph that advances
anything other than one cell shears the rest of its run. `_is_grid_glyph`
catches that for non-ASCII characters in the regular face, and
`_BLOCK_RECTS` draws U+2580-259F as rectangles.

The view hardcodes `QFont("Consolas")` (line 712). The theme field
`console_font` (`app/ui_theme.py`, Mechanicus names Share Tech Mono) never
reaches it. It only reaches the QSS of line-mode cards (`#Console`,
`#CardInput`) and some chrome (activity log, files, lanes, around
`ui_theme.py:1158`). That is a bug this ticket fixes.

## What to build

### 1. The font list

| id | Label | Source |
|---|---|---|
| `theme` | Theme default | the active theme's `console_font` chain |
| `consolas` | Consolas | Windows |
| `cascadia` | Cascadia Mono | https://github.com/microsoft/cascadia-code/releases |
| `jetbrains` | JetBrains Mono | https://github.com/JetBrains/JetBrainsMono/releases, the **NL** (no ligatures) faces |
| `plex` | IBM Plex Mono | https://github.com/IBM/plex/releases |
| `atkinson` | Atkinson Hyperlegible Mono | https://github.com/googlefonts/atkinson-hyperlegible-next-mono |
| `opendyslexic` | OpenDyslexic Mono | https://opendyslexic.org, https://antijingoist.itch.io/opendyslexic |

Cascadia isn't installed on the user's machine, so bundle it like the
others. Put the faces in `app/assets/fonts` (`main._load_bundled_fonts`
registers every .ttf/.otf there) and each family's license as
`app/assets/fonts/licenses/<Family>-OFL.txt`. Use static Regular, Bold,
Italic and Bold Italic files where the family ships them, not variable
fonts. Use the family name Qt reports for the file
(`QFontDatabase.applicationFontFamilies`), not the repo name.

Download into a fresh empty folder in your scratchpad, and copy only the
font and license files into the repo.

**OpenDyslexic Mono needs checking before you bundle it.** The Mono file
is one Regular face, about 35 kB, and it may predate the project's move to
the SIL OFL (older releases were CC-BY 3.0 on a Bitstream Vera base). Find
the exact file, its license, and its coverage of what Claude prints:
U+2500-257F box drawing, `● ⎿ ✻ ✶ ⏺ · … ↑ ↓ ✓ ✗ ❯`. If the license doesn't
allow bundling, stop and tell the user rather than shipping it. Thin
coverage is fine (fallback handles it, see 3), but say what's missing in
your final report.

### 2. "Theme default"

Store the sentinel `theme`, never the family it resolved to. Parse the
theme's CSS chain into family names (strip quotes, drop the generic
`monospace`) and hand the list to `QFont.setFamilies` so Qt keeps the
fallback order. A theme switch re-resolves the font only while `theme` is
selected.

The picker sets the font for the PTY terminals and for line-mode
`#Console`/`#CardInput`. The other `CONSOLE_FONT` consumers in the chrome
stay on the theme. Keep the picked value separate from `CONSOLE_FONT`
rather than overwriting it.

Default is `theme`. Mechanicus users will see their terminals switch from
Consolas to Share Tech Mono on upgrade. The theme always intended that, but
mention it in your report so the user can flip the default if they'd
rather not.

### 3. Grid safety for every style

The current grid check doesn't match how fonts outside Consolas behave:

- `_is_grid_glyph` measures only the regular face, but runs paint in bold
  or italic.
- Qt synthesizes bold for a family without a Bold face. On the real
  Windows platform that adds 0.25 px per glyph (measured with Share Tech
  Mono at 13 px: a 40-char bold run paints 10 px, about 1.4 cells, too
  wide). OpenDyslexic Mono and Share Tech Mono both hit this.
- **The offscreen platform the suite runs on reports equal advances**, so
  no headless metric check can see the drift.

Add one helper that owns the per-style font, something like
`_run_font(bold: bool, italic: bool, underline: bool) -> QFont`, cached per
combination and cleared when the metrics change. For each style, measure
the styled advance of "M". If it differs from `_cell_w`, set
`QFont.setLetterSpacing(AbsoluteSpacing, letter_px - drift)` on that styled
font. Measured on Windows: this brings a 40-char synthetic-bold run back to
exactly 40 cells, and its ink ends 1 px right of the regular run (bold ink
is fatter, which is fine). `_is_grid_glyph` then checks non-ASCII glyphs
against the styled font, not the base one.

`paintEvent`, the find overlay redraw and the cursor redraw all build fonts
by hand today (`QFont(self._font)` plus `setWeight`/`setItalic`). Route all
of them through the helper. Luna noticed the cursor redraw inherits
whatever font the painter last held, and the find overlay drops bold and
italic. Fix both while you're there: draw the cell with its own style.

### 4. Letter spacing

A stepper from 0 to 3 px of extra space per cell, default 0. Apply it with
`QFont.setLetterSpacing(AbsoluteSpacing, px)` on the base font, before
measuring "M". Measured: Qt adds the spacing to every glyph's advance in
`QFontMetricsF`, single characters included, so `_cell_w` grows by exactly
`px` and a drawText run still lands on the grid. Don't take that on trust
for the bundled fonts. The test in 6 has to prove it from painted pixels.

Things that read `_cell_w` or the font and need checking with spacing on:
cell hit testing (around line 2157), selection, find boxes, link
underlines, the reply stamp (it places itself by `len(text)` columns at
around line 3383, which stays right only if the stamp font carries the
same spacing), and the IME preedit (it measures its own advance).

If you can't get painted columns to line up across styles with spacing on,
drop letter spacing from this ticket and say so. Don't ship it half
aligned.

### 5. One metrics path, and fewer PTY resizes

Fold metric setup into one `_apply_metrics()` used by `__init__`,
`set_font_size`, and the new family and spacing setters. It recomputes
`_cell_w`, `_cell_h` and `_ascent`, clears `_grid_glyph_cache` and the
style cache, then resizes. Every rows/cols change resizes the PTY and
reprojects restored screens (`sizeChanged` is wired to both at
`terminal_card.py:1152`), and clicking a stepper three times shouldn't
make Claude redraw three times. Debounce setter-driven resizes through the
existing 120 ms `_resize_timer`, but keep Ctrl+wheel zoom feeling as
immediate as it does today.

Family and spacing are global. They apply to every PTY card, including
cards with a per-agent `spec.font_px`, and those cards keep their own
pixel size. `MainWindow._change_global_font` (around line 7128) shows the
loop and the override rule.

### 6. Options and persistence

Add two rows to the Appearance section in `TopBar.__init__`
(`main_window.py` around line 872), right after "Font size": "Terminal
font" (a `DropDownComboBox` like `theme_select`) and "Letter spacing" (−/+
buttons like the font size ones). Tooltips go through `app.tooltips`. No em
dash in any visible string (`test_no_em_dashes_in_visible_text`).

Persist `ui["terminal_font"]` (an id from the table, unknown values fall
back to `theme`) and `ui["terminal_letter_px"]` (int, clamped 0..3) in
`_session_payload`. These are additive keys with safe defaults, the same
pattern as `reply_stamps`, so no `SESSION_VERSION` bump. Each mutation
calls `_schedule_save()`. Restore them in two places, the same way
`console_font_px` works:

- `main.setup_application`, after `_load_bundled_fonts` and before any
  widget exists, so the real launch builds and replays every card once,
  with the right metrics.
- `MainWindow._restore_ui_state`, for the suite's path where cards may
  already exist.

## Tests

Add them to `tests/smoke/terminal.py`. Import the harness before `app`.

- Persistence round trip through a temp `SessionStore`: both keys survive,
  junk values fall back to the defaults.
- "Theme default" follows a theme switch. A picked family ignores one.
- A per-agent `font_px` card keeps its size when the family changes.
- The family resolves to the bundled font for every entry in the table
  (`QFontInfo(font).family()`), headless with the bundled files registered.
- **Painted alignment on the real Windows platform.** Offscreen can't see
  synthetic bold drift (see 3), so run this one as a small subprocess with
  `QT_QPA_PLATFORM=windows`. It only measures fonts and paints into a
  `QImage`, it never shows a window, and it starts no AI CLI. For every
  bundled family, every style, and spacing 0 and 3, paint a 40-char run of
  "M", find the rightmost ink, and assert it falls inside cell 40. Skip
  with a printed reason when the platform plugin can't start (CI without a
  desktop).
- A find match, the cursor cell and a reply stamp on a bold row paint with
  the row's style (render to `QImage`, compare against a regular-style
  render).

Keep check names ASCII. Run `tests\smoke_test.py -m terminal`, then
`--quick`.

## Working alongside ticket 02

Ticket 02 adds "Bold word starts": inside a text piece, the first half of
each word is drawn bold. It depends on your style helper, so stick to these
lines:

- You own font setup, metrics, `_run_font`, `_is_grid_glyph` and the
  family/spacing Options rows and `ui` keys.
- Ticket 02 owns `app/word_starts.py` (a new Qt-free module), one switch
  row placed after "Tooltips" in Appearance, the `ui["bold_word_starts"]`
  key, the call that splits a text piece at the word-start spans, and the
  word-start bold in the find overlay and cursor redraws.
- Keep the name and signature `_run_font(bold, italic, underline) -> QFont`
  exactly. If 02 merges first, it ships a minimal `_run_font` with that
  signature and already routes the find and cursor redraws through it.
  When you merge origin/main, keep your `_run_font` body and keep 02's
  call sites and span handling. In that case step 3's find and cursor fix
  is already done.

## Not in this ticket

Line spacing. Taller rows leave gaps in the box-drawing borders (`│` stops
at the font's own height), so it needs U+2500-257F drawn as geometry,
light, heavy, double, dashed and rounded forms, the way `_BLOCK_RECTS`
draws blocks. That is its own ticket. If you hit something here that makes
it easier, note it in your report.

README: add the feature to the feature list. Leave the check count, the
version and the CHANGELOG to the integrator.
