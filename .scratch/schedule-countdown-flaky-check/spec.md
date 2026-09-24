# The scheduled-send composer check is flaky, and fails on main today

Status: resolved

## Symptom

`tests/smoke_test.py` fails one check on `main`:

```
[FAIL] schedule: the composer says exactly when it will fire :: Sends today at 18:17, in 1:30:00.
```

Observed on 2026-08-21 in two full runs at different wall-clock times (the
caption read 18:14 and 18:17), and a third run the same afternoon also ended
`1 failed`. It failed every run on this machine. On the machine it was written
on, it passed. Nothing about the app is broken for the user; the composer's caption
is correct in both outcomes.

## Where

The check is `tests/smoke_test.py:10328` (search for
`says exactly when it will fire`):

```python
dlg.delay_edit.setText("90m")
dlg._revalidate()
...
check("schedule: the composer says exactly when it will fire",
      "in 1:29" in dlg.when_label.text(), dlg.when_label.text())
```

The code under test is `ScheduleMessageDialog._revalidate` in
`app/widgets/main_window.py:1445`, plus `scheduled_send.format_countdown`.

## Why it flips

`_revalidate` reads the clock twice:

1. `_resolve_due()` -> `parse_delay("90m")` returns 5400, and the due time is
   stored as `time.time() + 5400`.
2. The next line computes `self._due_ts - time.time()` for the caption.

If the clock advanced between those two reads, the remainder is 5399.xx, and
`format_countdown` truncates with `int()` to `1:29:59`. If it did not advance,
the remainder is exactly 5400.0 and the caption reads `1:30:00`.

Windows' default timer granularity is about 15.6ms, so two `time.time()` calls
a few microseconds apart routinely return the identical float. Whether the
tick lands inside that window is luck: machine speed, what else the process is
doing, and where in the 15.6ms period the call happens to fall.

So the assertion is pinned to one of two legitimate outputs. It was written
alongside the feature in `5831cf8` (2026-08-07, "Send a message on a countdown,
instead of pressing Enter now") and it looks like the observed string was
copied into the assertion rather than the intent behind it.

## The intent worth keeping

The check exists for a real reason: the composer has to state when the message
will go out, so a user cannot queue something for 3am by accident. Do not
delete it. The neighbouring check ("a custom delay resolves to a fire time")
already covers the fire TIMESTAMP being right, with a tolerance:

```python
check("schedule: a custom delay resolves to a fire time",
      text == "deploy the thing" and 5300 < when - _time.time() < 5500)
```

This one is about the CAPTION the user reads, which is a different surface
and worth its own check.

## Suggested fix

Assert what was meant ("roughly ninety minutes, in the countdown's own
format") rather than one of the two exact strings. In rough order of
preference:

1. **Accept either boundary.** Smallest change, keeps the check exactly as
   specific as it should be:

   ```python
   when_text = dlg.when_label.text()
   check("schedule: the composer says exactly when it will fire",
         "in 1:29:5" in when_text or "in 1:30:00" in when_text, when_text)
   ```

   `1:29:5x` is the only other thing a single clock tick can produce here (a
   test slow enough to lose a whole second between two adjacent lines has a
   worse problem than this check), so this stays tight enough to catch a real
   formatting regression.

2. **Parse the caption and compare with a tolerance.** Pull the `H:MM:SS` out
   of the label, convert to seconds, assert `5390 < secs <= 5400`. More code,
   but it says the intent out loud and cannot be re-broken by a rounding
   change.

3. **Freeze the clock.** Monkeypatch `time.time` for the duration of the two
   calls so the reading is deterministic. This removes the flake at the source
   but reaches into module internals from the test, which the rest of this
   suite avoids.

Option 1 unless there is a reason to prefer otherwise.

## Please do NOT

- Do not change `format_countdown` to round instead of truncate. A countdown
  that rounds up shows `1:30:00` for a message that fires in 89 minutes and 59
  seconds, and the card's live chip uses the same function; a countdown
  reading one second more than is left is worse than a test needing a fix.
- Do not make `_revalidate` reuse a single `time.time()` reading purely to
  satisfy the test. It would work, but the two readings are genuinely taken at
  different moments and the drift between them is real, if tiny.
- Do not delete the check.

## Done when

`tests\smoke_test.py` passes with zero failures, and the check still fails if
someone breaks the caption (verify by temporarily changing the `f"Sends ..."`
string in `_revalidate` and confirming it goes red).

Bump `__version__` in `app/__init__.py` and the check count in `README.md` if
the count changes.


## Answer

Fixed in the test, option 2, `tests/smoke_test.py:10328`. The caption is now
parsed and both halves are asserted: `format_clock(when)` must appear verbatim,
and the countdown must fall in `5390 < secs <= 5400`. No production code
changed. Suite: 1782 passed, 0 failed (including the live-Claude e2e).

Two corrections to the write-up above, both measured on this machine:

1. **It is not flaky here, it is a deterministic failure.** `time.time()` is
   `GetSystemTimeAsFileTime()` with `resolution=0.015625`, and 20000/20000
   back-to-back reads returned the identical float. Running
   `test_scheduled_send()` alone three times failed 3/3 with `1:30:00`. The
   board history agrees: every entry since 2026-08-18 writes it off as "the
   pre-existing schedule-composer flake", so the suite's exit code had been
   meaningless for four days. Calling it flaky is what let everyone keep
   skipping it.
2. **Option 1 would have been looser than it looks.** `format_countdown`'s
   formatting is already pinned deterministically at `smoke_test.py:10009` with
   fixed inputs, so this check is not the formatting regression net. Its unique
   job is that `_revalidate` wires the label to the right VALUES, and the check
   name is about the absolute fire time, which neither the old assertion nor
   option 1 ever looked at. `format_clock` had no coverage anywhere in the
   suite.

Mutation-tested both halves, per the "Done when" section. Dropping
`format_clock` from the f-string gives `Sends soon, in 1:30:00.` -> FAIL;
feeding `format_countdown` the wrong unit gives
`Sends today at 18:55, in 90:00:00.` -> FAIL; restored -> PASS.

All three "Please do NOT" items were respected: `format_countdown` still
truncates, `_revalidate` still takes its two genuine clock readings, the check
still exists. Version bumped to 0.17.3. The check count is unchanged at 1782,
so README needed no edit.
