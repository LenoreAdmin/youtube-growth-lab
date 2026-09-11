import subprocess
from pathlib import Path
import pytest


@pytest.mark.parametrize("status",["ok","deferred","failed"])
def test_manual_sync_button_waits_reloads_and_restores(status):
    source=Path("app/static/app.js").read_text(encoding="utf-8")
    prefix=source[:source.index('$("loginForm").addEventListener')]
    harness = r"""
const assert=require('node:assert/strict');
const button={disabled:false,textContent:'Aktualisieren'};
const errorArea={textContent:''};
global.document={getElementById:id=>id==='refresh'?button:errorArea};
let calls=[],finish;
api=async(url,data)=>{calls.push([url,data]);await new Promise(resolve=>finish=resolve);
 if(STATUS==='failed')throw Error('Sync fehlgeschlagen');
 return {status:STATUS,detail:'Zeitbudget erreicht'};};
load=async()=>{calls.push(['dashboard']);};
(async()=>{
 const run=guarded(manualSync);
 assert.equal(button.disabled,true);
 assert.equal(button.textContent,'Synchronisiere…');
 await manualSync(); // duplicate click cannot start another request
 assert.equal(calls.length,1);
 finish();await run;
 assert.deepEqual(calls,[['/api/sync',{}],['dashboard']]);
 assert.equal(button.disabled,false);
 assert.equal(button.textContent,'Aktualisieren');
 assert.equal(errorArea.textContent,STATUS==='ok'?'':STATUS==='deferred'?'Zeitbudget erreicht':'Sync fehlgeschlagen');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run(["node","-"],input=prefix+"\nconst STATUS="+repr(status)+";\n"+harness,
                   text=True,encoding="utf-8",capture_output=True,check=True)
    assert '$("refresh").onclick=()=>guarded(manualSync)' in source
    assert 'CRON_SECRET' not in source
    assert '/api/cron/sync' not in source
