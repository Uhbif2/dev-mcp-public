// Фон расширения: раз в 30 секунд — флот, вкладки, решения, действия.
//
// MV3 service worker живёт секунды, поэтому ритм держит chrome.alarms, а вся
// память между тиками лежит в chrome.storage (память вкладок, запуски, лог,
// статус). Без ответа флота ничего не делаем: продолжать или открывать вкладки
// вслепую — значит работать поверх уже сданного.

import { DEFAULT_MCP_URL, callTool, chatLines, expectProject, fleetState, reportExtension } from "./fleet.js";
import { deliverOwnerNotices, installOwnerNoticeClicks, recordServerAvailability } from "./owner-notices.js";
import { interfacePlan, INTERFACE_DEFAULTS } from "./interface-pressure.js";
import {
  DEFAULTS,
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
import {
  RECOVERY_DEFAULTS,
  accountOverloaded,
  accountRecovered,
  placeCheck,
  accountActionWait,
  attemptedRecovery,
  chatUrl,
  legacySession,
  markAccountAction,
  observeSession,
  projectPage,
  rememberSession,
} from "./recovery.js";

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
async function sendWithTimeout(tabId, message, ms = 25000) {
  let timer;
  try {
    return await Promise.race([
      chrome.tabs.sendMessage(tabId, message),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`вкладка не ответила за ${Math.round(ms / 1000)} с`)), ms); }),
    ]);
  } finally { clearTimeout(timer); }
}

// Путь чата (/c/<id>) из адреса вкладки: по нему вспоминается метка, когда первое
// сообщение ушло из страницы.
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
    maxTabs: num(cfg.maxTabs, LAUNCH_DEFAULTS.maxTabs, 1, 8),
  };
}

// Первый подтверждённый чат после перегрузки завершает паузу и ставит
// событие восстановления в надёжную очередь отчётности (storage + retry).
function queueAccountRecovery(launch, at) {
  const overloadAt = (launch.overloads || []).at(-1);
  if (!launch.overloadLevel || !overloadAt) return launch;
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

async function probe(tabId) {
  try {
    return await sendWithTimeout(tabId, { type: "probe" }, 5000);
  } catch {
    // После reload unpacked-extension старая isolated world может остаться в странице с
    // мёртвым runtime-port. Таймаут тоже повод реинжектить текущую версию, а не только
    // явная ошибка "Receiving end does not exist".
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["mode.js", "content.js"] });
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
  const [previousActive] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }).catch(() => []);
  const tab = await chrome.tabs.create({ url: cfg.projectUrl, active: false });
  await onCreated(tab); // сохранить владение ДО ожиданий: MV3 может завершить service worker
  await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});

  // Chrome 154 иногда не запускает renderer/content-script у новой background-tab под
  // давлением памяти. Сначала даём ей загрузиться в фоне; если probe всё ещё мёртв —
  // кратко активируем, чтобы гарантированно разморозить renderer, и возвращаем фокус.
  let ready = null;
  for (let i = 0; i < 8 && !ready; i += 1) {
    if (i) await new Promise((resolve) => setTimeout(resolve, 350));
    ready = await probe(tab.id);
  }
  if (!ready) {
    await chrome.tabs.update(tab.id, { active: true, autoDiscardable: false }).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 700));
    ready = await probe(tab.id);
    if (previousActive?.id && previousActive.id !== tab.id) {
      await chrome.tabs.update(previousActive.id, { active: true }).catch(() => {});
    }
  }

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

// UI recovery must remain available when the fleet's control API is slow/down.
// This path never sends messages or transfers claims using a cached snapshot.
async function recoverLocalInterfaces(tabs, cfg) {
  const saved=await chrome.storage.local.get(['ownTabs','labels','sessions','fleetWorkers',
    'interfaceQueue','reloadMem','accountActions','status','knownChats']);
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

async function tick(reason = "alarm") {
  if (running) return;
  running = true;
  try {
    const cfg = await settings();
    const extensionVersion = chrome.runtime?.getManifest?.()?.version || "";
    let tabs = await chrome.tabs.query({ url: "https://chatgpt.com/*" });
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
        if (page?.networkSuspicious) await chrome.tabs.reload(tab.id).catch(() => {});
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
        await chrome.storage.local.set({
          fleetUpdatedAt: Date.now(), ...(Array.isArray(fleet.projects) ? { fleetProjects: fleet.projects } : {}),
          fleetAgentStates: fleet.agentStates ?? null, fleetAttention: fleet.attention || [],
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
    if (!cfg.enabled) {
      for (const tab of tabs) await setTabLock(tab, false, "", pinned);
      await chrome.storage.local.set({ pinnedByUs: pinned });
      await reportExtension(cfg.mcpUrl, { version: extensionVersion, tabs: tabs.length, pending: 0, enabled: false })
        .catch(() => {}); // Disabled extension still has a heartbeat.
      return;
    }
    for (const tab of tabs) {
      if (tab.autoDiscardable !== false) {
        chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
      }
    }

    // Адреса чатов агентов — серверу: будить агента в ЕГО чате решает сервер (fleet/wake.py),
    // и ему нужен адрес даже после того, как вкладку закрыли.
    const { knownChats = {}, wakeMem = {} } = await chrome.storage.local.get(["knownChats", "wakeMem"]);
    const { wakeReports = {} } = await chrome.storage.local.get("wakeReports");
    const {fleetRefresh:previousRefresh} = await chrome.storage.local.get('fleetRefresh');
    if(previousRefresh?.error) {
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
      await chrome.storage.local.set({
        fleetRefresh: { startedAt: fleetStartedAt, finishedAt: Date.now(), error: String(error.message) },
        tickError: `Сервер флота не ответил: ${error.message}. Повторная попытка через 30 с.`,
      });
      if (!cfg.dryRun) await recordServerAvailability(false).catch(()=>{});
      await recoverLocalInterfaces(tabs,cfg);
      await log(`флот недоступен (${error.message}) — только локальное восстановление интерфейса`);
      return;
    }
    const { workers, projects, attention } = fleet;
    if (fleet.browser?.halt) {
      await chrome.storage.local.set({ browserHalt: { reason: fleet.browser.halt, at: Date.now(),
        source: "server", reported: true } });
      await log(`аварийная остановка с сервера: ${fleet.browser.halt}`);
      return;
    }
    await chrome.storage.local.set({ browserAutostart: fleet.browser?.autostart !== false });
    // Publish a completed query before browser/research work. A later failure
    // cannot hide fresh fleet data behind the timestamp of the whole tick.
    await chrome.storage.local.set({
      // Неполный ответ (нет строки очереди) не стирает проекты: иначе окно пишет «нет во флоте».
      fleetUpdatedAt: Date.now(), ...(Array.isArray(projects) ? { fleetProjects: projects } : {}),
      fleetAgentStates: fleet.agentStates ?? null, fleetAttention: attention || [],
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
      await chrome.action.setBadgeText({text:fleet.ownerChannel?.attention ? String(fleet.ownerChannel.attention) : ""}).catch(()=>{});
    }
    await researchTick(cfg.mcpUrl, { enabled: cfg.enabled, dryRun: cfg.dryRun });
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
      warningKey, "sessions", "recoveryMem", "accountActions", "kbMem", "interfaceQueue",
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
    const recoveryMem = stored.recoveryMem || {};
    const kbMem = stored.kbMem || {};
    let accountActions = stored.accountActions || { lastActionAt: 0, history: [] };
    const accountWait = (now = Date.now()) => accountActionWait(accountActions, now, RECOVERY_DEFAULTS);
    const reserveAccountAction = async (type, tabId, label, now = Date.now(), throttle = true) => {
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
    const prunedAtStart = pruneLaunched(launchState.launched || {}, workers, Date.now(), launchCfg);
    const expiredLaunchLabels = prunedAtStart.failed;
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
    const probes = new Map(await Promise.all(tabs.map(async (tab) => [tab.id, tab.discarded ? null : await probe(tab.id)])));
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
    const interfaceRows=tabs.map(t=>{
      const label=ownTabs[t.id]||labels.byTab[t.id]?.label||probes.get(t.id)?.label||"";
      return {id:t.id,label,own:!!ownTabs[t.id]||isAutostartLabel(label),page:probes.get(t.id),
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
    const pressureClosed=new Set();
    if (!cfg.dryRun) {
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
    const interfaceRoom=()=>tabs.filter(t=>!pressureClosed.has(t.id)).length+openedInterfaces<launchCfg.maxTabs;
    const canOpenInterface=()=>interfaceRoom() && !accountWait() &&
      (freeMemNow==null||freeMemNow>=launchCfg.minFreeMemMB);
    for (const page of probes.values()) {
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
    for (const order of fleet.wake || []) {
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
      if (pressure.degraded || !canOpenInterface()) continue;
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

    if (pressure.next && pressure.failed<INTERFACE_DEFAULTS.failedTabs && launchesAllowed &&
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

    for (const tab of orderedTabs) {
      if(pressureClosed.has(tab.id)) continue;
      const key = String(tab.id);
      live.add(key);
      const known = labels.byTab[key]?.label || ownTabs[key] || "";
      if (!sessions[key]) sessions[key] = legacySession(labels.byTab[key], cfg.projectUrl);
      // Выгруженная браузером вкладка (Memory Saver) не работает вовсе — вернуть её, если она
      // агентская. Замороженная (tab.frozen, Chrome 132+) не отвечает на probe.
      if (tab.discarded) {
        if (staleOwned(key, known)) {
          await closeStaleOwned(tab, key, known, "осиротевший автозапуск без fleet_join — освобождаю слот");
          continue;
        }
        if (known && !/^(owner|pult)/i.test(known)) agentLabels.push(known);
        status[key] = { title: tab.title, label: known, note: "выгружена браузером" };
        if (known && !/^(owner|pult)/i.test(known) && !cfg.dryRun) {
          const waitMs = accountWait();
          if (waitMs) {
            status[key].note = `выгружена браузером; общая пауза ещё ${Math.ceil(waitMs / 1000)} с`;
          } else {
            await reserveAccountAction("reload", tab.id, known);
            await chrome.tabs.reload(tab.id).catch(() => {});
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
        if (ownTabs[key] && knownWorker && !knownWorker.task &&
            unresponsiveSec >= launchCfg.closeIdleSec) {
          closedIdleLabels.add(known);
          await closeStaleOwned(
            tab, key, known,
            `зарегистрированная вкладка без задачи не отвечает ${Math.round(unresponsiveSec)}с — освобождаю слот`,
          );
          continue;
        }

        if (known && !/^(owner|pult)/i.test(known)) agentLabels.push(known);
        const lastRecoveryReloadAt = Number(
          oldUnresponsive.unresponsiveReloadAt || oldUnresponsive.frozenReloadAt || 0,
        );
        const recoveryReason = tab.frozen
          ? "вкладка заморожена Chrome"
          : "content-script вкладки не отвечает";
        const canReload = Boolean(
          ownTabs[key] && knownWorker && unresponsiveSec >= (tab.frozen ? 0 : 10) &&
          frozenReloads < 2 && !accountWait(now) && now - lastRecoveryReloadAt >= 60_000
        );
        if ((tab.frozen || knownWorker) && known && !/^(owner|pult)/i.test(known)) {
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
      if (state.label) {
        labels.byTab[key] = { label: state.label, path: path || labels.byTab[key]?.path || "" };
        if (path) labels.byChat[path] = state.label;
      } else {
        const byTab = labels.byTab[key];
        const sameChat = byTab && (!byTab.path || !path || byTab.path === path);
        state.label = (sameChat && byTab.label) || (path && labels.byChat[path]) || ownTabs[key] || "";
        if (state.label && byTab && path && !byTab.path) byTab.path = path;
      }
      // Вкладка владельца (метка owner…): её не запирают и не ведут — там работает человек
      // со своим агентом-надзирателем (fleet_owner).
      // 23.09.2026: метка pult… — то же самое; слово «owner» в вызове ChatGPT блокировал.
      if (/^(owner|pult)/i.test(state.label || "")) {
        await setTabLock(tab, false, state.label, pinned);
        status[key] = { title: tab.title, label: state.label, action: "skip", reason: "вкладка владельца — не трогаю" };
        continue;
      }

      // 13.10: «Не удалось загрузить аккаунт» — аккаунт перегружен. Единственный сигнал лимита:
      // жмём «Продолжить» сами, новые чаты на паузу (растущую), в эту вкладку ничего не шлём.
      if (state.accountLoadFailed) {
        const overloadNow = Date.now();
        const lastOverload = (launchState.overloads || []).slice(-1)[0] || 0;
        if (overloadNow - lastOverload > 3 * 60_000) {
          launchState = accountOverloaded(launchState, overloadNow);
          const counts = (hours) => (launchState.history || []).filter(
            (at) => at <= overloadNow && overloadNow - at < hours * 3600_000).length;
          launchState.accountEvents = [...(launchState.accountEvents || []), {
            type: "overload", at: overloadNow,
            chats_1h: counts(1), chats_3h: counts(3), chats_5h: counts(5),
            pause_min: Math.round((launchState.overloadUntil - overloadNow) / 60_000),
          }].slice(-72);
          await event("account-overload", { tabId: tab.id, label: state.label || "",
            pauseMin: Math.round((launchState.overloadUntil - overloadNow) / 60_000) });
          await log(`аккаунт перегружен («${state.accountLoadFailed.slice(0, 60)}») — новые чаты на паузе до ${new Date(launchState.overloadUntil).toLocaleTimeString()}`);
        }
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
          const waitMs = Math.max(0, (pendingStart.nextStartAt || 0) - startNow);
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
        const composerMissing = Boolean(worker && chatUrl(state.url) && state.composerPresent === false &&
          !state.generating && !state.blocked && !state.rateLimited && !state.failed);
        const recoveryState = composerMissing ? { ...state, composerMissing: true } : state;
        const plan = reloadPlan(recoveryState, oldReloadMem, oldReloadHistory, reloadNow, react.reload, worker);
        // Ошибочные страницы не исчерпывают часовой бюджет, но делят полосу загрузки.
        const paced = plan.reload && accountWait(reloadNow);
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

    let launchNote = "автозапуск выключен";
    let justOpened = "";
    if (cfg.enabled) {
      const freeMem = freeMemNow;
      const launchCfg = launchConfig(cfg);
      const beforeLaunch = launchState;
      let plan = launchesAllowed && !pressure.degraded && canOpenInterface()
        ? decideLaunch(projects, agentLabels, workers.filter((w) => !closedIdleLabels.has(w.label)),
            launchState, Date.now(), launchCfg, Math.random, freeMem)
        : { action: "skip", reason: !launchesAllowed ? "gate: chatgpt-reachable=down; launches=0" : "интерфейс занят восстановлением или достигнут предел открытых вкладок",
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
    const attentionCount=(attention?.length||0)+(fleet.ownerChannel?.attention||0);
    const badge = attentionCount ? String(attentionCount) : "";
    chrome.action.setBadgeText({ text: badge }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ color: attentionCount ? "#c62828" : "#6b6b70" }).catch(() => {});
    await chrome.storage.local.set({
      knownChats,
      wakeMem,
      kbMem: Object.fromEntries(Object.entries(kbMem).filter(([k, v]) => live.has(k) && v && Object.keys(v).length)),
      fleetWorkers: workers.filter((w) => w.task).map((w) => ({ id: w.id, label: w.label, task: w.task, silenceMin: w.silenceMin })),
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
    // Only after a successful tick. An explicit healthy composer observation
    // resets the server-side streak; an empty detail is heartbeat-only.
    const evidence = composerFailure || (composerHealthy ? { detail: "composer ok", page_text: "" } : {});
    const pending = Object.values(launchState.launched || {}).filter((item) => item?.needsStart).length;
    // Один account_event в heartbeat; отправленные события подтверждаем и удаляем,
    // неподтверждённые переживают перезапуск service worker в chrome.storage.
    const freshEvents = (launchState.accountEvents || []).filter(
      (item) => Number.isSafeInteger(item?.at) && Date.now() - item.at < 47 * 3600_000);
    const accountEvent = freshEvents[0];
    const reported = await reportExtension(cfg.mcpUrl, {
      version: extensionVersion, tabs: tabs.length, pending, enabled: true,
      tick_completed: true,
      runnable: Array.isArray(fleet.projects) ? fleet.projects.reduce(
        (total, project) => total + (Number.isSafeInteger(project.count) && project.count >= 0
          ? project.count : 0), 0) : null,
      ...evidence,
      ...(accountEvent ? { account_event: JSON.stringify(accountEvent) } : {}),
    }).then(() => true).catch(async (error) => {
      await log("телеметрия расширения недоступна: " + String(error).slice(0, 120));
      return false;
    });
    launchState.accountEvents = reported && accountEvent ? freshEvents.slice(1) : freshEvents;
    await chrome.storage.local.set({ [launchKey]: launchState });
  } catch (error) {
    await log(`ошибка цикла: ${error.message}`);
    await chrome.storage.local.set({ tickError: String(error.message), lastTick: new Date().toISOString() });
  } finally {
    running = false;
  }
}

// Раз в 30 секунд (минимум chrome.alarms с Chrome 120; старее — сам округлит до минуты):
// сорвавшийся ход и обрыв ответа замечаются вдвое быстрее, чем раз в минуту.
function schedule() {
  chrome.alarms.create("tick", { periodInMinutes: 0.5 });
}

function scheduleSoon(ms = 1000) {
  chrome.alarms.create("tick-soon", { when: Date.now() + Math.max(500, ms) });
}

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
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
  if (message?.type === "browser-control") {
    (async () => {
      const cfg = await settings();
      const request = {};
      if (typeof message.autostart === "boolean") request.autostart = message.autostart;
      if (message.resume) {
        request.halt = null;
        await chrome.storage.local.remove("browserHalt");
        await log("аварийная остановка снята владельцем");
      }
      const ok = await reportBrowserControl(cfg.mcpUrl, request).catch(() => false);
      if (ok && typeof message.autostart === "boolean") await chrome.storage.local.set({ browserAutostart: message.autostart });
      reply({ ok });
    })();
    return true;
  }
  if (message?.type === "tick-now") {
    tick("вручную").then(() => reply({ ok: true }));
    return true;
  }
  return false;
});
