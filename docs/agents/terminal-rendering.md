# Terminal rendering: doubled text in Claude cards

Doubled text in a card's scrollback has come back several times, each time in
a new shape, usually after a Claude Code release changed how it redraws. This
page lists the shapes seen so far, what defends against each, and how to pin
down the next one from real bytes instead of a screenshot.

## Why it keeps happening

AI Hive runs Claude with its classic renderer (`tui: "default"`), so AI Hive
owns the scrollback: every row that scrolls off a card goes into pyte's
history in `TerminalView`. Claude redraws its live frame in place by moving
the cursor and writing rows again. Any redraw that writes more rows than the
screen holds scrolls the extra rows off the top, and those rows are copies of
what is already in the scrollback.

The copies are in Claude's bytes. Windows Terminal or a bare conhost fed the
same stream ends up with the same copies, so a fix on our side means
recognising a copy and taking it back, or not asking Claude for the redraw
that makes it.

The pty is pywinpty 3.0.5 with its bundled OpenConsole 1.24. That ConPTY
passes Claude's output through almost verbatim. Claude's console has
`DISABLE_NEWLINE_AUTO_RETURN` set, so its bare `\n` reaches us as a line feed
without a carriage return. On every capture so far pyte and conhost agree on
every frame (`tools/render_replay.py --conhost`), which is how we know a copy
is Claude's doing and not an emulation bug.

## Shapes seen so far

| Shape | What triggers it | Defense |
|---|---|---|
| A frame taller than the card repainted from the top | Ctrl+O twice, the idle recap. Claude climbs with `ESC[nA`, ConPTY stops it at row 0, and the frame's top rows scroll off a second time. | `TerminalView._drop_repaint_copies` takes back pushed rows that equal the scrollback's end, cell for cell, after a cursor-up reached row 0. |
| A redraw that leaves the screen as it was | Claude's misplaced redraw after the recap. | `_drop_repeated_push`, the older rule. |
| A redraw at another width | Any column change. Claude erases the screen from the home position and draws the lines that filled it, picked by the old layout and wrapped at the new width. One column narrower, a row that reached the last column takes two rows, the redraw writes past the bottom, and the screen's top row scrolls off as a copy. | `request_repaint` takes a row away and gives it back, never a column (fixed 2026-11-03, claude 2.1.316). A real resize goes through the reflow (`REFLOW_BEGIN`/`REFLOW_END`), which replaces the scrollback with Claude's full reprint. |
| A redraw that pulls scrollback rows back on screen | The `/` or `@` menu scrolls the screen to make room, closing it strands the input box above blank rows, and the redraw that heals it shows the scrolled-off rows again while they are still in the scrollback. | `REPAINT_DONE`: once `request_repaint`'s redraw settles, the view takes out of the scrollback the rows that now start the screen (`_pull_back_redrawn`). A run that repeats once more on either side is real output and stays. A request that comes in while one is in flight runs after it. |
| A replay at the wrong size | Rebuilding a card from the raw stream at its current width. Claude's erase counts rows at the old width. | The agent records every view size with its stream offset (`ScreenSize`), and a replay feeds each part at its size. |

Two limits of Claude that the defenses lean on:

- A resize to more than 2048 rows gets no reprint at all, so a reflow never
  asks for more (`REFLOW_MAX_ROWS`).
- The idle recap needs three real prompts in the conversation and three
  minutes blurred (focus out) after a turn ends. The delay comes from the
  `tengu_sedge_lantern_config` flag, 180 s by default in 2.1.316.

## The detector

`TerminalView._scan_for_copies` watches every row that reaches a Claude
card's scrollback, between frames, after the take-back rules ran. A row
identical to the row before it, or a run of rows that repeats the run right
before it, counts as doubled when it holds prose (24 letters or digits; two
such rows for a run longer than one). A todo list Claude prints again further
down doesn't count, and neither do rules or short code lines.

A sighting writes one line to `session.log`, at most every 10 minutes per
agent:

```
RENDER-DUP agent=Agent 1 claude=2.1.316 size=30x155 period=1 run=2 text='The heading block reads ...' dump=...\render_dumps\20261103-024512-Agent-1-3f9c0a1e.json
```

The dump in `%APPDATA%\AIHive\AI Hive\render_dumps\` holds the agent's last
512 KiB of raw stream, every view size with its offset, and the Claude
version. The newest five are kept. Nothing is changed on screen; the line is
there so the next shape arrives with its bytes.

## When RENDER-DUP shows up

1. Find the line in `session.log` and note the Claude version. If it is newer
   than the one in the table above, suspect the update first.
2. Replay the dump: `.venv\Scripts\python.exe tools\render_replay.py <dump>
   --rows 40`. It prints where in the stream each doubled run landed and the
   end of the scrollback.
3. Add `--raw` to see what the bytes do with the take-back rules off, and
   `--conhost` to compare pyte with a real conhost frame by frame. Zero
   differences means Claude wrote the copy; a difference is ours to fix in
   pyte or `TerminalView`.
4. Look at the bytes just before the offset: is it a cursor-up to row 0, a
   home (`ESC[H`) with a row erase per screen row, or plain output? That
   names the shape, or tells you it is a new one.
5. Reproduce it live with `tools/render_capture.py` (below) at the dump's
   size, then write the regression check from those bytes.

## After a Claude Code update

Run the capture tool for each scenario at the card sizes people use, then
replay each capture. Every run launches a real claude in a scratch folder and
costs a little usage.

```powershell
.venv\Scripts\python.exe tools\render_capture.py 30 155 ctrl-o.json --scenario ctrl-o
.venv\Scripts\python.exe tools\render_capture.py 30 155 rows.json --scenario rows
.venv\Scripts\python.exe tools\render_capture.py 30 155 menu.json --scenario menu
.venv\Scripts\python.exe tools\render_capture.py 30 155 cols.json --scenario cols
.venv\Scripts\python.exe tools\render_replay.py rows.json --conhost
```

On claude 2.1.316: `ctrl-o`, `rows` and `menu` replay with no sighting, and
`cols` shows one copy per wiggle. The `cols` result documents Claude's width
redraw, which AI Hive avoids. If it stops showing copies, Claude fixed that
redraw. The capture marks where AI Hive would write `REPAINT_DONE` after each
row wiggle, and the replay puts the marker there, so `menu` replays as AI
Hive draws it. Its marks also print the scrollback with the input box
stranded and healed.

Changes in a release that would break the take-back rules without an error:

- Frames. Claude hides the cursor (`ESC[?25l`) to start a frame and shows it
  (`ESC[?25h`) to end it, and the rules judge whole frames by those. A switch
  to synchronized output (`ESC[?2026h`) alone would leave every frame open.
- How a repaint climbs. `_drop_repaint_copies` only trusts a cursor-up that
  reaches row 0. A repaint that homes the cursor with `ESC[H` reads the same
  as clearing the screen to print new text, and is left alone on purpose.
- How a resize redraws. The capture above is the check: one row shorter must
  push nothing.
- The full-width row. pyte keeps a pending wrap (cursor past the last column)
  through a line feed, a cursor move up or down, and an erase, where conhost
  drops it. Claude 2.1.316 always sends a carriage return after a row that
  fills the width, so this never shows. If `--conhost` ever differs right
  after such a row, this is why.

## Emoji and other grapheme clusters

A different symptom from doubled text: after an emoji with VS16 (`⚔️`,
`🛡️`, `⚠️`), the rest of its row went missing and every row below it drew
shifted right, until Claude's next redraws stacked copies of the status line.
pyte measured each code point on its own and stopped drawing a text run at a
VS16 or ZWJ, so the emoji took one cell where Claude and conhost count two,
and whatever followed it in the run was dropped. `app/grapheme_cells.py`
replaces pyte's `draw` with conhost's cluster model; its docstring has the
rules and the two conhost quirks left out.

Claude itself hides the bug: it writes each VS16 emoji between absolute
column moves (a space, `ESC[nG` back, the emoji, `ESC[nG` past it). ConPTY
doesn't. Its repaint from its own buffer, which follows Claude's left-arrow
agent view and the other alternate-screen views, writes the emoji plainly
with the rest of the row and relative moves after it. To see it, capture
Claude drawing an Edit diff with an emoji line, then Ctrl+O twice, then the
left arrow and Escape, and replay with `--conhost`. Since 2026-11-10,
`--conhost` compares conhost with the card's own screen (grapheme handling
and the private-CSI strip included), not with stock pyte. Stock pyte differed
on 105 of 110 frames of such a capture at 64x330.

## Older evidence

The screen of every agent at its last close is saved raw in
`%APPDATA%\AIHive\AI Hive\screens\` (app/screen_snapshot.py). It records no
sizes, so infer the width from the input box's rule line and the height from
a full repaint's `ESC[nA` (the card is n + 1 rows). A dump is better evidence
whenever there is one.
