// Обозначения пульта и бейджа: серверный снимок, без эвристики по числу вкладок/claims.
const natural = (n) => Number.isSafeInteger(n) && n >= 0;

export function agentOverview(states) {
  if (!states || !["working", "thinking", "waiting", "silent"].every((k) => natural(states[k]))) return null;
  // flow.agent_states() проверяет UI только после исключения свежего MCP: множества не пересекаются.
  return { active: states.working + states.thinking, mcp: states.working,
    generating: states.thinking, waiting: states.waiting, inactive: states.silent };
}

export function quickOverview(orders = [], generatingLabels = [], openedLabels = [], errorLabels = []) {
  if (!Array.isArray(orders)) return null;
  const generating = new Set(generatingLabels);
  const opened = new Set(openedLabels);
  const errors = new Set(errorLabels);
  const jobs = orders.filter((j) => j && !j.close && /^pult-q\d+(?:-[0-9a-z]+)?$/i.test(j.label || ""));
  const parkedJob = (j) => j.status === "parked" && !quickNeedsInterface(j);
  const hasOutbox = (j) => Boolean(j.send || Number(j.pending) > 0);
  // answered-unparked has delivered its answer and waits for Claude Code / lead;
  // it does not need a browser tab, even if its MCP activity is still recent.
  const answeredJob = (j) => !hasOutbox(j) && !j.wake && ["answered-unparked", "answered"].includes(j.status);
  const awaitingTab = (j) => hasOutbox(j) && !opened.has(j.label);
  const parked = jobs.filter(parkedJob).length;
  const answered = jobs.filter((j) => !parkedJob(j) && answeredJob(j)).length;
  const waitingSend = jobs.filter((j) => !parkedJob(j) && awaitingTab(j)).length;
  const active = jobs.filter((j) => !parkedJob(j) && !answeredJob(j) &&
    !awaitingTab(j) && !errors.has(j.label) &&
    (j.recentMcp === true || generating.has(j.label))).length;
  // Unclassified/quiet jobs are separate from completed answers and outbox.
  const waiting = jobs.length - active - parked - answered - waitingSend;
  return { open: jobs.length, active, parked, answered, waitingSend, waiting,
    opened: jobs.filter((j) => opened.has(j.label)).length };
}

// Выгрузка только собственной, неактивной и завершившей ход вкладки.
// Состояние idle приходит от сервера; недостаточно одного локального idleSec.
export function quickDiscardEligible(job, tab, page, idleSince, now = Date.now()) {
  return Boolean(job?.idle && !job.close && !job.send &&
    tab && !tab.active && !tab.discarded && page && !page.generating &&
    !page.blockerWaiting && !page.blocked && !page.failed && !page.rateLimited &&
    !page.interrupted && !page.pendingJoin && !page.pageLoadFailed &&
    !page.accountLoadFailed && page.composerEmpty === true &&
    page.lastRole === "assistant" &&
    Number.isFinite(idleSince) && idleSince > 0 && now - idleSince >= 30_000);
}

// Быстрые агенты не входят в серверную четвёрку; считаем открытые чаты один раз.
export function fleetOverview(states, quick) {
  const agents = agentOverview(states);
  if (!agents || !quick || !natural(quick.open) || !natural(quick.active) ||
      quick.active > quick.open) return null;
  const quickParked = quick.parked ?? 0;
  const quickAnswered = quick.answered ?? 0;
  const quickWaitingSend = quick.waitingSend ?? 0;
  const quickWaiting = quick.waiting ?? quick.open - quick.active - quickParked -
    quickAnswered - quickWaitingSend;
  const quickOpened = quick.opened ?? 0;
  if (![quickParked, quickAnswered, quickWaitingSend, quickWaiting, quickOpened].every(natural) ||
      quick.active + quickParked + quickAnswered + quickWaitingSend + quickWaiting !== quick.open ||
      quickOpened > quick.open) return null;
  const total = agents.active + agents.waiting + agents.inactive + quick.open;
  const working = agents.active + quick.active;
  if (!Number.isSafeInteger(total) || !Number.isSafeInteger(working)) return null;
  return { total, working, active: agents.active, mcp: agents.mcp,
    generating: agents.generating, waiting: agents.waiting,
    inactive: agents.inactive, quick: quick.open, quickActive: quick.active,
    quickIdle: quickWaiting, quickParked, quickAnswered, quickWaitingSend, quickWaiting, quickOpened };
}

export function badgeAppearance(states, quick, attention = 0, stale = false) {
  const counts = stale ? null : fleetOverview(states, quick);
  const alert = natural(attention) && attention > 0;
  return { text: counts ? String(counts.working) : "?",
    color: alert ? "#c62828" : counts?.working > 0 ? "#2e7d32" : "#757575",
    title: (counts
      ? `Работают сейчас: ${counts.working} = обычные ${counts.active} (MCP ${counts.mcp}, генерация ${counts.generating}) + быстрые ${counts.quickActive}; запаркованы ${counts.quickParked}; ответили, ждут владельца ${counts.quickAnswered}; ждут отправки ${counts.quickWaitingSend}; открыто вкладок ${counts.quickOpened}; ждут ${counts.waiting}; не подтверждены ${counts.inactive}`
      : "Агенты: нет свежих данных") + (alert ? ` · требуется внимание: ${attention}` : "") };
}

// Связь с сервером флота (20.11.2026, №116 п. 3). По журналу расширения за 17:16–18:31 UTC
// все 25 «флот недоступен» — тайм-аут fleet_list 15 с на перегруженном хосте, и одна серия из
// трёх подряд уже давала «!». Значок держит последнее хорошее значение до FLEET_STALE_MS;
// тревога «нет связи» — только при устойчивой потере связи; подсказка говорит, что случилось.
export const FLEET_LIST_TIMEOUT_MS = 30_000;
export const FLEET_STALE_MS = 600_000;
export const OFFLINE_ALERT_FAILURES = 3;
export const OFFLINE_ALERT_MS = 300_000;

export function classifyFleetError(message) {
  const text = String(message || "");
  if (/таймаут|timeout|timed out/i.test(text)) return { kind: "перегрузка",
    hint: "сервер отвечает дольше 30 с (обычно не хватает RAM хоста); повтор каждые 30 с — делать ничего не нужно, если это не дольше 10 мин" };
  if (/Failed to fetch|NetworkError|ECONNREFUSED|ERR_CONNECTION|HTTP 50[234]/i.test(text)) return { kind: "перезапуск",
    hint: "dev-mcp не принимает соединения: перезапускается (обычно до 1 мин) или остановлен; дольше 5 мин — restart.cmd на хосте" };
  if (/ответил ошибкой|HTTP \d{3}|ошибка MCP/i.test(text)) return { kind: "ошибка сервера",
    hint: "сервер ответил ошибкой — подробности в dev-mcp\\logs\\audit.jsonl (fleet_list)" };
  return { kind: "другое", hint: "подробности — в журнале расширения" };
}

export function nextConnection(prev, ok, now = Date.now(), error = "") {
  const p = prev && typeof prev === "object" ? prev : {};
  if (ok) return { failures: 0, lastOkAt: now, lastError: String(p.lastError || ""),
    lastErrorAt: Number(p.lastErrorAt) || 0, kind: "", hint: "" };
  const c = classifyFleetError(error);
  const failures = natural(p.failures) ? p.failures + 1 : 1;
  return { failures, lastOkAt: Number(p.lastOkAt) || 0, lastError: String(error || "").slice(0, 200),
    lastErrorAt: now, since: failures > 1 && Number(p.since) ? Number(p.since) : now,
    kind: c.kind, hint: c.hint };
}

export function offlineAlertDue(conn, now = Date.now()) {
  if (!conn || !natural(conn.failures) || conn.failures < OFFLINE_ALERT_FAILURES) return false;
  const since = Number(conn.lastOkAt) || Number(conn.since) || now; // без ответа с последнего успеха
  return now - since >= OFFLINE_ALERT_MS;
}

const hhmm = (ms) => new Date(ms).toISOString().slice(11, 16) + " UTC";

export function connectionNote(conn) {
  if (!conn || !natural(conn.failures) || conn.failures < 1) return "";
  const ok = conn.lastOkAt ? `последний ответ сервера ${hhmm(conn.lastOkAt)}` : "успешных ответов ещё не было";
  return ` · связь: неудач подряд ${conn.failures} (${conn.kind}), ${ok}; ${conn.hint}`;
}

// Значок с памятью: свежие данные → обычный вид; нет свежих, но последние хорошие моложе
// FLEET_STALE_MS → их число с пометкой времени; «?» — только когда данных нет дольше.
export function badgeWithMemory(states, quick, attention = 0, { at = 0, now = Date.now(),
  lastGood = null, conn = null } = {}) {
  const fresh = Boolean(at) && now - at <= FLEET_STALE_MS;
  let badge = badgeAppearance(states, quick, attention, !fresh);
  let good = null;
  if (fresh && badge.text !== "?") good = { states, quick, at };
  else if (lastGood && Number.isFinite(lastGood.at) && now - lastGood.at <= FLEET_STALE_MS) {
    const old = badgeAppearance(lastGood.states, lastGood.quick, attention, false);
    if (old.text !== "?") badge = { ...old, title: `данные на ${hhmm(lastGood.at)} — ${old.title}` };
  }
  if (badge.text === "?" && !connectionNote(conn)) badge = { ...badge, title: `${badge.title} больше 10 мин` };
  return { badge: { ...badge, title: badge.title + connectionNote(conn) }, good };
}

// Returns SVG paths on a fixed 24-hour axis; missing minutes are left as gaps,
// never interpolated as alleged measurements.
export function activityPaths(rows, now = Date.now()) {
  if (!Array.isArray(rows)) return { regular: "", quick: "", max: 1, count: 0 };
  const valid = rows.filter((r) => Number.isFinite(r?.ts) &&
    Number.isSafeInteger(r.working_regular) && r.working_regular >= 0 &&
    Number.isSafeInteger(r.working_quick) && r.working_quick >= 0 &&
    r.ts * 1000 >= now - 86_400_000 && r.ts * 1000 <= now);
  const max = Math.max(1, ...valid.map((r) => r.working_regular + r.working_quick));
  const path = (key) => valid.map((r, i) => {
    const x = 2 + 296 * (1 - (now - r.ts * 1000) / 86_400_000);
    const y = 55 - 50 * (key === "quick"
      ? (r.working_regular + r.working_quick) / max : r.working_regular / max);
    const gap = i > 0 && r.ts - valid[i - 1].ts > 120;
    return `${i === 0 || gap ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
  return { regular: path("regular"), quick: path("quick"), max, count: valid.length };
}

export function drawActivityChart(svg, rows, now = Date.now()) {
  const result = activityPaths(rows, now);
  const ns = "http://www.w3.org/2000/svg";
  const node = (name, attributes, text = "") => {
    const el = document.createElementNS(ns, name);
    for (const [k, v] of Object.entries(attributes)) el.setAttribute(k, v);
    el.textContent = text;
    return el;
  };
  const children = [
    node("path", { d: "M2 55H298", stroke: "#8a8a8a", fill: "none" }),
    ...[0, 6, 12, 18, 24].map((hour) => node("text",
      { x: 2 + 296 * hour / 24, y: 68, "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle",
        fill: "currentColor", "font-size": 8 }, hour === 24 ? "сейчас" : `−${24 - hour}ч`)),
    node("path", { d: result.quick, stroke: "#d28a30", "stroke-width": 1.7, "stroke-linecap": "round", fill: "none" }),
    node("path", { d: result.regular, stroke: "#2980b9", "stroke-width": 1.7, "stroke-linecap": "round", fill: "none" }),
  ];
  svg.replaceChildren(...children);
  return result;
}

// An explicit delivery order can coexist with a parked job while MCP and
// browser acknowledgements cross. Old servers may omit the open flag.
export function quickNeedsInterface(order) {
  return !order.close && order.open !== false && order.idle !== true &&
    (order.status !== "parked" || order.open === true);
}

// Возвращает основание закрытия только для вкладки, происхождение которой подтверждено.
// Чужая вкладка владельца, даже с тем же URL, без метки/ownTabs неприкосновенна.
export function specialTabClose({ label, owned, chatId, expectedChatId, page, watched, overseerKnown, quickOrders, quickKnown }) {
  if (!owned || (expectedChatId && chatId && expectedChatId !== chatId)) return "";
  if (label === "owner-sv") {
    if (page?.generating || (page && !page.composerEmpty && !page.composerOwned)) return "";
    if (!overseerKnown || !chatId || watched.has(chatId)) return "";
    return "надзор за чатом отключён";
  }
  if (!/^pult-q\d+(?:-[0-9a-z]+)?$/i.test(label || "") || !quickKnown) return "";
  const order = quickOrders.find((j) => j.label === label);
  return !order || order.close ? "быстрый агент закрыт" :
    !quickNeedsInterface(order)
      ? "быстрый агент работает через MCP без вкладки" : "";
}

export function tabHealth(rows, workers, excluded = new Set()) {
  const byLabel = new Map((workers || []).map((w) => [w.label, w]));
  let idle = 0, dead = 0;
  for (const r of rows || []) {
    if (excluded.has(r.id) || !r.own || !isFleetLabel(r.label)) continue;
    const worker = byLabel.get(r.label);
    if (!r.page || r.page.pageLoadFailed || r.page.accountLoadFailed) { dead += 1; continue; }
    if (!r.page.generating && !r.page.blocked && !r.page.pendingJoin &&
        Number(r.page.idleSec) >= 120 && (!worker || Number(worker.silenceMin) >= 2)) idle += 1;
  }
  return { idle, dead };
}

function isFleetLabel(label) {
  return /^f\d{4}-\d{4}[a-z0-9]{3}$/.test(String(label || ""));
}

// У активного захвата может умереть именно интерфейс: после 6 минут полной тишины
// освобождаем только вкладку-клавиатуру. Claim остаётся серверу и его wake/recovery.
export function silentClaimTabClose({ state, worker, owned, hasChat, wakePending }, thresholdSec = 360) {
  if (!owned || !hasChat || !worker?.task || !state || wakePending) return false;
  if (state.generating || state.blocked || state.blockerWaiting || state.rateLimited ||
      state.failed || state.pageLoadFailed || state.interrupted || state.pendingJoin ||
      state.unlockedFor > 0 || state.composerEmpty !== true) return false;
  return Number.isFinite(state.idleSec) && state.idleSec >= thresholdSec &&
    Number.isFinite(worker.silenceMin) && worker.silenceMin * 60 >= thresholdSec;
}

