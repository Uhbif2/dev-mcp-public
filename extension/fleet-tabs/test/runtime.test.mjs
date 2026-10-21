import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import * as policy from '../policy.js';
import * as telemetry from '../telemetry.js';
import * as recovery from '../recovery.js';
import * as ownerNotices from '../owner-notices.js';
import * as interfacePressure from '../interface-pressure.js';
import { chatLines } from '../fleet.js';
import { INSTALL_DEFAULTS } from '../config.js';

const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').replace(/^import\s+[\s\S]*?from\s+"[^"]+";\s*/gm, '');
const tab = (id) => ({ id, url: `https://chatgpt.com/c/abcdefab-${id}`, pinned: true, autoDiscardable: false });
const state = (label, extra = {}) => ({ label, generating: false, idleSec: 700, stallSec: 0,
  composerPresent: true, composerEmpty: true, lastRole: 'assistant', rateLimited: '', blocked: '', url: 'https://chatgpt.com/c/abcdefab-1', ...extra });
const worker = (label, extra = {}) => ({ label, id: `W-${label}`, task: 'P/TZ-1', silenceMin: 5, ...extra });
const autostartLabel = (ageMin = 10) => {
  const d = new Date(Date.now() - ageMin * 60_000);
  const pad = (n) => String(n).padStart(2, '0');
  return `f${pad(d.getMonth()+1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}abc`;
};

function harness({ tabs = [tab(1)], states = { 1: state('f1') }, workers = [worker('f1')], saved = {}, outcome = {ok:true}, expectFail = false, createFail = false, actionGapSec = 0, wake = [], modeReply = {mode_status:'implicit-default'}, gateDown = false, reservationEmpty = false, fleetError = null, researchError = null } = {}) {
  const data = { settings: { paceVersion: 2, enabled: true, dryRun: false, autostart: false, lockTabs: false, projects: ['P'] }, ...structuredClone(saved) };
  if (data.settings && !data.settings.projectUrl) data.settings.projectUrl = 'https://chatgpt.com/g/fleet/project';
  const actions = [], removed = [], created = [], createdProps = [], updated = [], reloaded = [], toolCalls = [], launchers = [], reports = [];
  const listener = { addListener() {} };
  const chrome = {
    storage: { local: {
      async get(keys) { return Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])); },
      async set(patch) { Object.assign(data, structuredClone(patch)); },
    } },
    tabs: {
      async query() { return tabs; }, async get(id) { return tab(id); },
      async update(id, patch) { updated.push({id, ...patch}); }, async reload(id) { reloaded.push(id); },
      async remove(id) { removed.push(id); },
      async create(props = {}) { if (createFail) throw Error('create failed'); createdProps.push(props); if (props.url === 'about:blank') return {id: 101}; const t = tab(100); created.push(t); return t; },
      async sendMessage(id, msg) {
        if (msg.type === 'probe') {
          if (states[id] instanceof Error) throw states[id];
          return structuredClone(states[id] || state('new'));
        }
        if (msg.type === 'lock') return {locked:false};
        actions.push({id, ...msg});
        if (msg.action === 'dismiss-rate-limit') { states[id].rateLimited = ''; return {ok:true}; }
        return outcome;
      },
    },
    scripting: { async executeScript() {} },
    system: { memory: { async getInfo() { return {availableCapacity: 2e9}; } } },
    notifications: { create() {}, onClicked:listener }, action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    runtime: { onInstalled: listener, onStartup: listener, onMessage: listener },
    alarms: { async get() { return {periodInMinutes:0.5}; }, create() {}, onAlarm:listener },
  };
  const context = vm.createContext({ ...policy, ...telemetry, ...recovery, ...interfacePressure,
    installOwnerNoticeClicks:(report)=>ownerNotices.installOwnerNoticeClicks(report,chrome),
    deliverOwnerNotices:(items,report)=>ownerNotices.deliverOwnerNotices(items,report,chrome),
    recordServerAvailability:(up)=>ownerNotices.recordServerAvailability(up,chrome),
    RECOVERY_DEFAULTS: {...recovery.RECOVERY_DEFAULTS, accountActionGapSec:actionGapSec, settleSec:0},
    chrome, Date, Math, setTimeout, clearTimeout, INSTALL_DEFAULTS, localSettings: async () => ({}),
    researchTick: async () => { if (researchError) throw Error(researchError); },
    DEFAULT_MCP_URL: 'mock', chatLines, fleetState: async (_url, launcher) => { if (fleetError) throw Error(fleetError); launchers.push(structuredClone(launcher)); return {workers, projects:[{name:'P', count:8, next:{tz:'TZ-2',kind:'work'}}], attention:[], wake, launchGate:{known:true,state:gateDown?'down':'up',allowed:!gateDown}}; },
    reportExtension: async (_url, payload) => { reports.push(structuredClone(payload)); return "extension lease updated"; },
    callTool: async (_url, name, args) => {
      toolCalls.push({name, args: structuredClone(args)});
      if (name === 'fleet_block') return 'OK: claim frozen';
      if (name === 'fleet_task_body') return JSON.stringify(modeReply);
      if (name === 'fleet_launch_reserve') return JSON.stringify(reservationEmpty ? {status:'empty'} :
        {status:'reserved',label:args.label,worker_id:'W-new',project:args.project,tz:'TZ-real',claim_id:123});
      if (name === 'fleet_launch_release') return JSON.stringify({status:'released'});
      return 'OK';
    },
    expectProject: async () => { if (expectFail) throw Error('expect failed'); },
  });
  vm.runInContext(source, context);
  return {data, actions, removed, created, createdProps, updated, reloaded, toolCalls, launchers, reports, async tick() { await vm.runInContext('tick("test")', context); } };
}

test('failed fleet query records completion and error without claiming a fresh snapshot', async () => {
  const h=harness({fleetError:'signal timed out',saved:{lastTickAt:1}});
  await h.tick();
  assert.equal(h.data.lastTickAt,1);
  assert.equal(h.data.fleetUpdatedAt,undefined);
  assert.equal(h.data.fleetRefresh.error,'signal timed out');
  assert.ok(h.data.fleetRefresh.finishedAt);
  assert.match(h.data.tickError,/Сервер флота не ответил/);
});

test('fleet API failure cannot disable the two-tab recovery lane or create new chats',async()=>{
  const now=Date.now(),tabs=[1,2,3,4,5].map(tab),states={},ownTabs={},workers=[];
  for(const t of tabs){const label='f'+t.id;ownTabs[t.id]=label;workers.push(worker(label));
    states[t.id]=state(label,{url:t.url,pageLoadFailed:'failed'});}
  const h=harness({tabs,states,workers,fleetError:'API down',actionGapSec:15,
    saved:{ownTabs,fleetWorkers:workers,reloadMem:{1:{since:now-5000},2:{since:now-5000}},
      settings:{enabled:true,autostart:true,projects:['P']},lastTickAt:1}});
  await h.tick();
  assert.deepEqual(h.removed,[3,4,5]);
  assert.deepEqual(h.reloaded,[1]);
  assert.equal(Object.keys(h.data.interfaceQueue).length,3);
  assert.equal(h.created.length,0);
  assert.equal(h.actions.some(a=>a.action==='continue'),false);
  assert.equal(h.data.lastTickAt,1,'local healing is not a successful fleet tick');
});
test('completed fleet snapshot survives a failure later in the tick', async () => {
  const h=harness({researchError:'research transport failed',saved:{lastTickAt:1}});
  await h.tick();
  assert.ok(h.data.fleetUpdatedAt>1);
  assert.equal(h.data.fleetProjects[0].count,8);
  assert.equal(h.data.fleetRefresh.error,'');
  assert.match(h.data.tickError,/research transport failed/);
});

test('DOWN blocks launch, wake-open and pending first message, but existing agent continues', async () => {
  const existing = harness({
    gateDown: true, wake: [
      { label: 'f1', url: 'https://chatgpt.com/c/abcdefab-1', text: 'resume' },
      { label: 'old', url: 'https://chatgpt.com/c/abcdefab-9', text: 'resume' },
    ],
    saved: { settings: {paceVersion:2,enabled:true,dryRun:false,autostart:true,projects:['P']} },
  });
  await existing.tick();
  assert.deepEqual(existing.created, [], 'no wake-open or new-agent tab while DOWN');
  assert.ok(existing.actions.some(x => x.action === 'continue' && x.text === 'resume'),
    'already-opened conversation must still receive wake text');
  assert.equal(existing.reports.length, 1, 'successful tick has a separate heartbeat');
  assert.equal(existing.reports[0].enabled, true);
  assert.equal(existing.reports[0].tick_completed, true);
  assert.equal(existing.reports[0].runnable, 8);
  assert.equal(existing.reports[0].detail, 'composer ok');

  const pending = harness({
    gateDown: true, states: { 1: state('f1', {url:'https://chatgpt.com/g/fleet/project',sessionReady:false}) },
    workers: [], saved: {
      ownTabs: {1:'f1'},
      launch: {chatHistoryVersion:2,history:[],launched:{
        f1:{at:Date.now(),project:'P',tabId:1,needsStart:true,nextStartAt:0},
      }},
    },
  });
  await pending.tick();
  assert.equal(pending.actions.some(x => x.action === 'start'), false,
    'pending project landing must not create the first conversation');
  assert.equal(pending.data.launch.launched.f1.needsStart, true);
  assert.match(pending.data.status['1'].reason, /недоступен/);
});

test('three persistent Cloudflare composer failures report evidence; composer success resets', async () => {
  const h = harness({
    gateDown: true,
    states: {1: state('f1', {composerPresent:false,composerEmpty:true,
      cloudflarePageText:'Unable to load site — check VPN',
      url:'https://chatgpt.com/c/abcdefab-1'})},
  });
  for (let i = 0; i < 3; i += 1) await h.tick();
  assert.equal(h.reports.length, 3);
  assert.ok(h.reports.every(x => x.detail.includes('не найдено поле ввода') &&
    x.page_text.includes('Unable to load site')));
  h.data.settings.enabled = true;
  // Successful composer observations send an explicit reset, not an empty heartbeat.
  const healthy = harness({states:{1:state('f1')}, gateDown:true});
  await healthy.tick();
  assert.equal(healthy.reports[0].detail, 'composer ok');
  assert.equal(healthy.reports[0].page_text, '');
});

test('verified server mode and SHA are carried into the send; stale or hostile mode waits', async () => {
  const sha='a'.repeat(64);
  const verified={mode_status:'verified',mode:'thinking',mode_explicit:true,
    mode_contract:'fleet-chat-mode/v1',mode_spec_sha:sha,outcome_stage:'понято'};
  const h=harness({modeReply:verified});
  await h.tick();
  const sent=h.actions.find(x=>x.action==='continue');
  assert.ok(sent);
  assert.equal(sent.mode,'thinking');
  assert.equal(sent.outcomeStage,'понято');
  assert.equal(sent.modeContract,'fleet-chat-mode/v1');
  assert.equal(sent.modeSpecSha,sha);
  assert.equal(h.toolCalls.some(x=>x.name==='fleet_task_body'),true);

  for (const unsafe of [{mode_status:'stale'}, {...verified,mode:'thinking;alert(1)'}]) {
    const blocked=harness({modeReply:unsafe});
    await blocked.tick();
    assert.equal(blocked.actions.some(x=>x.action==='continue'),false);
    assert.equal(blocked.data.status['1'].action,'wait');
  }
});

test('warning is acknowledged and both this tab and another continue; old global pause is removed', async () => {
  const h = harness({ tabs:[tab(1),tab(2)], states:{1:state('f1',{rateLimited:'Слишком много запросов'}),2:state('f2')},
    workers:[worker('f1'),worker('f2')], saved:{launch:{cooldownUntil:Date.now()+3600_000,chatCeiling:3}} });
  await h.tick();
  assert.deepEqual(h.actions.map(a=>[a.id,a.action]), [[1,'dismiss-rate-limit'],[1,'continue'],[2,'continue']]);
  assert.equal(h.data.launch.cooldownUntil, undefined);
  assert.equal(h.data.telemetrySummary.samples, 1);
});

test('fruitless chat releases claim through dev-mcp instead of waiting for stale recovery', async () => {
  const h=harness({
    states:{1:state('f1')},
    workers:[worker('f1')],
    saved:{mem:{1:{...policy.emptyMemory(),task:'P/TZ-1',fruitless:policy.DEFAULTS.maxFruitless}}},
  });
  await h.tick();
  const finish=h.toolCalls.find(x=>x.name==='fleet_finish');
  assert.ok(finish,'fleet_finish was not called');
  assert.equal(finish.args.agent,'W-f1');
  assert.match(finish.args.summary,/^handoff:/);
  assert.equal(h.data.status['1'].action,'release');
  assert.deepEqual(h.actions,[],'no extra chat message should be sent after auto-handoff');
});

test('second manual-permission wait freezes claim through dev-mcp instead of spamming continue', async () => {
  const h=harness({
    states:{1:state('f1',{blockerWaiting:'Жду владельца: требуется ручное одобрение'})},
    workers:[worker('f1')],
    saved:{mem:{1:{...policy.emptyMemory(),task:'P/TZ-1',blockerNudges:1}}},
  });
  await h.tick();
  const block=h.toolCalls.find(x=>x.name==='fleet_block');
  assert.ok(block,'fleet_block was not called');
  assert.equal(block.args.agent,'W-f1');
  assert.match(block.args.reason,/^blocked:/);
  assert.equal(h.data.status['1'].action,'block');
  assert.deepEqual(h.actions,[],'no chat message should be sent on second permission wait');
});

test('full conversation load failure uses shared paced navigation lane', async () => {
  const now=Date.now();
  const h=harness({
    actionGapSec:90,
    states:{1:state('f1',{composerPresent:false,pageLoadFailed:'Не удалось загрузить этот разговор ChatGPT'})},
    saved:{
      reloadMem:{1:{since:now-5000,lastAt:0}},
      accountActions:{lastActionAt:now,history:[{at:now,type:'reload',tabId:99,label:'other'}]},
    },
  });
  await h.tick();
  assert.deepEqual(h.reloaded,[]);
  assert.equal(h.data.accountActions.lastActionAt,now);
  assert.match(h.data.status['1'].reason,/пауза/);
});

test('failed page reload survives exhausted hourly history and pending session restore',async()=>{
  const now=Date.now();
  const h=harness({states:{1:state('f1',{composerPresent:false,pageLoadFailed:'Не удалось загрузить этот разговор ChatGPT'})},
    saved:{reloadMem:{1:{since:now-5000}},launch:{reloads:Array(24).fill(now-60000)},
      sessions:{1:{id:'abcdefab-1',url:'https://chatgpt.com/c/abcdefab-1',label:'f1'}},
      recoveryMem:{1:{since:now-20000,nextAt:now-1000,attempts:1}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[1]);
  assert.equal(h.data.launch.reloads.length,24,'failed-page lane must not erase normal reload history');
  assert.equal(h.actions.some(x=>x.action==='continue'),false);
});

test('mass failure keeps two and queues closed conversations without creating new chats',async()=>{
  const tabs=[1,2,3,4,5].map(tab), states={},ownTabs={},workers=[];
  for(const t of tabs){const label='f'+t.id;ownTabs[t.id]=label;workers.push(worker(label));
    states[t.id]=state(label,{url:t.url,composerPresent:false,pageLoadFailed:'Не удалось загрузить этот разговор ChatGPT'});}
  const h=harness({tabs,states,workers,saved:{ownTabs,settings:{enabled:true,dryRun:false,autostart:true,projects:['P']}}});
  await h.tick();
  assert.deepEqual(h.removed,[3,4,5]);
  assert.equal(Object.keys(h.data.interfaceQueue).length,3);
  assert.equal(h.created.length,0);
});

test('two failed tabs alternate the shared navigation lane across ticks',async()=>{
  const now=Date.now();
  const h=harness({tabs:[tab(1),tab(2)],workers:[worker('f1'),worker('f2')],actionGapSec:15,
    states:{1:state('f1',{pageLoadFailed:'failed'}),2:state('f2',{pageLoadFailed:'failed'})},
    saved:{reloadMem:{1:{since:now-5000},2:{since:now-5000}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[1]);
  h.data.accountActions.lastActionAt=0;
  h.data.reloadMem[1].lastAt=Date.now()-20000;
  await h.tick();
  assert.deepEqual(h.reloaded,[1,2],'least recently retried failed tab wins');
});

test('page load failure during confirmation hold never falls through to continue', async () => {
  const h=harness({
    states:{1:state('f1',{composerPresent:false,pageLoadFailed:'Не удалось загрузить этот разговор ChatGPT'})},
  });
  await h.tick();
  assert.deepEqual(h.reloaded,[]);
  assert.equal(h.actions.some(x=>x.action==='continue'),false);
  assert.equal(h.data.status['1'].action,'skip');
  assert.match(h.data.status['1'].reason,/жду подтверждения/);
});

test('live chat with vanished composer reloads instead of sending blind continue', async () => {
  const now=Date.now();
  const h=harness({
    states:{1:state('f1',{composerPresent:false,pageLoadFailed:'',failed:'',url:'https://chatgpt.com/c/abcdefab-1'})},
    saved:{reloadMem:{1:{since:now-9000,lastAt:0}}},
  });
  await h.tick();
  assert.deepEqual(h.reloaded,[1]);
  assert.equal(h.actions.some(x=>x.action==='continue'),false);
  assert.equal(h.data.status['1'].action,'reload');
});

test('project redirect is returned to the original session before any continue is sent', async () => {
  const original = 'https://chatgpt.com/g/fleet/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  const h = harness({
    states:{1:state('f1',{url:'https://chatgpt.com/g/fleet/project',sessionReady:false,rateLimited:'Too Many requests',rateLimitKind:'toast'})},
    saved:{sessions:{1:{label:'f1',id:'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',url:original,capturedAt:1}}},
  });
  await h.tick();
  assert.deepEqual(h.updated.filter(x=>x.url), [{id:1,url:original}]);
  assert.equal(h.actions.some(x=>x.action==='continue'),false);
  assert.equal(h.data.recoveryMem['1'].attempts,1);
  assert.equal(h.data.events.filter(e=>e.type==='session-lost').length,1);
  assert.equal(h.data.events.filter(e=>e.type==='session-restore-attempt').length,1);
});

test('lost-session restore no longer blocks an ordinary continuation in another tab', async () => {
  const original='https://chatgpt.com/g/fleet/c/bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee';
  const h=harness({
    tabs:[tab(1),tab(2)], actionGapSec:90,
    states:{1:state('f1'),2:state('f2',{url:'https://chatgpt.com/g/fleet/project',sessionReady:false})},
    workers:[worker('f1'),worker('f2')],
    saved:{sessions:{2:{label:'f2',id:'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee',url:original,capturedAt:1}}},
  });
  await h.tick();
  assert.deepEqual(h.updated.filter(x=>x.url),[{id:2,url:original}]);
  assert.equal(h.actions.some(x=>x.action==='continue'),true);
});

test('unconfirmed send preserves continuation budget and sets retry delay', async () => {
  const h = harness({outcome:{ok:false,detail:'not sent'}});
  await h.tick();
  assert.equal(h.data.mem['1'].continues, undefined);
  assert.ok(h.data.mem['1'].deliveryRetryAt > Date.now());
  await h.tick();
  assert.equal(h.actions.length, 1);
  assert.equal(h.data.launch.continuesSent, undefined);
});

test('MCP rejection and create failure do not spend launch budget', async () => {
  for (const fail of [{expectFail:true},{createFail:true}]) {
    const h=harness({...fail, tabs:[], workers:[], saved:{settings:{paceVersion:2,enabled:true,dryRun:false,autostart:true,projects:['P']},launch:{history:[],lastLaunchAt:0}}});
    await h.tick();
    assert.equal(h.data.launch.lastLaunchAt,0);
    assert.deepEqual(h.data.launch.history,[]);
    assert.equal(h.created.length,0);
  }
});

test('stale demand with no server claim never creates an agent chat', async () => {
  const h = harness({ tabs:[], workers:[], reservationEmpty:true,
    saved:{settings:{paceVersion:9,enabled:true,dryRun:false,autostart:true,projects:['P']}} });
  await h.tick();
  assert.equal(h.created.length,0);
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_launch_reserve'));
  assert.equal(h.actions.some(x=>x.action==='start'),false);
});

test('reusable idle tab takes next work before closing', async () => {
  const h = harness({ states:{1:state('f0923-1200abc')}, workers:[worker('f0923-1200abc',{task:null,taken:1,budgetLeft:3,silenceMin:15})] });
  await h.tick();
  assert.equal(h.removed.length,0);
  assert.equal(h.actions[0]?.action,'continue');
  assert.match(h.actions[0].text,/следующую/);
});

test('hard block stops only its own tab; owner dialog is untouched', async () => {
  const h=harness({tabs:[tab(1),tab(2)], states:{1:state('owner-one',{rateLimited:'Слишком много запросов'}),2:state('f2',{blocked:'Вы достигли лимита'})},workers:[worker('f2')]});
  await h.tick();
  assert.equal(h.actions.length,0);
  assert.equal(h.data.launch.cooldownUntil,undefined);
});

test('telemetry counts episodes once and does not call dismissal a successful request', () => {
  const one=telemetry.observeWarning(null,'modal',1000);
  assert.equal(telemetry.observeWarning(one.state,'modal',2000).fresh,false);
  assert.equal(telemetry.observeWarning(one.state,'',3000).state,null);
  const events=[{type:'warning',at:1000,tabId:1},{type:'dismissed',at:2000,tabId:1},
    {type:'sent',at:3000,tabId:2},{type:'send-failed',at:4000,tabId:1}];
  assert.equal(telemetry.summarizeEvents(events).samples,0);
  events.push({type:'sent',at:61000,tabId:1});
  assert.equal(telemetry.summarizeEvents(events).medianSec,60);
});

test('pending starts consume seats and reserve only the selected project', () => {
  const cfg={...policy.LAUNCH_DEFAULTS,projects:['P'],maxWorking:2,maxPending:8};
  const queue=[{name:'P',count:5,next:{tz:'X',kind:'work'}}];
  const now=Date.now();
  const launched={a:{at:now,project:'P'},b:{at:now,project:'P'}};
  assert.match(policy.decideLaunch(queue,[],[],{launched},now,cfg).reason,/рабочие места/);
  assert.equal(policy.decideLaunch(queue,[],[],{launched:{a:launched.a}},now,cfg).action,'launch');
});

test('join without first take keeps the demand reservation and does not double count a seat', () => {
  const now=Date.now();
  const cfg={...policy.LAUNCH_DEFAULTS,projects:['P'],maxWorking:2};
  const launched={a:{at:now,project:'P'}};
  const joined=[worker('a',{task:null,taken:0,budgetLeft:5})];
  const queue=[{name:'P',count:1,next:{tz:'X',kind:'work'}}];
  assert.equal(policy.decideLaunch(queue,['a'],joined,{launched},now,cfg).action,'skip');
  assert.equal(policy.decideLaunch([{...queue[0],count:2}],['a'],joined,{launched},now,cfg).action,'launch');
});

test('pending startup and hung generation never overwrite a draft', () => {
  const s=state('f1',{own:true,pendingJoin:true,composerEmpty:false,lastRole:'user',failed:'Network error',failedSec:500});
  assert.equal(policy.decide(s,undefined,{},Date.now()).action,'skip');
  assert.equal(policy.reloadPlan({...s,generating:true,stallSec:1000},null,[],Date.now()).reload,false);
});

test('created tab and reservation survive startup even before join', async () => {
  const h=harness({tabs:[],workers:[],saved:{settings:{paceVersion:2,enabled:true,dryRun:false,autostart:true,projects:['P']}}});
  await h.tick();
  assert.equal(h.created.length,1);
  assert.equal(h.data.launch.history.length,0, 'project-вкладка ещё не является новым чатом');
  assert.equal(h.data.launch.chatHistoryVersion,2);
  assert.equal(Object.keys(h.data.launch.launched).length,1);
  assert.ok(h.data.ownTabs['100']);
  assert.equal(h.data.events.filter(e=>e.type==='created').length,1);
  assert.equal(h.data.events.filter(e=>e.type==='started').length,0);
  assert.equal(Object.values(h.data.launch.launched)[0].needsStart,true);
});

test('persisted pending start is sent on a later tick and captures the new session URL', async () => {
  const url='https://chatgpt.com/g/fleet/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  const h=harness({
    states:{1:state('f1',{url:'https://chatgpt.com/g/fleet/project',sessionReady:false})}, workers:[],
    outcome:{ok:true,detail:'sent',url},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,launched:{f1:{at:Date.now(),project:'P',tabId:1,needsStart:true,nextStartAt:0}},history:[],lastLaunchAt:0}},
  });
  await h.tick();
  assert.equal(h.actions[0]?.action,'start');
  assert.equal(h.data.launch.launched.f1.needsStart,false);
  assert.equal(h.data.sessions['1'].url,url);
  assert.equal(h.data.events.filter(e=>e.type==='started').length,1);
  assert.equal(h.data.launch.history.length,1);
  assert.ok(h.data.launch.launched.f1.countedChatAt);
});

test('failed first message is not counted as a confirmed chat in telemetry', async () => {
  const h=harness({
    states:{1:state('f1',{url:'https://chatgpt.com/g/fleet/project',sessionReady:false})}, workers:[],
    outcome:{ok:false,detail:'composer changed'},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,launched:{f1:{at:Date.now(),project:'P',tabId:1,needsStart:true,nextStartAt:0}},history:[],lastLaunchAt:0}},
  });
  await h.tick();
  assert.equal(h.data.launch.history.length,0);
  assert.equal(h.data.launch.launched.f1.startAttempts,1);
});

test('account overload reports 1/3/5-hour confirmed-chat counts and pause to server', async () => {
  const now=Date.now();
  const h=harness({
    states:{1:state('f1',{accountLoadFailed:'Не удалось загрузить аккаунт'})},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,launched:{},
      history:[now-30*60_000,now-2*3600_000,now-4*3600_000],overloads:[]}},
  });
  await h.tick();
  const message=JSON.parse(h.reports.find(r=>r.account_event).account_event);
  assert.equal(message.type,'overload');
  assert.deepEqual([message.chats_1h,message.chats_3h,message.chats_5h],[1,2,3]);
  assert.equal(message.pause_min,10);
  assert.equal(h.data.launch.accountEvents.length,0,'confirmed event removed from retry queue');
  assert.equal(h.data.launch.overloadLevel,1);
});

test('first confirmed chat after overload reports account recovery once', async () => {
  const overloadAt=Date.now()-30*60_000;
  const h=harness({
    states:{1:state('f1',{url:'https://chatgpt.com/g/fleet/project',sessionReady:false})},
    workers:[],outcome:{ok:true,detail:'sent'},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,history:[],
      overloads:[overloadAt],overloadLevel:1,overloadUntil:Date.now()-1,
      launched:{f1:{at:Date.now(),project:'P',tabId:1,needsStart:true,nextStartAt:0}}}},
  });
  await h.tick();
  const message=JSON.parse(h.reports[0].account_event);
  assert.equal(message.type,'recovered');
  assert.equal(message.overload_at,overloadAt);
  assert.ok(message.at>=overloadAt);
  assert.equal(h.data.launch.overloadLevel,0);
  assert.equal(h.data.launch.accountEvents.length,0);
});

test('stale unregistered autostart tab is forgotten and replaced', async () => {
  const label=autostartLabel(10);
  const h=harness({
    states:{1:new Error('вкладка не ответила за 5 с')}, workers:[],
    saved:{ownTabs:{1:label},launch:{chatHistoryVersion:2,history:[],launched:{}},
      settings:{paceVersion:2,enabled:true,dryRun:false,autostart:true,projects:['P'],maxWorking:1,spareTabs:0}},
  });
  await h.tick();
  assert.deepEqual(h.removed,[1]);
  assert.equal(h.data.ownTabs['1'],undefined);
  assert.equal(h.created.length,1, 'освобождённый слот можно сразу заменить');
});

test('frozen known agent tab is reloaded automatically', async () => {
  const frozen={...tab(1),frozen:true};
  const h=harness({
    tabs:[frozen],states:{1:new Error('вкладка не ответила за 5 с')},workers:[worker('f1')],
    saved:{ownTabs:{1:'f1'},settings:{paceVersion:7,enabled:true,dryRun:false,autostart:false,projects:['P']}},
  });
  await h.tick();
  assert.deepEqual(h.reloaded,[1]);
  assert.ok(h.updated.some(x=>x.id===1 && x.autoDiscardable===false));
  assert.match(h.data.status['1'].reason,/заморожена Chrome/);
});

test('registered active unresponsive tab gets recovery reload instead of occupying a dead slot forever', async () => {
  const label=autostartLabel(1);
  const h=harness({
    states:{1:new Error('вкладка не ответила за 5 с')},workers:[worker(label)],
    saved:{ownTabs:{1:label},reloadMem:{1:{unresponsiveSince:Date.now()-15_000}},
      settings:{paceVersion:8,enabled:true,dryRun:false,autostart:false,projects:['P']}},
  });
  await h.tick();
  assert.deepEqual(h.reloaded,[1]);
  assert.equal(h.data.status['1'].action,'reload');
  assert.match(h.data.status['1'].reason,/content-script/);
});

test('registered idle unresponsive tab is closed after closeIdle and frees a launch slot', async () => {
  const idleSec=policy.LAUNCH_DEFAULTS.closeIdleSec+60;
  const label=autostartLabel(Math.ceil(idleSec/60)+1);
  const h=harness({
    states:{1:new Error('вкладка не ответила за 5 с')},
    workers:[worker(label,{task:null,taken:1,budgetLeft:3,silenceMin:Math.ceil(idleSec/60)+1})],
    saved:{ownTabs:{1:label},reloadMem:{1:{unresponsiveSince:Date.now()-idleSec*1000}},
      settings:{paceVersion:8,enabled:true,dryRun:false,autostart:true,projects:['P'],maxWorking:1,spareTabs:0},
      launch:{chatHistoryVersion:2,history:[],lastLaunchAt:0}},
  });
  await h.tick();
  assert.deepEqual(h.removed,[1]);
  assert.equal(h.data.ownTabs['1'],undefined);
  assert.equal(h.created.length,1,'закрытый idle-tab должен сразу освободить место новой работе: ' + JSON.stringify(h.data.launchStatus || h.data.launchNote || h.data.status));
});

test('fresh tool progress releases a fruitless stop, but not the task continuation cap', () => {
  const now=Date.now();
  const mem={task:'P/TZ-1',stopped:'6 повторов сорвавшегося хода подряд без прогресса',lastRetryAt:now-600_000,retries:6};
  const plan=policy.decide(state('f1'),worker('f1',{silenceMin:0}),mem,now);
  assert.equal(plan.mem.stopped,'');
  assert.equal(plan.action,'continue');
  assert.equal(policy.decide(state('f1'),worker('f1',{silenceMin:0}),{...mem,stopped:'потолок 40 «продолжи» на задачу'},now).action,'skip');
});

test('server wake order: open chat gets the server text before any ordinary continue', async () => {
  const text = "Продолжай P/TZ-1 — твой ход прервался. Вызови fleet_resume(worker_id='W-f1').";
  const h = harness({ wake: [{ label: 'f1', url: 'https://chatgpt.com/c/abcdefab-1', text }] });
  await h.tick();
  assert.deepEqual(h.actions.map(a => [a.id, a.action, a.text]), [[1, 'continue', text]]);
  assert.equal(h.data.status['1'].action, 'wake');
  assert.deepEqual(h.data.wakeMem, {}, 'delivered order is forgotten');
  assert.match(h.data.knownChats.f1.url, /abcdefab-1$/, 'chat address is remembered for the server');
  await h.tick();
  assert.ok(h.launchers.at(-1).chats.split('\n').includes('f1 https://chatgpt.com/c/abcdefab-1'), 'next fleet_list carries chat addresses');
});

test('server wake order for a closed chat opens it and waits for the page', async () => {
  const h = harness({ tabs: [], states: {}, workers: [worker('f9')],
    wake: [{ label: 'f9', url: 'https://chatgpt.com/c/abcdefab-100', text: 'Продолжай' }] });
  const wakeOpens = () => (h.data.events || []).filter((e) => e.type === 'wake-open').length;
  await h.tick();
  assert.equal(wakeOpens(), 1);
  assert.equal(h.data.wakeMem['100'].label, 'f9');
  assert.equal(h.data.wakeMem['100'].text, 'Продолжай');
  await h.tick(); // тот же приказ снова — второй вкладки для этого чата не будет
  assert.equal(wakeOpens(), 1);
});

test('closing the last tab of a window opens a blank sibling so Chrome itself stays open', async () => {
  const label=autostartLabel(10);
  const h=harness({
    tabs:[{...tab(1), windowId:7}],
    states:{1:new Error('вкладка не ответила за 5 с')}, workers:[],
    saved:{ownTabs:{1:label},launch:{chatHistoryVersion:2,history:[],launched:{}},
      settings:{paceVersion:2,enabled:true,dryRun:false,autostart:true,projects:['P'],maxWorking:1,spareTabs:0}},
  });
  await h.tick();
  assert.deepEqual(h.removed,[1]);
  assert.deepEqual(JSON.parse(JSON.stringify(h.createdProps[0])),{windowId:7,url:'about:blank',active:false});
});
