"""V5 API and dashboard: APP_TOKEN only, plan/ranking exposed, JS renders the plan without DOM errors."""
import subprocess
from pathlib import Path
import pytest
from test_api import client  # noqa: F401


def test_growth_endpoint_requires_token_and_exposes_plan(client, session, monkeypatch):
    from test_learning_v4 import seed_history, wire, NOW
    from app import learning
    wire(monkeypatch, session)
    seed_history(session, "a", days=500)
    seed_history(session, "b", days=500, base=200, seed=9)
    headers = {"Authorization": "Bearer test-token-only"}
    assert client.get("/api/growth").status_code == 401
    empty = client.get("/api/growth", headers=headers).json()
    assert empty["plan"] is None and empty["read_only"] is True
    learning.refresh(session, NOW)
    session.expire_all()
    growth = client.get("/api/growth", headers=headers).json()
    plan = growth["plan"]
    assert plan["priority_video_id"] in ("a", "b") and len(plan["ranking"]) == 2
    assert {r["priority"] for r in plan["ranking"]} == {1, 2}
    assert plan["confidence"] in ("low", "insufficient_data") and "keine Wahrscheinlichkeit" in plan["note"]
    dashboard = client.get("/api/dashboard", headers=headers).json()
    assert dashboard["growth_v5"]["plan"]["day"] == plan["day"]
    assert dashboard["growth_v5"]["scores"]["a"]["opportunity"]["components"]
    detail = client.get("/api/videos/a", headers=headers).json()
    assert detail["growth"]["state"] in growth["states"] and detail["growth"]["actions"][0]["status"] == "proposed"
    assert client.post("/api/growth", headers=headers).status_code == 405  # no write path exists


def test_v5_dashboard_javascript_renders_plan_and_card():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    prefix = source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:'',disabled:false})};
const ranking=[{priority:1,momentum_rank:1,active_rank:null,ineligible_reason:'Momentum wird geschützt – bewusst keine Änderung',video_id:'a',title:'Song A',opportunity_score:71.5,viewer_score:66,subscriber_score:40,state:'protect_momentum',regime:'breakout',breakout:true,action:'protect_no_change',reason:'Beschleunigung',notes:['n'],window_days:7,next_evaluation:'2026-09-21',held_since:null,revival:false,revival_signals:[]},
 {priority:2,momentum_rank:2,active_rank:1,active_priority_score:41.5,video_id:'b',title:'Song B',opportunity_score:35,viewer_score:30,subscriber_score:52,state:'revival_candidate',regime:'stable',breakout:false,action:'test_thumbnail',reason:'CTR',notes:[],window_days:14,next_evaluation:'2026-09-28',held_since:'2026-09-10',revival:true,revival_signals:['Packaging-/CTR-Schwäche']},
 {priority:3,video_id:'c',title:'Song C',opportunity_score:null,viewer_score:null,subscriber_score:null,state:'paid_excluded',regime:'paid_excluded',breakout:false,action:'observe',reason:'Werbung',notes:[],window_days:7,next_evaluation:'2026-09-21',revival:false,revival_signals:[],paid_status:'paid_excluded',paid_note:'26 Werbetage in der Historie, zuletzt 2026-09-05',paid:{days_until_clean:29,clean_days:3,required_clean_days:32}}];
const plan={day:'2026-09-11',status:'ok',active_status:'active',priority_video_id:'b',priority_title:'Song B',why:'CTR',why_priority:'Höchste aktive Growth-Priorität (41.5); Höherer Momentum-Score bei Song A bleibt geschützt.',action:'test_thumbnail',objective:'Viewer',do_not_change:['Titel'],success_metric:'ctr_or_views',success_criterion:'+15 %',window_days:14,next_evaluation:'2026-09-28',confidence:'low',subscriber_focus:'b',viewer_focus:'a',ranking,protected:[{video_id:'a',title:'Song A',action:'protect_no_change'}],momentum_top:{video_id:'a',title:'Song A',state:'protect_momentum',opportunity_score:71.5},track_record:{test_thumbnail:{positive:1,negative:0,neutral:2,inconclusive:0,n:3}},note:'keine Wahrscheinlichkeit'};
renderGrowth({plan});
assert.match(nodes.growthTitle.textContent,/Aktive Growth-Priorität #1: Song B/);
assert.match(nodes.growthDay.textContent,/Momentum schützen: Song A \(Nichts ändern – Momentum schützen\)/);
assert.match(nodes.growthTop.innerHTML,/Aktive Growth-Priorität<\/span><strong>42<\/strong>/);
assert.match(nodes.growthTop.innerHTML,/Growth Opportunity<\/span><strong>35<\/strong><small>Momentum-Rang 2/);
assert.match(nodes.growthTop.innerHTML,/Subscriber Opportunity/);
assert.match(nodes.growthDetail.innerHTML,/Empfohlene Aktion:<\/strong> Thumbnail testen/);
assert.match(nodes.growthDetail.innerHTML,/Momentum schützen \(keine Änderung\):<\/strong> Song A/);
assert.match(nodes.growthDetail.innerHTML,/Nicht verändern:<\/strong> Titel/);
assert.match(nodes.growthDetail.innerHTML,/nächste Auswertung 2026-09-28/);
assert.match(nodes.growthRanking.innerHTML,/<td>1<\/td><td>—<small>Momentum wird geschützt/);
assert.match(nodes.growthRanking.innerHTML,/<td>2<\/td><td><span class='up'>#1<\/span><small>Score 42/);
renderGrowth({plan:{...plan,status:'no_active_action',active_status:'none',priority_video_id:null,priority_title:null,action:null,why_priority:'Keine aktive Maßnahme empfohlen – kein änderbares Video mit ausreichender Evidenz.'}});
assert.match(nodes.growthTitle.textContent,/Keine aktive Maßnahme empfohlen/);
assert.match(nodes.growthTop.innerHTML,/Aktive Maßnahme<\/span><strong>keine<\/strong>/);
assert.match(nodes.growthTop.innerHTML,/Momentum-Spitze<\/span><strong>Song A/);
assert.match(nodes.growthDetail.innerHTML,/Empfohlene Aktion:<\/strong> Keine aktive Maßnahme empfohlen/);
renderGrowth({plan});
assert.match(nodes.growthRanking.innerHTML,/Revival-Kandidat<small>Packaging/);
assert.match(nodes.growthRanking.innerHTML,/Aktuell Paid beeinflusst/);
assert.match(nodes.growthRanking.innerHTML,/26 Werbetage in der Historie, zuletzt 2026-09-05 · noch 29 saubere Tage \(3\/32\)/);
assert.match(nodes.growthTop.innerHTML,/Werbung<\/span>/);
assert.match(nodes.growthRanking.innerHTML,/1\+ \/ 0− \/ 2= \/ 0\?/);
assert.match(nodes.growthRanking.innerHTML,/seit 2026-09-10/);
assert.equal((nodes.growthRanking.innerHTML.match(/<tr>/g)||[]).length,3);
renderGrowth({plan:null});
assert.match(nodes.growthDetail.innerHTML,/nächste stündliche Sync/);
const card=growthCard({day:'2026-09-11',state:'needs_packaging_test',action:'test_thumbnail',revival:{candidate:false,reason:'nein'},momentum:{score:55},
 opportunity:{score:40,reason:'r',components:[{name:'Tempo',signal:.2,weight:3,value:1.1,reference:1,available:true},{name:'CTR',signal:null,weight:1,value:null,reference:null,available:false}]},
 viewer:{score:30,reason:'r',components:[]},subscriber:{score:20,reason:'r',components:[]},
 actions:[{created_day:'2026-08-01',action:'test_thumbnail',status:'evaluated',outcome:'positive',target_metric:'ctr_or_views',window_days:14,evaluation:{detail:{metric:'views',relative_change:.2}},payload:{success_criterion:'s',stop_criterion:'x'}}]});
assert.match(card,/Packaging testen → Thumbnail testen/);assert.match(card,/fehlt/);assert.match(card,/positiv/);assert.match(card,/nicht kausal/);
assert.match(growthCard(null),/Noch keine Bewertung/);
"""
    subprocess.run(["node", "-"], input=prefix+"\n"+harness, text=True, encoding="utf-8", capture_output=True, check=True)
    assert "CRON_SECRET" not in source and "/api/cron/sync" not in source
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'id="growthCard"' in html and html.index('id="growthCard"') < html.index('id="backfillCard"')
