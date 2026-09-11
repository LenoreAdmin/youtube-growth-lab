"""V4 API and dashboard wiring: APP_TOKEN only, preview-disabled, redacted, JSON-safe."""
import subprocess
from pathlib import Path
from unittest.mock import Mock
import pytest
from app.config import settings
from test_api import client  # noqa: F401


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer cron-test-secret"])
def test_learning_endpoints_require_app_token(client, monkeypatch, authorization):
    import app.main as main
    run = Mock()
    monkeypatch.setattr(main.learning_module, "refresh", run)
    monkeypatch.setattr(settings, "cron_secret", "cron-test-secret")
    headers = {"Authorization": authorization} if authorization else {}
    assert client.get("/api/learning", headers=headers).status_code == 401
    assert client.post("/api/learning/rebuild", headers=headers).status_code == 401
    run.assert_not_called()


def test_learning_rebuild_preview_and_redaction(client, monkeypatch):
    import app.main as main
    headers = {"Authorization": "Bearer test-token-only"}
    run = Mock(return_value={"status": "ok", "scored": 0, "forecasts_created": 0, "videos_advised": 2, "dataset": "abc"})
    monkeypatch.setattr(main.learning_module, "refresh", run)
    assert client.post("/api/learning/rebuild", headers=headers).json()["videos_advised"] == 2
    assert run.call_args.kwargs["force"] is True
    monkeypatch.setattr(settings, "vercel_env", "preview")
    assert client.post("/api/learning/rebuild", headers=headers).status_code == 403
    monkeypatch.setattr(settings, "vercel_env", "production")
    monkeypatch.setattr(main.learning_module, "refresh", Mock(side_effect=RuntimeError("private-token-must-not-appear")))
    response = client.post("/api/learning/rebuild", headers=headers)
    assert response.status_code == 503 and "private-token" not in response.text


def test_dashboard_and_detail_expose_v4_end_to_end(client, session, monkeypatch):
    from test_learning_v4 import seed_history, wire, NOW
    from app import learning
    wire(monkeypatch, session)
    seed_history(session, "a", days=500)
    seed_history(session, "b", days=500, base=200, seed=9)
    headers = {"Authorization": "Bearer test-token-only"}
    empty = client.get("/api/dashboard", headers=headers).json()["learning_v4"]
    assert empty["dataset"] is None and empty["videos"]["a"]["regime"] == "insufficient_data"
    learning.refresh(session, NOW)
    session.expire_all()
    dashboard = client.get("/api/dashboard", headers=headers).json()
    v4 = dashboard["learning_v4"]
    assert set(v4["backtests"]) == {"24", "168", "720"}
    assert v4["dataset"]["audit"]["n_videos"] == 2 and v4["scorecard"]["168"]["n"] == 0
    video = v4["videos"]["a"]
    assert video["regime"] in ("declining", "stable", "growing", "accelerating", "breakout_candidate", "breakout")
    assert len(video["forecasts"]) == 3 and video["recommendation"]["next"]
    assert video["recommendation"]["targets"]["1000000"]["status"] == "insufficient_data"
    status = client.get("/api/learning", headers=headers).json()
    assert status["dataset"]["signature"] == v4["dataset"]["signature"]
    detail = client.get("/api/videos/a", headers=headers).json()
    assert detail["strategy"]["recommendation"]["version"] == "strategy-v4"
    assert detail["strategy"]["forecasts"][0]["horizon_hours"] == 24


def test_v4_dashboard_javascript_renders_without_dom_errors():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    prefix = source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:'',disabled:false})};
state={learning_v4:{dataset:{signature:'abcdef123456789',built_at:'2026-09-11T00:00:00Z',rows_per_horizon:{24:10,168:9,720:5},
 audit:{n_videos:2,videos:[{video_id:'a',first_day:'2016-02-01',last_day:'2026-09-08',analytics_days:3000,paid_days:26,retention_windows:120,reach_days:30}],excluded_feature_sources:['snapshots']},
 baselines:{status:'ok',n_rows:400,weighting:'video_balanced'},exclusions:{168:{paid_feature_window:40}},sample_sizes:{168:{n_rows:3000,n_origins:1000,n_videos:3,largest_video_share:.6}}},
 backtests:{168:{champion:'ridge',accepted:true,best_baseline:'baseline_7d',n_rows:400,n_origins:140,n_videos:3,folds:6,validation_scope:'temporal_within_video_validation',cross_video:{status:'insufficient_data',n_videos:3,required_videos:10},models:{baseline_7d:{mae:10,rmse:12},ridge:{mae:8,rmse:11,mae_video_balanced:8.5}},
 residual_quantiles:{kind:'empirical_80',n:100},breakout:{status:'insufficient_data',positives:12},signals:[{feature:'retention_avg',kind:'spearman_with_target_ratio',value:.3,n:400},{feature:'age_days',kind:'spearman_with_target_ratio',value:-.2,n:400}]}},
 scorecard:{168:{n:25,model_mae:9,baseline_mae:8,live_fallback:true}},limits:['x'],generalization:{scope:'cross_video_generalization',n_videos:3,required_videos:10,status:'insufficient_data'},
 videos:{a:{regime:'breakout',origin_day:'2026-09-11',recommendation:{label:'Breakout',regime:'breakout',what:'w',why:['y'],for:[],against:[{signal:'CTR',value:.01,channel_median:.05,n:40}],
  next:[{action:'Momentum schützen',detail:'d'}],do_not_change:['Titel'],confidence:{level:'low',n_rows:400,n_origins:140,n_videos:2,backtest_accepted:true,reason:'r'},generalization:{status:'insufficient_data',n_videos:2,required_videos:10},
  targets:{'100000':{status:'insufficient_data',reason:'r'},'1000000':{status:'insufficient_data'}},experiments:[{decision_id:1,hypothesis:'h',effect_status:'observed_change_not_causal',effect:{before_mean_daily:1,after_mean_daily:2,relative_change:1},conflicting_evidence:true}]},
  forecasts:[{horizon_hours:168,origin_day:'2026-09-11',model:'ridge',predicted_views:100,baseline_views:90,lower_views:50,upper_views:200,actual_views:null}]}}}};
renderLearning(state.learning_v4);
assert.match(nodes.learningTable.innerHTML,/validiert/);
assert.match(nodes.learningTable.innerHTML,/Fallback/);
assert.match(nodes.learningSignals.textContent,/nicht kausal/);
assert.match(nodes.learningAudit.innerHTML,/insufficient_data/);
assert.match(nodes.learningScope.textContent,/Zeitlich validiert auf bestehenden Videos/);
assert.match(nodes.learningScope.textContent,/Generalisierung auf neue Videos: unzureichende Daten \(3 von mindestens 10 Videos\)/);
assert.match(nodes.learningStatus.textContent,/n_rows 3’?000.*n_origins 1’?000.*n_videos 3/);
assert.match(nodes.learningTable.innerHTML,/zeitlich validiert/);
assert.match(nodes.learningTable.innerHTML,/unzureichende Daten<small>3\/10 Videos/);
assert.match(nodes.learningTable.innerHTML,/balanciert 8.5/);
const card=strategyCard(state.learning_v4.videos.a);
assert.match(card,/Generalisierung auf neue Videos: unzureichende Daten \(2 von 10 Videos\)/);assert.match(card,/n_origins 140/);
assert.match(card,/Momentum schützen/);assert.match(card,/NICHT ändern/);assert.match(card,/widersprüchliche/);assert.match(card,/nicht kausal/);
assert.match(forecastCell(v4For('a')),/empirisch 80/);
assert.match(modelCell(state.learning_v4.backtests[168]),/ridge ✓.*zeitlich, 3 Videos · neue Videos: unzureichend/);
assert.equal(modelCell(undefined),'insufficient_data');
assert.match(strategyCard(null),/Noch keine Empfehlung/);
assert.equal(REGIME_LABELS.paid_excluded,'Werbung');
"""
    subprocess.run(["node", "-"], input=prefix+"\n"+harness, text=True, encoding="utf-8", capture_output=True, check=True)
    assert '$("learningRun").onclick=()=>guarded(learningRun)' in source
    assert "CRON_SECRET" not in source and "/api/cron/sync" not in source
