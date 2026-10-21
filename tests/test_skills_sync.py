from __future__ import annotations

import tempfile
import unittest
import shutil
from pathlib import Path

from membridge.sync import SyncEngine, init_project
from membridge.service import AgentSyncService


SKILL_MD = """---
name: Release checklist
description: Run before publishing a release
status: validated
steps:
  - Run tests
  - Tag the release
---

# Release checklist

Use this when publishing.
"""


class SkillFolderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "skills.db")
        self.ctx = self.svc.bootstrap(project_path="skill-demo")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_install_folder_and_read_files(self) -> None:
        result = self.svc.skill_install(
            self.ctx,
            files={
                "SKILL.md": SKILL_MD,
                "scripts/check.sh": "echo check\n",
                "references/notes.md": "# notes\n",
            },
        )
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(result["status"], "validated")
        loaded = self.svc.skill_get(self.ctx, result["skill_id"])
        self.assertEqual(loaded["name"], "Release checklist")
        self.assertEqual(len(loaded["files"]), 3)
        self.assertIn("SKILL.md", {item["path"] for item in loaded["files"]})
        content = self.svc.skill_file(self.ctx, result["skill_id"], "scripts/check.sh")
        self.assertIn("echo check", content["content"])
        listed = self.svc.skill_list(self.ctx)
        self.assertEqual(listed["items"][0]["file_count"], 3)

    def test_rejects_path_traversal_and_missing_skill_md(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.skill_install(self.ctx, files={"README.md": "no skill"})
        with self.assertRaises(ValueError):
            self.svc.skill_install(self.ctx, files={"SKILL.md": SKILL_MD, "../evil.sh": "x"})

    def test_legacy_text_skill_still_works(self) -> None:
        result = self.svc.skill_add(self.ctx, name="Deploy", trigger="before deploy", steps=["run tests", "backup"])
        loaded = self.svc.skill_get(self.ctx, result["skill_id"])
        self.assertEqual(loaded["steps"], ["run tests", "backup"])
        self.assertEqual(loaded["file_count"], 1)


class SyncProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        init_project(self.project)
        shutil.rmtree(self.project / ".membridge" / "skills" / "example")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "adr.md").write_text("# ADR\n\nUse SQLite.\n", encoding="utf-8")
        inbox = self.project / ".membridge" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "chat.md").write_text("以后都用中文提交信息。", encoding="utf-8")
        skill = self.project / ".membridge" / "skills" / "release"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        (skill / "run.sh").write_text("echo release\n", encoding="utf-8")
        self.svc = AgentSyncService(Path(self.tmp.name) / "sync.db")

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_sync_once_then_idempotent(self) -> None:
        engine = SyncEngine(self.svc, self.project)
        first = engine.sync_once()
        self.assertEqual(first["docs"], 1)
        self.assertEqual(first["skills"], 1)
        self.assertEqual(first["inbox"], 1)
        self.assertEqual(first["candidates"], 1)
        second = engine.sync_once()
        self.assertEqual(second["docs"], 0)
        self.assertEqual(second["skills"], 0)
        self.assertEqual(second["inbox"], 0)
        self.assertGreaterEqual(second["skipped"], 3)


if __name__ == "__main__":
    unittest.main()
