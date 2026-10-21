# 忆桥 MemoryBridge

忆桥是一个**自部署的 Agent 身份 / 记忆 / 能力同步器**：任何支持 MCP 的客户端，只要连上同一个 MemoryBridge，agent 在每次新会话先 `agent_bootstrap`，就能拿回同一份人格、全局指令、项目上下文、记忆、文档和技能。

> **当前状态：可运行 MVP（MCP stdio + HTTP Hub + Web 控制台 + 可选 LLM 抽取）**。
> 已实现 Identity / Bootstrap Gate / Episode / 规则或 LLM 抽取 / 候选审核 / 记忆 / 文档 / 技能 / CLI / 跨设备 Hub / 测试；向量检索、op log/冲突合并、TLS/账号体系是下一阶段。

## 现在能做到什么

- **同一个 agent**：新会话调用 `agent_bootstrap`，返回人格、称呼、语气、边界、全局指令、项目约定和 `context_version`。
- **Bootstrap Gate**：没 bootstrap 就调记忆/文档/技能工具，返回 `BOOTSTRAP_REQUIRED`；身份或项目上下文变了，旧 context 返回 `CONTEXT_STALE`。
- **项目隔离**：按 git remote 或目录名建稳定 project；默认检索“当前项目 + 全局”，不会把 A 项目私有记忆串到 B 项目。
- **记忆**：事实 / 偏好 / 决策 / 教训 / 流程 / 文档引用；中文双字 + 英文词检索；反馈会调整置信度。
- **Capture & Curation**：`memory_ingest` 把原始对话存成 Episode，用规则或可选 LLM 抽取候选记忆；候选默认不可检索，批准后才进入 active，保留 `evidence_quote` 与 `episode_id`。
- **Web 控制台**：Hub 自带单页 UI（概览 / 采集 / 记忆 / 候选 / 文档 / 技能 / 身份 / 连接），浏览器直接管理，无需 Node 构建。
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

试试自动抽取 + 审核：

```powershell
python -m membridge --db .\membridge.db ingest `
  "以后都用中文提交信息。我们决定用 SQLite 而不是 Postgres。" `
  --project-path demo

python -m membridge --db .\membridge.db candidates --project-path demo
# 复制其中一个 memory_id
python -m membridge --db .\membridge.db approve mem_xxx --project-path demo
python -m membridge --db .\membridge.db search "中文" --project-path demo
```

`ingest` 会先写 Episode，再抽取候选；候选默认不可检索，`approve` 后才进入 active。`reject` 后同一句不会被自动重复抽取。

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

## Web 控制台

Hub 启动后，浏览器打开：

```text
http://HUB_IP:8787/
```

在「连接」页填 Hub 的 `MEMBRIDGE_TOKEN`，保存后即可使用：

- **概览**：身份、活跃记忆、候选数、抽取器状态、快速采集。
- **采集**：粘贴对话，选择“高置信直接激活 / 使用 LLM”，查看抽取结果。
- **记忆**：搜索、查看证据、编辑内容、软删除。
- **候选**：批准 / 拒绝；候选批准前不参与检索。
- **文档**：列表、查看、添加/更新 Markdown。
- **技能**：列表、添加。
- **身份**：改人格、称呼、语气、边界；保存后所有设备下一次 bootstrap 生效。
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

可选 LLM 抽取（OpenAI 兼容接口）：

```bash
MEMBRIDGE_LLM_BASE_URL=https://api.deepseek.com/v1
MEMBRIDGE_LLM_API_KEY=sk-...
MEMBRIDGE_LLM_MODEL=deepseek-chat
```

不配置就使用规则抽取；配置后 `memory_ingest` 会优先调 LLM，失败自动回退规则，并在结果里返回 `llm_error`。

## CLI

```powershell
python -m membridge --db .\membridge.db init
python -m membridge --db .\membridge.db seed
python -m membridge --db .\membridge.db status
python -m membridge --db .\membridge.db bootstrap --project-path .
python -m membridge --db .\membridge.db persona set --name 猫娘 --tone 可爱
python -m membridge --db .\membridge.db directive add "先给结论" --scope global
python -m membridge --db .\membridge.db remember "分页用 cursor" --type decision --project-path .
python -m membridge --db .\membridge.db ingest "以后都用中文提交信息。我们决定用 SQLite 而不是 Postgres。" --project-path .
python -m membridge --db .\membridge.db candidates --project-path .
python -m membridge --db .\membridge.db approve mem_xxx --project-path .
python -m membridge --db .\membridge.db reject mem_xxx --project-path .
python -m membridge --db .\membridge.db update mem_xxx --content "新的内容" --project-path .
python -m membridge --db .\membridge.db forget mem_xxx --project-path .
python -m membridge --db .\membridge.db search "分页" --project-path .
python -m membridge --db .\membridge.db docs add .\docs\adr.md --project-path .
python -m membridge --db .\membridge.db docs list --project-path .
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
| Episode | 原始对话/文档片段，候选记忆的证据来源 |
| Candidate | 抽取后待审核的记忆；默认不可检索，approve 后 active |
| Document | Markdown/ADR，按项目挂载，可被检索与引用 |
| Skill | 触发条件 + 步骤 + 状态（candidate/validated/global） |
| context_version | 身份 + 指令 + 项目约定的哈希；变更后旧会话失效 |
| Bootstrap Gate | 没有有效 context_version，记忆/文档/技能工具一律拒绝 |

## 目录

```text
membridge/
├─ db.py          SQLite schema / 连接 / 时间与 JSON 工具
├─ service.py     Identity、Bootstrap、记忆、Episode、审核、文档、技能
├─ extract.py     规则抽取（Episode → candidate）
├─ llm.py         可选 LLM 抽取（OpenAI 兼容，失败回退规则）
├─ webui.html     Hub 自带的单页控制台
├─ mcp_server.py  MCP stdio JSON-RPC、工具 schema、Gate 错误
├─ remote.py      HTTP Hub + RemoteService（MCP proxy）
├─ cli.py         命令行
└─ seed.py        演示数据
tests/
├─ test_service.py  服务/门禁/项目 key/中文检索/导入导出
├─ test_capture.py  Episode/抽取/审核/去重/更新/删除
├─ test_mcp.py      MCP initialize / tools / bootstrap / 跨进程 context
├─ test_remote.py   Hub / RemoteService / MCP proxy / 鉴权
└─ test_llm_webui.py  LLM 抽取/回退、Web UI / health
```

## 借用/参考了哪些开源设计

- **Letta MemFS**：root 身份文件 always-on + `MEMORY.md` 索引 + lazy 目录 + Git 版本 → 本项目的 Identity + 项目上下文分层。
- **mcp-memory-service**：`get_bootstrap_profile` 的 token 预算、avoid rules、user corrections → 本项目的 bootstrap 包结构。
- **Memobase**：结构化用户画像与“buffer + 少量固定 LLM 调用”的冷路径 → 规则抽取 + 候选审核；LLM 抽取下一阶段。
- **MemOS**：L1 trace / L2 policy / L3 world model / Skill → 后续技能晋升的参考。
- **官方 MCP Memory Server**：“每次开始先 Remembering…” → 证明 bootstrap 目前多靠客户端提示词；本项目用 Gate 把它变成服务端约束。

## 当前限制（下一阶段）

- **远程是 proxy + Hub**：本地 MCP proxy 连 HTTP Hub，不是原生 MCP streamable HTTP；Hub 是中心 SQLite 单点。
- **Hub 安全**：只有 Bearer token，没有账号/TLS；公网必须放在 Tailscale / Cloudflare Tunnel / Caddy 之后。
- **抽取是规则 + 可选 LLM**：`extract.py` 规则始终可用；配置 `MEMBRIDGE_LLM_*` 后优先调 LLM，失败回退规则。语义去重/字段级冲突未接。
- **没有向量检索**：中文用双字、英文用词匹配；embedding/rerank 未接。
- **没有 op log / 字段级冲突**：软删除有墓碑，但 Hub 模式下并发写同一记忆是最后写入生效；导入是 `INSERT OR IGNORE`。
- **技能晋升未做审核 UI**：目前 CLI/工具直接写，后续加“候选 → 验证 → 晋升”。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -v
```

当前 30 个测试覆盖：bootstrap、Gate、中文检索、项目隔离、项目 key（路径/ git remote）、context 失效、猫娘人格、Episode/抽取/审核/去重/更新/删除、文档/技能、反馈、导出导入、MCP initialize/tools/bootstrap、跨进程复用 context、HTTP Hub、RemoteService、MCP proxy、鉴权、远程 ingest/review、LLM 抽取/回退、Web UI/health。

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
