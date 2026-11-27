import test from "node:test";
import assert from "node:assert/strict";
import { OVERSEER_SENDS_PER_HOUR, overseerSendBudget } from "../policy.js";
import { memoryGuardPlan, overseerRevisionPlan } from "../memory-guard.js";

// chrome.storage.local переживает создание нового service worker; его JS-массив — нет.
function worker(storage) {
  return (now) => {
    const decision = overseerSendBudget(storage.overseerSends, now);
    storage.overseerSends = decision.sends;
    return decision.allowed;
  };
}

test("надзиратель: первые 12 попыток в час разрешены, 13-я запрещена", () => {
  const storage = { overseerSends: [] };
  const send = worker(storage);
  const start = 1_000_000;
  for (let i = 0; i < OVERSEER_SENDS_PER_HOUR; i++) {
    assert.equal(send(start + i * 1000), true, `отправка ${i + 1}`);
  }
  assert.equal(storage.overseerSends.length, 12);
  assert.equal(send(start + 12_000), false);
  assert.equal(storage.overseerSends.length, 12, "отклонённая попытка не расходует бюджет");
});

test("надзиратель: новый worker с тем же storage сохраняет предел, метки истекают через час", () => {
  const storage = { overseerSends: [] };
  const original = worker(storage);
  const start = 2_000_000;
  for (let i = 0; i < 12; i++) assert.equal(original(start + i * 1000), true);

  const restarted = worker(storage);
  assert.equal(restarted(start + 12_000), false, "перезапуск не возвращает 13-ю отправку");
  assert.equal(restarted(start + 3600_000), true, "метка ровно часовой давности истекла");
  assert.equal(storage.overseerSends.length, 12, "сохранено только скользящее часовое окно");
  assert.equal(restarted(start + 3600_000 + 1), false);
  assert.equal(restarted(start + 2 * 3600_000), true, "через следующий час старые метки удалены");
  assert.deepEqual(storage.overseerSends, [start + 2 * 3600_000]);
});

test("надзиратель: расчёт чистый и отбрасывает повреждённые и будущие метки", () => {
  const timestamps = [NaN, null, "123", -1, 20_000, 3_700_001, 3_599_999];
  const before = [...timestamps];
  const result = overseerSendBudget(timestamps, 3_600_000);
  assert.equal(result.allowed, true);
  assert.deepEqual(result.sends, [20_000, 3_599_999, 3_600_000]);
  assert.deepEqual(timestamps, before);
  assert.deepEqual(overseerSendBudget(undefined, 3_600_000).sends, [3_600_000]);
});

test("W11-2: owner-sv с устаревшей worker-привязкой не кандидат memory-guard", () => {
  const tab = {id:1,active:false,url:"https://chatgpt.com/c/abcdefab-1",autoDiscardable:true};
  const page = {url:tab.url,generating:false,composerEmpty:true,lastRole:"assistant"};
  const state = memoryGuardPlan({tabs:[tab],owned:{1:"owner-sv"},bindings:{1:{label:"f1118-1500abc"}},
    pages:new Map([[1,page]]),workers:[{label:"f1118-1500abc",task:null}],
    freeMemMB:100});
  assert.equal(state.eligible,0);
  assert.deepEqual(state.discard,[]);
});

test("W11-2: update_time changes require stable polling and never commit before reload", () => {
  const first = overseerRevisionPlan({value:"111"}, "222", 1_000_000);
  assert.equal(first.reload,false);
  assert.equal(first.state.value,"111");
  const changed = overseerRevisionPlan(first.state,"223",1_040_000);
  assert.equal(changed.reload,false,"another change resets stability window");
  const second = overseerRevisionPlan(changed.state,"223",1_071_000);
  assert.equal(second.reload,true);
  assert.equal(second.state.value,"111","blocked reload must not lose pending revision");
  const unchanged = overseerRevisionPlan({value:"223"},"223",1_100_000);
  assert.equal(unchanged.reload,false);
  assert.equal(unchanged.state.candidate,undefined);
  const unavailable = overseerRevisionPlan(first.state,null,1_060_000);
  assert.equal(unavailable.reload,false,"API failures never trigger a blind reload");
});

test("W11-2: missing baseline gets one stable bootstrap snapshot, not per-tick reload", () => {
  const first = overseerRevisionPlan({},123,100_000);
  assert.equal(first.reload,false);
  assert.equal(overseerRevisionPlan(first.state,"123",131_000).reload,true);
  assert.equal(overseerRevisionPlan({value:"123"},"123",161_000).reload,false);
});
