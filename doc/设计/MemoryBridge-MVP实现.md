# MemoryBridge MVP 实现说明

- 状态：v0.1（可运行）
- 日期：2026-10-21
- 代码：`membridge/`
- 测试：`tests/`

## 1. 目标与边界

MVP 只验证一件事：**同一个 MCP 服务，能否让任意客户端在新会话里恢复同一个 agent 的人格与上下文。**

已做：Identity、Bootstrap Gate、项目作用域、记忆/文档/技能文件夹、CLI、MCP、HTTP Hub、可选 LLM 抽取、watch 自动同步。  
未做/下一阶段：向量检索、多设备并发冲突（op log）、多租户、E2EE。

## 4.2 自动同步（watch）

```text
membridge init-project --path .
  └─ .membridge/{inbox,skills,state.json}

membridge watch --project-path . [--remote URL] [--auto-activate]
  ├─ docs/**/*.md            → doc_add（按内容 hash 去重）
  ├─ .membridge/skills/*/    → skill_install（整个文件夹）
  └─ .membridge/inbox/*      → memory_ingest（按内容 hash 去重）
```

- 正常使用不需要在 UI 手工“采集”；UI 的收件箱页只保留手动补录 fallback。
- 默认抽取为候选，人工审核后 active；加 `--auto-activate` 可让高置信候选直接进入 active（个人使用可选）。
- MCP `initialize.instructions` 要求 agent 在任务结束时调用 `memory_ingest`，把持久结论写回。
- 同步是本地进程 push 到 Hub，不依赖 Hub 访问用户文件系统；删除/重命名暂不自动处理。


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

- `extract.py`：确定性规则抽取；`llm.py`：可选 OpenAI 兼容抽取，失败回退规则。
- `sync.py`：本地项目同步器；`watch` 轮询 docs/、`.membridge/skills/`、`.membridge/inbox/`。
- `webui.html`：Hub 自带单页控制台（Material 3：概览/收件箱/记忆/候选/文档/技能/身份/连接）。

## 3. 数据模型

| 表 | 作用 |
|---|---|
| `identity` | 单行：name / persona / style_json / boundaries_json / version |
| `directives` | global 或 project 级 standing instruction，priority |
| `projects` | 稳定 key（git remote 或目录名）、id、name、path、conventions_json |
| `memories` | type / content / confidence / source / status / episode_id / evidence_quote / content_hash / version / uses / helpful |
| `episodes` | 原始对话/文档片段，候选记忆的证据来源 |
| `documents` | project、path、title、content |
| `skills` | name / trigger / steps_json / status / success / failure |
| `sessions` | context_version、agent_id、project_id、identity_version、过期时间 |
| `audit_log` | 追加写审计 |

## 4. Bootstrap 流程

```text
client → agent_bootstrap(project_path, agent_id, client)
  ├─ ensure_project(project_path)        # git remote / 目录名 → 稳定 key，幂等
  ├─ identity = get_identity()
  ├─ directives = global + project
  ├─ context_version = sha256(identity.version, directives, project.conventions)
  ├─ INSERT OR REPLACE sessions(...)
  └─ return { identity, directives, project, context_version, available, next_actions }
```

后续工具：

```text
memory_search(context_version, query)
  ├─ validate_context()
  │   ├─ session 不存在 → BOOTSTRAP_REQUIRED
  │   ├─ session 过期 → CONTEXT_STALE
  │   └─ 当前 identity/directives/project 哈希 ≠ ctx → CONTEXT_STALE
  └─ scope: 当前项目 + global（auto）/ 仅项目 / 仅 global
```

`context_version` 只哈希“身份 + 指令 + 项目约定”，不哈希记忆条数；新增记忆不会让会话失效，改人格/项目约定会。

## 4.1 Capture：Episode → 候选 → 审核

```text
memory_ingest(context_version, text)
  ├─ validate_context()
  ├─ INSERT episodes(content, project_id, agent_id, source)
  ├─ extract.py: 规则切句/分类 → candidates
  ├─ 去重（content_hash + active/candidate/rejected）
  ├─ INSERT memories(status='candidate' | 'active' if auto_activate)
  └─ return { episode_id, candidates, duplicates }

memory_candidates(ctx)       # 列出待审核
memory_decide(ctx, id, approve/reject)
memory_update(ctx, id, content/confidence)
memory_delete(ctx, id)       # 软删除 + deleted_at
```

- 候选默认不可检索；`memory_search` 只查 `status='active'`。
- 每条候选保留 `episode_id` + `evidence_quote`，可回溯到原始片段。
- `reject` 后同一内容的 hash 仍在 rejected，`memory_ingest` 不会自动重复提出。
- 抽取器：规则版始终可用；配置 `MEMBRIDGE_LLM_BASE_URL/API_KEY/MODEL` 后优先 LLM，失败回退规则，结果里返回 `extractor` 与 `llm_error`。已用真实 OpenAI 兼容端点验证抽取，候选保留证据引用。
- 自动入口：`membridge watch` 监听 `.membridge/inbox`，不需要手工“采集”；docs/ 与 skill 文件夹也由同一个 watch 增量同步。MCP instructions 要求 agent 在任务结束时调用 `memory_ingest` 写入持久结论。
- 技能是**文件夹**：`SKILL.md`（YAML frontmatter + 正文）+ `scripts/`、`references/`、`assets/`。Hub 落盘到 `data/skills/<skill_id>/`，DB 只存元数据与文件清单；`skill_install` / `skill_file` 读写整个包，UI 可上传目录。

## 5. 检索

- 中文：双字 bigram；英文：单词；去重后逐个 `in` 文本。
- 只检索 `status='active'`；候选/拒绝/删除不可见。
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

- 概览：身份、活跃记忆/候选/文档/技能、LLM 状态、手动补录。
- 收件箱：说明 `membridge watch` 自动同步；手动粘贴仅作 fallback。
- 记忆：搜索、编辑、软删除；候选：批准/拒绝。
- 文档/技能：文档列表；技能文件夹列表、目录查看、文件读取、目录上传。
- 身份：改 persona/style/boundaries；保存后下一次 bootstrap 生效。
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
| Memobase | 结构化画像、buffer + 固定 LLM 调用 | 本项目先手动/agent 写入，自动抽取下一阶段 |
| MemOS | L1/L2/L3 + Skill | 本项目只保留 Skill 最小表 |
| 官方 MCP memory | 先 “Remembering” 再检索 | 本项目用 `BOOTSTRAP_REQUIRED` 半强制 |

## 8. 已知限制与下一步

1. 远程：已有 Hub + 本地 proxy + Bearer；待 TLS、账号体系、限流、Hub 高可用。
2. 多设备同步：Hub 是中心 SQLite；待 op log + 游标 + 冲突副本，当前并发写最后写入生效。
3. 抽取：规则 + 可选 LLM（OpenAI 兼容）；待语义去重、字段级冲突、LLM 质量评测。
4. 向量检索：bge-m3 + pgvector + 混合排序。
5. 技能晋升：候选 → 验证 → 项目/全局，带审计。
6. 安全：密钥扫描、提示注入标记、权限与速率限制。
