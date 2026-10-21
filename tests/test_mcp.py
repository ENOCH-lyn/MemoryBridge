from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from membridge.seed import seed as seed_data
from membridge.service import AgentSyncService


ROOT = Path(__file__).resolve().parents[1]


def _run_mcp(db_path: Path, lines: list[dict]) -> list[dict]:
    payload = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "membridge", "--db", str(db_path), "serve", "--project", "demo"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, stderr = proc.communicate(payload, timeout=20)
    if proc.returncode != 0:
        raise AssertionError(f"MCP server failed ({proc.returncode}): {stderr}")
    responses = []
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "mcp.db"
        svc = AgentSyncService(self.db_path)
        seed_data(svc)
        svc.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_initialize_tools_and_bootstrap_gate(self) -> None:
        responses = _run_mcp(
            self.db_path,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "0"}},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "agent_bootstrap", "arguments": {"project_path": "demo", "client": "test"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "memory_search", "arguments": {"query": "分页"}},
                },
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "agent_whoami", "arguments": {}}},
            ],
        )
        by_id = {item["id"]: item["result"] for item in responses}
        self.assertIn("agent_bootstrap", by_id[1]["instructions"])
        tool_names = {tool["name"] for tool in by_id[2]["tools"]}
        self.assertIn("agent_bootstrap", tool_names)
        self.assertIn("memory_search", tool_names)

        bootstrap = json.loads(by_id[3]["content"][0]["text"])
        self.assertTrue(bootstrap["context_version"].startswith("ctx_"))
        self.assertEqual(bootstrap["identity"]["name"], "默认")

        gated = by_id[4]
        self.assertTrue(gated["isError"])
        self.assertIn("BOOTSTRAP_REQUIRED", gated["content"][0]["text"])
        whoami = json.loads(by_id[5]["content"][0]["text"])
        self.assertEqual(whoami["identity"]["name"], "默认")

    def test_context_version_can_be_reused_across_processes(self) -> None:
        first = _run_mcp(
            self.db_path,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "agent_bootstrap", "arguments": {"project_path": "demo", "client": "cli"}},
                },
            ],
        )
        ctx = json.loads(first[1]["result"]["content"][0]["text"])["context_version"]

        second = _run_mcp(
            self.db_path,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_search",
                        "arguments": {"context_version": ctx, "query": "分页 cursor"},
                    },
                },
            ],
        )
        result = second[1]["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertGreaterEqual(payload["count"], 1)


if __name__ == "__main__":
    unittest.main()
