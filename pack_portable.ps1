$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw '找不到 Python 3.10+' }

Push-Location $root
try {
  & $py -m PyInstaller --noconfirm --clean --onedir --windowed --name Renpu `
    --add-data 'schema.sql;.' `
    --add-data 'adapters;adapters' `
    --add-data 'scripts;scripts' `
    --add-data 'cli.py;.' `
    --add-data 'core.py;.' `
    --add-data 'config.py;.' `
    --add-data 'render.py;.' `
    --add-data 'server.py;.' `
    --add-data 'landing.html;.' `
    desktop.py

  & $py -m PyInstaller --noconfirm --clean --onedir --console --name renpu-cli `
    --add-data 'schema.sql;.' `
    --add-data 'adapters;adapters' `
    --add-data 'scripts;scripts' `
    --add-data 'render.py;.' `
    cli.py

  Copy-Item -Recurse -Force 'dist\renpu-cli\*' 'dist\Renpu\'
  Write-Host "Portable build ready: dist\Renpu\Renpu.exe + renpu-cli.exe"
} finally { Pop-Location }