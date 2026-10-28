"""Offline retrieval A/B harness for MemoryBridge.

Pulls a real export from a running hub (read-only), imports it into a throwaway
SQLite database, then compares two rankers on the same labelled query set:

* ``baseline`` - the old ``_score`` (coverage of query terms, no index)
* ``hybrid``   - BM25 + vector + RRF + salience (``memory_search`` current code)

Usage (token is read from the DSH profile so it never appears on the command line):

    python tools/eval_retrieval.py --profile ~/.dsh/profiles/desktop/cordis.patch.yml

Exit code is 0 even when the hybrid ranker loses, the comparison is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from membridge.db import init_db  # noqa: E402
from membridge.remote import RemoteService  # noqa: E402
from membridge.service import AgentSyncService, _score  # noqa: E402

#: (query, list of topic substrings that count as a hit)
QUERIES: list[tuple[str, list[str]]] = [
    ("怎么避免每次换会话就断片", ["preference.memorybridge.workflow", "reference.dsh.workbench-memory-mechanism"]),
    ("代理老是误报怎么处理", ["lesson.ops.proxy-probe-false-positive"]),
    ("QQ 机器人为什么用社区版", ["decision.qqbot.zaofan-fork"]),
    ("公网手机入口是哪个域名", ["fact.dsh.server-10.50.1.182"]),
    ("隧道 token 失效怎么判断", ["lesson.cloudflared.invalid-token-restart-loop"]),
    ("手机端看板布局约定", ["decision.bounty-ui.mobile-layout"]),
    ("详情页重跑分析为什么没用", ["fact.bounty-ui.reanalyze-noop-for-agent-items"]),
    ("数据库锁住是什么原因", ["lesson.bounty-db.no-llm-inside-transaction"]),
    ("网关 401 是不是 key 失效", ["lesson.llm-gateway.axonhub"]),
    ("记忆检索有没有向量", ["fact.membridge.retrieval-lexical-only"]),
    ("改 DSH 配置需要重启吗", ["dsh.profile.patch-hot-reload", "dsh.plugin.patch-shape.insert"]),
    ("中文赛事标题去重误伤", ["lesson.dedup.chinese-title-similarity"]),
    ("服务器上哪个账号能 sudo", ["fact.server.cloudever-accounts"]),
    ("审计日志记了什么", ["fact.bounty-platform.audit-log"]),
    ("队列熔断后怎么恢复", ["fact.bounty-platform.deploy"]),
    ("记忆库怎么防止重复写入", ["fact.membridge.retrieval-lexical-only", "reference.dsh.workbench-memory-mechanism"]),
    ("别人家的 AI 工作台怎么管记忆", ["reference.dsh.workbench-memory-mechanism"]),
    ("pnpm 装插件失败", ["lesson.dsh.optional-native-builds"]),
]


def read_token(profile: Path) -> str:
    text = profile.read_text(encoding="utf-8")
    match = re.search(r"--token\s*\n\s*-\s*(\S+)", text) or re.search(r"--token\s+(\S+)", text)
    if not match:
        raise SystemExit("cannot find --token in profile patch")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=str(Path.home() / ".dsh/profiles/desktop/cordis.patch.yml"))
    parser.add_argument("--url", default="https://mcp.enoch.host")
    parser.add_argument("--export", default="", help="reuse a saved export instead of fetching")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    if args.export and Path(args.export).exists():
        payload = json.loads(Path(args.export).read_text(encoding="utf-8"))
    else:
        service = RemoteService(args.url, read_token(Path(args.profile)))
        service.bootstrap(client="retrieval-eval")
        payload = service.export_data()

    workdir = Path(tempfile.mkdtemp(prefix="mb-eval-"))
    db_path = workdir / "eval.db"
    local = AgentSyncService(db_path)
    init_db(local.conn)
    local.import_data(payload)
    local.conn.commit()
    ctx = local.bootstrap(client="retrieval-eval")["context_version"]

    rows = local.conn.execute("SELECT * FROM memories WHERE status = 'active'").fetchall()
    topics = {row["topic"]: row["id"] for row in rows}

    def baseline(query: str, k: int = 5) -> list[str]:
        scored = []
        for row in rows:
            score = _score(
                query,
                row["content"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                item_type=row["type"],
            )
            if score > 0:
                scored.append((score, row["id"]))
        scored.sort(reverse=True)
        return [memory_id for _, memory_id in scored[:k]]

    def hybrid(query: str, k: int = 5) -> list[str]:
        result = local.memory_search(ctx, query, scope="all", k=k, touch=False)
        return [item["id"] for item in result["items"]]

    def metrics(ranker, k: int = 5) -> dict[str, float]:
        hit1 = hit3 = 0
        mrr = 0.0
        misses = []
        for query, topic_filters in QUERIES:
            expected = {topics[t] for t in topic_filters if t in topics}
            ranked = ranker(query, k)
            ranks = [idx + 1 for idx, memory_id in enumerate(ranked) if memory_id in expected]
            if ranks:
                hit1 += 1 if ranks[0] == 1 else 0
                hit3 += 1 if ranks[0] <= 3 else 0
                mrr += 1.0 / ranks[0]
            else:
                misses.append(query)
        total = len(QUERIES)
        return {
            "hit@1": hit1 / total,
            "hit@3": hit3 / total,
            "mrr@5": mrr / total,
            "misses": misses,
        }

    base = metrics(baseline)
    new = metrics(hybrid)

    print(f"memories={len(rows)} queries={len(QUERIES)} embedder={local.embedder.model}")
    print(f"baseline  hit@1={base['hit@1']:.2f} hit@3={base['hit@3']:.2f} mrr={base['mrr@5']:.3f} misses={len(base['misses'])}")
    print(f"hybrid    hit@1={new['hit@1']:.2f} hit@3={new['hit@3']:.2f} mrr={new['mrr@5']:.3f} misses={len(new['misses'])}")
    for label, result in (("baseline", base), ("hybrid", new)):
        if result["misses"]:
            print(f"  {label} misses: " + " | ".join(result["misses"]))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"baseline": base, "hybrid": new, "memories": len(rows)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
