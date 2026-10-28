"""Tests for hybrid retrieval, forgetting and write-time dedup."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from membridge import retrieval as R
from membridge.service import AgentSyncService


def _ts(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class TokenizerTest(unittest.TestCase):
    def test_bigrams_and_stopwords(self) -> None:
        terms = R.index_terms("为什么数据库锁住了")
        self.assertNotIn("为什", terms)
        self.assertNotIn("什么", terms)
        self.assertIn("数据", terms)
        self.assertIn("据库", terms)

    def test_aliases_only_on_expanded_query(self) -> None:
        self.assertNotIn("database", R.fts_match_query("数据库锁住"))
        self.assertIn("database", R.fts_match_query("数据库锁住", expand=True))

    def test_hash_embedder_is_deterministic_and_similar(self) -> None:
        embedder = R.HashEmbedder(dim=256)
        a = embedder.embed("SQLite 数据库 WAL 写锁")
        b = embedder.embed("SQLite 数据库 WAL 写锁")
        c = embedder.embed("完全无关的赛题报名截止时间")
        self.assertEqual(a, b)
        self.assertGreater(R.cosine(a, b), 0.99)
        self.assertLess(R.cosine(a, c), R.cosine(a, b))

    def test_vector_ranking_has_relative_cutoff(self) -> None:
        query = [1.0, 0.0, 0.0]
        vectors = {"a": [1.0, 0.0, 0.0], "b": [0.9, 0.1, 0.0], "c": [0.05, 1.0, 0.0]}
        ranked = R.vector_ranking(vectors, query)
        self.assertEqual([item[0] for item in ranked][:2], ["a", "b"])
        self.assertNotIn("c", [item[0] for item in ranked])


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "life.db")
        self.ctx = self.svc.bootstrap(project_path="demo")["context_version"]

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def _insert_raw(self, *, topic: str, content: str, item_type: str, days_old: float, importance: int = 2, pinned: int = 0) -> str:
        memory_id = f"mem_test_{abs(hash(topic)) % 100000}"
        stamp = _ts(days_old)
        self.svc.conn.execute(
            "INSERT INTO memories(id, project_id, type, content, confidence, source, status, topic, "
            "rationale, conditions, content_hash, pinned, importance, version, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 0.7, 'test', 'active', ?, '', '', ?, ?, ?, 1, ?, ?)",
            (memory_id, item_type, content, topic, topic, pinned, importance, stamp, stamp),
        )
        self.svc.conn.commit()
        return memory_id

    def test_maintain_archives_stale_fact_but_not_decision(self) -> None:
        stale = self._insert_raw(topic="fact.stale", content="很久以前的一次性事实。", item_type="fact", days_old=400)
        kept = self._insert_raw(topic="decision.kept", content="很久以前但重要的决定。", item_type="decision", days_old=400)
        self.svc.memory_maintain(self.ctx)
        statuses = {
            row["id"]: row["status"]
            for row in self.svc.conn.execute("SELECT id, status FROM memories").fetchall()
        }
        self.assertEqual(statuses[stale], "archived")
        self.assertEqual(statuses[kept], "active")

    def test_pinned_and_fresh_are_protected(self) -> None:
        pinned = self._insert_raw(topic="fact.pinned", content="固定的事实。", item_type="fact", days_old=400, pinned=1)
        fresh = self._insert_raw(topic="fact.fresh", content="刚写下的事实。", item_type="fact", days_old=1)
        self.svc.memory_maintain(self.ctx)
        statuses = {
            row["id"]: row["status"]
            for row in self.svc.conn.execute("SELECT id, status FROM memories").fetchall()
        }
        self.assertEqual(statuses[pinned], "active")
        self.assertEqual(statuses[fresh], "active")

    def test_dry_run_reports_without_changing(self) -> None:
        stale = self._insert_raw(topic="fact.stale2", content="陈旧事实。", item_type="fact", days_old=400)
        report = self.svc.memory_maintain(self.ctx, dry_run=True)
        self.assertTrue(any(item["memory_id"] == stale for item in report["archived"]))
        row = self.svc.conn.execute("SELECT status FROM memories WHERE id = ?", (stale,)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_archive_and_restore_round_trip(self) -> None:
        ctx = self.ctx
        written = self.svc.memory_write(ctx, "一个待归档的事实。", topic="fact.roundtrip", type="fact")
        memory_id = written["memory_id"]
        self.svc.memory_archive(ctx, memory_id, reason="outdated")
        self.assertEqual(
            self.svc.memory_search(ctx, "待归档的事实", k=3)["count"],
            0,
        )
        self.svc.memory_restore(ctx, memory_id)
        found = self.svc.memory_search(ctx, "待归档的事实", k=3)
        self.assertEqual(found["count"], 1)


class HybridSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AgentSyncService(Path(self.tmp.name) / "hybrid.db")
        self.ctx = self.svc.bootstrap(project_path="demo")["context_version"]
        self.svc.memory_write(
            self.ctx,
            "`agent-deep 失败：database is locked` 的根因是 LLM 初筛被包在写事务里，WAL 单写者 + busy_timeout 一到就锁死。",
            topic="lesson.db.lock",
            type="lesson",
            rationale="线上日志复现",
        )
        self.svc.memory_write(
            self.ctx,
            "看板手机端状态胶囊单行横向滚动，工具栏不吸顶。",
            topic="decision.ui.mobile",
            type="decision",
            rationale="412px 实测重叠",
        )
        self.svc.memory_write(
            self.ctx,
            "公网入口是 https://edsh.kipfel.top，Cloudflare 命名隧道转发到 3444。",
            topic="fact.ingress",
            type="fact",
        )

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def test_cross_language_alias_rescue(self) -> None:
        result = self.svc.memory_search(self.ctx, "数据库锁住是什么原因", k=3)
        self.assertGreaterEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["topic"], "lesson.db.lock")

    def test_scores_breakdown_and_usage_touch(self) -> None:
        result = self.svc.memory_search(self.ctx, "手机端布局", k=3)
        top = result["items"][0]
        self.assertIn("lex_rank", top["scores"])
        self.assertIn("salience", top["scores"])
        self.assertIn("retrieval", result)
        row = self.svc.conn.execute("SELECT hits, last_hit_at FROM memories WHERE id = ?", (top["id"],)).fetchone()
        self.assertGreaterEqual(row["hits"], 1)
        self.assertIsNotNone(row["last_hit_at"])

    def test_near_duplicate_write_is_rejected(self) -> None:
        result = self.svc.memory_write(
            self.ctx,
            "公网入口是 https://edsh.kipfel.top，Cloudflare 命名隧道转发到 3444。",
            topic="fact.ingress.copy",
            type="fact",
        )
        self.assertEqual(result.get("action"), "duplicate")
        forced = self.svc.memory_write(
            self.ctx,
            "公网入口是 https://edsh.kipfel.top，Cloudflare 命名隧道转发到 3444。",
            topic="fact.ingress.forced",
            type="fact",
            force=True,
        )
        self.assertEqual(forced.get("action"), "created")

    def test_bootstrap_respects_budget(self) -> None:
        for index in range(12):
            self.svc.memory_write(
                self.ctx,
                f"填充记忆 {index}：" + "很长的内容" * 80,
                topic=f"fact.bulk.{index}",
                type="fact",
                force=True,
            )
        old_budget = os.environ.get("MEMBRIDGE_BOOTSTRAP_CHAR_BUDGET")
        os.environ["MEMBRIDGE_BOOTSTRAP_CHAR_BUDGET"] = "1200"
        os.environ["MEMBRIDGE_BOOTSTRAP_MEMORY_LIMIT"] = "12"
        try:
            boot = self.svc.bootstrap(client="test")
        finally:
            if old_budget is None:
                os.environ.pop("MEMBRIDGE_BOOTSTRAP_CHAR_BUDGET", None)
            else:
                os.environ["MEMBRIDGE_BOOTSTRAP_CHAR_BUDGET"] = old_budget
            os.environ.pop("MEMBRIDGE_BOOTSTRAP_MEMORY_LIMIT", None)
        total = sum(len(item["content"]) for item in boot["project_memories"])
        self.assertLessEqual(total, 1400)
        self.assertLessEqual(len(boot["project_memories"]), 12)


if __name__ == "__main__":
    unittest.main()
