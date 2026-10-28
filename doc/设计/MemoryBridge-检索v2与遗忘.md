# MemoryBridge 0.2.0 · 混合检索与遗忘机制（实现说明）

> 状态：已实现并通过 `tests/`（45 个用例）；线上数据评测见任务文档 `docs/06`。

## 1. 模块划分

| 文件 | 职责 |
|---|---|
| `membridge/retrieval.py` | 分词、FTS5/余弦检索、RRF、salience/decay、嵌入器（hash / http / model2vec） |
| `membridge/db.py` | `vectors` 表、`search_fts` 虚表、记忆生命周期列与迁移 |
| `membridge/service.py` | `memory_search` / `doc_search` 混合排序、写入去重、归档/恢复、`memory_maintain`、按预算 bootstrap |
| `membridge/mcp_server.py` | 新工具 `memory_archive` / `memory_restore` / `memory_maintain` / `memory_stats`，`memory_write.force/importance` |
| `tools/eval_retrieval.py` | 离线 A/B：旧 `_score` vs 新混合检索，18 条标注查询 |

## 2. 检索公式

```
lex_norm = 1 - exp(-bm25 / 6)
cos      = cosine(query_vec, memory_vec)        # 仅当门限通过：cos ≥ 0.25 且 ≥ 0.75×max
salience = 0.34·freshness + 0.18·usage + 0.20·type + 0.16·importance + 0.12·confidence
final    = (lex_norm + cos) × (0.75 + 0.25·salience) × pinned?1.1:1
           + 0.05·(rrf / max_rrf) + 0.004·exact_substring
```

- 词法命中永远参与；向量命中只在“语义嵌入器”启用时参与（hash 模式下余弦与 BM25 同源，会重复计分，故置零）。
- 归一化用饱和函数而非 max 归一化：弱 BM25 冠军不能压过强语义命中。

## 3. 遗忘

```
decay = exp(-ln2·days/half_life) × type_weight × (0.5+0.5·imp) × (0.5+0.5·conf) × (1+0.4·ln(1+hits))
half_life = 45d (fact/reference) | 120d (其它)
归档条件：decay < 0.18 且 days > 45 且 !pinned 且 type ∉ {decision, lesson, preference}
```

归档只是 `status='archived'`，检索默认排除，`memory_restore` 可恢复；`memory_maintain` 支持 `dry_run`。
`bootstrap` 每 24h 自动跑一次（`MEMBRIDGE_AUTO_MAINTAIN=0` 关闭）。

## 4. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MEMBRIDGE_EMBED_PROVIDER` | `auto` | `auto` / `hash` / `http` / `model2vec` |
| `MEMBRIDGE_EMBED_MODEL` | 空 | http 模式为模型名；model2vec 模式为本地目录或 HF 名 |
| `MEMBRIDGE_EMBED_URL` / `MEMBRIDGE_EMBED_KEY` | 空 | OpenAI 兼容 `/embeddings` |
| `MEMBRIDGE_EMBED_DIM` | 空 | hash 维度（默认 512）或强制向量维度 |
| `MEMBRIDGE_BOOTSTRAP_CHAR_BUDGET` | `6000` | 单次 bootstrap 注入字符预算 |
| `MEMBRIDGE_BOOTSTRAP_MEMORY_LIMIT` | `12` | 单 scope 最多注入条数 |
| `MEMBRIDGE_AUTO_MAINTAIN` | `1` | 是否每日自动跑遗忘 |

`auto` 只在模型目录已存在时启用 model2vec，避免断网时触发 HF 下载卡住启动。

## 5. 兼容与回滚

- 新列通过 `ALTER TABLE` 增量添加，旧版本代码读新库不受影响。
- 索引缺失时自动回填（`ensure_index`），首次搜索可能稍慢。
- FTS5 不可用的 SQLite 构建会静默降级为纯向量/纯词法，服务不崩。
