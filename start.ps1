# Starts the planner on Windows. First run creates the venv and installs dependencies.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\pip.exe install -r requirements.txt
}
if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example - fill in your keys, then run this script again."
    exit
}

Write-Host "Planner running at http://localhost:8000  (Ctrl+C to stop)"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
