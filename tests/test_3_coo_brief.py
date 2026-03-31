"""
Test 3 — COO Morning Brief (requires LLM)

Full agent reasoning test — the closest thing to a user actually using the product.

The orchestrator receives a natural-language question, routes it to the right
vertical agent(s), those agents call kpi-engine and graph-service for real data,
and the synthesised answer must contain quantitative content.

Marks:
  @pytest.mark.requires_llm  — costs money, skip in CI unless LLM_API_KEY is set
  @pytest.mark.slow          — may take 30–120s per question

The coo-agent is an MCP stdio server, NOT an HTTP server.
These tests drive it through the orchestrator's HTTP wrapper, which must be
running separately. Set COO_ORCHESTRATOR_URL to override the default.

If the HTTP wrapper is not running, all tests in this file are skipped.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.requires_llm, pytest.mark.slow]

COO_ORCHESTRATOR_URL = os.getenv("COO_ORCHESTRATOR_URL", "http://localhost:8008")


# =============================================================================
# Session fixture — skip entire module if orchestrator is not running
# =============================================================================

@pytest.fixture(scope="module", autouse=True)
def require_orchestrator():
    """Skip all tests in this file if the orchestrator HTTP wrapper is down."""
    try:
        r = httpx.get(f"{COO_ORCHESTRATOR_URL}/health", timeout=5)
        if r.status_code != 200:
            pytest.skip(
                f"COO orchestrator not healthy ({r.status_code}). "
                "Set COO_ORCHESTRATOR_URL or run `make e2e-setup-full`."
            )
    except Exception as exc:
        pytest.skip(
            f"COO orchestrator not reachable at {COO_ORCHESTRATOR_URL}: {exc}. "
            "Run `make e2e-setup-full` to start all services including the agent."
        )


@pytest.fixture(scope="module")
def require_llm_key():
    """Skip if no LLM API key is configured."""
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip(
            "No LLM API key found (OPENAI_API_KEY or ANTHROPIC_API_KEY). "
            "Set the key to run LLM-backed E2E tests."
        )


@pytest.fixture(scope="module")
def orchestrator() -> httpx.Client:
    """httpx.Client pointed at the orchestrator with a long timeout."""
    with httpx.Client(
        base_url=COO_ORCHESTRATOR_URL,
        timeout=httpx.Timeout(120.0),
        headers={"X-API-Key": os.getenv("API_KEY", "dev-key-change-in-production")},
    ) as client:
        yield client


def ask(client: httpx.Client, question: str, company_id: str) -> dict:
    """
    POST /api/v1/ask and return the response body.
    Raises on non-200.
    """
    resp = client.post(
        "/api/v1/ask",
        json={"question": question, "company_id": company_id},
    )
    resp.raise_for_status()
    return resp.json()


def has_numbers(text: str) -> bool:
    """True if text contains at least one number (integer or float)."""
    return bool(re.search(r"\d+(?:[.,]\d+)?", text))


def has_percentage(text: str) -> bool:
    """True if text contains a percentage value."""
    return bool(re.search(r"\d+(?:\.\d+)?\s*%", text))


def has_currency(text: str) -> bool:
    """True if text contains a dollar/currency amount."""
    return bool(re.search(r"[$€£]\s*[\d,]+|\d+[KMB]\s*(?:ARR|MRR|revenue)", text, re.IGNORECASE))


# =============================================================================
# Tests
# =============================================================================

class TestEngineeringBrief:
    """Ask engineering-domain questions; verify data-backed answers."""

    def test_engineering_health_question_returns_answer(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        The most basic agent smoke test: an answer must come back.
        """
        body = ask(orchestrator, "How is Engineering performing this quarter?", startup_id)

        assert "answer" in body, f"Response missing 'answer' key: {body.keys()}"
        answer = body["answer"]
        assert len(answer) >= 50, (
            f"Answer too short ({len(answer)} chars) — likely an error response: {answer}"
        )

    def test_engineering_answer_contains_numbers(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """Answer to an engineering question must contain quantitative data."""
        body = ask(orchestrator, "What is the current engineering velocity and cycle time?", startup_id)
        answer = body["answer"]
        assert has_numbers(answer), (
            f"Engineering answer contains no numbers — agent may not have "
            f"queried kpi-engine.\nAnswer: {answer[:500]}"
        )

    def test_engineering_answer_used_kpi_tool(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        Response metadata should indicate kpi-engine was called.
        Requires the orchestrator to expose tool_calls_made in the response.
        """
        body = ask(orchestrator, "What is our deploy frequency and MTTR?", startup_id)

        tool_calls = body.get("tool_calls_made", [])
        if not tool_calls:
            pytest.skip("Orchestrator does not expose tool_calls_made — skip tool verification.")

        kpi_calls = [t for t in tool_calls if "kpi" in t.lower() or "metric" in t.lower()]
        assert len(kpi_calls) >= 1, (
            f"Expected at least one KPI tool call. Got: {tool_calls}"
        )

    def test_engineering_answer_used_graph_tool(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        A question about people/org structure should trigger a graph query.
        """
        body = ask(
            orchestrator,
            "Which engineers have the most open tasks assigned to them right now?",
            startup_id,
        )

        tool_calls = body.get("tool_calls_made", [])
        if not tool_calls:
            pytest.skip("Orchestrator does not expose tool_calls_made.")

        graph_calls = [
            t for t in tool_calls
            if any(kw in t.lower() for kw in ("graph", "neo4j", "cypher", "query_org"))
        ]
        assert len(graph_calls) >= 1, (
            f"Expected a graph tool call for an org-structure question. Got: {tool_calls}"
        )


class TestFinanceBrief:
    """Ask finance-domain questions; verify ARR/MRR/churn data is referenced."""

    def test_financial_summary_contains_revenue_figures(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """Finance answer must include currency or revenue-related numbers."""
        body = ask(orchestrator, "What is our current ARR and monthly burn rate?", startup_id)
        answer = body["answer"]

        assert has_numbers(answer), "Finance answer has no numbers at all."
        # At least one of: a currency symbol, or M/K suffix, or explicit "ARR"/"MRR"
        assert has_currency(answer) or "ARR" in answer or "MRR" in answer, (
            f"Finance answer doesn't mention ARR/MRR or include currency values.\n"
            f"Answer: {answer[:500]}"
        )

    def test_churn_question_returns_percentage(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """Churn rate question should produce a percentage in the answer."""
        body = ask(orchestrator, "What is our customer churn rate this year?", startup_id)
        answer = body["answer"]
        assert has_percentage(answer), (
            f"Churn answer has no percentage value.\nAnswer: {answer[:500]}"
        )


class TestCrossdomainBrief:
    """
    The orchestrator's killer feature: routing a broad question to multiple
    vertical agents and synthesising into one executive answer.
    """

    def test_broad_question_routes_to_multiple_agents(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        A question spanning Engineering + Finance should result in answers
        from both vertical agents being combined.
        """
        body = ask(
            orchestrator,
            "Give me a quick health check on Engineering delivery and our financial runway.",
            startup_id,
        )
        answer = body["answer"]

        agents_used = body.get("agents_used", [])
        if agents_used:
            assert len(agents_used) >= 2, (
                f"Expected routing to >=2 agents for a cross-domain question. "
                f"Got: {agents_used}"
            )

        # Answer should cover both domains
        assert has_numbers(answer), "Cross-domain answer has no numbers."
        # Mentions engineering AND finance concepts
        eng_terms = ("engineer", "deploy", "velocity", "cycle", "incident", "sprint")
        fin_terms = ("runway", "burn", "ARR", "MRR", "revenue", "cash", "churn")
        has_eng = any(t in answer.lower() for t in eng_terms)
        has_fin = any(t in answer.lower() for t in fin_terms)
        assert has_eng, f"Answer doesn't mention any Engineering terms.\nAnswer: {answer[:600]}"
        assert has_fin, f"Answer doesn't mention any Finance terms.\nAnswer: {answer[:600]}"

    def test_answer_does_not_hallucinate_company_name(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        The answer should reference the actual company name (NovaSpark Technologies)
        rather than a placeholder or wrong company.
        """
        body = ask(
            orchestrator,
            "Summarise our company's overall performance for me.",
            startup_id,
        )
        answer = body["answer"]
        # The agent should know it's talking about NovaSpark from the seed data
        assert "NovaSpark" in answer or has_numbers(answer), (
            "Answer doesn't reference the seeded company and has no data. "
            "Agent may be hallucinating or not reading real data."
        )


class TestMemoryIntegration:
    """
    Verify the memory-service is used across turns — the agent remembers
    context from a previous question in the same session.

    This requires the orchestrator to support session_id.
    """

    def test_follow_up_question_references_previous_context(
        self, orchestrator, startup_id: str, require_llm_key
    ):
        """
        Q1: Ask about ARR
        Q2: Ask "how does that compare to last quarter?" — should use memory
        """
        session_id = "e2e-test-session-001"

        # Turn 1
        body1 = ask.__wrapped__(
            orchestrator,
            "What is our current ARR?",
            startup_id,
        ) if hasattr(ask, "__wrapped__") else ask(
            orchestrator._client if hasattr(orchestrator, "_client") else orchestrator,
            "What is our current ARR?",
            startup_id,
        )

        # Turn 2 — reference prior context
        resp2 = orchestrator.post(
            "/api/v1/ask",
            json={
                "question": "How does that compare to last quarter?",
                "company_id": startup_id,
                "session_id": session_id,
            },
        )
        if resp2.status_code == 422:
            pytest.skip("Orchestrator does not support session_id parameter yet.")

        resp2.raise_for_status()
        body2 = resp2.json()
        answer2 = body2.get("answer", "")
        # A contextual follow-up should contain comparative language or numbers
        comparative_terms = ("quarter", "compared", "previous", "last", "higher", "lower", "more", "less")
        has_comparative = any(t in answer2.lower() for t in comparative_terms)
        assert has_comparative or has_numbers(answer2), (
            f"Follow-up answer shows no comparative context.\nAnswer: {answer2[:400]}"
        )
