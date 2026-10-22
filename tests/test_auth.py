from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from membridge.remote import create_hub


def _request(url: str, method: str = "GET", body: dict | None = None, cookie: str = "", token: str = ""):
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"mb_session={cookie}"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=5)
        return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _cookie(headers: dict) -> str:
    value = headers.get("Set-Cookie", "")
    return value.split(";", 1)[0].split("=", 1)[1] if "=" in value else ""


class AuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = create_hub(str(Path(self.tmp.name) / "auth.db"), host="127.0.0.1", port=0, token="")
        self.port = self.hub.server_address[1]
        self.thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.hub.shutdown()
        self.hub.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_default_admin_forced_change_and_token_auth(self) -> None:
        status, headers, body = _request(
            self.base + "/login", "POST", {"username": "admin", "password": "admin"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["result"]["must_change_password"])
        cookie = _cookie(headers)
        self.assertTrue(cookie)

        status, _, body = _request(self.base + "/rpc", "POST", {"method": "status", "args": [], "kwargs": {}}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "PASSWORD_CHANGE_REQUIRED")

        status, headers, body = _request(
            self.base + "/password",
            "POST",
            {"current_password": "admin", "new_password": "newpass123"},
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        new_cookie = _cookie(headers)
        self.assertTrue(new_cookie)

        status, _, body = _request(self.base + "/rpc", "POST", {"method": "status", "args": [], "kwargs": {}}, cookie=new_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _, body = _request(self.base + "/tokens", "POST", {"name": "test-mcp"}, cookie=new_cookie)
        self.assertEqual(status, 200)
        token = json.loads(body)["result"]["token"]
        self.assertTrue(token.startswith("mb_"))

        status, _, body = _request(self.base + "/rpc", "POST", {"method": "status", "args": [], "kwargs": {}}, token=token)
        self.assertEqual(status, 200)

        # Old password must not work after change.
        status, _, _ = _request(self.base + "/login", "POST", {"username": "admin", "password": "admin"})
        self.assertEqual(status, 401)

    def test_static_service_token_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            hub = create_hub(str(Path(folder) / "token.db"), host="127.0.0.1", port=0, token="service-token")
            port = hub.server_address[1]
            thread = threading.Thread(target=hub.serve_forever, daemon=True)
            thread.start()
            try:
                status, _, body = _request(
                    f"http://127.0.0.1:{port}/rpc",
                    "POST",
                    {"method": "status", "args": [], "kwargs": {}},
                    token="service-token",
                )
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["ok"])
            finally:
                hub.shutdown()
                hub.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
