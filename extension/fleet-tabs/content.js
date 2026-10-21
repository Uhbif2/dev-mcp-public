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
  const CONTENT_VERSION = 22;
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
  const ERROR_TEXT = /(something went wrong|network error|an error occurred|произошла ошибка|ошибка сети|не удалось)/i;
  const LABEL = /label\s*[=:]\s*['"«`]([^'"»`\s]+)['"»`]/i;
  // Окно ограничения ChatGPT: «Вы отправляете запросы слишком часто. Доступ к вашим
  // диалогам временно ограничен…» (21.09.2026) и родственные формулировки.
  const RATE_LIMIT = /(слишком часто|слишком много запросов|too many requests|sending requests too quickly)/i;
  const HARD_BLOCK = /(подозрительн\S* активност|достигли (лимита|предела)|подтвердите, что вы человек|unusual activity|reached (the|your) (usage |message )?limit|verify you are human|войдите (в аккаунт|снова)|log in again)/i;
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
  const FAILED_TURN = /(resume stream unavailable|поток продолжения недоступен|время доставки сообщения истекло|время ожидания (ответа )?истекло|сообщение не (было )?доставлено|не удалось (отправить|получить|сгенерировать|загрузить) (сообщение|ответ)|что-то пошло не так|произошла ошибка|ошибка сети|message delivery timed out|message (was )?not delivered|request timed out|something went wrong|an error occurred|network error)/i;
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

  function findComposer() {
    for (const selector of SEL.composer) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el) && !el.closest("#fleet-lock,nav,aside,[aria-hidden='true']")) return el;
      }
    }
    // Новый project composer (25.09.2026) может быть обычным contenteditable без
    // прежних id/data-testid. Сначала привязываемся к placeholder внутри него.
    for (const hint of document.querySelectorAll("main [data-placeholder],main [placeholder]")) {
      if (!visible(hint)) continue;
      const editor = hint.matches("[contenteditable='true'],textarea")
        ? hint
        : hint.closest("[contenteditable='true'],textarea");
      if (editor && visible(editor) && !editor.closest("nav,aside,[aria-hidden='true']")) return editor;
    }
    // Последний безопасный fallback: на странице ChatGPT рабочий редактор — видимый
    // contenteditable/textarea в main, не в диалоге и не в нашей плашке.
    return [...document.querySelectorAll("main [contenteditable],main textarea")]
      .find((el) => visible(el) && (el.tagName === "TEXTAREA" || el.isContentEditable) &&
        !el.closest("#fleet-lock,nav,aside,[role='dialog'],[role='alertdialog'],[aria-hidden='true']")) || null;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const messages = () => {
    const legacy = document.querySelectorAll("[data-message-author-role]");
    if (legacy.length) return legacy;
    return document.querySelectorAll("section[data-turn='user'],section[data-turn='assistant'],[data-turn='user'],[data-turn='assistant']");
  };
  const roleOf = (el) => el?.getAttribute("data-message-author-role") || el?.getAttribute("data-turn") || "";
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
    const first = document.querySelector("[data-message-author-role='user'],section[data-turn='user'],[data-turn='user']");
    const match = first ? LABEL.exec(first.innerText || "") : null;
    return match ? match[1] : "";
  }

  function lastRole() {
    const all = messages();
    if (!all.length) return null;
    const last = all[all.length - 1];
    return last.getAttribute("data-message-author-role") || last.getAttribute("data-turn");
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
  function noticeText(pattern) {
    const places = [
      ...document.querySelectorAll("[role='dialog'],[role='alertdialog'],[role='alert'],[role='status'],[data-testid*='toast']"),
    ];
    for (const el of places) {
      const text = (el.innerText || "").trim();
      if (visible(el) && text && pattern.test(text) &&
        !el.closest("#fleet-lock,[data-message-author-role],article,.markdown,.prose")) return text.slice(0, 300);
    }
    const scope = document.querySelector("main") || document.body;
    // Текст переписки не в счёт: агент вполне может написать «rate limit» в отчёте.
    const hit = [...scope.querySelectorAll("div,span,p")]
      .slice(-300)
      .find(
        (el) =>
          el.childElementCount === 0 &&
          visible(el) &&
          !el.closest("[data-message-author-role],[data-testid^='conversation-turn'],article") &&
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
        !el.closest("[data-message-author-role],article,.markdown,.prose,#fleet-lock");
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
        if (el.childElementCount !== 0 || el.closest("#fleet-lock,.markdown,.prose,[data-message-author-role='user']")) return false;
        const text = (el.textContent || "").trim();
        return text.length <= 160 && INTERRUPTED.test(text);
      });
    if (hit) return hit.textContent.trim().slice(0, 160);
    // Запасной путь: надпись внутри последнего ответа ассистента — только короткой
    // отдельной строкой. Ложное срабатывание стоит одной перезагрузки (reloadPlan
    // ограничивает частоту), пропуск — вкладки, висящей до утра.
    const all = messages();
    const last = all[all.length - 1];
    if (!last || last.getAttribute("data-message-author-role") !== "assistant") return "";
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
        if (el.closest("#fleet-lock,.markdown,.prose,[data-message-author-role='user'],pre,code")) return false;
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
        !b.closest("#fleet-lock,[data-message-author-role='user']") &&
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
    const generating = Boolean(stopButton());
    const composer = findComposer();
    if (generating) lastChangeAt = Date.now();
    return {
      label: label(),
      generating,
      idleSec: (Date.now() - lastChangeAt) / 1000,
      stallSec: (Date.now() - textChangeAt) / 1000,
      composerPresent: Boolean(composer),
      composerEmpty: composerText(composer) === "",
      composerOwned: Boolean(pendingSubmission && composerText(composer) === pendingSubmission),
      lastRole: lastRole(),
      hasContinueButton: Boolean(continueButton()),
      ui: uiDiagnostics(),
      errorText: errorText(),
      rateLimited: rateLimitText(),
      rateLimitKind: rateLimitKind(),
      sessionReady: messages().length > 0 && Boolean(composer),
      modeCapabilities: safe(() => globalThis.FleetChatModes?.probe(document) || null, null),
      blocked: noticeText(HARD_BLOCK),
      networkSuspicious: safe(() => noticeText(NETWORK_SUSPICIOUS), ""),
      blockerWaiting: safe(waitingForPermissionText, ""),
      interrupted: safe(interruptedText, ""),
      pageLoadFailed: safe(pageLoadFailureText, ""),
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

  function typeInto(el, text) {
    focusEditor(el);
    if (el.tagName === "TEXTAREA") {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    insertEditorText(el, text);
    if (composerText(el) !== text) {
      const data = new DataTransfer();
      data.setData("text/plain", text);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }
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

  async function startChat(mention, text) {
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
    const existing = composerText(el);
    if (existing) {
      // После выгрузки/reinject content-script локальный pendingSubmission теряется, но
      // background всё ещё знает точный starter. На новом чате безопасно дослать только
      // собственную заготовку: exact body либо mention-chip + exact body. Любой другой
      // текст остаётся человеческим черновиком и никогда не отправляется.
      const expectedLabel = LABEL.exec(text)?.[1] || "";
      const mentionText = normalizeComposer(mention || "");
      const noTurns = messages().length === 0;
      const lowExisting = existing.toLowerCase();
      const ownRecoveredDraft = noTurns && (
        existing === text || existing.endsWith(text) ||
        (expectedLabel && existing.includes(expectedLabel) && /fleet_join\s*\(/i.test(existing))
      );
      if (ownRecoveredDraft) {
        return submit(el, messages().length, "восстановлена наша стартовая заготовка после reinject");
      }

      // Новый mention-chip может сериализоваться не как точное "@name", а как имя коннектора
      // с декоративным текстом/неразрывными пробелами. На пустом автозапускаемом чате это
      // всё равно наша частичная заготовка: очищаем её и собираем starter заново.
      const ownPartialDraft = noTurns && (
        (mentionText && lowExisting.includes(mentionText.toLowerCase())) ||
        (expectedLabel && existing.includes(expectedLabel)) ||
        /fleet_join\s*\(/i.test(existing)
      );
      if (!ownPartialDraft) return { ok: false, detail: "в поле ввода уже есть чужой текст" };
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
      typeInto(el, text);
    }
    await sleep(600);
    if (!composerText(el)) return { ok: false, detail: "текст не встал в поле ввода" };
    return submit(el, before, `коннектор ${chip}`);
  }

  function sendButton(el) {
    const roots = [el?.closest?.("form"), composerRoot(), document].filter(Boolean);
    for (const root of roots) {
      for (const selector of SEL.send) {
        const send = root.querySelector?.(selector);
        if (send && visible(send) && !send.disabled && send.getAttribute("aria-disabled") !== "true") return send;
      }
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
    if (send) {
      send.focus?.();
      send.click();
    } else {
      for (const type of ["keydown", "keypress", "keyup"]) {
        el.dispatchEvent(new KeyboardEvent(type, {
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true,
        }));
      }
    }
    for (let i = 0; i < 15; i += 1) {
      await sleep(1000);
      if (stopButton() || messages().length > before) {
        pendingSubmission = "";
        return { ok: true, detail: `сообщение ушло${note ? `; ${note}` : ""}` };
      }
      if (rateLimitText() && !noticeText(HARD_BLOCK)) await dismissRateLimit();
    }
    return { ok: false, detail: `после отправки нет ни генерации, ни нового сообщения${note ? `; ${note}` : ""}` };
  }

  async function sendText(text) {
    const before = messages().length;
    const el = findComposer();
    if (!el) return { ok: false, detail: "не найдено поле ввода",
                      pageText: safe(cloudflarePageText, "") };
    if (pendingSubmission && composerText(el) === pendingSubmission) return submit(el, before, "повтор нашей неотправленной заготовки");
    if (composerText(el)) return { ok: false, detail: "в поле ввода уже есть текст" };
    typeInto(el, text);
    await sleep(500);
    if (composerText(el) !== text) return { ok: false, detail: "текст не встал в поле ввода" };
    return submit(el, before);
  }

  async function act(message) {
    if (message.action === "dismiss-rate-limit") return dismissRateLimit();
    if (message.action === "dismiss-gate") return dismissTransientGate();
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
        ? sendText(message.text)
        : startChat(message.mention, message.text));
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
