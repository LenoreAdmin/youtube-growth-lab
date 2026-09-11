from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from app.growth import window, enrich, assess, WINDOWS
from app.models import GrowthAssessment, Snapshot, Forecast, IngestCursor, Report, utcnow
from app import pipeline

NOW = datetime(2026,9,11,tzinfo=timezone.utc)

def points(hours=1500, rate=10):
    return [Row(observed_at=NOW-timedelta(hours=hours-i), views=i*rate) for i in range(hours+1)]

@pytest.mark.parametrize("hours", WINDOWS.values())
def test_all_windows_and_units(hours):
    value=window(points(),hours)
    assert value["velocity"] == 10
    assert value["delta_views"] == hours*10
    assert value["acceleration"] == 0


def test_jitter_uses_elapsed_time_not_nominal_hour():
    rows=[Row(observed_at=NOW-timedelta(minutes=65),views=0),Row(observed_at=NOW,views=65)]
    assert window(rows,1)["velocity"] == pytest.approx(60)


def test_gaps_corrections_and_conflicting_duplicates_are_unknown():
    assert window([points()[0],points()[-1]],720)["velocity"] is None
    rows=points(48)
    rows[30].views=0
    assert window(rows,24)["quality"] == "counter_correction"
    rows=points(48)
    rows.append(Row(observed_at=NOW,views=1))
    assert window(rows,24)["quality"] == "conflicting_snapshots"


def test_no_future_data_in_historical_window():
    rows=points(48)
    expected=window(rows,24,end=NOW-timedelta(days=1))
    rows.append(Row(observed_at=NOW+timedelta(days=1),views=999999))
    assert window(rows,24,end=NOW-timedelta(days=1)) == expected


def test_missing_metrics_not_zero_score_and_breakout_requires_repetition():
    current=dict(velocity=100,previous_velocity=10,historical_velocities=[10]*10)
    result=assess(current,[])
    assert result["regime"] == "breakout"
    assert result["score"] > 50
    assert set(result["factors"]) == {"velocity","acceleration"}
    current["historical_velocities"]=[10]
    assert assess(current,[])["regime"] != "breakout"


def test_robust_baseline_and_cooling_and_ad_exclusion():
    f=dict(velocity=10,previous_velocity=100,historical_velocities=[10]*10+[1000000])
    result=assess(f,[])
    assert result["baseline"]["velocity"] == 10
    assert result["regime"] == "cooling"
    f["advertising_views_reported"]=1
    assert assess(f,[])["score"] is None
    assert assess(f,[])["regime"] == "unknown"


def test_available_quality_components_contribute_and_stale_is_unknown():
    f=dict(velocity=10,previous_velocity=10,historical_velocities=[10]*10,
           ctr=.1,watchtime_efficiency=.8,subscriber_conversion=.03)
    peer=dict(velocity=10,ctr=.05,watchtime_efficiency=.4,subscriber_conversion=.01)
    assert len(assess(f,[peer])["factors"]) == 5
    assert assess(f,[peer])["score"] > assess(f,[])["score"]
    stale=enrich(f,points(),NOW+timedelta(hours=4))
    assert all(v["velocity"] is None for v in stale["windows"].values())
    assert assess(stale,[])["score"] is None


def test_growth_log_and_forecasts_survive_lifetime_timeout(monkeypatch,session):
    from test_import_loop import FakeYouTube, wire
    wire(monkeypatch,session)
    monkeypatch.setattr(pipeline.settings,"focus_video_id","a")
    now=utcnow()
    for i in range(73):
        session.add(Snapshot(video_id="a",observed_at=now-timedelta(hours=73-i),views=10000+i*100))
    session.commit()
    class LifetimeTimeout(FakeYouTube):
        attempts=0
        def query(self,video,start,end,metrics,dimensions):
            if dimensions == "elapsedVideoTimeRatio" and (end-start).days > 28:
                self.attempts+=1
                # Core work must already be visible in another transaction.
                assert session.scalar(select(func.count()).select_from(Forecast)) == 3
                raise TimeoutError("upstream secret must never be logged")
            return super().query(video,start,end,metrics,dimensions)
    client=LifetimeTimeout()
    result=pipeline.collect(client)
    assert result["status"] == "ok"
    assert client.attempts == 1
    assert "secret" not in str(result)
    assert session.scalar(select(func.count()).select_from(GrowthAssessment)) == 1
    assert session.get(IngestCursor,("a","retention_attempt")) is not None
    pipeline.collect(client)
    assert client.attempts == 1
    assert session.scalar(select(func.count()).select_from(GrowthAssessment)) == 1
    assert session.scalar(select(func.count()).select_from(Forecast)) == 3


def test_optional_budget_exhaustion_does_not_fail_core(monkeypatch,session):
    from test_import_loop import FakeYouTube, wire
    from app.budget import SyncBudgetExceeded
    wire(monkeypatch,session)
    monkeypatch.setattr(pipeline.settings,"focus_video_id","a")
    budget=Mock()
    budget.check.side_effect=SyncBudgetExceeded()
    issues=[]
    pipeline.optional_lifetime(FakeYouTube(),NOW,NOW.date(),budget,issues)
    assert issues == []


def test_optional_transport_is_bounded_and_does_not_change_shared_client(monkeypatch):
    import app.youtube as yt
    client=object.__new__(yt.YouTube)
    client.credentials=Mock()
    client.budget=None
    request=Mock()
    request.execute.return_value={"rows":[],"columnHeaders":[]}
    client.analytics=Mock()
    client.analytics.reports.return_value.query.return_value=request
    transport=Mock()
    factory=Mock(return_value=transport)
    monkeypatch.setattr(yt,"AuthorizedHttp",factory)
    client.query("a",NOW.date(),NOW.date(),"audienceWatchRatio",timeout=5,max_pages=1)
    assert factory.call_args.kwargs["http"].timeout == 5
    request.execute.assert_called_once_with(num_retries=0,http=transport)


def test_delayed_quality_metrics_do_not_zero_growth(session):
    from app.models import Daily, Reach
    for row in points(72):
        session.add(Snapshot(video_id="a",observed_at=row.observed_at,views=row.views))
    session.add(Daily(video_id="a",day=(NOW-timedelta(days=15)).date(),views=100,
        watch_minutes=500,average_duration=300,average_percentage=50,subscribers_gained=2,
        subscribers_lost=0,likes=2,comments=1,content_type="VIDEO",fetched_at=NOW-timedelta(days=10)))
    session.add(Reach(video_id="a",day=(NOW-timedelta(days=15)).date(),impressions=100,ctr=.1,report_id="fixture"))
    session.flush()
    f=next(f for video,_,f in pipeline.latest_features(session,NOW) if video.id=="a")
    assert f["velocity"] == 10
    assert f["ctr"] is None and f["watchtime_efficiency"] is None
    assert f["metric_status"]["ctr"] == "delayed"
    assert assess(f,[])["score"] == 50


def test_logged_v2_forecast_matures_without_rewriting_inputs(monkeypatch,session):
    from test_import_loop import FakeYouTube, wire
    from app.prediction import mature
    from copy import deepcopy
    wire(monkeypatch,session)
    now=utcnow()
    for i in range(73):
        session.add(Snapshot(video_id="a",observed_at=now-timedelta(hours=73-i),views=10000+i*100))
    session.commit()
    assert pipeline.collect(FakeYouTube())["status"] == "ok"
    forecast=session.scalar(select(Forecast).where(Forecast.horizon_hours==24))
    original=deepcopy(forecast.features)
    assert original["growth_version"] == "growth-v2"
    assert "growth_assessment" in original
    session.add(Snapshot(video_id="a",observed_at=forecast.target_at,views=22000))
    session.flush()
    mature(session,forecast.target_at)
    assert forecast.actual_views == 22000
    assert forecast.absolute_error is not None
    assert forecast.features == original
