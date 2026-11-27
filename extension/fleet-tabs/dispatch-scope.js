// Окно ограниченной отправки (chrome.storage quickDispatchScope) — разовое решение оператора:
// быстрым агентам уходят только одобренные сообщения (sha256) по одобренным адресам.
// В этом режиме выключены будильники, переоткрытие и разгрузка интерфейсов.
//
// 19.11.2026: окно не имело ни срока, ни условия конца. Все 11 одобренных сообщений ушли
// за 15:46–15:57, а потом каждое новое задание и каждая дописка заказчика часами висели
// «вне разрешённого набора» (21 задание), вкладки не перезагружались — флот стоял молча.
//
// Правило: окно закрывается само, когда
//   (а) не осталось одобренного сообщения, которое ещё ждёт отправки, или
//   (б) истёк срок: meta.until, заданный оператором, иначе since + ttlMs.
// Доставка доказана, если расширение подтвердило отправку (meta.delivered), если задание
// исчезло из приказов сервера, или если одобрялся новый чат (url ""), а чат уже создан.
// Любое другое расхождение (изменился текст или адрес) доставкой НЕ считается: окно
// продолжает блокировать до срока.

export const DISPATCH_SCOPE_TTL_MS = 30 * 60_000;

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

export function dispatchScopePlan(scope, meta = {}, jobs = [], digests = {}, now = Date.now(),
  { ttlMs = DISPATCH_SCOPE_TTL_MS, ordersOk = true } = {}) {
  if (scope == null) return { active: false, retire: false, reason: "", meta: null, remaining: [] };
  if (!isObject(scope)) {
    return { active: false, retire: true, reason: "окно ограниченной отправки повреждено", meta: null, remaining: [] };
  }
  const old = isObject(meta) ? meta : {};
  const since = Number.isFinite(old.since) && old.since <= now ? old.since : now;
  const until = Number.isFinite(old.until) ? old.until : since + ttlMs;
  const delivered = isObject(old.delivered) ? { ...old.delivered } : {};
  const next = { since, until, delivered, ...(Number.isFinite(old.until) ? {} : { untilDefault: true }) };
  const byId = new Map((jobs || []).filter((job) => job && job.id).map((job) => [String(job.id), job]));
  const remaining = new Set();
  for (const [id, approved] of Object.entries(scope)) {
    const messages = Array.isArray(approved?.messages) ? approved.messages : [];
    const job = byId.get(id);
    for (const hex of messages) {
      if (delivered[`${id}:${hex}`]) continue;
      // Без свежего списка приказов отсутствие задания ничего не доказывает.
      if (!ordersOk) { remaining.add(id); continue; }
      if (!job || job.close) continue;
      if (job.send && digests[id] === hex) { remaining.add(id); continue; }
      if (!approved.url && job.url) continue; // одобренный новый чат создан: первое сообщение ушло
      remaining.add(id);
    }
  }
  const left = [...remaining];
  if (now >= until) {
    return { active: false, retire: true, meta: next, remaining: left,
      reason: `срок окна ограниченной отправки истёк (${new Date(until).toISOString()}); ждали: ${left.join(", ") || "—"}` };
  }
  if (!left.length) {
    return { active: false, retire: true, meta: next, remaining: left,
      reason: "все одобренные сообщения доставлены — окно ограниченной отправки закрыто" };
  }
  return { active: true, retire: false, reason: "", meta: next, remaining: left };
}
