"""Execute the actual dashboard label function in Node; no duplicate implementation."""
import json
import subprocess
from pathlib import Path
import pytest


@pytest.mark.parametrize("prediction,expected", [
    ({"n":30,"audit":{"interval_kind":"empirical_80"}}, "Empirisches 80%-Intervall"),
    ({"n":100,"audit":{"interval_kind":"uncalibrated_scenario"}}, "Unkalibrierter Szenariobereich"),
    ({"n":100}, "Unkalibrierter Szenariobereich"),
])
def test_dashboard_interval_label_uses_explicit_metadata(prediction,expected):
    source=Path("app/static/app.js").read_text(encoding="utf-8")
    # Load the real script up to DOM event registration so its actual helper runs.
    prefix=source[:source.index('$("loginForm").addEventListener')]
    script=prefix+"\nconsole.log(JSON.stringify(intervalLabel("+json.dumps(prediction)+")));"
    result=subprocess.run(["node","-"],input=script,text=True,encoding="utf-8",capture_output=True,check=True)
    assert json.loads(result.stdout)==expected
    assert "${intervalLabel(p)}" in source
