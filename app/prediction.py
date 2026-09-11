"""Online outcome learning with embargoed, chronological Ridge challenger selection."""
from datetime import timedelta
from math import log1p, expm1
from typing import Protocol
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from .models import Forecast, Snapshot, ModelRun
from .metrics import aware

FEATURE_KEYS = ["velocity", "acceleration", "subscriber_conversion", "watchtime_efficiency", "strategy_evidence", "age_days"]


class Predictor(Protocol):
    def predict(self, features: dict, horizon_hours: int) -> dict: ...


def vector(f):
    # Missing flags prevent unknown from silently becoming an observed zero.
    return [float(f.get(k) or 0) for k in FEATURE_KEYS] + [float(f.get(k) is None) for k in FEATURE_KEYS]


def mature(session, now):
    for forecast in session.scalars(select(Forecast).where(Forecast.actual_views.is_(None), Forecast.target_at <= now)):
        snap = session.scalar(select(Snapshot).where(
            Snapshot.video_id == forecast.video_id,
            Snapshot.observed_at >= forecast.target_at,
            Snapshot.observed_at <= now,
            Snapshot.observed_at <= aware(forecast.target_at) + timedelta(hours=2)
        ).order_by(Snapshot.observed_at))
        if snap is None or snap.views < forecast.origin_views:
            continue
        forecast.actual_views = snap.views
        forecast.actual_at = snap.observed_at
        forecast.absolute_error = abs(snap.views-forecast.predicted_views)


def predict(session, video_id, origin, f, horizon):
    cutoff = aware(origin.observed_at)
    history = list(session.scalars(select(Forecast).where(
        Forecast.horizon_hours == horizon, Forecast.actual_views.is_not(None),
        Forecast.actual_at < cutoff).order_by(Forecast.origin_at)))
    # Use non-overlapping outcomes per video to avoid hourly pseudo-replication.
    independent, last = [], {}
    for row in history:
        if row.video_id not in last or aware(row.origin_at) >= last[row.video_id]:
            independent.append(row)
            last[row.video_id] = aware(row.target_at)
    history = [r for r in independent if r.features.get("content_type") == f.get("content_type")]
    baseline_gain = max(0, f["velocity"])*horizon
    gain = baseline_gain
    version = "velocity-v1"
    residuals = []
    n = len(history)
    if n >= 10:
        # Multiplicative correction is learned from prior realized outcomes.
        ratios = [(r.actual_views-r.origin_views+1)/(max(0, r.features["velocity"])*horizon+1) for r in history]
        factor = float(np.clip(np.median(ratios[-100:]), 0.1, 5))
        gain = max(0, (baseline_gain+1)*factor-1)
        version = "adaptive-velocity-v1"
    if n >= 50:
        split = int(n*0.8)
        validation = history[split:]
        # Training labels must have matured before the validation origins.
        train = [r for r in history[:split] if aware(r.actual_at) < aware(validation[0].origin_at)]
        if len(train) >= 30 and len(validation) >= 10:
            def fit(rows):
                model = make_pipeline(StandardScaler(), Ridge(alpha=10))
                model.fit([vector(r.features) for r in rows], [log1p(r.actual_views-r.origin_views) for r in rows])
                return model
            model = fit(train)
            estimates = np.maximum(0, np.expm1(np.clip(model.predict([vector(r.features) for r in validation]), 0, 25)))
            actuals = np.array([r.actual_views-r.origin_views for r in validation])
            baseline = np.array([max(0, r.features["velocity"])*horizon for r in validation])
            mae = float(np.mean(np.abs(estimates-actuals)))
            raw_mae = float(np.mean(np.abs(baseline-actuals)))
            correction = float(np.clip(np.median([(r.actual_views-r.origin_views+1)/
                (max(0,r.features["velocity"])*horizon+1) for r in train]),0.1,5))
            corrected_baseline = np.maximum(0,(baseline+1)*correction-1)
            baseline_mae = min(raw_mae, float(np.mean(np.abs(corrected_baseline-actuals))))
            accepted = mae < baseline_mae*0.95
            model_run = ModelRun(horizon_hours=horizon, training_rows=len(train),
                parameters={"model": "Ridge", "alpha": 10, "features": FEATURE_KEYS},
                metrics={"mae": mae, "baseline_mae": baseline_mae, "accepted": accepted})
            session.add(model_run)
            if accepted:
                champion = fit(history)
                gain = max(0, expm1(float(np.clip(champion.predict([vector(f)])[0], 0, 25))))
                model_run.parameters = {**model_run.parameters,
                    "origin_cutoff": cutoff.isoformat(),
                    "training_forecast_ids": [r.id for r in history],
                    "validation_forecast_ids": [r.id for r in validation],
                    "scaler_mean": champion[0].mean_.tolist(),
                    "scaler_scale": champion[0].scale_.tolist(),
                    "coefficients": champion[1].coef_.tolist(),
                    "intercept": float(champion[1].intercept_)}
                version = "ridge-v1"
    # Out-of-sample stored errors (not training residuals). Horizon and model match.
    residuals = [log1p(r.actual_views-r.origin_views)-log1p(max(0, r.predicted_views-r.origin_views))
                 for r in history if r.model_version == version]
    calibration_n = len(residuals)
    if calibration_n >= 30:
        samples = np.maximum(0, np.expm1(np.clip(log1p(gain)+np.array(residuals), 0, 25))) + origin.views
        low, high = [float(x) for x in np.quantile(samples, [0.1, 0.9])]
        def probability(threshold):
            if origin.views >= threshold:
                return 1.0
            hits = int(np.sum(samples >= threshold))
            # Do not invent rare-event probabilities outside observed residual support.
            if min(hits, len(samples)-hits) < 5:
                return None
            return (hits+1)/(len(samples)+2)
        p100k, p1m = probability(100_000), probability(1_000_000)
    else:
        # Scenario range, explicitly not a statistically calibrated interval.
        low, high = origin.views + gain*0.25, origin.views + gain*4
        p100k = p1m = None
    return Forecast(video_id=video_id, origin_at=cutoff, target_at=cutoff+timedelta(hours=horizon),
        horizon_hours=horizon, origin_views=origin.views, predicted_views=origin.views+gain,
        lower_views=low, upper_views=high, p100k=p100k, p1m=p1m,
        features=f, model_version=version, calibration_n=calibration_n)
