"""AI Hive MCP server — the orchestrator's control channel.

A stdlib-only MCP stdio server the Claude CLI spawns via --mcp-config. It speaks
newline-delimited JSON-RPC 2.0 on its own stdio (protocol contract verified
against Claude Code 2.1.197: initialize → notifications/initialized →
tools/list → tools/call, framing = one JSON object per line) and forwards each
tool call over a Windows named pipe to the running AI Hive GUI
(app.orchestrator_bridge). MUST NOT import PySide6 — it is a lightweight child
of the CLI.

Run as:  python -m app.mcp_server   (with AIHIVE_PIPE in env, set by the
bridge-generated mcp config; falls back to %LOCALAPPDATA%/AIHive/orchestrator.json).
AIHIVE_WS (same config) is the workspace this orchestrator is BOUND to — it is
echoed with every RPC and enforced by the GUI, scoping all tools to that
workspace.
"""

import json
import os
import sys
import threading
import uuid

PROTOCOL_VERSION = "2025-11-25"


# ---------------------------------------------------------------- tools ----

TOOLS = [
    {"name": "spawn_agent",
     "description": "Create a NEW worker agent in the workspace and give it a "
                    "task. It appears immediately in the grid and stays visible "
                    "until the user closes it. Omit model/effort to auto-select "
                    "by task (trivial→haiku, mid→sonnet, architecture→opus). "
                    "Prefer reassign_agent to reuse an idle/completed agent "
                    "before spawning a new one.",
     "inputSchema": {"type": "object", "properties": {
         "role": {"type": "string", "description": "role/title, e.g. 'Backend Architect'; also the agent's name"},
         "task": {"type": "string", "description": "the task to work on"},
         "model": {"type": "string", "enum": ["", "opus", "sonnet", "haiku", "fable"]},
         "effort": {"type": "string", "enum": ["", "low", "medium", "high", "xhigh", "max"]},
         "workspace_id": {"type": "string", "description": "optional; default = active workspace"}},
         "required": ["task"]}},
    {"name": "assign_task",
     "description": "Send a task to an EXISTING agent (by id or display name). "
                    "The agent's role/name adapt to the task.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "task": {"type": "string"},
         "role": {"type": "string"}}, "required": ["agent", "task"]}},
    {"name": "reassign_agent",
     "description": "Retask an idle/completed agent, preserving its session "
                    "(the task is typed into its existing terminal — no restart, "
                    "no context loss).",
     "inputSchema": {"type": "object", "properties": {
         "agent_id": {"type": "string"}, "task": {"type": "string"},
         "role": {"type": "string"}}, "required": ["agent_id", "task"]}},
    {"name": "set_agent_state",
     "description": "Mark an agent's lifecycle state. Use 'completed' when its "
                    "task is done (the card stays visible with a Completed badge; "
                    "agents never auto-close).",
     "inputSchema": {"type": "object", "properties": {
         "agent_id": {"type": "string"},
         "state": {"type": "string", "enum": ["working", "completed", "idle", "awaiting"]}},
         "required": ["agent_id", "state"]}},
    {"name": "list_agents",
     "description": "Snapshot of every agent (id, name, role, model, status, "
                    "assignment, task). Use this to see what the team is doing.",
     "inputSchema": {"type": "object", "properties": {
         "workspace_id": {"type": "string"}}}},
    {"name": "get_agent_output",
     "description": "Recent terminal output for an agent (best-effort screen "
                    "snapshot). Prefer the shared board for structured progress.",
     "inputSchema": {"type": "object", "properties": {
         "agent_id": {"type": "string"},
         "lines": {"type": "integer", "default": 80, "maximum": 400}},
         "required": ["agent_id"]}},
    {"name": "close_agent",
     "description": "Soft-close (mark Completed, keep the card) by default. "
                    "Only force=true removes the agent — the user normally "
                    "decides when agents disappear.",
     "inputSchema": {"type": "object", "properties": {
         "agent_id": {"type": "string"}, "force": {"type": "boolean", "default": False}},
         "required": ["agent_id"]}},
    {"name": "log_activity",
     "description": "Record a terse one-line activity note on the workspace's "
                    "shared board (the ## Activity log). Use this to tell peers "
                    "what you started / finished / changed. AI Hive serializes "
                    "these writes, so prefer it over editing board.md directly. "
                    "Available to every agent, including workers.",
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "terse one-line note"},
         "agent": {"type": "string", "description": "your display name (optional)"}},
         "required": ["message"]}},
]

_TOOL_TO_OP = {t["name"]: t["name"] for t in TOOLS}


# ------------------------------------------------------------- pipe RPC ----

def _pipe_name() -> str | None:
    name = os.environ.get("AIHIVE_PIPE")
    if name:
        return name
    # fallback: discover via the GUI's endpoint file
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AIHive")
        with open(os.path.join(base, "orchestrator.json"), encoding="utf-8") as f:
            return json.load(f).get("pipe")
    except (OSError, ValueError):
        return None


def _rpc(op: str, args: dict, timeout: float = 20.0) -> dict:
    """One request/response over the named pipe. Returns the bridge's result
    dict or raises RuntimeError('not_reachable'/'timeout'/...)."""
    name = _pipe_name()
    if not name:
        raise RuntimeError("not_reachable: AI Hive is not running")
    path = r"\\.\pipe\%s" % name
    box = {}

    def worker():
        try:
            with open(path, "r+b", buffering=0) as pipe:
                # AIHIVE_WS + AIHIVE_ROLE (set in this server's env by its
                # per-workspace mcp config) bind every op to the launching
                # workspace and role — the GUI enforces both; this is just the
                # identity channel
                req = json.dumps({"id": uuid.uuid4().hex, "op": op,
                                  "args": args,
                                  "ws": os.environ.get("AIHIVE_WS", ""),
                                  "role": os.environ.get("AIHIVE_ROLE", "")})
                pipe.write((req + "\n").encode("utf-8"))
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = pipe.read(1)
                    if not chunk:
                        break
                    buf += chunk
                box["resp"] = json.loads(bytes(buf).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError("timeout: AI Hive did not respond")
    if "err" in box:
        raise RuntimeError(f"not_reachable: {box['err']}")
    resp = box.get("resp") or {}
    if not resp.get("ok"):
        err = resp.get("error", {})
        raise RuntimeError(f"{err.get('code', 'error')}: {err.get('message', '')}")
    return resp.get("result", {})


# --------------------------------------------------------------- stdio ----

def _send(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _tool_result(payload: dict, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": is_error}


def _handle(req: dict):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        params = req.get("params", {})
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aihive", "version": "1.0.0"}}})
    elif method == "notifications/initialized":
        pass  # notification, no response
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        op = _TOOL_TO_OP.get(name)
        if op is None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": _tool_result({"error": f"unknown tool {name}"}, True)})
            return
        try:
            result = _rpc(op, args)
            _send({"jsonrpc": "2.0", "id": rid, "result": _tool_result(result)})
        except Exception as e:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": _tool_result({"error": str(e)}, True)})
    elif rid is not None:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": f"no method {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            _handle(req)
        except Exception:
            pass  # never crash the transport


if __name__ == "__main__":
    main()
