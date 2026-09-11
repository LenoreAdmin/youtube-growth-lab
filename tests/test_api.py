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


def test_experiment_changes_are_authenticated_append_only(client,session):
    from test_memory import decision
    from app.memory import activate
    from app.models import Snapshot, utcnow
    from datetime import timedelta
    now=utcnow()
    row=decision(session)
    session.add(Snapshot(video_id="a",observed_at=now,views=100))
    session.flush()
    headers={"Authorization":"Bearer test-token-only"}
    path=f"/api/memory/{row.id}/changes"
    data=dict(dimension="title",applied_at=now.isoformat(),before_value="Original",
              after_value="Updated",rationale="Test clearer benefit")
    assert client.post(path,json=data).status_code == 401
    assert client.post(path,json=data,headers=headers).status_code == 422
    activate(session,row,"a",now)
    session.commit()
    assert client.post(path,json={**data,"applied_at":(now+timedelta(days=1)).isoformat()},headers=headers).status_code == 422
    assert client.post(path,json=data,headers=headers).status_code == 201
    assert client.post(path,json={**data,"after_value":"Second"},headers=headers).status_code == 201
    result=client.get("/api/memory",headers=headers).json()[0]
    assert len(result["changes"]) == 2
    assert result["decision"]["hypothesis"] == row.hypothesis
    assert result["changes"][0]["after_value"] == "Updated"
    assert client.put(path,json=data,headers=headers).status_code == 405


def test_model_audit_requires_authentication(client,session):
    from app.models import ModelRun
    run=ModelRun(horizon_hours=24,training_rows=0,parameters={"version":"test"},metrics={"status":"insufficient_data"})
    session.add(run);session.commit()
    assert client.get(f"/api/models/{run.id}").status_code==401
    response=client.get(f"/api/models/{run.id}",headers={"Authorization":"Bearer test-token-only"})
    assert response.status_code==200
    assert response.json()["parameters"]["version"]=="test"
