import test from 'node:test';
import assert from 'node:assert/strict';
import { overseerTabPlan, OVERSEER_RESTORE_GRACE_MS } from '../overseer-tabs.js';

const chat = 'abcdefab-1234';
const url = `https://chatgpt.com/g/g-p-x/c/${chat}`;
const now = 1_800_000_000_000;

test('restored owner tab in its group is kept; no new tab, owner tab adopted without ownTabs', () => {
  const plan = overseerTabPlan({ tabs: [{ id: 7, url, groupId: 3, index: 4 }], chat, ownTabs: {}, now });
  assert.deepEqual(plan, { keep: 7, close: [], wait: false, adopt: true });
});

test('unloaded restored tab is found by pendingUrl', () => {
  const plan = overseerTabPlan({ tabs: [{ id: 9, url: '', pendingUrl: url }], chat, now });
  assert.equal(plan.keep, 9);
});

test('during session restore nothing is opened; after grace a missing chat may be opened', () => {
  const started = now - 30_000;
  assert.deepEqual(overseerTabPlan({ tabs: [], chat, browserStartedAt: started, now }),
    { keep: null, close: [], wait: true, adopt: false });
  const later = overseerTabPlan({ tabs: [], chat, browserStartedAt: now - OVERSEER_RESTORE_GRACE_MS - 1, now });
  assert.equal(later.wait, false);
  assert.equal(later.keep, null);
});

test('owner tab wins over our duplicate; only our inactive copy is closed', () => {
  const tabs = [{ id: 2, url, groupId: -1 }, { id: 1, url, groupId: 5 }];
  const plan = overseerTabPlan({ tabs, chat, ownTabs: { 2: 'owner-sv' }, now });
  assert.equal(plan.keep, 1);
  assert.deepEqual(plan.close, [2]);
});

test('two owner tabs with the chat: keep one, never close an owner tab', () => {
  const tabs = [{ id: 1, url, groupId: 5, index: 2 }, { id: 3, url, groupId: 5, index: 1 }];
  const plan = overseerTabPlan({ tabs, chat, now });
  assert.equal(plan.keep, 3);
  assert.deepEqual(plan.close, []);
});

test('our only tab is kept as ours (not adopted)', () => {
  const plan = overseerTabPlan({ tabs: [{ id: 4, url }], chat, ownTabs: { 4: 'owner-sv' }, now });
  assert.deepEqual(plan, { keep: 4, close: [], wait: false, adopt: false });
});
