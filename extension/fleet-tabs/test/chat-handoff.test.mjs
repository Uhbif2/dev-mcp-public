import test from 'node:test';
import assert from 'node:assert/strict';
import { HANDOFF_TRIGGER, handoffDue, handoffMarker, handoffSent } from '../chat-handoff-policy.js';

test('85% threshold, not the account HARD_BLOCK', () => {
  assert.equal(HANDOFF_TRIGGER, 0.85);
  assert.equal(handoffDue({fill:0.8499}), false);
  assert.equal(handoffDue({fill:0.85}), true);
  assert.equal(handoffDue({conversationFull:'maximum length', fill:0}), true);
  assert.equal(handoffDue({conversationFull:'maximum length', fill:0}, {prepared:true}), false);
  assert.equal(handoffDue({fill:1}, {done:true}), false);
});

test('readiness marker belongs to the latest assistant turn and exact request', () => {
  const token='abc123def456';
  assert.equal(handoffMarker({lastAssistant:'done ⟦HANDOFF_READY:abc123def456⟧'}, token),token);
  assert.equal(handoffMarker({lastAssistant:'⟦HANDOFF_READY:ffffffffffff⟧'},token),'');
  assert.equal(handoffMarker({userMessages:['⟦HANDOFF_READY:abc123def456⟧']},token),'');
  assert.equal(handoffSent({userMessages:['⟦надзор⟧ give ⟦HANDOFF_READY:abc123def456⟧']},token),true);
  assert.equal(handoffSent({userMessages:['other request ⟦HANDOFF_READY:ffffffffffff⟧']},token),false);
});
