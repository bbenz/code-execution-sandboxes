#!/usr/bin/env bash
# Build the engine images locally and run the orchestrator against the local Docker backend.
# No Azure needed. Requires Docker and Python 3.12+.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENGINE_IMAGE="referee-engine:local"
PRIMED_IMAGE="referee-engine:primed"

echo "==> Building the engine image ($ENGINE_IMAGE)…"
docker build -t "$ENGINE_IMAGE" "$ROOT_DIR/engine"

echo "==> Priming caches and committing $PRIMED_IMAGE…"
docker rm -f ces-primer >/dev/null 2>&1 || true
docker run -d --name ces-primer "$ENGINE_IMAGE" sleep infinity >/dev/null
docker exec ces-primer referee prime
docker exec ces-primer sh -c "rm -rf /work/* 2>/dev/null || true"
docker commit ces-primer "$PRIMED_IMAGE" >/dev/null
docker rm -f ces-primer >/dev/null

if [[ ! -d "$ROOT_DIR/.venv" ]]; then
  echo "==> Creating the virtual environment…"
  python -m venv "$ROOT_DIR/.venv"
fi
PY="$ROOT_DIR/.venv/bin/python"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT_DIR/orchestrator/requirements.txt"

export SANDBOX_BACKEND=docker
export LOCAL_ENGINE_IMAGE="$ENGINE_IMAGE"
export LOCAL_PRIMED_IMAGE="$PRIMED_IMAGE"
export CANDIDATE_CPU=1000m
export CANDIDATE_MEMORY=1024Mi

echo "==> Orchestrator on http://127.0.0.1:8000  (Ctrl+C to stop)"
cd "$ROOT_DIR/orchestrator"
exec "$PY" -m uvicorn orchestrator:app --host 127.0.0.1 --port 8000
