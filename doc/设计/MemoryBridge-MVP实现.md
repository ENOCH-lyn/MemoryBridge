# MemoryBridge MVP 实现说明

- 状态：v0.1（可运行）
- 日期：2026-10-21
- 代码：`membridge/`
- 测试：`tests/`

## 1. 目标与边界

MVP 只验证一件事：**同一个 MCP 服务，能否让任意客户端在新会话里恢复同一个 agent 的人格与上下文。**

已做：Identity、Bootstrap Gate、项目作用域、`memory_write` 直接 active、文档、技能文件夹、`identity_update`、audit、CLI、MCP、HTTP Hub。  
未做/下一阶段：向量检索、多设备并发冲突（op log）、多租户、E2EE。

## 2. 组件

```text
MCP client (Claude Code / Cursor / DSH / CLI)
        │  stdio JSON-RPC
        ▼
mcp_server.py
  ├─ initialize.instructions  "先 bootstrap"
  ├─ tools/list / tools/call
  ├─ resources: memory://identity
  └─ prompts: recall_for_task
        │
        ├── local mode ──────► service.py ──► db.py (SQLite)
        │
        └── remote mode ─────► remote.py RemoteService ──HTTP──► hub
                                                     (POST /rpc, Bearer)
```

- `service.py`：服务端只做校验与存储；不内置 LLM 抽取。
- `webui.html`：Hub 自带 Material 3 **只读**控制台（概览/记忆/文档/技能/身份/活动/连接）。

## 3. 数据模型

| 表 | 作用 |
|---|---|
| `identity` | 单行：name / persona / style_json / boundaries_json / version |
| `directives` | global 或 project 级 standing instruction，priority |
| `projects` | 稳定 key（git remote 或目录名）、id、name、path、conventions_json |
| `memories` | type / content / topic / scope(project_id) / rationale / conditions / confidence / source / status / episode_id / evidence_quote / content_hash / version / uses / helpful |
| `episodes` | legacy bulk ingest 的原始片段；不是主写入路径 |
| `documents` | project、path、title、content |
| `skills` | name / slug / description / status / manifest_json / dir_path / file_count；文件在 `data/skills/<id>/` |
| `sessions` | context_version、agent_id、project_id、identity_version、过期时间 |
| `audit_log` | 追加写审计；UI「活动」读它 |

## 3.1 项目识别与上下文注入

```text
project_path
  ├─ .membridge-project.json {"key":"custom:team/project","name":"..."} 优先
  ├─ git origin → git:host/path
  └─ 否则 name:<basename>
project_id = sha1(project_key)[:12]
```

- `bootstrap(project_path)` 自动创建/更新 project，并返回 `project`、`projects`（全部项目+计数）、`global_memories`、`project_memories`。
- `global_memories`：全局 active，pinned 优先，最多 20 条；新项目无搜索也能看到。
- `project_memories`：当前项目 active，pinned/最近优先，最多 20 条。
- `memory_search(scope=auto)`：当前项目 + 全局；`scope=all`：跨全部项目；结果带 `project_name/key`。
- `project_list`：列出项目 id/key/name/path/记忆/文档/技能计数；`memory_list(project_id=...)` 批量读。
- 同名目录可通过 `.membridge-project.json` 覆盖 key，避免 `name:src` 撞车。

## 4. Bootstrap 流程

```text
client → agent_bootstrap(project_path, agent_id, client)
  ├─ ensure_project(project_path)        # marker / git remote / 目录名 → 稳定 key，幂等
  ├─ identity = get_identity()
  ├─ directives = global + project
  ├─ context_version = sha256(identity.version, directives, project.conventions)
  ├─ INSERT OR REPLACE sessions(...)
  └─ return { identity, directives, project, projects, global_memories,
              project_memories, context_version, available, next_actions }
```

后续工具：

```text
memory_search(context_version, query)
  ├─ validate_context()
  │   ├─ session 不存在 → BOOTSTRAP_REQUIRED
  │   ├─ session 过期 → CONTEXT_STALE
  │   └─ 当前 identity/directives/project 哈希 ≠ ctx → CONTEXT_STALE
  └─ scope: 当前项目 + global（auto）/ 仅项目 / 仅 global / 全部项目（all）
```

`context_version` 只哈希“身份 + 指令 + 项目约定”，不哈希记忆条数；新增记忆不会让会话失效，改人格/项目约定会。

## 4.1 AI 直接写记忆（无候选/批准）

```text
memory_write(ctx, content, topic, type, scope="project",
             rationale, conditions, confidence, pinned=false)
  ├─ validate_context()
  ├─ topic 必须是稳定 lowercase key（如 decision.storage.sqlite）
  ├─ decision/lesson 必须有 rationale
  ├─ scope=project 默认当前项目；scope=global 仅用于用户级偏好
  ├─ SELECT active WHERE project_id IS ? AND topic = ?
  │    ├─ 命中 → UPDATE 同一条，version + 1
  │    └─ 未命中 → INSERT status='active'
  └─ audit: memory.write {topic, project_id, updated}
```

- **没有候选队列、没有人工批准**；UI 只读。AI 在用户提出持久要求时主动写。
- `topic` 是治理核心：后续修正更新同一条，不堆近似重复。
- `scope` 防止“A 项目用 SQLite”被写成所有项目都必须用 SQLite。
- `rationale` 记录为什么；`conditions` 记录适用边界（环境/阶段/并发/团队规范）。
- `pinned=true` 的记忆优先进入 bootstrap；global pinned 用于“主人喜欢黑色”这类跨项目核心事实。
- 身份用 `identity_update`；文档用 `doc_add`；技能用 `skill_install`/`skill_file`。
- `memory_ingest` / 服务端 LLM 抽取已移除；AI 客户端自己判断何时调用 `memory_write`。
- **技能是文件夹**：`SKILL.md`（YAML frontmatter + 正文）+ `scripts/`、`references/`、`assets/`。Hub 落盘到 `data/skills/<skill_id>/`，DB 只存元数据与文件清单；`skill_install` / `skill_file` 读写整个包，UI 只读查看。

## 5. 检索

- 中文：双字 bigram；英文：单词；去重后逐个 `in` 文本。
- 只检索 `status='active'`；候选/拒绝/删除不可见（legacy bulk ingest 的中间态不进入 UI）。
- 评分：`0.55*覆盖率 + 0.15*精确匹配 + 0.10*置信度 + 0.10*时间衰减 + 0.10*类型先验`。
- 输出：content、score、snippet、`[mem:id]`、episode_id/evidence；文档同理。
- 这是**词法检索**，不是 embedding；下一阶段接 bge-m3/pgvector。

## 6. MCP 协议

- `initialize` 返回 `protocolVersion`、`capabilities`、`serverInfo`、`instructions`。
- `instructions` 明确要求每次新会话先 `agent_bootstrap`，并在任务结束时用 `memory_ingest` 写入持久结论。
- 工具描述也重复该要求；其他工具要求 `context_version`。
- `tools/call` 的 Gate 错误返回 `isError: true` + `structuredContent.code`。
- 资源 `memory://identity`；prompt `recall_for_task`。
- 本地模式走 stdio；stdout 只写 JSON-RPC，日志走 stderr。

### 6.2 Web 控制台

Hub 在 `GET /` 直接返回 `webui.html`（单文件 HTML/CSS/JS，无构建、无 CDN）。界面按 Material 3 设计：导航 rail、tonal surface、filled/tonal/outlined 按钮、明暗主题、snackbar/dialog；不是营销落地页，而是操作控制台。

- 概览：身份、活跃记忆、文档/技能数、LLM 状态、AI 写入契约、最近写入。
- 记忆：只读；显示 type、project/global、topic、rationale、conditions、证据。
- 文档/技能：只读列表；技能可查看目录与单文件。
- 身份：只读；AI 通过 `identity_update` 改 persona/address/boundaries。
- 活动：显示 memory/doc/skill/identity 的 audit 写入。
- 连接：token、项目路径、MCP 配置、导出、健康检查。
- 连接：token、项目路径、MCP 配置片段、`/export` 下载、健康检查。

浏览器通过同源 `POST /rpc` 调用 Hub，token 存在 `localStorage`；页面本身不含数据，未带 token 只能看到连接页。

### 6.1 远程模式（Hub + 本地 Proxy）

```text
设备 A（有 DB）:  membridge hub --host 0.0.0.0 --port 8787 --token ...
设备 B（无 DB）:  membridge serve --remote http://A:8787 --token ... --project .
```

- Hub：`POST /rpc`（JSON `{method,args,kwargs}`）、`GET /health`、`GET /export`；Bearer token 鉴权。
- Hub 每个请求新建 `AgentSyncService`，避免 SQLite 跨线程问题；WAL 处理并发读。
- 本地 MCP proxy 用 `RemoteService` 把同名方法转发到 Hub；Gate 错误码原样转成 `GateError`。
- 项目 key 不再按绝对路径：有 `origin` remote 用 remote（`git:github.com/...`），否则用目录名。这样两台电脑 clone 同一仓库会落到同一个 project。
- 仍未做：op log、冲突副本、Hub 横向扩展、账号体系；并发写同一记忆是最后写入生效。

## 7. 参考与差异

| 来源 | 借用 | 差异 |
|---|---|---|
| Letta MemFS | root 身份 always-on、lazy 目录、Git 版本思路 | 本项目不绑定 runtime；用 SQLite + MCP |
| mcp-memory-service | bootstrap profile 的 token 预算/avoid rules/user corrections | 本项目把 bootstrap 做成 Gate；并加项目隔离 |
| Memobase | 结构化画像、分层 buffer | 本项目用 project/global scope + topic 表达，由 AI 直接写 |
| MemOS | L1 trace / L2 policy / L3 world model / Skill | 本项目保留 Skill 文件夹与 audit，不引入服务端 LLM |
| 官方 MCP memory | 先 “Remembering” 再检索 | 本项目用 `BOOTSTRAP_REQUIRED` 半强制 |

## 8. 开源对照（本项目采用的模式）

- [Mem0](https://github.com/mem0ai/mem0)：`user_id` / `agent_id` / `run_id` + metadata 过滤 → 本项目用 `scope=global|project`、`project_id`、`topic` 表达；不把项目结论写成全局。
- [Letta memory blocks](https://docs.letta.com/guides/agents/memory-blocks)：core memory 固定注入 + archival memory 检索 → 本项目 bootstrap 注入 `global_memories`/`project_memories`（pinned 优先），其余走 `memory_search`。
- [Zep](https://github.com/getzep/zep)：按 user/session/namespace 分图 → 本项目用 project key + project_id 分区；跨项目只允许显式 `scope=all`。
- [Basic Memory](https://github.com/basicmachines-co/basic-memory)：project = 本地目录，Markdown 为真相 → 本项目以 project_path 识别，文档仍需 agent 通过 `doc_add` 写入。
- [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service)：bootstrap profile 的 token 预算与 avoid rules → 本项目限制 bootstrap 记忆条数并优先 pinned。
- [MemOS](https://github.com/MemTensor/MemOS)：分层 memory / skill → 本项目保留 Skill 文件夹 + audit，不引入服务端 LLM 抽取。

设计结论：**项目是显式上下文，不是 AI 猜的；全局是显式 scope，不是默认；跨项目是显式查询，不是隐式混入。**

## 9. 已知限制与下一步

1. 远程：已有 Hub + 本地 proxy + Bearer；待 TLS、账号体系、限流、Hub 高可用。
2. 多设备同步：Hub 是中心 SQLite；待 op log + 游标 + 冲突副本，当前并发写最后写入生效。
3. 写入治理：topic/scope/rationale/conditions 已实现；待补冲突检测与批量审计报表。
4. 向量检索：bge-m3 + pgvector + 混合排序。
5. 技能晋升：候选 → 验证 → 项目/全局，带审计。
6. 安全：密钥扫描、提示注入标记、权限与速率限制。
