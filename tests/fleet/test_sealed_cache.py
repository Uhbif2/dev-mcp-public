import os
from pathlib import Path
import pytest
import tempfile
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext
from dev_mcp.fleet.sealed_cache import sweep,remove_projection


def test_sweep_reclaims_dead_owner_and_preserves_live_and_unknown(tmp_path):
    dead=tmp_path/'view-2147483647-abcd1234'
    live=tmp_path/f'view-{os.getpid()}-abcd1234'
    foreign=tmp_path/'other-user-data'
    for p in (dead,live,foreign):
        p.mkdir();(p/'witness').write_text('preserve unless dead')
    assert sweep(tmp_path)==1
    assert not dead.exists() and live.exists() and foreign.exists()


def test_projection_removal_refuses_external_link_and_preserves_target(tmp_path):
    view=tmp_path/'view-2147483647-abcd1234';view.mkdir()
    target=tmp_path/'personal';target.mkdir();(target/'witness').write_text('keep')
    try: (view/'escape').symlink_to(target,target_is_directory=True)
    except OSError: pytest.skip('symlink permission unavailable')
    with pytest.raises(ValueError): remove_projection(view,tmp_path)
    assert (target/'witness').read_text()=='keep' and view.exists()


def test_review_projection_shared_until_last_live_reader_retires(tmp_path):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",(str(tmp_path),),event='seed')
        tx.change("INSERT INTO tasks(project,tz,state,kind,created_at,updated_at) "
                  "VALUES('P','TZ','taken','review','now','now')",event='seed')
        for worker in ('W1','W2'):
            tx.change("INSERT INTO workers(id,label,first_seen,last_seen) VALUES(?,?,'now','now')",
                      (worker,worker),event='seed')
            tx.change("INSERT INTO claims(worker_id,project,tz,state,started_at) VALUES(?,'P','TZ','active','now')",
                      (worker,),event='seed')
    ctx=FleetContext(store)
    scratch=tempfile.TemporaryDirectory(dir=tmp_path)
    root=Path(scratch.name);(root/'sealed').write_text('immutable')
    view=('same sealed revision',scratch,root)
    ctx.sealed_read_views.update(W1=view,W2=view)
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='done' WHERE worker_id='W1'",event='retire')
    ctx._prune_sealed_views(force=True)
    assert set(ctx.sealed_read_views)=={'W2'} and (root/'sealed').is_file()
    with store.tx() as tx:
        tx.change("UPDATE workers SET revoked_at='now' WHERE id='W2'",event='retire')
    ctx._prune_sealed_views(force=True)
    assert not ctx.sealed_read_views and not root.exists()
    store.close()
