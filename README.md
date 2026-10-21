# 忆桥 MemoryBridge

忆桥是一个**自部署的 Agent 身份 / 记忆 / 能力同步器**：任何支持 MCP 的客户端，只要连上同一个 MemoryBridge，agent 在每次新会话先 `agent_bootstrap`，就能拿回同一份人格、全局指令、项目上下文、记忆、文档和技能。

> **当前状态：可运行 MVP（本地 stdio + HTTP Hub + 本地 MCP proxy）**。
> 已实现 Identity / Bootstrap Gate / 记忆 / 文档 / 技能 / CLI / 跨设备 Hub / 测试；自动抽取、向量检索、冲突合并是下一阶段。

## 现在能做到什么

- **同一个 agent**：新会话调用 `agent_bootstrap`，返回人格、称呼、语气、边界、全局指令、项目约定和 `context_version`。
- **Bootstrap Gate**：没 bootstrap 就调记忆/文档/技能工具，返回 `BOOTSTRAP_REQUIRED`；身份或项目上下文变了，旧 context 返回 `CONTEXT_STALE`。
- **项目隔离**：按项目路径自动建 project；默认检索“当前项目 + 全局”，不会把 A 项目私有记忆串到 B 项目。
- **记忆**：事实 / 偏好 / 决策 / 教训 / 流程 / 文档引用；中文双字 + 英文词检索；反馈会调整置信度。
- **文档**：Markdown/ADR 作为一等记忆，支持写入、搜索、按 id 读取。
- **技能**：可复用流程的候选/已验证状态，支持触发条件与步骤。
- **跨设备**：一台设备/服务器跑 `hub`，其他设备用 `serve --remote` 做本地 MCP proxy；连的是同一份身份与记忆。
- **零外部依赖**：Python 3.11+ 标准库 + SQLite；单文件数据库，可拷贝、可导出。
- **MCP 协议**：`initialize` 带 `instructions`；工具带描述与 annotations；另有 `memory://identity` 资源和 `recall_for_task` prompt。

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

- Hub 提供 `POST /rpc`、`GET /health`、`GET /export`；除 `/health` 外都要 Bearer token。
- 跨公网建议放在 Tailscale / Cloudflare Tunnel / Caddy TLS 之后，不要直接裸奔 `0.0.0.0:8787`。
- 项目 id 不再按绝对路径哈希：有 `origin` remote 时用 remote（如 `git:github.com/enoch-lyn/memorybridge`），否则用目录名；同一仓库在不同电脑上会落到同一个 project。
- 当前是“中心数据库”，没有 op log/冲突副本；并发写同一记忆时仍是最后写入生效。下一阶段做增量同步与冲突队列。

## CLI

```powershell
python -m membridge --db .\membridge.db init
python -m membridge --db .\membridge.db seed
python -m membridge --db .\membridge.db status
python -m membridge --db .\membridge.db bootstrap --project-path .
python -m membridge --db .\membridge.db persona set --name 猫娘 --tone 可爱
python -m membridge --db .\membridge.db directive add "先给结论" --scope global
python -m membridge --db .\membridge.db remember "分页用 cursor" --type decision --project-path .
python -m membridge --db .\membridge.db search "分页" --project-path .
python -m membridge --db .\membridge.db docs add .\docs\adr.md --project-path .
python -m membridge --db .\membridge.db docs search "SQLite" --project-path .
python -m membridge --db .\membridge.db skills list --project-path .
python -m membridge --db .\membridge.db export --out .\backup.json
python -m membridge --db .\membridge.db import --in .\backup.json
python -m membridge --db .\membridge.db hub --host 0.0.0.0 --port 8787
python -m membridge serve --remote http://127.0.0.1:8787 --token $env:MEMBRIDGE_TOKEN --project .
```

> `--json` 可放在命令任意位置；数据默认 UTF-8 输出。

## 核心概念

| 概念 | 说明 |
|---|---|
| Identity | 人格、称呼、语气、边界、版本；全局唯一，随数据库走 |
| Directive | 全局或项目级 standing instruction，带优先级 |
| Project | git remote 或目录名生成稳定 key；保存 name / path / conventions |
| Memory | `fact / preference / decision / lesson / procedure / reference`，带置信度与来源 |
| Document | Markdown/ADR，按项目挂载，可被检索与引用 |
| Skill | 触发条件 + 步骤 + 状态（candidate/validated/global） |
| context_version | 身份 + 指令 + 项目约定的哈希；变更后旧会话失效 |
| Bootstrap Gate | 没有有效 context_version，记忆/文档/技能工具一律拒绝 |

## 目录

```text
membridge/
├─ db.py          SQLite schema / 连接 / 时间与 JSON 工具
├─ service.py     Identity、Bootstrap、记忆、文档、技能、项目 key、导出
├─ mcp_server.py  MCP stdio JSON-RPC、工具 schema、Gate 错误
├─ remote.py      HTTP Hub + RemoteService（MCP proxy）
├─ cli.py         命令行
└─ seed.py        演示数据
tests/
├─ test_service.py  服务/门禁/项目 key/中文检索/导入导出
├─ test_mcp.py      MCP initialize / tools / bootstrap / 跨进程 context
└─ test_remote.py   Hub / RemoteService / MCP proxy / 鉴权
```

## 借用/参考了哪些开源设计

- **Letta MemFS**：root 身份文件 always-on + `MEMORY.md` 索引 + lazy 目录 + Git 版本 → 本项目的 Identity + 项目上下文分层。
- **mcp-memory-service**：`get_bootstrap_profile` 的 token 预算、avoid rules、user corrections → 本项目的 bootstrap 包结构。
- **Memobase**：结构化用户画像与“buffer + 少量固定 LLM 调用”的冷路径 → 后续自动抽取的参考。
- **MemOS**：L1 trace / L2 policy / L3 world model / Skill → 后续技能晋升的参考。
- **官方 MCP Memory Server**：“每次开始先 Remembering…” → 证明 bootstrap 目前多靠客户端提示词；本项目用 Gate 把它变成服务端约束。

## 当前限制（下一阶段）

- **远程是 proxy + Hub**：本地 MCP proxy 连 HTTP Hub，不是原生 MCP streamable HTTP；Hub 是中心 SQLite 单点。
- **Hub 安全**：只有 Bearer token，没有账号/TLS；公网必须放在 Tailscale / Cloudflare Tunnel / Caddy 之后。
- **没有自动抽取**：记忆目前由 agent/用户显式写入；LLM 抽取、去重、冲突合并未接。
- **没有向量检索**：中文用双字、英文用词匹配；embedding/rerank 未接。
- **没有冲突/墓碑/op log**：Hub 模式下并发写同一记忆是最后写入生效；导入是 `INSERT OR IGNORE`。
- **技能晋升未做审核 UI**：目前 CLI/工具直接写，后续加“候选 → 验证 → 晋升”。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -v
```

当前 18 个测试覆盖：bootstrap、Gate、中文检索、项目隔离、项目 key（路径/ git remote）、context 失效、猫娘人格、文档/技能、反馈、导出导入、MCP initialize/tools/bootstrap、跨进程复用 context、HTTP Hub、RemoteService、MCP proxy、鉴权。

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
