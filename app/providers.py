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

# _json_file's cache of the CLIs' settings files (Claude's and
# Antigravity's): path -> (mtime, size, dict)
_JSON_CACHE: dict = {}

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
# Shift+Tab. "" launches with NO --permission-mode flag and is the dialog
# default; the dialog names what that resolves to
# (user_default_permission_mode), because since claude 2.1.316 it is auto,
# not ask-each-time. "manual" is the explicit ask-each-time launch, the only
# way to get it once the CLI's own default is auto. acceptEdits/plan are the
# other steps of the ordinary Shift+Tab cycle; bypassPermissions is the opt-in
# mode that only appears in the cycle once launched with it. (Verified
# against `claude --permission-mode` choices on 2.1.316: acceptEdits, auto,
# bypassPermissions, manual, dontAsk, plan. There is NO "default" token.)
CLAUDE_PERMISSION_MODES = (
    ("Default", ""),
    ("Manual (ask before each action)", "manual"),
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
# "manual" on the command line.
CLAUDE_CLI_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions",
                               "manual", "dontAsk", "plan")

# transcript token -> the launch flag that reproduces it. Only the modes whose
# names differ between the two need an entry; anything else passes through if
# the CLI accepts it. "default" is the transcript's name for ask-each-time and
# must become the explicit "manual" flag: omitting the flag starts claude
# 2.1.316 in auto mode, so an agent the user put in manual mode came back in
# auto on every reopen.
_MODE_LAUNCH = {"": "", "default": "manual", "manual": "manual"}

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
    written "default" and launches as "manual", since no flag at all starts
    claude 2.1.316 in auto. Anything unrecognized becomes "" (omit the flag)
    rather than a bogus flag that would stop the agent launching."""
    token = (raw or "").strip()
    if token in _MODE_LAUNCH:
        return _MODE_LAUNCH[token]
    return token if token in CLAUDE_CLI_PERMISSION_MODES else ""


def permission_mode_display(raw: str) -> str:
    """A permission mode as a short label for the card header, e.g.
    "default" -> "manual". Unknown tokens show verbatim (a newer CLI's mode is
    better shown as-is than hidden). "" reads as "manual": it is what
    user_default_permission_mode returns when nothing says the CLI starts
    elsewhere."""
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
    model = _claude_user_settings().get("model")
    return model.strip() if isinstance(model, str) else ""


# the levels claude reads back from settings; "max" is session-only
_SAVED_EFFORTS = ("low", "medium", "high", "xhigh")


def user_default_effort(model: str = "") -> str:
    """The effort the Claude CLI will pick for `model` when AI Hive passes no
    --effort ("" = the model `user_default_model` names). `/effort` saves
    "as your default for new sessions" from ANY terminal, and since claude
    2.1.316 it saves per model: `modelSettings.<model id>.effortLevel` in
    `~/.claude/settings.json`, with the top-level "effortLevel" the fallback
    for a model that has none. A user on Opus 5.5 at xhigh had a top-level
    "high" left over, and the dialog and the card both said high.

    The CLI reads only low|medium|high|xhigh from there (max is
    session-only), so anything else is "", the model's own default.
    Measured on claude 2.1.309: no launch flag undoes a saved level, and
    `--settings '{"effortLevel": "auto"}'` still ran at the saved "high". So
    the New Agent dialog names this rather than promising the recommended
    effort.

    With no model pick at all, claude runs its own default model, which
    depends on the account. The per-model levels then name it only when
    they all agree. Never raises."""
    data = _claude_user_settings()
    key = _claude_model_key(model or user_default_model())
    per_model = data.get("modelSettings")
    if isinstance(per_model, dict):
        levels = set()
        for name, conf in per_model.items():
            if not (isinstance(name, str) and isinstance(conf, dict)):
                continue
            level = _saved_effort(conf.get("effortLevel"))
            if key and _claude_model_key(name) == key and level:
                return level
            levels.add(level)
        if (not key and not (model or user_default_model())
                and len(levels) == 1 and "" not in levels):
            return levels.pop()
    return _saved_effort(data.get("effortLevel"))


def _saved_effort(level) -> str:
    if isinstance(level, str) and level.strip().lower() in _SAVED_EFFORTS:
        return level.strip().lower()
    return ""


def claude_picker_label(model: str) -> str:
    """The label CLAUDE_MODELS gives the --model value `model`, without the
    label's own note in brackets: "opus" -> "Opus 5.5", "sonnet[1m]" ->
    "Sonnet 5.5". "" when the table has no entry for it, or for "" and
    "default". transcripts.claude_model_label and _claude_model_key both
    read the table through this, so the card's label and the modelSettings
    key can't disagree."""
    bare = _bare_claude_model(model)
    if not bare or bare == "default":
        return ""
    for label, value in CLAUDE_MODELS:
        if value and value == bare:
            return label.split(" (", 1)[0]
    return ""


def _bare_claude_model(model: str) -> str:
    """`model` lowercase, without a "[1m]" window suffix."""
    return (model or "").strip().lower().replace("[1m]", "").strip()


def _claude_model_key(model: str) -> str:
    """`model` as claude keys it under settings' "modelSettings": the full id,
    lowercase, without a "[1m]" window suffix. An alias resolves through the
    label CLAUDE_MODELS gives it ("opus" -> "Opus 5.5" -> "claude-opus-5-5"),
    since the CLI saves the model the alias meant, not the alias. "" for a
    pick with no single model behind it ("opusplan") or no pick at all.
    This parses the label, so a picker label must stay "<Family> <version>"
    (a smoke check holds every alias to its full id)."""
    bare = _bare_claude_model(model)
    if bare.startswith("claude-"):
        return bare
    m = re.fullmatch(r"([a-z]+) (\d+(?:\.\d+)?)",
                     claude_picker_label(bare).lower())
    return f"claude-{m.group(1)}-{m.group(2).replace('.', '-')}" if m else ""


# the startup modes a settings file's permissions.defaultMode may name
_SETTINGS_MODES = ("default", "manual", "acceptEdits", "plan", "auto",
                   "bypassPermissions", "dontAsk")


def user_default_permission_mode(cwd: str = "") -> str:
    """The permission mode a NEW Claude conversation in `cwd` starts in when
    AI Hive passes no --permission-mode, as the transcript names it ("auto",
    "default" for ask-each-time, "plan", ...). The card shows it until the
    transcript says, and the New Agent dialog's Default names it.

    A settings file's `permissions.defaultMode` wins, the most specific file
    first: `<cwd>/.claude/settings.local.json`, `<cwd>/.claude/settings.json`,
    then `~/.claude/settings.json`. Without one, the CLI's own default.
    claude 2.1.316 starts in auto, so "no flag" stopped meaning ask-each-time
    and every new card said "manual" over a terminal in auto mode. Whether
    the CLI does that is read off `~/.claude.json`: it sets
    "hasSeenAutoDefaultNotice" the first time a session starts in auto by
    default, and keeps the gate behind it as
    `cachedGrowthBookFeatures.tengu_harbor_willow`. A settings file that turns
    auto mode off (`disableAutoMode: "disable"`) rules auto out either way.
    Never raises; "" when nothing says, which reads as manual on the card."""
    files = [_claude_user_settings()]
    if cwd:
        files[:0] = [_json_file(os.path.join(cwd, ".claude", name))
                     for name in ("settings.local.json", "settings.json")]
    auto_off = any(_auto_mode_disabled(d) for d in files)
    for data in files:
        perms = data.get("permissions")
        mode = perms.get("defaultMode") if isinstance(perms, dict) else None
        if isinstance(mode, str) and mode.strip() in _SETTINGS_MODES:
            mode = mode.strip()
            if mode == "manual":
                return "default"   # the transcript's name for it
            if mode != "auto":
                return mode
            if not auto_off:
                return "auto"
            break
    if auto_off:
        return ""
    state = _json_file(os.path.join(os.path.expanduser("~"), ".claude.json"))
    gates = state.get("cachedGrowthBookFeatures")
    gate = gates.get("tengu_harbor_willow") if isinstance(gates, dict) else None
    if state.get("hasSeenAutoDefaultNotice") is True and gate is not False:
        return "auto"
    return ""


def _auto_mode_disabled(data: dict) -> bool:
    perms = data.get("permissions")
    return (data.get("disableAutoMode") == "disable"
            or (isinstance(perms, dict)
                and perms.get("disableAutoMode") == "disable"))


def _claude_user_settings() -> dict:
    """`~/.claude/settings.json` as a dict, {} when missing or unreadable."""
    return _json_file(os.path.join(os.path.expanduser("~"), ".claude",
                                   "settings.json"))


def _json_file(path: str) -> dict:
    """The JSON object in `path`, cached by mtime+size so a caller that runs
    on every card build costs one stat. {} when the file is missing or is
    not an object. An unreadable or half-written file keeps the last good
    reading: the CLIs rewrite these files while AI Hive reads them."""
    try:
        st = os.stat(path)
    except OSError:
        _JSON_CACHE.pop(path, None)
        return {}
    cached = _JSON_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return cached[2] if cached else {}
    if not isinstance(data, dict):
        data = {}
    _JSON_CACHE[path] = (st.st_mtime, st.st_size, data)
    return data


def gemini_user_default_settings() -> dict:
    """~/.gemini/antigravity-cli/settings.json as a dict, {} when missing.
    Cached by mtime+size, like the Claude settings (_json_file)."""
    return _json_file(os.path.join(os.path.expanduser("~"), ".gemini",
                                   "antigravity-cli", "settings.json"))


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
