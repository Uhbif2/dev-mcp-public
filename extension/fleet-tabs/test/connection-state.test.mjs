// Связь с сервером флота: значок с памятью и отложенная тревога (20.11.2026, №116 п. 3).
import test from 'node:test';
import assert from 'node:assert/strict';
import { badgeWithMemory, classifyFleetError, connectionNote, nextConnection, offlineAlertDue,
  FLEET_STALE_MS, OFFLINE_ALERT_MS } from '../fleet-ui-state.js';
import { fleetState } from '../fleet.js';

const states = { working: 2, thinking: 0, silent: 1, waiting: 0 };
const quick = { open: 3, active: 1 };
const T = Date.parse('2026-11-20T18:00:00Z');

test('classifies timeout, refused connection and server error', () => {
  assert.equal(classifyFleetError('MCP tools/call fleet_list: таймаут 15 с').kind, 'перегрузка');
  assert.equal(classifyFleetError('TypeError: Failed to fetch').kind, 'перезапуск');
  assert.equal(classifyFleetError('initialize: HTTP 502').kind, 'перезапуск');
  assert.equal(classifyFleetError('fleet_list: сервер ответил ошибкой: cannot begin').kind, 'ошибка сервера');
});

test('one failed poll keeps the last good number; "?" only after FLEET_STALE_MS', () => {
  const first = badgeWithMemory(states, quick, 0, { at: T, now: T });
  assert.equal(first.badge.text, '3');
  assert.ok(first.good);
  const conn = nextConnection({ failures: 0, lastOkAt: T }, false, T + 60_000, 'MCP tools/call fleet_list: таймаут 15 с');
  const held = badgeWithMemory(null, null, 0, { at: T, now: T + 200_000, lastGood: first.good, conn });
  assert.equal(held.badge.text, '3');
  assert.match(held.badge.title, /^данные на 18:00 UTC — Работают сейчас: 3/);
  assert.match(held.badge.title, /связь: неудач подряд 1 \(перегрузка\), последний ответ сервера 18:00 UTC/);
  assert.equal(held.good, null);
  const lost = badgeWithMemory(null, null, 0, { at: T, now: T + FLEET_STALE_MS + 1, lastGood: first.good, conn });
  assert.equal(lost.badge.text, '?');
  assert.match(lost.badge.title, /неудач подряд 1/);
});

test('offline alert needs >=3 consecutive failures and >=5 min without an answer', () => {
  let conn = { failures: 0, lastOkAt: T };
  for (let i = 1; i <= 3; i += 1) conn = nextConnection(conn, false, T + i * 30_000, 'таймаут 30 с');
  assert.equal(conn.failures, 3);
  assert.equal(offlineAlertDue(conn, T + 90_000), false, 'three timeouts in 90 s is not "no connection"');
  assert.equal(offlineAlertDue(conn, T + OFFLINE_ALERT_MS), true);
  const ok = nextConnection(conn, true, T + OFFLINE_ALERT_MS + 1);
  assert.equal(ok.failures, 0);
  assert.equal(offlineAlertDue(ok, T + 10 * OFFLINE_ALERT_MS), false);
  assert.equal(connectionNote(ok), '');
  // Never answered since the extension started: counted from the first failure.
  let fresh = nextConnection(null, false, T, 'Failed to fetch');
  fresh = nextConnection(fresh, false, T + 30_000, 'Failed to fetch');
  fresh = nextConnection(fresh, false, T + 60_000, 'Failed to fetch');
  assert.equal(fresh.since, T);
  assert.equal(offlineAlertDue(fresh, T + 60_000), false);
  assert.equal(offlineAlertDue(fresh, T + OFFLINE_ALERT_MS), true);
});

test('fleetState treats an error answer as a failure and waits 30 s', async () => {
  const before = globalThis.fetch;
  let timeoutSeen = 0;
  const realTimeout = AbortSignal.timeout;
  AbortSignal.timeout = (ms) => { timeoutSeen = Math.max(timeoutSeen, ms); return realTimeout.call(AbortSignal, ms); };
  const response = (data) => ({ ok: true, status: 200,
    headers: { get: (key) => (key.toLowerCase() === 'mcp-session-id' ? 's' : 'application/json') },
    async text() { return JSON.stringify(data); } });
  globalThis.fetch = async (_url, options) => {
    if (options.method === 'DELETE') return response({});
    const packet = JSON.parse(options.body);
    if (packet.method === 'tools/call') return response({ result: { isError: true, content: [{ type: 'text',
      text: 'ошибка: cannot begin fleet read: cannot start a transaction within a transaction' }] } });
    return response({ result: {} });
  };
  try {
    await assert.rejects(fleetState('http://127.0.0.1:8766/servers/dev/mcp'), /сервер ответил ошибкой/);
    assert.equal(timeoutSeen, 30_000);
  } finally {
    globalThis.fetch = before;
    AbortSignal.timeout = realTimeout;
  }
});
