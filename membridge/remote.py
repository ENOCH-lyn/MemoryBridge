"""HTTP hub and remote client for MemoryBridge.

The hub owns one SQLite database and exposes a small authenticated JSON-RPC-like
endpoint.  Devices run a local MCP proxy (`membridge serve --remote ...`) which
forwards tool calls to the hub.  This is deliberately not full MCP streamable
HTTP: the local proxy keeps client compatibility, while the hub handles shared
state.
"""

from __future__ import annotations

import hmac
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .service import AgentSyncService, GateError

DEFAULT_PORT = 8787
MAX_BODY = 2 * 1024 * 1024
WEBUI_PATH = Path(__file__).with_name("webui.html")

ALLOWED_METHODS = {
    "bootstrap",
    "get_identity",
    "get_agent_id",
    "set_identity",
    "identity_update",
    "add_directive",
    "list_directives",
    "ensure_project",
    "get_project",
    "project_list",
    "memory_add",
    "memory_write",
    "memory_search",
    "memory_list",
    "memory_feedback",
    "memory_update",
    "memory_delete",
    "doc_add",
    "doc_list",
    "doc_search",
    "doc_read",
    "skill_add",
    "skill_install",
    "skill_file",
    "skill_list",
    "skill_get",
    "project_context",
    "status",
    "audit_list",
    "export_data",
}


class HubHandler(BaseHTTPRequestHandler):
    server_version = "MemoryBridgeHub/0.1"
    protocol_version = "HTTP/1.1"
    db_path = ""
    token = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"[hub] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_body(status, body, "application/json; charset=utf-8")

    def _send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _webui(self) -> bytes:
        try:
            return WEBUI_PATH.read_bytes()
        except OSError:
            return b"MemoryBridge UI file is missing (membridge/webui.html)."

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {self.token}")

    def _service(self) -> AgentSyncService:
        return AgentSyncService(self.db_path)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/index.html", "/ui"}:
            self._send_body(200, self._webui(), "text/html; charset=utf-8")
            return
        if self.path == "/favicon.ico":
            self._send_body(204, b"", "image/x-icon")
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "membridge", "version": "0.1.0"})
            return
        if self.path == "/export":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "missing or invalid token"}})
                return
            svc = self._service()
            try:
                self._send_json(200, {"ok": True, "result": svc.export_data()})
            finally:
                svc.close()
            return
        self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "unknown path"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/rpc":
            self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "unknown path"}})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "missing or invalid token"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "invalid body length"}})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "invalid JSON"}})
            return
        method = body.get("method")
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            self._send_json(400, {"ok": False, "error": {"code": "METHOD_NOT_ALLOWED", "message": f"method not allowed: {method}"}})
            return
        args = body.get("args") or []
        kwargs = body.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "args must be list, kwargs must be object"}})
            return
        svc = self._service()
        try:
            result = getattr(svc, method)(*args, **kwargs)
            self._send_json(200, {"ok": True, "result": result})
        except GateError as exc:
            self._send_json(200, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(200, {"ok": False, "error": {"code": "BAD_REQUEST", "message": str(exc)}})
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[hub] internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._send_json(200, {"ok": False, "error": {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}})
        finally:
            svc.close()


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_hub(
    db_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str = "",
) -> HubServer:
    """Create a hub server without starting it (useful in tests)."""
    handler = type("ConfiguredHubHandler", (HubHandler,), {"db_path": db_path, "token": token})
    return HubServer((host, port), handler)


def run_hub(
    db_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str = "",
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise SystemExit("refusing to bind a non-loopback hub without --token (or MEMBRIDGE_TOKEN)")
    server = create_hub(db_path, host=host, port=port, token=token)
    actual_host, actual_port = server.server_address[:2]
    auth = "token required" if token else "no token (loopback only)"
    print(f"MemoryBridge hub: http://{actual_host}:{actual_port}  ({auth})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class RemoteService:
    """Proxy with the same method surface as AgentSyncService."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def close(self) -> None:
        return None

    def _call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        payload = json.dumps({"method": name, "args": list(args), "kwargs": kwargs}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + "/rpc", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"hub HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"hub unreachable: {exc.reason}") from exc
        if not data.get("ok"):
            error = data.get("error") or {}
            code = error.get("code", "INTERNAL")
            message = error.get("message", "hub error")
            if code in {"BOOTSTRAP_REQUIRED", "CONTEXT_STALE"}:
                raise GateError(code, message)
            raise RuntimeError(f"{code}: {message}")
        return data.get("result")

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, args, kwargs)

        return method
