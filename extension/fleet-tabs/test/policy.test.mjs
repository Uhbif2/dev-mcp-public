import assert from "node:assert/strict";
import test from "node:test";

import { DEFAULTS, decide, emptyMemory } from "../policy.js";
import { parseWorkers, staleTaskAttention } from "../fleet.js";

const T0 = 1_000_000_000;
const idleTab = (over = {}) => ({
  label: "bs-1",
  generating: false,
  idleSec: 120,
  composerEmpty: true,
  lastRole: "assistant",
  hasContinueButton: false,
  errorText: "",
  ...over,
});
const decide0 = (tab, worker, mem, now) => decide(tab, worker, mem, now, DEFAULTS, () => 0);
const busy = (over = {}) => ({ id: "W5", label: "bs-1", task: "P/TZ-1", silenceMin: 1, ...over });

test("ход кончился, задача взята → продолжить", () => {
  const r = decide0(idleTab(), busy(), emptyMemory(), T0);
  assert.equal(r.action, "continue");
  assert.equal(r.mem.continues, 1);
});

test("есть штатная кнопка продолжения → нажать её, а не писать текст", () => {
  assert.equal(decide0(idleTab({ hasContinueButton: true }), busy(), emptyMemory(), T0).action, "click-continue");
});

test("работа сдана (нет задачи) → не трогать", () => {
  assert.equal(decide0(idleTab(), busy({ task: null }), emptyMemory(), T0).action, "skip");
});

test("человек что-то набирает → не трогать", () => {
  const r = decide0(idleTab({ composerEmpty: false }), busy(), emptyMemory(), T0);
  assert.equal(r.action, "skip");
  assert.match(r.reason, /человек/);
});

test("идёт генерация или тишина короче порога → не трогать", () => {
  assert.equal(decide0(idleTab({ generating: true }), busy(), emptyMemory(), T0).action, "skip");
  assert.equal(decide0(idleTab({ idleSec: DEFAULTS.idleGraceSec - 1 }), busy(), emptyMemory(), T0).action, "skip");
});

test("вкладка без метки агента → не трогать", () => {
  assert.equal(decide0(idleTab({ label: "" }), busy(), emptyMemory(), T0).action, "skip");
});

test("не чаще одного продолжения в minGapSec", () => {
  const first = decide0(idleTab(), busy(), emptyMemory(), T0);
  const again = decide0(idleTab(), busy(), first.mem, T0 + (DEFAULTS.minGapSec - 1) * 1000);
  assert.equal(again.action, "skip");
});

test("продолжения без вызовов инструментов → claim уходит в auto-handoff после maxFruitless", () => {
  let mem = emptyMemory();
  let now = T0;
  let result;
  for (let i = 0; i < 10; i += 1) {
    const sinceMin = mem.lastContinueAt ? (now - mem.lastContinueAt) / 60_000 : 1;
    result = decide0(idleTab(), busy({ silenceMin: Math.ceil(sinceMin) + 5 }), mem, now);
    mem = result.mem;
    if (result.action === "release-claim") break;
    now += Math.max(DEFAULTS.minGapSec + 10, 70) * 1000;
  }
  assert.equal(result.action, "release-claim");
  assert.match(result.reason, /без единого вызова/);
  assert.match(result.releaseReason, /^handoff:/);
  assert.equal(decide0(idleTab(), busy({ silenceMin: 60 }), mem, now + 10 * 60_000).action, "skip");
  assert.equal(decide0(idleTab(), busy({ silenceMin: 1 }), mem, now + 10 * 60_000).action, "continue");
});

test("потолок продолжений освобождает claim для другого worker, а не оставляет stale ownership", () => {
  const mem = {
    ...emptyMemory(),
    task: "P/TZ-1",
    continues: DEFAULTS.maxContinuesPerTask,
    fruitless: 0,
    lastContinueAt: 0,
  };
  const r = decide0(idleTab(), busy(), mem, T0);
  assert.equal(r.action, "release-claim");
  assert.match(r.reason, /потолок/);
  assert.match(r.releaseReason, /^handoff:/);
});

test("если после продолжения были вызовы инструментов — счётчик холостых сбрасывается", () => {
  const first = decide0(idleTab(), busy(), emptyMemory(), T0);
  const later = T0 + (DEFAULTS.minGapSec + 10) * 1000;
  const second = decide0(idleTab(), busy({ silenceMin: 0 }), first.mem, later);
  assert.equal(second.action, "continue");
  assert.equal(second.mem.fruitless, 0);
});

test("новая задача снимает стоп и обнуляет счётчики", () => {
  const stopped = { ...emptyMemory(), task: "P/TZ-1", stopped: "что-то", continues: 7 };
  const r = decide0(idleTab(), busy({ task: "P/TZ-2" }), stopped, T0);
  assert.equal(r.action, "continue");
  assert.equal(r.mem.continues, 1);
});

test("наше сообщение без ответа: ждём порог, потом повтор; исчерпанный retry → auto-handoff claim", () => {
  const sent = decide0(idleTab(), busy(), emptyMemory(), T0).mem;
  const waiting = decide0(idleTab({ lastRole: "user", idleSec: 60 }), busy(), sent, T0 + 60_000);
  assert.equal(waiting.action, "skip");
  const stuckTab = idleTab({ lastRole: "user", idleSec: DEFAULTS.stuckUserTurnSec + 1 });
  let mem = sent;
  let now = T0 + 200_000;
  for (let i = 1; i <= DEFAULTS.maxRetriesPerTask; i += 1) {
    const r = decide0(stuckTab, busy({ silenceMin: 60 }), mem, now);
    assert.equal(r.action, "continue", `повтор ${i}`);
    assert.equal(r.mem.retries, i);
    assert.equal(decide0(stuckTab, busy({ silenceMin: 60 }), r.mem, now + 10_000).action, "skip"); // не чаще retryGapSec
    mem = r.mem;
    now += (DEFAULTS.retryGapSec + 1) * 1000;
  }
  const gaveUp = decide0(stuckTab, busy({ silenceMin: 60 }), mem, now);
  assert.equal(gaveUp.action, "release-claim");
  assert.match(gaveUp.releaseReason, /^handoff:/);
});

test("разбор fleet_list, включая метки с тире и воркеров без задачи", () => {
  const workers = parseWorkers(
    [
      "W1 (импортирован, исполнитель неизвестен — TZ-25) — Nova VPN/TZ-25; молчит 2676 мин",
      "W5 (bs-1) — без задачи; молчит 56 мин",
      "W6 (bs-2) — Beta App/review:TZ-01; молчит 0 мин",
      "мусор",
    ].join("\n"),
  );
  assert.deepEqual(workers.map((w) => [w.id, w.label, w.task, w.silenceMin]), [
    ["W1", "импортирован, исполнитель неизвестен — TZ-25", "Nova VPN/TZ-25", 2676],
    ["W5", "bs-1", null, 56],
    ["W6", "bs-2", "Beta App/review:TZ-01", 0],
  ]);
});

test("долгий молчащий active claim поднимает owner-alert, но не перераздаётся сам", () => {
  const workers = [
    { id: "W1", task: "EchoRedactor/TZ-36", silenceMin: 121 },
    { id: "W2", task: "EchoRedactor/TZ-23", silenceMin: 119 },
    { id: "W3", task: null, silenceMin: 500 },
  ];
  assert.deepEqual(staleTaskAttention(workers), [
    {
      task: "EchoRedactor/TZ-36",
      note: "W1 молчит 121 мин с активным claim — self-heal/recovery должен освободить его автоматически; ручной owner recover не требуется",
    },
  ]);
});

import { KEYBOARD_DEFAULTS, LAUNCH_DEFAULTS as BASE_LAUNCH_DEFAULTS, closePlan, decideLaunch as launchWithConfig, keyboardPlan, recordStartedChat, shouldClose } from "../policy.js";
const LAUNCH_DEFAULTS = { ...BASE_LAUNCH_DEFAULTS, projects: ["Beta App", "Nova VPN"] };
function decideLaunch(projects, labels, workers, launch, now, cfg = LAUNCH_DEFAULTS, ...extra) {
  return launchWithConfig(projects, labels, workers, launch, now, cfg, ...extra);
}
import { parseProjects, parseQueue } from "../fleet.js";

const BS = { name: "Beta App", count: 1, next: { tz: "review:TZ-01", kind: "review" } };
const VPN = { name: "Nova VPN", count: 2, next: { tz: "TZ-51", kind: "work" } };
const Q = () => [BS];

test("разбор строки очереди", () => {
  assert.deepEqual(
    parseQueue("W5 (bs-1) — без задачи; молчит 3 мин\nочередь: следующая к выдаче — Beta App/review:TZ-01 [review]; можно выдать сейчас: 3"),
    { next: { project: "Beta App", tz: "review:TZ-01", kind: "review" }, count: 3, known: true },
  );
  assert.deepEqual(parseQueue("очередь: выдавать нечего"), { next: null, count: 0, known: true });
  assert.equal(parseQueue("W5 (bs-1) — без задачи; молчит 3 мин").known, false);
});

test("разбор строк по проектам", () => {
  const text = [
    "W5 (bs-1) — без задачи; молчит 213 мин",
    "проект: Beta App — можно выдать 1; следующая review:TZ-01 [review]",
    "проект: Nova VPN — можно выдать 2; следующая TZ-51 [work]",
    "проект: Пустой — выдавать нечего",
    "очередь: следующая к выдаче — Beta App/review:TZ-01 [review]; можно выдать сейчас: 3",
  ].join("\r\n");
  assert.deepEqual(parseProjects(text), [BS, VPN, { name: "Пустой", count: 0, next: null }]);
});

test("есть задача отмеченного проекта и свободное место → запустить под этот проект", () => {
  const r = decideLaunch(Q(), [], [], {}, T0);
  assert.equal(r.action, "launch");
  assert.deepEqual(r.target, { project: "Beta App", tz: "review:TZ-01", kind: "review" });
  assert.equal(r.launch.lastLaunchAt, T0);
});

test("неотмеченный проект выше в очереди не блокирует отмеченный", () => {
  const r = decideLaunch([VPN, BS], [], [], {}, T0, { ...LAUNCH_DEFAULTS, projects: ["Beta App"] });
  assert.equal(r.action, "launch");
  assert.equal(r.target.project, "Beta App");
});

test("задачи только в неотмеченных проектах → не запускать и назвать их", () => {
  const r = decideLaunch([VPN, { ...BS, count: 0, next: null }], [], [], {}, T0,
    { ...LAUNCH_DEFAULTS, projects: ["Beta App"], parkBufferMin: 0 });
  assert.equal(r.action, "skip");
  assert.match(r.reason, /есть в неотмеченных: Nova VPN \(2\)/);
  assert.match(decideLaunch([BS], [], [], {}, T0, { ...LAUNCH_DEFAULTS, projects: [] }).reason, /ни один/);
});

test("сервер не сообщает очередь → вслепую не запускать", () => {
  assert.equal(decideLaunch(null, [], [], {}, T0).action, "skip");
});

test("ожидающий неотправленный старт не блокирует другой проект; короткий таймаут освобождает резерв", () => {
  const launch = { launched: { f1: { at: T0, task: "Beta App/x", project: "Beta App", needsStart: true } }, lastLaunchAt: 0 };
  const cfg = { ...LAUNCH_DEFAULTS, projects: ["Beta App", "Nova VPN"] };
  const next = decideLaunch([BS, VPN], ["f1"], [], launch, T0 + 60_000, cfg);
  assert.equal(next.action, "launch");
  assert.equal(next.target.project, "Nova VPN");
  const late = decideLaunch(Q(), [], [], launch, T0 + (LAUNCH_DEFAULTS.joinTimeoutSec + 1) * 1000);
  assert.deepEqual(late.failed, ["f1"]);
  assert.equal(late.action, "launch");
});

test("подтверждённо отправленный starter ждёт fleet_join дольше и не рождает дубликат", () => {
  const launch = { launched: { f1: {
    at: T0, task: "Beta App/x", project: "Beta App",
    needsStart: false, startedAt: T0 + 10_000, countedChatAt: T0 + 10_000,
  } } };
  const queue = [{ ...BS, count: 1 }];
  const stillWaiting = decideLaunch(
    queue, ["f1"], [], launch, T0 + (LAUNCH_DEFAULTS.joinTimeoutSec + 30) * 1000,
    { ...LAUNCH_DEFAULTS, projects: ["Beta App"], minGapSec: 1, parkBufferMin: 0 },
  );
  assert.equal(stillWaiting.action, "skip");
  assert.deepEqual(stillWaiting.failed, []);
  assert.match(stillWaiting.reason, /зарезервирована pending-start|ожидают подключения/);

  const expired = decideLaunch(
    queue, ["f1"], [], launch, T0 + (LAUNCH_DEFAULTS.startedJoinTimeoutSec + 1) * 1000,
    { ...LAUNCH_DEFAULTS, projects: ["Beta App"], minGapSec: 1 },
  );
  assert.deepEqual(expired.failed, ["f1"]);
  assert.equal(expired.action, "launch");
});

test("pending распределяются между всеми активными проектами, а не забивают первый", () => {
  const ETR = { name: "EchoRedactor", count: 5, next: { tz: "fix:TZ-33:3", kind: "fix" } };
  const cfg = { ...LAUNCH_DEFAULTS, projects: ["Nova VPN", "Beta App", "EchoRedactor"], minGapSec: 1, maxPending:3 };
  let launch = {};
  const q = [{ ...VPN, count: 8 }, { ...BS, count: 22 }, ETR];

  const a = decideLaunch(q, [], [], launch, T0, cfg, () => 0);
  assert.equal(a.target.project, "Nova VPN");
  launch = { ...a.launch, launched: { f1: { at: T0, project: a.target.project, task: "Nova VPN/fix:TZ-47" } } };

  const b = decideLaunch(q, ["f1"], [], launch, T0 + 2000, cfg, () => 0);
  assert.equal(b.target.project, "Beta App");
  launch = { ...b.launch, launched: { ...launch.launched, f2: { at: T0 + 2000, project: b.target.project, task: "Beta App/review:TZ-142" } } };

  const c = decideLaunch(q, ["f1","f2"], [], launch, T0 + 4000, cfg, () => 0);
  assert.equal(c.target.project, "EchoRedactor");
});

test("вся работа отмеченного проекта зарезервирована pending-start → не называем проект неотмеченным", () => {
  const launched = { f1: { at: T0, task: "Beta App/x", project: "Beta App" } };
  const cfg = { ...LAUNCH_DEFAULTS, projects: ["Beta App"], parkBufferMin: 0 };
  const plan = decideLaunch([BS], [], [], { launched }, T0 + 60_000, cfg);
  assert.equal(plan.action, "skip");
  assert.match(plan.reason, /зарезервирована pending-start/);
  assert.doesNotMatch(plan.reason, /неотмеченных: Beta App/);
});

const NO_WORK = [{ ...BS, count: 0, next: null }];
const parkWorker = (label, over = {}) =>
  ({ id: label, label, task: null, silenceMin: 0, budgetLeft: 5, ...over });

test("пустая очередь никогда не создаёт агентский чат, даже со старым буфером", () => {
  for (const workers of [[], [parkWorker("w1")], [parkWorker("w1"), parkWorker("w2")]]) {
    for (const parkBufferMin of [0, 3, 30]) {
      const r = decideLaunch(NO_WORK, [], workers, {}, T0,
        { ...LAUNCH_DEFAULTS, parkBufferMin, parkBufferMax: 50 });
      assert.equal(r.action, "skip");
      assert.equal(r.target, undefined);
    }
  }
});

test("единственная задача уже ожидает старта: пустой буфер не создаёт второго агента", () => {
  const launched = { f1: { at:T0, project:BS.name, task:"Beta App/TZ-1" } };
  const r = decideLaunch([BS], [], [], {launched}, T0+20_000);
  assert.equal(r.action,"skip");
  assert.match(r.reason,/зарезервирована/);
});

test("реальный захват до открытия чата не удаляет очередь первого сообщения", () => {
  const launched = { f1:{at:T0,project:BS.name,preclaimed:true,needsStart:true} };
  const workers = [{id:"W1",label:"f1",task:"Beta App/TZ-1",taken:1,budgetLeft:4}];
  const r=decideLaunch(NO_WORK,["f1"],workers,{launched},T0+1000);
  assert.ok(r.launch.launched.f1);
  assert.equal(r.action,"skip");
});

test("три мёртвых pending-start не держат maxPending дольше join timeout", () => {
  const launched = Object.fromEntries(["f1", "f2", "f3"].map((label) => [
    label, { at: T0, task: "Beta App/x", project: "Beta App" },
  ]));
  const cfg = { ...LAUNCH_DEFAULTS, projects: ["Beta App"], maxPending: 3, maxWorking: 8 };
  const queue = [{ ...BS, count: 5 }];
  const before = decideLaunch(queue, [], [], { launched }, T0 + (LAUNCH_DEFAULTS.joinTimeoutSec - 1) * 1000, cfg);
  assert.equal(before.action, "skip");
  assert.match(before.reason, /ожидают подключения 3\/3/);
  const after = decideLaunch(queue, [], [], { launched }, T0 + (LAUNCH_DEFAULTS.joinTimeoutSec + 1) * 1000, cfg);
  assert.equal(after.action, "launch");
  assert.deepEqual(after.failed.sort(), ["f1", "f2", "f3"]);
});

test("подключившаяся вкладка перестаёт быть ожидающей", () => {
  const launch = { launched: { f1: { at: T0, task: "x" } }, lastLaunchAt: 0 };
  const workers = [{ id: "W6", label: "f1", task: null, silenceMin: 0 }];
  assert.equal(decideLaunch(Q(), ["f1"], workers, launch, T0 + 60_000).action, "launch");
});

test("места считаются по всему флоту, без давно молчащих", () => {
  const busyFleet = [
    { id: "W5", label: "bs-1", task: "Beta App/TZ-02", silenceMin: 2 },
    { id: "W7", label: "x", task: "Beta App/TZ-03", silenceMin: 5 },
  ];
  const two = { ...LAUNCH_DEFAULTS, maxWorking: 2 };
  assert.match(decideLaunch(Q(), [], busyFleet, {}, T0, two).reason, /рабочие места/);
  const stale = [{ id: "W1", label: "imp", task: "Nova VPN/TZ-25", silenceMin: 2676 }, busyFleet[0]];
  assert.equal(decideLaunch(Q(), [], stale, {}, T0, two).action, "launch");
});

test("потолок вкладок — клавиатуры, не места агентов; и пауза между запусками", () => {
  const labels = Array.from({ length: LAUNCH_DEFAULTS.maxTabs }, (_, i) => `t${i}`);
  assert.match(decideLaunch(Q(), labels, [], {}, T0).reason, /потолок/);
  assert.equal(decideLaunch(Q(), labels.slice(1), [], {}, T0).action, "launch");
  // агентов больше, чем вкладок: места считает сервер, вкладок держим 1–3
  assert.equal(LAUNCH_DEFAULTS.maxWorking > LAUNCH_DEFAULTS.maxTabs, true);
  assert.match(decideLaunch(Q(), [], [], { lastLaunchAt: T0 - 2_000 }, T0).reason, /недавно/);
});

test("клавиатура: идущий ход закрывается через closeGeneratingSec, руки на странице — нет", () => {
  const on = { onChat: true, generating: true };
  const first = keyboardPlan(on, true, {}, T0);
  assert.equal(first.close, false);
  assert.equal(keyboardPlan(on, true, first.mem, T0 + 3_000).close, false);
  assert.equal(keyboardPlan(on, true, first.mem, T0 + 4_000).close, true);
  assert.equal(keyboardPlan(on, false, first.mem, T0 + 60_000).close, false, "чужую вкладку не трогаем");
  assert.equal(keyboardPlan({ ...on, onChat: false }, true, first.mem, T0 + 60_000).close, false, "чат ещё не создан");
  assert.equal(keyboardPlan({ ...on, blocked: true }, true, first.mem, T0 + 60_000).close, false, "подтверждение/вход");
  assert.equal(keyboardPlan({ ...on, composerOwned: true, composerEmpty: false }, true, first.mem, T0 + 60_000).close, false);
  assert.equal(keyboardPlan(on, true, first.mem, T0 + 60_000, { ...KEYBOARD_DEFAULTS, enabled: false }).close, false);
});

test("клавиатура: простой закрывается только при нехватке памяти и без ждущей будки", () => {
  const idle = { onChat: true, generating: false, freeMemMB: 5000 };
  assert.equal(keyboardPlan(idle, true, {}, T0).close, false);
  assert.equal(keyboardPlan({ ...idle, freeMemMB: 300 }, true, {}, T0).close, true);
  assert.equal(keyboardPlan({ ...idle, freeMemMB: 300, wakePending: true }, true, {}, T0).close, false);
});

test("закрывать только свою вкладку, которая закончила работу и долго молчит", () => {
  const done = { id: "W6", label: "f1", task: null, silenceMin: 20 };
  const quiet = idleTab({ idleSec: LAUNCH_DEFAULTS.closeIdleSec + 1 });
  assert.equal(shouldClose(quiet, done, true), true);
  assert.equal(shouldClose(quiet, done, false), false);
  assert.equal(shouldClose(quiet, { ...done, task: "P/TZ-1" }, true), false);
  assert.equal(shouldClose(idleTab({ idleSec: 60 }), done, true), false);
});

test("закрытие: обратный отсчёт виден, а недавно работавший воркер держит вкладку открытой", () => {
  const done = { id: "W6", label: "f1", task: null, silenceMin: 20 };
  const fresh = closePlan(idleTab({ idleSec: 60 }), done, true);
  assert.equal(fresh.close, false);
  assert.match(fresh.reason, new RegExp(`закрою примерно через ${Math.ceil((LAUNCH_DEFAULTS.closeIdleSec - 60) / 60)} мин`));
  // Переписка молчит давно, но воркер только что вызывал инструменты — рано.
  const busyWorker = closePlan(idleTab({ idleSec: 3600 }), { ...done, silenceMin: 1 }, true);
  assert.equal(busyWorker.close, false);
  assert.equal(closePlan(idleTab({ idleSec: 3600, generating: true }), done, true).close, false);
  assert.equal(closePlan(idleTab({ idleSec: 3600, composerEmpty: false }), done, true).close, false);
  assert.equal(closePlan(idleTab({ idleSec: 3600 }), done, true).close, true);
  assert.equal(closePlan(idleTab({ idleSec: 3600 }), done, false), null, "чужую вкладку не трогаем");
  assert.equal(closePlan(idleTab({ idleSec: 3600 }), { ...done, task: "P/TZ-1" }, true), null);
});

import { CONTINUE_TEXTS, continueText, clearLegacyCooldown, migrateSettings } from "../policy.js";
import { parseAttention } from "../fleet.js";

test("темп в секундах; старый часовой бюджет не ограничивает новые чаты", () => {
  const first = decideLaunch(Q(), [], [], {}, T0, LAUNCH_DEFAULTS, () => 1);
  assert.equal(first.launch.nextGapSec, Math.round(LAUNCH_DEFAULTS.minGapSec * 1.5));
  assert.deepEqual(first.launch.history, [], "простое открытие project-вкладки не считается новым чатом");
  assert.equal(decideLaunch(Q(), [], [], first.launch, T0 + (first.launch.nextGapSec - 1) * 1000).action, "skip");
  const full = { history: Array.from({ length: 20 }, (_, i) => T0 - i * 60_000), lastLaunchAt: 0 };
  assert.equal(decideLaunch(Q(), [], [], full, T0 + 1000).action, "launch");
  assert.equal(decideLaunch(Q(), [], [], full, T0 + 1000,
    {...LAUNCH_DEFAULTS, maxNewChatsPerHour: 1}).action, "launch");
  const counted = recordStartedChat({ history: [], launched: { f1: { at: T0 } } }, "f1", T0 + 2000);
  assert.deepEqual(counted.history, [T0 + 2000]);
  assert.equal(counted.launched.f1.countedChatAt, T0 + 2000);
  assert.deepEqual(recordStartedChat(counted, "f1", T0 + 3000).history, [T0 + 2000], "повторный recovery не считает чат дважды");
  const older = recordStartedChat({ history: [T0 - 4 * 3600_000, T0 - 6 * 3600_000] }, "f2", T0);
  assert.deepEqual(older.history, [T0 - 4 * 3600_000, T0], "до перегрузки храним 5 часов истории");
});

test("старое окно и выученный потолок не запрещают запуск после обновления", () => {
  const old = { cooldownUntil: T0 + 7200_000, cooldownSec: 7200, chatCeiling: 3, lastRateLimitAt: T0, history: [] };
  const plan = decideLaunch(Q(), [], [], old, T0);
  assert.equal(plan.action, "launch");
  assert.equal(plan.launch.cooldownUntil, undefined);
  assert.deepEqual(clearLegacyCooldown(old), { history: [] });
});

test("«продолжи» — разными словами и не ровно через minGapSec", () => {
  assert.ok(CONTINUE_TEXTS.length >= 5);
  assert.equal(continueText(() => 0), CONTINUE_TEXTS[0]);
  assert.equal(continueText(() => 0.999), CONTINUE_TEXTS[CONTINUE_TEXTS.length - 1]);
  const sent = decide(idleTab(), busy(), emptyMemory(), T0, DEFAULTS, () => 1);
  assert.equal(sent.mem.jitterSec, 30);
  const early = decide(idleTab(), busy(), sent.mem, T0 + (DEFAULTS.minGapSec + 15) * 1000, DEFAULTS, () => 0);
  assert.equal(early.action, "skip");
});

test("разбор строк «внимание» из fleet_list", () => {
  const text = "W19 (f1) — без задачи; молчит 1 мин\nвнимание: Beta App/fix:TZ-04:4 — нужен владелец: порочный круг: 4-й отказ подряд по TZ-04\nочередь: выдавать нечего";
  assert.deepEqual(parseAttention(text), [
    { task: "Beta App/fix:TZ-04:4", note: "порочный круг: 4-й отказ подряд по TZ-04" },
  ]);
});


test("разбор расхода чата: отдельные строки, старые строки воркеров не ломаются", () => {
  const workers = parseWorkers(
    [
      "W33 (f0922-1559r9y) — без задачи; молчит 1 мин",
      "W34 (f0922-1608h2d) — Beta App/TZ-06; молчит 0 мин",
      "W7 (cc-bs) — без задачи; молчит 3 мин",
      "чат W33: взято задач 2 из 5",
      "чат W34: взято задач 5 из 5",
    ].join("\n"),
  );
  assert.deepEqual(workers.map((w) => [w.id, w.task, w.budgetLeft]), [
    ["W33", null, 3],
    ["W34", "Beta App/TZ-06", 0],
    ["W7", null, undefined],
  ]);
});

const doneTab = (over = {}) => idleTab({ own: true, queueHasWork: true, ...over });
const afterTask = () => decide0(idleTab(), busy(), emptyMemory(), T0).mem;
const done = (over = {}) => busy({ task: null, taken: 1, budgetLeft: 4, ...over });

test("своя вкладка сдала работу, лимит чата не исчерпан → один раз «бери следующую»", () => {
  const r = decide0(doneTab(), done(), afterTask(), T0 + 600_000);
  assert.equal(r.action, "continue");
  assert.match(r.text, /fleet_take\(agent='W5'\)/);
  assert.equal(decide0(doneTab(), done(), r.mem, T0 + 900_000).action, "skip"); // повтора нет
});

test("«бери следующую» и без памяти вкладки о сданной задаче — по числу взятых задач", () => {
  // память вкладки сбросилась (расширение обновили после сдачи) — подталкиваем всё равно
  const r = decide0(doneTab(), done(), emptyMemory(), T0 + 600_000);
  assert.equal(r.action, "continue");
  assert.equal(r.mem.nudgedTaken, 1);
});

test("«бери следующую» не шлётся: лимит исчерпан, очередь пуста, чужая вкладка, задач не брал, сервер без лимита", () => {
  const mem = afterTask();
  const later = T0 + 600_000;
  assert.equal(decide0(doneTab(), done({ budgetLeft: 0, taken: 5 }), mem, later).action, "skip");
  assert.equal(decide0(doneTab({ queueHasWork: false }), done(), mem, later).action, "skip");
  assert.equal(decide0(doneTab({ own: false }), done(), mem, later).action, "skip");
  assert.equal(decide0(doneTab(), busy({ task: null }), mem, later).action, "skip"); // сервер без лимита
  assert.equal(decide0(doneTab(), done({ taken: 0, budgetLeft: 5 }), emptyMemory(), later).action, "skip");
  assert.equal(decide0(doneTab({ idleSec: DEFAULTS.idleGraceSec - 1 }), done(), mem, later).action, "skip"); // ещё договаривает
});

test("после следующей задачи «бери следующую» снова возможно", () => {
  const first = decide0(doneTab(), done(), afterTask(), T0 + 600_000);
  const working = decide0(idleTab(), busy({ task: "P/TZ-2", taken: 2, budgetLeft: 3 }), first.mem, T0 + 1_200_000);
  const again = decide0(doneTab(), done({ taken: 2, budgetLeft: 3 }), working.mem, T0 + 2_400_000);
  assert.equal(again.action, "continue");
});

test("своя вкладка между задачами занимает место — лишний новый чат не открывается", () => {
  const one = { ...LAUNCH_DEFAULTS, maxWorking: 1 };
  const between = [{ id: "W33", label: "f1", task: null, silenceMin: 1, budgetLeft: 3 }];
  assert.match(decideLaunch(Q(), ["f1"], between, {}, T0, one).reason, /рабочие места/);
  const spent = [{ ...between[0], budgetLeft: 0 }];
  assert.equal(decideLaunch(Q(), ["f1"], spent, {}, T0, one).action, "launch");
  const quiet = [{ ...between[0], silenceMin: 30 }];
  assert.equal(decideLaunch(Q(), ["f1"], quiet, {}, T0, one).action, "launch");
});


import { RELOAD_DEFAULTS, reloadPlan } from "../policy.js";

test('retired platform is removed while every other project and owner setting survives', () => {
  const saved = {paceVersion:10,projects:['Beta App','Fleet Platform','Custom'],projectsSeen:['Beta App','Fleet Platform','Custom'],enabled:false,maxWorking:12};
  assert.deepEqual(migrateSettings(saved),{...saved,projects:['Beta App','Custom'],projectsSeen:['Beta App','Custom']});
  const seenOnly = {...saved,projects:['Beta App','Custom']};
  assert.deepEqual(migrateSettings(seenOnly),{...seenOnly,projectsSeen:['Beta App','Custom']});
});

test('preclaimed startup does not reserve the remaining ready task twice', () => {
  const queued = [{name:'P',count:1,next:{tz:'TZ-2',kind:'work'}}];
  const launch = {lastLaunchAt:0,launched:{f1:{at:T0,project:'P',task:'P/TZ-1',preclaimed:true,needsStart:true}}};
  const workers = [{label:'f1',task:'P/TZ-1',silenceMin:0,taken:1,budgetLeft:19}];
  const plan = decideLaunch(queued,[],workers,launch,T0+1000,{...LAUNCH_DEFAULTS,projects:['P'],maxWorking:2},()=>0);
  assert.equal(plan.action,'launch');
  assert.equal(plan.target.tz,'TZ-2');
});

test("«Соединение прервано»: ждём interruptedSec, потом перезагрузка, не чаще minGapSec", () => {
  const cut = { interrupted: "Соединение прервано. Ожидание полного ответа", composerEmpty: true };
  const first = reloadPlan(cut, undefined, [], T0);
  assert.equal(first.reload, false);
  const held = reloadPlan(cut, first.mem, first.history, T0 + (RELOAD_DEFAULTS.interruptedSec + 1) * 1000);
  assert.equal(held.reload, true);
  assert.equal(held.history.length, 1);
  // сразу после перезагрузки снова обрыв — не долбим
  const again1 = reloadPlan(cut, held.mem, held.history, T0 + 120_000);
  const again2 = reloadPlan(cut, again1.mem, again1.history, T0 + 200_000);
  assert.equal(again2.reload, false);
  const later1 = reloadPlan(cut, again2.mem, again2.history, T0 + 400_000);
  assert.equal(later1.reload, true);
});

test("полный экран «не удалось загрузить разговор» быстро перезагружает вкладку и не уходит в reload-loop", () => {
  const broken = { pageLoadFailed: "Не удалось загрузить этот разговор ChatGPT", interrupted: "", composerEmpty: true };
  const first = reloadPlan(broken, undefined, [], T0);
  assert.equal(first.reload, false);
  const reload = reloadPlan(broken, first.mem, first.history, T0 + 3_000);
  assert.equal(reload.reload, true);
  assert.match(reload.reason, /перезагружаю страницу/);
  const again1 = reloadPlan(broken, reload.mem, reload.history, T0 + 10_000);
  const again2 = reloadPlan(broken, again1.mem, again1.history, T0 + 14_000);
  assert.equal(again2.reload, false);
  assert.match(again2.reason, /сразу после перезагрузки/);
  const recoveredStillBroken = reloadPlan(broken, again2.mem, again2.history, T0 + 20_000);
  assert.equal(recoveredStillBroken.reload, true, "полный load failure не должен ждать 3 минуты между reload");
});

test("исчезнувший composer в живом /c/ быстро лечится reload и имеет свой короткий gap", () => {
  const broken = { composerMissing: true, interrupted: "", pageLoadFailed: "", composerEmpty: true };
  const first = reloadPlan(broken, undefined, [], T0);
  assert.equal(first.reload, false);
  const reload = reloadPlan(broken, first.mem, first.history, T0 + (RELOAD_DEFAULTS.composerMissingSec + 1) * 1000);
  assert.equal(reload.reload, true);
  assert.match(reload.reason, /поле ввода разговора не загрузилось/);
  const again = reloadPlan(broken, reload.mem, reload.history, T0 + 20_000);
  assert.equal(again.reload, false);
  const later = reloadPlan(broken, again.mem, again.history, T0 + 40_000);
  assert.equal(later.reload, true);
});

test("перезагрузка: надпись пропала — счёт сначала; текст в поле ввода и потолок в час держат", () => {
  const cut = { interrupted: "Connection interrupted", composerEmpty: true };
  const seen = reloadPlan(cut, undefined, [], T0);
  const gone = reloadPlan({ interrupted: "", composerEmpty: true }, seen.mem, [], T0 + 30_000);
  assert.equal(gone.mem.since, 0);
  const typing = reloadPlan({ ...cut, composerEmpty: false }, { since: T0 - 600_000 }, [], T0);
  assert.equal(typing.reload, false);
  assert.match(typing.reason, /поле ввода/);
  const many = Array.from({ length: RELOAD_DEFAULTS.maxPerHour }, (_, i) => T0 - i * 60_000);
  const capped = reloadPlan(cut, { since: T0 - 600_000 }, many, T0);
  assert.equal(capped.reload, false);
  assert.match(capped.reason, /потолок/);
});

test("миграция полностью удаляет старый часовой предел из storage и сохраняет намерения", () => {
  const old = { maxWorking: 10, minGapMin: 0.5, projects: ["P"], maxNewChatsPerHour: 15, learnCeiling: false, dryRun: false };
  const next = migrateSettings(old);
  assert.equal("maxNewChatsPerHour" in next, false);
  assert.equal(next.maxWorking, 10);
  assert.equal(next.minGapMin, undefined, "пауза — отточенный параметр, не ручка");
  assert.equal(next.dryRun, false);
  assert.equal(next.enabled, true);
  assert.equal(next.autostart, undefined, "автозапуск слит с «Включено»");
  assert.deepEqual(next.projects, ["P"]);
  assert.equal(next.paceVersion, 10);
  assert.equal("maxNewChatsPerHour" in migrateSettings({ ...next, maxNewChatsPerHour: 12 }), false);
  assert.equal("chatCeiling" in migrateSettings({ ...next, chatCeiling: 3 }), false);
  assert.equal(migrateSettings({ ...next, enabled: false }).enabled, false);
});

test("миграция старого стандартного набора включает новый продуктовый проект", () => {
  const old = {
    paceVersion: 3,
    projects: ["Beta App", "Nova VPN"],
    maxWorking: 8,
    maxNewChatsPerHour: 0,
    minGapMin: 1.5,
  };
  const next = migrateSettings(old);
  assert.deepEqual(next.projects, ["Beta App", "Nova VPN", "EchoRedactor"]);
  assert.equal(next.paceVersion, 10);

  const reversed = migrateSettings({ ...old, projects: ["Nova VPN", "Beta App"] });
  assert.deepEqual(reversed.projects, ["Nova VPN", "Beta App", "EchoRedactor"]);
});

test("миграция не возвращает проект, если список уже был явно кастомизирован", () => {
  const custom = migrateSettings({
    paceVersion: 3,
    projects: ["Beta App"],
    maxNewChatsPerHour: 0,
    minGapMin: 1.5,
  });
  assert.deepEqual(custom.projects, ["Beta App"]);
  assert.equal(custom.paceVersion, 10);
});

test("миграция 10 выбрасывает отточенные параметры и старый бюджет", () => {
  const saved = { paceVersion: 8, enabled: true, autostart: true, maxWorking: 18, maxNewChatsPerHour: 0,
    maxPending: 10, spareTabs: 4, minGapMin: 0.25, retryGapSec: 20, dryRun: false, projects: ["P"] };
  const next = migrateSettings(saved);
  assert.deepEqual(next, { paceVersion: 10, enabled: true, maxWorking: 18, dryRun: false, projects: ["P"] });
  assert.equal(migrateSettings(next), next, "повторная миграция ничего не меняет");
});

test("defaults contain no installation-specific project names", () => {
  assert.deepEqual(BASE_LAUNCH_DEFAULTS.projects, []);
});

test("мало свободной памяти — новую вкладку не открываем; неизвестно — не мешаем", () => {
  const low = decideLaunch(Q(), [], [], {}, T0, LAUNCH_DEFAULTS, Math.random, LAUNCH_DEFAULTS.minFreeMemMB - 1);
  assert.match(low.reason, /свободно памяти/);
  assert.equal(decideLaunch(Q(), [], [], {}, T0, LAUNCH_DEFAULTS, Math.random, null).action, "launch");
  assert.equal(decideLaunch(Q(), [], [], {}, T0, LAUNCH_DEFAULTS, Math.random, 4000).action, "launch");
});


import { AUTOSTART_LABEL, isAutostartLabel, startRetryText } from "../policy.js";

const failedTab = (over = {}) =>
  idleTab({ failed: "Время доставки сообщения истекло. Попробуйте еще раз.", failedSec: DEFAULTS.failedTurnSec + 1, idleSec: 20, ...over });

test("«Время доставки сообщения истекло» → «Повторить», если кнопка есть, иначе «продолжи» — быстро", () => {
  const withButton = decide0(failedTab({ hasRetryButton: true }), busy(), emptyMemory(), T0);
  assert.equal(withButton.action, "click-retry");
  assert.ok(withButton.text); // кнопка пропадёт — будет чем продолжить текстом
  const noButton = decide0(failedTab(), busy(), emptyMemory(), T0);
  assert.equal(noButton.action, "continue");
  // idleSec 20 < idleGraceSec: сбой хода не ждёт общей паузы, только failedTurnSec
  const flash = decide0(failedTab({ failedSec: 3 }), busy(), emptyMemory(), T0);
  assert.equal(flash.action, "skip");
});

test("сбой хода после нашего сообщения (lastRole user) — тоже повтор, а не вечное ожидание", () => {
  const r = decide0(failedTab({ lastRole: "user" }), busy(), emptyMemory(), T0);
  assert.equal(r.action, "continue");
});

test("после прогресса счёт повторов с нуля", () => {
  let mem = decide0(failedTab(), busy({ silenceMin: 60 }), emptyMemory(), T0).mem;
  assert.equal(mem.retries, 1);
  // после повтора агент вызывал инструменты (silenceMin 0) — снова сбой через 5 минут
  const later = decide0(failedTab(), busy({ silenceMin: 0 }), mem, T0 + 300_000);
  assert.equal(later.mem.retries, 1);
});

test("старая вечная остановка «сообщение отправлено, ответа нет» снимается", () => {
  const legacy = { ...emptyMemory(), task: "P/TZ-1", stopped: "сообщение отправлено, ответа нет" };
  const r = decide0(idleTab({ lastRole: "user", idleSec: 300 }), busy(), legacy, T0);
  assert.equal(r.action, "continue");
  const real = { ...emptyMemory(), task: "P/TZ-1", stopped: "3 «продолжи» подряд без единого вызова инструмента" };
  assert.equal(decide0(idleTab(), busy(), real, T0).action, "skip");
});

test("ожидание ручных прав получает одно recovery-указание, затем claim автоблокируется вместо спама", () => {
  const tab = idleTab({ blockerWaiting: "Жду владельца: нужно одобрение прав", idleSec: DEFAULTS.idleGraceSec + 5 });
  const first = decide0(tab, busy(), { ...emptyMemory(), task: "P/TZ-1" }, T0);
  assert.equal(first.action, "continue");
  assert.match(first.text, /Не жди владельца/);
  assert.equal(first.mem.blockerNudges, 1);
  const second = decide0(tab, busy({ silenceMin: 60 }), first.mem, T0 + (DEFAULTS.minGapSec + 10) * 1000);
  assert.equal(second.action, "block-claim");
  assert.match(second.reason, /освобождаю claim/);
  assert.match(second.blockReason, /^blocked:/);
});

const startTab = (over = {}) =>
  idleTab({ label: "f0922-1743l3o", own: true, pendingJoin: true, idleSec: DEFAULTS.startRetryAfterSec + 5, ...over });

test("fleet_join не пришёл (отказ на старте) → повтор первого шага тем же вызовом, не больше maxStartRetries", () => {
  let r = decide0(startTab(), undefined, emptyMemory(), T0);
  assert.equal(r.action, "continue");
  assert.match(r.text, /fleet_join\(label='f0922-1743l3o', kind='chatgpt'\)/);
  assert.equal(decide0(startTab(), undefined, r.mem, T0 + 10_000).action, "skip"); // не долбим
  r = decide0(startTab(), undefined, r.mem, T0 + (DEFAULTS.retryGapSec + 1) * 1000);
  assert.equal(r.action, "continue");
  const third = decide0(startTab(), undefined, r.mem, T0 + 2 * (DEFAULTS.retryGapSec + 1) * 1000);
  assert.equal(third.action, "skip");
  assert.equal(startRetryText("x").includes("не обход"), true);
});

test("повтор первого шага: не своя, не ждёт подключения, ещё думает, ещё не ответила → не трогать", () => {
  assert.equal(decide0(startTab({ own: false }), undefined, emptyMemory(), T0).action, "skip");
  assert.equal(decide0(startTab({ pendingJoin: false }), undefined, emptyMemory(), T0).action, "skip");
  assert.equal(decide0(startTab({ generating: true }), undefined, emptyMemory(), T0).action, "skip");
  assert.equal(decide0(startTab({ idleSec: DEFAULTS.startRetryAfterSec - 1 }), undefined, emptyMemory(), T0).action, "skip");
  assert.equal(decide0(startTab({ lastRole: "user", idleSec: 70 }), undefined, emptyMemory(), T0).action, "skip");
  // но стартовое сообщение с «Время доставки истекло» — повторить сразу
  const failedStart = decide0(startTab({ lastRole: "user", idleSec: 20, failed: "Время доставки сообщения истекло", failedSec: 30 }), undefined, emptyMemory(), T0);
  assert.equal(failedStart.action, "continue");
  assert.match(failedStart.text, /fleet_join/);
});

test("своя вкладка, так и не подключившаяся к флоту, закрывается после closeFailedSec", () => {
  const tab = idleTab({ label: "f0922-1743l3o", idleSec: LAUNCH_DEFAULTS.closeFailedSec + 1 });
  assert.equal(closePlan(tab, undefined, true)?.close, true);
  assert.equal(closePlan({ ...tab, pendingJoin: true }, undefined, true), null); // ещё ждём подключения
  assert.equal(closePlan({ ...tab, idleSec: 60 }, undefined, true), null);
  assert.equal(closePlan(tab, undefined, false), null); // чужая
});

test("метка автозапуска распознаётся — вкладка своя и без памяти расширения", () => {
  assert.equal(isAutostartLabel("f0921-1213xvb"), true);
  assert.equal(isAutostartLabel("f0922-1743l3o"), true);
  assert.equal(isAutostartLabel("bs-1"), false);
  assert.equal(isAutostartLabel("cc-beta-app"), false);
  assert.ok(AUTOSTART_LABEL instanceof RegExp);
});

test("старый chatCeiling в launch storage не ограничивает запуск", () => {
  const plan = decideLaunch(Q(), [], [], { chatCeiling: 3, lastRateLimitAt: T0,
    history: Array.from({length: 50}, (_, i) => T0 - i * 60_000) }, T0);
  assert.equal(plan.action, "launch");
});

import { hungText } from "../policy.js";

test("зависание без надписи: «Стоп» есть, текст стоит stallSec и воркер молчит → перезагрузка", () => {
  const hung = { generating: true, stallSec: RELOAD_DEFAULTS.stallSec + 5, composerEmpty: true, interrupted: "" };
  const quietWorker = { id: "W41", task: "P/TZ-07", silenceMin: 22 };
  const r = reloadPlan(hung, undefined, [], T0, RELOAD_DEFAULTS, quietWorker);
  assert.equal(r.reload, true);
  assert.match(r.reason, /зависание/);
  // сразу снова — нет: не чаще minGapSec
  assert.equal(reloadPlan(hung, r.mem, r.history, T0 + 60_000, RELOAD_DEFAULTS, quietWorker).reload, false);
});

test("не зависание: текст меняется, генерации нет, воркер только что вызывал инструмент", () => {
  const base = { generating: true, stallSec: RELOAD_DEFAULTS.stallSec + 5, composerEmpty: true, interrupted: "" };
  const busyWorker = { id: "W41", task: "P/TZ-07", silenceMin: 1 };
  assert.equal(hungText({ ...base, stallSec: 30 }, { silenceMin: 30 }), "");
  assert.equal(hungText({ ...base, generating: false }, { silenceMin: 30 }), "");
  assert.equal(hungText(base, busyWorker), ""); // долгий вызов инструмента — не зависание
  assert.notEqual(hungText(base, undefined), ""); // ещё не подключилась — только по тексту
  const custom = { ...RELOAD_DEFAULTS, stallSec: 60 };
  assert.notEqual(hungText({ ...base, stallSec: 61 }, { silenceMin: 2 }, custom), "");
});

test("новый проект флота включается сам, снятая владельцем галочка остаётся снятой", async () => {
  const { includeNewProjects } = await import("../policy.js");
  const fleet = [{ name: "A" }, { name: "B" }, { name: "New" }];
  const first = includeNewProjects({ projects: ["A"] }, fleet);
  assert.deepEqual(first.projects, ["A", "B", "New"]);
  assert.deepEqual(first.added, ["B", "New"]);
  const owner = { projects: ["A", "New"], projectsSeen: first.projectsSeen };
  assert.equal(includeNewProjects(owner, fleet), null);
  const later = includeNewProjects(owner, [...fleet, { name: "Later" }]);
  assert.deepEqual(later.projects, ["A", "New", "Later"]);
  assert.ok(!later.projects.includes("B"));
});
