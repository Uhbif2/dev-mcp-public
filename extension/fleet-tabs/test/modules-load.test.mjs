// Каждый модуль service worker обязан загружаться: 14.10.2026 два принятых claim
// объявили parseAgentStates дважды — SyntaxError, расширение мертво, флот без агентов.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";

const stub = () => new Proxy(function () {}, {
  get: (t, k) => (k === "addListener" ? () => {} : stub()),
  apply: () => undefined,
});
globalThis.chrome = stub();

const dir = new URL("../", import.meta.url);
for (const name of readdirSync(dir).filter((f) => f.endsWith(".js"))) {
  test(`module loads: ${name}`, async () => {
    try {
      await import(new URL(name, dir));
    } catch (err) {
      assert.ok(!(err instanceof SyntaxError), `${name}: ${err.message}`);
    }
  });
}
