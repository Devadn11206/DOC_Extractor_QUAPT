# One script: installs everything on first run, then starts the app against the GPU server.
# Usage (PowerShell, from the project folder):  powershell -ExecutionPolicy Bypass -File run_app.ps1
#
# The server addresses and key are read from server.env next to this script (created on first run,
# never committed). The models run on that server, so this laptop needs no GPU.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. Latest code (only when this is a git checkout)
if (Test-Path .git) {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not update the code; continuing with the current version." }
}

# 3. Packages (first run creates .venv; later runs only add what changed)
if (-not (Test-Path .venv)) { uv venv --python 3.12 .venv }
uv pip install -q -p .venv\Scripts\python.exe -e ".[ui]"
if ($LASTEXITCODE -ne 0) { throw "Package install failed: see the messages above." }

# 4. Server settings
$config = Join-Path $PSScriptRoot "server.env"
if (-not (Test-Path $config)) {
    @"
# Fill in the values from the server owner, save, then run run_app.ps1 again.
VLLM_API_BASE=https://<pod-id>-8000.proxy.runpod.net/v1
VLLM_TEXT_API_BASE=https://<pod-id>-8001.proxy.runpod.net/v1
VLLM_OCR_API_BASE=https://<pod-id>-8002.proxy.runpod.net/v1
VLLM_API_KEY=
"@ | Set-Content -Encoding utf8 $config
    Write-Host "Created server.env. Fill in the server addresses and key, then run this script again."
    notepad $config
    exit 1
}
foreach ($line in Get-Content $config) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)\s*$') { Set-Item "env:$($Matches[1])" $Matches[2] }
}
if ($env:VLLM_API_BASE -like "*<pod-id>*" -or -not $env:VLLM_API_KEY) {
    Write-Host "server.env still has placeholders or no key. Fill it in and run again."
    notepad $config
    exit 1
}
$env:VLLM_TIMEOUT_SECONDS = "300"
$env:GRADIO_SHARE = "false"

# 5. Check the server, then start the app
.venv\Scripts\python.exe scripts\check_vllm.py
if ($LASTEXITCODE -ne 0) { throw "The model server check failed: is the server running and are the addresses right?" }
Write-Host "Opening http://127.0.0.1:7860 (press Ctrl+C here to stop)"
Start-Process "http://127.0.0.1:7860"
Set-Location src
..\.venv\Scripts\python.exe -m doc_extractor.app
