import test from "node:test";
import assert from "node:assert/strict";
import { placeCheck, accountOverloaded, accountRecovered } from "../recovery.js";
import { decideLaunch, LAUNCH_DEFAULTS } from "../policy.js";

const PROJECT = "https://chatgpt.com/g/g-p-example-project/project";
const CHAT = "https://chatgpt.com/g/g-p-example-project/c/aaaaaaaa-bbbb-cccc-dddd-111111111111";
const session = { label: "f1", id: "aaaaaaaa-bbbb-cccc-dddd-111111111111", url: CHAT };

test("new chat starts only on the project page", () => {
  assert.equal(placeCheck("start", PROJECT, null, PROJECT).ok, true);
  const home = placeCheck("start", "https://chatgpt.com/", null, PROJECT);
  assert.equal(home.ok, false);
  assert.equal(home.navigate, PROJECT);
  const other = placeCheck("start", "https://chatgpt.com/g/g-p-0000aaaa-other/project", null, PROJECT);
  assert.equal(other.ok, false);
});

test("continue is sent only inside the tab's own chat", () => {
  assert.equal(placeCheck("continue", CHAT, session, PROJECT).ok, true);
  for (const url of ["https://chatgpt.com/", PROJECT, "https://chatgpt.com/c/ffffffff-0000-0000-0000-000000000000"]) {
    const r = placeCheck("continue", url, session, PROJECT);
    assert.equal(r.ok, false, url);
    assert.equal(r.navigate, CHAT);
  }
  assert.equal(placeCheck("click-retry", "https://chatgpt.com/", null, PROJECT).ok, false);
});

test("account overload pauses launches with growing pause and resets on success", () => {
  let launch = accountOverloaded({}, 0);
  assert.equal(launch.overloadUntil, 10 * 60_000);
  launch = accountOverloaded(launch, 1);
  assert.equal(launch.overloadUntil - 1, 20 * 60_000);
  for (let i = 0; i < 5; i++) launch = accountOverloaded(launch, 2);
  assert.equal(launch.overloadUntil - 2, 120 * 60_000);
  const cfg = { ...LAUNCH_DEFAULTS, projects: ["P"] };
  const paused = decideLaunch({ P: 3 }, [], [], launch, 3, cfg);
  assert.equal(paused.action, "skip");
  assert.match(paused.reason, /перегружен/);
  assert.equal(accountRecovered(launch).overloadUntil, 0);
});
