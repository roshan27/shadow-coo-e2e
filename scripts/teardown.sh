#!/usr/bin/env bash
# =============================================================================
# E2E Teardown Script
#
# Stops infrastructure and optionally wipes volumes.
#
# Usage:
#   ./e2e/scripts/teardown.sh        # stop containers, keep volumes
#   ./e2e/scripts/teardown.sh --wipe # stop containers AND delete all volumes
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

WIPE=0
if [[ "${1:-}" == "--wipe" ]]; then
    WIPE=1
fi

if [[ "${WIPE}" == "1" ]]; then
    echo "[e2e-teardown] Stopping containers and deleting volumes..."
    docker compose -f "${ROOT_DIR}/docker-compose.infra.yml" down -v
else
    echo "[e2e-teardown] Stopping containers (volumes preserved)..."
    docker compose -f "${ROOT_DIR}/docker-compose.infra.yml" down
fi

echo "[e2e-teardown] Done."
