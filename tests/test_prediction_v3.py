from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
import numpy as np
import pytest
from sqlalchemy import select
from app.models import Forecast,PredictionAudit,ModelRun,Video,Daily,Report,Snapshot
from app.prediction_v3 import predict,training_history,feedback,target_probability,VERSION,vector
from app.organic import eligibility
from app.decisions import recommend

NOW=datetime(2026,9,11,12,tzinfo=timezone.utc)

def features(**changes):
    return dict(dict(velocity=10,acceleration=.1,ctr=.05,retention=.5,watchtime_efficiency=.5,
        watch_minutes=100,subscriber_conversion=.02,traffic_search=.2,traffic_suggested=.5,
        traffic_external=.3,age_days=45,content_type="VIDEO",prediction_version=VERSION,
        growth_assessment={"regime":"rising"},metric_status={"analytics":"available","traffic":"available"}),**changes)


def test_cold_start_audits_all_horizons_and_unknown_probabilities(session):
    for h in (24,168,720):
        f=predict(session,"a",Row(observed_at=NOW,views=20000),features(),h)
        session.flush()
        audit=session.get(PredictionAudit,f.id)
        assert f.predicted_views==20000+10*h
        assert f.p100k is None and f.p1m is None
        assert audit.metadata_json["targets"]["100000"]["status"]=="insufficient_data"
        assert audit.metadata_json["confidence"]=="insufficient_data"
        assert audit.recommendations[0]["priority"]==0
        assert session.get(ModelRun,audit.model_run_id).parameters["features"]


def outcome(session):
    f=predict(session,"a",Row(observed_at=NOW-timedelta(days=2),views=100),features(),24)
    f.actual_at=NOW-timedelta(days=1);f.actual_views=200;f.absolute_error=140
    session.flush()
    return f


def add_coverage(session,f,paid=False):
    from zoneinfo import ZoneInfo
    start=(f.origin_at-timedelta(days=32)).astimezone(ZoneInfo("America/Los_Angeles")).date()
    end=f.actual_at.astimezone(ZoneInfo("America/Los_Angeles")).date()
    days=(end-start).days+1
    for i in range(days):
        session.add(Daily(video_id="a",day=start+timedelta(days=i),views=10,watch_minutes=5,
            average_duration=30,average_percentage=50,subscribers_gained=0,subscribers_lost=0,likes=0,comments=0,fetched_at=NOW))
    r=Report(video_id="a",kind="traffic",start=start,end=end,
        rows=[{"insightTrafficSourceType":"ADVERTISING" if paid else "YT_SEARCH","views":days*10}],fetched_at=NOW)
    session.add(r);session.flush();return r


def test_organic_requires_complete_matching_coverage_and_rechecks_revisions(session):
    f=outcome(session)
    assert eligibility(session,f,NOW)["status"]=="insufficient_traffic_coverage"
    report=add_coverage(session,f)
    assert eligibility(session,f,NOW)["status"]=="eligible_organic"
    report.rows=[{"insightTrafficSourceType":"ADVERTISING","views":340}]
    session.flush()
    assert training_history(session,NOW+timedelta(seconds=1),24,"VIDEO")==[]
    feedback(session,NOW)
    assert session.get(PredictionAudit,f.id).feedback["status"]=="paid_excluded"


def test_future_reports_mismatch_and_missing_days_never_verify(session):
    f=outcome(session);r=add_coverage(session,f)
    assert eligibility(session,f,NOW-timedelta(hours=1))["status"]=="insufficient_traffic_coverage"
    r.rows=[{"insightTrafficSourceType":"YT_SEARCH","views":1}]
    session.flush()
    assert eligibility(session,f,NOW)["status"]=="insufficient_traffic_coverage"


def test_counter_corrections_excluded_even_if_ending_counter_recovers(session):
    f=outcome(session);add_coverage(session,f)
    for offset,views in ((0,100),(1,90),(2,200)):
        session.add(Snapshot(video_id="a",observed_at=f.origin_at+timedelta(hours=offset),views=views))
    session.flush()
    assert eligibility(session,f,NOW)["status"]=="counter_correction"


def test_probability_abstains_for_small_samples_and_unsupported_tails():
    assert target_probability(np.array([100000]*10),100000,100,10)["status"]=="insufficient_data"
    assert target_probability(np.array([100000]*30),100000,100,30)["probability"] is None
    result=target_probability(np.array([90000]*15+[110000]*15),100000,100,30)
    assert result["probability"]==.5
    assert result["interval_95"][0]<.5<result["interval_95"][1]


def test_recommendations_are_prioritized_hypotheses_and_regime_sensitive():
    rows=recommend(features(),"empirical_limited",[1])
    assert rows[0]["dimension"]=="content_format"
    assert {r["dimension"] for r in rows}=={"positioning","audience","content_format","title_thumbnail","hook","release"}
    assert all(r["status"]=="testable_hypothesis" for r in rows)
    assert rows[0]["linked_experiment_ids"]==[1]
    assert vector(features()) != vector(features(growth_assessment={"regime":"cooling"}))


def history(session,count=120):
    run=ModelRun(created_at=NOW-timedelta(days=300),horizon_hours=24,training_rows=0,parameters={},metrics={})
    session.add(run);session.flush()
    for i in range(count):
        video=f"hist-{i//4}"
        if i%4==0:
            session.add(Video(id=video,channel_id="channel",title=video,published_at=NOW-timedelta(days=400),duration_seconds=100))
            session.flush()
        start=NOW-timedelta(days=250)+timedelta(hours=i*36)
        ctr=.01*(1+i%7)
        f=Forecast(video_id=video,origin_at=start,target_at=start+timedelta(hours=24),horizon_hours=24,
            origin_views=1000,predicted_views=1240,lower_views=1000,upper_views=2000,p100k=None,p1m=None,
            features=features(ctr=ctr),model_version=VERSION+"/baseline",calibration_n=0,
            actual_at=start+timedelta(hours=24),actual_views=1000+int(np.exp(4+ctr*40)),absolute_error=100)
        session.add(f);session.flush()
        session.add(PredictionAudit(forecast_id=f.id,model_run_id=run.id,created_at=start,metadata_json={},feedback={},recommendations=[]))
    session.flush()


def test_ridge_weights_validation_and_reproduction(monkeypatch,session):
    import app.prediction_v3 as module
    history(session)
    # Provenance itself is integration-tested above; isolate numerical model evaluation here.
    monkeypatch.setattr(module,"eligibility",lambda *args:{"status":"eligible_organic"})
    f=predict(session,"a",Row(observed_at=NOW,views=20000),features(ctr=.04),24)
    session.flush()
    audit=session.get(PredictionAudit,f.id);run=session.get(ModelRun,audit.model_run_id)
    assert run.metrics["accepted"] is True
    assert run.metrics["mae"]<run.metrics["baseline_mae"]*.95
    p=run.parameters
    x=(np.array(vector(f.features))-p["scaler_mean"])/p["scaler_scale"]
    reproduced=max(0,float(np.expm1(np.clip(x@p["coefficients"]+p["intercept"],0,25))))
    assert f.predicted_views==pytest.approx(f.origin_views+reproduced)
    train=[session.get(Forecast,i) for i in run.metrics["training_ids"]]
    validation=[session.get(Forecast,i) for i in run.metrics["validation_ids"]]
    assert not ({r.video_id for r in train}&{r.video_id for r in validation})
    assert max(r.actual_at for r in train)<min(r.origin_at for r in validation)


def test_unverified_history_cannot_change_baseline(session):
    history(session,40)
    f=predict(session,"a",Row(observed_at=NOW,views=20000),features(),24)
    assert f.model_version==VERSION+"/baseline"
    assert f.predicted_views==20240


def test_experiment_registration_links_existing_predictions(session):
    from test_memory import decision
    from app.memory import activate
    f=predict(session,"a",Row(observed_at=NOW,views=20000),features(),24)
    session.add(Snapshot(video_id="a",observed_at=NOW,views=20000));session.flush()
    d=decision(session);activate(session,d,"a",NOW)
    assert d.measurement["forecast_ids"]==[f.id]


def test_overlapping_and_future_outcomes_do_not_multiply_training(monkeypatch,session):
    import app.prediction_v3 as module
    history(session,4)
    monkeypatch.setattr(module,"eligibility",lambda *args:{"status":"eligible_organic"})
    rows=list(session.scalars(select(Forecast).order_by(Forecast.origin_at)))
    rows[1].origin_at=rows[0].origin_at+timedelta(hours=1)
    rows[1].target_at=rows[1].origin_at+timedelta(hours=24)
    rows[2].actual_at=NOW+timedelta(days=1)
    session.flush()
    selected=training_history(session,NOW,24,"VIDEO")
    assert [r.id for r in selected]==[rows[0].id,rows[3].id]
    assert training_history(session,NOW,24,"UNKNOWN")==[]


def test_accepted_model_associations_influence_test_priority_without_causal_claim():
    rows=recommend(features(growth_assessment={"regime":"baseline"}),"empirical_limited",[],{"ctr":2,"velocity":.1})
    title=next(r for r in rows if r["dimension"]=="title_thumbnail")
    assert title["priority"]==1
    assert title["model_association"]==2
    assert title["status"]=="testable_hypothesis"
    assert "keine kausale" in title["guardrail"]
