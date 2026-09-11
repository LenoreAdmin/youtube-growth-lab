"""Statistical hardening: correlated day rows of few videos never masquerade as a large independent sample."""
from datetime import datetime, timedelta, timezone
import numpy as np
from sqlalchemy import select
from app import history, backtest, regimes, strategy, learning
from app.models import Video, Channel, LearningDataset
from test_learning_v4 import seed_history, wire, NOW, LAG


def synthetic_rows(n_videos, per_video, ratio=1.0, start=0):
    rows = []
    for v in range(start, start+n_videos):
        for i in range(per_video):
            rows.append({"video_id": f"v{v}", "origin": datetime(2026, 1, 1).date()+timedelta(days=i), "horizon_hours": 168,
                         "features": {"ratio_7_28": ratio*(1+v)+i*0.001, "accel_7d": 0.0, "velocity_7d": 10, "views_28d": 280},
                         "target_views": 70, "target_ratio": 1.0, "target": {}})
    return rows


def test_sample_sizes_separate_rows_origins_and_videos():
    rows = synthetic_rows(3, 1000)
    sizes = history.sample_sizes(rows)
    assert sizes == {**sizes, "n_rows": 3000, "n_origins": 1000, "n_videos": 3}
    assert sizes["rows_per_video"] == {"v0": 1000, "v1": 1000, "v2": 1000} and abs(sizes["largest_video_share"]-1/3) < 1e-9
    assert "korreliert" in sizes["independence_note"]


def test_confidence_is_capped_at_low_below_ten_videos_regardless_of_rows():
    many_rows = {"status": "ok", "n_rows": 5000, "n_origins": 1700, "n_videos": 3, "medians": {}}
    strong = {"status": "backtested", "accepted": True, "cross_video": {"status": "backtested", "accepted": True}}
    conf = strategy.confidence(many_rows, strong, {})
    assert conf["level"] == "low" and conf["max_level_for_n_videos"] == "low"
    assert conf["n_rows"] == 5000 and conf["n_origins"] == 1700 and conf["n_videos"] == 3
    assert "korrelierte Tageszeilen" in conf["reason"] and "nicht als allgemein" in conf["reason"]
    assert conf["scope"] == {"temporal_within_video_validation": "accepted", "cross_video_generalization": "insufficient_data"}
    assert conf["cross_video_accepted"] is False
    enough = {**many_rows, "n_videos": 12}
    assert strategy.confidence(enough, {"status": "backtested", "accepted": True, "cross_video": {"status": "insufficient_data"}}, {})["level"] == "low"
    assert strategy.confidence(enough, {"status": "backtested", "accepted": False, "cross_video": {"status": "backtested", "accepted": True}}, {})["level"] == "low"
    assert strategy.confidence(enough, strong, {})["level"] == "moderate"
    assert strategy.confidence({**many_rows, "status": "insufficient_data"}, strong, {})["level"] == "insufficient_data"
    rec = strategy.recommend({"traffic_search": .5, "traffic_total_7d": 10, "age_days": 100, "retention_avg": .4}, {"regime": "growing", "reason": "x"},
                             many_rows, strong, [], [])
    assert rec["generalization"]["status"] == "insufficient_data" and rec["generalization"]["required_videos"] == 10
    assert rec["targets"]["100000"]["status"] == "insufficient_data" and rec["confidence"]["level"] == "low"
    assert rec["next"]  # operational advice from within-video validation is still produced


def test_weighted_quantiles_balance_videos_and_report_dominance():
    values = list(range(100))
    assert abs(backtest.weighted_quantile(values, np.ones(100), .5)-np.quantile(values, .5)) < 1.0
    dominant = synthetic_rows(1, 3000, ratio=5.0)+synthetic_rows(2, 30, ratio=0.2, start=1)
    base = regimes.baselines(dominant)
    pooled_q50 = float(np.percentile([r["features"]["ratio_7_28"] for r in dominant], 50))
    assert base["weighting"] == "video_balanced" and base["n_videos"] == 3 and base["n_rows"] == 3060
    assert base["largest_video_share"] > 0.9 and base["rows_per_video"]["v0"] == 3000
    # The pooled median sits inside the long video's range (>5); with equal video weight it falls to the small videos (<1).
    assert pooled_q50 > 5 and base["ratio_7_28"]["q50"] < 1
    assert base["ratio_7_28"]["q95"] > base["ratio_7_28"]["q75"] > base["ratio_7_28"]["q50"]
    weights = backtest.video_weights(dominant)
    per_video = {}
    for r, w in zip(dominant, weights):
        per_video[r["video_id"]] = per_video.get(r["video_id"], 0)+w
    assert max(per_video.values())-min(per_video.values()) < 1e-6


def test_walk_forward_reports_scopes_and_cross_video_stays_insufficient_for_three_videos(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=800)
    seed_history(session, "b", days=800, base=300, seed=4)
    rows, _ = history.build(history.load(session), 168, LAG)
    result = backtest.walk_forward(rows, 168, LAG)
    assert result["validation_scope"] == "temporal_within_video_validation"
    assert result["n_videos"] == 2 and result["n_origins"] < result["n_rows"] and result["sample_sizes"]["n_videos"] == 2
    assert result["cross_video"]["status"] == "insufficient_data" and result["cross_video"]["required_videos"] == 10
    assert "nicht als allgemein" in result["cross_video"]["note"]
    champion = result["models"][result["champion"]]
    assert "mae_video_balanced" in champion and champion["n_videos"] == 2
    if result["accepted"]:
        assert champion["mae_video_balanced"] < champion["baseline_mae_video_balanced_same_rows"]*backtest.IMPROVEMENT
    breakout = backtest.breakout_backtest(rows, LAG)
    assert breakout["validation_scope"] == "temporal_within_video_validation" and breakout["threshold_weighting"] == "video_balanced"


def test_cross_video_generalization_activates_with_ten_videos(monkeypatch, session):
    wire(monkeypatch, session)
    for i in range(8):
        session.add(Video(id=f"v{i}", channel_id="channel", title=f"v{i}", published_at=NOW, duration_seconds=300))
    session.commit()
    for i, video_id in enumerate(["a", "b"]+[f"v{i}" for i in range(8)]):
        seed_history(session, video_id, days=560, base=50+40*i, seed=10+i)
    rows, _ = history.build(history.load(session), 168, LAG)
    result = backtest.walk_forward(rows, 168, LAG)
    cross = result["cross_video"]
    assert result["n_videos"] == 10 and cross["status"] == "backtested" and cross["folds"] > 0
    assert cross["scope"] == "cross_video_generalization" and set(backtest.BASELINES) <= set(cross["models"])
    assert cross["accepted"] == (cross["champion"] in backtest.CANDIDATES)


def test_dataset_and_overview_expose_separate_sample_sizes(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=600)
    seed_history(session, "b", days=600, base=200, seed=6)
    learning.refresh(session, NOW)
    session.expire_all()
    view = learning.overview(session, NOW)
    sizes = view["dataset"]["sample_sizes"]["168"]
    assert sizes["n_videos"] == 2 and sizes["n_origins"] < sizes["n_rows"]
    assert view["generalization"] == {"scope": "cross_video_generalization", "n_videos": 2, "required_videos": 10,
                                      "status": "insufficient_data", "temporal_scope": "temporal_within_video_validation"}
    bt = view["backtests"]["168"]
    assert bt["validation_scope"] == "temporal_within_video_validation" and bt["cross_video"]["status"] == "insufficient_data"
    assert bt["n_origins"] and bt["n_videos"] == 2
    rec = view["videos"]["a"]["recommendation"]
    assert rec["confidence"]["level"] in ("low", "insufficient_data") and rec["generalization"]["status"] == "insufficient_data"
    assert view["dataset"]["baselines"]["weighting"] == "video_balanced"
    assert any("n_videos" in limit for limit in view["limits"])
