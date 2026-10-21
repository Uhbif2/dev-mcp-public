// Browser side of the explicit, fail-closed Research MCP transport.
import { callTool } from "./fleet.js";

const ENDPOINTS = Object.freeze({
  next: "fleet_research_next",
  finish: "fleet_research_finish",
});
const JOB_KEY = "fleetResearchJobs";
const ID = /^r-[0-9a-f]{24}$/;

async function send(tabId, message) {
  return Promise.race([
    chrome.tabs.sendMessage(tabId, message),
    new Promise((_, reject) => setTimeout(() => reject(new Error("research tab unresponsive")), 6000)),
  ]);
}

function valid(job) {
  return Boolean(job && ID.test(job.id) &&
    typeof job.question === "string" && job.question.length <= 8000 &&
    /^\d{4}-\d\d-\d\dT/.test(job.deadline_at || ""));
}

async function settle(url, item, outcome) {
  const status = outcome?.status === "complete" && typeof outcome.report === "string"
    ? "complete" : "unavailable";
  const response = await callTool(url, ENDPOINTS.finish, {
    id: item.id, status,
    report: status === "complete" ? outcome.report.slice(0, 200000) : "",
    reason: status === "unavailable" ? String(outcome?.reason || "research UI unavailable").slice(0, 1000) : "",
  }, { timeoutMs: 20000 });
  const result = JSON.parse(response);
  return Boolean(result && result.id === item.id &&
    ["completed", "unavailable"].includes(result.status));
}

// Called from the existing fleet tick, never starts a second independent timer.
export async function researchTick(url, { enabled = true, dryRun = false } = {}) {
  if (!enabled || dryRun) return;
  let pending;
  try {
    pending = JSON.parse(await callTool(url, ENDPOINTS.next, {}, { timeoutMs: 8000 }));
    if (!Array.isArray(pending) || pending.some((item) => !valid(item))) return;
  } catch {
    // Old server / missing capability: no speculative tabs or submissions.
    return;
  }
  const stored = await chrome.storage.local.get(JOB_KEY);
  const jobs = stored[JOB_KEY] || {};
  const active = new Set(pending.map((item) => item.id));
  for (const [id, job] of Object.entries(jobs)) {
    if (active.has(id)) continue;
    if (job.tabId) await chrome.tabs.remove(job.tabId).catch(() => {});
    delete jobs[id];
  }
  for (const item of pending) {
    try {
      if (Date.now() >= Date.parse(item.deadline_at)) continue; // server owns timeout
      let job = jobs[item.id];
      if (!job) {
        const tab = await chrome.tabs.create({ url: "https://chatgpt.com/", active: false });
        job = { tabId: tab.id, sent: false, createdAt: Date.now() };
        jobs[item.id] = job;
        await chrome.storage.local.set({ [JOB_KEY]: jobs });
      }
      const tab = await chrome.tabs.get(job.tabId).catch(() => null);
      if (!tab) {
        if (await settle(url, item, { status: "unavailable", reason: "research tab closed" })) {
          delete jobs[item.id];
        }
        continue;
      }
      // Chrome can defer a background renderer: probe for a bounded time,
      // without resending a question blindly if the send acknowledgement was lost.
      if (tab.status !== "complete") continue;
      const response = await send(tab.id, {
        type: job.sent ? "research-poll" : "research-start",
        request: { id: item.id, question: item.question },
      });
      if (!response || !["pending", "complete", "unavailable"].includes(response.status)) continue;
      if (!job.sent && response.status === "pending") job.sent = true;
      if (response.status === "complete" || response.status === "unavailable") {
        if (await settle(url, item, response)) {
          await chrome.tabs.remove(tab.id).catch(() => {});
          delete jobs[item.id];
        }
      }
    } catch {
      // Transient network/UI failure: retry next tick, server expires at 40m.
    }
  }
  await chrome.storage.local.set({ [JOB_KEY]: jobs });
}
