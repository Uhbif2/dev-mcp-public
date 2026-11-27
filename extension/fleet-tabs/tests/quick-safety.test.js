import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

// Exercise the exact pure helpers used by the MV3 worker and the legacy VM harness.
const background = fs.readFileSync(new URL("../background.js", import.meta.url), "utf8");
const start = background.indexOf("// QUICK_SAFETY_START");
const end = background.indexOf("// QUICK_SAFETY_END");
assert.ok(start >= 0 && end > start);
const {
  quickTabsToClose, quickSendFingerprint, quickSendBaseline, confirmedQuickSend,
} = vm.runInNewContext(background.slice(start, end) +
  "\n({quickTabsToClose, quickSendFingerprint, quickSendBaseline, confirmedQuickSend})");

const chatA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const chatB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const chatPath = (url) => /\/c\/([0-9a-f-]{8,})/i.exec(url || "")?.[1] || "";
const job = { id: "Q4", label: "pult-q4", url:  `https://chatgpt.com/c/${chatA}`  };

test("parked quick close skips an owner's tab with the identical chat URL", () => {
  const tabs = [
    { id: 1, url: job.url }, // owner tab: never marked by extension
    { id: 2, url: job.url }, // extension-owned
    { id: 3, url: job.url }, // explicitly marked by extension
    { id: 4, url: job.url }, // another quick job
    { id: 5, url:  `https://chatgpt.com/c/${chatB}`  }, // navigated away
    { id: 6, url: job.url }, // already removed
    { id: 7, url: "https://chatgpt.com/g/project" }, // owned but not yet a chat
    { id: 8, url: job.url }, // owner tab inferred by labels.byChat, not extension-owned
  ];
  const ownTabs = { 2: job.label, 4: job.label, 5: job.label, 7: job.label };
  const labelsByTab = { 3: { label: job.label, path: chatA },
    8: { label: job.label, path: chatA } };
  const tabJobs = { 2: job.id, 3: job.id, 4: "Q5" };
  const selected = quickTabsToClose(tabs, job, {
    ownTabs, labelsByTab, tabJobs, closed: new Set([6]), chatPath,
  });
  assert.deepEqual(selected.map((t) => t.id), [2, 3, 7]);
  assert.equal(tabs[0].url, tabs[1].url, "owner URL identical to owned URL");
});

test("owner URL cannot give ownership even without a job URL", () => {
  const tabs = [{ id: 11, url: "https://chatgpt.com/" }];
  assert.deepEqual(quickTabsToClose(tabs, { ...job, url: "" }, { chatPath }), []);
});

test("quick send confirms only new user turns since pre-click baseline", () => {
  const command = "Repeat this exact request";
  const fingerprint = quickSendFingerprint(command);
  const before = { ok: true, nMessages: 8,
    userMessages: ["opening", command, "other command"] };
  const baseline = quickSendBaseline(before, fingerprint);
  assert.deepEqual({ ...baseline }, { fingerprint, messageCount: 8, userCount: 3 });

  assert.equal(confirmedQuickSend(before, command, null), false,
    "never confirm before a click/baseline");
  assert.equal(confirmedQuickSend(before, command, baseline), false,
    "old identical message never confirms a new attempt");
  assert.equal(confirmedQuickSend({ ...before, nMessages: 9 }, command, baseline), false,
    "a new assistant message cannot confirm an old user turn");
  assert.equal(confirmedQuickSend({ ok: true, nMessages: 10,
    userMessages: [...before.userMessages, "Unrelated new user message"] },
  command, baseline), false, "only newly arrived user text can confirm");
  assert.equal(confirmedQuickSend({ ok: true, nMessages: 10,
    userMessages: [...before.userMessages, command] }, command, baseline), true);
});

test("repeated identical say must get a fresh baseline after earlier acknowledgement", () => {
  const command = "status";
  const fp = quickSendFingerprint(command);
  const previousDelivered = { ok: true, nMessages: 12,
    userMessages: ["first", command] };
  const nextAttempt = quickSendBaseline(previousDelivered, fp);
  assert.equal(confirmedQuickSend(previousDelivered, command, nextAttempt), false);
  assert.equal(confirmedQuickSend({ ...previousDelivered, nMessages: 13,
    userMessages: [...previousDelivered.userMessages, command] }, command, nextAttempt), true);
});

test("different pending command, unavailable transcript and 80-user cap fail closed", () => {
  const original = "same first 140".repeat(20) + " A";
  const changed = "same first 140".repeat(20) + " B";
  const before = { ok: true, nMessages: 90, userMessages: ["first", original] };
  const baseline = quickSendBaseline(before, quickSendFingerprint(original));
  assert.equal(confirmedQuickSend({ ...before, nMessages: 91,
    userMessages: [...before.userMessages, original] }, changed, baseline), false);
  assert.equal(confirmedQuickSend({ ...before, nMessages: 91 }, original, baseline), false);
  assert.equal(confirmedQuickSend({ ok: false, nMessages: 91,
    userMessages: [...before.userMessages, original] }, original, baseline), false);
  assert.equal(quickSendBaseline({ ok: false }, quickSendFingerprint(original)), null);

  const capped = Array.from({ length: 80 }, (_, i) =>  `user ${i}` );
  const cappedBaseline = quickSendBaseline({ ok: true, nMessages: 130,
    userMessages: capped }, quickSendFingerprint(original));
  assert.equal(confirmedQuickSend({ ok: true, nMessages: 132,
    userMessages: capped }, original, cappedBaseline), false,
    "content.js exposes only first 80 user turns: never guess new content");
});

test("rendered markdown in a long chat confirms only a new matching user turn", () => {
  const text = "Follow the **numbered** instructions", fp = quickSendFingerprint(text);
  const firstPage = Array(80).fill("old");
  const baseline = quickSendBaseline({ok:true, nMessages:202, userCount:101, userMessages:firstPage}, fp);
  const next = {ok:true, nMessages:204, userCount:102, userMessages:firstPage, matchingUserIndex:101};
  assert.equal(confirmedQuickSend(next, text, baseline), true);
  assert.equal(confirmedQuickSend({...next, matchingUserIndex:100}, text, baseline), false);
  assert.equal(confirmedQuickSend(next, text, {...baseline, fullCount:false}), false);
});

test("after Chrome restart: agent mark in first message + same chat proves ownership", () => {
  const chatPath = (u) => (/\/c\/([0-9a-f-]+)/.exec(u || "") || [])[1] || "";
  const job = { id: "q8-651428", label: "pult-q8-651428", url: "https://chatgpt.com/c/abc-1" };
  const tabs = [
    { id: 1, url: "https://chatgpt.com/g/p/c/abc-1" },  // ours, lost ownTabs after restart
    { id: 2, url: "https://chatgpt.com/c/abc-1" },      // human tab of the same chat, no mark
    { id: 3, url: "https://chatgpt.com/c/other" },      // marked but another chat
  ];
  const marks = { 1: "Q8-651428", 2: "", 3: "Q8-651428" };
  const out = quickTabsToClose(tabs, job, { chatPath, marks });
  assert.deepEqual(out.map((t) => t.id), [1]);
});
