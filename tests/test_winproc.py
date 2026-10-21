import subprocess
import sys

import pytest

from dev_mcp import winproc


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_children_get_no_console_unless_they_ask(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(winproc, "_installed", False)
    original = subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__",
                        lambda self, *a, **k: seen.append(k.get("creationflags", 0)))
    winproc.install()
    subprocess.Popen(["git", "--version"])
    subprocess.Popen(["x"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    assert seen[0] & subprocess.CREATE_NO_WINDOW
    assert seen[0] & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert seen[1] == subprocess.CREATE_NEW_CONSOLE | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    subprocess.Popen(['x'],creationflags=subprocess.NORMAL_PRIORITY_CLASS)
    assert seen[2] & subprocess.NORMAL_PRIORITY_CLASS
    assert not seen[2] & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    subprocess.Popen.__init__ = original


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows only')
def test_serving_and_child_priorities_in_a_real_isolated_process():
    import json
    script = '''import json,subprocess,sys
from dev_mcp import winproc
winproc.install()
serving=winproc.normalize_serving_priority()
child=subprocess.check_output([sys.executable,'-c',
    'import psutil,json;p=psutil.Process();print(json.dumps({"cpu":int(p.nice())}))'],text=True)
print(json.dumps({'serving':serving,'child':json.loads(child)}))
'''
    result=json.loads(subprocess.check_output([sys.executable,'-c',script],text=True))
    assert result['serving']['cpu']==subprocess.NORMAL_PRIORITY_CLASS
    assert result['serving']['io']==2
    assert result['child']['cpu']==subprocess.BELOW_NORMAL_PRIORITY_CLASS
