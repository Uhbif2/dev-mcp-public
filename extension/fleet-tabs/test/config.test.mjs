import test from 'node:test';
import assert from 'node:assert/strict';
import { INSTALL_DEFAULTS, localSettings } from '../config.js';

test('fresh install contains no personal project and stays disabled', () => {
  assert.equal(INSTALL_DEFAULTS.enabled, false);
  assert.deepEqual(INSTALL_DEFAULTS.projects, []);
  assert.equal(INSTALL_DEFAULTS.projectUrl, 'https://chatgpt.com/');
  assert.equal(INSTALL_DEFAULTS.connector, 'dev-mcp');
});

test('local configuration is optional and cannot inject unrelated settings', async () => {
  globalThis.chrome = { runtime: { getURL: (p) => p } };
  globalThis.fetch = async () => ({ok: true, json: async () => ({
    enabled: true, connector: 'My connector', projects: ['Demo'],
    mcpUrl: 'https://untrusted.invalid/', lockTabs: false,
  })});
  assert.deepEqual(await localSettings(), {
    enabled: true, connector: 'My connector', projects: ['Demo'],
  });
});
