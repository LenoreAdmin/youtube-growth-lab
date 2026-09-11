from datetime import timedelta
from unittest.mock import Mock
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from pydantic import ValidationError
from app.config import Settings, settings
from app.models import JobLease, ImportedReport, Snapshot, IngestCursor, utcnow
from app import jobs, pipeline
from app.budget import Budget, SyncBudgetExceeded


def configured(**values):
    return Settings(_env_file=None, **{
        "app_env": "production", "database_url": "postgresql://user:placeholder@db/growth",
        "app_token": "a"*40, "cron_secret": "b"*40, **values})


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///tmp/test.db", "sqlite:////tmp/prod.db"])
def test_no_sqlite_in_production(url):
    with pytest.raises(ValidationError, match="PostgreSQL"):
        configured(database_url=url)


def test_preview_is_also_hosted():
    with pytest.raises(ValidationError):
        configured(app_env="development", vercel_env="preview", database_url="sqlite://")


def test_postgres_url_normalized_and_tls_added():
    value = configured()
    assert value.database_url.startswith("postgresql+psycopg://")
    assert "sslmode=require" in value.database_url


def test_secrets_must_be_distinct_and_errors_hide_inputs():
    secret = "never-show-this-value"*3
    with pytest.raises(ValidationError) as error:
        configured(app_token=secret, cron_secret=secret)
    assert secret not in str(error.value)


def test_lease_duplicate_overlap_and_expired_recovery(session):
    factory = sessionmaker(session.bind, expire_on_commit=False)
    owner, skipped = jobs.acquire(factory, "hour-a")
    assert owner and skipped is None
    assert jobs.acquire(factory, "hour-b")[1] == "already_running"
    jobs.release(factory, owner, "hour-a")
    assert jobs.acquire(factory, "hour-a")[1] == "already_completed"
    second, _ = jobs.acquire(factory, "hour-b")
    jobs.release(factory, owner, "hour-a")  # Old owner cannot release the new lease.
    with factory.begin() as s:
        lease = s.get(JobLease, "youtube-sync")
        assert lease.owner == second
        lease.expires_at = utcnow()-timedelta(minutes=1)
    recovered, _ = jobs.acquire(factory, "hour-c")
    assert recovered and recovered != second


def test_fourteen_days_of_hourly_buckets_use_persistent_state(session):
    factory = sessionmaker(session.bind, expire_on_commit=False)
    for hour in range(14*24):
        bucket = str(hour)
        owner, skipped = jobs.acquire(factory, bucket)
        assert owner and skipped is None
        jobs.release(factory, owner, bucket)
        assert jobs.acquire(factory, bucket)[1] == "already_completed"


def test_cron_requires_separate_secret_and_rejects_preview(monkeypatch):
    import app.main as main
    monkeypatch.setattr(settings, "cron_secret", "cron-"+"x"*40)
    monkeypatch.setattr(settings, "app_token", "dashboard-token")
    collect = Mock(return_value={"status":"ok"})
    monkeypatch.setattr(main, "collect", collect)
    with TestClient(main.app) as client:
        assert client.get("/api/cron/sync").status_code == 401
        assert client.get("/api/cron/sync",headers={"Authorization":"Bearer dashboard-token"}).status_code == 401
        assert client.get("/api/cron/sync?secret="+settings.cron_secret).status_code == 401
        assert collect.call_count == 0
        response = client.get("/api/cron/sync",headers={"Authorization":"Bearer "+settings.cron_secret})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert collect.call_count == 1
        assert "bucket" in collect.call_args.kwargs
        monkeypatch.setattr(settings, "vercel_env", "preview")
        assert client.get("/api/cron/sync",headers={"Authorization":"Bearer "+settings.cron_secret}).status_code == 403
        assert collect.call_count == 1


@pytest.mark.parametrize("status,expected", [("failed",503),("partial",503),("deferred",200),("already_completed",200)])
def test_cron_statuses(monkeypatch,status,expected):
    import app.main as main
    monkeypatch.setattr(settings,"cron_secret","c"*40)
    monkeypatch.setattr(main,"collect",lambda **kwargs: {"status":status})
    with TestClient(main.app) as client:
        assert client.get("/api/cron/sync",headers={"Authorization":"Bearer "+"c"*40}).status_code == expected


def test_oauth_from_environment_never_reads_files(monkeypatch):
    import app.youtube as youtube
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "google_refresh_token", "test-refresh")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(youtube, "build", lambda *args, **kwargs: object())
    monkeypatch.setattr(youtube.Credentials, "from_authorized_user_file",
                        Mock(side_effect=AssertionError("No files on Vercel")))
    client = youtube.YouTube()
    assert client.credentials.refresh_token == "test-refresh"
    assert client.credentials.token is None
    # Simulate Google's refresh response. Only access token changes, no file writes.
    response = Mock(status=200, data=json.dumps({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}).encode())
    client.credentials.refresh(Mock(return_value=response))
    assert client.credentials.token == "test-access"
    assert client.credentials.refresh_token == "test-refresh"


def test_missing_hosted_oauth_fails_closed(monkeypatch):
    import app.youtube as youtube
    monkeypatch.setattr(settings, "app_env", "production")
    for key in ("google_client_id","google_client_secret","google_refresh_token"):
        monkeypatch.setattr(settings,key,"")
    with pytest.raises(ValueError,match="environment"):
        youtube.YouTube()


def test_budget_checkpoint_survives_and_resumes(monkeypatch,session):
    from test_import_loop import FakeYouTube, wire
    wire(monkeypatch,session)
    class InterruptOnce(FakeYouTube):
        def query(self, video, start, end, metrics, dimensions):
            if dimensions == "creatorContentType":
                raise SyncBudgetExceeded()
            return super().query(video,start,end,metrics,dimensions)
    result = pipeline.collect(InterruptOnce(),bucket="first-hour")
    assert result["status"] == "deferred"
    session.expire_all()
    assert session.scalar(select(Snapshot)) is not None
    assert session.get(IngestCursor,("a","daily")) is not None
    assert pipeline.collect(FakeYouTube(),bucket="second-hour")["status"] == "ok"


def test_budget_raises_before_last_network_request(monkeypatch):
    import app.budget as budget
    monkeypatch.setattr(budget.time,"monotonic",lambda:100)
    clock = Budget(30)
    monkeypatch.setattr(budget.time,"monotonic",lambda:110)
    with pytest.raises(SyncBudgetExceeded):
        clock.check()


def test_imported_reach_is_not_downloaded_again(session):
    from test_pipeline import FakeReach
    client=FakeReach()
    client.download_reach=Mock(wraps=client.download_reach)
    pipeline.ingest_reach(session,client,[])
    pipeline.ingest_reach(session,client,[])
    assert client.download_reach.call_count == 1
    assert session.get(ImportedReport,"r1") is not None


def test_production_build_uses_migration_url_but_preview_does_not(monkeypatch):
    import app.release as release
    migrate=Mock()
    verify=Mock()
    monkeypatch.setattr(release,"migrate",migrate)
    monkeypatch.setattr(release,"check_schema",verify)
    monkeypatch.setattr(settings,"vercel_env","preview")
    release.build()
    migrate.assert_not_called()
    monkeypatch.setattr(settings,"vercel_env","production")
    monkeypatch.setattr(settings,"migration_database_url","")
    with pytest.raises(ValueError):
        release.build()
    monkeypatch.setattr(settings,"migration_database_url","postgresql://placeholder")
    for key in ("google_client_id","google_client_secret","google_refresh_token","channel_id"):
        monkeypatch.setattr(settings,key,"test-placeholder")
    release.build()
    migrate.assert_called_once_with("postgresql://placeholder",hosted=True)
    verify.assert_called_once()


def test_vercel_config_matches_route_budget_and_entrypoint():
    from pathlib import Path
    import tomllib
    value=json.loads(Path("vercel.json").read_text(encoding="utf-8-sig"))
    assert value["crons"] == [{"path":"/api/cron/sync","schedule":"0 * * * *"}]
    assert value["functions"]["app/main.py"]["maxDuration"] == 300
    project=tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8-sig"))
    assert project["tool"]["vercel"]["entrypoint"] == "app.main:app"
    assert Settings(_env_file=None).sync_budget_seconds < 300


def test_hosted_build_cannot_silently_skip_migrations_without_environment(monkeypatch):
    from app.release import build
    monkeypatch.setattr(settings, "vercel", "1")
    monkeypatch.setattr(settings, "vercel_env", "")
    with pytest.raises(ValueError, match="VERCEL_ENV"):
        build()


def test_release_migration_preserves_existing_data(tmp_path):
    from sqlalchemy import create_engine, text
    from app.release import migrate, check_schema
    url='sqlite:///'+(tmp_path/'migration-test.db').as_posix()
    migrate(url)
    engine=create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO channels (id,title,updated_at) VALUES ('keep','preserve',CURRENT_TIMESTAMP)"))
    migrate(url)
    check_schema(url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT title FROM channels WHERE id='keep'")) == 'preserve'
    engine.dispose()


def test_unicode_bad_cron_token_is_rejected_not_crashed(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi import HTTPException
    from app.main import cron_authenticate
    monkeypatch.setattr(settings,'cron_secret','x'*40)
    with pytest.raises(HTTPException) as error:
        cron_authenticate(HTTPAuthorizationCredentials(scheme='Bearer',credentials='ungültig'))
    assert error.value.status_code == 401
