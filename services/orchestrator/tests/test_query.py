"""POST /query happy path against a live stack.

This test talks to the running compose stack over HTTP; it is not a unit test with a mocked app. It
requires postgres to be populated (run `python -m app.ingest` first) and the orchestrator container
to be up and reachable at ORCHESTRATOR_URL (default http://localhost:8000).
"""

import os

import httpx
import psycopg
import pytest

from app.config import get_settings

# Defaults to the compose service hostname because this test is meant to be run with
# `docker compose run --rm orchestrator pytest tests/test_query.py`, which starts a second
# container on the same compose network as the already-running `orchestrator` service. Override to
# http://localhost:8000 when running against the published port from the host instead.
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8000")

pytestmark = pytest.mark.live_stack


def test_query_happy_path_returns_grounded_citation():
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/query",
        json={"question": "How long is the STEM OPT extension?"},
        timeout=300.0,
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["answer"].strip(), "Expected a non-empty answer"
    assert body["citations"], "Expected at least one citation"
    assert body["disclaimer"], "Expected a disclaimer on every answer"

    cited_urls = {c["source_url"] for c in body["citations"]}
    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source_url, resolved_url FROM documents")
            rows = cur.fetchall()
    known_urls = set()
    for source_url, resolved_url in rows:
        known_urls.add(source_url)
        if resolved_url:
            known_urls.add(resolved_url)

    assert cited_urls & known_urls, (
        f"None of the cited URLs {cited_urls} match a source_url/resolved_url in the documents "
        "table; citations must point at chunks that were actually retrieved."
    )
