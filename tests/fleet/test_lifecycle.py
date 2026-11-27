from dev_mcp.fleet.store import Store
from dev_mcp.fleet.lifecycle import metrics


def test_yield_and_root_blockers_do_not_confuse_acceptance_with_handover(tmp_path):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('Generic',?)",(str(tmp_path),),event='test.project')
        for tz,state,needs in [('consumer','ready','["middle"]'),('middle','frozen','["missing"]'),
                               ('cycle-a','ready','["cycle-b"]'),('cycle-b','frozen','["cycle-a"]')]:
            tx.change("INSERT INTO tasks(project,tz,state,needs,created_at,updated_at) VALUES('Generic',?,?,?,'t','t')",
                      (tz,state,needs),event='test.task')
        tx.record_event(kind='accept',project='Generic',tz='work')
        aid=tx.scalar('SELECT MAX(id) FROM events')
        tx.record_event(kind='integrate',project='Generic',tz='work',payload={'accept_id':aid,'commit':'a'*40})
    data=metrics(store._connection(),'2000')
    assert (data['accepted'],data['integrated'],data['artifact_available'])==(1,1,0)
    missing=next(r for r in data['root_blockers'] if r['tz']=='missing')
    assert missing['ready']==1 and missing['consumers']==2
    assert any(r['state']=='cycle' for r in data['root_blockers'])
    store.close()
