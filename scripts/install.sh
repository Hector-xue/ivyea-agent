#!/usr/bin/env bash
# Ivyea Agent 一键安装（Linux / macOS）。
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
# 可选环境变量：
#   IVYEA_VERSION=latest      # latest 或 v0.5.5 这种 tag；默认 latest release wheel
#   IVYEA_REF=main            # 从 git 分支/标签安装（开发/私有仓库 fallback）
#   IVYEA_LOCAL=/path/to/repo # 从本地仓库装（离线/开发）
#   IVYEA_AUTO_INSTALL=1      # 缺 Python/pipx 时尽量自动安装；默认 1
#   GITHUB_TOKEN=...          # 私有仓库读取 release 时可用
#   PIP_INDEX_URL=...         # pip 镜像（国内可用清华源加速）
#   IVYEA_WITH_SEMANTIC=1     # 同时安装本地语义检索依赖 sentence-transformers
set -euo pipefail

OWNER_REPO="${IVYEA_GITHUB_REPO:-Hector-xue/ivyea-agent}"
REPO="${IVYEA_REPO:-https://github.com/${OWNER_REPO}.git}"
VERSION="${IVYEA_VERSION:-latest}"
REF="${IVYEA_REF:-}"
AUTO_INSTALL="${IVYEA_AUTO_INSTALL:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || pwd)"
WHEELHOUSE="${IVYEA_WHEELHOUSE:-${SCRIPT_DIR}/wheelhouse}"

say() { printf '\033[32m[ivyea]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[ivyea] %s\033[0m\n' "$*" >&2; exit 1; }

can_sudo() {
  [ "$(id -u)" = "0" ] || command -v sudo >/dev/null 2>&1
}

run_root() {
  if [ "$(id -u)" = "0" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_python() {
  [ "$AUTO_INSTALL" = "1" ] || return 1
  say "未发现 Python 3.9+，尝试自动安装…"
  if command -v brew >/dev/null 2>&1; then
    brew install python
  elif command -v apt-get >/dev/null 2>&1 && can_sudo; then
    run_root apt-get update
    run_root apt-get install -y python3 python3-pip python3-venv
  elif command -v dnf >/dev/null 2>&1 && can_sudo; then
    run_root dnf install -y python3 python3-pip
  elif command -v yum >/dev/null 2>&1 && can_sudo; then
    run_root yum install -y python3 python3-pip
  elif command -v apk >/dev/null 2>&1 && can_sudo; then
    run_root apk add --no-cache python3 py3-pip
  else
    return 1
  fi
}

find_local_wheel() {
  [ -d "$WHEELHOUSE" ] || return 0
  find "$WHEELHOUSE" -maxdepth 1 \( -name 'ivyea_agent-*.whl' -o -name 'ivyea-agent-*.whl' \) | sort -V | tail -n 1 || true
}

install_from_wheelhouse() {
  local wheel="$1"
  local install_dir="${IVYEA_INSTALL_DIR:-$HOME/.ivyea/runtime}"
  local bin_dir="${IVYEA_BIN_DIR:-$HOME/.local/bin}"
  local venv_python="$install_dir/bin/python"
  local venv_pip="$install_dir/bin/pip"
  local launcher="$bin_dir/ivyea"

  say "发现离线依赖包：$WHEELHOUSE"
  say "安装到本地运行环境：$install_dir"
  "$PY" -m venv --clear "$install_dir" || die "创建 venv 失败。Linux 如缺 venv，请安装 python3-venv。"
  "$venv_pip" install --no-index --find-links "$WHEELHOUSE" "$wheel"
  if [ "${IVYEA_WITH_SEMANTIC:-0}" = "1" ] || [ -f "$WHEELHOUSE/.ivyea-semantic" ]; then
    say "安装本地语义检索依赖（sentence-transformers）…"
    "$venv_pip" install --no-index --find-links "$WHEELHOUSE" "sentence-transformers>=3.0"
  fi

  mkdir -p "$bin_dir"
  cat > "$launcher" <<EOF
#!/usr/bin/env sh
exec "$venv_python" -m ivyea_agent.cli "\$@"
EOF
  chmod +x "$launcher"

  local semantic_info=""
  if [ -f "$SCRIPT_DIR/semantic-manifest.json" ]; then
    semantic_info="$("$venv_python" - "$SCRIPT_DIR" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "semantic-manifest.json").read_text(encoding="utf-8"))
src = root / manifest["model_dir"]
dst_root = Path(os.environ.get("IVYEA_EMBEDDING_MODEL_DIR", str(Path.home() / ".ivyea" / "models" / "embedding")))
dst = dst_root / manifest["name"]
if not src.is_dir():
    raise SystemExit(f"bundled embedding model missing: {src}")
if dst.exists():
    shutil.rmtree(dst)
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(src, dst)
print(manifest.get("model") or manifest["name"])
print(dst)
PY
)"
    local semantic_model
    local semantic_path
    semantic_model="$(printf '%s\n' "$semantic_info" | sed -n '1p')"
    semantic_path="$(printf '%s\n' "$semantic_info" | sed -n '2p')"
    if [ -n "$semantic_path" ]; then
      say "配置离线本地语义检索模型：$semantic_model -> $semantic_path"
      "$launcher" retrieval embeddings --backend sentence-transformers --model "$semantic_model" --model-path "$semantic_path" --no-download --json >/dev/null || true
    fi
  fi

  say "✓ 离线安装完成。"
  if ! command -v ivyea >/dev/null 2>&1; then
    say "提示：重开终端，或先执行  export PATH=\"$bin_dir:\$PATH\""
  fi
  "$launcher" self doctor || true
  say "初始化本地检索索引…"
  "$launcher" retrieval sync --json >/dev/null || true
  say "下一步：  ivyea config   （配置主脑模型/密钥），然后  ivyea chat"
}

# 1) Python 3.9+
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  install_python || die "需要 Python 3.9+。自动安装失败，请先安装 python3 后重试。"
  PY="$(command -v python3 || command -v python || true)"
fi
[ -n "$PY" ] || die "需要 Python 3.9+，请先安装 python3。"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)' \
  || die "Python 版本过低（需 ≥3.9）：$($PY -V 2>&1)"
say "使用 $($PY -V 2>&1)"

# 2) 如果是离线包，直接用 venv 安装，避免 pipx 首次初始化时联网升级 pip。
LOCAL_WHEEL="$(find_local_wheel)"
if [ -n "$LOCAL_WHEEL" ]; then
  install_from_wheelhouse "$LOCAL_WHEEL"
  exit 0
fi

# 3) 在线安装确保 pipx
if ! command -v pipx >/dev/null 2>&1; then
  say "未发现 pipx，正在安装…"
  "$PY" -m pip install --user pipx >/dev/null || {
    if [ "$AUTO_INSTALL" = "1" ] && command -v apt-get >/dev/null 2>&1 && can_sudo; then
      run_root apt-get install -y pipx
    else
      die "pipx 安装失败，请手动执行 '$PY -m pip install --user pipx'。"
    fi
  }
  "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v pipx >/dev/null 2>&1 || { export PATH="$HOME/.local/bin:$PATH"; }
command -v pipx >/dev/null 2>&1 || die "pipx 安装失败，请手动 'python3 -m pip install --user pipx'。"

# 解析安装来源：优先「从 releases/download 直链下载 wheel」——不走限流严重的
# api.github.com（国内/共享出口 IP 常因 API 限流拿到 403）。失败再退 API，最后退 git。
install_spec() {
  "$PY" - "$OWNER_REPO" "$VERSION" "$REPO" <<'PY'
import json, os, sys, urllib.request, urllib.error

repo, version, repo_url = sys.argv[1], sys.argv[2], sys.argv[3]
token = os.environ.get("GITHUB_TOKEN", "")
UA = {"User-Agent": "ivyea-install"}

def head_ok(url):
    req = urllib.request.Request(url, method="HEAD", headers=dict(UA))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status < 400
    except Exception:
        return False

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def latest_tag():
    # 用 github.com 的 releases/latest 跳转拿到最新 tag，绕开 api 限流。
    op = urllib.request.build_opener(_NoRedirect)
    try:
        op.open(urllib.request.Request(f"https://github.com/{repo}/releases/latest", headers=dict(UA)), timeout=30)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "") or ""
        if "/tag/" in loc:
            return loc.split("/tag/", 1)[1].strip()
    except Exception:
        pass
    return ""

tag = version if version and version not in ("", "latest") else latest_tag()

# 1) 直链 wheel（约定命名 ivyea_agent-<ver>-py3-none-any.whl）
if tag:
    ver = tag[1:] if tag.startswith("v") else tag
    url = f"https://github.com/{repo}/releases/download/{tag}/ivyea_agent-{ver}-py3-none-any.whl"
    if head_ok(url):
        print(url)
        raise SystemExit(0)

# 2) API 兜底（资产命名不符约定 / 私有仓库带 token）
try:
    api = f"https://api.github.com/repos/{repo}/releases"
    u = f"{api}/latest" if version in ("", "latest") else f"{api}/tags/{version}"
    req = urllib.request.Request(u, headers={**UA, "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    for a in (data.get("assets") or []):
        if a.get("name", "").endswith(".whl"):
            print(a["browser_download_url"])
            raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass

# 3) git 兜底：优先用解析到的 tag（部分解析器会把分支名 main 误当 tag）。
print(f"git+{repo_url}@{tag}" if tag else f"git+{repo_url}@refs/heads/main")
PY
}

# 4) 安装 / 升级 ivyea-agent
if [ -n "${IVYEA_LOCAL:-}" ]; then
  SPEC="$IVYEA_LOCAL"
elif [ -n "$REF" ]; then
  SPEC="git+${REPO}@${REF}"
else
  say "查找安装来源（${OWNER_REPO}@${VERSION}）…"
  SPEC="$(install_spec 2>/tmp/ivyea-install-release.err || true)"
  if [ -z "$SPEC" ]; then
    say "解析安装来源失败：$(cat /tmp/ivyea-install-release.err 2>/dev/null || true)"
    say "回退到 git main 安装。私有仓库请先配置 GitHub 凭据，或设置 GITHUB_TOKEN/IVYEA_REF。"
    SPEC="git+${REPO}@refs/heads/main"
  fi
fi
say "安装 ivyea-agent（来源：$SPEC）…"
pipx install --force "$SPEC"
if [ "${IVYEA_WITH_SEMANTIC:-0}" = "1" ]; then
  say "安装本地语义检索依赖（sentence-transformers）…"
  pipx inject ivyea-agent "sentence-transformers>=3.0"
fi

say "✓ 安装完成。"
# pipx 把 ivyea 装进 ~/.local/bin；当前 shell 可能还没更新 PATH，先临时加上再自检。
export PATH="$HOME/.local/bin:$PATH"
if ! command -v ivyea >/dev/null 2>&1; then
  say "提示：重开终端，或先执行  export PATH=\"\$HOME/.local/bin:\$PATH\""
else
  ivyea self doctor || true
fi
ivyea retrieval sync --json >/dev/null 2>&1 || true
say "下一步：  ivyea config   （配置主脑模型/密钥），然后  ivyea chat"
