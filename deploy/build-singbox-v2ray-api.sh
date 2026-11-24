#!/usr/bin/env bash
# 从官方 SagerNet/sing-box 同版本源码编译带 with_v2ray_api 标签的二进制
# 默认仅编译到暂存路径并做离线校验，绝不自动覆盖正在运行的 sing-box！

set -euo pipefail

CURRENT_BIN="${CURRENT_BIN:-$(command -v sing-box || echo /usr/bin/sing-box)}"
SINGBOX_CONFIG="${SINGBOX_CONFIG:-/etc/sing-box/config.json}"
OUT_BIN="${OUT_BIN:-/tmp/sing-box-with-v2ray-api}"
WORK_DIR="${WORK_DIR:-/tmp/sing-box-build}"

if [ ! -x "$CURRENT_BIN" ]; then
  echo "[ERROR] 未找到当前 sing-box 二进制: $CURRENT_BIN" >&2
  exit 1
fi

if ! command -v go >/dev/null 2>&1; then
  echo "[ERROR] 未找到 Go 工具链，请先安装与当前 sing-box 匹配的 Go 版本。" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "[ERROR] 需要 git 克隆官方源码。" >&2
  exit 1
fi

VER_OUT="$("$CURRENT_BIN" version)"
echo "==> 当前运行版本信息："
echo "$VER_OUT"
echo "----------------------------------------"

VERSION="$(awk '/^sing-box version / {print $3; exit}' <<<"$VER_OUT")"
EXISTING_TAGS="$(awk -F'Tags: ' '/^Tags: / {print $2; exit}' <<<"$VER_OUT" | tr -d '\r')"

if [ -z "$VERSION" ]; then
  echo "[ERROR] 无法从 sing-box version 解析版本号。" >&2
  exit 1
fi

if grep -qw "with_v2ray_api" <<<"$EXISTING_TAGS"; then
  echo "[OK] 当前二进制已包含 with_v2ray_api 标签，无需重新编译。"
  exit 0
fi

# 保留原有全部 tags，并追加 with_v2ray_api
if [ -n "$EXISTING_TAGS" ]; then
  BUILD_TAGS="${EXISTING_TAGS},with_v2ray_api"
else
  BUILD_TAGS="with_gvisor,with_quic,with_dhcp,with_wireguard,with_utls,with_acme,with_clash_api,with_v2ray_api"
fi
BUILD_TAGS="$(echo "$BUILD_TAGS" | tr ' ' ',' | sed 's/,,*/,/g; s/^,//; s/,$//')"

GIT_TAG="v${VERSION#v}"
echo "==> 目标源码标签: $GIT_TAG"
echo "==> 编译标签: $BUILD_TAGS"
echo "==> 输出暂存路径: $OUT_BIN"

rm -rf "$WORK_DIR"
git clone --depth 1 --branch "$GIT_TAG" https://github.com/SagerNet/sing-box.git "$WORK_DIR"

cd "$WORK_DIR"
read -r LDFLAGS_SHARED < release/LDFLAGS
echo "==> 开始编译（不会影响运行中的 sing-box）..."
CGO_ENABLED=0 go build \
  -v \
  -trimpath \
  -tags "$BUILD_TAGS" \
  -ldflags "-X github.com/sagernet/sing-box/constant.Version=${VERSION#v} ${LDFLAGS_SHARED} -s -w -buildid=" \
  -o "$OUT_BIN" \
  ./cmd/sing-box

chmod 755 "$OUT_BIN"

echo ""
echo "==> 校验新二进制版本与标签："
NEW_VER_OUT="$("$OUT_BIN" version)"
echo "$NEW_VER_OUT"
if ! grep -q "with_v2ray_api" <<<"$NEW_VER_OUT"; then
  echo "[ERROR] 新编译的二进制未检测到 with_v2ray_api 标签！" >&2
  exit 1
fi

if [ -f "$SINGBOX_CONFIG" ]; then
  echo ""
  echo "==> 使用新二进制离线校验现有配置 ($SINGBOX_CONFIG)..."
  if [ -r "$SINGBOX_CONFIG" ]; then
    "$OUT_BIN" check -c "$SINGBOX_CONFIG"
  else
    sudo -n "$OUT_BIN" check -c "$SINGBOX_CONFIG"
  fi
  echo "[OK] 离线配置校验通过！"
fi

echo ""
echo "==> 编译与离线校验已完成，尚未替换正在运行的服务。确认切换可执行："
echo "    sudo cp -a ${CURRENT_BIN} ${CURRENT_BIN}.bak.$(date +%Y%m%d%H%M%S)"
echo "    sudo install -m 755 ${OUT_BIN} ${CURRENT_BIN}"
echo "    sudo systemctl restart sing-box"
echo "    # 探测 v2ray_api 是否已生效："
echo "    .venv/bin/python -m singbox_panel --config config/panel.yaml --probe-v2ray-api"
