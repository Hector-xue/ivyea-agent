# Ivyea Agent 一键安装（Windows PowerShell）。
# 用法：
#   iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
# 可选环境变量：
#   $env:IVYEA_VERSION = "latest" 或 "v0.5.0"
#   $env:IVYEA_REF     = "main" / tag，从 git 安装
#   $env:IVYEA_LOCAL   = "C:\path\to\repo"，从本地安装
#   $env:GITHUB_TOKEN  = 私有仓库读取 release 时可用
#   $env:PIP_INDEX_URL = pip 镜像
$ErrorActionPreference = "Stop"

$ownerRepo = if ($env:IVYEA_GITHUB_REPO) { $env:IVYEA_GITHUB_REPO } else { "Hector-xue/ivyea-agent" }
$repo = if ($env:IVYEA_REPO) { $env:IVYEA_REPO } else { "https://github.com/$ownerRepo.git" }
$version = if ($env:IVYEA_VERSION) { $env:IVYEA_VERSION } else { "latest" }
$ref  = if ($env:IVYEA_REF)  { $env:IVYEA_REF }  else { "" }

function Info($m) { Write-Host "[ivyea] $m" -ForegroundColor Green }
function Die($m)  { Write-Host "[ivyea] $m" -ForegroundColor Red; exit 1 }

# 1) Python
$py = $null
foreach ($c in @("py", "python", "python3")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) { Die "需要 Python 3.9+（从 python.org 安装，勾选 Add to PATH）。" }
Info "使用 Python：$py"

# 2) 确保 pipx
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
  Info "未发现 pipx，正在安装…"
  & $py -m pip install --user pipx
  & $py -m pipx ensurepath
  $env:Path = "$env:APPDATA\Python\Scripts;$env:LOCALAPPDATA\Programs\Python\Scripts;$env:Path"
}

function Get-ReleaseWheel($ownerRepo, $version) {
  $api = "https://api.github.com/repos/$ownerRepo/releases"
  $url = if (($version -eq "") -or ($version -eq "latest")) { "$api/latest" } else { "$api/tags/$version" }
  $headers = @{ "Accept" = "application/vnd.github+json"; "User-Agent" = "ivyea-install" }
  if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
  $release = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
  $asset = $release.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
  if (-not $asset) { throw "release 中没有 wheel 资产" }
  return $asset.browser_download_url
}

# 3) 安装 / 升级
if ($env:IVYEA_LOCAL) {
  $spec = $env:IVYEA_LOCAL
} elseif ($ref) {
  $spec = "git+$repo@$ref"
} else {
  Info "查找 GitHub Release wheel（$ownerRepo@$version）…"
  try {
    $spec = Get-ReleaseWheel $ownerRepo $version
  } catch {
    Info "Release wheel 不可用：$($_.Exception.Message)"
    Info "回退到 git main 安装。私有仓库请先配置 GitHub 凭据，或设置 GITHUB_TOKEN/IVYEA_REF。"
    $spec = "git+$repo@main"
  }
}
Info "安装 ivyea-agent（来源：$spec）…"
& $py -m pipx install --force $spec

Info "✓ 安装完成。重开 PowerShell 后："
Info "  ivyea config   然后  ivyea chat"
