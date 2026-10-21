"""Seed data for a first-run MemoryBridge demo."""

from __future__ import annotations

from .db import json_dumps, utcnow
from .service import AgentSyncService


def seed(service: AgentSyncService) -> dict:
    if service._meta("seed", "0") == "1":
        return {"seeded": False, "reason": "already seeded"}

    service.set_identity(
        name="默认",
        persona="可靠、直接、先给结论；不确定时明确说不确定。",
        tone="direct",
        language="zh",
        boundaries=["不编造未经验证的事实", "不泄露密钥与隐私"],
    )
    service._set_meta("agent_id", "agent_demo")

    service.add_directive("先给结论，再给细节。", scope="global", priority=90)
    service.add_directive("提交信息使用中文，格式 <type>(<scope>): <摘要>。", scope="global", priority=80)

    project = service.ensure_project(
        "demo",
        name="Demo 项目",
        conventions=["Python 3.13 + 标准库", "SQLite 存储", "测试必须真实执行"],
    )
    service.add_directive(
        "测试必须真实执行，不能只做静态检查。",
        scope="project",
        project_path="demo",
        priority=70,
    )

    ctx = service.bootstrap(project_path="demo", client="seed")["context_version"]
    service.memory_add(
        ctx,
        "列表分页使用 cursor，避免深 offset 性能退化。",
        type="decision",
        project_id=project["id"],
        confidence=0.9,
        source="seed",
    )
    service.memory_add(
        ctx,
        "Windows 路径含空格时，pytest fixture 应使用 pathlib 并用引号包裹路径。",
        type="lesson",
        project_id=project["id"],
        confidence=0.86,
        source="seed",
    )
    service.memory_add(
        ctx,
        "提交信息使用中文，格式为 <type>(<scope>): <摘要>。",
        type="preference",
        confidence=0.95,
        source="seed",
    )
    service.memory_add(
        ctx,
        "Demo 项目使用 SQLite + Python 标准库，零外部依赖。",
        type="fact",
        project_id=project["id"],
        confidence=0.92,
        source="seed",
    )
    service.memory_add(
        ctx,
        "发布前运行 python -m unittest discover -s tests -v。",
        type="procedure",
        project_id=project["id"],
        confidence=0.9,
        source="seed",
    )

    service.doc_add(
        ctx,
        path="docs/adr-001-local-first.md",
        title="ADR-001 本地优先",
        content=(
            "# ADR-001 本地优先\n\n"
            "## 决策\n\n"
            "MVP 使用 SQLite 单文件存储，Python 标准库实现 MCP stdio 服务；"
            "不引入外部数据库与向量服务，保证单机可跑、可拷贝、可离线演示。\n\n"
            "## 代价\n\n"
            "跨设备实时同步尚未实现；先用导出/导入和共享数据库文件过渡。\n"
        ),
        project_id=project["id"],
    )
    service.doc_add(
        ctx,
        path="docs/bootstrap-protocol.md",
        title="Bootstrap 协议",
        content=(
            "# Bootstrap 协议\n\n"
            "每个新会话必须先调用 `agent_bootstrap`，拿到 `context_version`。\n"
            "其他记忆/文档/技能工具必须携带该版本；缺失返回 `BOOTSTRAP_REQUIRED`，"
            "身份或项目上下文变化后返回 `CONTEXT_STALE`。\n"
        ),
        project_id=project["id"],
    )

    service.skill_install(
        ctx,
        files={
            "SKILL.md": (
                "---\n"
                "name: Windows pytest 路径修复\n"
                "description: pytest 在 Windows 上因路径含空格失败\n"
                "status: validated\n"
                "steps:\n"
                "  - 用 pathlib.Path 构造路径\n"
                "  - subprocess 调用时为路径加引号\n"
                "  - CI 中将 cache_dir 指向无空格目录\n"
                "---\n\n"
                "# Windows pytest 路径修复\n\n"
                "先在本地复现，再改路径构造；不要用字符串拼接处理 Windows 路径。\n"
            ),
            "scripts/check.ps1": "python -m pytest -q\n",
            "references/notes.md": "# Notes\n\nUse pathlib.Path and quote subprocess arguments.\n",
        },
        name="Windows pytest 路径修复",
        project_id=project["id"],
        status="validated",
    )
    service.skill_install(
        ctx,
        files={
            "SKILL.md": (
                "---\n"
                "name: MCP 客户端接入检查\n"
                "description: 新客户端连不上 MemoryBridge\n"
                "status: candidate\n"
                "steps:\n"
                "  - 确认 python -m membridge --db <path> serve 可手动启动\n"
                "  - 确认客户端配置里的 command/args 使用绝对路径\n"
                "  - 新会话先调用 agent_bootstrap，再调用 memory_search\n"
                "---\n\n"
                "# MCP 客户端接入检查\n\n"
                "先手工跑通 stdio，再检查客户端配置。\n"
            ),
            "scripts/smoke.ps1": "python -m membridge --help\n",
        },
        name="MCP 客户端接入检查",
        status="candidate",
    )

    service._set_meta("seed", "1")
    service.conn.commit()
    return {
        "seeded": True,
        "project_id": project["id"],
        "context_version": ctx,
        "status": service.status(),
    }
