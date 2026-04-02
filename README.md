# Shadow COO — E2E Test Suite

End-to-end tests and interactive agent explorer for the Shadow COO platform.

## Contents

| Path | Description |
|---|---|
| `tests/` | pytest E2E scenarios (health, pipeline, RBAC, agent) |
| `e2e_dashboard.html` | Interactive browser dashboard for the COO Agent |
| `scripts/setup.sh` | Start infra containers and seed test data |
| `scripts/teardown.sh` | Stop containers (optionally wipe volumes) |

---

## E2E Dashboard

A browser-based test runner that calls the COO Agent directly and visualises the full service call chain.

**Start it:**
```bash
python -m http.server 7777
# open http://localhost:7777/e2e_dashboard.html
```

**Features:**
- 9 test scenarios covering engineering, finance, cross-functional, and multi-turn questions
- Role switcher — run each test as **COO**, **CTO**, **CFO**, or **Engineering Lead**
- Per-role expected KPIs, access notes, and pass/fail assertions
- Full call chain per test: `coo-agent → kpi-engine → neo4j`, with structured REQUEST / RESPONSE blocks for every hop

**Requires:** `coo-agent` running on `http://localhost:8080` (see [roshan27/coo-agent](https://github.com/roshan27/coo-agent))

---

## pytest Scenarios

| Scenario | File | LLM required |
|---|---|---|
| 0 — Health checks | `test_0_health.py` | No |
| 1 — Data pipeline | `test_1_data_pipeline.py` | No |
| 2 — RBAC chain | `test_2_rbac_chain.py` | No |
| 3 — COO agent brief | `test_3_coo_brief.py` | Yes |

### Quickstart

```bash
# Install dependencies
pip install -e .

# Start infra + seed all company profiles
make e2e-setup

# Run CI-safe scenarios (no LLM)
make e2e-run

# Run all scenarios (requires ANTHROPIC_API_KEY or OPENAI_API_KEY)
make e2e-run-full

# Stop containers
make e2e-teardown
```

### Prerequisites

- Docker running with infra stack up (`make e2e-setup`)
- Python 3.11+
- For agent tests: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` set

---

## Infrastructure

The infra stack is defined in `../docker-compose.infra.yml` and includes:

| Service | Port |
|---|---|
| PostgreSQL (main) | 5432 |
| TimescaleDB | 5435 |
| Neo4j | 7474 / 7687 |
| Redis | 6379 |
| Memory DB (pgvector) | 5434 |
| pgAdmin | 5050 |

Application services run separately — see individual service repos for their docker-compose files.
