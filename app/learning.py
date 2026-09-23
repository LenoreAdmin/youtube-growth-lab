"""V4 orchestration: dataset build, backtests, live analytics forecasts, feedback and strategy log.

Runs as an optional, budgeted step of the hourly sync and on demand. Every write is
an idempotent upsert keyed by dataset signature, video/day/horizon or video/day/version,
so repeated or concurrent runs cannot duplicate or overwrite each other's results.
"""
from datetime import timedelta
from math import log1p, expm1
from sqlalchemy import select, func
from .db import Session
from .models import Video, LearningDataset, LearningBacktest, AnalyticsForecast, StrategyRecommendation, utcnow
from .metrics import aware
from .backfill import upsert
from .history import (VERSION as HISTORY_VERSION, HORIZONS, MIN_HISTORY_DAYS, PAID_WINDOW_DAYS, FEATURES, load, build, signature,
                      audit, features_at, target_at, pacific_day, lag_days, sample_sizes)
from .backtest import (VERSION as BACKTEST_VERSION, BASELINES, MIN_CROSS_VIDEOS, walk_forward, breakout_backtest, associations, fit,
                       predict, baseline_prediction)
from .regimes import baselines, classify
from .strategy import VERSION as STRATEGY_VERSION, recommend, experiment_context

VERSION = "learning-v4"
REBUILD_HOURS = 24
LIVE_FALLBACK_MIN = 20


def evaluate_forecasts(session, now, by_id):
    """Feedback loop: every stored forecast is scored against the later observed analytics days."""
    today = pacific_day(now)
    lag = lag_days()
    scored = 0
    for fc in session.scalars(select(AnalyticsForecast).where(AnalyticsForecast.actual_views.is_(None))):
        if fc.origin_day+timedelta(days=HORIZONS[fc.horizon_hours]+lag) > today:
            continue
        history = by_id.get(fc.video_id)
        target = target_at(history, fc.origin_day, fc.horizon_hours) if history else None
        if target is None:
            continue
        fc.actual_views = target["views"]
        fc.absolute_error = abs(target["views"]-fc.predicted_views)
        fc.log_error = log1p(max(0, target["views"]))-log1p(max(0.0, fc.predicted_views))
        fc.eligibility = "paid_excluded" if target["paid_views"] > 0 or (fc.features.get("paid_views_32d") or 0) > 0 else "eligible_organic"
        fc.evaluated_at = now
        scored += 1
    return scored


def scorecard(session):
    """Live out-of-sample errors per horizon: model versus the stored baseline on identical forecasts."""
    out = {}
    for h in HORIZONS:
        rows = list(session.scalars(select(AnalyticsForecast).where(AnalyticsForecast.horizon_hours == h,
            AnalyticsForecast.eligibility == "eligible_organic")))
        if not rows:
            out[str(h)] = {"n": 0, "never_validated": True, "status": "insufficient_data",
                           "note": "Noch keine Live-Prognose ausgewertet: Modellgüte stammt ausschließlich aus dem Backtest."}
            continue
        model = sum(abs(r.actual_views-r.predicted_views) for r in rows)/len(rows)
        base = sum(abs(r.actual_views-r.baseline_views) for r in rows)/len(rows)
        interval = [r for r in rows if r.lower_views is not None]
        out[str(h)] = {"n": len(rows), "never_validated": False, "model_mae": model, "baseline_mae": base,
                       "interval_hit_rate": sum(r.lower_views <= r.actual_views <= r.upper_views for r in interval)/len(interval) if interval else None,
                       "interval_n": len(interval),
                       "live_fallback": len(rows) >= LIVE_FALLBACK_MIN and model > base,
                       "status": "scored" if len(rows) >= LIVE_FALLBACK_MIN else "few_outcomes"}
    return out


def latest_dataset(session):
    return session.scalar(select(LearningDataset).order_by(LearningDataset.built_at.desc()))


def backtests_for(session, dataset):
    if dataset is None:
        return {}
    rows = session.scalars(select(LearningBacktest).where(LearningBacktest.dataset_signature == dataset.signature,
                                                          LearningBacktest.version == BACKTEST_VERSION))
    return {r.horizon_hours: r for r in rows}


def rebuild(session, now, histories, budget=None, force=False):
    """Deterministic dataset + walk-forward backtests; resumable per horizon, idempotent by signature."""
    lag = lag_days()
    config = {"history_version": HISTORY_VERSION, "backtest_version": BACKTEST_VERSION, "lag_days": lag,
              "horizons": {str(k): v for k, v in HORIZONS.items()}, "min_history_days": MIN_HISTORY_DAYS,
              "paid_window_days": PAID_WINDOW_DAYS, "features": FEATURES}
    rows_by_h, exclusions = {}, {}
    for h in HORIZONS:
        if budget:
            budget.check()
        rows_by_h[h], exclusions[str(h)] = build(histories, h, lag)
    sig = signature([r for h in sorted(rows_by_h) for r in rows_by_h[h]], config)
    dataset = session.get(LearningDataset, sig)
    latest = latest_dataset(session)
    if dataset is None:
        if latest and not force and aware(latest.built_at) > now-timedelta(hours=REBUILD_HOURS):
            return latest, None
        base = baselines(rows_by_h[168])
        statement = upsert(session, LearningDataset).values(signature=sig, built_at=now, version=VERSION, config=config,
            audit={**audit(histories, lag), "sample_sizes": {str(h): sample_sizes(r) for h, r in rows_by_h.items()}},
            rows_per_horizon={str(h): len(r) for h, r in rows_by_h.items()},
            exclusions=exclusions, baselines=base).on_conflict_do_nothing(index_elements=["signature"])
        session.execute(statement)
        session.commit()
        dataset = session.get(LearningDataset, sig)
    existing = backtests_for(session, dataset)
    for h in HORIZONS:
        if h in existing:
            continue
        if budget:
            budget.check()
        rows = rows_by_h[h]
        result = walk_forward(rows, h, lag, budget=budget)
        parameters, signals = {"champion": result["champion"]}, []
        if result["accepted"] and rows:
            model = fit(result["champion"], rows)
            if result["champion"] == "ridge":
                parameters.update(scaler_mean=model[0].mean_.tolist(), scaler_scale=model[0].scale_.tolist(),
                                  coefficients=model[1].coef_.tolist(), intercept=float(model[1].intercept_), alpha=10)
            else:
                parameters.update(feature_importances=model.feature_importances_.tolist(), n_estimators=150, max_depth=2, random_state=0)
            signals = associations(rows, model if result["champion"] == "ridge" else None)
        else:
            signals = associations(rows)
        if h == 168:
            result["breakout"] = breakout_backtest(rows, lag)
        statement = upsert(session, LearningBacktest).values(dataset_signature=sig, horizon_hours=h, version=BACKTEST_VERSION,
            built_at=utcnow(), champion=result["champion"], accepted=bool(result["accepted"]), result=result,
            parameters=parameters, signals=signals).on_conflict_do_nothing(index_elements=["dataset_signature", "horizon_hours", "version"])
        session.execute(statement)
        session.commit()
    return dataset, rows_by_h


def _predictor(name, rows_by_h, h):
    if name in BASELINES:
        return lambda f: baseline_prediction(name, f, h)
    model = fit(name, rows_by_h[h])
    return lambda f: predict(model, f)


def refresh(session, now, budget=None, force=False):
    """Evaluate outcomes, keep backtests fresh, then forecast, classify and advise every active video."""
    histories = load(session)
    by_id = {h.video.id: h for h in histories}
    scored = evaluate_forecasts(session, now, by_id)
    session.commit()
    dataset, rows_by_h = rebuild(session, now, histories, budget, force)
    backtests = backtests_for(session, dataset) if dataset else {}
    base = dataset.baselines if dataset else {"status": "insufficient_data", "n_rows": 0, "n_videos": 0, "medians": {}}
    live = scorecard(session)
    today = pacific_day(now)
    predictors = {}
    created, advised = 0, 0
    contexts = []
    for history in histories:
        if budget:
            budget.check()
        f = features_at(history, today)
        regime = classify(f, base)
        forecast_ids = []
        if f is not None:
            for h, bt in backtests.items():
                exists = session.scalar(select(AnalyticsForecast.id).where(AnalyticsForecast.video_id == history.video.id,
                    AnalyticsForecast.origin_day == today, AnalyticsForecast.horizon_hours == h))
                if exists:
                    forecast_ids.append(exists)
                    continue
                name = bt.champion
                if live.get(str(h), {}).get("live_fallback") and name not in BASELINES:
                    name = bt.result.get("best_baseline", "baseline_7d")
                if (name, h) not in predictors:
                    if rows_by_h is None and name not in BASELINES:
                        rows_by_h = {k: build(histories, k, lag_days())[0] for k in HORIZONS}
                    predictors[name, h] = _predictor(name, rows_by_h, h)
                predicted = predictors[name, h](f)
                baseline = baseline_prediction(bt.result.get("best_baseline", "baseline_7d"), f, h)
                quantiles = bt.result.get("residual_quantiles", {})
                lower = upper = None
                if quantiles.get("kind") == "empirical_80" and regime["regime"] not in ("paid_excluded", "insufficient_data"):
                    lower = float(max(0.0, expm1(log1p(predicted)+quantiles["q10"])))
                    upper = float(max(0.0, expm1(log1p(predicted)+quantiles["q90"])))
                statement = upsert(session, AnalyticsForecast).values(video_id=history.video.id, origin_day=today, horizon_hours=h,
                    created_at=now, model=name if name == bt.champion else f"live_fallback:{name}", model_version=VERSION,
                    dataset_signature=dataset.signature, predicted_views=predicted, baseline_views=baseline, lower_views=lower,
                    upper_views=upper, interval_kind=quantiles.get("kind", "uncalibrated_scenario") if lower is not None else "uncalibrated_scenario",
                    features={**f, "regime": regime["regime"]}, regime=regime["regime"]).on_conflict_do_nothing(
                    index_elements=["video_id", "origin_day", "horizon_hours"])
                session.execute(statement)
                created += 1
            session.flush()
            forecast_ids = list(session.scalars(select(AnalyticsForecast.id).where(AnalyticsForecast.video_id == history.video.id,
                AnalyticsForecast.origin_day == today)))
        experiments = experiment_context(session, history.video.id, history, now)
        weekly = backtests.get(168)
        recommendation = recommend(f, regime, base, weekly.result if weekly else None, weekly.signals if weekly else [], experiments)
        statement = upsert(session, StrategyRecommendation).values(video_id=history.video.id, origin_day=today, created_at=now,
            version=STRATEGY_VERSION, regime=regime["regime"], recommendation=recommendation, forecast_ids=forecast_ids,
            decision_ids=[e["decision_id"] for e in experiments])
        session.execute(statement.on_conflict_do_update(index_elements=["video_id", "origin_day", "version"],
            set_={"regime": statement.excluded.regime, "recommendation": statement.excluded.recommendation,
                  "forecast_ids": statement.excluded.forecast_ids, "decision_ids": statement.excluded.decision_ids,
                  "created_at": statement.excluded.created_at}))
        advised += 1
        todays = [{"horizon_hours": p.horizon_hours, "predicted_views": p.predicted_views, "baseline_views": p.baseline_views,
                   "lower_views": p.lower_views, "upper_views": p.upper_views} for p in session.scalars(select(AnalyticsForecast).where(
                   AnalyticsForecast.video_id == history.video.id, AnalyticsForecast.origin_day == today))]
        contexts.append({"video": history.video, "history": history, "features": f, "regime": regime, "forecasts": todays,
                         "experiments": experiments, "recommendation": recommendation})
    session.commit()
    # V5: rank, decide and plan from the same leakage-safe context; read-only towards YouTube.
    from .growth_engine import run as growth_run
    growth = growth_run(session, now, contexts, base, budget)
    return {"status": "ok", "scored": scored, "forecasts_created": created, "videos_advised": advised,
            "dataset": dataset.signature if dataset else None, "growth": growth}


def overview(session, now=None):
    """Everything the dashboard shows about historical learning; honest about sample sizes."""
    now = now or utcnow()
    dataset = latest_dataset(session)
    backtests = backtests_for(session, dataset)
    per_horizon = {}
    for h, bt in backtests.items():
        r = bt.result
        champion_metrics = (r.get("models") or {}).get(bt.champion) or {}
        baseline_metrics = (r.get("models") or {}).get(r.get("best_baseline")) or {}
        per_horizon[str(h)] = {"champion": bt.champion, "accepted": bt.accepted, "status": r.get("status"), "n_rows": r.get("n_rows"),
            "mae_views": champion_metrics.get("mae"), "baseline_mae_views": baseline_metrics.get("mae"),
            "mae_video_balanced_views": champion_metrics.get("mae_video_balanced"),
            "scale_note": "MAE in Views je Horizont – absolute Größe beachten, nicht nur die relative Verbesserung.",
            "n_origins": r.get("n_origins"), "n_videos": r.get("n_videos"), "validation_scope": r.get("validation_scope"),
            "cross_video": r.get("cross_video"), "folds": len(r.get("folds", [])), "models": r.get("models", {}),
            "best_baseline": r.get("best_baseline"), "residual_quantiles": r.get("residual_quantiles"),
            "breakout": r.get("breakout"), "built_at": bt.built_at, "signals": bt.signals[:12]}
    videos = {}
    for video in session.scalars(select(Video).where(Video.active.is_(True))):
        rec = session.scalar(select(StrategyRecommendation).where(StrategyRecommendation.video_id == video.id)
                             .order_by(StrategyRecommendation.origin_day.desc(), StrategyRecommendation.id.desc()))
        forecasts = []
        for h in HORIZONS:
            fc = session.scalar(select(AnalyticsForecast).where(AnalyticsForecast.video_id == video.id, AnalyticsForecast.horizon_hours == h)
                                .order_by(AnalyticsForecast.origin_day.desc()))
            if fc:
                forecasts.append({"horizon_hours": h, "origin_day": fc.origin_day, "model": fc.model, "predicted_views": fc.predicted_views,
                    "baseline_views": fc.baseline_views, "lower_views": fc.lower_views, "upper_views": fc.upper_views,
                    "interval_kind": fc.interval_kind, "actual_views": fc.actual_views, "absolute_error": fc.absolute_error,
                    "eligibility": fc.eligibility})
        evaluated = session.scalar(select(func.count()).select_from(AnalyticsForecast).where(
            AnalyticsForecast.video_id == video.id, AnalyticsForecast.actual_views.is_not(None)))
        videos[video.id] = {"regime": rec.regime if rec else "insufficient_data", "origin_day": rec.origin_day if rec else None,
                            "recommendation": rec.recommendation if rec else None, "forecasts": forecasts, "evaluated_forecasts": evaluated}
    n_videos = dataset.baselines.get("n_videos", 0) if dataset else 0
    return {"version": VERSION, "generalization": {"scope": "cross_video_generalization", "n_videos": n_videos, "required_videos": MIN_CROSS_VIDEOS,
                "status": "validated" if any(b.result.get("cross_video", {}).get("accepted") for b in backtests.values()) else "insufficient_data",
                "temporal_scope": "temporal_within_video_validation"},
            "dataset": {"signature": dataset.signature, "built_at": dataset.built_at, "rows_per_horizon": dataset.rows_per_horizon,
                "sample_sizes": dataset.audit.get("sample_sizes", {}),
                "exclusions": dataset.exclusions, "audit": dataset.audit, "baselines": {k: v for k, v in dataset.baselines.items() if k != "medians"},
                "medians": dataset.baselines.get("medians", {})} if dataset else None,
            "backtests": per_horizon, "scorecard": scorecard(session), "videos": videos,
            "limits": ["n_rows (korrelierte Tageszeilen), n_origins (Zeitpunkte) und n_videos (unabhängige Einheiten) sind getrennte Größen; Confidence richtet sich nach n_videos.",
                       "n_videos = Anzahl aktiver Videos; video-übergreifende Verallgemeinerung erst ab 10 Videos, Wahrscheinlichkeiten für 100k/1M erst ab 30.",
                       "Historische Features stammen aus rückwirkend geladenen Analytics (Lag beachtet); Snapshot-Velocity existiert nur seit dem ersten Live-Sync.",
                       "Tageswerte sind autokorreliert; Backtests nutzen wöchentliche Origins und ein Ausgangs-Embargo, Folds bleiben dennoch kanalintern.",
                       "Modelle gelten nur als besser, wenn sie die naive Baseline out-of-sample um mindestens 5 % schlagen."]}
