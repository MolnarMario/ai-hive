# Startup CLI auto-update gate (Claude Code + Gemini `agy`)

Plan of record. Grilled 2026-08-10; decisions at the bottom of each section are
settled unless re-opened.

## 1. The problem, root-caused

The reported symptom: "almost every time I open AI Hive I see `Update available!
Run: winget upgrade Anthropic.ClaudeCode`, so I close the app, kill processes,
upgrade from a terminal, and reopen." Plus a past incident: upgrading manually
from a terminal, then finding **no Opus 5 in `/model`** inside AI Hive, while
re-running the upgrade insisted it was already up to date.

That second half is the diagnosis. `claude.exe` is a **single 285 MB
self-contained binary**, and Windows cannot overwrite a running `.exe`. Every AI
Hive agent IS a `claude.exe` child held alive by the Job Object. So an upgrade
run with agents up cannot replace the file, but **winget records the new version
in its own database anyway**. From that moment three things all lie in the same
direction:

- the file on disk is still old, so its baked-in model table has no `opus-5`
  string and `/model` cannot offer Opus 5 (the documented client-side alias
  resolution in CLAUDE.md);
- that old binary keeps running its own vendor-channel version check, so the
  "Update available!" banner prints forever;
- winget compares its own recorded version against the manifest and answers
  "No available upgrade found", because by its books the job is done.

Corroboration on this machine: `claude.exe` is dated **Aug 8 00:33**, and the
board carries Agent 3 writing on Aug 7 that a restart "also frees claude.exe so
the pending winget upgrade can finally apply." It landed the moment nothing held
the file.

Current state is clean: one copy only, no native/npm/Links duplicates, PATH
agrees with `providers.resolve_claude()`, installed 2.1.224, manifest 2.1.224,
44 `opus-5` matches in the binary. `agy` is 1.1.11 (CLAUDE.md says 1.0.16 and is
stale).

**Consequence for the design:** the startup window is not merely a convenient
moment, it is the ONLY moment the binary is unlocked. And winget's report can
never be treated as evidence.

## 2. Settled decisions

| # | Decision | Rationale |
|---|---|---|
| Q1 | Claude via **winget**; Gemini via **`agy update`** | `agy` is not a winget package at all (`Google.AntigravityIDE` is the separate IDE). `claude update` installs a *native build* to a different location; `providers.resolve_claude()` has a hardcoded WinGet-Packages fallback, so a migration could leave AI Hive silently launching the stale copy. Winget keeps one install in one place. |
| Q2 | **Pre-flight process count**; if any `claude.exe`/`agy.exe` is alive, skip and name the count. Never kill | Those are the user's own sessions or another app's, outside our Job Object. Killing one can destroy a transcript. Also see 4.3: *not* running winget while locked is itself the fix for the poisoned-database bug. |
| Q3 | **Split budget.** Check is timeout-bounded (5 s) and fails open to launch. Install is never killed on a timer; it gets a Skip that launches AI Hive and leaves it running | A killed mid-install leaves a half-written 285 MB binary, worse than the banner. A hung network must never cost the user the app. |
| Q4 | **`ui.auto_update`, default OFF**, 🟢/⚫ LED, adjacent to the taskbar toggle, with a hover tooltip stating what activating it does | It mutates installed software unattended, like `auto_continue`/`startup_recovery` before it. One click, once, persisted. |
| Q5 | **Audit every version transition** to `session.log`. No patch-vs-minor gate | Nothing in Anthropic's numbering predicts whether a flag moved; 2.1.220→2.1.224 could break `--permission-mode` as easily as 2.2.0. A version gate buys false safety. An audit line turns "it broke this morning" into a lookup. |
| Q6 | Splash **before** `create_main_window()`; Claude then agy, **serially** | Paints in ms instead of after the whole window builds; makes an agent starting mid-update structurally impossible; keeps update logic out of `MainWindow`. Two multi-hundred-MB installers writing at once is a bad day. |
| Q7 | Success is decided by **`--version` on the resolved binary**, before and after. Never by winget's output | This is the fix for the actual bug. Costs 0.09 s (measured). |
| Q8 | A quiet **"update pending, restart to apply"** pill beside the LED. No chime | Answers "why am I still seeing the nag?" without interrupting. Same principle as the usage badge's can't-read pill. |
| Q9 | Stdlib-only `app/cli_update.py` with the **command runner injected**; real subprocess only from `main.py` | The suite must never upgrade the user's CLI mid-run. Same opt-in rule as `start_usage_polling()`. |

## 3. Measured facts the design leans on

| Command | Time | Output shape |
|---|---|---|
| `claude.exe --version` | **0.09 s** | `2.1.224 (Claude Code)` |
| `agy.exe --version` | **0.09 s** | `1.1.11` |
| `winget show --id Anthropic.ClaudeCode --exact` | ~0.5 s warm | line `Version: 2.1.224` |
| `winget upgrade --id ... --exact` (nothing to do) | 0.54 s warm | `No available upgrade found.` |
| `winget list` (cold source index refresh) | **2.8 s** | — |
| `agy update` (already current) | **0.37 s** | `⟳ Checking for updates... (current version 1.1.11)` then `✓ You are already on the latest version.` |

Measured with **zero `agy.exe` alive**, i.e. the unlocked path. It mutated
nothing: version, byte size (175 861 400) and mtime (Aug 9 14:04) all unchanged.

**Total gated startup cost when everything is current: roughly 1.5 s**, worst
case ~4 s if winget has to refresh a cold source index. Comfortably inside the
5 s check budget, so the splash is a glance and not a wait.

Neither `claude update` nor `agy update` accepts any flags: **no dry-run**, so
for those two, checking and installing are the same act. This is why the Claude
check uses `winget show` (a pure read) rather than `winget upgrade`.

## 4. Architecture

### 4.1 `app/cli_update.py` (new, Qt-free, stdlib-only)

Same class of module as `chime.py`, `limit_banner.py`, `taskbar_overlay.py`,
`screen_snapshot.py`: no PySide6 import, all decisions pure and testable.

```
Runner = Callable[[list[str], float], tuple[int, str]]   # argv, timeout -> (rc, output)

@dataclass(frozen=True)
class Target:
    key: str                  # "claude" | "gemini"
    label: str                # "Claude Code" | "Gemini (agy)"
    exe: str                  # providers.resolve_claude() / resolve_program("gemini")
    process_names: tuple      # ("claude.exe",) / ("agy.exe",)
    # claude only:
    winget_id: str | None     # "Anthropic.ClaudeCode"
    # agy only:
    self_update: tuple | None # ("update",)

class Status(str, Enum):
    UP_TO_DATE, UPDATED, BLOCKED_PROCESSES, DB_STALE, REPORTED_BUT_UNCHANGED,
    FAILED, TIMEOUT, NOT_INSTALLED, DISABLED

@dataclass(frozen=True)
class Outcome:
    target: str; status: Status; before: str; after: str; available: str; detail: str
```

Functions, each pure given a `Runner`:

- `parse_version(text) -> str` — first `\d+\.\d+\.\d+` in the output. Serves
  both CLIs and `winget show`'s `Version:` line.
- `version_tuple(s) -> tuple[int, ...]` — for ordering. An unparseable version
  is `()` and is treated as **up to date** (fail open; never install on a parse
  failure).
- `count_processes(names, runner) -> int` — `tasklist /FI "IMAGENAME eq X" /NH`.
  stdlib only, no psutil. At gate time AI Hive has started no agents, so any
  hit is foreign by construction.
- `check(target, runner) -> Outcome` — the CHECK phase, no mutation.
- `apply(target, outcome, runner) -> Outcome` — the INSTALL phase.
- `run_gate(targets, runner, enabled) -> list[Outcome]` — orchestration,
  serial, the thing `main.py` calls and the thing the suite drives.

### 4.2 The Claude check never asks winget what is installed

```
installed  = parse_version(run([exe, "--version"]))            # the FILE
available  = parse_version(run(["winget","show","--id",id,"--exact"]))  # the MANIFEST
```

A poisoned winget database cannot hide an update from us, because we never read
its installed-version record. This is the core repair.

### 4.3 State table

| Condition | Status | Action |
|---|---|---|
| toggle off | `DISABLED` | no runner call at all, splash never shown |
| exe not resolvable | `NOT_INSTALLED` | skip that target silently |
| any target process alive | `BLOCKED_PROCESSES` | **skip without running any upgrade command** |
| `installed >= available` | `UP_TO_DATE` | nothing |
| `installed < available` | → apply | run the upgrade |
| after apply, version moved | `UPDATED` | audit the transition |
| after apply, winget said "No available upgrade found" but `installed < available` | `DB_STALE` | retry once with a forced reinstall (see 4.4) |
| after apply, winget claimed success but version unchanged | `REPORTED_BUT_UNCHANGED` | report precisely; pill |
| non-zero rc / unparseable | `FAILED` | report; pill; never retried automatically |
| check exceeded 5 s | `TIMEOUT` | proceed to launch, no pill |

`BLOCKED_PROCESSES` skipping the upgrade command entirely is deliberate and is
the direct fix for section 1: running winget against a locked file is what wrote
the false record in the first place.

### 4.4 The un-wedging path (`DB_STALE`)

If the file is behind the manifest but winget answers "No available upgrade
found", its database is already poisoned from a previous locked run, and a plain
`winget upgrade` can never repair it. One forced reinstall of the manifest
version is the documented escape.

**Unverified:** the exact flag. Candidates are `winget install --id <id>
--exact --force` and `winget upgrade --id <id> --force`. Step 0 must confirm
which actually replaces the file, on a machine where the state can be observed.
Until confirmed, `DB_STALE` reports and shows the pill with the one command to
run by hand, and does NOT attempt a force. Shipping a guessed `--force` at
startup is not acceptable.

### 4.5 Threading

The gate runs the subprocesses on a `threading.Thread` and the splash polls a
`queue.Queue` on a `QTimer`, held open by a local `QEventLoop`. This is
mandatory, not stylistic: CLAUDE.md records that `gemini_usage.fetch()` shelling
out inline on the GUI thread froze the app ~6 s per minute. Never block the GUI
thread on a subprocess.

### 4.6 `app/widgets/update_splash.py` (new)

Frameless `QWidget` (Qt tool window), themed from the live `Palette` at paint
time like `ornaments.BootVeil` (no new QSS tokens). Shows one row per target:
label, state text, and a sweeping-arc spinner reusing the `BootVeil` motif.
Buttons: **Skip** (closes the loop, launches AI Hive, leaves any running
install alone) and nothing else. Auto-closes ~700 ms after the last outcome so
"nothing to do" is a glance, not a click.

## 5. Wiring

### 5.1 `main.py`

Insert between `setup_application(app)` (275) and `create_main_window()` (276):

```python
store = SessionStore()
session = store.load()
if session.get("ui", {}).get("auto_update", False):
    outcomes = update_gate.run(store)      # splash + serial check/apply
else:
    outcomes = []
window = create_main_window(store)
...
window.note_update_outcomes(outcomes)      # sets the pending pill, if any
window.show()
```

`create_main_window` already accepts a `store` (line 109); the extra `load()` is
cheap and keeps the factory unchanged. The gate sits far above
`autostart_active_workspace()` (283), so no agent can hold a binary.

Opting in from `main.py` rather than the factory is the same rule as
`start_usage_polling()` and `recover_blocked_at_startup()`: the offscreen suite
shares `create_main_window` and must never shell out.

### 5.2 `app/widgets/main_window.py`

**TopBar** (constructor near line 269, beside `taskbar_btn`):

- `self._auto_update = False`; `self.auto_update_btn = QToolButton`,
  `setObjectName("RecoveryToggle")` (reuses the lit/dim + LED treatment, no new
  QSS), text `f"{led} ⬇"`, added to `lay` immediately after
  `taskbar_btn` (line 356).
- `_on_auto_update_clicked` / `set_auto_update(on)` / `_refresh_auto_update_btn`,
  mirroring `_on_taskbar_clicked` / `set_taskbar_badge` /
  `_refresh_taskbar_btn` exactly. New signal `autoUpdateToggled(bool)`.
- Tooltip, both states, **no em dash** (`test_no_em_dashes_in_visible_text`):

  > ON: "Auto-update CLIs: ON. Next time AI Hive starts, it checks for a newer
  > Claude Code and Gemini (agy) CLI and installs it BEFORE any agent launches,
  > which is the only moment those files are not locked. Click to turn off."
  >
  > OFF: "Auto-update CLIs: OFF. Startup is untouched, so you keep whatever CLI
  > version is installed and may keep seeing Claude's own 'update available'
  > banner. Turning this on lets AI Hive install CLI updates at startup, which
  > changes installed software on your machine. Click to turn on."

- `self.update_pill = QLabel` (`objectName "UpdatePill"`), hidden by default,
  `note_update_pending(text)` to show it. Placed left of `auto_update_btn`.

**MainWindow**:

- `self._auto_update = False` beside `self._taskbar_badge` (line 1120).
- `_restore_ui_state`: `self._auto_update = bool(ui.get("auto_update", False))`
  then `self.top_bar.set_auto_update(...)`, beside the `taskbar_badge` restore
  (line 2598).
- `_ui_state` payload: `"auto_update": self._auto_update` beside
  `"taskbar_badge"` (line 3059).
- `_on_auto_update_toggled` → set field, `_schedule_save()`. **Additive optional
  key, NO `SESSION_VERSION` bump** (identical to `usage_visible`,
  `taskbar_badge`, `auto_continue`, `startup_recovery`).
- `note_update_outcomes(outcomes)` → pill text for `BLOCKED_PROCESSES`,
  `DB_STALE`, `REPORTED_BUT_UNCHANGED`, `FAILED`; nothing for `UPDATED`,
  `UP_TO_DATE`, `TIMEOUT`, `DISABLED`.

### 5.3 Audit lines (`SessionStore.audit`, already public at line 65)

```
UPDATE-CHECK claude installed=2.1.224 available=2.1.231
UPDATE claude 2.1.224 -> 2.1.231
UPDATE-SKIP claude (3 claude.exe alive)
UPDATE-STALE claude winget reported no upgrade but binary 2.1.224 < 2.1.231
UPDATE-UNCHANGED claude winget reported success, binary still 2.1.224
UPDATE-FAIL claude rc=1 <first line of output>
UPDATE-TIMEOUT claude check exceeded 5s
```

## 6. Step 0, before any code

1. ~~**`agy update` when already current.**~~ **RESOLVED 2026-08-10: 0.37 s, no
   mutation.** No cache, no timestamp file, no once-per-N-hours gate. It runs on
   every gated startup as-is. It also self-reports (`current version 1.1.11`),
   though per Q7 the authoritative reading stays `agy --version` on the resolved
   binary, not agy's own message.
2. **The `DB_STALE` force flag** (4.4). Confirm which winget invocation
   actually replaces a file whose database record is already ahead of it.
   Until then `DB_STALE` only reports.

### 6.1 Known edge: Skip while an agy install is in flight

`agy update` is one command for check and install, so it gets no kill timer
(§Q3). If the user hits **Skip** while agy is genuinely downloading, AI Hive
launches while the 176 MB binary is being replaced, and an autostarting Gemini
agent could execute a half-written file.

Given the measured 0.37 s for the common path this is a narrow window, so the
mitigation stays proportionate: on Skip with an agy install still running, defer
**Gemini agents only** out of the autostart and say so in the pill. Do not
build anything more elaborate for it.

## 7. Tests (`tests/smoke_test.py`, new section, injected runners only)

Every one drives `run_gate` with a fake `Runner`; **no test ever shells out.**

1. `parse_version` on `2.1.224 (Claude Code)`, on `1.1.11`, on winget's
   `Version: 2.1.224` line, and on garbage (→ up to date, no install).
2. Toggle OFF → zero runner calls, no splash.
3. `installed == available` → `UP_TO_DATE`, upgrade command never issued.
4. `installed < available`, version moves → `UPDATED` + audit line.
5. **Regression for the reported bug**: winget exits 0 claiming success, second
   `--version` unchanged → `REPORTED_BUT_UNCHANGED`, pill shown.
6. **Regression for the cause**: one `claude.exe` alive → `BLOCKED_PROCESSES`
   and **no upgrade argv in the recorded calls** (asserts the winget database is
   never poisoned by us).
7. `installed < available` but winget says "No available upgrade found" →
   `DB_STALE`, no forced command issued (until Step 0 resolves it).
8. Check timeout → `TIMEOUT`, gate returns, launch proceeds.
9. Serial ordering: agy's commands never interleave with Claude's.
10. `ui.auto_update` round-trips through `_ui_state` / `_restore_ui_state`,
    defaults **False** on a session that lacks the key, and `SESSION_VERSION`
    is unchanged.
11. Pill shown for exactly the four reporting statuses, hidden otherwise.
12. Tooltip strings in both states carry no em dash (covered automatically by
    `test_no_em_dashes_in_visible_text`).
13. `create_main_window` performs no update work (guards the suite).

## 8. Invariants respected

- Transient vs persisted: only the `ui.auto_update` preference saves, via
  `_schedule_save`, additive, no `SESSION_VERSION` bump. Outcomes are transient
  and never touch session state.
- No GUI-thread subprocess (the `gemini_usage` freeze).
- Qt-free stdlib-only decision module; real I/O only from `main.py`.
- Never `terminate()`/kill anything, least of all a foreign `claude.exe`.
- Everything auditable in `session.log`.
- No em dash in user-visible strings.
- Runs strictly before `autostart_active_workspace()`.

## 9. Out of scope

- **Grok** (`providers` has it) — a one-line `Target` addition later.
- **Mid-session updating** — impossible by construction; the pill is the answer.
- **Anything that migrates the install off winget**, including `claude update`,
  unless Q1 is deliberately re-opened.
