"""Lebenszyklus der Growth-Maßnahmen: proposed -> running -> evaluated.

Der in Production gemeldete Fehler: gespeicherte Empfehlungen wurden als laufende Experimente
behandelt und blockierten die JETZT-TUN-Queue, obwohl niemand sie ausgeführt hatte. Das System
hat keine Schreibrechte auf YouTube; ohne Bestätigung des Menschen läuft also nichts.
"""
from datetime import timedelta
from sqlalchemy import select
from app import growth_engine as ge, history, regimes
from app.models import GrowthAction, GrowthPlan, Reach, Video
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_growth_v5 import BASE
from test_actionable_growth import CONF, queue_row


def action_row(session, video_id, action="distribute_playlist_context", created_day=None, **changes):
    """Eine gespeicherte Maßnahme, standardmaessig als Vorschlag – so wie das System sie erzeugt."""
    created_day = created_day or TODAY-timedelta(days=1)
    row = GrowthAction(**{**{"video_id": video_id, "created_day": created_day, "created_at": NOW-timedelta(days=1),
                             "version": ge.VERSION, "state": "needs_distribution", "action": action,
                             "target_metric": "discovery_views_7d", "window_days": 14,
                             "evaluate_after": created_day+timedelta(days=17), "status": ge.PROPOSED,
                             "payload": {"steps": ["Playlist setzen"], "baseline": {"views_7d": 18}}}, **changes})
    session.add(row)
    session.commit()
    return row


def test_a_proposal_does_not_block_the_queue(session):
    action_row(session, "a", created_day=TODAY-timedelta(days=1))
    rows = [queue_row("a", "Trainstories", "needs_distribution", "distribute_playlist_context", 70, priority=1,
                      action_status=ge.PROPOSED, started_day=None, held_since=None)]
    plan = ge.daily_plan(rows, TODAY, {})
    assert [q["video_id"] for q in plan["queue"]] == ["a"], "ein Vorschlag ist die Aufgabe, nicht die Blockade"
    assert plan["running_experiments"] == []
    assert plan["now_do"]["status"] == ge.PROPOSED and plan["now_do"]["confirm"]["required"] is True
    assert plan["now_do"]["confirm"]["endpoint"].endswith("/start")
    assert plan["now_do"]["executed_automatically"] is False and "noch nicht gestartet" in plan["now_do"]["note"]


def test_only_the_users_confirmation_starts_the_measurement_window(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    row = action_row(session, "a")
    assert row.started_at is None and row.started_day is None and row.evaluate_after == row.created_day+timedelta(days=17)
    started = ge.start_action(session, row.id, NOW)
    assert started.status == ge.RUNNING and started.started_day == TODAY and started.started_at is not None
    # Messfenster laeuft ab der Bestaetigung, nicht ab dem Vorschlagstag.
    assert started.evaluate_after == TODAY+timedelta(days=14+LAG)
    assert started.baseline["frozen_day"] == str(TODAY) and started.baseline["confirmed_by"] == "channel_owner"
    assert "views_7d" in started.baseline and "nichts auf YouTube geändert" in started.baseline["note"]
    # Zweiter Klick startet nichts neu.
    again = ge.start_action(session, row.id, NOW+timedelta(hours=1))
    assert again.started_day == TODAY and again.evaluate_after == TODAY+timedelta(days=14+LAG)


def test_a_running_experiment_blocks_a_second_one_for_the_same_video(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    first = action_row(session, "a", created_day=TODAY-timedelta(days=2))
    ge.start_action(session, first.id, NOW)
    second = action_row(session, "a", action="probe_missing_evidence", created_day=TODAY)
    try:
        ge.start_action(session, second.id, NOW)
        raise AssertionError("ein zweites Experiment am selben Video muss abgelehnt werden")
    except ValueError as exc:
        assert "läuft bereits ein Experiment" in str(exc) and str(first.id) in str(exc)
    assert session.get(GrowthAction, second.id).status == ge.PROPOSED
    # Und der Plan bietet fuer dieses Video keine neue Aufgabe an, sondern zeigt das laufende Experiment.
    rows = [queue_row("a", "Trainstories", "needs_distribution", "distribute_playlist_context", 70, priority=1,
                      action_status=ge.RUNNING, started_day=str(TODAY))]
    plan = ge.daily_plan(rows, TODAY, {})
    assert plan["queue"] == [] and plan["running_experiments"][0]["video_id"] == "a"
    assert "seit deiner Bestätigung" in plan["running_experiments"][0]["note"]


def test_an_unconfirmed_action_is_never_scored_as_success_or_failure(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    created = TODAY-timedelta(days=30)
    proposal = action_row(session, "a", created_day=created, evaluate_after=created+timedelta(days=17))
    histories = {h.video.id: h for h in history.load(session)}
    assert ge.evaluate_actions(session, NOW, histories, BASE) == 0
    session.expire_all()
    assert session.get(GrowthAction, proposal.id).status == ge.PROPOSED
    assert session.get(GrowthAction, proposal.id).outcome is None
    assert ge.recent_results(session) == [], "ein nie gestarteter Vorschlag ist kein Ergebnis"
    assert ge.track_record(session).get("distribute_playlist_context") is None
    # Erst nach Bestaetigung und abgelaufenem Fenster wird gemessen.
    row = session.get(GrowthAction, proposal.id)
    row.status, row.started_day, row.started_at = ge.RUNNING, created, NOW-timedelta(days=30)
    session.commit()
    assert ge.evaluate_actions(session, NOW, histories, BASE) == 1
    session.commit()
    session.expire_all()
    scored = session.get(GrowthAction, proposal.id)
    assert scored.status == ge.EVALUATED and scored.outcome in ("positive", "negative", "neutral", "inconclusive")
    assert scored.evaluation["started_day"] == str(created)


def test_old_unconfirmed_actions_are_not_presented_as_running(monkeypatch, session):
    """Die Altlast vom 23.09.: nie bestaetigte Vorschlaege duerfen nicht bis zum 11.10. als laufend gelten."""
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    end = TODAY-timedelta(days=LAG)
    for i in range(7):
        session.add(Reach(video_id="a", day=end-timedelta(days=i), impressions=6, ctr=.08, report_id="r"))
        session.add(Reach(video_id="b", day=end-timedelta(days=i), impressions=2000, ctr=.05, report_id="r"))
    action_row(session, "a", action="improve_discovery", created_day=TODAY-timedelta(days=1),
               evaluate_after=TODAY+timedelta(days=17))
    session.commit()
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    histories = {h.video.id: h for h in history.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v], "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base), "forecasts": [], "experiments": [],
                 "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    assert plan["running_experiments"] == [], "nichts wurde bestaetigt, also laeuft nichts"
    assert any(q["video_id"] == "a" for q in plan["queue"]), "das Video bleibt handelbar"
    entry = next(q for q in plan["queue"] if q["video_id"] == "a")
    assert entry["status"] == ge.PROPOSED and entry["action_id"] is not None
    stale = session.scalar(select(GrowthAction).where(GrowthAction.video_id == "a", GrowthAction.action == "improve_discovery"))
    assert stale.status in (ge.PROPOSED, ge.SUPERSEDED)
    if stale.status == ge.SUPERSEDED:
        assert "nie ausgeführt" in stale.evaluation["note"]


def test_start_endpoint_needs_a_token_and_refuses_unknown_or_started_actions(monkeypatch, session):
    from fastapi.testclient import TestClient
    from app import main
    from app.db import Session as _
    monkeypatch.setattr(main, "Session", lambda: session)
    main.app.dependency_overrides[main.db] = lambda: session
    try:
        client = TestClient(main.app)
        row = action_row(session, "a")
        assert client.post(f"/api/growth/actions/{row.id}/start").status_code in (401, 403)
        headers = {"Authorization": "Bearer test-token-only"}
        assert client.post("/api/growth/actions/999999/start", headers=headers).status_code == 404
        body = client.post(f"/api/growth/actions/{row.id}/start", headers=headers).json()
        assert body["status"] == ge.RUNNING and body["executed_automatically"] is False
        # Der Endpunkt nutzt die echte Uhr, nicht die Fixture-Zeit.
        from app.models import utcnow
        assert body["started_day"] == str(history.pacific_day(utcnow())) and "nichts auf YouTube geändert" in body["note"]
        second = action_row(session, "a", action="probe_missing_evidence", created_day=TODAY)
        clash = client.post(f"/api/growth/actions/{second.id}/start", headers=headers)
        assert clash.status_code == 409 and "läuft bereits" in clash.json()["detail"]
    finally:
        main.app.dependency_overrides.clear()


def test_a_changed_decision_on_the_same_day_keeps_one_startable_proposal(monkeypatch, session):
    """Sonst zeigte die Queue auf eine bereits ersetzte Zeile und der Start-Klick lief ins Leere."""
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    stale = action_row(session, "a", action="improve_discovery", created_day=TODAY)
    histories = {h.video.id: h for h in history.load(session)}
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    contexts = [{"video": session.get(Video, "a"), "history": histories["a"],
                 "features": history.features_at(histories["a"], TODAY),
                 "regime": regimes.classify(history.features_at(histories["a"], TODAY), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}}]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    open_rows = list(session.scalars(select(GrowthAction).where(GrowthAction.video_id == "a", GrowthAction.status == ge.PROPOSED)))
    assert len(open_rows) == 1, "genau ein offener Vorschlag je Video"
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    entry = next(q for q in plan["queue"] if q["video_id"] == "a")
    assert entry["action_id"] == open_rows[0].id == stale.id and entry["action"] == open_rows[0].action
    # Und dieser Vorschlag laesst sich tatsaechlich starten.
    started = ge.start_action(session, entry["action_id"], NOW)
    assert started.status == ge.RUNNING
