import test from "node:test";
import assert from "node:assert/strict";

import {
  RECOVERY_DEFAULTS,
  accountActionWait,
  pacedAccountActionWait,
  ACCOUNT_ACTIONS_PER_HOUR,
  attemptedRecovery,
  chatUrl,
  legacySession,
  markAccountAction,
  observeSession,
  projectPage,
  rememberSession,
  accountOverloaded,
} from "../recovery.js";

const session = {
  id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  url: "https://chatgpt.com/g/fleet/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  label: "f0923-1200abc",
};
const page = (over = {}) => ({
  url: session.url, label: session.label, ready: true, rateLimited: "", blocked: "",
  loading: false, responded: true, composerEmpty: true, generating: false, unlockedFor: 0,
  ...over,
});

test("session URL is exact and is never replaced by a redirected or accidental chat", () => {
  assert.equal(chatUrl(session.url).id, session.id);
  assert.equal(chatUrl("https://evil.example/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"), null);
  assert.equal(projectPage("https://chatgpt.com/g/fleet/project"), true);
  assert.equal(projectPage(session.url), false);
  assert.equal(rememberSession(session, "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444", session.label, 2), session);
  assert.deepEqual(
    legacySession({ label: session.label, path: session.id }, "https://chatgpt.com/g/fleet/project"),
    session,
  );
});

test("project ejection restores forever with backoff and requires a stable original session", () => {
  const cfg = { ...RECOVERY_DEFAULTS, settleSec: 30 };
  const lost = observeSession(session, page({ url: "https://chatgpt.com/g/fleet/project", ready: false, rateLimited: "Too Many requests" }), null, 1_000, cfg);
  assert.equal(lost.lost, true);
  assert.equal(lost.canAttempt, true);
  const once = attemptedRecovery(lost.state, 1_000, cfg);
  assert.equal(once.attempts, 1);
  assert.equal(once.nextAt, 61_000);
  assert.equal(observeSession(session, page({ url: "https://chatgpt.com/g/fleet/project", ready: false }), once, 60_999, cfg).canAttempt, false);
  const again = observeSession(session, page({ url: "https://chatgpt.com/g/fleet/project", ready: false }), once, 61_000, cfg);
  assert.equal(again.canAttempt, true);
  assert.equal(attemptedRecovery(again.state, 61_000, cfg).nextAt, 181_000);

  const loading = observeSession(session, page(), once, 70_000, cfg);
  assert.equal(loading.settling, true);
  assert.equal(loading.restored, false);
  const stable = observeSession(session, page(), loading.state, 100_000, cfg);
  assert.equal(stable.restored, true);
});

test("recovery does not overwrite drafts, active generations, unlocked tabs or owner tabs", () => {
  const displaced = { url: "https://chatgpt.com/g/fleet/project", ready: false };
  assert.equal(observeSession(session, page({ ...displaced, composerEmpty: false }), null, 1).canAttempt, false);
  assert.equal(observeSession(session, page({ ...displaced, generating: true }), null, 1).canAttempt, false);
  assert.equal(observeSession(session, page({ ...displaced, unlockedFor: 60 }), null, 1).protected, true);
  assert.equal(observeSession(session, page({ ...displaced, label: "owner-main" }), null, 1).protected, true);
});

test("общий потолок защищает быстрые открытия, reload и отправки без придуманной квоты ChatGPT", () => {
  const now = 5_000_000;
  const cfg = { accountActionGapSec: 15 };
  let account = { history: [], lastActionAt: 0 };
  for (let i = 0; i < ACCOUNT_ACTIONS_PER_HOUR; i++) {
    account = markAccountAction(account, now - 1000, i % 3 === 0 ? "quick-open" :
      i % 3 === 1 ? "quick-send" : "reload", i, "pult-q1");
  }
  assert.equal(pacedAccountActionWait(account, now, cfg), 3600_000 - 1000);
  assert.equal(pacedAccountActionWait(account, now + 3600_000, cfg), 0);
  assert.equal(pacedAccountActionWait({ history: [], lastActionAt: now }, now + 1000, cfg), 14000);
});

test("all paced interface actions share one 15 second lane", () => {
  let account = markAccountAction({}, 1_000, "reload", 1, "f1");
  assert.equal(accountActionWait(account, 1_000), 15_000);
  assert.equal(accountActionWait(account, 15_999), 1);
  assert.equal(accountActionWait(account, 16_000), 0);
  account = markAccountAction(account, 61_000, "send-attempt", 2, "f2");
  assert.deepEqual(account.history.map((item) => item.type), ["reload", "send-attempt"]);
});

test("W11-7: overload level resets after twice the last pause with no further overload", () => {
  const now=10_000_000;
  const one=accountOverloaded({},now);
  assert.equal(one.overloadLevel,1);
  const two=accountOverloaded(one,now+60_000);
  assert.equal(two.overloadLevel,2);
  const after=accountOverloaded(two,now+60_000+40*60_000);
  assert.equal(after.overloadLevel,1);
  assert.equal(after.overloadUntil,now+41*60_000+10*60_000);
});

