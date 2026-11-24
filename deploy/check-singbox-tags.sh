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
  echo "[OK] 支持 with_v2ray_api -> 推荐在 panel.yaml 中使用 collector.source: v2ray_api（最准，不漏短连接）"
else
  echo "[INFO] 未包含 with_v2ray_api 标签"
  echo "       选项 A：直接使用官方默认自带的 clash_api（无需换二进制）"
  echo "       选项 B：换成带 with_v2ray_api 编译标签的 sing-box 二进制"
fi

if grep -q "with_clash_api" <<<"$OUT"; then
  echo "[OK] 支持 with_clash_api -> 可使用 experimental.clash_api (127.0.0.1:9090)"
else
  echo "[WARN] 未检测到 with_clash_api 标签"
fi
