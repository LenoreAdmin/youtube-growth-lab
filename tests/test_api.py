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


@pytest.mark.parametrize("authorization",[None,"Bearer wrong","Bearer cron-test-secret"])
def test_manual_sync_requires_app_token(client,monkeypatch,authorization):
    from unittest.mock import Mock
    import app.main as main
    collect=Mock()
    monkeypatch.setattr(main,"collect",collect)
    monkeypatch.setattr(settings,"cron_secret","cron-test-secret")
    response=client.post("/api/sync",headers={"Authorization":authorization} if authorization else {})
    assert response.status_code==401
    collect.assert_not_called()


@pytest.mark.parametrize("status,code",[("ok",200),("deferred",200),("already_running",409),("partial",503),("failed",503)])
def test_manual_sync_shared_pipeline_statuses(client,monkeypatch,status,code):
    from unittest.mock import Mock
    import app.main as main
    collect=Mock(return_value={"status":status})
    monkeypatch.setattr(main,"collect",collect)
    response=client.post("/api/sync",headers={"Authorization":"Bearer test-token-only"})
    assert response.status_code==code
    collect.assert_called_once_with()
    assert response.headers["Cache-Control"]=="no-store"


def test_manual_sync_preview_and_exception_redaction(client,monkeypatch):
    from unittest.mock import Mock
    import app.main as main
    collect=Mock(side_effect=RuntimeError("private-token-must-not-appear"))
    monkeypatch.setattr(main,"collect",collect)
    headers={"Authorization":"Bearer test-token-only"}
    monkeypatch.setattr(settings,"vercel_env","preview")
    assert client.post("/api/sync",headers=headers).status_code==403
    collect.assert_not_called()
    monkeypatch.setattr(settings,"vercel_env","production")
    response=client.post("/api/sync",headers=headers)
    assert response.status_code==503
    assert "private-token" not in response.text
    assert client.get("/api/sync",headers=headers).status_code==405


def test_manual_sync_honors_real_lease_and_updates_import_timestamp(client,session,monkeypatch):
    from app import pipeline, jobs
    import app.main as main
    from test_import_loop import FakeYouTube, wire
    wire(monkeypatch,session)
    monkeypatch.setattr(main,"collect",lambda:pipeline.collect(FakeYouTube()))
    headers={"Authorization":"Bearer test-token-only"}
    owner,_=jobs.acquire(pipeline.Session,"cron-hour")
    try:
        assert client.post("/api/sync",headers=headers).status_code==409
        assert client.get("/api/dashboard",headers=headers).json()["sync"] is None
    finally:
        jobs.release(pipeline.Session,owner,"cron-hour")
    assert client.post("/api/sync",headers=headers).status_code==200
    session.expire_all()
    dashboard=client.get("/api/dashboard",headers=headers).json()
    assert dashboard["sync"]["finished_at"] is not None
    assert dashboard["sync"]["status"]=="ok"
    assert dashboard["videos"][0]["views"]==20000
