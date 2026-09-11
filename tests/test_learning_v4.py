"""V4: leakage-free historical dataset, walk-forward backtests, regimes, strategy and feedback loop."""
import math
import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from app import history, backtest, regimes, strategy, learning, pipeline
from app.budget import Budget, SyncBudgetExceeded
from app.models import (Video, Daily, TrafficDaily, BackfillReport, IngestCursor, LearningDataset, LearningBacktest,
                        AnalyticsForecast, StrategyRecommendation, Decision, ExperimentChange, Snapshot, utcnow)

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
TODAY = history.pacific_day(NOW)
LAG = 3


def seed_history(session, video_id, days=900, base=100, trend=0.0005, paid_days=(), seed=1, end=None, breakout_from=None):
    """Deterministic synthetic analytics rows, one per Pacific day, with traffic and monthly retention."""
    rng = random.Random(seed)
    end = end or TODAY-timedelta(days=LAG)
    first = end-timedelta(days=days-1)
    video = session.get(Video, video_id)
    video.published_at = datetime(first.year, first.month, first.day, 12, tzinfo=timezone.utc)
    for i in range(days):
        day = first+timedelta(days=i)
        views = int(base*(1+trend*i)*(1+0.2*math.sin(i/7))*(1+rng.uniform(-0.1, 0.1)))
        if breakout_from and day >= breakout_from:
            views *= 6
        session.add(Daily(video_id=video_id, day=day, views=views, watch_minutes=views*2.5, average_duration=150+rng.uniform(-5, 5),
            average_percentage=45+rng.uniform(-3, 3), subscribers_gained=views//50, subscribers_lost=1, likes=views//20,
            comments=views//100, content_type="VIDEO", fetched_at=NOW))
        paid = day in paid_days
        session.add(TrafficDaily(video_id=video_id, day=day, source="YT_SEARCH", views=views-(views//3)-(10 if paid else 0),
                                 watch_minutes=1, paid=False, fetched_at=NOW))
        session.add(TrafficDaily(video_id=video_id, day=day, source="RELATED_VIDEO", views=views//3, watch_minutes=1, paid=False, fetched_at=NOW))
        if paid:
            session.add(TrafficDaily(video_id=video_id, day=day, source="ADVERTISING", views=10, watch_minutes=1, paid=True, fetched_at=NOW))
    for start, stop in history_months(first, end):
        session.add(BackfillReport(video_id=video_id, kind="retention", start=start, end=stop, fetched_at=NOW,
            rows=[{"elapsedVideoTimeRatio": x/10, "audienceWatchRatio": 1-x/16} for x in range(11)]))
    session.merge(IngestCursor(video_id=video_id, kind="daily", through=end))
    session.commit()


def history_months(first, end):
    from app.backfill import month_windows
    return list(month_windows(first, end))


def wire(monkeypatch, session):
    factory = sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "Session", factory)
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)
    return factory


def test_features_only_use_days_known_at_origin_and_targets_only_future(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=120)
    h = next(x for x in history.load(session) if x.video.id == "a")
    origin = TODAY-timedelta(days=40)
    f = history.features_at(h, origin)
    assert f["known_end"] == str(origin-timedelta(days=LAG))
    assert f["source"] == "analytics_daily_retrospective"
    known = [d for d in h.daily if d <= origin-timedelta(days=LAG)]
    expected_7d = sum(h.views(d) for d in sorted(known)[-7:])
    assert f["views_7d"] == expected_7d
    # Retention: only months that ended before the known end.
    assert f["retention_window"] and date.fromisoformat(f["retention_window"][1]) <= origin-timedelta(days=LAG)
    t = history.target_at(h, origin, 168)
    assert t["start"] == str(origin+timedelta(days=1)) and t["end"] == str(origin+timedelta(days=7))
    assert t["views"] == sum(h.views(origin+timedelta(days=i)) for i in range(1, 8))
    # Unobserved future is never a target; too-short history never a feature row.
    assert history.target_at(h, TODAY-timedelta(days=2), 168) is None
    assert history.features_at(h, h.first_day+timedelta(days=10)) is None
    # Snapshot/V2 data never enters the row.
    assert not any(k in f for k in ("velocity", "acceleration", "windows", "growth_assessment"))


def test_dataset_is_reproducible_and_paid_periods_are_excluded_with_reasons(monkeypatch, session):
    wire(monkeypatch, session)
    paid = {TODAY-timedelta(days=200), TODAY-timedelta(days=199)}
    seed_history(session, "a", days=400, paid_days=paid)
    seed_history(session, "b", days=60, seed=2)
    histories = history.load(session)
    rows, excluded = history.build(histories, 168, LAG)
    again, _ = history.build(histories, 168, LAG)
    config = {"lag": LAG}
    assert history.signature(rows, config) == history.signature(again, config)
    assert history.signature(rows, {"lag": 2}) != history.signature(rows, config)
    assert excluded["paid_feature_window"] >= 32 and excluded["paid_target_window"] >= 7
    origins = {r["origin"] for r in rows if r["video_id"] == "a"}
    # Every origin whose 32-day feature window or 7-day target window touches a paid day is gone.
    for p in paid:
        for delta in range(-7, 32+LAG):
            assert p+timedelta(days=delta) not in origins
    assert all(r["features"]["paid_views_32d"] == 0 and r["target"]["paid_views"] == 0 for r in rows)
    assert {r["video_id"] for r in rows} == {"a", "b"}
    audit = history.audit(histories, LAG)
    assert {v["video_id"]: v["paid_days"] for v in audit["videos"]} == {"a": 2, "b": 0}
    assert "snapshots" in audit["excluded_feature_sources"][0]


def test_walk_forward_embargo_baselines_and_honest_acceptance(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=1000, trend=0)
    seed_history(session, "b", days=1000, base=300, seed=3, trend=0)
    rows, _ = history.build(history.load(session), 168, LAG)
    result = backtest.walk_forward(rows, 168, LAG)
    assert result["status"] == "backtested" and result["folds"]
    subsampled = backtest.subsample(rows, 7)
    for fold in result["folds"]:
        cut = date.fromisoformat(fold["cut"])
        train = [r for r in subsampled if r["origin"] <= cut-timedelta(days=7+LAG)]
        # Every training outcome was fully observed before the fold's cut; nothing later leaks in.
        assert fold["n_train"] == len(train)
        assert max(r["origin"] for r in train)+timedelta(days=7+LAG) <= cut
    models = result["models"]
    assert set(backtest.BASELINES) <= set(models) and all(models[m]["n"] > 0 for m in backtest.BASELINES)
    for name in backtest.CANDIDATES:
        if name in models:
            entry = models[name]
            better = entry["mae"] < entry["baseline_mae_same_rows"]*backtest.IMPROVEMENT and entry["n"] >= backtest.MIN_CALIBRATION
            assert (result["champion"] == name) <= better
    assert result["accepted"] == (result["champion"] in backtest.CANDIDATES)
    assert result["residual_quantiles"]["n"] == models[result["champion"]]["n"]
    tiny = backtest.walk_forward(rows[:20], 168, LAG)
    assert tiny["status"] == "insufficient_data" and tiny["champion"] == "baseline_7d" and not tiny["accepted"]
    breakout = backtest.breakout_backtest(rows, LAG)
    assert breakout["status"] == "insufficient_data" or breakout["n_test"] >= backtest.MIN_CALIBRATION


def test_regimes_come_from_channel_quantiles_and_never_from_paid_periods(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    assert base["status"] == "ok" and base["ratio_7_28"]["q95"] > base["ratio_7_28"]["q75"] > base["ratio_7_28"]["q25"]
    f = rows[-1]["features"]
    calm = regimes.classify({**f, "ratio_7_28": base["ratio_7_28"]["q50"], "accel_7d": base["accel_7d"]["q50"]}, base)
    assert calm["regime"] == "stable" and calm["thresholds"]["ratio_q95"] == base["ratio_7_28"]["q95"]
    hot = {**f, "ratio_7_28": base["ratio_7_28"]["q99"]+1, "accel_7d": base["accel_7d"]["q99"]+1, "days_above_28d_last3": 3}
    assert regimes.classify(hot, base)["regime"] == "breakout"
    assert regimes.classify({**hot, "days_above_28d_last3": 2}, base)["regime"] == "breakout_candidate"
    assert regimes.classify({**f, "ratio_7_28": base["ratio_7_28"]["q05"]-0.01, "accel_7d": 0}, base)["regime"] == "declining"
    assert regimes.classify({**f, "ratio_7_28": base["ratio_7_28"]["q90"], "accel_7d": base["accel_7d"]["q90"]}, base)["regime"] == "accelerating"
    assert regimes.classify({**hot, "paid_views_32d": 5}, base)["regime"] == "paid_excluded"
    assert regimes.classify(None, base)["regime"] == "insufficient_data"
    assert regimes.classify(hot, {"status": "insufficient_data", "n_rows": 10})["regime"] == "insufficient_data"


def test_strategy_never_draws_organic_conclusions_from_paid_and_protects_momentum():
    base = {"status": "ok", "n_rows": 500, "n_videos": 3, "medians": {"retention_avg": {"median": .5, "n": 100}, "ctr_7d": {"median": .05, "n": 40}}}
    f = {"retention_avg": .3, "ctr_7d": .02, "traffic_search": .7, "traffic_suggested": .3, "traffic_total_7d": 100, "age_days": 400}
    paid = strategy.recommend({**f, "paid_views_32d": 3}, {"regime": "paid_excluded", "reason": "Werbung"}, base, None, [], [])
    assert paid["next"][0]["action"] == "keine organische Schlussfolgerung" and paid["for"] == [] and paid["against"] == []
    hot = strategy.recommend(f, {"regime": "breakout", "reason": "x"}, base, {"accepted": False}, [], [])
    assert hot["next"][0]["action"] == "Momentum schützen" and "Titel" in hot["do_not_change"] and "Thumbnail" in hot["do_not_change"]
    assert hot["confidence"]["level"] == "low" and hot["targets"]["100000"]["status"] == "insufficient_data"
    cold = strategy.recommend(f, {"regime": "declining", "reason": "x"}, base, {"accepted": False}, [], [])
    actions = [n["action"] for n in cold["next"]]
    assert "Retention-Schwachstelle untersuchen" in actions and "Thumbnail testen" in actions and "Experiment starten" in actions
    assert any(a["direction"] == "against" and "Retention" in a["signal"] for a in cold["against"])
    unknown = strategy.recommend(None, {"regime": "insufficient_data", "reason": "x"}, {"status": "insufficient_data", "n_rows": 0, "n_videos": 0, "medians": {}}, None, [], [])
    assert unknown["confidence"]["level"] == "insufficient_data" and unknown["next"][0]["action"] == "Daten sammeln"
    assert strategy.NO_MANIPULATION in unknown["do_not_change"]
    signal = [{"feature": "retention_avg", "kind": "standardized_ridge_coefficient", "value": .4}]
    assoc = strategy.recommend(f, {"regime": "stable", "reason": "x"}, base, {"accepted": True}, signal, [])
    assert any("nicht kausal" in w for w in assoc["why"])


def test_experiment_memory_before_after_and_conflicts(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=120)
    h = next(x for x in history.load(session) if x.video.id == "a")
    applied = NOW-timedelta(days=30)
    d1 = Decision(video_id="a", status="evaluated", hypothesis="Titel klarer", strategy_key="k", design="observational", confounders="",
                  content_strategy="c", title_strategy="t", thumbnail_strategy="th", hook_strategy="h", audience="a",
                  expected_result={"metric": "additional_views", "value": 10, "horizon_hours": 168},
                  measurement={"origin_at": applied.isoformat()}, actual_result={"relative_deviation": .5})
    d2 = Decision(video_id="a", status="evaluated", hypothesis="Titel klarer 2", strategy_key="k", design="observational", confounders="",
                  content_strategy="c", title_strategy="t", thumbnail_strategy="th", hook_strategy="h", audience="a",
                  expected_result={"metric": "additional_views", "value": 10, "horizon_hours": 168},
                  measurement={"origin_at": (NOW-timedelta(days=2)).isoformat()}, actual_result={"relative_deviation": -.2})
    session.add_all([d1, d2])
    session.flush()
    session.add(ExperimentChange(decision_id=d1.id, applied_at=applied, dimension="title", before_value="a", after_value="b", rationale="test"))
    session.flush()
    rows = strategy.experiment_context(session, "a", h, NOW)
    first, second = rows
    assert first["effect_status"] == "observed_change_not_causal" and first["effect"]["before_mean_daily"] > 0
    assert first["before_window"][1] < first["after_window"][0]
    assert second["effect_status"] == "after_window_not_yet_observed" and second["effect"] is None
    assert first["conflicting_evidence"] and second["conflicting_evidence"]


def test_refresh_is_idempotent_and_feedback_loop_scores_outcomes(monkeypatch, session):
    factory = wire(monkeypatch, session)
    seed_history(session, "a", days=800)
    seed_history(session, "b", days=800, base=250, seed=5)
    result = learning.refresh(session, NOW)
    assert result["status"] == "ok" and result["forecasts_created"] == 6 and result["videos_advised"] == 2
    session.expire_all()
    dataset = learning.latest_dataset(session)
    assert dataset.rows_per_horizon["168"] > 0 and dataset.baselines["status"] == "ok"
    backtests = learning.backtests_for(session, dataset)
    assert set(backtests) == {24, 168, 720} and all(b.result["status"] == "backtested" for b in backtests.values())
    assert backtests[168].result["breakout"]["status"] in ("insufficient_data", "backtested")
    # Second run within 24h: same dataset, no duplicate forecasts or recommendations.
    again = learning.refresh(session, NOW+timedelta(hours=1))
    assert again["forecasts_created"] == 0 and again["dataset"] == dataset.signature
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AnalyticsForecast)) == 6
    assert session.scalar(select(func.count()).select_from(StrategyRecommendation)) == 2
    assert session.scalar(select(func.count()).select_from(LearningDataset)) == 1
    forecast = session.scalar(select(AnalyticsForecast).where(AnalyticsForecast.video_id == "a", AnalyticsForecast.horizon_hours == 24))
    assert forecast.actual_views is None and forecast.features["known_end"] == str(TODAY-timedelta(days=LAG))
    assert forecast.model in backtest.BASELINES+backtest.CANDIDATES
    rec = session.scalar(select(StrategyRecommendation).where(StrategyRecommendation.video_id == "a"))
    assert rec.recommendation["regime"] == rec.regime and rec.recommendation["confidence"]["level"] in ("low", "insufficient_data")
    assert len(rec.forecast_ids) == 3
    # Time passes, the day is observed: the forecast is scored and stays eligible unless paid traffic appeared.
    later = NOW+timedelta(days=1+LAG)
    for video_id in ("a", "b"):
        for i in range(1, 2+LAG):
            day = TODAY+timedelta(days=i)
            session.add(Daily(video_id=video_id, day=day, views=120, watch_minutes=1, average_duration=1, average_percentage=1,
                subscribers_gained=0, subscribers_lost=0, likes=0, comments=0, content_type="VIDEO", fetched_at=later))
        session.merge(IngestCursor(video_id=video_id, kind="daily", through=TODAY+timedelta(days=1+LAG)))
    session.add(TrafficDaily(video_id="b", day=TODAY+timedelta(days=1), source="ADVERTISING", views=1, watch_minutes=0, paid=True, fetched_at=later))
    session.commit()
    monkeypatch.setattr(learning, "REBUILD_HOURS", 10**6)
    outcome = learning.refresh(session, later)
    assert outcome["scored"] == 2
    session.expire_all()
    scored = {f.video_id: f for f in session.scalars(select(AnalyticsForecast).where(AnalyticsForecast.horizon_hours == 24, AnalyticsForecast.origin_day == TODAY))}
    assert scored["a"].actual_views == 120 and scored["a"].eligibility == "eligible_organic" and scored["a"].absolute_error == abs(120-scored["a"].predicted_views)
    assert scored["b"].eligibility == "paid_excluded"
    card = learning.scorecard(session)
    assert card["24"]["n"] == 1 and card["24"]["status"] == "few_outcomes" and card["24"]["live_fallback"] is False
    view = learning.overview(session, later)
    assert view["videos"]["a"]["evaluated_forecasts"] == 1 and view["backtests"]["168"]["champion"]
    assert view["dataset"]["audit"]["n_videos"] == 2 and "medians" not in view["dataset"]["baselines"]


def test_live_fallback_to_baseline_when_model_underperforms(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    learning.refresh(session, NOW)
    session.expire_all()
    for i in range(learning.LIVE_FALLBACK_MIN):
        session.add(AnalyticsForecast(video_id="a", origin_day=TODAY-timedelta(days=100+i), horizon_hours=24, model="ridge", model_version="t",
            predicted_views=1000, baseline_views=100, interval_kind="uncalibrated_scenario", features={}, regime="stable",
            actual_views=100, absolute_error=900, log_error=0, eligibility="eligible_organic", evaluated_at=NOW))
    session.commit()
    card = learning.scorecard(session)
    assert card["24"]["live_fallback"] is True and card["24"]["model_mae"] > card["24"]["baseline_mae"]
    bt = learning.backtests_for(session, learning.latest_dataset(session))[24]
    bt.champion, bt.accepted = "ridge", True
    session.commit()
    session.execute(AnalyticsForecast.__table__.delete().where(AnalyticsForecast.origin_day == TODAY))
    session.commit()
    monkeypatch.setattr(learning, "REBUILD_HOURS", 10**6)
    learning.refresh(session, NOW)
    session.expire_all()
    fresh = session.scalar(select(AnalyticsForecast).where(AnalyticsForecast.origin_day == TODAY, AnalyticsForecast.horizon_hours == 24))
    assert fresh.model.startswith("live_fallback:baseline")


def test_learning_step_in_sync_is_optional_and_budget_safe(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire as wire_sync
    wire_sync(monkeypatch, session)
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)
    assert pipeline.collect(FakeYouTube())["status"] == "ok"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(StrategyRecommendation)) == 1
    rec = session.scalar(select(StrategyRecommendation))
    assert rec.regime == "insufficient_data"
    assert session.scalar(select(func.count()).select_from(AnalyticsForecast)) == 0
    calls = []
    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private-detail")
    monkeypatch.setattr(learning, "refresh", boom)
    result = pipeline.collect(FakeYouTube())
    assert calls and result["status"] == "ok"
    assert any(i.startswith("optional/learning: RuntimeError") for i in result["issues"])
    assert "private-detail" not in " ".join(result["issues"])
    def exhausted(*args, **kwargs):
        raise SyncBudgetExceeded()
    monkeypatch.setattr(learning, "refresh", exhausted)
    result = pipeline.collect(FakeYouTube())
    assert result["status"] == "ok" and not any("learning" in i for i in result["issues"])


def test_upserts_compile_for_postgresql():
    from sqlalchemy.dialects import postgresql
    from app.backfill import upsert
    fake = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    stmt = upsert(fake, AnalyticsForecast).values(video_id="a", origin_day=date(2026, 1, 1), horizon_hours=24, created_at=NOW, model="m",
        model_version="v", predicted_views=1, baseline_views=1, interval_kind="k", features={}, regime="stable").on_conflict_do_nothing(
        index_elements=["video_id", "origin_day", "horizon_hours"])
    assert "ON CONFLICT (video_id, origin_day, horizon_hours) DO NOTHING" in str(stmt.compile(dialect=postgresql.dialect()))
    rec = upsert(fake, StrategyRecommendation).values(video_id="a", origin_day=date(2026, 1, 1), created_at=NOW, version="v", regime="r",
        recommendation={}, forecast_ids=[], decision_ids=[])
    sql = str(rec.on_conflict_do_update(index_elements=["video_id", "origin_day", "version"], set_={"regime": rec.excluded.regime}).compile(dialect=postgresql.dialect()))
    assert "DO UPDATE SET regime = excluded.regime" in sql
