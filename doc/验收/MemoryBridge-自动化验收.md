# AI 直接写入 / 隔离 Claude/Codex 验收记录

日期：2026-10-21  
环境：Windows，Python 3.13，本机 Hub `127.0.0.1:8788`（无 token 本地模式）  
隔离客户端：Codex CLI 0.163.0，独立 `CODEX_HOME`（`tasks/ai-memory-hub/tmp/codex-home`）  
LLM：用户提供的 OpenAI 兼容端点，仅作为 **Codex 客户端模型**（不是服务端抽取器）；模型 `deepseek-v4.1-flash`，API key 未写入仓库。

## 1. 设计结论

- 不再有候选队列/人工批准；`memory_write` 直接写入 active。
- 不再有 `.membridge/inbox` / `transcript` / `watch`；AI 在用户提出持久要求时主动调用 MCP。
- UI 只读：概览、记忆、文档、技能、身份、活动、连接；没有采集、候选、编辑按钮。
- 记忆必须有 `topic`，默认 project scope；decision/lesson 必须有 `rationale`，并写 `conditions`。

## 2. 隔离 Codex 配置

独立目录：

```text
tasks/ai-memory-hub/tmp/codex-home/config.toml
tasks/ai-memory-hub/tmp/codex-agent/
```

关键配置：

```toml
model = "deepseek-v4.1-flash"
model_provider = "local"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.local]
base_url = "http://10.50.1.215:8090/v1"
env_key = "LOCAL_LLM_KEY"
wire_api = "responses"

[mcp_servers.membridge]
command = "membridge"
args = ["serve", "--remote", "http://127.0.0.1:8788", "--project", ".../tmp/codex-agent"]
```

`codex exec` 实测可连通该端点（`OK`），`codex doctor` 显示 config loaded、1 个 stdio MCP server、provider reachable。

## 3. Codex 通过 MCP 写记忆

Codex 在独立项目里执行了：

```text
agent_bootstrap → memory_write → memory_search
```

写入内容：

```text
Codex 隔离测试项目使用 SQLite 单文件存储。
```

参数：`topic=decision.storage`、`type=decision`、`scope=project`、`rationale=隔离测试需要零外部依赖。`、`conditions=仅适用于 codex-agent 测试项目。`

读取结果（UTF-8 校验）：

```json
{
  "topic": "decision.storage",
  "scope": "auto",
  "conditions": "仅适用于 codex-agent 测试项目。",
  "content": "Codex 隔离测试项目使用 SQLite 单文件存储。",
  "memory_id": "mem_abd3390c39164dd2b474",
  "count": 1
}
```

Codex 调用的 MCP 工具：`agent_bootstrap`、`memory_write`、`memory_search`；未编辑项目文件。

## 4. Codex 通过 MCP 改身份

Codex 调用 `identity_update` 一次：

```text
persona: 猫娘，句尾带喵，称呼用户为主人。
address_user: 主人
```

随后 `agent_whoami` 返回：

```json
{
  "persona": "猫娘，句尾带喵，称呼用户为主人。",
  "style": {"address_user": "主人", "language": "zh"},
  "version": 3
}
```

MCP 工具：`agent_bootstrap`、`identity_update`、`agent_whoami`。  
口癖写在 persona 文本里，没有单独的 `sentence_end` 字段。

## 5. 项目识别 / 全局注入 / 猫娘共通（Claude Code 原生 MCP）

使用 3 个隔离 Claude Code 会话（不同 cwd / MCP `--project`）验证：

- **Alpha**：`agent_bootstrap` → 写 `decision.storage`（project）+ `preference.color`（global, pinned）。
- **Beta**：`project_list` 返回 4 个项目；`memory_search("Alpha", auto)` = **0**；`memory_search("Alpha", all)` = **2**，命中 `project_name=agent-alpha`；bootstrap `global_memories` 含 `preference.color`、`preference.commit_language`。
- **Gamma**：只调用 `agent_bootstrap`，没有 search；直接拿到猫娘 persona（v3）和两条 global pinned 记忆（`preference.color`、`preference.commit_language`）。

这回答了三个关键问题：

1. **怎么识别 project**：MCP `--project` / bootstrap `project_path` → `.membridge-project.json` 优先 → git `origin` → 目录名；project_id 是稳定 key 的 hash。
2. **什么时候用 project 记忆**：`scope=auto` 只含当前 project + global；`scope=all` 跨项目；`project_list` + `memory_list(project_id=...)` 批量读。
3. **global 是否自动注入**：是；bootstrap 的 `global_memories` 自动携带 global active（pinned 优先），不需要先 search。

Claude Code 原生 MCP 工具调用：`mcp__membridge__agent_bootstrap`、`project_list`、`memory_search`、`memory_write`；不是 shell 模拟。

> 说明：Codex CLI 0.163 的 `codex exec` 存在已知问题：MCP server 已加载但工具不暴露给模型（[openai/codex#17904](https://github.com/openai/codex/issues/17904)）。因此 Codex 侧用手动 MCP 客户端验证协议；原生工具可用性用 Claude Code 验证。

## 6. 文件夹技能

`seed` 后 Hub 的 `data/skills/` 下有：

```text
skill_.../SKILL.md
skill_.../scripts/check.ps1
skill_.../references/notes.md
skill_.../scripts/smoke.ps1
```

`skill_list` 返回 `file_count=3` / `2`；`skill_get` 返回文件列表与 `SKILL.md`；`skill_file` 可读脚本。测试覆盖 `../evil.sh` 路径拒绝。

## 7. 测试与 UI

- `python -X utf8 -m unittest discover -s tests -v` → **31 tests OK**（含默认 admin 强制改密、会话、API token、静态 token 兼容）
- Headless Edge 渲染 `http://127.0.0.1:8788/`、`/#memory`、`/#identity`：只读页面正常；记忆显示 project/global、topic、rationale、conditions；身份页显示 persona 中的口癖。
- 截图：`doc/assets/webui.png`。
- Hub `/health` 200；服务端不配置 LLM。

## 8. 公网鉴权测试

- 默认 `admin/admin` 登录 → 200，`must_change_password=true`。
- 未改密直接 `/rpc` → **403 PASSWORD_CHANGE_REQUIRED**。
- `POST /password` 改密后 → 200，新会话 Cookie；旧密码登录 → **401**。
- `POST /tokens` 创建 API token → `/rpc` 用 Bearer token → **200**；token 只返回一次。
- 静态 `MEMBRIDGE_TOKEN` 仍可作服务 token（兼容测试通过）。
- 带 `X-Forwarded-Proto: https` 时，Cookie 带 `Secure`，并返回 HSTS。
- 登录失败限速：同 IP 5 分钟 5 次失败后 429。
- `compose.public.yaml`：只发布 Caddy 80/443，Hub 仅内部 `hub:8787`；`docker compose config` 通过。Docker 镜像构建仍受本机 Docker Hub 网络限制。
- 公网 MCP 仍需 **一个入站 443 端口**；MCP 本地 proxy 是出站连接，不需要第二个端口。

## 9. 未完成

- Docker 镜像构建因本机 Docker Hub 网络不可达未验证。
- 没有向量检索、op log/字段级冲突、TLS/账号体系。
- 技能晋升仍靠工具，没有单独 UI；UI 按设计只读。
