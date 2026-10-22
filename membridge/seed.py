"""Seed data for a first-run MemoryBridge demo."""

from __future__ import annotations

from .db import json_dumps, utcnow
from .service import AgentSyncService


def seed(service: AgentSyncService) -> dict:
    if service._meta("seed", "0") == "1":
        return {"seeded": False, "reason": "already seeded"}

    service.set_identity(
        name="猫娘",
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
    service.memory_write(
        ctx,
        "Demo 项目使用 SQLite 单文件存储，而不是 Postgres。",
        type="decision",
        topic="decision.storage",
        scope="project",
        project_id=project["id"],
        rationale="课程 MVP 是单用户、要求零外部依赖、可离线拷贝和演示。",
        conditions="适用于当前 demo/单用户、低并发场景；多用户或高并发需要重新评估 Postgres。",
        confidence=0.92,
        source="seed",
    )
    service.memory_write(
        ctx,
        "Demo 项目在 Windows 上跑 pytest 时，路径含空格应使用 pathlib 并给 subprocess 参数加引号。",
        type="lesson",
        topic="lesson.pytest_windows",
        scope="project",
        project_id=project["id"],
        rationale="Windows 会按空格拆分未引用的命令行参数，字符串拼接容易把路径截断。",
        conditions="仅适用于 Windows + 路径含空格的测试环境；Linux CI 不需要该处理。",
        confidence=0.86,
        source="seed",
    )
    service.memory_write(
        ctx,
        "用户偏好提交信息使用中文，格式为 <type>(<scope>): <摘要>。",
        type="preference",
        topic="preference.commit_language",
        scope="global",
        rationale="用户在多个项目中反复提出同一要求，属于跨项目偏好。",
        conditions="默认适用于所有项目；若某项目有团队规范，以项目指令为准。",
        confidence=0.95,
        source="seed",
        pinned=True,
    )
    service.memory_write(
        ctx,
        "Demo 项目使用 Python 标准库 + SQLite，零外部运行时依赖。",
        type="fact",
        topic="fact.stack",
        scope="project",
        project_id=project["id"],
        rationale="课程环境需要离线可跑、可直接拷贝数据库。",
        conditions="当前 MVP 阶段；接入向量检索或多人协作后可能增加依赖。",
        confidence=0.92,
        source="seed",
    )
    service.memory_write(
        ctx,
        "Demo 项目发布前运行 python -m unittest discover -s tests -v。",
        type="procedure",
        topic="procedure.release_tests",
        scope="project",
        project_id=project["id"],
        rationale="发布检查必须真实执行测试，不能只做静态检查。",
        conditions="在仓库根目录、使用项目 Python 环境执行。",
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
