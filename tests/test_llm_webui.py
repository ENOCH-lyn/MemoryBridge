from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from membridge.llm import LLMConfig
from membridge.remote import create_hub
from membridge.seed import seed as seed_data
from membridge.service import AgentSyncService


class _FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        content = json.dumps(
            [
                {
                    "type": "preference",
                    "content": "User prefers Chinese commit messages.",
                    "evidence_quote": "以后都用中文提交信息。",
                    "confidence": 0.91,
                }
            ],
            ensure_ascii=False,
        )
        payload = json.dumps({"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class LLMTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.db_path = Path(self.tmp.name) / "llm.db"
        self.config = LLMConfig(base_url=f"http://127.0.0.1:{self.port}", api_key="test", model="fake-model")
        self.svc = AgentSyncService(self.db_path, llm_config=self.config)
        self.ctx = self.svc.bootstrap(project_path="llm-demo")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_llm_extractor_is_used(self) -> None:
        result = self.svc.memory_ingest(self.ctx, "以后都用中文提交信息。")
        self.assertEqual(result["extractor"], "llm")
        self.assertIsNone(result["llm_error"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["type"], "preference")
        self.assertIn("Chinese", result["candidates"][0]["content"])

    def test_llm_failure_falls_back_to_rules(self) -> None:
        bad = LLMConfig(base_url="http://127.0.0.1:1", api_key="x", model="x", timeout=0.2)
        svc = AgentSyncService(Path(self.tmp.name) / "fallback.db", llm_config=bad)
        try:
            ctx = svc.bootstrap(project_path="fallback")["context_version"]
            result = svc.memory_ingest(ctx, "以后都用中文提交信息。")
            self.assertEqual(result["extractor"], "rules")
            self.assertIsNotNone(result["llm_error"])
            self.assertEqual(result["counts"]["created"], 1)
        finally:
            svc.close()

    def test_status_reports_llm(self) -> None:
        status = self.svc.status()
        self.assertTrue(status["llm"]["enabled"])
        self.assertEqual(status["llm"]["model"], "fake-model")


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


if __name__ == "__main__":
    unittest.main()
