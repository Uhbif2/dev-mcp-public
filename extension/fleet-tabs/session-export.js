// Экспорт полной сессии ChatGPT в JSON (владелец 20.11.2026, №116 п. 4).
// Источник — тот же объект, который сайт загружает для отрисовки чата: GET
// /backend-api/conversation/<id> (mapping всех узлов с ветвлениями, metadata). Читается в
// main-world вкладки с токеном страницы — как проверка update_time (background.js). Файл
// без потерь: исходный текст ответа вставляется в обёртку байт в байт, рядом — метаданные.
// Одна функция для попапа и для передачи сессии по факту конца (п. 5).

export const EXPORT_FORMAT = "fleet-tabs-session-export/1";

export function conversationIdOf(url) {
  const match = /^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/([0-9a-f][0-9a-f-]{7,})/i.exec(String(url || ""));
  return match ? match[1].toLowerCase() : "";
}

// Выполняется в странице (chrome.scripting.executeScript world:"MAIN") — без внешних ссылок.
export async function readConversationInPage(id) {
  const signal = AbortSignal.timeout(60_000);
  const session = await fetch("/api/auth/session", { signal }).then((r) => r.json()).catch(() => ({}));
  if (!session?.accessToken) return { ok: false, status: 0, detail: "нет сессии страницы (не выполнен вход?)" };
  const response = await fetch("/backend-api/conversation/" + id, {
    signal, headers: { authorization: "Bearer " + session.accessToken } });
  const text = await response.text();
  return response.ok ? { ok: true, status: response.status, text }
    : { ok: false, status: response.status, detail: text.slice(0, 200) };
}

// Счётчики для сверки с сайтом: узлы mapping, сообщения по ролям, ветвления, текущая ветка.
export function conversationStats(conversation) {
  const mapping = conversation && typeof conversation.mapping === "object" ? conversation.mapping : {};
  const nodes = Object.values(mapping);
  const roles = {};
  let messages = 0, branches = 0;
  for (const node of nodes) {
    if (Array.isArray(node?.children) && node.children.length > 1) branches += 1;
    const role = node?.message?.author?.role;
    if (!role) continue;
    messages += 1;
    roles[role] = (roles[role] || 0) + 1;
  }
  let current = 0;
  for (let id = conversation?.current_node, guard = 0; id && mapping[id] && guard <= nodes.length; guard += 1) {
    if (mapping[id].message?.author?.role) current += 1;
    id = mapping[id].parent;
  }
  return { nodes: nodes.length, messages, roles, branches, currentBranchMessages: current };
}

export function buildSessionExport(rawText, { url = "", id = "", version = "", now = Date.now() } = {}) {
  const conversation = JSON.parse(rawText); // невалидный ответ — исключение, файл не пишется
  if (!conversation || typeof conversation !== "object" || typeof conversation.mapping !== "object") {
    throw new Error("ответ сайта не похож на беседу (нет mapping)");
  }
  const stats = conversationStats(conversation);
  const meta = { format: EXPORT_FORMAT, exported_at: new Date(now).toISOString(), url,
    conversation_id: id || String(conversation.conversation_id || conversation.id || ""),
    title: String(conversation.title || ""), extension_version: version, source: "GET /backend-api/conversation/<id>",
    bytes: rawText.length, stats };
  const text = `{"meta":${JSON.stringify(meta)},"conversation":${rawText}}`;
  const safeTitle = String(conversation.title || "chat").replace(/[\\/:*?"<>|\s]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "chat";
  const stamp = new Date(now).toISOString().replace(/[-:]/g, "").slice(0, 15);
  return { text, filename: `chatgpt-${safeTitle}-${meta.conversation_id.slice(0, 8)}-${stamp}Z.json`, meta };
}
