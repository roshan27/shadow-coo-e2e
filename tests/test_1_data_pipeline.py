"""
Test 1 — Data Pipeline

Verifies the core data flow without any LLM:

  shadow-coo-db (seeded)
    └─► graph-service syncs canonical tables → Neo4j
    └─► kpi-engine batch computes → TimescaleDB
          └─► timeseries endpoint returns real values

Assumptions:
  - `make e2e-setup` has already run seed_canonical.py for the startup profile.
  - graph-service full_graph_sync Celery task fires on startup or is triggered
    via the setup script.
  - kpi-engine batch_compute is triggered via the setup script.

If the graph or KPI data isn't there yet this test will wait (with a timeout)
rather than immediately failing.

Company: NovaSpark Technologies (startup)
  ID:     8a6f16b1-b2de-58f0-9257-454c8de4fb6f
  Domain: novaspark.io
"""

from __future__ import annotations

import time

import httpx
import pytest
from sqlalchemy import text

from conftest import poll_until

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

# ---------------------------------------------------------------------------
# Known seed values from startup.yaml / computed batch run
# Assertions use wide bands (±40%) to absorb random seed variation
# ---------------------------------------------------------------------------
EXPECTED_EMPLOYEES_MIN  = 15     # startup profile seeds ~20
EXPECTED_EMPLOYEES_MAX  = 120
EXPECTED_DEV_ITEMS_MIN  = 40     # Jira issues, PRs, etc.
EXPECTED_ACCOUNTS_MIN   = 10     # Salesforce accounts

# KPI codes and expected approximate values (±40% tolerance)
KPI_EXPECTATIONS: list[tuple[str, float, float]] = [
    ("KPI-001", 700_000,   2_500_000),   # ARR  ~$1.26M
    ("KPI-002", 50_000,    200_000),     # MRR  ~$105K
    ("KPI-047", -30,       60),          # eNPS ~12.5
    ("KPI-021", -20,       60),          # NPS  ~19.4
]


# =============================================================================
# 1a  Postgres seed data present
# =============================================================================

class TestSeedDataPresent:
    """Verify canonical tables were populated by seed_canonical.py."""

    def test_canonical_employees_seeded(self, db, startup_id: str):
        count = db.execute(
            text("SELECT count(*) FROM canonical_employees WHERE company_id = :cid"),
            {"cid": startup_id},
        ).scalar()
        assert count >= EXPECTED_EMPLOYEES_MIN, (
            f"Expected >={EXPECTED_EMPLOYEES_MIN} employees, got {count}. "
            "Did you run `make e2e-setup`?"
        )
        assert count <= EXPECTED_EMPLOYEES_MAX, (
            f"Suspiciously high employee count ({count}) — possible duplicate seed."
        )

    def test_canonical_dev_items_seeded(self, db, startup_id: str):
        count = db.execute(
            text("SELECT count(*) FROM canonical_dev_items WHERE company_id = :cid"),
            {"cid": startup_id},
        ).scalar()
        assert count >= EXPECTED_DEV_ITEMS_MIN, (
            f"Expected >={EXPECTED_DEV_ITEMS_MIN} dev items, got {count}."
        )

    def test_canonical_accounts_seeded(self, db, startup_id: str):
        count = db.execute(
            text("SELECT count(*) FROM canonical_accounts WHERE company_id = :cid"),
            {"cid": startup_id},
        ).scalar()
        assert count >= EXPECTED_ACCOUNTS_MIN, (
            f"Expected >={EXPECTED_ACCOUNTS_MIN} accounts, got {count}."
        )

    def test_canonical_incidents_seeded(self, db, startup_id: str):
        count = db.execute(
            text("SELECT count(*) FROM canonical_incidents WHERE company_id = :cid"),
            {"cid": startup_id},
        ).scalar()
        assert count >= 1, "Expected at least 1 incident seeded."

    def test_employees_have_required_fields(self, db, startup_id: str):
        """Spot-check that key fields are populated (not all NULL)."""
        row = db.execute(
            text("""
                SELECT
                    count(*) FILTER (WHERE email IS NOT NULL)   AS with_email,
                    count(*) FILTER (WHERE name IS NOT NULL)    AS with_name,
                    count(*) FILTER (WHERE department IS NOT NULL) AS with_dept
                FROM canonical_employees
                WHERE company_id = :cid
            """),
            {"cid": startup_id},
        ).fetchone()
        assert row.with_email >= 1,  "No employees have email populated."
        assert row.with_name >= 1,   "No employees have name populated."
        assert row.with_dept >= 1,   "No employees have department populated."

    def test_all_three_companies_seeded(self, db, startup_id: str, midsize_id: str, enterprise_id: str):
        """All three company profiles should be present."""
        for cid, label in [
            (startup_id,    "startup"),
            (midsize_id,    "midsize"),
            (enterprise_id, "enterprise"),
        ]:
            count = db.execute(
                text("SELECT count(*) FROM canonical_employees WHERE company_id = :cid"),
                {"cid": cid},
            ).scalar()
            assert count >= 20, (
                f"{label} company ({cid}) has only {count} employees — "
                "may not have been seeded."
            )


# =============================================================================
# 1b  Graph-service — Neo4j populated
# =============================================================================

class TestGraphSync:
    """
    Verify that graph-service has synced canonical data into Neo4j.

    The conftest neo4j fixture skips if Neo4j is unreachable.
    """

    def test_person_nodes_in_neo4j(self, neo4j, startup_id: str):
        """
        Wait up to 60s for Person nodes to appear — the full_graph_sync
        Celery task may still be running when tests start.
        """
        def count_persons():
            with neo4j.session() as s:
                return s.run(
                    "MATCH (p:Person {company_id: $cid}) RETURN count(p) AS n",
                    cid=startup_id,
                ).single()["n"]

        count = poll_until(
            fn=count_persons,
            condition=lambda n: n >= EXPECTED_EMPLOYEES_MIN,
            timeout=90,
            interval=5,
            description=f">='{EXPECTED_EMPLOYEES_MIN} Person nodes in Neo4j",
        )
        assert count >= EXPECTED_EMPLOYEES_MIN

    def test_task_nodes_in_neo4j(self, neo4j, startup_id: str):
        with neo4j.session() as session:
            result = session.run(
                "MATCH (t:Task {company_id: $cid}) RETURN count(t) AS n",
                cid=startup_id,
            )
            count = result.single()["n"]
        assert count >= EXPECTED_DEV_ITEMS_MIN, (
            f"Expected >={EXPECTED_DEV_ITEMS_MIN} Task nodes, got {count}."
        )

    def test_customer_nodes_in_neo4j(self, neo4j, startup_id: str):
        with neo4j.session() as session:
            result = session.run(
                "MATCH (c:Customer {company_id: $cid}) RETURN count(c) AS n",
                cid=startup_id,
            )
            count = result.single()["n"]
        assert count >= EXPECTED_ACCOUNTS_MIN

    def test_person_task_relationships_exist(self, neo4j, startup_id: str):
        """At least one ASSIGNED_TO relationship must exist after sync."""
        with neo4j.session() as session:
            result = session.run(
                """
                MATCH (p:Person {company_id: $cid})-[:ASSIGNED_TO]->(t:Task {company_id: $cid})
                RETURN count(*) AS n
                """,
                cid=startup_id,
            )
            count = result.single()["n"]
        assert count >= 1, (
            "No ASSIGNED_TO relationships found — entity linker may not have run."
        )

    def test_graph_api_persons_endpoint(self, services: dict, http: httpx.Client, startup_id: str):
        """graph-service REST API returns persons from Neo4j."""
        resp = http.get(
            f"{services['graph']}/api/v1/persons",
            params={"company_id": startup_id, "limit": 10},
        )
        assert resp.status_code == 200, f"Persons endpoint: {resp.status_code} {resp.text[:200]}"
        body = resp.json()
        assert "items" in body
        assert body["total"] >= EXPECTED_EMPLOYEES_MIN
        # Spot-check structure of a returned person
        person = body["items"][0]
        assert "name" in person
        assert "company_id" in person

    def test_graph_api_tasks_endpoint(self, services: dict, http: httpx.Client, startup_id: str):
        """graph-service REST API returns tasks from Neo4j."""
        resp = http.get(
            f"{services['graph']}/api/v1/tasks",
            params={"company_id": startup_id, "limit": 10},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= EXPECTED_DEV_ITEMS_MIN

    def test_graph_api_blocker_chain(self, services: dict, http: httpx.Client, startup_id: str):
        """
        blocker-chain endpoint returns a valid structure.
        Use the first task returned and check the response shape —
        an empty chain is fine if no blockers exist.
        """
        # First get a task ID
        resp = http.get(
            f"{services['graph']}/api/v1/tasks",
            params={"company_id": startup_id, "limit": 1},
        )
        tasks = resp.json()["items"]
        if not tasks:
            pytest.skip("No tasks in graph yet.")

        task_id = tasks[0]["id"]
        resp2 = http.get(
            f"{services['graph']}/api/v1/tasks/{task_id}/blocker-chain",
            params={"company_id": startup_id},
        )
        assert resp2.status_code == 200
        body = resp2.json()
        assert "task_id" in body
        assert "chain" in body
        assert isinstance(body["chain"], list)


# =============================================================================
# 1c  KPI Engine — computed values present
# =============================================================================

class TestKpiCompute:
    """
    Verify kpi-engine has computed values for the startup company.

    Tolerances are ±40% to handle RNG variation in seed data.
    """

    def test_kpi_registry_lists_176_kpis(self, services: dict, http: httpx.Client, startup_id: str):
        """kpi-engine registry endpoint returns the full 176-KPI set."""
        resp = http.get(
            f"{services['kpi_engine']}/api/v1/kpis/registry/{startup_id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        count = body.get("count", len(body.get("kpis", [])))
        assert count == 176, (
            f"Expected 176 KPIs in registry, got {count}. "
            "reference_data may not be fully seeded."
        )

    def test_kpi_catalog_from_registry_service(self, services: dict, http: httpx.Client):
        """kpi-registry catalog endpoint returns non-empty list."""
        resp = http.get(f"{services['kpi_registry']}/api/v1/kpis/catalog")
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list)
        assert len(items) > 0, "KPI catalog is empty — reference data not seeded."
        # Verify structure
        item = items[0]
        assert "code" in item or "kpi_id" in item or "kpi_code" in item, (
            f"KPI item missing 'code'/'kpi_id'/'kpi_code' field: {item}"
        )

    @pytest.mark.parametrize("kpi_code,low,high", KPI_EXPECTATIONS)
    def test_kpi_timeseries_value_in_range(
        self,
        services: dict,
        http: httpx.Client,
        startup_id: str,
        kpi_code: str,
        low: float,
        high: float,
    ):
        """
        Timeseries endpoint returns a non-null value within the expected band
        for each key KPI.
        """
        resp = http.get(
            f"{services['kpi_engine']}/api/v1/kpis/timeseries",
            params={
                "kpi_ids": kpi_code,
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "granularity": "monthly",
                "company_id": startup_id,
            },
        )
        assert resp.status_code == 200, (
            f"{kpi_code} timeseries returned {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        results = body.get("results", body) if isinstance(body, dict) else body
        assert len(results) > 0, f"No results for {kpi_code}"

        # Find the matching KPI result
        kpi_result = next(
            (r for r in results if r.get("kpi_id") == kpi_code or r.get("code") == kpi_code),
            results[0] if results else None,
        )
        assert kpi_result is not None, f"Could not find {kpi_code} in timeseries response"

        data_points = kpi_result.get("data", [])
        assert len(data_points) > 0, (
            f"{kpi_code} has no data points — batch compute may not have run."
        )

        # Take the most recent non-null value
        values = [p["value"] for p in data_points if p.get("value") is not None]
        assert len(values) > 0, f"{kpi_code} all data points are null."

        latest = values[-1]
        assert low <= latest <= high, (
            f"{kpi_code} value {latest:.2f} is outside expected range [{low}, {high}]. "
            "Seed data may have changed significantly."
        )

    def test_batch_compute_endpoint_accepts_request(
        self,
        services: dict,
        http: httpx.Client,
        startup_id: str,
    ):
        """
        POST /batch with a small set of KPI codes succeeds.
        This verifies the endpoint is wired correctly — not a full recompute.
        """
        resp = http.post(
            f"{services['kpi_engine']}/api/v1/kpis/batch",
            json={
                "company_id": startup_id,
                "kpi_codes": ["KPI-001", "KPI-002"],
                "period_start": "2025-01-01",
                "period_end": "2025-01-31",
                "period_type": "monthly",
                "store": False,
            },
        )
        assert resp.status_code == 200, (
            f"Batch compute failed: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json()
        assert body["total"] == 2
        assert body["succeeded"] + body["failed"] == 2

    def test_validate_formula_endpoint(self, services: dict, http: httpx.Client):
        """Formula validation endpoint accepts a simple SQL formula."""
        resp = http.post(
            f"{services['kpi_engine']}/api/v1/kpis/validate",
            json={
                "formula": "SELECT count(*) FROM canonical_employees",
                "source_tables": ["canonical_employees"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "valid" in body


# =============================================================================
# 1d  Cross-service consistency checks
# =============================================================================

class TestCrossServiceConsistency:
    """
    The same underlying data should be visible from multiple services.
    Catches misconfiguration (wrong DB_URL, wrong company_id, etc.)
    """

    def test_employee_count_postgres_matches_neo4j(
        self, db, neo4j, startup_id: str
    ):
        """
        Row count in canonical_employees should be within ±5 of
        Person node count in Neo4j (minor lag is acceptable).
        """
        pg_count = db.execute(
            text("SELECT count(*) FROM canonical_employees WHERE company_id = :cid"),
            {"cid": startup_id},
        ).scalar()

        with neo4j.session() as session:
            neo4j_count = session.run(
                "MATCH (p:Person {company_id: $cid}) RETURN count(p) AS n",
                cid=startup_id,
            ).single()["n"]

        diff = abs(pg_count - neo4j_count)
        assert diff <= 5, (
            f"Postgres has {pg_count} employees, Neo4j has {neo4j_count} Person nodes "
            f"(diff={diff}). Graph sync may be stale or pointing at wrong DB."
        )

    def test_graph_api_count_matches_neo4j_direct(
        self, services: dict, http: httpx.Client, neo4j, startup_id: str
    ):
        """
        Count returned by graph-service API matches direct Neo4j Cypher count.
        Verifies the API is reading from the same Neo4j instance.
        """
        resp = http.get(
            f"{services['graph']}/api/v1/persons",
            params={"company_id": startup_id, "limit": 1},
        )
        api_total = resp.json()["total"]

        with neo4j.session() as session:
            neo4j_total = session.run(
                "MATCH (p:Person {company_id: $cid}) RETURN count(p) AS n",
                cid=startup_id,
            ).single()["n"]

        assert api_total == neo4j_total, (
            f"API reports {api_total} persons but Neo4j has {neo4j_total}. "
            "graph-service may be pointing at a different Neo4j."
        )
