from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from membridge.mcp_server import TOOL_BY_NAME, call_tool
from membridge.service import AgentSyncService, GateError


TEXT = (
    "以后都用中文提交信息。\n"
    "我们决定用 SQLite 而不是 Postgres。\n"
    "Windows 路径含空格时 pytest 会失败，应该用 pathlib。\n"
    "发布前运行 python -m unittest。\n"
    "项目使用 Python 标准库。\n"
)


class CaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "capture.db")
        self.ctx = self.svc.bootstrap(project_path="capture-demo")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_ingest_creates_candidates_with_evidence(self) -> None:
        result = self.svc.memory_ingest(self.ctx, TEXT, source="chat")
        self.assertTrue(result["episode_id"].startswith("ep_"))
        self.assertEqual(result["counts"]["created"], 5)
        types = {item["type"] for item in result["candidates"]}
        self.assertEqual(types, {"preference", "decision", "lesson", "procedure", "fact"})
        for item in result["candidates"]:
            self.assertEqual(item["status"], "candidate")
            self.assertTrue(item["evidence_quote"])
        self.assertEqual(self.svc.memory_candidates(self.ctx)["count"], 5)

    def test_candidates_are_not_searchable_until_approved(self) -> None:
        result = self.svc.memory_ingest(self.ctx, TEXT)
        decision = next(item for item in result["candidates"] if item["type"] == "decision")
        self.assertEqual(self.svc.memory_search(self.ctx, "SQLite")["count"], 0)
        approved = self.svc.memory_decide(self.ctx, decision["memory_id"], "approve")
        self.assertEqual(approved["status"], "active")
        found = self.svc.memory_search(self.ctx, "SQLite")
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["items"][0]["episode_id"], result["episode_id"])
        self.assertTrue(found["items"][0]["evidence_quote"])

    def test_reject_and_duplicate_ingest(self) -> None:
        first = self.svc.memory_ingest(self.ctx, TEXT)
        preference = next(item for item in first["candidates"] if item["type"] == "preference")
        self.svc.memory_decide(self.ctx, preference["memory_id"], "reject")
        remaining = self.svc.memory_candidates(self.ctx)["items"]
        self.assertNotIn(preference["memory_id"], {item["id"] for item in remaining})

        second = self.svc.memory_ingest(self.ctx, TEXT)
        self.assertEqual(second["counts"]["created"], 0)
        self.assertGreaterEqual(second["counts"]["duplicates"], 4)

    def test_auto_activate_high_confidence(self) -> None:
        text = "我们决定采用 SQLite 而不是 Postgres，因为需要事务。"
        result = self.svc.memory_ingest(self.ctx, text, auto_activate=True)
        self.assertEqual(result["counts"]["created"], 1)
        self.assertEqual(result["candidates"][0]["status"], "active")
        self.assertEqual(self.svc.memory_search(self.ctx, "SQLite")["count"], 1)

    def test_update_and_delete(self) -> None:
        memory = self.svc.memory_add(self.ctx, "旧的分页方案使用 offset。", type="decision")
        self.svc.memory_update(self.ctx, memory["memory_id"], content="新的分页方案使用 cursor。")
        self.assertEqual(self.svc.memory_search(self.ctx, "offset")["count"], 0)
        self.assertEqual(self.svc.memory_search(self.ctx, "cursor")["count"], 1)
        self.svc.memory_delete(self.ctx, memory["memory_id"])
        self.assertEqual(self.svc.memory_search(self.ctx, "cursor")["count"], 0)
        self.assertEqual(self.svc.memory_list(self.ctx, status="deleted")["count"], 1)

    def test_candidate_is_project_scoped(self) -> None:
        other = self.svc.ensure_project("other-project")
        self.svc.memory_ingest(self.ctx, "以后都用英文提交信息。")
        other_ctx = self.svc.bootstrap(project_path="other-project")["context_version"]
        self.assertEqual(self.svc.memory_candidates(other_ctx)["count"], 0)
        self.assertEqual(self.svc.memory_candidates(self.ctx)["count"], 1)
        self.assertNotEqual(other["id"], self.svc.ensure_project("capture-demo")["id"])

    def test_mcp_ingest_and_decide(self) -> None:
        self.assertIn("memory_ingest", TOOL_BY_NAME)
        ingest = call_tool(
            self.svc,
            "memory_ingest",
            {"context_version": self.ctx, "text": "以后都用中文提交信息。"},
        )
        self.assertFalse(ingest["isError"])
        payload = json.loads(ingest["content"][0]["text"])
        memory_id = payload["candidates"][0]["memory_id"]

        gated = call_tool(self.svc, "memory_search", {"query": "中文"})
        self.assertTrue(gated["isError"])
        self.assertIn("BOOTSTRAP_REQUIRED", gated["content"][0]["text"])

        approved = call_tool(
            self.svc,
            "memory_decide",
            {"context_version": self.ctx, "memory_id": memory_id, "decision": "approve"},
        )
        self.assertFalse(approved["isError"])
        found = call_tool(
            self.svc,
            "memory_search",
            {"context_version": self.ctx, "query": "中文"},
        )
        data = json.loads(found["content"][0]["text"])
        self.assertEqual(data["count"], 1)


if __name__ == "__main__":
    unittest.main()
