// Автообновление при «A temporary limit…» (20.11.2026, №116 п. 6).
import test from 'node:test';
import assert from 'node:assert/strict';
import { limitReloadPlan, limitExcluded, toggleLimitExclusion, TEMP_LIMIT_TEXT } from '../limit-reload.js';

const T = Date.parse('2026-11-20T19:00:00Z');
const tab = (id, chat = `abcdef0${id}-1111`) => ({ id, url: `https://chatgpt.com/c/${chat}` });
const LIMIT = 'A temporary limit is preventing this chat from loading. Try again later.';
const pages = (entries) => new Map(entries);

test('exact owner text matches', () => {
  assert.ok(TEMP_LIMIT_TEXT.test(LIMIT));
  assert.ok(!TEMP_LIMIT_TEXT.test('Unable to load conversation'));
});

test('limit text → reload after 1 min, then 2 → 5 → 10 → 10 min', () => {
  const t = tab(1);
  const seen = limitReloadPlan({ tabs: [t], pages: pages([[1, { temporaryLimit: LIMIT, composerEmpty: true }]]), now: T });
  assert.deepEqual(seen.reload, [], 'first sighting waits one minute');
  assert.equal(seen.mem['1'].nextAt, T + 60_000);
  let mem = seen.mem, now = T + 60_000;
  const gaps = [];
  for (let i = 0; i < 4; i += 1) {
    const plan = limitReloadPlan({ tabs: [t], pages: pages([[1, { temporaryLimit: LIMIT }]]), mem, now });
    assert.deepEqual(plan.reload, [1]);
    gaps.push((plan.mem['1'].nextAt - now) / 60_000);
    mem = plan.mem; now = plan.mem['1'].nextAt;
  }
  assert.deepEqual(gaps, [2, 5, 10, 10]);
  const early = limitReloadPlan({ tabs: [t], pages: pages([[1, { temporaryLimit: LIMIT }]]), mem, now: now - 1 });
  assert.deepEqual(early.reload, []);
});

test('loaded chat → nothing and backoff reset; unreadable tab keeps its state', () => {
  const mem = { 1: { attempts: 3, nextAt: T, url: tab(1).url }, 2: { attempts: 1, nextAt: T, url: tab(2).url } };
  const plan = limitReloadPlan({ tabs: [tab(1), tab(2)], pages: pages([[1, { temporaryLimit: '' }], [2, null]]), mem, now: T });
  assert.deepEqual(plan.reload, []);
  assert.equal(plan.mem['1'], undefined);
  assert.deepEqual(plan.mem['2'], mem[2]);
});

test('switched off → nothing; typed text and excluded tab are never reloaded', () => {
  const due = { 1: { attempts: 0, nextAt: T - 1, url: tab(1).url } };
  const limited = pages([[1, { temporaryLimit: LIMIT, composerEmpty: true }]]);
  assert.deepEqual(limitReloadPlan({ tabs: [tab(1)], pages: limited, mem: due, now: T, enabled: false }),
    { reload: [], mem: {}, skipped: [] });
  const typed = limitReloadPlan({ tabs: [tab(1)], pages: pages([[1, { temporaryLimit: LIMIT, composerEmpty: false }]]), mem: due, now: T });
  assert.deepEqual(typed.reload, []);
  assert.equal(typed.skipped[0].reason, 'в поле ввода набран текст');
  const excluded = toggleLimitExclusion({}, tab(1), true);
  assert.ok(limitExcluded(excluded, tab(1)) && !limitExcluded(excluded, tab(2)));
  const skip = limitReloadPlan({ tabs: [tab(1)], pages: limited, mem: due, now: T, excluded });
  assert.deepEqual(skip.reload, []);
  assert.equal(skip.skipped[0].reason, 'исключена владельцем');
  assert.ok(!limitExcluded(toggleLimitExclusion(excluded, tab(1), false), tab(1)));
  // Без адреса чата исключение — по вкладке.
  const home = { id: 7, url: 'https://chatgpt.com/' };
  assert.ok(limitExcluded(toggleLimitExclusion({}, home, true), home));
});

test('account lane busy → wait without spending an attempt; one reload per tick', () => {
  const due = { 1: { attempts: 0, nextAt: T - 1, url: tab(1).url }, 2: { attempts: 0, nextAt: T - 1, url: tab(2).url } };
  const limited = pages([[1, { temporaryLimit: LIMIT }], [2, { temporaryLimit: LIMIT }]]);
  const busy = limitReloadPlan({ tabs: [tab(1), tab(2)], pages: limited, mem: due, now: T, accountWaitMs: 5000 });
  assert.deepEqual(busy.reload, []);
  assert.equal(busy.mem['1'].attempts, 0);
  const free = limitReloadPlan({ tabs: [tab(1), tab(2)], pages: limited, mem: due, now: T });
  assert.deepEqual(free.reload, [1]);
  assert.equal(free.mem['2'].attempts, 0);
});
