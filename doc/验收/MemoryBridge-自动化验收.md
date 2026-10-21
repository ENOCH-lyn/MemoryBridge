# 自动化与文件夹技能验收记录

日期：2026-10-21  
环境：Windows，Python 3.13，本机 Hub `127.0.0.1:8788`（无 token 本地模式）  
证据：命令输出摘要；未保存 API key。

## 1. 自动化同步

对临时项目执行：

```powershell
membridge init-project --path .\tmp\watchproj
membridge sync --project-path .\tmp\watchproj --remote http://127.0.0.1:8788
membridge sync --project-path .\tmp\watchproj --remote http://127.0.0.1:8788
```

结果：

- 第一次：`docs=1 skills=1 inbox=1 candidates=1`
- 第二次：`docs=0 skills=0 inbox=0 skipped=3 candidates=0`
- 说明：按内容 hash 去重，重复运行不会重复抽取；docs、技能目录、inbox 都由同一个 `sync/watch` 推送。

## 2. 文件夹技能

`seed` 后 Hub 的 `data/skills/` 下有：

```text
skill_.../SKILL.md
skill_.../scripts/check.ps1
skill_.../references/notes.md
skill_.../scripts/smoke.ps1
```

`skill_list` 返回 `file_count=3`（pytest 技能）和 `file_count=2`（MCP 技能）；`skill_get` 返回文件列表与 `SKILL.md`；`skill_file` 可读取 `scripts/check.ps1`。测试还覆盖 `../evil.sh` 路径拒绝。

## 3. 真实 LLM 抽取

通过本地 Hub 的 `memory_ingest` 调用真实 OpenAI 兼容端点（模型 `deepseek-v4.1-flash`）：

输入：

```text
以后都用中文提交信息。我们决定用 SQLite 而不是 Postgres。Windows 路径含空格时 pytest 会失败，应该用 pathlib。
```

返回：

```json
{"extractor": "llm", "llm_error": null,
 "candidates": [
   {"type": "preference", "content": "以后提交信息（commit message）都用中文写。", "confidence": 0.95},
   {"type": "decision", "content": "项目数据库选用 SQLite，而不是 Postgres。", "confidence": 0.95},
   {"type": "lesson", "content": "Windows 上路径含空格时 pytest 会失败，应使用 pathlib 处理路径。", "confidence": 0.9}
 ]}
```

## 4. 测试与 UI

- `python -X utf8 -m unittest discover -s tests -v` → **34 tests OK**
- Headless Edge 渲染 `http://127.0.0.1:8788/`、`/#capture`、`/#skills`，截图见 `doc/assets/webui.png`。
- Hub `/health` 200；`status` 显示 `llm.enabled=true`，model `deepseek-v4.1-flash`。

## 5. 未完成

- Docker 镜像构建因本机 Docker Hub 网络不可达未验证。
- watch 是轮询，不处理删除/重命名，也不是 OS 级对话 hook。
- 没有向量检索、op log/字段级冲突、技能晋升审核 UI。
