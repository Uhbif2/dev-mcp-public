import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { LIVENESS_DEFAULTS, livenessPlan, blockedSummary } from '../liveness.js';
import { DISPATCH_SCOPE_TTL_MS, dispatchScopePlan } from '../dispatch-scope.js';

const hex = (text) => createHash('sha256').update(text).digest('hex');
const { stallMs, stepMs, restartEveryMs } = LIVENESS_DEFAULTS;

test('liveness: no demand or real progress never escalates', () => {
  const now = 1_000_000_000;
  assert.equal(livenessPlan({ since: now - 3 * stallMs, step: 2 }, { demand: 0 }, now).action, '');
  const moved = livenessPlan({ since: now - 3 * stallMs, step: 2 }, { demand: 5, progressed: true }, now);
  assert.equal(moved.action, '');
  assert.equal(moved.state.since, 0);
  assert.equal(moved.state.lastProgressAt, now);
});

test('liveness: stalled demand climbs diagnose → clear-local → free-slots → restart-worker', () => {
  const t0 = 2_000_000_000;
  let state = livenessPlan({}, { demand: 3 }, t0).state;
  assert.equal(livenessPlan(state, { demand: 3 }, t0 + stallMs - 1).action, '');
  const seen = [];
  let now = t0 + stallMs;
  for (let i = 0; i < 4; i++) {
    const plan = livenessPlan(state, { demand: 3 }, now);
    seen.push(plan.action); state = plan.state; now += stepMs;
  }
  assert.deepEqual(seen, ['diagnose', 'clear-local', 'free-slots', 'restart-worker']);
  assert.equal(state.step, 0, 'ladder restarts after worker restart');
  // A second restart within the hour degrades to freeing slots, never a reload loop.
  const again = livenessPlan({ ...state, since: now - stallMs - 3 * stepMs, step: 3 }, { demand: 3 }, now);
  assert.equal(again.action, 'free-slots');
  assert.ok(now - state.lastRestartAt < restartEveryMs);
});

test('liveness: a legitimate global pause holds the clock instead of escalating', () => {
  const now = 3_000_000_000;
  const held = livenessPlan({ since: now - 5 * stallMs, step: 3 }, { demand: 4, hold: 'пауза перегрузки аккаунта' }, now);
  assert.equal(held.action, '');
  assert.equal(held.state.since, now);
  assert.equal(held.state.step, 0);
});

test('liveness: blocked summary groups reasons without timestamps', () => {
  const text = blockedSummary({ a: 'отправка вне разрешённого набора', b: 'отправка вне разрешённого набора',
    c: 'повтор открытия после 2026-11-19T16:00:00.000Z' });
  assert.match(text, /^2× отправка вне разрешённого набора; 1× повтор открытия после …$/);
});

test('dispatch scope: approved new chats that now exist are consumed and the window retires (19.11 incident)', () => {
  const scope = { 'q31-w10399': { messages: [hex('first')], home: '', url: '' },
    'q5-w10414': { messages: [hex('first 2')], home: '', url: '' } };
  const jobs = [
    { id: 'q31-w10399', url: 'https://chatgpt.com/c/aaaaaaaa-1', send: 'follow-up' },
    { id: 'q5-w10414', url: 'https://chatgpt.com/c/aaaaaaaa-2', send: '' },
    { id: 'q38-w10399', url: '', send: 'brand new task' },
  ];
  const plan = dispatchScopePlan(scope, {}, jobs, { 'q31-w10399': hex('follow-up') }, 10_000);
  assert.equal(plan.retire, true);
  assert.match(plan.reason, /все одобренные сообщения доставлены/);
});

test('dispatch scope: pending or changed approvals keep blocking until the TTL', () => {
  const url = 'https://chatgpt.com/c/bbbbbbbb-1';
  const scope = { q1: { messages: [hex('reviewed')], home: '', url } };
  const pending = dispatchScopePlan(scope, {}, [{ id: 'q1', url, send: 'reviewed' }], { q1: hex('reviewed') }, 50_000);
  assert.equal(pending.active, true);
  assert.deepEqual(pending.remaining, ['q1']);
  assert.equal(pending.meta.until, 50_000 + DISPATCH_SCOPE_TTL_MS);
  const changed = dispatchScopePlan(scope, {}, [{ id: 'q1', url, send: 'changed' }], { q1: hex('changed') }, 50_000);
  assert.equal(changed.active, true, 'a different payload is not proof of delivery');
  const expired = dispatchScopePlan(scope, pending.meta, [{ id: 'q1', url, send: 'reviewed' }],
    { q1: hex('reviewed') }, 50_000 + DISPATCH_SCOPE_TTL_MS);
  assert.equal(expired.retire, true);
  assert.match(expired.reason, /срок окна/);
  const operatorUntil = dispatchScopePlan(scope, { since: 0, until: 9e12 }, [{ id: 'q1', url, send: 'x' }], {}, 50_000 + 2 * DISPATCH_SCOPE_TTL_MS);
  assert.equal(operatorUntil.active, true, 'an explicit operator deadline wins over the default TTL');
});

test('dispatch scope: confirmed delivery and missing orders', () => {
  const scope = { q1: { messages: [hex('m')], home: '', url: 'https://chatgpt.com/c/cccccccc-1' } };
  const delivered = dispatchScopePlan(scope, { since: 1, delivered: { [`q1:${hex('m')}`]: 2 } },
    [{ id: 'q1', url: 'https://chatgpt.com/c/cccccccc-1', send: 'm' }], { q1: hex('m') }, 10);
  assert.equal(delivered.retire, true);
  assert.equal(dispatchScopePlan(scope, { since: 1 }, [], {}, 10, { ordersOk: false }).active, true,
    'an unreadable order list is never evidence that the approved job is gone');
  assert.equal(dispatchScopePlan(scope, { since: 1 }, [], {}, 10).retire, true, 'closed job: nothing left to send');
  assert.equal(dispatchScopePlan('broken', {}, [], {}, 10).retire, true);
  assert.equal(dispatchScopePlan(null, {}, [], {}, 10).retire, false);
});
