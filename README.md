# 忆桥 MemoryBridge

忆桥是一个**自部署的 Agent 身份 / 记忆 / 能力同步器**：任何支持 MCP 的客户端，只要连上同一个 MemoryBridge，agent 在每次新会话先 `agent_bootstrap`，就能拿回同一份人格、全局指令、项目上下文、记忆、文档和技能。

> **当前状态：AI 维护、UI 只读的 MVP（MCP stdio + HTTP Hub + Material 3 控制台 + 文件夹技能）**。
> 记忆/文档/技能/身份全部由 agent 通过 MCP **直接写入 active**；没有候选队列、没有人工批准、UI 不提供编辑。向量检索、op log/冲突合并、TLS/账号体系是下一阶段。

## 现在能做到什么

- **同一个 agent**：新会话调用 `agent_bootstrap`，返回人格、称呼、边界、全局指令、项目约定和 `context_version`。
- **Bootstrap Gate**：没 bootstrap 就调记忆/文档/技能工具，返回 `BOOTSTRAP_REQUIRED`；身份或项目上下文变了，旧 context 返回 `CONTEXT_STALE`。
- **项目隔离**：按 git remote 或目录名建稳定 project；默认检索“当前项目 + 全局”，不会把 A 项目私有记忆串到 B 项目。
- **AI 直接写记忆**：`memory_write` 立即 upsert 为 active，不需要候选/批准；每条必须有稳定 `topic`，决策/教训必须有 `rationale` 和 `conditions`，避免把项目结论写成无场景的全局规则。
- **AI 直接写身份**：`identity_update` 修改 persona、称呼、边界；语气、口癖、句尾都写在 persona 文本里，没有单独的 `sentence_end` 字段。
- **AI 直接写文档 / 技能**：`doc_add` 写 Markdown；`skill_install` 安装文件夹技能（`SKILL.md` + scripts/references/assets）；UI 只显示。
- **Web 控制台（只读）**：概览、记忆、文档、技能、身份、活动（audit log）、连接；没有采集/候选/编辑按钮。
- **可审计**：所有写入落 `audit_log`，UI「活动」和 `audit_list` 可回看。
- **跨设备**：一台设备/服务器跑 `hub`，其他设备用 `serve --remote` 做本地 MCP proxy；连的是同一份身份与记忆。
- **零外部依赖**：Python 3.11+ 标准库 + SQLite；单文件数据库，可拷贝、可导出。
- **MCP 协议**：`initialize` 带 instructions；工具带描述与 annotations；另有 `memory://identity` 资源和 `recall_for_task` prompt。

## 60 秒跑起来

```powershell
cd C:\path\to\MemoryBridge
python -m membridge --db .\membridge.db seed
python -m membridge --db .\membridge.db bootstrap --project-path demo
```

会看到类似：

```json
{
  "identity": {"name": "默认", "persona": "可靠、直接、先给结论……", "version": 2},
  "project": {"id": "proj_...", "name": "Demo 项目", "conventions": ["Python 3.13 + 标准库", "..."]},
  "global_directives": ["先给结论，再给细节。", "..."],
  "context_version": "ctx_..."
}
```

试试猫娘：

```powershell
python -m membridge --db .\membridge.db persona set `
  --name 猫娘 --persona "你是猫娘，句尾带喵，称呼用户为主人。" `
  --tone 可爱 --suffix 喵 --address 主人
python -m membridge --db .\membridge.db bootstrap --project-path demo
```

试试 AI 直接写记忆（active，无候选）：

```powershell
python -m membridge --db .\membridge.db write `
  "Demo 项目使用 SQLite 单文件存储，而不是 Postgres。" `
  --topic decision.storage --type decision --scope project `
  --rationale "单用户、零外部依赖、可离线演示。" `
  --conditions "仅适用于当前 demo/低并发；多人协作时重评。" `
  --project-path demo

python -m membridge --db .\membridge.db search "SQLite" --project-path demo
```

`write` 直接 upsert 为 active；同一 project + topic 的后续写入会更新同一条，而不是堆近似重复。`decision`/`lesson` 必须给 `rationale`。

## 接入 MCP 客户端

先启动服务（stdio，由客户端拉起）：

```powershell
python -m membridge --db .\membridge.db serve --project .
```

Claude Code / Cursor / DSH 的 MCP 配置（把路径换成你的实际路径）：

```json
{
  "mcpServers": {
    "membridge": {
      "command": "python",
      "args": [
        "-m", "membridge",
        "--db", "C:\\path\\to\\MemoryBridge\\membridge.db",
        "serve",
        "--project", "C:\\path\\to\\your\\project"
      ],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\MemoryBridge"
      }
    }
  }
}
```

如果已经 `pip install -e .`，可以去掉 `PYTHONPATH`，把 `command` 改成 `membridge`。

**第一次对话**：客户端应把 `initialize.instructions` 注入模型；模型会先调 `agent_bootstrap`。如果某个客户端不注入 `instructions`，手动说一句“先调用 agent_bootstrap”即可，Gate 也会在后续工具调用时强制补调。

## 跨设备：Hub + 本地 Proxy

一台设备（或 VPS/NAS）持有数据库并跑 Hub：

```powershell
$env:MEMBRIDGE_TOKEN = "change-me-to-a-long-random-token"
python -m membridge --db .\membridge.db hub --host 0.0.0.0 --port 8787
```

其他设备只跑本地 MCP proxy，指向同一个 Hub：

```powershell
$env:MEMBRIDGE_TOKEN = "change-me-to-a-long-random-token"
python -m membridge serve --remote http://HUB_IP:8787 --token $env:MEMBRIDGE_TOKEN --project .
```

MCP 配置（设备 B）：

```json
{
  "mcpServers": {
    "membridge": {
      "command": "python",
      "args": [
        "-m", "membridge",
        "serve",
        "--remote", "http://HUB_IP:8787",
        "--token", "change-me-to-a-long-random-token",
        "--project", "C:\\path\\to\\your\\project"
      ],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\MemoryBridge"
      }
    }
  }
}
```

说明：

- Hub 提供 `GET /`（Web 控制台）、`POST /rpc`、`GET /health`、`GET /export`；除首页/健康检查外都要 Bearer token。
- 跨公网建议放在 Tailscale / Cloudflare Tunnel / Caddy TLS 之后，不要直接裸奔 `0.0.0.0:8787`。
- 项目 id 不再按绝对路径哈希：有 `origin` remote 时用 remote（如 `git:github.com/enoch-lyn/memorybridge`），否则用目录名；同一仓库在不同电脑上会落到同一个 project。
- 当前是“中心数据库”，没有 op log/冲突副本；并发写同一记忆时仍是最后写入生效。下一阶段做增量同步与冲突队列。

## 本地演示（无 token）

想立刻看界面，可以直接用本地模式（只监听 127.0.0.1，不需要 token）：

```powershell
python -m membridge --db .\data\membridge-local.db seed
python -m membridge --db .\data\membridge-local.db hub --host 127.0.0.1 --port 8788
```

浏览器打开 `http://127.0.0.1:8788/`。控制台默认项目是 `demo`；在「连接」里改成你自己的项目路径即可。数据库在 `data/membridge-local.db`，删除即可重置。

内容不是在这里录入的：agent 连上 MCP 后，在你说出持久要求时直接调用 `memory_write` / `doc_add` / `skill_install` / `identity_update`；UI 只读。

## Web 控制台

Hub 启动后，浏览器打开：

```text
http://HUB_IP:8787/
```

界面按 **Material 3** 设计：导航 rail、tonal surface、filled/tonal/outlined 按钮、明暗主题切换；单文件 HTML/CSS/JS，无 Node 构建、无外部 CDN。

在「连接」页填 Hub 的 `MEMBRIDGE_TOKEN`（本地无 token 模式可留空），保存后即可查看：

![MemoryBridge Web 控制台](doc/assets/webui.png)

- **概览**：身份、活跃记忆、文档/技能数、写入模式、AI 写入契约、最近写入。
- **记忆**：只读；显示 type、project/global、topic、rationale、conditions、证据。
- **文档**：只读；AI 通过 `doc_add` 维护。
- **技能**：只读；AI 通过 `skill_install` 安装文件夹，可查看目录和文件。
- **身份**：只读；AI 通过 `identity_update` 维护，口癖写在 persona 里。
- **活动**：audit log 中的 memory/doc/skill/identity 写入记录。
- **连接**：token、项目路径、MCP 配置片段、JSON 导出、健康检查。

页面是单文件 HTML/CSS/JS，由 Hub 直接提供，不需要 Node 构建，也不依赖外部 CDN。

## Docker 部署

```bash
cp .env.example .env        # 或直接 export MEMBRIDGE_TOKEN=...
docker compose up -d --build
```

`compose.yaml` 会启动 Hub（8787），数据放在 `membridge-data` volume。首次可灌演示数据：

```bash
docker compose exec hub membridge --db /data/membridge.db seed
```

## CLI

```powershell
python -m membridge --db .\membridge.db init
python -m membridge --db .\membridge.db seed
python -m membridge --db .\membridge.db status
python -m membridge --db .\membridge.db bootstrap --project-path .
python -m membridge --db .\membridge.db persona set --name 猫娘 --tone 可爱
python -m membridge --db .\membridge.db directive add "先给结论" --scope global
python -m membridge --db .\membridge.db remember "分页用 cursor" --type decision --project-path .
python -m membridge --db .\membridge.db write "以后都用中文提交信息。" --topic preference.commit_language --type preference --scope global --rationale "用户跨项目要求" --conditions "团队规范优先"
python -m membridge --db .\membridge.db search "中文" --project-path .
python -m membridge --db .\membridge.db forget mem_xxx --project-path .
python -m membridge --db .\membridge.db search "分页" --project-path .
python -m membridge --db .\membridge.db docs add .\docs\adr.md --project-path .
python -m membridge --db .\membridge.db docs list --project-path .
python -m membridge --db .\membridge.db docs search "SQLite" --project-path .
python -m membridge --db .\membridge.db skills list --project-path .
python -m membridge --db .\membridge.db skills install .\.membridge\skills\my-skill --project-path .
python -m membridge --db .\membridge.db skills file skill_xxx scripts/check.ps1 --project-path .
python -m membridge --db .\membridge.db export --out .\backup.json
python -m membridge --db .\membridge.db import --in .\backup.json
python -m membridge --db .\membridge.db hub --host 0.0.0.0 --port 8787
python -m membridge serve --remote http://127.0.0.1:8787 --token $env:MEMBRIDGE_TOKEN --project .
```

> `--json` 可放在命令任意位置；数据默认 UTF-8 输出。

## 核心概念

| 概念 | 说明 |
|---|---|
| Identity | 人格、称呼、边界、版本；语气/口癖写在 persona 文本里 |
| Directive | 全局或项目级 standing instruction，带优先级 |
| Project | git remote 或目录名生成稳定 key；保存 name / path / conventions |
| Memory | `fact / preference / decision / lesson / procedure / reference`，带 topic、scope、rationale、conditions、置信度与来源 |
| Episode | 原始对话/文档片段（批量 ingest 时的证据来源；不是主写入路径） |
| Document | Markdown/ADR，按项目挂载，由 agent 通过 `doc_add` 写入 |
| Skill | 文件夹包：`SKILL.md` + scripts/references/assets + 状态（candidate/validated/global） |
| context_version | 身份 + 指令 + 项目约定的哈希；变更后旧会话失效 |
| Bootstrap Gate | 没有有效 context_version，记忆/文档/技能工具一律拒绝 |

## 目录

```text
membridge/
├─ db.py          SQLite schema / 连接 / 时间与 JSON 工具
├─ service.py     Identity、Bootstrap、memory_write、文档、文件夹技能、audit
├─ webui.html     Hub 自带的 Material 3 只读控制台
├─ mcp_server.py  MCP stdio JSON-RPC、工具 schema、Gate 错误
├─ remote.py      HTTP Hub + RemoteService（MCP proxy）
├─ cli.py         命令行
└─ seed.py        演示数据
tests/
├─ test_service.py  服务/门禁/项目 key/中文检索/导入导出
├─ test_mcp.py      MCP initialize / tools / bootstrap / 跨进程 context
├─ test_remote.py   Hub / RemoteService / MCP proxy / 鉴权
├─ test_memory_write.py  直接 active 写入 / topic upsert / scope / rationale / identity / audit
├─ test_skills.py  文件夹技能 / 路径安全
└─ test_webui.py  Web UI / health
```

## 借用/参考了哪些开源设计

- **Letta MemFS**：root 身份文件 always-on + `MEMORY.md` 索引 + lazy 目录 + Git 版本 → 本项目的 Identity + 项目上下文分层。
- **mcp-memory-service**：`get_bootstrap_profile` 的 token 预算、avoid rules、user corrections → 本项目的 bootstrap 包结构。
- **Memobase**：结构化用户画像与分层 buffer → 本项目用 project/global scope + topic 表达画像，由 AI 直接写。
- **MemOS**：L1 trace / L2 policy / L3 world model / Skill → 后续技能晋升的参考。
- **官方 MCP Memory Server**：“每次开始先 Remembering…” → 证明 bootstrap 目前多靠客户端提示词；本项目用 Gate 把它变成服务端约束。

## 当前限制（下一阶段）

- **远程是 proxy + Hub**：本地 MCP proxy 连 HTTP Hub，不是原生 MCP streamable HTTP；Hub 是中心 SQLite 单点。
- **Hub 安全**：只有 Bearer token，没有账号/TLS；公网必须放在 Tailscale / Cloudflare Tunnel / Caddy 之后。
- **没有服务端 LLM 抽取**：这是设计选择；AI 客户端自己判断何时调用 `memory_write`，服务端只校验 topic/scope/rationale/conditions。
- **没有候选/批准**：这是设计选择，不是缺失；写入直接 active，靠 topic + scope + rationale + conditions + audit 保证可治理。
- **没有向量检索**：中文用双字、英文用词匹配；embedding/rerank 未接。
- **没有 op log / 字段级冲突**：软删除有墓碑，但 Hub 模式下并发写同一记忆是最后写入生效；导入是 `INSERT OR IGNORE`。
- **技能晋升未做审核 UI**：文件夹安装/读取已做，candidate → validated 的晋升仍靠 CLI/工具。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -v
```

当前 27 个测试覆盖：bootstrap、Gate、中文检索、项目隔离、项目 key（路径/ git remote）、context 失效、猫娘人格、文档/文件夹技能、反馈、导出导入、MCP initialize/tools/bootstrap、跨进程复用 context、HTTP Hub、RemoteService、MCP proxy、鉴权、远程 memory_write、Web UI/health、文件夹技能/路径安全、memory_write 直接 active/topic upsert/scope/rationale、identity_update/audit。

## 文档

- [产品需求文档](doc/需求/MemoryBridge-产品需求文档.md)
- [调研与可行性](doc/调研/MemoryBridge-调研与可行性.md)
- [开源复用调研](doc/调研/MemoryBridge-开源复用调研.md)
- [架构概览](doc/架构/MemoryBridge-架构概览.md)
- [ADR 架构决策](doc/架构/MemoryBridge-ADR架构决策文档.md)
- [MVP 实现说明](doc/设计/MemoryBridge-MVP实现.md)
- [12 周实施计划](doc/计划/MemoryBridge-12周实施计划.md)
- [文档索引](doc/README.md)
- [AI 协作规范](AGENTS.md)

## 许可

待定；当前建议 Apache-2.0，需与课程要求确认后写入 `LICENSE`。
