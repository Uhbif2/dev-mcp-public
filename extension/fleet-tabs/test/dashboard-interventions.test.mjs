import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the production renderOverview/renderSummary code with a strict DOM
// double. This tests actual nodes/textContent, not just a JSON schema or grep.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.textContent = '';
    this.className = '';
    this.style = {};
    this.dataset = {};
    this.hidden = false;
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  addEventListener() {}
  querySelectorAll() { return []; }
  set innerHTML(_) { throw new Error('unsafe innerHTML'); }
  get text() { return [this.textContent, ...this.children.map(x => x.text)].join(' '); }
}

function fixture() {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement(tag) { return new Element(tag); },
  };
  const source = fs.readFileSync(new URL('../dashboard/app.js', import.meta.url), 'utf8')
    .replace(/^import\s+[^\n]+from\s+"[^"]+";\s*/gm, '')
    .replace('export const transport =', 'const transport =')
    .replace(/\nrefresh\(\);\s*setInterval\(refresh, POLL_MS\);\s*$/, '\n');
  const context = { document, window: { addEventListener() {} }, setInterval() {},
    callTool() { throw new Error('unexpected network request'); }, DEFAULT_MCP_URL: '' };
  vm.runInNewContext(source, context, { filename: 'dashboard/app.js', timeout: 3000 });
  return { context, elements };
}

const days = {
  '2026-10-09': { 'manual-commit': 1 },
  '2026-10-10': { 'manual-guard-approval': 1 },
  '2026-10-11': { 'manual-commit': 2, 'manual-recover': 1 },
};

test('real dashboard rendering shows goal, trend and per-class daily rows', () => {
  const { context, elements } = fixture();
  context.renderOverview({
    status: 'ready', intents: [],
    interventions: { available: true, goal: 0, today: 3, yesterday: 1,
      rising: true, by_day: days, by_class: { 'manual-commit': 3 } },
  });
  const rendered = elements.get('summary').text;
  for (const fragment of ['Ручные вмешательства', 'Цель: 0',
                          'Сегодня: 3', 'Вчера: 1', 'Рост вмешательств',
                          '2026-10-11', 'manual-commit: 2',
                          'manual-recover: 1', '2026-10-09']) {
    assert.ok(rendered.includes(fragment), `not rendered: ${fragment}`);
  }
  assert.equal(elements.get('summary').children.length, 5);
  assert.equal(elements.get('summary').children.at(-1).style.gridColumn, '1 / -1');
});

test('empty and invalid data do not imply healthy zero and cannot inject HTML', () => {
  const { context, elements } = fixture();
  context.renderOverview({ status: 'empty', intents: [], interventions: {
    available: false, goal: 0, today: 0, yesterday: 0, rising: false, by_day: {},
  } });
  assert.match(elements.get('summary').text, /Учёт вмешательств пока недоступен/);
  assert.doesNotMatch(elements.get('summary').text, /Сегодня: 0/);

  context.renderOverview({ status: 'ready', intents: [], interventions: {
    available: true, goal: 0, today: 0, yesterday: 0, rising: false,
    by_day: { '2026-10-11': { '<img src=x onerror=alert(1)>': 1 } },
  } });
  assert.match(elements.get('summary').text, /Без роста/);
  assert.match(elements.get('summary').text, /<img src=x onerror=alert\(1\)>: 1/);
  context.renderOverview({ status: 'ready', intents: [], interventions: {
    available: true, goal: 0, today: 'secret', yesterday: 1, rising: false, by_day: {},
  } });
  assert.match(elements.get('summary').text, /Сводка вмешательств повреждена/);
});
