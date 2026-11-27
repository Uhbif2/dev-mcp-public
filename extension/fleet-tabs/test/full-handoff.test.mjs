// 21.11.2026: передача чата под надзором по факту «maximum length».
import test from "node:test";
import assert from "node:assert/strict";
import {
  FULL_HANDOFF_MAX_ATTEMPTS, continuationMarker, continuationText, fullHandoffStage, newFullHandoff,
  noteFailure, projectStartUrl, pruneFullHandoffs, transcriptFilename, transcriptMarkdown,
} from "../full-handoff.js";

test("new chat opens in the same ChatGPT project, otherwise a plain new chat", () => {
  assert.equal(projectStartUrl("https://chatgpt.com/g/g-p-6913abc-samplesec/c/00000001-0000-4000-8000-000000000001"),
    "https://chatgpt.com/g/g-p-6913abc-samplesec/project");
  assert.equal(projectStartUrl("https://chatgpt.com/c/00000001-0000-4000-8000-000000000001"), "https://chatgpt.com/");
  assert.equal(projectStartUrl("https://chatgpt.com/g/g-abc-custom-gpt/c/00000001"), "https://chatgpt.com/");
});

test("continuation text names the files, asks to dive in and continue, carries a marker", () => {
  const text = continuationText({ title: "Оркестрация ниш", oldUrl: "https://chatgpt.com/c/00000001-aaaa",
    oldId: "00000001-aaaa", files: ["a.json", "a.md"] });
  assert.match(text, /«a\.json» и «a\.md»/);
  assert.match(text, /Погрузись в контекст/);
  assert.match(text, /продолжи работу ровно с того места/);
  assert.ok(text.includes(continuationMarker("00000001-aaaa")));
  assert.equal(continuationMarker("00000001-AAAA"), "[fleet-continuation:00000001]");
});

test("transcript follows the current branch only and skips hidden/tool messages", () => {
  const conversation = {
    title: "T", current_node: "c",
    mapping: {
      root: { id: "root", parent: null, children: ["a"], message: null },
      a: { id: "a", parent: "root", children: ["b", "x"], message: { author: { role: "user" }, content: { parts: ["задание"] } } },
      x: { id: "x", parent: "a", children: [], message: { author: { role: "assistant" }, content: { parts: ["брошенная ветка"] } } },
      b: { id: "b", parent: "a", children: ["t"], message: { author: { role: "assistant" }, content: { parts: ["ответ"] } } },
      t: { id: "t", parent: "b", children: ["c"], message: { author: { role: "tool" }, content: { parts: ["вывод"] } } },
      c: { id: "c", parent: "t", children: [], message: { author: { role: "user" }, content: { parts: ["Продолжи"] },
        metadata: {} } },
    },
  };
  const md = transcriptMarkdown(conversation, { url: "https://chatgpt.com/c/x" });
  assert.match(md, /## 1\. Владелец\n\nзадание/);
  assert.match(md, /## 2\. Ассистент\n\nответ/);
  assert.match(md, /## 3\. Владелец\n\nПродолжи/);
  assert.ok(!md.includes("брошенная ветка") && !md.includes("вывод"));
  assert.equal(transcriptFilename("chatgpt-T-00000001-20261121T0800Z.json"), "chatgpt-T-00000001-20261121T0800Z.md");
});

test("stages run in order; draft mode stops prior to sending and keeps the old tab", () => {
  const rec = newFullHandoff({ oldId: "00000001", url: "u", tabId: 1, now: 0 });
  const order = [];
  for (const [field, value] of [["exportedAt", 1], ["newTabId", 2], ["attachedAt", 3], ["sentAt", 4],
                                ["successor", "new"], ["markedAt", 5], ["closedAt", 6]]) {
    order.push(fullHandoffStage(rec));
    rec[field] = value;
  }
  order.push(fullHandoffStage(rec));
  assert.deepEqual(order, ["export", "open", "attach", "send", "await-successor", "mark", "close-old", "finish"]);
  const draft = { ...newFullHandoff({ oldId: "x", url: "u", tabId: 1 }), exportedAt: 1, newTabId: 2, attachedAt: 3 };
  assert.equal(fullHandoffStage(draft, { draftOnly: true }), "draft");
  Object.assign(draft, { sentAt: 4, draft: true, successor: "s", markedAt: 5 });
  assert.equal(fullHandoffStage(draft, { draftOnly: true }), "finish");
});

test("failures back off and give up after the attempt limit", () => {
  const rec = newFullHandoff({ oldId: "x", url: "u", tabId: 1 });
  for (let i = 0; i < FULL_HANDOFF_MAX_ATTEMPTS; i++) noteFailure(rec, "attach", "нет поля", 1000);
  assert.equal(rec.failed, true);
  assert.equal(fullHandoffStage(rec), "failed");
  assert.match(rec.lastError, /^attach: нет поля/);
  assert.ok(rec.nextAt > 1000);
});

test("finished records are kept for a week, active ones always", () => {
  const now = 10 * 24 * 3600_000;
  const kept = pruneFullHandoffs({
    old: { done: true, finishedAt: 0 }, fresh: { done: true, finishedAt: now - 1000 }, active: { startedAt: 0 },
  }, now);
  assert.deepEqual(Object.keys(kept).sort(), ["active", "fresh"]);
});
