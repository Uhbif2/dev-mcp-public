import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../research-content.js", import.meta.url), "utf8");
const background = readFileSync(new URL("../research-background.js", import.meta.url), "utf8")
  .replace(/^import\s+[^\n]+\n/gm, "").replace("export async function researchTick", "async function researchTick");
const request = { id: "r-" + "a".repeat(24), question: "Research recent findings." };

function harness({ mode = null, planButton = null, report = null } = {}) {
  let listener = null;
  let clock = 10_000;
  const storage = new Map();
  const sourceLink = (href) => ({
    href, innerText: "source", textContent: "source",
  });
  const assistant = report && {
    innerText: report,
    getClientRects: () => [1],
    getAttribute: () => null,
    querySelectorAll: (sel) =>
      sel.startsWith("a[") ? [sourceLink("https://example.com/a"), sourceLink("https://example.org/b")]
        : sel.includes("h1") ? [{ textContent: "Results" }] : [],
  };
  const document = {
    querySelector(selector) {
      if (selector.includes("[role='alertdialog']")) return { innerText: "" };
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "main button,[role='dialog'] button") return planButton ? [planButton] : [];
      if (selector.includes("[data-message-author-role='assistant']")) return assistant ? [assistant] : [];
      return [];
    },
  };
  class FakeDate extends Date { static now() { return clock; } }
  const globals = {
    document, chrome: { runtime: { onMessage: { addListener(fn) { listener = fn; } } } },
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
    getComputedStyle: () => ({ visibility: "visible" }),
    Date: FakeDate,
    InputEvent: class InputEvent {}, Event: class Event {},
  };
  globals.globalThis = globals;
  if (mode) globals.FleetChatModes = mode;
  vm.runInNewContext(source, globals);
  function dispatch(message) {
    return new Promise((resolve) => {
      const async = listener(message, null, resolve);
      if (async === false && message.type !== "research-start") {
        // Listener called reply synchronously.
      }
    });
  }
  return { dispatch, storage, advance(ms) { clock += ms; } };
}

test("fail closed when research mode is not available", async () => {
  const browser = harness();
  const result = await browser.dispatch({ type: "research-start", request });
  assert.equal(result.status, "unavailable");
  assert.match(result.reason, /mode adapter unavailable/);
});

test("only an explicit unique plan confirmation is clicked", async () => {
  let clicks = 0;
  const button = {
    getClientRects: () => [1],
    getAttribute: () => null,
    innerText: "Start research",
    click: () => { clicks++; },
    disabled: false,
  };
  const browser = harness({ planButton: button });
  browser.storage.set("fleet-research:" + request.id, "sent");
  const answer = await browser.dispatch({ type: "research-poll", request });
  assert.equal(answer.status, "pending");
  assert.equal(clicks, 1);
  await browser.dispatch({ type: "research-poll", request });
  assert.equal(clicks, 1);
});

test("report requires stable content, sources and headings", async () => {
  const report = "# Results\n" + "Verified detail and explanation. ".repeat(35);
  const browser = harness({ report });
  browser.storage.set("fleet-research:" + request.id, "sent");
  const first = await browser.dispatch({ type: "research-poll", request });
  assert.equal(first.status, "pending");
  browser.advance(16_000);
  const second = await browser.dispatch({ type: "research-poll", request });
  assert.equal(second.status, "complete");
  assert.match(second.report, /example\.com\/a/);
  assert.match(second.report, /Source links/);
});

test("background uses the MCP bridge, reuses one tab and removes it after delivery", async () => {
  let reported = false;
  const jobs = {};
  const calls = [];
  const removed = [];
  let sendCount = 0;
  const pending = [{ ...request, project: "Demo",
    requested_at: new Date().toISOString(), deadline_at: new Date(Date.now() + 120000).toISOString() }];
  const chrome = {
    storage: { local: {
      get: async () => ({ fleetResearchJobs: structuredClone(jobs.fleetResearchJobs || {}) }),
      set: async (patch) => { Object.assign(jobs, structuredClone(patch)); },
    } },
    tabs: {
      create: async () => ({ id: 91 }),
      get: async () => ({ id: 91, status: "complete" }),
      remove: async (id) => { removed.push(id); },
      sendMessage: async (_id, msg) => {
        sendCount++;
        return msg.type === "research-start" ? { status: "pending" }
          : { status: "complete", report: "Final report with sources" };
      },
    },
  };
  const callTool = async (_url, name, args) => {
    calls.push({ name, args });
    if (name === "fleet_research_next") return JSON.stringify(reported ? [] : pending);
    reported = true;
    return JSON.stringify({ id: request.id, status: "completed" });
  };
  const context = vm.createContext({ chrome, callTool, Date, setTimeout, clearTimeout });
  vm.runInContext(background, context);
  await vm.runInContext('researchTick("mock")', context);
  assert.equal(jobs.fleetResearchJobs[request.id].tabId, 91);
  assert.equal(jobs.fleetResearchJobs[request.id].sent, true);
  await vm.runInContext('researchTick("mock")', context);
  assert.equal(sendCount, 2);
  assert.equal(calls.filter((v) => v.name === "fleet_research_finish").length, 1);
  assert.deepEqual(removed, [91]);
  assert.equal(Object.keys(jobs.fleetResearchJobs).length, 0);
});

test("missing bridge cannot create a tab", async () => {
  let created = 0;
  const chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    tabs: { create: async () => { created++; return { id: 12 }; } },
  };
  const context = vm.createContext({
    chrome, Date, setTimeout, clearTimeout,
    callTool: async () => { throw new Error("bridge not deployed"); },
  });
  vm.runInContext(background, context);
  await vm.runInContext('researchTick("mock")', context);
  assert.equal(created, 0);
});
