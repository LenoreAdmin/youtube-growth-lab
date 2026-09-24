"""Der echte Startflow aus dem Browser: Klick -> POST -> Neuladen -> sichtbarer Status.

Der Produktionsfehler: die Queue wird aus dem gespeicherten Plan-Snapshot gerendert. Der Start
schrieb korrekt in growth_actions, aber der Snapshot sagte weiter "Vorschlag – noch nicht gestartet",
bis der naechste stuendliche Lauf den Plan neu schrieb. Ein Fehlschlag war ausserdem nur im globalen
Fehlerelement am Seitenende sichtbar.
"""
import json
import subprocess
from datetime import timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import growth_engine as ge, main
from app.models import GrowthAction, GrowthPlan, Video
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG

HEADERS = {"Authorization": "Bearer test-token-only"}


def proposal(session, video_id="a", action="link_from_own_video", created_day=None, **changes):
    row = GrowthAction(**{**{"video_id": video_id, "created_day": created_day or TODAY, "created_at": NOW,
                             "version": ge.VERSION, "state": "needs_distribution", "action": action,
                             "target_metric": "discovery_views_7d", "window_days": 14,
                             "evaluate_after": (created_day or TODAY)+timedelta(days=17), "status": ge.PROPOSED,
                             "payload": {"steps": ["Endscreen setzen"], "baseline": {"views_7d": 18}}}, **changes})
    session.add(row)
    session.commit()
    return row


def stored_plan(session, rows):
    plan = {"day": str(TODAY), "version": ge.VERSION, "status": "ok", "active_status": "active",
            "queue": [{"rank": i+1, "action_id": r.id, "status": ge.PROPOSED, "video_id": r.video_id,
                       "title": session.get(Video, r.video_id).title, "action": r.action, "state": r.state,
                       "objective": "Discovery", "steps": ["Endscreen setzen"], "window_days": r.window_days,
                       "target_metric": r.target_metric, "evaluate_after": str(r.evaluate_after),
                       "executed_automatically": False,
                       "confirm": {"required": True, "label": "Als durchgeführt markieren – Experiment starten",
                                   "endpoint": f"/api/growth/actions/{r.id}/start"}}
                      for i, r in enumerate(rows)],
            "running_experiments": [], "results": [], "not_testable": [], "ranking": [], "protected": [],
            "queue_limit": 3, "queue_note": "x", "now_do": None, "momentum_ranking": []}
    plan["now_do"] = plan["queue"][0] if plan["queue"] else None
    session.add(GrowthPlan(day=TODAY, version=ge.VERSION, created_at=NOW, plan=plan))
    session.commit()
    return plan


@pytest.fixture
def client(monkeypatch, session):
    main.app.dependency_overrides[main.db] = lambda: session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_a_confirmed_start_is_visible_immediately_without_waiting_for_the_next_cron(monkeypatch, session, client):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    row = proposal(session)
    stored_plan(session, [row])
    before = client.get("/api/growth", headers=HEADERS).json()["plan"]
    assert [q["status"] for q in before["queue"]] == [ge.PROPOSED] and before["now_do"]["action_id"] == row.id

    started = client.post(f"/api/growth/actions/{row.id}/start", headers=HEADERS)
    assert started.status_code == 200 and started.json()["status"] == ge.RUNNING

    # Der Plan-Snapshot ist unveraendert – das Dashboard muss trotzdem sofort „laeuft“ zeigen.
    snapshot = session.scalar(select(GrowthPlan)).plan
    assert snapshot["queue"][0]["status"] == ge.PROPOSED, "Snapshot bleibt Momentaufnahme"
    after = client.get("/api/growth", headers=HEADERS).json()["plan"]
    assert after["queue"] == [] and after["now_do"] is None
    assert [r["action_id"] for r in after["running_experiments"]] == [row.id]
    assert after["running_experiments"][0]["started_day"] == str(ge.pacific_day(ge.utcnow()))
    assert "bestätigung" in after["running_experiments"][0]["note"].lower()
    assert "laufen" in after["queue_note"]
    # Auch im Dashboard-Payload, den das Frontend tatsaechlich laedt.
    dashboard = client.get("/api/dashboard", headers=HEADERS).json()["growth_v5"]["plan"]
    assert dashboard["queue"] == [] and dashboard["running_experiments"][0]["action_id"] == row.id
    # Und die Datenbank haelt Startzeit und eingefrorene Baseline.
    session.expire_all()
    stored = session.get(GrowthAction, row.id)
    assert stored.status == ge.RUNNING and stored.started_at is not None
    assert stored.baseline["frozen_day"] == str(ge.pacific_day(ge.utcnow()))


def test_a_stale_action_id_in_the_snapshot_is_remapped_to_the_open_proposal(monkeypatch, session, client):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    old = proposal(session, created_day=TODAY-timedelta(days=1))
    stored_plan(session, [old])
    # Der naechste Lauf hat den Vorschlag ersetzt: alte ID zeigt ins Leere.
    old.status, old.outcome = ge.SUPERSEDED, "inconclusive"
    fresh = proposal(session, action="probe_missing_evidence")
    session.commit()
    plan = client.get("/api/growth", headers=HEADERS).json()["plan"]
    assert plan["queue"][0]["action_id"] == fresh.id, "Button zeigt auf den offenen Vorschlag"
    assert plan["queue"][0]["confirm"]["endpoint"].endswith(f"/{fresh.id}/start")
    assert client.post(f"/api/growth/actions/{fresh.id}/start", headers=HEADERS).status_code == 200
    # Eine bereits ersetzte Maßnahme laesst sich nicht starten und sagt das deutlich.
    clash = client.post(f"/api/growth/actions/{old.id}/start", headers=HEADERS)
    assert clash.status_code == 409 and "superseded" in clash.json()["detail"]


def test_start_failures_are_reported_and_never_silent(monkeypatch, session, client):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    row = proposal(session)
    stored_plan(session, [row])
    assert client.post("/api/growth/actions/999999/start", headers=HEADERS).status_code == 404
    assert client.post(f"/api/growth/actions/{row.id}/start").status_code in (401, 403)
    assert client.post(f"/api/growth/actions/{row.id}/start", headers=HEADERS).status_code == 200
    again = client.post(f"/api/growth/actions/{row.id}/start", headers=HEADERS)
    assert again.status_code == 200 and again.json()["status"] == ge.RUNNING, "idempotent, kein Fehler"
    second = proposal(session, action="probe_missing_evidence", created_day=TODAY+timedelta(days=1))
    clash = client.post(f"/api/growth/actions/{second.id}/start", headers=HEADERS)
    assert clash.status_code == 409 and "Nicht trennbar" in clash.json()["detail"]


def test_the_browser_start_flow_shows_success_and_failure_in_the_card():
    """Fuehrt den echten Frontend-Code aus: Klick-Handler, Fehlertext, Rendering."""
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    # Alle Funktionen behalten, nur die DOM-Verdrahtung beim Laden weglassen.
    runnable = chr(10).join(l for l in source.splitlines()
                            if not l.startswith("$(") and "addEventListener" not in l)
    harness = r"""
(async () => {
const assert=require('node:assert/strict');
const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={textContent:'',innerHTML:'',hidden:false,className:''})};
let posted=[], response=null, failure=null, loaded=0;
global.fetch=async(url,opts)=>{posted.push({url,opts});
 if(failure)return {ok:false,json:async()=>({detail:failure})};
 return {ok:true,json:async()=>response}};
token="t";
state={growth_v5:{plan:{day:'2026-09-24',queue:[],running_experiments:[],results:[],not_testable:[]}}};
load=async()=>{loaded++;renderQueue(state.growth_v5.plan)};

// Erfolgsfall: Rueckmeldung sichtbar in der Karte, Dashboard neu geladen.
response={id:42,status:'running',started_day:'2026-09-24',evaluate_after:'2026-10-11'};
const button={disabled:false,textContent:'Als durchgeführt markieren',dataset:{actionId:'42'}};
await startExperiment(button);
assert.equal(posted[0].url,'/api/growth/actions/42/start');
assert.equal(posted[0].opts.method,'POST');
assert.match(posted[0].opts.headers.Authorization,/^Bearer /);
assert.equal(loaded,1,'Dashboard wird nach dem Start neu geladen');
assert.equal(nodes.queueStart.hidden,false);
assert.match(nodes.queueStart.textContent,/Bestätigt: Maßnahme #42 läuft seit 2026-09-24/);
assert.match(nodes.queueStart.textContent,/Baseline eingefroren/);
assert.equal(button.disabled,false);
assert.equal(button.textContent,'Als durchgeführt markieren');

// Fehlerfall: die Ursache steht an der Karte, nicht nur am Seitenende.
failure='Für dieses Video läuft bereits ein Experiment (#7, Auswertung 2026-10-11).';
let raised=null;
try{await startExperiment(button)}catch(e){raised=e}
assert.ok(raised,'Fehler wird weitergegeben');
assert.match(nodes.queueStart.textContent,/Start fehlgeschlagen \(Maßnahme #42\)/);
assert.match(nodes.queueStart.textContent,/läuft bereits ein Experiment/);
assert.match(nodes.queueStart.className,/down/);
assert.equal(button.disabled,false,'Button bleibt benutzbar');
assert.equal(loaded,1,'nach einem Fehlschlag wird nichts als erledigt dargestellt');
})().catch(e => {console.error(e && e.message || e); process.exit(1)});
"""
    result = subprocess.run(["node", "-"], input=runnable+chr(10)+harness,
                            text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_card_renders_the_button_and_the_running_block(monkeypatch, session, client):
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    assert 'id="queueStart"' in Path("app/static/index.html").read_text(encoding="utf-8")
    assert "start-experiment" in source and "/start" in source
    # Der Klick-Handler haengt an der Liste, damit auch neu gerenderte Buttons funktionieren.
    assert 'addEventListener("click"' in source and "closest(\".start-experiment\")" in source
