// Admission concerns visible ChatGPT interfaces, not the number of saved chats.
import { chatUrl } from './recovery.js';
export const INTERFACE_DEFAULTS = { maxTabs: 4, failedTabs: 2 };

export function interfacePlan(rows, workers, previous = {}, now = Date.now(), cfg = INTERFACE_DEFAULTS) {
  const active = new Set(workers.filter(w => w.task).map(w => w.label));
  const queue = Object.fromEntries(Object.entries(previous).filter(([label, entry]) =>
    active.has(label) && !/^(owner|pult)/i.test(label) && chatUrl(entry?.url) && Number.isFinite(entry?.at)));
  // A crash between tabs.create and acknowledgement must not strand a visible
  // conversation in the deferred queue forever.
  for (const row of rows) delete queue[row.label];
  const safe = rows.filter(r => r.own && r.label && !/^(owner|pult)/i.test(r.label) &&
    !(r.page?.unlockedFor > 0) && !(r.page && !r.page.composerEmpty && !r.page.composerOwned));
  const failures = safe.filter(r => r.page?.pageLoadFailed);
  const close = failures.slice(cfg.failedTabs);
  // Bring existing idle interfaces under the same cap as newly opened ones.
  // Generating tabs follow the keyboard's short, verified close delay instead.
  for (const row of safe) {
    if (rows.length - close.length <= cfg.maxTabs) break;
    if (!close.includes(row) && !failures.includes(row) && row.page &&
        !row.page.generating && row.url) close.push(row);
  }
  for (const row of close) {
    if (active.has(row.label) && row.url) queue[row.label] = {url:row.url,at:queue[row.label]?.at||now};
  }
  const visible = new Set(rows.map(r=>r.label));
  const next = Object.entries(queue).filter(([label])=>!visible.has(label))
    .sort((a,b)=>a[1].at-b[1].at)[0] || null;
  return { close:close.map(r=>r.id), queue, next,
    degraded:failures.length>0 || Object.keys(queue).length>0,
    failed:Math.min(failures.length,cfg.failedTabs) };
}
