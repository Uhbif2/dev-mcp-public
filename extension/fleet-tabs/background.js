// Фон расширения: раз в 30 секунд — флот, вкладки, решения, действия.
//
// MV3 service worker живёт секунды, поэтому ритм держит chrome.alarms, а вся
// память между тиками лежит в chrome.storage (память вкладок, запуски, лог,
// статус). Без ответа флота ничего не делаем: продолжать или открывать вкладки
// вслепую — значит работать поверх уже сданного.

import { DEFAULT_MCP_URL, callTool, chatLines, expectProject, fleetState, reportExtension } from "./fleet.js";
import { overseerTabPlan } from "./overseer-tabs.js";
import { deliverOwnerNotices, installOwnerNoticeClicks, recordServerAvailability } from "./owner-notices.js";
import { interfacePlan, INTERFACE_DEFAULTS } from "./interface-pressure.js";
import {
  DEFAULTS,
  OVERSEER_SENDS_PER_HOUR,
  overseerSendBudget,
  LAUNCH_DEFAULTS,
  RELOAD_DEFAULTS,
  clearLegacyCooldown,
  includeNewProjects,
  migrateSettings,
  pruneLaunched,
  recordStartedChat,
  hungText,
  closePlan,
  keyboardPlan,
  KEYBOARD_DEFAULTS,
  continueText,
  decide,
  decideLaunch,
  isAutostartLabel,
  reloadPlan,
} from "./policy.js";
import { recordEvent, observeWarning, summarizeEvents } from "./telemetry.js";
import { researchTick } from "./research-background.js";
import { INSTALL_DEFAULTS, localSettings } from "./config.js";
import { badgeAppearance, badgeWithMemory, nextConnection, offlineAlertDue, quickDiscardEligible, quickOverview, silentClaimTabClose, specialTabClose, quickNeedsInterface, tabHealth } from "./fleet-ui-state.js";
import { handoffDue, handoffMarker, handoffSent } from "./chat-handoff-policy.js";
import { memoryGuardPlan, liveAgentTabs, overseerRevisionPlan } from "./memory-guard.js";
import { dispatchScopePlan } from "./dispatch-scope.js";
import { limitReloadPlan } from "./limit-reload.js";
import { buildSessionExport, conversationIdOf, readConversationInPage } from "./session-export.js";
import {
  FULL_HANDOFF_LABEL, continuationMarker, continuationText, fullHandoffStage, newFullHandoff,
  noteFailure, projectStartUrl, pruneFullHandoffs, transcriptFilename, transcriptMarkdown,
} from "./full-handoff.js";
import { LIVENESS_PROGRESS_EVENTS, blockedSummary, livenessPlan } from "./liveness.js";
import {
  RECOVERY_DEFAULTS,
  accountOverloaded,
  accountRecovered,
  conversationApiCheck,
  conversation429Backoff,
  conversation429Unavailable,
  classifyPageIssue,
  placeCheck,
  accountActionWait,
  pacedAccountActionWait,
  attemptedRecovery,
  chatUrl,
  legacySession,
  markAccountAction,
  observeSession,
  projectPage,
  rememberSession,
  ACCOUNT_ACTIONS_PER_HOUR,
} from "./recovery.js";

// QUICK_SAFETY_START
// Pure guards for quick-agent tab ownership and message delivery evidence.
function quickTabsToClose(tabs, job, {
  ownTabs = {}, labelsByTab = {}, tabJobs = {}, closed = new Set(), chatPath, marks = {},
} = {}) {
  const expectedChat = chatPath(job.url || "");
  return tabs.filter((tab) => {
    const key = String(tab.id);
    // labels.byTab alone is not proof: it can be inferred from labels.byChat on an owner tab.
    // 17.11: метка агента в первом сообщении чата + тот же чат — доказательство и после
    // перезапуска Chrome (иначе вкладка закрытого q8 висела «владельца» навсегда).
    const marked = Boolean(expectedChat) && chatPath(tab.url || "") === expectedChat &&
      String(marks[key] || "").toUpperCase() === String(job.id || "").toUpperCase();
    const owned = ownTabs[key] === job.label || marked;
    const assigned = tabJobs[key] === job.id && labelsByTab[key]?.label === job.label;
    if (closed.has(tab.id) || (!owned && !assigned)) return false;
    if (tabJobs[key] && tabJobs[key] !== job.id) return false;
    // A URL match can verify an owned tab, never grant ownership to an unmarked tab.
    const actualChat = chatPath(tab.url || "");
    return !expectedChat || !actualChat || actualChat === expectedChat;
  });
}

function quickSendFingerprint(text) {
  // Lightweight command identity for storage; no raw command text is persisted.
  let hash = 0xcbf29ce484222325n;
  const value = String(text || "");
  for (const char of value) {
    hash ^= BigInt(char.codePointAt(0));
    hash = BigInt.asUintN(64, hash * 0x100000001b3n);
  }
  return `${value.length}:${hash.toString(16).padStart(16, "0")}`;
}

function quickSendBaseline(transcript, fingerprint) {
  if (!transcript?.ok || !Number.isSafeInteger(transcript.nMessages) ||
      transcript.nMessages < 0 || !Array.isArray(transcript.userMessages)) return null;
  const counted = Number.isSafeInteger(transcript.userCount) && transcript.userCount >= transcript.userMessages.length;
  return { fingerprint, messageCount: transcript.nMessages, userCount: counted ? transcript.userCount : transcript.userMessages.length,
    ...(counted ? { fullCount: true } : {}) };
}

function normalize(text) {
  return String(text || "").replace(/[\u200B\uFEFF]/g, "").replace(/\s+/g, " ").trim()
    .replace(/^@Dev['’]s Computer dev-mcp\+\s*/i, "").trim();
}

function confirmedQuickSend(transcript, text, baseline, fingerprint = quickSendFingerprint(text)) {
  if (!baseline || baseline.fingerprint !== fingerprint || !transcript?.ok ||
      !Number.isSafeInteger(transcript.nMessages) ||
      transcript.nMessages <= baseline.messageCount ||
      !Array.isArray(transcript.userMessages) ||
      !Number.isSafeInteger(baseline.userCount) || baseline.userCount < 0) return false;
  if (Number.isSafeInteger(transcript.matchingUserIndex) && Number.isSafeInteger(transcript.userCount) &&
      (baseline.fullCount || baseline.userCount < 80) && transcript.matchingUserIndex >= baseline.userCount &&
      transcript.matchingUserIndex < transcript.userCount) return true;
  if (transcript.userMessages.length <= baseline.userCount) return false;
  const expected = normalize(text).slice(0, 140);
  return Boolean(expected) && transcript.userMessages.slice(baseline.userCount)
    .some((userText) => normalize(userText).includes(expected));
}
function confirmedQuickStartTags(jobs) {
  // A URL alone is NOT delivery evidence: the server can learn it prior to sending.
  // "new" must never make another agent's live draft disposable.
  const delivered = new Set(["working", "parked", "answered", "answered-unparked"]);
  return [...new Set((jobs || []).filter((j) => chatPath(j.url || "") &&
      (delivered.has(j.status) || j.close === true))
    .map((j) => String(j.agentTag || (j.close === true ? j.id : "")).toUpperCase())
    .filter((tag) => /^Q\d+(?:-[A-Z0-9]+)?(?:\.[2-9]\d*)?$/.test(tag)))];
}
// QUICK_SAFETY_END

// Закрыть вкладку, не закрывая браузер (07.10.2026). Вкладка-клавиатура живёт ~20 с; когда она
// была последней в окне, chrome.tabs.remove закрывал окно, а с единственным окном — весь Chrome:
// следующая вкладка ждала холодного старта браузера. Последней вкладке сначала даём пустую соседку.
async function closeTabKeepBrowser(tab) {
  try {
    if (typeof tab.windowId === "number") {
      const siblings = await chrome.tabs.query({ windowId: tab.windowId });
      if (siblings.filter((t) => t.id !== tab.id).length === 0) {
        await chrome.tabs.create({ windowId: tab.windowId, url: "about:blank", active: false });
      }
    }
  } catch {
    // не смогли подстраховать окно — всё равно закрываем вкладку, как раньше
  }
  await chrome.tabs.remove(tab.id).catch(() => {});
}

const LOG_LIMIT = 200;
const SETTINGS = {
  // Намерения владельца — в окне расширения.
  enabled: INSTALL_DEFAULTS.enabled,
  projects: LAUNCH_DEFAULTS.projects,
  maxWorking: LAUNCH_DEFAULTS.maxWorking,
   // Служебное — свёрнуто в окне.
  dryRun: false,
  lockTabs: true,
  // Адреса — не ручки окна.
  mcpUrl: DEFAULT_MCP_URL,
  projectUrl: INSTALL_DEFAULTS.projectUrl,
  connector: INSTALL_DEFAULTS.connector,
  paceVersion: 10,
};

// Настройки окна → параметры decide и reloadPlan.
function reactConfig() {
  // Отточенные параметры (TUNED_KEYS в policy.js): значения — из кода, не из окна.
  return { decide: { ...DEFAULTS }, reload: { ...RELOAD_DEFAULTS } };
}

// Сообщение во вкладку с потолком ожидания: замороженная браузером вкладка не отвечает
// вовсе, и без потолка один её probe вешал бы весь тик — а с ним и остальные вкладки.
async function withTimeout(operation, ms, detail) {
  let timer;
  try {
    return await Promise.race([
      operation,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(detail)), ms); }),
    ]);
  } finally { clearTimeout(timer); }
}

async function sendWithTimeout(tabId, message, ms = 25000, backgroundOnly = false) {
  // 17.11: окно флота запускается свёрнутым (--start-minimized); в свёрнутом окне документ
  // hidden даже у активной вкладки, ChatGPT не дорисовывает composer, и текст «висит» в поле
  // без нажатия Send, пока владелец не откроет вкладку руками. Отправка (act) поэтому идёт
  // на видимой вкладке: окно разворачивается без фокуса, вкладка активируется, после
  // ответа content-script всё возвращается как было. Отправки — строго по одной.
  if (message?.type === "act") return backgroundSend(tabId, () => withTimeout(
    chrome.tabs.sendMessage(tabId, message), ms, `вкладка не ответила за ${Math.round(ms / 1000)} с`), backgroundOnly);
  return withTimeout(chrome.tabs.sendMessage(tabId, message), ms,
    `вкладка не ответила за ${Math.round(ms / 1000)} с`);
}

// Проверка только кода и Retry-After, без чтения содержимого разговора.
// accountControl=true проверяет отдельный endpoint, а не тот же перегретый ID.
async function conversationApiStatus(tabId, conv, accountControl = false) {
  try {
    const [res] = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, world: "MAIN",
      args: [conv, accountControl], func: async (id, control) => {
        const signal = AbortSignal.timeout(8000);
        const s = await fetch("/api/auth/session", { signal }).then((r) => r.json()).catch(() => ({}));
        if (!s.accessToken) return null;
        const endpoint = control ? "/backend-api/conversations?limit=1" : "/backend-api/conversation/" + id;
        const r = await fetch(endpoint, { signal, headers: { authorization: "Bearer " + s.accessToken } });
        const header = r.headers.get("retry-after");
        const seconds = header == null ? 0 : Number(header);
        const retryAfterSec = Number.isFinite(seconds) && seconds >= 0 ? seconds :
          Math.max(0, (Date.parse(header) - Date.now()) / 1000) || 0;
        return { status: r.status, retryAfterSec };
      } }), 10000, "проверка API разговора не ответила");
    return Number.isInteger(res?.result?.status) ? res.result : null;
  } catch { return null; }
}

// 20.11.2026 (№116 п. 4): полный JSON беседы (тот объект, что сайт грузит для чата) — для
// попапа и для передачи сессии по факту конца (п. 5). Чтение — в main-world вкладки.
async function exportSession(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const id = conversationIdOf(tab?.url);
  if (!id) throw new Error("во вкладке не открыт чат (адрес /c/<id>)");
  if (tab.discarded) throw new Error("вкладка выгружена — откройте её и повторите");
  const [res] = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, world: "MAIN",
    args: [id], func: readConversationInPage }), 70_000, "сайт не отдал беседу за 70 с");
  const page = res?.result;
  if (!page?.ok) throw new Error(`сайт ответил ${page?.status ?? "?"}: ${page?.detail || "нет ответа"}`);
  const built = buildSessionExport(page.text, { url: tab.url, id,
    version: chrome.runtime?.getManifest?.()?.version || "" });
  const s = built.meta.stats;
  await log(`экспорт сессии ${id.slice(0, 8)}: узлов ${s.nodes}, сообщений ${s.messages}, ` +
    `в текущей ветке ${s.currentBranchMessages}, ветвлений ${s.branches}, ${Math.round(built.meta.bytes / 1024)} КБ`);
  return built;
}

// 21.11.2026 (владелец): чат под надзором упёрся в «maximum length» — экспорт беседы, новый чат
// в том же проекте на месте вкладки, файлы экспорта + «погрузись и продолжи», надзор — на преемника.
// Экспорт держится в памяти SW; после перезапуска SW он повторяется из старой вкладки.
const fullHandoffExports = new Map();

async function fullHandoffFiles(rec) {
  const cached = fullHandoffExports.get(rec.oldId);
  if (cached) return cached;
  const tab = await chrome.tabs.get(rec.tabId).catch(() => null);
  if (!tab || chatPath(tab.url) !== rec.oldId) throw new Error("старая вкладка закрыта — экспорт повторить нельзя");
  const built = await exportSession(rec.tabId);
  const conversation = JSON.parse(built.text).conversation;
  rec.title = rec.title || built.meta.title;
  const files = [
    { name: built.filename, type: "application/json", text: built.text },
    { name: transcriptFilename(built.filename), type: "text/markdown",
      text: transcriptMarkdown(conversation, { title: rec.title, url: rec.url }) },
  ];
  rec.files = files.map((f) => ({ name: f.name, bytes: f.text.length }));
  fullHandoffExports.set(rec.oldId, files);
  return files;
}

async function fullHandoffStep(rec, ctx) {
  const { cfg, ownTabs, labels, overseen, live, launchesAllowed, reserveAccountAction } = ctx;
  const stage = fullHandoffStage(rec, { draftOnly: cfg.fullHandoffDraftOnly === true });
  const key = rec.oldId.slice(0, 8);
  if (stage === "export") {
    await fullHandoffFiles(rec);
    rec.exportedAt = Date.now();
    await log(`передача ${key}: экспорт готов — ` +
      rec.files.map((f) => `${f.name} ${Math.round(f.bytes / 1024)} КБ`).join(", "));
    return true;
  }
  if (stage === "open") {
    if (!launchesAllowed || cfg.dryRun) return false;
    await reserveAccountAction("handoff-open", null, FULL_HANDOFF_LABEL);
    const url = projectStartUrl(rec.url);
    const tab = await chrome.tabs.create({ url, active: false, index: Number(rec.index || 0) + 1,
      ...(rec.windowId ? { windowId: rec.windowId } : {}) });
    live.add(String(tab.id));
    rec.newTabId = tab.id;
    ownTabs[String(tab.id)] = FULL_HANDOFF_LABEL;
    labels.byTab[String(tab.id)] = { label: FULL_HANDOFF_LABEL, path: null };
    chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
    await chrome.storage.local.set({ ownTabs, labels });
    await log(`передача ${key}: открыл новый чат ${url} во вкладке ${tab.id}`);
    return true;
  }
  const newTab = rec.newTabId ? await chrome.tabs.get(rec.newTabId).catch(() => null) : null;
  if (!newTab && !["close-old", "finish"].includes(stage)) {
    for (const field of ["newTabId", "attachedAt", "sentAt", "successor", "draft"]) delete rec[field];
    throw new Error("вкладку преемника закрыли — открою заново");
  }
  if (stage === "attach" || stage === "send" || stage === "draft") {
    const page = await probe(rec.newTabId).catch(() => null);
    if (!page?.composerPresent || page.generating) return false; // страница ещё грузится
    if (page.lastRole === "user" || chatPath(page.url)) {
      const transcript = await sendWithTimeout(rec.newTabId, { type: "transcript" }, 8000).catch(() => null);
      if ((transcript?.userMessages || []).some((u) => String(u).includes(continuationMarker(rec.oldId)))) {
        rec.attachedAt ||= Date.now();
        rec.sentAt ||= Date.now();
        return true;
      }
      if (page.lastRole === "user") throw new Error("в новом чате чужое сообщение — повтор запрещён");
    }
    if (stage === "attach") {
      const files = await fullHandoffFiles(rec);
      const out = await sendWithTimeout(rec.newTabId, { type: "act", action: "attach-files", files,
        method: (rec.attempts || 0) % 2 ? "paste" : "input" }, 150_000).catch((error) => ({ ok: false, detail: String(error) }));
      if (!out?.ok) throw new Error(out?.detail || "файлы не прикрепились");
      rec.attachedAt = Date.now();
      await log(`передача ${key}: файлы прикреплены — ${out.detail || ""}`);
      return true;
    }
    const text = continuationText({ title: rec.title, oldUrl: rec.url, oldId: rec.oldId,
      files: (rec.files || []).map((f) => f.name) });
    const out = await sendWithTimeout(rec.newTabId, { type: "act", action: stage === "draft" ? "draft" : "continue", text },
      60_000).catch((error) => ({ ok: false, detail: String(error) }));
    if (!out?.ok) throw new Error(out?.detail || "сообщение не ушло");
    rec.sentAt = Date.now();
    if (stage === "draft") rec.draft = true;
    await log(`передача ${key}: ` + (stage === "draft"
      ? "файлы и текст вписаны, Send не нажат (режим «вписать, не отправлять»)" : "сообщение отправлено"));
    return true;
  }
  if (stage === "await-successor") {
    const page = await probe(rec.newTabId).catch(() => null);
    const successor = chatPath(page?.url || newTab?.url);
    if (!successor || successor === rec.oldId) return false;
    rec.successor = successor;
    return true;
  }
  if (stage === "mark") {
    const url = `https://chatgpt.com/c/${rec.successor}`;
    const mode = rec.mode || "send";
    try {
      await callTool(cfg.mcpUrl, "fleet_overseer_mark", { url, mode, engine: rec.engine || "proxy" }, { timeoutMs: 10000 });
    } catch (error) {
      if (!/engine|unexpected|argument/i.test(String(error))) throw error;
      await callTool(cfg.mcpUrl, "fleet_overseer_mark", { url, mode }, { timeoutMs: 10000 });
    }
    await callTool(cfg.mcpUrl, "fleet_overseer_mark", { url: rec.url, mode: "off" }, { timeoutMs: 10000 }).catch(() => {});
    overseerList.at = 0;
    overseen.set(rec.successor, { chat: rec.successor, url, mode, engine: rec.engine || "proxy" });
    ownTabs[String(rec.newTabId)] = "owner-sv";
    labels.byTab[String(rec.newTabId)] = { label: "owner-sv", path: rec.successor };
    labels.byChat[rec.successor] = "owner-sv";
    await chrome.storage.local.set({ ownTabs, labels });
    rec.markedAt = Date.now();
    await log(`передача ${key}: надзор (${mode}) перенесён на чат ${rec.successor.slice(0, 8)}`);
    return true;
  }
  if (stage === "close-old") {
    const old = await chrome.tabs.get(rec.tabId).catch(() => null);
    if (old && chatPath(old.url) === rec.oldId) {
      if (old.active && newTab) await chrome.tabs.update(newTab.id, { active: true }).catch(() => {});
      await chrome.tabs.remove(old.id).catch(() => {});
    }
    delete ownTabs[String(rec.tabId)];
    delete labels.byTab[String(rec.tabId)];
    await chrome.storage.local.set({ ownTabs, labels });
    rec.closedAt = Date.now();
    await log(`передача ${key}: старая вкладка закрыта, работа продолжается в ${rec.successor.slice(0, 8)}`);
    return true;
  }
  if (stage === "finish") {
    rec.done = true;
    rec.finishedAt = Date.now();
    fullHandoffExports.delete(rec.oldId);
    return true;
  }
  return false;
}

// A discarded owner-sv tab has no renderer. Poll only conversation metadata
// through ANOTHER extension-owned, loaded ChatGPT tab; never wake a human tab.
// Return only update_time, not the transcript (which remains on the server).
async function overseerConversationUpdateTime(tabId, conv) {
  if (!/^[0-9a-f-]{8,}$/i.test(conv)) return null;
  try {
    const [res] = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, world: "MAIN",
      args: [conv], func: async (id) => {
        const session = await fetch("/api/auth/session").then((r) => r.json()).catch(() => ({}));
        if (!session.accessToken) return null;
        const response = await fetch("/backend-api/conversation/" + id, {
          headers: { authorization: "Bearer " + session.accessToken },
        });
        if (!response.ok) return null;
        const update = (await response.json()).update_time;
        return (typeof update === "string" || typeof update === "number") ? String(update) : null;
      } }), 12_000, "метаданные надзорного чата не ответили");
    const value = res?.result;
    return typeof value === "string" && value.length <= 100 && value !== "0" ? value : null;
  } catch { return null; }
}

// 18.11: отправка БЕЗ переднего плана. Вывод вкладки вперёд не помогал: окно разворачивалось
// без фокуса, оставалось перекрытым, Chrome держал страницу hidden («страница НЕ стала
// видимой»), вкладка мигала у владельца на секунду, а Send так и не нажимался. Эмуляция фокуса
// через DevTools-протокол делает страницу visible+focused с идущим requestAnimationFrame
// (проверено живьём tools/cdp-diag/bgrender.mjs: hidden → visible, rAF 0 → 5–6/с), не трогая
// окна и фокус владельца — и отправки в разные вкладки идут параллельно, без общей очереди.
// Не вышло подключиться (нет разрешения, вкладка занята) — прежний путь через передний план.
const DEBUGGER_PROTOCOL = "1.3";
async function backgroundSend(tabId, operation, backgroundOnly = false) {
  if (!chrome.debugger?.attach) return backgroundOnly ? operation() : foregroundSend(tabId, operation);
  const target = { tabId };
  let attached = false;
  try {
    await withTimeout(chrome.debugger.attach(target, DEBUGGER_PROTOCOL), 3000, "debugger.attach не ответил");
    attached = true;
    // Сначала разморозить (обрабатывает браузер, не спящий рендерер), потом эмулировать фокус:
    // замороженная Chrome вкладка не отвечала на эмуляцию за 3 с (живьём 18.11 06:12).
    await withTimeout(chrome.debugger.sendCommand(target, "Page.setWebLifecycleState",
      { state: "active" }), 5000, "lifecycle не ответил").catch(() => {});
    // Свежая вкладка ещё грузится и может ответить позже: эмуляция всё равно применится, когда
    // рендерер освободится. Передний план тут не помогает (окно перекрыто) — продолжаем в фоне.
    await withTimeout(chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled",
      { enabled: true }), 8000, "эмуляция фокуса не ответила")
      .catch((error) => log(`вкладка ${tabId}: ${error.message} — отправляю в фоне без подтверждения эмуляции`).catch(() => {}));
  } catch (error) {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
    if (backgroundOnly) return operation();
    await log(`фоновая отправка недоступна для вкладки ${tabId} (${String(error?.message || error).slice(0, 80)}) — передний план`).catch(() => {});
    return foregroundSend(tabId, operation);
  }
  try {
    await new Promise((resolve) => setTimeout(resolve, 300)); // дать React дорисовать composer
    const result = await withTimeout(operation(), FOREGROUND_SEND_CAP_MS,
      `фоновая отправка не завершилась за ${FOREGROUND_SEND_CAP_MS / 1000} с`);
    await new Promise((resolve) => setTimeout(resolve, FOREGROUND_SETTLE_MS));
    return result;
  } finally {
    await withTimeout(chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled",
      { enabled: false }), 2000, "").catch(() => {});
    await withTimeout(chrome.debugger.detach(target), 2000, "").catch(() => {});
  }
}

let foregroundChain = Promise.resolve();
const FOREGROUND_SETTLE_MS = 1500;
async function pageVisible(tabId) {
  // 18.11: зависший рендерер оставлял executeScript без ответа навсегда — цепочка отправок
  // и весь тик вставали (running=true), новые чаты не открывались, Send не нажимался.
  try {
    const [res] = await withTimeout(chrome.scripting.executeScript({ target: { tabId },
      func: () => document.visibilityState }), 1500, "страница не ответила на visibilityState");
    return res?.result === "visible";
  } catch (error) {
    // Нет доступа к странице — не блокировать отправку; не ответила — null (ждать бесполезно).
    return /не ответила/.test(String(error?.message || error)) ? null : true;
  }
}
// Потолок одной отправки на переднем плане: что бы ни зависло внутри (рендерер, API окна),
// очередь отправок продолжается, а тик освобождается.
const FOREGROUND_SEND_CAP_MS = 60_000;
function foregroundSend(tabId, operation) {
  const inner = foregroundChain.then(async () => {
    let tab = null, win = null, previousActive = null;
    try {
      tab = await chrome.tabs.get(tabId);
      win = tab && chrome.windows?.get ? await chrome.windows.get(tab.windowId) : null;
      [previousActive] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
    } catch { /* тестовая среда или вкладка исчезла — отправляем как раньше */ }
    const wasMinimized = win?.state === "minimized";
    // Вкладка уже на виду (или окружение без окон) — ничего не трогаем и не ждём.
    const raise = Boolean(win) && (wasMinimized || !tab.active);
    if (!raise) return operation();
    try {
      if (wasMinimized) await chrome.windows.update(win.id, { state: "normal", focused: false }).catch(() => {});
      if (!tab.active) await chrome.tabs.update(tabId, { active: true }).catch(() => {});
      let visible = false;
      for (let i = 0; i < 15 && !(visible = await pageVisible(tabId)); i += 1) {
        if (visible === null) break; // рендерер не отвечает — дальше решает таймаут самой отправки
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      await log(`отправка на переднем плане: вкладка ${tabId}${wasMinimized ? ", окно развёрнуто" : ""}; ` +
        `страница ${visible ? "видима" : "НЕ стала видимой"}`).catch?.(() => {});
      const result = await operation();
      // Дать React дорисовать и отправить ход до возврата в фон.
      await new Promise((resolve) => setTimeout(resolve, FOREGROUND_SETTLE_MS));
      return result;
    } finally {
      if (previousActive?.id && previousActive.id !== tabId)
        await chrome.tabs.update(previousActive.id, { active: true }).catch(() => {});
      if (wasMinimized) await chrome.windows.update(win.id, { state: "minimized" }).catch(() => {});
    }
  });
  const run = withTimeout(inner, FOREGROUND_SEND_CAP_MS,
    `отправка на переднем плане не завершилась за ${FOREGROUND_SEND_CAP_MS / 1000} с`);
  foregroundChain = run.catch(() => {});
  return run;
}

// Путь чата (/c/<id>) из адреса вкладки: по нему вспоминается метка, когда первое
// сообщение ушло из страницы.
// Надзиратель хода (пульт _todo/59): список чатов под надзором и память по чатам — в фоне.
const overseerMem = {};
// Последовательные резервы не допускают гонку read-modify-write между тиками.
// Записываем попытку ДО отправки: перезапуск worker после клика не сбросит бюджет.
let overseerSendWrite = Promise.resolve();
function reserveOverseerSend() {
  const request = overseerSendWrite.then(async () => {
    const { overseerSends } = await chrome.storage.local.get("overseerSends");
    const decision = overseerSendBudget(overseerSends, Date.now(), OVERSEER_SENDS_PER_HOUR);
    await chrome.storage.local.set({ overseerSends: decision.sends });
    return decision.allowed;
  });
  overseerSendWrite = request.then(() => {}, () => {});
  return request;
}
let overseerList = { at: 0, chats: new Map(), ok: false };
async function overseerChats(cfg, { force = false, strict = false } = {}) {
  if (!force && Date.now() - overseerList.at < 60_000) return overseerList.chats;
  try {
    const rows = JSON.parse(await callTool(cfg.mcpUrl, "fleet_overseer_chats", {}, { timeoutMs: 7000 }));
    if (!Array.isArray(rows)) throw new Error("invalid overseer chat list");
    overseerList = { at: Date.now(), chats: new Map(rows.map((r) => [chatPath(r.url), r]).filter(([id]) => id)), ok: true };
  } catch (error) {
    overseerList.at = Date.now() - 30_000;
    overseerList.ok = false;
    if (strict) throw error; // popup must not mistake read failure for mode=off
  }
  return overseerList.chats;
}

// Быстрые агенты (fleet/quick.py, 15.11): чат-субагент Claude Code или ведущего. Метка pult-q<N> —
// вкладку не запирают и не ведут как флотскую; её ведёт quickTab: первое сообщение, ответы, закрытие.
const quickMem = {};
let quickList = { at: 0, jobs: [], ok: false };
async function quickOrders(cfg) {
  if (Date.now() - quickList.at < 15_000) return quickList.jobs;
  try {
    const jobs = JSON.parse(await callTool(cfg.mcpUrl, "fleet_quick_orders", {}, { timeoutMs: 7000 }));
    if (!Array.isArray(jobs)) throw new Error("invalid quick orders");
    quickList = { at: Date.now(), jobs, ok: true };
    // Metadata-only cache for emergency RAM protection when dev-mcp is unavailable.
    // Never persist the send payload or conversation content.
    await chrome.storage.local.set({ fleetQuickOrderSummary: jobs.map((j) => ({
      id: j.id, label: j.label, url: j.url || "", open: j.open, idle: j.idle,
      status: j.status, send: Boolean(j.send), wake: Boolean(j.wake), pending: j.pending || 0,
    })) });
  } catch {
    quickList.at = Date.now() - 5_000;
    quickList.ok = false;
  }
  return quickList.jobs;
}
async function openedQuickLabels(tabs) {
  const { ownTabs = {} } = await chrome.storage.local.get("ownTabs");
  return tabs.filter((t) => !t.discarded && t.status !== "unloaded")
    .map((t) => ownTabs[String(t.id)] || "").filter(Boolean);
}
const QUICK_SEND_PAUSE_MS = 10 * 60_000;
async function quickReport(cfg, report) {
  quickList.at = 0; // следующее чтение — свежее: outbox мог сдвинуться
  return callTool(cfg.mcpUrl, "fleet_quick_report", { report: JSON.stringify(report) }, { timeoutMs: 15000 })
    .catch(() => null);
}

function chatPath(url) {
  const match = /\/c\/([0-9a-f-]{8,})/i.exec(String(url || ""));
  return match ? match[1] : "";
}

// Настройки окна → параметры decideLaunch. Непонятное значение — берётся умолчание.
function launchConfig(cfg) {
  const num = (value, fallback, min, max) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : fallback;
  };
  return {
    ...LAUNCH_DEFAULTS,
    projects: cfg.projects,
    maxWorking: num(cfg.maxWorking, LAUNCH_DEFAULTS.maxWorking, 1, 500),
    // 17.10: число вкладок — намерение владельца (окно), а не отточенный параметр.
    maxTabs: num(cfg.maxTabs, LAUNCH_DEFAULTS.maxTabs, 1, 32),
  };
}

// Первый подтверждённый чат после перегрузки завершает паузу и ставит
// событие восстановления в надёжную очередь отчётности (storage + retry).
function queueAccountRecovery(launch, at) {
  const overloadAt = (launch.overloads || []).at(-1);
  if (!launch.overloadLevel || !overloadAt || at < (launch.overloadUntil || 0)) return launch;
  return accountRecovered({ ...launch, accountEvents: [
    ...(launch.accountEvents || []), { type: "recovered", at, overload_at: overloadAt },
  ].slice(-72) });
}

async function settings() {
  const { settings: saved } = await chrome.storage.local.get("settings");
  const migrated = migrateSettings(saved || { paceVersion: SETTINGS.paceVersion });
  if (migrated !== saved) await chrome.storage.local.set({ settings: migrated });
  return { ...SETTINGS, ...await localSettings(), ...migrated };
}

async function log(line) {
  const { log: lines = [] } = await chrome.storage.local.get("log");
  lines.push(`${new Date().toISOString()} ${line}`);
  await chrome.storage.local.set({ log: lines.slice(-LOG_LIMIT) });
}

installOwnerNoticeClicks(async (report) => {
  const cfg = await settings();
  await reportExtension(cfg.mcpUrl,{detail:"owner_notice_state:"+JSON.stringify(report)});
});

function notify(message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon128.png",
    title: "Вкладки флота",
    message: message.slice(0, 250),
    priority: 1,
  });
}

// Блокировка вкладки агента: ввод только через расширение, вкладка закреплена в полосе
// (у закреплённой нет крестика, её не закрывают «Закрыть вкладки справа»). Вхолостую и
// при выключенном расширении вкладки не запираются: тогда ими управляет человек.
async function setTabLock(tab, on, label, pinned) {
  const reply = await sendWithTimeout(tab.id, { type: "lock", on, label }, 2000).catch(() => null);
  const key = String(tab.id);
  if (on && !tab.pinned) {
    await chrome.tabs.update(tab.id, { pinned: true }).catch(() => {});
    pinned[key] = true;
  } else if (!on && tab.pinned && pinned[key]) {
    await chrome.tabs.update(tab.id, { pinned: false }).catch(() => {});
    delete pinned[key];
  }
  return reply;
}

// 20.11.2026 (№116 п. 6): «A temporary limit is preventing this chat from loading» на любых
// вкладках ChatGPT (и не флотских) — перезагрузка с backoff 1→2→5→10 мин по полосе аккаунта.
async function limitReloadTick(tabs, cfg) {
  const saved = await chrome.storage.local.get(["limitReloadMem", "limitReloadExcluded", "accountActions"]);
  const enabled = cfg.limitReload !== false;
  const pages = new Map();
  if (enabled) {
    for (const tab of tabs) {
      if (tab.discarded) continue;
      pages.set(tab.id, await sendWithTimeout(tab.id, { type: "limit-probe" }, 3000).catch(() => null));
    }
  }
  const now = Date.now();
  let account = saved.accountActions || {};
  const plan = limitReloadPlan({ tabs, pages, mem: saved.limitReloadMem || {}, enabled,
    excluded: saved.limitReloadExcluded || {}, now,
    accountWaitMs: pacedAccountActionWait(account, now, RECOVERY_DEFAULTS) });
  for (const id of plan.reload) {
    const entry = plan.mem[String(id)] || {};
    const next = new Date(entry.nextAt || now).toISOString().slice(11, 16);
    if (cfg.dryRun) {
      await log(`вхолостую: вкладка ${id} — «A temporary limit…», перезагрузил бы (№${entry.attempts || 1})`);
      continue;
    }
    account = markAccountAction(account, now, "reload", id, "limit-reload");
    await chrome.storage.local.set({ accountActions: account });
    await chrome.tabs.reload(id).catch(() => {});
    await log(`вкладка ${id}: «A temporary limit…» — перезагрузка №${entry.attempts || 1}; следующая не раньше ${next} UTC`);
  }
  if (JSON.stringify(plan.mem) !== JSON.stringify(saved.limitReloadMem || {})) {
    await chrome.storage.local.set({ limitReloadMem: plan.mem });
  }
}

async function probe(tabId) {
  try {
    return await sendWithTimeout(tabId, { type: "probe" }, 5000);
  } catch {
    // После reload unpacked-extension старая isolated world может остаться в странице с
    // мёртвым runtime-port. Таймаут тоже повод реинжектить текущую версию, а не только
    // явная ошибка "Receiving end does not exist".
    try {
      // A renderer can also leave executeScript pending forever after startup.
      // Bound reinjection so the same tab can reach recovery and release this cycle.
      await withTimeout(chrome.scripting.executeScript({
        target: { tabId }, files: ["mode.js", "content.js"], injectImmediately: true,
      }), 5000, "content-script не внедрился за 5 с");
      return await sendWithTimeout(tabId, { type: "probe" }, 5000);
    } catch {
      return null;
    }
  }
}

async function taskModeForSend(cfg, worker) {
  // A task is claimed *after* the generic fleet_join startup. Resolve mode
  // before later task messages, directly from server-verified spec_sha.
  if (!worker?.task) return { ok: true, payload: {} };
  const slash = worker.task.lastIndexOf("/");
  if (slash < 1) return { ok: false, reason: "invalid worker task identity" };
  try {
    const raw = await callTool(cfg.mcpUrl, "fleet_task_body", {
      project: worker.task.slice(0, slash),
      tz: worker.task.slice(slash + 1),
      page: 1,
    }, { timeoutMs: 8000 });
    const meta = JSON.parse(raw);
    if (meta.mode_status === "implicit-default") return { ok: true, payload: {} };
    if (meta.mode_status !== "verified") {
      return { ok: false, reason: "task mode provenance " + String(meta.mode_status || "missing") };
    }
    if (meta.mode_contract !== "fleet-chat-mode/v1" ||
        !/^[a-f0-9]{64}$/.test(meta.mode_spec_sha || "")) {
      return { ok: false, reason: "task mode has no verified spec version" };
    }
    if (meta.mode_explicit || meta.outcome_stage === "понято") {
      if (!["default", "thinking", "deep-research"].includes(meta.mode)) {
        return { ok: false, reason: "task mode is invalid" };
      }
      return {
        ok: true, payload: {
          mode: meta.mode, outcomeStage: meta.outcome_stage || null,
          modeContract: meta.mode_contract, modeSpecSha: meta.mode_spec_sha,
        },
      };
    }
    return { ok: true, payload: {} }; // no explicit mode: preserve legacy UI
  } catch (error) {
    return { ok: false, reason: "task mode lookup failed: " + String(error).slice(0, 150) };
  }
}

function newLabel() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const rand = Math.random().toString(36).slice(2, 5);
  return `f${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${rand}`;
}

function autostartLabelAt(label, now = Date.now()) {
  const match = /^f(\d{2})(\d{2})-(\d{2})(\d{2})[a-z0-9]{3}$/.exec(String(label || ""));
  if (!match) return 0;
  const current = new Date(now);
  const parts = match.slice(1).map(Number);
  let at = new Date(current.getFullYear(), parts[0] - 1, parts[1], parts[2], parts[3]).getTime();
  // Метка не содержит год; в первые часы января декабрьская вкладка относится к прошлому году.
  if (at > now + 24 * 3600_000) {
    at = new Date(current.getFullYear() - 1, parts[0] - 1, parts[1], parts[2], parts[3]).getTime();
  }
  return Number.isFinite(at) ? at : 0;
}

// MCP may return plain "Error calling tool ..." instead of JSON on failure.
function handoffResponse(raw) {
  try {
    const value = JSON.parse(raw);
    if (value && !Array.isArray(value) && typeof value === "object" &&
        typeof value.action === "string") return value;
  } catch { /* textual MCP error */ }
  return { action: "error", reason: String(raw || "empty MCP response").slice(0, 180),
    retry_after_s: 600 };
}

function handoffBackoff(result) {
  const requested = Number(result?.retry_after_s);
  return Math.max(5 * 60_000, Math.min(60 * 60_000,
    Number.isFinite(requested) ? requested * 1000 : 10 * 60_000));
}

// Первое сообщение новой вкладки. Порядок работы агента — в инструкции проекта
// «Fleet Dev-mcp»; здесь только метка (по ней расширение узнаёт вкладку во флоте)
// и явный первый шаг на случай, если инструкцию проекта кто-то сотрёт.
function starter(label, reservation) {
  if (reservation?.worker_id) return (
    `Ты агент флота dev-mcp, label='${label}'. За тобой уже закреплена реальная задача ` +
    `${reservation.project}/${reservation.tz}, claim_id=${reservation.claim_id}. ` +
    `Вызови fleet_join(label='${label}', kind='chatgpt'), затем ` +
    `fleet_resume(agent='${reservation.worker_id}') и работай по полученной памятке. ` +
    `Передавай claim_id в сдаче и вердикте; в старой схеме — первой строкой note.`
  );
  return (
    `Ты агент флота dev-mcp, метка label='${label}'. Работай по инструкции проекта: ` +
    `fleet_join(label='${label}', kind='chatgpt'), затем fleet_take со своим worker_id ` +
    `и дальше строго по памятке и docs/FLEET-PROTOCOL.md.`
  );
}

async function openAgentTab(cfg, label, target, onCreated) {
  const tab = await chrome.tabs.create({ url: cfg.projectUrl, active: false });
  await onCreated(tab); // сохранить владение ДО ожиданий: MV3 может завершить service worker
  await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});

  // The normal tick owns renderer recovery and the first send. Repeated full probes
  // here used to occupy the only dispatcher for up to 135 seconds per new tab.
  const ready = await sendWithTimeout(tab.id, { type: "probe" }, 1500).catch(() => null);

  await log(`запуск ${label} под ${target}: вкладка создана, renderer ${ready ? "отвечает" : "не отвечает"}; первое сообщение поставлено в очередь`);
  return { label, tabId: tab.id, ok: true, pending: true, responsive: Boolean(ready) };
}

// Свободная память машины, МБ; null — API недоступно (тогда запуск не ограничивается).
async function freeMemoryMB() {
  try {
    const info = await chrome.system.memory.getInfo();
    return Math.round(info.availableCapacity / 1048576);
  } catch {
    return null;
  }
}

let running = false;
let nextDispatchAt = Infinity;
let browserControlPending = false;
let browserControlWrite = Promise.resolve();
// This evidence must survive a failed storage.local write in the running worker.
// The supervisor and popup can read it without asking the broken DB to log itself.
const runtimeHealth = globalThis.fleetRuntimeHealth = {
  bootAt: Date.now(), cycleStartedAt: 0, cycleFinishedAt: 0, error: "", storageError: "",
};

// UI recovery must remain available when the fleet's control API is slow/down.
// This path never sends messages or transfers claims using a cached snapshot.
async function recoverLocalInterfaces(tabs, cfg) {
  const saved=await chrome.storage.local.get(['ownTabs','labels','sessions','fleetWorkers',
    'interfaceQueue','reloadMem','accountActions','status','knownChats','launch']);
  const pages=await Promise.all(tabs.map(async t=>({id:t.id,
    page:t.discarded?null:await probe(t.id)})));
  const rows=pages.map(({id,page})=>{
    const tab=tabs.find(t=>t.id===id);
    const label=saved.ownTabs?.[id]||saved.labels?.byTab?.[id]?.label||page?.label||'';
    return {id,label,page,own:!!saved.ownTabs?.[id]||isAutostartLabel(label),
      url:saved.sessions?.[id]?.url||chatUrl(tab.url)?.url};
  });
  const plan=interfacePlan(rows,saved.fleetWorkers||[],saved.interfaceQueue||{},Date.now(),
    {...INTERFACE_DEFAULTS,maxTabs:launchConfig(cfg).maxTabs});
  if(cfg.dryRun) return;
  const knownChats=saved.knownChats||{};
  for(const row of rows.filter(r=>plan.close.includes(r.id))) {
    if(row.url&&row.label) knownChats[row.label]={url:row.url,at:Date.now()};
  }
  await chrome.storage.local.set({interfaceQueue:plan.queue,knownChats});
  for(const id of plan.close) {
    await sendWithTimeout(id,{type:'lock',on:false},2000).catch(()=>null);
    await closeTabKeepBrowser(tabs.find(t=>t.id===id));
  }
  const reloadMem=saved.reloadMem||{},status=saved.status||{};
  let account=saved.accountActions||{};
  const failed=rows.filter(r=>!plan.close.includes(r.id)&&r.own&&r.page?.pageLoadFailed&&
    !/^(owner|pult)/i.test(r.label)&&!(r.page.unlockedFor>0));
  failed.sort((a,b)=>(reloadMem[a.id]?.lastAt||0)-(reloadMem[b.id]?.lastAt||0));
  for(const row of failed.slice(0,INTERFACE_DEFAULTS.failedTabs)) {
    const now=Date.now(),old=reloadMem[row.id];
    const api429=saved.launch?.apiCheck?.[row.id];
    const wait429=api429?.conv===chatPath(row.url) ? conversation429Backoff(api429,now) : 0;
    if(wait429) {
      status[row.id]={label:row.label,action:'skip',reason:
        (conversation429Unavailable(api429,now)?'разговор недоступен (429)':'429 разговора')+
        ': следующая проверка не раньше '+new Date(api429.nextAt).toISOString()};
      continue;
    }
    const retry=reloadPlan({...row.page,label:row.label},old,[],now,RELOAD_DEFAULTS);
    const wait=accountActionWait(account,now,RECOVERY_DEFAULTS);
    if(retry.reload&&wait) continue;
    reloadMem[row.id]=retry.mem;
    status[row.id]={label:row.label,action:retry.reload?'reload':'skip',reason:'локальное восстановление: '+retry.reason};
    if(retry.reload) {
      account=markAccountAction(account,now,'reload',row.id,row.label);
      await chrome.storage.local.set({accountActions:account,reloadMem});
      await sendWithTimeout(row.id,{type:'lock',on:false},2000).catch(()=>null);
      await chrome.tabs.reload(row.id).catch(()=>{});
    }
  }
  await chrome.storage.local.set({reloadMem,status,lastLocalRecoveryAt:Date.now()});
}

// Число — все обычные и открытые быстрые агенты; тревога меняет цвет, не число.
// 20.11.2026: при сбое опроса держим последнее хорошее значение до 10 мин (badgeWithMemory),
// в подсказке — последняя ошибка, её вид и время последнего ответа сервера.
async function showFleetBadge(states, quick, attention = 0, at = Date.now()) {
  const saved = await chrome.storage.local.get(["fleetLastGood", "fleetConnection"]).catch(() => ({}));
  const { badge, good } = badgeWithMemory(states, quick, attention, { at, now: Date.now(),
    lastGood: saved?.fleetLastGood || null, conn: saved?.fleetConnection || null });
  if (good) await chrome.storage.local.set({ fleetLastGood: good }).catch(() => {});
  await Promise.all([
    chrome.action.setBadgeText({ text: badge.text }).catch(() => {}),
    chrome.action.setBadgeBackgroundColor({ color: badge.color }).catch(() => {}),
    chrome.action.setTitle?.({ title: badge.title })?.catch(() => {}),
  ]);
}

// Сторож цикла (18.11): если тик висит дольше потолка, следующий будильник начинает новый,
// а поздний finally старого не сбрасывает флаг нового (поколение).
const TICK_STALL_MS = 180_000;
async function sha256Hex(text) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(text))));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
let tickGeneration = 0;
const quickCloseAckAt = new Map(); // id задания → когда последний раз подтверждали закрытие
// Замер фаз цикла (18.11): время от начала тика до метки, видно в runtime-health.phases.
const phaseMark = (name) => {
  runtimeHealth.progressAt = Date.now();
  (runtimeHealth.phases ||= {})[name] = runtimeHealth.progressAt - runtimeHealth.cycleStartedAt;
};
async function tick(reason = "alarm") {
  if (running) {
    const stalledMs = Date.now() - Number(runtimeHealth.progressAt || runtimeHealth.cycleStartedAt || 0);
    if (stalledMs < TICK_STALL_MS) return;
    runtimeHealth.stalls = Number(runtimeHealth.stalls || 0) + 1;
    // Reload the worker instead of running a second writer on the old snapshot.
    // Tab bindings, outbox baselines and account cooldowns survive in storage.
    chrome.runtime.reload();
    return;
  }
  const generation = ++tickGeneration;
  running = true;
  runtimeHealth.progressAt = Date.now();
  runtimeHealth.cycleStartedAt = Date.now();
  runtimeHealth.phases = {};
  runtimeHealth.error = "";
  runtimeHealth.storageError = "";
  try {
    await chrome.storage.local.set({ lastCycleStartedAt: Date.now() });
    const badgeCache = await chrome.storage.local.get(["fleetAgentStates", "fleetQuickStates",
      "fleetAttention", "fleetOwnerAttention", "fleetUpdatedAt"]);
    await showFleetBadge(badgeCache.fleetAgentStates, badgeCache.fleetQuickStates,
      (badgeCache.fleetAttention?.length || 0) + (badgeCache.fleetOwnerAttention || 0),
      Number(badgeCache.fleetUpdatedAt) || 0);
    const cfg = await settings();
    let { quickDispatchScope = null, quickDispatchScopeMeta = null } =
      await chrome.storage.local.get(["quickDispatchScope", "quickDispatchScopeMeta"]);
    let scopedDispatch = quickDispatchScope !== null;
    // 19.11: окно ограниченной отправки закрывается само (dispatch-scope.js).
    const retireDispatchScope = async (reason) => {
      await chrome.storage.local.set({ quickDispatchScope: null, quickDispatchScopeMeta: null });
      quickDispatchScope = null; quickDispatchScopeMeta = null; scopedDispatch = false;
      await log(`окно ограниченной отправки снято: ${reason}`);
    };
    // 19.11: прогресс этого тика для сторожа живости (liveness.js).
    const tickEventTypes = new Set();
    let restartWorkerAfterTick = false;
    const extensionVersion = chrome.runtime?.getManifest?.()?.version || "";
    let tabs = await chrome.tabs.query({ url: "https://chatgpt.com/*" });
    // Chrome replaces a failed navigation with chrome-error://chromewebdata/;
    // URL-filtered queries exclude it. Recover only tabs we previously owned.
    const ownedForRecovery = await chrome.storage.local.get(["ownTabs", "quickTabJobs"]);
    const otherTabs = await chrome.tabs.query({}).catch(() => []);
    for (const t of otherTabs) {
      if (!tabs.some((liveTab) => liveTab.id === t.id) &&
          /^chrome-error:/i.test(t.url || "") &&
          /^pult-q\d+/i.test(ownedForRecovery.ownTabs?.[String(t.id)] || "") &&
          ownedForRecovery.quickTabJobs?.[String(t.id)]) tabs.push(t);
    }
    // Critical RAM guard runs BEFORE fleetState(). When dev-mcp/core-mcp crashed
    // from pressure, a failed fleet query must not prevent local memory recovery.
    const earlyFreeMemMB = await freeMemoryMB();
    // No tab-count cap: memory pressure only triggers safe discard.
    const emergency = await chrome.storage.local.get([
      "ownTabs", "labels", "sessions", "wakeMem", "fleetWorkers",
      "launch", "fleetQuickOrderSummary",
    ]);
    let earlyDiscarded = 0;
    const earlyMemoryLow = earlyFreeMemMB !== null &&
      earlyFreeMemMB < LAUNCH_DEFAULTS.minFreeMemMB;
    const earlyLiveCount = liveAgentTabs(tabs, emergency.ownTabs || {},
      emergency.labels?.byTab || {}).length;
    if (earlyMemoryLow) {
      const pages = new Map(await Promise.all(tabs.map(async (tab) =>
        [tab.id, tab.discarded ? null : await probe(tab.id)])));
      const plan = memoryGuardPlan({
        tabs, owned: emergency.ownTabs || {}, bindings: emergency.labels?.byTab || {},
        pages, sessions: emergency.sessions || {}, wake: emergency.wakeMem || {},
        workers: emergency.fleetWorkers || [],
        quickJobs: emergency.fleetQuickOrderSummary || [],
        launch: emergency.launch?.launched || {}, freeMemMB: earlyFreeMemMB,
      });
      if (!cfg.dryRun) for (const item of plan.discard) {
        const tab = tabs.find((t) => t.id === item.id);
        const outcome = await chrome.tabs.discard(item.id).catch(() => null);
        if (!tab || !outcome?.discarded) continue;
        tab.discarded = true;
        earlyDiscarded += 1;
      }
      await chrome.storage.local.set({ fleetMemoryState: {
        at: Date.now(), freeMemMB: await freeMemoryMB(),
        originalFreeMemMB: earlyFreeMemMB, liveTabs: earlyLiveCount - earlyDiscarded,
        thresholdMB: LAUNCH_DEFAULTS.minFreeMemMB,
        lowMemory: earlyMemoryLow, discarded: earlyDiscarded, reason: plan.reason,
      } });
      if (earlyDiscarded) await log(`аварийная защита ОЗУ: выгружено ${earlyDiscarded} вкладок до опроса сервера`);
    }
    const { pinnedByUs: pinned = {} } = await chrome.storage.local.get("pinnedByUs");
    const lockOn = cfg.enabled && cfg.lockTabs && !cfg.dryRun;
    // Аварийная остановка (17.10, владелец): «подозрительная активность из вашей сети» —
    // ни отправок, ни запусков, ни перезагрузок, пока владелец не снимет её в окне.
    const { browserHalt } = await chrome.storage.local.get("browserHalt");
    // 18.10 (владелец): остановку по «Unusual activity» ChatGPT снимаем сами после паузы —
    // это временный антиспам, не авария. Ручная остановка с пульта сама не снимается.
    if (browserHalt && haltAutoResumable(browserHalt) && Date.now() >= haltResumeAt(browserHalt)) {
      await chrome.storage.local.remove("browserHalt");
      // 21.10 (владелец): баннер «Unusual activity» оставался во вкладке всю паузу, и первый же
      // тик после снятия ловил его снова — пауза удваивалась до 2 ч и по кругу. Вкладку с
      // баннером перезагружаем один раз; баннер в окне проверки — старый, не новая блокировка.
      await chrome.storage.local.set({ haltGraceUntil: Date.now() + HALT_GRACE_MS });
      for (const tab of tabs) {
        const page = tab.discarded ? null : await probe(tab.id).catch(() => null);
        // Never sacrifice an overseer draft to the post-halt recovery reload.
        const overseer = page?.label === "owner-sv" ||
          ownedForRecovery.ownTabs?.[String(tab.id)] === "owner-sv";
        if (page?.networkSuspicious && !overseer)
          await chrome.tabs.reload(tab.id).catch(() => {});
      }
      await reportBrowserControl(cfg.mcpUrl, { halt: null }).catch(() => false);
      await log(`аварийная остановка снята автоматически после паузы: ${browserHalt.reason}`);
      chrome.notifications?.create?.("fleet-halt", { type: "basic", iconUrl: "icon128.png",
        title: "Флот продолжает", message: "Пауза после «Unusual activity» прошла — работа возобновлена.", priority: 0 });
    } else if (browserHalt) {
      for (const tab of tabs) await setTabLock(tab, false, "", pinned);
      await chrome.storage.local.set({ pinnedByUs: pinned,
        tickError: `ОСТАНОВЛЕНО: ${browserHalt.reason}. ` + (haltAutoResumable(browserHalt)
          ? `Снимется само в ${new Date(haltResumeAt(browserHalt)).toLocaleTimeString()}; раньше — в окне расширения.`
          : "Снять — в окне расширения.") });
      if (!browserHalt.reported) {
        const sent = await reportBrowserControl(cfg.mcpUrl, { halt: { reason: browserHalt.reason,
          source: browserHalt.source || "page" } }).catch(() => false);
        if (sent) await chrome.storage.local.set({ browserHalt: { ...browserHalt, reported: true } });
      }
      // 21.10 (владелец): во время остановки окно писало «данные устарели — ожидаю ответа
      // сервера» — тик выходил раньше чтения флота. Чтение fleet_list браузер не трогает,
      // поэтому состояние флота обновляем и в остановке.
      const fleet = await fleetState(cfg.mcpUrl, null).catch(() => null);
      if (fleet) {
        const quickJobs = await quickOrders(cfg);
        const quick = quickList.ok ? quickOverview(quickJobs, [], await openedQuickLabels(tabs)) : null;
        await showFleetBadge(fleet.agentStates, quick,
          (fleet.attention?.length || 0) + (fleet.ownerChannel?.attention || 0));
        await chrome.storage.local.set({
          fleetUpdatedAt: Date.now(), ...(Array.isArray(fleet.projects) ? { fleetProjects: fleet.projects } : {}),
          fleetAgentStates: fleet.agentStates ?? null, fleetQuickStates: quick, fleetAttention: fleet.attention || [],
          fleetOwnerAttention: fleet.ownerChannel?.attention || 0,
          fleetRefresh: { startedAt: Date.now(), finishedAt: Date.now(), error: "" },
        });
        // Снято с сервера (fleet.cmd browser resume) — снимаем и здесь.
        if (browserHalt.reported && fleet.browser?.known && !fleet.browser.halt) {
          await chrome.storage.local.remove("browserHalt");
          await log("аварийная остановка снята на сервере");
        }
      }
      return;
    }
    // Автообновление при «A temporary limit…» не зависит от оркестрации флота (выключатель свой).
    await limitReloadTick(tabs, cfg).catch((error) =>
      log("автообновление при лимите: " + String(error?.message || error).slice(0, 120)));
    if (!cfg.enabled) {
      for (const tab of tabs) await setTabLock(tab, false, "", pinned);
      await chrome.storage.local.set({ pinnedByUs: pinned });
      await reportExtension(cfg.mcpUrl, { version: extensionVersion, tabs: tabs.length, pending: 0, enabled: false })
        .catch(() => {}); // Disabled extension still has a heartbeat.
      // Orchestration off does not mean the fleet has stopped working. Badge remains read-only.
      const readOnlyFleet = await fleetState(cfg.mcpUrl, null).catch(() => null);
      if (readOnlyFleet) {
        const quickJobs = await quickOrders(cfg);
        const quick = quickList.ok ? quickOverview(quickJobs, [], await openedQuickLabels(tabs)) : null;
        await showFleetBadge(readOnlyFleet.agentStates, quick,
          (readOnlyFleet.attention?.length || 0) + (readOnlyFleet.ownerChannel?.attention || 0));
        await chrome.storage.local.set({
          fleetUpdatedAt: Date.now(), fleetAgentStates: readOnlyFleet.agentStates ?? null,
          fleetQuickStates: quick, fleetAttention: readOnlyFleet.attention || [],
          fleetOwnerAttention: readOnlyFleet.ownerChannel?.attention || 0,
        });
      }
      return;
    }
    const { ownTabs: protectedTabs = {}, labels: protectedLabels = {} } =
      await chrome.storage.local.get(["ownTabs", "labels"]);
    for (const tab of tabs) {
      // Only extension-owned workers and supervised tabs. In particular, do
      // NOT change Chrome's discard setting for the owner's ordinary tabs.
      const key = String(tab.id), owned = protectedTabs[key] || "";
      const binding = protectedLabels.byTab?.[key];
      const overseer = owned === "owner-sv" ||
        (binding?.label === "owner-sv" && binding.path === chatPath(tab.url));
      if ((isAutostartLabel(owned) || overseer) &&
          tab.autoDiscardable !== false && !tab.discarded && !cfg.dryRun) {
        chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
      }
    }

    // Адреса чатов агентов — серверу: будить агента в ЕГО чате решает сервер (fleet/wake.py),
    // и ему нужен адрес даже после того, как вкладку закрыли.
    const { knownChats = {}, wakeMem = {} } = await chrome.storage.local.get(["knownChats", "wakeMem"]);
    const { wakeReports = {} } = await chrome.storage.local.get("wakeReports");
    const {fleetRefresh:previousRefresh} = await chrome.storage.local.get('fleetRefresh');
    if(previousRefresh?.error && !scopedDispatch) {
      await recoverLocalInterfaces(tabs,cfg);
      tabs=await chrome.tabs.query({url:'https://chatgpt.com/*'});
    }
    let fleet;
    const fleetStartedAt = Date.now();
    await chrome.storage.local.set({ fleetRefresh: { startedAt: fleetStartedAt, finishedAt: null, error: "" } });
    try {
      fleet = await fleetState(cfg.mcpUrl, {
        version: extensionVersion, tabs: tabs.length, chats: chatLines(knownChats),
      });
    } catch (error) {
      const { fleetConnection: priorConnection = null } = await chrome.storage.local.get("fleetConnection");
      const fleetConnection = nextConnection(priorConnection, false, Date.now(), error.message);
      await chrome.storage.local.set({
        fleetRefresh: { startedAt: fleetStartedAt, finishedAt: Date.now(), error: String(error.message) },
        tickError: `Сервер флота не ответил (${fleetConnection.kind}, неудач подряд ${fleetConnection.failures}): ` +
          `${error.message}. ${fleetConnection.hint}. Повторная попытка через 30 с.`,
        quickConnectivityLost: true, fleetConnection,
      });
      // «Нет связи» и «!» — только при устойчивой потере связи (≥3 неудач и ≥5 мин без ответа).
      if (!cfg.dryRun && offlineAlertDue(fleetConnection)) await recordServerAvailability(false).catch(()=>{});
      if (!scopedDispatch) await recoverLocalInterfaces(tabs,cfg);
      await log(`флот недоступен (${error.message}) — ${scopedDispatch
        ? "отправки ждут связи с сервером" : "только локальное восстановление интерфейса"}`);
      return;
    }
    // A real dev-mcp / network recovery clears stale send freezes, not just page reload.
    const { quickConnectivityLost = false, quickNetworkGateDown = false,
      quickSendFailures: priorQuickFailures = {} } =
      await chrome.storage.local.get(["quickConnectivityLost", "quickNetworkGateDown", "quickSendFailures"]);
    const gateDown = fleet.launchGate?.allowed === false;
    const connectionRecovered = quickConnectivityLost || (quickNetworkGateDown && !gateDown);
    if (connectionRecovered) {
      await chrome.storage.local.set({ quickSendFailures: {}, quickConnectivityLost: false,
        quickNetworkGateDown: gateDown });
      for (const memo of Object.values(quickMem)) {
        memo.failures = 0; memo.failureReason = ""; memo.sentAt = 0;
      }
      for (const [id, failure] of Object.entries(priorQuickFailures)) {
        if (failure?.count >= 3) await quickReport(cfg, {
          id, send_paused: false, send_pause_reason: "", send_pause_until: "",
          send_failures: 0,
        });
      }
      await log("связь dev-mcp/сеть восстановлена — локальные паузы отправки быстрых агентов сброшены");
    } else if (quickConnectivityLost || quickNetworkGateDown !== gateDown) {
      await chrome.storage.local.set({ quickConnectivityLost: false,
        quickNetworkGateDown: gateDown });
    }
    const { workers, projects, attention } = fleet;
    {
      const { fleetConnection: priorConnection = null } = await chrome.storage.local.get("fleetConnection");
      await chrome.storage.local.set({ fleetConnection: nextConnection(priorConnection, true, Date.now()) });
    }
    phaseMark("after-fleetState");
    const initialQuickJobs = await quickOrders(cfg);
    if (scopedDispatch) {
      const digests = {};
      for (const job of initialQuickJobs) {
        if (job?.send && quickDispatchScope?.[job.id]) digests[job.id] = await sha256Hex(job.send);
      }
      const scopePlan = dispatchScopePlan(quickDispatchScope, quickDispatchScopeMeta || {},
        initialQuickJobs, digests, Date.now(), { ordersOk: quickList.ok });
      if (scopePlan.retire) {
        if (!cfg.dryRun) await retireDispatchScope(scopePlan.reason);
      } else if (JSON.stringify(scopePlan.meta) !== JSON.stringify(quickDispatchScopeMeta)) {
        quickDispatchScopeMeta = scopePlan.meta;
        if (!cfg.dryRun) await chrome.storage.local.set({ quickDispatchScopeMeta });
      }
    }
    // ChatGPT can restore a project "New chat" draft in an unrelated tab.
    // Keep only server-confirmed starts (or closed jobs), with bounded persistence
    // because the server stops listing closed jobs after its retry window.
    const nowQuick = Date.now();
    const { quickConfirmedStarts: savedStarts = {} } = await chrome.storage.local.get("quickConfirmedStarts");
    const recentStarts = Object.fromEntries(Object.entries(savedStarts || {})
      .filter(([tag, at]) => /^Q\d+(?:-[A-Z0-9]+)?(?:\.[2-9]\d*)?$/.test(tag) &&
        Number.isFinite(at) && at <= nowQuick && nowQuick - at < 30 * 86400_000)
      .sort((a, b) => b[1] - a[1]).slice(0, 200));
    if (quickList.ok) {
      for (const tag of confirmedQuickStartTags(initialQuickJobs)) recentStarts[tag] = nowQuick;
    }
    if (JSON.stringify(recentStarts) !== JSON.stringify(savedStarts))
      await chrome.storage.local.set({ quickConfirmedStarts: recentStarts });
    const confirmedStartAgents = Object.keys(recentStarts);
    const initialQuick = quickList.ok ? quickOverview(initialQuickJobs, [],
      tabs.filter((t) => !t.discarded && t.status !== "unloaded")
        .map((t) => protectedTabs[String(t.id)] || "")) : null;
    await showFleetBadge(fleet.agentStates, initialQuick,
      (attention?.length || 0) + (fleet.ownerChannel?.attention || 0));
    if (fleet.browser?.halt) {
      await chrome.storage.local.set({ browserHalt: { reason: fleet.browser.halt, at: Date.now(),
        source: "server", reported: true } });
      await log(`аварийная остановка с сервера: ${fleet.browser.halt}`);
      return;
    }
    // Не перезаписываем намерение владельца старым снимком во время control+readback.
    if (!browserControlPending && fleet.browser?.known) {
      await chrome.storage.local.set({ browserAutostart: fleet.browser.autostart });
    }
    // Publish a completed query before browser/research work. A later failure
    // cannot hide fresh fleet data behind the timestamp of the whole tick.
    await chrome.storage.local.set({
      // Неполный ответ (нет строки очереди) не стирает проекты: иначе окно пишет «нет во флоте».
      fleetUpdatedAt: Date.now(), ...(Array.isArray(projects) ? { fleetProjects: projects } : {}),
      fleetAgentStates: fleet.agentStates ?? null, fleetQuickStates: initialQuick,
      fleetAttention: attention || [], fleetOwnerAttention: fleet.ownerChannel?.attention || 0,
      fleetRefresh: { startedAt: fleetStartedAt, finishedAt: Date.now(), error: "" }, tickError: "",
    });
    if (!cfg.dryRun) {
      await chrome.storage.local.set({ownerNativeBlocked:fleet.ownerChannel?.nativeBlocked === true});
      await recordServerAvailability(true).catch(()=>{});
      await deliverOwnerNotices(fleet.ownerNotices || [], async (report) => {
        await reportExtension(cfg.mcpUrl,{version:extensionVersion,
          detail:"owner_notice_state:"+JSON.stringify(report)});
      },chrome,Date.now(),fleet.ownerChannel?.nativeBlocked === true)
        .catch((error)=>log("уведомления владельца: "+String(error).slice(0,120)));
      // Badge already refreshed from this exact fleet snapshot above.
    }
    phaseMark("before-research");
    if (!scopedDispatch) await researchTick(cfg.mcpUrl, { enabled: cfg.enabled, dryRun: cfg.dryRun });
    const launchesAllowed = fleet.launchGate?.allowed !== false;
    const grown = includeNewProjects(cfg, projects);
    if (grown) {
      const { settings: saved = {} } = await chrome.storage.local.get("settings");
      await chrome.storage.local.set({
        settings: { ...saved, projects: grown.projects, projectsSeen: grown.projectsSeen },
      });
      cfg.projects = grown.projects;
      cfg.projectsSeen = grown.projectsSeen;
      if (grown.added.length) await log(`новые проекты флота включены: ${grown.added.join(", ")}`);
    }
    const freeMemNow = await freeMemoryMB();
    const kbCfg = KEYBOARD_DEFAULTS;
    const byLabel = new Map(workers.map((w) => [w.label, w]));
    const queueHasWork = (projects || []).some((p) => cfg.projects.includes(p.name) && p.count > 0);
    const memKey = cfg.dryRun ? "memDry" : "mem";
    const launchKey = cfg.dryRun ? "launchDry" : "launch";
    const eventKey = cfg.dryRun ? "eventsDry" : "events";
    const warningKey = cfg.dryRun ? "warningsDry" : "warnings";
    const stored = await chrome.storage.local.get([
      memKey, launchKey, "ownTabs", "attentionSeen", "reloadMem", "labels", eventKey,
      warningKey, "sessions", "recoveryMem", "accountActions", "kbMem", "interfaceQueue", "quickOpenAttempts", "quickIdleSince", "quickSendFailures", "quickSendBaselines", "quickTabJobs", "quickErrorRetries", "quickOrphanSince", "overseerPending", "overseerReloadAt", "overseerRevisions", "chatHandoffs", "chatHandoffLaunches",
    ]);
    let events = stored[eventKey] || [];
    const warnings = stored[warningKey] || {};
    let composerFailure = null;
    let composerHealthy = false;
    const observeComposer = (detail, pageText) => {
      if (/не найдено поле ввода/i.test(String(detail || "")) &&
          /unable to load site/i.test(String(pageText || ""))) {
        composerFailure = { detail: String(detail).slice(0, 500),
                            page_text: String(pageText).slice(0, 400) };
      }
    };
    const event = async (type, fields = {}) => {
      tickEventTypes.add(type);
      events = recordEvent(events, type, fields, fields.at || Date.now());
      await chrome.storage.local.set({ [eventKey]: events });
    };
    const collectNotices = async (outcome, tabId, label) => {
      if (outcome?.ok) { composerHealthy = true; composerFailure = null; }
      else observeComposer(outcome?.detail, outcome?.pageText);
      for (const notice of outcome?.rateEvents || []) {
        if (["warning", "dismissed"].includes(notice.type)) await event(notice.type, { tabId, label, at: notice.at });
      }
    };
    const memory = stored[memKey] || {};
    const reloadMem = stored.reloadMem || {};
    const react = reactConfig();
    const launchCfg = launchConfig(cfg);
    // Метки вкладок: ChatGPT не держит в странице первое сообщение длинного чата (и после
    // перезагрузки), а метка — только в нём. 22.09 так три вкладки агентов «перестали быть
    // флотом»: ни «продолжи», ни «бери следующую», одна висела 20+ минут.
    const labels = { byTab: {}, byChat: {}, ...(stored.labels || {}) };
    const ownTabs = stored.ownTabs || {};
    const sessions = stored.sessions || {};
    // Chrome assigns new tab IDs after profile reopen, and can restore pages
    // without starting their renderer. Rebind only a previously known chat ID;
    // an unlabeled project/owner page is never adopted by position or title.
    const boundChats = new Set(tabs.map(t => labels.byTab[t.id]?.path).filter(Boolean));
    let rebound = false;
    for (const tab of tabs) {
      const chat = chatUrl(tab.url || tab.pendingUrl);
      const label = chat && labels.byChat[chat.id];
      if (!chat || !label || boundChats.has(chat.id) || ownTabs[tab.id] || labels.byTab[tab.id] ||
          /^(owner|pult)/i.test(label) || (!isAutostartLabel(label) && !byLabel.has(label))) continue;
      ownTabs[tab.id] = label;
      labels.byTab[tab.id] = { label, path: chat.id };
      sessions[tab.id] = { label, ...chat, capturedAt: Date.now() };
      boundChats.add(chat.id);
      rebound = true;
    }
    if (rebound && !cfg.dryRun) await chrome.storage.local.set({ ownTabs, labels, sessions });
    const recoveryMem = stored.recoveryMem || {};
    const kbMem = stored.kbMem || {};
    let accountActions = stored.accountActions || { lastActionAt: 0, history: [] };
    const accountWait = (now = Date.now()) => pacedAccountActionWait(accountActions, now, RECOVERY_DEFAULTS);
    const quickWait = (now = Date.now()) => Math.max(accountWait(now),
      Math.max(0, (launchState.overloadUntil || 0) - now));
    const quickOpenAttempts = stored.quickOpenAttempts || {};
    const quickIdleSince = stored.quickIdleSince || {};
    const quickSendFailures = stored.quickSendFailures || {};
    const quickSendBaselines = stored.quickSendBaselines || {};
    const quickTabJobs = stored.quickTabJobs || {};
    const quickErrorRetries = stored.quickErrorRetries || {};
    const quickOrphanSince = stored.quickOrphanSince || {};
    const overseerPending = stored.overseerPending || {};
    const overseerReloadAt = stored.overseerReloadAt || {};
    const overseerRevisions = stored.overseerRevisions || {};
    const reserveAccountAction = async (type, tabId, label, now = Date.now(), throttle = true) => {
      if (interfaceTrialAt && /open|new-interface/.test(type)) {
        interfaceTrialAt = now;
        await chrome.storage.local.set({ interfaceTrialAt });
      }
      const previousLast = accountActions.lastActionAt || 0;
      accountActions = markAccountAction(accountActions, now, type, tabId, label);
      // Обычные сообщения не занимают полосу тяжёлой навигации: open/reload/restore.
      if (!throttle) accountActions.lastActionAt = previousLast;
      await chrome.storage.local.set({ accountActions });
      await event(type, { tabId, label, accountWindow: accountActions.history.length });
    };
    const status = {};
    const live = new Set();
    const agentLabels = [];
    const closedIdleLabels = new Set();
    let launchState = clearLegacyCooldown(stored[launchKey]);
    // До 0.12.8 history означал «создали project-вкладку», даже если чат/сообщение так
    // и не появились. Один раз выбрасываем этот загрязнённый счётчик; дальше history —
    // только подтверждённые старты чатов.
    const launchHistoryMigrated = launchState.chatHistoryVersion !== 2;
    if (launchHistoryMigrated) {
      const hasPending = Object.keys(launchState.launched || {}).length > 0;
      launchState = {
        ...launchState,
        history: [],
        chatHistoryVersion: 2,
        lastLaunchAt: hasPending ? (launchState.lastLaunchAt || 0) : 0,
      };
      await log("миграция запуска: старый счётчик project-вкладок очищен; учитываем только подтверждённые чаты для телеметрии");
    }
    // Освобождаем reservation ещё ДО обхода вкладок. Иначе неответившая вкладка после
    // joinTimeout выпадала из launched только в конце тика и могла ещё один цикл держать слот.
    const priorLaunches = launchState.launched || {};
    const prunedAtStart = pruneLaunched(priorLaunches, workers, Date.now(), launchCfg);
    const expiredLaunchLabels = prunedAtStart.failed;
    const expiredLaunches = Object.fromEntries(expiredLaunchLabels.map(label => [label, priorLaunches[label]]));
    launchState = { ...launchState, launched: prunedAtStart.launched };

    const staleOwned = (key, label, now = Date.now()) => {
      if (!ownTabs[key] || !isAutostartLabel(label) || byLabel.has(label) || (launchState.launched || {})[label]) return false;
      const createdAt = autostartLabelAt(label, now);
      return Boolean(createdAt && now - createdAt >= launchCfg.joinTimeoutSec * 1000);
    };
    const closeStaleOwned = async (tab, key, label, reason) => {
      await setTabLock(tab, false, label, pinned);
      await closeTabKeepBrowser(tab);
      live.delete(key);
      delete ownTabs[key];
      delete sessions[key];
      delete recoveryMem[key];
      delete labels.byTab[key];
      status[key] = { title: tab.title, label, action: "close", reason };
      await log(`${label}: вкладка закрыта — ${reason}`);
    };

    // Пять секунд на ВСЕ пробы вместо пяти секунд на каждую замороженную вкладку.
    phaseMark("before-probes");
    const probes = new Map(await Promise.all(tabs.map(async (tab) => [tab.id, tab.discarded ? null : await probe(tab.id)])));
    const abandonedDraftTabs = new Set();
    for (const tab of tabs) {
      const key = String(tab.id);
      const recordedLaunch = Object.entries(expiredLaunches).find(([, info]) => info?.tabId === tab.id);
      // ownTabs OR the exact tab ID in an expired launch record is ownership proof.
      // A DOM label, matching URL, or merely being on the project page is not.
      const label = ownTabs[key] || recordedLaunch?.[0];
      if (!isAutostartLabel(label) || byLabel.has(label)) continue;
      const expired = expiredLaunches[label];
      if (expired && expired.tabId !== tab.id) continue;
      if (!ownTabs[key] && !recordedLaunch) continue;
      if (!expired && !staleOwned(key, label)) continue;
      const page = probes.get(tab.id);
      // Never close a started conversation or an unidentified/human-edited composer.
      const draftOnly = page && !chatPath(page.url || tab.url) && !page.generating &&
        !page.lastRole && (page.composerEmpty === true || page.fleetDraftLabel === label);
      if (!draftOnly) continue;
      if (cfg.dryRun) {
        await log(`[вхолостую] ${label}: закрыл бы отменённый стартовый черновик`);
      } else {
        await closeStaleOwned(tab, key, label, "отменённый/просроченный запуск: стартовый черновик не отправлен");
        abandonedDraftTabs.add(tab.id);
      }
    }
    const accountIssue = [...probes].find(([id, page]) => ownTabs[id] && page?.accountLoadFailed);
    const overloadNow = Date.now();
    if (accountIssue && overloadNow >= (launchState.overloadUntil || 0)) {
      launchState = accountOverloaded(launchState, overloadNow);
      launchState.overloadReason = "account-load";
      const counts = (hours) => (launchState.history || []).filter(
        (at) => at <= overloadNow && overloadNow - at < hours * 3600_000).length;
      launchState.accountEvents = [...(launchState.accountEvents || []), {
        type: "overload", at: overloadNow,
        chats_1h: counts(1), chats_3h: counts(3), chats_5h: counts(5),
        pause_min: Math.round((launchState.overloadUntil - overloadNow) / 60_000),
      }].slice(-72);
      await chrome.storage.local.set({ [launchKey]: launchState });
      await event("account-overload", { tabId: accountIssue[0], label: ownTabs[accountIssue[0]],
        pauseMin: Math.round((launchState.overloadUntil - overloadNow) / 60_000) });
      await log(`аккаунт перегружен («${accountIssue[1].accountLoadFailed.slice(0, 60)}») — новые чаты на паузе до ${new Date(launchState.overloadUntil).toLocaleTimeString()}`);
    }
    // «Could not load» и 429 разговора локальны до независимой проверки аккаунта.
    {
      const loadFailures = [...probes.values()].filter((p) => p && (p.pageLoadFailed ||
        /could not load this chatgpt conversation|не удалось загрузить (этот )?разговор/i.test(String(p.interrupted || "")))).length;
      const nowLoad = Date.now();
      if (loadFailures < 3) launchState.loadFailFirstAt = 0;
      else if (!launchState.loadFailFirstAt) launchState.loadFailFirstAt = nowLoad;
      const failedTabs = [...probes.entries()].filter(([, p]) => p && (p.pageLoadFailed ||
        /could not load this chatgpt conversation|не удалось загрузить (этот )?разговор/i.test(String(p.interrupted || ""))))
        .map(([id, p]) => ({ id, conv: chatPath(p.url || tabs.find((t) => t.id === id)?.url) })).filter((x) => x.conv);
      const overloaded = (launchState.overloadUntil || 0) > nowLoad;
      // 200 -> можно восстановить UI; 429 -> вкладка ждёт индивидуальный бэкофф.
      launchState.apiCheck ||= {};
      let apiChecks = 0;
      const new429 = [];
      for (const item of failedTabs) {
        if (!ownTabs[item.id] || cfg.dryRun) continue;
        const prev = launchState.apiCheck[item.id]?.conv === item.conv
          ? launchState.apiCheck[item.id] : null;
        if (!prev || nowLoad >= (prev.nextAt || prev.at + 60_000)) {
          if (apiChecks >= 3) continue;
          apiChecks += 1;
          const result = await conversationApiStatus(item.id, item.conv);
          const check = conversationApiCheck(prev, result, nowLoad, item.conv);
          launchState.apiCheck[item.id] = check;
          if (result?.status === 429) {
            new429.push(item);
            await event("conversation-429", { tabId: item.id, retryAfterSec: check.retryAfterSec,
              backoffMin: Math.ceil((check.nextAt - nowLoad) / 60_000) });
          }
        }
        if (launchState.apiCheck[item.id]?.status === 200 && probes.get(item.id)) probes.get(item.id).apiOk = true;
      }
      for (const id of Object.keys(launchState.apiCheck)) {
        if (!probes.has(Number(id)) || !failedTabs.some((item) => String(item.id) === id))
          delete launchState.apiCheck[id];
      }
      const verifyDue = nowLoad - (launchState.loadVerifyAt || 0) >= 60_000;
      const overloadStartedAt = launchState.overloads?.at(-1) || 1;
      const legacyOverload = overloaded && !accountIssue &&
        launchState.overloadReason === "conversation-429" &&
        !(launchState.overloadControlAt >= overloadStartedAt);
      const isFleetTab = (id) => isAutostartLabel(ownTabs[id]) || /^pult-q\d/i.test(ownTabs[id] || "");
      const ownedFailure = new429.find(({id}) => ownTabs[id] !== "owner-sv");
      const legacySource = legacyOverload && tabs.find((tab) => isFleetTab(tab.id) &&
        !tab.discarded && !tab.frozen && tab.status === "complete" && chatUrl(tab.url));
      const controlTabId = legacyOverload ? legacySource?.id : !overloaded ? ownedFailure?.id : null;
      if (controlTabId != null && verifyDue && !cfg.dryRun) {
        launchState.loadVerifyAt = nowLoad;
        // 429 этого же разговора ничего не подтверждает: контролируем другой ресурс.
        const control = await conversationApiStatus(controlTabId, "", true);
        const checkedAt = Date.now();
        if (control?.status === 429) {
          const previousUntil = launchState.overloadUntil || 0;
          if (legacyOverload) launchState = { ...launchState, overloadLevel: 0 };
          launchState = accountOverloaded(launchState, checkedAt);
          launchState.overloadReason = "conversation-429";
          launchState.overloadControlAt = checkedAt;
          launchState.overloadUntil = Math.max(previousUntil, launchState.overloadUntil,
            checkedAt + (control.retryAfterSec || 0) * 1000);
          launchState.overloadPauseMs = launchState.overloadUntil - checkedAt;
          await chrome.storage.local.set({ [launchKey]: launchState });
          await event("account-overload", { reason: "conversation-429", tabs: loadFailures,
            pauseMin: Math.ceil((launchState.overloadUntil - checkedAt) / 60_000) });
        } else if (legacyOverload && control?.status === 200) {
          launchState = { ...launchState, overloadUntil: 0, overloadLevel: 0, overloadReason: "" };
          await chrome.storage.local.set({ [launchKey]: launchState });
          await event("account-overload-rechecked", { status: 200 });
          await log("пауза от старого 429 разговора снята: отдельная проверка аккаунта вернула 200");
        }
      }
    }
    // Защита от нехватки ОЗУ, НЕ лимит числа агентов или вкладок.
    // Не трогает чужие/активные вкладки, генерацию, черновик, pending start/send/wake.
    phaseMark("after-probes");
    const memoryQuickJobs = await quickOrders(cfg);
    const dispatchable = async (job) => {
      if (!scopedDispatch) return true;
      const approved = quickDispatchScope?.[job.id];
      if (job.label !== `pult-${job.id}` || !Array.isArray(approved?.messages) || !approved.messages.length) return false;
      if (!job.send) return !job.wake && !job.handoffOldUrl;
      // An id can be reused for later messages. Only the reviewed payload can start a turn.
      if (job.handoffOldUrl || approved.home !== (job.home || "")) return false;
      const url = job.url || "";
      if (approved.url !== url && (approved.url || !chatUrl(url) || !tabs.some((tab) =>
        quickTabJobs[tab.id] === job.id && ownTabs[tab.id] === job.label && chatPath(tab.url) === chatPath(url)))) return false;
      const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(job.send)));
      const hex = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
      return approved.messages.includes(hex);
    };
    const memoryGuard = memoryGuardPlan({
      tabs, owned: ownTabs, bindings: labels.byTab, pages: probes, sessions,
      workers, quickJobs: memoryQuickJobs, launch: launchState.launched || {},
      wake: wakeMem, freeMemMB: freeMemNow,
      minFreeMemMB: launchCfg.minFreeMemMB,
    });
    const memoryDiscarded = new Set();
    if (!cfg.dryRun) for (const candidate of memoryGuard.discard) {
      const tab = tabs.find((t) => t.id === candidate.id);
      if (!tab || tab.active || tab.discarded) continue;
      const result = await chrome.tabs.discard(tab.id).catch(() => null);
      if (!result?.discarded) continue; // Chrome may refuse (e.g. foreground tab).
      tab.discarded = true;
      probes.set(tab.id, null);
      memoryDiscarded.add(tab.id);
      await event("memory-discard", { tabId: tab.id, label: candidate.label,
        freeMemMB: memoryGuard.freeMemMB, liveTabs: memoryGuard.liveTabs });
      await log(`${candidate.label}: выгружена безопасная вкладка из-за нехватки ОЗУ`);
    }
    const memoryLowThisTick = memoryGuard.underPressure || earlyMemoryLow;
    await chrome.storage.local.set({ fleetMemoryState: {
      at: Date.now(), freeMemMB: memoryGuard.freeMemMB, originalFreeMemMB: earlyFreeMemMB,
      liveTabs: Math.max(0, memoryGuard.liveTabs - memoryDiscarded.size),
      thresholdMB: memoryGuard.thresholdMB,
      lowMemory: memoryLowThisTick, discarded: earlyDiscarded + memoryDiscarded.size,
      reason: memoryGuard.reason,
    } });
    const suspicious = [...probes.values()].find((page) => page?.networkSuspicious)?.networkSuspicious;
    const { haltGraceUntil = 0 } = await chrome.storage.local.get("haltGraceUntil");
    if (suspicious && Date.now() < haltGraceUntil) {
      await log("баннер «Unusual activity» в окне проверки после паузы — ждём перезагрузки вкладки");
    } else if (suspicious) {
      const { haltStreak: prev = { count: 0, lastAt: 0 } } = await chrome.storage.local.get("haltStreak");
      const count = Date.now() - prev.lastAt < HALT_STREAK_WINDOW_MS ? prev.count + 1 : 1;
      await chrome.storage.local.set({ haltStreak: { count, lastAt: Date.now() } });
      const halt = { reason: `ChatGPT: ${String(suspicious).slice(0, 160)}`, at: Date.now(), source: "page",
        reported: false, resumeAt: Date.now() + haltPauseMs(count) };
      for (const tab of tabs) await setTabLock(tab, false, "", pinned);
      await chrome.storage.local.set({ browserHalt: halt, pinnedByUs: pinned });
      await log(`АВАРИЙНАЯ ОСТАНОВКА: ${halt.reason}`);
      chrome.notifications?.create?.("fleet-halt", { type: "basic", iconUrl: "icon128.png",
        title: "Флот остановлен", message: halt.reason, priority: 2 });
      const sent = await reportBrowserControl(cfg.mcpUrl, { halt: { reason: halt.reason, source: "page" } })
        .catch(() => false);
      if (sent) await chrome.storage.local.set({ browserHalt: { ...halt, reported: true } });
      return;
    }
    const interfaceRows=tabs.filter(t => !abandonedDraftTabs.has(t.id)).map(t=>{
      const label=ownTabs[t.id]||labels.byTab[t.id]?.label||probes.get(t.id)?.label||"";
      return {id:t.id,label,own:!!ownTabs[t.id]||isAutostartLabel(label),page:probes.get(t.id),
        dormant:!!t.discarded || t.status==="unloaded",
        url:sessions[t.id]?.url||chatUrl(t.url)?.url};
    });
    const pressure=interfacePlan(interfaceRows,workers,stored.interfaceQueue||{},Date.now(),
      {...INTERFACE_DEFAULTS,maxTabs:launchCfg.maxTabs});
    const activityRows=interfaceRows.filter(r=>r.own&&byLabel.get(r.label)?.task).slice(0,24).map(r=>({
      label:r.label,task:byLabel.get(r.label).task,
      state:!r.page?'unresponsive':r.page.pageLoadFailed||r.page.accountLoadFailed?'error':r.page.generating?'generating':'idle',
      stall_s:Math.max(0,Math.min(86400,Math.floor(Number(r.page?.stallSec)||0)))}));
    if(activityRows.length && !cfg.dryRun) await reportExtension(cfg.mcpUrl,{detail:'activity:'+JSON.stringify(activityRows)})
      .catch(error=>log('наблюдение UI не принято: '+String(error).slice(0,100)));
    const pressureClosed=new Set(abandonedDraftTabs);
    if (!cfg.dryRun && !scopedDispatch) {
      // Persist the original addresses before closing any interface.
      await chrome.storage.local.set({interfaceQueue:pressure.queue});
      for (const id of pressure.close) {
        const tab=tabs.find(t=>t.id===id);
        await sendWithTimeout(id,{type:"lock",on:false},2000).catch(()=>null);
        await closeTabKeepBrowser(tab);
        pressureClosed.add(id);
        await event("interface-queued",{tabId:id,label:ownTabs[id]||labels.byTab[id]?.label||""});
      }
    }
    let openedInterfaces=0;
    const quickDiscardedThisTick = new Set(memoryDiscarded);
    const liveAgentCount = () => liveAgentTabs(tabs, ownTabs, labels.byTab)
      .filter((t) => !pressureClosed.has(t.id) && !quickDiscardedThisTick.has(t.id)).length +
      openedInterfaces; // metric only, never a reason to block opening
    // 18.11: слоты — только вкладки ФЛОТА (свои и pult-qN). Раньше в лимит 4 входили и открытые
    // чаты владельца: 3 его чата занимали 3 слота, флоту оставался один — агентов то 13 (чаты
    // владельца выгружены браузером), то 3. Чаты человека флот не ограничивают.
    const fleetInterface=(r)=>r.own||/^pult-q\d+/i.test(r.label||"");
    const interfaceRoom=()=>interfaceRows.filter(r=>fleetInterface(r)&&!pressureClosed.has(r.id)&&
      !quickDiscardedThisTick.has(r.id)&&!r.dormant).length+openedInterfaces<launchCfg.maxTabs;
    // The existing interface pacing is independent of RAM. Low RAM must NEVER block new tabs.
    const massFailure = pressure.failed >= INTERFACE_DEFAULTS.failedTabs &&
      !interfaceRows.some(r => fleetInterface(r) && r.page?.composerPresent &&
        !r.page.pageLoadFailed && !r.page.accountLoadFailed);
    // Re-evaluate even a persistent mass failure with one paced interface.
    // A real account cooldown or the server gate still takes precedence.
    let { interfaceTrialAt = 0 } = await chrome.storage.local.get("interfaceTrialAt");
    if (massFailure && !interfaceTrialAt) {
      interfaceTrialAt = Date.now();
      await chrome.storage.local.set({ interfaceTrialAt });
    } else if (!massFailure && interfaceTrialAt) {
      interfaceTrialAt = 0;
      await chrome.storage.local.set({ interfaceTrialAt });
    }
    // A free slot is useful only if the new renderer can deliver its first turn.
    // Under pressure, service loaded outboxes before starting more renderers.
    // A stuck chat gets a paced trial alongside it, so it cannot starve other jobs.
    const pendingLabels = new Set();
    for (const job of memoryQuickJobs) {
      if (job.send && quickNeedsInterface(job) && await dispatchable(job)) pendingLabels.add(job.label);
    }
    const tightMemory = Number.isFinite(freeMemNow) && freeMemNow < 3 * launchCfg.minFreeMemMB;
    const pendingRows = interfaceRows.filter(r => {
      if (r.dormant || !r.own) return false;
      const check = launchState.apiCheck?.[r.id];
      if (check?.conv === chatPath(r.url) && conversation429Backoff(check, Date.now())) return false;
      return pendingLabels.has(r.label) || (!scopedDispatch && launchState.launched?.[r.label]?.needsStart);
    });
    const lastOpenAt = (accountActions.history || []).reduce((at, action) =>
      /open|new-interface/.test(action.type || "") ? Math.max(at, action.at || 0) : at, 0);
    const deliveryBusy = () => {
      if (!tightMemory) return false;
      if (openedInterfaces > 0) return true;
      const pending = pendingRows.filter(r => !pressureClosed.has(r.id) && !quickDiscardedThisTick.has(r.id)).length;
      const interval = pending > 1 ? 5 * 60_000 : 60_000;
      return pending > 0 && Date.now() - lastOpenAt < interval;
    };
    const canOpenInterface=()=>interfaceRoom() && !quickWait() && !deliveryBusy() &&
      (!massFailure || Date.now() - interfaceTrialAt >= 5 * 60_000);
    for (const [tabId, page] of probes) {
      // A page we cannot recover must not keep refreshing an outage for Fleet.
      const label = ownTabs[tabId] || labels.byTab[tabId]?.label || page?.label || "";
      if (/^(owner|pult)/i.test(label) || (!ownTabs[tabId] && !byLabel.has(label))) continue;
      if (page?.composerPresent === true) composerHealthy = true;
      if (page?.composerPresent === false && page?.cloudflarePageText) {
        observeComposer("не найдено поле ввода", page.cloudflarePageText);
      }
    }

    // Возврат потерянной сессии важнее очередного «продолжи»: иначе первая готовая вкладка
    // в неизменном порядке tabs.query могла бы каждый раз забирать общую полосу раньше неё.
    // Вычислять порядок можно только после загрузки persisted state и проб вкладок.
    const priority = (tab) => {
      const key = String(tab.id);
      const page = probes.get(tab.id);
      const session = sessions[key] || legacySession(labels.byTab[key], cfg.projectUrl);
      const current = chatUrl(page?.url || tab.url);
      if (page?.pageLoadFailed) return -1;
      if (recoveryMem[key] || (session && (projectPage(page?.url || tab.url) || (current && current.id !== session.id)))) return 0;
      const label = labels.byTab[key]?.label || ownTabs[key] || page?.label || "";
      if ((launchState.launched || {})[label]?.needsStart) return 1;
      return 2;
    };
    const lastReload = (tab) => Number(reloadMem[tab.id]?.lastAt || 0);
    const orderedTabs = tabs.map((tab, index) => ({ tab, index, priority: priority(tab) }))
      .sort((a, b) => a.priority - b.priority ||
        (a.priority === -1 ? lastReload(a.tab) - lastReload(b.tab) : 0) || a.index - b.index)
      .map((item) => item.tab);
    let frozenReloads = 0;

    // attention теперь содержит только сбой self-heal (например, legacy draft, который
    // maintenance почему-то не auto-promote). Это НЕ owner approval и не причина отвлекать
    // человека: фиксируем диагностику и даём следующим tick/fleet_list снова запустить maintenance.
    const attentionSeen = stored.attentionSeen || {};
    const nextSeen = {};
    for (const item of attention || []) {
      nextSeen[item.task] = item.note;
      if (attentionSeen[item.task] !== item.note) {
        await log(`SELF-HEAL ${item.task}: ${item.note}`);
      }
    }

    // Приказы сервера «разбудить» (fleet/wake.py). Кого, когда и каким текстом — решил сервер;
    // здесь только найти вкладку этого чата или открыть чат заново. Текст отправит обход
    // вкладок ниже, когда страница будет готова (этой же или следующей итерацией).
    const reportWake = async (order, state) => {
      if (!/^w-[0-9a-f]{6}$/.test(String(order.wake || ""))) return;
      if (wakeReports[order.wake]?.state === state) return;
      try {
        await reportExtension(cfg.mcpUrl, {
          version: extensionVersion, tabs: tabs.length,
          detail: "wake_state:" + JSON.stringify({ wake: order.wake, state }),
        });
        wakeReports[order.wake] = { state, at: Date.now() };
        await chrome.storage.local.set({ wakeReports });
      } catch (error) {
        await log("будильник: отчёт о состоянии чата не принят — " + String(error).slice(0, 160));
      }
    };
    const wakeState = (page, tab, target) => {
      if (!page) return "error";
      const actual = chatUrl(page.url || tab.url);
      if (!actual || actual.id !== target.id) return "not_found";
      if (page.pageLoadFailed || page.accountLoadFailed || page.failed ||
          page.blocked || page.rateLimited) return "error";
      return page.generating === true ? "generating" : "idle";
    };
    for (const order of (scopedDispatch ? [] : fleet.wake || [])) {
      const target = chatUrl(order.url);
      if (!target) {
        await reportWake(order, "not_found");
        continue;
      }
      const openTab = tabs.find((t) => !pressureClosed.has(t.id) && chatUrl(probes.get(t.id)?.url || t.url)?.id === target.id);
      if (openTab) {
        await reportWake(order, wakeState(probes.get(openTab.id), openTab, target));
        const k = String(openTab.id);
        if (!wakeMem[k]) {
          wakeMem[k] = { label: order.label, text: order.text, at: Date.now() };
          await event("wake-order", { tabId: openTab.id, label: order.label });
        }
        continue;
      }
      // The server resource gate also covers wake-open. An already-open chat
      // above may continue; no new tab is permitted while ChatGPT is DOWN.
      if (!launchesAllowed) {
        await reportWake(order, "error");
        continue;
      }
      if (!canOpenInterface()) continue;
      if (Object.values(wakeMem).some((w) => w.label === order.label && Date.now() - w.at < 10 * 60_000)) {
        await reportWake(order, "not_found");
        continue;
      }
      if (cfg.dryRun) {
        await reportWake(order, "not_found");
        await log(`[вхолостую] будильник: открыл бы чат ${order.label}`);
        continue;
      }
      await reportWake(order, "not_found"); // новая вкладка уточнит состояние в следующем тике
      await reserveAccountAction("wake-open-attempt",null,order.label,Date.now());
      const opened = await chrome.tabs.create({ url: target.url, active: false }).catch(() => null);
      if (!opened) continue;
      openedInterfaces++;
      const k = String(opened.id);
      live.add(k); // не вычищать в конце тика: вкладка ещё не в списке tabs
      ownTabs[k] = order.label;
      labels.byTab[k] = { label: order.label, path: target.id };
      labels.byChat[target.id] = order.label;
      sessions[k] = { label: order.label, id: target.id, url: target.url, capturedAt: Date.now() };
      wakeMem[k] = { label: order.label, text: order.text, at: Date.now() };
      chrome.tabs.update(opened.id, { autoDiscardable: false }).catch(() => {});
      await log(`будильник: ${order.label} — открыл его чат; продолжение уйдёт, когда страница будет готова`);
      await event("wake-open", { tabId: opened.id, label: order.label });
    }

    if (!scopedDispatch && pressure.next && pressure.failed<INTERFACE_DEFAULTS.failedTabs && launchesAllowed &&
        canOpenInterface() && !cfg.dryRun) {
      const [label,entry]=pressure.next;
      await reserveAccountAction("interface-reopen-attempt",null,label,Date.now());
      const opened=await chrome.tabs.create({url:entry.url,active:false}).catch(()=>null);
      if(opened) {
        const key=String(opened.id), target=chatUrl(entry.url);
        openedInterfaces++; live.add(key); ownTabs[key]=label;
        if(target) {sessions[key]={label,...target,capturedAt:Date.now()};labels.byTab[key]={label,path:target.id};}
        delete pressure.queue[label];
        await chrome.storage.local.set({interfaceQueue:pressure.queue,ownTabs,sessions,labels});
      }
    }

    // Надзиратель хода: чаты владельца под надзором держим открытыми с меткой owner-sv —
    // флот их не ведёт и не запирает; после законченного хода снимок уходит серверу.
    phaseMark("before-overseer");
    const overseen = scopedDispatch ? new Map() : await overseerChats(cfg);
    const orders = cfg.enabled === false ? [] : await quickOrders(cfg);
    const quickJobs = [];
    const quickOpenBlocked = {};
    if (quickList.ok) for (const job of orders) {
      if (await dispatchable(job)) quickJobs.push(job);
      else if (job.send || job.wake) quickOpenBlocked[job.id] = "отправка вне разрешённого набора";
    }
    const quickByLabel = new Map(quickJobs.map((j) => [j.label, j]));
    // Length pressure is a conversation-local state, never a shared account halt.
    // Persist the request receipt across MV3 restarts; the transcript proves delivery.
    const handoffs = stored.chatHandoffs || {};
    const handoffLaunches = stored.chatHandoffLaunches || {};
    for (const tab of (scopedDispatch ? [] : tabs)) {
      const page = probes.get(tab.id);
      const oldId = chatPath(page?.url || tab.url);
      if (!oldId || !page || pressureClosed.has(tab.id)) continue;
      const label = ownTabs[String(tab.id)] || labels.byTab[String(tab.id)]?.label || page.label || "";
      const quickJob = quickByLabel.get(label);
      const worker = byLabel.get(label);
      const watched = overseen.get(oldId);
      if (quickJob?.handoffOldUrl && chatPath(quickJob.handoffOldUrl) === oldId) continue;
      // 21.11: чаты под надзором не передаются заранее (85 %) — только по факту «maximum length».
      if (watched && !quickJob && !worker && cfg.fullHandoff !== false) continue;
      const agent = quickJob ? (quickJob.agentTag || quickJob.id.toUpperCase())
        : worker?.id || (watched?.mode === "send"
          ? watched.tag || ("S-" + oldId.replaceAll("-", "").slice(0, 10)) : "");
      if (!agent) continue;
      const key = agent + ":" + oldId;
      const receipt = handoffs[key] ||= {};
      if (receipt.prepared || receipt.done) continue;
      // A real MCP refusal overrides a full-conversation signal until backoff expires.
      if (Date.now() < (receipt.errorRetryAt || 0)) continue;
      if (!handoffDue(page, receipt) && Date.now() < (receipt.nextPollAt || 0)) continue;
      receipt.nextPollAt = Date.now() + 60000;
      if (page.conversationFull && !receipt.bannerLogged) {
        receipt.bannerLogged = true;
        await log(`conversation_full ${key}: chars=${page.conversationUsage?.chars || 0}, turns=${page.conversationUsage?.turns || 0}, cyr=${page.conversationUsage?.cyrillic || 0}, fill=${page.fill || 0}`);
      }
      if (cfg.dryRun) continue;
      const handoffRefused = async (response) => {
        if (response.action !== "error" && response.action !== "refused") return false;
        const reason = String(response.reason || "handoff refused").slice(0, 180);
        receipt.errorRetryAt = Date.now() + handoffBackoff(response);
        if (receipt.lastError !== reason) await log(`handoff ${key}: отказ — ${reason}`);
        receipt.lastError = reason;
        return true;
      };
      try {
        const args = { agent, url: `https://chatgpt.com/c/${oldId}`, label,
          fill: Number(page.fill || 0), chars: Number(page.conversationUsage?.chars || 0),
          full: Boolean(page.conversationFull) };
        const response = handoffResponse(await callTool(cfg.mcpUrl, "fleet_chat_handoff",
          { ...args, command: "observe" }, { timeoutMs: 12000 }));
        if (await handoffRefused(response)) continue;
        delete receipt.errorRetryAt;
        delete receipt.lastError;
        receipt.serverFill = Number(response.fill || 0);
        if (response.action === "none") continue;
        const transcript = await sendWithTimeout(tab.id, { type: "transcript" }, 8000).catch(() => null);
        if (response.action === "request" && !page.conversationFull) {
          if (handoffSent(transcript, response.token)) receipt.requested = true;
          if (!receipt.requested && !page.generating && page.composerEmpty &&
              page.lastRole === "assistant" && !page.blocked && !page.rateLimited) {
            const sent = await sendWithTimeout(tab.id,
              { type: "act", action: "continue", text: response.text }, 45000)
              .catch(() => ({ ok: false }));
            if (sent?.ok) {
              receipt.requested = true;
              await log(`handoff: запрошен файл у ${key}`);
            }
          }
        } else if (response.action === "saved" &&
                   (handoffMarker(transcript, response.token) || page.conversationFull)) {
          const prepared = handoffResponse(await callTool(cfg.mcpUrl, "fleet_chat_handoff",
            { ...args, command: "prepare", token: response.token },
            { timeoutMs: 12000 }));
          if (await handoffRefused(prepared)) continue;
          receipt.prepared = true;
          if (!quickJob) handoffLaunches[key] ||= {
            agent, label: prepared.new_label || label, token: response.token, source: args.url,
            start: prepared.start, overseerMode: watched?.mode || "",
          };
          await log(`handoff: файл получен, подготовлен преемник ${key}`);
        } else if (response.action === "recover" && page.conversationFull) {
          const prepared = handoffResponse(await callTool(cfg.mcpUrl, "fleet_chat_handoff",
            { ...args, command: "prepare", token: response.token, full: true },
            { timeoutMs: 12000 }));
          if (await handoffRefused(prepared)) continue;
          receipt.prepared = true;
          if (!quickJob) handoffLaunches[key] ||= {
            agent, label: prepared.new_label || label, token: response.token, source: args.url,
            start: prepared.start, overseerMode: watched?.mode || "",
          };
          await log(`handoff: аварийное восстановление ${key}, из журнала MCP`);
        } else if (response.action === "prepared" || response.action === "activated") {
          receipt.prepared = true;
        }
      } catch (error) {
        await handoffRefused({ action: "error", reason: String(error), retry_after_s: 600 });
      }
    }
    // For regular fleet workers and explicitly send-supervised chats we create a
    // replacement tab ourselves. Quick agents use their existing quick.orders route.
    for (const [key, launch] of (scopedDispatch ? [] : Object.entries(handoffLaunches))) {
      if (launch.done) continue;
      if (Date.now() < (launch.nextPollAt || 0)) continue;
      try {
        if (!launch.tabId && launchesAllowed && canOpenInterface() && !cfg.dryRun) {
          await reserveAccountAction("handoff-open", null, launch.label);
          const tab = await chrome.tabs.create({ url: cfg.projectUrl, active: false });
          openedInterfaces++;
          live.add(String(tab.id));
          launch.tabId = tab.id;
          launch.openedAt = Date.now();
          ownTabs[String(tab.id)] = launch.overseerMode ? "owner-sv" : launch.label;
          labels.byTab[String(tab.id)] = { label: ownTabs[String(tab.id)], path: null };
          await chrome.storage.local.set({ ownTabs, labels, chatHandoffLaunches: handoffLaunches });
          await log(`handoff ${key}: открыл преемника во вкладке ${tab.id}`);
        }
        if (!launch.tabId) continue;
        const tab = tabs.find(t => t.id === launch.tabId);
        const page = tab && probes.get(tab.id) || await probe(launch.tabId).catch(() => null);
        // After MV3 interruption the click acknowledgement can be lost. Inspect
        // the actual new user turn before any retry, not just a remembered flag.
        if (!launch.sent && page?.lastRole === "user") {
          const transcript = await sendWithTimeout(launch.tabId,
            { type: "transcript" }, 8000).catch(() => null);
          if ((transcript?.userMessages || []).some(u =>
              String(u).includes("HANDOFF_SUCCESSOR:" + launch.token))) {
            launch.sent = true;
          } else {
            launch.conflict = true;
            await log(`handoff ${key}: новый чат содержит чужое пользовательское сообщение — повтор запрещён`);
          }
        }
        if (!launch.sent && !launch.conflict && page?.composerPresent &&
            page.composerEmpty && !page.generating) {
          const out = await sendWithTimeout(launch.tabId, {
            type: "act", action: "start", mention: cfg.connector, text: launch.start,
            confirmedStartAgents,
          }, 45000).catch(() => ({ ok: false }));
          if (out?.ok) launch.sent = true;
        }
        const successor = chatPath(page?.url);
        if (successor && successor !== chatPath(launch.source) && launch.sent) {
          const activated = handoffResponse(await callTool(cfg.mcpUrl, "fleet_chat_handoff", {
            agent: launch.agent, command: "activate", url: launch.source,
            successor_url: `https://chatgpt.com/c/${successor}`, token: launch.token,
          }, { timeoutMs: 12000 }));
          if (activated.action !== "activated") {
            launch.nextPollAt = Date.now() + handoffBackoff(activated);
            await log(`handoff launch ${key}: активация не принята — ${String(activated.reason || activated.action).slice(0, 160)}`);
            continue;
          }
          launch.done = true;
          handoffs[key].done = true;
          labels.byChat[successor] = launch.overseerMode ? "owner-sv" : launch.label;
          labels.byTab[String(launch.tabId)].path = successor;
          await log(`handoff ${key}: чат-преемник активирован ${successor}`);
        }
      } catch (error) {
        launch.nextPollAt = Date.now() + 10 * 60_000;
        await log(`handoff launch ${key}: ${String(error).slice(0, 180)}`);
      }
    }
    await chrome.storage.local.set({ chatHandoffs: handoffs, chatHandoffLaunches: handoffLaunches });
    if (!scopedDispatch && cfg.fullHandoff !== false) {
      const fullHandoffs = (await chrome.storage.local.get("fullHandoffs")).fullHandoffs || {};
      for (const tab of tabs) {
        const page = probes.get(tab.id);
        const oldId = chatPath(page?.url || tab.url);
        if (!oldId || !page?.conversationFull || fullHandoffs[oldId] || pressureClosed.has(tab.id)) continue;
        const watched = overseen.get(oldId);
        const label = ownTabs[String(tab.id)] || labels.byTab[String(tab.id)]?.label || page.label || "";
        if (!watched || byLabel.get(label) || quickByLabel.get(label)) continue; // агентов флота ведёт прежний путь
        if (watched.mode !== "send") {
          fullHandoffs[oldId] = { ...newFullHandoff({ oldId, url: page.url || tab.url, tabId: tab.id }),
            done: true, finishedAt: Date.now(), skipped: `режим ${watched.mode}` };
          await log(`передача ${oldId.slice(0, 8)}: беседа исчерпана, но надзор в режиме ${watched.mode} — новый чат не открываю`);
          continue;
        }
        fullHandoffs[oldId] = newFullHandoff({ oldId, url: page.url || tab.url, tabId: tab.id, index: tab.index,
          windowId: tab.windowId, mode: watched.mode, engine: watched.engine || "", title: tab.title || "" });
        await log(`передача ${oldId.slice(0, 8)}: «maximum length» — начинаю перенос в новый чат того же проекта`);
      }
      for (const rec of Object.values(fullHandoffs)) {
        for (let step = 0; step < 4 && !rec.done && !rec.failed && Date.now() >= (rec.nextAt || 0); step++) {
          try {
            if (!(await fullHandoffStep(rec, { cfg, ownTabs, labels, overseen, live, launchesAllowed,
                                                reserveAccountAction }))) break;
          } catch (error) {
            noteFailure(rec, fullHandoffStage(rec, { draftOnly: cfg.fullHandoffDraftOnly === true }), String(error?.message || error));
            await log(`передача ${rec.oldId.slice(0, 8)}: ${rec.lastError}` +
              (rec.failed ? " — попытки исчерпаны, нужен человек" : ""));
            break;
          }
        }
      }
      await chrome.storage.local.set({ fullHandoffs: pruneFullHandoffs(fullHandoffs) });
    }
    // Только собственные вкладки с подтверждённой меткой. owner/pult без ownTabs
    // принадлежат владельцу и никогда не удаляются.
    for (const tab of (scopedDispatch ? [] : tabs)) {
      const key = String(tab.id);
      const label = ownTabs[key] || labels.byTab[key]?.label || "";
      const page = probes.get(tab.id);
      const id = chatPath(page?.url || tab.url);
      const reason = specialTabClose({ label, owned: ownTabs[key] === label,
        chatId: id, expectedChatId: labels.byTab[key]?.path || chatPath(sessions[key]?.url),
        page, watched: overseen, overseerKnown: overseerList.ok,
        quickOrders: quickJobs, quickKnown: quickList.ok });
      if (!reason || pressureClosed.has(tab.id)) continue;
      if (cfg.dryRun) { await log(`[вхолостую] ${label}: закрыл бы — ${reason}`); continue; }
      await setTabLock(tab, false, label, pinned);
      await closeTabKeepBrowser(tab);
      pressureClosed.add(tab.id);
      if (/^pult-q\d+/i.test(label)) {
        const quickId = quickByLabel.get(label)?.id || quickTabJobs[key];
        if (quickId) await quickReport(cfg, { id: quickId, tab_closed: true, generating: false });
        delete quickTabJobs[key];
      }
      delete ownTabs[key];
      delete labels.byTab[key];
      delete sessions[key];
      status[key] = { title: tab.title, label, action: "close", reason };
      await log(`${label}: закрыта собственная вкладка — ${reason}`);
    }
    const { browserStartedAt = 0 } = await chrome.storage.local.get("browserStartedAt");
    for (const [id, w] of overseen) {
      if (!labels.byChat[id]) labels.byChat[id] = "owner-sv";
      const m = overseerMem[id] ||= { key: "", changedAt: Date.now(), reloadAt: 0, openAt: 0 };
      // 24.11 (владелец, overseer-tabs.js): продолжать в уже открытой вкладке чата — после
      // перезапуска Chrome в восстановленной, на её месте и в её группе, — а не открывать вторую.
      const plan = overseerTabPlan({ tabs: tabs.filter((t) => !pressureClosed.has(t.id)), chat: id,
        urlOf: (t) => probes.get(t.id)?.url || t.url || t.pendingUrl || "", ownTabs, browserStartedAt });
      const open = plan.keep != null;
      if (open) {
        m.opens = 0;
        const key = String(plan.keep), prior = labels.byTab[key];
        // Вкладку быстрого агента или воркера флота под надзор не берём; owner/пустую — берём.
        if ((!prior?.label || /^owner/i.test(prior.label)) &&
            (prior?.label !== "owner-sv" || (prior.path && prior.path !== id))) {
          labels.byTab[key] = { label: "owner-sv", path: id };
          await chrome.storage.local.set({ labels });
          if (plan.adopt) await log(`надзор: продолжаю в открытой вкладке чата ${id.slice(0, 8)}, вторую не открываю`);
        }
        for (const extraId of cfg.dryRun ? [] : plan.close) {
          const extra = tabs.find((t) => t.id === extraId);
          if (!extra) continue;
          await closeTabKeepBrowser(extra);
          pressureClosed.add(extraId);
          delete ownTabs[String(extraId)];
          delete labels.byTab[String(extraId)];
          await chrome.storage.local.set({ ownTabs, labels });
          await log(`надзор: закрыл лишнюю собственную копию чата ${id.slice(0, 8)}`);
        }
      }
      // Chrome ещё восстанавливает вкладки после запуска — прежняя вкладка вот-вот появится.
      if (!open && plan.wait) continue;
      // 15.11: чат с несуществующим адресом переоткрывался каждые 10 мин бесконечно.
      // Три открытия подряд без живой вкладки — хватит: сообщить один раз и не трогать.
      if (!open && (m.opens || 0) >= 3) {
        if (!m.gaveUp) { m.gaveUp = true; await log(`надзор: чат ${id.slice(0, 8)} не открывается — прекращаю (fleet.cmd overseer mode … off)`); }
        continue;
      }
      if (!open && launchesAllowed && canOpenInterface() && !cfg.dryRun &&
          Date.now() - m.openAt > 10 * 60_000) {
        m.openAt = Date.now();
        m.opens = (m.opens || 0) + 1;
        await reserveAccountAction("overseer-open", null, "owner-sv", Date.now());
        const opened = await chrome.tabs.create({ url: w.url, active: false }).catch(() => null);
        if (opened) {
          labels.byTab[String(opened.id)] = { label: "owner-sv", path: id };
          ownTabs[String(opened.id)] = "owner-sv";
          live.add(String(opened.id));
          await chrome.storage.local.set({ ownTabs, labels });
          chrome.tabs.update(opened.id, { autoDiscardable: false }).catch(() => {});
          await log(`надзор: открыл чат ${id.slice(0, 8)} (${w.mode})`);
        }
      }
    }
    // Parked/idle быстрый агент не держит вкладку: сохранённого на сервере URL достаточно.
    // Чужие вкладки владельца даже с тем же URL не закрываем.
    const quickTabOf = (j) => tabs.find((t) => !pressureClosed.has(t.id) &&
      (!quickTabJobs[String(t.id)] || quickTabJobs[String(t.id)] === j.id) &&
      (ownTabs[String(t.id)] === j.label || (!scopedDispatch && labels.byTab[String(t.id)]?.label === j.label)));
    const quickLabels = new Set(quickJobs.map((j) => j.label));
    if (quickList.ok) {
      const activeOrders = new Set(quickJobs.filter(quickNeedsInterface).map(j => j.id));
      for (const id of Object.keys(quickOpenAttempts)) if (!activeOrders.has(id)) delete quickOpenAttempts[id];
    }
    for (const label of Object.keys(quickIdleSince)) if (!quickLabels.has(label)) delete quickIdleSince[label];
    let discardsThisTick = 0;
    phaseMark("before-quick-open");
    const quickCloseAcks = [];
    const recoveringQuickTabs = new Set();
    for (const j of quickJobs) {
      runtimeHealth.progressAt = Date.now();
      if (j.handoffOldUrl) {
        for (const old of tabs.filter(t => !pressureClosed.has(t.id) &&
            chatPath(t.url) === chatPath(j.handoffOldUrl) &&
            (ownTabs[String(t.id)] === j.label ||
             quickTabJobs[String(t.id)] === j.id ||
             probes.get(t.id)?.quickMark === String(j.id).toUpperCase()))) {
          if (!cfg.dryRun) {
            await closeTabKeepBrowser(old);
            pressureClosed.add(old.id);
            delete ownTabs[String(old.id)];
            delete labels.byTab[String(old.id)];
            delete quickTabJobs[String(old.id)];
            await log(`handoff: старый быстрый чат ${j.id} закрыт, открываю преемника`);
          }
        }
      }
      // URL alone is insufficient: the owner may have opened that same chat in a private tab.
      const tabOf = () => quickTabOf(j);
      const m = quickMem[j.id] ||= { key: "", changedAt: Date.now(), reported: true, sentAt: 0, openAt: 0 };
      const open = tabOf();
      const failure = quickSendFailures[j.id];
      if (failure?.count >= 3) {
        if (failure.count > 3) {
          failure.count = 3; // Legacy counters must never show 4/3 or leave the pause cap.
          await chrome.storage.local.set({ quickSendFailures });
        }
        // Migrate old permanent entries into a bounded 10-minute pause.
        const until = Number(failure.pausedUntil) > 0
          ? Number(failure.pausedUntil) : Date.now() + QUICK_SEND_PAUSE_MS;
        if (!failure.pausedUntil) {
          failure.pausedUntil = until;
          await chrome.storage.local.set({ quickSendFailures });
        }
        if (Date.now() >= until) {
          delete quickSendFailures[j.id];
          m.failures = 0; m.failureReason = ""; m.sentAt = 0;
          await chrome.storage.local.set({ quickSendFailures });
          await quickReport(cfg, { id: j.id, send_paused: false,
            send_pause_until: "", send_pause_reason: "", send_failures: 0 });
          await event("quick-pause-expired", { label: j.label });
        } else {
          const text = `пауза отправки до ${new Date(until).toISOString()}: ${failure.reason || "не подтверждена отправка"}`;
          status[String(open?.id || `pause-${j.id}`)] = {
            label: j.label, action: "quick-paused", reason: text,
          };
          if (m.pauseReportedUntil !== until) {
            m.pauseReportedUntil = until;
            await quickReport(cfg, { id: j.id, send_paused: true,
              send_pause_until: new Date(until).toISOString(),
              send_pause_reason: String(failure.reason || "").slice(0, 150),
              send_failures: failure.count });
          }
          if (!open) { quickOpenBlocked[j.id] = text; continue; }
        }
      }
      // Единственный путь восстановления: shared classifyPageIssue + reloadPlan + account lane.
      // Проверять ошибки нужно ПОСЛЕ закрытия parked/idle — иначе сломанная вкладка остаётся навсегда.
      if (j.send || !j.idle || j.close) delete quickIdleSince[j.label];
      else quickIdleSince[j.label] ||= Date.now();
      if (j.close || !quickNeedsInterface(j)) {
        if (!cfg.dryRun) {
          // Close only extension-owned/marked tabs; matching URL never establishes ownership.
          const owned = quickTabsToClose(tabs, j, {
            ownTabs, labelsByTab: labels.byTab, tabJobs: quickTabJobs,
            closed: pressureClosed, chatPath,
            marks: Object.fromEntries(tabs.map((t) => [String(t.id), probes.get(t.id)?.quickMark || ""])),
          });
          for (const t of owned) {
            await closeTabKeepBrowser(t);
            pressureClosed.add(t.id);
            delete ownTabs[String(t.id)];
            delete labels.byTab[String(t.id)];
            delete quickTabJobs[String(t.id)];
            live.delete(String(t.id));
          }
          // 18.11: подтверждение закрытия шло последовательно с таймаутом 15 с для КАЖДОГО
          // закрытого задания в КАЖДОМ цикле — цикл рос до 80–115 с, новые чаты открывались раз
          // в 2 минуты. Теперь параллельно, без ожидания внутри цикла, не чаще раза в 5 мин на задание.
          const lastAck = Number(quickCloseAckAt.get(j.id) || 0);
          if ((owned.length || j.close) && (owned.length || Date.now() - lastAck > 5 * 60_000)) {
            quickCloseAckAt.set(j.id, Date.now());
            quickCloseAcks.push(quickReport(cfg, { id: j.id, tab_closed: true, generating: false })
              .then((ack) => { if (ack === null) return log(`быстрый агент ${j.id}: закрыт локально, подтверждение сервера не получено`); })
              .catch(() => {}));
          }
        }
        continue;
      }
      if (open) delete quickOpenAttempts[j.id];
      if (open && !open.discarded) {
        const page = probes.get(open.id);
        const expected = chatUrl(j.url);
        const actual = chatUrl(page?.url || open.url);
        const wrongChat = expected && actual && expected.id !== actual.id;
        const issue = wrongChat ? { kind: "wrong-chat", text: "conversation URL changed" } :
          classifyPageIssue(page, open, Boolean(expected || actual));
        const recoveryKey = String(open.id);
        const isFailure = Boolean(issue.kind);
        const priorErrorClass = m.lastErrorClass || "";
        const quickApi429 = launchState.apiCheck?.[recoveryKey];
        const quickOwn429 = quickApi429?.conv === (expected || actual)?.id && quickApi429?.status === 429
          ? quickApi429 : null;
        const quickUnavailable429 = isFailure && conversation429Unavailable(quickOwn429, Date.now());
        const observedErrorClass = quickUnavailable429 ? "conversation-429" : issue.kind;
        const observedErrorText = quickUnavailable429 ? "разговор недоступен (429)" : issue.text;
        if (isFailure || m.lastErrorClass) {
          if (m.lastErrorClass !== observedErrorClass) {
            await quickReport(cfg, { id: j.id, error_class: observedErrorClass, error: observedErrorText,
              generating: false });
            m.lastErrorClass = observedErrorClass;
          }
        }
        if (isFailure) {
          const failureText = issue.text || issue.kind;
          const broken = { ...(page || {}), composerEmpty: page?.composerEmpty !== false,
            pageLoadFailed: failureText, generating: false };
          const now = Date.now();
          const previousReload = reloadMem[recoveryKey];
          const previousHistory = launchState.reloads || [];
          const own429 = quickOwn429;
          const unavailable429 = conversation429Unavailable(own429, now);
          const wait429 = conversation429Backoff(own429, now);
          const plan = wait429 ? null : reloadPlan(broken, previousReload, previousHistory, now, react.reload);
          const target = expected?.url || (!j.url && actual?.url) || "";
          // A bound conversation has one recovery owner. The generic frozen-tab
          // path must not reload it again (including while this plan backs off).
          if (target) recoveringQuickTabs.add(open.id);
          if (wait429) {
            const reason = unavailable429 ? "разговор недоступен (429)" : "429 разговора";
            const text = reason + ": следующая проверка не раньше " + new Date(own429.nextAt).toISOString();
            status[recoveryKey] = { title: open.title, label: j.label, action: "quick-blocked", reason: text };
            if (unavailable429) quickOpenBlocked[j.id] = text;
            if (m.lastErrorClass !== (unavailable429 ? "conversation-429" : issue.kind)) {
              m.lastErrorClass = unavailable429 ? "conversation-429" : issue.kind;
              await quickReport(cfg, { id: j.id, error_class: m.lastErrorClass, error: text, generating: false });
            }
            continue;
          }
          const loading = issue.kind === "no-response" && open.status === "loading";
          const loadingSince = loading ? previousReload?.loadingSince || now : 0;
          plan.mem.loadingSince = loadingSince;
          const loadWait = loading ? Math.max(0, Math.max(loadingSince, previousReload?.lastAt || 0) + 120_000 - now) : 0;
          const wait = Math.max(quickWait(now), loadWait);
          if (loadWait && plan.reload) reloadMem[recoveryKey] = { ...previousReload, loadingSince };
          // Do not consume per-tab reload allowance when another tab owns the shared lane.
          // An unperformed plan must remain eligible on the next tick.
          const canNavigate = plan.reload && issue.kind !== "cloudflare" && issue.kind !== "blocked" && target && !wait && !cfg.dryRun;
          if (canNavigate || !plan.reload) {
            reloadMem[recoveryKey] = plan.mem;
            launchState.reloads = plan.history;
          }
          status[recoveryKey] = { title: open.title, label: j.label, action: "recover",
            reason: `${issue.kind}: ${plan.reason || failureText}${wait ? "; общая пауза " + Math.ceil(wait / 1000) + "с" : ""}` };
          // Sensitive gates and bans require a human; never bypass them by reloading.
          if (canNavigate) {
            await reserveAccountAction("reload", open.id, j.label, now, true);
            await chrome.tabs.update(open.id, { url: target }).catch((error) =>
              log(`быстрый агент ${j.id}: возврат к своему чату не удался: ${String(error).slice(0, 100)}`));
            await event("quick-recover", { tabId: open.id, label: j.label, errorClass: issue.kind });
            await log(`быстрый агент ${j.id}: ${issue.kind}, повторяю исходный чат ${target}`);
          }
          // Пока интерфейс сломан, НЕ отправлять сообщение и НЕ считать генерацию работой.
          continue;
        }
        if (priorErrorClass && /^(network-load|turn-failed|conversation-load|account-load|interrupted)$/.test(priorErrorClass) &&
            quickSendFailures[j.id]) {
          delete quickSendFailures[j.id];
          m.failures = 0; m.failureReason = ""; m.sentAt = 0;
          await chrome.storage.local.set({ quickSendFailures });
          await quickReport(cfg, { id: j.id, send_paused: false,
            send_pause_until: "", send_pause_reason: "", send_failures: 0 });
          await event("quick-network-recovered", { label: j.label });
        }
        if (reloadMem[recoveryKey]) delete reloadMem[recoveryKey];
      }
      if (open && j.send && open.autoDiscardable !== false && !open.discarded) {
        await chrome.tabs.update(open.id, { autoDiscardable: false }).catch(() => {});
      }
      // 16.11 (владелец): вкладка не работающего быстрого агента (ответил, запаркован, ждёт владельца)
      // ЗАКРЫВАЕТСЯ, а не выгружается: выгруженная висит в полосе вкладок («19 вкладок — работают 4»),
      // а при нужде всё равно грузится заново. Следующий say откроет чат по адресу из orders.
      const idleDiscarded = open && open.discarded && ownTabs[String(open.id)] === j.label &&
        j.idle && !j.send && !j.close && !open.active;
      if (idleDiscarded || (open && ownTabs[String(open.id)] === j.label && j.idle && !j.send &&
          m.reported !== false && discardsThisTick < 8 && quickDiscardEligible(j, open,
            probes.get(open.id), quickIdleSince[j.label], Date.now()))) {
        if (!cfg.dryRun && chatPath(j.url || open.url || "")) {
          const before = await freeMemoryMB();
          const removed = await chrome.tabs.remove(open.id).then(() => true).catch(() => false);
          if (removed) {
            delete ownTabs[String(open.id)];
            quickDiscardedThisTick.add(open.id);
            discardsThisTick++;
            await log(`быстрый агент ${j.id}: не работает — вкладка закрыта (чат откроется по адресу при следующем сообщении); свободная RAM до ${before ?? "?"} МБ`);
            await event("quick-idle-close", { tabId: open.id, label: j.label, freeBeforeMB: before });
          }
        }
        continue;
      }
      if (open?.discarded && j.send && !cfg.dryRun) {
        if (!canOpenInterface() || quickWait()) continue;
        await reserveAccountAction("quick-reload", open.id, j.label, Date.now());
        await chrome.tabs.update(open.id, { autoDiscardable: false }).catch(() => {});
        await chrome.tabs.reload(open.id).catch((error) => log(`быстрый агент ${j.id}: reload: ${String(error)}`));
        openedInterfaces += 1; // occupy the same global live-tab slot as a new quick tab
        await log(`быстрый агент ${j.id}: возвращаю выгруженный чат для отправки`);
        continue;
      }
      // A missing renderer or a refused tabs.create is retryable. Bound the
      // frequency, not the lifetime number of attempts for this outbox.
      const attempt = quickOpenAttempts[j.id];
      const openDelay = attempt?.count >= 3 ? QUICK_SEND_PAUSE_MS : 3 * 60_000;
      if (!open) {
        const retryAt = attempt ? attempt.at + openDelay : 0;
        const reason = cfg.dryRun ? "dryRun" : !launchesAllowed ? "запуски запрещены сервером" :
          (launchState.overloadUntil || 0) > Date.now() ? "пауза перегрузки аккаунта" :
          !interfaceRoom() ? `нет слота интерфейса (лимит ${launchCfg.maxTabs})` :
          accountWait() ? "полоса навигации аккаунта" : deliveryBusy() ? "мало ОЗУ, отправляются сообщения в открытых вкладках" :
          !canOpenInterface() ? "восстановление интерфейсов" :
          retryAt > Date.now() ? `повтор открытия после ${new Date(retryAt).toISOString()}` : "";
        if (reason) { quickOpenBlocked[j.id] = reason; continue; }
        quickOpenAttempts[j.id] = { count: Math.min(3, (attempt?.count || 0) + 1), at: Date.now() };
        await chrome.storage.local.set({ quickOpenAttempts });
        await reserveAccountAction("quick-open", null, j.label, Date.now());
        const opened = await chrome.tabs.create({ url: j.url || j.home || cfg.projectUrl, active: false }).catch(() => null);
        if (opened) {
          openedInterfaces++;
          live.add(String(opened.id));
          labels.byTab[String(opened.id)] = { label: j.label, path: chatPath(j.url) || null };
          ownTabs[String(opened.id)] = j.label; // своя вкладка: запуск флота её не берёт под задачу
          quickTabJobs[String(opened.id)] = j.id;
          if (j.url) labels.byChat[chatPath(j.url)] = j.label;
          await chrome.storage.local.set({ ownTabs, labels, quickTabJobs });
          chrome.tabs.update(opened.id, { autoDiscardable: false }).catch(() => {});
          m.reported = true; m.key = ""; m.changedAt = Date.now();
          scheduleSoon(1000);
          await log(`быстрый агент ${j.id}: открыл чат`);
        }
      }
    }
    await chrome.storage.local.set({ quickOpenBlocked });
    // 19.11: причина, по которой задание не получает чат, видна серверу — в quick show
    // и в pause_alarms («ждёт отправки N мин; причина: …»). Отчёт только при смене причины.
    if (!cfg.dryRun && quickList.ok) {
      const { quickOpenBlockedReported: reportedBlocked = {} } =
        await chrome.storage.local.get("quickOpenBlockedReported");
      const normalize = (text) => String(text || "").replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, "…").slice(0, 150);
      const nextReported = {}, blockedReports = [];
      for (const [id, reason] of Object.entries(quickOpenBlocked)) {
        nextReported[id] = normalize(reason);
        if ((reportedBlocked || {})[id] === nextReported[id] || quickSendFailures[id]?.count >= 3) continue;
        blockedReports.push(quickReport(cfg, { id, send_paused: true, send_pause_until: "",
          send_pause_reason: "расширение: " + String(reason || "").slice(0, 160) }).catch(() => null));
      }
      for (const id of Object.keys(reportedBlocked || {})) {
        if (id in nextReported || quickSendFailures[id]?.count >= 3) continue;
        blockedReports.push(quickReport(cfg, { id, send_paused: false, send_pause_until: "",
          send_pause_reason: "", send_failures: 0 }).catch(() => null));
      }
      if (blockedReports.length) await withTimeout(Promise.all(blockedReports), 3000, "").catch(() => {});
      await chrome.storage.local.set({ quickOpenBlockedReported: nextReported });
    }
    phaseMark("after-quick-open");
    // Подтверждения закрытия — фоном, с общим потолком: цикл не ждёт каждое по 15 с.
    await withTimeout(Promise.all(quickCloseAcks), 3000, "").catch(() => {});
    // Some servers remove closed quick jobs from orders immediately, without a close row.
    // Retire only tabs with a persisted job binding and no matching live job, after
    // a grace period (orders must have succeeded). Never close unrelated owner tabs.
    if (quickList.ok && !scopedDispatch) {
      const activeIds = new Set(quickJobs.map((j) => j.id));
      for (const [tabId, jobId] of Object.entries(quickTabJobs)) {
        if (activeIds.has(jobId)) { delete quickOrphanSince[tabId]; continue; }
        const owned = tabs.find((t) => String(t.id) === tabId &&
          /^pult-q\d+/i.test(ownTabs[tabId] || "") && !pressureClosed.has(t.id));
        if (!owned) { delete quickOrphanSince[tabId]; continue; }
        quickOrphanSince[tabId] ||= Date.now();
        if (Date.now() - quickOrphanSince[tabId] < 15_000 || cfg.dryRun) continue;
        await closeTabKeepBrowser(owned);
        pressureClosed.add(owned.id);
        live.delete(tabId);
        delete ownTabs[tabId];
        delete labels.byTab[tabId];
        delete quickTabJobs[tabId];
        delete quickOrphanSince[tabId];
        await log(`быстрый агент ${jobId}: сервер удалил задание, вкладка закрыта`);
      }
    }
    const quickTab = async (tab, state) => {
      const j = quickByLabel.get(state.label);
      if (!j) return "быстрый агент: задачи нет";
      if (quickTabJobs[String(tab.id)] && quickTabJobs[String(tab.id)] !== j.id)
        return `${j.id}: вкладка принадлежит другому быстрому заданию`;
      quickTabJobs[String(tab.id)] ||= j.id;
      const m = quickMem[j.id] ||= { key: "", changedAt: Date.now(), reported: true, sentAt: 0, openAt: 0 };
      // Persisted counter is authoritative after an MV3 restart or pause reset.
      m.failures = Math.min(3, Math.max(0, quickSendFailures[j.id]?.count ?? m.failures ?? 0));
      m.failureReason ||= quickSendFailures[j.id]?.reason || "";
      const path = chatPath(state.url);
      if (path && Object.entries(handoffs).some(([id, entry]) =>
          id.endsWith(":" + path) && entry.prepared))
        return `${j.id}: старый UI superseded — действия запрещены`;
      // A stale tab binding must never deliver a different quick agent's task or answer.
      if (j.url && path && path !== chatPath(j.url)) return `${j.id}: чужой URL вкладки — отправка запрещена`;
      if (path && labels.byChat[path] && labels.byChat[path] !== j.label)
        return `${j.id}: чат закреплён за другой меткой — отправка запрещена`;
      if (path && !j.url) {
        labels.byChat[path] = j.label;
        await quickReport(cfg, { id: j.id, url: `https://chatgpt.com/c/${path}` });
        j.url = `https://chatgpt.com/c/${path}`;
      }
      if (m.lastGenerating !== Boolean(state.generating)) {
        m.lastGenerating = Boolean(state.generating);
        await quickReport(cfg, { id: j.id, generating: m.lastGenerating });
      }
      // Transcript can confirm an already-sent user message DURING model generation.
      // A UI action's ok alone is not proof that the message was delivered.
      const t = await sendWithTimeout(tab.id, { type: "transcript", expectedSend: j.send || "" }, 8000).catch(() => null);
      // Only user turns added after our recorded pre-click transcript can confirm this send.
      // The baseline survives MV3 worker restarts; old identical commands must not count.
      const sendFingerprint = j.send ? quickSendFingerprint(j.send) : "";
      const sendBaseline = quickSendBaselines[j.id]?.fingerprint === sendFingerprint
        ? quickSendBaselines[j.id] : null;
      const confirmedSend = Boolean(j.send && confirmedQuickSend(t, j.send, sendBaseline, sendFingerprint));
      if (confirmedSend) {
        if (!chatPath(j.url || state.url)) {
          m.awaitingUrl = true;
          return `${j.id}: сообщение найдено в чате, ожидается URL`;
        }
        const ack = await quickReport(cfg, { id: j.id, sent: true, url: j.url || state.url });
        if (ack !== null) {
          await event("quick-send-confirmed", { tabId: tab.id, label: j.label });
          if (scopedDispatch && quickDispatchScope?.[j.id] && !cfg.dryRun) {
            const hex = await sha256Hex(j.send);
            quickDispatchScopeMeta = { ...(quickDispatchScopeMeta || {}),
              delivered: { ...(quickDispatchScopeMeta?.delivered || {}), [`${j.id}:${hex}`]: Date.now() } };
            await chrome.storage.local.set({ quickDispatchScopeMeta });
          }
          j.send = null; m.failures = 0; m.awaitingUrl = false;
          delete quickSendFailures[j.id];
          delete quickSendBaselines[j.id];
          await chrome.storage.local.set({ quickSendFailures, quickSendBaselines });
          if (j.closeAfterSend === true || j.close_after_send === true) {
            await closeTabKeepBrowser(tab);
            pressureClosed.add(tab.id);
            live.delete(String(tab.id));
            delete ownTabs[String(tab.id)];
            delete labels.byTab[String(tab.id)];
            delete quickTabJobs[String(tab.id)];
            await quickReport(cfg, { id: j.id, tab_closed: true, generating: false });
            return `${j.id}: отправка подтверждена в переписке, вкладка закрыта`;
          }
        }
      }
      if (state.generating) { m.changedAt = Date.now(); m.reported = false; return `${j.id}: идёт ход`; }
      const quickIssue = classifyPageIssue(state, tab, Boolean(chatUrl(j.url || state.url)));
      if (quickIssue.kind || state.blockerWaiting || state.rateLimited)
        return `${j.id}: чат не готов (${quickIssue.kind || "подтверждение или лимит"})`;
      if (j.send && t?.ok && (t.userMessages || []).length && !confirmedSend) {
        const firstUser = (t.userMessages || [])[0] || "";
        const expectedLabel = /label\s*[=:]\s*['"«`]([^'"»`\s]+)['"»`]/i.exec(j.send)?.[1] || "";
        if (expectedLabel && firstUser && !firstUser.includes(expectedLabel))
          return `${j.id}: метка первого сообщения не совпадает с заданием`;
      }
      // 17.11: чат начат стартовым сообщением ДРУГОГО быстрого агента — его ответ не засчитывать
      // этому заданию (q7-f1df2f получил «ответ» по задаче q6 из переиспользованной вкладки).
      {
        const firstUser = (t?.userMessages || [])[0] || "";
        const mark = /⟦быстрый агент (Q\d+(?:-[A-Z0-9]+)?(?:\.[2-9]\d*)?)⟧/.exec(firstUser)?.[1] || "";
        if (mark && mark.toUpperCase() !== String(j.agentTag || j.id).toUpperCase())
          return `${j.id}: чат начат заданием другого агента ${mark} — ответ не засчитываю`;
      }
      if (t?.ok && t.nMessages > 0) {
        const turnKey = `${t.nMessages}|${t.lastAssistant.length}|${t.lastAssistant.slice(-120)}`;
        if (turnKey !== m.key) { m.key = turnKey; m.changedAt = Date.now(); m.reported = false; return `${j.id}: ход меняется`; }
        if (!m.reported && Date.now() - m.changedAt >= 15_000) {
          if (t.lastRole === "assistant" && t.lastAssistant.trim() && chatPath(j.url || state.url)) {
            await quickReport(cfg, { id: j.id, url: j.url || state.url, answer: t.lastAssistant });
            await event("quick-answer", { tabId: tab.id, label: j.label });
          }
          m.reported = true;
        }
      }
      // Новый чат на странице проекта: черновик в поле — общий черновик проекта, start его заменяет
      // (как recoverableNewChatDraft у запуска флота). В идущем чате — только в пустое поле.
      const freshPage = !j.url && !chatPath(state.url) && !state.lastRole;
      // После третьего сбоя блокируем лишь до pausedUntil: следующий тик после 10 минут
      // снимает паузу в loop quickJobs (в том числе после перезапуска MV3 worker).
      if (m.failures >= 3 && quickSendFailures[j.id]?.count >= 3)
        return `${j.id}: отправка приостановлена до ${new Date(quickSendFailures[j.id].pausedUntil).toISOString()}: ${m.failureReason || "нет подтверждения"}`;
      if (m.awaitingUrl && !chatPath(state.url)) return `${j.id}: сообщение подтверждено, ожидается URL`;
      if (j.send && m.reported && (state.composerEmpty || freshPage || chatPath(state.url)) && !cfg.dryRun &&
          Date.now() - m.sentAt > 20_000) {
        const first = !j.url && !chatPath(state.url);
        // 16.11: полосу тяжёлой навигации (15 с между open/reload) съедали перезагрузки сломанных
        // чатов (59 действий за 15 мин), и сообщения в УЖЕ открытые чаты ждали её бесконечно.
        // Сообщение в открытый чат ждёт только общей паузы перегрузки аккаунта, не полосы навигации.
        // Часовой потолок действий аккаунта (защита от блокировки) соблюдается и здесь.
        const hourly = (() => { const now = Date.now(); const recent = (accountActions.history || [])
          .filter((x) => Number.isFinite(x?.at) && x.at <= now && now - x.at < 3600_000);
          return recent.length >= ACCOUNT_ACTIONS_PER_HOUR
            ? Math.max(0, recent[recent.length - ACCOUNT_ACTIONS_PER_HOUR].at + 3600_000 - now) : 0; })();
        // 18.11: первое сообщение в уже открытый чат тоже не навигация. Раньше оно ждало полосу
        // open/reload (15 с), которую каждое новое открытие других заданий сбрасывало раньше, —
        // отправка голодала десятки минут («пауза аккаунта ещё 5 с» по кругу). Теперь только
        // часовой потолок, перегрузка и короткий зазор 5 с от последнего действия аккаунта.
        const overload = Math.max(0, (launchState.overloadUntil || 0) - Date.now());
        // Зазор 5 с от последнего действия (0.19.45) тоже голодал: в каждом тике сначала
        // открывается новый чат, и все отправки после него видели «ещё 5 с» (живьём 07:18).
        const wait = Math.max(hourly, overload);
        if (wait) return `${j.id}: общая пауза аккаунта ещё ${Math.ceil(wait / 1000)} с`;
        // Older scripts only provide the first 80 turns. Keep their ambiguous
        // baselines fail-closed across a rolling extension update.
        if ((!sendBaseline?.fullCount && sendBaseline?.userCount >= 80) ||
            (!Number.isSafeInteger(t?.userCount) && t?.userMessages?.length >= 80))
          return `${j.id}: лимит 80 сообщений пользователя — безопасное подтверждение недоступно, повтор не выполняю`;
        if (!sendBaseline) {
          const before = quickSendBaseline(t, sendFingerprint);
          if (!before) return `${j.id}: снимок переписки до отправки недоступен — жду следующего тика`;
          quickSendBaselines[j.id] = before;
          // Persist BEFORE clicking; a restarted service worker must not trust old turns.
          await chrome.storage.local.set({ quickSendBaselines });
        }
        m.sentAt = Date.now();
        // Failed retries are not counted as account actions; record only verified delivery.
        // Новый чат — как у флота: action start с упоминанием коннектора dev-mcp (иначе у агента
        // нет инструментов); в существующий — continue.
        const outcome = await sendWithTimeout(tab.id, first
          ? { type: "act", action: "start", mention: cfg.connector, text: j.send, quick: true,
              confirmedStartAgents }
          : { type: "act", action: "continue", text: j.send, quick: true }, 45000, scopedDispatch)
          .catch((error) => ({ ok: false, detail: String(error) }));
        await event(outcome?.ok ? "quick-sent" : "quick-send-failed", { tabId: tab.id, label: j.label });
        if (!outcome?.ok && !outcome?.wait) {
          m.failures = Math.min(3, (m.failures || 0) + 1);
          m.failureReason = String(outcome?.detail || "нет подтверждения").slice(0, 150);
          const until = m.failures >= 3 ? Date.now() + QUICK_SEND_PAUSE_MS : 0;
          quickSendFailures[j.id] = { count: m.failures, reason: m.failureReason,
            at: Date.now(), ...(until ? { pausedUntil: until } : {}) };
          await chrome.storage.local.set({ quickSendFailures });
          await log(`быстрый агент ${j.id}: попытка ${m.failures}/3 — ${m.failureReason}`);
          if (until) {
            m.pauseReportedUntil = until;
            await event("quick-paused", { tabId: tab.id, label: j.label, reason: m.failureReason });
            status[String(tab.id)] = { title: tab.title, label: j.label, action: "quick-paused",
              reason: `${m.failureReason}; до ${new Date(until).toISOString()}` };
            await quickReport(cfg, { id: j.id, send_paused: true,
              send_pause_until: new Date(until).toISOString(),
              send_pause_reason: m.failureReason, send_failures: m.failures,
              error: `отправка приостановлена до ${new Date(until).toISOString()}: ${m.failureReason}` });
          }
        }
        if (outcome?.ok) {
          const made = chatUrl(outcome?.url);
          if (made) {
            j.url = made.url; labels.byChat[made.id] = j.label;
            labels.byTab[String(tab.id)] = { label: j.label, path: made.id };
            // An MCP tool call can mark the job working before DOM send confirmation.
            // Save the chat URL independently of the later `sent` acknowledgement.
            await quickReport(cfg, { id: j.id, url: made.url });
          }
          m.failures = 0; delete quickSendFailures[j.id];
          await chrome.storage.local.set({ quickSendFailures });
          m.awaitingUrl = !chatPath(j.url);
          await reserveAccountAction("quick-send", tab.id, j.label, Date.now(), false);
          m.reported = false; m.changedAt = Date.now();
          if (m.awaitingUrl) return `${j.id}: сообщение подтверждено, ожидается URL`;
          scheduleSoon(1000);
          return `${j.id}: клик отправки подтверждён; жду новое сообщение пользователя в переписке`;
        }
        return `${j.id}: не ушло — ${String(outcome?.detail || "").slice(0, 60)}`;
      }
      return `${j.id}: ${m.reported ? "ждёт" : "ход стабилизируется"}`;
    };
    const overseeTab = async (tab, state) => {
      const id = chatPath(state.url);
      const w = overseen.get(id);
      if (!w) return "";
      const m = overseerMem[id] ||= { key: "", changedAt: Date.now(), reloadAt: 0, openAt: 0 };
      if (m.pending) return "надзор: оцениваю ход";
      if (m.result) {
        const r = m.result;
        m.result = null;
        m.last = r.verdict || r.why || "";
        // A verdict is single-shot on the server (journal already contains mode=send).
        // Persist its exact text BEFORE attempting UI delivery: a surviving draft or
        // MV3 service-worker restart must not lose the command or force a page reload.
        if (r.action === "send" && r.text && w.mode === "send" && !cfg.dryRun &&
            m.key === r.key && !overseerPending[id]) {
          let allowed;
          try {
            allowed = await reserveOverseerSend();
          } catch (error) {
            await log(`надзор ${id.slice(0, 8)}: хранилище лимита недоступно — ${String(error).slice(0, 120)}`);
            return "надзор: лимит не проверен — отправка отменена";
          }
          if (!allowed) {
            await log(`надзор ${id.slice(0, 8)}: потолок ${OVERSEER_SENDS_PER_HOUR} отправок в час — «${r.verdict}» не отправлено`);
            return "надзор: потолок отправок в час";
          }
          overseerPending[id] = {
            text: String(r.text), verdict: String(r.verdict || ""), key: r.key,
            baseCount: Number(m.lastTranscriptCount || 0), attemptAt: 0,
          };
          await chrome.storage.local.set({ overseerPending });
        }
      }
      // Upgrade path for a verdict which was already typed before 0.19.30.
      // The original server result is single-shot; only the exact ⟦надзор⟧
      // marker from the live editor is safe to resume. Unknown owner drafts stay put.
      if (!overseerPending[id] && state.composerOverseerDraft && w.mode === "send" && !cfg.dryRun) {
        const before = await sendWithTimeout(tab.id, { type: "transcript" }, 8000).catch(() => null);
        if (before?.ok && before.lastRole === "assistant") {
          let allowed = false;
          try { allowed = await reserveOverseerSend(); }
          catch (error) { await log("надзор: бюджет восстановления недоступен: " + String(error).slice(0, 100)); }
          if (allowed) {
            overseerPending[id] = {
              legacyDraft: true, verdict: "восстановленный надзор",
              baseCount: Number(before.nMessages || 0), attemptAt: 0,
            };
            await chrome.storage.local.set({ overseerPending });
            await log(`надзор ${id.slice(0, 8)}: найден старый неотправленный ⟦надзор⟧ черновик; восстановление без reload`);
          }
        }
      }
      const queued = overseerPending[id];
      if (queued && w.mode !== "send") {
        delete overseerPending[id];
        await chrome.storage.local.set({ overseerPending });
        return "надзор: автоматическая отправка отключена";
      }
      if (queued) {
        const t = await sendWithTimeout(tab.id, { type: "transcript" }, 8000).catch(() => null);
        const norm = (value) => String(value || "").replace(/[\u200B\uFEFF]/g, "")
          .replace(/\s+/g, " ").trim();
        // A click timeout is NOT proof of failure: inspect the subsequent user turn
        // before any retry. A verdict may cite old identical text, hence baseCount.
        const delivered = t?.ok && Number(t.nMessages) > Number(queued.baseCount || 0) &&
          (t.userMessages || []).slice(-1).some((v) => queued.legacyDraft
            ? norm(v).startsWith("⟦надзор⟧")
            : Boolean(norm(queued.text) && norm(v).includes(norm(queued.text))));
        if (delivered) {
          delete overseerPending[id];
          await chrome.storage.local.set({ overseerPending });
          await event("overseer-sent", { tabId: tab.id, label: "owner-sv" });
          return "надзор: отправка подтверждена новым сообщением";
        }
        // 25.11 (jev-1): после решения в чате появилось другое сообщение (владелец успел написать) —
        // старое воздействие отменяется, а не отправляется поверх нового задания.
        if (!queued.legacyDraft && t?.ok && Number(t.nMessages) > Number(queued.baseCount || 0)) {
          delete overseerPending[id];
          await chrome.storage.local.set({ overseerPending });
          await log(`надзор ${id.slice(0, 8)}: после решения «${queued.verdict}» появилось новое сообщение — воздействие отменено`);
          return "надзор: воздействие отменено — в чате новое сообщение";
        }
        if (state.generating) return "надзор: жду подтверждения отправки во время генерации";
        if (queued.legacyDraft && !state.composerOverseerDraft)
          return "надзор: восстановленный черновик больше не распознан; не нажимаю Send";
        if (state.blockerWaiting || state.failed || state.rateLimited || state.blocked ||
            state.interrupted || !state.composerPresent) return "надзор: отправка ждёт доступного поля";
        if (Date.now() - Number(queued.attemptAt || 0) < 20_000)
          return "надзор: проверяю отправку после последней попытки";
        queued.attemptAt = Date.now();
        await chrome.storage.local.set({ overseerPending });
        // Same exact content-script path as quick agents: findComposer -> sendText ->
        // sendButton scoped to the composer -> verify NEW user turn. Unlike quick mode,
        // never replace an arbitrary nonempty owner draft.
        await reserveAccountAction("overseer-send", tab.id, "owner-sv", Date.now(), false);
        const action = queued.legacyDraft
          ? { type: "act", action: "overseer-resume-draft" }
          : { type: "act", action: "continue", text: queued.text, overseer: true };
        const outcome = await sendWithTimeout(tab.id, action, 45000)
          .catch((error) => ({ ok: false, detail: String(error) }));
        await event(outcome?.ok ? "overseer-sent" : "overseer-failed",
          { tabId: tab.id, label: "owner-sv" });
        if (outcome?.ok) {
          delete overseerPending[id];
          await chrome.storage.local.set({ overseerPending });
        }
        await log(`надзор ${id.slice(0, 8)}: ${queued.verdict} → ${outcome?.ok ? "отправлено с DOM-подтверждением" : "оставляю черновик: " + outcome?.detail}`);
        return `надзор: ${queued.verdict} — ${outcome?.ok ? "отправлено" : "отправка ожидает повтора"}`;
      }
      if (state.generating) {
        m.changedAt = Date.now();
        return "надзор: идёт ход";
      }
      if (state.blockerWaiting || state.failed || state.rateLimited || state.blocked || state.interrupted) {
        return "надзор: чат не готов (подтверждение, ошибка или лимит)";
      }
      const t = await sendWithTimeout(tab.id, { type: "transcript" }, 8000).catch(() => null);
      if (!t?.ok) return "надзор: текст не прочитан";
      m.lastTranscriptCount = Number(t.nMessages || 0);
      const turnKey = `${t.nMessages}|${t.lastAssistant.length}|${t.lastAssistant.slice(-120)}`;
      if (turnKey !== m.key) {
        m.key = turnKey;
        m.changedAt = Date.now();
        m.reported = false;
        return "надзор: ход меняется";
      }
      const stableSec = (Date.now() - m.changedAt) / 1000;
      // 25.11 (jev-1): сервер снял fleet-блокер ждущего чата — одна переоценка того же хода.
      if (w.reobserve && m.reobserveSeen !== w.reobserve) {
        m.reobserveSeen = w.reobserve;
        if (m.reported) { m.reported = false; await log(`надзор ${id.slice(0, 8)}: условие ожидания изменилось — переоценка`); }
      }
      if (!m.reported && stableSec >= 20) {
        m.reported = true;
        const snap = { url: state.url, user_messages: t.userMessages, last_assistant: t.lastAssistant,
          prev_assistant: t.prevAssistant, last_role: t.lastRole, n_messages: t.nMessages, generating: false,
          last_assistant_id: t.lastAssistantId || null, user_truncated: Boolean(t.userTruncated),
          composer_empty: Boolean(state.composerEmpty), blocked: Boolean(state.blocked) };
        m.pending = callTool(cfg.mcpUrl, "fleet_overseer_observe", { snapshot: JSON.stringify(snap) },
          { timeoutMs: 300_000 })
          .then((raw) => {
            m.result = { ...JSON.parse(raw), key: turnKey };
            m.observedKey = turnKey; // only acknowledged observations establish an API baseline
          })
          .catch((error) => { m.result = null; m.last = "ошибка: " + String(error).slice(0, 80); })
          .finally(() => { m.pending = null; });
        return "надзор: ход закончен, оцениваю";
      }
      // Владелец пишет в своём окне — новые ходы эта вкладка увидит только после перезагрузки.
      // Do not erase the 437-char unsent overseer draft. Persist the five-minute
      // reload cooldown across MV3 service-worker restarts.
      if (state.composerEmpty !== true || overseerPending[id])
        return "надзор: черновик в поле — перезагрузка запрещена";
      if (stableSec > 300 && Date.now() - Number(overseerReloadAt[id] || 0) >= 300_000 &&
          !accountWait() && !cfg.dryRun) {
        overseerReloadAt[id] = Date.now();
        await chrome.storage.local.set({ overseerReloadAt });
        m.reloadAt = overseerReloadAt[id];
        await reserveAccountAction("reload", tab.id, "owner-sv", Date.now(), true);
        await chrome.tabs.reload(tab.id).catch(() => {});
        return "надзор: обновляю страницу (не чаще 5 мин)";
      }
      return `надзор: ${m.last || "ход стабилен"} (${Math.round(stableSec)} с)`;
    };

    // Separate small budgets prevent live baseline sampling from starving
    // discarded conversations when there are many supervised chats.
    let overseerDiscardChecks = 0, overseerLiveChecks = 0;
    const isWatchedOwnerTab = (tab, id) => {
      const key = String(tab.id), binding = labels.byTab[key];
      return Boolean(id && overseen.has(id) &&
        (ownTabs[key] === "owner-sv" || binding?.label === "owner-sv") &&
        (!binding?.path || binding.path === id));
    };
    for (const tab of orderedTabs) {
      runtimeHealth.progressAt = Date.now();
      if(pressureClosed.has(tab.id)) continue;
      const key = String(tab.id);
      live.add(key);
      if (recoveringQuickTabs.has(tab.id)) continue;
      const known = labels.byTab[key]?.label || ownTabs[key] || "";
      if (scopedDispatch && (!quickByLabel.has(known) || ownTabs[key] !== known)) continue;
      if (!sessions[key]) sessions[key] = legacySession(labels.byTab[key], cfg.projectUrl);
      // Выгруженная браузером вкладка (Memory Saver) не работает вовсе — вернуть её, если она
      // агентская. Замороженная (tab.frozen, Chrome 132+) не отвечает на probe.
      if (tab.discarded || tab.status === "unloaded") {
        const watchedId = chatPath(tab.url || tab.pendingUrl);
        if (isWatchedOwnerTab(tab, watchedId)) {
          // Chrome's external Memory Saver may still have discarded an old tab.
          // Do not load its multi-GB renderer just to see whether a turn arrived.
          if (tab.autoDiscardable !== false && !cfg.dryRun) {
            await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
            tab.autoDiscardable = false;
          }
          status[key] = { title: tab.title, label: "owner-sv",
            note: "надзор: выгружена; проверка update_time без загрузки" };
          const prior = overseerRevisions[watchedId] || {};
          const now = Date.now();
          // A durable verdict is already work to deliver: this is not a
          // speculative snapshot reload, even if update_time stayed unchanged.
          if (overseerPending[watchedId]) {
            status[key].note = "надзор: выгружена; ожидает отправки сохранённого вердикта";
            if (!cfg.dryRun && !accountWait() &&
                now - Number(overseerReloadAt[watchedId] || 0) >= 300_000) {
              await reserveAccountAction("reload", tab.id, "owner-sv", now, true);
              if (await chrome.tabs.reload(tab.id).then(() => true, () => false)) {
                overseerReloadAt[watchedId] = now;
                await chrome.storage.local.set({ overseerReloadAt });
                status[key].note = "надзор: восстанавливаю интерфейс для отправки вердикта";
              }
            }
            continue;
          }
          if (now - Number(prior.checkedAt || 0) >= 45_000 && overseerDiscardChecks < 1) {
            // No human/unknown tab is an API fetch source. This source's renderer
            // already exists; the discarded watched renderer stays asleep.
            const source = tabs.find((candidate) => candidate.id !== tab.id &&
              !candidate.discarded && candidate.status !== "unloaded" && !candidate.frozen &&
              /^(https:\/\/chatgpt\.com\/)/.test(candidate.url || "") &&
              /^(?:f\d{4}-\d{4}[a-z0-9]{3}|pult-q\d+(?:-[0-9a-z]+)?|owner-sv)$/i
                .test(ownTabs[String(candidate.id)] || ""));
            if (!source) {
              status[key].note = "надзор: выгружена; нет своей загруженной вкладки для API";
            } else {
              overseerDiscardChecks += 1;
              const updated = await overseerConversationUpdateTime(source.id, watchedId);
              const decision = overseerRevisionPlan(prior, updated, now);
              overseerRevisions[watchedId] = decision.state;
              if (!updated) status[key].note = "надзор: API update_time недоступен, повторю позже";
              else if (decision.reload) {
                // Unlike routine five-minute UI refreshes, this reload requires
                // a confirmed changed (or previously unknown) server revision.
                if (accountWait() || now - Number(overseerReloadAt[watchedId] || 0) < 300_000) {
                  status[key].note = "надзор: новый ход обнаружен; ожидаю разрешения перезагрузки";
                } else if (!cfg.dryRun) {
                  await reserveAccountAction("reload", tab.id, "owner-sv", now, true);
                  const loaded = await chrome.tabs.reload(tab.id).then(() => true, () => false);
                  if (loaded) {
                    overseerRevisions[watchedId] = { value: updated, checkedAt: now };
                    overseerReloadAt[watchedId] = now;
                    await chrome.storage.local.set({ overseerRevisions, overseerReloadAt });
                    status[key].note = "надзор: серверный ход изменился — загрузила вкладку для снимка";
                    await log(`надзор ${watchedId.slice(0, 8)}: update_time изменился — восстанавливаю вкладку для снимка`);
                  }
                }
              } else if (decision.state.candidate) {
                status[key].note = "надзор: update_time изменился; подтверждаю стабильность";
              }
            }
          }
          continue;
        }
        if (staleOwned(key, known)) {
          await closeStaleOwned(tab, key, known, "осиротевший автозапуск без fleet_join — освобождаю слот");
          continue;
        }
        // Keep memory-shed keyboards asleep while RAM is low. A pending start
        // or an explicit server wake still gets the next available interface.
        if (memoryLowThisTick && !wakeMem[key] && !launchState.launched?.[known]?.needsStart) {
          status[key] = { label: known, note: "выгружена; ждёт работы или освобождения ОЗУ" };
          continue;
        }
        status[key] = { title: tab.title, label: known, note: "выгружена браузером" };
        if (known && !/^(owner|pult)/i.test(known) && !cfg.dryRun) {
          const waitMs = accountWait();
          if (waitMs || !canOpenInterface()) {
            status[key].note = `выгружена браузером; общая пауза ещё ${Math.ceil(waitMs / 1000)} с`;
          } else {
            await reserveAccountAction("reload", tab.id, known);
            await chrome.tabs.reload(tab.id).catch(() => {});
            openedInterfaces += 1; // reserve a live slot for this reload; tab snapshot remains discarded
            await log(`${known}: вкладка была выгружена браузером — загружаю заново`);
          }
        }
        continue;
      }
      let state = probes.get(tab.id);
      if (!state) {
        if (staleOwned(key, known)) {
          await closeStaleOwned(tab, key, known, "осиротевший автозапуск не отвечает и не зарегистрирован — освобождаю слот");
          continue;
        }
        const knownWorker = known ? byLabel.get(known) : null;
        const now = Date.now();
        const oldUnresponsive = reloadMem[key] || {};
        const unresponsiveSince = Number(oldUnresponsive.unresponsiveSince || now);
        const unresponsiveSec = Math.max(0, (now - unresponsiveSince) / 1000);
        reloadMem[key] = { ...oldUnresponsive, unresponsiveSince };

        // Зарегистрированная extension-вкладка без задачи не должна навсегда занимать
        // физический tab-slot только потому, что content-script умер. Сначала даём ей
        // несколько recovery reload; после обычного closeIdle она освобождает место.
        if (ownTabs[key] && isAutostartLabel(known) &&
            ((knownWorker && !knownWorker.task && unresponsiveSec >= launchCfg.closeIdleSec) ||
             (knownWorker?.task && knownWorker.silenceMin * 60 >= 360 && unresponsiveSec >= 360))) {
          if (!knownWorker.task) closedIdleLabels.add(known);
          await closeStaleOwned(tab, key, known,
            `вкладка не отвечает ${Math.round(unresponsiveSec)}с, последний MCP ${knownWorker.silenceMin} мин назад — освобождаю интерфейс`);
          continue;
        }

        if (known && !/^(owner|pult)/i.test(known)) agentLabels.push(known);
        const lastRecoveryReloadAt = Number(
          oldUnresponsive.unresponsiveReloadAt || oldUnresponsive.frozenReloadAt || 0,
        );
        const pendingRenderer = Boolean(launchState.launched?.[known]?.needsStart);
        const ownQuick = Boolean(known && /^pult-q\d+/i.test(known) && ownTabs[key]);
        const recoveryReason = tab.frozen
          ? "вкладка заморожена Chrome"
          : "content-script вкладки не отвечает";
        const canReload = Boolean(
          ownTabs[key] && (knownWorker || ownQuick || pendingRenderer) && unresponsiveSec >= (tab.frozen ? 0 : 10) &&
          frozenReloads < 2 && !quickWait(now) && now - lastRecoveryReloadAt >= 60_000
        );
        // 18.11: своя вкладка быстрого агента (pult-qN) тоже восстанавливается — раньше она
        // навсегда оставалась «страница не отвечает», и текст задания висел неотправленным.
        if (((tab.frozen || knownWorker || pendingRenderer) && known && !/^(owner|pult)/i.test(known)) || ownQuick) {
          status[key] = {
            title: tab.title, label: known, action: canReload ? "reload" : "skip",
            reason: canReload
              ? `${recoveryReason} — перезагружаю, чтобы вернуть content-script`
              : `${recoveryReason} — recovery ждёт следующего тика`,
          };
          if (canReload && !cfg.dryRun) {
            frozenReloads += 1;
            reloadMem[key] = {
              ...reloadMem[key],
              unresponsiveReloadAt: now,
              ...(tab.frozen ? { frozenReloadAt: now } : {}),
            };
            await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
            await reserveAccountAction("reload", tab.id, known, now);
            await chrome.tabs.reload(tab.id).catch(() => {});
            await log(`${known}: ${recoveryReason} — перезагружаю`);
          }
          continue;
        }
        status[key] = { title: tab.title, label: known, note: "страница не отвечает" };
        continue;
      }
      if (reloadMem[key]?.unresponsiveSince) {
        const { unresponsiveSince, unresponsiveReloadAt, ...rest } = reloadMem[key];
        reloadMem[key] = rest;
      }
      if (staleOwned(key, known)) {
        await closeStaleOwned(tab, key, known, "осиротевший автозапуск не зарегистрирован — освобождаю слот");
        continue;
      }
      const path = chatPath(state.url);
      if (path && Object.entries(handoffs).some(([id, entry]) =>
          id.endsWith(":" + path) && entry.prepared)) {
        status[key] = { title: tab.title, label: known, action: "superseded",
          reason: "чат передан преемнику; новые действия старому UI запрещены" };
        continue;
      }
      // A watched owner-sv tab is bound by our own tab ID and chat ID; the first
      // human message may contain an old f*-agent label. Never let that DOM label
      // reclassify a supervisor tab as an ordinary worker (reload loop).
      const trustedOverseer = isWatchedOwnerTab(tab, path);
      if (trustedOverseer) state.label = "owner-sv";
      if (scopedDispatch && state.label && state.label !== known) continue;
      if (state.label) {
        labels.byTab[key] = { label: state.label, path: path || labels.byTab[key]?.path || "" };
        if (path) labels.byChat[path] = state.label;
      } else {
        const byTab = labels.byTab[key];
        const sameChat = byTab && (!byTab.path || !path || byTab.path === path);
        state.label = (sameChat && byTab.label) || (path && labels.byChat[path]) || ownTabs[key] || "";
        if (state.label && byTab && path && !byTab.path) byTab.path = path;
      }
      // Managed supervisor must remain observable even under Chrome Memory Saver.
      // Non-supervised human tabs are deliberately not modified.
      if (trustedOverseer && tab.autoDiscardable !== false && !cfg.dryRun) {
        await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
        tab.autoDiscardable = false;
      }
      // Вкладка владельца (метка owner…): её не запирают и не ведут — там работает человек
      // со своим агентом-надзирателем (fleet_owner).
      // 23.09.2026: метка pult… — то же самое; слово «owner» в вызове ChatGPT блокировал.
      if (/^(owner|pult)/i.test(state.label || "")) {
        await setTabLock(tab, false, state.label, pinned);
        const sv = /^owner-sv/i.test(state.label || "")
          ? await overseeTab(tab, state).catch((error) => "надзор: ошибка " + String(error).slice(0, 80))
          : /^pult-q\d+(?:-[0-9a-z]+)?$/i.test(state.label || "") && ownTabs[key] === state.label
            ? await quickTab(tab, state).catch((error) => "быстрый агент: ошибка " + String(error).slice(0, 80)) : "";
        // A successful observation establishes the server-side revision
        // baseline. Once discarded, comparison won't mistake old turns for new.
        const watchedId = chatPath(state.url);
        const memo = overseerMem[watchedId];
        if (trustedOverseer && memo?.reported && memo.observedKey === memo.key &&
            !memo.pending && !memo.result &&
            !state.generating && !overseerPending[watchedId] &&
            Date.now() - Number(overseerRevisions[watchedId]?.checkedAt || 0) >= 60_000 &&
            overseerLiveChecks < 1) {
          overseerLiveChecks += 1;
          const updated = await overseerConversationUpdateTime(tab.id, watchedId);
          if (updated) overseerRevisions[watchedId] = { value: updated, checkedAt: Date.now() };
          else overseerRevisions[watchedId] = {
            ...overseerRevisions[watchedId], checkedAt: Date.now(),
          };
        }
        status[key] = { title: tab.title, label: state.label, action: "skip", reason: sv || "вкладка владельца — не трогаю" };
        continue;
      }

      // 13.10: «Не удалось загрузить аккаунт» — аккаунт перегружен. Единственный сигнал лимита:
      // жмём «Продолжить» сами, новые чаты на паузу (растущую), в эту вкладку ничего не шлём.
      if (state.accountLoadFailed) {
        if (!cfg.dryRun) {
          await sendWithTimeout(tab.id, { type: "act", action: "click-account-continue" }, 5000).catch(() => null);
        }
        status[key] = { title: tab.title, label: state.label, action: "skip",
          reason: "аккаунт перегружен — нажал «Продолжить», в вкладку не пишу" };
        continue;
      }

      // URL первого распознанного чата становится постоянной целью вкладки. После ошибки
      // ChatGPT может заменить его страницей проекта или даже случайно созданным новым чатом;
      // такой URL нельзя принимать за новую норму.
      const now = Date.now();
      const remembered = rememberSession(sessions[key], state.url, state.label, now);
      if (remembered) {
        sessions[key] = remembered;
        labels.byTab[key] = { label: remembered.label, path: remembered.id };
        labels.byChat[remembered.id] = remembered.label;
      }
      if (sessions[key]?.url && sessions[key]?.label) {
        knownChats[sessions[key].label] = { url: sessions[key].url, at: now };
      }
      const recovery = state.pageLoadFailed ? {state:null,lost:false,restored:false} : observeSession(
        sessions[key],
        {
          url: state.url || tab.url,
          label: state.label,
          ready: state.sessionReady,
          rateLimited: state.rateLimited,
          blocked: state.blocked,
          loading: tab.status === "loading",
          responded: true,
          composerEmpty: state.composerEmpty,
          generating: state.generating,
          unlockedFor: state.unlockedFor || 0,
        },
        recoveryMem[key],
        now,
        RECOVERY_DEFAULTS,
      );
      if (recovery.restored) {
        await event("session-restored", {
          tabId: tab.id, label: state.label, attempts: recoveryMem[key]?.attempts || 0,
          heldMs: recovery.durationMs,
        });
        await log(`${state.label}: исходная сессия снова стабильна (${Math.round(recovery.durationMs / 1000)} с)`);
        delete recoveryMem[key];
      } else if (recovery.state) {
        recoveryMem[key] = recovery.state;
        if (recovery.lost) {
          await event("session-lost", {
            tabId: tab.id, label: state.label, from: sessions[key]?.url,
            to: state.url || tab.url, rateLimitKind: state.rateLimitKind || "",
          });
          await log(`${state.label}: ChatGPT выбросил вкладку из исходной сессии на ${state.url || tab.url}`);
        }
        const waitMs = Math.max(accountWait(now), Math.max(0, recovery.state.nextAt - now));
        let reason = recovery.settling
          ? `исходная сессия загружена; проверяю устойчивость ${RECOVERY_DEFAULTS.settleSec} с`
          : `выброшена из исходной сессии; возврат${waitMs ? ` через ${Math.ceil(waitMs / 1000)} с` : " сейчас"}`;
        if (recovery.protected) reason = "исходная сессия отличается, но вкладка временно разблокирована — не трогаю";
        status[key] = { title: tab.title, label: state.label, action: "restore", reason };
        if (recovery.canAttempt && !recovery.protected && !cfg.dryRun && !accountWait(now)) {
          await sendWithTimeout(tab.id, { type: "lock", on: false }, 2000).catch(() => null);
          await reserveAccountAction("session-restore-attempt", tab.id, state.label, now);
          recoveryMem[key] = attemptedRecovery(recovery.state, now, RECOVERY_DEFAULTS);
          await chrome.storage.local.set({ recoveryMem });
          await chrome.tabs.update(tab.id, { url: sessions[key].url }).catch(() => {});
          status[key].reason = `возвращаю исходную сессию; попытка ${recoveryMem[key].attempts}`;
          await log(`${state.label}: возвращаю ${sessions[key].url}, попытка ${recoveryMem[key].attempts}`);
        }
        // Пока URL не удержался хотя бы settleSec, не перезагружать, не закрывать и не
        // отправлять «продолжи»: иначе действие попадёт на страницу проекта/случайный чат.
        continue;
      }
      // Своя — открыта автозапуском (ownTabs) или несёт метку автозапуска: память о
      // вкладке теряется при переустановке расширения, а метка остаётся в чате.
      const own = Boolean(ownTabs[key]) || isAutostartLabel(state.label);
      if (own && !ownTabs[key]) ownTabs[key] = state.label;
      state.own = own;
      state.pendingJoin = Boolean(state.label && (launchState.launched || {})[state.label]);
      const worker = byLabel.get(state.label);
      if (state.label) {
        agentLabels.push(state.label);
        const launchInfo = (launchState.launched || {})[state.label];
        // Pending project-page ещё не агентский чат. Не закрываем composer нашей плашкой
        // и не закрепляем вкладку до первого подтверждённого сообщения/реального /c/ URL.
        // startedAt означает лишь, что content-script подтвердил клик/начало отправки.
        // Это ещё не доказательство созданного разговора: новый UI иногда остаётся на /project.
        // Не накрываем такую страницу серой блокировкой — только реальный /c/ URL или fleet_join.
        const awaitingStart = launchInfo?.preclaimed && launchInfo?.needsStart;
        const lockReady = Boolean(chatUrl(state.url) || (worker && !awaitingStart));
        const reply = await setTabLock(tab, lockOn && lockReady, state.label, pinned);
        state.locked = Boolean(reply?.locked);
      }
      const who = `${state.label || "?"}${worker ? `/${worker.id}` : ""}`;

      // Явная transport/UI ошибка с Retry важнее эвристики interstitial.
      // Например "check your connection" раньше ложно считалось OAuth из-за слова connect.
      if (state.label && state.ui?.gate && !state.failed && !state.pageLoadFailed) {
        if (state.ui.gateSensitive) {
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "",
            action: "skip", reason: `чувствительная преграда ChatGPT: ${state.ui.gate}`, locked: state.locked };
          await log(`${who}: чувствительная преграда ChatGPT — ${state.ui.gate}`);
          continue;
        }
        if (!cfg.dryRun) {
          const gate = await sendWithTimeout(tab.id, { type: "act", action: "dismiss-gate" }, 5000)
            .catch((error) => ({ ok: false, detail: String(error) }));
          await log(`${who}: ${gate?.detail || "промежуточная преграда не закрылась"}`);
          if (gate?.ok) {
            const fresh = await probe(tab.id);
            if (fresh) state = { ...state, ...fresh, label: state.label };
          }
        }
      }

      if (state.label) {
        const warning = observeWarning(warnings[key], state.rateLimited);
        if (warning.state) warnings[key] = warning.state; else delete warnings[key];
        if (warning.fresh) {
          const now = Date.now();
          const starts = [1, 2, 5, 10, 30, 60].map((min) =>
            (launchState.history || []).filter((at) => now - at < min * 60000).length);
          const actions = [1, 2, 5, 10, 20, 30, 60].map((min) =>
            (accountActions.history || []).filter((item) => now - item.at < min * 60000).length);
          await event("warning", { tabId: tab.id, label: state.label, kind: state.rateLimitKind || "", starts, actions,
            activeAgentTabs: agentLabels.length });
          await log(`${who}: «слишком много запросов» (${state.rateLimitKind || "неизвестный вид"}); ` +
            `действий за 1/2/5/10/20/30/60 мин: ${actions.join("/")} — остальные работают`);
        }
        if (state.rateLimited && !state.blocked && !cfg.dryRun) {
          const ack = await sendWithTimeout(tab.id, { type: "act", action: "dismiss-rate-limit" }, 3000)
            .catch((error) => ({ ok: false, detail: String(error) }));
          if (ack?.ok) {
            await event("dismissed", { tabId: tab.id, label: state.label, heldMs: Date.now() - warnings[key].since });
            delete warnings[key];
            const fresh = await probe(tab.id);
            if (fresh) state = { ...state, ...fresh, label: state.label };
          }
          if (ack?.ok || warning.fresh) await log(`${who}: ${ack?.detail}`);
        }
        await chrome.storage.local.set({ [warningKey]: warnings });
        if (state.blocked || state.rateLimited) {
          status[key] = { label: state.label, worker: worker?.id, task: worker?.task, action: "skip",
            reason: state.blocked || "окно пока не закрылось; другие вкладки работают" };
          continue;
        }
      }

      // Создание вкладки и первое сообщение разделены сохранённой очередью. Нельзя держать
      // 90-секундный sleep внутри MV3 service worker: Chrome вправе выгрузить его и навсегда
      // оставить вкладку на странице проекта.
      const pendingStart = (launchState.launched || {})[state.label];
      if (pendingStart?.needsStart) {
        const currentChat = chatUrl(state.url);
        if (currentChat) {
          // Сообщение могло уйти перед выгрузкой service worker. URL чата достаточен,
          // чтобы не создать второй чат повторной отправкой стартового текста.
          const startedAt = pendingStart.startedAt || Date.now();
          launchState = queueAccountRecovery(recordStartedChat(launchState, state.label, startedAt), startedAt);
          launchState.launched[state.label] = {
            ...launchState.launched[state.label], needsStart: false, startedAt,
          };
          await event("started-recovered", { tabId: tab.id, label: state.label });
        } else {
          if (!launchesAllowed) {
            status[key] = { title: tab.title, label: state.label, action: "wait",
              reason: "ChatGPT недоступен: новый чат не создаю" };
            continue;
          }
          const startNow = Date.now();
          const waitMs = Math.max(0, (pendingStart.nextStartAt || 0) - startNow,
            (launchState.overloadUntil || 0) - startNow);
          let reason = waitMs
            ? `первое сообщение в очереди; общая пауза ещё ${Math.ceil(waitMs / 1000)} с`
            : "отправляю первое сообщение";
          if (state.generating) reason = "первое сообщение ждёт конца генерации";
          const recoverableNewChatDraft = !state.composerEmpty && !state.composerOwned && !state.lastRole;
          if (recoverableNewChatDraft) reason = "проверяю стартовую заготовку после reinject";
          else if (!state.composerEmpty && !state.composerOwned) reason = "первое сообщение ждёт: в поле ввода есть текст";
          status[key] = { title: tab.title, label: state.label, action: "start", reason, locked: state.locked };
          const startPlace = placeCheck("start", state.url, null, cfg.projectUrl);
          if (!startPlace.ok) {
            status[key].action = "skip";
            status[key].reason = startPlace.reason;
            if (!cfg.dryRun && startPlace.navigate) {
              await chrome.tabs.update(tab.id, { url: startPlace.navigate }).catch(() => {});
            }
            await event("launch-wrong-place", { tabId: tab.id, label: state.label, url: String(state.url || "").slice(0, 120) });
            continue;
          }
          if (!cfg.dryRun && !waitMs && !state.generating && (state.composerEmpty || state.composerOwned || recoverableNewChatDraft)) {
            if (pendingStart.preclaimed) {
              try {
                const fresh = JSON.parse(await callTool(cfg.mcpUrl, "fleet_launch_reserve", {
                  label: state.label, project: pendingStart.reservation.project,
                }, { timeoutMs: 15000 }));
                if (fresh.status !== "reserved") {
                  await callTool(cfg.mcpUrl,"fleet_launch_release",{
                    label:state.label,claim_id:pendingStart.reservation.claim_id,
                  },{timeoutMs:15000}).catch(() => {});
                  await event("launch-cancelled-no-work", { label: state.label, tabId: tab.id });
                  await closeTabKeepBrowser(tab);
                  delete launchState.launched[state.label];
                  delete ownTabs[key];
                  continue;
                }
                pendingStart.reservation = fresh;
              } catch (error) {
                status[key].reason = "старт ждёт подтверждения реальной задачи: " + String(error);
                continue;
              }
            }
            await reserveAccountAction("start-attempt", tab.id, state.label, startNow, false);
            const outcome = await sendWithTimeout(tab.id, {
              type: "act", action: "start", mention: cfg.connector, text: starter(state.label, pendingStart.reservation),
              confirmedStartAgents,
            }, 45000).catch((error) => ({ ok: false, detail: String(error) }));
            await collectNotices(outcome, tab.id, state.label);
            const chat = chatUrl(outcome?.url);
            if (chat) {
              sessions[key] = rememberSession(sessions[key], chat.url, state.label, Date.now());
              labels.byTab[key] = { label: state.label, path: chat.id };
              labels.byChat[chat.id] = state.label;
            }
            if (outcome?.ok) {
              const startedAt = Date.now();
              launchState = queueAccountRecovery(recordStartedChat(launchState, state.label, startedAt), startedAt);
              launchState.launched[state.label] = {
                ...launchState.launched[state.label], needsStart: false, startedAt,
              };
              status[key].reason = "первое сообщение ушло; жду fleet_join";
              await event("started", { tabId: tab.id, label: state.label });
            } else {
              const attempts = (pendingStart.startAttempts || 0) + 1;
              launchState.launched[state.label] = { ...pendingStart, startAttempts: attempts,
                nextStartAt: Date.now() + react.decide.startRetryAfterSec * 1000 };
              scheduleSoon(react.decide.startRetryAfterSec * 1000);
              status[key].reason = `первое сообщение не подтверждено: ${outcome?.detail}; повтор будет через ${react.decide.startRetryAfterSec} с`;
              await event("start-failed", { tabId: tab.id, label: state.label, attempts });
            }
            const afterStart = outcome?.ok ? null : await probe(tab.id);
            const ui = afterStart?.ui;
            const uiNote = ui
              ? `; ui: composer=${ui.composer ? "да" : "нет"}, gate=${ui.gate || "—"}, buttons=[${(ui.buttons || []).join(" | ")}]`
              : "";
            await log(`${state.label}: старт → ${outcome?.ok ? "ок" : "НЕ ВЫШЛО"} (${outcome?.detail})${uiNote}`);
          }
          continue;
        }
      }

      // Завершённая own-tab не должна занимать слот только потому, что сама страница ChatGPT
      // сломалась. Раньше pageLoadFailed уходил в reload/continue раньше closePlan, и idle worker
      // без задачи мог бесконечно перезагружаться, удерживая maxWorking/spareTabs и RAM.
      const closing = closePlan(state, worker, own, launchConfig(cfg));

      // Одноразовая клавиатура (§3 v2): ход принят сервером — вкладка больше не нужна; простой
      // при нехватке памяти — тоже. Замолчавший разговор будит сервер (wake) по адресу из knownChats.
      const current = chatUrl(state.url);
      const kb = keyboardPlan({
        ...state,
        onChat: Boolean(current && sessions[key]?.id === current.id),
        wakePending: Boolean(wakeMem[key]),
        freeMemMB: freeMemNow,
      }, own && !cfg.dryRun, kbMem[key], Date.now(), kbCfg);
      kbMem[key] = kb.mem;
      if (kb.mem.generatingSince && !kb.close) {
        scheduleSoon(Math.max(500, kb.mem.generatingSince + kbCfg.closeGeneratingSec * 1000 - Date.now()));
      }
      if (kb.close) {
        if (sessions[key]?.label) knownChats[sessions[key].label] = { url: sessions[key].url, at: Date.now() };
        status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "", action: "close", reason: kb.reason };
        await setTabLock(tab, false, state.label, pinned);
        await closeTabKeepBrowser(tab);
        live.delete(key);
        delete ownTabs[key];
        delete kbMem[key];
        const ix = agentLabels.indexOf(state.label);
        if (ix >= 0) agentLabels.splice(ix, 1);
        await event("keyboard-close", { tabId: tab.id, label: state.label });
        await log(`${who}: вкладка закрыта — ${kb.reason}`);
        continue;
      }
      // Живой claim сам по себе не причина держать мёртвую вкладку без действий.
      // Сервер продолжит следить за самим разговором; сохраняем URL для wake.
      if (silentClaimTabClose({ state, worker, owned: own && !cfg.dryRun,
          hasChat: Boolean(current && sessions[key]?.id === current.id),
          wakePending: Boolean(wakeMem[key]) })) {
        if (sessions[key]?.label) knownChats[sessions[key].label] = { url: sessions[key].url, at: Date.now() };
        status[key] = { title: tab.title, label: state.label, worker: worker?.id || "",
          task: worker?.task || "", action: "close", reason: "6 минут без MCP и генерации — освобождаю вкладку, claim остаётся серверу" };
        await setTabLock(tab, false, state.label, pinned);
        await closeTabKeepBrowser(tab);
        live.delete(key);
        delete ownTabs[key];
        delete kbMem[key];
        const ix = agentLabels.indexOf(state.label);
        if (ix >= 0) agentLabels.splice(ix, 1);
        await event("stale-claim-tab-close", { tabId: tab.id, label: state.label });
        continue;
      }
      if (closing?.close && state.pageLoadFailed) {
        status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", action: "close", reason: closing.reason };
        if (cfg.dryRun) {
          await log(`[вхолостую] ${who}: закрыл бы — ${closing.reason}`);
        } else {
          await setTabLock(tab, false, state.label, pinned);
          await closeTabKeepBrowser(tab);
          delete ownTabs[key];
          const ix = agentLabels.indexOf(state.label);
          if (ix >= 0) agentLabels.splice(ix, 1);
          await log(`${who}: вкладка закрыта — ${closing.reason}${worker ? " (чат остался в проекте)" : ""}`);
        }
        continue;
      }

      // Явные поломки UI лечим перезагрузкой и НЕ проваливаемся в обычное «продолжи».
      // Помимо надписей ChatGPT бывает состояние: реальный /c/ ещё открыт, worker жив,
      // но composer исчез. В таком состоянии send всё равно невозможен — reload дешевле спама.
      if (state.label) {
        const reloadNow = Date.now();
        const oldReloadMem = reloadMem[key];
        // Failed pages share a paced two-tab lane, not a global hourly stop.
        const oldReloadHistory = state.pageLoadFailed ? [] : (launchState.reloads || []);
        // Один классификатор UI для обычных и быстрых вкладок; нормальный reloadPlan
        // по-прежнему решает выдержку, лимиты попыток и общий navigation lane.
        const pageIssue = classifyPageIssue(state, tab, Boolean(chatUrl(state.url)));
        const composerMissing = Boolean(worker &&
          (pageIssue.kind === "composer-missing" || pageIssue.kind === "cloudflare") &&
          !state.rateLimited && !state.failed);
        const recoveryState = composerMissing ? { ...state, composerMissing: true } : state;
        const api429 = launchState.apiCheck?.[key];
        const own429 = api429?.conv === chatPath(state.url || tab.url) && api429?.status === 429 ? api429 : null;
        const wait429 = conversation429Backoff(own429, reloadNow);
        if (wait429) {
          const reason = (conversation429Unavailable(own429, reloadNow)
            ? "разговор недоступен (429)" : "429 разговора") +
            ": следующая проверка не раньше " + new Date(own429.nextAt).toISOString();
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "",
            task: worker?.task || "", action: "skip", reason };
          continue;
        }
        const plan = reloadPlan(recoveryState, oldReloadMem, oldReloadHistory, reloadNow, react.reload, worker);
        // Ошибочные страницы не исчерпывают часовой бюджет, но делят полосу загрузки.
        const paced = plan.reload && (accountWait(reloadNow) ||
          Math.max(0, (launchState.overloadUntil || 0) - reloadNow));
        reloadMem[key] = paced ? oldReloadMem : plan.mem;
        if(!state.pageLoadFailed) launchState.reloads = paced ? oldReloadHistory : plan.history;
        const explicitRecovery = Boolean(state.interrupted || state.pageLoadFailed || composerMissing);
        if (plan.reason && !plan.reload && (explicitRecovery || hungText(state, worker, react.reload))) {
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "", action: "skip", reason: plan.reason };
          if ((reloadMem[key].since || 0) > Date.now() - 65_000) await log(`${who}: ${plan.reason}`);
          if (explicitRecovery) continue;
        }
        if (plan.reload) {
          const waitMs = accountWait(reloadNow);
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "",
            action: waitMs ? "skip" : "reload",
            reason: waitMs ? `${plan.reason}; общая пауза ещё ${Math.ceil(waitMs / 1000)} с` : plan.reason };
          if (cfg.dryRun) {
            await log(`[вхолостую] ${who}: перезагрузил бы — ${plan.reason}`);
          } else if (waitMs) {
            // План не засчитан: следующий тик снова проверит состояние и общий интервал.
          } else {
            // Сначала снять блокировку: иначе «Покинуть сайт?» задержит перезагрузку.
            await sendWithTimeout(tab.id, { type: "lock", on: false }, 2000).catch(() => null);
            await reserveAccountAction("reload", tab.id, state.label, reloadNow, true);
            await chrome.tabs.reload(tab.id).catch(() => {});
            await log(`${who}: ${plan.reason}`);
          }
          continue;
        }
      }

      // Будильник сервера: отправить его текст, как только чат готов. Идёт раньше обычного
      // «продолжи» — это решение сервера, а не догадка расширения по странице.
      const wakeOrder = wakeMem[key];
      if (wakeOrder) {
        const ageMin = (Date.now() - wakeOrder.at) / 60_000;
        const current = chatUrl(state.url);
        const onChat = Boolean(current && sessions[key]?.id === current.id);
        const readyToSend = onChat && !state.generating && !state.rateLimited && !state.blocked &&
          (state.composerEmpty || state.composerOwned);
        if (state.generating) {
          delete wakeMem[key]; // агент уже работает — будить не нужно
        } else if (readyToSend && !cfg.dryRun) {
          const mode = await taskModeForSend(cfg, worker);
          if (!mode.ok) {
            status[key] = { title: tab.title, label: state.label, action: "wait",
              reason: "режим не подтверждён: " + mode.reason };
            continue;
          }
          await reserveAccountAction("wake-send", tab.id, state.label, Date.now(), false);
          const outcome = await sendWithTimeout(tab.id, {
            type: "act", action: "continue", text: wakeOrder.text, ...mode.payload,
          }, 45000)
            .catch((error) => ({ ok: false, detail: String(error) }));
          await collectNotices(outcome, tab.id, state.label);
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "",
            action: "wake", reason: `будильник сервера: ${outcome?.ok ? "продолжение отправлено" : `не ушло — ${outcome?.detail}`}` };
          await event(outcome?.ok ? "wake-sent" : "wake-failed", { tabId: tab.id, label: state.label });
          await log(`${who}: будильник → ${outcome?.ok ? "ок" : "НЕ ВЫШЛО"} (${outcome?.detail})`);
          if (outcome?.ok || ageMin > 10) delete wakeMem[key];
          continue;
        } else if (ageMin > 10) {
          delete wakeMem[key];
        } else {
          status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", task: worker?.task || "",
            action: "skip", reason: "будильник: жду, пока чат загрузится" };
          continue;
        }
      }

      // DOM fleetDraftLabel alone does not establish ownership. Unsent drafts in
      // owned, expired launch tabs are closed before interface pressure above;
      // never close a human tab merely because the shared project composer has a label.

      const tabState = { ...state, queueHasWork };
      const previousMem = memory[key];
      const result = decide(tabState, worker, previousMem, Date.now(), react.decide);
      if (closing?.close && result.action === "skip") {
        status[key] = { title: tab.title, label: state.label, worker: worker?.id || "", action: "close", reason: closing.reason };
        if (cfg.dryRun) {
          await log(`[вхолостую] ${who}: закрыл бы — ${closing.reason}`);
        } else {
          await setTabLock(tab, false, state.label, pinned);
          await closeTabKeepBrowser(tab);
          delete ownTabs[key];
          const ix = agentLabels.indexOf(state.label);
          if (ix >= 0) agentLabels.splice(ix, 1);
          await log(`${who}: вкладка закрыта — ${closing.reason}${worker ? " (чат остался в проекте)" : ""}`);
        }
        continue;
      }

      memory[key] = result.mem;
      status[key] = {
        title: tab.title,
        label: state.label,
        worker: worker?.id || "",
        task: worker?.task || "",
        locked: state.locked,
        action: result.action,
        // Работа сдана — важнее, когда вкладка закроется, чем почему её не продолжают.
        reason: result.action === "skip" ? closing?.reason || result.reason : result.reason,
        continues: result.mem.continues,
      };
      if (result.action === "skip") continue;

      // Холостой/сломанный чат не должен держать claim до stale-recovery. Это не
      // blocker самой задачи: fleet_finish возвращает её в ready (а этот worker повторно
      // ту же задачу не получит — queue._held_before). Другой worker подхватит её сразу.
      if (result.action === "release-claim") {
        if (cfg.dryRun) {
          await log(`[вхолостую] ${who}: освободил бы claim для handoff — ${result.reason}`);
        } else if (worker?.id) {
          let reply = "";
          try {
            reply = await callTool(cfg.mcpUrl, "fleet_finish", {
              agent: worker.id,
              summary: result.releaseReason || `handoff: ${result.reason}`,
            });
          } catch (error) {
            reply = `Отказано: ${error.message || error}`;
          }
          const ok = Boolean(reply) && !/^\s*Отказано\b/i.test(reply);
          if (ok) {
            memory[key] = result.mem;
            status[key].action = "release";
            status[key].reason = "claim освобождён для другого worker; этот чат возьмёт другую работу";
            await event("auto-release", { tabId: tab.id, label: state.label, worker: worker.id });
            await log(`${who}: claim автоматически освобождён для handoff — ${result.reason}`);
            scheduleSoon(1500);
          } else {
            if (previousMem) memory[key] = previousMem; else delete memory[key];
            status[key].action = "skip";
            status[key].reason = `авто-handoff не подтверждён: ${String(reply).slice(0, 240)}`;
            await event("auto-release-failed", { tabId: tab.id, label: state.label, worker: worker.id });
            scheduleSoon(20_000);
          }
        }
        continue;
      }

      // Второе подряд ожидание ручных прав — уже не повод спамить «продолжи» и не
      // повод держать claim до stale-recovery. Сам extension освобождает blocker через
      // dev-mcp и на следующем тике этот чат сможет взять другую runnable-задачу.
      if (result.action === "block-claim") {
        if (cfg.dryRun) {
          await log(`[вхолостую] ${who}: освободил бы claim — ${result.reason}`);
        } else if (worker?.id) {
          let reply = "";
          try {
            reply = await callTool(cfg.mcpUrl, "fleet_block", {
              agent: worker.id,
              reason: result.blockReason || `blocked: ${result.reason}`,
            });
          } catch (error) {
            reply = `Отказано: ${error.message || error}`;
          }
          const ok = Boolean(reply) && !/^\s*Отказано\b/i.test(reply);
          if (ok) {
            memory[key] = result.mem;
            status[key].action = "block";
            status[key].reason = "claim автоматически освобождён; беру другую работу на следующем тике";
            await event("auto-block", { tabId: tab.id, label: state.label, worker: worker.id });
            await log(`${who}: claim автоматически frozen — ${result.reason}`);
            scheduleSoon(1500);
          } else {
            if (previousMem) memory[key] = previousMem; else delete memory[key];
            status[key].action = "skip";
            status[key].reason = `автоблок не подтверждён: ${String(reply).slice(0, 240)}`;
            await event("auto-block-failed", { tabId: tab.id, label: state.label, worker: worker.id });
            scheduleSoon(20_000);
          }
        }
        continue;
      }

      const sendsRequest = ["continue", "click-continue", "click-retry"].includes(result.action);
      const requestWait = 0; // обычные сообщения не делят глобальную reload/restore-полосу
      if (requestWait) {
        if (previousMem) memory[key] = previousMem; else delete memory[key];
        status[key].action = "skip";
        status[key].reason = `${result.reason}; общая пауза аккаунта ещё ${Math.ceil(requestWait / 1000)} с`;
        status[key].continues = previousMem?.continues || 0;
        continue;
      }
      if (cfg.dryRun) {
        await log(`[вхолостую] ${who}: ${result.action} — ${result.reason}`);
        continue;
      }
      if (result.action === "stop" || result.action === "notify") {
        await log(`${who}: ${result.action} — ${result.reason}`);
        notify(`${who}: ${result.reason}`);
        continue;
      }
      const sendPlace = placeCheck(result.action, state.url, sessions[key], cfg.projectUrl);
      if (!sendPlace.ok) {
        if (previousMem) memory[key] = previousMem; else delete memory[key];
        status[key] = { ...status[key], action: "skip", reason: sendPlace.reason };
        if (sendPlace.navigate) {
          await chrome.tabs.update(tab.id, { url: sendPlace.navigate }).catch(() => {});
        }
        await event("send-wrong-place", { tabId: tab.id, label: state.label,
          url: String(state.url || "").slice(0, 120), action: result.action });
        continue;
      }
      const taskMode = await taskModeForSend(cfg, worker);
      if (!taskMode.ok) {
        status[key] = { ...status[key], action: "wait",
          reason: "режим не подтверждён: " + taskMode.reason };
        if (previousMem) memory[key] = previousMem; else delete memory[key];
        continue;
      }
      // Память о попытке переживает остановку service worker. Неясный исход не означает
      // «не отправлено»: дать вкладке время показать ответ, не дублировать сразу.
      memory[key] = { ...(previousMem || {}), deliveryRetryAt: Date.now() + react.decide.retryGapSec * 1000 };
      await chrome.storage.local.set({ [memKey]: memory });
      await reserveAccountAction("send-attempt", tab.id, state.label, Date.now(), false);
      const outcome = await sendWithTimeout(tab.id, {
        type: "act", action: result.action, text: result.text || continueText(),
        ...taskMode.payload,
      })
        .catch((error) => ({ ok: false, detail: String(error) }));
      const sentAt = Date.now();
      await collectNotices(outcome, tab.id, state.label);
      if (outcome?.ok) {
        memory[key] = result.mem;
        launchState.continuesSent = [...(launchState.continuesSent || []).filter((at) => sentAt - at < 3600_000), sentAt];
        await event("sent", { tabId: tab.id, label: state.label, action: result.action });
      } else {
        status[key].reason = `отправка не подтверждена: ${outcome?.detail}; повтор после проверки`;
        status[key].continues = previousMem?.continues || 0;
        await event("send-failed", { tabId: tab.id, label: state.label });
      }
      await chrome.storage.local.set({ [memKey]: memory });
      await log(`${who}: ${result.action} → ${outcome?.ok ? "ок" : "НЕ ВЫШЛО"} (${outcome?.detail}) — ${result.reason}`);
      if (!outcome?.ok) notify(`${who}: не удалось продолжить — ${outcome?.detail}`);
    }

    let launchNote = scopedDispatch ? "запуск ограничен проверенными быстрыми заданиями" : "автозапуск выключен";
    let justOpened = "";
    if (cfg.enabled && !scopedDispatch) {
      const freeMem = freeMemNow;
      const launchCfg = launchConfig(cfg);
      const beforeLaunch = launchState;
      let plan = launchesAllowed && canOpenInterface()
        ? decideLaunch(projects, agentLabels, workers.filter((w) => !closedIdleLabels.has(w.label)),
            launchState, Date.now(), launchCfg, Math.random, freeMem)
        : { action: "skip", reason: !launchesAllowed ? "gate: chatgpt-reachable=down; launches=0" :
          "интерфейс занят восстановлением или достигнут предел открытых вкладок",
            launch: launchState, failed: [] };
      const launchWait = 0; // tabs.create не является запросом модели и не ждёт global action lane
      // План не является запуском: fleet_expect и tabs.create ещё могут отказать.
      launchState = plan.action === "launch" && !cfg.dryRun
        ? { ...plan.launch, lastLaunchAt: beforeLaunch.lastLaunchAt || 0, nextGapSec: beforeLaunch.nextGapSec }
        : plan.launch;
      const pace = `подтверждённых чатов за 5 ч ${(launchState.history || []).length} (наблюдение, без часового лимита)` +
        (freeMem != null ? `, свободно памяти ${(freeMem / 1024).toFixed(1)} ГБ` : "");
      launchNote = `${plan.action}: ${plan.reason} (${pace})`;
      const failedLabels = [...new Set([...expiredLaunchLabels, ...plan.failed])];
      for (const label of failedLabels) {
        const reservation = stored[launchKey]?.launched?.[label]?.reservation || beforeLaunch.launched?.[label]?.reservation;
        if (reservation && !cfg.dryRun) {
          await callTool(cfg.mcpUrl, "fleet_launch_release", { label, claim_id: reservation.claim_id },
            { timeoutMs: 8000 }).catch(() => null);
        }
        await log(`запуск ${label}: истёк таймаут регистрации fleet_join — reservation освобождён`);
        notify(`Агент ${label} не подключился к флоту за допустимое окно старта — reservation освобождён, зависшая вкладка будет закрыта`);
      }
      if (plan.action === "launch") {
        let target = `${plan.target.project}/${plan.target.tz}`;
        const label = newLabel();
        if (cfg.dryRun) {
          await log(`[вхолостую] открыл бы новую вкладку под ${target}`);
        } else {
          // Только целевой старт закрепляет проект. Буферный агент должен
          // свободно ждать любую следующую задачу на fleet_take.
          let pinnedProject = true;
          let reservation = null;
          if (plan.target) {
            try {
              await expectProject(cfg.mcpUrl, label, plan.target.project);
              reservation = JSON.parse(await callTool(cfg.mcpUrl, "fleet_launch_reserve", {
                label, project: plan.target.project,
              }, { timeoutMs: 15000 }));
              if (reservation.status !== "reserved") throw new Error("свободной задачи уже нет");
              target = `${reservation.project}/${reservation.tz}`;
            } catch (error) {
              pinnedProject = false;
              launchNote = `skip: флот не закрепил проект — ${error.message}`;
              await log(`запуск под ${target} отменён: флот не закрепил проект (${error.message})`);
            }
          }
          if (pinnedProject) {
            await log(`запуск ${label} под ${target}: ${pace}`);
            await reserveAccountAction("new-interface-attempt",null,label,Date.now());
            let opened;
            try {
              opened = await openAgentTab(cfg, label, target, async (tab) => {
                launchState = plan.launch;
                const createdAt = Date.now();
                // tabs.create сам по себе не отправляет запрос модели и не должен занимать
                // глобальную полосу сообщений. Иначе каждый старт гарантированно стоял ещё
                // accountActionGapSec до первого текста.
                await event("created", { tabId: tab.id, label, at: createdAt });
                launchState.launched = { ...launchState.launched, [label]: {
                  at: createdAt, task: plan.target ? target : null,
                  project: reservation.project, park: false, preclaimed: true, reservation, tabId: tab.id,
                  needsStart: true,
                  nextStartAt: createdAt,
                } };
                openedInterfaces++;
                justOpened = String(tab.id);
                ownTabs[justOpened] = label;
                await chrome.storage.local.set({ [launchKey]: launchState, ownTabs });
              });
            } catch (error) {
              if (reservation) await callTool(cfg.mcpUrl, "fleet_launch_release", {
                label, claim_id: reservation.claim_id,
              }, { timeoutMs: 8000 }).catch(() => null);
              await log(`запуск ${label}: ${error.message}`);
            }
            if (opened) {
              await collectNotices(opened, opened.tabId, label);
              scheduleSoon(800);
            }
            // Pending project-page не блокируем. Lock включится на следующем тике
            // только после подтверждённого старта чата / появления worker.
          }
        }
      }
    }

    for (const key of Object.keys(memory)) if (!live.has(key)) delete memory[key];
    for (const key of Object.keys(reloadMem)) if (!live.has(key)) delete reloadMem[key];
    for (const key of Object.keys(sessions)) if (!live.has(key) && key !== justOpened) delete sessions[key];
    for (const key of Object.keys(recoveryMem)) if (!live.has(key) && key !== justOpened) delete recoveryMem[key];
    for (const key of Object.keys(labels.byTab)) if (!live.has(key) && key !== justOpened) delete labels.byTab[key];
    const chats = Object.keys(labels.byChat);
    for (const key of chats.slice(0, Math.max(0, chats.length - 300))) delete labels.byChat[key];
    for (const key of Object.keys(ownTabs)) if (!live.has(key) && key !== justOpened) delete ownTabs[key];
    for (const key of Object.keys(quickTabJobs)) if (!live.has(key)) delete quickTabJobs[key];
    for (const key of Object.keys(pinned)) if (!live.has(key) && key !== justOpened) delete pinned[key];
    for (const key of Object.keys(warnings)) if (!live.has(key)) delete warnings[key];
    for (const key of Object.keys(wakeMem)) {
      const age = Date.now() - (wakeMem[key].at || 0);
      // Свежую запись держим, даже если вкладка ещё не в списке: иначе повтор приказа откроет вторую.
      if ((!live.has(key) && age > 2 * 60_000) || age > 15 * 60_000) delete wakeMem[key];
    }
    for (const token of Object.keys(wakeReports)) {
      if (Date.now() - (wakeReports[token]?.at || 0) > 2 * 3600_000) delete wakeReports[token];
    }
    for (const label of Object.keys(knownChats)) if (Date.now() - (knownChats[label].at || 0) > 72 * 3600_000) delete knownChats[label];
    // Фактическая память и число живых вкладок: только наблюдение, не ограничение.
    // Вставляем в тот же heartbeat: отдельный fleet_extension_report(detail) сбросил бы
    // серию Cloudflare evidence в net_sensor. Здесь page_text сохраняет исходную семантику.
    const freeMemAfter = await freeMemoryMB();
    const memorySnapshot = {
      at: Date.now(), freeMemMB: freeMemAfter, originalFreeMemMB: earlyFreeMemMB,
      liveTabs: liveAgentCount(), thresholdMB: launchCfg.minFreeMemMB,
      lowMemory: memoryLowThisTick, discarded: earlyDiscarded + memoryDiscarded.size,
      eligible: memoryGuard.eligible, reason: memoryGuard.reason,
    };
    await chrome.storage.local.set({ fleetMemoryState: memorySnapshot });
    // Генерация UI в позднем снимке дополняет быстрых агентов, а не стирает число тревог.
    const finalQuick = quickList.ok ? quickOverview(orders,
      interfaceRows.filter((r) => !pressureClosed.has(r.id) && r.page?.generating).map((r) => r.label),
      tabs.filter((t) => !t.discarded && t.status !== "unloaded" && !pressureClosed.has(t.id))
        .map((t) => ownTabs[String(t.id)] || ""),
      tabs.filter((t) => !pressureClosed.has(t.id) &&
        classifyPageIssue(probes.get(t.id), t, Boolean(chatUrl(t.url))).kind)
        .map((t) => ownTabs[String(t.id)] || "")) : null;
    // 17.11: «открыто вкладок» считало только вкладки быстрых агентов из ownTabs. Реальные
    // вкладки ChatGPT этого Chrome: флота (с меткой агента/быстрого) и остальные (владельца).
    const liveChatTabs = tabs.filter((t) => !pressureClosed.has(t.id) && /chatgpt\.com/.test(t.url || ""));
    const fleetTabCount = liveChatTabs.filter((t) => ownTabs[String(t.id)] ||
      status[String(t.id)]?.label).length;
    const tabCounts = { fleet: fleetTabCount, owner: liveChatTabs.length - fleetTabCount,
      loaded: liveChatTabs.filter((t) => !t.discarded && t.status !== "unloaded").length, at: Date.now() };
    await chrome.storage.local.set({
      tabCounts,
      knownChats,
      wakeMem,
      kbMem: Object.fromEntries(Object.entries(kbMem).filter(([k, v]) => live.has(k) && v && Object.keys(v).length)),
      fleetWorkers: workers.filter((w) => w.task).map((w) => ({ id: w.id, label: w.label, task: w.task, silenceMin: w.silenceMin })),
      fleetQuickStates: finalQuick,
      quickIdleSince,
      quickOpenAttempts,
      quickTabJobs,
      quickErrorRetries,
      quickOrphanSince,
      overseerPending,
      overseerReloadAt,
      overseerRevisions,
      fleetTabHealth: tabHealth(interfaceRows, workers, pressureClosed),
      fleetTabHealthAt: Date.now(),
      // Единый серверный счётчик TZ-310; локальная классификация workers здесь не нужна.
      fleetAgentStates: fleet.agentStates ?? null,
      pinnedByUs: pinned,
      [memKey]: memory,
      reloadMem,
      sessions,
      recoveryMem,
      accountActions,
      labels,
      [launchKey]: launchState,
      ownTabs,
      status,
      ...(Array.isArray(projects) ? { fleetProjects: projects } : {}),
      fleetAttention: attention || [],
      attentionSeen: nextSeen,
      cooldownNote: "",
      tickError: "",
      [warningKey]: warnings,
      telemetrySummary: summarizeEvents(events),
      launchNote,
      lastTick: `${new Date().toLocaleTimeString("ru-RU")} (${reason})`,
      lastTickAt: Date.now(),
    });
    await showFleetBadge(fleet.agentStates, finalQuick,
      (attention?.length || 0) + (fleet.ownerChannel?.attention || 0));
    // Only after a successful tick. An explicit healthy composer observation
    // resets the server-side streak; an empty detail is heartbeat-only.
    const evidence = composerFailure || (composerHealthy ? { detail: "composer ok", page_text: "" } : {});
    const memoryPayload = {
      free_mb: memorySnapshot.freeMemMB, live_tabs: memorySnapshot.liveTabs,
      threshold_mb: memorySnapshot.thresholdMB, discarded: memorySnapshot.discarded,
      status: memorySnapshot.lowMemory ? "low_memory" : "ok",
    };
    const heartbeatDetail = [evidence.detail, "memory_guard:" + JSON.stringify(memoryPayload)]
      .filter(Boolean).join("; ").slice(0, 500);
    const pending = Object.values(launchState.launched || {}).filter((item) => item?.needsStart).length;
    // Один account_event в heartbeat; отправленные события подтверждаем и удаляем,
    // неподтверждённые переживают перезапуск service worker в chrome.storage.
    const freshEvents = (launchState.accountEvents || []).filter(
      (item) => Number.isSafeInteger(item?.at) && Date.now() - item.at < 47 * 3600_000);
    const accountEvent = freshEvents[0];
    const quickGenerating = [...new Set(interfaceRows
      .filter((r) => !pressureClosed.has(r.id) && /^pult-q\d+/i.test(r.label) && r.page?.generating)
      .map((r) => r.label))].slice(0, 32);
    // 19.11: сторож живости быстрых агентов (liveness.js): есть работа, но нет ни отправок,
    // ни ответов, ни идущего хода — лестница diagnose → clear-local → free-slots → restart-worker.
    {
      const demandJobs = quickList.ok ? orders.filter((job) => !job.close && (job.send || job.wake)) : [];
      const progressed = quickGenerating.length > 0 ||
        [...tickEventTypes].some((type) => LIVENESS_PROGRESS_EVENTS.has(type));
      const hold = cfg.dryRun ? "dryRun" : !quickList.ok ? "нет списка приказов" :
        !launchesAllowed ? "запуски запрещены сервером" :
        (launchState.overloadUntil || 0) > Date.now() ? "пауза перегрузки аккаунта" : "";
      const { quickLiveness: previousLiveness = {} } = await chrome.storage.local.get("quickLiveness");
      const watch = livenessPlan(previousLiveness || {}, { demand: demandJobs.length, progressed, hold }, Date.now());
      const summary = blockedSummary(quickOpenBlocked);
      if (watch.action) {
        const minutes = Math.round((watch.stalledMs || 0) / 60_000);
        await log(`сторож живости: ${demandJobs.length} заданий ждут ${minutes} мин без отправок и ответов; ` +
          `ступень ${watch.action}; причины: ${summary || "не названы"}`);
        await event("liveness-" + watch.action, { waiting: demandJobs.length, minutes });
      }
      if (watch.action === "clear-local") {
        if (scopedDispatch) await retireDispatchScope("сторож живости: затор при открытом окне");
        for (const id of Object.keys(quickOpenAttempts)) delete quickOpenAttempts[id];
        const paused = Object.keys(quickSendFailures);
        for (const id of paused) delete quickSendFailures[id];
        for (const memo of Object.values(quickMem)) { memo.failures = 0; memo.failureReason = ""; memo.sentAt = 0; }
        await chrome.storage.local.set({ quickOpenAttempts, quickSendFailures, interfaceTrialAt: 0 });
        await withTimeout(Promise.all(paused.map((id) => quickReport(cfg, { id, send_paused: false,
          send_pause_until: "", send_pause_reason: "", send_failures: 0 }).catch(() => null))), 3000, "").catch(() => {});
      }
      if (watch.action === "free-slots") {
        for (const tab of tabs) {
          const key = String(tab.id);
          if (pressureClosed.has(tab.id) || !/^pult-q\d+/i.test(ownTabs[key] || "")) continue;
          if (probes.get(tab.id)?.generating) continue;
          const jobId = quickTabJobs[key];
          await closeTabKeepBrowser(tab);
          pressureClosed.add(tab.id);
          live.delete(key); delete ownTabs[key]; delete labels.byTab[key]; delete quickTabJobs[key];
          if (jobId) await quickReport(cfg, { id: jobId, tab_closed: true, generating: false }).catch(() => null);
          await log(`сторож живости: ${jobId || key} — вкладка закрыта, слот освобождён (чат откроется заново)`);
        }
        await chrome.storage.local.set({ ownTabs, labels, quickTabJobs });
      }
      await chrome.storage.local.set({ quickLiveness: { ...watch.state, demand: demandJobs.length,
        summary, at: Date.now() } });
      if (watch.action === "restart-worker") restartWorkerAfterTick = true;
    }
    if (quickGenerating.length) await reportExtension(cfg.mcpUrl, {
      detail: "quick_generating:" + JSON.stringify(quickGenerating),
    }).catch((error) => log("телеметрия quick UI недоступна: " + String(error).slice(0, 100)));
    const reported = await reportExtension(cfg.mcpUrl, {
      version: extensionVersion, tabs: tabs.length, pending, enabled: true,
      tick_completed: true,
      runnable: Array.isArray(fleet.projects) ? fleet.projects.reduce(
        (total, project) => total + (Number.isSafeInteger(project.count) && project.count >= 0
          ? project.count : 0), 0) : null,
      ...evidence, detail: heartbeatDetail,
      ...(accountEvent ? { account_event: JSON.stringify(accountEvent) } : {}),
    }).then(() => true).catch(async (error) => {
      await log("телеметрия расширения недоступна: " + String(error).slice(0, 120));
      return false;
    });
    launchState.accountEvents = reported && accountEvent ? freshEvents.slice(1) : freshEvents;
    await chrome.storage.local.set({ [launchKey]: launchState });
    if (restartWorkerAfterTick) {
      await log("сторож живости: перезапускаю service worker расширения");
      chrome.runtime.reload();
    }
  } catch (error) {
    runtimeHealth.error = String(error.message || error);
    if (/FILE_ERROR_NO_SPACE|IO error:/i.test(runtimeHealth.error)) runtimeHealth.storageError = runtimeHealth.error;
    try {
      await log(`ошибка цикла: ${error.message}`);
      await chrome.storage.local.set({ tickError: runtimeHealth.error, lastTick: new Date().toISOString() });
    } catch (persistError) {
      runtimeHealth.storageError = String(persistError.message || persistError);
    }
  } finally {
    // A failed query, disabled mode or intentional pause is a completed cycle,
    // not a successful fleet snapshot and not a hung worker. Keep both clocks.
    try {
      await chrome.storage.local.set({ lastCycleFinishedAt: Date.now() });
    } catch (persistError) {
      runtimeHealth.storageError = String(persistError.message || persistError);
    } finally {
      if (generation === tickGeneration) {
        phaseMark("end");
        chrome.storage.local.set({ lastCyclePhases: runtimeHealth.phases }).catch(() => {});
        runtimeHealth.cycleFinishedAt = Date.now();
        running = false;
        if (Number.isFinite(nextDispatchAt)) {
          const delay = nextDispatchAt - Date.now();
          nextDispatchAt = Infinity;
          scheduleSoon(delay);
        }
      }
    }
  }
}

// Раз в 30 секунд (минимум chrome.alarms с Chrome 120; старее — сам округлит до минуты):
// сорвавшийся ход и обрыв ответа замечаются вдвое быстрее, чем раз в минуту.
function schedule() {
  chrome.alarms.create("tick", { periodInMinutes: 0.5 });
}

function scheduleSoon(ms = 1000) {
  const due = Date.now() + Math.max(500, ms);
  if (running) nextDispatchAt = Math.min(nextDispatchAt, due);
  else chrome.alarms.create("tick-soon", { when: due });
}

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(() => {
  // 24.11: начало восстановления сессии Chrome — надзор не открывает дубли (overseer-tabs.js).
  chrome.storage.local.set({ browserStartedAt: Date.now() }).catch(() => {});
  schedule();
});
// Будильник с прежним периодом (1 мин) переживает обновление расширения — переставить.
chrome.alarms.get("tick").then((alarm) => {
  if (!alarm || alarm.periodInMinutes !== 0.5) schedule();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "tick" || alarm.name === "tick-soon") tick(alarm.name);
});
// Пауза после «Unusual activity»: 20 мин, повтор в пределах 6 ч — вдвое дольше, не больше 2 ч.
const HALT_STREAK_WINDOW_MS = 6 * 3600 * 1000;
const HALT_GRACE_MS = 3 * 60 * 1000;
function haltPauseMs(count) {
  return Math.min(20 * 2 ** Math.max(0, count - 1), 120) * 60 * 1000;
}
function haltAutoResumable(halt) {
  return String(halt?.reason || "").startsWith("ChatGPT:");
}
function haltResumeAt(halt) {
  return Number(halt?.resumeAt) || (Number(halt?.at) || 0) + haltPauseMs(1);
}

async function reportBrowserControl(url, request) {
  const extensionVersion = chrome.runtime?.getManifest?.()?.version || "";
  await reportExtension(url, { version: extensionVersion,
    detail: "browser_control:" + JSON.stringify({ ...request, by: "extension" }) });
  return true;
}

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message?.type === "fleet-sleep") {
    // Таймер для скрытой страницы (content.js sleep): у service worker нет сдерживания вкладок.
    setTimeout(() => reply({ ok: true }), Math.max(0, Math.min(Number(message.ms) || 0, 60000)));
    return true;
  }
  if (message?.type === "runtime-health") {
    reply({ ...runtimeHealth, running });
    return false;
  }
  if (message?.type === "export-session") {
    exportSession(Number(message.tabId))
      .then((built) => reply({ ok: true, text: built.text, filename: built.filename, meta: built.meta }))
      .catch((error) => reply({ ok: false, detail: String(error?.message || error).slice(0, 200) }));
    return true;
  }
  if (message?.type === "overseer-state" || message?.type === "overseer-mark") {
    (async () => {
      const cfg = await settings();
      try {
        if (message.type === "overseer-mark") {
          await callTool(cfg.mcpUrl, "fleet_overseer_mark", { url: String(message.url || ""), mode: String(message.mode || "off") },
            { timeoutMs: 10000 });
          overseerList.at = 0;
          await log(`надзор: чат ${chatPath(message.url).slice(0, 8)} → ${message.mode}`);
        }
        const chats = await overseerChats(cfg, { force: true, strict: true });
        reply({ ok: true, mode: chats.get(chatPath(message.url))?.mode || "off" });
      } catch (error) {
        reply({ ok: false, detail: String(error).slice(0, 200) });
      }
    })();
    return true;
  }
  if (message?.type === "browser-control") {
    const operation = browserControlWrite.then(async () => {
      const cfg = await settings();
      const request = {};
      if (typeof message.autostart === "boolean") request.autostart = message.autostart;
      if (message.resume) {
        request.halt = null;
        await chrome.storage.local.remove("browserHalt");
        await log("аварийная остановка снята владельцем");
      }
      browserControlPending = true;
      try {
        await reportBrowserControl(cfg.mcpUrl, request);
        if (typeof request.autostart === "boolean") {
          // Подтверждение report само по себе не равно применению browser-control.json.
          // Сверяем с fleet_list и только после совпадения меняем локальный кэш.
          let confirmed = null;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            const snapshot = await fleetState(cfg.mcpUrl, null);
            if (snapshot.browser?.known && snapshot.browser.autostart === request.autostart) {
              confirmed = snapshot.browser.autostart;
              break;
            }
            if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 500));
          }
          if (confirmed === null) throw new Error("сервер не подтвердил новое состояние автозапуска");
          await chrome.storage.local.set({ browserAutostart: confirmed });
          return { ok: true, autostart: confirmed };
        }
        return { ok: true };
      } catch (error) {
        return { ok: false, detail: String(error).slice(0, 180) };
      } finally {
        browserControlPending = false;
      }
    });
    browserControlWrite = operation.then(() => {}, () => {});
    operation.then(reply, (error) => reply({ ok: false, detail: String(error) }));
    return true;
  }
  if (message?.type === "tick-now") {
    tick("вручную").then(() => reply({ ok: true }));
    return true;
  }
  return false;
});
