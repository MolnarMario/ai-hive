"""Orchestrator control channel: a GUI-side named-pipe RPC server.

An "Orchestrator" agent (a Claude Code session launched with --mcp-config) talks
to a stdlib-only MCP server (app/mcp_server.py) which forwards each tool call
over a Windows named pipe to THIS server, running inside the GUI process. Because
QLocalServer delivers its signals on the GUI thread, the executor can call
WorkspaceManager methods directly — no cross-thread marshaling.

Wire protocol on the pipe: newline-delimited JSON. Request
{"id","op","args","ws"} -> Response {"id","ok":true,"result"} or
{"id","ok":false,"error":{"code","message"}}. "ws" is the caller's BINDING
workspace (from its per-workspace mcp config): the executor confines every
lookup and mutation to it, so an orchestrator can never see or touch agents
in another workspace; "" (legacy config) keeps the old global behavior. This
is a private RPC, distinct from the MCP JSON-RPC the server speaks to the CLI
on its own stdio.

Everything is additive and guarded: if QtNetwork/listen fails the bridge just
disables itself and the base app is unaffected.
"""

import json
import os
import secrets

from PySide6.QtCore import QObject, QStandardPaths

try:
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    HAS_QTNETWORK = True
except ImportError:  # pragma: no cover
    QLocalServer = QLocalSocket = None
    HAS_QTNETWORK = False

from .terminal_agent import AssignmentState

_STATE_BY_NAME = {s.value: s for s in AssignmentState}

# ops that mutate persisted state -> trigger an immediate session save
_MUTATING_OPS = {"spawn_agent", "assign_task", "reassign_agent",
                 "set_agent_state", "close_agent"}

# ops only an ORCHESTRATOR may call. Workers get a config that exposes just
# log_activity (via --allowedTools), but the bridge ALSO refuses these ops for
# role=="worker" as defense in depth — a worker can never spawn, retask, or
# close a peer even if its tool list were somehow widened. Same enforcement
# shape as the AIHIVE_WS workspace scoping.
_ORCHESTRATOR_ONLY_OPS = {"spawn_agent", "assign_task", "reassign_agent",
                          "set_agent_state", "close_agent"}


def _appdata_dir() -> str:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    os.makedirs(base, exist_ok=True)
    return base


class OrchestratorBridge(QObject):
    """Named-pipe RPC server that maps orchestrator ops onto WorkspaceManager."""

    def __init__(self, manager, active_ws=None, parent=None, on_mutation=None):
        super().__init__(parent)
        self.manager = manager
        self._active_ws = active_ws or (lambda: manager.active_id)
        # called after any successful mutating op so the session saves
        # immediately (a debounce window is a loss window on kill)
        self._on_mutation = on_mutation
        self.enabled = False
        self.pipe_name = ""
        self._server = None
        self._buffers = {}   # QLocalSocket -> bytearray
        self.endpoint_path = os.path.join(_appdata_dir(), "orchestrator.json")
        self._mcp_dir = os.path.join(_appdata_dir(), "mcp")

    # ------------------------------------------------------------ lifecycle ---

    def start(self) -> bool:
        if not HAS_QTNETWORK:
            return False
        token = secrets.token_hex(4)
        self.pipe_name = f"aihive-{os.getpid()}-{token}"
        self._server = QLocalServer(self)
        QLocalServer.removeServer(self.pipe_name)  # clear any stale pipe
        if not self._server.listen(self.pipe_name):
            self._server = None
            return False
        self._server.newConnection.connect(self._on_connection)
        self.enabled = True
        self._write_endpoint()
        self._purge_stale_configs()  # old configs point at dead pipes
        return True

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
            for p in (self.endpoint_path,):
                if os.path.isfile(p):
                    os.remove(p)
        except OSError:
            pass
        self.enabled = False

    def _write_endpoint(self) -> None:
        try:
            with open(self.endpoint_path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "pipe": self.pipe_name,
                           "pid": os.getpid()}, f)
        except OSError:
            pass

    def _purge_stale_configs(self) -> None:
        """Configs from previous runs reference dead pipes — remove them so
        nothing can ever launch against a stale endpoint."""
        try:
            for f in os.listdir(self._mcp_dir):
                if f.startswith("aihive") and f.endswith(".json"):
                    os.remove(os.path.join(self._mcp_dir, f))
        except OSError:
            pass

    def mcp_config_path_for(self, ws_id: str, role: str = "orchestrator") -> str:
        """Write and return the --mcp-config for an agent BOUND to one
        workspace and ROLE. AIHIVE_WS + AIHIVE_ROLE ride in the server env and
        are echoed back with every RPC, so the executor enforces that (a) the
        agent only touches its own workspace and (b) a 'worker' can only
        log_activity — never spawn/retask/close a peer."""
        import sys
        path = os.path.join(self._mcp_dir, f"aihive-{role}-{ws_id[:12]}.json")
        try:
            os.makedirs(self._mcp_dir, exist_ok=True)
            cfg = {"mcpServers": {"aihive": {
                "command": sys.executable,
                "args": ["-m", "app.mcp_server"],
                "env": {"AIHIVE_PIPE": self.pipe_name,
                        "AIHIVE_WS": ws_id,
                        "AIHIVE_ROLE": role,
                        "PYTHONPATH": str(os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__))))},
            }}}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            return ""
        return path

    # -------------------------------------------------------------- server ---

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._buffers.pop(s, None))

    def _on_ready(self, sock) -> None:
        self._buffers[sock] += bytes(sock.readAll().data())
        while b"\n" in self._buffers[sock]:
            line, _, rest = self._buffers[sock].partition(b"\n")
            self._buffers[sock] = bytearray(rest)
            self._handle_line(sock, bytes(line))

    def _handle_line(self, sock, line: bytes) -> None:
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        rid = req.get("id")
        try:
            op = req.get("op", "")
            result = self._dispatch(op, req.get("args") or {},
                                    ws=req.get("ws") or "",
                                    role=req.get("role") or "")
            resp = {"id": rid, "ok": True, "result": result}
            if op in _MUTATING_OPS and self._on_mutation is not None:
                try:
                    self._on_mutation()  # persist NOW, not after a debounce
                except Exception:
                    pass
        except _RpcError as e:
            resp = {"id": rid, "ok": False,
                    "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # never let the executor crash the GUI
            resp = {"id": rid, "ok": False,
                    "error": {"code": "internal", "message": repr(e)}}
        try:
            sock.write((json.dumps(resp) + "\n").encode("utf-8"))
            sock.flush()
        except Exception:
            pass

    # ------------------------------------------------------------ executor ---
    # Runs on the GUI thread (QLocalServer signals are GUI-thread), so it may
    # call WorkspaceManager mutations directly. It NEVER opens a dialog or
    # spins a nested loop (reentrancy safety); the agent-limit path returns a
    # structured error rather than a QMessageBox.

    def _target_ws(self, ws_id: str) -> str:
        return ws_id or self._active_ws() or ""

    def _resolve_scoped(self, ref: str, ws_id: str):
        """Resolve an agent by id or (case-insensitive) name, WITHIN the
        binding workspace when one is set. A bound orchestrator can never
        reach an agent in another workspace, and duplicate names in other
        workspaces can't be matched by accident. Legacy connections without
        a binding fall back to the global lookup."""
        if not ws_id:
            return self.manager.resolve_agent(ref)
        w = self.manager.workspace(ws_id)
        if w is None:
            return None
        for a in w.agents:
            if a.id == ref:
                return a
        low = (ref or "").strip().lower()
        for a in w.agents:
            if a.spec.name.lower() == low:
                return a
        return None

    def _agent_info(self, agent, ws_id: str) -> dict:
        row = agent.roster_row()
        row.update({"agent_id": agent.id, "workspace_id": ws_id,
                    "auto_created": agent.auto_created,
                    "running": agent.is_running()})
        return row

    def _dispatch(self, op: str, args: dict, ws: str = "", role: str = "") -> dict:
        """Execute one op. `ws` is the caller's BINDING workspace and `role` its
        binding role (both from its per-workspace mcp config): every lookup and
        mutation is confined to the workspace, and a 'worker' role is refused
        the orchestration ops. Empty ws/role (legacy config) keep the old
        global orchestrator behavior."""
        m = self.manager
        if role == "worker" and op in _ORCHESTRATOR_ONLY_OPS:
            raise _RpcError("forbidden", "worker agents may only log_activity "
                                         "and read; only the orchestrator can "
                                         "spawn, retask, or close agents")
        if op == "log_activity":
            ws_id = ws or self._target_ws("")
            wobj = m.workspace(ws_id)
            if wobj is None or wobj.board is None:
                raise _RpcError("bad_args", "no board for this workspace")
            name = args.get("agent") or args.get("name") or "agent"
            ok = wobj.board.append_activity(name, args.get("message", ""))
            return {"logged": bool(ok), "workspace_id": ws_id}

        if op == "spawn_agent":
            requested = args.get("workspace_id", "")
            if ws and requested and requested != ws:
                raise _RpcError("scope", "this orchestrator is bound to its "
                                         "own workspace and cannot spawn "
                                         "agents elsewhere")
            ws_id = ws or self._target_ws(requested)
            if m.workspace(ws_id) is None:
                raise _RpcError("bad_args", "workspace not found (was it "
                                            "deleted?)" if ws else
                                            "no active workspace")
            agent = m.spawn_worker(ws_id, args.get("task", ""),
                                   role=args.get("role", ""),
                                   model=args.get("model", ""),
                                   effort=args.get("effort", ""))
            if agent is None:
                raise _RpcError(
                    "limit_reached",
                    "workspace is at its agent cap — reassign an idle/completed "
                    "agent (reassign_agent) or ask the user to close one")
            return self._agent_info(agent, ws_id)

        if op == "assign_task":
            agent = self._resolve_scoped(args.get("agent", ""), ws)
            if agent is None:
                raise _RpcError("no_such_agent", "agent not found in this "
                                                 "workspace")
            was_idle = agent.assignment in (AssignmentState.IDLE,
                                            AssignmentState.COMPLETED,
                                            AssignmentState.AWAITING)
            wsp = m.workspace_of(agent.id)
            m.assign_task(wsp.id if wsp else "", agent.id, args.get("task", ""),
                          role=args.get("role", ""))
            return {"agent_id": agent.id, "name": agent.spec.name,
                    "delivered": True, "was_idle": was_idle}

        if op == "reassign_agent":
            agent = self._resolve_scoped(args.get("agent_id", ""), ws)
            if agent is None:
                raise _RpcError("no_such_agent", "agent not found in this "
                                                 "workspace")
            ok = m.reassign_agent(agent.id, args.get("task", ""),
                                  role=args.get("role", ""))
            return {"agent_id": agent.id, "name": agent.spec.name,
                    "role": agent.spec.role, "delivered": ok}

        if op == "set_agent_state":
            agent = self._resolve_scoped(args.get("agent_id", ""), ws)
            state = _STATE_BY_NAME.get(args.get("state", ""))
            if state is None:
                raise _RpcError("bad_args", "unknown state")
            if agent is None or not m.set_assignment_state(agent.id, state):
                raise _RpcError("no_such_agent", "agent not found in this "
                                                 "workspace")
            return {"agent_id": agent.id, "state": state.value}

        if op == "list_agents":
            ws_id = ws or args.get("workspace_id", "")
            agents = []
            for w in m.workspaces:
                if ws_id and w.id != ws_id:
                    continue
                for a in w.agents:
                    agents.append(self._agent_info(a, w.id))
            return {"agents": agents}

        if op == "get_agent_output":
            agent = self._resolve_scoped(args.get("agent_id", ""), ws)
            if agent is None:
                raise _RpcError("no_such_agent", "agent not found in this "
                                                 "workspace")
            lines = int(args.get("lines", 80) or 80)
            return {"agent_id": agent.id, "text": _agent_output(agent, lines)}

        if op == "close_agent":
            agent = self._resolve_scoped(args.get("agent_id", ""), ws)
            if agent is None:
                raise _RpcError("no_such_agent", "agent not found in this "
                                                 "workspace")
            if args.get("force"):
                wsp = m.workspace_of(agent.id)
                m.remove_terminal(wsp.id if wsp else "", agent.id)
                return {"agent_id": agent.id, "closed": True, "mode": "removed"}
            # soft close honors rule #9 — mark completed, keep the card
            m.set_assignment_state(agent.id, AssignmentState.COMPLETED)
            return {"agent_id": agent.id, "closed": False, "mode": "soft"}

        raise _RpcError("bad_args", f"unknown op {op!r}")


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _agent_output(agent, lines: int) -> str:
    """Best-effort recent output. For pty agents, render the raw VT tail through
    a throwaway pyte screen so the orchestrator gets clean text (not a scrape).

    This runs on the GUI thread (QLocalServer signals) while the MCP client
    blocks on an RPC timeout, so the pyte feed MUST be bounded: rendering the
    full 512 KB PTY buffer could stall the event loop past the timeout. Only
    the recent tail is needed to fill `lines` rows — feed at most that."""
    lines = max(1, min(400, lines))
    if agent.is_pty:
        try:
            import pyte
            rows_wanted = max(lines, 40)
            # a wide row is ~200 chars; keep a generous multiple so wrapped
            # lines and escape sequences still fill the screen, but cap it far
            # below the 512 KB buffer so the feed is always cheap
            tail = agent.pty_replay()[-(rows_wanted * 400):]
            screen = pyte.Screen(120, rows_wanted)
            pyte.Stream(screen).feed(tail)
            rows = [r.rstrip() for r in screen.display]
            return "\n".join(rows).strip("\n")
        except Exception:
            return ""
    return "".join(t for _s, t in list(agent.log)[-lines:])
