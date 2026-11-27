// Экспорт полной сессии ChatGPT (20.11.2026, №116 п. 4).
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSessionExport, conversationIdOf, conversationStats, readConversationInPage, EXPORT_FORMAT } from '../session-export.js';

const raw = JSON.stringify({ title: 'Длинная беседа / v2', conversation_id: 'abcdef01-2222', current_node: 'n3',
  mapping: {
    root: { id: 'root', children: ['n1'] },
    n1: { id: 'n1', parent: 'root', message: { author: { role: 'user' }, content: { parts: ['привет'] } }, children: ['n2', 'n2b'] },
    n2: { id: 'n2', parent: 'n1', message: { author: { role: 'assistant' } }, children: ['n3'] },
    n2b: { id: 'n2b', parent: 'n1', message: { author: { role: 'assistant' } }, children: [] },
    n3: { id: 'n3', parent: 'n2', message: { author: { role: 'tool' } }, children: [] },
  } }, null, 1);

test('conversation id from chat and project-chat urls', () => {
  assert.equal(conversationIdOf('https://chatgpt.com/c/ABCDEF01-2222'), 'abcdef01-2222');
  assert.equal(conversationIdOf('https://chatgpt.com/g/g-p-x/c/abcdef01-3333?model=a'), 'abcdef01-3333');
  assert.equal(conversationIdOf('https://chatgpt.com/'), '');
});

test('export keeps the original text byte-for-byte and counts every node and branch', () => {
  const built = buildSessionExport(raw, { url: 'https://chatgpt.com/c/abcdef01-2222', id: 'abcdef01-2222',
    version: '0.19.63', now: Date.parse('2026-11-20T19:30:00Z') });
  assert.ok(built.text.endsWith(`,"conversation":${raw}}`));
  const parsed = JSON.parse(built.text);
  assert.equal(parsed.meta.format, EXPORT_FORMAT);
  assert.deepEqual(parsed.meta.stats, { nodes: 5, messages: 4, roles: { user: 1, assistant: 2, tool: 1 },
    branches: 1, currentBranchMessages: 3 });
  assert.deepEqual(parsed.conversation, JSON.parse(raw));
  assert.equal(built.filename, 'chatgpt-Длинная-беседа-v2-abcdef01-20261120T193000Z.json');
});

test('invalid or non-conversation answers never produce a file', () => {
  assert.throws(() => buildSessionExport('<html>login</html>'));
  assert.throws(() => buildSessionExport('{"detail":"Unauthorized"}'), /нет mapping/);
  assert.deepEqual(conversationStats(null), { nodes: 0, messages: 0, roles: {}, branches: 0, currentBranchMessages: 0 });
});

test('page reader uses the page session token and reports HTTP errors', async () => {
  const before = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push([url, init.headers?.authorization || '']);
    if (url === '/api/auth/session') return { json: async () => ({ accessToken: 'tok' }) };
    return { ok: url.endsWith('/ok'), status: url.endsWith('/ok') ? 200 : 404, text: async () => (url.endsWith('/ok') ? raw : 'not found') };
  };
  try {
    const ok = await readConversationInPage('ok');
    assert.deepEqual([ok.ok, ok.status, ok.text], [true, 200, raw]);
    assert.deepEqual(calls[1], ['/backend-api/conversation/ok', 'Bearer tok']);
    const missing = await readConversationInPage('gone');
    assert.deepEqual([missing.ok, missing.status, missing.detail], [false, 404, 'not found']);
  } finally { globalThis.fetch = before; }
});
