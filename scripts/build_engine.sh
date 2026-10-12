#!/usr/bin/env bash
# Build the referee engine image in Azure Container Registry (server-side; no local Docker needed).
# ACR builds aren't reproducible, so a rebuild of unchanged sources still gets a new digest and new disk
# images. The image is also tagged with a hash of engine/, and the build is skipped when `latest` already
# came from the current sources. Set REFEREE_FORCE_BUILD=true to rebuild anyway.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGINE_DIR="${ROOT_DIR}/engine"

ACR_NAME="$(azd env get-value acrName 2>/dev/null || true)"
RG_NAME="$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || true)"

if [[ -z "${ACR_NAME}" ]]; then
  echo "ERROR: acrName not found in the azd environment. Run 'azd provision' first." >&2
  exit 1
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

# Hash the files the build sends (the ones engine/.dockerignore doesn't exclude).
source_hash() {
  (cd "${ENGINE_DIR}" && find . -type f | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r file; do
    case "/${file}" in
      */target/*|*/__pycache__/*|*/.pytest_cache/*|*/.ruff_cache/*|*/.referee/*|*.pyc) continue ;;
    esac
    printf '%s %s\n' "${file}" "$(sha256 < "${file}")"
  done | sha256 | cut -c1-12)
}

digest_of() {
  az acr repository show --name "${ACR_NAME}" --image "referee-engine:$1" --query digest -o tsv 2>/dev/null || true
}

SOURCE_TAG="src-$(source_hash)"
if [[ "${REFEREE_FORCE_BUILD:-}" != "true" ]]; then
  BUILT="$(digest_of "${SOURCE_TAG}")"
  if [[ -n "${BUILT}" && "${BUILT}" == "$(digest_of latest)" ]]; then
    echo "referee-engine:latest is already built from the current engine sources (${SOURCE_TAG}). Skipping the build."
    exit 0
  fi
fi

echo "Building referee-engine:latest (${SOURCE_TAG}) in ${ACR_NAME}..."
az acr build \
  --registry "${ACR_NAME}" \
  ${RG_NAME:+--resource-group "${RG_NAME}"} \
  --image referee-engine:latest \
  --image "referee-engine:${SOURCE_TAG}" \
  --file "${ENGINE_DIR}/Dockerfile" \
  "${ENGINE_DIR}"