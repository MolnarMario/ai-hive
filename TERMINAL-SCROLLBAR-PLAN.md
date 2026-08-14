# Terminal scrollbar with prompt markers

## Context

Every agent card is a terminal with no scrollbar. There is no visible way to see
how far back a conversation goes, no way to drag through it, and no way to jump
to the moment a particular instruction was given. The ask: a slim scrollbar on
every agent window, with dot markers where the user submitted a prompt, so those
submissions act as milestones you can jump between. A cleared conversation
(`/clear`) or a fresh terminal starts over with an empty bar.

**The finding that shapes the whole design.** Claude Code currently keeps its
scrollback *inside itself*, so AI Hive has nothing to scroll. Verified, not
assumed: real `.vt` snapshots from `%APPDATA%\AIHive\AI Hive\screens` replayed
through pyte at their true geometry produce `len(screen.history.top) == 0` for 4
of 5 sessions. Claude sends `?1049h` and never `?1049l`, then redraws frames in
place, so nothing scrolls off the top and `HISTORY_LINES = 2000` sits unused for
every Claude agent. A scrollbar wired to the existing `TerminalView.scroll_by()`
would be permanently empty on every Claude card.

The cause is a documented CLI setting, read out of `claude.exe`'s own schema:

> `tui: "default" | "fullscreen"` - Terminal UI renderer. "fullscreen" uses the
> flicker-free alt-screen renderer with virtualized scrollback (equivalent to
> `CLAUDE_CODE_NO_FLICKER=1`). "default" uses the classic main-screen renderer.

`tui` is unset by default (the binary's upsell tests `tui === undefined`) and
there is a `/tui` slash command. **Decision: AI Hive takes the scrollback back**
by launching Claude agents with `tui: "default"`, so the conversation scrolls
into AI Hive's own pyte `HistoryScreen`. That buys a real proportional thumb,
exact marker positions, click-to-jump, and selection across history. Cost: the
classic renderer is not flicker-free, and Claude's internal virtual scroll is
replaced by ours.

Markers are **the user's own typed prompts only**. The bar is a **~10px overlay
on the terminal's right edge** (no stolen terminal columns).

---

## Step 0 - live spike (gate; do this before writing feature code)

The whole feature rests on the classic renderer actually filling
`history.top` **as `TerminalView.feed()` sees it** (private-CSI stripping,
partial-escape carry and DECSET tracking are part of the answer, so do not test
with a bare pyte screen).

Scratch cwd only - CLAUDE.md forbids test Claude sessions in the user's real
project folders. Use `%TEMP%\aihive-tui-spike\`, script in the session
scratchpad, not the repo.

Harness (~80 lines, `QT_QPA_PLATFORM=offscreen`): write `{"tui": "default"}` to
a scratch `settings.json`; `env = pty_worker.agent_environment()` (the real
function, so the `CLAUDE_CODE_*` strip is identical to production); spawn
`claude.exe --settings <path> --session-id <uuid>` at 120x40; pump reads into a
real `TerminalView.feed()`; after each chunk record `len(history.top)`,
`history.top.pushed`, `_alt_screen`, `_mouse_tracking`, and total chars fed.
Drive it the way AI Hive does: wait for a `terminal_agent._CLAUDE_READY_HINTS`
member, `write(prompt)`, wait 350ms (the `_write_task_to_pty` delayed-Enter
rule), `write("\r")`. Two prompts, then `/clear`. Control run with
`{"tui": "fullscreen"}` should reproduce `pushed == 0`.

Gates:

| # | Assert | If it fails |
|---|---|---|
| S1 | `pushed` grows with conversation length | feature dead, go to fallback |
| S2 | `_alt_screen` False after boot | same |
| S3 | history text holds both prompts and both replies, in order | markers cannot anchor, fallback |
| S4 | a `_CLAUDE_READY_HINTS` member still appears in the footer | **blocker.** `prompt_ready` gates task delivery and the BootVeil; extend `_CLAUDE_READY_HINTS` before shipping |
| S5 | record `_mouse_tracking`, and separately whether anything Claude draws in classic mode actually wants the wheel (open a permission menu and a plan prompt, try to scroll them) | decides Phase 6 and bounds its one real cost |
| S6 | `/clear` emits `\x1b[3J` or nothing | decides primary vs secondary reset |
| S7 | line delta between `pushed_at_enter + input_row` and where the prompt actually landed in history | `<= 3`: ship the simple anchor. `> 3`: add the confirm-scan (Phase 4d) |
| S8 | RSS and `len(top)` after ~10 turns at 120 cols | decides whether `HISTORY_LINES` is raised |
| S9 | how many times the same frame text repeats per turn, both renderers, **and how many lines a single turn pushes into history** | feeds the `recheck_limit` risk and the churn risk below |
| S10 | **how it looks.** Run one agent under each renderer side by side in the real app and watch a streaming reply | judgement gate, decided by eye, not by a number |

**S10 is not optional and is not a formality.** The renderer AI Hive is
switching to is, by the CLI's own description, the one that is *not*
flicker-free. That cost is paid on every Claude card, every day, in exchange for
a scrollbar - and it is the single most likely reason to want this feature
reverted later. Every other gate measures machinery; this one measures whether
the trade is worth making. Decide it before the toggle defaults ON, and if the
flicker is bad, ship with the toggle defaulting OFF (the bar still works fully
on pty shell agents, which is fallback F2 by another route).

Fallbacks: **F1** retry with `env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"]="1"`
(mechanically works - `PtyWorker.start` layers `spec.env` on top of
`agent_environment()` after the strip - but it re-adds a `CLAUDE_CODE_*` var the
sanitization invariant strips, so it needs an explicit allowlist comment naming
it and why it does not trip nested-session detection, which keys on
`CLAUDECODE`). **F2** ship degraded: Phases 1-4 are renderer-independent, so the
bar works fully on pty shell agents and stays auto-hidden on Claude cards.
There is no F3 - an alt-screen app's transcript cannot be mirrored into a linear
buffer.

---

## Phase 1 - `TerminalView`: coordinates and signals

`app/widgets/terminal_view.py`.

**The one identity** (put it in the module docstring):

```
abs_line(visual row r) == history.top.pushed - scroll_offset + r
```

Both branches of `_visible_line` (`:528-536`) reduce to it: live rows are
`pushed + r`; a history row at `idx = len(hist) - off + r` is
`pushed - len(hist) + idx`, the same expression. No special cases.

`.pushed` surviving a history clear is a **feature**, not a bug to fix: pyte's
`_reset_history()` calls `.clear()` on the same deque (verified in
`pyte/screens.py`), so after a clear the coordinate stays monotonic and correct
for lines pushed afterwards. Comment `_CountingDeque` (`:189-204`) accordingly -
"resetting `pushed` on clear" is the obvious wrong fix.

Add:

- `history_pushed()`, `abs_line_at_row(r)`, `history_span() -> (oldest, newest)`
- `anchor_line()` - the input box's `'>'` row from `_input_block_span()`
  (`:1060`) when there is one, else the caret row
- `scroll_to_abs(abs_line, lead=2)` - put the line `lead` rows below the top of
  the view (the lead absorbs the S7 slop)
- `viewChanged = Signal()` - offset, history length or page size changed.
  TRANSIENT, view-only, **never** wired to a save (the `activity_changed` rule).
  Coalesced behind a 50ms single-shot `QTimer` with an `immediate` bypass, and
  guarded by a `_view_sig = (pushed, len(top), offset, rows)` no-op check
  because `feed()` runs several times a second per agent. Fire from:
  `scroll_by` (inside the `new != offset` branch), `_snap_to_bottom` (inside the
  non-zero guard - this covers all 7 call sites at `:803, :1178, :1190, :1440,
  :1472, :1539, :1593` in one place), `scroll_to_abs`, the `feed()` anchor block
  (`:466-485`), the `?1049h` offset-zeroing branch (`:456`), `_apply_resize`,
  `reset()`, `set_marks()`.
- `historyCleared = Signal()` - in `feed()`, reuse the already-captured
  `hist_before` and compare `len(top_after) < hist_before`. History only grows
  toward `maxlen`, so a shrink can only be `_reset_history` (ED 3 or
  `screen.reset()`). On it: zero `_scroll_offset`, clear `_marks`, emit.
  Also a public `note_history_cleared()` for the two card paths that call
  `self.terminal.screen.reset()` directly and bypass `feed()`
  (`terminal_card.py:743, :768`).
- `promptSubmitted = Signal(str, int)` - text, absolute anchor line.
- `_marks: list[tuple[int, str]]` with `set_marks()` / `marks()` (pure view
  data; the durable copy is on the agent).

**Capture point**: the bare-Enter branch already in `keyPressEvent`
(`:807-809`), which is already trusted for exactly this meaning (it calls
`_reset_undo()`, "called on submit (bare Enter)"). Add `_note_prompt_submit()`
beside `_reset_undo()`: take `self._input_text()` (`:1486`), bail on empty, bail
if it matches `terminal_agent._NUM_OPTION_RE` (reuse it - `_INPUT_PROMPTS`
includes `❯`, the selection caret Claude paints on a highlighted menu row, so
`_input_text()` on a live menu returns `"1. Yes"`), else emit.

Record in the code comment why every alternative is rejected: `agent.write()` /
`_on_key_input` see a `\r` inside a bracketed paste (`_paste_text:1592`);
`_last_input_ts` is any keystroke; a `UserPromptSubmit` hook is forbidden by the
`startup`-exclusion invariant. This point also excludes, for free, everything
the user asked to exclude - `deliver_task`, `nudge` (auto-continue's
"Continue") and scheduled sends all reach `worker.write` and never touch
`keyPressEvent`.

**Cache `_view_state()`** (`:519-526`) behind `(pushed, len(top))`. It copies up
to 2000 items on *every* repaint while scrolled back, which is exactly the
"parked in scrollback reading" case this feature creates. Drop the cache in
`reset()` and on `historyCleared`.

---

## Phase 2 - the scrollbar widget

New module `app/widgets/terminal_scrollbar.py` (not `ornaments.py` - already
49KB of general chrome and this is coupled to `TerminalView`'s coordinates; not
`terminal_view.py` - already 93KB).

**Subclass `QScrollBar`, do not hand-paint.** `app/ui_theme.py:747-764` already
styles `QScrollBar:vertical` at exactly 10px wide, `p.BORDER` handle,
`p.SCROLL_HOVER` hover, radius 5, transparent groove, zero-size add/sub lines -
the requested look for free, following every skin through the existing
`apply_theme` -> `build_qss` -> `repolish` path with no new token. Drag,
click-to-page, min handle height and hover states come free.

```python
class TerminalScrollBar(QScrollBar):
    WIDTH = 10
    markActivated = Signal(int)   # absolute line
```

- Value space is absolute lines: `0` = oldest line held, `maximum` = live.
- `refresh()`: `hist = len(view.screen.history.top)`; `hist == 0` -> `hide()`
  and return (**auto-hide**: nothing to scroll means the terminal keeps every
  pixel and every mouse event). Else `setRange(0, hist)`,
  `setPageStep(view.screen.lines)`, `setValue(hist - view.scroll_offset())`
  under `blockSignals`, `show()`.
- `valueChanged` -> `view.scroll_by(...)`, behind a `_syncing` flag so
  `refresh()` cannot loop.
- `wheelEvent` forwards to `self._view.wheelEvent(e)` - the three wheel regimes
  stay decided in exactly one place.
- `paintEvent`: `super().paintEvent(e)` first, then markers *on top* of the
  handle (the one under the thumb is the one you are on). `Palette` read at
  paint time (the `BootVeil` rule), `ACCENT_ORANGE` at ~200 alpha, ~4x3px, inset
  3px. Skip marks outside `history_span()`.
- `mousePressEvent`: a marker within 5px -> `markActivated.emit(abs)` and
  `accept()` (no fall-through to page-step); otherwise `super()`.
- `ToolTip` event -> nearest marker within 5px, text via `terminal_card._snippet`.
- `NoFocus` + `ArrowCursor` (the view is I-beam) so it never steals focus.

**Replaces the painted "▲ N" badge** at `terminal_view.py:1843-1853`, which sits
at `width() - tw - 20` - exactly where the bar goes. The bar's thumb position
carries the same information better.

---

## Phase 3 - the marker model

**Marks live on `TerminalAgent`** ("model owns state, widgets only subscribe") -
a `TerminalView` is re-created on every card rebuild, so view-local storage
would lose them on any retile.

`app/terminal_agent.py`:

```python
@dataclass
class PromptMark:
    uid: int    # monotonic per agent; the ONLY safe key for a per-card map
    pos: int    # cumulative CHARACTER offset into this agent's pty stream
    text: str
    ts: float
```

`uid` exists because the card maps marks to view lines and **`id(mark)` cannot
be that key**. Marks are FIFO-capped at 200, and CPython reuses an object's
`id()` once it is collected, so a dropped mark can hand its address to a newly
allocated one, which then silently inherits the dropped marker's line position -
a dot pointing at the wrong place, with nothing to notice it by. `pos` is not
safe either (two submits with no output between them share an offset). A plain
monotonic counter on the agent, never reused within its life, is.

New fields beside `_pty_buffer`: `_pty_total` (every char ever emitted, never
trimmed), `_pty_dropped` (chars evicted off the front of the 512KiB buffer),
`_prompt_marks` capped at `PROMPT_MARK_CAP = 200` FIFO, and
`prompt_marks_changed = Signal()` - TRANSIENT, never `dirty`, **never
persisted** (the transcript is the durable record; a persisted marker list goes
stale exactly the way a persisted limit latch does).

Methods: `note_prompt_submitted(text)` (stamps `pos = _pty_total` at the moment
of the Enter, i.e. immediately before the child's response), `prompt_marks()`,
`clear_prompt_marks()` (guarded on non-empty so it stays silent),
`replay_marks()` -> `[(m.pos - _pty_dropped, m) for m in marks if m.pos >=
_pty_dropped]` - offsets *within* `pty_replay()`, dropping marks whose bytes
aged out (their content is gone from the replay too). `_on_pty_output` maintains
`_pty_total`/`_pty_dropped`; `restart()` zeroes both and clears marks;
`seed_pty_replay` sets `_pty_total = len(text)`.

**Card wiring** (`terminal_card.py`, pty branch):

```python
self.terminal.promptSubmitted.connect(self._on_prompt_submitted)
self.agent.prompt_marks_changed.connect(self._refresh_marks)
self.terminal.historyCleared.connect(self._on_history_cleared)
self.terminal.viewChanged.connect(self.scroll_bar.refresh)
self.scroll_bar.markActivated.connect(self.terminal.scroll_to_abs)
```

`_on_prompt_submitted` returns early on `self.agent.is_waiting()` - a menu or
question Enter is a selection, not a typed prompt. That reuses machinery that is
already authoritative (`_tool_waiting` is hook-driven and fires *before*
`AskUserQuestion`/`ExitPlanMode` renders; `_screen_waiting` catches classic
numbered menus) and is the second half of the `_NUM_OPTION_RE` guard.
`self._mark_lines: dict[mark.uid -> abs_line]` is per-card, because a rebuilt
view has a different `pushed` origin. Key on `uid`, never `id(mark)` - see the
dataclass note above.

**Re-anchoring across a card rebuild** - the one non-obvious part. A rebuilt
card re-feeds `agent.pty_replay()` into a fresh view, so `pushed` restarts at 0
and no stored absolute line is portable. Feed the replay **in segments split at
the marks' stream offsets** and re-derive each anchor with the same function
used live:

```python
def _replay_with_marks(self, replay: str) -> None:
    pos = 0
    self._mark_lines.clear()
    for off, mark in self.agent.replay_marks():
        off = max(0, min(len(replay), off))
        if off > pos:
            self.terminal.feed(replay[pos:off]); pos = off
        self._mark_lines[mark.uid] = self.terminal.anchor_line()
    if pos < len(replay):
        self.terminal.feed(replay[pos:])
    self._refresh_marks()
```

Splitting mid-escape is safe - `feed()` carries a trailing partial escape across
calls (`_esc_carry`). And note what `mark.pos` already includes: the typed
characters were echoed *as the user typed them*, before Enter, so at `pos` the
prompt text is already painted in the input box. A reviewer reading this will be
tempted to "fix" the split by extending the segment past the submit echo - do
not. That would break the capture/replay symmetry below and re-anchor to a
different row than the live path used. The genuine slop is that Claude
*reprints* the submitted prompt as committed output afterwards, at a row that
may differ from the live input box; that is what S7 measures and `lead` absorbs. Call it from the two replay sites: `TerminalCard.__init__`
(`:149-151`) and `_rerender_restored` (`:768-769`, after
`note_history_cleared()` + `screen.reset()`). The symmetry is what makes it
exact: `anchor_line()` is one function, run over identical screen state at
capture and at replay.

**Accuracy limits** (document in the module docstring): the anchor is the input
box row at submit time and the renderer then reprints the prompt as permanent
output - `lead=2` absorbs a couple of lines. Only if S7 exceeds 3 lines, add a
single-slot pending confirm-scan over *newly pushed lines only*, bounded to
`rows*2` lines, falling back to the approximate anchor. A shrinking resize loses
live rows through pyte's `delete_lines` without advancing `pushed`, so a mark
still on the live screen can drift by the shrink amount - bounded by screen
height, self-correcting once the line enters history, not worth code.

---

## Phase 4 - reset semantics

| Event | Detection | Action |
|---|---|---|
| `/clear` or `/resume` to another conversation | **primary**: the `SessionStart` hook (`resume\|clear\|compact`) reports a new session id and `WorkspaceManager.sync_live_sessions` applies the pin change | call `a.clear_prompt_marks()` at both places a pin is actually applied (hook branch and mtime branch). Transient; does not touch the existing `dirty` emit |
| `/clear` drawing `ED 3` | **secondary**: history shrink inside `feed()` | `historyCleared` -> view drops `_marks`, card clears `_mark_lines` and the agent's marks |
| agent restart | `TerminalAgent.restart()` | zero `_pty_total`/`_pty_dropped`, clear marks |
| `RIS` (`\x1bc`) | history shrink | same as ED 3 |
| new terminal / new card | fresh view (`pushed == 0`) and fresh agent | bar auto-hidden until history exists |
| restored (seeded) screen | marks are empty on a fresh run | scrollback shows, no markers - correct, AI Hive did not observe those submits |

Both are used: the pin change is primary because it is authoritative and
renderer-independent, the ED 3 shrink fires within one frame instead of within
`SESSION_SYNC_MS`.

---

## Phase 5 - card layout

`terminal_card.py`, pty branch. Creation order gives the right z-order with no
extra `raise_()`:

```python
self.terminal = TerminalView(...)      # in the QVBoxLayout, stretch 1
root.addWidget(self.terminal, 1)
self.scroll_bar = TerminalScrollBar(self.terminal, self.terminal)   # bottom
self.overlay = QLabel(self.terminal)                                # wake banner
self.boot = BootVeil(self.terminal)                                 # top
```

`_place_overlay` (`:833-845`) gains, before the existing overlay work:
`self.scroll_bar.setGeometry(self.terminal.width() - WIDTH, 0, WIDTH,
self.terminal.height())`. It is already driven per-resize by the event filter at
`:712-714`; do not add anything that reads `screen_text()` there
(the `_refresh_overlay` rule).

Because the bar is a **child of the terminal**, `_apply_resize` still computes
`cols` from `self.terminal.width()` - no terminal columns are stolen, which was
the requirement. The cost is the rightmost ~1.5 columns rendering under a mostly
transparent 10px strip, mitigated by auto-hide.

---

## Phase 6 - `tui: "default"` injection and the wheel regime

**Injection via the shared `--settings` file, not an env var.** `spec.env` would
mean re-adding a `CLAUDE_CODE_*` variable that `pty_worker.agent_environment()`
deliberately strips - an invariant with a live data-loss story behind it. The
settings file is already written, already armed onto every Claude agent
(`MainWindow._arm_agent_mcp`), already re-armed on restore, already never
persisted; and `tui` is documented and schema-validated where the env vars are
undocumented internals.

- `session_hook.write_settings_file(..., tui: str = "")` -> payload becomes
  `{"hooks": hooks}` plus `"tui"` when non-empty. **The `SessionStart` matcher
  stays `"resume|clear|compact"`** - comment above the `tui` line saying it is a
  renderer setting with nothing to do with hooks, so nobody tidies them
  together. Update the "carrying ONLY our hooks" docstring.
- `MainWindow`: extract the `:1155-1168` block into `_write_hook_settings()`
  which passes `tui = "default" if self._terminal_scrollback else ""`, keeping
  the existing `HOOK-SETUP-FAIL` audit and the `_hook_settings_path = ""`
  degrade. Seed `self._terminal_scrollback` from the raw session dict *before*
  that call, restore it in `_restore_ui_state`, serialize it in
  `to_session_dict`'s `data["ui"]`. Additive optional key under `"ui"` -
  **no `SESSION_VERSION` bump**, same as `usage_visible` / `taskbar_badge`.
  Toggling calls `_write_hook_settings()` then `_schedule_save()`, never
  `_touch`.
- **Toggle**: a checkable item in `TopBar.contextMenuEvent`, not another button -
  that method's own docstring establishes the rule for set-once preferences, and
  the bar already carries the chime, taskbar badge, two recovery switches, a
  theme picker and two font buttons. Copy (no em dash): *"Claude agents run the
  classic renderer so their conversation scrolls into AI Hive's own scrollbar.
  Applies to agents started from now on."*
- **Running agents keep the renderer they launched with.** Do not auto-restart
  anything - restarting a working agent for a cosmetic setting is the class of
  surprise the `nudge`-vs-`deliver_task` rules exist to prevent. The per-card
  right-click Restart is already there.

**Wheel regime.** Precedence today is mouse-tracking > altscreen > local history
(`:842-869`). Under the classic renderer Claude will very likely still enable
mouse tracking for its clickable menus while *not* being in the alternate
screen, and the wheel would keep forwarding - the new bar would drag but the
wheel would not drive it, which reads as broken. Change the first test to
`self._mouse_tracking and self._alt_screen`: a terminal owns the scrollback for
an *inline* application, and only a full-screen app that also wants the mouse
gets the wheel (xterm / Windows Terminal behaviour).

Risk: **menu clicking is unaffected** - `mousePressEvent`/`mouseReleaseEvent`
key on `_mouse_tracking` alone and are untouched; only the wheel changes. The
existing `test_scrollback` still passes (its mouse-tracking assertions run
between the `?1049h` at line 5033 and the `?1049l` at 5059, so `_alt_screen` is
True there). An inline non-Claude TUI that wants wheel reports would lose them;
`Shift+PageUp/PageDown` (`:642-651`) still pages regardless.

---

## Phase 7 - performance

1. `_view_state()` signature cache (Phase 1) - the single biggest win, and it
   helps today before any renderer change.
2. `viewChanged` coalescing (50ms single-shot) plus the `_view_sig` no-op guard,
   so a streaming agent updates the bar ~20x/s rather than per feed. The bar's
   `refresh()` is O(marks) and never walks history.
3. **`HISTORY_LINES` stops being dead weight and becomes a user-visible
   ceiling.** Today the constant is unreachable for Claude agents (history is
   always empty); the moment AI Hive owns the scrollback it is the line at which
   the oldest conversation - and the oldest milestone dots with it - falls off
   the top and cannot be scrolled back to. A busy agent passes 2000 lines in
   well under an hour, so at the current value a long session's early prompts
   quietly stop being reachable. That is the cost side, and it is the one a user
   actually notices.
   The other side: pyte stores each history line as a sparse dict of `Char`
   namedtuples, roughly `cols` entries per dense line, and a hive routinely runs
   six agents. So **measure, do not raise blind.** Gate on S8: raise to
   4000-5000 only if the measured per-agent cost lands under ~15MB, otherwise
   keep 2000. Either way record the measurement AND the reachability
   consequence in a comment beside the constant, so the next person sees it is a
   deliberate trade and not an untouched default.
   **Second pressure on the same constant, from the opposite side:** in
   main-screen mode the spinner, token counter and tool boxes repaint into the
   primary buffer, so a single turn can push near-duplicate intermediate frames
   into history and burn the 2000 lines far faster than the conversation itself
   would. S9 measures lines-pushed-per-turn for exactly this.
   **Do NOT respond to churn by filtering duplicate lines out of the history
   push.** Every coordinate here rests on `abs = pushed - offset + row`, and the
   live-row half (`pushed + r`) holds only while *every* line that leaves the
   screen is counted. Suppress one append and the live buffer shifts up while
   `pushed` does not, so every mark anchored to a live row drifts by one,
   permanently, per suppressed line. If churn is bad the honest options are
   raising the constant or living with it.
4. Marker cap 200/agent FIFO; the bar only paints marks inside `history_span()`.

---

## Phase 8 - risks

1. **The spike says no** (Claude keeps the alt screen despite the setting).
   Mitigated by F1/F2.
1b. **The classic renderer flickers enough to be worse than the problem.** The
   only risk here whose cost is paid continuously rather than once, and the
   likeliest reason to revert the whole feature months later. Gated by S10,
   decided by eye; if it loses, the toggle ships defaulting OFF and the bar
   still serves every pty shell agent.
2. **`prompt_ready` regresses** under the classic renderer (S4). Highest
   consequence in the plan: it silently breaks first-task-submit and the
   BootVeil, the exact class of bug CLAUDE.md records as costing hours to
   isolate. Explicit spike gate, not a later discovery.
3. **`recheck_limit` degrades** - it deliberately reads a *raw* 40-line tail
   because the fullscreen renderer keeps redrawing the limit menu, so earlier
   renders linger. With fewer full-frame redraws the menu ages out of the
   4000-char `_screen_tail` sooner and it could report "resumed" prematurely.
   Measure with S9; re-read `LIMIT-DETECTION-FINDINGS.md` and re-tune before the
   toggle defaults ON.
4. **Anchor slop** beyond `lead` (S7) - handled by the bounded confirm-scan,
   built only if measured necessary.
5. **`--settings` is CLI-argument precedence**, so it overrides a user's own
   `tui` and any `/tui fullscreen` they persist. The toggle is the escape hatch;
   the tooltip says so.
6. **An older CLI could reject an unknown settings key** - verified present on
   the installed build; watch `TerminalAgent._on_finished`'s hard-exit path
   during the spike.
7. **Milestones age out at `HISTORY_LINES`** (Phase 7.3). Not a defect, but the
   feature's real limit: markers are only reachable while their lines are still
   in history, so "jump to any point in the conversation" is honestly "jump to
   any point in the last N lines". Say so in the README rather than letting a
   user discover their early dots vanished.

## Ordering

Phases 1-3, 5 and 7.1-7.2 are renderer-independent and land full value on pty
shell agents even under fallback F2. Phase 6 is the only part that changes how
agents launch and comes last-but-one on purpose, so an ambiguous spike does not
block the rest. Phase 4's pin-change half depends on Phase 6 being in use.

---

## Verification

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

All checks must pass; update README.md's Verify count. New checks in
`tests/smoke_test.py` (headless offscreen QPA, isolated tmp `SessionStore`
only):

1. `test_terminal_view_signal` - `viewChanged` fires on `scroll_by`,
   `_snap_to_bottom`, `_apply_resize`, `reset` and a history-growing `feed`;
   does **not** fire on a clamped no-op `scroll_by(-1)` at offset 0 or a feed
   that pushes no lines. Drive the coalescer by invoking the timer slot directly.
2. `test_terminal_abs_line_identity` - after 80 fed lines `abs_line_at_row(r)`
   identifies the same text live and scrolled back; `scroll_to_abs(a, lead=0)`
   puts line `a` on row 0; `history_span()` brackets every valid id.
3. `test_terminal_prompt_submit_signal` - bare Enter on a synthetic `"> hello
   world"` input box emits `promptSubmitted`; Ctrl/Shift/Alt+Enter, an empty
   input, and a menu row (`"❯ 1. Yes"`) emit nothing.
4. `test_prompt_marks_bypass_task_delivery` - `agent.write("\r")`,
   `deliver_task(...)` and `nudge("Continue")` produce **zero** marks. This is
   the "markers are the user's own prompts" contract.
5. `test_prompt_mark_replay_reanchor` - two marks at known char offsets survive
   a card rebuild and land on the lines holding their prompt text; a mark older
   than `_pty_dropped` is dropped, not mis-anchored.
5b. `test_prompt_mark_uid_never_reused` - push past `PROMPT_MARK_CAP` so the
   FIFO evicts, and assert every surviving mark's `uid` is distinct from every
   evicted one and that `_mark_lines` holds no entry for an evicted mark. This
   is the regression check for keying on `id()`, which CPython reuses after GC
   and which would silently give a new marker a dropped one's position.
6. `test_terminal_history_cleared` - `\x1b[3J` fires `historyCleared`, empties
   history, zeroes the offset, drops marks, leaves `pushed` unchanged, and
   `abs_line_at_row` stays self-consistent for lines pushed afterwards.
7. `test_prompt_marks_reset_on_session_change` - a pin change through
   `sync_live_sessions` clears marks; `restart()` clears marks and both counters.
8. `test_terminal_scrollbar_overlay` - the bar is a child of `card.terminal`,
   `NoFocus`, hidden at zero history, visible after history exists, pinned to
   `width() - WIDTH` and full height after a resize, and `screen.columns` is
   identical with and without it (no stolen columns).
9. `test_terminal_scrollbar_markers_paint` - render to a `QPixmap` (as
   `test_terminal_link_underline` does) and assert accent pixels at the two
   expected Y bands and none between.
10. `test_terminal_scrollbar_click_jumps` - a press within 5px of a marker emits
    `markActivated` and moves the offset so the line sits at `lead`; a press
    away from any marker pages by `screen.lines`.
11. `test_wheel_regime_inline_mouse_tracking` - tracking ON + altscreen OFF
    scrolls local history and sends nothing; both ON still forwards SGR reports;
    altscreen alone still sends arrows. `test_scrollback` must pass unchanged.
12. `test_claude_tui_setting` - `tui="default"` yields `{"hooks":…, "tui":
    "default"}`, `tui=""` yields no `tui` key, and in both cases
    `hooks["SessionStart"][0]["matcher"] == "resume|clear|compact"` (the
    `startup`-exclusion invariant asserted where a future edit will see it).
13. `test_terminal_scrollback_preference` - round-trips through
    `session["ui"]["terminal_scrollback"]`, `SESSION_VERSION` unchanged,
    `_restore_ui_state` applies it, toggling rewrites the settings file
    with/without the key and never marks the session dirty beyond
    `_schedule_save`.
14. `test_scrollbar_follows_theme` - after `apply_theme(<other skin>)` a
    re-render uses the new `Palette` values.

`test_no_em_dashes_in_visible_text` covers the new module automatically.

**Manual end-to-end after the suite**: launch AI Hive, start a Claude agent,
send three prompts, confirm the bar appears with three dots, drag it, click the
first dot and confirm the view lands on that prompt, `/clear` and confirm the
bar and dots vanish, then close and reopen and confirm the restored card shows
scrollback with the bar in the right place.

## Documentation

- `terminal_view.py` docstring: two wheel regimes becomes three with the AND
  condition; add the `abs = pushed - off + r` identity and the capture point.
- `terminal_card.py` docstring: the replay is now segmented.
- `session_hook.write_settings_file` docstring: no longer "ONLY our hooks".
- `CLAUDE.md`: a new invariant covering (a) markers come from the bare-Enter
  branch and nothing else, with each rejected source and why; (b) `.pushed` is
  the absolute coordinate and is deliberately not reset by a history clear;
  (c) the `tui` key is a renderer setting living in the hook settings file and
  must never be entangled with the `startup`-exclusion matcher.
- `README.md`: feature list and the Verify check count.
