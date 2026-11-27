// Передача чата под надзором по факту конца беседы (владелец 21.11.2026).
// Сайт показал «You've reached the maximum length for this conversation…» — заранее мы
// за длиной не следим. Дальше: полный экспорт беседы (session-export.js) → новый чат в том же
// проекте ChatGPT на месте старой вкладки → во вложении файл экспорта (+ текстовая расшифровка
// текущей ветки) → «погрузись в контекст и продолжи с того же места» → надзор переносится на
// новый чат, старая вкладка закрывается. Здесь — чистые функции без Chrome API (для тестов).

export const FULL_HANDOFF_LABEL = "owner-sv-next"; // до регистрации преемника: не закрывать как «надзор отключён»
export const FULL_HANDOFF_MAX_ATTEMPTS = 5;
export const FULL_HANDOFF_KEEP_MS = 7 * 24 * 3600_000;

export function projectStartUrl(url) {
  const match = /^https:\/\/chatgpt\.com\/g\/(g-p-[^/?#]+)\//i.exec(String(url || ""));
  return match ? `https://chatgpt.com/g/${match[1]}/project` : "https://chatgpt.com/";
}

export function continuationMarker(oldId) {
  return `[fleet-continuation:${String(oldId || "").slice(0, 8).toLowerCase()}]`;
}

export function continuationText({ title = "", oldUrl = "", oldId = "", files = [] } = {}) {
  const names = files.map((f) => `«${f}»`).join(" и ");
  return [
    `Продолжение чата «${title || "без названия"}» (${oldUrl}): он достиг максимальной длины, поэтому работа переносится сюда.`,
    `Во вложении — экспорт той беседы: ${names}. JSON — полный исходный объект сайта (все сообщения и ветки), .md — текущая ветка текстом.`,
    "Погрузись в контекст: прочитай переписку целиком, особенно последнее задание владельца и последние ответы, " +
      "и продолжи работу ровно с того места, где остановился предыдущий чат. Не начинай заново и не переспрашивай уже решённое.",
    continuationMarker(oldId),
  ].join("\n\n");
}

function partsText(content) {
  if (!content || typeof content !== "object") return "";
  if (Array.isArray(content.parts)) {
    return content.parts.map((p) => (typeof p === "string" ? p : p && typeof p.text === "string" ? p.text : "")).
      filter(Boolean).join("\n");
  }
  return typeof content.text === "string" ? content.text : "";
}

// Текущая ветка (от current_node к корню) — сообщения владельца и ассистента текстом.
export function transcriptMarkdown(conversation, { title = "", url = "" } = {}) {
  const mapping = conversation && typeof conversation.mapping === "object" ? conversation.mapping : {};
  const chain = [];
  const seen = new Set();
  for (let id = conversation?.current_node; id && mapping[id] && !seen.has(id); id = mapping[id].parent) {
    seen.add(id);
    chain.push(mapping[id]);
  }
  chain.reverse();
  const out = [`# ${title || conversation?.title || "Беседа"}`, "", `Источник: ${url}`, "",
    "Текущая ветка беседы: сообщения владельца и ассистента; служебные и вызовы инструментов опущены.", ""];
  let n = 0;
  for (const node of chain) {
    const message = node?.message;
    const role = message?.author?.role;
    if (role !== "user" && role !== "assistant") continue;
    if (message?.metadata?.is_visually_hidden_from_conversation) continue;
    const text = partsText(message.content).trim();
    if (!text) continue;
    n += 1;
    out.push(`## ${n}. ${role === "user" ? "Владелец" : "Ассистент"}`, "", text, "");
  }
  return out.join("\n");
}

export function transcriptFilename(jsonName) {
  return String(jsonName || "chat.json").replace(/\.json$/i, "") + ".md";
}

export function newFullHandoff({ oldId, url, tabId, index = 0, windowId, mode = "send", engine = "", title = "", now = Date.now() }) {
  return { oldId, url, tabId, index, windowId, mode, engine, title, startedAt: now, attempts: 0 };
}

// Следующий шаг. Каждый шаг идемпотентен и записывает свою отметку времени.
export function fullHandoffStage(rec, { draftOnly = false } = {}) {
  if (!rec || rec.done) return "done";
  if (rec.failed) return "failed";
  if (!rec.exportedAt) return "export";
  if (!rec.newTabId) return "open";
  if (!rec.attachedAt) return "attach";
  if (!rec.sentAt) return draftOnly ? "draft" : "send";
  if (!rec.successor) return "await-successor";
  if (!rec.markedAt) return "mark";
  if (!rec.closedAt && !rec.draft) return "close-old";
  return "finish";
}

export function noteFailure(rec, stage, detail, now = Date.now()) {
  rec.attempts = (rec.attempts || 0) + 1;
  rec.lastError = `${stage}: ${String(detail || "").slice(0, 200)}`;
  rec.nextAt = now + Math.min(10 * 60_000, 30_000 * 2 ** Math.min(rec.attempts, 5));
  if (rec.attempts >= FULL_HANDOFF_MAX_ATTEMPTS) rec.failed = true;
  return rec;
}

export function pruneFullHandoffs(all, now = Date.now()) {
  const out = {};
  for (const [id, rec] of Object.entries(all || {})) {
    if ((rec.done || rec.failed) && now - Number(rec.finishedAt || rec.startedAt || 0) > FULL_HANDOFF_KEEP_MS) continue;
    out[id] = rec;
  }
  return out;
}
