from datetime import timedelta
import pytest
from app.memory import DecisionInput, create_decision, activate, evaluate_memory, evidence
from app.models import Snapshot, utcnow

def decision(session):
    return create_decision(session, DecisionInput(hypothesis="Klarer Hook erhöht Bindung",
        content_strategy="Tutorial",title_strategy="Konkretes Ergebnis",thumbnail_strategy="Ein Motiv",
        hook_strategy="Ergebnis zuerst",audience="Einsteiger",expected_views_gain=100,horizon_hours=24))

def test_draft_can_exist_before_video_upload(session):
    row=decision(session)
    assert row.video_id is None
    assert row.status == "draft"

def test_activation_freezes_registration(session):
    now=utcnow()
    session.add(Snapshot(video_id="a",observed_at=now,views=100))
    session.flush()
    row=decision(session)
    activate(session,row,"a",now)
    with pytest.raises(ValueError):
        activate(session,row,"a",now)

def test_failed_experiment_is_saved_and_not_proven(session):
    now=utcnow()
    session.add(Snapshot(video_id="a",observed_at=now,views=100))
    session.flush()
    row=decision(session)
    activate(session,row,"a",now)
    session.add(Snapshot(video_id="a",observed_at=now+timedelta(hours=24),views=150))
    session.flush()
    evaluate_memory(session,now+timedelta(hours=25))
    assert row.actual_result["views_gain"] == 50
    assert row.deviation == -50
    assert row.next_hypothesis
    assert evidence(session,row.strategy_key)["status"] == "Unentschieden"

def test_single_controlled_success_is_not_proof(session):
    row=decision(session)
    row.video_id="a"
    row.control_video_id="b"
    row.design="matched_control"
    row.status="evaluated"
    row.evaluated_at=utcnow()
    row.actual_result={"control_adjusted_residual":1}
    session.flush()
    e=evidence(session,row.strategy_key)
    assert e["n"] == 1
    assert e["status"] == "Unentschieden"
    assert e["confidence"] < .95

def test_failure_lowers_future_strategy_feature(session):
    row=decision(session)
    row.video_id="a"
    row.control_video_id="b"
    row.design="matched_control"
    row.status="evaluated"
    row.evaluated_at=utcnow()
    row.actual_result={"control_adjusted_residual":-1}
    session.flush()
    assert evidence(session,row.strategy_key)["model_feature"] < 0

def test_repeated_same_video_not_independent(session):
    for _ in range(12):
        row=decision(session)
        row.video_id="a"
        row.control_video_id="b"
        row.design="matched_control"
        row.status="evaluated"
        row.evaluated_at=utcnow()
        row.actual_result={"control_adjusted_residual":1}
    session.flush()
    assert evidence(session,row.strategy_key)["n"] == 1


def test_matched_control_evaluates_against_registered_comparison(session):
    from app.models import Daily
    from app.prediction import predict
    now=utcnow()
    for video_id in ("a","b"):
        for days,views in ((2,80),(1,90),(0,100)):
            session.add(Snapshot(video_id=video_id,observed_at=now-timedelta(days=days),views=views))
        session.add(Daily(video_id=video_id,day=(now-timedelta(days=3)).date(),views=100,
            watch_minutes=500,average_duration=300,average_percentage=50,subscribers_gained=2,
            subscribers_lost=0,likes=2,comments=1,content_type="VIDEO",fetched_at=now))
    session.flush()
    control=predict(session,"b",Snapshot(views=100,observed_at=now),
        {"velocity":100/24,"acceleration":0,"subscriber_conversion":.02,"watchtime_efficiency":.5},24)
    session.add(control)
    session.flush()
    row=decision(session)
    row.design="matched_control"
    row.control_video_id="b"
    row.confounders="Gleiches Format; Thema bleibt möglicher Störfaktor."
    activate(session,row,"a",now)
    session.add_all([Snapshot(video_id="a",observed_at=now+timedelta(hours=24),views=250),
                     Snapshot(video_id="b",observed_at=now+timedelta(hours=24),views=200)])
    session.flush()
    evaluate_memory(session,now+timedelta(hours=25))
    assert row.actual_result["control_adjusted_residual"] == pytest.approx(.5)
    assert evidence(session,row.strategy_key)["wins"] == 1
