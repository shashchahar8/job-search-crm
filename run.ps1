$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Expected virtual environment Python at $Python"
}

& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
run.ps1


