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
    assert ge.state_of(features(paid_views_32d=3), {"regime": "paid_excluded"}, BASE, {"candidate": True}) == "paid_excluded"
    cooldown = features(paid_views_32d=3, paid={"status": "paid_cooldown", "paid_days_total": 2, "clean_days": 10, "required_clean_days": 32, "days_until_clean": 22})
    assert ge.state_of(cooldown, {"regime": "paid_excluded"}, BASE, {"candidate": True}) == "paid_cooldown"
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
    from test_actionable_growth import CHANNEL
    # Interne Verlinkung setzt ein belegtes Quellvideo voraus; ohne Ressourcenlage wird nichts behauptet.
    assert ge.choose_action("needs_discovery", f, {"signals": []}, BASE, [], {}, None, CHANNEL)[0] == "improve_discovery"
    assert ge.choose_action("needs_discovery", f, {"signals": []}, BASE, [], {})[0] == "observe"
    assert ge.choose_action("scale_opportunity", f, {"signals": []}, BASE, [], {}, None, CHANNEL)[0] == "cross_promote"
    assert ge.choose_action("revival_candidate", f, {"signals": ["erneute Beschleunigung über Kanal-q75", "Search-Anteil über Kanalmedian"]}, BASE, [], {})[0] == "protect_no_change"
    assert ge.choose_action("revival_candidate", features(ctr_7d=.02, age_days=800), {"signals": ["Packaging-/CTR-Schwäche bei guten Qualitätswerten", "x"]}, BASE, [], {})[0] == "test_title_thumbnail"
    assert ge.choose_action("revival_candidate", features(ctr_7d=.02, age_days=200), {"signals": ["Packaging-/CTR-Schwäche bei guten Qualitätswerten", "x"]}, BASE, [], {})[0] == "test_thumbnail"
    assert ge.choose_action("revival_candidate", f, {"signals": ["Search-Anteil über Kanalmedian", "y"]}, BASE, [], {}, None, CHANNEL)[0] == "improve_discovery"
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
    assert plan["day"] == str(TODAY) and len(plan["ranking"]) == 2 and plan["ranking"][0]["priority"] == 1 == plan["ranking"][0]["momentum_rank"]
    assert plan["momentum_top"]["video_id"] == plan["ranking"][0]["video_id"]
    if plan["active_status"] == "active":
        for key in ("why", "why_priority", "action", "objective", "do_not_change", "success_metric", "success_criterion", "window_days", "next_evaluation", "confidence"):
            assert plan[key] is not None
        assert plan["objective"] in ("Viewer", "Subscriber", "Watchtime", "Discovery")
        assert date.fromisoformat(plan["next_evaluation"]) == TODAY+timedelta(days=plan["window_days"]+LAG)
        assert next(r for r in plan["ranking"] if r["video_id"] == plan["priority_video_id"])["active_rank"] == 1
    else:
        assert plan["status"] == "no_active_action" and plan["priority_video_id"] is None and plan["action"] is None
        assert plan["why_priority"].startswith("Keine aktive Maßnahme empfohlen")
    assert plan["confidence"] in ("low", "insufficient_data")
    assert "keine Wahrscheinlichkeit" in plan["note"] and plan["read_only"] and plan["priority_semantics"]
    assert session.scalar(select(func.count()).select_from(GrowthAction)) == 2
    assert session.scalar(select(func.count()).select_from(GrowthScore)) == 2
    first = {r.video_id: r for r in session.scalars(select(GrowthAction))}
    # Neu erzeugte Maßnahmen sind Vorschlaege, keine laufenden Experimente.
    assert all(r.status == "proposed" and r.action in ge.ACTIONS and r.state in ge.STATES for r in first.values())
    # Second run the same day and the next day: idempotent, the pending action is held.
    learning.refresh(session, NOW+timedelta(hours=1))
    monkeypatch.setattr(learning, "REBUILD_HOURS", 10**6)
    learning.refresh(session, NOW+timedelta(days=1))
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(GrowthAction)) == 2
    assert session.scalar(select(func.count()).select_from(GrowthPlan)) == 2
    latest = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc())).plan
    held = next(r for r in latest["ranking"] if r["video_id"] == "a")
    if first["a"].action in ge.PASSIVE_ACTIONS:
        # Passive Aktionen werden taeglich neu entschieden und sperren nichts.
        assert held.get("held_since") is None
    else:
        assert held["action"] == first["a"].action and held["held_since"] == str(TODAY)
    view = ge.overview(session)
    assert view["plan"]["priority_video_id"] and view["scores"]["a"]["opportunity"]["components"] and view["read_only"] is True
    assert view["actions"]["a"][0]["status"] == "proposed"


def test_protect_supersedes_pending_test_and_plan_prefers_winner(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=200, seed=3)
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=2), version="t", state="needs_packaging_test", action="test_title",
        target_metric="views_7d", window_days=14, evaluate_after=TODAY+timedelta(days=15), status="proposed", payload={"reason": "old"}))
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
    assert rows[("a", "proposed")].action == "protect_no_change" and rows[("a", "proposed")].state == "protect_momentum"
    plan = session.scalar(select(GrowthPlan)).plan
    # The winner keeps its protection and leads the momentum ranking, but never becomes the active priority.
    assert plan["ranking"][0]["video_id"] == "a" and plan["ranking"][0]["breakout"] is True and plan["ranking"][0]["active_rank"] is None
    assert plan["protected"][0]["video_id"] == "a" and plan["protected"][0]["action"] == "protect_no_change"
    assert plan["priority_video_id"] != "a" and plan["status"] == "no_active_action"  # b only observes
    assert "Keine aktive Maßnahme empfohlen" in plan["combined_decision"] and "Schutz aktiv für: a" in plan["combined_decision"]
    assert "Titel" in plan["do_not_change"]


def test_feedback_outcomes_positive_negative_neutral_inconclusive(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=200, trend=0)
    h = next(x for x in history.load(session) if x.video.id == "a")
    created = TODAY-timedelta(days=20)
    def action(video_id, act="test_title", target="views_7d"):
        row = GrowthAction(video_id=video_id, created_day=created, version="t", state="needs_packaging_test", action=act, target_metric=target,
                           window_days=7, evaluate_after=created+timedelta(days=7+LAG), status="running", started_day=created,
                           started_at=NOW-timedelta(days=9), payload={})
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
    row.status = "running"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "negative"
    before_views = [session.get(Daily, ("a", created-timedelta(days=i))).views for i in range(1, 8)]
    for i in range(1, 8):
        session.get(Daily, ("a", created+timedelta(days=i))).views = before_views[i-1]
    row = session.get(GrowthAction, row.id)
    row.status = "running"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "neutral"
    session.add(TrafficDaily(video_id="a", day=created+timedelta(days=2), source="ADVERTISING", views=5, watch_minutes=0, paid=True, fetched_at=NOW))
    row = session.get(GrowthAction, row.id)
    row.status = "running"
    session.commit()
    ge.evaluate_actions(session, NOW, {"a": next(x for x in history.load(session) if x.video.id == "a")}, BASE)
    session.commit()
    session.expire_all()
    assert session.get(GrowthAction, row.id).outcome == "inconclusive"
    # Windows not yet observed stay pending; protect counts holding momentum as positive.
    fresh = GrowthAction(video_id="a", created_day=TODAY-timedelta(days=2), version="t", state="protect_momentum", action="protect_no_change",
                         target_metric="views_7d", window_days=7, evaluate_after=TODAY+timedelta(days=8), status="running",
                         started_day=TODAY-timedelta(days=2), started_at=NOW, payload={})
    session.add(fresh)
    session.commit()
    assert ge.evaluate_actions(session, NOW, {"a": h}, BASE) == 0
    assert session.get(GrowthAction, fresh.id).status == "running"
    record = ge.track_record(session)
    assert record["test_title"]["n"] == 1 and record["test_title"]["net_negative"] is False


def test_growth_step_runs_inside_sync_and_is_read_only(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire as wire_sync
    wire_sync(monkeypatch, session)
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)
    assert pipeline_collect(FakeYouTube())["status"] == "ok"
    session.expire_all()
    plan = session.scalar(select(GrowthPlan))
    assert plan is not None and plan.plan["status"] == "insufficient_data" and plan.plan["active_status"] == "none"
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
        target_metric="views_7d", window_days=7, evaluate_after=date(2026, 1, 11), status="proposed", payload={}).on_conflict_do_nothing(index_elements=["video_id", "created_day"])
    assert "ON CONFLICT (video_id, created_day) DO NOTHING" in str(stmt.compile(dialect=postgresql.dialect()))
    plan = upsert(fake, GrowthPlan).values(day=date(2026, 1, 1), version="v", created_at=NOW, plan={})
    assert "DO UPDATE SET plan = excluded.plan" in str(plan.on_conflict_do_update(index_elements=["day", "version"], set_={"plan": plan.excluded.plan}).compile(dialect=postgresql.dialect()))


def _row(video_id, state, action, opportunity, viewer=50, subscriber=50, external=None, regime="stable"):
    return {"video_id": video_id, "title": video_id.title(), "state": state, "regime": regime, "breakout": state == "protect_momentum",
            "action": action, "opportunity_score": opportunity, "viewer_score": viewer, "subscriber_score": subscriber, "revival": False,
            "revival_signals": [], "confidence": "low", "reason": "r", "notes": [], "window_days": 14, "target_metric": "views_7d",
            "success_criterion": "s", "objective": "Viewer", "do_not_change": ["Titel"] if action == "protect_no_change" else ["Thumbnail"],
            "next_evaluation": "2026-10-01", "held_since": None, "momentum": None, "paid_status": "organic", "external": external}


def _plan(rows):
    rows = sorted(rows, key=lambda r: -(r["opportunity_score"] or 0))
    for i, r in enumerate(rows):
        r["priority"] = i+1
    return ge.daily_plan(rows, TODAY, {})


def test_protected_top_scorer_is_never_active_priority_and_changeable_video_becomes_number_one():
    plan = _plan([_row("teaser", "protect_momentum", "protect_no_change", 60, regime="breakout"),
                  _row("shine", "needs_packaging_test", "test_thumbnail", 38, viewer=38, subscriber=47),
                  _row("trainstories", "paid_cooldown", "observe", None)])
    assert plan["ranking"][0]["video_id"] == "teaser" and plan["momentum_top"]["video_id"] == "teaser"
    assert plan["active_status"] == "active" and plan["priority_video_id"] == "shine" and plan["action"] == "test_thumbnail"
    ranks = {r["video_id"]: (r["active_rank"], r["ineligible_reason"]) for r in plan["ranking"]}
    assert ranks["shine"][0] == 1 and ranks["teaser"][0] is None and "geschützt" in ranks["teaser"][1]
    assert ranks["trainstories"][0] is None and "Werbetraffic" in ranks["trainstories"][1]
    assert plan["protected"][0]["video_id"] == "teaser" and plan["protected"][0]["action"] == "protect_no_change" and len(plan["protected"]) == 1
    assert "Aktive Growth-Priorität #1: Shine" in plan["combined_decision"] and "Geschützt (keine Änderung): Teaser" in plan["combined_decision"]
    assert "bleibt geschützt" in plan["why_priority"] and "Titel" in plan["protected"][0]["do_not_change"]


def test_external_v6_signals_shape_the_active_priority_without_touching_protection():
    strong = {"score": 82, "kind": "search", "key": "train journey", "gap": "existing_video_opportunity", "demand_source": "own_analytics", "subscriber_fit": 60}
    plan = _plan([_row("teaser", "protect_momentum", "protect_no_change", 70, external={**strong, "score": 95}),
                  _row("shine", "needs_discovery", "improve_discovery", 55),
                  _row("trainstories", "observe", "target_search_opportunity", 48, external=strong)])
    assert plan["priority_video_id"] == "trainstories" and plan["external_signals"]["available"] is True
    assert plan["external_signals"]["key"] == "train journey" and plan["internal_signals"]["active_priority_score"] > 55
    assert next(r for r in plan["ranking"] if r["video_id"] == "shine")["active_rank"] == 2
    assert plan["protected"][0]["video_id"] == "teaser" and plan["protected"][0]["action"] == "protect_no_change"


def test_no_active_measure_is_stated_instead_of_inventing_a_number_one():
    plan = _plan([_row("teaser", "protect_momentum", "protect_no_change", 60),
                  _row("shine", "observe", "observe", 40),
                  _row("trainstories", "paid_excluded", "observe", None)])
    assert plan["status"] == "no_active_action" and plan["active_status"] == "none" and plan["priority_video_id"] is None
    assert plan["action"] is None and plan["why_priority"].startswith("Keine aktive Maßnahme empfohlen")
    assert plan["combined_decision"].startswith("Keine aktive Maßnahme empfohlen") and "Teaser" in plan["combined_decision"]
    assert all(r["active_rank"] is None for r in plan["ranking"]) and plan["momentum_top"]["video_id"] == "teaser"
    assert "Titel" in plan["do_not_change"]
    # A paid-cooldown video with a strong external opportunity still never gets active priority.
    plan = _plan([_row("trainstories", "paid_cooldown", "observe", None, external={"score": 90, "kind": "search", "key": "x", "gap": "search_opportunity", "demand_source": "public_proxy"})])
    assert plan["active_status"] == "none" and plan["priority_video_id"] is None
