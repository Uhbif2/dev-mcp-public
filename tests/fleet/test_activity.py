from datetime import datetime,timedelta,timezone
import pytest
from dev_mcp.fleet import activity,flow
from dev_mcp.fleet.store import Store


def test_browser_observation_is_fresh_claim_bound_and_cannot_heartbeat_model(tmp_path):
    s=Store.open(str(tmp_path/'fleet.sqlite'))
    now=datetime.now(timezone.utc);old=(now-timedelta(minutes=5)).isoformat()
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",(str(tmp_path),),event='seed')
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P','TZ','taken',?,?)",(old,old),event='seed')
        tx.change("INSERT INTO workers(id,label,first_seen,last_seen) VALUES('W','label',?,?)",(old,old),event='seed')
        tx.change("INSERT INTO claims(worker_id,project,tz,state,started_at) VALUES('W','P','TZ','active',?)",(old,),event='seed')
    assert flow.agent_states(s)['silent']==1
    row={'label':'label','task':'P/TZ','state':'generating','stall_s':5}
    assert activity.record(s,[row])==1
    assert flow.agent_states(s)['thinking']==1
    with s.read() as tx: assert tx.one("SELECT last_seen FROM workers WHERE id='W'")['last_seen']==old
    assert activity.record(s,[{**row,'task':'P/other'}])==0
    assert flow.agent_states(s,now=now+timedelta(minutes=3))['silent']==1
    activity.record(s,[{**row,'stall_s':400}])
    assert flow.agent_states(s)['thinking']==0
    with pytest.raises(ValueError): activity.record(s,[{**row,'state':'approved'}])
