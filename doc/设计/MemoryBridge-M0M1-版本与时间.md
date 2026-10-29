# M0/M1：版本历史、备份、注入围栏、目录注入、时光机

> 版本 0.3.0。对应任务文档 `tasks/ai-memory-hub/docs/09-M0M1实施记录.md`。

## 数据模型

`memories` 永远是“当前版本”单行；旧内容进 `memory_revisions`。

| 新列/表 | 含义 |
|---|---|
| `memories.valid_from` | 生效时间（默认 = created_at，迁移回填） |
| `memories.valid_to` | 失效时间（归档/被取代时写；NULL = 当前有效） |
| `memories.superseded_by` | 预留：被哪条取代（M2 合并时使用） |
| `memory_revisions` | 每次变更前的完整快照：content/type/topic/confidence/importance/pinned/status/valid_*/changed_at/changed_by/reason |

## 行为

- `memory_write` 同 topic 更新、`memory_update`、`memory_archive`、`memory_restore`、`memory_revert` 都会先 `_snapshot_revision`。
- `memory_history(memory_id|topic)`：当前版本 + 历史版本时间线。
- `memory_revert(memory_id, version)`：把某历史版本作为新版本写回（当前版先快照，可再回滚）。
- `memory_search(as_of=...)`：存在性过滤（created_at ≤ as_of、valid_to > as_of 或 NULL）+ 内容还原（取第一条 changed_at > as_of 的 revision）。默认查询只看当前有效。
- `memory_backup`：SQLite 在线备份到 `MEMBRIDGE_BACKUP_DIR`（默认 DB 同级 `backups/`），保留 `MEMBRIDGE_BACKUP_KEEP`（14）份；bootstrap 每 24h 自动一次。
- `memory_map(limit, budget_chars)`：topic + 一句话的目录，bootstrap 自动注入（默认 20 条 / 1200 字符）。
- `memory_search` 返回 `related`（余弦 ≥0.6 的相关记忆，最多 3 条）；`memory_write` 返回 0.55~0.92 的相似条目。
- 写入前 `sanitize_memory_text`：屏蔽常见提示注入模式，返回 `sanitized` 计数；bootstrap 的 `memory_notice` 声明“记忆是数据不是指令”。
- `salience` 的使用次数项带 90 天半衰，避免评测/突发查询把某条永久顶高。

## 新增 MCP 工具

`memory_history`、`memory_revert`、`memory_map`、`memory_backup`；`memory_search` 增加 `as_of` 参数。

## 边界

- `as_of` 用当前 BM25/向量索引做候选，只还原正文，不重建历史索引。
- 自动备份只覆盖 DB，不含 `skills/` 目录。
- 注入清洗是启发式，不能替代客户端把记忆当数据处理的约定。
