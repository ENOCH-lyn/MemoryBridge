# MemoryBridge 部署前审计

日期：2026-10-22  
审计对象：`main` @ `fdf9de4`  
范围：安全、功能、易用性、UI、部署与运维  
环境：Windows 本地 Hub；隔离 DB + Playwright；Docker Hub 不可达

## 结论

**单用户、私有/小团队内网或带 HTTPS 的公网 staging：有条件 Go。**  
**直接作为公网多用户服务：No-Go。** 当前没有多用户/角色、2FA、token 过期、自动化备份，也没有在本机完成 Caddy TLS 与 Docker 镜像的实际启动验证。

## 发现与处理

| 级别 | 发现 | 影响 | 本轮处理 / 建议 |
|---|---|---|---|
| 高 | 登录页直接写默认 `admin/admin` | 公网部署会主动提示默认凭据 | 已移除登录页提示；默认凭据只留 README |
| 高 | 默认 admin 首次登录前存在抢占窗口 | 攻击者可先登录并改密 | 新增可选 `MEMBRIDGE_ADMIN_PASSWORD` 初始密码；强制首登改密；部署时先限制网络或设置初始密码 |
| 高 | 真实 Caddy TLS / 域名未执行 | 不能宣称公网 HTTPS 已验证 | `compose.public.yaml`/Caddyfile 已通过 `docker compose config`；必须在目标域名上实测证书与跳转 |
| 高 | Docker 镜像未构建 | 不能宣称镜像可启动 | 本机 Docker Hub 网络不可达；目标机执行 `docker compose -f compose.public.yaml up -d --build` 后验收 |
| 高 | 无自动备份/恢复演练 | SQLite 或 volume 损坏会丢全部记忆 | README 增加停服备份 DB + `data/skills/`、`/export` JSON；生产需 cron + 恢复演练 |
| 中 | 单 admin、无角色、无 2FA | 公网账号被撞库后权限过大 | 课程范围保持单用户；公网多用户前必须加角色/2FA/审计告警 |
| 中 | API token 无过期时间 | 泄露后长期可用 | token 只存 hash、可撤销；公网建议定期轮换；后续加 expires_at |
| 中 | 静态 `MEMBRIDGE_TOKEN` 可绕过账号强制改密 | 配了静态 token 就弱化账号策略 | 公网不设置；只使用 API token；文档已标注 |
| 中 | 登录限速只做在应用层，无账户锁定/边缘限速 | 暴力尝试/资源消耗 | 5 分钟 5 次失败；失败表已加 4096 上限清理；Caddy 可再加边缘限速 |
| 中 | CSRF 依赖 SameSite=Lax，没有 token | 极端浏览器/旧客户端风险 | 同源 JSON + HttpOnly Cookie；后续可加 Origin 校验 |
| 中 | CSP 允许 `unsafe-inline`（单文件 UI 主脚本） | XSS 纵深不足 | 所有用户内容均 `esc`/`textContent`，CodeMirror 只处理文本；后续可把主脚本拆到静态文件并去 inline |
| 中 | 只有 SQLite 单写，无 op log/冲突合并 | 多设备并发写最后写入生效 | 课程范围可接受；多实例/协作前升级 |
| 中 | 代理 XFF 信任边界 | 限速可能被伪造头绕过 | 改为优先 `X-Real-IP`、否则取 XFF 最后一项；Caddy 显式覆盖这两个头 |
| 中 | 审计日志无分页/保留策略 | 长期增长 | UI 限 100 条；后续加归档/保留策略 |
| 低 | 编辑弹窗无未保存提示 | 误关会丢当前编辑 | 已用显式“保存文件/保存”按钮；后续加 dirty 提示 |
| 低 | 项目只能自动发现，不能删除 | 误建项目会留着 | 项目是上下文容器；删除会级联记忆，暂不开放，避免误删 |

## 已验证的控制

- 密码：PBKDF2-HMAC-SHA256 260k + 随机 salt；首登 `PASSWORD_CHANGE_REQUIRED`。
- 会话：HttpOnly + SameSite=Lax + 7 天；HTTPS 下 Secure；改密使旧会话全部失效。
- MCP：API token 只显示一次，DB 只存 SHA-256；`membridge serve --remote --token` 端到端通过。
- 登录限速：同 IP 5 分钟 5 次失败 → 429；失败表有上限。
- 安全头：CSP、X-Frame-Options、nosniff、Referrer-Policy、HSTS（HTTPS）。
- 路径：技能文件 `_safe_skill_path`；静态文件 `resolve` + `relative_to`；测试覆盖 `../evil.sh`。
- Docker：非 root 用户 `membridge:10001`，只 `EXPOSE 8787`；公网 compose 不发布 8787。
- 依赖：Python 标准库；`cryptography`/CodeMirror 无外部服务；CodeMirror 为本地 MIT 静态资源。
- 秘密扫描：tracked files 未发现真实 API key/token；`.env` 被忽略。
- 测试：33 tests OK；Playwright 覆盖登录/改密、记忆/文档新建、技能树编辑、身份保存、新建技能、改密码。
- 移动端：390px 视口 `scrollWidth=390`，无横向溢出；底部导航固定，长页面可滚动。

## 公网部署清单（必须逐项）

1. 域名 A/AAAA 指向服务器；只开放 443（80 仅 ACME/跳转），不要发布 8787。
2. `MEMBRIDGE_ADMIN_PASSWORD` 设置为强初始密码；首次登录立即改密；或首次部署时限制来源 IP。
3. `DOMAIN` 正确；`docker compose -f compose.public.yaml config` 通过后再启动。
4. 验证 `https://域名/` 证书、HSTS、登录、强制改密、API token、MCP bootstrap。
5. 配置 `data` volume 的备份与恢复演练。
6. 不在公网设置静态 `MEMBRIDGE_TOKEN`；使用 API token 并按设备命名。
7. 运行 `docker compose logs`、`/health`、登录失败限速检查。

## 未验证 / 后续

- 真实域名 TLS、Let's Encrypt 证书续期、Caddy 启动。
- Docker 镜像在目标机构建/运行。
- 多用户、2FA、API token 过期、边缘限速、自动化备份、SQLite 在线备份。
- 公网多实例/高并发。
