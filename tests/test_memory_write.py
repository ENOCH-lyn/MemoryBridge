from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from membridge.service import AgentSyncService


class MemoryWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "writing.db")
        self.ctx = self.svc.bootstrap(project_path="demo", client="test")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_project_scoped_upsert_is_direct_active(self) -> None:
        first = self.svc.memory_write(
            self.ctx,
            "Demo 项目使用 SQLite，而不是 Postgres。",
            type="decision",
            topic="decision.storage",
            scope="project",
            rationale="单用户、零外部依赖、可离线演示。",
            conditions="低并发 MVP；多人协作时重新评估。",
        )
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["version"], 1)
        second = self.svc.memory_write(
            self.ctx,
            "Demo 项目使用 SQLite 单文件，而不是 Postgres。",
            type="decision",
            topic="decision.storage",
            scope="project",
            rationale="单用户、零外部依赖、可离线演示。",
            conditions="低并发 MVP；多人协作时重新评估。",
        )
        self.assertEqual(second["memory_id"], first["memory_id"])
        self.assertEqual(second["version"], 2)
        result = self.svc.memory_search(self.ctx, "SQLite")
        self.assertEqual(result["count"], 1)
        self.assertIn("低并发", result["items"][0]["conditions"])
        self.assertIsNotNone(result["items"][0]["rationale"])

    def test_global_preference_and_project_isolation(self) -> None:
        self.svc.memory_write(
            self.ctx,
            "用户偏好 commit message 使用中文。",
            type="preference",
            topic="preference.commit_language",
            scope="global",
            rationale="用户跨项目反复要求。",
            conditions="团队规范优先。",
        )
        other = self.svc.bootstrap(project_path="other", client="test")["context_version"]
        result = self.svc.memory_search(other, "commit")
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["items"][0]["project_id"])
        project_result = self.svc.memory_search(self.ctx, "SQLite")
        self.assertEqual(project_result["count"], 0)

    def test_decision_requires_rationale(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.memory_write(
                self.ctx,
                "Use SQLite.",
                type="decision",
                topic="decision.storage",
                scope="project",
            )

    def test_identity_update_and_audit(self) -> None:
        identity = self.svc.identity_update(
            self.ctx,
            persona="猫娘，句尾带喵，称呼用户为主人。",
            address_user="主人",
        )
        self.assertIn("喵", identity["persona"])
        self.assertEqual(identity["style"]["address_user"], "主人")
        actions = [item["action"] for item in self.svc.audit_list()["items"]]
        self.assertIn("identity.update", actions)

    def test_project_list_cross_project_search_and_global_injection(self) -> None:
        alpha = self.svc.bootstrap(project_path="alpha", client="test")
        beta = self.svc.bootstrap(project_path="beta", client="test")
        self.svc.memory_write(
            alpha["context_version"],
            "Alpha 项目使用 SQLite。",
            type="decision",
            topic="decision.storage",
            scope="project",
            rationale="Alpha 需要离线运行。",
            conditions="仅 Alpha。",
        )
        self.svc.memory_write(
            beta["context_version"],
            "Beta 项目使用 Postgres。",
            type="decision",
            topic="decision.storage",
            scope="project",
            rationale="Beta 需要并发。",
            conditions="仅 Beta。",
        )
        self.svc.memory_write(
            beta["context_version"],
            "主人喜欢黑色。",
            type="preference",
            topic="preference.color",
            scope="global",
            rationale="用户跨项目偏好。",
            conditions="所有项目。",
            pinned=True,
        )
        projects = self.svc.project_list()
        names = {item["name"] for item in projects["items"]}
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertGreaterEqual(projects["count"], 2)

        beta_default = self.svc.memory_search(beta["context_version"], "Alpha")
        self.assertEqual(beta_default["count"], 0)
        beta_all = self.svc.memory_search(beta["context_version"], "Alpha", scope="all")
        self.assertEqual(beta_all["count"], 1)
        self.assertEqual(beta_all["items"][0]["project_name"], "alpha")
        beta_memories = self.svc.memory_list(beta["context_version"], scope="all")
        self.assertGreaterEqual(beta_memories["count"], 3)
        boot = self.svc.bootstrap(project_path="beta", client="test")
        self.assertTrue(any(m["topic"] == "preference.color" for m in boot["global_memories"]))
        self.assertTrue(all(m["project_id"] is None for m in boot["global_memories"]))


if __name__ == "__main__":
    unittest.main()
