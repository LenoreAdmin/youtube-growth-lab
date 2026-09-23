"""Data-driven growth regimes: thresholds are video-balanced channel quantiles, never fixed view counts."""
import numpy as np
from .backtest import weighted_quantile, video_weights
from .history import sample_sizes

VERSION = "regimes-v4"
REGIMES = ["declining", "stable", "growing", "accelerating", "breakout_candidate", "breakout", "paid_excluded", "insufficient_data"]
MIN_BASELINE_ROWS = 100
MIN_MEDIAN_N = 30
MIN_WEEKLY_VIEWS = 50   # Below this, momentum/acceleration is noise: never classify a breakout.
QUANTILES = (5, 25, 50, 75, 90, 95, 99)


def usable_median(ref):
    """A channel median is only a yardstick with enough rows AND a non-zero value."""
    ref = ref or {}
    return bool(ref.get("median")) and (ref.get("n") or 0) >= MIN_MEDIAN_N


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
    for key in ("retention_avg", "subscriber_conversion_7d", "pct_7d", "ctr_7d", "traffic_search", "traffic_suggested", "traffic_external",
                "traffic_browse", "traffic_shorts", "like_rate_7d", "velocity_7d"):
        values, weights = _weighted(rows, key)
        kept = [r for r in rows if r["features"].get(key) is not None]
        entry = {"median": weighted_quantile(values, weights, .5), "n": len(values),
                 "n_videos": len({r["video_id"] for r in kept})} if values else {"median": None, "n": 0, "n_videos": 0}
        # A median of 0 (the channel is inactive on most days) is not a threshold anyone can fall below.
        entry["usable"] = usable_median(entry)
        medians[key] = entry
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
    if f.get("paid_views_32d", 0) > 0 or f.get("paid_views_lag_gap", 0) > 0:
        profile = f.get("paid") or {}
        detail = ("Werbe-Views in den letzten 7 bekannten Tagen oder in der Analytics-Lücke" if profile.get("status") == "paid_excluded"
                  else f"letzter Werbetag {profile.get('last_paid_day')}, sauberes Fenster {profile.get('clean_days')} von {profile.get('required_clean_days', 32)} Tagen")
        return {"regime": "paid_excluded", "reason": f"{f.get('paid_views_32d', 0)+f.get('paid_views_lag_gap', 0)} als Werbung klassifizierte Views "
                f"im 32-Tage-Fenster ({detail}); organisches Regime nicht identifizierbar.", "version": VERSION, "paid": profile}
    if base.get("status") != "ok" or f.get("ratio_7_28") is None:
        return {"regime": "insufficient_data", "reason": "Kanal-Baseline noch nicht belastbar oder keine Views im 28-Tage-Fenster.",
                "version": VERSION, "baseline_n": base.get("n_rows", 0)}
    views_7d, views_28d = f.get("views_7d"), f.get("views_28d") or 0
    if views_7d is not None and views_7d < MIN_WEEKLY_VIEWS and views_28d < 4*MIN_WEEKLY_VIEWS:
        # Absolute activity floor: a video with almost no delivery can never be "accelerating",
        # so it can never be protected as momentum. The distribution gap is the real finding.
        return {"regime": "insufficient_data", "low_activity": True, "version": VERSION,
                "views_7d": views_7d, "views_28d": views_28d, "min_weekly_views": MIN_WEEKLY_VIEWS,
                "reason": f"Nur {views_7d} Views in 7 und {views_28d} in 28 bekannten Tagen (Schwelle {MIN_WEEKLY_VIEWS}/Woche): "
                          "Momentum ist auf dieser Menge nicht messbar; keine Breakout-/Beschleunigungseinstufung."}
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
