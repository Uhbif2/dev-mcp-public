import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// composerKey из content.js как есть (без DOM): сверка «текст встал в поле».
const src = readFileSync(new URL("../content.js", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const pick = (name) => {
  const start = src.indexOf(`const ${name} = `);
  const end = src.indexOf(";\n", start);
  return src.slice(start, end + 1);
};
const composerKey = new Function(`${pick("normalizeComposer")}\n${pick("composerKey")}\nreturn composerKey;`)();

test("нумерованный текст совпадает с textContent ProseMirror (абзацы без \\n)", () => {
  const sent = "Продолжай W7-3.\n1. CLAIM-WORKTREES.md: маршрут.\n2. Перечень проектов.\n10) Ещё пункт.";
  // ProseMirror: каждая строка — <p>, textContent склеивает их без переводов строк.
  const fieldText = "Продолжай W7-3.1. CLAIM-WORKTREES.md: маршрут.2. Перечень проектов.10) Ещё пункт.";
  assert.equal(composerKey(fieldText), composerKey(sent));
  // innerText (с \n) тоже совпадает; редактор, превративший строки в <ol>, — тоже.
  assert.equal(composerKey(sent.replace(/\n/g, "\n")), composerKey(sent));
  assert.equal(composerKey("Продолжай W7-3.CLAIM-WORKTREES.md: маршрут.Перечень проектов.Ещё пункт."),
    composerKey(sent));
});

const block = (name) => {
  const start = src.indexOf(`const ${name} = `);
  return src.slice(start, src.indexOf("\n  };", start) + 5);
};
const confirmsUserMessage = new Function(
  `${pick("normalizeComposer")}\n${pick("composerKey")}\n${pick("comparableUserText")}\n${block("confirmsUserMessage")}\nreturn confirmsUserMessage;`)();

test("отрендеренный markdown нашего задания подтверждает отправку", () => {
  const sent = "Задача W7-6: правило.\n1. Класс **b** только если `accept` позже submit.\n2. Нет spec_path — класс e.";
  const shown = "You said: @dev-mcp Задача W7-6: правило. Класс b только если accept позже submit. Нет spec_path — класс e.";
  assert.equal(confirmsUserMessage(shown, sent), true);
  assert.equal(confirmsUserMessage("You said: совсем другое сообщение владельца", sent), false);
});

test("чужой текст по-прежнему не совпадает", () => {
  assert.notEqual(composerKey("1. другой текст"), composerKey("1. наш текст"));
  assert.notEqual(composerKey("версия 2.5 вышла"), composerKey("версия 25 вышла").replace("25", "2"));
});
