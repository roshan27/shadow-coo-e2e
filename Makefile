# =============================================================================
# Shadow COO — E2E Test Runner
#
# Prerequisites:
#   - Docker running
#   - All service containers built (make build in each service repo)
#   - For full tests: OPENAI_API_KEY or ANTHROPIC_API_KEY set
#
# Quickstart:
#   make e2e-setup    # Start infra, seed data, trigger sync + compute
#   make e2e-run      # Run Scenarios 0–2 (no LLM, CI-safe)
#   make e2e-teardown # Stop containers
# =============================================================================

.PHONY: all help e2e-setup e2e-setup-startup e2e-run e2e-run-full \
        e2e-run-health e2e-run-pipeline e2e-run-rbac e2e-run-agent \
        e2e-teardown e2e-teardown-wipe e2e-install e2e-clean

ROOT_DIR := $(abspath $(dir $(abspath $(lastword $(MAKEFILE_LIST))))/..)
E2E_DIR  := $(abspath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

help:
	@echo ""
	@echo "Shadow COO E2E Tests"
	@echo "──────────────────────────────────────────────────────"
	@echo "  make e2e-install        Install test dependencies"
	@echo "  make e2e-setup          Start infra + seed all 3 company profiles"
	@echo "  make e2e-setup-startup  Start infra + seed startup profile only (faster)"
	@echo "  make e2e-run            Run Scenarios 0-2 (no LLM, CI-safe)"
	@echo "  make e2e-run-full       Run all 4 scenarios (requires LLM key)"
	@echo "  make e2e-run-health     Scenario 0: health checks only"
	@echo "  make e2e-run-pipeline   Scenario 1: data pipeline"
	@echo "  make e2e-run-rbac       Scenario 2: RBAC permission chain"
	@echo "  make e2e-run-agent      Scenario 3: COO agent brief (requires LLM)"
	@echo "  make e2e-teardown       Stop containers (keep volumes)"
	@echo "  make e2e-teardown-wipe  Stop containers + delete all volumes"
	@echo ""

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
e2e-install:
	cd $(E2E_DIR) && pip install -e .

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
e2e-setup:
	bash $(E2E_DIR)/scripts/setup.sh all

e2e-setup-startup:
	bash $(E2E_DIR)/scripts/setup.sh startup

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
e2e-run:
	@echo "Running E2E scenarios 0-2 (CI-safe, no LLM)..."
	cd $(E2E_DIR) && python -m pytest tests/ \
		-m "e2e and not requires_llm" \
		-v --tb=short \
		--timeout=120

e2e-run-full:
	@echo "Running all E2E scenarios (includes LLM tests)..."
	cd $(E2E_DIR) && python -m pytest tests/ \
		-m "e2e" \
		-v --tb=short \
		--timeout=180

e2e-run-health:
	@echo "Running health checks only..."
	cd $(E2E_DIR) && python -m pytest tests/test_0_health.py \
		-v --tb=short

e2e-run-pipeline:
	@echo "Running data pipeline tests..."
	cd $(E2E_DIR) && python -m pytest tests/test_1_data_pipeline.py \
		-v --tb=short --timeout=120

e2e-run-rbac:
	@echo "Running RBAC chain tests..."
	cd $(E2E_DIR) && python -m pytest tests/test_2_rbac_chain.py \
		-v --tb=short

e2e-run-agent:
	@echo "Running COO agent brief tests (requires LLM key)..."
	cd $(E2E_DIR) && python -m pytest tests/test_3_coo_brief.py \
		-v --tb=short --timeout=180

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
e2e-teardown:
	bash $(E2E_DIR)/scripts/teardown.sh

e2e-teardown-wipe:
	bash $(E2E_DIR)/scripts/teardown.sh --wipe

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
e2e-clean:
	cd $(E2E_DIR) && find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	cd $(E2E_DIR) && find . -name "*.pyc" -delete 2>/dev/null || true
	cd $(E2E_DIR) && rm -rf .pytest_cache .coverage htmlcov 2>/dev/null || true
