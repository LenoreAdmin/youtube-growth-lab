from datetime import timedelta
from app.models import Snapshot, Forecast, utcnow
from app.prediction import predict, mature

def feature(v=10):
    return {"velocity":v,"acceleration":0,"subscriber_conversion":None,"watchtime_efficiency":None}

def test_cold_start_abstains_from_probabilities(session):
    p=predict(session,"a",Snapshot(views=20000,observed_at=utcnow()),feature(),24)
    assert p.predicted_views == 20240
    assert p.p100k is None and p.p1m is None
    assert p.target_at > p.origin_at

def test_evaluation_uses_only_close_future_snapshot(session):
    now=utcnow()
    p=predict(session,"a",Snapshot(views=100,observed_at=now-timedelta(hours=24)),feature(),24)
    session.add(p)
    session.add(Snapshot(video_id="a",observed_at=now+timedelta(hours=4),views=400))
    session.flush()
    mature(session,now+timedelta(hours=5))
    assert p.actual_views is None
    session.add(Snapshot(video_id="a",observed_at=now+timedelta(hours=1),views=350))
    session.flush()
    mature(session,now+timedelta(hours=5))
    assert p.actual_views == 350
    assert p.absolute_error == 10

def test_feedback_adjusts_future_forecasts(session):
    now=utcnow()
    for i in range(12):
        origin=now-timedelta(days=30-i*2)
        p=predict(session,"a",Snapshot(views=100,observed_at=origin),feature(),24)
        p.actual_views=220
        p.actual_at=origin+timedelta(hours=24)
        session.add(p)
        session.flush()
    result=predict(session,"a",Snapshot(views=20000,observed_at=now),feature(),24)
    assert result.model_version == "adaptive-velocity-v1"
    assert result.predicted_views < 20240

def test_future_labels_cannot_leak(session):
    now=utcnow()
    for i in range(12):
        p=predict(session,"a",Snapshot(views=100,observed_at=now-timedelta(days=i+2)),feature(),24)
        p.actual_views=10000
        p.actual_at=now+timedelta(days=1)
        session.add(p)
    session.flush()
    result=predict(session,"a",Snapshot(views=100,observed_at=now),feature(),24)
    assert result.model_version == "velocity-v1"

def test_hourly_forecasts_do_not_inflate_independent_sample_count(session):
    now=utcnow()
    for i in range(20):
        p=predict(session,"a",Snapshot(views=100,observed_at=now-timedelta(hours=50-i)),feature(),24)
        p.actual_views=220
        p.actual_at=p.target_at
        session.add(p)
    session.flush()
    result=predict(session,"a",Snapshot(views=100,observed_at=now),feature(),24)
    assert result.model_version == "velocity-v1"


def test_ridge_challenger_is_validated_before_selection(session):
    from app.models import Forecast, ModelRun
    from sqlalchemy import select
    now=utcnow()
    for i in range(70):
        origin=now-timedelta(days=160-i*2)
        v=10+i%15
        session.add(Forecast(video_id="a",origin_at=origin,target_at=origin+timedelta(hours=24),
            horizon_hours=24,origin_views=100,predicted_views=100+v*24,lower_views=100,upper_views=100+v*48,
            p100k=None,p1m=None,features=feature(v),model_version="velocity-v1",calibration_n=0,
            actual_views=100+v*v*2,actual_at=origin+timedelta(hours=24),absolute_error=abs(v*v*2-v*24)))
    session.flush()
    result=predict(session,"a",Snapshot(views=100,observed_at=now),feature(20),24)
    assert result.model_version == "ridge-v1"
    run=session.scalar(select(ModelRun))
    assert run.metrics["accepted"] is True
    assert run.metrics["mae"] < run.metrics["baseline_mae"]


def test_empirical_probabilities_are_smoothed_and_ordered(session):
    from app.models import Forecast
    now=utcnow()
    for i in range(35):
        origin=now-timedelta(days=100-i*2)
        session.add(Forecast(video_id="a",origin_at=origin,target_at=origin+timedelta(hours=24),
            horizon_hours=24,origin_views=100,predicted_views=340,lower_views=100,upper_views=600,
            p100k=None,p1m=None,features=feature(),model_version="adaptive-velocity-v1",calibration_n=30,
            actual_views=350+i,actual_at=origin+timedelta(hours=24),absolute_error=10+i))
    session.flush()
    result=predict(session,"a",Snapshot(views=99700,observed_at=now),feature(),24)
    assert result.calibration_n == 35
    assert result.p1m is None
    assert result.p100k is not None and 0 < result.p100k < 1
