"""Data-driven growth regimes: thresholds are video-balanced channel quantiles, never fixed view counts."""
import numpy as np
from .backtest import weighted_quantile, video_weights
from .history import sample_sizes

VERSION = "regimes-v4"
REGIMES = ["declining", "stable", "growing", "accelerating", "breakout_candidate", "breakout", "paid_excluded", "insufficient_data"]
MIN_BASELINE_ROWS = 100
QUANTILES = (5, 25, 50, 75, 90, 95, 99)


def _weighted(rows, key):
    kept = [r for r in rows if r["features"].get(key) is not None]
    return [r["features"][key] for r in kept], video_weights(kept) if kept else np.array([])


def baselines(rows):
    """Channel distribution of organic 7d/28d pace ratio and week-over-week acceleration.

    Every video carries equal total weight, so one long history cannot define the channel's
    normal on its own; the dominance of the largest video is reported alongside.
    """
    sizes = sample_sizes(rows)
    ratios, ratio_weights = _weighted(rows, "ratio_7_28")
    accels, accel_weights = _weighted(rows, "accel_7d")
    medians = {}
    for key in ("retention_avg", "subscriber_conversion_7d", "pct_7d", "ctr_7d", "traffic_search", "traffic_suggested", "traffic_external", "like_rate_7d"):
        values, weights = _weighted(rows, key)
        kept = [r for r in rows if r["features"].get(key) is not None]
        medians[key] = {"median": weighted_quantile(values, weights, .5), "n": len(values),
                        "n_videos": len({r["video_id"] for r in kept})} if values else {"median": None, "n": 0, "n_videos": 0}
    q = lambda values, weights: {f"q{p:02d}": weighted_quantile(values, weights, p/100) for p in QUANTILES} if len(values) else {}
    return {"version": VERSION, "n_rows": len(ratios), "n_origins": sizes["n_origins"], "n_videos": sizes["n_videos"],
            "rows_per_video": sizes["rows_per_video"], "largest_video_share": sizes["largest_video_share"],
            "weighting": "video_balanced", "ratio_7_28": q(ratios, ratio_weights), "accel_7d": q(accels, accel_weights), "medians": medians,
            "status": "ok" if len(ratios) >= MIN_BASELINE_ROWS else "insufficient_data",
            "bias_note": "Schwellen sind video-balanciert; mit wenigen Videos beschreiben sie diesen Kanal, nicht neue Videos."}


def classify(f, base):
    """Descriptive regime with the thresholds that produced it; no probability claim."""
    if f is None:
        return {"regime": "insufficient_data", "reason": "Weniger als 28 Tage Analytics-Historie.", "version": VERSION}
    if f.get("paid_views_32d", 0) > 0:
        return {"regime": "paid_excluded", "reason": f"{f['paid_views_32d']} als Werbung klassifizierte Views in den letzten 32 bekannten Tagen.",
                "version": VERSION}
    if base.get("status") != "ok" or f.get("ratio_7_28") is None:
        return {"regime": "insufficient_data", "reason": "Kanal-Baseline noch nicht belastbar oder keine Views im 28-Tage-Fenster.",
                "version": VERSION, "baseline_n": base.get("n_rows", 0)}
    r, a = f["ratio_7_28"], f["accel_7d"]
    rq, aq = base["ratio_7_28"], base["accel_7d"]
    sustained = f.get("days_above_28d_last3", 0) >= 3
    if r >= rq["q95"] and a >= aq["q75"] and sustained:
        regime = "breakout"
    elif r >= rq["q95"] or a >= aq["q95"]:
        regime = "breakout_candidate"
    elif r >= rq["q75"] and a >= aq["q75"]:
        regime = "accelerating"
    elif r >= rq["q75"]:
        regime = "growing"
    elif r <= rq["q25"]:
        regime = "declining"
    else:
        regime = "stable"
    return {"regime": regime, "version": VERSION, "ratio_7_28": r, "accel_7d": a, "sustained_3d": sustained,
            "thresholds": {"ratio_q25": rq["q25"], "ratio_q75": rq["q75"], "ratio_q95": rq["q95"], "accel_q75": aq["q75"], "accel_q95": aq["q95"]},
            "baseline_n": base["n_rows"], "baseline_videos": base["n_videos"],
            "reason": f"7-Tage-Tempo = {r:.2f}× 28-Tage-Tempo; Wochenänderung {a*100:+.0f} %; Schwellen aus {base['n_rows']} organischen Kanaltagen."}
