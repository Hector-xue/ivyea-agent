# Ivyea Agent 一键安装（Windows PowerShell）。
# 用法：
#   iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
# 可选环境变量：$env:IVYEA_REF / $env:IVYEA_LOCAL / $env:PIP_INDEX_URL
$ErrorActionPreference = "Stop"

$repo = if ($env:IVYEA_REPO) { $env:IVYEA_REPO } else { "https://github.com/Hector-xue/ivyea-agent.git" }
$ref  = if ($env:IVYEA_REF)  { $env:IVYEA_REF }  else { "main" }

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

# 3) 安装 / 升级
$spec = if ($env:IVYEA_LOCAL) { $env:IVYEA_LOCAL } else { "git+$repo@$ref" }
Info "安装 ivyea-agent（来源：$spec）…"
& $py -m pipx install --force $spec

Info "✓ 安装完成。重开 PowerShell 后："
Info "  ivyea config   然后  ivyea chat"
