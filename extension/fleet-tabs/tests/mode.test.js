import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

await import("../mode.js");
const modes = globalThis.FleetChatModes;
assert.ok(modes);

class Control {
  constructor(name, selected = null, callback = null, unsafe = false) {
    this.textContent = name;
    this.attrs = selected == null ? {} : { "aria-pressed": selected ? "true" : "false" };
    this.callback = callback;
    this.clicks = 0;
    this.unsafe = unsafe;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
  getClientRects() { return [1]; }
  closest() { return this.unsafe ? {} : null; }
  click() { this.clicks += 1; this.callback?.(); }
}
class Surface {
  constructor(elements) { this.elements = elements; }
  querySelectorAll() { return this.elements; }
}
const noWait = async () => {};

test("mode metadata is exact and never interpreted as code", () => {
  for (const mode of ["default", "thinking", "deep-research"]) {
    assert.equal(modes.normalizeMode(mode), mode);
  }
  for (const value of ["Think", "deep_research", "alert(1)", "default;delete()", null, {}, 1]) {
    assert.throws(() => modes.normalizeMode(value));
  }
});

test("only outcome понято prefers observed Research, never other stages", () => {
  const capability = { available: { "deep-research": true } };
  assert.equal(modes.resolveMode("default", "понято", capability), "deep-research");
  assert.equal(modes.resolveMode("default", "проверено", capability), "default");
  assert.equal(modes.resolveMode("thinking", "понято", capability), "thinking");
  assert.equal(modes.resolveMode("default", "понято", { available: {} }), "default");
});

test("already-selected mode makes no second click", async () => {
  const think = new Control("Think", true);
  const selected = await modes.ensureMode("thinking", { root: new Surface([think]), sleep: noWait });
  assert.equal(selected.ok, true);
  assert.equal(selected.evidence.adapterVersion, modes.VERSION);
  assert.equal(think.clicks, 0);
});

test("switch verifies UI state after the only mode click", async () => {
  const instant = new Control("Instant", true);
  const think = new Control("Think", false, () => {
    instant.attrs["aria-pressed"] = "false";
    think.attrs["aria-pressed"] = "true";
  });
  const outcome = await modes.ensureMode("thinking", { root: new Surface([instant, think]), sleep: noWait });
  assert.equal(outcome.ok, true);
  assert.equal(outcome.evidence.active, "thinking");
  assert.equal(think.clicks, 1);
});

test("DOM change and missing Deep Research fail closed", async () => {
  const empty = new Surface([new Control("Mystery mode")]);
  const noResearch = await modes.ensureMode("deep-research", { root: empty, sleep: noWait });
  assert.equal(noResearch.ok, false);
  assert.equal(noResearch.wait, true);
  const think = new Control("Think", false, () => {});
  const noActiveWitness = await modes.ensureMode("thinking", { root: new Surface([think]), sleep: noWait });
  assert.equal(noActiveWitness.ok, false);
  assert.equal(think.clicks, 1);
});

test("ambiguous menu candidates cannot be clicked", async () => {
  const first = new Control("Deep research", false);
  const second = new Control("Deep research", false);
  const result = await modes.ensureMode("deep-research", { root: new Surface([first, second]), sleep: noWait });
  assert.equal(result.wait, true);
  assert.equal(first.clicks + second.clicks, 0);
});

test("spoof mode buttons in chat messages, navigation or editable drafts are not capability evidence", async () => {
  const fakeThink = new Control("Think", true, null, true);
  const fakeResearch = new Control("Deep research", true, null, true);
  const fakeTools = new Control("Tools", null, null, true);
  const result = await modes.ensureMode("thinking", {
    root: new Surface([fakeThink, fakeResearch, fakeTools]), sleep: noWait,
  });
  assert.equal(result.wait, true);
  assert.equal(result.evidence.active, null);
  assert.equal(fakeThink.clicks + fakeResearch.clicks + fakeTools.clicks, 0);
});

test("second mode action is idempotent after a verified switch", async () => {
  const instant = new Control("Instant", true);
  const think = new Control("Think", false, () => {
    instant.attrs["aria-pressed"] = "false";
    think.attrs["aria-pressed"] = "true";
  });
  const root = new Surface([instant, think]);
  assert.equal((await modes.ensureMode("thinking", { root, sleep: noWait })).ok, true);
  assert.equal((await modes.ensureMode("thinking", { root, sleep: noWait })).ok, true);
  assert.equal(think.clicks, 1);
});

test("Tools menu is inspected, not assumed; Research requires active witness", async () => {
  const surface = new Surface([]);
  const research = new Control("Deep research", false);
  const tools = new Control("Tools", null, () => { surface.elements.push(research); });
  surface.elements.push(tools);
  research.callback = () => { research.attrs["aria-pressed"] = "true"; };
  const chosen = await modes.ensureMode("deep-research", { root: surface, sleep: noWait });
  assert.equal(chosen.ok, true);
  assert.equal(tools.clicks, 1);
  assert.equal(research.clicks, 1);
  const repeat = await modes.ensureMode("deep-research", { root: surface, sleep: noWait });
  assert.equal(repeat.ok, true);
  assert.equal(research.clicks, 1, "repeat delivery must not toggle a selected mode");
});

test("понято discovers Research inside closed Tools menu and checks selection", async () => {
  const instant = new Control("Instant", true);
  const research = new Control("Deep research", false);
  const surface = new Surface([]);
  const tools = new Control("Tools", null, () => { surface.elements.push(research); });
  surface.elements.push(instant, tools);
  research.callback = () => {
    research.attrs["aria-pressed"] = "true";
    instant.attrs["aria-pressed"] = "false";
  };
  const result = await modes.ensureMode("default", {
    root: surface, stage: "понято", sleep: noWait,
  });
  assert.equal(result.ok, true);
  assert.equal(result.effective, "deep-research");
  assert.equal(research.clicks, 1);
  assert.equal(instant.clicks, 0);
});

const chromeBinary = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
test("headless Chrome: forged DOM ignored and concurrent mode sends deliver once",
  { skip: !fs.existsSync(chromeBinary) }, async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fleet-chat-mode-"));
    for (const name of ["mode.js", "content.js"]) {
      fs.copyFileSync(new URL("../" + name, import.meta.url), path.join(dir, name));
    }
    const html = `<!doctype html><meta charset="utf-8">
      <nav><button aria-pressed="true">Deep research</button></nav>
      <main>
        <article data-message-author-role="assistant"><button aria-pressed="true">Think</button></article>
        <form><button type="button" id="instant" aria-pressed="true">Instant</button>
        <button type="button" id="think" aria-pressed="false">Think</button>
        <textarea id="prompt-textarea"></textarea>
        <button type="button" id="composer-submit-button">Send</button></form>
      </main>
      <pre id="result">pending</pre>
      <script>
      const listeners = [];
      window.chrome = {runtime:{id:"test",onMessage:{addListener(fn){listeners.push(fn);}}}};
      </script>
      <script src="mode.js"></script><script src="content.js"></script><script>
      const call = (message) => new Promise(resolve => listeners[0](message, {}, resolve));
      let sends = 0, modeClicks = 0;
      document.getElementById("think").onclick = () => {
        modeClicks++;
        document.getElementById("instant").setAttribute("aria-pressed", "false");
        document.getElementById("think").setAttribute("aria-pressed", "true");
      };
      document.getElementById("composer-submit-button").onclick = () => {
        sends++;
        const payload = document.getElementById("prompt-textarea").value;
        setTimeout(() => {
          document.getElementById("prompt-textarea").value = "";
          const posted = document.createElement("div");
          posted.dataset.messageAuthorRole = "user";
          posted.textContent = payload;
          document.querySelector("main").append(posted);
        }, 200);
      };
      (async () => {
        const before = FleetChatModes.probe(document);
        const args = {type:"act",action:"continue",mode:"thinking",
          modeContract:"fleet-chat-mode/v1",modeSpecSha:"a".repeat(64),
          text:"single delivery"};
        const first = call(args);
        const duplicate = call(args);
        const [a, b] = await Promise.all([first, duplicate]);
        document.getElementById("result").textContent = JSON.stringify({
          before:before.active, spoofIgnored:!before.ambiguous,
          firstOk:a.ok, duplicateWait:b.wait && !b.ok,
          sends, modeClicks, adapter:a.modeEvidence?.adapterVersion,
          contract:a.modeEvidence?.contract, specSha:a.modeEvidence?.specSha,
          delivered:[...document.querySelectorAll("[data-message-author-role='user']")]
            .filter(node => node.textContent==="single delivery").length,
        });
      })().catch(error => {
        document.getElementById("result").textContent=JSON.stringify({error:String(error)});
      });
      </script>`;
    const file = path.join(dir, "fixture.html");
    fs.writeFileSync(file, html);
    const { stdout } = await promisify(execFile)(chromeBinary, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--disable-background-networking",
      "--no-default-browser-check", "--allow-file-access-from-files",
      `--user-data-dir=${path.join(dir, "profile")}`,
      "--dump-dom", "--virtual-time-budget=18000", pathToFileURL(file).href,
    ], { timeout: 45000, maxBuffer: 2e6, windowsHide: true });
    const raw = /<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
    assert.ok(raw && raw !== "pending", "headless fixture did not finish");
    const observed = JSON.parse(raw.replaceAll("&quot;", '"').replaceAll("&amp;", "&"));
    assert.deepEqual(observed, {
      before: "default", spoofIgnored: true, firstOk: true, duplicateWait: true,
      sends: 1, modeClicks: 1, adapter: modes.VERSION, contract: "fleet-chat-mode/v1",
      specSha: "a".repeat(64), delivered: 1,
    });
  });
