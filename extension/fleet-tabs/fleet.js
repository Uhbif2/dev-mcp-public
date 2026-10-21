// Клиент флота: отдельная запись присутствия при изменении списка чатов,
// затем чистое чтение fleet_list на каждом тике.
//
// Ходим в уже работающий dev-mcp (mcp-proxy[dev] :8766), а не заводим отдельный
// сервер статуса: лишний процесс — ещё одна вещь, которая может лежать, когда
// вкладкам нужна помощь. fleet_list только читает реестр и никого не «трогает».

import { parseOwnerNotices, parseOwnerChannel } from "./owner-notices.js";

export const DEFAULT_MCP_URL = "http://127.0.0.1:8766/servers/dev/mcp";

export function fleetSnapshotStatus(data, now = Date.now()) {
  const updatedAt = Number(data.fleetUpdatedAt || data.lastTickAt || 0);
  // 17.10: тик с восстановлением вкладок идёт дольше 90 с, и окно ложно писало «устарели».
  const stale = !updatedAt || now - updatedAt > 180_000;
  const refresh = data.fleetRefresh || {};
  const refreshing = !refresh.finishedAt && Number(refresh.startedAt) > 0 &&
    now - Number(refresh.startedAt) < 30_000;
  const error = String(refresh.error || "");
  const detail = error ? `сервер не ответил: ${error} · повторная попытка через 30 с`
    : refreshing ? "данные устарели — обновляю состояние флота"
    : "данные устарели — ожидаю ответа сервера";
  return { stale, refreshing, error, detail };
}

async function readMessage(response) {
  const text = await response.text();
  const type = response.headers.get("content-type") || "";
  if (type.includes("text/event-stream")) {
    const data = text
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .filter(Boolean);
    return data.length ? JSON.parse(data[data.length - 1]) : null;
  }
  return text.trim() ? JSON.parse(text) : null;
}

export async function callTool(url, name, args = {}, { timeoutMs = 15000 } = {}) {
  const signal = AbortSignal.timeout(timeoutMs);
  const base = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  };
  const init = await fetch(url, {
    method: "POST",
    headers: base,
    signal,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "fleet-tabs", version: "0.1.0" },
      },
    }),
  });
  if (!init.ok) throw new Error(`initialize: HTTP ${init.status}`);
  const session = init.headers.get("mcp-session-id");
  const headers = session ? { ...base, "mcp-session-id": session } : base;
  try {
    await readMessage(init);
    await fetch(url, {
      method: "POST",
      headers,
      signal,
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    });
    const call = await fetch(url, {
      method: "POST",
      headers,
      signal,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 2,
        method: "tools/call",
        params: { name, arguments: args },
      }),
    });
    if (!call.ok) throw new Error(`${name}: HTTP ${call.status}`);
    const message = await readMessage(call);
    if (message?.error) throw new Error(`${name}: ${message.error.message || "ошибка MCP"}`);
    const content = message?.result?.content || [];
    return content.map((item) => item.text || "").join("\n");
  } finally {
    if (session) {
      // Cleanup must not occupy Chrome's small per-origin connection pool
      // indefinitely. It also covers failures while reading initialize.
      fetch(url, { method: "DELETE", headers, signal: AbortSignal.timeout(3000) })
        .then((response) => response?.body?.cancel?.()).catch(() => {});
    }
  }
}

// Строка fleet_list: `W5 (bs-1) — Beta App/TZ-01; молчит 3 мин`
// или `W4 (проверка связи) — без задачи; молчит 884 мин`.
const LINE = /^(\S+) \((.*)\) — (.+); молчит (\d+) мин\s*$/;
// Расход чата — отдельной строкой: `чат W33: взято задач 2 из 5`. Чат web-ChatGPT берёт
// задачи подряд, пока не исчерпает свой лимит; нет строки — воркер без лимита
// (CLI) или сервер старый, и тогда «бери следующую» расширение не шлёт.
const CHAT = /^чат (\S+): взято задач (\d+) из (\d+)\s*$/;

export const STALE_TASK_SILENCE_MIN = 120;

export function parseWorkers(text) {
  const workers = [];
  const chats = new Map();
  for (const raw of String(text || "").split(/\r?\n/)) {
    const chat = CHAT.exec(raw.trim());
    if (chat) {
      chats.set(chat[1], { taken: Number(chat[2]), budget: Number(chat[3]) });
      continue;
    }
    const match = LINE.exec(raw.trim());
    if (!match) continue;
    const [, id, label, where, silence] = match;
    workers.push({
      id,
      label,
      task: where === "без задачи" ? null : where,
      silenceMin: Number(silence),
    });
  }
  for (const worker of workers) {
    const chat = chats.get(worker.id);
    if (chat) {
      worker.taken = chat.taken;
      worker.budgetLeft = Math.max(0, chat.budget - chat.taken);
    }
  }
  return workers;
}

export function staleTaskAttention(workers, silenceMin = STALE_TASK_SILENCE_MIN) {
  return (workers || [])
    .filter((worker) => worker.task && worker.silenceMin >= silenceMin)
    .map((worker) => ({
      task: worker.task,
      note: `${worker.id} молчит ${worker.silenceMin} мин с активным claim — self-heal/recovery должен освободить его автоматически; ручной owner recover не требуется`,
    }));
}

// Последняя строка fleet_list:
// `очередь: следующая к выдаче — Beta App/review:TZ-01 [review]; можно выдать сейчас: 3`
// или `очередь: выдавать нечего`.
const QUEUE = /^очередь: следующая к выдаче — (.+)\/([^/\s]+) \[(\w+)\]; можно выдать сейчас: (\d+)\s*$/;

export function parseQueue(text) {
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = QUEUE.exec(raw.trim());
    if (match) {
      const [, project, tz, kind, count] = match;
      return { next: { project, tz, kind }, count: Number(count), known: true };
    }
    if (raw.trim() === "очередь: выдавать нечего") return { next: null, count: 0, known: true };
  }
  // Старый сервер без строки очереди — запускать вслепую нельзя.
  return { next: null, count: 0, known: false };
}

// Server-owned world resource: DOWN forbids NEW ChatGPT agent chats, even when
// the queue still contains work. Existing conversations may still be resumed.
export function parseLaunchGate(text) {
  for (const raw of String(text || "").split(/\r?\n/)) {
    // The live fleet_list uses "launches=allowed" for UP/UNKNOWN and "0"
    // for DOWN. Numeric counts are also understood for older deployments.
    const match = /^gate:\s*chatgpt-reachable=([a-z]+);\s*launches=(allowed|\d+)(?:;.*)?$/i.exec(raw.trim());
    if (match) {
      const state = match[1].toLowerCase();
      const capacity = match[2].toLowerCase();
      return { known: true, state, allowed: state !== "down" &&
        (capacity === "allowed" || Number(capacity) > 0) };
    }
  }
  return { known: false, state: "unknown", allowed: true };
}

// Четыре состояния — только из серверного flow.format_agent_states(), а не
// из числа вкладок/claim и не из локальной оценки времени молчания.
// Старый сервер без этой строки: null (popup показывает «нет данных сервера»).
export function parseAgentStates(text) {
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = /^(?:агенты:\s*)?(?:работают|MCP≤2мин)\s+(\d+)\s*·\s*(?:думают|генерация UI)\s+(\d+)\s*·\s*(?:молчат|не подтверждены)\s+(\d+)\s*·\s*ждут\s+(\d+)\s*$/.exec(raw.trim());
    if (!match) continue;
    const values = match.slice(1).map(Number);
    if (values.every(Number.isSafeInteger)) {
      const [working, thinking, silent, waiting] = values;
      const why = /^агенты-молчание:\s*отвечают без вкладки\s+(\d+)\s*·\s*ход окончен\s+(\d+)\s*·\s*без следов\s+(\d+)/m
        .exec(String(text || ""));
      const silentWhy = why ? { answering: Number(why[1]), ended: Number(why[2]), unknown: Number(why[3]) } : null;
      return { working, thinking, silent, waiting, silentWhy };
    }
  }
  return null;
}

// Heartbeat and composer evidence are writes, NEVER hidden side effects of
// fleet_list. Retry on the next successful tick if this tool is unavailable.
export async function reportExtension(url, { version = "", tabs = 0, pending = 0,
                                             detail = "", page_text = "",
                                             enabled = null, tick_completed = false,
                                             runnable = null, account_event = "" } = {}) {
  const args = {
    version: String(version).slice(0, 80),
    tabs: Number.isSafeInteger(tabs) && tabs >= 0 ? tabs : 0,
    pending: Number.isSafeInteger(pending) && pending >= 0 ? pending : 0,
    detail: String(detail).slice(0, 500),
    page_text: String(page_text).slice(0, 2000),
  };
  if (typeof enabled === "boolean") args.enabled = enabled;
  if (tick_completed === true) args.tick_completed = true;
  if (Number.isSafeInteger(runnable) && runnable >= 0) args.runnable = runnable;
  if (typeof account_event === "string" && account_event) args.account_event = account_event;
  const response = await callTool(url, "fleet_extension_report", args, { timeoutMs: 7000 });
  if (!response.includes("extension lease updated")) throw new Error("extension report not acknowledged");
  return response;
}

// Строки fleet_list по проектам, в порядке выдачи:
// `проект: Beta App — можно выдать 1; следующая review:TZ-01 [review]`
// `проект: Nova VPN — выдавать нечего`
const PROJECT = /^проект: (.+?) — (?:можно выдать (\d+); следующая (\S+) \[(\w+)\]|выдавать нечего)\s*$/;

export function parseProjects(text) {
  const projects = [];
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = PROJECT.exec(raw.trim());
    if (!match) continue;
    const [, name, count, tz, kind] = match;
    projects.push({ name, count: Number(count || 0), next: tz ? { tz, kind } : null });
  }
  return projects;
}

// Что ждёт владельца: `внимание: Beta App/fix:TZ-04:3 — нужен владелец: <причина>`.
const ATTENTION = /^внимание: (.+?) — нужен владелец: (.*)$/;

export function parseAttention(text) {
  const items = [];
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = ATTENTION.exec(raw.trim());
    if (match) items.push({ task: match[1], note: match[2] });
  }
  return items;
}

let lastPersistedPresence = "";

export async function fleetState(url = DEFAULT_MCP_URL, launcher = null) {
  const args = {};
  if (launcher && typeof launcher === "object") {
    if (launcher.version) args.launcher_version = String(launcher.version);
    if (Number.isFinite(launcher.tabs)) args.launcher_tabs = Number(launcher.tabs);
    if (Number.isFinite(launcher.pending)) args.launcher_pending = Number(launcher.pending);
    if (launcher.chats) {
      args.launcher_chats = String(launcher.chats);
      const identity = url + "\n" + args.launcher_chats;
      if (identity !== lastPersistedPresence) {
        try {
          const result = await callTool(url, "fleet_presence",
            { launcher_chats: args.launcher_chats }, { timeoutMs: 6000 });
          if (result.includes("Сохранено адресов чатов:")) lastPersistedPresence = identity;
        } catch (_) {
          // Older servers have no separate write-channel. Reading the queue
          // must remain available; a later tick retries presence.
        }
      }
    }
  }
  const text = await callTool(url, "fleet_list", args);
  const agentStates = parseAgentStates(text);
  // fleet_list остаётся чистым серверным чтением. Локальный снимок нужен
  // popup, который живёт отдельно от background service worker.
  if (globalThis.chrome?.storage?.local?.set) {
    try {
      await globalThis.chrome.storage.local.set({ fleetAgentStates: agentStates });
    } catch (_) {
      // Ошибка кэша не должна остановить обработку очереди/будильника.
    }
  }
  const queue = parseQueue(text);
  const workers = parseWorkers(text);
  const explicitAttention = parseAttention(text);
  const seen = new Set(explicitAttention.map((item) => item.task));
  const staleAttention = staleTaskAttention(workers).filter((item) => !seen.has(item.task));
  // Без строки очереди сервер старый — по проектам тоже ничего не знаем.
  return {
    workers,
    queue,
    agentStates,
    ownerNotices: parseOwnerNotices(text),
    ownerChannel: parseOwnerChannel(text),
    projects: queue.known ? parseProjects(text) : null,
    attention: [...explicitAttention, ...staleAttention],
    wake: parseWake(text),
    launchGate: parseLaunchGate(text),
    browser: parseBrowserControl(text),
  };
}

// Намерение владельца о браузере флота: `browser: autostart=on|off; halt=none|<причина>`.
// halt — аварийная остановка всей работы (подозрительная активность из сети).
export function parseBrowserControl(text) {
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = /^browser:\s*autostart=(on|off);\s*halt=(.*)$/i.exec(raw.trim());
    if (match) {
      const halt = match[2].trim();
      return { known: true, autostart: match[1].toLowerCase() === "on",
        halt: halt && halt.toLowerCase() !== "none" ? halt : "" };
    }
  }
  return { known: false, autostart: true, halt: "" };
}

// Приказ сервера разбудить агента (fleet/wake.py): `разбудить: <label> <url> :: <текст>`.
// Решает сервер — кого, когда и каким текстом; расширение только открывает чат и отправляет.
const WAKE = /^разбудить: (\S+) (https:\/\/chatgpt\.com\/\S+) :: (.+)$/;

export function parseWake(text) {
  const orders = [];
  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = WAKE.exec(raw.trim());
    if (match) orders.push({ label: match[1], url: match[2], text: match[3] });
  }
  return orders;
}

// Адреса чатов для сервера: строки «label url», свежие первыми, не больше limit.
export function chatLines(known, now = Date.now(), maxAgeMs = 48 * 3600_000, limit = 200) {
  return Object.entries(known || {})
    .filter(([, v]) => v?.url && now - (v.at || 0) < maxAgeMs)
    .sort((a, b) => (b[1].at || 0) - (a[1].at || 0))
    .slice(0, limit)
    .map(([label, v]) => `${label} ${v.url}`)
    .join("\n");
}

// Закрепить проект за будущей вкладкой: её fleet_take возьмёт задачу только оттуда.
export async function expectProject(url, label, project) {
  const text = await callTool(url, "fleet_expect", { label, project });
  if (!text.includes("закреплена")) throw new Error(text.trim().slice(0, 200) || "пустой ответ");
}

export async function listWorkers(url = DEFAULT_MCP_URL) {
  return (await fleetState(url)).workers;
}
