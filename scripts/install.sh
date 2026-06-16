#!/usr/bin/env bash
# Ivyea Agent 一键安装（Linux / macOS）。
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
# 可选环境变量：
#   IVYEA_REF=main            # 安装的分支/标签
#   IVYEA_LOCAL=/path/to/repo # 从本地仓库装（离线/开发）
#   PIP_INDEX_URL=...         # pip 镜像（国内可用清华源加速）
set -euo pipefail

REPO="${IVYEA_REPO:-https://github.com/Hector-xue/ivyea-agent.git}"
REF="${IVYEA_REF:-main}"

say() { printf '\033[32m[ivyea]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[ivyea] %s\033[0m\n' "$*" >&2; exit 1; }

# 1) Python 3.9+
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || die "需要 Python 3.9+，请先安装 python3。"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)' \
  || die "Python 版本过低（需 ≥3.9）：$($PY -V 2>&1)"
say "使用 $($PY -V 2>&1)"

# 2) 确保 pipx
if ! command -v pipx >/dev/null 2>&1; then
  say "未发现 pipx，正在安装…"
  "$PY" -m pip install --user pipx >/dev/null
  "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v pipx >/dev/null 2>&1 || { export PATH="$HOME/.local/bin:$PATH"; }
command -v pipx >/dev/null 2>&1 || die "pipx 安装失败，请手动 'python3 -m pip install --user pipx'。"

# 3) 安装 / 升级 ivyea-agent
if [ -n "${IVYEA_LOCAL:-}" ]; then
  SPEC="$IVYEA_LOCAL"
else
  SPEC="git+${REPO}@${REF}"
fi
say "安装 ivyea-agent（来源：$SPEC）…"
pipx install --force "$SPEC"

say "✓ 安装完成。"
if ! command -v ivyea >/dev/null 2>&1; then
  say "提示：重开终端，或先执行  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
say "下一步：  ivyea config   （配置主脑模型/密钥），然后  ivyea chat"
