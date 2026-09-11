"""V5 growth engine: relative scores, states, revival, protect-winners, one action, feedback, daily plan, read-only."""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select, func
from app import growth_engine as ge, learning, history
from app.models import (GrowthAction, GrowthPlan, GrowthScore, GrowthAssessment, Decision, Daily, TrafficDaily, IngestCursor, Video, utcnow)
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG

BASE = {"status": "ok", "n_rows": 500, "n_origins": 400, "n_videos": 3, "ratio_7_28": {"q05": .5, "q25": .8, "q50": 1.0, "q75": 1.2, "q90": 1.5, "q95": 1.8, "q99": 2.5},
        "accel_7d": {"q05": -.4, "q25": -.1, "q50": 0.0, "q75": .15, "q90": .4, "q95": .6, "q99": 1.0},
        "medians": {k: {"median": v, "n": 100, "n_videos": 3} for k, v in {"retention_avg": .5, "pct_7d": 45, "subscriber_conversion_7d": .02, "ctr_7d": .05,
                    "traffic_search": .3, "traffic_suggested": .4, "traffic_browse": .1, "velocity_7d": 100}.items()}}


def features(**changes):
    return {**{"ratio_7_28": 1.0, "accel_7d": 0.0, "velocity_7d": 100, "views_28d": 2800, "retention_avg": .5, "pct_7d": 45,
               "subscriber_conversion_7d": .02, "ctr_7d": None, "traffic_search": .3, "traffic_suggested": .4, "traffic_browse": .1,
               "traffic_total_7d": 700, "age_days": 400, "subs_gained_7d": 14, "subs_net_7d": 12, "watch_minutes_7d": 1000, "days_above_28d_last3": 1,
               "paid_views_32d": 0}, **changes}


def test_scores_exclude_missing_components_and_stay_relative():
    f = features(ctr_7d=None)
    board = ge.scores(f, {"regime": "stable"}, BASE, [], None, {"peak_velocity": 500})
    opp = board["opportunity"]
    assert 0 <= opp["score"] <= 100 and "keine Wahrscheinlichkeit" in opp["note"]
    assert "Thumbnail-CTR vs Median" in opp["missing"] and "Live-Momentum (Snapshots, V2)" in opp["missing"]
    assert all(c["signal"] is None for c in opp["components"] if not c["available"])
    assert board["viewer"]["score"] != board["subscriber"]["score"]
    hot = ge.scores(features(ratio_7_28=2.0, accel_7d=.7, days_above_28d_last3=3), {"regime": "breakout"}, BASE,
                    [{"horizon_hours": 168, "predicted_views": 1200, "baseline_views": 700, "lower_views": 900, "upper_views": 1500}], {"score": 80}, None)
    assert hot["opportunity"]["score"] > opp["score"] and hot["viewer"]["score"] > board["viewer"]["score"]
    paid = ge.scores(features(paid_views_32d=3), {"regime": "paid_excluded"}, BASE, [], None, None)
    assert paid["opportunity"]["score"] is None and paid["subscriber"]["score"] is None and "Werbetraffic" in paid["viewer"]["reason"]
    assert ge.scores(None, {"regime": "insufficient_data"}, BASE, [], None, None)["opportunity"]["score"] is None
    assert ge.scores(f, {"regime": "stable"}, {"status": "insufficient_data", "medians": {}}, [], None, None)["viewer"]["score"] is None
    strong_sub = ge.scores(features(subscriber_conversion_7d=.08, subs_net_7d=60), {"regime": "stable"}, BASE, [], None, None)
    assert strong_sub["subscriber"]["score"] > board["subscriber"]["score"]


def test_states_and_revival_are_data_driven():
    stable = features()
    assert ge.state_of(stable, {"regime": "breakout"}, BASE, {"candidate": False}) == "protect_momentum"
    assert ge.state_of(stable, {"regime": "accelerating"}, BASE, {"candidate": False}) == "protect_momentum"
    assert ge.state_of(stable, {"regime": "growing"}, BASE, {"candidate": False}) == "scale_opportunity"
    assert ge.state_of(features(retention_avg=.3), {"regime": "declining"}, BASE, {"candidate": False}) == "needs_retention_analysis"
    assert ge.state_of(features(ctr_7d=.02), {"regime": "declining"}, BASE, {"candidate": False}) == "needs_packaging_test"
    assert ge.state_of(features(traffic_search=.1, traffic_suggested=.1, traffic_browse=.05), {"regime": "stable"}, BASE, {"candidate": False}) == "needs_discovery"
    assert ge.state_of(stable, {"regime": "stable"}, BASE, {"candidate": False}) == "observe"
    assert ge.state_of(stable, {"regime": "paid_excluded"}, BASE, {"candidate": True}) == "paid_excluded"
    assert ge.state_of(None, {"regime": "insufficient_data"}, BASE, {"candidate": False}) == "insufficient_data"
    assert ge.state_of(stable, {"regime": "stable"}, BASE, {"candidate": True}) == "revival_candidate"
    peak = {"peak_velocity": 2000}
    young = ge.revival(features(age_days=100), {"regime": "stable"}, BASE, peak)
    assert young["candidate"] is False
    old_good = ge.revival(features(retention_avg=.7, subscriber_conversion_7d=.05, velocity_7d=100, ratio_7_28=.9), {"regime": "stable"}, BASE, peak)
    assert old_good["candidate"] and "gute Retention trotz schwacher Distribution" in old_good["signals"] and "gute Abo-Conversion trotz niedriger Views" in old_good["signals"]
    packaging = ge.revival(features(ctr_7d=.02, retention_avg=.7, accel_7d=.3), {"regime": "stable"}, BASE, None)
    assert packaging["candidate"] and any("Packaging" in s for s in packaging["signals"]) and any("Beschleunigung" in s for s in packaging["signals"])
    assert ge.revival(features(), {"regime": "stable"}, BASE, peak)["candidate"] is False
    assert ge.revival(features(), {"regime": "paid_excluded"}, BASE, peak)["candidate"] is False


def test_action_engine_protects_winners_and_never_stacks_experiments():
    f = features()
    assert ge.choose_action("protect_momentum", f, {"signals": []}, BASE, [], {})[0] == "protect_no_change"
    assert ge.choose_action("protect_momentum", f, {"signals": []}, BASE, [{"status": "registered", "decision_id": 1}], {})[0] == "protect_no_change"
    action, notes = ge.choose_action("needs_packaging_test", f, {"signals": []}, BASE, [{"status": "registered", "decision_id": 7}], {})
    assert action == "observe" and "#7" in notes[0]
    assert ge.choose_action("needs_packaging_test", features(ctr_7d=.02), {"signals": []}, BASE, [], {})[0] == "test_thumbnail"
    assert ge.choose_action("needs_packaging_test", features(ctr_7d=None), {"signals": []}, BASE, [], {})[0] == "test_title"
    assert ge.choose_action("needs_retention_analysis", f, {"signals": []}, BASE, [], {})[0] == "investigate_retention"
    assert ge.choose_action("needs_discovery", f, {"signals": []}, BASE, [], {})[0] == "improve_discovery"
    assert ge.choose_action("scale_opportunity", f, {"signals": []}, BASE, [], {})[0] == "cross_promote"
    assert ge.choose_action("revival_candidate", f, {"signals": ["erneute Beschleunigung über Kanal-q75", "Search-Anteil über Kanalmedian"]}, BASE, [], {})[0] == "protect_no_change"
    assert ge.choose_action("revival_candidate", features(ctr_7d=.02, age_days=800), {"signals": ["Packaging-/CTR-Schwäche bei guten Qualitätswerten", "x"]}, BASE, [], {})[0] == "test_title_thumbnail"
    assert ge.choose_action("revival_candidate", features(ctr_7d=.02, age_days=200), {"signals": ["Packaging-/CTR-Schwäche bei guten Qualitätswerten", "x"]}, BASE, [], {})[0] == "test_thumbnail"
    assert ge.choose_action("revival_candidate", f, {"signals": ["Search-Anteil über Kanalmedian", "y"]}, BASE, [], {})[0] == "improve_discovery"
    assert ge.choose_action("paid_excluded", f, {"signals": []}, BASE, [], {})[0] == "observe"
    # A net-negative observed track record demotes an action to observe; protection is never demoted.
    record = {"test_thumbnail": {"positive": 0, "negative": 3, "n": 3, "net_negative": True}, "protect_no_change": {"positive": 0, "negative": 3, "n": 3, "net_negative": True}}
    action, notes = ge.choose_action("needs_packaging_test", features(ctr_7d=.02), {"signals": []}, BASE, [], record)
    assert action == "observe" and "kein Kausalnachweis" in notes[0]
    assert ge.choose_action("protect_momentum", f, {"signals": []}, BASE, [], record)[0] == "protect_no_change"
    conf = {"level": "low", "n_videos": 3}
    details = ge.action_details("test_thumbnail", "needs_packaging_test", features(ctr_7d=.02), {"regime": "declining"}, BASE, {}, {"candidate": False}, None, conf, [], [])
    for key in ("reason", "signals", "against", "confidence", "target_metric", "window_days", "success_criterion", "stop_criterion", "do_not_change", "objective"):
        assert details[key]
    assert details["window_days"] == 14 and details["target_metric"] == "ctr_or_views" and "Titel" in details["do_not_change"]
    assert ge.NO_MANIPULATION in details["do_not_change"] and "ändert nichts auf YouTube" in details["read_only"]
    assert any("low" in a for a in details["against"]) and details["experiment_template"]["horizon_hours"] == 168
    protect = ge.action_details("protect_no_change", "protect_momentum", f, {"regime": "breakout"}, BASE, {}, {"candidate": False}, {"score": 80}, conf, [], [])
    assert protect["stop_criterion"].startswith("Nicht anwendbar") and protect["objective"] == "Discovery"


def test_run_writes_scores_actions_plan_and_holds_actions_until_evaluated(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=700)
    seed_history(session, "b", days=700, base=300, seed=8)
    result = learning.refresh(session, NOW)
    assert result["growth"]["ranked"] == 2 and result["growth"]["priority"] in ("a", "b")
    session.expire_all()
    plan = session.scalar(select(GrowthPlan)).plan
    assert plan["day"] == str(TODAY) and len(plan["ranking"]) == 2 and plan["ranking"][0]["priority"] == 1
    assert plan["priority_video_id"] == plan["ranking"][0]["video_id"]
    for key in ("why", "why_priority", "action", "objective", "do_not_change", "success_metric", "success_criterion", "window_days", "next_evaluation", "confidence"):
        assert plan[key] is not None
    assert plan["objective"] in ("Viewer", "Subscriber", "Watchtime", "Discovery") and plan["confidence"] in ("low", "insufficient_data")
    assert date.fromisoformat(plan["next_evaluation"]) == TODAY+timedelta(days=plan["window_days"]+LAG)
    assert "keine Wahrscheinlichkeit" in plan["note"] and plan["read_only"]
    assert session.scalar(select(func.count()).select_from(GrowthAction)) == 2
    assert session.scalar(select(func.count()).select_from(GrowthScore)) == 2
    first = {r.video_id: r for r in session.scalars(select(GrowthAction))}
    assert all(r.status == "pending" and r.action in ge.ACTIONS and r.state in ge.STATES for r in first.values())
    # Second run the same day and the next day: idempotent, the pending action is held.
    learning.refresh(session, NOW+timedelta(hours=1))
    monkeypatch.setattr(learning, "REBUILD_HOURS", 10**6)
    learning.refresh(session, NOW+timedelta(days=1))
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(GrowthAction)) == 2
    assert session.scalar(select(func.count()).select_from(GrowthPlan)) == 2
    latest = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc())).plan
    held = next(r for r in latest["ranking"] if r["video_id"] == "a")
    if first["a"].action != "protect_no_change":
        assert held["action"] == first["a"].action and held["held_since"] == str(TODAY)
    view = ge.overview(session)
    assert view["plan"]["priority_video_id"] and view["scores"]["a"]["opportunity"]["components"] and view["read_only"] is True
    assert view["actions"]["a"][0]["status"] == "pending"


def test_protect_supersedes_pending_test_and_plan_prefers_winner(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=200, seed=3)
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=2), version="t", state="needs_packaging_test", action="test_title",
        target_metric="views_7d", window_days=14, evaluate_after=TODAY+timedelta(days=15), status="pending", payload={"reason": "old"}))
    session.commit()
    histories = {h.video.id: h for h in history.load(session)}
    f = history.features_at(histories["a"], TODAY)
    contexts = [{"video": session.get(Video, v), "history": histories[v], "features": history.features_at(histories[v], TODAY),
                 "regime": {"regime": "breakout" if v == "a" else "stable", "reason": "x"}, "forecasts": [], "experiments": [],
                 "recommendation": {"confidence": {"level": "low", "n_videos": 2, "n_rows": 500, "n_origins": 400}}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, BASE)
    session.expire_all()
    rows = {(r.video_id, r.status): r for r in session.scalars(select(GrowthAction))}
    assert rows[("a", "superseded")].action == "test_title" and rows[("a", "superseded")].outcome == "inconclusive"
    assert rows[("a", "pending")].action == "protect_no_change" and rows[("a", "pending")].state == "protect_momentum"
    plan = session.scalar(select(GrowthPlan)).plan
    assert plan["priority_video_id"] == "a" and plan["action"] == "protect_no_change" and "Titel" in plan["do_not_change"]
    assert plan["ranking"][0]["breakout"] is True


def test_feedback_outcomes_positive_negative_neutral_inconclusive(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=200, trend=0)
    h = next(x for x in history.load(session) if x.video.id == "a")
    created = TODAY-timedelta(days=20)
    def action(video_id, act="test_title", target="views_7d"):
        row = GrowthAction(video_id=video_id, created_day=created, version="t", state="needs_packaging_test", action=act, target_metric=target,
                           window_days=7, evaluate_after=created+timedelta(days=7+LAG), status="pending", payload={})
        session.add(row)
        session.flush()
        return row
    row = action("a")
    for i in range(1, 8):  # after window far above before window
        session.get(Daily, ("a", created+timedelta(days=i))).views = 1000
    session.commit()
    assert ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE) == 1
    session.commit()
    session.expire_all()
    row = session.get(GrowthAction, row.id)
    assert row.status == "evaluated" and row.outcome == "positive" and row.evaluation["detail"]["metric"] == "views"
    assert row.evaluation["after"]["views"] == 7000 and "keine Kausalwirkung" in row.evaluation["note"]
    for i in range(1, 8):
        session.get(Daily, ("a", created+timedelta(days=i))).views = 1
    session.commit()
    row.status = "pending"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "negative"
    before_views = [session.get(Daily, ("a", created-timedelta(days=i))).views for i in range(1, 8)]
    for i in range(1, 8):
        session.get(Daily, ("a", created+timedelta(days=i))).views = before_views[i-1]
    row = session.get(GrowthAction, row.id)
    row.status = "pending"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "neutral"
    session.add(TrafficDaily(video_id="a", day=created+timedelta(days=2), source="ADVERTISING", views=5, watch_minutes=0, paid=True, fetched_at=NOW))
    row = session.get(GrowthAction, row.id)
    row.status = "pending"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "inconclusive"
    # Windows not yet observed stay pending; protect counts holding momentum as positive.
    fresh = GrowthAction(video_id="a", created_day=TODAY-timedelta(days=2), version="t", state="protect_momentum", action="protect_no_change",
                         target_metric="views_7d", window_days=7, evaluate_after=TODAY+timedelta(days=8), status="pending", payload={})
    session.add(fresh)
    session.commit()
    assert ge.evaluate_actions(session, NOW, {"a": h}, BASE) == 0
    assert session.get(GrowthAction, fresh.id).status == "pending"
    record = ge.track_record(session)
    assert record["test_title"]["n"] == 1 and record["test_title"]["net_negative"] is False


def test_growth_step_runs_inside_sync_and_is_read_only(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire as wire_sync
    wire_sync(monkeypatch, session)
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)
    assert pipeline_collect(FakeYouTube())["status"] == "ok"
    session.expire_all()
    plan = session.scalar(select(GrowthPlan))
    assert plan is not None and plan.plan["status"] == "insufficient_data"
    assert session.scalar(select(GrowthAction)).action == "observe"
    source = Path("app/youtube.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(update|insert|delete|set|rate|thumbnails\(\)\.set)\(", source)
    assert "youtube.readonly" in source and "youtube.force-ssl" not in source and "youtube.upload" not in source
    engine = Path("app/growth_engine.py").read_text(encoding="utf-8")
    assert "YouTube(" not in engine and "googleapiclient" not in engine


def pipeline_collect(client):
    from app import pipeline
    return pipeline.collect(client)


def test_upserts_compile_for_postgresql():
    from sqlalchemy.dialects import postgresql
    from app.backfill import upsert
    fake = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    stmt = upsert(fake, GrowthAction).values(video_id="a", created_day=date(2026, 1, 1), created_at=NOW, version="v", state="observe", action="observe",
        target_metric="views_7d", window_days=7, evaluate_after=date(2026, 1, 11), status="pending", payload={}).on_conflict_do_nothing(index_elements=["video_id", "created_day"])
    assert "ON CONFLICT (video_id, created_day) DO NOTHING" in str(stmt.compile(dialect=postgresql.dialect()))
    plan = upsert(fake, GrowthPlan).values(day=date(2026, 1, 1), version="v", created_at=NOW, plan={})
    assert "DO UPDATE SET plan = excluded.plan" in str(plan.on_conflict_do_update(index_elements=["day", "version"], set_={"plan": plan.excluded.plan}).compile(dialect=postgresql.dialect()))
