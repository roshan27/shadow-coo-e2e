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
# Phase 5: Create TimescaleDB daily_kpi_rollup hypertable + backfill
# ---------------------------------------------------------------------------
log "Creating daily_kpi_rollup hypertable and backfilling from kpi_values..."
python3 - << 'PYEOF'
import sys
try:
    import psycopg

    # 1. Create hypertable in TimescaleDB
    ts = psycopg.connect("postgresql://shadow_coo:shadow_coo_dev@localhost:5435/shadow_coo_ts")
    ts.autocommit = True
    cur = ts.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_kpi_rollup (
            day          DATE             NOT NULL,
            kpi_id       TEXT             NOT NULL,
            company_id   TEXT             NOT NULL DEFAULT '',
            value_simple DOUBLE PRECISION,
            value_num    DOUBLE PRECISION,
            value_denom  DOUBLE PRECISION
        )
    """)
    try:
        cur.execute("SELECT create_hypertable('daily_kpi_rollup', 'day', if_not_exists => TRUE)")
    except Exception:
        pass
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_kpi_rollup_company ON daily_kpi_rollup (company_id, kpi_id, day DESC)")
    ts.close()

    # 2. Read deduplicated rows from postgres-main
    pg = psycopg.connect("postgresql://shadow_coo:shadow_coo_dev@localhost:5432/shadow_coo")
    pgcur = pg.cursor()
    pgcur.execute("""
        SELECT DISTINCT ON (kr.company_id, kr.code, kv.period_start)
            kv.period_start, kr.code, kv.value, CAST(kr.company_id AS TEXT)
        FROM kpi_values kv
        JOIN kpi_registry kr ON kr.kpi_id = kv.kpi_id
        WHERE kv.period_start IS NOT NULL
        ORDER BY kr.company_id, kr.code, kv.period_start, kv.recorded_at DESC
    """)
    rows = pgcur.fetchall()
    pg.close()

    # 3. Write to TimescaleDB
    ts = psycopg.connect("postgresql://shadow_coo:shadow_coo_dev@localhost:5435/shadow_coo_ts")
    tscur = ts.cursor()
    tscur.execute("TRUNCATE daily_kpi_rollup")
    tscur.executemany(
        "INSERT INTO daily_kpi_rollup (day, kpi_id, value_simple, company_id) VALUES (%s, %s, %s, %s)",
        rows
    )
    ts.commit()
    tscur.execute("SELECT count(*) FROM daily_kpi_rollup")
    count = tscur.fetchone()[0]
    ts.close()
    print(f"  daily_kpi_rollup: {count} rows")
except Exception as e:
    print(f"  WARNING: TimescaleDB backfill failed: {e}", file=sys.stderr)
PYEOF

# ---------------------------------------------------------------------------
# Phase 6: Seed e2e test user into rbac-service
# ---------------------------------------------------------------------------
log "Seeding e2e test user into rbac-service..."
python3 - << 'PYEOF'
import sys, uuid
from datetime import datetime, timezone

E2E_USER_ID   = str(uuid.uuid5(uuid.NAMESPACE_DNS, "e2e-test-user.shadow-coo"))
STARTUP_ID    = str(uuid.uuid5(uuid.NAMESPACE_DNS, "novaspark.io"))

try:
    import psycopg
    pg = psycopg.connect("postgresql://shadow_coo:shadow_coo_dev@localhost:5432/shadow_coo")
    pgcur = pg.cursor()
    pgcur.execute("""
        INSERT INTO users (user_id, company_id, email, name, status, is_super_admin, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (email) DO NOTHING
    """, (E2E_USER_ID, STARTUP_ID, 'e2e-test@shadow-coo.internal', 'E2E Test User',
          'active', True, datetime.now(timezone.utc), datetime.now(timezone.utc)))
    pg.commit()
    pg.close()
    print(f"  e2e user {E2E_USER_ID} seeded (or already exists)")
except Exception as e:
    print(f"  WARNING: e2e user seed failed: {e}", file=sys.stderr)
PYEOF

# Assign e2e user to Admin role via rbac-service API (best-effort)
RBAC_URL="${RBAC_SERVICE_URL:-http://localhost:8001}"
E2E_USER_ID=$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'e2e-test-user.shadow-coo'))")
STARTUP_ID=$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'novaspark.io'))")

# Get admin role ID
ADMIN_ROLE_ID=$(curl -sf \
    -H "X-API-Key: ${API_KEY:-dev-key-change-in-production}" \
    -H "X-User-Id: ${E2E_USER_ID}" \
    -H "X-Company-Id: ${STARTUP_ID}" \
    "${RBAC_URL}/api/v1/roles/?company_id=${STARTUP_ID}&role_name=Admin" 2>/dev/null \
    | python3 -c "import sys,json; roles=json.load(sys.stdin); print(roles[0]['role_id'] if roles else '')" 2>/dev/null || echo "")

if [[ -n "${ADMIN_ROLE_ID}" ]]; then
    curl -sf -X POST \
        -H "X-API-Key: ${API_KEY:-dev-key-change-in-production}" \
        -H "X-User-Id: ${E2E_USER_ID}" \
        -H "X-Company-Id: ${STARTUP_ID}" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${E2E_USER_ID}\", \"role_id\": \"${ADMIN_ROLE_ID}\"}" \
        "${RBAC_URL}/api/v1/users/${E2E_USER_ID}/roles" 2>/dev/null \
        && log "e2e user assigned to Admin role." \
        || warn "Could not assign Admin role to e2e user (may already be assigned)."
else
    warn "Admin role not found — e2e user may lack RBAC permissions."
fi

# ---------------------------------------------------------------------------
# Phase 7: Trigger graph-service full sync (per-company to avoid multi-tenant mix)
# ---------------------------------------------------------------------------
log "Triggering graph-service full sync for each company..."
GRAPH_WORKER="graph-service-worker"
if docker ps --format '{{.Names}}' | grep -q "^${GRAPH_WORKER}$"; then
    for CID in "${STARTUP_ID}" \
               "$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'meridiansoftware.com'))")" \
               "$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'apexsystems.com'))")"; do
        docker exec "${GRAPH_WORKER}" \
            celery -A src.tasks.sync_tasks call \
            src.tasks.sync_tasks.full_graph_sync \
            --args "[\"${CID}\"]" \
            2>/dev/null || warn "Graph sync dispatch failed for ${CID}."
    done
    log "Graph sync tasks dispatched (running async in worker)."
else
    warn "graph-service-worker not running — skipping graph sync."
fi

# ---------------------------------------------------------------------------
# Phase 8: Trigger kpi-engine batch compute
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
log "  make e2e-run         # Scenarios 0-2 (no LLM) — 45 tests"
log "  make e2e-run-full    # All scenarios including agent (requires LLM key)"
log ""
log "Company IDs:"
python3 -c "
import uuid
for name, domain in [('startup', 'novaspark.io'), ('midsize', 'meridiansoftware.com'), ('enterprise', 'apexsystems.com')]:
    print(f'  {name:12s}  {uuid.uuid5(uuid.NAMESPACE_DNS, domain)}')
"
