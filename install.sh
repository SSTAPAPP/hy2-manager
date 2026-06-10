#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/hy2-manager"
BIN_PATH="/usr/local/bin/hy2"
REPO_URL="${HY2_MANAGER_REPO:-https://github.com/SSTAPAPP/hy2-manager.git}"
BRANCH="${HY2_MANAGER_BRANCH:-main}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 权限运行。"
    exit 1
  fi
}

install_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl git python3 openssl iproute2 procps
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl git python3 openssl iproute procps-ng
  elif command -v yum >/dev/null 2>&1; then
    yum install -y ca-certificates curl git python3 openssl iproute procps-ng
  else
    echo "未识别的包管理器，请先安装 curl、git、python3、openssl、iproute2。"
    exit 1
  fi
}

sync_project() {
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT

  echo "正在拉取 hy2-manager 项目..."
  if ! git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmp/src" 2>/dev/null; then
    git clone --depth 1 "$REPO_URL" "$tmp/src"
  fi

  mkdir -p "$APP_DIR"
  install -m 0755 "$tmp/src/hy2ctl.py" "$APP_DIR/hy2ctl.py"
  install -m 0755 "$tmp/src/hy2.sh" "$APP_DIR/hy2.sh"
  install -m 0644 "$tmp/src/README.md" "$APP_DIR/README.md"
  install -m 0755 "$tmp/src/install.sh" "$APP_DIR/install.sh"
  ln -sf "$APP_DIR/hy2.sh" "$BIN_PATH"
}

main() {
  need_root
  install_deps
  sync_project
  echo "管理脚本已安装：$BIN_PATH"
  if [ "${HY2_SKIP_CORE_INSTALL:-0}" = "1" ]; then
    echo "已跳过 Hysteria2 初始化。运行 hy2 打开菜单。"
  else
    echo "开始初始化 Hysteria2 服务..."
    "$BIN_PATH" install
    echo "部署完成。运行 hy2 打开管理菜单。"
  fi
}

main "$@"
