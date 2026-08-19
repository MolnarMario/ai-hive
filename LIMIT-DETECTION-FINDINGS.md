# Plan-limit detection: four defects found in live forensics (2026-08-09)

**Status: v2, post-review.** v1 was reviewed by a second agent
(`~/.gemini/antigravity-cli/brain/242652ac-.../LIMIT-DETECTION-REVIEW.md`).
This version folds in what the review established, records what was rejected
and why, and supersedes both documents. Changes from v1 are listed in
"Resolution log" at the end.

All findings come from reading live artifacts of a running instance, not from
reasoning about the code. Evidence is quoted inline so this stands alone, but
everything is re-derivable from the paths in "How to verify".

**Scope:** `app/terminal_agent.py` (`_scrape_limit`), `app/transcripts.py`
(`_read_limit_cut_off`), `app/widgets/main_window.py` (`_auto_continue_agent`,
`_resume_blocked_agents`, `_ledger_cut_off`), `app/limit_banner.py`.

**Still open and most in need of a third opinion:** Finding 2's root cause.
Two theories have now been proposed and both are refuted. Do not implement a
fix for it.

---

## How to verify

Session dir: `%APPDATA%\AIHive\AI Hive\`

| artifact | what it settles |
|---|---|
| `session.log` | audit trail: `LIMIT BLOCKED / NO-LATCH / PHANTOM / STARTUP-SCAN`, app pid lifetimes |
| `limit_events.jsonl` | the ledger: one record per episode, carries `source` (`live` vs `transcript`) |
| `screens/*.vt` | raw VT snapshots; mtime marks the last graceful `closeEvent` |
| `live_sessions.jsonl` | SessionStart hook edges; reset each run |
| `~/.claude/projects/C--Users-Mario-Downloads-AI-Projects-Chess-2-0/*.jsonl` | the two conversations involved |

Conversations: `469db124-...` (old, Aug 6 to Aug 9 07:34) and
`2cfd7ecc-...` (current, from Aug 9 07:34).

Transcript records are UTC (`Z`); `session.log` and the ledger are local
(UTC+3, DST). A one-hour slip here is easy and I made it once mid-investigation.

---

## Timeline (established, all from artifacts)

```
Aug 6 06:39:48  genuine cut-off in 469db124, "resets 6:50am"   [rec 253, err=rate_limit]
Aug 6 06:50:08  AI Hive nudges: "The usage limit has reset..."  [rec 255, promptSource=typed]
Aug 6 06:50:28  ledger: resumed, tries=1
                ...conversation then sits idle for three days...

Aug 9 06:14:35  app pid 23076 starts; agent resumes 469db124
Aug 9 06:15:16  LIMIT BLOCKED resets=2026-08-09 06:50            <-- FALSE (Finding 3)
Aug 9 06:52:34  LIMIT PHANTOM -> dismissed
Aug 9 07:21:07  LIMIT NO-LATCH (echo guard, 6:50am banner)
Aug 9 07:34:11  new conversation 2cfd7ecc begins (/clear or /compact)
Aug 9 07:34:15  SESSION-SYNC 469db124 -> 2cfd7ecc
Aug 9 07:53:00  GENUINE cut-off, "resets 10am"        [2cfd7ecc rec 384, err=rate_limit]
Aug 9 07:53:08  BG-SHELL count=7 baseline=3 (gear lit; NO hourglass)  <-- Finding 2
Aug 9 07:55:01  pid 23076 closes.  No live LIMIT BLOCKED ever logged for this cut-off.
Aug 9 07:55:27  pid 23020 starts; STARTUP-SCAN found=1 armed=1 (hourglass appears)
Aug 9 07:55:54  pid 23020 closes gracefully (27s lifetime; screens/*.vt mtime)
                ...no AI Hive running; the 10:00 reset passes unattended...
Aug 9 11:42:05  pid 4860 starts; STARTUP-SCAN found=1 armed=1
Aug 9 11:42:12  all 12 agents fire SessionStart source='resume'
Aug 9 11:42:12  Claude's OWN "Continue from where you left off."  [rec 389, isMeta:true]
Aug 9 11:42:13  <task-notification> re-enqueued and dequeued      [rec 387/388/391]
Aug 9 11:51:04  LIMIT PHANTOM -> dismissed ("no real work lost")  <-- FALSE (Finding 1)
```

Net outcome: a genuine cut-off at 07:53 was never resumed. The interrupted
work (serve the app locally, test the online flow in two browser tabs) was
abandoned, and the ledger closed the episode as
`"transcript shows no real work lost"`.

---

## Finding 1 (highest impact): a genuine cut-off is dismissed as phantom because Claude Code's own resume plumbing counts as "the conversation carried on"

**Status: confirmed by review. Fix criterion revised twice, now settled.**

**Symptom.** The 07:53 cut-off was correctly armed at 11:42:05 by startup
recovery, then silently dismissed at 11:51:04. No nudge was ever sent.
`AUTO_CONTINUE_TEXT` appears **0 times** in `2cfd7ecc`.

**Evidence.** What ran after the resume was not work anyone asked for:

| rec | local | text | fields |
|---|---|---|---|
| 389 | 11:42:12 | `Continue from where you left off.` | `isMeta: true`, no `promptSource` |
| 390 | 11:42:12 | `No response requested.` (assistant) | |
| 391 | 11:42:13 | `<task-notification> ... status: stopped` | `promptSource='system'`, `origin={'kind':'task-notification'}` |

`Continue from where you left off.` is Claude Code's own string:
`grep -rn "Continue from where" --include=*.py .` returns nothing in this repo.
Contrast AI Hive's own nudge on Aug 6 (rec 255): `promptSource='typed'`,
`origin={'kind':'human'}`, no `isMeta`.

**Root cause.** `transcripts.py:254-255` in `_read_limit_cut_off`:

```python
# every assistant turn overwrites the verdict, so only the LAST
# one counts -- a banner followed by real output is history
```

Every assistant turn overwrites the verdict unconditionally. The
`_is_synthetic_user_turn` guard exists to discount CLI plumbing but is only
consulted for the turn *behind* the banner (`last_user_synthetic`, used at
`transcripts.py:267`), never for turns *after* it that clear the verdict. So
recs 389 to 391 turned `cut_off: True` into `cut_off: False`, and
`_auto_continue_agent` (`main_window.py:1997-2006`) dismissed on
`not info["cut_off"]`.

Second, independent reason it was never nudged even before the dismissal:
`_resume_blocked_agents` filters on `not a.is_busy()` (`main_window.py:1933`),
and the plumbing made the agent busy within a second of resuming, so it was
skipped at the first watchdog tick.

### Fix criterion (revised; do not use v1's)

Two candidate gates were proposed and **both are refuted by an empirical scan
of all 1,620 readable transcripts** (1,623 files, 3 skipped for size):

- **v1's proposal, `origin.kind == 'human'` / `promptSource == 'typed'`** —
  rejected. The reviewer raised this and I confirmed it independently: the
  `promptSource=None, origin=None` bucket holds 546 bare-string user records
  including `'please refactor the parser'` (148) and
  `'can you add Chess960 castling support?'` (74). Real work. The gate would
  misclassify user-typed slash commands and plain prompts alike.
- **My fallback, "bare string + no tag + `promptSource is None`"** — rejected
  by the same records, same reason.
- **The review's proposal, hardcoding the literal
  `"Continue from where you left off."`** — not recommended. It is
  CLI-version and locale specific, and it is a different category from
  `_SYNTHETIC_USER_TAGS`, whose allowlist is deliberately structural tags
  rather than prose (see CLAUDE.md on why keying off a leading `<` alone was
  wrong).

**Use the structural marker instead.** Record 389 carries **`"isMeta": true`**.
It is CLI-emitted, and `grep -rn "isMeta" --include=*.py .` confirms nothing in
this repo uses it today.

`isMeta` alone is not "plumbing" though. Across 484 `isMeta: true` user
records it also marks image-attachment metadata (223), `<local-command-caveat>`
(147), and skill-context injections (`Base directory for this skill: ...`,
`Approach this as the design lead ...`). One record,
`'Check on the smoke test background run ...'`, is a genuine instruction and
carries `promptSource='system'`.

So the criterion is:

> A user record is **non-work** when it matches `_SYNTHETIC_USER_TAGS`, or when
> `isMeta is True` **and** `promptSource` is absent/None.
>
> A `cut_off` is cleared only when a user record that is **not** non-work
> appears *after* the banner.

Two properties matter and both were checked:

- **Phrased as "does a real user record exist after the banner", not "is the
  immediately preceding record real."** Image metadata often follows a typed
  prompt, so an adjacency test would read a genuine resume as still-blocked.
- **The `promptSource is None` clause keeps the ambiguous case safe.** The
  `promptSource='system'` instruction above is treated as real work, so the
  error falls on the side of clearing rather than typing a stray `Continue`.

Verified against both live cases: Aug 6 rec 255 (AI Hive's own nudge) clears
correctly; Aug 9 recs 389 (`isMeta`, no `promptSource`) and 391 (tagged) do not.

**Confidence.** Root cause: high. Fix criterion: medium-high, up from medium in
v1. It now rests on a full-corpus scan rather than a handful of records, but
still only on CLI 2.1.224 and one user's transcripts.

---

## Finding 2: the live scrape missed the genuine 07:53 cut-off; only the transcript scan caught it

**Status: symptom confirmed, cause UNKNOWN. Two theories proposed, both
refuted. Do not implement a fix.**

**Symptom.** The gear (background shell) was lit at 07:53:08 and 07:53:29 but
no hourglass. The hourglass only appeared after the next app launch.

**Evidence.** No `LIMIT BLOCKED` from pid 23076 for this cut-off despite it
running until 07:55:01. The ledger records it `source=transcript`, never
`source=live`. There is also **no `NO-LATCH` line** for the 10am banner;
`_note_limit_skip` fires whenever a banner is found and rejected, so its
absence means `_scrape_limit` never *found* the banner, rather than finding and
rejecting it.

### Theory A (v1, mine): the scan window is too narrow

`self._tail_lines(40, skip_blank=True)` at `terminal_agent.py:1288`. The banner
is followed by the `/upgrade or /usage-credits` line, the turn-duration display
and input-box redraws, which may push it past 40 lines of content. Same family
as the CVsummer2026 miss that `skip_blank=True` was added for.

**Unproven.** Never reproduced. The live path only had about two minutes here
(07:53:00 cut-off, 07:55:01 app close), so "missed it" and "did not get the
chance" are not separable from this episode alone.

### Theory B (review's): `_scrape_limit()` runs before `_prompt_ready` is set

The review proposed that a live cut-off arrives while `_prompt_ready` is
`False`, `_scrape_limit` bails, `_prompt_ready` flips `True` later in the same
frame, output then ceases, and the scrape never runs again.

**Refuted three ways:**

1. `_prompt_ready` is **never reset per turn** — only at `__init__`
   (`terminal_agent.py:244`), `start()` (:344) and `restart()` (:412). The
   agent had been running since 07:34, so it was `True` at 07:53. The premise
   fails.
2. `_on_idle_timeout` **does** call `_scrape_limit()`
   (`terminal_agent.py:1175`), two seconds after output settles. "Never
   executed again" overlooks the settle path that exists for this case.
3. Had it bailed on `not _prompt_ready` with a banner visible,
   `_note_limit_skip` would have written a `NO-LATCH` line. There is none. The
   theory predicts a log entry that does not exist.

### Do not apply the review's Action Plan item 2

It proposes moving `_scrape_limit()` to run *after* `_prompt_ready` is
evaluated in `_on_pty_output`. That ordering is deliberate; the docstring
states *"(This is checked before `_on_pty_output` sets the flag, so the burst
that ends the replay is itself excluded.)"* Reversing it makes the
replay-ending burst eligible to latch, which is exactly Finding 3's failure
mode. The review's Q5 answer ("none of these fixes conflict with the
invariants in CLAUDE.md") is incorrect on this point.

**What would actually settle it:** reproduce a live cut-off with output
captured, or add a temporary unconditional trace of
`(len(region), bool(banner), _prompt_ready)` at the top of `_scrape_limit`.

---

## Finding 3: a stale banner replayed by `--resume` produces a false cut-off latch

**Status: confirmed by review; mechanism and fix unchanged from v1.**

**Symptom.** `2026-08-09 06:15:16 LIMIT BLOCKED resets=2026-08-09 06:50` on an
agent working normally at ~60% account usage. Hourglass shown alongside the
working indicator for 37 minutes.

**Evidence that no cut-off occurred.** `469db124` holds 656 records spanning
Aug 6 to Aug 9 and **exactly one** `rate_limit` record:

```
[253] 2026-08-06T03:39:48.207Z  err='rate_limit' status=429 model='<synthetic>'
      "You've hit your session limit · resets 6:50am (Europe/Bucharest)"
```

Nothing on Aug 9. Work ran straight through the latch instant (prompt
03:15:15.687Z, assistant 03:15:20.533Z, continuing past 03:38Z). The ledger's
banner is byte-identical to rec 253 with `source: "live"`, so it came from
`_screen_tail`, which is fed only by child output.

**Why it was reachable.** At restore time the conversation ended at rec 262,
and rec 253 was only ~542 rendered characters from the end (recs 254 to 262 are
one 65-char user line and one 477-char assistant reply; the rest render
nothing). Comfortably inside the 4000-char `_screen_tail`.

**Root cause.** Two guards that should have stopped it, both inert:

1. **The `_prompt_ready` gate never engaged.** `_scrape_limit`'s docstring
   asserts *"The input-box footer marks the end of the replay, and a real
   cut-off can only happen after it."* False for CLI 2.1.224: across
   `screens/*.vt`, the first `_CLAUDE_READY_HINTS` match
   (`terminal_agent.py:155`) lands 2.1 to 3.6 KB into streams of 78 to 210 KB,
   and in one snapshot 34 KB *before* the "welcome back" splash. Readiness goes
   live while the replay is still painting. Corroborated by zero `NO-LATCH`
   lines despite the audit hook being wired (`workspace_manager.py:485`) and
   demonstrably working (`BG-SHELL` lines use it).
2. **The echo guard was disarmed by the launch.** `_limit_last_banner`
   (`terminal_agent.py:1309`) is reset to `""` by `start()`/`restart()`
   (:350, :406) on the reasoning *"a new screen: nothing is an echo yet"*. True
   for a fresh launch, false for `--resume`, where the screen replays history.

**Amplifier, not cause.** `parse_reset_clock` (`limit_banner.py:153-159`)
resolves a bare clock against today, so a three-day-old banner surfaced as a
plausible `resets=2026-08-09 06:50`. The latch decision at
`terminal_agent.py:1314` happens *before* the clock is parsed at :1324, so
date-awareness alone would not have prevented it. There is also no date on
screen to parse.

**Fix.** Date the banner from the transcript, which already computes this:
`_read_limit_cut_off` records `at = _record_epoch(rec)` and anchors
`resets_at` via `limit_banner.banner_reset_at(text, when)`
(`transcripts.py:267-273`). When about to latch on a *banner with no menu*,
look the banner text up in the transcript and refuse the latch if its
timestamp predates the agent's `_session_started`. Audit the refusal via
`_note_limit_skip`.

Fails safe on the race CLAUDE.md warns about: a genuinely new banner not yet
flushed is simply not found, which is absence of evidence, so it latches as
today. Only a positively-dated old banner blocks the latch, matching the
"positive evidence only" discipline `synthetic` already follows.

The review agreed transcript-dating is preferable to a menu requirement (its
Q2), on the grounds that a live cut-off always satisfies
`when >= _session_started`.

**Note on the review's Q3.** It stated the banner-alone latch path exists to
catch a menu that scrolled out of view while the banner remained. No evidence
was given, and the code says the reverse: the documented concern is the
*banner* scrolling while the *menu* is still up
(`terminal_agent.py:1320-1323`), and CLAUDE.md states the menu "renders
directly BELOW the banner, so it is in view whenever the banner is." Treat the
original question as still open.

**Recurrence.** Not specific to this conversation. Every agent auto-continue
rescues ends its conversation a few hundred characters after a banner, so any
such agent is primed to false-latch on the next restart. It surfaced here only
because the chat sat idle from Aug 6 to Aug 9.

---

## Finding 4 (minor): ledger double-files an open episode, and mislabels a real cut-off

**Status: confirmed by review.**

**Double-file.** The 07:53 cut-off appears twice, identical `key`, both
`source=transcript`, once per app launch that scanned it (07:55:27 and
11:42:05). `_ledger_seen` is a plain in-memory set initialised at
`main_window.py:1107`, populated only by this process at :1781, never seeded
from `limit_events.jsonl`. The guard at :1868 therefore dedupes only within one
process run. Since a single `dismissed` was written at 11:51:04, the other
`cut_off` stays open permanently, skewing `open_cut_offs` / `latest_window`.

**Fix.** Seed `_ledger_seen` at startup from the ledger. Note the review's
Action Plan names `limit_ledger.read_events()`, which **does not exist**; the
API is `read_all()` (`limit_ledger.py:147`), with `closed_keys()` (:169) and
`open_cut_offs()` (:175) alongside.

**Mislabel.** The 11:51:04 outcome is `dismissed` /
`"transcript shows no real work lost"` on a genuine cut-off that cost about
1h42m. The outcome (clearing the hourglass on an agent already going again) was
right; the ledger's account is wrong, and the ledger exists specifically to
answer "what was interrupted last night, and did it recover". Fixing Finding 1
fixes this label as a side effect.

---

## Cross-cutting observations

- **Findings 2 and 3 are the same bug in opposite directions.** A stale banner
  in the screen tail caused a false positive; a real banner missing from it
  caused a false negative. Both say the live screen scrape is the weakest of
  the three signals, and that the transcript should be consulted at the
  *decision* point rather than only afterwards.
- **The watchdog is in-process, so closing AI Hive forfeits auto-continue.**
  Here pid 23020 lived 27 seconds and nothing ran from 07:55:54 to 11:42:05, so
  the 10:00 reset passed with nothing alive to act on. Both toggles were on.
  Working as designed, but it is the dominant real-world failure mode and may
  deserve a note in the UI.
- **`agent=` in audit lines is not unique.** Every workspace numbers its own
  Agent 1, so `session.log` lines cannot be attributed to a workspace without
  the ledger. Consider adding `ws=` to `_limit_audit`.

---

## Recommended order of work

1. **Finding 1** — the only one that loses work. Fix criterion above.
2. **Finding 3** — transcript-dating in `_scrape_limit`.
3. **Finding 4** — seed `_ledger_seen` from `read_all()`.
4. **Finding 2** — investigate only; no fix until the cause is established.

Every fix needs a regression check in `tests/smoke_test.py`; the suite is
headless and must stay on isolated tmp `SessionStore` paths. Suggested cases:
resume plumbing (`isMeta` turn) after a cut-off record; `--resume` replay of a
historical banner; `_ledger_seen` dedupe across simulated restarts.

---

## Resolution log (v1 to v2)

| item | outcome |
|---|---|
| F1 root cause | Confirmed by review; unchanged |
| F1 fix: `origin.kind == 'human'` | **Rejected.** Review's objection confirmed independently against 1,620 transcripts |
| F1 fix: bare-string heuristic (mine) | **Rejected** by the same scan |
| F1 fix: hardcode the English string (review's) | **Not recommended** — version and locale specific, prose in a structural allowlist |
| F1 fix: `isMeta` + `promptSource` (new) | **Adopted.** Structural, corpus-verified, currently unused in the repo |
| F2 root cause: narrow window (mine) | Still unproven |
| F2 root cause: `_prompt_ready` ordering (review's) | **Refuted** three ways |
| F2 Action Plan item 2 (reorder the scrape) | **Rejected** — would worsen Finding 3 |
| F3 | Confirmed; fix unchanged. Review's Q2 agrees |
| F3 / review Q3 rationale for banner-alone path | **Unsupported**; code says the reverse. Question still open |
| F4 | Confirmed. Review's `read_events()` corrected to `read_all()` |
| Review Q5 ("no conflicts with CLAUDE.md") | **Incorrect** — its own item 2 conflicts |
