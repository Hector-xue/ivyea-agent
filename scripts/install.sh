#!/usr/bin/env bash
# Ivyea Agent 一键安装（Linux / macOS）。
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
# 可选环境变量：
#   IVYEA_VERSION=latest      # latest 或 v0.5.0 这种 tag；默认 latest release wheel
#   IVYEA_REF=main            # 从 git 分支/标签安装（开发/私有仓库 fallback）
#   IVYEA_LOCAL=/path/to/repo # 从本地仓库装（离线/开发）
#   GITHUB_TOKEN=...          # 私有仓库读取 release 时可用
#   PIP_INDEX_URL=...         # pip 镜像（国内可用清华源加速）
set -euo pipefail

OWNER_REPO="${IVYEA_GITHUB_REPO:-Hector-xue/ivyea-agent}"
REPO="${IVYEA_REPO:-https://github.com/${OWNER_REPO}.git}"
VERSION="${IVYEA_VERSION:-latest}"
REF="${IVYEA_REF:-}"

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

release_spec() {
  "$PY" - "$OWNER_REPO" "$VERSION" <<'PY'
import json
import os
import sys
import urllib.request

repo, version = sys.argv[1], sys.argv[2]
api = f"https://api.github.com/repos/{repo}/releases"
url = f"{api}/latest" if version in ("", "latest") else f"{api}/tags/{version}"
req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ivyea-install"})
token = os.environ.get("GITHUB_TOKEN", "")
if token:
    req.add_header("Authorization", f"Bearer {token}")
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.load(resp)
assets = data.get("assets") or []
for asset in assets:
    name = asset.get("name", "")
    if name.endswith(".whl"):
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("release 中没有 wheel 资产")
PY
}

# 3) 安装 / 升级 ivyea-agent
if [ -n "${IVYEA_LOCAL:-}" ]; then
  SPEC="$IVYEA_LOCAL"
elif [ -n "$REF" ]; then
  SPEC="git+${REPO}@${REF}"
else
  say "查找 GitHub Release wheel（${OWNER_REPO}@${VERSION}）…"
  if SPEC="$(release_spec 2>/tmp/ivyea-install-release.err)"; then
    :
  else
    say "Release wheel 不可用：$(cat /tmp/ivyea-install-release.err 2>/dev/null || true)"
    say "回退到 git main 安装。私有仓库请先配置 GitHub 凭据，或设置 GITHUB_TOKEN/IVYEA_REF。"
    SPEC="git+${REPO}@main"
  fi
fi
say "安装 ivyea-agent（来源：$SPEC）…"
pipx install --force "$SPEC"

say "✓ 安装完成。"
if ! command -v ivyea >/dev/null 2>&1; then
  say "提示：重开终端，或先执行  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
say "下一步：  ivyea config   （配置主脑模型/密钥），然后  ivyea chat"
