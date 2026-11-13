"""The context contract: bootstrap and search stay small, and say when they are unsure."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from membridge.mcp_server import INSTRUCTIONS, TOOLS
from membridge.service import AgentSyncService

COMPACT_KEYS = {"id", "topic", "type", "content", "age_days"}


class CompactContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "compact.db")
        for index in range(20):
            self.svc.memory_write(
                self.svc.bootstrap(project_path="demo")["context_version"],
                f"第 {index} 条：这是用于测试注入预算的内容。" + "很长的内容" * 40,
                topic=f"fact.bulk.{index}",
                type="fact",
                force=True,
            )
        self.ctx = self.svc.bootstrap(project_path="demo")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_bootstrap_stays_small(self) -> None:
        boot = self.svc.bootstrap(project_path="demo")
        rows = boot["project_memories"]
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 12)
        for row in rows:
            self.assertEqual(set(row), COMPACT_KEYS)
        # The old shape (projects + catalogue + recent_* + policy) measured ~20k
        # characters on the production database.
        self.assertLessEqual(len(json.dumps(boot, ensure_ascii=False)), 6000)
        for retired in ("next_actions", "capture_contract", "memory_map", "projects", "recent_lessons"):
            self.assertNotIn(retired, boot)
        self.assertIn("notice", boot)
        self.assertIn("available", boot)

    def test_bootstrap_optional_sections(self) -> None:
        boot = self.svc.bootstrap(project_path="demo", include=["projects", "map", "policy"])
        self.assertEqual(set(boot["projects"][0]), {"id", "key", "name", "path", "memories"})
        self.assertIn("line", boot["memory_map"][0])
        self.assertIn("required_tools", boot["policy"])
        # Unknown section names are ignored rather than echoed back.
        ignored = self.svc.bootstrap(project_path="demo", include=["nope"])
        self.assertNotIn("nope", ignored)

    def test_project_memories_come_before_global(self) -> None:
        payload = json.dumps(self.svc.bootstrap(project_path="demo"), ensure_ascii=False)
        self.assertLess(payload.index('"project_memories"'), payload.index('"global_memories"'))

    def test_memory_get_returns_the_full_record(self) -> None:
        write = self.svc.memory_write(
            self.ctx,
            "demo 用 SQLite 单文件存储。",
            topic="decision.storage",
            type="decision",
            rationale="零外部依赖、可离线演示。",
            conditions="单用户低并发；多人协作时重评。",
        )
        got = self.svc.memory_get(self.ctx, memory_id=write["memory_id"])
        self.assertEqual(got["memory"]["topic"], "decision.storage")
        self.assertEqual(got["memory"]["rationale"], "零外部依赖、可离线演示。")
        self.assertEqual(got["scope"], "project")
        self.assertIn("age_days", got)
        self.assertIn("revisions", got)
        by_topic = self.svc.memory_get(self.ctx, topic="decision.storage")
        self.assertEqual(by_topic["memory"]["id"], write["memory_id"])

    def test_memory_get_requires_scope_for_another_project(self) -> None:
        write = self.svc.memory_write(
            self.ctx, "demo 专用事实。", topic="fact.demo.only", type="fact", force=True
        )
        other = self.svc.bootstrap(project_path="other")["context_version"]
        with self.assertRaises(ValueError):
            self.svc.memory_get(other, memory_id=write["memory_id"])
        self.assertIsNotNone(self.svc.memory_get(other, memory_id=write["memory_id"], scope="all"))

    def test_search_reports_strength(self) -> None:
        hit = self.svc.memory_search(self.ctx, "注入预算", k=3)
        self.assertIn(hit["strength"], {"ok", "weak"})
        self.assertIn("items", hit)
        for item in hit["items"]:
            self.assertTrue({"id", "topic", "type", "content", "age_days", "project", "score"} <= set(item))
        filtered = self.svc.memory_search(self.ctx, "注入预算", k=3, min_score=99)
        self.assertEqual(filtered["count"], 0)
        self.assertEqual(filtered["strength"], "none")
        self.assertIn("hint", filtered)

    def test_search_full_detail_is_console_only(self) -> None:
        self.svc.memory_write(
            self.ctx,
            "带理由的记忆条目。",
            topic="decision.detail",
            type="decision",
            rationale="因为要测 detail=full。",
            conditions="只在控制台用。",
        )
        full = self.svc.memory_search(self.ctx, "带理由的记忆条目", k=1, detail="full")
        self.assertIn("rationale", full["items"][0])
        self.assertIn("project_id", full["items"][0])
        compact = self.svc.memory_search(self.ctx, "带理由的记忆条目", k=1)
        self.assertNotIn("rationale", compact["items"][0])
        with self.assertRaises(ValueError):
            self.svc.memory_search(self.ctx, "x", detail="nope")

    def test_tools_and_instructions_expose_the_new_contract(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("memory_get", names)
        self.assertIn("memory_get", INSTRUCTIONS)
        self.assertIn("strength", INSTRUCTIONS)
        schema = next(tool for tool in TOOLS if tool["name"] == "agent_bootstrap")["inputSchema"]
        self.assertIn("include", schema["properties"])
        search = next(tool for tool in TOOLS if tool["name"] == "memory_search")["inputSchema"]
        self.assertIn("min_score", search["properties"])


if __name__ == "__main__":
    unittest.main()
