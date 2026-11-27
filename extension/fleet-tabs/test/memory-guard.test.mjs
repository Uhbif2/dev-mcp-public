import test from "node:test";
import assert from "node:assert/strict";
import { memoryGuardPlan, liveAgentTabs, MEMORY_GUARD_DEFAULTS } from "../memory-guard.js";

const url = (n) => `https://chatgpt.com/c/abcdefab-${n}`;
const regular = "f1116-0935abc";
const tab = (id, extra = {}) => ({ id, url: url(id), active: false, discarded: false, ...extra });
const page = (id, label, extra = {}) => ({
  label, url: url(id), generating: false, composerEmpty: true, lastRole: "assistant",
  composerPresent: true, idleSec: 45, ...extra,
});

test("memory guard: threshold only, no tab or agent count limit", () => {
  assert.deepEqual(MEMORY_GUARD_DEFAULTS, { minFreeMemMB: 250 });
});

test("159 MB: discard safely answered quick and regular; protect human, active, generating, draft, pending send", () => {
  const tabs = Array.from({length: 8}, (_, i) => tab(i + 1));
  const owned = { 1: "pult-q1", 2: regular, 3: "pult-q3", 4: "pult-q4",
    5: "pult-q5", 6: "pult-q6", 7: "pult-q7" }; // tab 8 is a human owner tab
  tabs[2].active = true;
  const pages = new Map(tabs.map((t, i) =>
    [t.id, page(t.id, owned[t.id] || "owner-console", i === 3 ? {generating:true} :
      i === 4 ? {composerEmpty:false} : {})]));
  const quickJobs = [
    { label: "pult-q1", idle:true, status:"answered" },
    { label: "pult-q3", idle:true }, { label: "pult-q4", idle:true },
    { label: "pult-q5", idle:true },
    { label: "pult-q6", send:"pending request", open:true },
    { label: "pult-q7", idle:true },
  ];
  const plan = memoryGuardPlan({ tabs, owned, pages,
    workers:[{label:regular,task:null}], quickJobs, freeMemMB:159 });
  assert.equal(plan.liveTabs,7);
  assert.equal(plan.underPressure,true);
   assert.match(plan.reason,/159 МБ/);
  assert.deepEqual(plan.discard.map(r=>r.id),[1,2,7]);
  assert.equal(plan.discard.some(r=>[3,4,5,6,8].includes(r.id)),false);
  assert.equal(liveAgentTabs(tabs,owned).length,7);
});

test("owner-sv is never a memory-discard candidate even at 159 MB", () => {
  const tabs=[tab(1),tab(2)];
  const owned={1:"owner-sv",2:"pult-q2"};
  const pages=new Map([[1,page(1,"owner-sv")],[2,page(2,"pult-q2")]]);
  const plan=memoryGuardPlan({tabs,owned,pages,freeMemMB:159,
    quickJobs:[{label:"pult-q2",idle:true,status:"answered",open:false}]});
  assert.deepEqual(plan.discard.map(v=>v.id),[2]);
  assert.equal(plan.liveTabs,1);
});

test("at 500 MB no discard even with 9 or 40 live agent tabs", () => {
  const tabs = Array.from({length:40},(_,i)=>tab(i+1));
  const owned = Object.fromEntries(tabs.map(t=>[t.id,`pult-q${t.id}`]));
  const pages = new Map(tabs.map(t=>[t.id,page(t.id,owned[t.id])]));
  const quickJobs=tabs.map(t=>({label:owned[t.id],idle:true,open:false}));
  for (const count of [8, 9, 40]) {
    const plan=memoryGuardPlan({tabs:tabs.slice(0,count),owned,pages,quickJobs,freeMemMB:500});
    assert.equal(plan.liveTabs,count);
    assert.deepEqual(plan.discard,[]);
    assert.equal(plan.underPressure,false);
    assert.equal('limit' in plan,false);
  }
});

test("discarded tabs not counted; project landing and unknown chat never discarded", () => {
  const tabs=[tab(1,{discarded:true}),tab(2,{url:"https://chatgpt.com/g/owner/project"}),
    tab(3),tab(4)];
  const owned={1:"pult-q1",2:"pult-q2",3:"pult-q3"};
  const pages=new Map([
    [2,page(2,"pult-q2",{url:tabs[1].url})],
    [3,page(3,"pult-q3")],
    [4,page(4,"owner-person")],
  ]);
  const plan=memoryGuardPlan({tabs,owned,pages,freeMemMB:159,quickJobs:[
    {label:"pult-q2",idle:true}, {label:"pult-q3",idle:true,send:"new order"}
  ]});
  assert.equal(plan.liveTabs,2);
  assert.deepEqual(plan.discard,[]);
});
