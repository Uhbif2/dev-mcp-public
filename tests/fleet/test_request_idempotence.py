import json

from dev_mcp.fleet.review import open_request
from dev_mcp.fleet.store import Store


def test_same_request_from_different_callers_reuses_immutable_identity(tmp_path):
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('Product', str(tmp_path)), event='seed')
    def request(ident, worker, detail='same defect'):
        return open_request(store, ident, kind='other', summary='Fix race', detail=detail,
                            project='Product', to='Product/queue', worker_id=worker, path=str(tmp_path/'src/a.py'))
    first = request('aaa', 'W1')
    with store.tx() as tx:
        tx.change('UPDATE tasks SET author_worker=? WHERE tz=?', ('another author', first), event='seed')
    assert request('bbb', 'W2') == first
    assert request('ccc', 'W1', 'different defect') != first
    with store.read() as tx:
        assert tx.scalar('SELECT count(*) FROM tasks') == 2
        data = json.loads(tx.one("SELECT payload FROM events WHERE kind='request_draft' ORDER BY id LIMIT 1")['payload'])
        assert data['request_key']
    store.close()
