"""Reparatur-Sprint aus dem 14-Tage-Post-Mortem: Jobs laufen durch, Guardrails sind messbar, Evidenz ist ehrlich."""
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from app import discovery, growth_engine as ge, history, learning, monitor, pipeline, regimes, strategy
from app.budget import Budget, SyncBudgetExceeded
from app.config import settings
from app.models import (DiscoveryOpportunity, DiscoveryRun, GrowthAction, GrowthPlan, GrowthScore, LearningDataset, SyncRun, utcnow)
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_api import client  # noqa: F401

# Die real in Production gemessenen Kanalmediane (Stand 2026-09-23): mehrheitlich 0 bzw. n=18.
BASE0 = {"status": "ok", "n_rows": 8202, "n_videos": 3, "weighting": "video_balanced",
         "ratio_7_28": {"q05": .5, "q25": .8, "q50": 1., "q75": 1.2, "q90": 1.5, "q95": 1.8, "q99": 2.5},
         "accel_7d": {"q05": -.4, "q25": -.1, "q50": 0., "q75": .15, "q90": .4, "q95": .6, "q99": 1.},
         "medians": {"retention_avg": {"median": 0.5007, "n": 8229, "usable": True},
                     "pct_7d": {"median": 51.89, "n": 6403, "usable": True},
                     "subscriber_conversion_7d": {"median": 0.0, "n": 6403, "usable": False},
                     "ctr_7d": {"median": 0.0812, "n": 18, "usable": False},
                     "traffic_search": {"median": 0.0, "n": 6403, "usable": False},
                     "traffic_suggested": {"median": 0.0, "n": 6403, "usable": False},
                     "traffic_browse": {"median": 0.0, "n": 6403, "usable": False}}}


def features(**changes):
    return {**{"ratio_7_28": 1.0, "accel_7d": 0.0, "views_7d": 400, "views_28d": 1600, "velocity_7d": 57.0,
               "retention_avg": 0.2, "pct_7d": 30.0, "ctr_7d": 0.01, "subscriber_conversion_7d": 0.0,
               "traffic_search": 0.0, "traffic_suggested": 0.0, "traffic_browse": 0.0, "traffic_total_7d": 10,
               "days_above_28d_last3": 1, "age_days": 400, "paid_views_32d": 0, "watch_minutes_7d": 100,
               "subs_gained_7d": 0, "subs_net_7d": 0}, **changes}


# ---------------------------------------------------------------------------- P0
def test_budget_reserve_keeps_seconds_for_the_optional_phase(monkeypatch):
    import app.budget as bm
    monkeypatch.setattr(bm.time, "monotonic", lambda: 0.0)
    budget = bm.Budget(210, reserve=90)
    monkeypatch.setattr(bm.time, "monotonic", lambda: 100.0)
    with pytest.raises(SyncBudgetExceeded):
        budget.check()            # 110 s übrig, davon 90 s reserviert -> Kernphase stoppt
    budget.release()
    budget.check()                # reservierte Sekunden stehen der optionalen Phase zur Verfügung
    assert round(budget.remaining()) == 110
    assert pipeline.OPTIONAL_RESERVE_SECONDS > 0


def test_optional_jobs_run_even_when_the_core_import_defers(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire as wire_sync
    wire_sync(monkeypatch, session)
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)

    class CoreExhausted(FakeYouTube):
        def query(self, video, start, end, metrics, dimensions):
            raise SyncBudgetExceeded()

    result = pipeline.collect(CoreExhausted())
    assert result["status"] == "deferred"
    session.expire_all()
    # Genau der Produktionsfehler: trotz erschöpftem Kernbudget wurde V4/V5 ausgeführt.
    assert session.scalar(select(GrowthPlan)) is not None
    assert session.scalar(select(func.count()).select_from(GrowthScore)) >= 1


def test_cron_jobs_endpoint_is_separate_authenticated_and_idempotent(client, session, monkeypatch):
    import app.main as main
    factory = sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(main, "Session", factory)
    monkeypatch.setattr(settings, "cron_secret", "c"*40)
    refresh = Mock(return_value={"status": "ok"})
    run = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(main.learning_module, "refresh", refresh)
    monkeypatch.setattr(main.discovery_module, "run", run)
    import app.youtube as youtube
    monkeypatch.setattr(youtube, "YouTube", lambda *a, **k: object())
    assert client.get("/api/cron/jobs").status_code == 401
    assert client.get("/api/cron/jobs", headers={"Authorization": "Bearer test-token-only"}).status_code == 401
    assert refresh.call_count == 0
    headers = {"Authorization": "Bearer "+"c"*40}
    body = client.get("/api/cron/jobs", headers=headers).json()
    assert body == {"status": "ok", "learning": "ok", "discovery": "ok", "issues": []}
    assert refresh.call_count == 1 and run.call_count == 1
    # Gleiche Stunde: Lease verhindert Doppelarbeit.
    assert client.get("/api/cron/jobs", headers=headers).json()["status"] == "already_completed"
    assert refresh.call_count == 1
    monkeypatch.setattr(settings, "vercel_env", "preview")
    assert client.get("/api/cron/jobs", headers=headers).status_code == 403
    monkeypatch.setattr(settings, "vercel_env", "production")


def test_cron_jobs_reports_failures_without_leaking_details(client, session, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "Session", sessionmaker(session.bind, expire_on_commit=False))
    monkeypatch.setattr(settings, "cron_secret", "d"*40)
    monkeypatch.setattr(main.learning_module, "refresh", Mock(side_effect=RuntimeError("private-token-must-not-appear")))
    monkeypatch.setattr(main.discovery_module, "run", Mock(side_effect=RuntimeError("private-token-must-not-appear")))
    import app.youtube as youtube
    monkeypatch.setattr(youtube, "YouTube", lambda *a, **k: object())
    response = client.get("/api/cron/jobs", headers={"Authorization": "Bearer "+"d"*40})
    assert response.status_code == 503 and "private-token" not in response.text
    assert response.json()["issues"] == ["learning: RuntimeError", "discovery: RuntimeError"]
    # SyncBudgetExceeded ist kein Fehler, sondern aufgeschobene Arbeit (Lease fuer denselben Bucket zuruecksetzen).
    from app.models import JobLease
    lease = session.get(JobLease, "youtube-jobs")
    lease.completed_bucket = None
    session.commit()
    monkeypatch.setattr(main.learning_module, "refresh", Mock(side_effect=SyncBudgetExceeded()))
    monkeypatch.setattr(main.discovery_module, "run", Mock(return_value={"status": "already_completed"}))
    later = client.get("/api/cron/jobs", headers={"Authorization": "Bearer "+"d"*40})
    assert later.status_code == 200 and later.json()["learning"] == "deferred"


def test_monitor_flags_stale_jobs_overdue_actions_and_deferred_streaks(session):
    empty = monitor.health(session, NOW)
    assert empty["level"] == "critical" and any("noch nie erzeugt" in w for w in empty["warnings"])
    session.add_all([
        GrowthPlan(day=TODAY, version="v", created_at=NOW, plan={}),
        DiscoveryRun(day=TODAY, started_at=NOW, finished_at=NOW, status="ok", units_used=5, issues=[], stats={}),
        LearningDataset(signature="s1", built_at=NOW, version="v", config={}, audit={}, rows_per_horizon={}, exclusions={}, baselines={}),
        SyncRun(started_at=NOW, finished_at=NOW, status="ok", issues=[]),
    ])
    session.commit()
    assert monitor.health(session, NOW)["level"] == "ok"
    # 49 h später sind Plan, Discovery und Datensatz überaltert.
    stale = monitor.health(session, NOW+timedelta(hours=49))
    assert stale["level"] == "critical" and len(stale["warnings"]) >= 3
    assert stale["checks"]["plan_age_hours"] >= 48 and stale["checks"]["thresholds"]["stale_critical_hours"] == 48
    # Überfällig ist nur ein bestaetigt gestartetes Experiment; ein Vorschlag laeuft nicht und kann nicht ueberfaellig sein.
    session.add(GrowthAction(video_id="b", created_day=TODAY-timedelta(days=20), version="v", state="observe", action="test_title",
                             target_metric="views_7d", window_days=7, evaluate_after=TODAY-timedelta(days=2), status="proposed", payload={}))
    session.commit()
    assert monitor.health(session, NOW)["checks"]["overdue_actions"] == []
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=20), version="v", state="observe", action="test_title",
                             target_metric="views_7d", window_days=7, evaluate_after=TODAY-timedelta(days=2), status="running",
                             started_day=TODAY-timedelta(days=20), started_at=NOW-timedelta(days=20), payload={}))
    session.commit()
    overdue = monitor.health(session, NOW)
    assert overdue["level"] == "critical" and overdue["checks"]["overdue_actions"][0]["days_overdue"] == 2
    assert any("Auswertungstermin" in w for w in overdue["warnings"])
    # deferred ist kein gesunder Zustand.
    for _ in range(3):
        session.add(SyncRun(started_at=NOW, finished_at=NOW, status="deferred", issues=[]))
    session.commit()
    streak = monitor.health(session, NOW)
    assert streak["checks"]["deferred_streak"] >= monitor.DEFERRED_STREAK
    assert any("ohne Status ok" in w for w in streak["warnings"]) and streak["level"] == "critical"


def test_health_endpoint_exposes_only_the_job_level(client, session):
    session.add(GrowthPlan(day=TODAY, version="v", created_at=utcnow(), plan={}))
    session.commit()
    body = client.get("/health").json()
    assert set(body) == {"status", "jobs"} and body["status"] == "ok" and body["jobs"] in monitor.LEVELS


def test_dashboard_exposes_the_health_block(client, session):
    body = client.get("/api/dashboard", headers={"Authorization": "Bearer test-token-only"}).json()
    assert body["health"]["level"] in monitor.LEVELS and isinstance(body["health"]["warnings"], list)
    assert "deferred" in body["health"]["note"]


# ---------------------------------------------------------------------------- P1
def test_activity_floor_prevents_protect_momentum_on_a_dead_video(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=2, trend=0)          # ~14 Views/Woche
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    h = next(x for x in history.load(session) if x.video.id == "a")
    f = history.features_at(h, TODAY)
    regime = regimes.classify(f, base)
    assert regime["regime"] == "insufficient_data" and regime["low_activity"] is True
    assert regime["min_weekly_views"] == regimes.MIN_WEEKLY_VIEWS and f["views_7d"] < regimes.MIN_WEEKLY_VIEWS
    assert regime["regime"] not in ge.PROTECT_REGIMES
    state = ge.state_of(f, regime, base, {"candidate": False})
    # Belegte Distributionslücke, kein Schutz: seit dem Actionable-Sprint praezise als Verteilungsproblem benannt.
    assert state == "needs_distribution"
    board = ge.scores(f, regime, base, [{"horizon_hours": 168, "predicted_views": 30, "baseline_views": 10,
                                         "lower_views": 5, "upper_views": 90}], {"score": 95}, {"peak_velocity": 100})
    comps = {c["name"]: c for c in board["opportunity"]["components"]}
    for noisy in ("Tempo 7d vs 28d (Kanalquantile)", "Wochenbeschleunigung", "Regime V4",
                  "Live-Momentum (Snapshots, V2)", "V4-Prognose 7d vs Baseline"):
        assert comps[noisy]["available"] is False, noisy
    from test_actionable_growth import CHANNEL
    action, notes = ge.choose_action(state, f, {"signals": []}, base, [], {}, None, CHANNEL)
    # Kein Packaging-Test und kein Beobachten: ein Distributions- oder Evidenzexperiment mit belegter Ressource.
    assert action in ("link_from_own_video", "probe_missing_evidence") and action not in ge.PASSIVE_ACTIONS
    # Ohne geprueftes Inventar und ohne belegtes Quellvideo wird nichts behauptet.
    assert ge.choose_action(state, f, {"signals": []}, base, [], {}, None, None)[0] == "observe"
    assert any("Auslieferung zu gering" in n for n in notes)
    # Und im Plan: niemals geschützt, sondern aktiv handelbar.
    row = {"video_id": "a", "title": "A", "state": state, "regime": regime["regime"], "breakout": False, "action": action,
           "opportunity_score": board["opportunity"]["score"], "viewer_score": None, "subscriber_score": None, "revival": False,
           "revival_signals": [], "confidence": "low", "reason": "r", "notes": [], "window_days": 14, "target_metric": "discovery_views_7d",
           "success_criterion": "s", "objective": "Discovery", "do_not_change": ["Titel"], "next_evaluation": "2026-10-01",
           "held_since": None, "momentum": None, "paid_status": "organic", "external": None, "priority": 1}
    plan = ge.daily_plan([row], TODAY, {})
    assert plan["active_status"] == "active" and plan["priority_video_id"] == "a" and plan["protected"] == []


def test_zero_and_thin_medians_are_never_used_as_thresholds():
    assert regimes.usable_median({"median": 0.0, "n": 6403}) is False
    assert regimes.usable_median({"median": 0.0812, "n": 18}) is False
    assert regimes.usable_median({"median": 0.0812, "n": 100}) is True
    f = features()
    assert ge._median_signal(f, BASE0, "ctr_7d")[0] is None                      # n=18
    assert ge._median_signal(f, BASE0, "subscriber_conversion_7d")[0] is None    # Median 0
    assert strategy._compare(f, BASE0, "ctr_7d", "CTR") is None
    assert strategy._compare(f, BASE0, "retention_avg", "Retention") is not None
    # Der 0-Median darf keinen (unerreichbaren) Discovery-Zustand erzeugen.
    assert ge.state_of(f, {"regime": "stable"}, BASE0, {"candidate": False}) == "observe"
    # Nutzbare Mediane funktionieren weiterhin.
    assert ge.state_of(f, {"regime": "declining"}, BASE0, {"candidate": False}) == "needs_retention_analysis"
    rev = ge.revival(features(age_days=800, retention_avg=0.9, subscriber_conversion_7d=0.5, ratio_7_28=0.5),
                     {"regime": "stable"}, BASE0, {"peak_velocity": 1000})
    assert "gute Abo-Conversion trotz niedriger Views" not in rev["signals"]      # Median 0 ist kein Beleg


def test_observe_does_not_block_a_later_actionable_opportunity(monkeypatch, session):
    factory = wire(monkeypatch, session)
    monkeypatch.setattr(discovery, "Session", factory)
    seed_history(session, "a", days=420, base=40, trend=0)
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)

    def ctx(day):
        h = next(x for x in history.load(session) if x.video.id == "a")
        f = history.features_at(h, day)
        return [{"video": h.video, "history": h, "features": f, "regime": regimes.classify(f, base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": {"level": "low", "n_videos": 1,
                                                                      "n_rows": base["n_rows"], "n_origins": 0}}}]

    ge.run(session, NOW, ctx(TODAY), base)
    session.expire_all()
    first = session.scalar(select(GrowthAction).order_by(GrowthAction.id.desc()))
    assert first.action == "observe" and first.status == "proposed"
    session.add(DiscoveryOpportunity(day=TODAY+timedelta(days=1), kind="suggested", key="ext_neighbour", video_id="a",
        gap="suggested_opportunity", scores={"external_audience_score": 82.0, "suggested_opportunity_score": 82.0},
        components={"components": []}, evidence={"demand_source": "own_analytics", "evidence_level": "own_analytics",
                                                 "actionable": True, "own_suggested_views_90d": 25, "title": "Nachbarvideo",
                                                 "context_usable": True, "context_reason": "eigene Analytics"},
        status="open"))
    session.commit()
    ge.run(session, NOW+timedelta(days=1), ctx(TODAY+timedelta(days=1)), base)
    session.expire_all()
    actions = [(r.created_day, r.action, r.status) for r in session.scalars(select(GrowthAction).order_by(GrowthAction.id))]
    # Am Folgetag – nicht erst nach 10 Tagen Sperre.
    assert actions[-1][1] == "target_suggested_cluster" and actions[-1][0] == TODAY+timedelta(days=1)
    assert actions[0][2] == "superseded"
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc())).plan
    assert plan["active_status"] == "active" and plan["priority_video_id"] == "a"


def test_paid_status_is_recomputed_from_current_data(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, paid_days={TODAY-timedelta(days=LAG+40)})
    session.add(GrowthScore(video_id="a", day=TODAY-timedelta(days=10), version="v", state="paid_cooldown", action="observe",
        opportunity={"score": None}, viewer={"score": None}, subscriber={"score": None}, revival={},
        momentum={"paid": {"status": "paid_cooldown", "clean_days": 29, "required_clean_days": 32, "days_until_clean": 3}},
        created_at=NOW))
    session.commit()
    view = ge.overview(session, NOW)
    assert view["paid_live"]["a"]["status"] == "organic_with_paid_history"
    assert view["paid_live"]["a"]["clean_days"] >= 32 and view["paid_live"]["a"]["days_until_clean"] == 0
    assert view["scores"]["a"]["paid_stored"]["status"] == "paid_cooldown"        # gespeichert bleibt sichtbar
    assert view["scores"]["a"]["paid_live"]["status"] == "organic_with_paid_history"


# ---------------------------------------------------------------------------- P2
def test_seed_relevance_is_filter_only_and_proxy_scores_are_capped():
    fake = type("S", (), {"bind": type("B", (), {"dialect": type("D", (), {"name": "sqlite"})()})()})()
    comps = [discovery._component("Relevanz zum Sealand-Video (Token-Abdeckung)", 1.0, 3, 1.0, "zirkulär", scoring=False),
             discovery._component("Nachfrage-Proxy: Median-Views der Top-Ergebnisse", 0.9, 1.5, 30005, proxy=True)]
    assert discovery._score([comps[0]]) is None                      # allein liefert Relevanz keinen Score mehr
    raw = discovery._score(comps)
    assert raw is not None and discovery.grade(raw, "weak_proxy") <= discovery.PROXY_SCORE_CAP
    assert discovery.grade(raw, "own_analytics") == raw
    assert discovery.grade(raw, "none") is None
    assert discovery.PROXY_SCORE_CAP < ge.EXTERNAL_MIN_SCORE          # Proxy kann keine Aktion auslösen
    assert fake is not None


def test_generic_single_word_seeds_are_filtered(monkeypatch, session):
    from test_discovery_v6 import DiscoveryClient, wire as wire_v6
    wire_v6(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2)
    discovery.run(DiscoveryClient(), NOW)
    session.expire_all()
    queries = {q.query: q for q in session.scalars(select(discovery.DiscoveryQuery))}
    generic = [q for q, row in queries.items() if len(q.split()) == 1 and row.source != "own_search_term"]
    assert generic == [], generic
    assert all(len(q.split()) >= discovery.MIN_SEED_TOKENS or queries[q].source == "own_search_term" for q in queries)


def test_proxy_only_chance_like_trans_mongolian_is_not_treated_as_proven(monkeypatch, session):
    """Produktionsfall: 'trans mongolian' – kein eigener Such-Traffic, nur öffentliche Probe."""
    wire(monkeypatch, session)
    session.add(DiscoveryOpportunity(day=TODAY, kind="search", key="trans mongolian", video_id="a",
        gap="existing_video_opportunity",
        scores={"external_audience_score": discovery.PROXY_SCORE_CAP, "search_opportunity_score": discovery.PROXY_SCORE_CAP},
        components={"components": []},
        evidence={"demand_source": "public_proxy", "evidence_level": "weak_proxy", "actionable": False, "score_capped": True,
                  "own_search_views_90d": 0, "probe": {"n": 25, "median_views": 30005, "our_rank": None}}, status="open"))
    session.commit()
    best = discovery.best_for_video(session, "a")
    assert best["key"] == "trans mongolian" and best["actionable"] is False and best["evidence_level"] == "weak_proxy"
    assert best["score"] <= discovery.PROXY_SCORE_CAP
    f = features(views_7d=400)
    action, _ = ge.choose_action("observe", f, {"signals": []}, BASE0, [], {}, best)
    assert action == "observe"                                        # keine aktive Maßnahme auf Proxy-Evidenz
    view = discovery.overview(session, NOW)
    assert view["actionable"] == 0 and view["evidence_policy"]["proxy_score_cap"] == discovery.PROXY_SCORE_CAP


def test_missing_search_term_details_are_reported_as_missing_demand_evidence(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=120)
    missing = discovery.demand_evidence(session, "a", [])
    assert missing["status"] == "unavailable_below_api_threshold" and missing["search_views_90d"] > 0
    assert "keine reale Nachfrage-Evidenz" in missing["note"]
    assert discovery.demand_evidence(session, "a", [("train journey", 40)])["status"] == "available"
    assert discovery.demand_evidence(session, "b", [])["status"] == "no_search_traffic"


# ---------------------------------------------------------------------------- P3
def test_forecast_scorecard_marks_never_validated_and_reports_absolute_mae(monkeypatch, session):
    card = learning.scorecard(session)
    assert card and all(v["never_validated"] is True and v["n"] == 0 for v in card.values())
    assert all("Backtest" in v["note"] for v in card.values())
    wire(monkeypatch, session)
    seed_history(session, "a", days=500)
    seed_history(session, "b", days=500, base=200, seed=3)
    learning.refresh(session, NOW)
    session.expire_all()
    view = learning.overview(session, NOW)
    weekly = view["backtests"]["168"]
    assert weekly["mae_views"] is not None and weekly["baseline_mae_views"] is not None
    assert "absolute Größe" in weekly["scale_note"]
    assert view["scorecard"]["168"]["never_validated"] is True


# ---------------------------------------------------------------------------- UI
def test_dashboard_javascript_renders_health_and_evidence():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    prefix = source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:'',hidden:false,className:''})};
renderHealth({level:'ok',warnings:[],checks:{}});
assert.equal(nodes.healthBanner.hidden,true);
renderHealth({level:'critical',warnings:['Growth Plan ist 240 h alt (Grenze 48 h) - Jobs laufen nicht durch.'],
 checks:{last_sync_status:'deferred',deferred_streak:12,plan_age_hours:240,discovery_age_hours:240}});
assert.equal(nodes.healthBanner.hidden,false);
assert.match(nodes.healthBanner.className,/down/);
assert.match(nodes.healthBanner.textContent,/ACHTUNG - Jobs laufen nicht durch|ACHTUNG – Jobs laufen nicht durch/);
assert.match(nodes.healthBanner.textContent,/Letzter Sync: deferred/);
assert.match(nodes.healthBanner.textContent,/Serie ohne ok: 12/);
assert.match(evidenceBadge({evidence_level:'weak_proxy',score_capped:true,actionable:false}),/nur Proxy/);
assert.match(evidenceBadge({evidence_level:'weak_proxy',score_capped:true,actionable:false}),/gedeckelt/);
assert.match(evidenceBadge({evidence_level:'own_analytics',actionable:true}),/belegt/);
"""
    subprocess.run(["node", "-"], input=prefix+"\n"+harness, text=True, encoding="utf-8", capture_output=True, check=True)
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'id="healthBanner"' in html and html.index('id="healthBanner"') < html.index('id="growthCard"')
    assert "renderHealth(state.health)" in source
