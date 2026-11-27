import { DEFAULTS as REACT, KEYBOARD_DEFAULTS, LAUNCH_DEFAULTS, RELOAD_DEFAULTS } from "./policy.js";
import { fleetSnapshotStatus, callTool, DEFAULT_MCP_URL } from "./fleet.js";
import { fleetOverview, drawActivityChart } from "./fleet-ui-state.js";
import { INTERFACE_DEFAULTS } from "./interface-pressure.js";
import { RECOVERY_DEFAULTS } from "./recovery.js";
import { INSTALL_DEFAULTS, localSettings } from "./config.js";

// Окно показывает намерения владельца. Отточенные параметры (TUNED_KEYS в policy.js) — только
// для чтения в «Служебном»: их значение задаёт механика ChatGPT и телеметрия, а не выбор.
const DEFAULTS = {
  enabled: INSTALL_DEFAULTS.enabled,
  dryRun: false,
  lockTabs: true,
  projects: LAUNCH_DEFAULTS.projects,
  maxWorking: LAUNCH_DEFAULTS.maxWorking,
  maxTabs: LAUNCH_DEFAULTS.maxTabs,
   projectUrl: INSTALL_DEFAULTS.projectUrl,
};

const NUMBERS = ["maxWorking", "maxTabs"];
let autostartPending = null;
let activityCache = [];
let activityFetchedAt = 0;
let activityError = "";

const TUNED = [
  ["закрыть вкладку после начала ответа, с", KEYBOARD_DEFAULTS.closeGeneratingSec],
  ["закрыть простаивающую вкладку, с", LAUNCH_DEFAULTS.closeIdleSec],
  ["выгружать безопасный простой при свободной ОЗУ <, МБ", LAUNCH_DEFAULTS.minFreeMemMB],
  ["закрыть простой при свободной ОЗУ <, МБ", KEYBOARD_DEFAULTS.pressureMemMB],
  ["пауза между запусками, с", LAUNCH_DEFAULTS.minGapSec],
  ["одновременно подключающихся", LAUNCH_DEFAULTS.maxPending],
  ["«Повторить» после сбоя ответа, с", REACT.failedTurnSec],
  ["«продолжи», если наше сообщение без ответа, с", REACT.stuckUserTurnSec],
  ["повтор одной вкладке не чаще, с", REACT.retryGapSec],
  ["«Соединение прервано» → перезагрузка, с", RELOAD_DEFAULTS.interruptedSec],
  ["вкладок с ошибкой восстанавливается", INTERFACE_DEFAULTS.failedTabs],
  ["пауза между загрузками интерфейса, с", RECOVERY_DEFAULTS.accountActionGapSec],
  ["обычных перезагрузок в час (ошибка разговора отдельно)", RELOAD_DEFAULTS.maxPerHour],
];

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

// Проекты — из последнего ответа флота (в порядке выдачи) плюс отмеченные, которых
// во флоте уже нет: их видно, чтобы можно было снять галочку.
// Состояния агентов берутся только из server-owned fleetAgentStates, не из вкладок
// и не из количества захватов по проектам.
function renderProjects(fleetProjects, checked, stale = false, staleDetail = "данные устарели") {
  const box = document.getElementById("projects");
  const known = fleetProjects || [];
  const names = [...known.map((p) => p.name), ...checked.filter((n) => !known.some((p) => p.name === n))];
  if (!names.length) {
    box.replaceChildren(el("div", { className: "muted", textContent: "Флот ещё не ответил — нажми «Проверить сейчас»." }));
    return;
  }
  box.replaceChildren(
    ...names.map((name) => {
      const info = known.find((p) => p.name === name);
      const detail = stale
        ? staleDetail
        : !info
          ? "нет во флоте"
          : info.next
            ? `можно выдать ${info.count} · следующая ${info.next.tz}`
            : "готовых к выдаче 0";
      const input = el("input", { type: "checkbox", checked: checked.includes(name) });
      input.dataset.project = name;
      input.addEventListener("change", () => {
        const selected = [...box.querySelectorAll("input[data-project]")]
          .filter((i) => i.checked)
          .map((i) => i.dataset.project);
        save({ projects: selected });
      });
      return el("label", {}, input, el("span", { className: "name", textContent: name }), el("span", { className: "muted", textContent: detail }));
    }),
  );
}

async function render() {
  const data = await chrome.storage.local.get([
    "settings",
    "browserHalt",
    "browserAutostart",
    "status",
    "log",
    "lastTick",
    "lastTickAt",
    "fleetProjects",
    "fleetWorkers",
    "fleetAgentStates",
    "fleetQuickStates",
    "quickSendFailures",
    "fleetMemoryState",
    "tabCounts",
    "fleetTabHealth",
    "fleetTabHealthAt",
    "launchNote",
    "fleetAttention",
    "cooldownNote",
    "telemetrySummary",
    "tickError",
    "fleetUpdatedAt",
    "fleetRefresh",
  ]);
  const runtime = await Promise.resolve(chrome.runtime?.sendMessage?.({ type: "runtime-health" })).catch(() => null);
  const storageError = runtime?.storageError
    ? `Хранилище браузера не записывает данные: ${runtime.storageError}. Наблюдатель проверяет восстановление.` : "";
  document.getElementById("cooldown").textContent = storageError || data.tickError || "";
  const stats = data.telemetrySummary;
  const recovery = stats?.ejections
    ? ` Выбросов из сессии: ${stats.ejections}, устойчиво возвращено: ${stats.restored || 0}.`
    : "";
  document.getElementById("rateStats").textContent = stats?.samples
    ? `Окон: ${stats.warnings}, закрыто: ${stats.dismissed}. До подтверждённой отправки: медиана ${Math.round(stats.medianSec)} с, p90 ${Math.round(stats.p90Sec)} с (${stats.samples} наблюдений). Это время восстановления, не окно квоты.${recovery}`
    : `Время восстановления ещё не измерено. Окно «слишком часто» не останавливает остальные вкладки.${recovery}`;
  const attention = data.fleetAttention || [];
  document.getElementById("attention").replaceChildren(
    ...attention.map((item) =>
      el(
        "div",
        { className: "attention" },
        el("b", { textContent: `Нужен владелец: ${item.task}` }),
        el("span", { textContent: item.note }),
      ),
    ),
  );
  const cfg = { ...DEFAULTS, ...await localSettings(), ...(data.settings || {}) };
  if (Date.now() - activityFetchedAt > 60_000) {
    activityFetchedAt = Date.now();
    try {
      const response = await callTool(cfg.mcpUrl || DEFAULT_MCP_URL, "fleet_activity_24h", {}, { timeoutMs: 6000 });
      const parsed = JSON.parse(response);
      if (!Array.isArray(parsed)) throw new Error("unexpected history");
      activityCache = parsed;
      activityError = "";
    } catch (error) { activityError = String(error); }
  }
  const chart = drawActivityChart(document.getElementById("activityChart"), activityCache);
  document.getElementById("activityNote").textContent = activityError
    ? `История недоступна: ${activityError}`
    : chart.count ? `${chart.count} точек · синий — обычные, оранжевый — сумма с быстрыми (MCP); генерация быстрых без вызова MCP в истории недоступна`
      : "Пока нет точек; они накапливаются после отчётов расширения.";
  const snapshot = fleetSnapshotStatus(data);
  if (storageError) { snapshot.stale = true; snapshot.detail = storageError; }
  const stale = snapshot.stale;
  document.getElementById("enabled").checked = cfg.enabled;
  if (autostartPending === null) {
    document.getElementById("browserAutostart").checked = data.browserAutostart !== false;
  }
  document.getElementById("halt").hidden = !data.browserHalt;
  document.getElementById("haltReason").textContent = data.browserHalt
    ? `${data.browserHalt.reason}. Пока остановка стоит, расширение ничего не отправляет, не открывает и не перезагружает; сервер не трогает браузер.` +
      (String(data.browserHalt.reason || "").startsWith("ChatGPT:")
        ? ` Снимется само в ${new Date(Number(data.browserHalt.resumeAt) || (Number(data.browserHalt.at) || 0) + 20 * 60_000).toLocaleTimeString()} (пауза 20 мин, при повторе за 6 ч — вдвое, не больше 2 ч).`
        : " Сама не снимается — кнопка ниже.")
    : "";
  document.getElementById("dryRun").checked = cfg.dryRun;
  document.getElementById("tuned").textContent = TUNED.map(([k, v]) => `${k}: ${v}`).join("\n");
  document.getElementById("lockTabs").checked = cfg.lockTabs;
  for (const id of NUMBERS) {
    const input = document.getElementById(id);
    if (document.activeElement !== input) input.value = cfg[id];
  }
  renderProjects(data.fleetProjects, cfg.projects, stale, snapshot.detail);
  const memory = data.fleetMemoryState;
  const freshMemory = memory && Date.now() - Number(memory.at || 0) < 180_000;
  document.getElementById("memoryGuard").textContent = freshMemory
    ? `ОЗУ свободно: ${memory.freeMemMB ?? "н/д"} МБ (порог ${memory.thresholdMB} МБ) · живых вкладок агентов: ${memory.liveTabs} · выгружено: ${memory.discarded || 0}` +
      (memory.lowMemory ? " · мало ОЗУ: безопасные вкладки выгружаются; новые не блокируются" : "")
    : "Данные об ОЗУ и живых вкладках пока не получены";
  const paused = Object.entries(data.quickSendFailures || {}).filter(([, v]) =>
    v?.count >= 3 && Number(v.pausedUntil) > Date.now());
  document.getElementById("quickSendPauses").textContent = paused.length
    ? "Паузы отправки быстрых: " + paused.slice(0, 10).map(([id, v]) =>
      `${id}: ${String(v.reason || "нет подтверждения").slice(0, 150)}; повтор после ${new Date(v.pausedUntil).toLocaleTimeString("ru-RU")}`).join(" | ")
    : "";
  const totals = !stale && fleetOverview(data.fleetAgentStates, data.fleetQuickStates);
  if (totals) document.getElementById("activityNote").textContent +=
    ` · сейчас ${totals.working} (обычные ${totals.active}, быстрые ${totals.quickActive})`;
  for (const [id, value] of Object.entries({
    agentTotal: totals?.working, agentWaiting: totals?.waiting,
    agentInactive: totals?.inactive, agentQuick: totals?.quickActive,
    quickParked: totals?.quickParked, quickAnswered: totals?.quickAnswered,
    quickWaitingSend: totals?.quickWaitingSend, quickOpened: totals?.quickOpened,
  })) document.getElementById(id).textContent = value ?? "—";
  // 17.11: плитка — реальные вкладки ChatGPT этого Chrome: флота и владельца отдельно.
  const tc = data.tabCounts;
  if (tc && Number.isFinite(tc.fleet)) {
    const tile = document.getElementById("quickOpened");
    tile.textContent = `${tc.fleet} флота · ${tc.owner} твоих`;
    tile.parentElement.title = `Вкладки ChatGPT во флотском Chrome: флота ${tc.fleet}, владельца ${tc.owner}; загружено ${tc.loaded}`;
  }
  document.getElementById("agentFresh").textContent = totals
    ? "действие в dev-mcp ≤2 мин или генерация"
    : "Нет свежих данных";
  document.getElementById("quickWorking").textContent = totals
    ? `всего быстрых: ${totals.quick} (открытых вкладок ${totals.quickOpened})` : "—";
  document.getElementById("workingBreakdown").textContent = totals
    ? `${totals.working} = обычные ${totals.active} (MCP ${totals.mcp}, генерация ${totals.generating}) + быстрые ${totals.quickActive}; запаркованы ${totals.quickParked}; ответили, ждут владельца ${totals.quickAnswered}; ждут отправки ${totals.quickWaitingSend}; открыто вкладок ${totals.quickOpened}`
    : "Нет подтверждённой разбивки";
  document.getElementById("agentTotal").parentElement.title = totals
    ? `Обычные: свежий MCP ${totals.mcp}, генерация UI ${totals.generating}; быстрые работают ${totals.quickActive}, запаркованы ${totals.quickParked}, ответили, ждут владельца ${totals.quickAnswered}, ждут отправки ${totals.quickWaitingSend}, открыто вкладок ${totals.quickOpened}`
    : "Нет свежих данных";

  // dev и контроллер подтверждаются текущим тиком; core и внешний шлюз этим
  // расширением отдельно не опрашиваются — отсутствие данных не означает UP.
  const health = (id, status, detail) => {
    const node = document.getElementById(id);
    node.dataset.health = status;
    node.title = detail;
    node.setAttribute("aria-label", `${node.textContent.trim()}: ${detail}`);
  };
  health("healthDev", snapshot.error || storageError ? "down" : stale ? "unknown" : "ok",
    snapshot.error || storageError || (stale ? snapshot.detail : "dev отвечает"));
  health("healthCore", "unknown", "Нет отдельной проверки core");
  health("healthGateway", "unknown", "Нет отдельной проверки шлюза");
  const controllerError = runtime?.storageError || runtime?.error || data.tickError;
  const controllerFresh = runtime &&
    Date.now() - Number(runtime.cycleFinishedAt || runtime.cycleStartedAt || 0) < 180_000;
  health("healthController", controllerError ? "down" : controllerFresh ? "ok" : "unknown",
    controllerError || (controllerFresh ? "Контроллер выполняет тики" : "Нет свежего тика"));
  document.getElementById("lastTick").textContent =
    (data.lastTick || "—") +
    (cfg.dryRun ? " · вхолостую" : "") +
    (stale ? " · ⚠ состояние не обновляется" : "");
  document.getElementById("launchNote").textContent = stale
    ? snapshot.detail
    : (data.launchNote || "—");
  const rows = Object.values(data.status || {}).map((s) => {
    const cells = s.label
      ? [`${s.locked ? "🔒 " : ""}${s.label}${s.worker ? ` (${s.worker})` : ""}`, s.task || "—", s.action ? `${s.action}: ${s.reason}` : s.note || "", s.continues ?? ""]
      : [(s.title || "").slice(0, 24), "—", s.note || "не агент флота — не трогаю", ""];
    return el("tr", { className: s.label ? "" : "other" }, ...cells.map((v) => el("td", { textContent: String(v) })));
  });
  document.getElementById("tabs").replaceChildren(...rows);
  document.getElementById("log").textContent = (data.log || []).slice(-40).reverse().join("\n");
}

async function save(patch) {
  const { settings = {} } = await chrome.storage.local.get("settings");
  await chrome.storage.local.set({ settings: { ...settings, ...patch } });
  render();
}

for (const id of ["enabled", "dryRun", "lockTabs"]) {
  document.getElementById(id).addEventListener("change", (e) => save({ [id]: e.target.checked }));
}
// 21.11.2026: передача чата под надзором по факту «maximum length» и режим «вписать, не отправлять».
(async () => {
  const { settings = {} } = await chrome.storage.local.get("settings");
  const full = document.getElementById("fullHandoff");
  const draft = document.getElementById("fullHandoffDraftOnly");
  if (!full || !draft) return;
  full.checked = settings.fullHandoff !== false;
  draft.checked = settings.fullHandoffDraftOnly === true;
  full.addEventListener("change", (e) => save({ fullHandoff: e.target.checked }));
  draft.addEventListener("change", (e) => save({ fullHandoffDraftOnly: e.target.checked }));
})();
// 20.11.2026 (№116 п. 6): автообновление при «A temporary limit…» — общий выключатель и исключение
// для открытой вкладки (по чату, если адрес чата есть, иначе по вкладке).
(async () => {
  const { settings = {}, limitReloadExcluded = {} } = await chrome.storage.local.get(["settings", "limitReloadExcluded"]);
  const toggle = document.getElementById("limitReload");
  toggle.checked = settings.limitReload !== false;
  toggle.addEventListener("change", (e) => save({ limitReload: e.target.checked }));
  const [tab] = await Promise.resolve(chrome.tabs?.query?.({ active: true, currentWindow: true })).catch(() => []) || [];
  if (!tab || !/^https:\/\/chatgpt\.com\//i.test(tab.url || "")) return;
  const { limitExcluded, toggleLimitExclusion } = await import("./limit-reload.js");
  const box = document.getElementById("limitExclude");
  document.getElementById("limitExcludeRow").hidden = false;
  box.checked = limitExcluded(limitReloadExcluded, tab);
  box.addEventListener("change", async (e) => {
    const latest = (await chrome.storage.local.get("limitReloadExcluded")).limitReloadExcluded || {};
    await chrome.storage.local.set({ limitReloadExcluded: toggleLimitExclusion(latest, tab, e.target.checked) });
  });
})();
document.getElementById("browserAutostart").addEventListener("change", async (e) => {
  const input = e.target;
  const requested = input.checked;
  autostartPending = requested; // stale storage.onChanged must not flicker the switch
  input.disabled = true;
  let reply;
  try {
    reply = await chrome.runtime.sendMessage({ type: "browser-control", autostart: requested });
  } catch (error) {
    reply = { ok: false, detail: String(error) };
  }
  autostartPending = null;
  input.disabled = false;
  if (reply?.ok && reply.autostart === requested) {
    input.checked = requested; // сервер подтвердил и значение перечитано
  } else {
    const latest = await chrome.storage.local.get("browserAutostart");
    input.checked = latest.browserAutostart !== false;
    document.getElementById("launchNote").textContent =
      "Автозапуск не подтверждён сервером: " + (reply?.detail || "нет readback");
  }
  await render();
});
// 20.11.2026 (№116 п. 4): экспорт открытого чата — полный объект беседы с сайта, файл .json.
(async () => {
  const [tab] = await Promise.resolve(chrome.tabs?.query?.({ active: true, currentWindow: true })).catch(() => []) || [];
  if (!tab || !/^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/[0-9a-f-]{8,}/i.test(tab.url || "")) return;
  document.getElementById("exportRow").hidden = false;
  const button = document.getElementById("exportSession");
  const note = document.getElementById("exportNote");
  button.addEventListener("click", async () => {
    button.disabled = true;
    note.textContent = "читаю беседу с сайта…";
    const reply = await chrome.runtime.sendMessage({ type: "export-session", tabId: tab.id })
      .catch((error) => ({ ok: false, detail: String(error) }));
    button.disabled = false;
    if (!reply?.ok) { note.textContent = "не вышло: " + (reply?.detail || "нет ответа"); return; }
    const url = URL.createObjectURL(new Blob([reply.text], { type: "application/json" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: reply.filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    const s = reply.meta?.stats || {};
    note.textContent = `${reply.filename}: сообщений ${s.messages} (в текущей ветке ${s.currentBranchMessages}), ` +
      `узлов ${s.nodes}, ветвлений ${s.branches}`;
  });
})();
// Надзиратель хода (пульт _todo/59, шаг 3): отметка «под надзором» для открытого чата ChatGPT.
(async () => {
  const [tab] = await Promise.resolve(chrome.tabs?.query?.({ active: true, currentWindow: true })).catch(() => []) || [];
  if (!tab || !/^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/[0-9a-f-]{8,}/i.test(tab.url || "")) return;
  const row = document.getElementById("overseerRow");
  const select = document.getElementById("overseerMode");
  const note = document.getElementById("overseerNote");
  row.hidden = false;
  select.disabled = true;
  note.textContent = "проверяю на сервере";
  const state = await chrome.runtime.sendMessage({ type: "overseer-state", url: tab.url })
    .catch((error) => ({ ok: false, detail: String(error) }));
  if (!state?.ok || !["off", "shadow", "send"].includes(state.mode)) {
    note.textContent = "режим не прочитан: " + (state?.detail || "сервер не ответил");
    return;
  }
  select.value = state.mode;
  select.disabled = false;
  note.textContent = "";
  select.addEventListener("change", async () => {
    const requested = select.value;
    select.disabled = true;
    const reply = await chrome.runtime.sendMessage({ type: "overseer-mark", url: tab.url, mode: requested })
      .catch((error) => ({ ok: false, detail: String(error) }));
    if (reply?.ok && ["off", "shadow", "send"].includes(reply.mode)) {
      state.mode = reply.mode;
      select.value = reply.mode;
    } else select.value = state.mode;
    note.textContent = reply?.ok ? "сохранено" : "не сохранилось: " + (reply?.detail || "");
    select.disabled = false;
  });
})();

document.getElementById("resume").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "browser-control", resume: true });
  render();
});
for (const id of NUMBERS) {
  const input = document.getElementById(id);
  input.addEventListener("change", (e) => {
    const n = Number(e.target.value);
    const min = Number(input.min), max = Number(input.max);
    save({ [id]: Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : DEFAULTS[id] });
  });
}
document.getElementById("exportStats").addEventListener("click", async () => {
  const { events = [], telemetrySummary = {}, accountActions = {} } = await chrome.storage.local.get(["events", "telemetrySummary", "accountActions"]);
  const blob = new Blob([JSON.stringify({ version: 2, exportedAt: new Date().toISOString(), events, accountActions, summary: telemetrySummary }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = `fleet-tabs-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});
document.getElementById("now").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "tick-now" });
  render();
});
document.getElementById("copyInstructions").addEventListener("click", async (e) => {
  try {
    const text = await (await fetch(chrome.runtime.getURL("PROJECT-INSTRUCTIONS.md"))).text();
    await navigator.clipboard.writeText(text);
    e.target.textContent = "Скопировано — вставь в инструкции проекта";
  } catch (error) {
    e.target.textContent = `Не вышло: ${error.message}`;
  }
});
document.getElementById("openProject").addEventListener("click", async () => {
  const { settings = {} } = await chrome.storage.local.get("settings");
  const local = await localSettings();
  chrome.tabs.create({ url: settings.projectUrl || local.projectUrl || DEFAULTS.projectUrl });
});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.status || changes.log || changes.fleetProjects || changes.fleetAgentStates || changes.fleetQuickStates || changes.fleetAttention || changes.cooldownNote || changes.lastTickAt || changes.launchNote || changes.tickError || changes.fleetUpdatedAt || changes.fleetRefresh) render();
});
render();
if (chrome.runtime?.sendMessage) {
  chrome.runtime.sendMessage({ type: "tick-now" }).then(() => render()).catch(() => {});
}
