from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from membridge.service import AgentSyncService, GateError
from membridge.seed import seed as seed_data


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.svc = AgentSyncService(self.db_path)

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_bootstrap_returns_identity_and_context(self) -> None:
        result = self.svc.bootstrap(project_path="demo", client="test")
        self.assertEqual(result["identity"]["name"], "默认")
        self.assertTrue(result["context_version"].startswith("ctx_"))
        self.assertEqual(result["project"]["name"], "demo")
        self.assertIn("next_actions", result)

    def test_gate_requires_bootstrap(self) -> None:
        with self.assertRaises(GateError) as ctx:
            self.svc.memory_search(None, "anything")
        self.assertEqual(ctx.exception.code, "BOOTSTRAP_REQUIRED")

    def test_memory_add_and_search_chinese(self) -> None:
        ctx = self.svc.bootstrap(project_path="demo")["context_version"]
        self.svc.memory_add(ctx, "列表分页使用 cursor，避免深 offset 性能退化。", type="decision")
        result = self.svc.memory_search(ctx, "分页 cursor")
        self.assertEqual(result["count"], 1)
        self.assertIn("cursor", result["items"][0]["content"])
        self.assertTrue(result["items"][0]["citation"].startswith("[mem:"))

    def test_project_isolation(self) -> None:
        project_a = self.svc.ensure_project("project-a")
        project_b = self.svc.ensure_project("project-b")
        ctx_a = self.svc.bootstrap(project_path="project-a")["context_version"]
        self.svc.memory_add(ctx_a, "alpha-only memory", type="fact", project_id=project_a["id"])
        doc = self.svc.doc_add(ctx_a, path="secret.md", content="alpha-only doc", project_id=project_a["id"])
        ctx_b = self.svc.bootstrap(project_path="project-b")["context_version"]
        result = self.svc.memory_search(ctx_b, "alpha-only")
        self.assertEqual(result["count"], 0)
        with self.assertRaises(ValueError):
            self.svc.doc_read(ctx_b, doc["document_id"])
        self.assertNotEqual(project_a["id"], project_b["id"])

    def test_project_key_stable_across_path_spellings(self) -> None:
        first = self.svc.ensure_project("project-a")
        second = self.svc.ensure_project("./project-a")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["key"], "name:project-a")

    def test_project_key_uses_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            git_dir = Path(repo_dir) / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text(
                '[remote "origin"]\n\turl = git@github.com:ENOCH-lyn/MemoryBridge.git\n',
                encoding="utf-8",
            )
            project = self.svc.ensure_project(repo_dir)
            self.assertEqual(project["key"], "git:github.com/enoch-lyn/memorybridge")

    def test_context_stale_after_identity_change(self) -> None:
        ctx = self.svc.bootstrap()["context_version"]
        self.svc.set_identity(tone="catgirl", suffix="喵")
        with self.assertRaises(GateError) as raised:
            self.svc.memory_search(ctx, "anything")
        self.assertEqual(raised.exception.code, "CONTEXT_STALE")

    def test_catgirl_persona_is_returned_by_bootstrap(self) -> None:
        self.svc.set_identity(
            name="猫娘",
            persona="你是猫娘，句尾带喵，称呼用户为主人。",
            tone="可爱",
            suffix="喵",
            address="主人",
        )
        result = self.svc.bootstrap()
        self.assertEqual(result["identity"]["name"], "猫娘")
        self.assertEqual(result["identity"]["style"]["sentence_end"], "喵")
        self.assertEqual(result["identity"]["style"]["address_user"], "主人")

    def test_documents_and_skills(self) -> None:
        ctx = self.svc.bootstrap(project_path="demo")["context_version"]
        self.svc.doc_add(ctx, path="docs/adr.md", title="ADR", content="使用 SQLite 作为本地存储。")
        docs = self.svc.doc_search(ctx, "SQLite")
        self.assertEqual(docs["count"], 1)
        doc = self.svc.doc_read(ctx, docs["items"][0]["document_id"])
        self.assertIn("SQLite", doc["content"])

        self.svc.skill_add(ctx, name="部署检查", trigger="发布前", steps=["跑测试", "备份数据库"])
        skills = self.svc.skill_list(ctx, query="部署")
        self.assertEqual(skills["count"], 1)
        skill = self.svc.skill_get(ctx, skills["items"][0]["id"])
        self.assertEqual(len(skill["steps"]), 2)

    def test_feedback_updates_confidence(self) -> None:
        ctx = self.svc.bootstrap()["context_version"]
        memory = self.svc.memory_add(ctx, "test fact", type="fact", confidence=0.8)
        result = self.svc.memory_feedback(ctx, memory["memory_id"], "harmful")
        self.assertLess(result["confidence"], 0.8)

    def test_export_import_round_trip(self) -> None:
        seed_data(self.svc)
        payload = self.svc.export_data()
        self.assertEqual(payload["format"], "membridge-export-v1")
        self.assertGreater(len(payload["memories"]), 0)
        # Import into a second database; ids must not collide.
        with tempfile.TemporaryDirectory() as other:
            other_svc = AgentSyncService(Path(other) / "other.db")
            try:
                result = other_svc.import_data(payload)
                self.assertGreater(result["imported"], 0)
                self.assertEqual(other_svc.get_identity()["name"], self.svc.get_identity()["name"])
            finally:
                other_svc.close()

    def test_seed_is_idempotent(self) -> None:
        first = seed_data(self.svc)
        second = seed_data(self.svc)
        self.assertTrue(first["seeded"])
        self.assertFalse(second["seeded"])


if __name__ == "__main__":
    unittest.main()
