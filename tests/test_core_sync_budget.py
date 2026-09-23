"""Der Kernimport muss im Budget fertig werden; V3 ist abgeleitete Arbeit und darf ihn nicht auf deferred setzen.

Messung vor dem Fix (produktionsnaher Profiler, 3 Videos / 12 Tage Snapshots und Prognosen):
v3_assessments_and_forecasts 10.431 und v3_feedback 9.380 Datenbankabfragen je Lauf, alle
uebrigen Schritte zusammen unter 170. Bei Neon ist jede Abfrage ein Roundtrip.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from sqlalchemy import func, select
from app import pipeline, prediction_v3
from app.budget import Budget, SyncBudgetExceeded
from app.models import Daily, PredictionAudit, Snapshot, SyncRun
from app.prediction_v3 import RECHECK_HOURS, due_for_recheck, feedback, predict, training_history
from test_prediction_v3 import features

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)


def matured(session, hours_ago, paid_known=None):
    """Eine faellige, gereifte Prognose samt Audit; optional mit bereits bekanntem Provenienz-Status."""
    origin = NOW - timedelta(hours=hours_ago)
    f = predict(session, "a", Row(observed_at=origin, views=100), features(), 24)
    f.actual_at, f.actual_views, f.absolute_error = origin + timedelta(hours=24), 200, 140.0
    session.flush()
    audit = session.get(PredictionAudit, f.id)
    audit.created_at = origin
    if paid_known:
        audit.feedback = {"status": paid_known, "checked_at": NOW.isoformat()}
    session.flush()
    return f, audit


def test_recheck_is_bounded_per_run_and_at_most_daily(session):
    old, newer = matured(session, 80), matured(session, 40)
    feedback(session, NOW, limit=1)
    session.flush()
    # Aelteste Pruefung zuerst, damit jede Zeile drankommt, aber nur eine je Lauf.
    assert old[1].feedback.get("checked_at") and not newer[1].feedback.get("checked_at")
    stamp = old[1].feedback["checked_at"]
    feedback(session, NOW, limit=1)
    session.flush()
    # Frisch geprueft: nicht erneut; stattdessen rueckt die naechste Zeile nach.
    assert old[1].feedback["checked_at"] == stamp and newer[1].feedback.get("checked_at")


def test_due_for_recheck_uses_the_daily_window(session):
    audit = Row(feedback=None)
    assert due_for_recheck(audit, NOW) is True
    assert due_for_recheck(Row(feedback={"checked_at": "kein-datum"}), NOW) is True
    fresh = (NOW - timedelta(hours=RECHECK_HOURS - 1)).isoformat()
    stale = (NOW - timedelta(hours=RECHECK_HOURS + 1)).isoformat()
    assert due_for_recheck(Row(feedback={"checked_at": fresh}), NOW) is False
    assert due_for_recheck(Row(feedback={"checked_at": stale}), NOW) is True


def test_known_ineligible_rows_are_not_verified_again(session, monkeypatch):
    """Ein Pruefergebnis wird uebernommen; eine noch nie gepruefte Zeile wird vollstaendig geprueft."""
    matured(session, 80, paid_known="paid_window")
    matured(session, 40, paid_known="insufficient_traffic_coverage")
    calls = []
    monkeypatch.setattr(prediction_v3, "eligibility",
                        lambda s, row, cutoff: calls.append(row.id) or {"status": "eligible_organic"})
    assert training_history(session, NOW + timedelta(seconds=1), 24, "VIDEO") == []
    assert calls == []          # kein einziger Provenienz-Roundtrip fuer bekannt ungeeignete Zeilen
    unknown = matured(session, 30)[0]      # Reifezeit muss vor dem Cutoff liegen
    assert [r.id for r in training_history(session, NOW + timedelta(seconds=1), 24, "VIDEO")] == [unknown.id]
    assert calls == [unknown.id]


def test_v3_phase_cannot_defer_the_core_sync(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire
    wire(monkeypatch, session)

    def exhausted(*a, **kw):
        raise SyncBudgetExceeded()
    monkeypatch.setattr(pipeline, "feedback", exhausted)
    result = pipeline.collect(FakeYouTube())
    assert result["status"] == "ok"
    assert result["issues"] == ["optional/v3_predictions: Zeitbudget erreicht; naechster Cron setzt fort"]
    session.expire_all()
    # Der Kernimport selbst ist vollstaendig.
    assert session.scalar(select(func.count()).select_from(Snapshot)) == 1
    assert session.scalar(select(func.count()).select_from(Daily)) == 1


def test_v3_runs_after_v4_v5_v6_and_timings_are_recorded(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire
    wire(monkeypatch, session)
    order = []
    for name in ("optional_learning", "optional_discovery", "optional_lifetime"):
        monkeypatch.setattr(pipeline, name, (lambda n: lambda *a, **kw: order.append(n))(name))
    original = pipeline.optional_predictions
    monkeypatch.setattr(pipeline, "optional_predictions",
                        lambda *a, **kw: (order.append("v3"), original(*a, **kw))[1])
    assert pipeline.collect(FakeYouTube())["status"] == "ok"
    assert order[-1] == "v3" and "optional_learning" in order
    session.expire_all()
    run = session.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    for step in ("client_and_channel", "videos_list", "snapshots", "analytics_and_reports", "total"):
        assert isinstance(run.timings.get(step), (int, float)), run.timings
    core = sum(run.timings[s] for s in ("client_and_channel", "videos_list", "snapshots", "analytics_and_reports"))
    assert core <= run.timings["total"] + 0.5
