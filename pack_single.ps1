# -*- coding: utf-8 -*-
<#
  仁谱 Renpu · 单文件打包脚本（可重复构建）

  产出两个**单文件** EXE（不是「一个文件夹几十个文件」）：
    dist\Renpu.exe      --onefile --windowed  双击即用的桌面工作台
    dist\renpu-cli.exe  --onefile --console   给 PowerShell / 脚本 / Agent 调

  为什么必须出两个：
    --windowed 的 EXE 在 cmd 里带参数运行时**没有控制台**（stdout 是 None），
    输出无法被脚本捕获。所以 CLI 用独立的 console 版。

  为什么必须 --onefile：
    单文件每次启动都要把运行时解压到临时目录（15~25MB），所以：
      · GUI 内部调 CLI 一律走**进程内调用**（import cli -> dispatch）
      · tour 也走进程内调用
    详见 app.py 与 desktop2.py 的 frozen 分支。

  用法：
    powershell -ExecutionPolicy Bypass -File .\pack_single.ps1
    powershell -ExecutionPolicy Bypass -File .\pack_single.ps1 -Clean
#>
[CmdletBinding()]
param(
    # 用哪个 Python（必须带 tkinter + pycryptodome）
    [string]$Python = "",
    # 清理旧产物
    [switch]$Clean,
    # 跳过 UPX 压缩
    [switch]$NoUpx
)

# 注意：PowerShell 5.1 读 .ps1 时若无 BOM 会按 ANSI 解码，中文注释全乱码。
# 本文件以 UTF-8 + BOM 保存，勿用工具重写成无 BOM 版本。

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 原生命令写 stderr 不该让整个脚本挂掉 —— 用 Continue + 手动查 $LASTEXITCODE
$ErrorActionPreference = 'Continue'

function Write-Step($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host $msg -ForegroundColor Green }

# ── 1. 定位 Python ──
if (-not $Python) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Python = $c; break }
    }
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) { Write-Host '找不到 Python 3.12+（需要有 tkinter）' -ForegroundColor Red; exit 1 }
Write-Step "使用 Python：$Python"

# ── 2. 依赖体检 ──
& $Python -c "import sys, tkinter; print('python', sys.version)"
if ($LASTEXITCODE -ne 0) { Write-Host '这个 Python 没有 tkinter，换一个' -ForegroundColor Red; exit 1 }
& $Python -c "import Crypto; print('pycryptodome OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host '安装 pycryptodome ...' -ForegroundColor Yellow
    & $Python -m pip install pycryptodome
}
& $Python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    Write-Host '安装 pyinstaller ...' -ForegroundColor Yellow
    & $Python -m pip install pyinstaller
}

# ── 3. 清理 ──
if ($Clean) {
    foreach ($d in @('build\Renpu', 'build\renpu-cli', 'dist\Renpu', 'dist\renpu-cli')) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}
New-Item -ItemType Directory -Force -Path 'dist' | Out-Null

# ── 4. 共享参数 ──
# 资源：schema.sql（建库）+ adapters（数据源）+ scripts/tour.py（一键体验）
$datas = @(
    '--add-data', 'schema.sql;.',
    '--add-data', 'adapters;adapters',
    '--add-data', 'scripts;scripts'
)
# 隐式导入：这些模块是延迟 import 的，PyInstaller 静态分析扫不到
$hidden = @(
    '--hidden-import', 'adapters.demo',
    '--hidden-import', 'adapters.wechat',
    '--hidden-import', 'adapters._wechat_decrypt',
    '--hidden-import', 'render',
    '--hidden-import', 'render3d',
    '--hidden-import', 'render_profile',
    '--hidden-import', 'community',
    '--hidden-import', 'server',
    '--hidden-import', 'config',
    '--hidden-import', 'core',
    # http.server 靠 email.parser 解析请求头，必须显式带上
    '--hidden-import', 'email.parser',
    '--hidden-import', 'email.message',
    '--hidden-import', 'email.utils'
)
$upx = @()
if (-not $NoUpx) {
    $upxCmd = Get-Command upx -ErrorAction SilentlyContinue
    if ($upxCmd) { $upx = @('--upx-dir', $upxCmd.Source) }
    else { Write-Host '未找到 UPX，跳过压缩（体积会大一点）' -ForegroundColor DarkGray }
}
# 排除用不上的大模块，砍体积
# 注意：**不能排除 email** —— http.server 依赖 email.parser 解析请求头，
# 排除掉会让 `renpu serve` / GUI 的「Agent 接入」直接 ModuleNotFoundError。
$excludes = @(
    '--exclude-module', 'numpy',
    '--exclude-module', 'pandas',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'pytest',
    '--exclude-module', 'IPython',
    '--exclude-module', 'setuptools',
    '--exclude-module', 'pip',
    '--exclude-module', 'PIL',
    '--exclude-module', 'xmlrpc',
    '--exclude-module', 'pydoc_data'
)

$common = @('--noconfirm', '--clean') + $datas + $hidden + $excludes + $upx

# ── 5. GUI 版：单文件 + 无控制台 ──
Write-Host ''
Write-Ok '>> 打包 Renpu.exe (GUI / onefile / windowed)'
& $Python -m PyInstaller @common --onefile --windowed --name Renpu app.py
if ($LASTEXITCODE -ne 0) { Write-Host 'Renpu.exe 打包失败' -ForegroundColor Red; exit 1 }

# ── 6. CLI 版：单文件 + 有控制台 ──
Write-Host ''
Write-Ok '>> 打包 renpu-cli.exe (CLI / onefile / console)'
& $Python -m PyInstaller @common --onefile --console --name renpu-cli app.py
if ($LASTEXITCODE -ne 0) { Write-Host 'renpu-cli.exe 打包失败' -ForegroundColor Red; exit 1 }

# ── 7. 产物清单 ──
Write-Host ''
Write-Ok '打包完成'
Get-ChildItem 'dist\*.exe' | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    $mb = [math]::Round($_.Length / 1MB, 2)
    Write-Host ("  {0,-16} {1,8} MB  SHA256={2}" -f $_.Name, $mb, $hash) -ForegroundColor Cyan
}
Write-Host ''
Write-Host '用法:'
Write-Host '  dist\Renpu.exe              双击 -> 桌面工作台'
Write-Host '  dist\renpu-cli.exe tour     一键体验'
Write-Host '  dist\renpu-cli.exe --help   全部命令'
exit 0
