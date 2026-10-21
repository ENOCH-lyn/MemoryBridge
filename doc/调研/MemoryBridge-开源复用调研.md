# MemoryBridge 开源复用调研：Agent 身份 / 记忆 / 能力同步器

- 日期：2026-10-21
- 问题：现有开源项目能否直接复用？哪条路线最快、最适合课程大作业？
- 方法：官方 README、文档、仓库代码与论文；未对所有项目做统一环境压测；性能数字多为项目自报。
- 结论先行：**没有现成项目完整实现“一个 MCP 承载同一个 agent 的身份、记忆、能力，并在每次会话 bootstrap”的产品；但组件层可复用度很高。建议自研薄协议层，复用存储、抽取、embedding、文档与技能设计的成熟部分。**

---

## 1. 结论摘要

| 判断 | 结论 |
|---|---|
| 有没有“开箱即用”的整体 | **没有**。现有项目要么是记忆库，要么是 agent runtime，要么是文件型知识库；没人把“启动时恢复同一个 agent”作为协议一等公民 |
| 最接近的项目 | **mcp-memory-service**（Bootstrap Profile + MCP + 多设备同步）、**Letta MemFS**（root 身份文件 + Git 同步）、**MemOS**（L1/L2/L3 + Skill）、**Memobase**（结构化用户画像）、**Basic Memory**（Markdown + MCP + 会话启动插件） |
| 可直接复用 | MCP SDK / FastMCP、Postgres + pgvector、本地 embedding、Memobase 画像 schema、mcp-memory-service 的 bootstrap profile 设计、OpenMemory 的会话导入、官方 MCP memory 的 KG schema |
| 必须自研 | **Identity Pack**、**Bootstrap Gate**、**context_version**、**项目作用域与优先级**、**来源/信任/冲突治理**、**跨设备 op log 同步**、**经验晋升** |
| 推荐路线 | 自研 `agent-sync` MCP（FastMCP + Postgres/pgvector），复用 Memobase 抽取思路与 mcp-memory-service 的 bootstrap 设计；把 Mem0/Graphiti 作为可插拔后端，而不是底座 |
| 最快路线（降级） | Fork `mcp-memory-service`，加 Identity Pack + Gate + 项目隔离；2–3 周可出 demo，但课程创新点会被稀释 |
| 不建议 | 把 Letta 当底座（锁 runtime，违背“任何 MCP 客户端”）；复刻 MemOS 全量（Neo4j + Qdrant + 多适配器，课程做不完）；直接部署 Graphiti 参考 MCP（无鉴权） |

一句话：**“记忆”已经卷得很厉害，“同一个 agent 的启动协议”还没人认真做。** 你的差异化不在“再做一个 Mem0”，而在 **Bootstrap + Identity + Scope + Governance** 这一层。

---

## 2. 评估框架

按你的需求，用 10 个维度评估：

| 维度 | 说明 |
|---|---|
| 身份 / 人格 | 是否支持 persona、称呼、语气、边界等 always-on 身份 |
| 启动协议 | 是否有“每次会话先加载”的机制；是否强制 |
| 跨设备 | 是否有多设备同步；离线/冲突如何处理 |
| 跨 agent | 是否 MCP 原生；是否绑定某个 runtime |
| 项目隔离 | 是否能按项目/仓库隔离，避免全局记忆污染 |
| 治理 | 来源、置信度、时效、冲突、删除、审计 |
| 文档 | 是否能把文档作为一等记忆 |
| 技能 / 经验 | 是否支持可复用 Skill/工作流与晋升 |
| 许可 | 是否允许课程项目复用/修改 |
| 部署 | 单机可跑？依赖多重？ |

---

## 3. 重点项目逐个看

### 3.1 mcp-memory-service：最接近“MCP 记忆服务”的整体

**是什么**：Python 实现的记忆后端，同时提供 REST、MCP、OAuth、CLI 和 Web Dashboard；支持 SQLite（本地）、Cloudflare（云）、Hybrid（本地读 + 后台云同步）、Milvus；本地 ONNX embedding；知识图谱、beliefs/矛盾检测、自动整合、文档导入；Apache-2.0（[仓库](https://github.com/doobidoo/mcp-memory-service)）。

**与你需求最相关的点**：

- **`get_bootstrap_profile`**：为新 agent 组装“行为画像”，带 token 预算，支持 task-aware 检索；包含 mistake notes（avoid rules）和 user corrections（偏好），还能合并多个 agent 的数据（[测试](https://github.com/doobidoo/mcp-memory-service/blob/main/tests/test_bootstrap_profile.py)）。
- **多设备同步**：Hybrid backend 支持 Cloudflare 或另一个 HTTP MCP Memory Service 作为 secondary，delta-sync 默认关闭（[README](https://github.com/doobidoo/mcp-memory-service)）。
- **客户端 Hook**：Claude Code hooks、Codex hooks，能在会话开始取上下文、结束保存学习。
- **许可友好**：Apache-2.0，可改可商用。

**缺口**：

- **不是强制 bootstrap**：`get_bootstrap_profile` 是一个工具，模型可以不调用；没有 gate。
- **不按项目隔离**：README 明确写 “profiles are not isolated by project”。
- **没有 Identity Pack**：bootstrap 是“行为画像”，不是“人格/身份 + 全局指令 + 项目覆盖”的版本化模型。
- **没有 context_version**：人格/记忆更新后，无法判断当前会话是否过期。
- **单人维护**：README 自述由一个人维护，长期依赖有风险。

**复用建议**：**借鉴其 bootstrap profile 的结构与 token 预算策略**；如果走“最快路线”，可以 fork 后加 Identity/Gate/Project Scope。不建议原样作为最终产品，因为它的核心抽象是“记忆条目”，不是“同一个 agent”。

**适配分：4/5（最接近，但缺协议层）。**

---

### 3.2 Letta / Letta Code：最接近“身份 + 记忆 + 技能”的 runtime

**是什么**：MemGPT 团队的有状态 agent runtime；现在叫 Letta Code，支持 CLI、Web、Telegram/Slack/Discord、云端和本地 backend；Apache-2.0（[仓库](https://github.com/letta-ai/letta-code)）。

**最值得借鉴的是 MemFS**（[文档](https://docs.letta.com/concepts/memfs)）：

- 记忆是 **Git 仓库**，agent 用普通文件工具读写；commit/push 后成为“在所有电脑上的记忆”。
- 根目录的 `persona.md`、`human.md` 等文件**每轮都加载进 system prompt**；子目录通过 `MEMORY.md` 索引按需读取。这就是“身份 always-on + 深度记忆 lazy-load”的教科书式实现。
- `skills/` 放在记忆仓库里，随 agent 走。
- `/init` 初始化/刷新项目记忆，`/remember` 显式教学，`dreaming` 后台整合，`/doctor` 检查记忆漂移。
- 云端 agent 的 MemFS 可以跨电脑 projection；共享记忆用于多个云端 agent。

**缺口**：

- 它是 **agent runtime**，不是“任意 MCP 客户端共用的身份层”。你要用 Letta，就得把 agent 跑在 Letta 里；Claude Code/Cursor 不会因为连了 Letta 的 MCP 就自动变成同一个 Letta agent。
- 本地 backend 的跨设备同步要自己负责；云端是 Letta 托管。
- 对课程来说，直接基于 Letta 会变成“学 Letta”，而不是“做自己的 Agent Sync”。

**复用建议**：**照搬 MemFS 的信息架构**（root identity 文件 + `MEMORY.md` 索引 + lazy dirs + Git 版本），但用 MCP + 自己的存储实现。不要把 Letta 当底座。

**适配分：3.5/5（设计极佳，直接复用会锁 runtime）。**

---

### 3.3 Memobase：最值得复用的“结构化用户画像”抽取

**是什么**：以用户画像为中心的开源记忆系统；FastAPI + Postgres + Redis + pgvector；Apache-2.0（[仓库](https://github.com/memodb-io/memobase)）。

**值得借鉴**（来自独立分析 [lhl/agentic-memory](https://github.com/lhl/agentic-memory)）：

- **8 大类 / ~40 个子槽位的画像 schema**：`basic_info`、`contact_info`、`education`、`demographics`、`work`、`interest`、`psychological`、`life_event`；可按项目定制，strict/free 两种模式。
- **冷路径批处理**：buffer 到约 1024 token 后，固定 3 次 LLM 调用（entry summary → topic extraction → YOLO merge），成本可预测。
- **双通道**：可变的 UserProfile（“你是谁”）+ 只追加的 UserEvent（“发生了什么”）。
- **可复现的 LoCoMo 结果**：75.78 overall / 85.05 temporal（项目自报，仓库带预测与判分产物）。

**缺口**：

- 它是**用户画像**，不是**人格/行为指令**；“猫娘”这种 persona 不在其 schema 里。
- **没有 provenance、versioning、trust labels**：更新是 in-place，冲突靠 LLM 判断；外部内容可以写进画像，没有信任带。
- **有 MCP 集成，但不是以 bootstrap/身份为中心**；它的强项是画像抽取与读取，不是“任意客户端启动同一个 agent”。

**复用建议**：**复用画像 schema 与 3 次调用流水线**，但把写入改成“候选 → 审核 → 版本化”，并加 `source_episode_id`、`trust`、`valid_from/to`。这正好补上它的短板，也是你的治理差异化。

**适配分：4/5（抽取层最佳参考）。**

---

### 3.4 Mem0 / OpenMemory：抽取成熟，但“同步”不是它的重点

**Mem0 OSS**（Apache-2.0）：抽取—整合—检索流水线成熟；支持 `user_id` / `agent_id` / `run_id` 作用域；有图记忆变体；LoCoMo 上相对 OpenAI memory 提升 26%、p95 延迟降 91%、token 省 90%（论文自报，[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)）。Mem0 的 MCP 是**托管服务**，不是自部署（[文档](https://docs.mem0.ai/platform/mem0-mcp)）。

**OpenMemory**（MIT）：现在定位是“跨 coding harness 搬运会话”的 CLI/TUI，支持 Claude Code、Codex、OpenCode，能预览、选择性导入；Roadmap 包括 Skills、MCPs、Plugins、`CLAUDE.md`/`AGENTS.md`，以及实时 autosync（[仓库](https://github.com/mem0ai/openmemory)）。

**缺口**：

- OpenMemory 是**一次性/手动搬运**，autosync 还没完成；不是常驻身份服务。
- Mem0 MCP 托管，数据在 Mem0 账号；自部署要自己包 MCP。
- 没有“人格/身份 + 启动 gate”。

**复用建议**：**复用 OpenMemory 的会话导入/导出适配**（尤其 Claude Code/Codex 的 session 格式），用来做“首次迁移”；记忆抽取可以用 Mem0 OSS 作为 baseline 或 fallback，但不要让它定义你的数据模型。

**适配分：3/5（可作迁移工具与基线，不是核心）。**

---

### 3.5 MemOS 2.0：技能演化最完整，但工程最重

**是什么**：Memory OS；MemCube 隔离、MemScheduler 异步摄入、L1 trace / L2 policy / L3 world model、Skill 结晶；自部署需要 Neo4j + Qdrant；Apache-2.0（[仓库](https://github.com/MemTensor/MemOS)）。

**本地插件**（`apps/memos-local-plugin`）值得重点看：

- 本地 SQLite、文件型记忆；L1 轨迹、L2 策略、L3 世界模型、Skill 四层。
- 有 step-level（工具结果/observation）和 task-level（人类评分/隐式信号）两个反馈通道；高价值模式结晶为 Skill；检索按 Skill → trace/episode → world model 三层。
- 提供 OpenClaw / Hermes / DSH 适配器；DSH 版本在每轮用户输入后自动 recall（3 秒上限），后台做 capture/relation/intent/summary/embedding。

**缺口**：

- 它是**agent 插件**，不是 MCP；每个 agent 要单独适配。
- 本地插件没有跨设备同步；云端/自部署是另一套。
- 完整自部署重（Neo4j + Qdrant）；课程周期内复刻不现实。
- 人格/身份不是显式一等公民，更多是“记忆 + 策略”。

**复用建议**：**复刻“L1/L2/L3 + Skill”的分层与晋升思想**，但用最小 schema 实现（trace → lesson → skill）；不要复刻其调度与多适配器。

**适配分：3/5（技能层参考价值最高）。**

---

### 3.6 Basic Memory：文档 + MCP + 会话启动插件，但 AGPL

**是什么**：Markdown 文件 + wikilink 知识图谱 + SQLite 索引 + MCP；本地优先，官方云同步付费；Claude Code 插件提供 session-start briefings、pre-compaction checkpoints、`/basic-memory:*` 命令；AGPL-3.0（[仓库](https://github.com/basicmachines-co/basic-memory)）。

**值得借鉴**：

- Markdown 作为 canonical，人和 AI 双向编辑；Obsidian 兼容。
- MCP 工具带 behavior hints（readOnly/destructive/idempotent），便于客户端渐进发现。
- Claude Code 插件的“会话开始简报 + 压缩前 checkpoint”是**客户端侧启动注入**的成熟范例。
- 多项目路由、schema 系统、云/本地混合。

**缺口**：

- 没有 Identity Pack / 强制 bootstrap / 治理（来源、信任、冲突、时效）。
- AGPL-3.0：如果课程项目要闭源或换许可证，**不要复制代码**；可以作为独立 MCP 服务集成，或只借鉴文件格式。
- 云同步是其商业产品，自部署多设备要自己解决。

**复用建议**：**借鉴 Markdown 格式与“会话开始简报”交互**；代码层面保持距离，除非你接受 AGPL。

**适配分：3/5（文档层最佳参考，许可需注意）。**

---

### 3.7 Graphiti：时序知识图谱与来源追溯最强

**是什么**：Zep 的开源时序知识图谱；双时序（事实何时为真、系统何时知道）、增量更新、混合检索；MCP server 支持 FalkorDB/Neo4j；Apache-2.0（[仓库](https://github.com/getzep/graphiti)）。

**值得借鉴**：

- `valid_at` / `invalid_at` 的双时序边，适合“部署环境从 Vercel 换成 Cloudflare”这类冲突。
- `get_episode_entities` 可以从 episode 追溯创建的实体/事实，天然适合 provenance。
- 实体类型可配置（Preference / Requirement / Procedure / Event / Document 等）。
- MCP 工具有 `add_memory`、`search_memory_facts`、`get_episode_entities` 等，接口设计可参考。

**缺口**：

- 图数据库运维重（FalkorDB/Neo4j）；LLM 抽取成本高。
- 官方 MCP server 明确写“参考实现，无鉴权、无授权、无 prompt-injection 防护，不要生产部署”（[README](https://github.com/getzep/graphiti/tree/main/mcp_server)）。
- 没有身份/人格/技能晋升。

**复用建议**：作为 **P2 的可插拔 `GraphStore`**，不要一开始就上；MVP 用 Postgres 关系表 + 1-hop 足够。若做“时效/冲突”实验，可复用其双时序模型。

**适配分：2.5/5（强，但重）。**

---

### 3.8 Cognee：最简单的 `remember/recall/forget` MCP

**是什么**：开源记忆引擎 + MCP server；提供 `remember`、`recall`、`forget` 三个极简工具，支持 session cache 与永久图记忆、dataset 级隔离、Cloud/API/Direct 三种模式；Apache-2.0（[仓库](https://github.com/topoteretes/cognee)，[MCP README](https://github.com/topoteretes/cognee/tree/main/cognee-mcp)）。

**值得借鉴**：

- **极简工具面**：`remember/recall/forget`，降低模型选择成本；适合作为“记忆后端”的接口参考。
- 默认按 MCP 客户端自动命名 dataset（如 `claude_code_memory`），可通过 `COGNEE_MCP_AGENT_SCOPED=false` 共享；这是“agent 作用域”的一种实现。
- `search_tools` 渐进发现，避免一次暴露太多工具。

**缺口**：

- 没有身份/人格/启动 gate；dataset 是隔离，但不是“同一 agent 的连续身份”。
- 检索以图/向量为主，治理与时效弱于 Graphiti。

**复用建议**：如果只需要“记住/回忆”，可以直接用；但你的核心是 bootstrap + identity，Cognee 只能当后端候选。

**适配分：2.5/5。**

---

### 3.9 官方 MCP Memory Server：最小知识图谱 + “提示词式 bootstrap”

**是什么**：官方参考实现，本地 JSONL 知识图谱；实体/关系/观察；MIT（[仓库](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)）。

**最有趣的细节**：README 直接建议在客户端 Custom Instructions 里写：

> “Always begin your chat by saying only ‘Remembering...’ and retrieve all relevant information from your knowledge graph.”

这说明**官方生态也认为 bootstrap 要靠客户端提示词**，而不是服务端强制。它没有 gate、没有同步、没有身份模型。

**复用建议**：复用其实体/关系/观察 schema 作为最小 KG；或作为“无依赖 fallback”。不要作为主架构。

**适配分：1.5/5。**

---

### 3.10 Memory Bank MCP：文件型多项目记忆，靠“自定义指令”

**是什么**：把 Cline Memory Bank 的文件结构搬到 MCP；多项目目录隔离、文件读写、项目列表；MIT（[仓库](https://github.com/alioshr/memory-bank-mcp)）。

**值得借鉴**：

- 多项目目录与路径穿越防护。
- 文件结构（projectbrief / productContext / activeContext / systemPatterns / techContext / progress）适合做“项目上下文包”。

**缺口**：

- README 要求用户把一大段 custom instructions 粘贴到客户端；没有服务端强制。
- 没有同步、抽取、治理、身份。

**复用建议**：借鉴“项目上下文文件结构”，不要复用其交互模式。

**适配分：1.5/5。**

---

### 3.11 其他值得知道的项目

| 项目 | 定位 | 许可 | 与你的关系 |
|---|---|---|---|
| **ByteRover（原 Cipher）** | 上下文树 + 类 Git 版本控制 + 云同步 + MCP，22+ agent | Elastic 2.0 | 交互参考；许可不适合直接复用代码 |
| **Supermemory** | 托管记忆 API，核心闭源 | 商业 | 竞品参考；版本链/关系设计可看 |
| **MemU** | 主动式 agent 记忆框架 | 开源 | 可看其记忆分层；非 MCP 身份层 |
| **LangMem / LangGraph Store** | 记忆 SDK / 长期存储 | MIT/Apache | 适合做后端库，不是跨 agent 协议 |
| **OpenMemory（Mem0）** | 跨 harness 会话搬运 CLI | MIT | 首次迁移工具；不是常驻服务 |

---

## 4. 横向对比

图例：● 强 / ◐ 部分 / ○ 弱或无。

| 项目 | 身份/人格 | 启动协议 | 跨设备 | 项目隔离 | 治理/来源 | 文档 | 技能 | 许可 | 适配分 |
|---|---|---|---|---|---|---|---|---|---|
| mcp-memory-service | ◐ | ◐（工具） | ●（Hybrid） | ○ | ◐（beliefs） | ◐ | ○ | Apache-2.0 | **4** |
| Letta / MemFS | ● | ●（root 每轮） | ●（云） | ◐ | ◐ | ◐ | ● | Apache-2.0 | 3.5（锁 runtime） |
| Memobase | ◐（用户画像） | ○ | ◐（中心） | ● | ○ | ◐ | ○ | Apache-2.0 | 4（抽取层） |
| Mem0 / OpenMemory | ○ | ○ | ◐ | ◐ | ◐ | ○ | ○ | Apache-2.0 / MIT | 3 |
| MemOS 2.0 | ◐ | ◐（插件 recall） | ○（本地） | ◐（Cube） | ◐ | ◐ | ● | Apache-2.0 | 3 |
| Basic Memory | ◐ | ◐（插件） | ●（云付费） | ● | ○ | ● | ○ | AGPL-3.0 | 3 |
| Graphiti | ○ | ○ | ◐ | ●（group_id） | ●（双时序） | ◐ | ○ | Apache-2.0 | 2.5 |
| Cognee | ○ | ○ | ◐ | ●（dataset） | ○ | ◐ | ○ | Apache-2.0 | 2.5 |
| 官方 MCP memory | ○ | ◐（提示词） | ○ | ○ | ○ | ○ | ○ | MIT | 1.5 |
| Memory Bank MCP | ○ | ○（自定义指令） | ◐（文件同步） | ● | ○ | ●（文件） | ○ | MIT | 1.5 |

**读表结论**：

- **身份 + 启动**：Letta 最强，但锁 runtime；mcp-memory-service 有 bootstrap profile，但不是强制、不隔离项目。
- **项目隔离 + 治理**：Graphiti 最强，但重；Memobase 有项目隔离，没治理。
- **文档**：Basic Memory 最强，但 AGPL。
- **技能**：MemOS 最强，但插件化、工程重。
- **没有一行同时满足**：身份 + 强制启动 + 跨设备 + 项目隔离 + 治理 + 文档 + 技能。

---

## 5. MCP 协议层：能做到什么，不能做到什么

### 5.1 `instructions` 是“可选提示”，不是强制

MCP `InitializeResult` 有可选字段：

```ts
instructions?: string;
```

规范描述（[schema.ts](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2025-11-25/schema.ts)）：

> “Instructions describing how to use the server and its features. This can be used by clients to improve the LLM's understanding... For example, this information MAY be added to the system prompt.”

关键词是 **MAY**：客户端可以不注入、可以只展示给用户、可以截断。**不能假设所有客户端都会把它放进模型上下文。**

### 5.2 工具描述是最可靠的“软引导”

工具 `description` 会进入模型可见的工具列表，通常比 `instructions` 更可靠。可以在 `agent_bootstrap` 的描述里写“每次新会话第一个调用”，在其他工具描述里写“需先 bootstrap”。

### 5.3 没有“服务端主动调用工具”的机制

MCP 的 `prompts` 是**用户触发**的模板，不是服务端自动执行；`sampling` 是服务端请求客户端调用模型，不是让服务端调用自己的工具；`elicitation` 是向用户要信息。**服务端无法在会话开始时主动执行 `agent_bootstrap`。**

所以“强制”只能靠：

1. `instructions` + 工具描述（软）；
2. **Bootstrap Gate**：未 bootstrap 时，记忆类工具返回 `BOOTSTRAP_REQUIRED` 错误（半硬）；
3. **CLI wrapper**：在启动客户端前先 bootstrap 并注入（硬，但需要用户用 CLI 启动）；
4. 客户端 Hook（硬，但需要适配每个客户端）。

### 5.4 `roots` 可用于项目识别，但支持不一

MCP `roots` 让客户端把工作区根目录告诉服务端（[规范](https://modelcontextprotocol.io/specification/2025-11-25/client/roots)）。Cursor 明确声明支持 Roots（[文档](https://cursor.com/docs/mcp)）；Claude Code 的文档没有承诺 `instructions` 注入，Roots 支持也需要实测。

**建议**：`roots` > `agent_bootstrap(project_path)` > 当前目录推断；都没有时只加载全局身份，不猜项目。

### 5.5 对产品的直接含义

| 能力 | 可行性 | 说明 |
|---|---|---|
| 服务端声明“先 bootstrap” | ✅ | `instructions` + 工具描述 |
| 未 bootstrap 拒绝记忆工具 | ✅ | 返回结构化错误；模型会补调 |
| 未 bootstrap 拒绝“说话” | ❌ | 服务端管不到模型是否直接回答 |
| 自动识别项目 | ◐ | 取决于客户端是否给 roots；需 fallback |
| 跨设备同一身份 | ✅ | 服务端状态 + 设备 token |
| 完全零客户端适配 | ◐ | 支持 instructions 的客户端体验最好；其余靠 gate/CLI |

---

## 6. 复用矩阵

| 能力 | 推荐来源 | 许可 | 复用方式 | 价值 | 风险 |
|---|---|---|---|---|---|
| MCP server 框架 | FastMCP / 官方 SDK | MIT/Apache | 直接依赖 | 高 | 版本演进 |
| 存储 + 向量 + 全文 | PostgreSQL + pgvector + tsvector | PostgreSQL | 直接依赖 | 高 | 10 万级需调参 |
| 本地 embedding | FastEmbed / ONNX（bge-m3） | Apache/MIT | 直接依赖 | 高 | CPU 延迟 |
| 用户画像抽取 | Memobase | Apache-2.0 | 借鉴 schema/提示词；可参考代码 | 高 | 无 provenance/trust |
| Bootstrap Profile | mcp-memory-service | Apache-2.0 | 借鉴结构/算法；可复制小段代码并署名 | 高 | 不隔离项目、非强制 |
| 时序事实/来源 | Graphiti | Apache-2.0 | P2 适配为 `GraphStore` | 中 | 图库运维重 |
| Markdown 文档格式 | Basic Memory | AGPL-3.0 | **只借鉴格式，不复制代码** | 中 | 许可传染 |
| Root 身份 + lazy 目录 | Letta MemFS | Apache-2.0 | 借鉴信息架构 | 高 | 不能直接用 runtime |
| L1/L2/L3 + Skill | MemOS | Apache-2.0 | 借鉴分层与晋升 | 中 | 工程重 |
| 会话导入 | OpenMemory | MIT | 直接 CLI/适配器 | 中 | 一次性搬运 |
| 最小 KG | 官方 MCP memory | MIT | 直接依赖/参考 | 低 | 太基础 |
| 项目上下文文件 | Memory Bank MCP | MIT | 借鉴结构 | 低 | 依赖自定义指令 |

**不要直接复用**：

- **Basic Memory 代码**：AGPL-3.0；若你的项目不是 AGPL，复制会传染。可作为独立进程/外部 MCP 使用。
- **ByteRover 代码**：Elastic 2.0；只借鉴交互。
- **Graphiti 参考 MCP server**：官方明确无鉴权/无注入防护，不能直接暴露公网。
- **Letta 作为底座**：会失去“任意 MCP 客户端”的定位。
- **MemOS 全量自部署**：Neo4j + Qdrant + 多适配器，课程周期不够。

---

## 7. 推荐架构：一个 MCP 表面，可插拔后端

### 7.1 产品形态

```text
任意 MCP 客户端（Claude Code / Cursor / DSH / ...）
        │
        ▼
┌─────────────────────────────────────┐
│ agent-sync MCP（你自研的薄协议层）  │
│                                     │
│  agent_bootstrap  ← 强制入口        │
│  agent_whoami                       │
│  memory_search / memory_write       │
│  doc_search / doc_read              │
│  skill_search / skill_get           │
│  project_context                    │
│  agent_feedback                     │
│                                     │
│  Identity Pack + Bootstrap Gate     │
│  context_version + 项目作用域       │
└──────────────┬──────────────────────┘
               │
     ┌─────────┼──────────┬───────────┐
     ▼         ▼          ▼           ▼
 Postgres   Git 文档   抽取 Worker   可选适配器
 +pgvector   (Markdown)  (Memobase   (Mem0 / Graphiti
                        风格/自研)    / MemOS)
```

**设计要点**：

- **一个 MCP 表面**：用户只配一个 server；内部后端可替换。
- **Bootstrap 是唯一入口**：`agent_bootstrap` 返回 Identity Pack + 项目上下文 + 索引 + `context_version`；其他工具校验 context。
- **身份与记忆分离**：Identity 是 always-on 的小包；记忆/文档/技能按需检索。
- **项目作用域**：`roots` 或显式 `project_path`；全局身份 + 项目覆盖。
- **写回治理**：记忆写入先候选，全局晋升需确认；技能晋升需验证。
- **可插拔**：`Extractor` / `Embedder` / `GraphStore` / `DocStore` 接口；默认自研实现，P2 接 Mem0/Graphiti。

### 7.2 Bootstrap 协议（建议）

`agent_bootstrap` 返回示例：

```json
{
  "agent_id": "agent_enoch",
  "identity": {
    "persona": "猫娘",
    "style": {"tone": "可爱", "sentence_end": "喵", "address_user": "主人"},
    "boundaries": ["不违反安全规则", "不假装是人类"]
  },
  "global_directives": ["提交信息用中文", "先结论后细节"],
  "project": {
    "id": "proj_itime",
    "source": "roots",
    "conventions": ["Python + Vue", "测试必须真实执行"],
    "recent_decisions": ["ADR-014 单机 Compose"]
  },
  "context_version": "ctx_01J...",
  "available": {"memories": 128, "docs": 34, "skills": 7}
}
```

**Gate 规则**：

- `agent_bootstrap` 之外的记忆/文档/技能工具，必须带有效 `context_version`。
- 缺失 → `BOOTSTRAP_REQUIRED`；过期 → `CONTEXT_STALE`（附 diff 或提示重新 bootstrap）。
- 身份/项目上下文变更后，旧 `context_version` 失效；下一次工具调用触发刷新。
- 写工具额外要求 `trust` 与权限；全局身份修改只能由用户确认。

### 7.3 四条实现路线

| 路线 | 做法 | 工期 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|---|
| **A. Fork mcp-memory-service** | 以它为后端，加 Identity Pack、Gate、项目隔离、context_version | 2–3 周出 demo | 最快；bootstrap/sync/hooks 现成；Apache-2.0 | 代码面广；课程创新被稀释；上游单人维护 | 时间紧的 fallback |
| **B. 自研薄 MCP + 复用组件** | FastMCP + Postgres/pgvector + Memobase 风格抽取 + 自研 Gate/Identity/Sync | 6–8 周 | 可控、创新点清晰、易评测 | 工作量大；抽取/同步要自己调 | **推荐** |
| **C. 基于 Letta** | 用 Letta 的 MemFS/agent 作为身份与记忆 | 1–2 周 | 身份/记忆/技能最完整 | 锁 runtime；不满足“任意 MCP 客户端”；课程变成学 Letta | 不推荐 |
| **D. 网关包 Mem0/Graphiti** | `agent-sync` MCP 调现有记忆服务 | 3–4 周 | 复用成熟后端 | 两套服务；schema 映射；gate 跨服务难；延迟 | 中期可选 |

**推荐 B**，理由：

1. 你的核心创新是 **Bootstrap + Identity + Scope + Governance**，不是抽取算法；薄层更容易讲清楚。
2. 课程评测需要可控变量；自研层可以精确做消融。
3. 复用 Memobase 的 schema/提示词与 mcp-memory-service 的 bootstrap 设计，已经省掉最费时的“从零想 schema”。
4. 如果 W4 还没跑通闭环，再切 A。

---

## 8. 需要自研 vs 可以复用

| 模块 | 自研 | 复用/借鉴 | 说明 |
|---|---|---|---|
| Identity Pack | ✅ | Letta root files、Memobase profile | persona/style/boundaries + 版本 |
| Bootstrap Gate | ✅ | mcp-memory-service bootstrap 概念 | 无现成 gate |
| context_version | ✅ | — | 过期检测 |
| 项目作用域 | ✅ | Cursor roots、Memory Bank 目录 | roots > path > cwd |
| 记忆模型 | ◐ | Memobase/Mem0/Graphiti | 类型 + 来源 + 时效 |
| 抽取流水线 | ◐ | Memobase 3-call、Mem0 | 先复用提示词/schema |
| 存储/检索 | ◐ | Postgres/pgvector | 自研排序/打包 |
| 同步 | ✅ | mcp-memory-service hybrid 参考 | op log + 游标 |
| 文档 | ◐ | Basic Memory 格式、Git | 自己实现，避 AGPL |
| 技能晋升 | ◐ | MemOS L1/L2/L3 | 最小实现 |
| 评测 | ✅ | LoCoMo/LongMemEval | CrossProjectBench 自建 |

---

## 9. 课程落地：先做 6 个 spike

在写代码前，用 1–2 周做以下实验，直接决定 ADR-008/009/015：

| Spike | 问题 | 方法 | 通过标准 |
|---|---|---|---|
| S1 | `instructions` + 工具描述能否让模型先 bootstrap？ | Claude Code / Cursor / DSH 各 10 次新会话 | ≥7/10 首调用 bootstrap；否则上 gate/CLI |
| S2 | 客户端是否给 roots？ | 在 3 个客户端里打印 `roots/list` | 至少 1 个可用；记录 fallback |
| S3 | Bootstrap Gate 的体验 | 未 bootstrap 调 memory_search，观察模型是否补调 | ≥8/10 自动补调 |
| S4 | Memobase 抽取质量/成本 | 100 条 Episode，比较自研 prompt vs Memobase 风格 | 精确率 ≥0.8；成本可接受 |
| S5 | pgvector 延迟 | 5 万条，4C8G，混合检索 | p95 ≤400ms |
| S6 | 人格一致性 | “猫娘”注入，20 次盲评 | ≥95% 首答符合 |

**课程时间映射**：

- W1–W2：S1–S4 + 本报告；
- W3–W4：MCP 协议层 + Gate + Identity；
- W5–W6：抽取 + 检索；
- W7：同步；
- W8：文档；
- W9：技能；
- W10：控制台/安全；
- W11：评测；
- W12：演示/报告。

---

## 10. 风险与许可

| 风险 | 说明 | 对策 |
|---|---|---|
| 客户端不读 `instructions` | MCP 规范只说 MAY | Gate + CLI；不承诺 100% |
| MCP 规范演进 | 2025-11-25 仍在变 | 版本协商；适配层隔离 |
| AGPL 传染 | Basic Memory | 只借鉴格式；独立进程集成 |
| Elastic 许可 | ByteRover | 不复制代码 |
| 参考 server 不安全 | Graphiti MCP 无鉴权 | 不直接公网；自建鉴权 |
| 单人维护 | mcp-memory-service | 锁版本；可替换 |
| 抽取成本 | LLM 调用 | 批处理 + 小模型 + 缓存 |
| 范围蔓延 | 想复刻 MemOS | 锁定 MVP；P2 清单 |

---

## 11. 参考链接

1. mcp-memory-service — https://github.com/doobidoo/mcp-memory-service
2. Letta MemFS — https://docs.letta.com/concepts/memfs
3. Letta Code — https://github.com/letta-ai/letta-code
4. Memobase — https://github.com/memodb-io/memobase
5. Memobase 独立分析 — https://github.com/lhl/agentic-memory/blob/main/ANALYSIS-memobase.md
6. Mem0 — https://github.com/mem0ai/mem0 ；MCP — https://docs.mem0.ai/platform/mem0-mcp
7. OpenMemory — https://github.com/mem0ai/openmemory
8. MemOS — https://github.com/MemTensor/MemOS ；本地插件 — https://github.com/MemTensor/MemOS/tree/main/apps/memos-local-plugin
9. Basic Memory — https://github.com/basicmachines-co/basic-memory
10. Graphiti — https://github.com/getzep/graphiti ；MCP — https://github.com/getzep/graphiti/tree/main/mcp_server
11. Cognee — https://github.com/topoteretes/cognee ；MCP — https://github.com/topoteretes/cognee/tree/main/cognee-mcp
12. 官方 MCP Memory Server — https://github.com/modelcontextprotocol/servers/tree/main/src/memory
13. Memory Bank MCP — https://github.com/alioshr/memory-bank-mcp
14. MCP Lifecycle — https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
15. MCP Schema（`instructions` 字段）— https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2025-11-25/schema.ts
16. MCP Roots — https://modelcontextprotocol.io/specification/2025-11-25/client/roots
17. Cursor MCP（Roots 支持）— https://cursor.com/docs/mcp
18. Claude Code MCP — https://code.claude.com/docs/en/mcp

---

## 12. 一句话给答辩

> 现有开源项目已经把“记忆抽取、存储、文档、技能、同步”分别做得很成熟，但没有人把“**同一个 agent 在任意 MCP 客户端里被启动**”做成协议。忆桥的复用策略是：**用别人的零件，做自己的启动协议与治理层**。
