"""Historical backfill: real API history only, resumable cursors, no duplicates, no rewrites."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from googleapiclient.errors import HttpError
from app import backfill, jobs, pipeline
from app.budget import SyncBudgetExceeded
from app.models import (Video, Daily, Snapshot, TrafficDaily, BackfillReport, BackfillProgress, BackfillRun,
                        Forecast, Report, IngestCursor, utcnow)


class HistoryYouTube:
    """Deterministic Analytics fixture: one row per day, three traffic sources, monthly retention."""
    def __init__(self, paid_days=(), fail=None):
        self.calls = []
        self.paid_days = set(paid_days)
        self.fail = fail

    def query(self, video, start, end, metrics, dimensions):
        self.calls.append((video, start, end, dimensions))
        if self.fail:
            raise self.fail
        days = [start+timedelta(days=i) for i in range((end-start).days+1)]
        if dimensions == "day":
            return [{"day": str(d), "views": 10, "estimatedMinutesWatched": 5.0, "averageViewDuration": 30.0,
                     "averageViewPercentage": 50.0, "subscribersGained": 1, "subscribersLost": 0, "likes": 1, "comments": 0} for d in days]
        if dimensions == "day,insightTrafficSourceType":
            rows = []
            for d in days:
                sources = [("YT_SEARCH", 6), ("RELATED_VIDEO", 4)]
                if d in self.paid_days:
                    sources = [("YT_SEARCH", 5), ("RELATED_VIDEO", 4), ("ADVERTISING", 1)]
                rows += [{"day": str(d), "insightTrafficSourceType": s, "views": v, "estimatedMinutesWatched": v/2} for s, v in sources]
            return rows
        if dimensions == "elapsedVideoTimeRatio":
            return [{"elapsedVideoTimeRatio": x/10, "audienceWatchRatio": 1-x/20, "relativeRetentionPerformance": .5} for x in range(11)]
        raise AssertionError(dimensions)

    def reach_reports(self):
        return []


def wire(monkeypatch, session, published=None, synced=True):
    factory = sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(backfill, "Session", factory)
    monkeypatch.setattr(pipeline, "Session", factory)
    monkeypatch.setattr(backfill.settings, "analytics_lag_days", 3)
    for v in session.scalars(select(Video)):
        if published:
            v.published_at = published
        if synced:
            # The hourly sync has processed the video: its cursor marks the window it keeps rewriting.
            session.merge(IngestCursor(video_id=v.id, kind="daily", through=backfill.analytics_end(utcnow())))
    session.commit()
    return factory


def sync_boundary():
    return backfill.analytics_end(utcnow())-timedelta(days=backfill.SYNC_MARGIN)


def http_error(status, reason=b'{"error":{"message":"quota exceeded"}}'):
    return HttpError(Mock(status=status, reason="err"), reason)


def test_backfill_imports_history_and_is_idempotent(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 5, 20, 12, tzinfo=timezone.utc))
    client = HistoryYouTube(paid_days=[date(2026, 6, 3)])
    result = backfill.run(client)
    assert result["status"] == "ok"
    end = backfill.analytics_end(utcnow())
    calls = len(client.calls)
    session.expire_all()
    daily = list(session.scalars(select(Daily).where(Daily.video_id == "a")))
    assert daily and min(d.day for d in daily) == date(2026, 5, 20) and max(d.day for d in daily) == sync_boundary()
    assert all(d.content_type == "UNKNOWN" for d in daily)
    paid = list(session.scalars(select(TrafficDaily).where(TrafficDaily.paid.is_(True))))
    assert {(r.video_id, r.day, r.source) for r in paid} == {("a", date(2026, 6, 3), "ADVERTISING"), ("b", date(2026, 6, 3), "ADVERTISING")}
    months = list(session.scalars(select(BackfillReport).where(BackfillReport.video_id == "a")))
    assert [(m.start, m.end) for m in months][:2] == [(date(2026, 5, 20), date(2026, 5, 31)), (date(2026, 6, 1), date(2026, 6, 30))]
    assert all(m.end <= end for m in months)
    progress = {(p.video_id, p.kind): p for p in session.scalars(select(BackfillProgress))}
    assert all(p.status == "complete" for p in progress.values()) and len(progress) == 6
    assert progress[("a", "traffic")].through == end
    # Second run: nothing new to fetch, nothing duplicated, no API calls.
    assert backfill.run(client)["status"] == "ok"
    assert len(client.calls) == calls
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Daily)) == len(daily)*2
    assert session.scalar(select(func.count()).select_from(BackfillReport)) == len(months)*2
    run = session.scalar(select(BackfillRun).order_by(BackfillRun.id.desc()))
    assert run.status == "ok" and run.finished_at is not None
    summary = backfill.summary(session)
    assert summary["complete"] is True
    assert summary["coverage"]["paid_days"] == 2
    assert summary["coverage"]["daily_first"] == date(2026, 5, 20)


def test_backfill_never_overwrites_existing_daily_rows(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    earlier = utcnow()-timedelta(days=10)
    session.add(Daily(video_id="a", day=date(2026, 8, 1), views=999, watch_minutes=1, average_duration=1,
        average_percentage=1, subscribers_gained=0, subscribers_lost=0, likes=0, comments=0, content_type="VIDEO", fetched_at=earlier))
    session.commit()
    client = HistoryYouTube()
    assert backfill.run(client)["status"] == "ok"
    session.expire_all()
    kept = session.get(Daily, ("a", date(2026, 8, 1)))
    assert kept.views == 999 and kept.fetched_at.replace(tzinfo=timezone.utc) == earlier.replace(microsecond=kept.fetched_at.microsecond)
    days = sorted(d.day for d in session.scalars(select(Daily).where(Daily.video_id == "a")))
    # Only the gap before the earliest sync-owned row was filled; later days belong to the hourly sync.
    assert days[0] == date(2026, 7, 1) and days[-1] == date(2026, 8, 1)
    assert all(d.content_type == "VIDEO" for d in session.scalars(select(Daily).where(Daily.video_id == "a")))
    assert not any(c[3] == "day" and c[1] > min(date(2026, 7, 31), sync_boundary()) for c in client.calls)


def test_daily_stage_never_enters_the_sync_window_or_unsynced_videos(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), synced=False)
    client = HistoryYouTube()
    assert backfill.run(client)["status"] == "ok"
    session.expire_all()
    waiting = session.get(BackfillProgress, ("a", "daily"))
    assert waiting.status == "pending" and "Sync" in waiting.note
    assert not any(c[3] == "day" for c in client.calls)
    assert session.scalar(select(func.count()).select_from(Daily)) == 0
    assert backfill.summary(session)["complete"] is False
    # Once the sync owns a cursor, only days at least SYNC_MARGIN below it are backfilled.
    end = backfill.analytics_end(utcnow())
    session.merge(IngestCursor(video_id="a", kind="daily", through=end))
    session.commit()
    assert backfill.run(HistoryYouTube())["status"] == "ok"
    session.expire_all()
    assert session.scalar(select(func.max(Daily.day)).where(Daily.video_id == "a")) == end-timedelta(days=backfill.SYNC_MARGIN)
    assert backfill.sync_window_start(session, "a", date(2026, 1, 1)) == end-timedelta(days=30)


def test_concurrent_sync_insert_never_breaks_backfill_and_is_not_overwritten(monkeypatch, session):
    """A row committed by another writer mid-chunk is left untouched; the run still completes."""
    factory = wire(monkeypatch, session, published=datetime(2026, 4, 1, 12, tzinfo=timezone.utc))
    clash = date(2026, 4, 10)
    class RacingSync(HistoryYouTube):
        def query(self, video, start, end, metrics, dimensions):
            rows = super().query(video, start, end, metrics, dimensions)
            if dimensions == "day" and video == "a" and start <= clash <= end:
                with factory.begin() as other:
                    other.add(Daily(video_id="a", day=clash, views=777, watch_minutes=1, average_duration=1, average_percentage=1,
                        subscribers_gained=0, subscribers_lost=0, likes=0, comments=0, content_type="VIDEO", fetched_at=utcnow()))
            return rows
    result = backfill.run(RacingSync())
    assert result["status"] == "ok" and result["issues"] == []
    session.expire_all()
    assert session.get(Daily, ("a", clash)).views == 777
    assert session.get(BackfillProgress, ("a", "daily")).status == "complete"
    days = [d.day for d in session.scalars(select(Daily).where(Daily.video_id == "a"))]
    assert len(days) == len(set(days)) and clash in days
    assert result["stats"]["daily_rows"] == session.scalar(select(func.count()).select_from(Daily))-1
    # Re-running upserts traffic/retention atomically without duplicates.
    before = (session.scalar(select(func.count()).select_from(TrafficDaily)), session.scalar(select(func.count()).select_from(BackfillReport)))
    for row in session.scalars(select(BackfillProgress).where(BackfillProgress.kind != "daily")):
        row.through = None
        row.status = "pending"
    session.commit()
    assert backfill.run(HistoryYouTube())["status"] == "ok"
    session.expire_all()
    assert (session.scalar(select(func.count()).select_from(TrafficDaily)), session.scalar(select(func.count()).select_from(BackfillReport))) == before


def test_upserts_compile_to_postgresql_on_conflict():
    from sqlalchemy.dialects import postgresql
    from types import SimpleNamespace
    fake = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    daily = backfill.upsert(fake, Daily).values(video_id="a", day=date(2026, 1, 1), views=1, watch_minutes=1, average_duration=1,
        average_percentage=1, subscribers_gained=0, subscribers_lost=0, likes=0, comments=0, content_type="VIDEO", fetched_at=utcnow()
        ).on_conflict_do_nothing(index_elements=["video_id", "day"])
    assert "ON CONFLICT (video_id, day) DO NOTHING" in str(daily.compile(dialect=postgresql.dialect()))
    traffic = backfill.upsert(fake, TrafficDaily).values(video_id="a", day=date(2026, 1, 1), source="YT_SEARCH", views=1, watch_minutes=1, paid=False, fetched_at=utcnow())
    sql = str(traffic.on_conflict_do_update(index_elements=["video_id", "day", "source"], set_={"views": traffic.excluded.views}).compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (video_id, day, source) DO UPDATE SET views = excluded.views" in sql


def test_budget_interruption_resumes_from_cursor_without_duplicates(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2025, 1, 1, 12, tzinfo=timezone.utc))
    class Interrupt(HistoryYouTube):
        def query(self, *args):
            if len(self.calls) == 3:
                raise SyncBudgetExceeded()
            return super().query(*args)
    client = Interrupt()
    first = backfill.run(client)
    assert first["status"] == "deferred"
    session.expire_all()
    progress = session.get(BackfillProgress, ("a", "daily"))
    assert progress.status == "partial" and progress.through is not None
    resumed_from = progress.through
    before = session.scalar(select(func.count()).select_from(Daily))
    assert before > 0
    second = HistoryYouTube()
    assert backfill.run(second)["status"] == "ok"
    assert second.calls[0][1] == resumed_from+timedelta(days=1)
    session.expire_all()
    days = [d.day for d in session.scalars(select(Daily).where(Daily.video_id == "a"))]
    assert len(days) == len(set(days))
    assert session.get(BackfillProgress, ("a", "daily")).status == "complete"


def test_throttling_aborts_run_without_marking_errors(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    result = backfill.run(HistoryYouTube(fail=http_error(429)))
    assert result["status"] == "throttled"
    assert "reach_reports" not in result["stats"]
    session.expire_all()
    rows = list(session.scalars(select(BackfillProgress)))
    assert rows and all(r.status == "partial" and r.attempts == 0 for r in rows if r.kind != "daily")
    assert backfill.classify(http_error(403, b'{"error":{"message":"Quota exceeded"}}')) == "throttled"
    assert backfill.classify(http_error(503)) == "throttled"
    assert backfill.classify(http_error(400, b"bad")) == "unsupported"
    assert backfill.classify(TimeoutError()) == "transient"


def test_transient_errors_count_attempts_and_retry_failed_resets(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    for _ in range(backfill.MAX_ATTEMPTS):
        assert backfill.run(HistoryYouTube(fail=TimeoutError()))["status"] == "partial"
    session.expire_all()
    stuck = session.get(BackfillProgress, ("a", "traffic"))
    assert stuck.status == "error" and stuck.attempts == backfill.MAX_ATTEMPTS and "transient" in stuck.note
    skipped = HistoryYouTube()
    assert backfill.run(skipped)["status"] == "ok"
    assert skipped.calls == []
    assert backfill.run(HistoryYouTube(), retry_failed=True)["status"] == "ok"
    session.expire_all()
    assert session.get(BackfillProgress, ("a", "traffic")).status == "complete"


def test_backfill_lease_does_not_block_hourly_sync(monkeypatch, session):
    factory = wire(monkeypatch, session)
    owner, _ = jobs.acquire(factory, None, backfill.LEASE)
    try:
        assert backfill.run(HistoryYouTube())["status"] == "already_running"
        sync_owner, skipped = jobs.acquire(factory, "hour")
        assert sync_owner and skipped is None
        jobs.release(factory, sync_owner, "hour")
    finally:
        jobs.release(factory, owner, None, backfill.LEASE)


def test_no_snapshots_are_created_by_backfill(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    backfill.run(HistoryYouTube())
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Snapshot)) == 0


def test_month_windows_are_complete_pacific_months():
    windows = list(backfill.month_windows(date(2026, 1, 15), date(2026, 3, 30)))
    assert windows == [(date(2026, 1, 15), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28))]
    assert list(backfill.month_windows(date(2026, 3, 1), date(2026, 3, 30))) == []


def forecast(session, origin, actual_at, views=100):
    f = Forecast(video_id="a", origin_at=origin, target_at=actual_at, horizon_hours=24, origin_views=views,
        predicted_views=views+10, lower_views=views, upper_views=views+40, features={"velocity": 1},
        model_version="test", calibration_n=0, actual_views=views+12, actual_at=actual_at, absolute_error=2)
    session.add(f)
    session.flush()
    return f


def add_daily(session, start, end, fetched_at, views=10):
    for i in range((end-start).days+1):
        session.add(Daily(video_id="a", day=start+timedelta(days=i), views=views, watch_minutes=5, average_duration=30,
            average_percentage=50, subscribers_gained=0, subscribers_lost=0, likes=0, comments=0, fetched_at=fetched_at))


def test_backfilled_daily_traffic_closes_coverage_gap_and_keeps_paid_excluded(monkeypatch, session):
    from app.organic import eligibility
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
    f = forecast(session, now-timedelta(days=2), now-timedelta(days=1))
    start = backfill.pacific_day(f.origin_at)-timedelta(days=32)
    end = backfill.pacific_day(f.actual_at)
    fetched = now-timedelta(days=40)
    add_daily(session, start, end, fetched)
    # Sliding report covers all but the two oldest days, exactly the gap after a first deployment.
    report_start = start+timedelta(days=2)
    session.add(Report(video_id="a", kind="traffic", start=report_start, end=end, fetched_at=fetched,
        rows=[{"insightTrafficSourceType": "YT_SEARCH", "views": ((end-report_start).days+1)*10}]))
    session.flush()
    assert eligibility(session, f, now)["status"] == "insufficient_traffic_coverage"
    for d in (start, start+timedelta(days=1)):
        session.add(TrafficDaily(video_id="a", day=d, source="YT_SEARCH", views=6, watch_minutes=1, paid=False, fetched_at=fetched))
        session.add(TrafficDaily(video_id="a", day=d, source="RELATED_VIDEO", views=4, watch_minutes=1, paid=False, fetched_at=fetched))
    session.flush()
    # Rows without a cursor that reached those days are not evidence.
    assert eligibility(session, f, now)["status"] == "insufficient_traffic_coverage"
    session.add(BackfillProgress(video_id="a", kind="traffic", first=start, target=end, through=start, status="partial", attempts=0))
    session.flush()
    assert eligibility(session, f, now)["status"] == "insufficient_traffic_coverage"
    progress = session.get(BackfillProgress, ("a", "traffic"))
    progress.through = end
    session.flush()
    result = eligibility(session, f, now)
    assert result["status"] == "eligible_organic" and result["daily_traffic_days"] == 2
    # As-of semantics: history fetched after the cutoff cannot verify earlier decisions.
    assert eligibility(session, f, fetched-timedelta(days=1))["status"] == "insufficient_traffic_coverage"
    # Totals must match the daily analytics for each day.
    session.get(TrafficDaily, ("a", start, "YT_SEARCH")).views = 5
    session.flush()
    assert eligibility(session, f, now)["status"] == "insufficient_traffic_coverage"
    session.get(TrafficDaily, ("a", start, "YT_SEARCH")).views = 6
    session.add(TrafficDaily(video_id="a", day=start+timedelta(days=5), source="ADVERTISING", views=1, watch_minutes=0, paid=True, fetched_at=fetched))
    session.flush()
    assert eligibility(session, f, now)["status"] == "paid_excluded"


def test_video_history_reports_sources_and_months(monkeypatch, session):
    wire(monkeypatch, session, published=datetime(2026, 6, 10, 12, tzinfo=timezone.utc))
    backfill.run(HistoryYouTube(paid_days=[date(2026, 6, 20)]))
    session.expire_all()
    history = backfill.video_history(session, "a")
    assert history["paid_views"] == 1
    assert [t["source"] for t in history["traffic_sources"]][:2] == ["YT_SEARCH", "RELATED_VIDEO"]
    assert history["retention_months"][0]["start"] == date(2026, 6, 10)
    assert abs(history["retention_months"][0]["average_watch_ratio"]-0.75) < 1e-9
    assert {p["kind"]: p["status"] for p in history["progress"]} == {k: "complete" for k in backfill.KINDS}
