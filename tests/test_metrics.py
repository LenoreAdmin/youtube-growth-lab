from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
import pytest
from app.metrics import velocity, momentum, features

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
def points(values, hours=24):
    return [Row(observed_at=NOW+timedelta(hours=i*hours), views=v) for i,v in enumerate(values)]

def test_acceleration_and_velocity_units():
    f=velocity(points([100,340,820]))
    assert f["velocity"] == 20
    assert f["acceleration"] == pytest.approx(10/24)

def test_correction_is_not_negative_growth():
    assert velocity(points([100,90,150]))["velocity"] is None

def test_internal_correction_detected():
    p=points([100,300,290,400,800],12)
    assert velocity(p)["velocity"] is None

def test_insufficient_data_is_unknown():
    assert velocity(points([100,120]))["velocity"] is None

def test_outage_is_not_daily_velocity():
    assert velocity(points([100,120,180],100))["velocity"] is None

def test_zero_views_conversion_unknown():
    f=features(Row(duration_seconds=600),points([0,0,0]),[])
    assert f["subscriber_conversion"] is None
    assert f["watchtime_efficiency"] is None

def test_weighted_efficiency_and_conversion():
    d=[Row(views=100,watch_minutes=500,subscribers_gained=3,subscribers_lost=1,content_type="VIDEO")]
    f=features(Row(duration_seconds=600),points([0,100,200]),d)
    assert f["watchtime_efficiency"] == .5
    assert f["subscriber_conversion"] == .03

def test_score_no_peers_does_not_invent_baseline():
    f=velocity(points([0,100,300]))
    f["watchtime_efficiency"]=None
    m=momentum(f,[])
    assert m["relative_performance"] is None
    assert m["direction"] == "Gewinnt"
    assert 0 <= m["score"] <= 100
