"""Shared-board control channel: a GUI-side named-pipe RPC server.

Every Claude agent is launched with a small --mcp-config that lets it call one
tool — `log_activity` — to post a note to its workspace's shared board. That
call travels: agent's CLI -> a stdlib-only MCP server (app/mcp_server.py) ->
a Windows named pipe -> THIS server, running inside the GUI process. Because
QLocalServer delivers its signals on the GUI thread, the executor can call
WorkspaceManager/board methods directly — no cross-thread marshaling.

(Historically this bridge also carried an "orchestrator" toolset that let one
agent spawn and direct others. That feature was removed; agents coordinate
only through the board now. The class/file keep their old names so existing
imports and the endpoint/config layout stay stable.)

Wire protocol on the pipe: newline-delimited JSON. Request {"id","op","args","ws"}
-> Response {"id","ok":true,"result"} or {"id","ok":false,"error":{"code","message"}}.
"ws" is the caller's BINDING workspace (from its per-workspace mcp config): the
executor confines the note to that workspace's board; "" (legacy config) falls
back to the active workspace. This is a private RPC, distinct from the MCP
JSON-RPC the server speaks to the CLI on its own stdio.

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

# A single RPC request is a short JSON line; cap the per-connection accumulation
# so a client that opens the pipe and streams bytes without a newline can't grow
# GUI memory without bound.
_MAX_REQUEST_BYTES = 1 << 20  # 1 MiB


def _appdata_dir() -> str:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    os.makedirs(base, exist_ok=True)
    return base


class OrchestratorBridge(QObject):
    """Named-pipe RPC server that maps the log_activity op onto the board."""

    def __init__(self, manager, active_ws=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._active_ws = active_ws or (lambda: manager.active_id)
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

    def mcp_config_path_for(self, ws_id: str) -> str:
        """Write and return the --mcp-config for an agent BOUND to one
        workspace. AIHIVE_WS rides in the server env and is echoed back with
        every RPC, so a note always lands on the right workspace's board."""
        import sys
        path = os.path.join(self._mcp_dir, f"aihive-{ws_id[:12]}.json")
        try:
            os.makedirs(self._mcp_dir, exist_ok=True)
            cfg = {"mcpServers": {"aihive": {
                "command": sys.executable,
                "args": ["-m", "app.mcp_server"],
                "env": {"AIHIVE_PIPE": self.pipe_name,
                        "AIHIVE_WS": ws_id,
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
        buf = self._buffers.get(sock)
        if buf is None:
            return
        buf += bytes(sock.readAll().data())
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            self._buffers[sock] = buf
            self._handle_line(sock, bytes(line))
        # A request is tiny (a JSON line). A client that streams bytes without a
        # newline must not grow this buffer without bound inside the GUI — drop
        # it once the pending fragment is implausibly large.
        if len(buf) > _MAX_REQUEST_BYTES:
            self._buffers.pop(sock, None)
            try:
                sock.abort()
            except Exception:
                pass

    def _handle_line(self, sock, line: bytes) -> None:
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        rid = req.get("id")
        try:
            result = self._dispatch(req.get("op", ""), req.get("args") or {},
                                    ws=req.get("ws") or "")
            resp = {"id": rid, "ok": True, "result": result}
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
    # call WorkspaceManager/board methods directly. It NEVER opens a dialog or
    # spins a nested loop (reentrancy safety).

    def _target_ws(self, ws_id: str) -> str:
        return ws_id or self._active_ws() or ""

    def _dispatch(self, op: str, args: dict, ws: str = "") -> dict:
        """Execute one op. `ws` is the caller's BINDING workspace (from its
        per-workspace mcp config); the note is confined to that workspace's
        board. Empty ws (legacy config) falls back to the active workspace."""
        if op == "log_activity":
            ws_id = ws or self._target_ws("")
            wobj = self.manager.workspace(ws_id)
            if wobj is None or wobj.board is None:
                raise _RpcError("bad_args", "no board for this workspace")
            name = args.get("agent") or args.get("name") or "agent"
            ok = wobj.board.append_activity(name, args.get("message", ""))
            return {"logged": bool(ok), "workspace_id": ws_id}

        raise _RpcError("bad_args", f"unknown op {op!r}")


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
