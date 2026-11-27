"""Real MCP author/reviewer -> sealed commit -> immutable gate -> owner artifact."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime,timezone
import zipfile

import pytest
from fastmcp import Client

from dev_mcp.audit import Audit
from dev_mcp.policy import Policy
from dev_mcp.server import build_server
from dev_mcp.fleet import goals,owner_notices
from dev_mcp.fleet.integrate import Integrator,Batch
from dev_mcp.fleet.results import ResultPublisher,available
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.owner.overview import snapshot


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,
                          encoding='utf-8',check=True).stdout.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize('project',['Compiler Demo','Research Demo'])
async def test_complete_cycle_exposes_verified_artifact_without_forging_owner_acceptance(tmp_path,monkeypatch,project):
    root=tmp_path/'project'
    root.mkdir()
    config=tmp_path/'policy.toml'
    config.write_text(f"roots = ['{root}']\nwrite_roots = ['{root}']\nexec_roots = ['{root}']\n"
                      "exec_allowed_bin = ['python']\nexec_timeout_cap_s = 30\nallow_write = false\n"
                      "deny_patterns = ['.git','.env']\n",encoding='utf-8')
    verifier=root/'verify.py'
    verifier.write_text("from pathlib import Path\nassert Path('result.md').read_text(encoding='utf-8') == '# Verified result\\n'\n",encoding='utf-8')
    (root/'.gitattributes').write_text('result.md export-ignore\n',encoding='utf-8')
    git(root,'init','-q');git(root,'config','user.email','test@example.invalid')
    git(root,'config','user.name','Test');git(root,'add','verify.py','.gitattributes');git(root,'commit','-qm','baseline')
    flags={'commit':True,'gate':'python verify.py'}
    monkeypatch.setattr(goals,'project_info',lambda p:flags)
    monkeypatch.setenv('DEV_MCP_FLEET_REQUIRE_GREEN_SUBMIT','1')
    database=tmp_path/'data/fleet/fleet.sqlite'
    server=build_server(Policy.load(config),Audit(tmp_path/'logs/audit.jsonl'),fleet_db=database)
    store=Store.open(str(database))
    stamp=datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)',(project,str(root)),event='test.project')
        tx.change("INSERT INTO tasks(project,tz,title,state,owns,acceptance,created_at,updated_at) "
                  "VALUES(?,'TZ-1','Verified result','ready',?,?,?,?)",
                  (project,json.dumps([norm_path(str(root/'result.md'))]),json.dumps(['python verify.py']),stamp,stamp),event='test.task')
    integrator=Integrator(store,tmp_path/'integrate.json',project_info=lambda _:flags)
    assert integrator.tick()==[]
    async def call(client,name,args):
        result=await client.call_tool(name,args)
        text='\n'.join(c.text for c in result.content if hasattr(c,'text'))
        assert not result.is_error,text
        return text
    async with Client(server) as author_client,Client(server) as review_client:
        author=re.search(r'\bW\d+\b',await call(author_client,'fleet_join',{'label':'author','kind':'chatgpt'})).group()
        assert 'TZ-1' in await call(author_client,'fleet_take',{'agent':author})
        await call(author_client,'write_file',{'path':str(root/'result.md'),'content':'# Verified result\n','agent':author})
        await call(author_client,'run',{'root':str(root),'argv':['python','verify.py'],'agent':author})
        await call(author_client,'fleet_submit',{'agent':author,'note':'Versioned result created and verified.'})
        reviewer=re.search(r'\bW\d+\b',await call(review_client,'fleet_join',{'label':'reviewer','kind':'chatgpt'})).group()
        assert reviewer!=author
        assert 'review:TZ-1' in await call(review_client,'fleet_take',{'agent':reviewer})
        await call(review_client,'run',{'root':str(root),'argv':['python','verify.py'],'agent':reviewer})
        await call(review_client,'fleet_review',{'agent':reviewer,'verdict':'accept','note':'Independent verifier passed.'})
    publisher=ResultPublisher(store,lambda _:flags)
    assert publisher.tick()==0  # accepted alone is never a result artifact
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project=? AND tz='TZ-1'",(project,))=='accepted'
    changes=integrator.tick()
    assert any(e['kind']=='gate' and e['rc']==0 for e in changes),changes
    assert publisher.tick()==1
    with store.read() as tx:
        result=available(tx)[0]
        assert result['gate_event'] and not result['owner_accepted']
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.done'")==0
    archive=Path(result['artifact'])
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==result['sha256']
    with zipfile.ZipFile(archive) as package:
        assert '# Verified result' in package.read('changes.patch').decode()
        assert package.read('files/result.md')==b'# Verified result\n'
        assert json.loads(package.read('receipt.json'))['revision']==git(root,'rev-parse','HEAD')
    assert snapshot(store)['human']['done'][0]['artifact']==archive.as_uri()
    monkeypatch.setattr(owner_notices,'prepare_ops_requests',lambda _:0)
    owner_notices.reconcile(store)
    with store.read() as tx:
        assert tx.one("SELECT id FROM owner_notices WHERE kind='done' AND state='active'")
    assert ResultPublisher(store,lambda _:flags).tick()==0  # restart is idempotent
    archive.write_bytes(b'changed after delivery')
    with store.read() as tx:
        assert available(tx)==[]
    assert snapshot(store)['human']['done']==[]
    store.close()


@pytest.mark.parametrize('absolute_script',[False,True])
def test_gate_tests_committed_bytes_even_when_dirty_tree_would_pass(tmp_path,absolute_script):
    root=tmp_path/'source';root.mkdir()
    (root/'gate.py').write_text('raise SystemExit(1)\n',encoding='utf-8')
    git(root,'init','-q');git(root,'config','user.email','test@example.invalid')
    git(root,'config','user.name','Test');git(root,'add','.');git(root,'commit','-qm','red committed version')
    (root/'gate.py').write_text('raise SystemExit(0)\n',encoding='utf-8')
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('Generic',?)",(str(root),),event='test.project')
    command=subprocess.list2cmdline(['python',str(root/'gate.py') if absolute_script else 'gate.py'])
    engine=Integrator(store,tmp_path/'cursor',project_info=lambda _:{'gate':command})
    event=engine._gate(Batch('Generic',str(root)))
    assert event['rc']==1
    assert event['payload']['tested_revision']==git(root,'rev-parse','HEAD')
    assert (root/'gate.py').read_text()=='raise SystemExit(0)\n'
    store.close()


def test_busy_gate_preserves_green_and_creates_no_code_repair(tmp_path,monkeypatch):
    from dev_mcp.fleet import exec_preflight
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('Generic',?)",(str(tmp_path),),event='test.project')
        tx.record_event(kind='gate',project='Generic',rc=0,payload={'key':'old-green'})
    engine=Integrator(store,tmp_path/'cursor',project_info=lambda _:{'gate':'python -V'})
    monkeypatch.setattr(exec_preflight,'run_command',lambda *a,**k:
                        exec_preflight.ExecutionResult(('python','-V'),124,'','resource busy: owned','not_started_busy'))
    event=engine._gate(Batch('Generic',str(tmp_path)))
    assert event['kind']=='gate_wait'
    with store.read() as tx:
        assert tx.scalar("SELECT rc FROM events WHERE kind='gate' ORDER BY id DESC LIMIT 1")==0
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'styk:%'")==0
    store.close()


def test_backfilled_older_result_does_not_displace_newer_result_or_renotify(tmp_path,monkeypatch):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    monkeypatch.setattr(owner_notices,'prepare_ops_requests',lambda _:0)
    artifact=tmp_path/'result.zip'
    artifact.write_bytes(b'published content')
    def published(tz,aid):
        with store.tx() as tx:
            tx.record_event(kind='owner_result_available',project='Generic',tz=tz,payload={
                'accept_id':aid,'revision':'b'*40,'artifact':str(artifact),
                'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()})
    published('newer',200)
    owner_notices.reconcile(store)
    with store.read() as tx:
        first=tx.scalar("SELECT id FROM owner_notices WHERE kind='done' AND state='active'")
    published('older',100)
    owner_notices.reconcile(store)
    with store.read() as tx:
        assert available(tx)[0]['tz']=='newer'
        assert tx.scalar("SELECT id FROM owner_notices WHERE kind='done' AND state='active'")==first
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner_notice.queued'")==1
    store.close()


def test_integration_overtaken_by_newer_accept_is_dropped_not_rewaited(tmp_path):
    # 07.11: ведущий принимает каждые минуты — старые интеграции писали owner_result_wait 14 дней.
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.record_event(kind='accept',project='P',tz='TZ-LEAD',payload={})
        old=tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'")
        tx.record_event(kind='integrate',project='P',tz='TZ-LEAD',payload={'commit':'a'*40,'accept_id':old})
        tx.record_event(kind='accept',project='P',tz='TZ-LEAD',payload={})
    publisher=ResultPublisher(store,lambda _:{})
    publisher.tick();publisher.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner_result_wait'")==0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner_result_superseded'")==1
    store.close()
