import { DEFAULTS as REACT, KEYBOARD_DEFAULTS, LAUNCH_DEFAULTS, RELOAD_DEFAULTS } from "./policy.js";
import { fleetSnapshotStatus } from "./fleet.js";
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

const TUNED = [
  ["закрыть вкладку после начала ответа, с", KEYBOARD_DEFAULTS.closeGeneratingSec],
  ["закрыть простаивающую вкладку, с", LAUNCH_DEFAULTS.closeIdleSec],
  ["не открывать вкладку при свободной ОЗУ <, МБ", LAUNCH_DEFAULTS.minFreeMemMB],
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
    "launchNote",
    "fleetAttention",
    "cooldownNote",
    "telemetrySummary",
    "tickError",
    "fleetUpdatedAt",
    "fleetRefresh",
  ]);
  document.getElementById("cooldown").textContent = data.tickError || "";
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
  const snapshot = fleetSnapshotStatus(data);
  const stale = snapshot.stale;
  document.getElementById("enabled").checked = cfg.enabled;
  document.getElementById("browserAutostart").checked = data.browserAutostart !== false;
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
  const states = data.fleetAgentStates;
  document.getElementById("agentStates").textContent = !stale && states &&
    ["working", "thinking", "silent", "waiting"].every((key) => Number.isInteger(states[key]) && states[key] >= 0)
    ? `MCP≤2мин ${states.working} · генерация UI ${states.thinking} · не подтверждены ${states.silent}` +
      (states.silentWhy && states.silent ? ` (отвечают без вкладки ${states.silentWhy.answering} · ход окончен ${states.silentWhy.ended} · без следов ${states.silentWhy.unknown})` : "") +
      ` · ждут ${states.waiting} (без задачи: очередь пуста или задачи заблокированы)`
    : "нет данных сервера";
  document.getElementById("lastTick").textContent =
    (data.lastTick || "—") +
    (cfg.dryRun ? " · вхолостую" : "") +
    (stale ? " · ⚠ данные старше 90 с" : "");
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
document.getElementById("browserAutostart").addEventListener("change", async (e) => {
  const reply = await chrome.runtime.sendMessage({ type: "browser-control", autostart: e.target.checked });
  if (!reply?.ok) document.getElementById("launchNote").textContent = "сервер не ответил — настройка не сохранена";
  render();
});
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
  if (changes.status || changes.log || changes.fleetProjects || changes.fleetAgentStates || changes.fleetAttention || changes.cooldownNote || changes.lastTickAt || changes.launchNote || changes.tickError || changes.fleetUpdatedAt || changes.fleetRefresh) render();
});
render();
if (chrome.runtime?.sendMessage) {
  chrome.runtime.sendMessage({ type: "tick-now" }).then(() => render()).catch(() => {});
}
