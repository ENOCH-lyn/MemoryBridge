"""HTTP helpers for talking to the hub (and optional embedding endpoints).

Why this module exists
----------------------
Python's ``socket.create_connection`` walks ``getaddrinfo`` results in resolver
order and stops at the first address whose TCP connect succeeds.  On hosts with
a dual-stack network where the IPv6 path reaches the peer's edge but drops the
TLS records, the AAAA entry is picked first, the TCP handshake succeeds, and the
TLS handshake then hangs until the socket timeout.  The failure surfaces as
``TimeoutError: _ssl.c:1011: The handshake operation timed out`` or
``RemoteDisconnected`` and it is *intermittent*, which makes the MCP server look
"down" for a single tool call.

Measured on the deployment that motivated this module (Cloudflare-fronted hub):
default order -> 1 of 6 attempts timed out at 15 s; AF_INET -> 6 of 6 succeeded
in ~0.5 s.

Two mitigations, both applied here:

1. **Pin IPv4** for hub/embedding HTTP connections (``MEMBRIDGE_IPV4``, default
   on).  Connections that fail during the connect/handshake phase never reached
   the server, so re-pinning is enough; no fallback ladder is needed.
2. **Bounded retry** for the connect/handshake phase only.  A response timeout
   may mean the server already applied the call, so callers that mutate state
   should pass ``retries=1``.

Environment
-----------
``MEMBRIDGE_IPV4``
    ``1``/``prefer`` (default) force IPv4, ``0``/``off`` use the system order.
``MEMBRIDGE_HTTP_TIMEOUT``
    Default request timeout in seconds (default ``30``).
"""

from __future__ import annotations

import http.client
import os
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30.0

# Errors that are raised while the connection is being established, i.e. before
# any byte of the request body could have reached the server.
_CONNECT_PHASE_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    socket.timeout,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.gaierror,
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "off", "false", "no", "system"}


def force_ipv4() -> bool:
    """Whether hub connections are pinned to IPv4 (default: yes)."""
    return _env_flag("MEMBRIDGE_IPV4", True)


def default_timeout() -> float:
    raw = os.environ.get("MEMBRIDGE_HTTP_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _create_connection_ipv4(
    address: tuple[str, int],
    timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """``socket.create_connection`` restricted to ``AF_INET`` results."""
    host, port = address
    last_error: OSError | None = None
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        # No A record at all: let the system resolver decide instead of failing.
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)  # type: ignore[arg-type]
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"getaddrinfo returned no usable address for {host!r}")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        if force_ipv4():
            self._create_connection = _create_connection_ipv4  # type: ignore[assignment]


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """``HTTPSHandler`` that opens :class:`_PinnedHTTPSConnection` instead.

    Mirrors the stock ``https_open`` of the running interpreter: CPython 3.12+
    dropped ``_check_hostname`` from the handler, so only ``context`` is passed.
    """

    def https_open(self, req: urllib.request.Request):  # type: ignore[override]
        return self.do_open(_PinnedHTTPSConnection, req, context=getattr(self, "_context", None))


_OPENER = urllib.request.build_opener(_PinnedHTTPSHandler())


def _is_connect_phase(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return reason is None or isinstance(reason, _CONNECT_PHASE_ERRORS)
    return isinstance(exc, _CONNECT_PHASE_ERRORS)


def urlopen(
    request: str | urllib.request.Request,
    *,
    timeout: float | None = None,
    retries: int = 3,
    backoff: float = 0.4,
    label: str = "http",
):
    """Open ``request`` with IPv4 pinning and bounded connect-phase retries.

    ``urllib.error.HTTPError`` is never retried here: it is a real server answer
    and the caller decides what it means.  Returns the response object, which the
    caller must close.
    """
    attempts = max(1, int(retries))
    effective_timeout = default_timeout() if timeout is None else float(timeout)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _OPENER.open(request, timeout=effective_timeout)
        except urllib.error.HTTPError:
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last_exc = exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            retryable = _is_connect_phase(exc) and attempt < attempts
            print(
                f"[{label}] attempt {attempt}/{attempts} failed: "
                f"{type(exc).__name__}: {_short(exc)}"
                + (" - retrying" if retryable else ""),
                file=sys.stderr,
                flush=True,
            )
            if not retryable:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def _short(exc: BaseException, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
