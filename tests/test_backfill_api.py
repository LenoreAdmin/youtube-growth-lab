"""Manual backfill endpoint: APP_TOKEN only, preview-disabled, redacted failures, status exposure."""
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from app.config import settings
from app.models import Video
from test_api import client  # noqa: F401  shared authenticated test client fixture


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer cron-test-secret"])
def test_backfill_requires_app_token(client, monkeypatch, authorization):
    import app.main as main
    run = Mock()
    monkeypatch.setattr(main.backfill_module, "run", run)
    monkeypatch.setattr(settings, "cron_secret", "cron-test-secret")
    headers = {"Authorization": authorization} if authorization else {}
    assert client.post("/api/backfill", headers=headers).status_code == 401
    assert client.get("/api/backfill", headers=headers).status_code == 401
    run.assert_not_called()


@pytest.mark.parametrize("status,code", [("ok", 200), ("deferred", 200), ("throttled", 200), ("partial", 200),
                                         ("already_running", 409), ("failed", 503)])
def test_backfill_statuses(client, monkeypatch, status, code):
    import app.main as main
    run = Mock(return_value={"status": status, "issues": [], "stats": {}})
    monkeypatch.setattr(main.backfill_module, "run", run)
    response = client.post("/api/backfill", headers={"Authorization": "Bearer test-token-only"})
    assert response.status_code == code
    run.assert_called_once_with(retry_failed=False)
    assert response.headers["Cache-Control"] == "no-store"
    if code == 200:
        assert response.json()["status"] == status


def test_backfill_retry_flag_preview_and_redaction(client, monkeypatch):
    import app.main as main
    run = Mock(return_value={"status": "ok", "issues": [], "stats": {}})
    monkeypatch.setattr(main.backfill_module, "run", run)
    headers = {"Authorization": "Bearer test-token-only"}
    assert client.post("/api/backfill", json={"retry_failed": True}, headers=headers).status_code == 200
    run.assert_called_once_with(retry_failed=True)
    monkeypatch.setattr(settings, "vercel_env", "preview")
    assert client.post("/api/backfill", headers=headers).status_code == 403
    assert run.call_count == 1
    monkeypatch.setattr(settings, "vercel_env", "production")
    monkeypatch.setattr(main.backfill_module, "run", Mock(side_effect=RuntimeError("private-token-must-not-appear")))
    response = client.post("/api/backfill", headers=headers)
    assert response.status_code == 503
    assert "private-token" not in response.text


def test_backfill_status_and_dashboard_summary_end_to_end(client, session, monkeypatch):
    from test_backfill import HistoryYouTube, wire
    from app import backfill
    import app.main as main
    wire(monkeypatch, session, published=datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
    original = backfill.run
    monkeypatch.setattr(main.backfill_module, "run", lambda retry_failed=False: original(HistoryYouTube()))
    headers = {"Authorization": "Bearer test-token-only"}
    before = client.get("/api/dashboard", headers=headers).json()["backfill"]
    assert before["last_run"] is None and before["complete"] is False
    assert before["stages"]["traffic"]["pending"] == 2
    assert client.post("/api/backfill", headers=headers).json()["status"] == "ok"
    session.expire_all()
    status = client.get("/api/backfill", headers=headers).json()
    assert status["last_run"]["status"] == "ok" and status["complete"] is True
    assert status["coverage"]["daily_first"] == "2026-05-01"
    assert len(status["progress"]) == 6 and all(p["status"] == "complete" for p in status["progress"])
    assert any("Reporting API" in note for note in status["limits"])
    dashboard = client.get("/api/dashboard", headers=headers).json()
    assert dashboard["backfill"]["stages"]["retention"]["complete"] == 2
    assert dashboard["sync"] is None  # Backfill runs are never confused with hourly sync runs.
    detail = client.get("/api/videos/a", headers=headers).json()
    assert detail["history"]["traffic_sources"][0]["source"] == "YT_SEARCH"
    assert detail["history"]["paid_views"] == 0
