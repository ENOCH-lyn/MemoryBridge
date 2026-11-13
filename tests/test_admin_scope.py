"""The browser console is an admin surface; MCP clients keep the agent scope.

Regression: the console listed every project's documents (scope="all") but
opened them with the default scope="auto", so a document that belongs to some
project failed with "document is outside the current scope" even though it was
visible in the list.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from membridge.auth import hash_password
from membridge.remote import create_hub
from membridge.seed import seed as seed_data
from membridge.service import AgentSyncService


class AdminScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scope.db"
        svc = AgentSyncService(self.db_path)
        seed_data(svc)
        # A project document: unreachable with scope="auto" from a session that
        # bootstrapped without a project, which is exactly what the console does.
        project = svc.ensure_project("/srv/other-project")
        self.project_id = project["id"]
        self.doc = svc.doc_add(
            svc.bootstrap(project_path="/srv/other-project")["context_version"],
            path="docs/other.md",
            content="# other project doc",
        )
        # seed() already creates `admin` with must_change_password=1; give the
        # test a known password and skip the forced rotation.
        svc.conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = 'admin'",
            (hash_password("test-password"),),
        )
        svc.conn.commit()
        svc.close()
        self.hub = create_hub(str(self.db_path), host="127.0.0.1", port=0, token="")
        self.port = self.hub.server_address[1]
        self.thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.hub.shutdown()
        self.hub.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def _post(self, path: str, payload: dict, cookie: str | None = None) -> tuple[int, dict, str]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8")), response.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers.get("Set-Cookie", "")

    def _login(self) -> str:
        status, body, cookie = self._post("/login", {"username": "admin", "password": "test-password"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["username"], "admin")
        return cookie.split(";", 1)[0]

    def _ctx(self, cookie: str) -> str:
        status, body, _ = self._post("/rpc", {"method": "bootstrap", "args": [], "kwargs": {"client": "web"}}, cookie)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        return body["result"]["context_version"]

    # -- tests ---------------------------------------------------------
    def test_console_reads_and_deletes_cross_project_document(self) -> None:
        cookie = self._login()
        ctx = self._ctx(cookie)
        status, body, _ = self._post("/rpc", {"method": "doc_read", "args": [ctx, self.doc["document_id"]]}, cookie)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["result"]["project_id"], self.project_id)
        self.assertEqual(body["result"]["content"], "# other project doc")

        status, body, _ = self._post("/rpc", {"method": "doc_delete", "args": [ctx, self.doc["document_id"]]}, cookie)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["result"]["deleted"])

    def test_api_token_still_honours_agent_scope(self) -> None:
        cookie = self._login()
        status, body, _ = self._post("/tokens", {"name": "regression"}, cookie)
        self.assertEqual(status, 200, body)
        token = body["result"]["token"]
        ctx = self._ctx(cookie)

        request = urllib.request.Request(
            self.url + "/rpc",
            data=json.dumps({"method": "doc_read", "args": [ctx, self.doc["document_id"]]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertFalse(body["ok"])
        self.assertIn("outside the current scope", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
