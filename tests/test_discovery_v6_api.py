"""V6 API and dashboard wiring: APP_TOKEN only, preview-disabled manual run, JSON-safe overview, JS renders the section."""
import subprocess
from pathlib import Path
from unittest.mock import Mock
import pytest
from app.config import settings
from test_api import client  # noqa: F401


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer cron-test-secret"])
def test_discovery_endpoints_require_app_token(client, monkeypatch, authorization):
    import app.main as main
    run = Mock()
    monkeypatch.setattr(main.discovery_module, "run", run)
    monkeypatch.setattr(settings, "cron_secret", "cron-test-secret")
    headers = {"Authorization": authorization} if authorization else {}
    assert client.get("/api/discovery", headers=headers).status_code == 401
    assert client.post("/api/discovery/run", headers=headers).status_code == 401
    run.assert_not_called()


def test_discovery_manual_run_preview_statuses_and_redaction(client, monkeypatch):
    import app.main as main
    headers = {"Authorization": "Bearer test-token-only"}
    import app.youtube as youtube
    monkeypatch.setattr(youtube, "YouTube", lambda *a, **k: object())
    run = Mock(return_value={"status": "ok", "stats": {"searches": 1}, "issues": [], "day": "2026-09-11"})
    monkeypatch.setattr(main.discovery_module, "run", run)
    assert client.post("/api/discovery/run", headers=headers).json()["status"] == "ok"
    assert run.call_args.kwargs["force"] is True
    monkeypatch.setattr(main.discovery_module, "run", Mock(return_value={"status": "already_running"}))
    assert client.post("/api/discovery/run", headers=headers).status_code == 409
    monkeypatch.setattr(main.discovery_module, "run", Mock(return_value={"status": "failed", "issues": ["x"]}))
    assert client.post("/api/discovery/run", headers=headers).status_code == 503
    monkeypatch.setattr(settings, "vercel_env", "preview")
    assert client.post("/api/discovery/run", headers=headers).status_code == 403
    monkeypatch.setattr(settings, "vercel_env", "production")
    monkeypatch.setattr(main.discovery_module, "run", Mock(side_effect=RuntimeError("private-token-must-not-appear")))
    response = client.post("/api/discovery/run", headers=headers)
    assert response.status_code == 503 and "private-token" not in response.text


def test_dashboard_exposes_discovery_and_video_detail_audience(client, session, monkeypatch):
    from test_discovery_v6 import DiscoveryClient, wire
    from test_learning_v4 import seed_history, NOW
    from app import discovery, learning
    wire(monkeypatch, session)
    seed_history(session, "a", days=500)
    seed_history(session, "b", days=500, base=60, seed=2)
    headers = {"Authorization": "Bearer test-token-only"}
    empty = client.get("/api/discovery", headers=headers).json()
    assert empty["best"] is None and empty["capabilities"] and empty["read_only"] is True
    discovery.run(DiscoveryClient(), NOW)
    learning.refresh(session, NOW)
    session.expire_all()
    view = client.get("/api/discovery", headers=headers).json()
    assert view["best"]["kind"] in ("search", "suggested", "cluster") and view["top"] and view["quota"]["units_used"] > 0
    assert view["per_video"]["a"]["score"] is not None
    dashboard = client.get("/api/dashboard", headers=headers).json()
    assert dashboard["discovery_v6"]["day"] == view["day"]
    plan = dashboard["growth_v5"]["plan"]
    assert "external_signals" in plan and "internal_signals" in plan and plan["combined_decision"]
    assert dashboard["learning_v4"]["generalization"]["n_videos"] == 2  # public data never inflates n_videos


def test_v6_dashboard_javascript_renders_discovery_section():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    prefix = source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:'',disabled:false})};
state={videos:[{id:'a',title:'Trainstories'},{id:'b',title:'Shine On'}]};
const best={kind:'search',key:'train journey',gap:'existing_video_opportunity',video_id:'a',video_title:'Trainstories',scores:{external_audience_score:71,search_opportunity_score:71,subscriber_fit_score:55},
 evidence:{demand_source:'own_analytics',own_search_views_90d:40,uncertainty:'moderat',missing:['Thumbnail-CTR']},trend:[{day:'2026-09-09',score:60},{day:'2026-09-11',score:71}]};
renderDiscovery({best,top:[best,{kind:'suggested',key:'ext1',gap:'suggested_opportunity',video_id:'a',video_title:'Trainstories',scores:{external_audience_score:64,suggested_opportunity_score:64},evidence:{title:'Night train ambient',demand_source:'public_proxy'},trend:[],outcome:'positive'}],
 per_video:{a:{kind:'search',key:'train journey',gap:'existing_video_opportunity',score:71,demand_source:'own_analytics',shared_tokens:['train']},b:null},
 last_run:{day:'2026-09-11',status:'ok',units_used:811,issues:[]},quota:{units_used:811,daily_limit:1500},insufficient:3,
 capabilities:[{source:'Google Trends',status:'nicht genutzt',note:'keine API'}],memory:{search:{positive:2,negative:0,neutral:1,inconclusive:0,weight:1}},limits:['l']});
assert.match(nodes.discoveryTitle.textContent,/Beste externe Chance: „train journey“ → Trainstories/);
assert.match(nodes.discoveryStatus.textContent,/811 \/ 1’?500 Einheiten/);
assert.match(nodes.discoveryTop.innerHTML,/eigene Analytics \(real\)/);
assert.match(nodes.discoveryTop.innerHTML,/Bestehendes Video sichtbar machen/);
assert.match(nodes.discoveryDetail.innerHTML,/Shine On:<\/strong> noch keine externe Chance/);
assert.match(nodes.discoveryDetail.innerHTML,/gemeinsame Themen: train/);
assert.match(nodes.discoveryTable.innerHTML,/40 eigene Such-Views/);
assert.match(nodes.discoveryTable.innerHTML,/öffentlicher Proxy/);
assert.match(nodes.discoveryTable.innerHTML,/\+11 über 2 Tage/);
assert.match(nodes.discoverySources.innerHTML,/Google Trends/);
assert.match(nodes.discoverySources.innerHTML,/2\+ \/ 0− \/ 1= \/ 0\?/);
renderDiscovery({best:null,top:[],per_video:{},last_run:null,quota:{units_used:0,daily_limit:1500},capabilities:[],memory:{},limits:[]});
assert.match(nodes.discoveryStatus.textContent,/Noch kein Discovery-Lauf/);
const plan={day:'2026-09-11',status:'ok',priority_video_id:'a',priority_title:'Trainstories',why:'w',why_priority:'p',action:'target_search_opportunity',objective:'Viewer',do_not_change:['Thumbnail'],success_metric:'discovery_views_7d',success_criterion:'s',window_days:28,next_evaluation:'2026-10-12',confidence:'low',
 internal_signals:{state:'observe',regime:'stable',opportunity_score:48,paid_status:'organic'},external_signals:{available:true,kind:'search',key:'train journey',gap:'existing_video_opportunity',score:71,demand_source:'own_analytics'},combined_decision:'Trainstories: intern observe + externe Chance → target_search_opportunity.',ranking:[]};
renderGrowth({plan});
assert.match(nodes.growthDetail.innerHTML,/Interne Signale:<\/strong> Beobachten/);
assert.match(nodes.growthDetail.innerHTML,/Externe Nachfrage-Signale:<\/strong> Suchintention „train journey“/);
assert.match(nodes.growthDetail.innerHTML,/Kombinierte Entscheidung:/);
assert.match(nodes.growthDetail.innerHTML,/Suchintention gezielt bedienen/);
"""
    subprocess.run(["node", "-"], input=prefix+"\n"+harness, text=True, encoding="utf-8", capture_output=True, check=True)
    assert '$("discoveryRun").onclick=()=>guarded(discoveryRun)' in source
    assert "CRON_SECRET" not in source and "/api/cron/sync" not in source
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert html.index('id="growthCard"') < html.index('id="discoveryCard"') < html.index('id="backfillCard"')
