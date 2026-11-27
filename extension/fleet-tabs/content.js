// Content-скрипт на chatgpt.com: только смотрит на страницу и по команде фона
// отправляет «продолжи». Решений не принимает — их принимает policy.js в фоне.
//
// Разметка ChatGPT меняется, поэтому у каждого элемента несколько селекторов, а
// отправка проверяется по факту (появилась генерация или наше сообщение), а не
// считается успешной по нажатию.
(() => {
  // Versioned sentinel: after an unpacked-extension reload the old isolated world may
  // leave window.__fleetTabs behind while its chrome.runtime port is already invalid.
  // A version bump lets background.js inject the current content script into existing
  // ChatGPT tabs without requiring the user to close/reopen every conversation.
  const CONTENT_VERSION = 35;
  if (window.__fleetTabsVersion === CONTENT_VERSION) return;
  window.__fleetTabsVersion = CONTENT_VERSION;
  window.__fleetTabs = true;

  const SEL = {
    composer: [
      "[data-testid='prompt-textarea']",
      "#prompt-textarea",
      "[contenteditable='true'][data-lexical-editor='true']",
      "main [contenteditable='true'][role='textbox']",
      "main [contenteditable='plaintext-only'][role='textbox']",
      "main .ProseMirror[contenteditable='true']",
      "main .ProseMirror[contenteditable='plaintext-only']",
      "main [contenteditable='true'][data-placeholder]",
      "main [contenteditable='plaintext-only'][data-placeholder]",
      "div[contenteditable='true'][id*='prompt']",
      "textarea[name='prompt-textarea']",
      "#mobile-composer-prompt",
      "form textarea",
    ],
    send: [
      "#composer-submit-button",
      "[data-testid='send-button']",
      "button[data-testid*='send-button']",
      "button[aria-label*='Send' i]",
      "button[aria-label*='Отправ' i]",
      "button.composer-submit-btn",
    ],
    stop: [
      "[data-testid='stop-button']",
      "button[aria-label*='Stop' i]",
      "button[aria-label*='Останов' i]",
    ],
  };
  const CONTINUE_BUTTON = /^(continue generating|continue|продолжить генерацию|продолжить)$/i;
  const NEW_CHAT = "[data-testid*='new-chat'],[aria-label*='New chat' i],[aria-label*='Новый чат' i]";
  const ERROR_TEXT = /(no response from chatgpt|check your connection|something went wrong|network error|an error occurred|произошла ошибка|ошибка сети|не удалось)/i;
  const LABEL = /label\s*[=:]\s*['"«`]([^'"»`\s]+)['"»`]/i;
  // Окно ограничения ChatGPT: «Вы отправляете запросы слишком часто. Доступ к вашим
  // диалогам временно ограничен…» (21.09.2026) и родственные формулировки.
  const RATE_LIMIT = /(слишком часто|слишком много запросов|too many requests|sending requests too quickly)/i;
  const HARD_BLOCK = /(подозрительн\S* активност|достигли (лимита|предела)|подтвердите, что вы человек|unusual activity|reached (the|your) (usage |message )?limit|verify you are human|войдите (в аккаунт|снова)|log in again)/i;
  // Length belongs to this conversation, not to the account. Never route it to HARD_BLOCK.
  const CONVERSATION_FULL = /(you(?:'|’)ve reached the maximum length for this conversation|maximum (?:conversation|chat) length|достигл[иао]? (?:максимальн\S* )?(?:длины|длину) (?:этого )?(?:разговора|диалога|чата)|максимальн\S* длин\S* (?:этого )?(?:разговора|диалога|чата))/i;
  // Deliberately conservative until banner-vs-live calibration has enough observations.
  const CHAT_CAPACITY_CHARS = 80_000;
  // 17.10 (владелец): «подозрительная активность из вашей сети» — сигнал антифрода. Любое
  // продолжение работы только усиливает подозрение: расширение останавливает ВСЮ работу.
  const NETWORK_SUSPICIOUS = /((suspicious|unusual) (activity|traffic|requests?)[^.\n]{0,80}(your|this) (network|ip|device|connection)|(your|this) (network|ip)[^.\n]{0,60}(suspicious|unusual) (activity|traffic)|подозрительн\S* (активност|действ|трафик)\S*[^.\n]{0,80}(ваш\S* (сет|ip|устройств)|сет)|(ваше[йй]? (сет|ip|устройств)\S*|из вашей сети)[^.\n]{0,60}подозрительн)/i;
  const ACK_BUTTON = /^(понятно|ok|okay|got it)$/i;
  const DIALOGS = "[role='dialog'],[role='alertdialog'],dialog[open]";
  const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  let rateEvents = [];
  let noticeOpen = false;

  // Обрыв ответа: «Соединение прервано. Ожидание полного ответа» (22.09.2026) и
  // английские варианты. Ищется только короткий самостоятельный текст вне прозы ответа —
  // агент может и сам написать эту фразу в отчёте.
  const INTERRUPTED = /^\s*(соединение (было )?(прервано|потеряно)|ожидание полного ответа|connection (was )?(interrupted|lost)|network connection lost|waiting for (the )?(full|complete) (response|answer|message))/i;

  // Ход сорвался и сам не продолжится: «Время доставки сообщения истекло. Попробуйте ещё
  // раз.» (22.09.2026) и родственные. Для расширения это «надо продолжить»: нажать
  // «Повторить», если кнопка есть, иначе отправить «продолжи».
  const FAILED_TURN = /(no response from chatgpt|check your connection|resume stream unavailable|поток продолжения недоступен|время доставки сообщения истекло|время ожидания (ответа )?истекло|сообщение не (было )?доставлено|не удалось (отправить|получить|сгенерировать|загрузить) (сообщение|ответ)|что-то пошло не так|произошла ошибка|ошибка сети|message delivery timed out|message (was )?not delivered|request timed out|something went wrong|an error occurred|network error)/i;
  // 13.10: экран перегрузки аккаунта — сигнал исчерпания лимита; кнопку «Продолжить» жмём сами.
  const ACCOUNT_LOAD_FAILED = /(не удалось загрузить (?:ваш |этот )?аккаунт|unable to load (?:your )?account|could(?:n't| not) load (?:your )?account|failed to load (?:your )?account)/i;
  const ACCOUNT_CONTINUE = /^(продолжить|continue)$/i;
  const PAGE_LOAD_FAILED = /(не удалось загрузить (?:этот )?(?:разговор|чат)(?: chatgpt)?|could(?:n't| not) load (?:this )?(?:conversation|chat)|unable to load (?:this )?(?:conversation|chat))/i;
  const RETRY_BUTTON = /^(повторить|попробовать (снова|ещё раз|еще раз)|попробуйте (ещё|еще) раз|retry|try again|regenerate|сгенерировать (заново|ещё раз|еще раз))$/i;
  // Агент закончил ход ожиданием ручного решения/прав, хотя throughput-контур должен
  // разрешать это сам. Такой текст нельзя лечить бесконечными «продолжи».
  const WAITING_FOR_PERMISSION = /(жду\s+(владельц|owner|одобрени|разрешени)|нужн(?:о|ы|ен|а)?\s+(?:ручн\S*\s+)?(?:одобрени|разрешени|доступ|прав)|требуется\s+(?:ручн\S*\s+)?(?:одобрени|разрешени|доступ)|не могу продолжить без\s+(?:одобрени|разрешени|доступ)|waiting for\s+(?:owner|approval|permission)|need(?:s|ed)?\s+(?:owner\s+)?(?:approval|permission|access))/i;
  const STRONG_PERMISSION_BLOCK = /(жду\s+(?:владельц|owner|одобрени|разрешени)|не могу продолжить без\s+(?:одобрени|разрешени|доступ)|(?:cannot|can['’]?t)\s+continue\s+without\s+(?:approval|permission|access)|waiting for\s+(?:owner|approval|permission))/i;
  const NEGATED_PERMISSION_NEED = /(?:не\s+(?:нужно|нужен|нужна|нужны|требуется|требуются|жду)|без\s+(?:ручн\S*\s+)?(?:одобрения|разрешения)|(?:do\s+not|don't|does\s+not|doesn't|no\s+longer)\s+(?:need|require|wait\s+for))[^.!?\n]{0,100}(?:одобрени|разрешени|доступ|прав|owner|approval|permission|access)/i;

  // Новые распознаватели сбоев не должны ронять probe: упал probe — расширение теряет
  // вкладку целиком («страница не отвечает»), а не одну подсказку.
  const safe = (fn, fallback) => {
    try {
      return fn();
    } catch {
      return fallback;
    }
  };

  const find = (list) => {
    for (const selector of list) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    return null;
  };

  function usableComposer(el) {
    if (!el || !visible(el) || el.closest("#fleet-lock,nav,aside,[aria-hidden='true'],[hidden],[inert],[aria-expanded='false']")) return false;
    // Floating/docked duplicates must never win over the main conversation composer.
    const float = el.closest("[data-state='closed'],[data-state='collapsed'],[aria-label*='floating chat' i]");
    if (float && (!visible(float) || float.getAttribute("data-state") !== null)) return false;
    return true;
  }
  function findComposer() {
    for (const selector of SEL.composer) {
      const candidates = [...document.querySelectorAll(selector)].filter(usableComposer);
      const active = candidates.find((el) => el.closest("main") && el.closest("form")) ||
        candidates.find((el) => el.closest("main")) || candidates.find((el) => el.closest("form")) || candidates[0];
      if (active) return active;
    }
    // Новый project composer (25.09.2026) может быть обычным contenteditable без
    // прежних id/data-testid. Сначала привязываемся к placeholder внутри него.
    for (const hint of document.querySelectorAll("main [data-placeholder],main [placeholder]")) {
      if (!visible(hint)) continue;
      const editor = hint.matches("[contenteditable='true'],textarea")
        ? hint
        : hint.closest("[contenteditable='true'],textarea");
      if (usableComposer(editor)) return editor;
    }
    // Последний безопасный fallback: на странице ChatGPT рабочий редактор — видимый
    // contenteditable/textarea в main, не в диалоге и не в нашей плашке.
    return [...document.querySelectorAll("main [contenteditable],main textarea")]
      .find((el) => usableComposer(el) && (el.tagName === "TEXTAREA" || el.isContentEditable) &&
        !el.closest("[role='dialog'],[role='alertdialog']")) || null;
  }

  // 17.11: без флагов против троттлинга скрытая (свёрнутая или закрытая другими окнами) страница
  // попадает под интенсивное сдерживание Chrome — цепочки setTimeout срабатывают раз в минуту,
  // и sleep(200) в цикле ожидания растягивает одно действие на десятки минут. Пока страница
  // скрыта, таймер держит service worker расширения (его не сдерживают): ответ на сообщение
  // будит content-скрипт без таймера страницы. Свой setTimeout — только страховка.
  const sleep = (ms) => new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    if (document.hidden && globalThis.chrome?.runtime?.id) {
      try {
        chrome.runtime.sendMessage({ type: "fleet-sleep", ms }, () => { void chrome.runtime.lastError; finish(); });
        setTimeout(finish, ms + 2000);
        return;
      } catch { /* контекст расширения потерян — обычный таймер */ }
    }
    setTimeout(finish, ms);
  });

  // Новая DOM-разметка: h4[data-conversation-role] может лежать в узкой обёртке
  // заголовка, а весь текст хода — в её соседнем блоке на 1–3 уровня выше.
  // Поднимаемся лишь в пределах ОДНОГО хода: никогда не захватываем соседнюю роль.
  const roleHeader = "h4[data-conversation-role]";
  const turnContainer = (h) => {
    let node = h.parentElement;
    let fallback = node;
    for (let depth = 0; node && depth < 9; depth++, node = node.parentElement) {
      if (node.querySelectorAll(roleHeader).length > 1) break;
      fallback = node;
      const clone = node.cloneNode(true);
      for (const chrome of clone.querySelectorAll(
        roleHeader + ",button,svg,[data-testid*='action'],[aria-hidden='true']"
      )) chrome.remove();
      if ((clone.textContent || "").trim()) return node;
    }
    return fallback;
  };
  const messages = () => {
    const legacy = document.querySelectorAll("[data-message-author-role]");
    if (legacy.length) return legacy;
    const turns = document.querySelectorAll("section[data-turn='user'],section[data-turn='assistant'],[data-turn='user'],[data-turn='assistant']");
    if (turns.length) return turns;
    return [...document.querySelectorAll("main h4[data-conversation-role]")]
      .map(turnContainer).filter(Boolean);
  };
  const roleOf = (el) => el?.getAttribute("data-message-author-role") || el?.getAttribute("data-turn") ||
    el?.querySelector?.(roleHeader)?.getAttribute("data-conversation-role") || "";
  const messageText = (el) => {
    if (!el) return "";
    const header = el.querySelector?.(roleHeader);
    if (!header) return (el.innerText || el.textContent || "").trim();
    const clone = el.cloneNode(true);
    for (const chrome of clone.querySelectorAll(
      roleHeader + ",button,svg,[data-testid*='action'],[aria-hidden='true']"
    )) chrome.remove();
    return (clone.innerText || clone.textContent || "").trim();
  };
  function conversationUsage() {
    // Visible transcript including tool-result text; no network requests or full transcript IPC.
    const turns = [...messages()];
    let chars = 0;
    let cyrillic = 0;
    for (const turn of turns) {
      const value = messageText(turn);
      chars += value.length;
      cyrillic += (value.match(/[\u0400-\u04ff]/g) || []).length;
    }
    return { chars, turns: turns.length, cyrillic,
      // Informational estimate only; not used for a security or quota decision.
      estimatedTokens: Math.round((chars - cyrillic) / 4 + cyrillic / 2),
      fill: Math.min(1, chars / CHAT_CAPACITY_CHARS) };
  }
  // Коннектор добавляет @mention к стартовому сообщению, но не меняет тело задачи.
  const comparableUserText = (value) => String(value || "")
    .replace(/[\u200B\uFEFF]/g, "").replace(/\s+/g, " ").trim()
    .replace(/^@Dev['’]s Computer dev-mcp\+\s*/i, "").trim();
  // 17.11: в чате наше сообщение показано ОТРЕНДЕРЕННЫМ markdown (без «1.», `…`, **…**), и
  // дословный поиск не находил отправленное задание → «не ушло», повтор, пауза. Сверка по ключу
  // букв/цифр (как для поля ввода); у длинного текста — по первым 300 знакам ключа (UI сворачивает).
  const confirmsUserMessage = (actual, expected) => {
    const needle = comparableUserText(expected);
    if (!needle) return false;
    if (comparableUserText(actual).includes(needle)) return true;
    const key = composerKey(needle).slice(0, 300);
    return key.length >= 20 && composerKey(comparableUserText(actual)).includes(key);
  };
  function waitingForPermissionText() {
    const all = [...messages()];
    for (let i = all.length - 1; i >= 0; i -= 1) {
      if (roleOf(all[i]) !== "assistant") continue;
      const text = (all[i].innerText || all[i].textContent || "").trim();
      if (!WAITING_FOR_PERMISSION.test(text)) return "";
      if (NEGATED_PERMISSION_NEED.test(text) && !STRONG_PERMISSION_BLOCK.test(text)) return "";
      return text.slice(0, 500);
    }
    return "";
  }
  // Только текст, который вставили мы и который человек с тех пор не менял.
  // После неудачной отправки он не должен превращаться в вечный «черновик человека».
  let pendingSubmission = "";
  document.addEventListener("input", (event) => {
    if (event.isTrusted) pendingSubmission = "";
  }, true);

  // «Тишина» — время, в течение которого не менялась переписка: число сообщений, длина
  // последнего и идёт ли генерация. Любые изменения DOM для этого не годятся: живая
  // страница ChatGPT шевелится и без агента, и тишина никогда бы не наступала.
  let lastChangeAt = Date.now();
  let lastSignature = "";
  // Отдельно — когда менялся сам текст переписки, без учёта генерации: «Стоп» на месте, а
  // текст стоит — это зависание (22.09: вкладка висела без всякой надписи).
  let textChangeAt = Date.now();
  let textSignature = "";
  let idleWakeTimer = 0;
  let lastGenerating = false;

  function scheduleIdleWake(reason = "turn-idle", delayMs = 5500) {
    clearTimeout(idleWakeTimer);
    idleWakeTimer = setTimeout(() => {
      try {
        const sent = chrome.runtime?.sendMessage?.({ type: "tick-now", reason });
        sent?.catch?.(() => {});
      } catch {
        // Extension may have been reloaded while this isolated world is still alive.
      }
    }, delayMs);
  }

  function signature() {
    const all = messages();
    const last = all[all.length - 1];
    return `${all.length}|${last ? (last.textContent || "").length : 0}|${Boolean(stopButton())}`;
  }
  function touchIfChanged() {
    const generating = Boolean(stopButton());
    const current = signature();
    if (current !== lastSignature) {
      lastSignature = current;
      lastChangeAt = Date.now();
    }
    const text = current.slice(0, current.lastIndexOf("|"));
    const textChanged = text !== textSignature;
    if (textChanged) {
      textSignature = text;
      textChangeAt = Date.now();
    }
    // Do not wait for the next 30-second background alarm after a turn ends.
    // If ChatGPT exposes a Stop button, wake once after true -> false. If the new
    // UI does not expose it, stable assistant text still schedules the same wake.
    if ((lastGenerating && !generating) || (textChanged && !generating)) {
      scheduleIdleWake("turn-idle");
    }
    if (generating && !lastGenerating) scheduleIdleWake("turn-started", 100);
    lastGenerating = generating;
  }
  setInterval(touchIfChanged, 2000);

  // «Стоп» генерации: точный data-testid, а по подписи — только внутри поля ввода, чтобы
  // посторонняя кнопка «Stop…» на странице не выглядела вечной генерацией.
  function stopButton() {
    const exact = document.querySelector("[data-testid='stop-button']");
    if (exact) return exact;
    const root = composerRoot();
    return root ? root.querySelector("button[aria-label*='Stop' i],button[aria-label*='Останов' i]") : null;
  }
  const normalizeComposer = (value) => String(value ?? "").replace(/[\u200B\uFEFF]/g, "").trim();
  // 15.11: \u043C\u043D\u043E\u0433\u043E\u0441\u0442\u0440\u043E\u0447\u043D\u044B\u0439 \u0442\u0435\u043A\u0441\u0442 \u0432 contenteditable \u043F\u0440\u0435\u0432\u0440\u0430\u0449\u0430\u0435\u0442\u0441\u044F \u0432 \u0430\u0431\u0437\u0430\u0446\u044B (\n \u2192 \n\n, NBSP, \u0445\u0432\u043E\u0441\u0442\u043E\u0432\u044B\u0435
  // \u043F\u0440\u043E\u0431\u0435\u043B\u044B) \u2014 \u0441\u0442\u0440\u043E\u0433\u043E\u0435 \u0440\u0430\u0432\u0435\u043D\u0441\u0442\u0432\u043E \u0434\u0430\u0432\u0430\u043B\u043E \u00AB\u0442\u0435\u043A\u0441\u0442 \u043D\u0435 \u0432\u0441\u0442\u0430\u043B \u0432 \u043F\u043E\u043B\u0435 \u0432\u0432\u043E\u0434\u0430\u00BB \u043F\u0440\u0438 \u0432\u0441\u0442\u0430\u0432\u0448\u0435\u043C \u0442\u0435\u043A\u0441\u0442\u0435.
  // 15.11, \u043F\u043E\u0437\u0436\u0435: \u0441\u0442\u0440\u043E\u043A\u0438 \u00AB1. \u2026\u00BB, \u00AB- \u2026\u00BB \u0440\u0435\u0434\u0430\u043A\u0442\u043E\u0440 \u043F\u0440\u0435\u0432\u0440\u0430\u0449\u0430\u0435\u0442 \u0432 \u0441\u043F\u0438\u0441\u043E\u043A \u2014 \u043D\u043E\u043C\u0435\u0440\u0430 \u0438 \u043C\u0430\u0440\u043A\u0435\u0440\u044B \u043F\u0440\u043E\u043F\u0430\u0434\u0430\u044E\u0442 \u0438\u0437
  // innerText; \u0441\u0432\u0435\u0440\u043A\u0430 \u0438\u0445 \u043D\u0435 \u0443\u0447\u0438\u0442\u044B\u0432\u0430\u0435\u0442 (\u0438\u043D\u0430\u0447\u0435 \u00AB\u043D\u0435 \u0432\u0441\u0442\u0430\u043B\u043E\u00BB \u2192 \u043F\u043E\u0432\u0442\u043E\u0440\u043D\u0430\u044F \u0432\u0441\u0442\u0430\u0432\u043A\u0430 \u2192 \u0442\u0435\u043A\u0441\u0442 \u0432\u0434\u0432\u043E\u0435 \u2192 \u0432\u0435\u0447\u043D\u044B\u0439 \u043E\u0442\u043A\u0430\u0437).
  // 16.11: \u043f\u043e\u043b\u0435 \u0432\u0432\u043e\u0434\u0430 \u0442\u0435\u043f\u0435\u0440\u044c markdown-\u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440 (data-composer-markdown): `\u043a\u043e\u0434`, **\u0436\u0438\u0440\u043d\u044b\u0439**, # \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0438,
  // > \u0446\u0438\u0442\u0430\u0442\u044b, \\u044d\u043a\u0440\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435, \u00ab\u0442\u0438\u043f\u043e\u0433\u0440\u0430\u0444\u0438\u043a\u0430\u00bb \u0440\u0435\u043d\u0434\u0435\u0440\u044f\u0442\u0441\u044f \u0438\u043d\u0430\u0447\u0435, \u0447\u0435\u043c \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044b\u0439 \u0442\u0435\u043a\u0441\u0442, \u2014 \u0441\u0432\u0435\u0440\u043a\u0430 \u043f\u043e
  // \u043f\u0440\u043e\u0431\u0435\u043b\u0430\u043c/\u043c\u0430\u0440\u043a\u0435\u0440\u0430\u043c \u0434\u0430\u0432\u0430\u043b\u0430 \u00ab\u0442\u0435\u043a\u0441\u0442 \u043d\u0435 \u0432\u0441\u0442\u0430\u043b \u0432 \u043f\u043e\u043b\u0435 \u0432\u0432\u043e\u0434\u0430\u00bb \u043f\u0440\u0438 \u0432\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043d\u043e\u043c \u0442\u0435\u043a\u0441\u0442\u0435 \u0438 \u043f\u0430\u0443\u0437\u0443 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438.
  // \u0421\u0432\u0435\u0440\u044f\u0435\u043c \u0442\u043e\u043b\u044c\u043a\u043e \u0431\u0443\u043a\u0432\u044b \u0438 \u0446\u0438\u0444\u0440\u044b \u043f\u043e \u043f\u043e\u0440\u044f\u0434\u043a\u0443: \u0434\u043b\u044f \u00ab\u0432 \u043f\u043e\u043b\u0435 \u0440\u043e\u0432\u043d\u043e \u043d\u0430\u0448 \u0442\u0435\u043a\u0441\u0442\u00bb \u044d\u0442\u043e\u0433\u043e \u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e.
  // 17.11: composerText() \u0431\u0435\u0440\u0451\u0442 textContent \u2014 \u0430\u0431\u0437\u0430\u0446\u044B ProseMirror \u0441\u043A\u043B\u0435\u0435\u043D\u044B \u0411\u0415\u0417 \n, \u043F\u043E\u044D\u0442\u043E\u043C\u0443 \u00AB1. \u00BB \u0432
  // \u043F\u043E\u043B\u0435 \u043D\u0435 \u0432 \u043D\u0430\u0447\u0430\u043B\u0435 \u0441\u0442\u0440\u043E\u043A\u0438. \u041D\u0443\u043C\u0435\u0440\u0430\u0446\u0438\u044F \u0441\u0440\u0435\u0437\u0430\u043B\u0430\u0441\u044C \u0442\u043E\u043B\u044C\u043A\u043E \u0443 \u043E\u0436\u0438\u0434\u0430\u0435\u043C\u043E\u0433\u043E \u0442\u0435\u043A\u0441\u0442\u0430 \u2192 \u043B\u044E\u0431\u043E\u0439 say \u0441
  // \u043D\u0443\u043C\u0435\u0440\u043E\u0432\u0430\u043D\u043D\u044B\u043C\u0438 \u043F\u0443\u043D\u043A\u0442\u0430\u043C\u0438 \u0432\u0435\u0447\u043D\u043E \u00AB\u043D\u0435 \u0432\u0441\u0442\u0430\u0432\u0430\u043B\u00BB. \u0421\u0440\u0435\u0437\u0430\u0435\u043C \u043C\u0430\u0440\u043A\u0435\u0440\u044B \u00ABN. \u00BB/\u00ABN) \u00BB \u0432 \u043B\u044E\u0431\u043E\u043C \u043C\u0435\u0441\u0442\u0435, \u0441\u0438\u043C\u043C\u0435\u0442\u0440\u0438\u0447\u043D\u043E.
  const composerKey = (value) => normalizeComposer(value)
    .replace(/(^|\n)[ \t\u00A0]*\d{1,3}[.)][ \t\u00A0]+/g, "$1")
    .replace(/(^|[^\p{L}\p{N}])\d{1,3}[.)](?=[ \t\u00A0])/gu, "$1")
    .normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const sameComposerText = (a, b) => composerKey(a) === composerKey(b);

  function projectPlaceholderOnly(el) {
    if (!el || typeof el.value === "string" || messages().length) return false;
    const raw = normalizeComposer(el.innerText ?? el.textContent);
    return /^(?:(?:новый чат в|new chat in|ask anything in)\s+\S.+|ask anything|message chatgpt|спросить что угодно|спросите что-нибудь|сообщение chatgpt)$/i.test(raw);
  }

  function composerText(el) {
    if (!el) return "";
    const normalize = normalizeComposer;
    if (typeof el.value === "string") return normalize(el.value);

    const raw = normalize(el.innerText ?? el.textContent);
    if (!raw) return "";

    // Current project landing (25.09.2026) may render its placeholder as literal
    // text inside the contenteditable without any data-placeholder attribute.
    // With no conversation turns yet, this exact UI hint is not a user draft.
    if (projectPlaceholderOnly(el)) return "";

    // Новый project composer может держать видимый placeholder реальным DOM-текстом
    // внутри contenteditable. Для логики запуска это всё ещё пустое поле.
    const clone = el.cloneNode?.(true);
    if (clone?.querySelectorAll) {
      for (const ghost of clone.querySelectorAll("[data-placeholder],[data-lexical-placeholder],[aria-hidden='true']")) {
        if (ghost.getAttribute?.("aria-hidden") === "true") {
          ghost.remove();
          continue;
        }
        const ghostText = normalize(ghost.textContent);
        const ghostHints = [
          ghost.getAttribute?.("data-placeholder"),
          ghost.getAttribute?.("data-lexical-placeholder"),
          ghost.getAttribute?.("placeholder"),
          ghost.getAttribute?.("aria-label"),
        ].map(normalize).filter(Boolean);
        // Удаляем только настоящий placeholder. Если execCommand уже вставил текст
        // внутрь этого же узла, его textContent отличается от подсказки и это ввод.
        if (!ghostText || ghostHints.includes(ghostText)) ghost.remove();
      }
      const cleaned = normalize(clone.textContent);
      if (cleaned) return cleaned;

      const hints = [
        el.getAttribute?.("data-placeholder"),
        el.getAttribute?.("placeholder"),
        el.isContentEditable ? el.getAttribute?.("aria-label") : "",
      ].map(normalize).filter(Boolean);
      if (hints.includes(raw)) return "";
    }
    return raw;
  }

  function label() {
    const first = document.querySelector("[data-message-author-role='user'],section[data-turn='user'],[data-turn='user']") ||
      [...messages()].find((el) => roleOf(el) === "user");
    const match = first ? LABEL.exec(first.innerText || "") : null;
    return match ? match[1] : "";
  }

  function lastRole() {
    const all = messages();
    if (!all.length) return null;
    const last = all[all.length - 1];
    return roleOf(last) || null;
  }

  function continueButton() {
    const scope = document.querySelector("main") || document;
    return [...scope.querySelectorAll("button")].find(
      (b) => b.offsetParent !== null && CONTINUE_BUTTON.test((b.innerText || "").trim()),
    );
  }

  function errorText() {
    const scope = document.querySelector("main") || document.body;
    const hit = [...scope.querySelectorAll("div,span,p")]
      .slice(-400)
      .find((el) => el.childElementCount === 0 && ERROR_TEXT.test(el.textContent || ""));
    return hit ? hit.textContent.trim().slice(0, 160) : "";
  }

  // Ищем в диалогах, всплывающих уведомлениях и последних листьях страницы; своё окно
  // блокировки (#fleet-lock) не в счёт.
  // 15.11: новая разметка ChatGPT (сообщение — родитель h4[data-conversation-role]) и поле ввода не
  // исключались — текст задачи, цитирующий «Unusual activity…», остановил весь флот на 40 мин.
  const NOT_NOTICE = ":has(> h4[data-conversation-role]),#prompt-textarea,[contenteditable='true'],textarea,form";
  function noticeText(pattern) {
    const places = [
      ...document.querySelectorAll("[role='dialog'],[role='alertdialog'],[role='alert'],[role='status'],[data-testid*='toast']"),
    ];
    for (const el of places) {
      const text = (el.innerText || "").trim();
      if (visible(el) && text && pattern.test(text) &&
        !el.closest(`#fleet-lock,[data-message-author-role],[data-turn-key],article,.markdown,.prose,${NOT_NOTICE}`)) return text.slice(0, 300);
    }
    const scope = document.querySelector("main") || document.body;
    // Текст переписки не в счёт: агент вполне может написать «rate limit» в отчёте.
    const hit = [...scope.querySelectorAll("div,span,p")]
      .slice(-300)
      .find(
        (el) =>
          el.childElementCount === 0 &&
          visible(el) &&
          !el.closest(`[data-message-author-role],[data-turn-key],[data-testid^='conversation-turn'],article,${NOT_NOTICE}`) &&
          pattern.test(el.textContent || ""),
      );
    return hit ? hit.textContent.trim().slice(0, 200) : "";
  }

  const rateLimitText = () => noticeText(RATE_LIMIT);

  function rateLimitKind() {
    const text = rateLimitText();
    if (!text) return "";
    const modal = [...document.querySelectorAll(DIALOGS)].some((el) =>
      visible(el) && RATE_LIMIT.test(el.innerText || ""));
    return modal ? "dialog" : "toast";
  }

  async function dismissRateLimit() {
    const dialog = [...document.querySelectorAll(DIALOGS)].find((el) => {
      const text = el.innerText || "";
      return visible(el) && RATE_LIMIT.test(text) && !HARD_BLOCK.test(text) &&
        !el.closest("[data-message-author-role],[data-turn-key],article,.markdown,.prose,#fleet-lock");
    });
    if (!dialog) return { ok: false, detail: "нет подходящего окна «слишком часто»" };
    if (!noticeOpen) rateEvents.push({ type: "warning", at: Date.now() });
    noticeOpen = true;
    const button = [...dialog.querySelectorAll("button")].find((b) =>
      visible(b) && !b.disabled && ACK_BUTTON.test((b.innerText || b.getAttribute("aria-label") || "").trim()));
    if (!button) return { ok: false, detail: "в окне нет кнопки «Понятно»" };
    button.click();
    for (let i = 0; i < 10; i += 1) {
      await sleep(100);
      if (!rateLimitText()) {
        noticeOpen = false;
        rateEvents.push({ type: "dismissed", at: Date.now() });
        return { ok: true, detail: "«Понятно» нажато, окно исчезло" };
      }
    }
    return { ok: false, detail: "«Понятно» нажато, окно осталось" };
  }

  function interruptedText() {
    const scope = document.querySelector("main") || document.body;
    const hit = [...scope.querySelectorAll("div,span,p")]
      .slice(-400)
      .find((el) => {
        if (el.childElementCount !== 0 || el.closest("#fleet-lock,.markdown,.prose,[data-message-author-role='user'],:has(> h4[data-conversation-role='user'])")) return false;
        const text = (el.textContent || "").trim();
        return text.length <= 160 && INTERRUPTED.test(text);
      });
    if (hit) return hit.textContent.trim().slice(0, 160);
    // Запасной путь: надпись внутри последнего ответа ассистента — только короткой
    // отдельной строкой. Ложное срабатывание стоит одной перезагрузки (reloadPlan
    // ограничивает частоту), пропуск — вкладки, висящей до утра.
    const all = messages();
    const last = all[all.length - 1];
    if (!last || roleOf(last) !== "assistant") return "";
    const turn = last.closest("[data-testid^='conversation-turn'],article") || last;
    const inTurn = [...turn.querySelectorAll("div,span,p")].find((el) => {
      if (el.childElementCount !== 0) return false;
      const text = (el.textContent || "").trim();
      return text.length <= 80 && INTERRUPTED.test(text);
    });
    return inTurn ? inTurn.textContent.trim().slice(0, 160) : "";
  }

  // Надпись о сбое — короткий самостоятельный текст вне прозы ответа и вне нашего
  // сообщения: агент может написать «что-то пошло не так» в отчёте, это не сбой.
  // Только в последнем ходе и под ним: старая ошибка выше по переписке — не текущий сбой,
  // иначе вкладку «повторяли» бы каждые полторы минуты до остановки.
  function lastTurn() {
    const all = messages();
    const last = all[all.length - 1];
    return last ? last.closest("[data-testid^='conversation-turn'],article") || last : null;
  }
  const inOrAfter = (anchor, el) =>
    !anchor || anchor === el || Boolean(anchor.compareDocumentPosition(el) & (Node.DOCUMENT_POSITION_FOLLOWING | Node.DOCUMENT_POSITION_CONTAINED_BY));

  function failedText() {
    if (safe(pageLoadFailureText, "") || safe(accountLoadFailureText, "")) return "";
    const scope = document.querySelector("main") || document.body;
    const anchor = lastTurn();
    const hit = [...scope.querySelectorAll("div,span,p")]
      .slice(-500)
      .find((el) => {
        if (el.closest("#fleet-lock,.markdown,.prose,[data-message-author-role='user'],:has(> h4[data-conversation-role='user']),pre,code")) return false;
        if (!inOrAfter(anchor, el)) return false;
        const text = (el.textContent || "").trim();
        if (!text || text.length > 300 || !FAILED_TURN.test(text)) return false;
        // Новый красный Resume stream unavailable — контейнер с кнопкой Retry,
        // поэтому он не leaf. Старый фильтр childElementCount===0 его отбрасывал.
        const retry = [...el.querySelectorAll?.("button") || []].some((b) =>
          RETRY_BUTTON.test((b.innerText || b.getAttribute("aria-label") || "").trim()));
        return el.childElementCount === 0 || retry;
      });
    if (hit) return hit.textContent.trim().slice(0, 160);
    for (const el of document.querySelectorAll("[role='alert'],[role='status'],[data-testid*='toast']")) {
      const text = (el.innerText || "").trim();
      if (text && text.length <= 300 && FAILED_TURN.test(text) && !el.closest("#fleet-lock")) return text.slice(0, 160);
    }
    return "";
  }

  function retryButton() {
    const scope = document.querySelector("main") || document;
    const anchor = lastTurn();
    return [...scope.querySelectorAll("button")].find(
      (b) =>
        b.offsetParent !== null &&
        inOrAfter(anchor, b) &&
        !b.closest("#fleet-lock,[data-message-author-role='user'],:has(> h4[data-conversation-role='user'])") &&
        (RETRY_BUTTON.test((b.innerText || "").trim()) || RETRY_BUTTON.test((b.getAttribute("aria-label") || "").trim())),
    );
  }

  function accountLoadFailureText() {
    const scope = document.body;
    if (!scope) return "";
    const hit = [...scope.querySelectorAll("h1,h2,h3,[role='alert'],[role='dialog'],div,span,p")]
      .slice(-500)
      .find((el) => {
        if (!visible(el) || el.closest("#fleet-lock,[data-message-author-role],article,.markdown,.prose,pre,code")) return false;
        const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
        return text && text.length <= 240 && ACCOUNT_LOAD_FAILED.test(text);
      });
    return hit ? (hit.innerText || hit.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200) : "";
  }

  function accountContinueButton() {
    return [...document.querySelectorAll("button,a[role='button']")].find((b) =>
      visible(b) && ACCOUNT_CONTINUE.test((b.innerText || b.getAttribute("aria-label") || "").trim()));
  }

  // 20.11.2026 (№116 п. 6): экран «A temporary limit is preventing this chat from loading».
  const TEMP_LIMIT = /A temporary limit is preventing this chat from loading/i;
  function temporaryLimitText() {
    const scope = document.body;
    if (!scope) return "";
    const hit = [...scope.querySelectorAll("h1,h2,h3,[role='alert'],[role='status'],div,span,p")]
      .slice(-500)
      .find((el) => {
        if (!visible(el) || el.closest("#fleet-lock,[data-message-author-role],article,.markdown,.prose,pre,code")) return false;
        const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
        return text && text.length <= 240 && TEMP_LIMIT.test(text);
      });
    return hit ? (hit.innerText || hit.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200) : "";
  }

  function pageLoadFailureText() {
    const scope = document.querySelector("main") || document.body;
    if (!scope) return "";
    const hit = [...scope.querySelectorAll("h1,h2,h3,[role='alert'],[role='status'],div,span,p")]
      .slice(-500)
      .find((el) => {
        if (!visible(el) || el.closest("#fleet-lock,[data-message-author-role],article,.markdown,.prose,pre,code")) return false;
        const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
        return text && text.length <= 240 && PAGE_LOAD_FAILED.test(text);
      });
    return hit ? (hit.innerText || hit.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200) : "";
  }

  function cloudflarePageText() {
    // A bounded excerpt around the REAL Cloudflare heading, never a dump of
    // conversation content, drafts or credentials to the server.
    if (findComposer() || messages().length) return "";
    const text = safe(() => String(document.body?.innerText || document.body?.textContent || ""), "");
    const at = text.toLowerCase().indexOf("unable to load site");
    if (at < 0) return "";
    return text.slice(Math.max(0, at - 50), at + 350).replace(/\s+/g, " ").trim().slice(0, 400);
  }

  const SAFE_GATE_BUTTON = /^(continue|продолжить|got it|понятно|not now|не сейчас|skip|пропустить|close|закрыть|dismiss|позже|maybe later)$/i;
  const SENSITIVE_GATE = /(verify(?:\s+that)?\s+you(?:\s+are)?\s+human|captcha|sign in|log in|войдите|подтвердите.{0,40}человек|(?:grant|allow|request)\w*\s+(?:permission|access)|permission\s+(?:required|request)|approval\s+(?:required|request)|authorize|connect(?:ing)?\s+(?:your\s+)?(?:account|app|application|connector|service)|разреш(?:ить|ение).{0,40}(?:доступ|подключ)|одобрен|авторизац|подключ(?:ить|ение).{0,40}(?:аккаунт|уч[её]тн|коннектор|приложен))/i;

  function transientGate() {
    const scopes = [
      ...document.querySelectorAll("[role='dialog'],[role='alertdialog'],dialog[open]"),
      ...document.querySelectorAll("main [role='status'],main [role='alert']"),
    ].filter((el) => visible(el) && !el.closest("#fleet-lock"));
    for (const scope of scopes) {
      const text = (scope.innerText || scope.textContent || "").trim().replace(/\s+/g, " ").slice(0, 500);
      const buttons = [...scope.querySelectorAll("button,[role='button']")].filter(visible);
      if (!text && !buttons.length) continue;
      if (SENSITIVE_GATE.test(text)) return { text, button: null, sensitive: true };
      const button = buttons.find((b) => SAFE_GATE_BUTTON.test(
        ((b.innerText || b.textContent || b.getAttribute("aria-label") || "")).trim(),
      ));
      if (button) return { text, button, sensitive: false };
    }

    // Некоторые новые interstitial-экраны ChatGPT — не dialog, а просто main.
    // Смотрим только когда ещё нет ни composer, ни переписки, и анализируем локальный
    // контейнер вокруг заведомо безопасной кнопки, а не весь проект со списком старых чатов.
    if (!findComposer() && !messages().length) {
      const main = document.querySelector("main");
      const buttons = main ? [...main.querySelectorAll("button,[role='button']")].filter(visible) : [];
      for (const button of buttons) {
        const label = (button.innerText || button.textContent || button.getAttribute("aria-label") || "").trim();
        if (!SAFE_GATE_BUTTON.test(label)) continue;
        const local = button.closest("form,section,article") || button.parentElement?.parentElement || button.parentElement || main;
        const text = (local?.innerText || local?.textContent || label).trim().replace(/\s+/g, " ").slice(0, 500);
        if (SENSITIVE_GATE.test(text)) return { text, button: null, sensitive: true };
        return { text, button, sensitive: false };
      }
    }
    return null;
  }

  function uiDiagnostics() {
    const gate = transientGate();
    const buttons = [...document.querySelectorAll("main button,[role='dialog'] button,[role='alertdialog'] button")]
      .filter((b) => visible(b) && !b.closest("#fleet-lock"))
      .slice(0, 12)
      .map((b) => (b.innerText || b.textContent || b.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 100))
      .filter(Boolean);
    return {
      composer: Boolean(findComposer()),
      gate: gate?.text?.slice(0, 240) || "",
      gateSensitive: Boolean(gate?.sensitive),
      buttons,
      title: document.title.slice(0, 120),
    };
  }

  async function dismissTransientGate() {
    const gate = transientGate();
    if (!gate) return { ok: false, detail: "безопасная промежуточная преграда не найдена" };
    if (gate.sensitive || !gate.button) return { ok: false, detail: `преграда требует ручной/чувствительной проверки: ${gate.text.slice(0, 180)}` };
    const label = (gate.button.innerText || gate.button.textContent || gate.button.getAttribute("aria-label") || "").trim();
    gate.button.click();
    await sleep(500);
    return { ok: true, detail: `закрыта промежуточная преграда «${label}»` };
  }

  // Сколько надпись о сбое держится: решение «повторить» — после failedTurnSec, а не на
  // мгновенной вспышке, которую ChatGPT сам уберёт.
  let failedSince = 0;
  let failedLast = "";

  function probe() {
    touchIfChanged();
    const usage = safe(conversationUsage, { chars: 0, turns: 0, cyrillic: 0, estimatedTokens: 0, fill: 0 });
    const conversationFull = safe(() => noticeText(CONVERSATION_FULL), "");
    const generating = Boolean(stopButton());
    const composer = findComposer();
    if (generating) lastChangeAt = Date.now();
    return {
      label: label(),
      // 17.11: метка быстрого агента из ПЕРВОГО сообщения чата — доказательство владения вкладкой,
      // переживающее перезапуск Chrome (ownTabs по ID вкладок теряется).
      quickMark: (() => {
        const first = [...messages()].find((el) => roleOf(el) === "user");
        return (/⟦быстрый агент (Q\d+(?:-[A-Z0-9]+)?(?:\.[2-9]\d*)?)⟧/.exec(first?.innerText || "") || [])[1] || "";
      })(),
      generating,
      idleSec: (Date.now() - lastChangeAt) / 1000,
      stallSec: (Date.now() - textChangeAt) / 1000,
      composerPresent: Boolean(composer),
      composerEmpty: composerText(composer) === "",
      composerOwned: Boolean(pendingSubmission && composerText(composer) === pendingSubmission),
      // Legacy owner-sv verdict was already typed before 0.19.30, but lost its
      // in-memory delivery receipt. Expose only a marker bit; never the draft text.
      composerOverseerDraft: /^⟦надзор⟧(?:\s|$)/.test(composerText(composer)) &&
        composerText(composer).length > 25,
      // 17.11: стартовый черновик флота в пустом чате (после перезапуска Chrome вкладка теряет
      // привязку и висела «не из флота» с готовым текстом). Наружу — только метка, не текст.
      fleetDraftLabel: messages().length ? "" : ((composerText(composer).match(
        /⟦быстрый агент (Q\d+-[A-Z0-9]+)⟧|метка label='([^']{1,60})'/) || []).slice(1).find(Boolean) || ""),
      lastRole: lastRole(),
      hasContinueButton: Boolean(continueButton()),
      ui: uiDiagnostics(),
      errorText: errorText(),
      rateLimited: rateLimitText(),
      rateLimitKind: rateLimitKind(),
      sessionReady: messages().length > 0 && Boolean(composer),
      modeCapabilities: safe(() => globalThis.FleetChatModes?.probe(document) || null, null),
      blocked: conversationFull ? "" : noticeText(HARD_BLOCK),
      conversationFull,
      conversationUsage: usage,
      fill: usage.fill,
      networkSuspicious: safe(() => noticeText(NETWORK_SUSPICIOUS), ""),
      blockerWaiting: safe(waitingForPermissionText, ""),
      interrupted: safe(interruptedText, ""),
      pageLoadFailed: safe(pageLoadFailureText, ""),
      temporaryLimit: safe(temporaryLimitText, ""),
      accountLoadFailed: safe(accountLoadFailureText, ""),
      cloudflarePageText: safe(cloudflarePageText, ""),
      ...(() => {
        const failed = safe(failedText, "");
        if (!failed) failedSince = 0;
        else if (!failedSince || failed !== failedLast) failedSince = Date.now();
        failedLast = failed;
        return {
          failed,
          failedSec: failed ? (Date.now() - failedSince) / 1000 : 0,
          hasRetryButton: Boolean(failed && safe(retryButton, null)),
        };
      })(),
      url: location.href,
    };
  }

  // Явные UI-сбои не должны ждать следующего 30-секундного alarm background.js.
  // Пока ошибка видна, будим фон раз в несколько секунд: page-load failure успеет дать
  // reloadPlan свой короткий hold, а failed turn — нажать штатный «Повторить».
  let failureWakeAt = 0;
  setInterval(() => {
    const failure = safe(pageLoadFailureText, "") || safe(failedText, "");
    const liveChatWithoutComposer = /\/c\//.test(location.pathname) && messages().length > 0 && !findComposer();
    if (!failure && !liveChatWithoutComposer) return;
    const now = Date.now();
    if (now - failureWakeAt < 2500) return;
    failureWakeAt = now;
    try {
      const sent = chrome.runtime?.sendMessage?.({ type: "tick-now", reason: "explicit-ui-failure" });
      sent?.catch?.(() => {});
    } catch {
      // Расширение могли перезагрузить между обнаружением ошибки и sendMessage.
    }
  }, 1000);

  function focusEditor(el) {
    el.focus();
    if (el.tagName === "TEXTAREA") return;
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function insertEditorText(el, text) {
    focusEditor(el);
    const before = composerText(el);
    document.execCommand("insertText", false, text);
    if (composerText(el) !== before) return;

    // contenteditable="plaintext-only" в новом project composer не всегда принимает
    // execCommand. Вставляем через Range и затем посылаем input, чтобы React увидел DOM.
    const selection = window.getSelection();
    const range = selection.rangeCount ? selection.getRangeAt(0) : document.createRange();
    if (!selection.rangeCount) {
      range.selectNodeContents(el);
      range.collapse(false);
    }
    const node = document.createTextNode(text);
    range.deleteContents();
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "insertText", data: text,
    }));
  }

  async function typeInto(el, text) {
    focusEditor(el);
    if (el.tagName === "TEXTAREA") {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    // Длинный текст — сразу одним способом: вставка как из буфера (ChatGPT кладёт её в
    // data-prompt-literal-paste); execCommand на 1–5 КБ применялся частично/асинхронно, а второй способ
    // поверх давал текст вдвое (15.11). Ждём, пока редактор применит, до 8 с.
    if (text.length > 400) {
      const data = new DataTransfer();
      data.setData("text/plain", text);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
      for (let waited = 0; waited < 8000 && !sameComposerText(composerText(el), text); waited += 200) await sleep(200);
      return;
    }
    insertEditorText(el, text);
    // Редактор применяет вставку асинхронно: ждём до 1,5 с, прежде чем решить «не встало».
    for (let waited = 0; waited < 1500 && !sameComposerText(composerText(el), text); waited += 100) await sleep(100);
    if (!sameComposerText(composerText(el), text)) {
      if (composerText(el)) clearComposer(el); // иначе вставка добавит текст второй раз
      const data = new DataTransfer();
      data.setData("text/plain", text);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }
  }

  async function settledComposer(el, text, ms = 3000) {
    for (let waited = 0; waited < ms; waited += 150) {
      if (sameComposerText(composerText(el), text)) return true;
      await sleep(150);
    }
    return sameComposerText(composerText(el), text);
  }

  async function waitFor(list, ms) {
    for (let waited = 0; waited <= ms; waited += 500) {
      const el = find(list);
      if (el) return el;
      await sleep(500);
    }
    return null;
  }

  async function waitForComposer(ms) {
    for (let waited = 0; waited <= ms; waited += 250) {
      const el = findComposer();
      if (el) return el;
      await sleep(250);
    }
    return null;
  }

  function mentionOption(name) {
    const wanted = name.toLowerCase();
    const options = [
      ...document.querySelectorAll("[role='option'],[role='menuitem'],[role='menuitemradio'],[cmdk-item],[data-suggestion-index]"),
    ].filter((o) => visible(o));
    return (
      options.find((o) => (o.textContent || "").trim().toLowerCase() === wanted) ||
      options.find((o) => (o.textContent || "").toLowerCase().includes("dev-mcp"))
    );
  }

  async function waitForMentionOption(name, ms = 5000) {
    for (let waited = 0; waited <= ms; waited += 250) {
      const option = mentionOption(name);
      if (option) return option;
      await sleep(250);
    }
    return null;
  }

  function clearComposer(el) {
    pendingSubmission = "";
    if (el.tagName === "TEXTAREA") {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, "");
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    el.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("delete", false);
    selection.removeAllRanges();
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Новый чат: сначала пытаемся превратить «@<коннектор>» в connector option/chip.
  // Если ChatGPT не показывает DOM-подсказку, оставляем сырой @mention: именно этот путь
  // исторически работал и ChatGPT сам разбирал подключённый коннектор при отправке.
  function projectNewChatCta() {
    const composer = findComposer();
    const nodes = [...document.querySelectorAll(NEW_CHAT)]
      .filter((node) => {
        if (!visible(node) || !node.closest("main") || node.closest("nav,aside")) return false;
        if (node.matches("[contenteditable]:not([contenteditable='false']),textarea,[role='textbox']") ||
            node.closest("[contenteditable]:not([contenteditable='false']),textarea,[role='textbox']")) return false;
        if (composer && (node === composer || composer.contains(node))) return false;
        return true;
      });
    const node = nodes[0] || null;
    return node?.closest?.("button,a,[role='button']") || node;
  }

  function projectComposerHint() {
    const scope = document.querySelector("main");
    if (!scope) return null;
    const candidates = [
      ...scope.querySelectorAll("[data-placeholder],[placeholder],[aria-label],p,span"),
    ];
    return candidates.find((node) => {
      if (!visible(node) || node.closest("nav,aside,#fleet-lock")) return false;
      const values = [
        node.getAttribute?.("data-placeholder"),
        node.getAttribute?.("placeholder"),
        node.getAttribute?.("aria-label"),
        node.textContent,
      ].map((v) => String(v || "").trim()).filter(Boolean);
      return values.some((v) => /^(новый чат|new chat)(?:\s+(?:в|in)\b|$)/i.test(v) && v.length < 120);
    }) || null;
  }

  async function activateProjectComposer() {
    const hint = projectComposerHint();
    if (!hint) return null;
    const click = hint.closest("button,[role='button']") || hint;
    click.focus?.();
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      const Ctor = type === "pointerdown" && typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
      click.dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, view: window }));
    }
    await sleep(300);
    const focused = document.activeElement;
    if (focused && focused.closest?.("main") &&
        (focused.tagName === "TEXTAREA" || focused.isContentEditable || focused.getAttribute?.("role") === "textbox")) {
      return focused;
    }
    return findComposer();
  }

  function chatModeChoice() {
    const scopes = [
      ...document.querySelectorAll("[role='dialog'],[role='menu'],[role='listbox'],[data-radix-popper-content-wrapper]"),
      document.querySelector("main"),
    ].filter(Boolean);
    for (const scope of scopes) {
      const hit = [...scope.querySelectorAll("button,[role='button'],[role='menuitem'],[role='option'],[role='tab']")]
        .find((node) => visible(node) && /^(chat|чат)$/i.test((node.innerText || node.textContent || "").trim()));
      if (hit) return hit;
    }
    return null;
  }

  async function submitStart(el, before, note) {
    const submitted = composerText(el);
    const outcome = await submit(el, before, note);
    // A confirmed Send may leave a persisted project-New-chat draft behind.
    // Clear ONLY the exact text we just sent, never an unrelated new draft.
    if (outcome.ok && submitted && sameComposerText(composerText(el), submitted)) clearComposer(el);
    return outcome;
  }

  async function startChat(mention, text, replaceDraft = false, confirmedStartAgents = []) {
    // Current project landing pages may render a "New chat" CTA first and only mount
    // the composer after it is clicked. Older code waited 20s for a composer that could
    // never exist, consuming all pending-start slots. Try the existing composer first;
    // then click only the main-content CTA (never the global/sidebar New Chat).
    // На текущей project-page визуальный composer уже нарисован, но настоящий
    // contenteditable может монтироваться только после фокуса. Не ждём 8 секунд
    // несуществующий editor: сначала активируем видимую подсказку «Новый чат в …».
    let el = findComposer() || await activateProjectComposer();
    let recoveredProjectLanding = Boolean(el);
    if (!el) el = await waitForComposer(1500);
    if (!el) {
      const cta = projectNewChatCta();
      if (cta) {
        cta.focus?.();
        cta.click();
        recoveredProjectLanding = true;
        await sleep(400);
        el = findComposer() || await activateProjectComposer() || await waitForComposer(1800);
      }
      // Некоторые варианты UI после New chat показывают выбор Chat / Work.
      if (!el) {
        const chat = chatModeChoice();
        if (chat) {
          chat.focus?.();
          chat.click();
          recoveredProjectLanding = true;
          await sleep(300);
        }
        el = findComposer() || await activateProjectComposer() || await waitForComposer(4500);
      }
    }
    if (!el) {
      return {
        ok: false,
        detail: recoveredProjectLanding
          ? "не найдено поле ввода после project New chat"
          : "не найдено поле ввода (страница проекта не загрузилась или не вошли в ChatGPT)",
        pageText: safe(cloudflarePageText, ""),
      };
    }
    if (projectPlaceholderOnly(el)) {
      clearComposer(el);
      // plaintext-only project composer иногда не выполняет execCommand(delete).
      // Тогда очищаем только подтверждённый UI-placeholder и уведомляем React через input.
      if (projectPlaceholderOnly(el)) {
        el.textContent = "";
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true, inputType: "deleteContentBackward", data: null,
        }));
      }
    }
    let existing = composerText(el);
    if (existing) {
      // После выгрузки/reinject content-script локальный pendingSubmission теряется, но
      // background всё ещё знает точный starter. На новом чате безопасно дослать только
      // собственную заготовку: exact body либо mention-chip + exact body. Любой другой
      // текст остаётся человеческим черновиком и никогда не отправляется.
      const expectedLabel = LABEL.exec(text)?.[1] || "";
      // 17.11: черновик ДРУГОГО агента (⟦быстрый агент QX⟧ / label='X') — не наша заготовка:
      // его «очистка и дописывание» отправили в чат q7 стартовое задание q6.
      const agentMark = (s) => (String(s).match(/⟦быстрый агент (Q\d+(?:-[A-Z0-9]+)?(?:\.[2-9]\d*)?)⟧|метка label='([^']{1,60})'/) || [])
        .slice(1).find(Boolean) || "";
      const theirs = agentMark(existing), ours = agentMark(text);
      if (theirs && ours && theirs.toUpperCase() !== ours.toUpperCase()) {
        // Only the background has authoritative delivery state. Never infer it
        // from the age, label or URL of a draft; a live Q agent may be typing.
        // 18.11: стартовый черновик обычного агента флота (f<дата>-…) — общий черновик проекта,
        // а не набирающий текст быстрый агент: в новом чате без ходов его можно очистить.
        // Общий черновик проекта приносит в новую вкладку и стартовые тексты других быстрых
        // агентов: их вкладки держат свой DOM, очистка ЭТОГО поля их не задевает. Опасна только
        // отправка чужого текста (17.11) — её здесь нет: поле очищается и пишется своё задание.
        // В чате с ходами чужой черновик по-прежнему не трогаем.
        if (messages().length)
          return { ok: false, detail: `в поле стартовый черновик другого агента ${theirs} — не трогаю` };
        clearComposer(el);
        if (composerText(el)) {
          // Some project editors ignore execCommand(delete); this exact draft is
          // already proven disposable by the background.
          el.textContent = "";
          el.dispatchEvent(new InputEvent("input", {
            bubbles: true, inputType: "deleteContentBackward", data: null,
          }));
        }
        await sleep(100);
        if (composerText(el)) return { ok: false, detail: `не удалось очистить устаревший черновик ${theirs}` };
        existing = "";
      }
      // 18.11: ChatGPT держит ОДИН общий черновик страницы проекта и подставляет его в каждую
      // новую вкладку. Стартовое сообщение обычного агента флота («Ты агент флота …,
      // label='X'») генерирует сам флот, человек его не пишет; в новом чате без ходов его можно
      // очистить и вписать своё задание. Раньше оно считалось человеческим черновиком — и все
      // новые вкладки быстрых агентов стояли с «чужой текст без метки» бесконечно.
      if (existing && ours && !theirs && messages().length === 0 &&
          /Ты агент флота [^\n,]{1,60}, label='[^']{1,60}'/.test(existing) &&
          !sameComposerText(existing, text)) {
        clearComposer(el);
        if (composerText(el)) {
          el.textContent = "";
          el.dispatchEvent(new InputEvent("input", {
            bubbles: true, inputType: "deleteContentBackward", data: null,
          }));
        }
        await sleep(100);
        if (composerText(el)) return { ok: false, detail: "не удалось очистить стартовый черновик флота" };
        existing = "";
      }
      if (existing) {
      // A quick agent must NEVER replace an unmarked human draft even when
      // replaceDraft=true. Only its own fully identified draft may be recovered.
      // 18.11: в новом чате поле, где остался ТОЛЬКО чип коннектора (@… dev-mcp+), — наша же
      // незаконченная заготовка (чип встал, текст нет). Раньше её принимали за чужой текст, и
      // все новые вкладки быстрых агентов стояли с «чужой текст без метки» до паузы.
      const mentionKey = normalizeComposer(mention || "").toLowerCase();
      const mentionOnlyDraft = messages().length === 0 && Boolean(mentionKey) &&
        existing.toLowerCase().split(mentionKey).join("")
          .replace(/подключено|connected|[\s @+:—–\-.,]/gi, "") === "";
      if (replaceDraft && ours && !theirs && !mentionOnlyDraft &&
          !sameComposerText(existing, text))
        return { ok: false, detail: "в поле ввода уже есть чужой текст без метки агента" };
      const mentionText = normalizeComposer(mention || "");
      const noTurns = messages().length === 0;
      const lowExisting = existing.toLowerCase();
      const ownRecoveredDraft = noTurns && (
        existing === text || existing.endsWith(text) ||
        (expectedLabel && existing.includes(expectedLabel) && /fleet_join\s*\(/i.test(existing))
      );
      if (ownRecoveredDraft) {
        return submitStart(el, messages().length, "восстановлена наша стартовая заготовка после reinject");
      }

      // Новый mention-chip может сериализоваться не как точное "@name", а как имя коннектора
      // с декоративным текстом/неразрывными пробелами. На пустом автозапускаемом чате это
      // всё равно наша частичная заготовка: очищаем её и собираем starter заново.
      const ownPartialDraft = noTurns && (
        (mentionText && lowExisting.includes(mentionText.toLowerCase())) ||
        (expectedLabel && existing.includes(expectedLabel)) ||
        /fleet_join\s*\(/i.test(existing)
      );
      if (!ownPartialDraft && !replaceDraft) return { ok: false, detail: "в поле ввода уже есть чужой текст" };
      clearComposer(el);
      if (composerText(el)) {
        el.textContent = "";
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true, inputType: "deleteContentBackward", data: null,
        }));
      }
      await sleep(100);
      if (composerText(el)) return { ok: false, detail: "не удалось очистить нашу частичную стартовую заготовку" };
      }
    }
    const before = messages().length;
    focusEditor(el);
    let chip = "не требуется";
    if (mention) {
      insertEditorText(el, `@${mention}`);
      const option = await waitForMentionOption(mention);
      if (option) {
        option.click();
        chip = "выбран из подсказки";
        await sleep(500);
      } else {
        chip = "без DOM-подсказки";
      }
      focusEditor(el);
      insertEditorText(el, ` ${text}`);
    } else {
      await typeInto(el, text);
    }
    await sleep(600);
    if (!composerText(el)) return { ok: false, detail: "текст не встал в поле ввода" };
    return submitStart(el, before, `коннектор ${chip}`);
  }

  function sendButton(el) {
    if (!usableComposer(el)) return null;
    const isSend = (button) => {
      if (!visible(button) || button.disabled || button.getAttribute("aria-disabled") === "true" ||
          button.closest("nav,aside,[data-app-action-sidebar-project-row]")) return false;
      const label = (button.getAttribute("aria-label") || "").trim();
      const testid = button.getAttribute("data-testid") || "";
      return /^(send|send prompt|отправить)$/i.test(label) ||
        /^(composer-submit-button|send-button)$/i.test(button.id || "") ||
        /^send-button$/i.test(testid) ||
        (button.type === "submit" && /^(send|send prompt|отправить)?$/i.test(label));
    };
    // The current ComposerLayoutFooter is five ancestors above ProseMirror, without a form.
    // Never fall back to document: a project named "Beta App" has sidebar action buttons.
    let root = el;
    for (let depth = 0; root && depth < 12; depth++, root = root.parentElement) {
      if (root.matches?.("main,body,nav,aside")) break;
      const button = [...root.querySelectorAll("button")].find(isSend);
      if (button) return button;
    }
    return null;
  }

  async function submit(el, before, note = "") {
    pendingSubmission = composerText(el);
    let send = null;
    for (let waited = 0; waited <= 3500 && !send; waited += 250) {
      send = sendButton(el);
      if (!send) await sleep(250);
    }
    if (!send) return { ok: false, detail: "нет активной кнопки Send в composer выбранного поля" };
    send.click();
    for (let i = 0; i < 15; i += 1) {
      await sleep(1000);
      const ownMessage = [...messages()].slice(before).some((msg) =>
        roleOf(msg) === "user" && confirmsUserMessage(messageText(msg), pendingSubmission));
      if (ownMessage) {
        pendingSubmission = "";
        return { ok: true, detail: `сообщение подтверждено${note ? `; ${note}` : ""}` };
      }
      if (rateLimitText() && !noticeText(HARD_BLOCK)) await dismissRateLimit();
    }
    return { ok: false, detail: `нет нового пользовательского сообщения с нашим текстом${note ? `; ${note}` : ""}` };
  }

  async function sendText(text, replaceDraft = false, strictDraft = false) {
    const before = messages().length;
    const el = findComposer();
    if (!el) return { ok: false, detail: "не найдено поле ввода",
                      pageText: safe(cloudflarePageText, "") };
    if (pendingSubmission && sameComposerText(composerText(el), pendingSubmission) &&
        (!strictDraft || sameComposerText(composerText(el), text)))
      return submit(el, before, "повтор нашей неотправленной заготовки");
    // 15.11: после неудачной попытки наш же текст остаётся в поле (переживает перезагрузку расширения,
    // pendingSubmission — нет); тот же текст — просто отправить, чужой — не трогать.
    if (composerText(el) && sameComposerText(composerText(el), text)) return submit(el, before, "наш текст уже в поле");
    // Остаток нашей же неудачной вставки (текст вдвое, обрезан) — очистить и вставить заново; чужой — не трогать.
    const residue = composerKey(composerText(el));
    const ours = composerKey(text);
    if (!strictDraft && residue && ours.length > 40 &&
        (residue.includes(ours.slice(0, 120)) || ours.includes(residue.slice(0, 120)))) {
      clearComposer(el);
      await sleep(150);
    }
    if (composerText(el) && replaceDraft) {
      clearComposer(el);
      await sleep(150);
    }
    if (composerText(el)) return { ok: false, detail: "в поле ввода уже есть текст" };
    await typeInto(el, text);
    if (!(await settledComposer(el, text))) return { ok: false, detail: "текст не встал в поле ввода" };
    return submit(el, before);
  }

  // ---- 21.11.2026: вложения и черновик для передачи чата по факту «maximum length» -------------
  function composerRoot(el) {
    let root = el;
    for (let depth = 0; root?.parentElement && depth < 8; depth++) {
      if (root.matches?.("form") || root.parentElement.matches?.("main,body")) break;
      root = root.parentElement;
    }
    return root || document.body;
  }

  function fileInputFor(el) {
    const inputs = [...document.querySelectorAll('input[type="file"]')];
    const near = inputs.filter((i) => composerRoot(el).contains(i));
    const pool = near.length ? near : inputs;
    return pool.find((i) => !/^image\//i.test(i.accept || "") && i.multiple) ||
      pool.find((i) => !/^image\//i.test(i.accept || "")) || pool[0] || null;
  }

  function attachmentEvidence(root, names) {
    const text = (root.innerText || root.textContent || "").toLowerCase();
    const shown = names.filter((name) => {
      const stem = String(name).toLowerCase().replace(/\.(json|md)$/i, "");
      return text.includes(stem.slice(0, 14)) || text.includes(stem.slice(-10));
    }).length;
    const tiles = root.querySelectorAll('[data-testid*="attachment" i],[data-testid*="file" i],[aria-label*="remove file" i],[aria-label*="удалить файл" i]').length;
    const busy = Boolean(root.querySelector('[role="progressbar"],[aria-busy="true"]'));
    const failed = /не удалось загрузить|unable to upload|failed to upload|upload failed/i.test(text);
    return { shown, tiles, busy, failed };
  }

  async function attachFiles(message) {
    const el = findComposer();
    if (!el) return { ok: false, detail: "не найдено поле ввода" };
    const files = (message.files || []).map((f) =>
      new File([String(f.text || "")], String(f.name || "file.txt"), { type: f.type || "text/plain" }));
    if (!files.length) return { ok: false, detail: "нет файлов для вложения" };
    const names = files.map((f) => f.name);
    const root = composerRoot(el);
    const before = attachmentEvidence(root, names);
    if (before.shown >= names.length) return { ok: true, detail: "файлы уже в поле" };
    const data = new DataTransfer();
    for (const file of files) data.items.add(file);
    const input = message.method === "paste" ? null : fileInputFor(el);
    if (input) {
      input.files = data.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      focusEditor(el);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }
    for (let waited = 0; waited <= 120_000; waited += 1000) {
      await sleep(1000);
      const now = attachmentEvidence(composerRoot(findComposer() || el), names);
      if (now.failed) return { ok: false, detail: "сайт сообщил об ошибке загрузки файла" };
      if ((now.shown >= names.length || now.tiles >= before.tiles + names.length) && !now.busy)
        return { ok: true, detail: `${input ? "input[type=file]" : "вставка"}: ${names.join(", ")}` };
    }
    return { ok: false, detail: `вложение не подтвердилось за 120 с (${input ? "input" : "вставка"})` };
  }

  async function draftText(text) {
    const el = findComposer();
    if (!el) return { ok: false, detail: "не найдено поле ввода" };
    if (composerText(el) && sameComposerText(composerText(el), text)) return { ok: true, detail: "текст уже в поле" };
    if (composerText(el)) return { ok: false, detail: "в поле ввода уже есть текст" };
    await typeInto(el, text);
    return (await settledComposer(el, text)) ? { ok: true, detail: "текст вписан, Send не нажат" }
      : { ok: false, detail: "текст не встал в поле ввода" };
  }

  async function act(message) {
    if (message.action === "attach-files") return attachFiles(message);
    if (message.action === "draft") return draftText(message.text);
    if (message.action === "dismiss-rate-limit") return dismissRateLimit();
    if (message.action === "dismiss-gate") return dismissTransientGate();
    if (message.action === "overseer-resume-draft") {
      const el = findComposer();
      const draft = composerText(el);
      if (!el || !/^⟦надзор⟧(?:\s|$)/.test(draft) || draft.length < 26 ||
          !location.pathname.includes("/c/"))
        return { ok: false, detail: "нет подтверждённой автоматической заготовки надзора" };
      // Same scoped sendButton + NEW user-message acknowledgement as quick-send;
      // no text insertion, no replacement of an arbitrary owner's draft.
      return submit(el, messages().length, "восстановлен старый черновик ⟦надзор⟧");
    }
    if (noticeText(CONVERSATION_FULL)) return { ok: false, detail: "conversation_full: нужен чат-преемник" };
    if (noticeText(HARD_BLOCK)) return { ok: false, detail: "лимит модели, проверка или вход — требуется действие владельца" };
    if (rateLimitText()) {
      const dismissed = await dismissRateLimit();
      if (!dismissed.ok) return dismissed;
    }
    let verifiedMode = {};
    if (["start", "continue", "click-continue", "click-retry"].includes(message.action) &&
        Object.prototype.hasOwnProperty.call(message, "mode")) {
      if (!globalThis.FleetChatModes) {
        return { ok: false, wait: true, detail: "ChatGPT mode adapter is not loaded" };
      }
      if (message.modeContract !== "fleet-chat-mode/v1" ||
          !/^[a-f0-9]{64}$/.test(message.modeSpecSha || "")) {
        return { ok: false, wait: true, detail: "режим не связан с подтверждённой версией шапки задачи" };
      }
      const mode = await globalThis.FleetChatModes.ensureMode(message.mode, {
        stage: message.outcomeStage || null, root: document,
      });
      if (!mode.ok) {
        return { ok: false, wait: true, detail: "режим UI не подтверждён: " + mode.detail,
          modeEvidence: mode.evidence };
      }
      verifiedMode = {
        modeEffective: mode.effective,
        modeEvidence: { ...mode.evidence, contract: message.modeContract, specSha: message.modeSpecSha },
      };
    }
    if (message.action === "click-continue") {
      const button = continueButton();
      if (!button) return { ok: false, detail: "кнопка продолжения пропала" };
      button.click();
      await sleep(3000);
      return { ...(stopButton() ? { ok: true, detail: "нажата кнопка продолжения" } :
        { ok: false, detail: "кнопка нажата, генерации нет" }), ...verifiedMode };
    }
    if (message.action === "click-account-continue") {
      const button = accountContinueButton();
      if (!button) return { ok: false, detail: "кнопки «Продолжить» нет" };
      button.click();
      return { ok: true, detail: "нажата «Продолжить» на экране перегрузки аккаунта" };
    }
    if (message.action === "click-retry") {
      const button = retryButton();
      if (!button) return { ...await sendText(message.text), ...verifiedMode }; // кнопка пропала — «продолжи» текстом
      // Retry нередко сам инициирует навигацию/reload. Нельзя держать message-port ещё
      // 10 секунд: isolated world уничтожается раньше ответа, и background записывает
      // ложный timeout, хотя клик уже сработал. Сам факт клика подтверждаем сразу;
      // результат на следующем probe проверит policy и при необходимости повторит.
      button.click();
      return { ok: true, detail: "нажата «Повторить»; результат проверю следующим probe", ...verifiedMode };
    }
    if (message.action === "continue" || message.action === "start") {
      const response = await (message.action === "continue"
        ? sendText(message.text, message.quick === true, message.overseer === true)
        : startChat(message.mention, message.text, message.quick === true, message.confirmedStartAgents));
      return { ...response, ...verifiedMode };
    }
    return { ok: false, detail: `неизвестное действие ${message.action}` };
  }


  // ---- Блокировка вкладки агента -------------------------------------------------
  //
  // Во вкладку агента всё идёт через расширение: человек может читать, листать и
  // копировать, но не может случайно нажать «Стоп», набрать текст, отредактировать
  // своё сообщение или уйти в другой чат. Блокируются только настоящие (isTrusted)
  // события человека — действия расширения синтетические (click(), execCommand) и
  // проходят. Диалоги ChatGPT (подтверждения инструментов и т.п.) не блокируются.
  const UNLOCK_MS = 10 * 60 * 1000;
  const lock = { on: false, label: "", unlockedUntil: 0, box: null, note: null, timer: 0 };
  const CTRL_ALLOWED = new Set(["c", "f", "a", "+", "-", "=", "0"]);
  const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"]);

  // После перезагрузки расширения старая копия скрипта остаётся в странице без связи с
  // фоном: она должна снять свою блокировку, иначе вкладку уже никто не разблокирует.
  const orphaned = () => {
    try {
      return !chrome.runtime?.id;
    } catch {
      return true;
    }
  };
  const locked = () => lock.on && Date.now() >= lock.unlockedUntil && !orphaned();
  const inDialog = (t) => Boolean(t?.closest?.("[role='dialog'],[role='alertdialog']"));
  const ownUi = (t) => Boolean(t?.closest?.("#fleet-lock"));

  function composerRoot() {
    const el = findComposer();
    if (!el) return null;
    return el.closest("form") || el.parentElement?.parentElement?.parentElement || el;
  }

  function guarded(target) {
    if (!target?.closest || ownUi(target) || inDialog(target)) return false;
    const root = composerRoot();
    if (root && root.contains(target)) return true; // поле ввода, «Стоп», «Отправить», вложения
    if (target.closest(SEL.stop.join(","))) return true;
    if (target.closest(NEW_CHAT) || target.closest("nav, aside")) return true; // уйти в другой чат
    const link = target.closest("a[href]");
    if (link && link.target !== "_blank") return true;
    const turn = target.closest("[data-testid^='conversation-turn'],article");
    if (turn && target.closest("button") && turn.querySelector("[data-message-author-role='user']")) {
      return true; // правка своего сообщения перезапускает ответ
    }
    return false;
  }

  function keyAllowed(e) {
    if (inDialog(e.target) || ownUi(e.target)) return true;
    if (e.ctrlKey || e.metaKey) return CTRL_ALLOWED.has(String(e.key).toLowerCase()) && !e.shiftKey;
    const root = composerRoot();
    const inComposer = Boolean(root && root.contains(e.target));
    // Стрелка вверх в пустом поле ввода открывает правку последнего сообщения.
    return SCROLL_KEYS.has(e.key) && !inComposer;
  }

  function flash() {
    if (!lock.note) return;
    lock.note.textContent = "Вкладку ведёт расширение — ввод заблокирован";
    clearTimeout(lock.flashTimer);
    lock.flashTimer = setTimeout(() => {
      if (lock.note) lock.note.textContent = `Агент флота ${lock.label} — ввод через расширение`;
    }, 2500);
  }

  function block(e) {
    e.preventDefault();
    e.stopImmediatePropagation();
    flash();
  }

  const onPointer = (e) => {
    if (e.isTrusted && locked() && guarded(e.target)) block(e);
  };
  const onKey = (e) => {
    if (e.isTrusted && locked() && !keyAllowed(e)) block(e);
  };
  const onData = (e) => {
    if (e.isTrusted && locked() && !inDialog(e.target) && !ownUi(e.target)) block(e);
  };
  const onUnload = (e) => {
    if (!locked()) return;
    e.preventDefault();
    e.returnValue = ""; // Chrome спросит «Покинуть сайт?», если человек уже трогал вкладку
  };
  for (const type of ["pointerdown", "mousedown", "mouseup", "click", "dblclick", "auxclick", "contextmenu", "touchstart"]) {
    window.addEventListener(type, onPointer, { capture: true });
  }
  for (const type of ["keydown", "keypress"]) window.addEventListener(type, onKey, { capture: true });
  for (const type of ["paste", "drop", "cut"]) window.addEventListener(type, onData, { capture: true });
  window.addEventListener("beforeunload", onUnload, { capture: true });

  // Плашка поверх поля ввода: видно, почему не печатается, и есть осознанный выход.
  function place() {
    if (!lock.box) return;
    if (orphaned()) {
      lock.on = false;
      clearInterval(lock.timer);
      lock.box.remove();
      lock.box = null;
      return;
    }
    if (!locked()) {
      lock.box.style.display = "none";
      return;
    }
    const root = composerRoot();
    const r = root?.getBoundingClientRect();
    const s = lock.box.style;
    s.display = "flex";
    if (r && r.width > 0) {
      Object.assign(s, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`, right: "", bottom: "" });
    } else {
      Object.assign(s, { left: "", top: "", width: "auto", height: "auto", right: "16px", bottom: "16px" });
    }
  }

  function ensureBox() {
    if (lock.box) return;
    for (const stale of document.querySelectorAll("#fleet-lock")) stale.remove();
    const box = document.createElement("div");
    box.id = "fleet-lock";
    Object.assign(box.style, {
      position: "fixed", zIndex: "2147483647", display: "none", alignItems: "center", justifyContent: "center",
      gap: "12px", padding: "8px 14px", boxSizing: "border-box", borderRadius: "16px",
      background: "rgba(24,24,27,.78)", color: "#fff", font: "13px/1.3 system-ui,sans-serif",
      backdropFilter: "blur(2px)", cursor: "default", userSelect: "none",
    });
    const note = document.createElement("span");
    const unlock = document.createElement("button");
    unlock.type = "button";
    unlock.textContent = "Разблокировать на 10 мин";
    Object.assign(unlock.style, {
      font: "inherit", color: "#fff", background: "transparent", border: "1px solid rgba(255,255,255,.5)",
      borderRadius: "10px", padding: "3px 10px", cursor: "pointer",
    });
    unlock.addEventListener("click", () => {
      const ok = window.confirm(
        "Разблокировать вкладку агента на 10 минут?\n\nАвтомат продолжит её вести. Остановишь генерацию или " +
          "напишешь сюда сам — агент может потерять ход работы.",
      );
      if (!ok) return;
      lock.unlockedUntil = Date.now() + UNLOCK_MS;
      place();
    });
    box.append(note, unlock);
    // Контейнер — <html>, а не <body>: наблюдатель тишины смотрит на страницу, и мигание
    // плашки не должно выглядеть как активность агента.
    document.documentElement.appendChild(box);
    lock.box = box;
    lock.note = note;
  }

  function setLock(on, label) {
    lock.on = Boolean(on);
    if (label) lock.label = label;
    if (lock.on) {
      ensureBox();
      lock.note.textContent = `Агент флота ${lock.label} — ввод через расширение`;
      if (!lock.timer) lock.timer = setInterval(place, 500);
    } else if (lock.timer) {
      clearInterval(lock.timer);
      lock.timer = 0;
    }
    place();
    return { locked: locked(), unlockedFor: Math.max(0, Math.round((lock.unlockedUntil - Date.now()) / 1000)) };
  }

  let acting = false;
  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message?.type === "probe") {
      reply({ ...probe(), locked: locked(), unlockedFor: Math.max(0, Math.ceil((lock.unlockedUntil - Date.now()) / 1000)) });
      return false;
    }
    // Лёгкая проба для автообновления при «A temporary limit…» (limit-reload.js): на всех вкладках.
    if (message?.type === "limit-probe") {
      reply({ temporaryLimit: safe(temporaryLimitText, ""),
        composerEmpty: safe(() => composerText(findComposer()) === "", true) });
      return false;
    }
    // Надзиратель хода (пульт _todo/59): текст чата для оценки законченного хода.
    // Ход исполнителя — все сообщения ассистента после последнего сообщения человека.
    if (message?.type === "transcript") {
      try {
        const all = [...messages()];
        const text = messageText;
        const users = all.filter((el) => roleOf(el) === "user").map((el) => text(el).slice(0, 12000));
        let lastUser = -1;
        all.forEach((el, i) => { if (roleOf(el) === "user") lastUser = i; });
        const turnEls = all.slice(lastUser + 1).filter((el) => roleOf(el) === "assistant");
        const turn = turnEls.map(text).join("\n\n");
        // 25.11 (надзор jev-1): стабильный ID ответа, если DOM его даёт; иначе сервер берёт хеш текста.
        const lastEl = turnEls[turnEls.length - 1];
        const idEls = lastEl ? [lastEl, ...(lastEl.querySelectorAll?.("[data-message-id]") || [])] : [];
        const lastAssistantId = idEls.map((el) => el.getAttribute?.("data-message-id")).filter(Boolean).pop() || null;
        // Длинный чат: первые 20 (исходное поручение) и последние 60 (новое слово владельца).
        const usersOut = users.length > 80 ? [...users.slice(0, 20), ...users.slice(-60)] : users;
        let prevUser = -1;
        all.slice(0, Math.max(0, lastUser)).forEach((el, i) => { if (roleOf(el) === "user") prevUser = i; });
        const prev = lastUser > 0 ? all.slice(prevUser + 1, lastUser).filter((el) => roleOf(el) === "assistant")
          .map(text).join("\n\n") : "";
        reply({ ok: true, nMessages: all.length, lastRole: roleOf(all[all.length - 1]) || null,
          userCount: users.length, matchingUserIndex: message.expectedSend
            ? users.findLastIndex(user => confirmsUserMessage(user, message.expectedSend)) : -1,
          userMessages: usersOut, userTruncated: users.length > 80, lastAssistantId,
          lastAssistant: turn.slice(-60000), prevAssistant: prev.slice(-6000) });
      } catch (error) {
        reply({ ok: false, detail: String(error) });
      }
      return false;
    }
    if (message?.type === "lock") {
      reply(setLock(message.on, message.label));
      return false;
    }
    if (message?.type === "act") {
      // A second wake/tick cannot re-send while the first waits for mode
      // activation or submit acknowledgement. Report wait, not a hard failure.
      if (acting) { reply({ ok: false, wait: true, detail: "предыдущее действие ещё выполняется" }); return false; }
      acting = true;
      rateEvents = [];
      act(message).then(
        (result) => { acting = false; reply({ ...result, url: location.href, rateEvents }); },
        (error) => { acting = false; reply({ ok: false, detail: String(error), url: location.href, rateEvents }); },
      );
      return true;
    }
    return false;
  });
})();
