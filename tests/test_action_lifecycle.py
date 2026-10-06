"""Lebenszyklus der Growth-Maßnahmen: proposed -> running -> evaluated.

Traeger ist eine Reichweitenaktion, weil JETZT TUN ausschliesslich solche enthaelt.

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


def action_row(session, video_id, action="repackage_for_reach", created_day=None, **changes):
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


def attested(session, video_id="a", score=70.0):
    """Eine belegte Audience-Chance – ohne sie gibt es keine Reichweitenaktion und damit keine Aufgabe."""
    from app.models import DiscoveryOpportunity
    from test_actionable_growth import seed_profile
    seed_profile(session, video_id)
    session.add(DiscoveryOpportunity(day=TODAY, kind="suggested", key=f"nachbarschaft-{video_id}",
                                     video_id=video_id, gap="suggested_opportunity",
                                     scores={"external_audience_score": score}, components={}, status="open",
                                     evidence={"evidence_level": "own_analytics", "actionable": True,
                                               "context_usable": True, "title": "Nachbarcluster"}))
    session.commit()


def test_a_proposal_does_not_block_the_queue(session):
    action_row(session, "a", created_day=TODAY-timedelta(days=1))
    rows = [queue_row("a", "Trainstories", "needs_distribution", "repackage_for_reach", 70, priority=1,
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
        assert "Nicht trennbar" in str(exc) and str(first.id) in str(exc)
    assert session.get(GrowthAction, second.id).status == ge.PROPOSED
    # Und der Plan bietet fuer dieses Video keine neue Aufgabe an, sondern zeigt das laufende Experiment.
    rows = [queue_row("a", "Trainstories", "needs_distribution", "repackage_for_reach", 70, priority=1,
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
    assert ge.track_record(session).get("repackage_for_reach") is None
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
    attested(session, "a")
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
        assert clash.status_code == 409 and "Nicht trennbar" in clash.json()["detail"]
    finally:
        main.app.dependency_overrides.clear()


def test_a_changed_decision_on_the_same_day_keeps_one_startable_proposal(monkeypatch, session):
    """Sonst zeigte die Queue auf eine bereits ersetzte Zeile und der Start-Klick lief ins Leere."""
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)      # belegtes Quellvideo fuer die interne Verlinkung
    stale = action_row(session, "a", action="improve_discovery", created_day=TODAY)
    attested(session, "a")
    histories = {h.video.id: h for h in history.load(session)}
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}} for v in ("a", "b")]
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


def test_an_open_proposal_always_carries_the_current_steps(monkeypatch, session):
    """Sonst zeigte die Queue neue Schritte, waehrend der Start die alten einfriert."""
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)      # belegtes Quellvideo fuer die interne Verlinkung
    stale = action_row(session, "a", created_day=TODAY, payload={"steps": ["Alter Schritt"], "baseline": {}})
    attested(session, "a")
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    histories = {h.video.id: h for h in history.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    row = session.get(GrowthAction, stale.id)
    assert row.status == ge.PROPOSED and row.payload["steps"] != ["Alter Schritt"]
    assert row.payload["primary_lever"] and row.payload["do_not_change"]
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    entry = next(q for q in plan["queue"] if q["video_id"] == "a")
    assert entry["steps"] == row.payload["steps"], "Queue und gespeicherte Maßnahme zeigen dasselbe"


def test_a_running_measure_can_be_cancelled_so_it_does_not_hold_up_reach(session):
    """Eine alte Messung darf eine staerkere Reichweitenaktion nicht tagelang blockieren.

    Abgebrochen wird protokolliert, nicht geloescht: die Zeile bleibt als `superseded` mit Grund und
    Tag erhalten, und ihr Ergebnis gilt ausdruecklich als nicht gemessen.
    """
    row = action_row(session, "a", created_day=TODAY-timedelta(days=9), status=ge.RUNNING,
                     started_day=TODAY-timedelta(days=9), started_at=NOW,
                     evaluate_after=TODAY+timedelta(days=5))
    cancelled = ge.cancel_action(session, row.id, "Reichweite hat Vorrang.", NOW)
    session.commit()
    assert cancelled.status == ge.SUPERSEDED
    assert session.get(GrowthAction, row.id) is not None, "nicht geloescht"
    assert cancelled.evaluation["note"] == "Reichweite hat Vorrang."
    assert cancelled.evaluation["cancelled_by"] == "channel_owner"
    assert cancelled.evaluation["measured"] is False
    assert cancelled.evaluation["cancelled_day"] == str(ge.pacific_day(NOW))
    # Ein zweiter Klick aendert nichts, und was nicht laeuft, kann nicht abgebrochen werden.
    assert ge.cancel_action(session, row.id, None, NOW).status == ge.SUPERSEDED
    open_row = action_row(session, "b", created_day=TODAY)
    try:
        ge.cancel_action(session, open_row.id, None, NOW)
        raise AssertionError("ein Vorschlag ist nicht laufend")
    except ValueError as exc:
        assert "laufende" in str(exc)
    try:
        ge.cancel_action(session, 99999, None, NOW)
        raise AssertionError("unbekannte Maßnahme")
    except LookupError:
        pass


def test_a_measure_without_development_is_replaced_without_a_click(monkeypatch, session):
    """Der Loop entscheidet selbst: eine Maßnahme ohne Entwicklung haelt ihr Video nicht fest.

    Der Kanalinhaber klickt dafuer nichts. Die alte Zeile bleibt als `superseded` erhalten, mit dem
    Befund und dem Vermerk, dass die Engine entschieden hat, und dasselbe Lauf plant das Video neu.
    """
    from app.models import GrowthPlan
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=6, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    attested(session, "a")       # legt auch die eigenen Videoangaben an
    from test_actionable_growth import details_for, starved
    blocking = action_row(session, "a", action="probe_missing_evidence",
                          created_day=TODAY-timedelta(days=9), status=ge.RUNNING,
                          started_day=TODAY-timedelta(days=9), started_at=NOW,
                          evaluate_after=TODAY+timedelta(days=5),
                          payload=details_for("probe_missing_evidence", starved(), ["laeuft"]))
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    histories = {h.video.id: h for h in history.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base),
                 "forecasts": [], "experiments": [],
                 "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    # Ein einziger Lauf: die alte Messung wird ersetzt und das Video neu geplant.
    stale = session.get(GrowthAction, blocking.id)
    assert stale.status == ge.SUPERSEDED, "die alte Messung haelt das Video nicht mehr"
    assert stale.evaluation["cancelled_by"] == "engine" and stale.evaluation["measured"] is False
    assert stale.evaluation["replaced"] is True
    assert stale.evaluation["progress"]["verdict"] == "weak"
    assert "keine zusaetzliche Auslieferung" in stale.evaluation["note"]
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc(), GrowthPlan.id.desc())).plan
    entry = next((q for q in plan["queue"] if q["video_id"] == "a"), None)
    assert entry is not None, f"nicht neu geplant: {[(q['video_id'], q['action']) for q in plan['queue']]}"
    assert entry["action"] == "repackage_for_reach"
    package = entry["brief"]["packaging"]
    # Ohne belegte Aussage ueber den Song gibt es keinen neuen Titel, aber Beschreibung und Thumbnail.
    assert package["title"] is None and package["description"]
    assert any("In this video:" in line for line in package["description"])
    assert package["thumbnail"] is None or "Bildinhalt" in package["thumbnail"]


def test_the_executed_change_is_recognised_without_a_confirmation_click(monkeypatch, session):
    """Nach der Umsetzung auf YouTube laeuft die Messung von selbst – der Loop braucht keinen Klick."""
    from test_actionable_growth import seed_profile
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=6, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    attested(session, "a")
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    histories = {h.video.id: h for h in history.load(session)}

    def contexts():
        return [{"video": session.get(Video, v), "history": histories[v],
                 "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base),
                 "forecasts": [], "experiments": [],
                 "recommendation": {"confidence": CONF}} for v in ("a", "b")]

    ge.run(session, NOW, contexts(), base)
    session.expire_all()
    proposed = session.scalar(select(GrowthAction).where(GrowthAction.video_id == "a",
                                                         GrowthAction.action == "repackage_for_reach",
                                                         GrowthAction.status == ge.PROPOSED))
    assert proposed is not None, "es gibt eine vorbereitete Packaging-Aktion"
    package = proposed.payload["brief"]["packaging"]
    assert package["source_title"] == "a", "der heutige Titel wird mitgefuehrt"
    assert package["source_description"] is not None, "und die heutige Beschreibung"
    # Diese Maßnahme verlangt keinen neuen Titel. Der Kanalinhaber ersetzt die Beschreibungszeilen;
    # der Sync holt die Beschreibung mit, also faellt die Aenderung von selbst auf.
    from app.models import VideoProfile
    profile = session.get(VideoProfile, "a")
    profile.description = "In this video: Trans Mongolian Railway, Landscape."
    session.commit()
    ge.run(session, NOW, contexts(), base)
    session.expire_all()
    started = session.get(GrowthAction, proposed.id)
    assert started.status == ge.RUNNING, "das Messfenster laeuft ohne Bestaetigungsklick"
    assert started.started_day == TODAY
    assert started.payload["executed_change"] == "description"
    assert started.payload["executed_as_proposed"] is False, "kein Titel vorgeschlagen, also kein Abgleich"


def test_a_measure_that_moves_something_is_kept(session):
    """Gute Entwicklung wird geschuetzt: dann ersetzt der Loop die Maßnahme nicht."""
    from types import SimpleNamespace
    row = SimpleNamespace(started_day=TODAY-timedelta(days=12), created_day=TODAY-timedelta(days=12),
                          window_days=28, target_metric="views_7d", action="repackage_for_reach")
    strong = {"paid_views": 0, "observed_days": 9, "impressions": 900, "views": 240, "ctr": 0.05,
              "watch_minutes": 300, "discovery_views": 180, "subscribers": 2}
    weak = {**strong, "impressions": 10, "views": 2, "discovery_views": 1, "subscribers": 0}
    history = SimpleNamespace(daily={}, traffic={}, reach={})
    calls = {}

    def fake_window(_history, start, end):
        calls["n"] = calls.get("n", 0)+1
        return weak if calls["n"] == 1 else strong

    import app.growth_engine as engine
    original = engine._window_metrics
    engine._window_metrics = fake_window
    try:
        verdict = engine.progress_of(None, row, history, BASE, NOW)
    finally:
        engine._window_metrics = original
    assert verdict["verdict"] == "holding", verdict
    assert "bleibt" in verdict["note"]


def test_without_enough_observed_days_there_is_no_interim_verdict(session):
    from types import SimpleNamespace
    row = SimpleNamespace(started_day=TODAY-timedelta(days=1), created_day=TODAY-timedelta(days=1),
                          window_days=28, target_metric="views_7d", action="repackage_for_reach")
    verdict = ge.progress_of(None, row, SimpleNamespace(daily={}, traffic={}, reach={}), BASE, NOW)
    assert verdict["verdict"] == "too_early" and str(ge.MIN_SIGNAL_DAYS) in verdict["note"]
