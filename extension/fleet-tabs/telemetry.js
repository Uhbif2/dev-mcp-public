// События имеют абсолютное время и tabId: повторный probe одного окна — не новое окно.
// Интервал warning→success — наблюдаемое восстановление, НЕ окно серверной квоты.
export function recordEvent(events, type, fields = {}, now = Date.now()) {
  return [...events.filter((e) => now - e.at < 7 * 86400_000), { ...fields, type, at: now }].slice(-4000);
}

export function observeWarning(previous, text, now = Date.now()) {
  if (!text) return { state: null, fresh: false };
  if (previous?.text === text) return { state: previous, fresh: false };
  return { state: { text, since: now }, fresh: true };
}

export function summarizeEvents(events) {
  const pending = new Map();
  const recoverySec = [];
  let warnings = 0, dismissed = 0, ejections = 0, restored = 0;
  for (const e of events) {
    if (e.type === "warning") { warnings++; if (!pending.has(e.tabId)) pending.set(e.tabId, e.at); }
    if (e.type === "dismissed") dismissed++;
    if (e.type === "session-lost") ejections++;
    if (e.type === "session-restored") restored++;
    if (["sent", "started", "progress"].includes(e.type) && pending.has(e.tabId)) {
      recoverySec.push((e.at - pending.get(e.tabId)) / 1000);
      pending.delete(e.tabId);
    }
  }
  recoverySec.sort((a, b) => a - b);
  const n = recoverySec.length;
  return { warnings, dismissed, ejections, restored, samples: n, unconfirmed: pending.size,
    medianSec: n ? (recoverySec[Math.floor((n - 1) / 2)] + recoverySec[Math.floor(n / 2)]) / 2 : null,
    p90Sec: n ? recoverySec[Math.ceil(n * 0.9) - 1] : null };
}
