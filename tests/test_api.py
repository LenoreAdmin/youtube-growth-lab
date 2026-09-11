import pytest
from fastapi.testclient import TestClient
from app.main import app, db
from app.config import settings

@pytest.fixture
def client(session):
    app.dependency_overrides[db]=lambda:session
    original=settings.app_token
    settings.app_token="test-token-only"
    with TestClient(app) as c:
        yield c
    settings.app_token=original
    app.dependency_overrides.clear()

def test_private_data_requires_token(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/memory").status_code == 401

def test_empty_dashboard_and_static_page(client):
    response=client.get("/api/dashboard",headers={"Authorization":"Bearer test-token-only"})
    assert response.status_code == 200
    assert response.json()["videos"][0]["score"] is None
    page=client.get("/")
    assert page.status_code == 200
    assert "Content-Security-Policy" in page.headers

def test_invalid_memory_rejected(client):
    assert client.post("/api/memory",json={},headers={"Authorization":"Bearer test-token-only"}).status_code == 422

def test_health(client):
    assert client.get("/health").json() == {"status":"ok"}
