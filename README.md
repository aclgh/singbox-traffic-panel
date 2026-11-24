# singbox-traffic-panel

保留现有纯净 `sing-box` 配置的**极轻量级流量监控与设备凭据后台**（专为新加坡 OCI Free Tier VPS 设计）。

## 核心特性

- **不破坏现有纯净 `sing-box`**：仅在原有配置上增加 `experimental.v2ray_api` 或 `experimental.clash_api` 本地回环接口，完全保留现有路由、TLS/REALITY、Hysteria2 设置。
- **双数据源采集（二选一 / 自动降级）**：
  - `v2ray_api`（推荐、最准确、不漏短连接）：纯 Python HTTP/2 gRPC (`h2`) 调用 `StatsService/QueryStats`，零 C++ `grpcio` 依赖，按用户 + 入站/出站精确计费。
  - `clash_api`（官方包默认自带、无需换二进制）：持久订阅 `ws://127.0.0.1:9090/connections`，按连接增量计算上行/下行流量并自动清理断开连接。
  - `auto` 模式：启动先探测 `v2ray_api`，不可达自动切换到 `clash_api`。
- **SQLite WAL 持久化（重启不丢数据）**：每 `10~30` 秒（默认 `15s`）将流量增量原子落盘，并在每次修改凭据重载 `sing-box` 前以及服务退出时立即强制落盘，支持 **今日 / 本月 / 累计** 汇总与分时/每日趋势图。
- **本月 OCI 10 TB 免费额度红线预警**：
  - 东京中转 VLESS 按 **2×** 计入、新加坡直连 Hysteria2 按 **1×** 计入（倍率与账期重置日均可在 `panel.yaml` 自由调整）。
  - 实时展示进度条、分协议拆解公式、日均速率与月底预计用量，70% 提醒、85% 红线告警。
- **在线一键新增 / 吊销 / 恢复设备凭据 + 离线二维码**：
  - 新增设备自动生成高强度随机 VLESS UUID 与 Hysteria2 密码，自动推导 REALITY `pbk` 公钥（也可手动指定）。
  - 自动同步维护 `experimental.v2ray_api.stats.users`。
  - 每次写入前**自动备份 `config.json` → 执行 `sing-box check` 校验 → 原子替换 → 平滑重载**，校验或重载失败自动回滚。
  - **100% 本地离线生成 SVG/PNG 二维码**（`segno`）与 `vless://` / `hysteria2://` 链接及 `sing-box` 客户端 JSON，前端零外部 CDN，凭据绝不离开本机。
- **默认仅监听 `127.0.0.1:8088`**：代码级强制阻止意外绑定公网端口，平时一条 SSH 隧道即可在本地浏览器安全访问，外网完全扫不到。

---

## 本地快速预览（Demo 模式）

无需连接真实 VPS，即可在本地完整体验仪表盘、OCI 10 TB 进度条、新增/吊销设备及离线二维码生成：

```bash
# 方式 A：使用 uv
uv sync
uv run python -m singbox_panel --demo

# 方式 B：使用标准 venv + pip
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m singbox_panel --demo
```

随后在本地浏览器打开：`http://127.0.0.1:8088`

---

## 新加坡 VPS 生产部署

### 1. 检查现有 `sing-box` 编译标签

```bash
bash deploy/check-singbox-tags.sh /usr/bin/sing-box
```

- 如果输出包含 `with_v2ray_api`（推荐）：将 [`config/singbox-v2ray-api.snippet.json`](config/singbox-v2ray-api.snippet.json) 中的 `experimental.v2ray_api` 片段合并到你的 `/etc/sing-box/config.json`，确保 `inbounds` 和 `users`（如 `owrt-router`、`mac`、`mobile`）与你现有配置一致。
- 如果使用官方默认包不带 `with_v2ray_api`：无需更换二进制，直接将 [`config/singbox-clash-api.snippet.json`](config/singbox-clash-api.snippet.json) 中的 `experimental.clash_api` 片段（监听 `127.0.0.1:9090`）合并到 `/etc/sing-box/config.json`。

重载 `sing-box` 前记得先校验：

```bash
sing-box check -c /etc/sing-box/config.json && systemctl reload-or-restart sing-box
```

### 2. 安装后台服务

```bash
sudo bash deploy/install.sh
```

编辑 `/opt/singbox-traffic-panel/config/panel.yaml`，按你的实际配置修改：

- `collector.source`：`v2ray_api`、`clash_api` 或 `auto`
- `protocols[*].inbound_tag`：与 `/etc/sing-box/config.json` 中 VLESS / Hysteria2 入站的 `tag` 对齐
- `protocols[*].client.server_host`：你的新加坡 VPS 域名或公网 IP（用于生成可扫码客户端链接）
- `server.ssh_host_hint`：你的 SSH 主机别名（仅作页面提示）

检查兼容性并启动：

```bash
/opt/singbox-traffic-panel/.venv/bin/python -m singbox_panel \
  --config /opt/singbox-traffic-panel/config/panel.yaml --check-config

sudo systemctl enable --now singbox-traffic-panel
sudo systemctl status singbox-traffic-panel
```

### 3. 本地通过 SSH 隧道安全访问

在你的 Mac / PC 终端执行：

```bash
ssh -L 8088:127.0.0.1:8088 <你的新加坡VPS>
```

然后在本地浏览器打开：**http://127.0.0.1:8088**。

---

## 运行单元测试

```bash
uv run pytest -v
```

## 目录结构

```text
singbox-traffic-panel/
├── config/
│   ├── panel.example.yaml              # 生产配置模板
│   ├── singbox-v2ray-api.snippet.json  # v2ray_api 启用片段
│   ├── singbox-clash-api.snippet.json  # clash_api 启用片段
│   └── singbox-demo.json               # 本地演示用配置
├── deploy/
│   ├── check-singbox-tags.sh           # sing-box 编译标签检查脚本
│   ├── install.sh                      # VPS 一键安装脚本
│   └── singbox-traffic-panel.service   # systemd 服务单元
├── src/singbox_panel/
│   ├── app.py                          # FastAPI REST API + 静态 SPA
│   ├── config.py                       # YAML 配置解析 + 回环地址安全拦截
│   ├── crypto_x25519.py                # 纯 Python RFC 7748 X25519 公钥推导
│   ├── db.py                           # SQLite WAL 存储 + OCI 10 TB 估算引擎
│   ├── share_links.py                  # VLESS/Hysteria2 分享链接 + 离线 QR
│   ├── singbox_config.py               # 非侵入式配置编辑（备份/校验/回滚/重载）
│   ├── collectors/
│   │   ├── base.py                     # 采集器调度与定时持久化落盘
│   │   ├── grpc_v2ray.py               # 纯 Python HTTP/2 gRPC + Protobuf 编解码
│   │   ├── v2ray_api.py                # v2ray_api 精确采集器
│   │   ├── clash_api.py                # clash_api WebSocket 增量追踪器
│   │   └── mock.py                     # Demo 流量模拟器
│   └── static/                         # 零 CDN 自托管单页 Web 仪表盘
└── tests/                              # 全模块单元测试与 API 集成测试
```
