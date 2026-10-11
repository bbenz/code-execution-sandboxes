#!/usr/bin/env bash
# Build the referee engine image in Azure Container Registry (server-side; no local Docker needed).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

acr_name() { azd env get-value acrName 2>/dev/null || true; }
ACR_NAME="$(acr_name)"
RG_NAME="$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || true)"

if [[ -z "${ACR_NAME}" ]]; then
  echo "ERROR: acrName not found in the azd environment. Run 'azd provision' first." >&2
  exit 1
fi

echo "Building referee-engine:latest in ${ACR_NAME}…"
az acr build \
  --registry "${ACR_NAME}" \
  ${RG_NAME:+--resource-group "${RG_NAME}"} \
  --image referee-engine:latest \
  --file "${ROOT_DIR}/engine/Dockerfile" \
  "${ROOT_DIR}/engine"
