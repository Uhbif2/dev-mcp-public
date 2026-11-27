// Physical memory guard shared by ordinary Fleet and pult-q agents.
// Only extension-owned, inactive tabs with a confirmed persisted chat URL may be
// discarded. A draft, pending send/wake, model turn or unconfirmed new chat is protected.
const QUICK = /^pult-q\d+(?:-[0-9a-z]+)?$/i;
const REGULAR = /^f\d{4}-\d{4}[a-z0-9]{3}$/;

export const MEMORY_GUARD_DEFAULTS = Object.freeze({
  minFreeMemMB: 250, // ниже наблюдаемого фонового остатка ОЗУ 450–600 МБ
});

function chatIdentity(url) {
  try {
    const u = new URL(url);
    if (u.origin !== "https://chatgpt.com" || u.username || u.password) return "";
    return /^\/(?:g\/[^/]+\/)?c\/([0-9a-f-]{8,})\/?$/i.exec(u.pathname)?.[1] || "";
  } catch { return ""; }
}

function ownedLabel(tab, owned = {}, bindings = {}) {
  const key = String(tab.id);
  const owner = owned[key] || "";
  const bound = bindings[key]?.label || "";
  // A supervised owner tab can contain an old f*-worker label in its DOM.
  // Never let that binding turn an owner-sv tab into a memory-shed candidate.
  if (owner === "owner-sv" || bound === "owner-sv") return "";
  // pult-q alone is not proof of ownership: the human may use that label too.
  if (QUICK.test(owner)) return owner;
  return REGULAR.test(owner) ? owner :
    REGULAR.test(bound) ? bound : "";
}

// Persisted, metadata-only signal for a discarded supervised conversation.
// A changed update_time must be stable over two polls before a heavy UI reload.
// An unknown baseline also requires confirmation: it may hide an unobserved turn.
export function overseerRevisionPlan(record = {}, updateTime, now = Date.now(), stableMs = 30_000) {
  const value = typeof updateTime === "string" || typeof updateTime === "number"
    ? String(updateTime) : "";
  if (!value || value === "0") return { state: { ...record, checkedAt: now }, reload: false };
  if (value === String(record.value || "")) {
    return { state: { value, checkedAt: now }, reload: false };
  }
  const candidateAt = String(record.candidate || "") === value
    ? Number(record.candidateAt || now) : now;
  const state = { ...record, candidate: value, candidateAt, checkedAt: now };
  return { state, reload: now - candidateAt >= stableMs };
}

export function liveAgentTabs(tabs, owned = {}, bindings = {}) {
  return (tabs || []).filter(t => !t.discarded && t.status !== "unloaded" &&
    Boolean(ownedLabel(t, owned, bindings)));
}

export function memoryGuardPlan({
  tabs = [], owned = {}, bindings = {}, pages = new Map(), sessions = {},
  workers = [], quickJobs = [], launch = {}, wake = {},
  freeMemMB = null, minFreeMemMB = MEMORY_GUARD_DEFAULTS.minFreeMemMB,
} = {}) {
  const alive = liveAgentTabs(tabs, owned, bindings);
  const underPressure = Number.isFinite(freeMemMB) && freeMemMB < minFreeMemMB;
  const jobs = new Map(quickJobs.map(j => [j.label, j]));
  const agents = new Map(workers.map(w => [w.label, w]));
  const candidates = [];
  for (const tab of alive) {
    const key = String(tab.id), label = ownedLabel(tab, owned, bindings);
    const page = typeof pages?.get === "function" ? pages.get(tab.id) : pages[key];
    // Chrome refuses to discard the active tab. Never discard an active user draft.
    if (tab.active || !page || page.generating || page.composerEmpty !== true ||
        page.composerOwned || page.blocked || page.rateLimited || page.blockerWaiting ||
        page.pendingJoin || page.failed || page.pageLoadFailed || page.accountLoadFailed ||
        page.interrupted || page.unlockedFor > 0) continue;
    const quick = QUICK.test(label);
    const job = quick ? jobs.get(label) : null;
    const worker = agents.get(label);
    if (quick && (job?.send || job?.wake || job?.pending > 0 ||
        (job?.open === true && job?.status === "new"))) continue;
    if (!quick && (wake[key] || launch[label]?.needsStart)) continue;
    // Never discard a project landing before the first /c/ URL is recorded.
    const destination = quick ? job?.url || sessions[key]?.url || page.url || tab.url :
      sessions[key]?.url || page.url || tab.url;
    if (!chatIdentity(destination)) continue;
    const idle = quick
      ? Boolean(job && (job.status === "parked" || job.idle === true ||
          job.status === "answered" || job.open === false))
      : Boolean(worker && !worker.task);
    const answered = page.lastRole === "assistant";
    if (!idle && !answered) continue;
    const priority = idle ? 0 : 1;
    candidates.push({ id: tab.id, label, priority, idle, answered });
  }
  candidates.sort((a, b) => a.priority - b.priority || a.id - b.id);
  return {
    freeMemMB: Number.isFinite(freeMemMB) ? Math.round(freeMemMB) : null,
    liveTabs: alive.length, thresholdMB: minFreeMemMB, underPressure,
    discard: underPressure ? candidates : [],
    eligible: candidates.length,
    reason: underPressure
      ? `мало ОЗУ: свободно ${Math.round(freeMemMB)} МБ < ${minFreeMemMB} МБ; безопасные вкладки выгружаются`
      : "",
  };
}
