$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "请先安装 Python 3.10+" }
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -e ".[full]"
& .\.venv\Scripts\pip.exe install pytest
& .\.venv\Scripts\playwright.exe install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host "安装完成。请编辑 .env，然后运行："
Write-Host ".\.venv\Scripts\zotero-pdf-harvester.exe --collection '你的 Zotero 分类名'"
