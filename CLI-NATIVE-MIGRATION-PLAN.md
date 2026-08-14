# Implementation spec: "Let Claude Code update itself"

A consented, reversible, one-click switch that moves a package-manager Claude
Code install onto Anthropic's self-updating native install, and can pause,
resume, or fully revert afterwards.

**This document is written to be implemented by an agent with no prior context.**
Read `CLAUDE.md` in full first (it is the invariant record and overrides
everything here on conflict), then `CLI-AUTO-UPDATE-PLAN.md` at the repo root
for the vocabulary this reuses (`Target`, `Status`, the startup gate, the pill).
The startup gate itself is already built and shipped; this is a separate
control that sits beside it and demotes it.

Facts marked MEASURED were read off the reporting machine or quoted verbatim
from `https://code.claude.com/docs/en/setup` on 2026-08-10/11. Facts marked
UNVERIFIED must be settled by the steps given, not assumed.

---

## 0. Ground rules for whoever implements this

* **Verify before you build on it.** Two things in here are explicitly
  unverified (§8). Do not paper over them; run the stated check and record the
  answer.
* **Nothing in this feature may shell out from `create_main_window()` or from
  any test.** The suite is offscreen, shares `create_main_window`, and must
  never install software, touch the network, or touch the user's real
  `~/.claude/settings.json`. Runners are INJECTED; the real one is passed from
  the GUI layer only. This mirrors `start_usage_polling()` and
  `run_update_gate()`.
* **Do not kill a `claude.exe`, ever.** Killing an agent can destroy a
  transcript. Every blocked path reports and waits.
* **No em dash in any user-visible string.** Enforced by
  `test_no_em_dashes_in_visible_text`, which parses non-docstring string
  literals in every module.
* Verify with `.venv\Scripts\python.exe tests\smoke_test.py`; all checks must
  pass. (This line used to exempt `pty: Ctrl+C stopped the loop` as known-flaky.
  It was never flaky — it was reporting a real dead-Ctrl+C bug, now fixed.)
  Update the check count in `README.md` (two places) when done.

---

## 1. The problem

Claude Code installed through a package manager does not update itself. MEASURED
on the reporting machine:

| source | version |
| --- | --- |
| Anthropic (`registry.npmjs.org/@anthropic-ai/claude-code/latest`) | **2.1.226** |
| winget manifest (`winget show --id Anthropic.ClaudeCode`) | 2.1.224 |
| the installed file (`claude.exe --version`) | 2.1.224 |

The lag is structural: a human publishes each winget manifest. The CLI's
"Update available!" banner compares against Anthropic, not against winget, so a
fully upgraded winget install still nags in every terminal. Worse, model aliases
resolve CLIENT-SIDE from a table baked into the installed binary (see
`CLAUDE.md`), so a stale binary silently cannot launch newer models.

> Native installations automatically update in the background to keep you on
> the latest version.

> Homebrew, WinGet, apt, dnf, and apk installations do not auto-update by
> default.

**There is no setting that fixes this.** Claude Code knows a package manager
owns its file and refuses to replace it; the documented tell is that
`claude update` on such an install replies `Claude is up to date!` regardless of
the actual version. The install method IS the behaviour, so changing the
behaviour means changing the install. Hence a migration, not a preference.

---

## 2. Scope

**Build:** everything in §3 to §7.

**Do NOT build:**
* `CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE=1` support, anywhere. It makes a
  RUNNING Claude Code invoke `winget upgrade` on itself, which is the exact act
  that writes the false winget database record `app/cli_update.py` exists to
  prevent. The vendor documents the failure in the same paragraph that offers
  the flag: "On WinGet the upgrade may fail while Claude Code is running because
  Windows locks the executable."
* The `DB_STALE` forcing flag (still unverified upstream; §4.2 makes it
  unreachable for Claude anyway).
* Migration of `agy`, or any non-Windows install method.
* `DISABLE_AUTOUPDATER` for AI Hive's own agents. Real question (a hive of
  eleven agents is eleven background updaters against one install directory),
  but it argues against the mechanism this adopts and needs evidence nobody has
  yet. Separate change, after §8 is answered.
* Any change to AI Hive's shared `--settings` file. Its `hooks` matchers are the
  timing-critical `startup`-exclusion ones; leave them alone.

---

## 3. The contract: one control, several states

The control is NEVER a boolean. Its label, its modal, and what it does are a
function of the detected state, so a user is never offered an action that does
not apply to their machine.

| state | detected by | control offers |
| --- | --- | --- |
| `MANAGED` | resolved exe under `\Microsoft\WinGet\Packages\` | **Enable automatic updates** (§5) |
| `SELF_ACTIVE` | resolved exe under `%USERPROFILE%\.local\bin\`, no disabling key | **Pause automatic updates**; *Revert to the winget install* (secondary) |
| `SELF_PAUSED` | as above, plus `DISABLE_AUTOUPDATER` or `DISABLE_UPDATES` set | **Resume automatic updates** |
| `NOT_APPLICABLE` | npm global install | nothing (it already auto-updates) |
| `LOCKED_BY_POLICY` | managed settings enforce updates | nothing, with the reason shown |
| `MISSING` | no Claude Code resolved | nothing |

**`SELF_ACTIVE` ⇄ `SELF_PAUSED` is the primary undo**: one key in
`~/.claude/settings.json`, instant, no download, no reinstall, and it freezes
the user on exactly the version they have. That is the "I do not trust anything
newer than x.y.z" case, and it must be reachable from the same control.

`SELF_ACTIVE` → `MANAGED` (full revert: `winget install Anthropic.ClaudeCode`
plus removing the native install) is offered but deliberately SECONDARY in the
modal. It is a second install operation, not a toggle; conflating the two undos
in one click is how a user who wanted to pause ends up reinstalling.

Offer a third, softer choice in the same modal, which will suit more people than
either: `autoUpdatesChannel: "stable"`, documented as "about one week old,
skipping releases with major regressions".

**THE CHANNEL IS NEVER WRITTEN ALONE.** `stable` is a channel, not a ceiling, so
setting it on a machine that is ahead of stable lets the next update move the
user BACKWARDS onto an older build. That is not a cosmetic downgrade: it is
exactly the stale-binary state this whole feature exists to end, because model
aliases resolve from a table baked into the installed file (`CLAUDE.md`), so an
older build silently cannot launch newer models. The vendor's own control does
not have this hole: "Switching from `latest` to `stable` via `/config` prompts
you to either stay on the current version or allow the downgrade. Choosing to
stay sets `minimumVersion` to that version. Switching back to `latest` clears
it."

So `set_channel` writes the PAIR, mirroring `/config`:

* to `stable`: `autoUpdatesChannel: "stable"` AND `minimumVersion` = the version
  read off the FILE right then (the same `<exe> --version` reading everything
  else here is decided by, never a remembered one). An unreadable version means
  the floor cannot be established, so the channel is REFUSED (`ok=False`) rather
  than written without it.
* back to `latest`: write the channel and REMOVE `minimumVersion`, so the pin
  cannot outlive the reason for it.

`minimumVersion` is documented as constraining updates only; it is NOT
`requiredMinimumVersion`, which stops Claude Code starting at all. Do not write
that one, ever.

---

## 4. File-by-file

### 4.1 `app/providers.py` — `resolve_claude()`

Current body: `shutil.which("claude")` first, then a hardcoded WinGet-Packages
guess, then the bare name.

**Change: check the native launcher BEFORE `shutil.which`.**

```
1. %USERPROFILE%\.local\bin\claude.exe     (new, FIRST)
2. shutil.which("claude")
3. the existing WinGet-Packages guess
4. "claude.exe"
```

THIS IS THE MOST LIKELY WAY TO SHIP THE WHOLE FEATURE BROKEN. MEASURED on the
reporting machine:

```
PATH contains:      ...\WinGet\Packages\Anthropic.ClaudeCode_...\
PATH does NOT contain: %USERPROFILE%\.local\bin
Get-Command claude -All  ->  the WinGet copy, and only it
```

The winget package directory is on PATH **directly** (not through a Links
shim). The native installer APPENDS its own directory, so after a migration
PATH holds both and order decides, and `shutil.which` would keep returning the
STALE winget copy. AI Hive would then go on launching the old binary and the
migration would appear to do nothing. This is exactly the risk `CLAUDE.md`
records ("a migration could leave AI Hive silently launching the stale copy").

Deliberate consequence, worth keeping: AI Hive does not depend on the
installer's PATH edit at all, so it never needs a new terminal, and a RESTORED
agent picks the new binary up on its own. `AgentSpec.program` is not persisted
(`to_dict` stores `user_program`; `from_dict` re-runs `build_spec`), so every
agent rebuilt from `session.json` re-resolves through §4.1.

**But an agent that already exists in the LIVE process does not, and the
migration must not claim otherwise.** `build_spec` bakes `spec.program` from
`providers.build_invocation` once, at construction (`process_worker.py`, the
`kind in AI_KINDS` branch), and the only thing that ever rebuilds it mid-process
is `AgentSpec.set_permission_mode`. So a card that existed before the migration
keeps the WinGet path in its spec, and STARTING it again relaunches the stale
binary: the one case where "it did nothing" is a real report rather than the
PATH trap above. `set_permission_mode`'s rebuild is the precedent for the fix;
see §5 step 5, which must do it rather than gesture at it.

### 4.2 `app/cli_update.py` — detect the install method

`default_targets()` currently hardcodes `winget_id=CLAUDE_WINGET_ID` for Claude.
Replace with a path decision:

```python
def _claude_target(exe: str) -> Target:
    """Which updater owns this binary. A native install self-updates
    (`claude update`); a winget package does not, and asking `claude update`
    to replace a winget-managed file is how you end up with two installs.
    Decided on the PATH because nothing else distinguishes them: the binary,
    the version string and the process name are identical."""
    common = dict(key="claude", label=LABELS["claude"], exe=exe,
                  process_names=(_image_name(exe, "claude.exe"),))
    if cli_install.classify_install(exe) is cli_install.InstallKind.WINGET:
        return Target(**common, winget_id=CLAUDE_WINGET_ID)
    return Target(**common, self_update=("update",))
```

Nothing downstream changes: `needs_apply` already returns True unconditionally
for a `self_update` target, `upgrade_argv` already yields `[exe, "update"]`, and
`apply` already reads an unchanged version after a clean self-update as
`UP_TO_DATE` rather than the `REPORTED_BUT_UNCHANGED` that same reading means
for winget. **`Status.DB_STALE` becomes structurally unreachable for Claude on a
native install** (there is no package database to go stale), which retires the
one status whose only remedy was a manual command.

No flag day: the same build serves a winget machine and a native one, and
rolling back needs no code revert.

### 4.3 `app/cli_install.py` (NEW, Qt-free, stdlib-only, never raises)

Same house pattern as `cli_update.py` / `chime.py` / `limit_banner.py`: no
PySide6 import, every entry point returns a value instead of raising, every
decision pure given an injected `Runner` (`argv, timeout -> (rc, output)`;
reuse `cli_update.RC_TIMEOUT`).

```python
class InstallKind(str, Enum):
    WINGET, NATIVE, NPM, UNKNOWN, MISSING

class UpdateState(str, Enum):
    MANAGED, SELF_ACTIVE, SELF_PAUSED, NOT_APPLICABLE, LOCKED_BY_POLICY, MISSING

@dataclass(frozen=True)
class Situation:
    state: UpdateState
    kind: InstallKind
    exe: str            # the resolved binary this describes
    version: str        # "" when unread
    channel: str        # "" | "latest" | "stable"
    paused_key: str     # "" | "DISABLE_AUTOUPDATER" | "DISABLE_UPDATES"
    detail: str         # ONE sentence for the panel, user-facing

@dataclass(frozen=True)
class Result:
    ok: bool
    action: str         # "migrate" | "pause" | "resume" | "channel" | "revert" | "cleanup"
    before: str = ""
    after: str = ""
    detail: str = ""
```

Functions:

* `native_launcher_path() -> str` — `%USERPROFILE%\.local\bin\claude.exe`
* `native_versions_dir() -> str` — `%USERPROFILE%\.local\share\claude\versions`
* `classify_install(exe) -> InstallKind` — by canonical path. Reuse the
  `realpath` + `normcase` canonicalisation added in commit `929a5d7`
  (`cli_update._canonical`); a plain string compare is wrong because winget also
  installs a symlink shim. `\Microsoft\WinGet\Packages\` → WINGET;
  `\.local\bin\` → NATIVE; an npm global directory (e.g. `%APPDATA%\npm\`, or a
  path containing `node_modules`) → NPM; empty/nonexistent → MISSING.
* `detect(exe, settings_path, managed_path, runner=None) -> Situation` — pure
  apart from reading files and, only if a `runner` is given, one `--version`.
* `install_argv() -> list` — the documented installer, and nothing else:
  `["powershell", "-NoProfile", "-NonInteractive", "-Command",
    "irm https://claude.ai/install.ps1 | iex"]`
* `winget_install_argv() -> list`, `winget_uninstall_argv() -> list`
* `read_settings(path) -> tuple[dict, str]` — `({}, "<reason>")` when the file
  is unparseable. **Never raises, never rewrites.**
* `write_settings(path, mutate) -> Result` — re-reads, applies `mutate(dict)`,
  then writes atomically (temp + replace) with one `.bak` generation, the same
  discipline `SessionStore.save` uses. It takes a MUTATION, not a finished
  document, so no caller can hand it a stale copy (see the rules below).
* `set_paused(path, on, strict=False) -> Result` — writes/removes
  `env.DISABLE_AUTOUPDATER` (or `DISABLE_UPDATES` when `strict`).
* `set_channel(path, channel, installed_version) -> Result` — the top-level
  `autoUpdatesChannel` AND its `minimumVersion` floor, per §3. Refuses when
  `channel == "stable"` and `installed_version` is unparseable.
* `winget_exe_path() -> str` — where the WinGet copy lives, independent of what
  `resolve_claude()` currently answers. Needed because after §4.1 nothing else
  can name that file, and §5's cleanup is the one operation still about it.
* `migrate(runner, on_event=None, timeout=None) -> Result` — §5.
* `cleanup(runner, winget_exe) -> Result` — §5. The path is a PARAMETER, never
  re-derived from the resolved binary.
* `audit_lines(result) -> list[str]` — §6.

**Settings-file rules (§4.3 is where a real user's data can be destroyed).**
MEASURED: the reporting user's `~/.claude/settings.json` already holds
`permissions`, two `hooks` entries, `enabledPlugins`, `model`, `effortLevel`,
`tui`, and more. AI Hive has NEVER written this file before (it uses its own
`--settings` file), so this is a genuine escalation.

* Read-modify-write preserving every unknown key.
* `DISABLE_AUTOUPDATER` / `DISABLE_UPDATES` live under the `env` object;
  `autoUpdatesChannel` and `minimumVersion` are top-level.
* Touch exactly the one key involved. Never reformat the document.
* Atomic write, one `.bak` generation kept.
* **RE-READ IMMEDIATELY BEFORE THE REPLACE, inside `write_settings`, and apply
  the mutation to THAT copy.** Atomicity stops a torn file; it does nothing
  about a LOST UPDATE, and this file has other writers. The running CLI owns it
  too: `/model` writing "saved as your default for new sessions" and `/config`
  writing the channel both land here, and a hive can have a dozen agents in
  which any of that can happen at any moment. The read in `detect()` happens
  before a modal the user may sit on for a minute, so a naive write replays a
  minute-old document over whatever an agent saved in between and silently
  discards it. This is `CLAUDE.md`'s own rule for `session.json` ("ALWAYS copy
  the live file aside and re-read it immediately before writing"), which is
  there because a hand edit based on stale forensics once destroyed a user's
  freshly created agents. Same file class, same rule. `set_paused` /
  `set_channel` therefore take the KEY AND VALUE to apply, never a whole
  document to write.
* **If the file does not parse, REFUSE and say so.** Never rewrite it, never
  "repair" it. Returning `ok=False` with a reason is the correct outcome. This
  applies to the RE-READ too: a file that parsed at `detect()` and does not
  parse now must abort the write, not fall back to the earlier copy.

`managed_path`: managed settings can enforce `autoUpdatesChannel`,
`requiredMinimumVersion` and `requiredMaximumVersion`, and overriding an
organisation's policy is not ours to do. **Do not guess the path** — read it
from `https://code.claude.com/docs/en/permissions#managed-settings` and record
what you used in a comment. If any managed file present enforces update
behaviour, the state is `LOCKED_BY_POLICY` and the control does nothing.

### 4.4 `app/widgets/update_panel.py` (NEW)

The panel and the consent modal. Threading discipline is copied from
`app/widgets/update_splash.py` and is not optional: worker thread, `queue.Queue`
drained by a `QTimer`, never an inline subprocess on the GUI thread. (Precedent:
`gemini_usage.fetch()` ran inline and froze the app about 6s a minute.)

House precedent for dialogs is `AddTerminalDialog` / `ScheduleMessageDialog` in
`main_window.py`; put these in the new module rather than growing that file.

### 4.5 `app/widgets/main_window.py` — where the control lives

**Do NOT add a new top-bar button.** MEASURED: the top bar's minimum width is
already 2101px, and this is a ONE-TIME setup action, so a permanent slot is the
wrong trade. Two adjacent update controls meaning different things is worse than
either.

The existing `⬇` button (`TopBar.auto_update_btn`) **absorbs it**: clicking it
opens a small **Updates** panel that shows the detected state in one sentence
plus the state-appropriate action, with the existing startup-gate preference
(`ui.auto_update`) as a checkbox inside. The `⬇` tooltip names the current state
so the answer is available without a click. Keep `TopBar.autoUpdateToggled` and
`MainWindow._on_auto_update_toggled` working exactly as they do now for that
checkbox.

**Nothing new is persisted in `session.json`.** The state is DERIVED from the
filesystem and `~/.claude/settings.json` on every read, exactly as the plan
usage reading is derived rather than stored. A stored install-method goes stale
the moment the user installs anything by hand. The only persisted key remains
`ui.auto_update`, unchanged, no `SESSION_VERSION` bump.

### 4.6 `app/ui_theme.py`

Reuse existing tokens (the `#UpdatePill` / `#RecoveryToggle` block is the
precedent). Any new object name must meet the WCAG contrast smoke check on every
skin; run the suite, a low-contrast token fails it.

---

## 5. What the migration does, and why it does NOT fumble over running agents

**The native installer writes to a DIFFERENT directory** (`%USERPROFILE%\
.local\`) than the winget package (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\
...`). It never replaces the running file, so **the migration itself is
lock-free and may run with every agent alive.** That is the structural advantage
over the startup gate, which is confined to the one moment nothing is running.

Only the CLEANUP keeps the old constraint (`winget uninstall` cannot remove a
package whose `.exe` is running), so cleanup is SEPARATE, OPTIONAL and
DEFERRABLE and never blocks the win.

Order of operations:

1. `detect()`. Refuse where §3 says to refuse. **Record the WinGet exe path
   now** (`winget_exe_path()`): the moment the native launcher exists,
   `resolve_claude()` stops naming this file (§4.1), and step 7 is the one
   operation that is still about it.
2. Show the consent modal (§7). The action button is disabled until the
   checkbox is ticked. Cancel has default focus.
3. Run `install_argv()`, streaming output into the panel. **Never killed on a
   timer** (a half-written 285 MB binary is worse than a nag); Cancel is the
   escape hatch, matching the gate's install phase.
4. **Verify off the FILE, never off the installer's report** — the rule the
   whole `cli_update` module is built on. Run
   `%USERPROFILE%\.local\bin\claude.exe --version`, require a parseable version
   not older than the previous one. An installer that reports success while the
   file did not change is a FAILURE.
5. **Rebuild every live Claude spec, not just future ones.** §4.1 fixes what
   `resolve_claude()` ANSWERS; it does not touch a `spec.program` already baked
   by `build_spec`, so without this step every card that existed before the
   migration relaunches the WinGet binary for the rest of the process (§4.1).
   Walk `manager.all_agents()`, and for each `spec.provider == "claude"` re-run
   `providers.build_invocation(...)` and assign `spec.program`/`spec.args`,
   exactly as `AgentSpec.set_permission_mode` already does. Three rules:
   it must NOT restart, stop or otherwise disturb a RUNNING agent (the new path
   applies at that agent's next launch, which is what "lock-free" bought us);
   it must NOT emit `dirty`, because `program`/`args` are derived and are not
   persisted at all (`to_dict` stores `user_program`); and it is idempotent, so
   a second migration attempt is harmless.
6. Report the new state. Offer cleanup as a follow-up, not a gate.
7. Cleanup, only when the user asks for it.

Cleanup: needs no `claude.exe` running THE WINGET FILE alive. Reuse the
path-identity counting from `929a5d7` (`cli_update.count_processes(..., exe=)`)
so the unrelated Claude DESKTOP app, whose binary is also named `Claude.exe`,
is not mistaken for the CLI. **`exe=` is the recorded WinGet path from step 1,
NEVER `resolve_claude()`.** By cleanup time §4.1 has made that function return
the NATIVE launcher, so a count against it answers a question nobody asked: the
user's own WinGet-launched sessions do not match it, the count comes back zero,
and `winget uninstall` runs against a file those sessions are holding. That is
under-counting, the direction `count_processes` documents as the unsafe one, and
here it is unsafe twice over: it either fails and poisons the package database
again, or it removes the rollback out from under a live session. If any genuine
CLI is alive, say so and let the user try later. **Never kill one.**

---

## 6. Audit

Every outcome goes to `session.log` via `SessionStore.audit(message)` (public,
`app/session_store.py:65`), same reasoning as the gate's `UPDATE-*` lines: this
feature will one day be debugged from a log line, months later.

```
CLI-MIGRATE-STATE   <state> kind=<kind> version=<v> exe=<path>
CLI-MIGRATE-START
CLI-MIGRATE-OK      <before> -> <after>
CLI-MIGRATE-FAIL    <reason>
CLI-MIGRATE-REBIND  <n> claude specs repointed at <exe>
CLI-MIGRATE-PAUSE   <key>
CLI-MIGRATE-RESUME  <key>
CLI-MIGRATE-CHANNEL <channel> floor=<minimumVersion | none>
CLI-MIGRATE-REVERT  <outcome>
CLI-MIGRATE-CLEANUP <outcome | blocked: N alive> exe=<winget path>
```

`REBIND` and the `exe=` on `CLEANUP` are not decoration: both name the thing the
line is ABOUT rather than the thing the app happened to resolve, which is
exactly the confusion §4.1 and §5 introduce. A cleanup that reports the native
path is a cleanup that counted the wrong file.

---

## 7. Consent modal copy

The user asked for this and the instinct is right: AI Hive would be downloading
and executing a script from the internet and changing installed software. State
it, do not soften it.

Required, in this order:

1. **What runs**, verbatim and copyable:
   `powershell -NoProfile -Command "irm https://claude.ai/install.ps1 | iex"`,
   with the plain line: *this downloads and runs Anthropic's official installer
   from claude.ai.*
2. **What changes**: a second copy of Claude Code is installed under
   `%USERPROFILE%\.local\`; from then on Claude Code updates itself in the
   background. No Administrator rights are needed (documented).
3. **What does NOT change**: conversations, settings, logins, MCP config, and
   the existing winget copy, which stays until separately removed and is the
   rollback.
4. **How to undo**, both levels (pause vs full revert), BEFORE they agree.
5. Checkbox **"I understand and agree"**, which is what enables the action
   button.

No em dash anywhere in this copy.

---

## 8. UNVERIFIED, and the checks that settle it

**(a) Is the Windows native launcher a stub or the whole binary?** The docs
describe the launcher-into-`versions/` indirection for macOS and Linux
explicitly and say nothing equivalent for Windows. This decides whether a
background update can apply while agents run. Immediately after the first
successful migration:

```powershell
(Get-Item "$env:USERPROFILE\.local\bin\claude.exe").Length
Get-ChildItem "$env:USERPROFILE\.local\share\claude\versions\"
```

Hundreds of MB (compare: the winget binary is MEASURED at 284,981,920 bytes)
means the launcher IS the binary, a background update cannot replace it while
agents run, and the startup gate is load-bearing. A few KB with the bulk under
`versions\` means updates apply side by side. **Record the answer in
`CLAUDE.md`** under the update-gate invariant either way.

**(b) Does `install.ps1` ever prompt when run non-interactively?** Drive it once
with stdin closed (`subprocess.DEVNULL`, as `cli_update.subprocess_runner`
already does) before trusting the panel with it. A prompt with no stdin would
hang the install thread; the Cancel path must cover it regardless.

---

## 9. Tests (`tests/smoke_test.py`, new section, injected runners only)

No test installs anything, shells out, or touches the real
`~/.claude/settings.json` — use `tempfile` dirs, exactly as the existing
sections do. Follow `_FakeCli` (module level, near `test_cli_auto_update`) for
the recording-runner pattern.

1. `classify_install`: a WinGet-Packages path is WINGET; a `.local\bin` path is
   NATIVE; an npm global path is NPM; an empty path is MISSING.
2. `classify_install` ignores case and resolves a symlink shim to its target.
3. `detect`: winget install with no policy is `MANAGED`.
4. `detect`: native install, no disabling key, is `SELF_ACTIVE`.
5. `detect`: native install with `env.DISABLE_AUTOUPDATER` is `SELF_PAUSED`,
   and `paused_key` names it.
6. `detect`: `DISABLE_UPDATES` also reads as `SELF_PAUSED`.
7. `detect`: an npm install is `NOT_APPLICABLE` and offers nothing.
8. `detect`: managed settings enforcing updates give `LOCKED_BY_POLICY`.
9. `install_argv` is the documented installer line and contains no `winget`.
10. `migrate` decides success by RE-READING the file, so an installer that
    reports rc 0 while the version is unchanged is `ok=False`.
11. `migrate` with a runner that never returns is cancellable and leaves no
    partial state claim.
12. `resolve_claude()` prefers the native launcher even when `shutil.which`
    would return a winget path (the §4.1 trap).
13. `set_paused(True)` writes exactly one key and preserves every other key,
    including unknown ones.
14. `set_paused(False)` restores the original document shape.
15. a `.bak` generation exists after any settings write.
16. an unparseable `settings.json` is REFUSED (`ok=False`), and the file on disk
    is byte-identical afterwards.
17. `set_channel("stable", "2.1.226")` writes `autoUpdatesChannel` AND
    `minimumVersion: "2.1.226"`, and touches nothing else.
18. `set_channel("stable", "")` is REFUSED (`ok=False`) and writes nothing: a
    channel without a floor is the downgrade hole (§3).
19. `set_channel("latest", ...)` REMOVES `minimumVersion` rather than leaving
    the floor behind.
20. `set_channel` never writes `requiredMinimumVersion` (that one stops Claude
    Code starting at all, and is not ours to set).
21. **the lost-update check**: `write_settings` re-reads. Mutate the file on
    disk AFTER the caller's `read_settings` and BEFORE the write (simulating an
    agent's `/model` save); the foreign key must survive and the intended key
    must still land.
22. a re-read that no longer parses aborts the write; the file on disk is
    byte-identical and `ok=False`.
23. the Claude `Target` for a native install is `self_update` and never emits
    the string `winget`.
24. `DB_STALE` is unreachable for a native target even when the runner prints
    "No available upgrade found".
25. **the live-spec rebuild** (§5 step 5): an `AgentSpec` built by `build_spec`
    while `resolve_claude()` returned the WinGet path has its `program` pointed
    at the native launcher after the rebuild pass, with `user_program`,
    `user_args`, `model`, `effort` and `permission_mode` unchanged; a running
    agent is not stopped or restarted; no `dirty` is emitted.
26. cleanup counts against the RECORDED WINGET path, not `resolve_claude()`: a
    live process on the winget binary blocks it even when the resolved binary
    is the native launcher. It issues no kill command, and the Claude DESKTOP
    app's path does not block it.
27. the consent action is unavailable until the checkbox is ticked.
28. no em dash in any panel or modal string.
29. `create_main_window` runs none of this (grep-style check, mirroring the
    existing "create_main_window contains no update work at all" check).

Expect roughly +30 checks. Update `README.md`'s count in both places.

---

## 10. Invariants that must survive

* The startup gate is DEMOTED, not deleted. After migration it no longer keeps
  the user current; it guarantees a downloaded update has LANDED before agents
  launch rather than "the next time you start Claude Code". It stays fully
  load-bearing for `agy`, which has no auto-updater and no native option, and it
  remains the single enforcement point for: the binary changes only when nothing
  is holding it.
* `CLAUDE.md` currently says "Claude deliberately stays on winget: `claude
  update` installs a NATIVE build to a different location and
  `providers.resolve_claude()` has a hardcoded WinGet-Packages fallback, so a
  migration could leave AI Hive silently launching the stale copy." **Rewrite
  that sentence.** The risk is now HANDLED by §4.1 rather than avoided, and left
  as-is the invariant reads as forbidding this feature. Record §8(a)'s answer
  and that `DB_STALE` is unreachable on a native install.
* Update `README.md`: the auto-update feature paragraph, the layout list (two
  new modules), and the check count.
* Transient vs persisted: the detected state is DERIVED every time and never
  stored. Only `ui.auto_update` persists, unchanged. No `SESSION_VERSION` bump.
  `spec.program`/`spec.args` are derived too (`to_dict` stores `user_program`),
  so §5 step 5's rebuild must not emit `dirty`.
* **A channel is never written without its floor** (§3). `stable` on its own can
  move the user onto an older build, which is the stale-alias failure this
  feature exists to end, arrived at through the feature itself.
* **Two paths, and code must always say which one it means.** After §4.1,
  `resolve_claude()` names the NATIVE binary and nothing names the WinGet one.
  Anything still about the WinGet install (cleanup, revert, the rollback
  sentence in the modal) takes that path as a PARAMETER captured before the
  migration. Re-deriving it from the resolved binary is the §5 bug.
* **`~/.claude/settings.json` has other writers, and they are ours.** Every
  running agent's `/model` and `/config` writes it. Any mutation re-reads
  immediately before the replace (§4.3), the same rule `session.json` follows
  and for the same reason.
