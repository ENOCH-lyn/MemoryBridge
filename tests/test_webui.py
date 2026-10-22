from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from membridge.remote import create_hub
from membridge.seed import seed as seed_data
from membridge.service import AgentSyncService


class WebUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "web.db"
        svc = AgentSyncService(db_path)
        seed_data(svc)
        svc.close()
        self.hub = create_hub(str(db_path), host="127.0.0.1", port=0, token="")
        self.port = self.hub.server_address[1]
        self.thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.hub.shutdown()
        self.hub.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_index_and_health(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("忆桥", body)
            self.assertIn("/rpc", body)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            self.assertTrue(data["ok"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/codemirror/lib/codemirror.js", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"CodeMirror", response.read()[:400])
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/health", method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
