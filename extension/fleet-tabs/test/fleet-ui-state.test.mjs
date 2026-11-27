import test from "node:test";
import assert from "node:assert/strict";
import { agentOverview, quickOverview, quickDiscardEligible, fleetOverview, badgeAppearance, activityPaths, specialTabClose,
  silentClaimTabClose, tabHealth } from "../fleet-ui-state.js";

test("активные агенты: только свежий MCP плюс непересекающаяся генерация UI", () => {
  const states = { working: 5, thinking: 2, waiting: 3, silent: 8 };
  assert.deepEqual(agentOverview(states), { active: 7, mcp: 5, generating: 2, waiting: 3, inactive: 8 });
  assert.equal(agentOverview({ ...states, working: -1 }), null);
  assert.equal(agentOverview(null), null);
});

test("бейдж: только работающие сейчас обычные и быстрые, без удвоения и тревог", () => {
  const states = { working: 4, thinking: 1, waiting: 10, silent: 6 };
  const quick = { open: 3, active: 2 };
  assert.deepEqual(fleetOverview(states, quick), {
    total: 24, working: 7, active: 5, mcp: 4, generating: 1, waiting: 10,
    inactive: 6, quick: 3, quickActive: 2, quickIdle: 1,
    quickParked: 0, quickAnswered: 0, quickWaitingSend: 0, quickWaiting: 1, quickOpened: 0,
  });
  assert.deepEqual(badgeAppearance(states, quick), {
    text: "7", color: "#2e7d32",
    title: "Работают сейчас: 7 = обычные 5 (MCP 4, генерация 1) + быстрые 2; запаркованы 0; ответили, ждут владельца 0; ждут отправки 0; открыто вкладок 0; ждут 10; не подтверждены 6",
  });
  const alert = badgeAppearance(states, quick, 9);
  assert.equal(alert.text, "7");
  assert.equal(alert.color, "#c62828");
  assert.match(alert.title, /требуется внимание: 9/);
  assert.equal(badgeAppearance(states, quick, 0, true).text, "?");
  assert.equal(badgeAppearance(states, null).text, "?");
  assert.equal(badgeAppearance(null, quick, 2).color, "#c62828");
});

test("бейдж: зелёный при работе быстрых, серый при ожидании, неизвестное не ноль", () => {
  const waiting = { working: 0, thinking: 0, waiting: 12, silent: 0 };
  assert.equal(badgeAppearance(waiting, { open: 2, active: 0 }).text, "0");
  assert.equal(badgeAppearance(waiting, { open: 2, active: 0 }).color, "#757575");
  assert.equal(badgeAppearance(waiting, { open: 2, active: 1 }).color, "#2e7d32");
  assert.equal(badgeAppearance(waiting, { open: 2, active: 1 }).text, "1");
  assert.equal(badgeAppearance(waiting, { open: 0, active: 0 }, 1).color, "#c62828");
  assert.equal(badgeAppearance(waiting, { open: 0, active: 0 }, 1).text, "0");
  assert.equal(fleetOverview(waiting, { open: 1, active: 2 }), null);
  assert.equal(fleetOverview(waiting, { open: -1, active: 0 }), null);
  assert.equal(fleetOverview(waiting, null), null);
});

test("быстрые: открытые отдельно от работающих, регистрация не равна активности", () => {
  const jobs = [
    { label: "pult-q1", recentMcp: false },
    { label: "pult-q2", recentMcp: true },
    { label: "pult-q3", recentMcp: false, close: true },
    { label: "pult-q4", recentMcp: false },
  ];
  assert.deepEqual(quickOverview(jobs, ["pult-q1"]), { open: 3, active: 2, parked: 0, answered: 0, waitingSend: 0, waiting: 1, opened: 0 });
  assert.deepEqual(quickOverview(jobs, []), { open: 3, active: 1, parked: 0, answered: 0, waitingSend: 0, waiting: 2, opened: 0 });
  assert.deepEqual(quickOverview([]), { open: 0, active: 0, parked: 0, answered: 0, waitingSend: 0, waiting: 0, opened: 0 });
  const jobs2 = [...jobs, { label: "pult-q5", status: "parked", recentMcp: true }];
  assert.deepEqual(quickOverview(jobs2, ["pult-q1", "pult-q5"], ["pult-q1", "pult-q5"], ["pult-q2"]),
    { open: 4, active: 1, parked: 1, answered: 0, waitingSend: 0, waiting: 2, opened: 2 });
  assert.equal(quickOverview(null), null);
});

test("answered-unparked waits for owner, not a tab; only unsent outbox without live tab awaits sending", () => {
  const jobs = [
    { label: "pult-q1", status: "answered-unparked", recentMcp: true, open: false, idle: true },
    { label: "pult-q2", status: "parked", recentMcp: true, open: false },
    { label: "pult-q3", status: "new", send: "queued", pending: 1, open: true },
    { label: "pult-q4", status: "working", send: "in composer", pending: 1, recentMcp: true },
    { label: "pult-q5", status: "working", recentMcp: true },
  ];
  const quick = quickOverview(jobs, [], ["pult-q4", "pult-q5"]);
  assert.deepEqual(quick, {open: 5, active: 2, parked: 1, answered: 1,
    waitingSend: 1, waiting: 0, opened: 2});
  assert.deepEqual(quickOverview(jobs.slice(0, 1), [], ["pult-q1"]), {
    open: 1, active: 0, parked: 0, answered: 1, waitingSend: 0, waiting: 0, opened: 1
  }, "even a stale opened answered tab does not create demand for it");
  const summary = fleetOverview({working:0,thinking:0,waiting:0,silent:0},quick);
  assert.equal(summary.quickAnswered, 1);
  assert.equal(summary.quickWaitingSend, 1);
  assert.equal(badgeAppearance({working:0,thinking:0,waiting:0,silent:0},quick).title.includes("ответили, ждут владельца 1"),true);
});

test("история: 24 часа, два графика, без выдуманных точек в пробелах", () => {
  const now = 1_800_000_000_000;
  const points = [
    { ts: now / 1000 - 300, working_regular: 3, working_quick: 2 },
    { ts: now / 1000 - 60, working_regular: 4, working_quick: 0 },
    { ts: now / 1000, working_regular: 1, working_quick: 1 },
  ];
  const paths = activityPaths(points, now);
  assert.equal(paths.max, 5);
  assert.equal(paths.count, 3);
  assert.equal((paths.quick.match(/M/g) || []).length, 2);
  assert.equal((paths.regular.match(/M/g) || []).length, 2);
  assert.equal(activityPaths(points, now + 86_400_001).count, 0);
});

test("idle quick: выгружается через 30 с, но не во время ответа, с черновиком или pending send", () => {
  const now = 500_000;
  const job = { idle: true, close: false, send: null };
  const tab = { id: 1, active: false, discarded: false };
  const page = { generating: false, composerEmpty: true, lastRole: "assistant" };
  const eligible = (j = job, t = tab, s = page, idleAt = now - 30_000) =>
    quickDiscardEligible(j, t, s, idleAt, now);
  assert.equal(eligible(), true);
  assert.equal(eligible(job, tab, page, now - 29_999), false);
  assert.equal(eligible({ ...job, send: "продолжи" }), false);
  assert.equal(eligible({ ...job, idle: false }), false);
  assert.equal(eligible({ ...job, close: true }), false);
  assert.equal(eligible(job, { ...tab, active: true }), false);
  assert.equal(eligible(job, { ...tab, discarded: true }), false);
  for (const field of ["generating", "blocked", "rateLimited", "failed", "blockerWaiting", "pendingJoin"]) {
    assert.equal(eligible(job, tab, { ...page, [field]: true }), false, field);
  }
  assert.equal(eligible(job, tab, { ...page, composerEmpty: false }), false);
  assert.equal(eligible(job, tab, null), false);
});

test("чужие, активные quick и действующий надзор никогда не закрываются", () => {
  const base = { owned: true, chatId: "abcdef12", expectedChatId: "abcdef12",
    page: { generating: false, composerEmpty: true }, watched: new Map(),
    overseerKnown: true, quickKnown: true,
    quickOrders: [{ label: "pult-q1", close: false }] };
  assert.equal(specialTabClose({ ...base, label: "pult-q1" }), "");
  assert.equal(specialTabClose({ ...base, label: "pult-q1", owned: false, quickOrders: [] }), "");
  assert.equal(specialTabClose({ ...base, label: "owner-mine" }), "");
  assert.equal(specialTabClose({ ...base, label: "owner-sv", watched: new Map([["abcdef12", {}]]) }), "");
  assert.equal(specialTabClose({ ...base, label: "owner-sv", page: { generating: true } }), "");
  assert.equal(specialTabClose({ ...base, label: "owner-sv", chatId: "different-chat" }), "");
  assert.equal(specialTabClose({ ...base, label: "pult-q1", quickKnown: false, quickOrders: [] }), "");
  assert.equal(specialTabClose({ ...base, label: "owner-sv", overseerKnown: false }), "");
});

test("закрыть own quick после quick close или исчезновения задания; owner-sv после off", () => {
  const base = { owned: true, chatId: "abcdef12", page: { composerEmpty: true },
    watched: new Map(), overseerKnown: true, quickKnown: true, quickOrders: [] };
  assert.match(specialTabClose({ ...base, label: "pult-q1" }), /быстрый агент закрыт/);
  assert.match(specialTabClose({ ...base, label: "pult-q1", quickOrders: [{ label: "pult-q1", close: true }] }), /быстрый агент закрыт/);
  assert.match(specialTabClose({ ...base, label: "owner-sv" }), /надзор.*отключён/);
  assert.equal(specialTabClose({ ...base, label: "owner-sv", page: { composerEmpty: false } }), "");
});

test("простой и неответившие вкладки: только принадлежащие флоту и без свежего MCP/UI", () => {
  const label = (n) => `f1115-1234${String(n).padStart(3, "0")}`;
  const rows = [
    { id: 1, label: label(1), own: true, page: { idleSec: 150 } },
    { id: 2, label: label(2), own: true, page: null },
    { id: 3, label: label(3), own: true, page: { generating: true, idleSec: 400 } },
    { id: 4, label: "owner-sv", own: true, page: null },
    { id: 5, label: "pult-q4", own: true, page: null },
    { id: 6, label: label(6), own: false, page: null },
  ];
  assert.deepEqual(tabHealth(rows, [], new Set([3])), { idle: 1, dead: 1 });
  assert.deepEqual(tabHealth(rows, [{ label: label(1), silenceMin: 1 }]), { idle: 0, dead: 1 });
  assert.deepEqual(tabHealth(rows, [], new Set([2])), { idle: 1, dead: 0 });
});

test("6 минут тишины: закрывается только клавиатура с активным claim, без черновика и преград", () => {
  const state = { idleSec: 361, composerEmpty: true, generating: false };
  const worker = { task: "P/TZ-1", silenceMin: 7 };
  const base = { state, worker, owned: true, hasChat: true, wakePending: false };
  assert.equal(silentClaimTabClose(base), true);
  assert.equal(silentClaimTabClose({ ...base, worker: { ...worker, silenceMin: 1 } }), false);
  assert.equal(silentClaimTabClose({ ...base, state: { ...state, idleSec: 60 } }), false);
  assert.equal(silentClaimTabClose({ ...base, worker: { ...worker, task: null } }), false);
  assert.equal(silentClaimTabClose({ ...base, owned: false }), false);
  assert.equal(silentClaimTabClose({ ...base, hasChat: false }), false);
  assert.equal(silentClaimTabClose({ ...base, wakePending: true }), false);
  for (const field of ["generating", "blocked", "blockerWaiting", "rateLimited", "failed", "interrupted", "pendingJoin"]) {
    assert.equal(silentClaimTabClose({ ...base, state: { ...state, [field]: true } }), false, field);
  }
  assert.equal(silentClaimTabClose({ ...base, state: { ...state, composerEmpty: false } }), false);
});
