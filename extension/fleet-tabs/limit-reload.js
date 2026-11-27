// Автообновление вкладок ChatGPT при «A temporary limit is preventing this chat from loading.
// Try again later.» (владелец 20.11.2026, №116 п. 6). Касается любых вкладок chatgpt.com,
// в том числе не принадлежащих флоту. Перезагрузка — с backoff 1 → 2 → 5 → 10 мин, не больше
// одной за тик и не чаще полосы аккаунта (recovery.js). Загрузившийся чат сбрасывает backoff.
// Вкладку с набранным, но не отправленным текстом не трогаем. Выключатель — settings.limitReload
// (по умолчанию включено), исключения — storage.limitReloadExcluded {tabs:{id:true}, chats:{"c:<id>":true}}.

export const TEMP_LIMIT_TEXT = /A temporary limit is preventing this chat from loading/i;
export const LIMIT_RELOAD_BACKOFF_MIN = [1, 2, 5, 10];

export function limitChatKey(url) {
  const match = /\/c\/([0-9a-f][0-9a-f-]{7,})/i.exec(String(url || ""));
  return match ? `c:${match[1].toLowerCase()}` : "";
}

export function limitExcluded(excluded, tab) {
  const ex = excluded && typeof excluded === "object" ? excluded : {};
  const key = limitChatKey(tab?.url);
  return Boolean(ex.tabs?.[String(tab?.id)] || (key && ex.chats?.[key]));
}

// Переключить исключение для вкладки: по чату (переживает перезапуск браузера), иначе по tabId.
export function toggleLimitExclusion(excluded, tab, on) {
  const ex = excluded && typeof excluded === "object" ? excluded : {};
  const tabs = { ...(ex.tabs || {}) }, chats = { ...(ex.chats || {}) };
  const key = limitChatKey(tab?.url);
  if (key) { if (on) chats[key] = true; else delete chats[key]; }
  if (on && !key) tabs[String(tab?.id)] = true;
  if (!on) delete tabs[String(tab?.id)];
  return { tabs, chats };
}

const delayMs = (attempts) =>
  LIMIT_RELOAD_BACKOFF_MIN[Math.min(Math.max(0, attempts), LIMIT_RELOAD_BACKOFF_MIN.length - 1)] * 60_000;

/**
 * tabs — chrome.tabs; pages — Map(tabId → probe | null); mem — storage.limitReloadMem.
 * Первое наблюдение текста лимита ставит перезагрузку через 1 мин; каждая следующая — по лестнице.
 * accountWaitMs > 0 — полоса аккаунта занята: ждём, счётчик не тратим.
 */
export function limitReloadPlan({ tabs = [], pages = new Map(), mem = {}, enabled = true, excluded = {},
  now = Date.now(), accountWaitMs = 0 } = {}) {
  const reload = [], skipped = [], next = {};
  if (!enabled) return { reload, mem: {}, skipped };
  for (const tab of tabs) {
    const id = String(tab?.id);
    const old = mem && typeof mem === "object" ? mem[id] : null;
    const page = pages.get(tab?.id);
    if (page == null) { if (old) next[id] = old; continue; } // не прочитали — состояние не теряем
    if (!page.temporaryLimit) continue;                        // чат загрузился — backoff сброшен
    const state = old && old.url === tab.url ? { ...old } : { attempts: 0, nextAt: now + delayMs(0), url: tab.url, since: now };
    if (limitExcluded(excluded, tab)) { skipped.push({ id: tab.id, reason: "исключена владельцем" }); continue; }
    if (page.composerEmpty === false) {
      next[id] = state;
      skipped.push({ id: tab.id, reason: "в поле ввода набран текст" });
      continue;
    }
    if (now < state.nextAt || reload.length || accountWaitMs > 0) { next[id] = state; continue; }
    const attempts = (Number(state.attempts) || 0) + 1;
    next[id] = { ...state, attempts, lastAt: now, nextAt: now + delayMs(attempts) };
    reload.push(tab.id);
  }
  return { reload, mem: next, skipped };
}
