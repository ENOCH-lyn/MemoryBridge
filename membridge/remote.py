"""HTTP hub and remote client for MemoryBridge.

The hub owns one SQLite database and exposes a small authenticated JSON-RPC-like
endpoint. Devices run a local MCP proxy (`membridge serve --remote ...`) which
forwards tool calls to the hub over one HTTPS port. The hub also serves the
read-only web console and the login/session/API-token endpoints.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time

from . import __version__
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import netutil

from .auth import (
    authenticate,
    change_password,
    create_api_token,
    create_session,
    delete_session,
    get_session,
    list_api_tokens,
    revoke_api_token,
    update_username,
    verify_api_token,
)
from .service import AgentSyncService, GateError

DEFAULT_PORT = 8787
MAX_BODY = 2 * 1024 * 1024
WEBUI_PATH = Path(__file__).with_name("webui.html")
STATIC_ROOT = Path(__file__).with_name("static")
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
COOKIE_NAME = "mb_session"
SESSION_MAX_AGE = 7 * 24 * 3600
LOGIN_WINDOW = 300
LOGIN_LIMIT = 5

#: Methods where the browser console acts as the single admin and therefore must
#: not be boxed in by the agent-scope rules: the console already lists documents
#: across every project (scope="all"), so opening or deleting one must not fail
#: with "document is outside the current scope".  MCP clients (api/static tokens)
#: are untouched — for them scope is the agent's recall boundary.
ADMIN_SCOPE_METHODS = {"doc_read", "doc_delete"}

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
    "project_delete",
    "memory_add",
    "memory_write",
    "memory_search",
    "memory_get",
    "memory_list",
    "memory_feedback",
    "memory_update",
    "memory_delete",
    "memory_archive",
    "memory_restore",
    "memory_maintain",
    "memory_stats",
    "doc_add",
    "doc_list",
    "doc_delete",
    "doc_search",
    "doc_read",
    "skill_add",
    "skill_install",
    "skill_file",
    "skill_save_file",
    "skill_delete_file",
    "skill_delete",
    "skill_list",
    "skill_get",
    "project_context",
    "status",
    "audit_list",
    "export_data",
    "memory_history",
    "memory_revert",
    "memory_map",
    "memory_backup",
}


class HubHandler(BaseHTTPRequestHandler):
    server_version = "MemoryBridgeHub/0.2"
    protocol_version = "HTTP/1.1"
    db_path = ""
    token = ""
    _failure_lock = threading.Lock()
    _failures: dict[str, list[float]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"[hub] {self.address_string()} {fmt % args}", file=sys.stderr)

    # ------------------------------------------------------------------
    # response helpers
    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload: dict[str, Any], *, cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_body(status, body, "application/json; charset=utf-8", cookie=cookie)

    def _send_body(self, status: int, body: bytes, content_type: str, *, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        if self._secure_cookie():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _webui(self) -> bytes:
        try:
            return WEBUI_PATH.read_bytes()
        except OSError:
            return b"MemoryBridge UI file is missing (membridge/webui.html)."

    def _static(self, request_path: str) -> bool:
        relative = request_path[len("/static/") :]
        safe = (STATIC_ROOT / relative).resolve()
        try:
            safe.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            return False
        if not safe.is_file():
            return False
        content_type = STATIC_TYPES.get(safe.suffix.lower(), "application/octet-stream")
        self._send_body(200, safe.read_bytes(), content_type)
        return True

    # ------------------------------------------------------------------
    # auth helpers
    # ------------------------------------------------------------------
    def _secure_cookie(self) -> bool:
        if os.environ.get("MEMBRIDGE_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}:
            return True
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _session_id(self) -> str | None:
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == COOKIE_NAME:
                return value
        return None

    def _session_cookie(self, session_id: str) -> str:
        flags = f"Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Lax"
        if self._secure_cookie():
            flags += "; Secure"
        return f"{COOKIE_NAME}={session_id}; {flags}"

    def _cleared_session_cookie(self) -> str:
        flags = "Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        if self._secure_cookie():
            flags += "; Secure"
        return f"{COOKIE_NAME}=; {flags}"

    def _client_ip(self) -> str:
        if os.environ.get("MEMBRIDGE_TRUST_PROXY", "").lower() in {"1", "true", "yes"}:
            real = self.headers.get("X-Real-IP", "").strip()
            if real:
                return real
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                # The nearest proxy appends the observed client last; prefer that
                # over a client-supplied first value.
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def _rate_limited(self) -> bool:
        now = time.time()
        with self._failure_lock:
            if len(self._failures) > 4096:
                for key in list(self._failures):
                    self._failures[key] = [stamp for stamp in self._failures[key] if now - stamp < LOGIN_WINDOW]
                    if not self._failures[key]:
                        self._failures.pop(key, None)
                while len(self._failures) > 4096:
                    self._failures.pop(next(iter(self._failures)), None)
            values = [stamp for stamp in self._failures.get(self._client_ip(), []) if now - stamp < LOGIN_WINDOW]
            self._failures[self._client_ip()] = values
            return len(values) >= LOGIN_LIMIT

    def _record_failure(self) -> None:
        with self._failure_lock:
            self._failures.setdefault(self._client_ip(), []).append(time.time())

    def _clear_failures(self) -> None:
        with self._failure_lock:
            self._failures.pop(self._client_ip(), None)

    def _service(self) -> AgentSyncService:
        return AgentSyncService(self.db_path)

    def _auth_context(self, svc: AgentSyncService) -> dict[str, Any] | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header[7:].strip()
            if self.token and hmac.compare_digest(token, self.token):
                return {"kind": "static", "user": None}
            api_user = verify_api_token(svc.conn, token)
            if api_user:
                return {"kind": "api", "user": api_user}
        session = get_session(svc.conn, self._session_id())
        if session:
            return {"kind": "session", "user": session}
        return None

    def _require_session(self, svc: AgentSyncService, *, allow_must_change: bool = False) -> dict[str, Any] | None:
        ctx = self._auth_context(svc)
        if not ctx or ctx["kind"] != "session":
            self._send_json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "login required"}})
            return None
        if ctx["user"]["must_change_password"] and not allow_must_change:
            self._send_json(
                403,
                {"ok": False, "error": {"code": "PASSWORD_CHANGE_REQUIRED", "message": "change the default password first"}},
            )
            return None
        return ctx

    def _require_access(self, svc: AgentSyncService) -> dict[str, Any] | None:
        ctx = self._auth_context(svc)
        if not ctx:
            self._send_json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "authentication required"}})
            return None
        if ctx["kind"] == "session" and ctx["user"]["must_change_password"]:
            self._send_json(
                403,
                {"ok": False, "error": {"code": "PASSWORD_CHANGE_REQUIRED", "message": "change the default password first"}},
            )
            return None
        return ctx

    # ------------------------------------------------------------------
    # request helpers
    # ------------------------------------------------------------------
    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "invalid body length"}})
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "invalid JSON"}})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "JSON object required"}})
            return None
        return body

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_body(204, b"", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        # Cloudflare/health probes may use HEAD; keep it simple and avoid a 501.
        self._send_body(200, b"", "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/index.html", "/ui"}:
            self._send_body(200, self._webui(), "text/html; charset=utf-8")
            return
        if self.path.startswith("/static/") and self._static(self.path):
            return
        if self.path == "/favicon.ico":
            self._send_body(204, b"", "image/x-icon")
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "membridge", "version": __version__})
            return
        if self.path == "/me":
            svc = self._service()
            try:
                ctx = self._auth_context(svc)
                if not ctx:
                    self._send_json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "authentication required"}})
                    return
                user = ctx["user"]
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "kind": ctx["kind"],
                            "username": user["username"] if user else "service-token",
                            "must_change_password": bool(user["must_change_password"]) if user else False,
                        },
                    },
                )
            finally:
                svc.close()
            return
        if self.path == "/tokens":
            svc = self._service()
            try:
                ctx = self._require_session(svc)
                if not ctx:
                    return
                self._send_json(200, {"ok": True, "result": {"items": list_api_tokens(svc.conn, ctx["user"]["user_id"])}})
            finally:
                svc.close()
            return
        if self.path == "/export":
            svc = self._service()
            try:
                if not self._require_access(svc):
                    return
                self._send_json(200, {"ok": True, "result": svc.export_data()})
            finally:
                svc.close()
            return
        self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "unknown path"}})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/login":
            self._login()
            return
        if self.path == "/logout":
            self._logout()
            return
        if self.path == "/password":
            self._change_password()
            return
        if self.path == "/account/username":
            self._change_username()
            return
        if self.path == "/tokens":
            self._create_token()
            return
        if self.path.startswith("/tokens/"):
            self._revoke_token(self.path.rsplit("/", 1)[-1])
            return
        if self.path != "/rpc":
            self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "unknown path"}})
            return
        svc = self._service()
        try:
            auth = self._require_access(svc)
            if not auth:
                return
            body = self._read_json()
            if body is None:
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
            if auth["kind"] == "session" and method in ADMIN_SCOPE_METHODS and "scope" not in kwargs:
                kwargs = {**kwargs, "scope": "all"}
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

    def _login(self) -> None:
        if self._rate_limited():
            self._send_json(429, {"ok": False, "error": {"code": "RATE_LIMITED", "message": "too many login attempts"}})
            return
        body = self._read_json()
        if body is None:
            return
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        svc = self._service()
        try:
            user = authenticate(svc.conn, username, password)
            if user is None:
                self._record_failure()
                self._send_json(401, {"ok": False, "error": {"code": "INVALID_CREDENTIALS", "message": "invalid username or password"}})
                return
            self._clear_failures()
            session_id = create_session(svc.conn, user["id"])
            self._send_json(
                200,
                {
                    "ok": True,
                    "result": {
                        "username": user["username"],
                        "must_change_password": bool(user["must_change_password"]),
                    },
                },
                cookie=self._session_cookie(session_id),
            )
        finally:
            svc.close()

    def _logout(self) -> None:
        svc = self._service()
        try:
            delete_session(svc.conn, self._session_id())
            self._send_body(204, b"", "application/json; charset=utf-8", cookie=self._cleared_session_cookie())
        finally:
            svc.close()

    def _change_password(self) -> None:
        svc = self._service()
        try:
            ctx = self._require_session(svc, allow_must_change=True)
            if not ctx:
                return
            body = self._read_json()
            if body is None:
                return
            try:
                change_password(
                    svc.conn,
                    ctx["user"]["user_id"],
                    str(body.get("current_password", "")),
                    str(body.get("new_password", "")),
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": {"code": "INVALID_PASSWORD", "message": str(exc)}})
                return
            session_id = create_session(svc.conn, ctx["user"]["user_id"])
            self._send_json(200, {"ok": True, "result": {"changed": True}}, cookie=self._session_cookie(session_id))
        finally:
            svc.close()

    def _change_username(self) -> None:
        svc = self._service()
        try:
            ctx = self._require_session(svc)
            if not ctx:
                return
            body = self._read_json()
            if body is None:
                return
            try:
                username = update_username(
                    svc.conn,
                    ctx["user"]["user_id"],
                    str(body.get("current_password", "")),
                    str(body.get("username", "")),
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": {"code": "INVALID_USERNAME", "message": str(exc)}})
                return
            svc._audit("user.username", ctx["user"]["user_id"], {"username": username})
            svc.conn.commit()
            self._send_json(200, {"ok": True, "result": {"username": username}})
        finally:
            svc.close()

    def _create_token(self) -> None:
        svc = self._service()
        try:
            ctx = self._require_session(svc)
            if not ctx:
                return
            body = self._read_json()
            if body is None:
                return
            _, raw = create_api_token(svc.conn, ctx["user"]["user_id"], str(body.get("name", "mcp")))
            self._send_json(200, {"ok": True, "result": {"token": raw}})
        finally:
            svc.close()

    def _revoke_token(self, token_id: str) -> None:
        svc = self._service()
        try:
            ctx = self._require_session(svc)
            if not ctx:
                return
            ok = revoke_api_token(svc.conn, ctx["user"]["user_id"], token_id)
            self._send_json(200 if ok else 404, {"ok": ok, "result": {"revoked": ok}})
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
    server = create_hub(db_path, host=host, port=port, token=token)
    actual_host, actual_port = server.server_address[:2]
    auth = "account login + API tokens" if not token else "account login + API tokens + static service token"
    print(f"MemoryBridge hub: http://{actual_host}:{actual_port}  ({auth})", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[hub] WARNING: public bind; use HTTPS reverse proxy and change the default admin password immediately.", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class RemoteService:
    """Proxy with the same method surface as AgentSyncService."""

    #: Name fragments that mark a state-changing RPC.  Such calls are tried
    #: once only: a timeout after the hub applied them would otherwise duplicate
    #: writes.  Read-only RPCs are safe to retry on connection-phase failures.
    _WRITE_FRAGMENTS = (
        "write",
        "add",
        "update",
        "delete",
        "archive",
        "restore",
        "revert",
        "install",
        "save",
        "set",
        "edit",
        "backup",
        "maintain",
        "feedback",
        "create",
    )

    def __init__(self, base_url: str, token: str = "", timeout: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = netutil.default_timeout() if timeout is None else float(timeout)

    def close(self) -> None:
        return None

    def _retries_for(self, name: str) -> int:
        lowered = name.lower()
        return 1 if any(frag in lowered for frag in self._WRITE_FRAGMENTS) else 3

    def _call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        payload = json.dumps({"method": name, "args": list(args), "kwargs": kwargs}, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            # Cloudflare Browser Integrity Check rejects the default Python UA.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MemoryBridge/0.2",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + "/rpc", data=payload, headers=headers, method="POST")
        try:
            with netutil.urlopen(
                request,
                timeout=self.timeout,
                retries=self._retries_for(name),
                label=f"remote:{name}",
            ) as response:
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
