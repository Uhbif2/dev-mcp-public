import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    out.push(path.relative(root, full));
    if (entry.isDirectory()) walk(full, out);
  }
  return out;
}

test('unpacked extension contains no Chrome-reserved underscore names', () => {
  const bad = walk(root)
    .filter((rel) => rel.split(/[\\/]/).some((part) => part.startsWith('_')));
  assert.deepEqual(
    bad,
    [],
    'Chrome refuses unpacked extensions containing files/directories whose name starts with "_"',
  );
});

test('manifest is valid MV3 and versioned', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.equal(manifest.background?.type, 'module');
  assert.ok(manifest.content_scripts?.some((x) => x.js?.includes('content.js')));
});
