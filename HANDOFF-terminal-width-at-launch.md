# HANDOFF: terminal width at launch (the half-width restored conversation)

**Status:** fixed and verified (v0.16.2). This document exists so the next
agent can (a) understand *why* the fix is shaped the way it is before changing
it, (b) re-verify it in minutes rather than re-deriving it in hours, and
(c) know exactly what was deliberately left unfixed.

Everything marked **MEASURED** below was measured on the reporter's machine,
not reasoned about. Re-measure before disagreeing.

---

## 1. The report

> "if I re-open AI Hive and there is an older conversation that is loaded, if I
> scroll a bit up, the conversation width is basically half. half of the left
> side has the text it should, but the right half is empty. no matter how far
> up you scroll, this bug should not be present and the text area width of past
> conversations with that agent should always be full width, as you see below
> the problematic area."

With a screenshot: a card whose scrollback is wrapped at roughly a third of the
card's width, switching **mid-sentence** to full width, with the live tail
below it correct.

## 2. The one fact everything follows from

**A terminal's column count is not cosmetic and cannot be repaired after the
fact.** Claude Code word-wraps *its own* text to whatever the pseudo-console
reports and emits hard line breaks; a `--resume` launch dumps the entire past
conversation the instant it boots. So:

- Lines **pyte** wrapped can be repaired — that is what
  `TerminalCard._reproject_on_size` does: re-feed the raw pty stream at the new
  width, because the raw stream is the truth and the screen is only a
  projection of it.
- Lines **the child** wrapped cannot. Re-projecting a stream that already
  contains the child's own newlines reproduces those newlines exactly.

Exact reflow is impossible here even in principle: a real terminal reflows
using the autowrap flag on lines filled to the last column, and Claude's lines
are word-wrapped well short of the edge, so there is no wrap flag to key on.
Heuristic re-wrapping of SGR-styled TUI output was considered and rejected.

**Therefore the only fix is prevention**, and that is what landed.

## 3. Evidence the diagnosis is right (reproduce it yourself)

### 3a. The user's own saved screen snapshot

`%APPDATA%\AIHive\AI Hive\screens\*.vt` are raw VT tails
(`app/screen_snapshot.py`). Strip escapes and profile printable width per block
of lines. On `ddaa4b1b….vt` (194 KB, an entire session, under the 512 KB cap so
nothing was dropped):

```
lines     0-  399  max= 34
lines   400-  799  max= 34
lines   800- 1199  max= 73     <- the seam
lines  1200- 1599  max= 73
...                            (73 for the rest of the file)
```

Line 941 is where it flips, **mid-sentence**, on exactly the text the user's
screenshot shows at the transition ("class) and any hand-edited URL with
nothing ref-shaped before the -c-."). Lines 920-940 show Claude's input box
being redrawn at the narrow width, then the tail reprinted wide — i.e. the
child got a SIGWINCH, redrew its current frame only, and everything above it
kept the width it was written for.

Two scripts used for this live under the scratchpad, not the repo; they are ~30
lines each (strip `\x1b\[[0-9;?]*[A-Za-z]`, convert CUF to spaces, histogram
`len(line.rstrip())`). Trivial to rewrite.

### 3b. The launch timing (**MEASURED**)

`main()` does `window.show()` and then `window.autostart_active_workspace()`
with no turn of the event loop in between. A window restoring **maximized**
reports its **restore-down** geometry at that point:

```
right after show():            1249 x 662
after one processEvents():     1536 x 793
after the event loop settles:  1536 x 793
```

The reporter's `session.json` had exactly that shape:
`"window": {"w": 1249, "h": 662, "maximized": true}`.

### 3c. The full picture, on a look-alike session (**MEASURED**)

Four workspaces x two pty agents, sidebar 298, restore-down 1249x662,
maximized — the reporter's shape, with PowerShell agents instead of
`claude.exe`. Tracing every `PtyWorker.start`/`resize`:

**Before the fix**

```
SPAWN   62ms  A0  30x100     <- DEFAULT_COLS, not the card's width
SPAWN   94ms  A1  30x100
...     (all eight spawn at 100)
RESIZE 421ms  Agent 1  43x84 <- only the VISIBLE workspace is ever corrected
RESIZE 421ms  Agent 2  43x84

WS lot-watcher       active=True  [84, 84]
WS AI Hive           active=False [100, 100]   <- never corrected, ever
WS Testing Platform  active=False [100, 100]
WS One More Tile     active=False [100, 100]
```

**After the fix**

```
RESIZE 172ms  (all eight told 43x84)
SPAWN  172ms  Agent 1  43x84   <- every spawn already at the real width
...
WS lot-watcher       active=True  [84, 84]
WS AI Hive           active=False [84, 84]
WS Testing Platform  active=False [84, 84]
WS One More Tile     active=False [84, 84]
```

## 4. The three holes, and what closes each

All three are documented as an invariant in `CLAUDE.md` ("A pty child NEVER
paints at a width its card does not have"). Do not reopen any of them.

| # | Hole | Fix |
|---|---|---|
| 1 | `autostart_active_workspace()` runs before the window has its real geometry (§3b) | `MainWindow.settle_layout()` pumps the queue first |
| 2 | `PtyWorker` spawns at `DEFAULT_COLS` (100); `TerminalView` debounces its resize by 120 ms | `TerminalView.flush_resize()` applies the pending resize before any spawn |
| 3 | `QStackedLayout` lays out the CURRENT page only, so unopened workspaces are never sized — and the autostart starts *every* workspace's agents | `PageStack.layout_hidden_pages()` |

### Why `PageStack` is shaped that way

Qt lays out only what it considers **visible**. `page.setGeometry(rect)` on a
hidden page updates the page's own geometry and **does nothing to its
children** — measured: the cards stayed at 100 columns. `layout().activate()`
does not help either. The idiom that works is `WA_DontShowOnScreen` + `show()`
+ `hide()` inside one call: Qt treats the page as visible for layout purposes
while the platform never maps it, and because the show/hide pair happens
without returning to the event loop, nothing paints and the stack's own idea of
which page is current is untouched. The column count lands one layout pass
later (hence `settle_layout` pumps again afterwards).

It is debounced (120 ms) because the trigger is a window drag, and it is also
called when a page or an agent is added/removed off screen, since a retile
changes every sibling card's width.

## 5. The regression it introduced, and the better rule that replaced it

**Read this before touching `drop_restored_screen`.** The first cut of the fix
broke two existing checks, and the reason is instructive.

`TerminalCard._on_status` used to drop the restored screen snapshot when a
child started **while `_pending_replay` was still set** — a proxy for "this
card has never re-rendered at a settled size", which held only because the
autostart raced ahead of the 120 ms resize debounce. `settle_layout` settles
those sizes on purpose, which consumes `_pending_replay` first, so the proxy
stopped distinguishing a launch from a wake and every restored card kept last
night's screen under its resuming child.

The repair was not to restore the race but to **say what is meant**:

- `autostart_active_workspace()` now calls `card.drop_restored_screen()`
  explicitly for each agent it is about to start. It is the only party that
  knows a start is the launch restore.
- `_on_status` keeps the `_pending_replay` test as a backstop for a card whose
  child starts before any layout.
- `_on_status` also gained a case of its own: a start that **resumes** a
  conversation (`spec.resume`, still readable at STARTING — `start()` clears it
  just after the worker launches) reprints that whole conversation itself, so
  the snapshot underneath is a duplicate wrapped for *last* session's width.
  That is the same bug arriving by the one door the autostart does not cover:
  a stopped card the user wakes with a keystroke. A plain pty shell reprints
  nothing, so its conversation is kept and scrolls up as a real terminal's
  would (still asserted by "screens: waking a stopped card KEEPS the
  conversation on screen").

`_drop_restored_screen` was renamed to `drop_restored_screen` (it has an
external caller now) and made safe to call twice: `_unhook_restored()` guards
the one-shot teardown with a `_restored_hooked` flag, because PySide **warns
rather than raises** on a repeat disconnect, so the old `try/except
(RuntimeError, TypeError)` swallowed nothing and printed a wall of
`RuntimeWarning`.

## 6. Files changed

| File | Change |
|---|---|
| `app/widgets/main_window.py` | new `PageStack` (replaces the bare `QStackedWidget`); new `MainWindow.settle_layout()`; `autostart_active_workspace()` settles first and drops each restored screen explicitly; `layout_hidden_pages()` also fired on page/agent add + remove |
| `app/widgets/terminal_view.py` | new `TerminalView.flush_resize()` |
| `app/widgets/terminal_card.py` | `_drop_restored_screen` → `drop_restored_screen` (+ resume-wake case, + `_unhook_restored`/`_restored_hooked`) |
| `tests/smoke_test.py` | new `test_pty_width_at_launch` (6 checks); +1 check in `test_screen_snapshots` for the resume-wake case |
| `CLAUDE.md` | new width invariant; restored-screen invariant rewritten to describe the explicit call |
| `README.md`, `app/__init__.py` | check count 1753 → 1762; v0.16.1 → v0.16.2 |

## 7. How to verify (2 minutes)

```
.venv\Scripts\python.exe tests\smoke_test.py
```

Expect **1761 passed, 1 failed**. The one failure is
`schedule: the composer says exactly when it will fire` — a **pre-existing,
time-of-day flake** in the test's own expectation (it wants `"in 1:29"` and
the countdown reads `"in 1:30:00"` when the clock has not ticked past the
second yet). Verified to fail identically on clean `e5caef0` in a stash.

To confirm the fix is load-bearing rather than incidental, stash the three app
files (keeping the test) and run `test_pty_width_at_launch` alone: it fails 4
of its 6 checks with exactly the bug —

```
[FAIL] pty width: none was spawned at the placeholder default
       :: [('Agent 1', 100), ('A2', 100), ... all eight at 100]
[FAIL] pty width: ...and so is every workspace still off screen
       :: [('A1', 100, 100), ... six cards stuck at 100]
[FAIL] pty width: no child was ever told two different widths
       :: {...: [100, 40]}
[FAIL] pty width: opening a workspace does not re-wrap its terminals
       :: ([... 100], [... 40])
```

Note the test builds its session with **all eight agents running** at save
time; only agents that were RUNNING are autostarted on restore, so starting
seven of eight makes the first check fail for an uninteresting reason.

## 8. Deliberately NOT fixed

**A width change mid-conversation still leaves earlier output wrapped as it
was.** If the user resizes the window, closes a sibling agent, or solos a card
while an agent is talking, everything the child already printed keeps the old
wrapping; only the frame the child redraws on SIGWINCH comes back at the new
width. This is inherent to app-wrapped terminal output and is what every real
terminal emulator does — see §2 for why exact reflow is not available.

If someone decides to attack it anyway, the honest options are, in order of
increasing risk:

1. **Record the pty width alongside the stream.** `TerminalAgent` already
   stamps stream offsets for `PromptMark`/`ReplyMark`; a width mark would let a
   projection *know* which regions were written at which width instead of
   guessing. That is a prerequisite for anything below, and is useful on its
   own (e.g. to warn, or to trim).
2. **Re-wrap a known-width region** from W1 to W2 during projection. Knowing W1
   exactly makes the continuation test well-defined ("previous line + first
   word of this one would have exceeded W1") rather than a guess. The hard part
   is not the wrapping, it is that SGR runs span the joins and TUI art must be
   left alone.
3. Do not attempt heuristic reflow *without* (1). It was considered and
   rejected here precisely because guessing the old width makes every
   subsequent decision unfalsifiable.

## 9. Related context worth knowing

- `.scratch/reply-timestamp-accuracy/spec.md` was accidentally deleted during
  this work and **restored** by reconstructing it from this repo's own Claude
  transcripts under `~/.claude/projects/…-ai-hive/` (the `Write` that created
  it, plus the two later `Edit`s, cross-checked against a `Read` of the same
  file). It is worth a glance to confirm it reads as its author remembers.
  Incidentally: those transcripts are a usable recovery source for any
  untracked file an agent wrote.
- This work was carried out from the *Testing Platform* AI Hive workspace by
  mistake, so its two `log_activity` entries are on that board rather than this
  repo's. Nothing else about it is misplaced.
