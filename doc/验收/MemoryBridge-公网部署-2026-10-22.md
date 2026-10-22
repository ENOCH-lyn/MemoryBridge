# MemoryBridge 公网部署记录

日期：2026-10-22  
域名：`https://mcp.enoch.host`  
服务器：`191.40.46.74`（SSH `3528`，用户 `root`）

## 架构

```text
Cloudflare (TLS, proxy)
        │  443/80
        ▼
OpenResty 1.27.1.2 (1Panel 容器，host network)
        │  http://127.0.0.1:8787
        ▼
membridge.service (systemd, 用户 membridge)
        │
        ├─ /var/lib/membridge/membridge.db
        └─ /var/lib/membridge/skills/
```

- Hub 只监听 `127.0.0.1:8787`，公网只经 OpenResty。
- 没有动现有 `enoch.host` 站点、8080 Halo、8090 1Panel、MySQL。
- OpenResty 新增：`/opt/1panel/www/conf.d/mcp.enoch.host.conf`（仓库备份：`deploy/openresty-mcp.conf`）。
- 证书：`/opt/1panel/www/sites/mcp.enoch.host/ssl/`，当前为 **self-signed**；Cloudflare SSL/TLS 必须是 **Full**，不能是 Full (strict)。
- Cloudflare 会把 `CF-Connecting-IP` 传给 OpenResty，Hub 用 `X-Real-IP` 识别客户端；OpenResty 覆盖 XFF，避免伪造。
- 只开 443 使用 MCP；MCP 是本地 stdio proxy 出站连接，不需要第二个入站端口。

## 账号 / token（不写入仓库）

- 初始 admin 密码：服务器 `/root/membridge-admin-password`（root 600）。
- 初始 MCP API token：服务器 `/root/membridge-api-token`（root 600）。
- 首次登录必须改密；改密后旧会话失效，API token 不受影响。
- 建议登录后：修改密码 → 在「连接」页撤销 `initial-mcp`，按设备创建新 token。

## 服务管理

```bash
systemctl status membridge
systemctl restart membridge
journalctl -u membridge -n 50 --no-pager
```

环境文件：`/etc/membridge/env`（600），包含 admin 初始密码、Cookie Secure、trust proxy 等。

## 已验收

- `GET https://mcp.enoch.host/` → 200
- `HEAD https://mcp.enoch.host/` → 200
- `GET https://mcp.enoch.host/health` → `{"ok":true,...}`
- `POST /login` 初始密码 → 200，`must_change_password=true`
- 初始 API token + 本地 MCP proxy：
  - `initialize` 成功
  - `agent_bootstrap` `isError=false`
  - 返回猫娘 persona 与空项目 `public-smoke`
- 临时 token 测完已撤销；保留 `initial-mcp` 供首次使用。

## 已知边界

- origin 证书是 self-signed，Cloudflare 必须 Full；要 Full strict 需换 Cloudflare Origin CA 或 Let's Encrypt 证书。
- 没有配置自动备份；建议 cron 停服复制 DB + skills，或定期 `/export`。
- Cloudflare Browser Integrity Check 会拒绝默认 Python UA；MCP 本地 proxy 已带浏览器 UA。
- 1Panel/OpenResty 是宿主机既有环境，后续如果 1Panel 重写站点配置，需要保留 `mcp.enoch.host.conf`。
