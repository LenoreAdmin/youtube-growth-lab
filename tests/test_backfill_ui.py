import subprocess
from pathlib import Path
import pytest


@pytest.mark.parametrize("sequence", [["ok"], ["deferred", "deferred", "ok"], ["deferred", "throttled"], ["failed"]])
def test_backfill_button_continues_deferred_rounds_then_reloads(sequence):
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    prefix = source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const button={disabled:false,textContent:'Historie nachladen'};
const errorArea={textContent:''};
global.document={getElementById:id=>id==='backfillRun'?button:errorArea};
let calls=[],finish;
const queue=[...SEQUENCE];
api=async(url,data)=>{calls.push([url,data]);await new Promise(resolve=>finish=resolve);
 const status=queue.shift();
 if(status==='failed')throw Error('Backfill fehlgeschlagen');
 return {status,issues:status==='throttled'?['API-Limit']:[]};};
load=async()=>{calls.push(['dashboard']);};
(async()=>{
 const run=guarded(backfill);
 assert.equal(button.disabled,true);
 assert.match(button.textContent,/Runde 1/);
 await backfill(); // duplicate click cannot start another loop
 for(let i=0;i<SEQUENCE.length;i++){assert.equal(calls.length,i+1);finish();await new Promise(r=>setTimeout(r,0));}
 await run;
 assert.deepEqual(calls,[...SEQUENCE.map(()=>['/api/backfill',{}]),['dashboard']]);
 assert.equal(button.disabled,false);
 assert.equal(button.textContent,'Historie nachladen');
 const last=SEQUENCE.at(-1);
 assert.equal(errorArea.textContent,last==='ok'?'':last==='throttled'?'API-Limit':'Backfill fehlgeschlagen');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run(["node", "-"], input=prefix+"\nconst SEQUENCE="+repr(sequence)+";\n"+harness,
                   text=True, encoding="utf-8", capture_output=True, check=True)
    assert '$("backfillRun").onclick=()=>guarded(backfill)' in source
    assert "CRON_SECRET" not in source
    assert "/api/cron/sync" not in source
