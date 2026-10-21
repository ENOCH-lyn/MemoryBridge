# MemoryBridge MVP 实现说明

- 状态：v0.1（可运行）
- 日期：2026-10-21
- 代码：`membridge/`
- 测试：`tests/`

## 1. 目标与边界

MVP 只验证一件事：**同一个 MCP 服务，能否让任意客户端在新会话里恢复同一个 agent 的人格与上下文。**

不做：自动 LLM 抽取、向量检索、远程 HTTP、多设备并发同步、鉴权。  
先做：Identity、Bootstrap Gate、项目作用域、记忆/文档/技能、CLI、MCP 协议、测试。

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
        ▼
service.py
  ├─ AgentSyncService
  ├─ Identity / Directives
  ├─ Bootstrap + context_version + Gate
  ├─ Memory / Documents / Skills
  └─ Export / Import
        │
        ▼
db.py  →  SQLite 单文件
```

## 3. 数据模型

| 表 | 作用 |
|---|---|
| `identity` | 单行：name / persona / style_json / boundaries_json / version |
| `directives` | global 或 project 级 standing instruction，priority |
| `projects` | 路径哈希 id、name、path、conventions_json |
| `memories` | type / content / confidence / source / status / uses / helpful |
| `documents` | project、path、title、content |
| `skills` | name / trigger / steps_json / status / success / failure |
| `sessions` | context_version、agent_id、project_id、identity_version、过期时间 |
| `audit_log` | 追加写审计 |

## 4. Bootstrap 流程

```text
client → agent_bootstrap(project_path, agent_id, client)
  ├─ ensure_project(project_path)        # 路径哈希，幂等
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

## 5. 检索

- 中文：双字 bigram；英文：单词；去重后逐个 `in` 文本。
- 评分：`0.55*覆盖率 + 0.15*精确匹配 + 0.10*置信度 + 0.10*时间衰减 + 0.10*类型先验`。
- 输出：content、score、snippet、`[mem:id]`；文档同理。
- 这是**词法检索**，不是 embedding；下一阶段接 bge-m3/pgvector。

## 6. MCP 协议

- `initialize` 返回 `protocolVersion`、`capabilities`、`serverInfo`、`instructions`。
- `instructions` 明确要求每次新会话先 `agent_bootstrap`。
- 工具描述也重复该要求；其他工具要求 `context_version`。
- `tools/call` 的 Gate 错误返回 `isError: true` + `structuredContent.code`。
- 资源 `memory://identity`；prompt `recall_for_task`。
- 仅 stdio；stdout 只写 JSON-RPC，日志走 stderr。

## 7. 参考与差异

| 来源 | 借用 | 差异 |
|---|---|---|
| Letta MemFS | root 身份 always-on、lazy 目录、Git 版本思路 | 本项目不绑定 runtime；用 SQLite + MCP |
| mcp-memory-service | bootstrap profile 的 token 预算/avoid rules/user corrections | 本项目把 bootstrap 做成 Gate；并加项目隔离 |
| Memobase | 结构化画像、buffer + 固定 LLM 调用 | 本项目先手动/agent 写入，自动抽取下一阶段 |
| MemOS | L1/L2/L3 + Skill | 本项目只保留 Skill 最小表 |
| 官方 MCP memory | 先 “Remembering” 再检索 | 本项目用 `BOOTSTRAP_REQUIRED` 半强制 |

## 8. 已知限制与下一步

1. 远程 MCP / HTTP：加 bearer token、TLS、会话管理。
2. 多设备同步：op log + 游标 + 冲突副本；当前导出/导入只是过渡。
3. 自动抽取：LLM 抽取 + 去重 + 冲突检测 + 审核队列。
4. 向量检索：bge-m3 + pgvector + 混合排序。
5. 技能晋升：候选 → 验证 → 项目/全局，带审计。
6. 安全：密钥扫描、提示注入标记、权限与速率限制。
