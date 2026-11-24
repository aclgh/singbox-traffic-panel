#!/usr/bin/env bash
# 非侵入式新加坡 VPS 安装脚本（默认直接在当前仓库目录安装，不修改现有 sing-box 配置，不开放任何公网端口）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
SERVICE_NAME="singbox-traffic-panel"

echo "==> 安装目录: $APP_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] 需要 Python 3.10+，请先安装 python3 与 python3-venv" >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# 检查 python3-venv / ensurepip 是否可用；Ubuntu/Debian root 环境下自动补装
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    echo "==> 检测到缺少 python${PY_VER}-venv，正在通过 apt 自动安装..."
    apt-get update -qq
    apt-get install -y "python${PY_VER}-venv" || apt-get install -y python3-venv
  else
    echo "[ERROR] 缺少 Python venv (ensurepip) 模块，请先执行：" >&2
    echo "        sudo apt update && sudo apt install -y python${PY_VER}-venv" >&2
    exit 1
  fi
fi

mkdir -p "$APP_DIR"
if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
  echo "==> 复制项目文件到 $APP_DIR ..."
  cp -a "$SCRIPT_DIR/pyproject.toml" "$SCRIPT_DIR/requirements.txt" "$SCRIPT_DIR/README.md" "$APP_DIR/"
  cp -a "$SCRIPT_DIR/src" "$SCRIPT_DIR/config" "$SCRIPT_DIR/deploy" "$APP_DIR/"
fi

cd "$APP_DIR"
mkdir -p data/backups

if [ -d ".venv" ] && [ ! -x ".venv/bin/pip" ]; then
  echo "==> 清理上次未创建完整的 .venv 目录..."
  rm -rf .venv
fi

if [ ! -x ".venv/bin/pip" ]; then
  echo "==> 创建 Python 虚拟环境 (.venv)..."
  if ! python3 -m venv .venv; then
    rm -rf .venv
    echo "[ERROR] 创建虚拟环境失败，已清理残留目录。" >&2
    exit 1
  fi
fi

echo "==> 安装最小化纯 Python 依赖..."
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt
if ! .venv/bin/pip install --no-deps -e .; then
  # 兜底：若无法拉取构建后端，直接写入 .pth 让 .venv 识别 src 目录
  SITE_PKG="$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
  echo "$APP_DIR/src" > "$SITE_PKG/singbox_panel.pth"
fi

if [ ! -f "config/panel.yaml" ]; then
  cp config/panel.example.yaml config/panel.yaml
  chmod 600 config/panel.yaml
  if [ -n "${SUDO_USER:-}" ] && [ "$APP_DIR" = "$SCRIPT_DIR" ]; then
    chown "$SUDO_USER:$SUDO_USER" config/panel.yaml
  fi
  echo "==> 已生成默认配置: $APP_DIR/config/panel.yaml（请按实际 sing-box 配置编辑）"
fi

if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
  sed "s|/opt/singbox-traffic-panel|$APP_DIR|g" deploy/singbox-traffic-panel.service \
    > "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  echo "==> 已安装 systemd 服务: ${SERVICE_NAME}.service（工作目录: $APP_DIR）"
  echo ""
  echo "下一步："
  echo "  1) 运行 $APP_DIR/deploy/check-singbox-tags.sh 确认 sing-box 支持 v2ray_api 还是 clash_api"
  echo "  2) 按 config/singbox-v2ray-api.snippet.json 或 singbox-clash-api.snippet.json 在 sing-box 中启用本地统计 API"
  echo "  3) 编辑 $APP_DIR/config/panel.yaml 填入你的 server_host 与 inbound_tag"
  echo "  4) 检查配置: $APP_DIR/.venv/bin/python -m singbox_panel --config $APP_DIR/config/panel.yaml --check-config"
  echo "  5) 启动服务: sudo systemctl enable --now ${SERVICE_NAME}"
  echo "  6) 在本地电脑通过 SSH 隧道打开后台："
  echo "       ssh -L 8088:127.0.0.1:8088 <你的新加坡VPS>"
  echo "     浏览器访问: http://127.0.0.1:8088"
else
  echo "==> 非 root 或无 systemd 环境，可手动启动："
  echo "    $APP_DIR/.venv/bin/python -m singbox_panel --config $APP_DIR/config/panel.yaml"
fi
