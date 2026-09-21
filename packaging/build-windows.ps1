$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "未找到 Windows Python Launcher (py)。请安装 Python 3.10+。"
}

py -3 -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.build-venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
& .\.build-venv\Scripts\pyinstaller.exe --clean --noconfirm packaging\agent-data-pipeline.spec

Write-Host "构建完成：$Root\dist\AgentDataPipeline"