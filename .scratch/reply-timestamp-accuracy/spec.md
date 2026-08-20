# Reply timestamp still reads wrong to the user

Status: ready-for-human

## The ask, verbatim

> each timestamp should be under each final reply of a promp, just like you
> have "✻ Crunched for 58s". you should also have a date and time when you
> finished "crunching/working/cooking/sauteeing/etc"

Then, after two rounds of fixes (below), the user tested again and said:

> so where is the date and timestamp? [screenshot of the card header] the one
> on the top next to the usage % is not useful because if i open the app, i
> only get to see the current time instead of when the answer was actually
> generated.

Then, after a third fix, the user tested again with a fresh `/clear`d
conversation and said:

> no, it's still broken [screenshot] you are dumb.

The prior agent (me) could not reproduce a discrepancy in that last
screenshot's own numbers (see "What the last screenshot actually shows"
below) and the user is out of patience. Handing this to a fresh pair of eyes
rather than guessing a fourth time.

## What exists today (three commits, in order)

1. **`c8bce9d`** — `#CardReplyTime` header badge (added in an earlier,
   already-merged commit `bc190a9`, before this thread) showed `HH:MM` only,
   never a date. Fixed to show `Aug 18, 14:32`-style once the reply wasn't
   today. `TerminalCard._refresh_reply_time` (`app/widgets/terminal_card.py:1305`),
   shared formatter `_format_reply_stamp` near the top of the same file.

2. **`623b177`** — the header badge only ever shows the LATEST reply. User
   wanted a stamp under EVERY finished turn, inline in the terminal itself,
   like Claude's own "Crunched for Ns" footer. Added a second milestone type,
   `ReplyMark` (`app/terminal_agent.py`, mirrors the existing `PromptMark`
   scrollbar-milestone system), anchored via new
   `TerminalView.reply_anchor_line()` (`app/widgets/terminal_view.py`), which
   scans up from the freshly-redrawn input box to find the reply's own footer
   row and paints a stamp into its blank right-hand tail in `paintEvent`.

3. **`cb4cc4f`** — found and fixed a real, verified bug: reopening AI Hive
   resumes the conversation via `--resume`, which replays the WHOLE past
   conversation as real terminal output before the screen ever goes quiet.
   That replay settling was indistinguishable from a fresh reply ending, so
   `_on_idle_timeout` stamped `_last_reply_ts` / minted a `ReplyMark` with
   `time.time()` — i.e. "now, at launch time" — for what was actually old
   history. Fixed with `TerminalAgent._settled_once`
   (`app/terminal_agent.py:371`, checked in `_on_idle_timeout` at
   `app/terminal_agent.py:1397`): only the FIRST busy→idle settle of a
   *resumed* launch is suppressed; every settle after that stamps normally,
   and a non-resumed launch is never suppressed at all.

All three commits are on `main`, not pushed to origin. Full smoke suite green
at each step (1729 checks as of `cb4cc4f`; one pre-existing, unrelated,
timing-flaky failure — "schedule composer" — verified to also fail on a
clean checkout with none of these changes).

## What the last screenshot actually shows

Card header for "AGENT 3": `Opus 5 · high · a[uto?]   8% of 1M   17:07`,
directly under a fresh `/clear` → `say "hi"` → `hi` → `✻ Cogitated for 2s`
exchange.

At the moment I looked at this (immediately after the user's message), I
checked the actual OS clock and the AI Hive process's own start time:

```
current wall clock:        17:08:42
ai-hive main.py started:   17:06:48   (both the .venv and system-Python launches)
commit cb4cc4f timestamp:  17:00:13
```

So: the running process was launched 6+ minutes AFTER the resume-replay fix
landed (it has the fix), and the badge reads `17:07` — about a minute
BEHIND the current `17:08:42`, not equal to it. For a conversation that was
just `/clear`d (i.e. NOT resumed — `_resume_attempt` is false, so the
`cb4cc4f` fix doesn't even engage on this path), a reply that finished about
a minute ago reading `17:07` while it is now `17:08` is what CORRECT
behavior looks like: a real, frozen, slightly-stale stamp, not a live clock
that would read `17:08` if it just re-read `time.time()` on every repaint.

I could not find a code path that overwrites `reply_time_label`'s text on
anything OTHER than the `activity_changed` busy→idle edge
(`TerminalCard._on_activity` → `_refresh_reply_time`,
`app/widgets/terminal_card.py:1301`) — there is no timer, no paint-time
re-stamp, nothing that would make it silently track "now".

**I don't have a reproduction that contradicts the fix, going purely off this
screenshot's own numbers.** That doesn't mean nothing is wrong — see below.

## Hypotheses for what's still actually broken (untested by me)

Ranked by how likely I think they are, given I could not reproduce from the
screenshot alone:

1. **UX/legibility, not a logic bug.** A bare `HH:MM` sitting in a header
   next to other live-updating badges (usage %, which DOES refresh
   continuously) is visually indistinguishable from a live clock unless you
   watch it over several minutes or hover for the tooltip
   (`app/widgets/terminal_card.py:1305`, tooltip = full `YYYY-MM-DD
   HH:MM:SS`). The user may be reading "close to current time" as "IS
   current time" without actually watching it fail to advance. Worth asking
   directly: *does the badge visibly change if you watch a card for 5+
   minutes without it replying again?* If it does NOT change, the badge is
   working as designed and this is purely a legibility/trust problem (maybe
   needs a relative format — "2m ago" — or a small icon distinguishing it
   from a live reading, or splitting it more clearly from the usage-%
   cluster).

2. **A restart mid-session that isn't the classic `--resume` path.** There
   may be a SECOND way the app ends up replaying/redrawing a conversation
   that `_resume_attempt` doesn't cover — e.g. `TerminalCard._rerender_restored`
   / `_replay_with_marks` re-projecting the scrollback on a retile or width
   change (`app/widgets/terminal_card.py`, see the CLAUDE.md bullets "A width
   change RE-PROJECTS the scrollback" and "That projection happens ONCE, at
   the SETTLED width"). That re-projection re-feeds the pty replay buffer
   through a FRESH `TerminalView`/pyte screen, but it does NOT go through
   `TerminalAgent._on_idle_timeout` at all (it's a pure view-side replay, not
   a busy/idle cycle) — so in THEORY it can't touch `_last_reply_ts`. Worth
   double-checking there isn't some indirect trigger (e.g. `request_repaint()`
   from `_check_input_gap` — CLAUDE.md's "A dropdown that scrolls the classic
   renderer can strand the input box" bullet — sending a resize nudge to the
   live child that produces a burst of real output and a genuine, CORRECT new
   busy→idle edge, which could look like "the badge just updated for no
   reason" from the user's point of view even though it's technically right).

3. **The two duplicate `main.py` processes.** While diagnosing this I found
   TWO simultaneously-running `main.py` processes on the user's machine — one
   launched from `.venv\Scripts\pythonw.exe`, one from the system
   `C:\Program Files\Python311\pythonw.exe` — both started at the exact same
   second. Only ONE has a visible "AI Hive" window (confirmed via
   `EnumWindows`); the other is running with no window, i.e. **exactly the
   zombie scenario CLAUDE.md's single-instance-mutex invariant warns about**
   ("A closed window is a dead process... A zombie instance that outlives its
   window holds the single-instance mutex and — with `_closing` latched —
   can never save again"). I did NOT chase this further — it's off-topic from
   the timestamp bug on its face, but if the single-instance mutex is
   somehow not actually preventing two full launches (rather than one
   exiting cleanly the instant it loses the mutex race), that's a
   significant separate bug worth a look, and it's conceivable it's
   SOMEHOW involved (e.g. two processes both writing/reading the same
   session state, if that's what's confusing "which agent's timestamp is
   which"). Repro: `Get-CimInstance Win32_Process | Where-Object
   {$_.CommandLine -match 'main\.py'}` while the app is running.

4. **Simplest possible explanation I haven't ruled out:** the user is
   comparing the badge against a DIFFERENT reply than they think — e.g. they
   glance at the badge, see a time, and assume it's for the message
   currently on screen, when it's actually still showing the PREVIOUS
   agent's/PREVIOUS turn's stamp because the new turn hasn't settled yet
   (busy → idle needs `BUSY_IDLE_MS` = 2000ms of quiet after the LAST output
   burst before it updates). On a fast "hi"/"hi" exchange this window is
   short but not zero.

## Suggested next step

Before changing any more code: get the user to do ONE precise test and
report back literally what they see, because every round so far has been "I
tried it, still broken" with a screenshot that (to me) doesn't visibly
contradict the current implementation:

1. Open a workspace, note the OS clock time.
2. Send a message, wait for the reply to fully finish (footer like "Crunched
   for Ns" stops changing).
3. Note what the header badge (`#CardReplyTime`, next to the usage-% badge)
   says at that moment.
4. **Wait 3-5 minutes, doing nothing else in that card.**
5. Look at the badge again. Ask: did it change? If yes — genuine bug, and now
   there's a tight repro window (something is re-stamping without a real
   busy→idle edge). If no — the feature is working; the complaint is about
   legibility/trust, not correctness, and the fix is presentation (e.g. a
   relative "Xm ago" format, or visually separating it from the live usage-%
   reading so it doesn't read as "also live").

Also worth asking directly: is the complaint about the HEADER badge
(`#CardReplyTime`, one per card, latest reply only) or the INLINE per-message
stamps added in `623b177` (drawn above each individual reply's own footer
line, inside the terminal content)? The user's screenshots so far have only
ever shown the header badge — I have not seen confirmation either way that
the inline per-message stamps are rendering (or not) in their real,
non-resumed usage.

## Relevant files

- `app/terminal_agent.py` — `ReplyMark`, `note_reply_settled`,
  `reply_marks`/`clear_reply_marks`/`reply_replay_marks`, `_last_reply_ts`,
  `_settled_once`, `_on_idle_timeout` (~line 1397), `last_reply_at` (~line
  1166).
- `app/widgets/terminal_view.py` — `reply_anchor_line`, `set_reply_marks`/
  `reply_marks`, the inline-stamp paint block inside `paintEvent`.
- `app/widgets/terminal_card.py` — `_format_reply_stamp` (module-level,
  shared formatter), `reply_time_label`/`_refresh_reply_time` (header badge),
  `_refresh_reply_marks`/`_on_reply_mark_added`/`_reply_mark_lines` (inline
  marks), `_replay_with_marks` (re-anchors both mark types across a card
  rebuild in one merged pass).
- `CLAUDE.md` — three invariant bullets already document all of the above in
  detail: "A reply-finished stamp is a SEPARATE milestone type...", "The
  first busy->idle settle of a resumed launch is a REPLAY finishing, not a
  reply...", plus the pre-existing "reply-finished time stamp" line in
  README.md's Verify section.
- `tests/smoke_test.py` — `test_agent_last_reply_at`,
  `test_reply_settle_skips_resume_replay`, `test_reply_time_card_ui`,
  `test_reply_marks_inline` all pass against the current code.

## Comments

### 2026-08-19 — fixed by reading the timestamp off the transcript

The hypotheses above were all about the LIVE path. The actual defect was
structural: **a reply time was only ever a clock reading taken at a settle
this process watched.** That can serve exactly one case, and it is not the
case anyone complained about. Every user report was about reopening the app.

So `cb4cc4f` was correct and made the symptom worse: suppressing the
resume-replay stamp meant a restored conversation had NO live stamp for any
past turn, so `_refresh_reply_time` hit `ts is None` and **hid the badge**,
and zero inline marks existed. "Where is the date and timestamp?" answered
with nothing at all. The previous round diagnosed the header badge reading
`17:07` as correct — it was, but the user had ruled that surface out two
rounds earlier ("the one on the top next to the usage % is not useful").

Claude timestamps every record it writes, so the fix is to stop asking the
clock. New `transcripts.reply_times()` / `latest_reply_at()`.

**Two things were measured rather than assumed, and both changed the design:**

1. **Claude's "<verb> for Ns" footer does not persist per turn.** Rendered
   seven real captured `.vt` screens at five widths each: at most ONE footer
   survived in 2000 lines of history, usually none. So the row a LIVE
   `ReplyMark` anchors to is gone for every older turn, and footer-anchoring
   recovered marks was a dead end before it was written.
2. **Matching a reply's HEAD lands on a stale re-render.** pyte does not
   reflow, so a card resized mid-session keeps both copies of a reply in
   scrollback, the older truncated where the redraw overwrote it. Head
   matching found the stale one first and stamped four rows into it, i.e. the
   middle of a paragraph. `_reply_end_row` therefore matches the reply's
   TAIL and requires the row below to be blank — which also happens to be the
   only row a stamp reliably renders on, since `paintEvent` skips a row whose
   content runs close to the right edge.

Measured end to end against five real paired (screen, transcript) samples:
**11/15 anchored, up from 4/13** with head matching. The four misses are
conversations whose content is not in the captured scrollback at all — no
technique reaches those. Prompt matching was measured too (3/13) and is not
better, so the existing `_recover_marks` lives with the same limit.

Shipped: `transcripts.reply_times`/`latest_reply_at`,
`TerminalAgent.set_transcript_reply_at` + `reply_time_changed` (transient,
`max(live, transcript)`), `WorkspaceManager.refresh_ai_titles` polls it at
`SESSION_SYNC_MS` for stopped agents too but never mid-turn,
`TerminalCard._recover_reply_marks` merged behind live marks. +26 checks
(1766 total, only the pre-existing schedule-composer flake failing).
CLAUDE.md's resumed-launch bullet corrected — it claimed transcript-backed
reply recovery was impossible. v0.15.0.

**Not addressed, and worth knowing:** hypothesis 3 above (two simultaneous
`main.py` processes, one windowless) was not investigated. It is unrelated to
this bug but is still an open question about the single-instance mutex.
