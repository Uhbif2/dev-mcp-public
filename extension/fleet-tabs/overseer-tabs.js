// Вкладка чата под надзором: продолжать в уже открытой, а не открывать вторую (24.11.2026).
//
// Владелец: «когда браузер закрывается, а потом я его открываю — расширение открывает ещё одну
// вкладку вместо того, чтобы продолжать в старой, на том же месте и в той же группе вкладок».
// Причины: (1) первый такт после запуска Chrome идёт раньше, чем Chrome восстановил прежние
// вкладки, — чата «нет», открывается новый; (2) после перезапуска у вкладки новый id, и прежняя
// привязка ownTabs/labels.byTab к ней не относится.
// Решение: пока идёт восстановление сессии (RESTORE_GRACE_MS после onStartup) — не открывать;
// любую вкладку с этим чатом (url, pendingUrl) взять под надзор привязкой labels.byTab, без
// ownTabs — вкладка владельца никогда не становится «собственной» и не закрывается расширением;
// предпочесть вкладку владельца (не из ownTabs, в группе) нашей; лишние НАШИ копии закрыть.

export const OVERSEER_RESTORE_GRACE_MS = 3 * 60_000;

function chatId(url) {
  const match = /\/c\/([0-9a-f-]{8,})/i.exec(String(url || ""));
  return match ? match[1] : "";
}

/**
 * @param {object} o
 * @param {Array<object>} o.tabs       вкладки chatgpt.com (живые, не закрытые в этом такте)
 * @param {string} o.chat              id чата под надзором
 * @param {(tab:object)=>string} [o.urlOf]  адрес вкладки (по умолчанию url || pendingUrl)
 * @param {Record<string,string>} o.ownTabs
 * @param {number} [o.browserStartedAt] Date.now() из chrome.runtime.onStartup
 * @param {number} [o.now]
 * @returns {{keep:number|null, close:number[], wait:boolean, adopt:boolean}}
 */
export function overseerTabPlan({ tabs = [], chat, urlOf, ownTabs = {}, browserStartedAt = 0, now = Date.now() }) {
  const addr = urlOf || ((t) => t.url || t.pendingUrl || "");
  const matches = tabs.filter((t) => chatId(addr(t)) === chat || chatId(t.pendingUrl) === chat);
  if (!matches.length) {
    const restoring = browserStartedAt > 0 && now - browserStartedAt < OVERSEER_RESTORE_GRACE_MS;
    return { keep: null, close: [], wait: restoring, adopt: false };
  }
  const ours = (t) => ownTabs[String(t.id)] === "owner-sv";
  const grouped = (t) => typeof t.groupId === "number" && t.groupId >= 0;
  const rank = (t) => (ours(t) ? 2 : 0) + (grouped(t) ? 0 : 1);
  const keep = [...matches].sort((a, b) => rank(a) - rank(b) || (a.index ?? 0) - (b.index ?? 0))[0];
  // Закрываем только свои лишние копии; вкладки владельца не трогаем никогда.
  const close = matches.filter((t) => t !== keep && ours(t) && !t.active).map((t) => t.id);
  return { keep: keep.id, close, wait: false, adopt: !ours(keep) };
}
