"""AI-agent provider registry.

Each provider maps to a CLI. Claude Code is fully wired — its real
`--model` / `--effort` flags (verified on this machine: effort tokens are
low|medium|high|xhigh|max; model aliases include opus|sonnet|haiku|fable
and opusplan, and full ids name the older models).
Other providers are editable command templates that only launch once their
CLI is installed; until then the dialog shows them as "not detected".

This module is Qt-free (pure data + os/shutil helpers) so the model layer
and headless tests can use it without a GUI.
"""

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field

DEFAULT_MODEL = ""   # empty = omit the flag, use the CLI's own default
DEFAULT_EFFORT = ""

# cache for _claude_user_defaults: (mtime, size, model, effort)
_USER_DEFAULTS_CACHE: tuple = (0.0, -1, "", "")

# resolve_claude's PATH lookup: (the PATH it searched, the file it found).
# One `shutil.which` is about 5 ms and every Claude spec built at load asks
# for it, so the answer is kept for the launch. A miss is never kept, so a
# CLI installed by hand while AI Hive runs is still found. forget_claude()
# drops it.
_CLAUDE_ON_PATH: tuple = ("", "")


@dataclass(frozen=True)
class Provider:
    key: str
    display: str
    exe_names: tuple            # for shutil.which detection
    models: tuple              # ((label, value), ...); value "" = default
    efforts: tuple = ()        # effort tokens (Claude only); "" prepended = default
    # ((label, value), ...) startup permission modes (Claude only). value "" =
    # launch with no --permission-mode flag (the CLI's own default), which is
    # exactly today's behavior. These mirror the modes the interactive TUI
    # cycles through with Shift+Tab, so a spawned agent can start pre-set.
    permission_modes: tuple = ()
    # every token the CLI's own --permission-mode actually accepts. A SUPERSET
    # of the dropdown above, because a mode read back off a live conversation
    # can be one the dialog never offers (the TUI's Shift+Tab cycle sets modes
    # of its own). Empty = only the dropdown values are launchable.
    cli_permission_modes: tuple = ()
    native_flags: bool = False  # True → --model/--effort (Claude); else template
    base_cmd: str = ""         # template providers, e.g. "codex"
    model_flag: str = ""       # template, e.g. "--model {model}" / "-m {model}"
    fallback_paths: tuple = ()  # known install paths (env vars expanded) —
    # the app may run with a PATH older than the CLI's install
    note: str = ""


# providers whose CLI can resume its previous conversation with --continue
# (claude verified 2.1.197; agy verified 1.0.16; grok verified 0.2.93)
RESUME_PROVIDERS = ("claude", "gemini", "grok")


# The models claude's own /model menu lists, in its order (claude 2.1.309).
# The newest of each family goes by the CLI's alias, which resolves
# client-side to the newest model the installed CLI knows; the version in its
# label is what the alias meant when this list was last refreshed, so update
# the labels when a new model ships. Older models go by full id. There are no
# "[1m]" entries: the fable, opus and sonnet aliases resolve to models with a
# native 1M window, so "opus[1m]" launched the same model as "opus", and
# claude's menu offers no 1M variant of Opus 4.6 or Sonnet 4.6 either. "opusplan" plans with Opus
# and executes with Sonnet; the menu doesn't list it but --model takes it.
CLAUDE_MODELS = (
    ("Default", ""),
    ("Opus 5.5", "opus"),
    ("Sonnet 5.5", "sonnet"),
    ("Fable 5.1", "fable"),
    ("Haiku 5.5", "haiku"),
    ("Haiku 4.5", "claude-haiku-4-5"),
    ("Sonnet 5", "claude-sonnet-5"),
    ("Opus 5", "claude-opus-5"),
    ("Fable 5", "claude-fable-5"),
    ("Opus 4.8", "claude-opus-4-8"),
    ("Opus 4.7", "claude-opus-4-7"),
    ("Opus 4.6", "claude-opus-4-6"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Opus Plan (Opus plans, Sonnet executes)", "opusplan"),
)
CLAUDE_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")

GEMINI_MODELS = (
    ("Default", ""),
    ("Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (High)"),
    ("Gemini 3.8 Flash (Medium)", "Gemini 3.8 Flash (Medium)"),
    ("Gemini 3.8 Flash (Low)", "Gemini 3.8 Flash (Low)"),
    ("Gemini 3.7 Flash (High)", "Gemini 3.7 Flash (High)"),
    ("Gemini 3.7 Flash (Medium)", "Gemini 3.7 Flash (Medium)"),
    ("Gemini 3.7 Flash (Low)", "Gemini 3.7 Flash (Low)"),
    ("Gemini 3.6 Flash (High)", "Gemini 3.6 Flash (High)"),
    ("Gemini 3.6 Flash (Medium)", "Gemini 3.6 Flash (Medium)"),
    ("Gemini 3.6 Flash (Low)", "Gemini 3.6 Flash (Low)"),
    ("Gemini 3.1 Pro (High)", "Gemini 3.1 Pro (High)"),
    ("Gemini 3.1 Pro (Low)", "Gemini 3.1 Pro (Low)"),
    ("Claude Sonnet 4.6 (Thinking)", "Claude Sonnet 4.6 (Thinking)"),
    ("Claude Opus 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)"),
    ("GPT-OSS 120B (Medium)", "GPT-OSS 120B (Medium)"),
)
GEMINI_EFFORTS = ("", "low", "medium", "high")
# The startup permission modes the interactive TUI cycles through with
# Shift+Tab. "" launches with NO --permission-mode flag (the CLI's own default,
# i.e. today's behavior) and is the dialog default. acceptEdits/plan are the
# other two steps of the ordinary Shift+Tab cycle; bypassPermissions is the
# opt-in mode that only appears in the cycle once launched with it. (Verified
# against `claude --permission-mode` choices on 2.1.x: acceptEdits, auto,
# bypassPermissions, manual, dontAsk, plan — note there is NO "default" token,
# so "Normal" must OMIT the flag rather than pass one.)
CLAUDE_PERMISSION_MODES = (
    ("Normal", ""),
    ("Accept edits (auto-approve file edits)", "acceptEdits"),
    ("Plan mode (read-only until you approve)", "plan"),
    ("Bypass permissions (skip all prompts)", "bypassPermissions"),
)
# Every token `claude --permission-mode` accepts (verified 2.1.220). The
# dropdown above is the curated subset a NEW agent can be launched in; this is
# the full vocabulary, needed because the mode is also read back off a live
# conversation, where the user's own Shift+Tab may have picked something the
# dialog never offers ("auto" is what a current CLI records where an older
# build said "acceptEdits"). Note there is NO "default" token even though the
# transcript writes that name for the ask-each-time mode: it is spelled
# "manual" on the command line, or reproduced exactly by omitting the flag.
CLAUDE_CLI_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions",
                               "manual", "dontAsk", "plan")

# transcript token -> the launch flag that reproduces it. Only the modes whose
# names differ between the two need an entry; anything else passes through if
# the CLI accepts it. "default"/"manual" both mean ask-each-time, which is what
# omitting the flag already does, so they map to "".
_MODE_LAUNCH = {"": "", "default": "", "manual": ""}

# ...and how each reads on the card. Short lowercase words, since this sits in
# a header chip beside the model and effort.
_MODE_DISPLAY = {
    "": "manual", "default": "manual", "manual": "manual",
    "auto": "auto", "acceptEdits": "auto edits", "plan": "plan",
    "bypassPermissions": "bypass", "dontAsk": "no prompts",
}


def normalize_permission_mode(raw: str) -> str:
    """The `--permission-mode` value that reproduces `raw` on the next launch.

    `raw` is a mode as the CLI names it internally (what a transcript record
    carries), which is not always a launchable token: the ask-each-time mode is
    written "default" and has no flag spelling at all. Anything unrecognized
    becomes "" (omit the flag) rather than a bogus flag that would stop the
    agent launching."""
    token = (raw or "").strip()
    if token in _MODE_LAUNCH:
        return _MODE_LAUNCH[token]
    return token if token in CLAUDE_CLI_PERMISSION_MODES else ""


def permission_mode_display(raw: str) -> str:
    """A permission mode as a short label for the card header, e.g.
    "default" -> "manual". Unknown tokens show verbatim (a newer CLI's mode is
    better shown as-is than hidden); "" is the CLI default, i.e. "manual"."""
    token = (raw or "").strip()
    return _MODE_DISPLAY.get(token, token)


_GEMINI_MODE_DISPLAY = {
    "": "manual", "default": "manual", "ask-permission": "manual",
    "accept-edits": "auto", "auto": "auto", "accept_edits": "auto",
    "always-proceed": "bypass", "always_proceed": "bypass",
    "yolo": "bypass", "bypasspermissions": "bypass", "bypass": "bypass",
    "plan": "plan",
}


def gemini_permission_mode_display(raw: str) -> str:
    """A Gemini permission mode as a short label for the card header, e.g.
    'accept-edits' -> 'auto', 'always-proceed' -> 'bypass', 'plan' -> 'plan'."""
    token = (raw or "").strip().lower()
    return _GEMINI_MODE_DISPLAY.get(token, token)

PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        key="claude", display="Claude Code", exe_names=("claude",),
        models=CLAUDE_MODELS, efforts=CLAUDE_EFFORTS,
        permission_modes=CLAUDE_PERMISSION_MODES,
        cli_permission_modes=CLAUDE_CLI_PERMISSION_MODES, native_flags=True,
        note="Anthropic Claude Code: full interactive agent."),
    "openai": Provider(
        key="openai", display="OpenAI (Codex CLI)", exe_names=("codex",),
        models=(("Default", ""),
                # Keep the moving flagship on its stable alias.  The OpenAI
                # model registry currently maps gpt-5.6 to GPT-5.6 Sol; using
                # the alias lets a new Codex terminal follow that update.
                ("GPT-5.6 (Sol)", "gpt-5.6"),
                ("GPT-5.6 Terra", "gpt-5.6-terra"),
                ("GPT-5.6 Luna", "gpt-5.6-luna"),
                ("GPT-5.5", "gpt-5.5")),
        base_cmd="codex", model_flag="--model {model}",
        note="Requires the OpenAI Codex CLI (`codex`) on PATH."),
    "gemini": Provider(
        key="gemini", display="Gemini CLI",
        exe_names=("agy", "gemini"),
        models=GEMINI_MODELS, efforts=(), native_flags=True,
        base_cmd="agy", model_flag="--model {model}",
        fallback_paths=(r"%LOCALAPPDATA%\agy\bin\agy.exe",),
        note="Google Gemini CLI (`agy`): Gemini 3.x agent."),
    "grok": Provider(
        key="grok", display="Grok (xAI CLI)", exe_names=("grok",),
        # `grok models` reports one entry for this account (grok-build, the
        # default); more may appear per plan. "" omits -m and uses the CLI's
        # own default. Model IDs are single tokens, so -m {model} is one arg.
        models=(("Default", ""), ("grok-build", "grok-build")),
        base_cmd="grok", model_flag="-m {model}",
        # the xAI installer drops grok.exe under the user profile, which may not
        # be on the app's PATH (verified: %USERPROFILE%\.grok\bin\grok.exe)
        fallback_paths=(r"%USERPROFILE%\.grok\bin\grok.exe",),
        note="xAI Grok CLI (`grok`): interactive Grok agent."),
}

AI_PROVIDER_KEYS = tuple(PROVIDERS.keys())


def get(key: str) -> Provider | None:
    return PROVIDERS.get(key)


def resolve_claude() -> str:
    """The Claude Code binary AI Hive launches, native install FIRST.

    THE ORDER IS LOAD-BEARING AND IS THE MOST LIKELY WAY TO SHIP THE NATIVE
    MIGRATION BROKEN. Measured on the reporting machine: the winget package
    directory is on PATH DIRECTLY (not through a Links shim), and
    `%USERPROFILE%\\.local\\bin` is not on PATH at all. The native installer
    APPENDS its own directory, so after a migration PATH holds both and order
    decides which one `shutil.which` finds. It would keep answering with the
    STALE winget copy, AI Hive would go on launching the old binary, and the
    migration would appear to have done nothing. That is exactly the risk
    CLAUDE.md used to record as a reason not to migrate at all; checking the
    native launcher first is what handles it.

    Deliberate consequence, worth keeping: AI Hive does not depend on the
    installer's PATH edit, so a migration never needs a new terminal, and every
    agent rebuilt from `session.json` re-resolves through here (`to_dict`
    stores `user_program`, and `from_dict` re-runs `build_spec`). An agent that
    already exists in the LIVE process does not, which is why the migration
    also rebuilds those specs (`MainWindow.rebind_claude_specs`).

    Only the PATH lookup is cached (`_claude_on_path`). The native check runs
    every time, so a migration is seen on the next call even before the
    install path calls `forget_claude()`.
    """
    native = os.path.join(os.environ.get("USERPROFILE")
                          or os.path.expanduser("~"),
                          ".local", "bin", "claude.exe")
    if os.path.isfile(native):
        return native
    found = _claude_on_path()
    if found:
        return found
    guess = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        r"Microsoft\WinGet\Packages"
        r"\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\claude.exe")
    return guess if os.path.isfile(guess) else "claude.exe"


def _claude_on_path() -> str:
    """`shutil.which("claude")`, kept while PATH is unchanged and the file it
    named is still there. "" when PATH has no claude."""
    global _CLAUDE_ON_PATH
    searched = os.environ.get("PATH", "")
    key, found = _CLAUDE_ON_PATH
    if found and key == searched and os.path.isfile(found):
        return found
    found = shutil.which("claude") or ""
    _CLAUDE_ON_PATH = (searched, found)
    return found


def forget_claude() -> None:
    """Drop the cached PATH lookup, so the next `resolve_claude()` searches
    again. `cli_install` and `cli_update` call it after every step that
    installs, upgrades or removes a Claude Code binary."""
    global _CLAUDE_ON_PATH
    _CLAUDE_ON_PATH = ("", "")


def user_default_model() -> str:
    """The model the Claude CLI will pick when AI Hive passes no --model, i.e.
    the user's own `~/.claude/settings.json` "model" setting (what `/model`
    writes when it says "saved as your default"). A cold-start seed for the
    card's model label (once the conversation has a transcript, the
    transcript is the truth) and what the New Agent dialog's "Default" names.
    "" when unset/unreadable. Cheap to call (cached by mtime+size); never
    raises."""
    return _claude_user_defaults()[0]


def user_default_effort() -> str:
    """The effort the Claude CLI will pick when AI Hive passes no --effort:
    the "effortLevel" that `/effort` saves in `~/.claude/settings.json` from
    ANY terminal ("saved as your default for new sessions"). The CLI reads
    only low|medium|high|xhigh from there (max is session-only), so anything
    else is "", the model's own default. Measured on claude 2.1.309: no
    launch flag undoes a saved level, and `--settings '{"effortLevel":
    "auto"}'` still ran at the saved "high". So the New Agent dialog names
    this rather than promising the recommended effort. Never raises."""
    return _claude_user_defaults()[1]


def _claude_user_defaults() -> tuple[str, str]:
    """(model, effort) from `~/.claude/settings.json`, cached by mtime+size.
    An unreadable file keeps the last good reading."""
    global _USER_DEFAULTS_CACHE
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        st = os.stat(path)
    except OSError:
        return ("", "")
    cached = _USER_DEFAULTS_CACHE
    if cached[0] == st.st_mtime and cached[1] == st.st_size:
        return (cached[2], cached[3])
    model = effort = ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            if isinstance(data.get("model"), str):
                model = data["model"].strip()
            level = data.get("effortLevel")
            if isinstance(level, str) and level.strip().lower() in (
                    "low", "medium", "high", "xhigh"):
                effort = level.strip().lower()
    except (OSError, ValueError):
        return (cached[2], cached[3])
    _USER_DEFAULTS_CACHE = (st.st_mtime, st.st_size, model, effort)
    return (model, effort)


_GEMINI_SETTINGS_CACHE: tuple = (0.0, -1, {})


def gemini_user_default_settings() -> dict:
    """Read ~/.gemini/antigravity-cli/settings.json safely. Cached by mtime+size."""
    global _GEMINI_SETTINGS_CACHE
    path = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "settings.json")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    if _GEMINI_SETTINGS_CACHE[0] == st.st_mtime and _GEMINI_SETTINGS_CACHE[1] == st.st_size:
        return _GEMINI_SETTINGS_CACHE[2]
    data = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        return _GEMINI_SETTINGS_CACHE[2]
    _GEMINI_SETTINGS_CACHE = (st.st_mtime, st.st_size, data)
    return data


def gemini_user_default_model() -> str:
    """The default model configured in Gemini/agy settings.json, or fallback."""
    sett = gemini_user_default_settings()
    model = sett.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return "Gemini 3.8 Flash (High)"


_GEMINI_MODELS_CACHE: tuple | None = None


def fetch_gemini_models() -> tuple[tuple[str, str], ...]:
    """Query `agy models` to fetch the live model list from the CLI."""
    global _GEMINI_MODELS_CACHE
    prog = resolve_program("gemini")
    if not prog or not detected("gemini"):
        return GEMINI_MODELS
    try:
        import subprocess
        res = subprocess.run(
            [prog, "models"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout:
            entries = [("Default", "")]
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("Fetching"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    display = parts[1].strip()
                    if display:
                        entries.append((display, display))
                elif line:
                    entries.append((line, line))
            if len(entries) > 1:
                _GEMINI_MODELS_CACHE = tuple(entries)
                return _GEMINI_MODELS_CACHE
    except Exception:
        pass
    return GEMINI_MODELS


def gemini_available_models() -> tuple[tuple[str, str], ...]:
    """Return available Gemini models. Uses cached CLI output if available,
    falling back to GEMINI_MODELS."""
    global _GEMINI_MODELS_CACHE
    if _GEMINI_MODELS_CACHE is not None:
        return _GEMINI_MODELS_CACHE
    return GEMINI_MODELS


def resolve_program(key: str) -> str:
    if key == "claude":
        return resolve_claude()
    p = PROVIDERS.get(key)
    if p is None:
        return ""
    for exe in p.exe_names:
        found = shutil.which(exe)
        if found:
            return found
    # the app process may hold a PATH from before the CLI was installed —
    # known install locations still resolve it
    for guess in p.fallback_paths:
        path = os.path.expandvars(guess)
        if os.path.isfile(path):
            return path
    return p.base_cmd or p.exe_names[0]


# Codex 0.156 began running every interactive session inside one shared
# app-server daemon per Windows user, and added `--no-daemon` with it.
CODEX_NO_DAEMON_SINCE = (0, 156, 0)
_CODEX_PKG_RE = re.compile(r"@openai[\\/]codex[\\/]", re.IGNORECASE)
_CODEX_SHIM_MAX_BYTES = 64 * 1024
_SHIM_DIR_VARS = ("%~dp0", "%dp0%", "${basedir}", "$basedir", "$PSScriptRoot")


def codex_version(program: str) -> tuple:
    """(major, minor, patch) of the Codex CLI `program` launches, or () when
    that can't be told without running it.

    Read from the npm package's package.json, never by starting the CLI: a
    launch shouldn't wait on node, and the smoke suite must never start a real
    AI CLI. `program` is what resolve_program found, usually a shim. npm's own
    names the package relative to itself (`%dp0%\\node_modules\\@openai\\...`);
    a hand-written one can name it by absolute path (measured: the reporting
    machine's `~\\.local\\bin\\codex.cmd`). A standalone codex.exe has no
    package.json and reads as unknown."""
    if program and not os.path.isabs(program):
        # a bare name (resolve_program's fallback) launches through this
        # same PATH lookup, so read the file the launch would find
        program = shutil.which(program) or ""
    if not program or not os.path.isfile(program):
        return ()
    for pkg in _codex_package_dirs(program):
        try:
            with open(os.path.join(pkg, "package.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get("name") != "@openai/codex":
            continue
        found = re.match(r"(\d+)\.(\d+)\.(\d+)", str(meta.get("version", "")))
        if found:
            return tuple(int(n) for n in found.groups())
    return ()


def codex_has_no_daemon(program: str) -> bool:
    """Does the Codex CLI at `program` accept --no-daemon? False when unknown,
    since an older Codex refuses to start on a flag it doesn't know."""
    return codex_version(program) >= CODEX_NO_DAEMON_SINCE


def _codex_package_dirs(program: str) -> list:
    """Folders that may hold the @openai/codex package `program` runs."""
    path = os.path.normpath(program)
    here = os.path.dirname(path)
    dirs = []
    # `program` inside the package itself, e.g. its bin\codex.js or the
    # vendor codex.exe of the platform package nested in it
    up = here
    while os.path.dirname(up) != up:
        if (os.path.basename(up).lower() == "codex"
                and os.path.basename(os.path.dirname(up)).lower() == "@openai"):
            dirs.append(up)
        up = os.path.dirname(up)
    dirs.append(os.path.join(here, "node_modules", "@openai", "codex"))
    if os.path.splitext(path)[1].lower() in ("", ".cmd", ".bat", ".ps1"):
        try:
            if os.path.getsize(path) <= _CODEX_SHIM_MAX_BYTES:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            else:
                text = ""
        except OSError:
            text = ""
        # a path with spaces is quoted in any shim that works, so a quoted
        # string or else a bare word is the whole path
        for m in re.finditer(r'"([^"\r\n]*)"|\'([^\'\r\n]*)\'|(\S+)', text):
            token = next(g for g in m.groups() if g is not None)
            hit = _CODEX_PKG_RE.search(token)
            if not hit:
                continue
            prefix = token[:hit.start()]
            for var in _SHIM_DIR_VARS:
                prefix = prefix.replace(var, here + os.sep)
            prefix = os.path.expandvars(prefix)
            if os.path.isabs(prefix):
                dirs.append(os.path.normpath(
                    os.path.join(prefix, "@openai", "codex")))
    return dirs


def detected(key: str) -> bool:
    if key == "claude":
        return (os.path.isfile(resolve_claude())
                or bool(_claude_on_path()))
    p = PROVIDERS.get(key)
    if p is None:
        return False
    if any(shutil.which(e) for e in p.exe_names):
        return True
    return any(os.path.isfile(os.path.expandvars(g)) for g in p.fallback_paths)


def build_invocation(key: str, model: str = "", effort: str = "",
                     custom_command: str = "", extra_args=None,
                     permission_mode: str = "") -> tuple[str, list]:
    """Return (program, args) for an AI provider.

    custom_command (if given) overrides the built-in template/base for
    template providers, so users can wire their own CLI invocation.
    permission_mode (Claude only) is a Shift+Tab startup mode; "" omits the
    flag (the CLI default).
    """
    extra_args = list(extra_args or [])
    p = PROVIDERS.get(key)
    if p is None:
        return "", extra_args

    if p.native_flags:  # Claude: real flags
        program = resolve_program(key)
        args = []
        if model:
            args += ["--model", model]
        # defense in depth: 'ultracode' (and any non-CLI token) is NOT a valid
        # --effort value — it's an in-session mode — so it must never launch
        if effort and effort in p.efforts and effort:
            args += ["--effort", effort]
        # only ever pass a mode the provider actually declares (the "" default
        # is not a launch token — it means "omit the flag"), so a stale or
        # bogus value can never reach the CLI as an invalid --permission-mode.
        # The CLI's own vocabulary wins where it is known, since a mode adopted
        # from a live conversation is often outside the dialog's shortlist.
        valid_modes = (set(p.cli_permission_modes)
                       or {v for _, v in p.permission_modes if v})
        if permission_mode and permission_mode in valid_modes:
            args += ["--permission-mode", permission_mode]
        return program, args + extra_args

    # template provider (OpenAI/Gemini/custom)
    if custom_command.strip():
        tokens = _split_command(custom_command)
        program = tokens[0] if tokens else resolve_program(key)
        args = tokens[1:]
    else:
        program = resolve_program(key)
        args = []
        if model and p.model_flag:
            # substitute AFTER splitting the template: a multiword model value
            # ("Gemini 3.1 Pro (High)") must stay ONE argument, not four
            args += [t.format(model=model)
                     for t in _split_command(p.model_flag)]
    return program, args + extra_args


def _split_command(command: str) -> list:
    """Windows-aware command split. shlex(posix=False) keeps the surrounding
    quotes on tokens like "C:\\Program Files\\x\\y.exe", which then fails as a
    program path — strip them (a token that is fully quoted has no other
    quotes inside on Windows)."""
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in tokens]
