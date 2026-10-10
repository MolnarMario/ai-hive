"""Authoritative Claude-hook reporting: live conversation ids AND "needs user".

This one stdlib-only script backs two shared-`--settings` hook families that AI
Hive injects into every Claude agent. The original purpose (below) is live-
conversation tracking via `SessionStart`. It ALSO carries the authoritative
"the agent is waiting for the user" signal that drives the sidebar "?" and the
notification chime: PreToolUse on AskUserQuestion/ExitPlanMode (the only
reliable way to catch those — they render no numbered menu the screen scrape
can see and fire no Notification hook, per claude-code#59908), PostToolUse to
clear it, and Stop to close a lingering prompt and detect a plain free-text
question the agent ended its turn on. These prompt events are EDGES appended to
a separate events file and read incrementally by the manager; SessionStart
records stay in the mapping file. See write_settings_file / read_prompt_events.

A third family, the agent-lanes hooks, goes ONLY to laned agents, whatever
the lanes toggles say (its own settings file, `lanes=True`): PostToolUse on
the edit tools warns when another lane changed the same file, and
UserPromptSubmit injects unread lane notices. Both print
hookSpecificOutput.additionalContext. The Stop hook above also keeps a
laned agent going once when it ends a turn with uncommitted lane changes
(lane_stop_decision), printed as the Stop hook's {"decision": "block"}.
Nothing else is ever printed. Their data files (lanes.json, notices, seen)
are written by app/lane_service.py; the formats live here, next to their
reader. See the "agent lanes" section below.


AI Hive pins each Claude agent to a conversation with `--session-id`/`--resume`,
but the LIVE conversation can drift out from under that pin: the user runs
`/resume` INSIDE the TUI and switches conversations, a `/clear` starts a new one,
or a session forks. AI Hive never sees the child's real current id — and in a
folder with two-plus agents the filesystem CANNOT say which agent owns which
transcript (a resume touches them all), so mtime correlation can only guess and
a wrong guess swaps or strands live conversations (a real, repeated incident).

The fix is to stop guessing and have the child report the truth. Claude fires a
`SessionStart` hook on launch AND on every in-TUI conversation switch
(`source` in {startup, resume, clear, compact}), and the payload carries the id
and transcript_path now in effect (verified live against Claude 2.1.197 in
AI Hive's exact launch config: interactive PTY, sanitized env, `--settings`
injection). AI Hive:

  * writes ONE shared settings file with this SessionStart hook and passes it to
    every Claude agent via `--settings` (verified: `--settings` honors a `hooks`
    section, and injected hooks are ADDITIVE with the user's own — they are
    never clobbered);
  * sets a per-agent `AIHIVE_AGENT_ID` env var (the TerminalAgent.id) so each
    hook line is attributable to exactly one agent — the signal the filesystem
    could not give, which is what makes multi-agent folders safe;
  * reads the resulting mapping file on the session-sync timer and in
    `closeEvent` (before the final save) to reconcile each agent's pin to its
    real live conversation.

This module is deliberately Qt-free and stdlib-only: when Claude runs the hook
it executes `python session_hook.py <mapping_path>`, and that process must start
instantly with no heavy imports. The same module provides the writer/reader
helpers AI Hive uses, so the record format lives in ONE place.
"""

import json
import os
import sys
import time

# env var carrying the AI Hive agent id (== TerminalAgent.id) into the hook
AGENT_ID_ENV = "AIHIVE_AGENT_ID"

# The interactive tools that pause a turn awaiting the user but render NO
# numbered+caret menu the screen scrape can detect — and (confirmed against
# claude-code#59908) fire NO Notification hook either. A PreToolUse hook on
# these tool names is the ONLY reliable "the agent is now asking you" signal.
WAITING_TOOLS = "AskUserQuestion|ExitPlanMode"
_WAITING_TOOL_SET = set(WAITING_TOOLS.split("|"))

# Agent lanes (app/lanes.py, app/lane_service.py). The edit tools whose
# PostToolUse runs the overlap check, and the per-run env vars that arm the
# lane hooks. MainWindow._arm_agent_mcp sets them ONLY for a laned Claude
# agent, never persisted; every path is
# absolute and lives in the workspace's own .aihive folder (never a lane's).
EDIT_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
_EDIT_TOOL_SET = set(EDIT_TOOLS.split("|"))
LANE_UID_ENV = "AIHIVE_AGENT_UID"        # AgentSpec.uid
LANE_ROOT_ENV = "AIHIVE_LANE_ROOT"       # the agent's own worktree
LANE_REPO_ENV = "AIHIVE_LANE_REPO"       # the main checkout
LANES_INDEX_ENV = "AIHIVE_LANES_INDEX"   # <ws>/.aihive/lanes.json
LANE_NOTICES_ENV = "AIHIVE_NOTICES"      # <ws>/.aihive/notices/<uid>.jsonl
LANE_SEEN_ENV = "AIHIVE_LANE_SEEN"       # <ws>/.aihive/seen/<uid>.json
LANE_ENV_KEYS = (LANE_UID_ENV, LANE_ROOT_ENV, LANE_REPO_ENV, LANES_INDEX_ENV,
                 LANE_NOTICES_ENV, LANE_SEEN_ENV)
LANES_INDEX_VERSION = 1
# a notice that sat unread this long describes a state that has moved on
NOTICE_MAX_AGE_S = 6 * 3600
NOTICE_MAX_SHOWN = 8
SEEN_KEYS_CAP = 2000
# lanes.json older than this (three of LaneService's 15 s polls) counts as
# switched off. AI Hive refreshes `ts` on every poll, so a write of
# "enabled": false that lost a race with a hook reading the file, or an AI
# Hive that is gone, still silences the hooks within this long.
LANES_STALE_S = 45.0
# the turn-end nudge (lane_stop_decision) runs `git status` inside the
# agent's Stop: a slow disk or a locked index must not hold its turn end
# for long, so git gets this long and a timeout means no nudge
STOP_GIT_TIMEOUT_S = 5.0
STOP_FILES_SHOWN = 3
_CREATE_NO_WINDOW = 0x08000000


def _hook_command(mapping_path: str, events_path: str | None = None,
                  python_exe: str | None = None) -> str:
    """The shell command Claude runs for our hooks: this script, told where to
    append. argv[1] is the SessionStart mapping file; argv[2] (optional) is the
    prompt-events file the PreToolUse/PostToolUse/Stop hooks append to. The
    single script dispatches on the event name in the stdin payload, so one
    command string serves every hook. Every path is quoted so spaces in the
    venv/app/session paths (the common case on Windows) survive the shell
    split."""
    py = python_exe or sys.executable
    script = os.path.abspath(__file__)
    cmd = f'"{py}" "{script}" "{mapping_path}"'
    if events_path is not None:
        cmd += f' "{events_path}"'
    return cmd


def write_settings_file(settings_path: str, mapping_path: str,
                        events_path: str | None = None,
                        python_exe: str | None = None,
                        tui: str = "", lanes: bool = False) -> None:
    """Write the shared `--settings` file carrying our hooks, and optionally
    the terminal-renderer choice.

    Everything else is left alone, so passing it to `--settings` adds what we
    need without disturbing any other setting; and because Claude merges hooks
    across sources, the user's own hooks still fire too (verified).
    Best-effort.

    `tui` ("default" | "fullscreen" | "" to omit) selects the CLI's renderer.
    It is NOT a hook and has nothing to do with the matchers below -- it is
    here only because this is already the file every Claude agent is launched
    with. AI Hive passes "default" (the classic main-screen renderer) when it
    wants to own the scrollback: the "fullscreen" renderer keeps its own
    virtualized scrollback inside the alternate screen, which leaves the
    terminal's history buffer permanently empty and the scrollbar with nothing
    to show. Verified on real sessions: 0 lines of history under fullscreen,
    a clean linear conversation under default.

    Two families of hooks, all pointing at this one script:
      * SessionStart -> live-conversation tracking (the original purpose).
      * PreToolUse/PostToolUse/Stop -> the "agent needs the user" signal that
        drives the sidebar "?" and the notification chime. These fire mid-
        session (never at launch, never on prompt-submit), so they don't touch
        the timing-sensitive first-task-submit path the SessionStart `startup`
        matcher famously broke.

    `lanes` adds the agent-lanes family (a SEPARATE settings file, given only
    to laned agents, so no other agent
    pays a Python start-up per edit): PostToolUse on the edit tools warns
    about a real overlap with another lane, and UserPromptSubmit injects
    unread lane notices. Both print `additionalContext` and stay silent
    without the lane env vars. Verified live on Claude Code 2.1.287: both
    reach the model, and a UserPromptSubmit hook does NOT drop the first
    delivered task (it fires after the submit, unlike SessionStart/startup).
    """
    cmd = _hook_command(mapping_path, events_path, python_exe)
    hooks: dict = {
        # Match only the IN-SESSION conversation switches (resume/clear/
        # compact), NOT `startup`. At launch the live id already equals the
        # id AI Hive put on the command line, so a startup firing adds no
        # information -- and firing during startup perturbs the TUI's settle
        # just enough that the first task-submit Enter is dropped and the
        # task never runs (verified live: with `startup` included, a freshly
        # spawned agent silently ignored its first task). Excluding startup
        # keeps exactly the signal we need and leaves launch timing pristine.
        "SessionStart": [
            {"matcher": "resume|clear|compact",
             "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
        ]
    }
    if events_path is not None:
        hooks.update({
            # the instant the agent opens an interactive question/plan prompt
            "PreToolUse": [
                {"matcher": WAITING_TOOLS,
                 "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
            # the user answered it -> the prompt is closed
            "PostToolUse": [
                {"matcher": WAITING_TOOLS,
                 "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
            # turn ended: closes any lingering tool prompt AND lights up if the
            # agent's last message was a plain free-text question
            "Stop": [
                {"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}
            ],
        })
    if lanes:
        lane_hook = [{"type": "command", "command": cmd, "timeout": 10}]
        hooks.setdefault("PostToolUse", []).append(
            {"matcher": EDIT_TOOLS, "hooks": lane_hook})
        hooks["UserPromptSubmit"] = [{"hooks": lane_hook}]
    payload: dict = {"hooks": hooks}
    if tui:
        # deliberately a sibling of `hooks`, never folded into it: the matchers
        # above are timing-critical (see the `startup` exclusion) and a
        # renderer preference must never be edited as if it were part of them
        payload["tui"] = tui
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, settings_path)


def reset_map(mapping_path: str) -> None:
    """Truncate the mapping file at app start. Agent ids are minted fresh every
    run (TerminalAgent.id), so lines from a previous run can never match a live
    agent — but starting clean keeps the file small and the read unambiguous."""
    try:
        open(mapping_path, "w", encoding="utf-8").close()
    except OSError:
        pass


def read_live_map(mapping_path: str) -> dict:
    """{agent_id: latest_record} from the mapping file. Each record is
    {session_id, transcript_path, cwd, source, ts}. Later lines win, so the
    result is each agent's CURRENT live conversation. Never raises -> {}."""
    out: dict = {}
    try:
        with open(mapping_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                aid = rec.get("agent_id")
                if aid:
                    out[aid] = rec  # last write for this agent wins
    except OSError:
        pass
    return out


def reset_events(events_path: str) -> None:
    """Truncate the prompt-events file at app start (see reset_map)."""
    try:
        open(events_path, "w", encoding="utf-8").close()
    except OSError:
        pass


# prompt-event kinds appended to the events file. Two targets, each a sticky
# boolean on the agent: "tool" (an AskUserQuestion/ExitPlanMode prompt is open)
# and "turn" (the agent ended a turn on a free-text question).
EV_TOOL_SET = "tool_set"
EV_TOOL_CLEAR = "tool_clear"
EV_TURN_SET = "turn_set"
EV_TURN_CLEAR = "turn_clear"


def read_prompt_events(events_path: str, offset: int = 0):
    """Read NEW prompt-event lines appended since `offset`. Returns
    (records, new_offset). Events are EDGES applied once (not a level to
    reconcile), so reading incrementally is essential — a stale line must never
    be re-applied after a local output-clear. Only complete (newline-
    terminated) lines are consumed; a half-written trailing line is left for the
    next read. Never raises."""
    records: list = []
    try:
        size = os.path.getsize(events_path)
    except OSError:
        return records, offset
    if size < offset:      # file was truncated/rotated -> start over
        offset = 0
    try:
        with open(events_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return records, offset
    # consume only up to the last newline; keep the tail for next time
    nl = data.rfind(b"\n")
    if nl < 0:
        return records, offset
    consumed = data[:nl + 1]
    new_offset = offset + len(consumed)
    for line in consumed.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("agent_id") and rec.get("kind"):
            records.append(rec)
    return records, new_offset


def _ends_with_question(text) -> bool:
    """True if `text` (an assistant message) ends on a question. Trailing
    whitespace and common wrapping punctuation (quotes/backticks/asterisks/
    parens) are stripped first so `...want that?"` or `...right?)` still count.
    Requiring the FINAL char to be '?' keeps ordinary prose (which merely
    CONTAINS questions) from lighting the indicator."""
    if not isinstance(text, str):
        return False
    return text.rstrip(" \t\r\n\"'`*)]}").endswith("?")


def _append_event(events_path: str, kind: str, extra: dict | None = None) -> None:
    record = {"agent_id": os.environ.get(AGENT_ID_ENV, ""),
              "kind": kind, "ts": time.time()}
    if extra:
        record.update(extra)
    line = json.dumps(record) + "\n"
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _append_record(mapping_path: str, payload: dict) -> None:
    record = {
        "agent_id": os.environ.get(AGENT_ID_ENV, ""),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"),
        "source": payload.get("source"),
        "ts": time.time(),
    }
    # single pre-built line + append mode: concurrent hooks from sibling agents
    # interleave at line granularity at worst, which the reader tolerates.
    line = json.dumps(record) + "\n"
    with open(mapping_path, "a", encoding="utf-8") as fh:
        fh.write(line)


# ------------------------------------------------------------ agent lanes ---
# The overlap hook and lane notices (agent lanes Phase 2). AI Hive's LaneService WRITES lanes.json and the notices; the hooks
# below READ them, so the file formats live here, in one place, next to
# their reader. Everything is best-effort: a hook problem prints nothing.

def _write_json_atomic(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        # Windows refuses the replace while another process (a hook reading
        # lanes.json) has the target open
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_lanes_index(path: str, data: dict) -> None:
    """lanes.json: {version, enabled, ts, repo, lanes: {uid: {..., fork}},
    files: {repo path: [{uid, agent, branch, state}]}}. `enabled` False
    (the switch went off) silences every lane hook at once, mid-run, and so
    does a `ts` older than LANES_STALE_S."""
    _write_json_atomic(path, data)


def lanes_index_live(index, now: float = 0.0) -> bool:
    """Should the lane hooks act on this lanes.json? Only when it says
    enabled and was written within LANES_STALE_S."""
    if not isinstance(index, dict) or not index.get("enabled"):
        return False
    ts = index.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return (now or time.time()) - ts <= LANES_STALE_S


def overlap_key(path: str, peer: str, level: str, fork: str = "",
                peer_fork: str = "") -> str:
    """The dedupe key of one overlap, shared by LaneService's notices
    (lanes.Overlap.key) and the overlap hook, so an agent is told once
    whichever got there first. `peer` is a lane uid or "base". The forks
    (`git merge-base` of each lane with its base) are the round of work: a
    lane that landed its work and moved on to the new base has a new fork,
    so the same file overlapping again later is news again."""
    return f"{path}|{peer}|{level}|{fork[:12]}|{peer_fork[:12]}"


def read_lanes_index(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def append_notice(path: str, key: str, text: str, ts: float = 0.0) -> None:
    """One lane notice for one agent, injected on its next prompt. `key`
    dedupes against overlap warnings the agent already got (see _seen)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": ts or time.time(), "key": key, "text": text})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_seen(path: str) -> dict:
    """{"keys": [...], "offset": int}: what this agent was already told
    (overlap warnings and notices share the keys) and how far into its
    notices file it has read. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    keys = data.get("keys")
    offset = data.get("offset")
    return {"keys": [k for k in keys if isinstance(k, str)]
            if isinstance(keys, list) else [],
            "offset": offset if isinstance(offset, int) and offset >= 0
            else 0}


def _save_seen(path: str, seen: dict) -> None:
    seen["keys"] = seen["keys"][-SEEN_KEYS_CAP:]
    _write_json_atomic(path, seen)


def _lane_env(env) -> dict | None:
    """The lane env vars, or None when this agent's lane hooks are not
    armed (the agent had no lane at launch)."""
    vals = {k: env.get(k, "") for k in LANE_ENV_KEYS}
    return vals if all(vals.values()) else None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(path: str, folder: str) -> bool:
    try:
        rel = os.path.relpath(path, folder)
    except ValueError:
        return False
    return not (rel == os.pardir or rel.startswith(os.pardir + os.sep))


def lane_overlap_context(payload: dict, env=None) -> str:
    """PostToolUse on an edit tool: text to put in front of the agent when
    the file it just edited is one another lane also changed, or one its
    base changed since the lane forked, or when it edited outside its own
    lane (the main checkout or another agent's lane). "" otherwise, and
    once per (file, peer, level): repeats are tracked in the seen file."""
    lane = _lane_env(os.environ if env is None else env)
    if lane is None:
        return ""
    index = read_lanes_index(lane[LANES_INDEX_ENV])
    if not lanes_index_live(index):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str) or not target:
        return ""
    if not os.path.isabs(target):
        target = os.path.join(str(payload.get("cwd") or lane[LANE_ROOT_ENV]),
                              target)
    target = os.path.normpath(target)
    root, repo = lane[LANE_ROOT_ENV], lane[LANE_REPO_ENV]
    uid = lane[LANE_UID_ENV]
    seen = read_seen(lane[LANE_SEEN_ENV])
    known = set(seen["keys"])
    lines = []
    lanes_info = index.get("lanes") or {}
    mine = lanes_info.get(uid) or {}

    def fork_of(info) -> str:
        fork = info.get("fork") if isinstance(info, dict) else ""
        return fork if isinstance(fork, str) else ""

    if _inside(target, root):
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        for owner in index.get("files", {}).get(rel, []) or []:
            if not isinstance(owner, dict) or owner.get("uid") == uid:
                continue
            level = ("conflicts" if owner.get("state") == "conflicts"
                     else "overlap")
            key = overlap_key(rel, str(owner.get("uid")), level,
                              fork_of(mine),
                              fork_of(lanes_info.get(owner.get("uid"))))
            if key in known:
                continue
            known.add(key)
            seen["keys"].append(key)
            who = f"{owner.get('agent', 'another agent')} " \
                  f"({owner.get('branch', '?')})"
            if level == "conflicts":
                lines.append(f"{who} changed {rel} too, and the two versions "
                             f"would CONFLICT when merged. Keep your change "
                             f"there minimal and coordinate through the "
                             f"board.")
            else:
                how = ("uncommitted" if owner.get("state") == "dirty"
                       else "committed")
                lines.append(f"{who} also changed {rel} ({how}). Keep your "
                             f"change there small and local; the integrator "
                             f"merges both.")
        base = mine.get("base_ref") or "the base branch"
        if rel in (mine.get("base_changed") or []):
            key = overlap_key(rel, "base", "overlap", fork_of(mine))
            if key not in known:
                known.add(key)
                seen["keys"].append(key)
                lines.append(f"{base} changed {rel} since your lane started. "
                             f"Merge {base} into your branch before you "
                             f"change it much further.")
    elif _inside(target, repo) or _inside(
            target, os.path.normpath(repo) + ".lanes"):
        key = f"outside|{_norm(target)}"
        if key not in known:
            seen["keys"].append(key)
            lines.append(f"You edited {target}, which is OUTSIDE your lane "
                         f"{root}. Make your changes in your own lane, never "
                         f"in the main checkout or another agent's lane.")
    if not lines:
        return ""
    try:
        _save_seen(lane[LANE_SEEN_ENV], seen)
    except OSError:
        pass
    return "AI Hive lane notice: " + " ".join(lines)


def lane_notice_context(env=None, now: float = 0.0) -> str:
    """UserPromptSubmit: the agent's unread lane notices (written by
    AI Hive's LaneService: the base moved under a file it changed, another
    lane started changing one of its files). Skips what the overlap hook
    already told it, drops stale ones, and remembers how far it read."""
    lane = _lane_env(os.environ if env is None else env)
    if lane is None:
        return ""
    index = read_lanes_index(lane[LANES_INDEX_ENV])
    if not lanes_index_live(index, now):
        return ""
    seen = read_seen(lane[LANE_SEEN_ENV])
    path = lane[LANE_NOTICES_ENV]
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    offset = seen["offset"] if seen["offset"] <= size else 0
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return ""
    nl = data.rfind(b"\n")
    if nl < 0:
        return ""
    seen["offset"] = offset + nl + 1
    known = set(seen["keys"])
    now = now or time.time()
    texts = []
    for raw in data[:nl + 1].decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("text"), str):
            continue
        key = str(rec.get("key") or "")
        if key and key in known:
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and now - ts > NOTICE_MAX_AGE_S:
            continue
        if key:
            known.add(key)
            seen["keys"].append(key)
        texts.append(rec["text"])
    try:
        _save_seen(lane[LANE_SEEN_ENV], seen)
    except OSError:
        pass
    if not texts:
        return ""
    more = len(texts) - NOTICE_MAX_SHOWN
    shown = texts[:NOTICE_MAX_SHOWN]
    body = "\n".join(f"- {t}" for t in shown)
    if more > 0:
        body += f"\n- ...and {more} more like these."
    return ("AI Hive lane notices (other agents' lanes and the base branch "
            "changed files you also changed):\n" + body)


def printable(text: str) -> str:
    """Text from outside AI Hive (a file name, an agent name) as plain text
    for a model or a notice: control characters (an ESC, a newline in a
    name) dropped. MainWindow uses it too."""
    return "".join(ch for ch in text
                   if ch >= " " and not "\x7f" <= ch <= "\x9f")


def _lane_dirty_files(root: str):
    """Paths with uncommitted changes in the lane, or None when git fails,
    times out or is missing: then the caller can't tell, and stays quiet."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain", "-z"],
            cwd=root, capture_output=True, timeout=STOP_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                 "GCM_INTERACTIVE": "never"},
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    toks = r.stdout.decode("utf-8", "replace").split("\0")
    files, i = [], 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if len(tok) < 4:
            continue
        files.append(tok[3:])
        if tok[0] in "RC" or tok[1] in "RC":
            i += 1          # -z puts a rename's old name in the next token
    return files


def lane_stop_decision(payload: dict, env=None, now: float = 0.0) -> str:
    """Stop: the reason to keep a laned agent going once, when it ends a
    turn with uncommitted changes in its lane, or "" to let it stop. The
    lane prompt already says to commit finished work with a "Task done"
    line (lanes.DONE_MARK); this backs it for the agent that forgot.

    Blocks only a laned agent (lane env set) while the lane machinery runs
    (lanes.json live), that is not the integrator (it commits as part of its
    checklist and is never held up), on its first stop of the turn
    (`stop_hook_active` is exactly False, so a Claude that never sends the
    field is never blocked at all), whose last message is not a question (an
    agent waiting for the user stays waiting), and only when `git status`
    in the lane works and lists something. Anything unreadable means no
    block: a missed nudge costs nothing, a wrong one costs a turn."""
    lane = _lane_env(os.environ if env is None else env)
    if lane is None or payload.get("stop_hook_active") is not False:
        return ""
    if _ends_with_question(payload.get("last_assistant_message")):
        return ""
    index = read_lanes_index(lane[LANES_INDEX_ENV])
    if not lanes_index_live(index, now):
        return ""
    entry = (index.get("lanes") or {}).get(lane[LANE_UID_ENV])
    # "" is an ordinary lane; a missing role means an index this AI Hive
    # didn't write, and the integrator must never be the one held up
    if not isinstance(entry, dict) or entry.get("role") != "":
        return ""
    files = _lane_dirty_files(lane[LANE_ROOT_ENV])
    if not files:
        return ""
    n = len(files)
    names = ", ".join(printable(f)[:120] for f in files[:STOP_FILES_SHOWN])
    if n > STOP_FILES_SHOWN:
        names += f", +{n - STOP_FILES_SHOWN} more"
    return (f"AI Hive: your lane has {n} uncommitted "
            f"file{'' if n == 1 else 's'} ({names}). If the task you just did "
            f"is finished, have GPT-6-Luna review it as your lane "
            f"instructions say and fix what holds up, then commit them on "
            f"your lane branch now, ending the commit message with a line "
            f"of just \"Task done\". If it is not "
            f"finished, or the user asked you not to commit, end your turn "
            f"without committing.")


def _dispatch(mapping_path, events_path, payload: dict) -> str:
    """Route one hook payload to the right file/record by its event name.
    Returns text for the model ("" for none): the context the lane hooks
    add, or for a Stop the reason it is blocked (lane_stop_decision)."""
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        if mapping_path:
            _append_record(mapping_path, payload)
        return ""
    if event == "UserPromptSubmit":
        return lane_notice_context()
    if event == "PostToolUse" and payload.get("tool_name") in _EDIT_TOOL_SET:
        return lane_overlap_context(payload)
    if event == "Stop":
        # a turn ended: any in-flight tool prompt is resolved (covers the user
        # cancelling an AskUserQuestion, where no PostToolUse fires), and the
        # agent is now waiting iff its last message was a plain question.
        # Written even when the stop is blocked below: the manager moves the
        # reply's stamp to a later Stop of the same turn (note_reply_stopped)
        if events_path:
            try:
                _append_event(events_path, EV_TOOL_CLEAR, {"tool": "stop"})
                if _ends_with_question(payload.get("last_assistant_message")):
                    _append_event(events_path, EV_TURN_SET)
                else:
                    _append_event(events_path, EV_TURN_CLEAR)
            except OSError:
                pass
        return lane_stop_decision(payload)
    if not events_path:
        return ""
    if event == "PreToolUse":
        if payload.get("tool_name") in _WAITING_TOOL_SET:
            _append_event(events_path, EV_TOOL_SET,
                          {"tool": payload.get("tool_name")})
    elif event == "PostToolUse":
        if payload.get("tool_name") in _WAITING_TOOL_SET:
            _append_event(events_path, EV_TOOL_CLEAR,
                          {"tool": payload.get("tool_name")})
    return ""


def main(argv) -> int:
    """Hook entrypoint: read the hook JSON from stdin, stamp it with the
    per-agent env id, append the matching record(s). argv[1] is the SessionStart
    mapping file; argv[2] (optional) is the prompt-events file. Silent + best-
    effort: a hook must NEVER break the Claude session it is attached to, so
    it always exits 0. It prints only the lane hooks' text: context as
    hookSpecificOutput.additionalContext, which the CLI hands the model, and
    a blocked Stop as {"decision": "block", "reason"}, the Stop hook's own
    shape (verified on Claude Code 2.1.291: the agent continues with the
    reason, and its next Stop carries stop_hook_active true)."""
    mapping_path = argv[1] if len(argv) > 1 else None
    events_path = argv[2] if len(argv) > 2 else None
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    context, event = "", ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            event = str(payload.get("hook_event_name") or "")
            context = _dispatch(mapping_path, events_path, payload) or ""
    except Exception:
        context = ""
    if context and event:
        if event == "Stop":
            out = {"decision": "block", "reason": context}
        else:
            out = {"hookSpecificOutput": {"hookEventName": event,
                                          "additionalContext": context}}
        try:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
