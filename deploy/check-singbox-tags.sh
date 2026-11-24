#!/usr/bin/env bash
# 在新加坡 VPS 上检查现有 sing-box 二进制是否带有 with_v2ray_api / with_clash_api 编译标签

set -euo pipefail

BIN="${1:-sing-box}"

if ! command -v "$BIN" >/dev/null 2>&1; then
  echo "[ERROR] 未找到 $BIN，请传入完整路径，例如: $0 /usr/bin/sing-box" >&2
  exit 1
fi

OUT="$("$BIN" version)"
echo "$OUT"
echo "----------------------------------------"

if grep -q "with_v2ray_api" <<<"$OUT"; then
  echo "[OK] 支持 with_v2ray_api -> 推荐在 panel.yaml 中使用 collector.source: v2ray_api（最准，按设备/协议精确统计，不漏短连接）"
else
  echo "[INFO] 未包含 with_v2ray_api 标签（官方预编译二进制默认不带该标签）"
  echo "       选项 A（推荐，精确按设备统计）：运行 deploy/build-singbox-v2ray-api.sh 用同版本官方源码编译带 with_v2ray_api 的二进制"
  echo "       选项 B（仅协议/OCI 总额）：使用官方默认 clash_api（注意：clash_api /connections 不返回 metadata.user，设备维度会归为 unknown）"
fi

if grep -q "with_clash_api" <<<"$OUT"; then
  echo "[OK] 支持 with_clash_api -> 可使用 experimental.clash_api (127.0.0.1:9090)"
else
  echo "[WARN] 未检测到 with_clash_api 标签"
fi
