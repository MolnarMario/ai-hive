"""How Claude Code is INSTALLED, and the consented switch onto the install
method that updates itself.

Qt-free and stdlib-only, the same class of module as `cli_update.py`,
`chime.py`, `limit_banner.py` and `screen_snapshot.py`: nothing here imports
PySide6, nothing here raises, and every decision is pure given an injected
`Runner` (`argv, timeout -> (rc, output)`, reusing `cli_update.RC_TIMEOUT`).
The real runner is only ever handed in from the GUI layer, exactly like
`start_usage_polling()` and `run_update_gate()`: the offscreen smoke suite
shares `create_main_window` and must never install software, touch the network,
or touch the user's real `~/.claude/settings.json`.

WHY THIS EXISTS
---------------
A package-manager Claude Code does not update itself, and no setting fixes
that. Measured on the reporting machine: Anthropic offered 2.1.226 while the
winget manifest offered 2.1.224 and the installed file was 2.1.224. The lag is
structural (a human publishes each winget manifest) and the CLI's own "Update
available!" banner compares against Anthropic, not against winget, so a fully
upgraded winget install still nags in every terminal. Worse, model aliases
resolve CLIENT-SIDE from a table baked into the installed binary (see
CLAUDE.md), so a stale binary silently cannot launch newer models.

Anthropic documents the two halves of this verbatim:

    Native installations automatically update in the background to keep you on
    the latest version.

    Homebrew, WinGet, apt, dnf, and apk installations do not auto-update by
    default.

The install method IS the behaviour, so changing the behaviour means changing
the install. Hence a migration, not a preference.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
`CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE=1` is not supported anywhere. It makes
a RUNNING Claude Code invoke `winget upgrade` on itself, which is the exact act
that writes the false winget database record `cli_update.py` exists to prevent.
The vendor documents the failure in the same paragraph that offers the flag:
"On WinGet the upgrade may fail while Claude Code is running because Windows
locks the executable."

It also never kills a process. Every blocked path reports and waits, because
killing a `claude.exe` can destroy a transcript.

WHY THE MIGRATION MAY RUN WITH AGENTS ALIVE
-------------------------------------------
The native installer writes to a DIFFERENT directory (`%USERPROFILE%\\.local\\`)
than the winget package (`%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\...`). It
never replaces the running file, so the migration itself is lock-free. That is
the structural advantage over the startup gate, which is confined to the one
moment nothing is running. Only the CLEANUP keeps the old constraint (`winget
uninstall` cannot remove a package whose `.exe` is running), so cleanup is
separate, optional and deferrable and never blocks the win.

TWO PATHS, AND CODE MUST ALWAYS SAY WHICH ONE IT MEANS
------------------------------------------------------
After `providers.resolve_claude()` learned to prefer the native launcher,
nothing in the app names the WinGet binary any more. Anything still ABOUT that
install (cleanup, revert, the rollback sentence in the modal) takes its path as
a PARAMETER captured before the migration, or reads it from `winget_exe_path()`
here. Re-deriving it from the resolved binary is the bug this note exists to
prevent: the count would come back zero because the user's own WinGet-launched
sessions do not match the NATIVE path, and `winget uninstall` would then run
against a file those sessions are holding.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum

from . import cli_update

# Where the two installs live. Both are documented (code.claude.com/docs/en/
# setup): the native installer puts the launcher in `%USERPROFILE%\.local\bin`
# with its payload under `%USERPROFILE%\.local\share\claude\versions`, and the
# uninstall instructions name exactly those two paths.
_NATIVE_BIN = (".local", "bin")
_NATIVE_SHARE = (".local", "share", "claude")

# The winget package directory, same hardcoded shape `providers.resolve_claude`
# has always carried as its fallback.
_WINGET_PACKAGES = r"Microsoft\WinGet\Packages"
_WINGET_DIR_PREFIX = "Anthropic.ClaudeCode_"

# Managed (organisation policy) settings. Read from the documented locations,
# NOT guessed: code.claude.com/docs/en/settings#settings-files names the Windows
# file as `C:\Program Files\ClaudeCode\managed-settings.json` with a drop-in
# directory `managed-settings.d\` beside it, plus the Group Policy registry keys
# `HKLM\SOFTWARE\Policies\ClaudeCode` and `HKCU\SOFTWARE\Policies\ClaudeCode`
# (value `Settings`, JSON). The legacy `C:\ProgramData\ClaudeCode\` path is
# documented as unsupported since v2.1.75 and is deliberately not read.
_MANAGED_DIR_NAME = "ClaudeCode"
_MANAGED_FILE = "managed-settings.json"
_MANAGED_DROPIN = "managed-settings.d"
_POLICY_KEY = r"SOFTWARE\Policies\ClaudeCode"
_POLICY_VALUE = "Settings"

# The two env keys that stop a native install updating itself. DISABLE_AUTOUPDATER
# stops only the background check (`claude update` still works); DISABLE_UPDATES
# blocks every update path. Both live under the `env` object in settings.json.
PAUSE_KEY = "DISABLE_AUTOUPDATER"
PAUSE_KEY_STRICT = "DISABLE_UPDATES"
_PAUSE_KEYS = (PAUSE_KEY_STRICT, PAUSE_KEY)   # strict first: it is the stronger

# Top-level settings keys this module may write. `minimumVersion` constrains
# UPDATES only. `requiredMinimumVersion`/`requiredMaximumVersion` stop Claude
# Code STARTING at all and are managed-settings policy, so they are never
# written here, and a test asserts it.
CHANNEL_KEY = "autoUpdatesChannel"
FLOOR_KEY = "minimumVersion"
CHANNELS = ("latest", "stable")

# Settings keys that mean an administrator has taken update behaviour out of
# the user's hands. Any of these in a managed document makes the control inert.
_POLICY_UPDATE_KEYS = (CHANNEL_KEY, FLOOR_KEY, "requiredMinimumVersion",
                       "requiredMaximumVersion")

# the documented installer, and nothing else
INSTALL_URL = "https://claude.ai/install.ps1"
INSTALL_COMMAND = 'powershell -NoProfile -Command "irm %s | iex"' % INSTALL_URL


class InstallKind(str, Enum):
    """Which installer owns the resolved binary. `str` mixin so a kind survives
    being logged or compared against a plain string."""

    WINGET = "winget"
    NATIVE = "native"
    NPM = "npm"
    UNKNOWN = "unknown"
    MISSING = "missing"


class UpdateState(str, Enum):
    """What the one control offers, which is a function of the machine and
    never a boolean. A user is never shown an action that does not apply."""

    MANAGED = "managed"                  # a package manager owns it: migrate
    SELF_ACTIVE = "self-active"          # native, updating itself: pause
    SELF_PAUSED = "self-paused"          # native, pinned by an env key: resume
    NOT_APPLICABLE = "not-applicable"    # npm already auto-updates
    LOCKED_BY_POLICY = "locked-by-policy"
    MISSING = "missing"


# The states in which the control does anything at all.
ACTIONABLE_STATES = (UpdateState.MANAGED, UpdateState.SELF_ACTIVE,
                     UpdateState.SELF_PAUSED)


@dataclass(frozen=True)
class Situation:
    """The DERIVED state of the machine. Never persisted, in `session.json` or
    anywhere else: a stored install method goes stale the moment the user
    installs something by hand, exactly like a stored usage reading."""

    state: UpdateState
    kind: InstallKind
    exe: str = ""           # the resolved binary this describes
    version: str = ""       # "" when unread
    channel: str = ""       # "" | "latest" | "stable"
    paused_key: str = ""    # "" | "DISABLE_AUTOUPDATER" | "DISABLE_UPDATES"
    floor: str = ""         # minimumVersion, "" when unset
    detail: str = ""        # ONE sentence for the panel, user-facing

    def actionable(self) -> bool:
        return self.state in ACTIONABLE_STATES


@dataclass(frozen=True)
class Result:
    """The outcome of one act. Every entry point returns one of these rather
    than raising, so a caller can always report something."""

    ok: bool
    action: str          # migrate|pause|resume|channel|revert|cleanup|rebind
    before: str = ""
    after: str = ""
    detail: str = ""
    path_added: bool = False   # migrate only: see `ensure_native_on_path`


# ------------------------------------------------------------------ paths ---

def _home() -> str:
    return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def native_launcher_path() -> str:
    """`%USERPROFILE%\\.local\\bin\\claude.exe`, whether or not it exists."""
    return os.path.join(_home(), *_NATIVE_BIN, "claude.exe")


def native_share_dir() -> str:
    """`%USERPROFILE%\\.local\\share\\claude`, the payload directory the
    documented uninstall removes."""
    return os.path.join(_home(), *_NATIVE_SHARE)


def native_versions_dir() -> str:
    return os.path.join(native_share_dir(), "versions")


def settings_path() -> str:
    """The user's own `~/.claude/settings.json`. AI Hive has NEVER written this
    file before (it uses its own `--settings` file for hooks), so every writer
    here re-reads immediately before replacing, see `write_settings`."""
    return os.path.join(_home(), ".claude", "settings.json")


def managed_settings_dir() -> str:
    """`C:\\Program Files\\ClaudeCode`, per the documented Windows location."""
    base = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return os.path.join(base, _MANAGED_DIR_NAME)


def managed_settings_path() -> str:
    return os.path.join(managed_settings_dir(), _MANAGED_FILE)


def winget_exe_path() -> str:
    """Where the WinGet copy lives, INDEPENDENT of what `resolve_claude()`
    answers.

    Needed because once the native launcher exists, `resolve_claude()` names
    that one and nothing else in the app can name this file, while cleanup and
    revert are the two operations still about it. "" when there is no winget
    install to name."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), _WINGET_PACKAGES)
    guess = os.path.join(
        base,
        _WINGET_DIR_PREFIX + "Microsoft.Winget.Source_8wekyb3d8bbwe",
        "claude.exe")
    if os.path.isfile(guess):
        return guess
    # the source suffix is not ours to predict forever, so fall back to any
    # Anthropic.ClaudeCode_* package directory rather than giving up
    try:
        for name in sorted(os.listdir(base)):
            if not name.startswith(_WINGET_DIR_PREFIX):
                continue
            candidate = os.path.join(base, name, "claude.exe")
            if os.path.isfile(candidate):
                return candidate
    except OSError:
        pass
    return ""


def _compute_updated_path(current: str, target: str) -> str | None:
    """Pure decision for `ensure_native_on_path`: the PATH string to write, or
    `None` when `target` is already present and nothing should change.

    Split out so this can be unit tested without touching the real registry
    (the offscreen smoke suite must never do that, same rule as every other
    injected side effect in this module). Comparison is case-insensitive with
    a trailing backslash stripped on both sides, because Windows PATH lookups
    are case-insensitive and a user or installer may have written the entry
    either way."""
    entries = [p for p in current.split(";") if p]
    norm_target = os.path.normcase(target.rstrip("\\"))
    if any(os.path.normcase(p.rstrip("\\")) == norm_target for p in entries):
        return None
    return (current.rstrip(";") + ";" + target) if current else target


def ensure_native_on_path() -> bool:
    """Append `%USERPROFILE%\\.local\\bin` to the User PATH, so a PLAIN
    terminal (not just AI Hive) resolves the current `claude` after a
    migration.

    `resolve_claude()` never needed this — it checks the native launcher path
    directly, on purpose, so AI Hive's own launches never depended on the
    installer's PATH edit. But a user's own terminal only has `shutil.which`,
    which only finds what PATH lists, and MEASURED on the reporting machine:
    the native installer did not put its own directory on PATH at all. Two
    concrete symptoms follow from that, for the two shapes of migrated
    machine: with BOTH installs present, a user kept typing `claude` into the
    OLD winget copy after migrating (stale version, silently) until the
    winget copy was removed by `cleanup()`, and with ONLY the native install
    (no winget fallback to fall back to), a bare `claude` in an ordinary
    terminal did not resolve AT ALL.

    Idempotent and additive-only, mirroring `write_settings`'s own rule for
    `~/.claude/settings.json`: the CURRENT registry value is read immediately
    before writing (another installer could have touched PATH since AI Hive
    last looked), and the directory is appended only when `_compute_updated_
    path` says it is not already there. Only the User environment key is
    touched, never Machine (that needs Administrator, and a native install is
    a per-user install that does not need it either). Broadcasts
    WM_SETTINGCHANGE afterwards so already-open Explorer-launched processes
    pick it up without a reboot; a terminal that was already open when this
    runs still needs to be reopened regardless, like any other PATH change.

    Never raises (every failure, including off-Windows, is a silent no-op:
    this is a nicety, not something that may take the migration down with
    it — see the `path_updater` try/except in `migrate()`). Returns True only
    when it actually changed the registry."""
    try:
        import winreg  # noqa: PLC0415 - Windows only, and optional
    except ImportError:
        return False
    target = os.path.join(_home(), *_NATIVE_BIN)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            updated = _compute_updated_path(current, target)
            if updated is None:
                return False
            winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ,
                              updated)
    except OSError:
        return False
    _broadcast_environment_change()
    return True


def _broadcast_environment_change() -> None:
    """Tell already-running processes PATH changed, the way installers do.
    Purely a nicety (a new terminal picks up the registry change anyway with
    or without this), so any failure here is swallowed."""
    try:
        import ctypes  # noqa: PLC0415 - Windows only
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.create_unicode_buffer("Environment"),
            0x0002, 5000, ctypes.byref(result))
    except Exception:  # noqa: BLE001 - never let a broadcast fail anything
        pass


def classify_install(exe: str) -> InstallKind:
    """Which installer owns `exe`, decided on the PATH.

    Nothing else distinguishes them: the binary, the version string and the
    process name are identical across install methods. Canonicalised through
    `cli_update._canonical` (`realpath` + `normcase`) rather than compared as a
    string, because winget also installs a symlink shim and a path reached
    through it spells the same file differently."""
    if not exe:
        return InstallKind.MISSING
    path = cli_update._canonical(exe)
    if os.path.normcase(_WINGET_PACKAGES) in path:
        return InstallKind.WINGET
    if os.path.normcase(os.path.join(*_NATIVE_BIN)) in path:
        return InstallKind.NATIVE
    if os.path.normcase("node_modules") in path or \
            os.path.normcase(os.sep + "npm" + os.sep) in path:
        return InstallKind.NPM
    return InstallKind.UNKNOWN if os.path.isfile(exe) else InstallKind.MISSING


def _image_name(exe: str) -> str:
    name = os.path.basename(exe or "")
    if not name:
        return "claude.exe"
    return name if name.lower().endswith(".exe") else name + ".exe"


# --------------------------------------------------------------- settings ---

def read_settings(path: str) -> tuple:
    """`(document, reason)`. A reason means REFUSE: the file is there and does
    not parse, so nothing may be written over it.

    A file that simply does not exist is not a refusal, it is an empty
    document: the first pause or channel write creates it. Read with
    `utf-8-sig` for the same BOM tolerance `SessionStore.load` has, because a
    hand edit through PowerShell writes one."""
    if not path:
        return {}, "no settings path"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        return {}, f"could not read {os.path.basename(path)}: {e}"
    except ValueError as e:
        return {}, f"{os.path.basename(path)} is not valid JSON: {e}"
    if not isinstance(data, dict):
        return {}, f"{os.path.basename(path)} is not a JSON object"
    return data, ""


def write_settings(path: str, mutate, action: str = "settings") -> Result:
    """Apply `mutate(document)` to `path` and replace it atomically.

    IT TAKES A MUTATION, NOT A FINISHED DOCUMENT, and that is the whole point.
    `~/.claude/settings.json` HAS OTHER WRITERS AND THEY ARE OURS: every running
    agent's `/model` ("saved as your default for new sessions") and `/config`
    (the update channel) writes this file, and a hive can have a dozen agents in
    which either can happen at any moment. The document read in `detect()` is
    then shown behind a modal the user may sit on for a minute, so writing THAT
    copy back would replay a minute-old file over whatever an agent saved in
    between and silently discard it. Atomicity stops a torn file; it does
    nothing about a lost update. So the read happens HERE, immediately before
    the replace, and the mutation is applied to that copy. This is CLAUDE.md's
    own rule for `session.json` ("ALWAYS copy the live file aside and re-read it
    immediately before writing"), which is there because a hand edit based on
    stale forensics once destroyed a user's freshly created agents.

    A re-read that no longer parses ABORTS. It never falls back to an earlier
    copy and never "repairs" the file: returning `ok=False` with a reason is the
    correct outcome for a document we cannot safely replace.

    The JSON is re-serialised at 2-space indent, so whitespace is normalised;
    every key, its value and its ORDER are preserved, including keys this module
    knows nothing about."""
    if not path:
        return Result(False, action, detail="no settings path")
    data, reason = read_settings(path)
    if reason:
        return Result(False, action, detail=reason)
    try:
        detail = mutate(data) or ""
    except Exception as e:  # noqa: BLE001 - a mutation must never crash the app
        return Result(False, action, detail=f"{type(e).__name__}: {e}")
    try:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp",
                                       dir=folder)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        # one rolling generation, the discipline SessionStore.save uses: a copy
        # rather than a rename, so a crash between the two steps can never leave
        # NO settings.json at all
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        return Result(False, action, detail=f"{type(e).__name__}: {e}")
    return Result(True, action, detail=detail)


def _env_object(doc: dict) -> dict:
    env = doc.get("env")
    return env if isinstance(env, dict) else {}


def _is_on(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() not in ("", "0", "false", "no")


def paused_key_in(doc: dict) -> str:
    """Which env key, if any, is currently holding updates back."""
    env = _env_object(doc)
    for key in _PAUSE_KEYS:
        if _is_on(env.get(key)):
            return key
    return ""


def set_paused(path: str, on: bool, strict: bool = False) -> Result:
    """Pause or resume the native install's background updater.

    This is the PRIMARY undo for the migration: one key, instant, no download,
    no reinstall, and it freezes the user on exactly the version they have.
    Turning it back off removes BOTH keys, because either one could have been
    what paused them (a user may have set `DISABLE_UPDATES` by hand), and a
    "resume" that left the stronger key in place would do nothing while
    reporting success. An `env` object this created and then emptied is removed
    again, so a pause followed by a resume restores the document's shape."""
    key = PAUSE_KEY_STRICT if strict else PAUSE_KEY
    action = "pause" if on else "resume"

    def mutate(doc: dict) -> str:
        env = doc.get("env")
        if not isinstance(env, dict):
            env = {}
        if on:
            env[key] = "1"
        else:
            for name in _PAUSE_KEYS:
                env.pop(name, None)
        if env:
            doc["env"] = env
        else:
            doc.pop("env", None)
        return key

    result = write_settings(path, mutate, action)
    return Result(result.ok, action, after=key if result.ok else "",
                  detail=result.detail if not result.ok else "")


def set_channel(path: str, channel: str, installed_version: str = "") -> Result:
    """Write the release channel AND its floor, mirroring what `/config` does.

    THE CHANNEL IS NEVER WRITTEN ALONE. `stable` is a channel, not a ceiling, so
    setting it on a machine that is AHEAD of stable lets the next update move
    the user BACKWARDS onto an older build. That is not cosmetic: it is exactly
    the stale-binary state this whole feature exists to end, because model
    aliases resolve from a table baked into the installed file (CLAUDE.md), so
    an older build silently cannot launch newer models. The vendor's own control
    has no such hole: "Switching from `latest` to `stable` via `/config` prompts
    you to either stay on the current version or allow the downgrade. Choosing
    to stay sets `minimumVersion` to that version. Switching back to `latest`
    clears it."

    So a version that cannot be parsed means the floor cannot be established,
    and the channel is REFUSED rather than written without it. Going back to
    `latest` REMOVES the floor, so the pin cannot outlive the reason for it.

    `minimumVersion` constrains updates only. `requiredMinimumVersion` stops
    Claude Code starting at all and is never written here."""
    channel = (channel or "").strip().lower()
    if channel not in CHANNELS:
        return Result(False, "channel",
                      detail=f"unknown release channel {channel or '(empty)'}")
    floor = ""
    if channel == "stable":
        floor = cli_update.parse_version(installed_version)
        if not floor:
            return Result(
                False, "channel", after=channel,
                detail="the installed version could not be read, so there is "
                       "no floor to stop the stable channel moving you onto an "
                       "older build. Nothing was written.")

    def mutate(doc: dict) -> str:
        doc[CHANNEL_KEY] = channel
        if floor:
            doc[FLOOR_KEY] = floor
        else:
            doc.pop(FLOOR_KEY, None)
        return floor

    result = write_settings(path, mutate, "channel")
    return Result(result.ok, "channel", before=floor if result.ok else "",
                  after=channel,
                  detail=result.detail if not result.ok else "")


# --------------------------------------------------------------- policies ---

def registry_policies() -> list:
    """Managed settings delivered by Group Policy, read-only.

    `HKLM\\SOFTWARE\\Policies\\ClaudeCode` then the user-level
    `HKCU\\SOFTWARE\\Policies\\ClaudeCode`, each carrying JSON in a `Settings`
    value. Never raises and answers with an empty list off Windows."""
    docs = []
    try:
        import winreg  # noqa: PLC0415 - Windows only, and optional
    except ImportError:
        return docs
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, _POLICY_KEY) as key:
                raw, _kind = winreg.QueryValueEx(key, _POLICY_VALUE)
            doc = json.loads(os.path.expandvars(str(raw)))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def managed_policies(managed_path: str = "") -> list:
    """Every file-based managed document, base file first then the drop-in
    directory alphabetically, which is the documented merge order."""
    path = managed_path or managed_settings_path()
    docs = []
    doc, reason = read_settings(path)
    if doc and not reason:
        docs.append(doc)
    dropin = os.path.join(os.path.dirname(path) or ".", _MANAGED_DROPIN)
    try:
        names = sorted(n for n in os.listdir(dropin)
                       if n.lower().endswith(".json") and not n.startswith("."))
    except OSError:
        names = []
    for name in names:
        doc, reason = read_settings(os.path.join(dropin, name))
        if doc and not reason:
            docs.append(doc)
    return docs


def policy_reason(docs) -> str:
    """One sentence naming the policy that owns update behaviour, or "".

    Overriding an organisation's policy is not ours to do, so any of these
    makes the control inert rather than merely awkward."""
    for doc in docs or ():
        if not isinstance(doc, dict):
            continue
        for key in _POLICY_UPDATE_KEYS:
            if key in doc:
                return (f"Your organisation's managed settings set {key}, so "
                        f"update behaviour is not yours to change here.")
        env = _env_object(doc)
        for key in _PAUSE_KEYS:
            if key in env:
                return (f"Your organisation's managed settings set {key}, so "
                        f"update behaviour is not yours to change here.")
    return ""


# ---------------------------------------------------------------- detect ---

_DETAIL = {
    UpdateState.MANAGED:
        "Claude Code was installed by WinGet, which does not update it. You "
        "keep whatever version is installed until someone upgrades it by hand.",
    UpdateState.SELF_ACTIVE:
        "Claude Code is on the native install and updates itself in the "
        "background.",
    UpdateState.SELF_PAUSED:
        "Claude Code is on the native install, but background updates are "
        "switched off, so it stays on this version.",
    UpdateState.NOT_APPLICABLE:
        "Claude Code was installed with npm, which already updates itself. "
        "There is nothing to change here.",
    UpdateState.MISSING:
        "No Claude Code install was found, so there is nothing to update.",
}


def detect(exe: str, settings_file: str = "", managed_path: str = "",
           runner=None, policies=None) -> Situation:
    """Read the machine and answer with the ONE state the control acts on.

    Pure apart from reading files and, only when a `runner` is given, one
    `--version`. Nothing here is cached or stored: the state is derived on every
    read, because a remembered install method is wrong the moment the user
    installs anything by hand."""
    kind = classify_install(exe)
    version = ""
    if runner is not None and exe:
        rc, text = runner([exe, "--version"], cli_update.CHECK_TIMEOUT_S)
        if rc == 0:
            version = cli_update.parse_version(text)

    doc, _reason = read_settings(settings_file or settings_path())
    channel = doc.get(CHANNEL_KEY)
    channel = channel.strip().lower() if isinstance(channel, str) else ""
    floor = doc.get(FLOOR_KEY)
    floor = floor.strip() if isinstance(floor, str) else ""
    paused = paused_key_in(doc)

    docs = list(policies) if policies is not None else \
        managed_policies(managed_path) + registry_policies()
    locked = policy_reason(docs)

    if locked:
        state, detail = UpdateState.LOCKED_BY_POLICY, locked
    elif kind is InstallKind.MISSING:
        state, detail = UpdateState.MISSING, _DETAIL[UpdateState.MISSING]
    elif kind is InstallKind.NPM:
        state = UpdateState.NOT_APPLICABLE
        detail = _DETAIL[UpdateState.NOT_APPLICABLE]
    elif kind is InstallKind.NATIVE:
        state = UpdateState.SELF_PAUSED if paused else UpdateState.SELF_ACTIVE
        detail = _DETAIL[state]
    else:
        # WINGET, and UNKNOWN with it: an install method we cannot name is not
        # known to update itself, and the migration is safe for either one. It
        # installs a SECOND copy in its own directory and leaves the existing
        # one alone, which is also the rollback.
        state = UpdateState.MANAGED
        detail = _DETAIL[UpdateState.MANAGED] if kind is InstallKind.WINGET \
            else ("Claude Code was installed by something AI Hive does not "
                  "recognise, so it may not update itself.")
    return Situation(state=state, kind=kind, exe=exe or "", version=version,
                     channel=channel, paused_key=paused, floor=floor,
                     detail=detail)


# ------------------------------------------------------------------ argv ---

def install_argv() -> list:
    """The documented installer, and nothing else.

    No Administrator rights are needed (documented). VERIFIED by reading the
    script rather than running it (111 lines, 2026-08-11): it contains NO
    interactive construct at all, so it cannot park the install thread on a
    prompt. It resolves `latest`, rejects a version string that is not `N.N.N`
    (an HTML error page), downloads the platform binary, verifies its SHA256
    against the SIGNED manifest, then shells to `<downloaded>.exe install`;
    every failure path writes an error and exits non-zero. `subprocess_runner`
    closes stdin anyway, so an unanticipated prompt in a future version EOFs
    instead of hanging, and the panel's Close is the escape hatch either way."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"irm {INSTALL_URL} | iex"]


def winget_install_argv() -> list:
    return [cli_update.WINGET, "install", "--id", cli_update.CLAUDE_WINGET_ID,
            "--exact", "--accept-source-agreements",
            "--accept-package-agreements"]


def winget_uninstall_argv() -> list:
    return [cli_update.WINGET, "uninstall", "--id",
            cli_update.CLAUDE_WINGET_ID, "--exact"]


# ----------------------------------------------------------------- verbs ---

def migrate(runner, on_event=None, timeout: float = cli_update.INSTALL_TIMEOUT_S,
            launcher: str = "", before: str = "", path_updater=None) -> Result:
    """Install the native build, then decide success by READING THE FILE.

    Never by the installer's report: that is the rule the whole `cli_update`
    module is built on, and an installer that reports success while the file did
    not change is a FAILURE. The install itself is never killed on a timer (a
    half-written 285 MB binary is worse than a nag banner); the panel's Cancel
    is the escape hatch, matching the startup gate's install phase.

    `before` is the version of the OUTGOING install, so a native build older
    than what the user already had is refused rather than announced.

    `path_updater`, when given, runs ONCE after a successful install and is
    `ensure_native_on_path`. It is INJECTED exactly like `runner` — the
    offscreen smoke suite calls `migrate()` directly and must never touch the
    real Windows User PATH registry key, so the default (`None`) is a no-op
    and only `update_panel.py`'s live caller passes the real function. See
    `ensure_native_on_path` for why this exists at all: `resolve_claude()`
    never needed PATH, but a user's own terminal does."""
    launcher = launcher or native_launcher_path()
    _emit(on_event, "install", INSTALL_COMMAND)
    rc, text = runner(install_argv(), timeout)
    _emit(on_event, "output", text or "")

    vrc, vtext = runner([launcher, "--version"], cli_update.CHECK_TIMEOUT_S)
    after = cli_update.parse_version(vtext) if vrc == 0 else ""
    if not after:
        return Result(False, "migrate", before=before,
                      detail=_install_failure(rc, text, launcher))
    if before and cli_update.version_tuple(after) < \
            cli_update.version_tuple(before):
        return Result(False, "migrate", before=before, after=after,
                      detail=f"the native install is {after}, older than the "
                             f"{before} you already had, so it was not adopted")
    path_added = False
    if path_updater is not None:
        try:
            path_added = bool(path_updater())
        except Exception:  # noqa: BLE001 - a PATH nicety must never fail the migration
            path_added = False
    return Result(True, "migrate", before=before, after=after,
                  path_added=path_added)


def _install_failure(rc: int, text, launcher: str) -> str:
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip()[:160]
            break
    if not os.path.isfile(launcher):
        return (f"the installer finished (rc={rc}) but there is no program at "
                f"{launcher}. {first}").strip()
    return (f"the installer finished (rc={rc}) but {os.path.basename(launcher)} "
            f"could not report a version. {first}").strip()


def cleanup(runner, winget_exe: str,
            timeout: float = cli_update.INSTALL_TIMEOUT_S) -> Result:
    """Remove the old WinGet package, once nothing is running it.

    `winget_exe` is a PARAMETER and must be the path recorded BEFORE the
    migration, never `providers.resolve_claude()`. By cleanup time that function
    answers with the NATIVE launcher, so counting against it asks a question
    nobody asked: the user's own WinGet-launched sessions do not match it, the
    count comes back zero, and `winget uninstall` runs against a file those
    sessions are holding. That is under-counting, the direction
    `cli_update.count_processes` documents as the unsafe one, and here it is
    unsafe twice over: it either fails and poisons the package database again,
    or it removes the rollback out from under a live session.

    A live CLI blocks and is NEVER killed. Killing one can destroy a
    transcript, and these are the user's own sessions."""
    if not winget_exe:
        return Result(False, "cleanup",
                      detail="there is no WinGet install left to remove")
    alive = cli_update.count_processes((_image_name(winget_exe),), runner,
                                       cli_update.CHECK_TIMEOUT_S,
                                       exe=winget_exe)
    if alive != 0:
        return Result(False, "cleanup", after=winget_exe,
                      detail=_alive_detail(alive))
    rc, text = runner(winget_uninstall_argv(), timeout)
    if os.path.isfile(winget_exe):
        return Result(False, "cleanup", after=winget_exe,
                      detail=f"the file is still there after the uninstall "
                             f"(rc={rc})")
    return Result(True, "cleanup", after=winget_exe,
                  detail=text.strip().splitlines()[0][:160] if text.strip()
                  else "")


def _alive_detail(alive: int) -> str:
    if alive == cli_update.UNKNOWN_PROCESSES:
        return ("AI Hive could not tell whether the old Claude Code is in use, "
                "so it left it alone. Try again later.")
    return (f"{alive} Claude Code session is still using the old install, so "
            f"it was left alone. Close it and try again."
            if alive == 1 else
            f"{alive} Claude Code sessions are still using the old install, so "
            f"it was left alone. Close them and try again.")


def revert(runner, on_event=None,
           timeout: float = cli_update.INSTALL_TIMEOUT_S) -> Result:
    """The FULL undo: put the WinGet package back, then remove the native one.

    Deliberately secondary to pause/resume in the panel, because it is a second
    install operation rather than a toggle, and conflating the two undos in one
    click is how a user who wanted to pause ends up reinstalling.

    Order matters and is not interchangeable: the WinGet copy is installed and
    VERIFIED FIRST, so a failed reinstall leaves the user with the working
    native install rather than with nothing. The native files come off only
    after that, and only when nothing is running them."""
    launcher = native_launcher_path()
    alive = cli_update.count_processes((_image_name(launcher),), runner,
                                       cli_update.CHECK_TIMEOUT_S, exe=launcher)
    if alive != 0:
        return Result(False, "revert", detail=_alive_detail(alive))

    _emit(on_event, "install", " ".join(winget_install_argv()))
    rc, text = runner(winget_install_argv(), timeout)
    _emit(on_event, "output", text or "")
    restored = winget_exe_path()
    if not restored:
        return Result(False, "revert",
                      detail=f"the WinGet install did not come back (rc={rc}), "
                             f"so the native install was left in place")
    vrc, vtext = runner([restored, "--version"], cli_update.CHECK_TIMEOUT_S)
    after = cli_update.parse_version(vtext) if vrc == 0 else ""
    if not after:
        return Result(False, "revert",
                      detail="the restored WinGet copy could not report a "
                             "version, so the native install was left in place")

    removed, problem = _remove_native()
    if problem:
        return Result(False, "revert", after=after,
                      detail=f"WinGet Claude Code {after} is back, but the "
                             f"native install could not be removed: {problem}")
    return Result(True, "revert", after=after, detail=removed)


def _remove_native() -> tuple:
    """Delete the documented native install (`~\\.local\\bin\\claude.exe` and
    `~\\.local\\share\\claude`). Returns (what was removed, problem)."""
    removed = []
    launcher, share = native_launcher_path(), native_share_dir()
    try:
        if os.path.isfile(launcher):
            os.remove(launcher)
            removed.append(launcher)
        if os.path.isdir(share):
            shutil.rmtree(share)
            removed.append(share)
    except OSError as e:
        return ", ".join(removed), str(e)
    return ", ".join(removed), ""


def _emit(on_event, kind: str, payload) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception:  # noqa: BLE001 - progress reporting must never break this
        pass


# ----------------------------------------------------------------- audit ---
# Same reasoning as the gate's UPDATE-* lines: this feature will one day be
# debugged from a log line, months later. REBIND and the exe= on CLEANUP are not
# decoration, they name the thing the line is ABOUT rather than the thing the
# app happened to resolve, which is exactly the confusion two install paths
# introduce. A cleanup that reports the native path is a cleanup that counted
# the wrong file.

def state_line(situation: Situation) -> str:
    return (f"CLI-MIGRATE-STATE {situation.state.value} "
            f"kind={situation.kind.value} "
            f"version={situation.version or '?'} exe={situation.exe or '?'}")


def audit_lines(result: Result) -> list:
    if result is None:
        return []
    action, detail = result.action, result.detail or ""
    if action == "migrate":
        if result.ok:
            lines = [f"CLI-MIGRATE-OK {result.before or '?'} -> {result.after}"]
            if result.path_added:
                lines.append("CLI-MIGRATE-PATH added .local\\bin to the User "
                             "PATH")
            return lines
        return [f"CLI-MIGRATE-FAIL {detail}"]
    if action == "pause":
        return [f"CLI-MIGRATE-PAUSE {result.after or detail}"]
    if action == "resume":
        return [f"CLI-MIGRATE-RESUME {result.after or detail}"]
    if action == "channel":
        return [f"CLI-MIGRATE-CHANNEL {result.after} "
                f"floor={result.before or 'none'}"]
    if action == "revert":
        return [f"CLI-MIGRATE-REVERT {'ok ' + result.after if result.ok else detail}"]
    if action == "cleanup":
        outcome = "ok" if result.ok else f"blocked: {detail}"
        return [f"CLI-MIGRATE-CLEANUP {outcome} exe={result.after or '?'}"]
    if action == "rebind":
        return [f"CLI-MIGRATE-REBIND {result.before} claude specs repointed "
                f"at {result.after}"]
    return []
