// Сторож живости быстрых агентов (19.11.2026).
//
// Инвариант: если есть работа (задание с неотправленным сообщением или будильником),
// а флот stallMs подряд ничего не отправил, не получил ни одного ответа и ни одна его
// вкладка не генерирует — это затор, а не ожидание. Причины заторов были разные
// (окно ограниченной отправки без срока, сторож ОЗУ, бессрочное «жду» у вкладки, занятые
// слоты интерфейса), и каждый раз система стояла молча до ручного вмешательства.
// Сторож не угадывает причину: он поднимается по лестнице, по ступени раз в stepMs,
// и каждую ступень пишет в журнал расширения.
//
//   diagnose       — сводка причин блокировки (журнал + событие);
//   clear-local    — снять локальные мягкие блокировки: окно ограниченной отправки,
//                    счётчики повторов открытия, паузы после трёх неудачных отправок;
//   free-slots     — закрыть свои негенерирующие вкладки быстрых агентов (задание и его
//                    очередь остаются на сервере, чат откроется заново по адресу);
//   restart-worker — перезапуск service worker расширения (не чаще restartEveryMs),
//                    затем лестница начинается заново.
//
// Законные общие паузы — аварийная остановка, запрет запусков сервером, перегрузка
// аккаунта, нет связи с сервером, dryRun — затором не считаются: таймер стоит, пока
// пауза идёт, и отсчёт начинается после неё.

export const LIVENESS_DEFAULTS = Object.freeze({
  stallMs: 10 * 60_000,
  stepMs: 5 * 60_000,
  restartEveryMs: 60 * 60_000,
});

export const LIVENESS_STEPS = Object.freeze(["diagnose", "clear-local", "free-slots", "restart-worker"]);

// Прогресс — только то, что двигает работу: подтверждённая отправка, ответ, идущий ход.
export const LIVENESS_PROGRESS_EVENTS = Object.freeze(new Set([
  "quick-sent", "quick-answer", "wake-sent", "quick-send-confirmed",
]));

const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

export function livenessPlan(prev = {}, { demand = 0, progressed = false, hold = "" } = {},
  now = Date.now(), cfg = LIVENESS_DEFAULTS) {
  const lastRestartAt = num(prev?.lastRestartAt);
  const lastProgressAt = progressed ? now : num(prev?.lastProgressAt);
  const keep = { lastRestartAt, lastProgressAt,
    ...(prev?.lastAction ? { lastAction: prev.lastAction, lastActionAt: num(prev.lastActionAt) } : {}) };
  if (!(demand > 0) || progressed) return { state: { ...keep, since: 0, step: 0 }, action: "" };
  if (hold) return { state: { ...keep, since: now, step: 0, hold: String(hold).slice(0, 120) }, action: "" };
  const since = num(prev?.since) > 0 && num(prev.since) <= now ? num(prev.since) : now;
  const step = Math.max(0, Math.min(LIVENESS_STEPS.length - 1, Math.floor(num(prev?.step))));
  const due = since + cfg.stallMs + step * cfg.stepMs;
  if (now < due) return { state: { ...keep, since, step }, action: "", dueAt: due };
  let action = LIVENESS_STEPS[step];
  let nextStep = step + 1, nextSince = since, restartAt = lastRestartAt;
  if (action === "restart-worker") {
    if (now - lastRestartAt < cfg.restartEveryMs) action = "free-slots";
    else restartAt = now;
    nextStep = 0; nextSince = now; // новая лестница после перезапуска или его отказа
  }
  return {
    state: { lastRestartAt: restartAt, lastProgressAt, since: nextSince, step: nextStep,
      lastAction: action, lastActionAt: now },
    action, stalledMs: now - since,
  };
}

// Короткая человекочитаемая сводка причин для журнала и сервера.
export function blockedSummary(blocked = {}, limit = 4) {
  const counts = new Map();
  for (const reason of Object.values(blocked || {})) {
    const key = String(reason || "причина не указана").replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, "…").slice(0, 80);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1]).slice(0, limit)
    .map(([reason, count]) => `${count}× ${reason}`).join("; ");
}
