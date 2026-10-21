import test from 'node:test';
import assert from 'node:assert/strict';
import {fleetSnapshotStatus} from '../fleet.js';

test('failed refresh ends updating and shows the transport error', () => {
  const state = fleetSnapshotStatus({lastTickAt:1,
    fleetRefresh:{startedAt:180000,finishedAt:195000,error:'signal timed out'}},200000);
  assert.equal(state.stale,true);
  assert.equal(state.refreshing,false);
  assert.match(state.detail,/сервер не ответил: signal timed out/);
});
test('abandoned refresh cannot spin forever after worker restart', () => {
  const state = fleetSnapshotStatus({fleetRefresh:{startedAt:1000}},200000);
  assert.equal(state.refreshing,false);
  assert.match(state.detail,/ожидаю ответа/);
});
test('fresh fleet query is fresh even when later tick work failed', () => {
  const state = fleetSnapshotStatus({lastTickAt:1,fleetUpdatedAt:199000},200000);
  assert.equal(state.stale,false);
});
