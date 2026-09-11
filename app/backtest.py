"""Walk-forward backtesting: only the past trains the future, baselines must be beaten out of sample."""
from datetime import timedelta
from math import log1p, expm1
import numpy as np
import sklearn
from scipy.stats import spearmanr
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from .history import FEATURES, HORIZONS, vector, sample_sizes

VERSION = "backtest-v4"
WITHIN = "temporal_within_video_validation"
CROSS = "cross_video_generalization"
MIN_CROSS_VIDEOS = 10
BASELINES = ("baseline_7d", "baseline_28d")
CANDIDATES = ("ridge", "gbr")
IMPROVEMENT = 0.95  # A model must cut the best baseline's MAE by at least five percent.
MIN_TRAIN, MIN_TEST, MIN_GBR_TRAIN, MIN_CALIBRATION = 30, 5, 200, 30
MIN_BREAKOUT_POSITIVES = 30


def baseline_prediction(name, f, horizon_hours):
    days = HORIZONS[horizon_hours]
    if name == "baseline_7d":
        return max(0.0, f["velocity_7d"])*days
    return max(0.0, f["views_28d"]/28)*days


def video_weights(rows):
    """Equal total weight per video so one long history cannot dominate a fit or a quantile."""
    counts = {}
    for r in rows:
        counts[r["video_id"]] = counts.get(r["video_id"], 0)+1
    return np.array([len(rows)/(len(counts)*counts[r["video_id"]]) for r in rows])


def fit(name, rows):
    x = [vector(r["features"]) for r in rows]
    y = [log1p(max(0, r["target_views"])) for r in rows]
    weights = video_weights(rows)
    if name == "ridge":
        model = make_pipeline(StandardScaler(), Ridge(alpha=10))
        model.fit(x, y, ridge__sample_weight=weights)
    elif name == "gbr":
        model = GradientBoostingRegressor(n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)
        model.fit(x, y, sample_weight=weights)
    else:
        raise ValueError(name)
    return model


def predict(model, f):
    return float(max(0.0, expm1(np.clip(model.predict([vector(f)])[0], 0, 25))))


def subsample(rows, spacing_days):
    """Weekly-spaced origins per video reduce day-to-day pseudo-replication."""
    first = {}
    kept = []
    for r in sorted(rows, key=lambda r: (r["origin"], r["video_id"])):
        anchor = first.setdefault(r["video_id"], r["origin"])
        if (r["origin"]-anchor).days % spacing_days == 0:
            kept.append(r)
    return kept


def metrics(pairs, rows=None):
    """Pooled errors plus a video-balanced MAE (mean of per-video MAEs) and separate sample sizes."""
    if not pairs:
        return {"n": 0}
    errors = np.array([p-a for p, a in pairs], dtype=float)
    logs = np.array([log1p(max(0, a))-log1p(max(0, p)) for p, a in pairs])
    out = {"n": len(pairs), "mae": float(np.mean(np.abs(errors))), "rmse": float(np.sqrt(np.mean(errors**2))),
           "median_ae": float(np.median(np.abs(errors))), "log_mae": float(np.mean(np.abs(logs))), "bias": float(np.mean(errors))}
    if rows:
        per_video = {}
        for (p, a), r in zip(pairs, rows):
            per_video.setdefault(r["video_id"], []).append(abs(p-a))
        out.update(mae_video_balanced=float(np.mean([np.mean(v) for v in per_video.values()])),
                   n_videos=len(per_video), n_origins=len({r["origin"] for r in rows}))
    return out


def _evaluate(rows, horizon_hours, lag_days, min_train_days, step_days, budget, scope):
    """Shared walk-forward loop. Within-video: train on everything observed before the cut.
    Cross-video: train only on OTHER videos observed before the cut, test on each held-out video."""
    days = HORIZONS[horizon_hours]
    predictions = {name: [] for name in BASELINES+CANDIDATES}
    rows_by_id = {id(r): r for r in rows}
    folds = []
    first, last = rows[0]["origin"], rows[-1]["origin"]
    cut = first+timedelta(days=min_train_days)
    while cut <= last:
        if budget:
            budget.check()
        observed_by = cut-timedelta(days=days+lag_days)
        train_all = [r for r in rows if r["origin"] <= observed_by]
        test_all = [r for r in rows if cut <= r["origin"] < cut+timedelta(days=step_days)]
        cut += timedelta(days=step_days)
        groups = [(None, train_all, test_all)] if scope == WITHIN else [
            (v, [r for r in train_all if r["video_id"] != v], [r for r in test_all if r["video_id"] == v]) for v in sorted({r["video_id"] for r in test_all})]
        for held_out, train, test in groups:
            if len(train) < MIN_TRAIN or len(test) < MIN_TEST or (scope == CROSS and len({r["video_id"] for r in train}) < 2):
                continue
            fold = {"cut": str(cut-timedelta(days=step_days)), "n_train": len(train), "n_test": len(test), "models": list(BASELINES),
                    "train_videos": len({r["video_id"] for r in train}), "held_out_video": held_out}
            for name in BASELINES:
                predictions[name] += [(baseline_prediction(name, r["features"], horizon_hours), r["target_views"], id(r)) for r in test]
            for name in CANDIDATES:
                if name == "gbr" and len(train) < MIN_GBR_TRAIN:
                    continue
                model = fit(name, train)
                predictions[name] += [(predict(model, r["features"]), r["target_views"], id(r)) for r in test]
                fold["models"].append(name)
            folds.append(fold)
    models = {}
    for name, pairs in predictions.items():
        if not pairs:
            continue
        ids = {row_id for _, _, row_id in pairs}
        entry = metrics([(p, a) for p, a, _ in pairs], [rows_by_id[i] for _, _, i in pairs])
        # Fair comparison: the best baseline restricted to exactly the rows this model predicted.
        if name in CANDIDATES:
            same = {b: metrics([(p, a) for p, a, row_id in predictions[b] if row_id in ids], [rows_by_id[i] for _, _, i in predictions[b] if i in ids]) for b in BASELINES}
            best = min(same, key=lambda b: same[b]["mae"])
            entry["baseline_mae_same_rows"] = same[best]["mae"]
            entry["baseline_mae_video_balanced_same_rows"] = same[best].get("mae_video_balanced")
        else:
            entry["baseline_mae_same_rows"] = None
        models[name] = entry
    return models, predictions, folds


def _select(models, predictions):
    best_baseline = min((n for n in BASELINES if n in models), key=lambda n: models[n]["mae"])
    champion, accepted = best_baseline, False
    for name in CANDIDATES:
        entry = models.get(name)
        # Pooled AND video-balanced MAE must improve: one long video may not carry the acceptance alone.
        if entry and entry["n"] >= MIN_CALIBRATION and entry["mae"] < entry["baseline_mae_same_rows"]*IMPROVEMENT \
                and entry.get("mae_video_balanced", 0) < (entry.get("baseline_mae_video_balanced_same_rows") or 0)*IMPROVEMENT:
            if not accepted or entry["mae"] < models[champion]["mae"]:
                champion, accepted = name, True
    residuals = [log1p(max(0, a))-log1p(max(0, p)) for p, a, _ in predictions[champion]]
    quantiles = {"n": len(residuals)}
    if len(residuals) >= MIN_CALIBRATION:
        quantiles.update(q10=float(np.quantile(residuals, .1)), q90=float(np.quantile(residuals, .9)), kind="empirical_80")
    else:
        quantiles["kind"] = "uncalibrated_scenario"
    return best_baseline, champion, accepted, quantiles


def walk_forward(rows, horizon_hours, lag_days, spacing_days=7, min_train_days=365, step_days=91, budget=None):
    """Expanding-window folds with an outcome embargo: a training target must be fully observed before the cut.

    Scope is temporal validation on the channel's existing videos. Generalization to new
    videos is evaluated separately and stays insufficient_data below MIN_CROSS_VIDEOS.
    """
    rows = subsample(rows, spacing_days)
    days = HORIZONS[horizon_hours]
    sizes = sample_sizes(rows)
    result = {"version": VERSION, "horizon_hours": horizon_hours, "validation_scope": WITHIN, "n_rows": sizes["n_rows"],
              "n_origins": sizes["n_origins"], "n_videos": sizes["n_videos"], "sample_sizes": sizes, "folds": [],
              "libraries": {"sklearn": sklearn.__version__, "numpy": np.__version__},
              "config": {"spacing_days": spacing_days, "min_train_days": min_train_days, "step_days": step_days, "embargo_days": days+lag_days,
                         "improvement": IMPROVEMENT, "min_train": MIN_TRAIN, "min_test": MIN_TEST, "min_gbr_train": MIN_GBR_TRAIN,
                         "weighting": "video_balanced", "min_cross_videos": MIN_CROSS_VIDEOS},
              "cross_video": {"scope": CROSS, "status": "insufficient_data", "n_videos": sizes["n_videos"], "required_videos": MIN_CROSS_VIDEOS,
                              "note": "Muster sind auf bestehenden Videos zeitlich validiert, nicht als allgemein für neue Videos bewiesen."}}
    if not rows:
        return {**result, "status": "insufficient_data", "champion": "baseline_7d", "accepted": False, "models": {}}
    models, predictions, folds = _evaluate(rows, horizon_hours, lag_days, min_train_days, step_days, budget, WITHIN)
    result["folds"] = folds
    if not any(name in models for name in BASELINES):
        return {**result, "status": "insufficient_data", "champion": "baseline_7d", "accepted": False, "models": models}
    best_baseline, champion, accepted, quantiles = _select(models, predictions)
    if sizes["n_videos"] >= MIN_CROSS_VIDEOS:
        cross_models, cross_predictions, cross_folds = _evaluate(rows, horizon_hours, lag_days, min_train_days, step_days, budget, CROSS)
        if any(name in cross_models for name in BASELINES):
            cross_best, cross_champion, cross_accepted, _ = _select(cross_models, cross_predictions)
            result["cross_video"].update(status="backtested", models=cross_models, folds=len(cross_folds), champion=cross_champion,
                                         accepted=cross_accepted, best_baseline=cross_best)
    return {**result, "status": "backtested", "champion": champion, "accepted": accepted, "best_baseline": best_baseline,
            "models": models, "residual_quantiles": quantiles}


def breakout_backtest(rows, lag_days, quantile=0.95, spacing_days=7, min_train_days=365, step_days=91):
    """Probability calibration for 'next 7 days far above trailing pace'; abstains without enough positives."""
    rows = subsample(rows, spacing_days)
    days = HORIZONS[168]
    sizes = sample_sizes(rows)
    out = {"version": VERSION, "quantile": quantile, "validation_scope": WITHIN, "n_rows": sizes["n_rows"], "n_origins": sizes["n_origins"],
           "n_videos": sizes["n_videos"], "status": "insufficient_data", "positives": 0, "threshold_weighting": "video_balanced"}
    if not rows:
        return out
    base, model, first, last = [], [], rows[0]["origin"], rows[-1]["origin"]
    cut = first+timedelta(days=min_train_days)
    positives = 0
    while cut <= last:
        observed_by = cut-timedelta(days=days+lag_days)
        train = [r for r in rows if r["origin"] <= observed_by]
        test = [r for r in rows if cut <= r["origin"] < cut+timedelta(days=step_days)]
        cut += timedelta(days=step_days)
        if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
            continue
        threshold = weighted_quantile([r["target_ratio"] for r in train], video_weights(train), quantile)
        labels = [int(r["target_ratio"] >= threshold) for r in train]
        positives = max(positives, sum(labels))
        if sum(labels) < MIN_BREAKOUT_POSITIVES or sum(labels) == len(labels):
            continue
        rate = sum(labels)/len(labels)
        clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))
        clf.fit([vector(r["features"]) for r in train], labels, logisticregression__sample_weight=video_weights(train))
        for r in test:
            actual = int(r["target_ratio"] >= threshold)
            base.append((rate, actual))
            model.append((float(clf.predict_proba([vector(r["features"])])[0][1]), actual))
    out["positives"] = positives
    if len(model) < MIN_CALIBRATION:
        return out
    brier = lambda pairs: float(np.mean([(p-a)**2 for p, a in pairs]))
    out.update(status="backtested", n_test=len(model), brier_model=brier(model), brier_base_rate=brier(base),
               accepted=brier(model) < brier(base)*IMPROVEMENT)
    return out


def weighted_quantile(values, weights, q):
    """Quantile of a weighted sample (weights normalised); equals np.quantile for equal weights."""
    values, weights = np.asarray(values, dtype=float), np.asarray(weights, dtype=float)
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights)-0.5*weights
    return float(np.interp(q*weights.sum(), cumulative, values))


def associations(rows, champion_model=None):
    """Observed, non-causal associations between features and relative future growth."""
    signals = []
    if champion_model is not None and hasattr(champion_model, "steps"):
        scaler, ridge = champion_model.steps[0][1], champion_model.steps[-1][1]
        if hasattr(ridge, "coef_"):
            for name, coef in zip(FEATURES, ridge.coef_[:len(FEATURES)]):
                signals.append({"feature": name, "kind": "standardized_ridge_coefficient", "value": float(coef)})
    ratios = [r["target_ratio"] for r in rows]
    for name in FEATURES:
        values = [(r["features"].get(name), t) for r, t in zip(rows, ratios) if r["features"].get(name) is not None]
        if len(values) >= MIN_CALIBRATION and len({v for v, _ in values}) > 1:
            rho = spearmanr([v for v, _ in values], [t for _, t in values])
            corr = float(rho.statistic if hasattr(rho, "statistic") else rho[0])
            if not np.isnan(corr):
                signals.append({"feature": name, "kind": "spearman_with_target_ratio", "value": corr, "n": len(values)})
    return signals
