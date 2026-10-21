from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
