// Решение «что сделать с вкладкой» — чистая функция, без браузера и сети.
//
// Вкладка web-ChatGPT работает окнами ~27 минут: ход кончается, а задача во флоте
// ещё взята. Тогда нужно одно «продолжи». Всё остальное здесь — предохранители:
// не мешать человеку, не долбить вкладку, которая не двигается, и не продолжать то,
// что уже сдано.

export const DEFAULTS = {
  idleGraceSec: 5, // завершённый ход подхватываем на ближайшем тике, без лишней четверти минуты
  minGapSec: 20, // обычный follow-up не должен ждать минуту; фактический тик всё равно ограничивает темп
  maxFruitless: 2, // два пустых хода подряд — это blocker, а не повод спамить дальше
  maxContinuesPerTask: 6, // длинная серия «продолжи» означает системный blocker, а не полезную работу
  // Наше сообщение последнее, генерации нет, ответа нет — ход сорвался: «Время доставки
  // сообщения истекло», обрыв после перезагрузки. До 22.09 это сразу останавливало вкладку
  // навсегда («сообщение отправлено, ответа нет»), и после каждой перезагрузки из-за
  // «Соединение прервано» агент вставал насовсем. Теперь — повторить.
  stuckUserTurnSec: 120,
  failedTurnSec: 5, // явный UI-сбой держится немного → сразу нажимаем штатный Retry
  retryGapSec: 20, // повтор одной вкладке быстро, но без двойного клика на одном тике
  maxRetriesPerTask: 4, // подряд без прогресса → стоп и автоматический recovery вместо спама
  startRetryAfterSec: 15, // fleet_join должен появляться быстро; иначе повторяем первый шаг
  maxStartRetries: 2,
};

// Метки, которые ставит автозапуск (newLabel в background.js): f0922-1743l3o. Вкладка с
// такой меткой — наша, даже если её нет в ownTabs: 22.09 вчерашняя вкладка W11 висела
// пустой весь день, потому что память расширения о ней потерялась.
export const AUTOSTART_LABEL = /^f\d{4}-\d{4}[a-z0-9]{3}$/;
export const isAutostartLabel = (label) => AUTOSTART_LABEL.test(String(label || ""));

export const LAUNCH_DEFAULTS = {
  // Все подключённые продуктовые проекты остаются включёнными. Приоритет задаёт сам флот;
  // ускорение одного проекта никогда не должно молча выключать два остальных.
  projects: [], // discovered from this installation's fleet, or local-settings.json
  // Столько воркеров флота одновременно держат задачи (во всех браузерах). До 22.09.2026
  // было 2, но и их не набиралось: каждый чат брал одну задачу, и в среднем работало 0.7
  // агента — упирались в темп новых чатов. Теперь чат берёт задачи подряд.
  // 09.10.2026: 8 → 16 (владелец): вкладки — одноразовые клавиатуры (maxTabs 3), память не растёт;
  // при 8 местах на все проекты 17 готовых задач Fleet Platform стояли без агентов.
  maxWorking: 500, // 13.10 (владелец): число агентов ограничивает только аккаунт ChatGPT (перегрузка → пауза), не мы
  betweenTasksMin: 15, // держим полезный чат между задачами: startup дороже 15 минут idle-контекста
  // 14.10: новый чат открывается только после резерва настоящей задачи.
  parkBufferMin: 0,
  parkBufferMax: 0,
  // Потолок вкладок-агентов в этом браузере — рабочие места плюс запас на вкладки, которые
  // сдали работу и ждут закрытия. Ниже мест он не бывает: при 8 местах и потолке 6
  // (так было до 22.09) до восьми агентов было не дойти никогда.
  spareTabs: 2,
  // Вкладка — одноразовая клавиатура (архитектура v2 §3, 07.10.2026): ход идёт на серверах
  // OpenAI и без вкладки, будит замолчавший разговор сервер (fleet/wake.py). Агентов — maxWorking,
  // вкладок одновременно — maxTabs. До этого вкладка жила весь ход: 13 вкладок, 4.6 ГБ, 194 МБ свободно.
  maxTabs: 4, // 16.10: ограничиваем одновременно загруженный интерфейс, не число чатов
  // Свободной памяти меньше — новую вкладку не открывать. Вкладка-клавиатура живёт ~1 мин и
  // стоит ~300 МБ; у машины владельца обычный фон — 450–600 МБ свободно, поэтому 1200 (0.17)
  // запрещал любой новый чат. 194 МБ (было до 0.17) — уже подкачка.
  minFreeMemMB: 350,
  // Это темп владельца, а не выдуманное окно квоты ChatGPT. Старый журнал не измерял
  // восстановление: оставленный диалог повторно запускал паузу на 20/40 минут.
  minGapSec: 4, // 09.10 (владелец): запуск быстрее, аккаунт выдерживает
  maxPending: 2,
  joinTimeoutSec: 150, // starter ещё НЕ ушёл: 2.5 мин достаточно, чтобы не держать мёртвый project-tab
  startedJoinTimeoutSec: 480, // starter подтверждён: первый ответ/tool-call ChatGPT иногда приходит через 5–6 мин
  // 2 мин: вкладка — клавиатура, а не место агента; чат остаётся в проекте и сервер разбудит его
  // по адресу. Было 15 мин — простаивающие вкладки держали гигабайты.
  closeIdleSec: 120,
  closeFailedSec: 180, // не подключилась — быстро закрываем осиротевшую вкладку
  activeSilenceMin: 60, // воркер молчит дольше — не считаем его занятым местом
};

// projects: [{ name, count, next: {tz, kind} | null }] в порядке выдачи, или null —
//           сервер очередь не сообщает
// agentLabels: метки вкладок-агентов в этом браузере (из первых сообщений)
// workers: воркеры флота (fleet_list)
// launch:  { launched: {label: {at, task}}, lastLaunchAt }
// → { action: 'skip'|'launch', reason, target?: {project, tz, kind}, launch, failed: [label] }
//
// Вкладка открывается под проект (fleet_expect), поэтому задача неотмеченного проекта,
// стоящая выше в очереди, запуск не блокирует: новая вкладка её и не возьмёт.
export function clearLegacyCooldown(launch = {}) {
  const { cooldownUntil, cooldownSec, chatCeiling, lastRateLimitAt, ...rest } = launch;
  return rest;
}

// Отточенные параметры: их верное значение задаёт механика ChatGPT и браузера, подбирается по
// телеметрии и правится в коде (DEFAULTS, LAUNCH_DEFAULTS, RELOAD_DEFAULTS, KEYBOARD_DEFAULTS),
// а не владельцем. До 0.18 они были ручками окна, и старые сохранённые значения (spareTabs 4,
// maxPending 10) молча перекрывали новые умолчания. Владельцу остаются намерения: включено,
// проекты, сколько агентов; служебное — вхолостую и блокировка ввода.
export const TUNED_KEYS = [
  "autostart", "spareTabs", "maxPending", "minGapMin", "interruptedSec", "failedTurnSec",
  "stuckUserTurnSec", "stallMin", "retryGapSec", "reloadGapMin", "reloadsPerHour", "keyboard",
];

// Новый проект флота включается сам; снятая владельцем галочка остаётся снятой (08.10.2026).
// Раньше список отмеченных был белым списком: проект, зарегистрированный после настройки окна
// (Mill Works, memory_service), молча не получал ни одного нового чата — агенты брали его задачи только
// в перерывах между своими. `projectsSeen` — все проекты, которые окно уже видело: только
// невиданные добавляются в `projects`. → { projects, projectsSeen, added } или null — менять нечего.
export function includeNewProjects(cfg, fleetProjects) {
  const names = (fleetProjects || []).map((p) => p && p.name).filter(Boolean);
  const projects = Array.isArray(cfg.projects) ? cfg.projects : [];
  const seen = Array.isArray(cfg.projectsSeen) ? cfg.projectsSeen : [...projects];
  const added = names.filter((name) => !seen.includes(name) && !projects.includes(name));
  const unseen = names.filter((name) => !seen.includes(name));
  if (!unseen.length && Array.isArray(cfg.projectsSeen)) return null;
  return { projects: [...projects, ...added], projectsSeen: [...new Set([...seen, ...names])], added };
}

export function migrateSettings(saved = {}) {
  for (const key of ['projects', 'projectsSeen']) {
    if (Array.isArray(saved[key]) && saved[key].includes('Fleet Platform')) {
      saved = {...saved,[key]:saved[key].filter((name) => name !== 'Fleet Platform')};
    }
  }
  if (saved.paceVersion >= 10 && !["maxNewChatsPerHour", "chatCeiling", "learnCeiling"].some((key) => key in saved)) return saved;
  const v8 = migrateV8(saved);
  const obsolete = new Set(["maxNewChatsPerHour", "chatCeiling", "learnCeiling"]);
  const clean = Object.fromEntries(Object.entries(v8).filter(([key]) => !TUNED_KEYS.includes(key) && !obsolete.has(key)));
  return { ...clean, paceVersion: 10 };
}

function migrateV8(saved) {
  if (saved.paceVersion >= 8) return saved;

  const { learnCeiling, ...rest } = saved;
  const projects = Array.isArray(saved.projects) ? saved.projects : null;
  const oldDefault = ["Nova VPN", "Beta App"];
  const oldDefaultSet = projects &&
    projects.length === oldDefault.length &&
    oldDefault.every((name) => projects.includes(name));
  const previousGap = Number(saved.minGapMin);
  const oldDefaultGap = !Number.isFinite(previousGap) || previousGap <= 0 || previousGap === 1.5 ||
    (saved.paceVersion === 5 && previousGap === 0.5);
  const minGapMin = oldDefaultGap ? 0.25 : Math.max(0.25, previousGap);

  // v8: hands-off режим владельца подтверждён 25.09 после обнаруженного stale disabled-state.
  // Автоматику включаем ОДИН раз при миграции; явное выключение уже на v8 сохраняется.
  return {
    ...rest,
    paceVersion: 8,
    enabled: true,
    autostart: true,
    ...(projects ? { projects: oldDefaultSet ? [...projects, "EchoRedactor"] : projects } : {}),
    minGapMin,
  };
}

// freeMemMB: свободная память машины (chrome.system.memory) или null — неизвестно.
export function pruneLaunched(launched = {}, workers = [], nowMs = Date.now(), cfg = LAUNCH_DEFAULTS) {
  const byLabel = new Map(workers.map((w) => [w.label, w]));
  const pending = {};
  const failed = [];
  for (const [label, info] of Object.entries(launched || {})) {
    const worker = byLabel.get(label);
    // Чат без закреплённой задачи уже присоединился к флоту: он больше не pending,
    // а при свежем fleet_take учитывается отдельно в буфере. Не удваивать место.
    if (info.park && worker) continue;
    // fleet_join ещё не равен fleet_take: иначе на одну задачу открывались два чата.
    // Старый сервер без строки бюджета не даёт различить эти состояния.
    const awaitingPreclaimedStart = info.preclaimed && !info.startedAt && info.needsStart !== false;
    if (worker && !awaitingPreclaimedStart && (worker.task || worker.taken > 0 || worker.budgetLeft == null)) continue;
    // До подтверждённой отправки project-tab можно выбрасывать быстро. После отправки
    // ChatGPT может несколько минут формировать первый ответ и только потом вызвать fleet_join.
    // Один общий короткий timeout раньше снимал reservation с живого чата и открывал дубликат.
    const started = Boolean(info.startedAt || info.countedChatAt || info.needsStart === false);
    const timeoutSec = started ? (cfg.startedJoinTimeoutSec || cfg.joinTimeoutSec) : cfg.joinTimeoutSec;
    if (nowMs - info.at > timeoutSec * 1000) failed.push(label);
    else pending[label] = info;
  }
  return { launched: pending, failed };
}

// history — только реально созданные чаты (первое сообщение подтверждено / появился /c/),
// а не просто открытые project-вкладки. Последние 5 ч нужны для отчёта о перегрузке,
// но НЕ ограничивают количество запусков.
export function recordStartedChat(launch = {}, label, nowMs = Date.now()) {
  const state = { history: [], launched: {}, ...launch };
  const info = state.launched?.[label];
  if (info?.countedChatAt) return state;
  const history = [...(state.history || []).filter((at) => nowMs - at < 5 * 3600_000), nowMs];
  return {
    ...state,
    history,
    launched: info ? { ...state.launched, [label]: { ...info, countedChatAt: nowMs } } : state.launched,
  };
}

export function decideLaunch(projects, agentLabels, workers, launch, nowMs, cfg = LAUNCH_DEFAULTS, rand = Math.random, freeMemMB = null) {
  const state = { launched: {}, lastLaunchAt: 0, nextGapSec: cfg.minGapSec, history: [], ...clearLegacyCooldown(launch) };
  state.history = (state.history || []).filter((at) => nowMs - at < 5 * 3600_000);
  const pruned = pruneLaunched(state.launched, workers, nowMs, cfg);
  const launched = pruned.launched;
  const failed = pruned.failed;
  const next = { ...state, launched };
  const skip = (reason) => ({ action: "skip", reason, launch: next, failed });

  if (!projects) return skip("сервер не сообщает очередь — вслепую не запускаю");
  if ((state.overloadUntil || 0) > nowMs) {
    return skip(`аккаунт перегружен — новые чаты на паузе ещё ${Math.ceil((state.overloadUntil - nowMs) / 60_000)} мин`);
  }
  if (!cfg.projects.length) return skip("не отмечен ни один проект");
  const reservations = (project) => Object.values(launched).filter((info) =>
    info.project === project || (!info.project && info.task?.startsWith(`${project}/`))).length;
  // A server claim is already absent from the ready forecast. Subtract only
  // legacy, unclaimed starts; subtracting preclaims again hid real free tasks.
  const unclaimedReservations = (project) => Object.values(launched).filter((info) =>
    !info.preclaimed && (info.project === project || (!info.project && info.task?.startsWith(`${project}/`)))).length;

  // Не забивать все maxPending первым проектом очереди. При нескольких активных проектах
  // сначала даём каждому хотя бы один pending-start, затем второй и т.д. Внутри одинакового
  // числа reservation сохраняется серверный порядок проектов/приоритетов.
  const runnable = projects
    .filter((p) => cfg.projects.includes(p.name) && p.count > unclaimedReservations(p.name) && p.next);
  const pick = runnable.length
    ? runnable.reduce((best, p) => reservations(p.name) < reservations(best.name) ? p : best)
    : null;
  // New conversations require real work. A parking buffer repeatedly created
  // agents whose first answer was "empty queue"; waiting belongs to existing chats.
  if (!pick) {
    const reserved = projects.filter(p => cfg.projects.includes(p.name) && p.count > 0 && p.next);
    const elsewhere = projects.filter(p => !cfg.projects.includes(p.name) && p.count > 0);
    return skip((reserved.length ? "работа зарезервирована pending-start" :
      "в отмеченных проектах нет свободной задачи для нового агента") +
      (elsewhere.length ? `; есть в неотмеченных: ${elsewhere.map(p => `${p.name} (${p.count})`).join(", ")}` : ""));
  }
  const pending = Object.keys(launched);
  // В fleet_list нет явного флага park: недавний вызов fleet_take приближённо
  // определяется по свежему silenceMin. Не считать чужую паузу между задачами,
  // исчерпавшие бюджет чаты и невалидную/устаревшую телеметрию.
  const own = new Set(agentLabels);
  const fresh = (w, limit) => Number.isFinite(w.silenceMin) &&
    w.silenceMin >= 0 && w.silenceMin < limit;
  const between = (w) => !w.task && own.has(w.label) && w.budgetLeft > 0 &&
    (w.taken == null || w.taken > 0) &&
    fresh(w, cfg.betweenTasksMin + 1);
  const parked = workers.filter((w) => !launched[w.label] && !w.task && !between(w) &&
    fresh(w, Math.min(cfg.betweenTasksMin + 1, cfg.activeSilenceMin)) &&
    (w.budgetLeft == null || (Number.isFinite(w.budgetLeft) && w.budgetLeft > 0)));
  const working = workers.filter((w) => !launched[w.label] &&
    ((w.task && fresh(w, cfg.activeSilenceMin)) || between(w))).length;
  const parkMax = Math.max(0, Math.floor(Number.isFinite(cfg.parkBufferMax) ? cfg.parkBufferMax : LAUNCH_DEFAULTS.parkBufferMax));
  const parkMin = Math.min(parkMax, Math.max(0,
    Math.floor(Number.isFinite(cfg.parkBufferMin) ? cfg.parkBufferMin : LAUNCH_DEFAULTS.parkBufferMin)));
  // Все pending-start считаются стартующими: новый buffer-start не должен
  // раздувать парк, пока другие вкладки только проходят fleet_join.
  const bufferSize = parked.length + pending.length;
  // Присоединившийся, но ещё не взявший задачу worker не должен одновременно
  // считаться будущим парковочным местом и резервировать эту же выдачу.
  // Обычный pending без join не запрещает пополнение буфера.
  const joinedPending = Object.keys(launched).some((label) => workers.some(
    (w) => w.label === label && !w.task && w.taken === 0));
  if (!pick && joinedPending && projects.some((p) => cfg.projects.includes(p.name) &&
      p.count > 0 && p.count <= reservations(p.name))) {
    return skip("работа уже зарезервирована pending-start");
  }
  if (!pick && (bufferSize >= parkMin || bufferSize >= parkMax)) {
    const reserved = projects
      .filter((p) => cfg.projects.includes(p.name) && p.count > 0 && p.count <= reservations(p.name))
      .map((p) => `${p.name} (${p.count} runnable / ${reservations(p.name)} pending)`);
    const elsewhere = projects
      .filter((p) => !cfg.projects.includes(p.name) && p.count > 0)
      .map((p) => `${p.name} (${p.count})`);
    const context = reserved.length
      ? `runnable работа отмеченных проектов уже зарезервирована pending-start: ${reserved.join(", ")}`
      : "в отмеченных проектах выдавать нечего";
    return skip(`буфер полон ${bufferSize}/${parkMin}; ${context}` +
      (elsewhere.length ? `; есть в неотмеченных: ${elsewhere.join(", ")}` : ""));
  }
  const target = pick ? { project: pick.name, tz: pick.next.tz, kind: pick.next.kind } : null;
  if (pending.length >= cfg.maxPending) return skip(`ожидают подключения ${pending.length}/${cfg.maxPending} вкладок`);
  const tabs = agentLabels.length + pending.filter((label) => !agentLabels.includes(label)).length;
  const maxTabs = cfg.maxTabs ?? cfg.maxWorking + cfg.spareTabs;
  if (tabs >= maxTabs) return skip(`открыто ${tabs} вкладок-агентов — потолок ${maxTabs}`);
  if (freeMemMB != null && freeMemMB < cfg.minFreeMemMB) {
    return skip(`свободно памяти ${freeMemMB} МБ < ${cfg.minFreeMemMB} МБ — новую вкладку не открываю`);
  }
  // Своя вкладка между задачами (сдала, лимит чата не исчерпан, только что работала) —
  // тоже занятое место: иначе на каждую сдачу открывался бы лишний новый чат.
  if (working + pending.length >= cfg.maxWorking) return skip(`заняты все рабочие места: ${working}+${pending.length} стартуют/${cfg.maxWorking}`);
  const gapSec = Math.min(next.nextGapSec || cfg.minGapSec, cfg.minGapSec * 1.5);
  if (nowMs - next.lastLaunchAt < gapSec * 1000) {
    return skip(`недавно уже запускали — следующий не раньше чем через ${Math.ceil((next.lastLaunchAt + gapSec * 1000 - nowMs) / 60000)} мин`);
  }
  next.lastLaunchAt = nowMs;
  // history учитывает только подтверждённые старты и не является лимитом.
  next.nextGapSec = Math.round(cfg.minGapSec * (1 + 0.5 * rand()));
  return { action: "launch", reason: target
    ? `${target.project}: следующая ${target.tz}`
    : `буфер парковки ${bufferSize}/${parkMin}`, target, launch: next, failed };
}

export const KEYBOARD_DEFAULTS = {
  enabled: true,
  closeGeneratingSec: 4, // адрес чата уже сохранён; генерация продолжается без renderer
  pressureMemMB: 800, // свободно меньше — закрываем и простаивающие свои вкладки, не дожидаясь closeIdleSec
};

// Одноразовая клавиатура: закрыть свою вкладку, как только она своё отработала.
// state: проба вкладки + { onChat, wakePending, freeMemMB }; mem: { generatingSince } этой вкладки.
// → { close, reason, mem }. Не закрываем то, что требует рук на странице: подтверждение
// инструмента/вход (blocked), лимит, сбой, черновик в поле ввода, ещё не созданный чат.
export function keyboardPlan(state, own, mem = {}, nowMs = Date.now(), cfg = KEYBOARD_DEFAULTS) {
  const keep = (reason, next = {}) => ({ close: false, reason, mem: next });
  if (!cfg.enabled || !own) return keep("");
  if (!state.onChat) return keep("чат ещё не создан");
  if (state.blocked || state.rateLimited || state.failed || state.pageLoadFailed || state.interrupted) {
    return keep("странице нужны руки или восстановление");
  }
  if (state.composerOwned && !state.composerEmpty) return keep("в поле ввода черновик");
  if (state.generating) {
    const since = mem.generatingSince || nowMs;
    if (nowMs - since >= cfg.closeGeneratingSec * 1000) {
      return { close: true, reason: "ход идёт на сервере — клавиатура больше не нужна", mem: {} };
    }
    return keep("ход только начался", { generatingSince: since });
  }
  if (state.wakePending) return keep("ждёт отправки будки");
  if (state.freeMemMB != null && state.freeMemMB < cfg.pressureMemMB) {
    return { close: true, reason: `свободно ${state.freeMemMB} МБ — простаивающую вкладку закрываю, разбудит сервер`, mem: {} };
  }
  return keep("простаивает");
}

// Закрыть свою вкладку, которая точно больше не нужна: чат остаётся в проекте ChatGPT,
// а память браузера — главный потолок числа агентов. «Точно» — это одновременно:
// вкладку открыл автозапуск; во флоте у её воркера нет задачи (сдал работу, вынес
// вердикт или задач не было); не идёт генерация; поле ввода пустое; и переписка, и сам
// воркер (вызовы инструментов) молчат closeIdleSec.
// → { close: boolean, reason: string } | null (не наша вкладка / работа ещё идёт)
export function closePlan(tab, worker, ownTab, cfg = LAUNCH_DEFAULTS) {
  if (!ownTab) return null;
  if (!worker) {
    // Так и не подключилась (отказ на старте, коннектор не встал): уведомление ушло на
    // joinTimeoutSec, а вкладка держит память и место в потолке вкладок.
    if (!tab.label || tab.pendingJoin || tab.generating || !tab.composerEmpty) return null;
    if (tab.idleSec < cfg.closeFailedSec) return null;
    return { close: true, reason: "так и не подключилась к флоту — закрываю (чат остался в проекте)" };
  }
  if (worker.task) return null;
  if (tab.generating) return { close: false, reason: "работа сдана; жду конца генерации" };
  if (!tab.composerEmpty) return { close: false, reason: "работа сдана; в поле ввода текст — не закрываю" };
  const quietSec = Math.min(tab.idleSec, worker.silenceMin * 60);
  if (quietSec < cfg.closeIdleSec) {
    const left = Math.max(1, Math.ceil((cfg.closeIdleSec - quietSec) / 60));
    return { close: false, reason: `работа сдана; закрою примерно через ${left} мин` };
  }
  return { close: true, reason: "работа сдана, вкладка молчит" };
}

export function shouldClose(tab, worker, ownTab, cfg = LAUNCH_DEFAULTS) {
  return Boolean(closePlan(tab, worker, ownTab, cfg)?.close);
}

// «Продолжи» разными словами: один и тот же текст каждые полчаса во многих вкладках —
// лишний машинный отпечаток. Смысл у всех вариантов один.
export const CONTINUE_TEXTS = [
  "продолжи",
  "Продолжай по задаче.",
  "Продолжи работу с того места, где остановился.",
  "Давай дальше по памятке.",
  "Продолжай, пожалуйста.",
  "Дальше — по плану задачи.",
];

export function continueText(rand = Math.random) {
  return CONTINUE_TEXTS[Math.floor(rand() * CONTINUE_TEXTS.length) % CONTINUE_TEXTS.length];
}

export function emptyMemory() {
  return {
    task: null, continues: 0, fruitless: 0, lastContinueAt: 0, stopped: "", jitterSec: 0,
    doneTask: null, nudgedTaken: 0, retries: 0, lastRetryAt: 0, startRetries: 0, blockerNudges: 0,
  };
}

// Чат сдал работу, а следующую не взял (ход кончился на сдаче или агент остановился по
// старой привычке «одна задача — один чат»). Одно такое сообщение на сданную задачу.
export function nextTaskText(workerId) {
  return `Задача закрыта. Возьми следующую в этом же чате: fleet_take(agent='${workerId}'), и дальше по её памятке. Задач нет или лимит чата исчерпан — коротко отчитайся и остановись.`;
}

export function blockerRecoveryText(workerId) {
  return `Не жди владельца и не проси ручного одобрения. Контур флота должен решать это автоматически. Прочитай точный код отказа: если право уже есть — продолжай разрешённым способом/run_task; если request_note отклонён — не повторяй его. Если без этого работа объективно невозможна, освободи claim через fleet_block(agent='${workerId}', reason='blocked: <точная причина>') или fleet_finish(..., summary='blocked: ...'). Иначе продолжай задачу сейчас.`;
}

// Отработавшей своей вкладке, у чата которой остался лимит, — «бери следующую», если в
// отмеченных проектах есть что выдать. Иначе — ничего: закроет closePlan.
//
// «Уже подталкивали» помнится по числу взятых чатом задач (worker.taken из fleet_list), а
// не по памяти вкладки: 22.09 чаты, сдавшие работу до обновления расширения, остались без
// подталкивания — память вкладки о сданной задаче к тому времени уже сбросилась.
function nudgeNext(tab, worker, next, cfg) {
  const skip = (reason) => ({ action: "skip", reason, mem: next });
  const taken = worker.taken || 0;
  if (!tab.own || taken < 1) return skip("у воркера нет задачи — работа сдана или ещё не взята");
  if (!(worker.budgetLeft > 0)) return skip("работа сдана; лимит задач этого чата исчерпан");
  if (!tab.queueHasWork) return skip("работа сдана; в отмеченных проектах выдавать нечего");
  if (next.nudgedTaken >= taken) return skip("работа сдана; «бери следующую» уже отправлено");
  if (tab.generating) return skip("идёт генерация");
  if (!tab.composerEmpty && !tab.composerOwned) return skip("в поле ввода есть текст — там работает человек");
  if (tab.lastRole === "user") return skip("ждём ответа на последнее сообщение");
  if (tab.idleSec < cfg.idleGraceSec) return skip(`работа сдана; тишина ${Math.round(tab.idleSec)}с < ${cfg.idleGraceSec}с`);
  next.nudgedTaken = taken;
  return {
    action: "continue",
    reason: `${next.doneTask || `задача №${taken}`} сдана, лимит чата не исчерпан — «бери следующую»`,
    text: nextTaskText(worker.id),
    mem: next,
  };
}

// Обрыв ответа: ChatGPT пишет «Соединение прервано. Ожидание полного ответа» и может так
// висеть бесконечно — с кнопкой «Стоп», то есть для decide это вечная генерация. Лечится
// перезагрузкой страницы: ответ подтягивается целиком, дальше вкладку ведёт decide.
export const RELOAD_DEFAULTS = {
  interruptedSec: 45, // надпись должна продержаться — короткий обрыв ChatGPT чинит сам
  pageFailureSec: 2, // полный экран "conversation failed to load" сам редко оживает
  pageFailureGapSec: 15, // если reload не помог, пробуем снова быстро, но без tight loop
  composerMissingSec: 8, // реальный /c/ с воркером, но composer исчез — UI сломан
  composerMissingGapSec: 30,
  // Зависание без надписи (22.09): кнопка «Стоп» есть, а переписка не меняется и воркер не
  // вызывает инструментов. Перезагрузка не отменяет ответ: ChatGPT доводит его на сервере и
  // подтягивает после загрузки страницы, дальше вкладку ведёт decide.
  stallSec: 360,
  minGapSec: 180, // одну вкладку — не чаще раза в 3 минуты
  // На всё расширение: загрузка страницы — тоже запрос к ChatGPT. 22.09 при 10 вкладках
  // обрывов было 7 за 40 минут; 8 в час упёрлись бы в потолок, и вкладки встали бы.
  maxPerHour: 24,
};

// Зависла ли генерация: «Стоп» на месте, переписка не меняется stallSec, и воркер столько же
// не вызывает инструментов (долгий вызов инструмента — не зависание: он обновляет silenceMin).
export function hungText(tab, worker, cfg = RELOAD_DEFAULTS) {
  if (!tab.generating || !(tab.stallSec >= cfg.stallSec)) return "";
  if (worker && worker.silenceMin * 60 < cfg.stallSec) return "";
  const min = Math.round(tab.stallSec / 60);
  return `идёт генерация, но переписка не меняется ${min} мин${worker ? ` и воркер молчит ${worker.silenceMin} мин` : ""}`;
}

// tab: { interrupted: string, composerEmpty, generating, stallSec }; mem: { since, lastAt } по
// вкладке; history: времена перезагрузок всех вкладок; worker — для зависания без надписи.
// → { reload: boolean, reason, mem, history }
export function reloadPlan(tab, mem, history, nowMs, cfg = RELOAD_DEFAULTS, worker = undefined) {
  const state = { since: 0, lastAt: 0, ...mem };
  const recent = (history || []).filter((at) => nowMs - at < 3600_000);
  const out = (reload, reason) => ({ reload, reason, mem: state, history: recent });
  const pageFailure = String(tab.pageLoadFailed || "");
  const composerMissing = Boolean(tab.composerMissing);
  const interrupted = pageFailure || (composerMissing ? "поле ввода разговора не загрузилось" : tab.interrupted);
  if (!interrupted) {
    state.since = 0;
    const hung = hungText(tab, worker, cfg);
    if (!hung) return out(false, "");
    if (!tab.composerEmpty) return out(false, "зависание, но в поле ввода текст — не перезагружаю");
    if (state.lastAt && nowMs - state.lastAt < cfg.minGapSec * 1000) return out(false, `${hung} — перезагружали недавно, жду`);
    if (recent.length >= cfg.maxPerHour) return out(false, `${hung}; за час уже ${recent.length} перезагрузок — потолок`);
    state.lastAt = nowMs;
    recent.push(nowMs);
    return out(true, `${hung} — зависание, перезагружаю страницу`);
  }
  if (!state.since) state.since = nowMs;
  const heldSec = (nowMs - state.since) / 1000;
  const holdSec = pageFailure ? cfg.pageFailureSec : composerMissing ? cfg.composerMissingSec : cfg.interruptedSec;
  const minGapSec = pageFailure ? cfg.pageFailureGapSec : composerMissing ? cfg.composerMissingGapSec : cfg.minGapSec;
  const kind = pageFailure ? "разговор не загрузился" : composerMissing ? "поле ввода разговора не загрузилось" : "обрыв ответа";
  if (heldSec < holdSec) return out(false, `${kind} ${Math.round(heldSec)}с — жду подтверждения`);
  if (!tab.composerEmpty) return out(false, `${kind}, но в поле ввода текст — не перезагружаю`);
  if (state.lastAt && nowMs - state.lastAt < minGapSec * 1000) {
    return out(false, `${kind} повторился сразу после перезагрузки — жду`);
  }
  if (recent.length >= cfg.maxPerHour) return out(false, `${kind}; за час уже ${recent.length} перезагрузок — потолок`);
  state.lastAt = nowMs;
  state.since = 0;
  recent.push(nowMs);
  return out(true, `«${String(interrupted).slice(0, 60)}» держится ${Math.round(heldSec)}с — перезагружаю страницу`);
}

// Первый шаг ещё раз: вкладка ответила, но fleet_join так и не пришёл. 22.09 на десятой
// вкладке ChatGPT отклонил fleet_join «системой безопасности OpenAI», агент по правилу 9
// остановился, а повтор тем же вызовом сработал сразу. Повторяем тот же вызов, не другой
// путь, и не больше maxStartRetries раз; дальше — уведомление по joinTimeoutSec.
export function startRetryText(label) {
  return `Повтори первый шаг тем же вызовом: fleet_join(label='${label}', kind='chatgpt'), затем fleet_take со своим worker_id — и дальше по инструкции проекта. Отказ платформы на первом вызове бывает разовым; повтор того же вызова — не обход стража. Снова отказ — коротко напиши, какой, и остановись.`;
}

function startRetry(tab, next, nowMs, cfg) {
  const skip = (reason) => ({ action: "skip", reason, mem: next });
  if (!tab.own || !tab.pendingJoin) return skip(`воркер с меткой ${tab.label} не найден во флоте`);
  if (tab.generating) return skip("вкладка стартует — идёт генерация");
  if (!tab.composerEmpty && !tab.composerOwned) return skip("в поле ввода есть текст — там работает человек");
  if (tab.lastRole === "user") return skip("вкладка стартует — ждём первого ответа");
  if (tab.idleSec < cfg.startRetryAfterSec) return skip(`ждём fleet_join (тишина ${Math.round(tab.idleSec)}с)`);
  if (next.startRetries >= cfg.maxStartRetries) return skip(`fleet_join не пришёл и после ${next.startRetries} повторов — жду уведомления о сбое`);
  if (next.lastContinueAt && nowMs - next.lastContinueAt < cfg.retryGapSec * 1000) return skip("повтор первого шага уже отправлен — жду");
  next.startRetries += 1;
  next.lastContinueAt = nowMs;
  return {
    action: "continue",
    reason: `fleet_join не пришёл — повтор первого шага (${next.startRetries}-й)`,
    text: startRetryText(tab.label),
    mem: next,
  };
}

// Ход сорвался: «Время доставки сообщения истекло. Попробуйте ещё раз.», «Что-то пошло не
// так», или наше сообщение последнее, а ответа нет. Это равноценно «надо продолжить»: есть
// кнопка «Повторить» — нажать, нет — отправить «продолжи». Не чаще retryGapSec на вкладку.
function retryTurn(tab, worker, next, nowMs, cfg, rand) {
  const failedFor = tab.failed ? (tab.failedSec ?? tab.idleSec) : 0;
  const stuck = tab.lastRole === "user" && tab.idleSec >= cfg.stuckUserTurnSec;
  if (!(tab.failed && failedFor >= cfg.failedTurnSec) && !stuck) return null;
  const skip = (reason) => ({ action: "skip", reason, mem: next });
  if (next.lastRetryAt && nowMs - next.lastRetryAt < cfg.retryGapSec * 1000) return skip("сбой хода — повтор уже отправлен, жду");
  if (next.retries >= cfg.maxRetriesPerTask) {
    next.stopped = `${next.retries} повторов сорвавшегося хода подряд без прогресса`;
    const reason = `${next.stopped}${tab.failed ? `: ${tab.failed}` : ""}`;
    // Зарегистрированный worker не должен держать claim до stale-timeout только потому,
    // что сломался его ChatGPT turn. Возвращаем задачу в ready для другого worker.
    if (worker?.id) {
      return { action: "release-claim", reason, releaseReason: `handoff: ${reason}`, mem: next };
    }
    return { action: "notify", reason, mem: next };
  }
  next.retries += 1;
  next.lastRetryAt = nowMs;
  next.lastContinueAt = nowMs;
  const why = tab.failed ? `«${tab.failed.slice(0, 60)}»` : "наше сообщение без ответа";
  return {
    action: tab.hasRetryButton ? "click-retry" : "continue",
    reason: `сбой хода (${why}) — ${tab.hasRetryButton ? "жму «Повторить»" : "отправляю «продолжи»"} (${next.retries}-й повтор)`,
    text: worker ? continueText(rand) : startRetryText(tab.label),
    mem: next,
  };
}

// tab:    { label, generating, idleSec, composerEmpty, lastRole, hasContinueButton, errorText,
//           own (вкладку открыл автозапуск), queueHasWork (в отмеченных проектах есть что выдать) }
// worker: { id, label, task, silenceMin, budgetLeft? } | undefined
// mem:    память по вкладке между тиками (см. emptyMemory)
// → { action: 'skip'|'continue'|'click-continue'|'release-claim'|'block-claim'|'notify', reason, mem, text? }
export function decide(tab, worker, mem, nowMs, cfg = DEFAULTS, rand = Math.random) {
  const next = { ...emptyMemory(), ...mem };
  const skip = (reason) => ({ action: "skip", reason, mem: next });

  if (!tab.label) return skip("нет метки агента в первом сообщении — вкладка не из флота");
  if (tab.blocked) return skip(`требуется действие во вкладке: ${tab.blocked}`);
  if (tab.rateLimited) return skip("окно «слишком часто» ещё открыто — отправка ждёт его закрытия");
  if (tab.generating) return skip("идёт генерация");
  if (!tab.composerEmpty && !tab.composerOwned) return skip("в поле ввода есть текст — там работает человек");
  if (next.deliveryRetryAt > nowMs) return skip("предыдущая отправка не подтверждена — жду перед повтором");
  // «Остановлено: сообщение отправлено, ответа нет» до 22.09 значило «навсегда». Теперь
  // такой ход повторяется — старую остановку снимаем, иначе обновление её не вылечит.
  if (next.stopped === "сообщение отправлено, ответа нет") next.stopped = "";
  if (!worker) {
    const retry = tab.own && tab.pendingJoin ? retryTurn(tab, worker, next, nowMs, cfg, rand) : null;
    return retry || startRetry(tab, next, nowMs, cfg);
  }
  if (!worker.task) {
    const keep = { doneTask: next.task || next.doneTask, nudgedTaken: next.nudgedTaken };
    Object.assign(next, emptyMemory(), keep);
    return nudgeNext(tab, worker, next, cfg);
  }
  if (next.task !== worker.task) {
    // Новая задача — счётчики с чистого листа, в том числе снятый стоп.
    Object.assign(next, emptyMemory(), { task: worker.task });
  }
  const progressedAfter = Math.max(next.lastRetryAt || 0, next.lastContinueAt || 0);
  if (next.stopped && /без (единого вызова инструмента|прогресса)/.test(next.stopped) &&
    progressedAfter && worker.silenceMin * 60 < (nowMs - progressedAfter) / 1000 - 30) {
    next.stopped = "";
    next.retries = 0;
    next.fruitless = 0;
  }
  if (next.stopped) return skip(`остановлено: ${next.stopped}`);
  if (tab.generating) return skip("идёт генерация");
  if (!tab.composerEmpty && !tab.composerOwned) return skip("в поле ввода есть текст — там работает человек");
  // Прогресс (вызов инструмента после нашего повтора) — счёт повторов с нуля.
  if (next.lastRetryAt && worker.silenceMin * 60 < (nowMs - next.lastRetryAt) / 1000 - 30) next.retries = 0;
  const retry = retryTurn(tab, worker, next, nowMs, cfg, rand);
  if (retry) return retry;
  // Видимая ошибка ещё не продержалась failedTurnSec: не посылать обычное «продолжи»
  // поверх неё. Через несколько секунд либо исчезнет сама, либо retryTurn обработает её.
  if (tab.failed) return skip(`сбой хода виден ${Math.round(tab.failedSec || 0)}с — жду подтверждения`);
  if (tab.idleSec < cfg.idleGraceSec) return skip(`тишина ${Math.round(tab.idleSec)}с < ${cfg.idleGraceSec}с`);

  if (tab.blockerWaiting) {
    if (next.blockerNudges >= 1) {
      return {
        action: "block-claim",
        reason: "агент повторно ждёт ручных прав/одобрения — освобождаю claim автоматически",
        blockReason: `blocked: агент после recovery-указания снова ждёт ручного решения: ${String(tab.blockerWaiting).slice(0, 300)}`,
        mem: next,
      };
    }
    next.blockerNudges += 1;
    next.continues += 1;
    next.lastContinueAt = nowMs;
    next.jitterSec = 0;
    return {
      action: "continue",
      reason: "агент ждёт ручного решения — перевожу в автоматический blocker/recovery-контур",
      text: blockerRecoveryText(worker.id),
      mem: next,
    };
  }

  const sinceContinueSec = next.lastContinueAt ? (nowMs - next.lastContinueAt) / 1000 : Infinity;
  if (tab.lastRole === "user") return skip("ждём ответа на последнее сообщение");
  if (sinceContinueSec < cfg.minGapSec + (next.jitterSec || 0)) {
    return skip(`прошлое «продолжи» ${Math.round(sinceContinueSec)}с назад`);
  }

  if (next.lastContinueAt) {
    // silenceMin приходит из fleet_list целыми минутами. При быстром follow-up (<60с)
    // прежняя поправка -60 делала любой ход «холостым» математически. Пока окно меньше
    // минуты, 0 минут считаем свежим прогрессом; надёжно наращиваем fruitless только
    // когда интервал уже измерим этой минутной метрикой.
    if (sinceContinueSec < 60) {
      if (worker.silenceMin === 0) next.fruitless = 0;
    } else {
      const idleSinceContinue = worker.silenceMin * 60 >= sinceContinueSec - 60;
      next.fruitless = idleSinceContinue ? next.fruitless + 1 : 0;
    }
  }
  if (next.fruitless >= cfg.maxFruitless) {
    next.stopped = `${next.fruitless} «продолжи» подряд без единого вызова инструмента`;
    return {
      action: "release-claim",
      reason: next.stopped,
      releaseReason: `handoff: чат не делает tool-прогресса — ${next.stopped}`,
      mem: next,
    };
  }
  if (next.continues >= cfg.maxContinuesPerTask) {
    next.stopped = `потолок ${cfg.maxContinuesPerTask} «продолжи» на задачу`;
    return {
      action: "release-claim",
      reason: next.stopped,
      releaseReason: `handoff: исчерпан полезный контекст чата — ${next.stopped}`,
      mem: next,
    };
  }

  next.continues += 1;
  next.lastContinueAt = nowMs;
  next.jitterSec = Math.round(30 * rand()); // небольшой jitter без лишних двух минут простоя
  return {
    action: tab.hasContinueButton ? "click-continue" : "continue",
    reason: `ход закончился, задача ${worker.task} ещё взята (${next.continues}-е продолжение)`,
    mem: next,
  };
}
