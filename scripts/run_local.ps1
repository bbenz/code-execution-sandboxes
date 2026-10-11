#!/usr/bin/env pwsh
# Build the engine images locally and run the orchestrator against the local Docker backend.
# No Azure needed. Requires Docker Desktop and Python 3.12+.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$engineImage = "referee-engine:local"
$primedImage = "referee-engine:primed"

Write-Host "==> Building the engine image ($engineImage)…"
docker build -t $engineImage "$root/engine"

Write-Host "==> Priming caches and committing $primedImage…"
docker rm -f ces-primer 2>$null | Out-Null
docker run -d --name ces-primer $engineImage sleep infinity | Out-Null
docker exec ces-primer referee prime
docker exec ces-primer sh -c "rm -rf /work/* 2>/dev/null || true"
docker commit ces-primer $primedImage | Out-Null
docker rm -f ces-primer | Out-Null

if (-not (Test-Path "$root/.venv")) {
    Write-Host "==> Creating the virtual environment…"
    python -m venv "$root/.venv"
}
$py = Join-Path $root ".venv/Scripts/python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r "$root/orchestrator/requirements.txt"

$env:SANDBOX_BACKEND = "docker"
$env:LOCAL_ENGINE_IMAGE = $engineImage
$env:LOCAL_PRIMED_IMAGE = $primedImage
$env:CANDIDATE_CPU = "1000m"
$env:CANDIDATE_MEMORY = "1024Mi"

Write-Host "==> Orchestrator on http://127.0.0.1:8000  (Ctrl+C to stop)"
Push-Location "$root/orchestrator"
try { & $py -m uvicorn orchestrator:app --host 127.0.0.1 --port 8000 } finally { Pop-Location }
