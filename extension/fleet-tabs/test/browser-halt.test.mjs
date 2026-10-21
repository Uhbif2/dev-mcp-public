import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { parseBrowserControl } from '../fleet.js';

test('fleet_list browser line: autostart and emergency halt', () => {
  assert.deepEqual(parseBrowserControl('x\nbrowser: autostart=on; halt=none\n'), { known: true, autostart: true, halt: '' });
  assert.deepEqual(parseBrowserControl('browser: autostart=off; halt=ChatGPT: suspicious activity'),
    { known: true, autostart: false, halt: 'ChatGPT: suspicious activity' });
  // An old server without the line must not stop the fleet.
  assert.deepEqual(parseBrowserControl('очередь: выдавать нечего'), { known: false, autostart: true, halt: '' });
});

test('suspicious-network notice is recognised, agent prose about traffic is not', () => {
  const src = fs.readFileSync(new URL('../content.js', import.meta.url), 'utf8');
  const re = eval(/const NETWORK_SUSPICIOUS = (\/.*\/i);/.exec(src)[1]);
  for (const text of ['We have detected suspicious activity coming from your network.',
    'Unusual activity has been detected from your device. Try again later.',
    'Из вашей сети исходят подозрительные действия', 'Подозрительная активность из вашей сети'])
    assert.ok(re.test(text), text);
  for (const text of ['You are sending requests too quickly', 'unusual traffic spike in logs', 'Something went wrong'])
    assert.ok(!re.test(text), text);
});

test('ChatGPT "unusual activity" halt resumes by itself with growing pause; owner halt does not', () => {
  const src = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8');
  const body = /\/\/ Пауза после «Unusual activity»[\s\S]*?(?=\nasync function reportBrowserControl)/.exec(src)[0];
  const api = new Function(body + '\nreturn { haltPauseMs, haltAutoResumable, haltResumeAt };')();
  const min = 60 * 1000;
  assert.deepEqual([1, 2, 3, 4, 9].map((n) => api.haltPauseMs(n) / min), [20, 40, 80, 120, 120]);
  assert.ok(api.haltAutoResumable({ reason: 'ChatGPT: Unusual activity has been detected' }));
  assert.ok(!api.haltAutoResumable({ reason: 'владелец: стоп' }));
  assert.equal(api.haltResumeAt({ at: 1000 }), 1000 + 20 * min);
  assert.equal(api.haltResumeAt({ at: 1000, resumeAt: 5 }), 5);
});
