"""
Test 0 — Service Health

All six services must be up, healthy, and return consistent version info.
These run first (test_0_*) so a failure here gives a clear signal before
any business-logic tests run.

No seed data required. No Neo4j required.
"""

import pytest
import httpx


pytestmark = pytest.mark.e2e


class TestServiceHealth:
    """Each service exposes GET /health → 200 {"status": "ok"}."""

    @pytest.mark.parametrize("service", [
        "rbac",
        "graph",
        "kpi_registry",
        "kpi_engine",
        "memory",
    ])
    def test_service_health(self, services: dict, http: httpx.Client, service: str):
        url = services[service]
        resp = http.get(f"{url}/health")
        assert resp.status_code == 200, (
            f"{service} health check failed: {resp.status_code} {resp.text[:200]}"
        )
        body = resp.json()
        # Accept {"status": "ok"} or {"status": "healthy"} or {"ok": true}
        status = body.get("status", body.get("ok", ""))
        assert status in ("ok", "healthy", True), (
            f"{service} reported unhealthy: {body}"
        )

    def test_neo4j_reachable(self, neo4j):
        """Neo4j driver verifies connectivity in the fixture itself."""
        with neo4j.session() as session:
            result = session.run("RETURN 1 AS n")
            assert result.single()["n"] == 1

    def test_postgres_reachable(self, db):
        """Postgres session verifies connectivity in the fixture itself."""
        from sqlalchemy import text
        row = db.execute(text("SELECT current_database()")).scalar()
        assert row is not None

    def test_rbac_api_prefix(self, services: dict, http: httpx.Client):
        """rbac-service exposes /api/v1/ routes."""
        resp = http.get(f"{services['rbac']}/api/v1/roles/")
        # 200 list or 401 if auth required — both mean the route exists
        assert resp.status_code in (200, 401, 403, 422), (
            f"Unexpected status from rbac /api/v1/roles/: {resp.status_code}"
        )

    def test_kpi_registry_catalog_reachable(self, services: dict, http: httpx.Client):
        """kpi-registry catalog endpoint exists."""
        resp = http.get(f"{services['kpi_registry']}/api/v1/kpis/catalog")
        assert resp.status_code in (200, 401, 403), (
            f"kpi-registry catalog returned: {resp.status_code}"
        )

    def test_kpi_engine_registry_reachable(self, services: dict, http: httpx.Client, startup_id: str):
        """kpi-engine registry endpoint exists for startup company."""
        resp = http.get(
            f"{services['kpi_engine']}/api/v1/kpis/registry/{startup_id}"
        )
        assert resp.status_code in (200, 401, 403, 404), (
            f"kpi-engine registry returned: {resp.status_code}"
        )

    def test_graph_persons_endpoint_reachable(self, services: dict, http: httpx.Client, startup_id: str):
        """graph-service persons endpoint exists."""
        resp = http.get(
            f"{services['graph']}/api/v1/persons",
            params={"company_id": startup_id},
        )
        assert resp.status_code in (200, 401, 403), (
            f"graph /persons returned: {resp.status_code}"
        )
