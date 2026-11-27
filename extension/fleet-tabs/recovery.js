// Единая классификация состояния страниц обычных и быстрых агентов.
// Ошибка имеет приоритет над индикатором генерации и свежим MCP: сломанный интерфейс не работает.
// Разговор и сообщение пользователя не анализируем, используем только bounded probe от content.js.
export function classifyPageIssue(page, tab = {}, hasChat = false) {
  const url = String(tab.url || tab.pendingUrl || page?.url || "");
  if (/^chrome-error:/i.test(url) || /ERR_(?:CONNECTION|INTERNET|NETWORK|TIMED_OUT)/i.test(String(tab.title || "")))
    return { kind: "network-load", text: "browser navigation failed" };
  if (!page) return { kind: "no-response", text: "content script not responding" };
  if (page.pageLoadFailed) return { kind: "conversation-load", text: String(page.pageLoadFailed).slice(0, 160) };
  if (page.accountLoadFailed) return { kind: "account-load", text: String(page.accountLoadFailed).slice(0, 160) };
  if (page.cloudflarePageText) return { kind: "cloudflare", text: String(page.cloudflarePageText).slice(0, 160) };
  if (page.blocked || page.networkSuspicious || page.ui?.gateSensitive)
    return { kind: "blocked", text: String(page.blocked || page.networkSuspicious || page.ui?.gate || "").slice(0, 160) };
  const error = String(page.failed || page.interrupted || page.errorText || "");
  if (/no response from chatgpt|check your connection|network error|message delivery timed out|something went wrong|соединение прервано|ошибка сети/i.test(error))
    return { kind: "turn-failed", text: error.slice(0, 160) };
  if (page.interrupted) return { kind: "interrupted", text: String(page.interrupted).slice(0, 160) };
  if (page.failed) return { kind: "turn-failed", text: String(page.failed).slice(0, 160) };
  if (hasChat && page.composerPresent === false && !page.generating && !page.loading)
    return { kind: "composer-missing", text: "conversation composer missing" };
  return { kind: "", text: "" };
}

// Адрес сессии — закреплённая цель вкладки. Текущий URL после ошибки её не заменяет.
export const RECOVERY_DEFAULTS = {
  // Эксперимент владельца: открытие интерфейса и перезагрузка делят одну полосу.
  // Обычная отправка сообщения её не занимает.
  accountActionGapSec: 15,
  retryBaseSec: 60,
  retryMaxSec: 300,
  settleSec: 30, // наблюдавшийся выброс приходит через 4–8 с после загрузки
};

export function chatUrl(value) {
  try {
    const url = new URL(value);
    if (url.origin !== "https://chatgpt.com" || url.username || url.password) return null;
    const hit = /^\/(?:g\/[^/]+\/)?c\/([0-9a-f-]{8,})\/?$/i.exec(url.pathname);
    return hit ? { id: hit[1], url: `${url.origin}${url.pathname.replace(/\/$/, "")}` } : null;
  } catch { return null; }
}

export function projectPage(value) {
  try {
    const url = new URL(value);
    return url.origin === "https://chatgpt.com" && /^\/g\/[^/]+\/project\/?$/.test(url.pathname);
  } catch { return false; }
}

export function legacySession(record, projectUrl) {
  if (!record?.label || /^(owner|pult)/i.test(record.label)) return null;
  if (!/^[0-9a-f-]{8,}$/i.test(record.path || "")) return null;
  let url = `https://chatgpt.com/c/${record.path}`;
  if (projectPage(projectUrl)) url = projectUrl.replace(/\/project\/?$/, `/c/${record.path}`);
  return { label: record.label, ...chatUrl(url) };
}

export function rememberSession(previous, value, label, now) {
  if (previous) return previous; // в том числе случайно созданный чат после «продолжи» на проекте
  if (!label || /^(owner|pult)/i.test(label)) return null;
  const chat = chatUrl(value);
  return chat ? { label, ...chat, capturedAt: now } : null;
}

export function accountActionWait(account, now, cfg = RECOVERY_DEFAULTS) {
  return Math.max(0, (account?.lastActionAt || 0) + cfg.accountActionGapSec * 1000 - now);
}

// Локальный предохранитель, НЕ документированная квота ChatGPT. Одна история для
// открытий, перезагрузок и отправок всех типов агентов, включая быстрых.
export const ACCOUNT_ACTIONS_PER_HOUR = 120;
export function pacedAccountActionWait(account, now, cfg = RECOVERY_DEFAULTS, limit = ACCOUNT_ACTIONS_PER_HOUR) {
  const recent = (account?.history || []).filter((item) =>
    Number.isFinite(item?.at) && item.at <= now && now - item.at < 3600_000);
  const ceiling = recent.length >= limit
    ? Math.max(0, recent[recent.length - limit].at + 3600_000 - now) : 0;
  return Math.max(accountActionWait(account, now, cfg), ceiling);
}

export function markAccountAction(account, now, type, tabId, label = "") {
  const history = [...(account?.history || []).filter((item) => now - item.at < 3600_000),
    { at: now, type, tabId, label }].slice(-500);
  return { lastActionAt: now, history };
}

// Одно наблюдение UI. Отсутствие тоста НЕ означает восстановление: нужен исходный чат,
// его переписка/редактор и два наблюдения с интервалом settleSec без нового ограничения.
export function observeSession(session, page, previous, now, cfg = RECOVERY_DEFAULTS) {
  const none = { state: null, lost: false, restored: false, protected: false };
  if (!session) return none;
  const current = chatUrl(page.url);
  const matches = current?.id === session.id;
  const wrongChat = current && !matches;
  const displaced = projectPage(page.url) || wrongChat;
  const state = { since: now, attempts: 0, nextAt: now,
    lastAttemptAt: 0, stableSince: 0, ...previous };
  if (/^(owner|pult)/i.test(page.label || "") || page.unlockedFor > 0 ||
      (page.label && page.label !== session.label)) {
    return { ...none, state: previous || displaced ? state : null, protected: true };
  }
  if (!previous && !displaced) return none;
  // Первый возврат разрешён сразу. После каждой попытки применяется возрастающая пауза;
  // общий аккаунтный интервал дополнительно сериализует попытки разных вкладок.
  const ready = matches && page.ready && !page.rateLimited && !page.blocked && !page.loading;
  if (ready) {
    if (!state.stableSince) state.stableSince = now;
    if (now - state.stableSince >= cfg.settleSec * 1000) return { ...none, restored: true, durationMs: now - state.since };
  } else state.stableSince = 0;
  return { state, lost: !previous && displaced, restored: false, protected: false,
    canAttempt: Boolean(!ready && !page.loading && page.responded && page.composerEmpty && !page.generating &&
      !page.blocked && (displaced || matches) && now >= state.nextAt),
    settling: ready };
}

export function attemptedRecovery(state, now, cfg = RECOVERY_DEFAULTS) {
  const attempts = state.attempts + 1;
  const delay = Math.min(cfg.retryMaxSec, cfg.retryBaseSec * 2 ** Math.min(attempts - 1, 12));
  return { ...state, attempts, lastAttemptAt: now, nextAt: now + delay * 1000, stableSince: 0 };
}

export function recoveryCandidate(rows, account, now, cfg = RECOVERY_DEFAULTS) {
  if (accountActionWait(account, now, cfg)) return null;
  return rows.filter((row) => row.recovery.canAttempt && !row.recovery.protected)
    .sort((a, b) => a.recovery.state.lastAttemptAt - b.recovery.state.lastAttemptAt ||
      a.recovery.state.since - b.recovery.state.since || a.tabId - b.tabId)[0] || null;
}

// 13.10 (владелец): сайт часто сбрасывает старый чат на главную или проект, а новый чат после
// «Не удалось загрузить аккаунт» — на общую страницу. Писать туда нельзя: «продолжи» на главной
// создаёт новый чат, тратит лимит и сбивает учёт; агент вне проекта — без его инструкций.
// Перед каждой отправкой — проверка места. Не то место — перейти туда, где надо, и ничего не слать.
function projectId(value) {
  // Проект — сегмент после /g/; у настоящих проектов это g-p-<hex>-<slug>, и slug сайт меняет,
  // поэтому сравниваем hex, а если его нет — сегмент целиком.
  let segment = "";
  try { segment = (/^\/g\/([^/]+)/.exec(new URL(value).pathname) || [])[1] || ""; } catch { return ""; }
  const hit = /^g-p-([0-9a-f]+)/i.exec(segment);
  return hit ? hit[1].toLowerCase() : segment.toLowerCase();
}

export function placeCheck(action, stateUrl, session, projectUrl) {
  if (action === "start") {
    if (projectPage(stateUrl) && projectId(stateUrl) && projectId(stateUrl) === projectId(projectUrl)) {
      return { ok: true };
    }
    return { ok: false, navigate: projectUrl,
      reason: "новый чат не на странице проекта — открываю проект, первое сообщение не шлю" };
  }
  const here = chatUrl(stateUrl);
  if (session?.id) {
    if (here?.id === session.id) return { ok: true };
    return { ok: false, navigate: session.url,
      reason: "вкладка ушла со своего чата (сайт сбросил на другую страницу) — возвращаю чат, «продолжи» не шлю" };
  }
  if (here) return { ok: true };
  return { ok: false, navigate: null,
    reason: "вкладка не в чате, свой чат неизвестен — ничего не шлю" };
}

// 429 одного разговора не означает 429 аккаунта. Не опрашиваем его чаще
// Retry-After или 2→4→8→16→30 мин: иначе расширение само поддерживает лимит.
export function conversationApiCheck(previous, result, nowMs, conv = "") {
  const status = Number.isInteger(result?.status) ? result.status : null;
  const prev = previous?.conv === conv ? previous : null;
  if (status === null) {
    return prev?.status === 429 ? { ...prev, nextAt: Math.max(prev.nextAt || 0, nowMs + 60_000) } :
      { at: nowMs, status: null, conv, nextAt: nowMs + 60_000 };
  }
  if (status !== 429) return { at: nowMs, status, conv, nextAt: nowMs + 60_000 };
  const consecutive = prev?.status === 429 ? (prev.consecutive || 1) + 1 : 1;
  const first429At = prev?.status === 429 ? (prev.first429At || prev.at) : nowMs;
  const exponentialMs = Math.min(30 * 60_000, 2 * 60_000 * 2 ** Math.min(consecutive - 1, 12));
  const retryAfterSec = Math.max(0, Number(result.retryAfterSec) || 0);
  return { at: nowMs, status, conv, consecutive, first429At, retryAfterSec,
    nextAt: nowMs + Math.max(exponentialMs, retryAfterSec * 1000) };
}

export function conversation429Backoff(check, nowMs) {
  return check?.status === 429 ? Math.max(0, (check.nextAt || 0) - nowMs) : 0;
}

export function conversation429Unavailable(check, nowMs) {
  return check?.status === 429 && Number.isFinite(check.first429At) &&
    nowMs - check.first429At >= 30 * 60_000;
}

// «Не удалось загрузить аккаунт» или независимо подтверждённая 429 —
// пауза 10 → 20 → 40 … ≤ 120 мин; после тишины >= 2 длительностей сброс.
export function accountOverloaded(launch, nowMs) {
  const priorLevel = launch?.overloadLevel || 0;
  const lastAt = (launch?.overloads || []).at(-1);
  const inferredMs = lastAt && Number(launch?.overloadUntil) > lastAt
    ? launch.overloadUntil - lastAt
    : Math.min(10 * 2 ** Math.max(0, priorLevel - 1), 120) * 60_000;
  const lastPauseMs = Number(launch?.overloadPauseMs) > 0 ? launch.overloadPauseMs : inferredMs;
  const decayed = lastAt && nowMs - lastAt >= 2 * lastPauseMs ? 0 : priorLevel;
  const level = Math.min(decayed + 1, 5);
  const pauseMin = Math.min(10 * 2 ** (level - 1), 120);
  return { ...launch, overloadLevel: level, overloadUntil: nowMs + pauseMin * 60_000,
    overloadPauseMs: pauseMin * 60_000,
    overloads: [...(launch?.overloads || []).filter((at) => nowMs - at < 86_400_000), nowMs] };
}

export function accountRecovered(launch) {
  return launch?.overloadLevel ? { ...launch, overloadLevel: 0, overloadUntil: 0 } : launch;
}
