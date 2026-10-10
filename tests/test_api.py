"""FastAPI endpoints. One TestClient for the whole module: the MCP session manager can only start once."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import point_app_at, release_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    point_app_at(monkeypatch, tmp_path_factory.mktemp("api"))
    from app.main import app

    with TestClient(app) as test_client:  # runs the lifespan: opens Lucene (if available) and MCP sessions
        yield test_client
    release_app()
    monkeypatch.undo()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["pdf_root_accessible"] is True
    assert set(body) == {"status", "pdf_root_accessible", "search_index_ready"}  # nothing else leaks


def test_create_list_info_and_errors(client):
    created = client.post("/pdfs", json={"filename": "r/a.pdf", "content": "REST text", "title": "T"})
    assert created.status_code == 201 and created.json()["path"] == "r/a.pdf"

    assert client.post("/pdfs", json={"filename": "r/a.pdf", "content": "again"}).status_code == 409
    assert client.post("/pdfs", json={"filename": "../x.pdf", "content": "x"}).status_code == 400
    assert client.post("/pdfs", json={"filename": "x.pdf", "content": ""}).status_code == 422

    assert client.get("/pdfs").json()["count"] == 1
    assert client.get("/pdfs/r/a.pdf").json()["page_count"] == 1
    assert client.get("/pdfs/nope.pdf").status_code == 404


def test_search_endpoint(client):
    pytest.importorskip("lucene", reason="PyLucene is not installed")
    client.post("/pdfs", json={"filename": "s.pdf", "content": "searchable searchable text"})
    body = client.get("/search", params={"q": "searchable"}).json()
    assert body["results"][0]["occurrences"] == 2
    assert client.get("/search", params={"q": "", "limit": 5}).status_code == 422
    assert client.get("/search", params={"q": "zzzz"}).json()["results"] == []


def test_mcp_endpoint_is_mounted(client):
    # A bare GET is not a valid MCP request, but it must reach the MCP app (not a 404 from FastAPI routing).
    assert client.get("/mcp", headers={"accept": "text/event-stream"}).status_code != 404
