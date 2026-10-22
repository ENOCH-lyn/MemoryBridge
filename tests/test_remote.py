from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from membridge.remote import RemoteService, create_hub
from membridge.seed import seed as seed_data
from membridge.service import AgentSyncService, GateError


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token"


class RemoteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hub.db"
        svc = AgentSyncService(self.db_path)
        seed_data(svc)
        svc.close()
        self.hub = create_hub(str(self.db_path), host="127.0.0.1", port=0, token=TOKEN)
        self.port = self.hub.server_address[1]
        self.thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.hub.shutdown()
        self.hub.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_remote_bootstrap_and_memory(self) -> None:
        svc = RemoteService(self.url, TOKEN)
        boot = svc.bootstrap(project_path="demo", client="remote-test")
        self.assertTrue(boot["context_version"].startswith("ctx_"))
        memory = svc.memory_add(boot["context_version"], "remote-only fact", type="fact")
        result = svc.memory_search(boot["context_version"], "remote-only")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], memory["memory_id"])

    def test_remote_gate_and_stale(self) -> None:
        svc = RemoteService(self.url, TOKEN)
        with self.assertRaises(GateError) as raised:
            svc.memory_search(None, "anything")
        self.assertEqual(raised.exception.code, "BOOTSTRAP_REQUIRED")

        boot = svc.bootstrap(project_path="demo")
        svc.set_identity(persona="remote catgirl", tone="cute", suffix="喵")
        with self.assertRaises(GateError) as stale:
            svc.memory_search(boot["context_version"], "anything")
        self.assertEqual(stale.exception.code, "CONTEXT_STALE")
        fresh = svc.bootstrap(project_path="demo")
        self.assertEqual(fresh["identity"]["style"]["sentence_end"], "喵")

    def test_remote_auth_failure(self) -> None:
        svc = RemoteService(self.url, "wrong-token")
        with self.assertRaises(RuntimeError):
            svc.bootstrap(project_path="demo")

    def test_remote_memory_write(self) -> None:
        svc = RemoteService(self.url, TOKEN)
        boot = svc.bootstrap(project_path="demo")
        written = svc.memory_write(
            boot["context_version"],
            "以后都用中文提交信息。",
            topic="preference.commit_language",
            type="preference",
            scope="global",
            rationale="用户跨项目要求。",
            conditions="团队规范优先。",
        )
        self.assertEqual(written["status"], "active")
        found = svc.memory_search(boot["context_version"], "中文")
        self.assertGreaterEqual(found["count"], 1)

    def test_mcp_proxy_uses_remote_hub(self) -> None:
        payload = "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "agent_bootstrap", "arguments": {"project_path": "demo", "client": "proxy"}},
                },
            ]
        ) + "\n"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "membridge",
                "serve",
                "--remote",
                self.url,
                "--token",
                TOKEN,
                "--project",
                "demo",
            ],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr = proc.communicate(payload, timeout=20)
        if proc.returncode != 0:
            raise AssertionError(f"MCP proxy failed: {stderr}")
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        by_id = {item["id"]: item["result"] for item in responses}
        self.assertIn("agent_bootstrap", by_id[1]["instructions"])
        boot = json.loads(by_id[2]["content"][0]["text"])
        self.assertTrue(boot["context_version"].startswith("ctx_"))
        self.assertEqual(boot["identity"]["name"], "猫娘")


if __name__ == "__main__":
    unittest.main()
