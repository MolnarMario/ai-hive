"""Async process execution engine.

One ProcessWorker per terminal agent, built on QProcess: output arrives as
event-loop signals (no threads, no GUI blocking). Windows specifics that
this module encodes, all empirically verified on the target machine:

- `powershell -NoLogo -NoProfile -Command -` runs each piped stdin
  statement immediately with no prompt noise and exits 0 on stdin EOF;
  multi-line blocks need a trailing blank line (hence trailing_blank_line).
- `cmd /d /Q /K` behaves likewise (prompt echo included); `/d` skips
  AutoRun. `chcp 65001` corrupts non-ASCII stdin, so cmd output is decoded
  UTF-8-first with cp437 fallback instead (HybridDecoder).
- QProcess.terminate() is WM_CLOSE — console apps ignore it. Graceful stop
  is stdin EOF (closeWriteChannel); the hard stop is a Job Object tree
  kill, which also reaps grandchildren (`ping -t` etc.) and — thanks to
  KILL_ON_JOB_CLOSE — everything else if this GUI process dies.
"""

import codecs
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from . import providers

DEFAULT_FONT_PX = 13

CREATE_NO_WINDOW = 0x08000000

# Induce ANSI color from tools that check these; keep python children live.
COMMON_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "TERM": "xterm-256color",
    "FORCE_COLOR": "1",
    "CLICOLOR_FORCE": "1",
}

PS_UTF8_INIT = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8")

FLUSH_INTERVAL_MS = 33          # ~30 fps output batching
PENDING_CAP = 1024 * 1024       # raw bytes buffered between flushes
CAP_KEEP_HEAD = 64 * 1024
CAP_KEEP_TAIL = 256 * 1024


class AgentKind(str, Enum):
    POWERSHELL = "powershell"
    PWSH = "pwsh"
    CMD = "cmd"
    PYTHON_SCRIPT = "python"
    CLAUDE = "claude"
    OPENAI = "openai"
    GEMINI = "gemini"
    GROK = "grok"
    CUSTOM = "custom"


# AI-agent kinds map 1:1 onto a provider key in app/providers.py.
AI_KINDS = {AgentKind.CLAUDE: "claude", AgentKind.OPENAI: "openai",
            AgentKind.GEMINI: "gemini", AgentKind.GROK: "grok"}

# Kinds that only make sense inside a real pseudo-console (interactive TUIs).
PTY_ONLY_KINDS = {AgentKind.CLAUDE, AgentKind.OPENAI, AgentKind.GEMINI,
                  AgentKind.GROK}


@dataclass
class AgentSpec:
    kind: AgentKind
    name: str
    # what this agent RUNS, for the card header's sublabel: the provider
    # display name for an AI kind, else "PowerShell" / "cmd" / "python x.py".
    # build_spec sets it once; nothing mutates it afterwards. (It used to also
    # carry a guessed role -- "Testing Agent" -- inferred from the assigned
    # task, which renamed the agent as a side effect. That is gone.)
    role: str = ""
    # the display name was chosen by the user, not generated as "Agent N"
    custom_name: bool = False
    program: str = ""
    args: list = field(default_factory=list)
    cwd: str = ""
    env: dict = field(default_factory=dict)
    encoding_out: str = "hybrid"   # "hybrid" | "utf-8" | "cp437"
    encoding_in: str = "utf-8"
    init_lines: list = field(default_factory=list)
    trailing_blank_line: bool = False
    merge_stderr: bool = True
    line_ending: str = "\r\n"
    pty: bool = False  # run inside a real pseudo-console (ConPTY) if True
    # AI-agent configuration (empty for shells/scripts)
    provider: str = ""       # provider key: "claude"|"openai"|"gemini"|"grok"
    model: str = ""          # "" = provider default
    effort: str = ""         # "" = provider default (Claude: low..max)
    # Claude startup permission mode (a Shift+Tab mode). "" = omit the flag =
    # the CLI's own default (today's behavior). Baked into `args` by build_spec
    # via providers.build_invocation, like model/effort.
    permission_mode: str = ""
    custom_command: str = ""  # user override for template providers
    font_px: int = 0         # 0 = follow the global/default size
    # extra system-prompt text injected for coordination (Claude)
    system_prompt: str = ""
    extra_dirs: list = field(default_factory=list)  # --add-dir targets
    mcp_config_path: str = ""       # --mcp-config for the board log_activity tool
    # --settings file carrying the shared SessionStart hook that reports this
    # agent's LIVE conversation id back to AI Hive (app/session_hook.py). Like
    # mcp_config_path it is a per-run path, armed before start and re-armed on
    # restore, and is NEVER persisted (not in to_dict).
    settings_path: str = ""
    resume: bool = False            # one-shot: resume prior conversation (restore)
    # each Claude agent OWNS one conversation, pinned by id. Resuming uses
    # --resume <id>, never --continue: "most recent in this folder" is wrong
    # the moment two agents share a project folder (they'd race for the same
    # conversation, and the loser opens something else or clobbers a peer's).
    session_id: str = ""
    # user-facing fields only (program/args are rebuilt from the profile)
    user_program: str = ""   # PYTHON_SCRIPT: script path; CUSTOM: exe
    user_args: list = field(default_factory=list)

    def set_permission_mode(self, mode: str) -> bool:
        """Adopt `mode` as the startup permission mode and REBUILD the provider
        args that carry it. Returns True when something actually changed.

        Mutating the field alone is not enough: `args` is baked once by
        build_spec, so the new mode would persist correctly but every launch
        for the rest of this process would still use the old flag. Claude only
        (no other provider has the concept), and `mode` must already be a
        launchable token, i.e. run through providers.normalize_permission_mode.
        """
        mode = (mode or "").strip()
        if mode == self.permission_mode:
            return False
        self.permission_mode = mode
        if self.provider == "claude":
            self.program, self.args = providers.build_invocation(
                self.provider, model=self.model, effort=self.effort,
                custom_command=self.custom_command,
                extra_args=list(self.user_args), permission_mode=mode)
        return True

    def effective_args(self) -> list:
        """Args actually passed to the process, including coordination flags
        (Claude native flags only)."""
        args = list(self.args)
        if self.provider == "claude":
            if self.resume:
                # pinned id -> resume THIS agent's own conversation;
                # --continue only as legacy fallback for pre-pinning sessions
                args += (["--resume", self.session_id] if self.session_id
                         else ["--continue"])
            elif self.session_id:
                # fresh start still declares its identity, so the NEXT resume
                # can pin to it (verified: claude --session-id <uuid>)
                args += ["--session-id", self.session_id]
            for d in self.extra_dirs:
                if d:
                    args += ["--add-dir", d]
            if self.settings_path:
                # inject the SessionStart hook without touching the user's
                # global config; Claude merges hooks across sources, so this is
                # additive (verified against 2.1.197).
                args += ["--settings", self.settings_path]
            if self.system_prompt:
                args += ["--append-system-prompt", self.system_prompt]
            if self.mcp_config_path:
                # verified against Claude Code 2.1.197: load only our MCP server
                # and pre-approve its single tool so the call doesn't block on a
                # prompt. Every Claude agent gets exactly log_activity — enough
                # to post to the shared board, nothing more.
                args += ["--mcp-config", self.mcp_config_path,
                         "--strict-mcp-config",
                         "--allowedTools", "mcp__aihive__log_activity"]
        elif self.provider == "gemini":
            # Antigravity CLI (agy) shares --continue, --conversation <id>, and --add-dir
            if self.resume:
                args += (["--conversation", self.session_id] if self.session_id
                         else ["--continue"])
            for d in self.extra_dirs:
                if d:
                    args += ["--add-dir", d]
        elif self.provider == "grok":
            # xAI Grok CLI (grok 0.2.93) resumes the folder's most recent
            # session with --continue (verified). It exposes --cwd rather than
            # a repeatable --add-dir, and has no system-prompt flag, so no
            # board wiring — like Gemini it's an interactive agent only.
            if self.resume:
                args += ["--continue"]
        return args

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value, "name": self.name, "role": self.role,
            "custom_name": self.custom_name,
            "cwd": self.cwd, "user_program": self.user_program,
            "user_args": list(self.user_args), "pty": self.pty,
            "provider": self.provider, "model": self.model,
            "effort": self.effort, "permission_mode": self.permission_mode,
            "custom_command": self.custom_command,
            "font_px": self.font_px,
            "session_id": self.session_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "AgentSpec":
        spec = build_spec(
            AgentKind(d.get("kind", "powershell")),
            d.get("name", "Agent"), role=d.get("role", ""),
            cwd=d.get("cwd", ""), program=d.get("user_program", ""),
            args=list(d.get("user_args", [])),
            pty=bool(d.get("pty", False)),
            model=d.get("model", ""), effort=d.get("effort", ""),
            permission_mode=d.get("permission_mode", ""),
            custom_command=d.get("custom_command", ""),
            font_px=int(d.get("font_px", 0) or 0),
        )
        # restored agents keep their pinned conversation ("" = legacy, which
        # resumes via --continue once and gets pinned on its next fresh start)
        spec.session_id = str(d.get("session_id", "") or "")
        spec.custom_name = bool(d.get("custom_name", False))
        return spec


def _resolve_claude() -> str:
    return providers.resolve_claude()


def build_spec(kind: AgentKind, name: str, role: str = "", cwd: str = "",
               program: str = "", args: list | None = None,
               pty: bool = False, model: str = "", effort: str = "",
               custom_command: str = "", font_px: int = 0,
               permission_mode: str = "") -> AgentSpec:
    """Profile factory: fills in the verified per-shell invocation modes.

    When pty=True the shells launch in their INTERACTIVE form (real prompt,
    PSReadLine, etc.) because a pseudo-console makes them behave like a true
    terminal; the piped-stdin invocations are only used in line mode.

    AI-agent kinds (Claude/OpenAI/Gemini/Grok) route through app.providers so
    the model/effort selections become real CLI flags.
    """
    # Coerce to the real enum, because a plain str gets this far in practice
    # and does not fail until much later, somewhere else. Qt is the source:
    # QComboBox.currentData() round-trips a value through QVariant, and a
    # str-mixin enum comes back out as a plain str (verified) — so every agent
    # built from the New Agent dialog carried kind="claude" rather than
    # AgentKind.CLAUDE. Nothing here notices: AgentKind is a str-mixin, so the
    # `in PTY_ONLY_KINDS` / `in AI_KINDS` lookups below all still hit. What
    # breaks is `AgentSpec.to_dict`'s `self.kind.value`, on every save, for the
    # life of the process — the agent degrades to a minimal record and silently
    # loses its model, effort, permission mode, role and task on the next
    # restore. Restarting "fixed" it only because `from_dict` rebuilds the enum.
    # Doing it here makes the invariant true by construction for every caller.
    kind = AgentKind(kind)
    args = list(args or [])
    if kind in PTY_ONLY_KINDS:
        pty = True
    spec = AgentSpec(kind=kind, name=name, role=role, cwd=cwd, pty=pty,
                     model=model, effort=effort, permission_mode=permission_mode,
                     custom_command=custom_command,
                     font_px=font_px,
                     user_program=program, user_args=args)
    if kind in AI_KINDS:
        spec.provider = AI_KINDS[kind]
        prov = providers.get(spec.provider)
        prog, prov_args = providers.build_invocation(
            spec.provider, model=model, effort=effort,
            custom_command=custom_command, extra_args=args,
            permission_mode=permission_mode)
        spec.program = prog
        spec.args = prov_args
        spec.role = role or (prov.display if prov else spec.provider)
        return spec
    if kind in (AgentKind.POWERSHELL, AgentKind.PWSH):
        exe = "powershell.exe" if kind == AgentKind.POWERSHELL else (
            shutil.which("pwsh") or "pwsh.exe")
        spec.program = exe
        spec.args = (["-NoLogo", "-NoProfile"] if pty
                     else ["-NoLogo", "-NoProfile", "-Command", "-"])
        spec.init_lines = [] if pty else [PS_UTF8_INIT]
        spec.encoding_out = "utf-8"
        spec.trailing_blank_line = not pty
        spec.role = role or ("PowerShell" if kind == AgentKind.POWERSHELL
                             else "PowerShell 7")
    elif kind == AgentKind.CMD:
        spec.program = "cmd.exe"
        spec.args = ["/d"] if pty else ["/d", "/Q", "/K"]
        spec.env = {} if pty else {"PROMPT": "$P$G"}
        spec.encoding_out = "hybrid"
        spec.encoding_in = "cp437"
        spec.role = role or "cmd"
    elif kind == AgentKind.PYTHON_SCRIPT:
        spec.program = sys.executable
        spec.args = ["-u", program] + args if program else ["-u"] + args
        spec.encoding_out = "utf-8"
        spec.merge_stderr = False
        spec.role = role or f"python {os.path.basename(program)}".strip()
    else:  # CUSTOM
        spec.program = program
        spec.args = args
        spec.encoding_out = "hybrid"
        spec.merge_stderr = False
        spec.role = role or os.path.basename(program)
    return spec


# ---------------------------------------------------------------- decode ---

class HybridDecoder:
    """Incremental UTF-8-first decoder with cp437 fallback per chunk.

    cmd/PS 5.1 builtins emit OEM cp437 on pipes while modern tools emit
    UTF-8, sometimes interleaved in one session. ASCII (the common case)
    and valid UTF-8 take the strict path; anything else falls back to
    cp437, which never fails (every byte maps). An incomplete trailing
    UTF-8 sequence is held until more bytes arrive.
    """

    def __init__(self, mode: str = "hybrid"):
        self._mode = mode
        self._tail = b""
        self._tail_since = 0.0
        if mode != "hybrid":
            self._dec = codecs.getincrementaldecoder(mode)(errors="replace")

    def feed(self, data: bytes) -> str:
        if self._mode != "hybrid":
            return self._dec.decode(data)
        buf = self._tail + data
        self._tail = b""
        cut = len(buf)
        for i in range(1, min(4, len(buf)) + 1):
            b = buf[-i]
            if b < 0x80:
                break  # ends in ASCII: nothing incomplete
            if b >= 0xC0:  # UTF-8 lead byte
                need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
                if i < need:
                    cut = len(buf) - i  # sequence still incomplete: hold it
                break
        self._tail = buf[cut:]
        if self._tail:
            self._tail_since = time.monotonic()
        chunk = buf[:cut]
        if not chunk:
            return ""
        try:
            return chunk.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return chunk.decode("cp437", "replace")

    @property
    def has_tail(self) -> bool:
        return bool(self._tail)

    def flush_stale(self, max_age_s: float = 0.13) -> str:
        """Release a held tail when the stream goes idle: a genuine UTF-8
        continuation arrives within the same pipe read in practice, so an
        aging tail is cp437 text (e.g. box-drawing from `tree`) that should
        render rather than being withheld until process exit."""
        if self._mode != "hybrid" or not self._tail:
            return ""
        if time.monotonic() - self._tail_since < max_age_s:
            return ""
        return self.flush()

    def flush(self) -> str:
        if self._mode != "hybrid":
            return self._dec.decode(b"", final=True)
        tail, self._tail = self._tail, b""
        return tail.decode("cp437", "replace") if tail else ""


# ----------------------------------------------------------------- WinJob ---

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # fixed-capacity stand-in for JOBOBJECT_BASIC_PROCESS_ID_LIST's flexible
    # trailing array (ctypes has no flexible array member); we only ever
    # read the NumberOfAssignedProcesses header field, so a generous cap is
    # enough -- no realistic agent process tree needs more.
    _BG_JOB_PID_CAP = 64

    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _BG_JOB_PID_CAP),
        ]

    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # QueryInformationJobObject's info buffer is a real (variable-shaped)
    # struct, unlike the fixed-size ones the other WinJob calls exchange --
    # give it explicit prototypes rather than relying on ctypes' default
    # int-sized inference, which can truncate the HANDLE on 64-bit.
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    # same explicit-prototype discipline as QueryInformationJobObject above:
    # this one hands back a HANDLE-adjacent BOOL too, and it's only ever used
    # to label a pid for a human (describe_pid), so a truncation here should
    # fail closed to the bare "pid N" fallback, not silently misread memory.
    _k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    # SetConsoleCtrlHandler's first parameter is a POINTER, and it is passed
    # NULL for the ignore-flag half of enable_ctrl_c_for_children() -- without
    # an explicit prototype ctypes infers an int-sized argument and the
    # callback pointer is truncated on 64-bit (a truncated handler address is
    # a crash on the next Ctrl+C, not a failed call).
    _k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    _k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    _PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _CTRL_C_EVENT = 0
    _CTRL_BREAK_EVENT = 1


class WinJob:
    """One Job Object per worker: tree kill + crash-safe reaping."""

    def __init__(self):
        self._handle = None
        self._assigned = False
        if sys.platform != "win32":
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            _k32.CloseHandle(handle)
            return
        self._handle = handle

    def assign(self, pid: int) -> bool:
        if not self._handle or not pid:
            return False
        proc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                False, int(pid))
        if not proc:
            return False
        try:
            self._assigned = bool(_k32.AssignProcessToJobObject(self._handle, proc))
        finally:
            _k32.CloseHandle(proc)
        return self._assigned

    @property
    def assigned(self) -> bool:
        return self._assigned

    def terminate_tree(self, exit_code: int = 1) -> bool:
        if not self._handle or not self._assigned:
            return False
        return bool(_k32.TerminateJobObject(self._handle, exit_code))

    def process_count(self) -> int:
        """Best-effort count of processes currently alive in this job (the
        agent itself plus any descendant it spawned) -- used to notice a
        still-running background command even once the agent has gone
        quiet. Returns 0 on any failure (no handle, unsupported, query
        error), same as every other WinJob method never raising; callers
        must treat 0 as "unknown," not "empty.\""""
        if not self._handle:
            return 0
        info = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
        needed = wintypes.DWORD(0)
        ok = _k32.QueryInformationJobObject(
            self._handle, _JobObjectBasicProcessIdList, ctypes.byref(info),
            ctypes.sizeof(info), ctypes.byref(needed))
        if not ok:
            return 0
        return int(info.NumberOfAssignedProcesses)

    def process_ids(self) -> list[int]:
        """Best-effort list of process ids currently alive in this job --
        the identity-carrying sibling of process_count(), used to tell WHICH
        extra processes to kill (see TerminalAgent.kill_bg_shell_extras)
        rather than just how many there are. Same contract as
        process_count(): an empty list on any failure means "unknown," not
        "empty." Capped at _BG_JOB_PID_CAP like the query buffer itself; no
        realistic agent process tree needs more."""
        if not self._handle:
            return []
        info = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
        needed = wintypes.DWORD(0)
        ok = _k32.QueryInformationJobObject(
            self._handle, _JobObjectBasicProcessIdList, ctypes.byref(info),
            ctypes.sizeof(info), ctypes.byref(needed))
        if not ok:
            return []
        n = min(int(info.NumberOfProcessIdsInList), _BG_JOB_PID_CAP)
        return [int(info.ProcessIdList[i]) for i in range(n)]

    def close(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None
            self._assigned = False


def _taskkill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW,
                       timeout=5)
    except Exception:
        pass


def describe_pid(pid: int) -> str:
    """Best-effort short label for a pid -- its executable's base name (e.g.
    "java.exe", "adb.exe") -- so a human picking one process out of the kill
    menu can tell them apart. Purely cosmetic: nothing here feeds a kill
    decision. Falls back to a bare "pid N" if the process can't be queried
    (already exited, access denied, non-Windows)."""
    if sys.platform != "win32" or not pid:
        return f"pid {pid}"
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False,
                              int(pid))
    if not handle:
        return f"pid {pid}"
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok or not buf.value:
            return f"pid {pid}"
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        _k32.CloseHandle(handle)


_ctrl_c_handler_installed = False
# The console-ctrl callback is invoked by Windows on a thread it injects into
# this process, forever -- so the WINFUNCTYPE object must outlive the call that
# registered it. A garbage-collected handler is a crash on the next Ctrl+C, not
# a lost registration, hence this module-level reference.
_ctrl_c_handler = None


def _self_protect_ctrl(event: int) -> bool:
    """Swallow Ctrl+C/Ctrl+Break aimed at AI HIVE ITSELF (see
    enable_ctrl_c_for_children); pass every other console event through."""
    return event in (_CTRL_C_EVENT, _CTRL_BREAK_EVENT)


def enable_ctrl_c_for_children() -> bool:
    """Clear the INHERITED ignore-Ctrl+C flag, so a pty child can be interrupted.

    Without this, Ctrl+C in a full-terminal card does NOTHING to a running
    command, and PtyWorker.stop()'s "graceful Ctrl+C" is a no-op that always
    escalates to the Job-Object kill. The chain, measured rather than inferred:
    conhost consumes a 0x03 written into the pty as a CONTROL KEY (the child's
    input mode carries ENABLE_PROCESSED_INPUT) and raises CTRL_C_EVENT instead
    of delivering the byte -- so if that event is ignored, the keystroke reaches
    NOBODY, as neither a signal nor data. And it is ignored, because
    PEB.ProcessParameters.ConsoleFlags bit 0 ("ignore Ctrl+C") is INHERITED at
    CreateProcess time by every descendant: AI Hive picks it up from whatever
    launched AI HIVE (any harness that spawns with CREATE_NEW_PROCESS_GROUP --
    including a Claude Code agent terminal, i.e. how this repo is developed and
    how the smoke suite is usually run), and hands it to every agent, and every
    agent hands it to every build/test/ping it starts. Not a pywinpty
    behaviour: it spawns with EXTENDED_STARTUPINFO_PRESENT |
    CREATE_UNICODE_ENVIRONMENT and nothing else. Verified A/B in one process:
    flag set -> `ping -t` under a pty PowerShell ignores 0x03 forever; flag
    cleared -> ping dies, the shell prints "Control-C" and returns to its
    prompt. Also verified with NO console attached (the pythonw production
    shape), where the API still succeeds.

    Two halves, and the split is deliberate:

    * The ignore flag is cleared on EVERY call. It is a PEB field costing a
      syscall, only inheritance matters, and nothing guarantees some other
      component has not set it again since the last spawn.
    * The self-protect handler is installed ONCE. SetConsoleCtrlHandler APPENDS
      to a handler table, so re-registering the same callback per spawn would
      run it once per agent ever started.

    That handler is NOT optional. Clearing the flag without it makes AI Hive
    itself killable by a Ctrl+C in a console it shares with its launcher, and a
    hard kill skips closeEvent -- no final save, no transcript backup, no screen
    snapshots, i.e. exactly the silent-loss class the persistence invariants
    exist to prevent. It returns True (handled, do nothing) for
    CTRL_C_EVENT/CTRL_BREAK_EVENT and False for CLOSE/LOGOFF/SHUTDOWN, so
    shutdown behaves as it always has. Handlers are per-process and are NOT
    inherited -- only the flag is -- so protecting ourselves costs the children
    nothing.

    Never raises; returns False off Windows or on any API failure, the same
    contract every WinJob method keeps."""
    global _ctrl_c_handler_installed, _ctrl_c_handler
    if sys.platform != "win32":
        return False
    try:
        # Self-protection goes in FIRST, and the order is not stylistic: between
        # clearing the flag and installing the handler this process runs with
        # Windows' default handler, which TERMINATES on Ctrl+C. If the handler
        # cannot be installed, refuse the whole thing -- a dead Ctrl+C in the
        # cards is a mild failure, an AI Hive killable without closeEvent is a
        # session-losing one.
        if not _ctrl_c_handler_installed:
            handler = _PHANDLER_ROUTINE(_self_protect_ctrl)
            if not _k32.SetConsoleCtrlHandler(
                    ctypes.cast(handler, ctypes.c_void_p), True):
                return False
            _ctrl_c_handler = handler
            _ctrl_c_handler_installed = True
        return bool(_k32.SetConsoleCtrlHandler(None, False))
    except Exception:
        return False


# ----------------------------------------------------------------- worker ---

class WorkerState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    DEAD = "dead"


class ProcessWorker(QObject):
    """QProcess wrapper: batched decoded output, EOF stop, Job tree kill."""

    started = Signal(int)            # pid
    output = Signal(str, str)        # stream ("stdout"|"stderr"), text
    finished = Signal(int, bool)     # exit code, crashed
    failed = Signal(str)             # FailedToStart message
    state_changed = Signal(object)   # WorkerState

    def __init__(self, spec: AgentSpec, parent: QObject | None = None):
        super().__init__(parent)
        self.id = uuid.uuid4().hex
        self.spec = spec
        self.state = WorkerState.IDLE
        self.exit_info: tuple[int, bool] | None = None
        self._proc: QProcess | None = None
        self._job: WinJob | None = None
        self._gen = 0  # invalidates stale grace timers across restarts
        self._restart_pending = False
        self._queued_lines: list[str] = []  # typed while STARTING
        self._pend = {"stdout": bytearray(), "stderr": bytearray()}
        self._dec = {}
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

    # ------------------------------------------------------------ control ---

    def start(self) -> None:
        if self.state in (WorkerState.STARTING, WorkerState.RUNNING,
                          WorkerState.STOPPING):
            return
        self._gen += 1
        self.exit_info = None
        self._pend = {"stdout": bytearray(), "stderr": bytearray()}
        self._dec = {s: HybridDecoder(self.spec.encoding_out)
                     for s in ("stdout", "stderr")}

        old = self._proc  # a restarted worker must not accumulate dead
        if old is not None:  # QProcess children with live connections
            for sig in (old.started, old.readyReadStandardOutput,
                        old.readyReadStandardError, old.finished,
                        old.errorOccurred):
                try:
                    sig.disconnect()
                except RuntimeError:
                    pass
            old.deleteLater()

        proc = QProcess(self)
        self._proc = proc
        proc.setProgram(self.spec.program)
        proc.setArguments(list(self.spec.effective_args()))
        if self.spec.cwd and os.path.isdir(self.spec.cwd):
            proc.setWorkingDirectory(self.spec.cwd)

        env = QProcessEnvironment.systemEnvironment()
        for key, val in {**COMMON_ENV, **self.spec.env}.items():
            env.insert(key, val)
        proc.setProcessEnvironment(env)

        if self.spec.merge_stderr:
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        else:
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)

        # No CREATE_NO_WINDOW handling needed: with piped channels (never
        # ForwardedChannels here) Qt already creates console children
        # windowless and unattached to any launch console.

        proc.started.connect(self._on_started)
        proc.readyReadStandardOutput.connect(self._on_ready_out)
        proc.readyReadStandardError.connect(self._on_ready_err)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._set_state(WorkerState.STARTING)
        proc.start()

    def send_line(self, text: str) -> bool:
        if self.state is WorkerState.STARTING or (
                self.state is WorkerState.STOPPING and self._restart_pending):
            self._queued_lines.append(text)  # delivered to the new session
            return True
        if self.state != WorkerState.RUNNING or self._proc is None:
            return False
        self._write_line(text)
        return True

    def _write_line(self, text: str) -> None:
        data = (text + self.spec.line_ending).encode(self.spec.encoding_in,
                                                     "replace")
        if self.spec.trailing_blank_line:
            data += self.spec.line_ending.encode("ascii")
        self._proc.write(data)

    def stop(self, grace_ms: int = 800) -> None:
        """Graceful: stdin EOF (verified exit 0 for cmd/PS), then tree kill."""
        if self.state not in (WorkerState.RUNNING, WorkerState.STARTING):
            return
        self._set_state(WorkerState.STOPPING)
        if self._proc is not None:
            self._proc.closeWriteChannel()
        gen = self._gen
        # self as receiver context: Qt drops the pending callback if the
        # worker is destroyed before the grace period elapses.
        QTimer.singleShot(grace_ms, self, lambda: self._grace_kill(gen))

    def kill(self) -> None:
        if self._proc is None or self.state in (WorkerState.IDLE, WorkerState.DEAD):
            return
        killed = self._job.terminate_tree() if self._job else False
        if not killed:
            pid = self._proc.processId()
            if pid:
                _taskkill_tree(pid)
        self._proc.kill()  # belt and braces; also covers not-yet-assigned

    def dispose(self) -> None:
        """Final teardown (card close / workspace delete / app quit).

        The Job Object tree kill is effectively synchronous, so the brief
        waitForFinished only reaps the notification — it prevents
        "QProcess: Destroyed while process is still running" at shutdown
        without any user-visible stall.
        """
        self._restart_pending = False
        if self._proc is None or self.state in (WorkerState.IDLE,
                                                WorkerState.DEAD):
            return
        self.kill()
        self._proc.waitForFinished(1500)

    def restart(self) -> None:
        if self.state in (WorkerState.RUNNING, WorkerState.STARTING,
                          WorkerState.STOPPING):
            self._restart_pending = True
            # Leave RUNNING synchronously: the kill is async (the finished
            # notification arrives via the event loop), and callers must not
            # be able to send_line() into the doomed session meanwhile.
            self._set_state(WorkerState.STOPPING)
            self.kill()
        else:
            self.start()

    def is_running(self) -> bool:
        return self.state in (WorkerState.STARTING, WorkerState.RUNNING)

    def pid(self) -> int | None:
        if self._proc is not None and self.is_running():
            return self._proc.processId() or None
        return None

    def process(self) -> QProcess | None:
        return self._proc

    def job_process_count(self) -> int:
        return self._job.process_count() if self._job else 0

    def job_process_ids(self) -> list[int]:
        return self._job.process_ids() if self._job else []

    def kill_extra_processes(self, keep: set[int]) -> list[int]:
        """Hard-kill every process in this job EXCEPT the given ids -- see
        TerminalAgent.kill_bg_shell_extras, which decides who's in `keep`.
        Each victim is tree-killed individually (never the job as a whole,
        which would also take down the interactive process itself)."""
        victims = [p for p in self.job_process_ids() if p not in keep]
        for p in victims:
            _taskkill_tree(p)
        return victims

    def kill_pid(self, pid: int) -> None:
        """Hard-kill exactly one process -- the single-item equivalent of
        kill_extra_processes, for TerminalAgent.kill_bg_shell_pid."""
        _taskkill_tree(pid)

    # -------------------------------------------------------------- slots ---

    def _set_state(self, state: WorkerState) -> None:
        if state is not self.state:
            self.state = state
            self.state_changed.emit(state)

    def _on_started(self) -> None:
        self._job = WinJob()
        self._job.assign(self._proc.processId())
        for line in self.spec.init_lines:
            data = (line + self.spec.line_ending).encode(self.spec.encoding_in,
                                                         "replace")
            self._proc.write(data)
        if self.state is WorkerState.STARTING:  # not already stopping
            self._set_state(WorkerState.RUNNING)
            for line in self._queued_lines:  # typed while starting
                self._write_line(line)
        self._queued_lines.clear()
        self.started.emit(self._proc.processId())

    def _on_ready_out(self) -> None:
        self._pend["stdout"] += bytes(self._proc.readAllStandardOutput().data())
        self._cap_pending("stdout")
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _on_ready_err(self) -> None:
        self._pend["stderr"] += bytes(self._proc.readAllStandardError().data())
        self._cap_pending("stderr")
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _cap_pending(self, stream: str) -> None:
        buf = self._pend[stream]
        if len(buf) <= PENDING_CAP:
            return
        # Newline-aligned splice points: an arbitrary byte cut can land
        # mid-UTF-8-sequence (making the strict decode of the WHOLE flush
        # fall back to cp437 mojibake) or mid-ANSI-escape. Fall back to the
        # raw offsets only if a window somehow contains no newline at all.
        head_end = buf.rfind(b"\n", 0, CAP_KEEP_HEAD) + 1
        if head_end <= 0:
            head_end = CAP_KEEP_HEAD
        tail_start = buf.find(b"\n", len(buf) - CAP_KEEP_TAIL)
        tail_start = tail_start + 1 if tail_start != -1 else len(buf) - CAP_KEEP_TAIL
        dropped = tail_start - head_end
        marker = f"\n[... {dropped} bytes dropped (output flood) ...]\n"
        self._pend[stream] = (buf[:head_end] + marker.encode("ascii")
                              + buf[tail_start:])

    def _flush(self) -> None:
        any_pending = False
        for stream, buf in self._pend.items():
            dec = self._dec[stream]
            if buf:
                any_pending = True
                text = dec.feed(bytes(buf))
                buf.clear()
                if text:
                    self.output.emit(stream, text.replace("\r\n", "\n"))
            elif dec.has_tail:
                stale = dec.flush_stale()
                if stale:
                    self.output.emit(stream, stale.replace("\r\n", "\n"))
        if not any_pending and not any(d.has_tail for d in self._dec.values()):
            self._flush_timer.stop()

    def _grace_kill(self, gen: int) -> None:
        if gen == self._gen and self.state is WorkerState.STOPPING:
            self.kill()

    def _on_finished(self, code: int, status) -> None:
        self._flush_timer.stop()
        self._flush()
        for stream in ("stdout", "stderr"):
            tail = self._dec[stream].flush() if stream in self._dec else ""
            if tail:
                self.output.emit(stream, tail.replace("\r\n", "\n"))
        crashed = status == QProcess.ExitStatus.CrashExit
        self.exit_info = (code, crashed)
        if self._job:
            self._job.close()
            self._job = None
        self._set_state(WorkerState.DEAD)
        self.finished.emit(code, crashed)
        if self._restart_pending:
            self._restart_pending = False
            self._set_state(WorkerState.IDLE)
            self.start()

    def _on_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            if self._job:
                self._job.close()
                self._job = None
            self._set_state(WorkerState.DEAD)
            self.failed.emit(
                f"failed to start: {self.spec.program} "
                f"({self._proc.errorString() if self._proc else 'unknown'})")
