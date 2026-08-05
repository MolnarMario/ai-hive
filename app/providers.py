"""AI-agent provider registry.

Each provider maps to a CLI. Claude Code is fully wired — its real
`--model` / `--effort` flags (verified on this machine: effort tokens are
low|medium|high|xhigh|max; model aliases include opus|sonnet|haiku|fable).
Other providers are editable command templates that only launch once their
CLI is installed; until then the dialog shows them as "not detected".

This module is Qt-free (pure data + os/shutil helpers) so the model layer
and headless tests can use it without a GUI.
"""

import json
import os
import shlex
import shutil
from dataclasses import dataclass, field

DEFAULT_MODEL = ""   # empty = omit the flag, use the CLI's own default
DEFAULT_EFFORT = ""

# cache for user_default_model: (mtime, size, model)
_USER_MODEL_CACHE: tuple = (0.0, -1, "")


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
    native_flags: bool = False  # True → --model/--effort (Claude); else template
    base_cmd: str = ""         # template providers, e.g. "codex"
    model_flag: str = ""       # template, e.g. "--model {model}" / "-m {model}"
    fallback_paths: tuple = ()  # known install paths (env vars expanded) —
    # the app may run with a PATH older than the CLI's install
    note: str = ""


# providers whose CLI can resume its previous conversation with --continue
# (claude verified 2.1.197; agy verified 1.0.16; grok verified 0.2.93)
RESUME_PROVIDERS = ("claude", "gemini", "grok")


CLAUDE_MODELS = (
    ("Default", ""), ("Opus", "opus"), ("Sonnet", "sonnet"),
    ("Haiku", "haiku"), ("Fable", "fable"),
)
CLAUDE_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
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

PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        key="claude", display="Claude Code", exe_names=("claude",),
        models=CLAUDE_MODELS, efforts=CLAUDE_EFFORTS,
        permission_modes=CLAUDE_PERMISSION_MODES, native_flags=True,
        note="Anthropic Claude Code: full interactive agent."),
    "openai": Provider(
        key="openai", display="OpenAI (Codex CLI)", exe_names=("codex",),
        models=(("Default", ""),
                ("GPT-5.6 Sol", "gpt-5.6-sol"),
                ("GPT-5.6 Terra", "gpt-5.6-terra"),
                ("GPT-5.6 Luna", "gpt-5.6-luna"),
                # Keep prior choices available for restored or pinned agents.
                ("GPT-5.1", "gpt-5.1"),
                ("GPT-5.1 Codex", "gpt-5.1-codex")),
        base_cmd="codex", model_flag="--model {model}",
        note="Requires the OpenAI Codex CLI (`codex`) on PATH."),
    "gemini": Provider(
        key="gemini", display="Gemini (Antigravity CLI)",
        exe_names=("agy", "gemini"),
        # values are the CLI's own display strings — verified against
        # `agy models` + a live `-p --model` round-trip (agy 1.0.16)
        models=(("Default", ""),
                ("Gemini 3.1 Pro (High)", "Gemini 3.1 Pro (High)"),
                ("Gemini 3.1 Pro (Low)", "Gemini 3.1 Pro (Low)"),
                ("Gemini 3.5 Flash (High)", "Gemini 3.5 Flash (High)"),
                ("Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash (Medium)"),
                ("Gemini 3.5 Flash (Low)", "Gemini 3.5 Flash (Low)"),
                ("Claude Sonnet 4.6 (Thinking)", "Claude Sonnet 4.6 (Thinking)"),
                ("Claude Opus 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)"),
                ("GPT-OSS 120B (Medium)", "GPT-OSS 120B (Medium)")),
        base_cmd="agy", model_flag="--model {model}",
        fallback_paths=(r"%LOCALAPPDATA%\agy\bin\agy.exe",),
        note="Google Antigravity CLI (`agy`): Gemini 3.x agent."),
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
    found = shutil.which("claude")
    if found:
        return found
    guess = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        r"Microsoft\WinGet\Packages"
        r"\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\claude.exe")
    return guess if os.path.isfile(guess) else "claude.exe"


def user_default_model() -> str:
    """The model the Claude CLI will pick when AI Hive passes no --model, i.e.
    the user's own `~/.claude/settings.json` "model" setting (what `/model`
    writes when it says "saved as your default"). Only a COLD-START seed for
    the card's model label: once the conversation has a transcript, the
    transcript is the truth. "" when unset/unreadable. Cheap to call (cached by
    mtime+size); never raises."""
    global _USER_MODEL_CACHE
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if _USER_MODEL_CACHE[0] == st.st_mtime and _USER_MODEL_CACHE[1] == st.st_size:
        return _USER_MODEL_CACHE[2]
    model = ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            model = data["model"].strip()
    except (OSError, ValueError):
        return _USER_MODEL_CACHE[2]
    _USER_MODEL_CACHE = (st.st_mtime, st.st_size, model)
    return model


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


def detected(key: str) -> bool:
    if key == "claude":
        prog = resolve_claude()
        return bool(shutil.which("claude")) or os.path.isfile(prog)
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
        # bogus value can never reach the CLI as an invalid --permission-mode
        valid_modes = {v for _, v in p.permission_modes if v}
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
