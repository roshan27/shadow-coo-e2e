#!/usr/bin/env bash
# =============================================================================
# E2E Setup Script
#
# Runs in three phases:
#   1. Start shared infrastructure (Postgres, Neo4j, Redis, TimescaleDB)
#   2. Seed all three company profiles into shadow_coo_db
#   3. Trigger graph sync and KPI batch compute so tests have data to assert on
#
# Usage:
#   ./e2e/scripts/setup.sh           # seed all three profiles
#   ./e2e/scripts/setup.sh startup   # seed startup profile only (faster)
#   SKIP_INFRA=1 ./e2e/scripts/setup.sh  # skip docker compose up (already running)
# =============================================================================

set -euo pipefail

PROFILE="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SEED_SCRIPT="${ROOT_DIR}/shadow-coo-db/seed/scripts/seed_canonical.py"
REF_SCRIPT="${ROOT_DIR}/shadow-coo-db/seed/scripts/seed_reference.py"
PROFILES_DIR="${ROOT_DIR}/shadow-coo-db/seed/data/companies"

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[e2e-setup]${NC} $*"; }
warn() { echo -e "${YELLOW}[e2e-setup]${NC} $*"; }
fail() { echo -e "${RED}[e2e-setup]${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Phase 1: Start infrastructure
# ---------------------------------------------------------------------------
if [[ "${SKIP_INFRA:-0}" == "0" ]]; then
    log "Starting shared infrastructure..."
    docker compose -f "${ROOT_DIR}/docker-compose.infra.yml" up -d

    log "Waiting for Postgres to be healthy..."
    for i in $(seq 1 30); do
        if docker exec shadow_coo_postgres pg_isready -U shadow_coo &>/dev/null; then
            log "Postgres is ready."
            break
        fi
        if [[ $i -eq 30 ]]; then
            fail "Postgres did not become healthy within 60s."
        fi
        sleep 2
    done

    log "Waiting for Neo4j to be healthy..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:7474 &>/dev/null; then
            log "Neo4j is ready."
            break
        fi
        if [[ $i -eq 30 ]]; then
            warn "Neo4j did not respond within 60s — graph tests may be skipped."
            break
        fi
        sleep 2
    done
else
    log "Skipping infra startup (SKIP_INFRA=1)."
fi

# ---------------------------------------------------------------------------
# Phase 2: Seed reference data (KPI definitions)
# ---------------------------------------------------------------------------
log "Seeding KPI reference data..."
if [[ -f "${REF_SCRIPT}" ]]; then
    python "${REF_SCRIPT}" || warn "Reference seed failed — KPI catalog may be empty."
else
    warn "Reference seed script not found at ${REF_SCRIPT}."
fi

# ---------------------------------------------------------------------------
# Phase 3: Seed canonical company data
# ---------------------------------------------------------------------------
seed_profile() {
    local yaml="${PROFILES_DIR}/$1"
    if [[ -f "${yaml}" ]]; then
        log "Seeding $1..."
        python "${SEED_SCRIPT}" --profile "${yaml}" || warn "Seed failed for $1."
    else
        warn "Profile not found: ${yaml}"
    fi
}

case "${PROFILE}" in
    startup)
        seed_profile "startup.yaml"
        ;;
    midsize)
        seed_profile "midsize.yaml"
        ;;
    enterprise)
        seed_profile "enterprise.yaml"
        ;;
    all|*)
        seed_profile "startup.yaml"
        seed_profile "midsize.yaml"
        seed_profile "enterprise.yaml"
        ;;
esac

# ---------------------------------------------------------------------------
# Phase 4: Trigger graph-service full sync
# ---------------------------------------------------------------------------
log "Triggering graph-service full sync..."
GRAPH_WORKER="graph-service-worker"
if docker ps --format '{{.Names}}' | grep -q "^${GRAPH_WORKER}$"; then
    docker exec "${GRAPH_WORKER}" \
        celery -A src.tasks.sync_tasks call \
        src.tasks.sync_tasks.full_graph_sync \
        2>/dev/null || warn "Graph sync task dispatch failed — will try via API."
else
    warn "graph-service-worker container not running — skipping Celery trigger."
    warn "Start graph-service and re-run setup, or data will be synced on schedule."
fi

# ---------------------------------------------------------------------------
# Phase 5: Trigger kpi-engine batch compute
# ---------------------------------------------------------------------------
log "Triggering kpi-engine batch compute..."
KPI_WORKER="kpi-engine-worker"
if docker ps --format '{{.Names}}' | grep -q "^${KPI_WORKER}$"; then
    docker exec "${KPI_WORKER}" \
        celery -A src.tasks.compute_tasks call \
        src.tasks.compute_tasks.compute_daily_rollups \
        2>/dev/null || warn "KPI compute task dispatch failed."
else
    warn "kpi-engine-worker container not running — skipping KPI compute trigger."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log ""
log "Setup complete. Run tests with:"
log "  make e2e-run         # Scenarios 0-2 (no LLM)"
log "  make e2e-run-full    # All scenarios including agent (requires LLM key)"
log ""
log "Company IDs:"
python3 -c "
import uuid
for name, domain in [('startup', 'novaspark.io'), ('midsize', 'meridiansoftware.com'), ('enterprise', 'apexsystems.com')]:
    print(f'  {name:12s}  {uuid.uuid5(uuid.NAMESPACE_DNS, domain)}')
"
