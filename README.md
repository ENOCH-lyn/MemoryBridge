# 忆桥 MemoryBridge

忆桥是面向个人开发者 / 小团队的自部署 AI 记忆与文档中枢。它把不同电脑、不同 agent、不同项目的经历汇聚成一份**有出处、会过期、可冲突合并、能沉淀经验**的记忆，让换机、换项目不必重新调教。

> 当前阶段：**立项与文档**。仓库暂不含可运行代码；先对齐问题、边界、架构与评测，再进入实现。

## 解决什么问题

| 断点 | 现象 |
|---|---|
| 设备 | 公司台式机 / 家里笔记本 / 服务器各一份记忆，换机即失忆 |
| 工具 | `CLAUDE.md`、`AGENTS.md`、Cursor rules、ChatGPT memory 互不相通 |
| 项目 | 项目 A 的决策与踩坑，项目 B 不会自动知道 |
| 文档 | ADR / SOP / 笔记散落，agent 检索不到或检索到过期版本 |

现有方案各有强项：Mem0 的抽取流水线、MemOS 的记忆分层、Letta 的有状态 agent、Graphiti 的时序知识图谱、basic-memory 的 Markdown + MCP、ByteRover 的版本化上下文。但“自部署 + 跨设备同步 + 治理 + 经验晋升 + 文档一体”仍缺少开箱即用的组合，这就是忆桥的切入点。

## 产品范围

- **自部署优先**：Docker Compose 单机可跑，数据可带走，无强制云依赖。
- **跨设备一等公民**：离线可写、增量同步、冲突可解释。
- **跨 agent 接入**：MCP 优先，CLI / Hook 补充；不绑定单一 agent。
- **治理优先**：来源、置信度、时效、敏感级、信任级、审计；记忆可编辑、可删除。
- **经验闭环**：任务轨迹 → 候选经验 → 验证 → 晋升为跨项目 Skill → 退役。
- **文档即记忆**：Markdown + Git，文档既给人看，也能被 agent 引用到章节。

**当前非目标**：多租户 SaaS、实时协作编辑、端到端加密、移动端 App、模型微调。这些不砍，课程周期内做不完。

## 文档入口

1. [产品需求文档](doc/需求/MemoryBridge-产品需求文档.md)：定位、用户、功能需求、非功能、数据模型、API/MCP、同步、安全、评测与实施计划。
2. [调研与可行性](doc/调研/MemoryBridge-调研与可行性.md)：竞品矩阵、学术脉络、可行性评分、Go/No-Go 门槛。
3. [开源复用调研](doc/调研/MemoryBridge-开源复用调研.md)：开源项目评估、MCP 协议约束、复用矩阵、三条实现路线与 spike 计划。
4. [架构概览](doc/架构/MemoryBridge-架构概览.md)：组件、数据流、部署与待定项。
5. [ADR 架构决策](doc/架构/MemoryBridge-ADR架构决策文档.md)：17 条决策的背景、备选、代价、验证与复审条件。
6. [12 周实施计划](doc/计划/MemoryBridge-12周实施计划.md)：里程碑、门槛、分工与预算。
7. [文档索引](doc/README.md)：全部文档与后续拆分计划。
8. [AI 协作规范](AGENTS.md)：事实来源、文档职责、Git 与验证约束。

## 仓库结构

```text
.
├─ README.md
├─ AGENTS.md
├─ doc/
│  ├─ README.md
│  ├─ 需求/MemoryBridge-产品需求文档.md
│  ├─ 调研/MemoryBridge-调研与可行性.md
│  ├─ 架构/MemoryBridge-架构概览.md
│  ├─ 架构/MemoryBridge-ADR架构决策文档.md
│  └─ 计划/MemoryBridge-12周实施计划.md
└─ .gitignore
```

后续实现阶段预计增加 `server/`、`web/`、`mcp/`、`deploy/`、`bench/`，以实际 ADR 为准。

## 当前状态

- [x] 问题定义与竞品调研
- [x] 可行性判断与 Go/No-Go 门槛
- [x] PRD v1.0
- [x] 架构概览与 12 周计划
- [x] ADR 架构决策文档（17 条）
- [x] 开源复用调研（10 个项目 + 3 条路线）
- [ ] 技术选型验证（MCP + Postgres/pgvector 最小闭环）
- [ ] 记忆抽取与检索原型
- [ ] 跨设备同步与冲突测试
- [ ] CrossProjectBench 与实验

## 本地开发

暂无可运行代码。文档阶段只需 Markdown 阅读器；后续实现会在此补充环境要求、启动命令与验证方式。

## 许可

待定。当前建议 Apache-2.0，需与课程要求确认后写入 `LICENSE`。
