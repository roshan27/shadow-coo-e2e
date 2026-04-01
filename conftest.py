"""
E2E Test Configuration and Shared Fixtures

All fixtures are session-scoped so the expensive setup (seeding, graph sync,
KPI batch compute) only runs once per test session.

Environment variables (with defaults for local dev):
  RBAC_URL          http://localhost:8007
  GRAPH_URL         http://localhost:8003
  KPI_REGISTRY_URL  http://localhost:8004
  KPI_ENGINE_URL    http://localhost:8005
  MEMORY_URL        http://localhost:8006
  NEO4J_URI         bolt://localhost:7687
  NEO4J_USER        neo4j
  NEO4J_PASSWORD    shadow_coo_dev
  DB_URL            postgresql+psycopg://shadow_coo:shadow_coo_dev@localhost:5432/shadow_coo
  API_KEY           dev-key-change-in-production
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Generator

import httpx
import pytest
import structlog
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

logger = structlog.get_logger(__name__)

# =============================================================================
# Deterministic company IDs — computed the same way as seed_canonical.py:
#   uuid.uuid5(uuid.NAMESPACE_DNS, primary_domain)
# =============================================================================
STARTUP_ID    = str(uuid.uuid5(uuid.NAMESPACE_DNS, "novaspark.io"))
MIDSIZE_ID    = str(uuid.uuid5(uuid.NAMESPACE_DNS, "meridiansoftware.com"))
ENTERPRISE_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "apexsystems.com"))

# =============================================================================
# Service base URLs
# =============================================================================
SERVICES: dict[str, str] = {
    "rbac":         os.getenv("RBAC_URL",         "http://localhost:8007"),
    "graph":        os.getenv("GRAPH_URL",         "http://localhost:8003"),
    "kpi_registry": os.getenv("KPI_REGISTRY_URL",  "http://localhost:8004"),
    "kpi_engine":   os.getenv("KPI_ENGINE_URL",    "http://localhost:8005"),
    "memory":       os.getenv("MEMORY_URL",        "http://localhost:8006"),
}

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "shadow_coo_dev")
DB_URL         = os.getenv("DB_URL",         "postgresql+psycopg://shadow_coo:shadow_coo_dev@localhost:5432/shadow_coo")
API_KEY        = os.getenv("API_KEY",        "dev-key-change-in-production")

# Deterministic UUID for the e2e test user (required by kpi-registry)
E2E_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "e2e-test-user.shadow-coo"))

# Default headers for all service calls
DEFAULT_HEADERS = {
    "X-API-Key":    API_KEY,
    "X-Company-Id": STARTUP_ID,
    "X-User-Id":    E2E_USER_ID,
}


# Services that use /healthz instead of /health
HEALTHZ_SERVICES = {"kpi_engine"}

# =============================================================================
# Helpers
# =============================================================================

def wait_for_service(url: str, timeout: int = 30, interval: float = 2.0) -> bool:
    """
    Poll /health (or /healthz) until 200 or timeout expires.
    Sends the API key header so services that require auth don't 401.
    """
    # Try /health first, then /healthz — handle both conventions
    paths = ["/health", "/healthz"]
    headers = {"X-API-Key": API_KEY}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in paths:
            try:
                r = httpx.get(f"{url}{path}", timeout=5, headers=headers)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
        time.sleep(interval)
    return False


def poll_until(
    fn,
    condition,
    timeout: int = 60,
    interval: float = 3.0,
    description: str = "condition",
) -> Any:
    """
    Repeatedly call fn() until condition(result) is True or timeout expires.
    Returns the last result. Raises TimeoutError if condition never met.
    """
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = fn()
        if condition(result):
            return result
        time.sleep(interval)
    raise TimeoutError(
        f"Timed out after {timeout}s waiting for {description}. "
        f"Last result: {result}"
    )


# =============================================================================
# Session-scoped fixtures
# =============================================================================

@pytest.fixture(scope="session")
def services() -> dict[str, str]:
    """Base URLs for all services."""
    return SERVICES


@pytest.fixture(scope="session")
def startup_id() -> str:
    return STARTUP_ID


@pytest.fixture(scope="session")
def midsize_id() -> str:
    return MIDSIZE_ID


@pytest.fixture(scope="session")
def enterprise_id() -> str:
    return ENTERPRISE_ID


@pytest.fixture(scope="session")
def http() -> Generator[httpx.Client, None, None]:
    """
    Shared httpx.Client with default auth headers.
    Connection-pooled, reused across the whole test session.
    """
    with httpx.Client(
        timeout=httpx.Timeout(30.0),
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def db():
    """
    SQLAlchemy session connected to postgres-main.
    Only created if the DB is reachable; skips the test if not.
    """
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(DB_URL, pool_pre_ping=True)
        Session = sessionmaker(bind=engine)
        session = Session()
        # Quick connectivity check
        session.execute(text("SELECT 1"))
        yield session
        session.close()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture(scope="session")
def neo4j():
    """
    Neo4j driver session.
    Only created if Neo4j is reachable; skips the test if not.
    """
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASSWORD),
        )
        driver.verify_connectivity()
        yield driver
        driver.close()
    except Exception as exc:
        pytest.skip(f"Neo4j not reachable: {exc}")


@pytest.fixture(scope="session", autouse=True)
def assert_services_up(services: dict[str, str]):
    """
    Session-wide gate: skip entire test session if core services are down.
    Checked once at the start; individual tests can still skip themselves
    if a specific service is down.
    """
    unavailable = []
    for name, url in services.items():
        if not wait_for_service(url, timeout=10):
            unavailable.append(f"{name} ({url})")

    if unavailable:
        pytest.skip(
            f"Services not available — run `make e2e-setup` first.\n"
            f"Unavailable: {', '.join(unavailable)}"
        )
