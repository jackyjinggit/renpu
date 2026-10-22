# 仁谱 一键安装 · Windows PowerShell
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here
$py = $env:RENPU_PYTHON
if (-not $py) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
}
if (-not $py) { Write-Error "[X] python not found (need 3.10+). Install Python and add it to PATH." ; exit 1 }

Write-Host "── 仁谱 一键安装"
Write-Host "· Python: $py"

# 1. 装依赖
& $py -c "import Crypto.Cipher" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "· 装 pycryptodome..."
    & $py -m pip install --user --upgrade pycryptodome
}
Write-Host "· pycryptodome: ✅"

# 2. 把 bin/ 加 PATH（用户级）
$binDir = "$parent\bin"
$cur = [Environment]::GetEnvironmentVariable("Path", "User")
if ($cur -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$cur;$binDir", "User")
    Write-Host "· PATH 已写入（用户级）"
} else { Write-Host "· PATH 已含 $binDir" }

# 3. 注册 skill 到 workbuddy
$skillDst = Join-Path $env:LOCALAPPDATA "workbuddy\skills\renpu"
if (Test-Path $skillDst) { Write-Host "· skill 已注册: $skillDst" }
else {
    New-Item -ItemType Directory -Path (Split-Path $skillDst) -Force | Out-Null
    Copy-Item -Recurse -Force $parent $skillDst
    Write-Host "· skill 已注册: $skillDst"
}

Write-Host ""
Write-Host "✅ 装好啦。再开一个 PowerShell，运行："
Write-Host "   renpu doctor"
Write-Host "   renpu init && renpu ingest --demo"