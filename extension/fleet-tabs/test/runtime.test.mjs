import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import { createHash, webcrypto } from 'node:crypto';
import * as policy from '../policy.js';
import * as telemetry from '../telemetry.js';
import * as recovery from '../recovery.js';
import * as ownerNotices from '../owner-notices.js';
import * as interfacePressure from '../interface-pressure.js';
import * as memoryGuard from '../memory-guard.js';
import * as dispatchScope from '../dispatch-scope.js';
import * as liveness from '../liveness.js';
import * as fleetUiState from '../fleet-ui-state.js';
import * as chatHandoffPolicy from '../chat-handoff-policy.js';
import * as limitReload from '../limit-reload.js';
import * as sessionExport from '../session-export.js';
import * as fullHandoff from '../full-handoff.js';
import * as overseerTabs from '../overseer-tabs.js';
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

function harness({ exportPage = null, tabs = [tab(1)], states = { 1: state('f1') }, workers = [worker('f1')], saved = {}, outcome = {ok:true}, expectFail = false, createFail = false, actionGapSec = 0, wake = [], quickJobs: initialQuickJobs = [], serverAutostart: initialAutostart = true, rejectBrowserReadback = false, modeReply = {mode_status:'implicit-default'}, gateDown = false, reservationEmpty = false, fleetError = null, researchError = null, stalledInjection = false, storageWriteError = null, transcript = null, memoryFreeMB = null, overseen = [], conversationTimes = {}, apiStatuses = {}, controlStatus = null, windowState = null, debuggerStub = null, handoffReply = null } = {}) {
  const data = { settings: { paceVersion: 2, enabled: true, dryRun: false, autostart: false, lockTabs: false, projects: ['P'] }, ...structuredClone(saved) };
  if (data.settings && !data.settings.projectUrl) data.settings.projectUrl = 'https://chatgpt.com/g/fleet/project';
  const actions = [], removed = [], created = [], createdProps = [], updated = [], reloaded = [], discarded = [], toolCalls = [], launchers = [], reports = [];
  let quickJobs = initialQuickJobs, quickTranscript = transcript, serverAutostart = initialAutostart, memoryFreeBytes = memoryFreeMB == null ? 2e9 : memoryFreeMB * 1048576, runtimeMessage;
  const apiTimes = {...conversationTimes}, apiReads = [];
  const listener = { addListener() {} };
  const chrome = {
    storage: { local: {
      async get(keys) { return Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])); },
      async set(patch) { if (storageWriteError) throw Error(storageWriteError); Object.assign(data, structuredClone(patch)); },
    } },
    tabs: {
      async query() { return tabs; }, async get(id) { return tab(id); },
      async update(id, patch) { updated.push({id, ...patch}); const t=tabs.find(t=>t.id===id); if(t)Object.assign(t,patch); },
      async reload(id) { reloaded.push(id); const t=tabs.find(t=>t.id===id); if(t)t.discarded=false; },
      async discard(id) { const t=tabs.find(t=>t.id===id); if(!t || t.active)return undefined; t.discarded=true; discarded.push(id); memoryFreeBytes+=100*1048576; return {...t}; },
      async remove(id) { removed.push(id); },
      async create(props = {}) { if (createFail) throw Error('create failed'); createdProps.push(props); if (props.url === 'about:blank') return {id: 101}; const t = tab(100); created.push(t); return t; },
      async sendMessage(id, msg) {
        if (msg.type === 'probe') {
          if (states[id] instanceof Error) throw states[id];
          return structuredClone(states[id] || state('new'));
        }
        if (msg.type === 'limit-probe') { const s = states[id]; return s instanceof Error ? null : {temporaryLimit: s?.temporaryLimit || '', composerEmpty: s?.composerEmpty !== false}; }
        if (msg.type === 'lock') return {locked:false};
        if (msg.type === 'transcript') return quickTranscript || {
          ok:true, userMessages:[], nMessages:0, lastAssistant:'', lastRole:'user' };
        actions.push({id, ...msg});
        if (msg.action === 'dismiss-rate-limit') { states[id].rateLimited = ''; return {ok:true}; }
        return outcome;
      },
    },
    scripting: { async executeScript(request) {
      if (stalledInjection) return new Promise(() => {});
      if (request?.world === 'MAIN' && String(request.func).includes('readConversationInPage')) return [{result: exportPage}];
      if (request?.world === 'MAIN' && String(request.func).includes('update_time')) {
        apiReads.push({tabId:request.target.tabId,chat:request.args[0]});
        return [{result: apiTimes[request.args[0]] ?? null}];
      }
      if (request?.world === 'MAIN' && String(request.func).includes('endpoint = control')) {
        const [chat,control] = request.args;
        apiReads.push({tabId:request.target.tabId,chat,control:!!control});
        return [{result: control ? controlStatus : apiStatuses[chat] ?? null}];
      }
    } },
    ...(debuggerStub ? { debugger: debuggerStub } : {}),
    ...(windowState ? { windows: { async get(id) { return { id, state: windowState }; }, async update() {} } } : {}),
    system: { memory: { async getInfo() { return {availableCapacity: memoryFreeBytes}; } } },
    notifications: { create() {}, onClicked:listener }, action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    runtime: { onInstalled: listener, onStartup: listener, onMessage: {addListener(fn){runtimeMessage=fn;}} },
    alarms: { async get() { return {periodInMinutes:0.5}; }, create() {}, onAlarm:listener },
  };
  const context = vm.createContext({ ...policy, ...telemetry, ...recovery, ...interfacePressure, ...memoryGuard, ...dispatchScope, ...liveness, ...fleetUiState, ...chatHandoffPolicy, ...limitReload, ...sessionExport, ...fullHandoff, ...overseerTabs,
    crypto: webcrypto, TextEncoder,
    installOwnerNoticeClicks:(report)=>ownerNotices.installOwnerNoticeClicks(report,chrome),
    deliverOwnerNotices:(items,report)=>ownerNotices.deliverOwnerNotices(items,report,chrome),
    recordServerAvailability:(up)=>ownerNotices.recordServerAvailability(up,chrome),
    RECOVERY_DEFAULTS: {...recovery.RECOVERY_DEFAULTS, accountActionGapSec:actionGapSec, settleSec:0},
    chrome, Date, Math, setTimeout: stalledInjection ? (fn, ms) => setTimeout(fn, Math.min(ms, 5)) : setTimeout,
    clearTimeout, INSTALL_DEFAULTS, localSettings: async () => ({}),
    researchTick: async () => { if (researchError) throw Error(researchError); },
    DEFAULT_MCP_URL: 'mock', chatLines, fleetState: async (_url, launcher) => { if (fleetError) throw Error(fleetError); launchers.push(structuredClone(launcher)); return {workers, projects:[{name:'P', count:8, next:{tz:'TZ-2',kind:'work'}}], attention:[], wake, browser:{known:true,autostart:serverAutostart,halt:''}, launchGate:{known:true,state:gateDown?'down':'up',allowed:!gateDown}}; },
    reportExtension: async (_url, payload) => {
      reports.push(structuredClone(payload));
      if (payload.detail?.startsWith('browser_control:') && !rejectBrowserReadback) {
        const request=JSON.parse(payload.detail.slice('browser_control:'.length));
        if(typeof request.autostart==='boolean')serverAutostart=request.autostart;
      }
      return "extension lease updated";
    },
    callTool: async (_url, name, args) => {
      toolCalls.push({name, args: structuredClone(args)});
      if (name === 'fleet_quick_orders') return JSON.stringify(quickJobs);
      if (name === 'fleet_chat_handoff') return handoffReply ?? JSON.stringify({action:'none', fill:0, source:'audit', chars:0});
      if (name === 'fleet_overseer_chats') return JSON.stringify(overseen);
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
  return {data, actions, removed, created, createdProps, updated, reloaded, discarded, toolCalls, launchers, reports, apiReads,
    health() { return vm.runInContext('({...runtimeHealth,running})', context); },
    async tick() { await vm.runInContext('tick("test")', context); },
    setQuickJobs(value) { quickJobs=value; vm.runInContext('quickList.at=0',context); },
    setTranscript(value) { quickTranscript=value; },
    setConversationTime(id, value) { apiTimes[id] = value; },
    setApiStatus(id, value) { apiStatuses[id] = value; },
    message(payload) { return new Promise((resolve)=>runtimeMessage(payload,{},resolve)); },
    reserveOverseer() { return vm.runInContext('reserveOverseerSend()', context); },
    setOverseerMemo(id, memo) {
      vm.runInContext(`overseerMem[${JSON.stringify(id)}] = ${JSON.stringify(memo)}`, context);
    },
    setOutcome(value) { outcome = value; } };
}

test('надзор: chrome.storage сохраняет потолок после restart и не допускает гонок', async () => {
  const h = harness();
  const attempts = await Promise.all(Array.from({length: 13}, () => h.reserveOverseer()));
  assert.equal(attempts.filter(Boolean).length, 12);
  assert.equal(h.data.overseerSends.length, 12);

  const restored = harness({saved: {overseerSends: h.data.overseerSends}});
  assert.equal(await restored.reserveOverseer(), false, 'новый service worker читает сохранённый предел');
  assert.equal(restored.data.overseerSends.length, 12);

  const expired = harness({saved: {overseerSends: Array(12).fill(Date.now() - 3600_001)}});
  assert.equal(await expired.reserveOverseer(), true);
  assert.equal(expired.data.overseerSends.length, 1, 'старые метки удалены из storage');

  const broken = harness({storageWriteError: 'storage unavailable'});
  await assert.rejects(broken.reserveOverseer(), /storage unavailable/, 'при ошибке записи нельзя отправлять');
});

test('owner-sv verdict sends its own 437-char draft via quick-proven scoped send path, never reloads it', async () => {
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1',text='Надзор: '+ 'действие '.repeat(48);
  const own={...tab(1),active:false,autoDiscardable:true};
  const h=harness({tabs:[own],states:{1:state('owner-sv',{url,composerEmpty:false})},
    workers:[],overseen:[{url,mode:'send'}],
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  h.setOverseerMemo(id,{key:'verdict-key',changedAt:Date.now()-400_000,
    reported:true,lastTranscriptCount:1,
    result:{action:'send',text,verdict:'continue',key:'verdict-key'}});
  await h.tick();
  const sends=h.actions.filter(x=>x.action==='continue' && x.overseer===true);
  assert.equal(sends.length,1,JSON.stringify(h.data.status));
  assert.equal(sends[0].text,text);
  assert.notEqual(sends[0].quick,true,'never overwrite a nonmatching human draft');
  assert.deepEqual(h.reloaded,[],'an unsent draft cannot be reloaded');
  assert.equal(h.data.overseerPending[id],undefined,'DOM-acknowledged send clears durable outbox');
  assert.equal(h.data.overseerSends.length,1,'only one hourly send budget reservation');
  assert.ok(h.updated.some(x=>x.id===1 && x.autoDiscardable===false),'owner-sv cannot be discarded');
});

test('trusted owner-sv tab cannot be hijacked by old f-agent label found in DOM', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1';
  const oldLabel='f1116-1120abc', text='⟦надзор⟧ Продолжай работу.';
  const h=harness({tabs:[{...tab(1),active:false}],
    states:{1:state(oldLabel,{url,composerEmpty:false,composerOverseerDraft:true})},
    workers:[worker(oldLabel)],overseen:[{url,mode:'send'}],outcome:{ok:true},
    transcript:{ok:true,nMessages:2,lastRole:'assistant',lastAssistant:'Готово',
      userMessages:['Задача']},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  await h.tick();
  assert.ok(h.actions.some(a=>a.action==='overseer-resume-draft'));
  assert.equal(h.data.labels.byTab['1'].label,'owner-sv');
  assert.equal(h.data.ownTabs['1'],'owner-sv');
  assert.deepEqual(h.reloaded,[]);
});

test('owner-sv failed send keeps durable draft; retry does not consume budget again or reload', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1',text='Надзор: строгая повторная отправка 437 символов '+ 'z'.repeat(390);
  const h=harness({tabs:[{...tab(1),active:false,autoDiscardable:true}],
    states:{1:state('owner-sv',{url,composerEmpty:false})},workers:[],
    overseen:[{url,mode:'send'}],outcome:{ok:false,detail:'Send button was not found'},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  h.setOverseerMemo(id,{key:'v',changedAt:Date.now()-360_000,reported:true,
    lastTranscriptCount:1,result:{action:'send',text,verdict:'continue',key:'v'}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
  assert.equal(h.data.overseerPending[id].text,text);
  assert.deepEqual(h.reloaded,[]);
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1,'no retry flood within 20 s');
  h.data.overseerPending[id].attemptAt=Date.now()-21_000;
  h.setOutcome({ok:true});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,2);
  assert.equal(h.data.overseerPending[id],undefined);
  assert.equal(h.data.overseerSends.length,1,'retries do not consume extra hourly send quota');
  assert.deepEqual(h.reloaded,[]);
});

test('owner-sv restores old tagged verdict after previous result was lost; user turn confirms timeout', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1';
  const draft='⟦надзор⟧ Продолжи работу и предъяви проверку.';
  const states={1:state('owner-sv',{url,composerEmpty:false,composerOverseerDraft:true})};
  const h=harness({tabs:[{...tab(1),active:false}],states,workers:[],
    overseen:[{url,mode:'send'}],
    transcript:{ok:true,nMessages:2,lastRole:'assistant',lastAssistant:'Ответ агента',
      userMessages:['Исходное поручение']},
    outcome:{ok:false,detail:'content message port timeout'},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='overseer-resume-draft').length,1);
  assert.equal(h.data.overseerPending[id].legacyDraft,true);
  assert.equal(h.data.overseerSends.length,1);
  assert.deepEqual(h.reloaded,[]);
  states[1]={...states[1],composerEmpty:true,composerOverseerDraft:false,generating:true};
  h.setTranscript({ok:true,nMessages:3,lastRole:'user',lastAssistant:'',
    userMessages:['Исходное поручение',draft]});
  await h.tick();
  assert.equal(h.data.overseerPending[id],undefined,'new user turn acknowledges original send after timeout');
  assert.equal(h.actions.filter(a=>a.action==='overseer-resume-draft').length,1,
    'no duplicate sends after transcript confirms');
});

test('owner-sv retains old draft and reload is rate-limited across service worker boots', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1';
  const initial={ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}};
  const rows=[{url,mode:'send'}];
  const h=harness({tabs:[{...tab(1),active:false}],states:{1:state('owner-sv',
    {url,composerEmpty:false})},workers:[],overseen:rows,saved:initial});
  h.setOverseerMemo(id,{key:'0|0|',changedAt:Date.now()-600_000,reported:true});
  await h.tick();
  assert.deepEqual(h.reloaded,[],'draft always protects against reload');
  const recent={...initial,overseerReloadAt:{[id]:Date.now()-200_000}};
  const afterBoot=harness({tabs:[{...tab(1),active:false}],states:{1:state('owner-sv',{url})},
    workers:[],overseen:rows,saved:recent});
  afterBoot.setOverseerMemo(id,{key:'0|0|',changedAt:Date.now()-600_000,reported:true});
  await afterBoot.tick();
  assert.deepEqual(afterBoot.reloaded,[],'persisted 5-minute cooldown applies after worker restart');
  const eligible=harness({tabs:[{...tab(1),active:false}],states:{1:state('owner-sv',{url})},
    workers:[],overseen:rows,saved:{...initial,overseerReloadAt:{[id]:Date.now()-301_000}}});
  eligible.setOverseerMemo(id,{key:'0|0|',changedAt:Date.now()-600_000,reported:true});
  await eligible.tick();
  await eligible.tick();
  assert.deepEqual(eligible.reloaded,[1],'one permitted refresh only, then 5 minute cooldown');
});

test('browser autostart: серверная запись и readback определяют локальное значение, не тик со старым снимком', async () => {
  const h=harness({serverAutostart:true,workers:[]});
  const response=await h.message({type:'browser-control',autostart:false});
  assert.deepEqual(JSON.parse(JSON.stringify(response)),{ok:true,autostart:false});
  assert.equal(h.data.browserAutostart,false);
  await h.tick();
  assert.equal(h.data.browserAutostart,false);
  assert.ok(h.reports.some(r=>r.detail?.includes('browser_control:')));

  const stale=harness({serverAutostart:true,rejectBrowserReadback:true});
  const result=await stale.message({type:'browser-control',autostart:false});
  assert.equal(result.ok,false);
  assert.equal(stale.data.browserAutostart,undefined,'никакого оптимистичного local commit без readback');
});

test('159 MB emergency: discard answered quick and regular tabs, report RAM', async () => {
  const regular='f1116-0935abc',quick='pult-q16';
  const tabs=[1,2,3].map(id=>({...tab(id),active:id===3,pinned:false}));
  const h=harness({tabs,
    memoryFreeMB:159,
    states:{1:state(quick,{url:tabs[0].url,lastRole:'assistant',composerEmpty:true}),
      2:state(regular,{url:tabs[1].url,lastRole:'assistant',composerEmpty:true}),
      3:state('owner-console',{url:tabs[2].url})},
    quickJobs:[{id:'q16',label:quick,url:tabs[0].url,idle:false,open:true,send:null,status:'working'}],
    workers:[worker(regular)],
    saved:{ownTabs:{1:quick,2:regular},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label:quick},2:{label:regular}},byChat:{}},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,
        projects:['P'],maxTabs:32,maxLiveAgentTabs:8}}});
  await h.tick();
  assert.deepEqual([...h.discarded].sort(),[1,2],JSON.stringify(h.data.fleetMemoryState));
  assert.ok(h.created.length>=1,'низкая ОЗУ не блокирует новый чат при свободном штатном слоте');
  assert.deepEqual(h.reloaded, [], 'the same tick must not undo its own memory rescue');
  assert.equal(h.data.fleetMemoryState.lowMemory,true);
  assert.equal(h.data.fleetMemoryState.originalFreeMemMB,159);
  assert.ok(h.data.fleetMemoryState.liveTabs>=h.created.length);
  assert.equal(h.data.fleetMemoryState.discarded,2);
  const report = h.reports.find(r=>r.tick_completed);
  assert.ok(report?.detail?.includes('"live_tabs":'+h.data.fleetMemoryState.liveTabs),h.data.tickError || JSON.stringify(h.data.status));
  assert.ok(report.detail.includes('"threshold_mb":250'));
  assert.equal(h.actions.filter(a=>['start','continue'].includes(a.action)).length,0);
});

test('memory rescue runs before fleet API: dev-mcp outage cannot block safe discards', async () => {
  const regular='f1116-0935abc',quick='pult-q16';
  const tabs=[1,2].map(id=>({...tab(id),active:false,pinned:false}));
  const h=harness({tabs,memoryFreeMB:159,fleetError:'dev-mcp offline',
    states:{1:state(quick,{url:tabs[0].url,lastRole:'assistant'}),
      2:state(regular,{url:tabs[1].url,lastRole:'assistant'})},
    workers:[worker(regular)],
    saved:{ownTabs:{1:quick,2:regular},
      fleetWorkers:[{label:regular,task:'P/TZ-1'}],
      fleetQuickOrderSummary:[{id:'q16',label:quick,url:tabs[0].url,
        status:'answered',idle:true,open:false,send:false,wake:false}],
      labels:{byTab:{1:{label:quick},2:{label:regular}},byChat:{}}}});
  await h.tick();
  assert.deepEqual([...h.discarded].sort(),[1,2]);
  assert.equal(h.data.fleetMemoryState.liveTabs,0);
  assert.equal(h.data.fleetMemoryState.originalFreeMemMB,159);
  assert.match(h.data.tickError,/dev-mcp offline/);
  assert.equal(h.created.length,0);
});

test('9th quick opens despite obsolete maxLiveAgentTabs=8 and 159 MB free RAM', async () => {
  const tabs=Array.from({length:8},(_,i)=>({...tab(i+1),active:false,pinned:false}));
  const owned=Object.fromEntries(tabs.map((t,i)=>[t.id,'pult-q'+(i+1)]));
  const states=Object.fromEntries(tabs.map((t,i)=>[t.id,state(owned[t.id],{
    url:t.url,lastRole:'user',composerEmpty:true})]));
  const jobs=tabs.map((t,i)=>({id:'q'+(i+1),label:owned[t.id],
    url:t.url,idle:false,open:true,send:null,status:'working'}));
  jobs.push({id:'q9',label:'pult-q9',home:'https://chatgpt.com/g/fleet/project',
    idle:false,open:true,send:'new task',status:'new'});
  const h=harness({tabs,states,workers:[],quickJobs:jobs,serverAutostart:false,memoryFreeMB:159,
    saved:{ownTabs:owned,quickTabJobs:Object.fromEntries(tabs.map(t=>[t.id,'q'+t.id])),
      labels:{byTab:{},byChat:{}},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,
        lockTabs:false,projects:['P'],maxTabs:32,maxLiveAgentTabs:8}}});
  await h.tick();
  assert.ok(h.createdProps.some(p=>p.url==='https://chatgpt.com/g/fleet/project'), 'новая quick-вкладка открывается даже при 159 МБ');
  assert.ok(h.data.fleetMemoryState.liveTabs>=9);
  assert.equal(h.data.fleetMemoryState.lowMemory,true);
  assert.equal('maxLiveTabs' in h.data.fleetMemoryState,false);
  assert.equal('waitingQuick' in h.data.fleetMemoryState,false);
  assert.equal(h.data.status['waiting-q9'],undefined);
  const report=h.reports.find(r=>r.tick_completed);
  assert.ok(report.detail.includes('"live_tabs":' + (tabs.length + h.created.length)));
  assert.doesNotMatch(report.detail,/waiting_quick|ждёт места/);
});

test('quick parked и idle закрывают свою вкладку вместо сохранения renderer', async () => {
  const label='pult-q16', url='https://chatgpt.com/c/abcdefab-1';
  const j={id:'q16',label,idle:true,open:false,status:'parked',close:false,send:null,url};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],
    states:{1:state(label,{url})},workers:[],quickJobs:[j],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}}}});
  await h.tick();
  assert.ok(h.removed.includes(1));
  assert.deepEqual(h.discarded,[]);
  assert.equal(h.data.ownTabs['1'],undefined);
  assert.equal(h.actions.filter(a=>a.action==='continue'||a.action==='start').length,0);
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report' &&
    JSON.parse(c.args.report).tab_closed === true));
});

test('quick send: persisted third failure pauses without marking sent or spending budget',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const settings={paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],
    states:{1:state(label,{url,composerEmpty:true})},outcome:{ok:false,detail:'send not acknowledged'},
    workers:[worker('f1')],quickJobs:[{id:'q16',label,url,idle:false,send:'new task'}],
    saved:{settings,ownTabs:{1:label},quickSendFailures:{q16:{count:2,reason:'previous failure'}},
      labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}}}});
  await h.tick();
  assert.equal(h.data.quickSendFailures.q16.count,3);
  assert.ok(h.data.quickSendFailures.q16.pausedUntil>=Date.now()+9*60_000);
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
  assert.ok(h.data.events.some(e=>e.type==='quick-paused'));
  assert.ok(h.data.status['1'].reason.includes('send not acknowledged'));
  assert.equal(h.data.accountActions.history.filter(e=>e.type==='quick-send').length,0);
  assert.equal(h.toolCalls.filter(c=>c.name==='fleet_quick_report'&&c.args.report.includes('"sent":true')).length,0);
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report'&&
    JSON.parse(c.args.report).send_paused===true &&
    JSON.parse(c.args.report).send_pause_reason==='send not acknowledged'));
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1,'paused task must not send again');
  h.data.quickSendFailures.q16.pausedUntil=Date.now()-1; // 10 minutes elapsed
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,2,'expired pause retries automatically');
  assert.ok(h.data.events.some(e=>e.type==='quick-pause-expired'));
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report'&&
    JSON.parse(c.args.report).send_paused===false));
});

test('legacy three-failure pause is migrated to 10-minute expiry, never permanent', async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],workers:[],outcome:{ok:false,detail:'again'},
    states:{1:state(label,{url})},quickJobs:[{id:'q16',label,url,send:'new task',open:true,idle:false}],
    saved:{ownTabs:{1:label},quickSendFailures:{q16:{count:3,reason:'старое зависание'}},
      labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  const until=h.data.quickSendFailures.q16.pausedUntil;
  assert.ok(until>Date.now());
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0);
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).send_pause_reason==='старое зависание'));
  h.data.quickSendFailures.q16.pausedUntil=Date.now()-1000;
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
});

test('restored dev-mcp clears persisted quick send failures immediately, reports unpause', async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],workers:[],
    states:{1:state(label,{url})},quickJobs:[{id:'q16',label,url,send:'new task',open:true,idle:false}],
    saved:{quickConnectivityLost:true,quickSendFailures:{q16:{count:3,reason:'не найдено поле ввода',
      pausedUntil:Date.now()+10*60_000}},ownTabs:{1:label},
      labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.equal(h.data.quickConnectivityLost,false);
  assert.equal(h.data.quickSendFailures.q16,undefined);
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).send_paused===false));
});

test('network error disappears: live quick send freeze clears even without a dev-mcp outage', async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const states={1:state(label,{url,failed:'No response from ChatGPT. Check your connection.'})};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],workers:[],
    states,quickJobs:[{id:'q16',label,url,send:'new task',open:true,idle:false}],
    saved:{ownTabs:{1:label},quickSendFailures:{q16:{count:3,reason:'No response',
      pausedUntil:Date.now()+600_000}},labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0);
  states[1]=state(label,{url});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
  assert.ok(h.data.events.some(e=>e.type==='quick-network-recovered'));
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).send_paused===false));
});

test('connector-prefixed NEW DOM user turn confirms quick sent after click', async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const task='Ты агент флота, label="pult-q16". Работай по полученной памятке.';
  const states={1:state(label,{url})};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],workers:[],
    states,
    quickJobs:[{id:'q16',label,url,send:task,open:true,idle:false}],
    transcript:{ok:true,nMessages:0,userMessages:[],lastAssistant:'',lastRole:'user'},
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.equal(h.actions.filter(x=>x.action==='continue').length,1);
  assert.equal(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).sent===true),false,'click alone cannot confirm');
  states[1]=state(label,{url,generating:true});
  h.setTranscript({ok:true,nMessages:2,userMessages:["@dev-mcp "+task],
    lastAssistant:'',lastRole:'user'});
  await h.tick();
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).sent===true));
  assert.equal(h.data.quickSendFailures.q16,undefined);
});

test('quick send: tab bound to another quick job is never used',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],
    states:{1:state(label,{url})},workers:[worker('f1')],
    quickJobs:[{id:'new-job',label,url,send:'new task',idle:false}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'old-job'},labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue'||a.action==='start').length,0);
});

test('quick close removes every owned duplicate tab and reports closure', async () => {
  const label = 'pult-q16', url = 'https://chatgpt.com/c/abcdefab-1';
  const tabs = [1, 2].map(id => ({...tab(id), pinned:false, active:false}));
  const h = harness({tabs, states:{1:state(label,{url}),2:state(label,{url})},
    workers:[], quickJobs:[{id:'q16',label,url,close:true,idle:false}],
    saved:{ownTabs:{1:label,2:label},quickTabJobs:{1:'q16',2:'q16'},
      labels:{byTab:{1:{label},2:{label}},byChat:{}}}});
  await h.tick();
  assert.deepEqual(h.removed.sort(),[1,2]);
  assert.ok(h.toolCalls.some(c => c.name === 'fleet_quick_report' &&
    JSON.parse(c.args.report).tab_closed === true));
});

test('quick job disappearing from orders closes its owned tab after grace period', async () => {
  const label='pult-q16';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states:{1:state(label)},
    workers:[],quickJobs:[],saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      quickOrphanSince:{1:Date.now()-65_000},
      labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.ok(h.removed.includes(1));
  assert.equal(h.data.quickTabJobs['1'],undefined);
});

test('quick No response: error-class to MCP, not working, same-chat recovery', async () => {
  const label='pult-q16', url='https://chatgpt.com/c/abcdefab-1';
  const states={1:state(label,{url,failed:'No response from ChatGPT. Check your connection.',
    composerEmpty:true,composerPresent:true})};
  const job={id:'q16',label,url,send:'pending',idle:false,open:true,recentMcp:true};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states,
    workers:[],quickJobs:[job],saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}},
      reloadMem:{1:{since:Date.now()-65_000}}}});
  await h.tick();
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).error_class==='turn-failed'));
  assert.ok(h.updated.some(x=>x.id===1 && x.url===url));
  assert.equal(h.data.fleetQuickStates.active,0, 'recentMcp cannot mask broken UI');
  assert.equal(h.actions.filter(a=>['continue','start'].includes(a.action)).length,0);
  states[1]=state(label,{url});
  h.setQuickJobs([{...job,send:null}]);
  await h.tick();
  assert.equal(h.data.fleetQuickStates.active,1, 'healthy page clears error classification');
});

test('quick Could not load conversation shares reloadPlan and account lane', async () => {
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const broken=state(label,{url,composerPresent:false,pageLoadFailed:'Could not load this ChatGPT conversation'});
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states:{1:broken},workers:[],
    quickJobs:[{id:'q16',label,url,send:'pending',idle:false,open:true,recentMcp:true}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}},
      reloadMem:{1:{since:Date.now()-65_000}}}});
  await h.tick();
  assert.ok(h.updated.some(x=>x.id===1 && x.url===url));
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).error_class==='conversation-load'));
  assert.equal(h.data.fleetQuickStates.active,0);
  assert.equal(h.actions.filter(a=>['start','continue'].includes(a.action)).length,0);
  await h.tick();
  assert.equal(h.updated.filter(x=>x.url===url).length,1,
    'one failed tab must not spin in repeated navigation');
});

test('two failed quick conversations recover one at a time on shared account lane', async () => {
  const jobs=[1,2].map(i=>({id:'q'+i,label:'pult-q'+i,url:'https://chatgpt.com/c/abcdefab-'+i,
    idle:false,open:true,send:'pending'}));
  const tabs=[1,2].map(i=>({...tab(i),active:false,pinned:false}));
  const states=Object.fromEntries(jobs.map((j,i)=>[i+1,state(j.label,{url:j.url,
    pageLoadFailed:'Could not load this ChatGPT conversation',composerPresent:false})]));
  const now=Date.now();
  const h=harness({tabs,states,workers:[],quickJobs:jobs,actionGapSec:15,
    saved:{ownTabs:{1:'pult-q1',2:'pult-q2'},quickTabJobs:{1:'q1',2:'q2'},
      labels:{byTab:{1:{label:'pult-q1'},2:{label:'pult-q2'}},byChat:{}},
      reloadMem:{1:{since:now-65_000},2:{since:now-65_000}}}});
  await h.tick();
  assert.equal(h.updated.filter(x=>x.url).length,1);
  assert.equal(h.data.accountActions.history.filter(x=>x.type==='reload').length,1);
  h.data.accountActions.lastActionAt=Date.now()-16_000;
  await h.tick();
  assert.equal(h.updated.filter(x=>x.url).length,2);
  assert.deepEqual(h.updated.filter(x=>x.url).map(x=>x.url).sort(),jobs.map(j=>j.url).sort());
});

test('quick successful UI click is not sent; transcript confirms even during generation and closes tab', async () => {
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1', text='New task 2026';
  const states={1:state(label,{url})};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states,
    workers:[],quickJobs:[{id:'q16',label,url,idle:false,open:true,
      closeAfterSend:true,send:text}],outcome:{ok:true,url},
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}}}});
  await h.tick();
  assert.equal(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).sent===true),false,'click must not be mistaken for delivery');
  states[1]={...states[1],generating:true};
  h.setTranscript({ok:true,userMessages:[text],nMessages:1,lastAssistant:'',lastRole:'user'});
  await h.tick();
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).sent===true));
  assert.ok(h.removed.includes(1));
  assert.ok(h.toolCalls.some(x=>x.name==='fleet_quick_report' &&
    JSON.parse(x.args.report).tab_closed===true));
});

test('quick browser error navigates the same tab to the known chat, with retry delay', async () => {
  const label = 'pult-q16', url = 'https://chatgpt.com/c/abcdefab-1';
  const t = {...tab(1),url:'chrome-error://chromewebdata/',active:false,pinned:false,status:'complete'};
  const h = harness({tabs:[t],states:{1:new Error('no content script')},workers:[],serverAutostart:false,
    quickJobs:[{id:'q16',label,url,send:'pending',idle:false}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},labels:{byTab:{1:{label}},byChat:{}},
      reloadMem:{1:{since:Date.now()-65_000}}}});
  await h.tick();
  assert.ok(h.updated.some(x => x.id === 1 && x.url === url));
  assert.equal(h.data.accountActions.history.filter(x=>x.type==='reload').length,1);
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report' &&
    JSON.parse(c.args.report).error_class==='network-load'));
  await h.tick();
  assert.equal(h.updated.filter(x => x.url === url).length,1,'shared reloadPlan blocks immediate repeats');
});

test('quick UI generation labels are sent separately from heartbeat evidence', async () => {
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],
    states:{1:state(label,{url,generating:true})},workers:[],
    quickJobs:[{id:'q16',label,url,idle:false,send:null}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.ok(h.reports.some(r => r.detail === 'quick_generating:["pult-q16"]'));
  assert.ok(h.reports.some(r => r.tick_completed && !String(r.detail || '').startsWith('quick_generating:')));
});

test('quick-send под общей паузой и потолком не расходует новый action и не пишет в чат',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1',now=Date.now();
  const quick={id:'q16',label,url,idle:false,send:'Новая задача'};
  const history=Array.from({length:120},()=>({at:now-5000,type:'reload',label:'f1'}));
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],
    states:{1:state(label,{url,composerEmpty:true,lastRole:'assistant'})},
    workers:[worker('f1')],actionGapSec:15,quickJobs:[quick],
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}},
      accountActions:{lastActionAt:now-5000,history},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0);
  assert.equal(h.data.accountActions.history.length,120);
  assert.match(h.data.status['1'].reason,/общая пауза аккаунта/);
});

test('18.11: первое сообщение в уже открытый quick-чат не голодает за полосой навигации',async()=>{
  const label='pult-q16',home='https://chatgpt.com/g/fleet/project',now=Date.now();
  const quick={id:'q16',label,send:'Задача',idle:false,home};
  const mk=(lastAt)=>harness({tabs:[{...tab(1),url:home,active:false,pinned:false}],
    states:{1:state(label,{url:home,composerEmpty:true,lastRole:''})},
    workers:[worker('f1')],actionGapSec:15,quickJobs:[quick],
    transcript:{ok:true,nMessages:0,userMessages:[],lastAssistant:'',lastRole:''},
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label}},byChat:{}},
      accountActions:{lastActionAt:lastAt,history:[{at:lastAt,type:'quick-open',label:'pult-q17'}]},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  // Открытие другого чата 7 с назад: полоса навигации (15 с) ещё занята, но отправка — не навигация.
  const sent=mk(now-7000); await sent.tick();
  assert.equal(sent.actions.filter(a=>a.action==='start').length,1);
  // Открытие другого чата 1 с назад в том же тике тоже не держит отправку (живьём 07:18 голодало).
  const burst=mk(now-1000); await burst.tick();
  assert.equal(burst.actions.filter(a=>a.action==='start').length,1);
});

test('18.11: зависший рендерер при отправке на переднем плане не вешает цепочку и тик',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states:{1:state(label,{url})},
    workers:[],quickJobs:[{id:'q16',label,url,send:'Задача',open:true,idle:false}],
    transcript:{ok:true,nMessages:0,userMessages:[],lastAssistant:'',lastRole:'user'},
    stalledInjection:true,windowState:'minimized',
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label}},byChat:{}}}});
  const done=await Promise.race([h.tick().then(()=>'done'),new Promise(r=>setTimeout(()=>r('hung'),10_000))]);
  assert.equal(done,'done','visibilityState без ответа не должен держать тик');
  assert.ok(h.data.status['1']?.reason,'исход отправки записан, вкладка не потеряна');
});

test('18.11: отправка идёт в фоне через эмуляцию фокуса — без активации вкладки и окна',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1',calls=[];
  const debuggerStub={async attach(t){calls.push(['attach',t.tabId]);},async detach(t){calls.push(['detach',t.tabId]);},
    async sendCommand(t,m,p){calls.push([m,p?.enabled??p?.state]);return {};}};
  const h=harness({tabs:[{...tab(1),active:false,pinned:false}],states:{1:state(label,{url})},
    workers:[],quickJobs:[{id:'q16',label,url,send:'Задача',open:true,idle:false}],
    transcript:{ok:true,nMessages:0,userMessages:[],lastAssistant:'',lastRole:'user'},
    windowState:'minimized',debuggerStub,
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label}},byChat:{}}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,1);
  assert.deepEqual(calls.filter(c=>c[0]==='Emulation.setFocusEmulationEnabled').map(c=>c[1]),[true,false]);
  assert.ok(calls.some(c=>c[0]==='detach'),'отладчик отключается после отправки');
  assert.equal(h.updated.filter(u=>u.active===true).length,0,'вкладка не выводится вперёд');
});

test('parked with an explicit outbox order opens, confirms the new turn, and frees its slot', async () => {
  const id = 'q16', label = 'pult-q16', url = tab(1).url, send = 'dispatch regression';
  const job = { id, label, url, send, idle: false, open: true, status: 'parked', closeAfterSend: true };
  const initial = { settings: { paceVersion: 10, enabled: true, projects: ['P'], maxWorking: 1 } };
  const cold = harness({ tabs: [], workers: [worker('busy')], quickJobs: [job], saved: initial });
  await cold.tick();
  assert.equal(cold.createdProps[0]?.url, url);
  assert.equal(cold.data.quickTabJobs[100], id);
  const h = harness({ tabs: [{ ...tab(100), url }], workers: [worker('busy')], states: { 100: state(label, {url}) },
    quickJobs: [job], saved: cold.data });
  await h.tick();
  assert.equal(h.actions.filter(a => a.quick && a.action === 'continue').length, 1, JSON.stringify(h.data.status));
  assert.equal(h.removed.length, 0);
  h.setTranscript({ ok: true, nMessages: 1, userMessages: [send], lastAssistant: '', lastRole: 'user' });
  await h.tick();
  assert.ok(h.toolCalls.some(c => c.name === 'fleet_quick_report' && JSON.parse(c.args.report).sent));
  assert.deepEqual(h.removed, [100]);
});

test('quick-open retries a fourth time after backoff, including after MV3 restart', async () => {
  const job = {id:'q16', label:'pult-q16', url:tab(1).url, open:true, send:'pending'};
  const h = harness({tabs:[], workers:[worker('busy')], quickJobs:[job], createFail:true,
    saved:{settings:{paceVersion:10, enabled:true, projects:['P'], maxWorking:1}}});
  for (let count=1; count<=3; count++) {
    await h.tick();
    assert.equal(h.data.quickOpenAttempts.q16.count, count);
    if (count < 3) h.data.quickOpenAttempts.q16.at -= 181_000;
  }
  const restored = harness({tabs:[], workers:[worker('busy')], quickJobs:[job], saved:h.data});
  await restored.tick();
  assert.ok(!restored.createdProps.some(p => p.url === job.url));
  restored.data.quickOpenAttempts.q16.at -= 601_000;
  await restored.tick();
  assert.equal(restored.createdProps[0]?.url, job.url);
  assert.equal(restored.data.quickTabJobs[100], job.id);
});

test('low RAM gives an opened outbox time to send, then allows a trial for another job', async () => {
  const first = {id:'q1', label:'pult-q1', url:tab(1).url, open:true, idle:false, send:'current'};
  const next = {id:'q2', label:'pult-q2', url:tab(2).url, open:true, idle:false, send:'next'};
  const saved = {ownTabs:{1:first.label}, quickTabJobs:{1:first.id},
    settings:{paceVersion:10, enabled:true, projects:['P'], maxWorking:1, maxTabs:28},
    accountActions:{lastActionAt:0, history:[{at:Date.now()-30_000, type:'quick-open'}]}};
  const run = () => harness({tabs:[tab(1)], workers:[worker('busy')], states:{1:state(first.label)},
    memoryFreeMB:390, quickJobs:[first,next], saved});
  const h = run();
  await h.tick();
  assert.equal(h.actions.filter(a => a.quick && a.action === 'continue').length, 1);
  assert.equal(h.created.length, 0);
  assert.match(h.data.quickOpenBlocked.q2, /ОЗУ/);
  saved.accountActions.history[0].at -= 61_000;
  const trial = run();
  await trial.tick();
  assert.equal(trial.createdProps[0]?.url, next.url);
});

test('a still-loading quick conversation keeps its navigation and retries once after grace', async () => {
  const t = {...tab(1), status:'loading'}, label = 'pult-q1';
  const h = harness({tabs:[t], states:{1:new Error('loading')}, workers:[worker('busy')],
    quickJobs:[{id:'q1',label,url:t.url,open:true,send:'pending'}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q1'},reloadMem:{1:{since:Date.now()-80_000,
      unresponsiveSince:Date.now()-80_000}},settings:{enabled:true,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.ok(h.data.reloadMem[1].loadingSince);
  assert.equal(h.updated.filter(u => u.url).length, 0);
  assert.deepEqual(h.reloaded, []);
  h.data.reloadMem[1].loadingSince -= 121_000;
  await h.tick();
  assert.equal(h.updated.filter(u => u.url === t.url).length, 1);
  assert.deepEqual(h.reloaded, []);
});

test('one failed chat does not block a different job with a free interface', async () => {
  const job = {id:'q4', label:'pult-q4', url:tab(4).url, open:true, send:'pending'};
  const h = harness({tabs:[tab(1)], states:{1:state('f1',{pageLoadFailed:'load failed'})},
    workers:[worker('f1')], quickJobs:[job], saved:{ownTabs:{1:'f1'}}});
  await h.tick();
  assert.equal(h.createdProps[0]?.url, job.url);
});

test('persistent local load failures alone do not escalate to a model account cooldown', async () => {
  const tabs = [1,2,3].map(tab);
  const states = Object.fromEntries(tabs.map(t => [t.id, state('f'+t.id,{url:t.url,pageLoadFailed:'failed'})]));
  const saved = {ownTabs:{1:'f1',2:'f2',3:'f3'}, launch:{loadFailFirstAt:Date.now()-70_000}};
  const h = harness({tabs, states, workers:[], saved});
  await h.tick();
  assert.equal(h.data.tickError, '');
  assert.ok(!h.data.launch.overloadUntil);
  assert.ok(!h.data.events?.some(e => e.type==='account-overload'));
});

test('quick-open: пачка чатов не обходит общие 15 секунд между навигациями',async()=>{
  const jobs=[{id:'q16',label:'pult-q16',idle:false,home:'https://chatgpt.com/g/fleet/project'},
    {id:'q17',label:'pult-q17',idle:false,home:'https://chatgpt.com/g/fleet/project'}];
  const h=harness({tabs:[],states:{},workers:[worker('f1')],quickJobs:jobs,actionGapSec:15,
    saved:{settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.equal(h.created.length,1,'две новые quick-вкладки нельзя открывать пачкой');
  assert.equal(h.data.accountActions.history.filter(x=>x.type==='quick-open').length,1);
});

test('background passes only confirmed quick-agent starts and retains closed evidence across ticks',async()=>{
  const home='https://chatgpt.com/g/fleet/project';
  const tag='Q12-C83ACC',label='pult-q12-c83acc';
  const jobs=[
    {id:'q12-c83acc',agentTag:tag,label,url:'',send:'⟦быстрый агент Q12-C83ACC⟧ Новый старт',status:'new',open:true},
    {id:'q10-c83acc',label:'pult-q10-c83acc',url:'https://chatgpt.com/c/abcdefab-10',close:true},
    {id:'q8-c83acc',agentTag:'Q8-C83ACC',label:'pult-q8-c83acc',url:'https://chatgpt.com/c/abcdefab-8',status:'working',open:false,idle:true,send:null},
    {id:'q11-c83acc',agentTag:'Q11-C83ACC',label:'pult-q11-c83acc',url:'https://chatgpt.com/c/abcdefab-11',status:'new',open:false,idle:true,send:null},
  ];
  const h=harness({tabs:[{...tab(1),url:home,active:false}],
    states:{1:state(label,{url:home,lastRole:'',composerEmpty:true})},workers:[],quickJobs:jobs,
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q12-c83acc'},labels:{byTab:{1:{label,path:null}},byChat:{}}}});
  await h.tick();
  const action=h.actions.find((a)=>a.action==='start' && a.quick);
  assert.ok(action,'fresh quick startup must reach act');
  assert.deepEqual([...action.confirmedStartAgents].sort(),['Q10-C83ACC','Q8-C83ACC']);
  assert.equal(h.data.quickConfirmedStarts['Q10-C83ACC']>0,true);
  h.setQuickJobs([jobs[0]]);
  await h.tick();
  assert.equal(h.data.quickConfirmedStarts['Q10-C83ACC']>0,true,
    'confirmed closed agent remains known after orders stop returning it');
});

test('quick-reload под account overload ждёт и не трогает выгруженный чат',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false,discarded:true,pinned:false}],
    states:{1:state(label,{url})},workers:[worker('f1')],
    quickJobs:[{id:'q16',label,url,idle:false,send:'Задание'}],
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label,path:'abcdefab-1'}},byChat:{}},
      launch:{chatHistoryVersion:2,history:[],launched:{},overloadUntil:Date.now()+60_000},
      settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[]);
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0);
});

test('Unusual activity останавливает и быстрые отправки',async()=>{
  const label='pult-q16',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false}],
    states:{1:state(label,{url})},quickJobs:[{id:'q16',label,idle:false,url,send:'Проверка'}],
    saved:{ownTabs:{1:label},browserHalt:{reason:'ChatGPT: Unusual activity',at:Date.now(),
      resumeAt:Date.now()+20*60_000}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>['start','continue'].includes(a.action)).length,0);
  assert.equal(h.toolCalls.filter(c=>c.name==='fleet_quick_report').length,0);
});

test('failed fleet query records completion and error without claiming a fresh snapshot', async () => {
  const h=harness({fleetError:'signal timed out',saved:{lastTickAt:1}});
  await h.tick();
  assert.equal(h.data.lastTickAt,1);
  assert.equal(h.data.fleetUpdatedAt,undefined);
  assert.equal(h.data.fleetRefresh.error,'signal timed out');
  assert.ok(h.data.fleetRefresh.finishedAt);
  assert.match(h.data.tickError,/Сервер флота не ответил/);
  assert.ok(h.data.lastCycleFinishedAt >= h.data.lastCycleStartedAt);
});

test('fleet poll failures: no "no connection" alarm before 5 min without an answer (20.11.2026)', async () => {
  const lastOk = Date.now() - 60_000;
  const h=harness({fleetError:'MCP tools/call fleet_list: таймаут 30 с',
    saved:{lastTickAt:1,fleetConnection:{failures:2,lastOkAt:lastOk,since:Date.now()-30_000}}});
  await h.tick();
  assert.equal(h.data.fleetConnection.failures,3);
  assert.equal(h.data.fleetConnection.kind,'перегрузка');
  assert.equal(h.data.fleetConnection.lastOkAt,lastOk);
  assert.match(h.data.tickError,/перегрузка, неудач подряд 3/);
  assert.equal(h.data.ownerConnectivity,undefined,'three timeouts in ~90 s must not raise the alarm');
  const late=harness({fleetError:'TypeError: Failed to fetch',
    saved:{lastTickAt:1,fleetConnection:{failures:5,lastOkAt:Date.now()-6*60_000}}});
  await late.tick();
  assert.equal(late.data.fleetConnection.kind,'перезапуск');
  assert.equal(late.data.ownerConnectivity.failures,1);
  const ok=harness({saved:{fleetConnection:{failures:7,lastOkAt:1,kind:'перезапуск'}}});
  await ok.tick();
  assert.equal(ok.data.fleetConnection.failures,0);
  assert.ok(ok.data.fleetConnection.lastOkAt>1);
});

test('"A temporary limit" page reloads on schedule with fleet orchestration off; switch and typed text respected (20.11.2026)', async () => {
  const LIMIT='A temporary limit is preventing this chat from loading. Try again later.';
  const t=tab(1), due={1:{attempts:0,nextAt:1,url:t.url}};
  const run=async (page, settings)=>{
    const h=harness({tabs:[t],states:{1:state('mine',{url:t.url,...page})},workers:[],
      saved:{limitReloadMem:structuredClone(due),settings}});
    await h.tick(); return h;
  };
  const on=await run({temporaryLimit:LIMIT},{enabled:false});
  assert.deepEqual(on.reloaded,[1]);
  assert.equal(on.data.limitReloadMem['1'].attempts,1);
  assert.ok(on.data.limitReloadMem['1'].nextAt-Date.now()>60_000);
  assert.ok(on.data.log.some(line=>/«A temporary limit…» — перезагрузка №1/.test(line)));
  assert.deepEqual((await run({temporaryLimit:LIMIT},{enabled:false,limitReload:false})).reloaded,[]);
  assert.deepEqual((await run({temporaryLimit:LIMIT,composerEmpty:false},{enabled:false})).reloaded,[]);
  const loaded=await run({temporaryLimit:''},{enabled:false});
  assert.deepEqual(loaded.reloaded,[]);
  assert.equal(loaded.data.limitReloadMem['1'],undefined);
});

test('export-session returns the site conversation byte-for-byte with meta (20.11.2026)', async () => {
  const raw='{"title":"T","conversation_id":"abcdefab-1","current_node":"b","mapping":{"r":{"id":"r","children":["a"]},"a":{"id":"a","parent":"r","message":{"author":{"role":"user"}},"children":["b","c"]},"b":{"id":"b","parent":"a","message":{"author":{"role":"assistant"}},"children":[]},"c":{"id":"c","parent":"a","message":{"author":{"role":"assistant"}},"children":[]}}}';
  const h=harness({exportPage:{ok:true,status:200,text:raw}});
  const reply=await h.message({type:'export-session',tabId:1});
  assert.equal(reply.ok,true,reply.detail);
  assert.ok(reply.text.endsWith(',"conversation":'+raw+'}'));
  const parsed=JSON.parse(reply.text);
  assert.equal(parsed.meta.stats.messages,3);
  assert.equal(parsed.meta.stats.branches,1);
  assert.equal(parsed.meta.stats.currentBranchMessages,2);
  assert.match(reply.filename,/^chatgpt-T-abcdefab-\d{8}T\d{6}Z\.json$/);
  const bad=harness({exportPage:{ok:false,status:429,detail:'rate'}});
  const fail=await bad.message({type:'export-session',tabId:1});
  assert.equal(fail.ok,false);
  assert.match(fail.detail,/сайт ответил 429/);
});

test('failed storage cannot hide its own failure or permanently retain the cycle lock',async()=>{
  const error='IO error: .../021116.ldb: FILE_ERROR_NO_SPACE';
  const h=harness({storageWriteError:error,saved:{lastTickAt:1}});
  for(let i=0;i<2;i++) {
    await h.tick();
    assert.equal(h.health().running,false);
    assert.equal(h.health().storageError,error);
    assert.ok(h.health().cycleFinishedAt);
    assert.equal(h.data.lastTickAt,1,'failed persistence cannot claim a successful tick');
    assert.deepEqual(h.reports,[]);
  }
});

test('lazy restored known conversation rebinds a new tab ID before renderer recovery',async()=>{
  const label=autostartLabel(1), restored={...tab(41),status:'unloaded'};
  const h=harness({tabs:[restored],states:{41:new Error('renderer not loaded')},workers:[worker(label)],
    saved:{ownTabs:{1:label},labels:{byTab:{1:{label,path:'abcdefab-41'}},byChat:{'abcdefab-41':label}}}});
  await h.tick();
  assert.equal(h.data.ownTabs[41],label);
  assert.equal(h.data.sessions[41].id,'abcdefab-41');
  assert.deepEqual(h.reloaded,[41],h.data.tickError || JSON.stringify(h.data.status));
  assert.deepEqual(h.actions,[],'unloaded conversation must not receive blind continuation');
});

test('unowned lazy session placeholders cannot occupy every active interface seat',async()=>{
  const tabs=[...Array.from({length:5},(_,i)=>({...tab(i+10),status:'unloaded'})),tab(1)];
  const states={1:state('owner'),...Object.fromEntries(tabs.slice(0,5).map(t=>[t.id,new Error('unloaded')]))};
  const h=harness({tabs,states,workers:[],saved:{
    settings:{paceVersion:10,enabled:true,dryRun:false,projects:['P'],maxTabs:4},
    launch:{chatHistoryVersion:2,history:[],launched:{}}}});
  await h.tick();
  assert.equal(h.created.length,1,'a fresh worker needs one real interface');
  assert.deepEqual(h.removed,[],'unknown dormant pages and owner are preserved');
  assert.deepEqual(h.reloaded,[],'unknown dormant pages are not adopted or navigated');
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

test('disabled and halted workers finish cycles without claiming a fresh fleet snapshot',async()=>{
  for(const saved of [
    {settings:{paceVersion:10,enabled:false}},
    {browserHalt:{reason:'владелец: стоп',reported:true,at:Date.now()}},
  ]) {
    const h=harness({tabs:[],saved:{lastTickAt:1,...saved}});
    await h.tick();
    assert.ok(h.data.lastCycleStartedAt);
    assert.ok(h.data.lastCycleFinishedAt>=h.data.lastCycleStartedAt);
    assert.equal(h.data.lastTickAt,1);
    await h.tick(); // early return must release the serialization lock, too
    assert.ok(h.data.lastCycleFinishedAt>=h.data.lastCycleStartedAt);
  }
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
  assert.match(existing.reports[0].detail, /^composer ok; memory_guard:/);

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
  assert.match(healthy.reports[0].detail, /^composer ok; memory_guard:/);
  assert.equal(healthy.reports[0].page_text, '');
});

test('unmanaged and owner pages cannot keep the Fleet launch gate down', async () => {
  const failure = {composerPresent:false, cloudflarePageText:'Unable to load site'};
  const h = harness({
    gateDown:true,
    tabs:[tab(1),tab(2)],
    states:{1:state('',failure),2:state('owner-console',failure)},
    workers:[worker('owner-console',{task:''})],
  });
  for (let i = 0; i < 3; i += 1) await h.tick();
  assert.equal(h.reports.length,3);
  assert.ok(h.reports.every(report => report.tick_completed && report.detail.startsWith('memory_guard:') && !report.page_text));
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

test('stalled script reinjection cannot block healthy agents or the next cycle', async () => {
  const h=harness({
    tabs:[tab(1),tab(2)], states:{1:new Error('no content script'),2:state('f2')},
    workers:[worker('f1'),worker('f2')], stalledInjection:true,
    saved:{ownTabs:{1:'f1',2:'f2'},reloadMem:{1:{unresponsiveSince:Date.now()-15_000}}},
  });
  const completes = async () => {
    let timer;
    try {
      await Promise.race([h.tick(),new Promise((_,reject)=>{
        timer=setTimeout(()=>reject(Error('one renderer blocked the fleet cycle')),500);
      })]);
    } finally { clearTimeout(timer); }
  };
  await completes();
  assert.ok(h.reloaded.includes(1),'unresponsive owned tab reaches existing recovery');
  assert.ok(h.actions.some(a=>a.id===2),'healthy agent continues in the same cycle');
  assert.ok(h.data.lastCycleFinishedAt);
  const reports=h.reports.length;
  await completes();
  assert.ok(h.reports.length>reports,'serialization lock is released for the next alarm');
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

test('quick send immediately reports URL from successful start even before DOM sent ack', async () => {
  const label='pult-q98', url='https://chatgpt.com/c/abcdefab-98';
  const h=harness({tabs:[{...tab(1),active:false}],workers:[],
    states:{1:state(label,{url:'',lastRole:'',composerEmpty:true})},
    quickJobs:[{id:'q98',label,send:'task',url:'',open:true,idle:false}],
    outcome:{ok:true,url},saved:{ownTabs:{1:label},quickTabJobs:{1:'q98'}}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='start').length,1);
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report' &&
    JSON.parse(c.args.report).url===url), 'URL must reach the server independently of transcript ack');
  assert.equal(h.toolCalls.filter(c=>c.name==='fleet_quick_report' &&
    JSON.parse(c.args.report).sent===true).length,0);
});

test('quick send clamps legacy 4/3 failure record and does not click during the pause', async () => {
  const label='pult-q98',url='https://chatgpt.com/c/abcdefab-1';
  const h=harness({tabs:[{...tab(1),active:false}],workers:[],
    states:{1:state(label,{url})},quickJobs:[{id:'q98',label,url,send:'task',open:true,idle:false}],
    saved:{ownTabs:{1:label},quickSendFailures:{q98:{count:4,reason:'old retries',pausedUntil:Date.now()+600_000}}}});
  await h.tick();
  assert.equal(h.data.quickSendFailures.q98.count,3);
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0);
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_quick_report' &&
    JSON.parse(c.args.report).send_failures===3));
});

test('18.11: открытые чаты владельца не занимают слоты интерфейса флота',async()=>{
  const human=(id)=>({...tab(id),url:`https://chatgpt.com/c/0000000${id}-aaaa`,pinned:false,active:false});
  const humanState=()=>state('',{url:'https://chatgpt.com/c/human',lastRole:'assistant'});
  const jobs=[{id:'q16',label:'pult-q16',idle:false,send:'Задача',home:'https://chatgpt.com/g/fleet/project'}];
  const h=harness({tabs:[human(1),human(2),human(3),human(4)],states:{1:humanState(),2:humanState(),3:humanState(),4:humanState()},
    workers:[worker('f1')],quickJobs:jobs,actionGapSec:0,
    saved:{settings:{paceVersion:10,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:['P'],maxWorking:1}}});
  await h.tick();
  assert.equal(h.created.length,1,'четыре человеческих чата не должны блокировать вкладку быстрого агента');
  assert.equal(h.removed.length,0,'чаты владельца не закрываются ради флота');
});

test('W10-5: plain MCP handoff error is not JSON-parsed or retried each tick, including full chats', async () => {
  const label = 'pult-q6', url = 'https://chatgpt.com/c/abcdefab-1';
  const h = harness({ tabs: [{...tab(1),active:false}], workers: [],
    states: {1: state(label, {url,conversationFull:true,fill:1})},
    quickJobs: [{id:'q6',agentTag:'Q6-7278A5',label,url,idle:false}],
    handoffReply: `Error calling tool 'fleet_chat_handoff': PermissionError: handoff: quick conversation does not match`,
    saved: {ownTabs: {1:label}} });
  await h.tick();
  const calls = () => h.toolCalls.filter(t => t.name === 'fleet_chat_handoff');
  assert.equal(calls().length, 1);
  const receipt = h.data.chatHandoffs['Q6-7278A5:abcdefab-1'];
  assert.ok(receipt.errorRetryAt >= Date.now() + 5 * 60_000 - 2000);
  await h.tick();
  assert.equal(calls().length, 1, 'conversationFull does not defeat handoff backoff');
});

test('W10-5: expired reserved project draft closes only its original ownTabs tab', async () => {
  const label = autostartLabel(10), project = 'https://chatgpt.com/g/fleet/project';
  const draft = (id) => state('', {url:project,lastRole:'',fleetDraftLabel:label,
    composerEmpty:false,composerPresent:true});
  const h = harness({tabs:[{...tab(1),url:project,active:false},
    {...tab(2),url:project,active:false}],states:{1:draft(1),2:draft(2)},workers:[],
    saved:{ownTabs:{1:label},launch:{chatHistoryVersion:2,history:[],
      launched:{[label]:{at:Date.now()-10*60_000,tabId:1,needsStart:true,preclaimed:true,
        reservation:{claim_id:100}}}},settings:{paceVersion:2,enabled:false,dryRun:false,
          autostart:false,lockTabs:false,projects:['P']}}});
  await h.tick();
  assert.deepEqual(h.removed,[1], 'only launch-owned tab is closed, same-URL human tab remains');
  assert.equal(h.data.ownTabs?.['1'],undefined);
  assert.match(h.data.status['1'].reason,/стартовый черновик не отправлен/);
});

test('W10-5: human project draft with a fleet label in DOM is never ownership proof', async () => {
  const label = autostartLabel(15), project = 'https://chatgpt.com/g/fleet/project';
  const h = harness({tabs:[{...tab(1),url:project,active:false}],
    states:{1:state('',{url:project,lastRole:'',fleetDraftLabel:label,composerEmpty:false})},
    workers:[],saved:{settings:{paceVersion:2,enabled:false,dryRun:false,
      autostart:false,lockTabs:false,projects:['P']}}});
  await h.tick();
  assert.deepEqual(h.removed,[], 'neither DOM draft nor project URL authorizes closure');
});

test('W10-5: exact expired launch tabId proves ownership even after ownTabs storage was lost', async () => {
  const label = autostartLabel(10), project = 'https://chatgpt.com/g/fleet/project';
  const h = harness({tabs:[{...tab(1),url:project,active:false},
    {...tab(2),url:project,active:false}],workers:[],
    states:{1:state('',{url:project,lastRole:'',composerEmpty:true}),
      2:state('',{url:project,lastRole:'',composerEmpty:true})},
    saved:{launch:{chatHistoryVersion:2,launched:{[label]:{at:Date.now()-600_000,
      tabId:1,needsStart:true,preclaimed:true}}},
      settings:{paceVersion:2,enabled:false,dryRun:false,autostart:false,projects:['P']}}});
  await h.tick();
  assert.deepEqual(h.removed,[1], 'launch tabId, unlike the shared URL, is sufficient provenance');
});

test('W11-2: discarded supervised tab uses another owned renderer, does not reload for unchanged update_time', async () => {
  const id='abcdefab-1', url='https://chatgpt.com/c/'+id;
  const label='f1118-1500abc';
  const h=harness({tabs:[{...tab(1),url,active:false,discarded:true,autoDiscardable:true},
      {...tab(2),active:false}],states:{2:state(label,{url:'https://chatgpt.com/c/abcdefab-2'})},
    workers:[worker(label)],overseen:[{url,mode:'send'}],conversationTimes:{[id]:'111'},
    saved:{ownTabs:{1:'owner-sv',2:label},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}},
      overseerRevisions:{[id]:{value:'111',checkedAt:Date.now()-100_000}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[], 'unchanged server revision does not allocate a heavyweight renderer');
  assert.ok(h.updated.some(x=>x.id===1 && x.autoDiscardable===false),'protected even after external discard');
  assert.ok(h.apiReads.some(r=>r.tabId===2 && r.chat===id),'only another owned renderer reads ChatGPT metadata');
  assert.equal(h.data.overseerRevisions[id].value,'111');
  assert.ok(!h.toolCalls.some(t=>t.name==='fleet_overseer_observe'),'no phantom new turn');
});

test('W11-2: changed stable update_time reloads discarded owner-sv once for a DOM snapshot', async () => {
  const id='abcdefab-1', url='https://chatgpt.com/c/'+id;
  const label='f1118-1500abc';
  const h=harness({tabs:[{...tab(1),url,active:false,discarded:true,autoDiscardable:true},
      {...tab(2),active:false}],states:{2:state(label,{url:'https://chatgpt.com/c/abcdefab-2'})},
    workers:[worker(label)],overseen:[{url,mode:'send'}],conversationTimes:{[id]:'222'},
    saved:{ownTabs:{1:'owner-sv',2:label},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}},
      overseerRevisions:{[id]:{value:'111',checkedAt:Date.now()-100_000}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[], 'first changed metadata reading only starts confirmation');
  assert.equal(h.data.overseerRevisions[id].candidate,'222');
  h.data.overseerRevisions[id].candidateAt=Date.now()-40_000;
  h.data.overseerRevisions[id].checkedAt=Date.now()-70_000;
  await h.tick();
  assert.deepEqual(h.reloaded,[1], 'second stable reading permits only supervised renderer reload');
  assert.equal(h.data.overseerRevisions[id].value,'222','revision committed after successful reload');
  assert.ok(!h.reloaded.includes(2),'other agent is only an API source');
});

test('W11-2: discarded supervised tab with no safe API source stays asleep', async () => {
  const id='abcdefab-1',url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),url,discarded:true,active:false,autoDiscardable:true},
      {...tab(2),active:false}],states:{2:state('',{url:'https://chatgpt.com/c/abcdefab-2'})},
    workers:[],overseen:[{url,mode:'shadow'}],conversationTimes:{[id]:'222'},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[], 'human renderer is never an API proxy');
  assert.deepEqual(h.apiReads,[]);
  assert.ok(!h.updated.some(x=>x.id===2),'human tab setting untouched');
});

test('24.11: supervised chat open in the owner tab is continued there — no second tab, nothing reloaded, not owned', async () => {
  // Было (W11-2): вкладку человека с тем же чатом не трогали, а надзор открывал свою. Решение
  // владельца 24.11: продолжать в его вкладке (на её месте и в её группе).
  const id='abcdefab-1', url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),url,active:false,autoDiscardable:true,groupId:4}],
    states:{1:state('owner',{url})},workers:[],overseen:[{url,mode:'send'}],serverAutostart:false,reservationEmpty:true,saved:{settings:{paceVersion:2,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:[]}}});
  await h.tick();
  assert.deepEqual(h.created,[]);
  assert.deepEqual(h.reloaded,[]);
  assert.deepEqual(h.removed,[]);
  assert.equal(h.data.labels.byTab['1'].label,'owner-sv');
  assert.notEqual((h.data.ownTabs||{})['1'],'owner-sv');
});

test('24.11: right after Chrome start the supervised chat is not opened again while tabs are being restored', async () => {
  const url='https://chatgpt.com/c/abcdefab-77';
  const h=harness({tabs:[],states:{},workers:[],overseen:[{url,mode:'send'}],serverAutostart:false,
    reservationEmpty:true,saved:{browserStartedAt:Date.now()-20_000,settings:{paceVersion:2,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:[]}}});
  await h.tick();
  assert.equal(h.createdProps.filter(p=>p.url===url).length,0);
});

test('24.11: our duplicate of a supervised chat is closed, the owner tab in its group keeps supervision', async () => {
  const id='abcdefab-1', url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),url,groupId:4,active:false},{...tab(2),url,groupId:-1,active:false}],
    states:{1:state('owner',{url}),2:state('owner-sv',{url})},workers:[],overseen:[{url,mode:'send'}],
    serverAutostart:false,reservationEmpty:true,
    saved:{ownTabs:{2:'owner-sv'},labels:{byTab:{2:{label:'owner-sv',path:id}},byChat:{}},settings:{paceVersion:2,enabled:true,dryRun:false,autostart:false,lockTabs:false,projects:[]}}});
  await h.tick();
  assert.ok(h.removed.includes(2));
  assert.ok(!h.removed.includes(1));
  assert.equal(h.data.labels.byTab['1'].label,'owner-sv');
  assert.deepEqual(h.created,[]);
});

test('W11-2: durable supervisor outbox wakes its own discarded tab even without an API source', async () => {
  const id='abcdefab-1',url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),url,discarded:true,active:false,autoDiscardable:true}],
    workers:[],overseen:[{url,mode:'send'}],
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}}},
      overseerPending:{[id]:{text:'⟦надзор⟧ Продолжи.',baseCount:4}}}});
  await h.tick();
  assert.deepEqual(h.reloaded,[1],'persisted command is enough to restore its own tab');
  assert.deepEqual(h.apiReads,[],'no new metadata request is required');
  assert.equal(h.data.overseerPending[id].baseCount,4,'outbox survives reload');
});

test('W11-2: failed server observation cannot advance live update_time baseline', async () => {
  const id='abcdefab-1',url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),url,active:false,autoDiscardable:false}],
    states:{1:state('owner-sv',{url})},workers:[],overseen:[{url,mode:'shadow'}],
    transcript:{ok:true,nMessages:2,lastRole:'assistant',lastAssistant:'Готово',userMessages:['Задача']},
    conversationTimes:{[id]:'222'},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}}}}});
  h.setOverseerMemo(id,{key:'2|6|Готово',changedAt:Date.now()-31_000,reported:false});
  await h.tick();
  assert.ok(h.toolCalls.some(c=>c.name==='fleet_overseer_observe'));
  assert.deepEqual(h.apiReads,[], 'invalid observe receipt never becomes a confirmed baseline');
  assert.equal(h.data.overseerRevisions?.[id],undefined);
});

test('W11-7: one conversation 429 with independent control 200 never pauses account or reloads tab', async () => {
  const id='abcdefab-1',now=Date.now();
  const h=harness({states:{1:state('f1',{pageLoadFailed:'Could not load this ChatGPT conversation'})},
    apiStatuses:{[id]:{status:429,retryAfterSec:240}},controlStatus:{status:200},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,loadFailFirstAt:now-90_000},
      reloadMem:{1:{since:now-10_000}}}});
  await h.tick();
  assert.equal(h.data.launch.overloadUntil || 0,0);
  assert.equal(h.data.launch.apiCheck[1].status,429);
  assert.ok(h.data.launch.apiCheck[1].nextAt>=now+240_000,'Retry-After outranks exponential backoff');
  assert.deepEqual(h.reloaded,[]);
  assert.equal(h.apiReads.filter(r=>!r.control).length,1);
  assert.equal(h.apiReads.filter(r=>r.control).length,1);
  assert.match(h.data.status['1'].reason,/429 разговора/);
  assert.equal(h.data.events.some(e=>e.type==='conversation-429'),true);
  assert.equal(h.data.events.some(e=>e.type==='account-overload'),false);
  await h.tick();
  assert.equal(h.apiReads.filter(r=>!r.control).length,1,'no polling during Retry-After');
  assert.deepEqual(h.reloaded,[]);
});

test('W11-7: independent conversations endpoint 429 confirms account-wide overload', async () => {
  const id='abcdefab-1';
  const h=harness({states:{1:state('f1',{pageLoadFailed:'Could not load this ChatGPT conversation'})},
    apiStatuses:{[id]:{status:429,retryAfterSec:21}},controlStatus:{status:429,retryAfterSec:20},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2}}});
  await h.tick();
  assert.equal(h.data.launch.overloadLevel,1);
  assert.ok(h.data.launch.overloadUntil>Date.now());
  assert.equal(h.data.launch.overloadReason,'conversation-429');
  assert.equal(h.data.events.some(e=>e.type==='account-overload'),true);
});

test('legacy account pause from a conversation 429 is rechecked before releasing another outbox', async () => {
  const now = Date.now(), source = { ...tab(1), status: 'complete' }, label = 'pult-q1';
  const next = { id: 'q2', label: 'pult-q2', url: tab(2).url, open: true, send: 'test delivery' };
  const simulate = (controlStatus) => harness({ tabs: [source], states: { 1: state(label) },
    workers: [], serverAutostart: false, quickJobs: [next], controlStatus,
    saved: { ownTabs: { 1: label }, launch: { chatHistoryVersion: 2, overloadLevel: 5,
      overloadUntil: now + 60_000, overloadReason: 'conversation-429', overloads: [now - 40_000] } } });

  const healthy = simulate({ status: 200 });
  await healthy.tick();
  assert.deepEqual(healthy.apiReads.map(read => read.control), [true]);
  assert.equal(healthy.data.launch.overloadUntil, 0);
  assert.equal(healthy.createdProps[0]?.url, next.url);

  const unknown = simulate(null);
  await unknown.tick();
  assert.equal(unknown.data.launch.overloadUntil, now + 60_000);
  assert.deepEqual(unknown.created, []);

  const limited = simulate({ status: 429, retryAfterSec: 900 });
  await limited.tick();
  assert.ok(limited.data.launch.overloadUntil >= now + 900_000);
  assert.ok(limited.data.launch.overloadControlAt >= now);
  assert.deepEqual(limited.created, []);
  await limited.tick();
  assert.equal(limited.apiReads.length, 1, 'a verified pause is preserved on the next tick');
});

test('two blocked outboxes cannot permanently occupy the low-memory delivery lane', async () => {
  const jobs = [1, 2, 3].map(n => ({ id: `q${n}`, label: `pult-q${n}`,
    url: tab(n).url, open: true, send: 'test delivery' }));
  const h = harness({ tabs: [tab(1), tab(2)], workers: [], serverAutostart: false, memoryFreeMB: 165,
    states: { 1: state(jobs[0].label, { generating: true }),
      2: state(jobs[1].label, { url: tab(2).url, generating: true }) }, quickJobs: jobs,
    saved: { ownTabs: { 1: jobs[0].label, 2: jobs[1].label },
      quickTabJobs: { 1: jobs[0].id, 2: jobs[1].id },
      accountActions: { lastActionAt: 0, history: [{ at: Date.now() - 301_000, type: 'quick-open' }] } } });
  await h.tick();
  assert.equal(h.createdProps[0]?.url, jobs[2].url);
  assert.deepEqual(h.removed, [], 'in-flight interfaces are preserved');
});

test('scoped dispatch opens only the approved payload and address and suppresses other launch paths', async () => {
  const send = 'reviewed local task';
  const job = { id: 'q1', label: 'pult-q1', url: tab(1).url, open: true, send };
  const excluded = { ...job, id: 'q2', label: 'pult-q2', url: tab(2).url };
  const ownerWake = { label: 'f2', url: tab(3).url, text: 'wake outside scope' };
  const saved = { quickDispatchScope: { q1: { messages: [createHash('sha256').update(send).digest('hex')],
    home: '', url: job.url } } };
  const run = (candidate) => harness({ tabs: [], workers: [], quickJobs: [excluded, candidate],
    wake: [ownerWake], overseen: [{ url: tab(4).url, mode: 'send' }], saved });

  const approved = run(job);
  await approved.tick();
  assert.deepEqual(approved.createdProps.map(props => props.url), [job.url]);
  assert.match(approved.data.quickOpenBlocked.q2, /вне разрешённого набора/);

  for (const candidate of [{ ...job, send: 'changed payload' }, { ...job, url: tab(5).url }]) {
    const denied = run(candidate);
    await denied.tick();
    assert.deepEqual(denied.createdProps, []);
    assert.match(denied.data.quickOpenBlocked.q1, /вне разрешённого набора/);
  }
});

test('low memory does not reserve delivery time for excluded jobs or a backed-off conversation', async () => {
  const now = Date.now(), send = 'reviewed local task';
  const [limited, excluded, next] = [1, 2, 3].map(n => ({ id: `q${n}`, label: `pult-q${n}`,
    url: tab(n).url, open: true, send }));
  const approved = (job) => ({ messages: [createHash('sha256').update(send).digest('hex')], home: '', url: job.url });
  const h = harness({ tabs: [tab(1), tab(2)], workers: [], memoryFreeMB: 390,
    quickJobs: [limited, excluded, next], states: {
      1: state(limited.label, { pageLoadFailed: 'Could not load this ChatGPT conversation' }),
      2: state(excluded.label, { url: excluded.url }),
    }, saved: { quickDispatchScope: { q1: approved(limited), q3: approved(next) },
      ownTabs: { 1: limited.label, 2: excluded.label },
      launch: { chatHistoryVersion: 2, apiCheck: { 1: { conv: 'abcdefab-1', status: 429,
        at: now - 1000, nextAt: now + 120_000 } } },
      accountActions: { lastActionAt: 0, history: [{ at: now - 30_000, type: 'quick-open' }] } } });
  await h.tick();
  assert.deepEqual(h.createdProps.map(props => props.url), [next.url]);
  assert.deepEqual(h.reloaded, []);
  assert.deepEqual(h.actions.filter(action => action.type === 'act'), []);
});

test('W11-7: a cooled conversation API 200 clears the 429 block without global recovery', async () => {
  const id='abcdefab-1',now=Date.now();
  const h=harness({states:{1:state('f1',{pageLoadFailed:'Could not load this ChatGPT conversation'})},
    apiStatuses:{[id]:{status:200}},
    saved:{ownTabs:{1:'f1'},launch:{chatHistoryVersion:2,apiCheck:{
      1:{conv:id,status:429,at:now-300_000,first429At:now-300_000,nextAt:now-1}}},
      reloadMem:{1:{since:now-10_000}}}});
  await h.tick();
  assert.equal(h.data.launch.apiCheck[1].status,200);
  assert.equal(h.data.launch.apiCheck[1].first429At,undefined);
  assert.ok(!h.data.launch.overloadUntil);
  assert.deepEqual(h.reloaded,[1]);
  assert.equal(h.apiReads.some(r=>r.control),false,'200 does not need a second control request');
});

test('W11-7: 429 exponential pacing, 200 recovery, and prolonged unavailability', () => {
  const id='abcdefab-1',base=10_000_000;
  const first=recovery.conversationApiCheck(null,{status:429,retryAfterSec:300},base,id);
  assert.equal(first.nextAt,base+300_000);
  const second=recovery.conversationApiCheck(first,{status:429,retryAfterSec:0},first.nextAt,id);
  assert.equal(second.nextAt,first.nextAt+240_000);
  const third=recovery.conversationApiCheck(second,{status:429},second.nextAt,id);
  assert.equal(third.nextAt,second.nextAt+480_000);
  const fourth=recovery.conversationApiCheck(third,{status:429},third.nextAt,id);
  assert.equal(fourth.nextAt,third.nextAt+960_000);
  const fifth=recovery.conversationApiCheck(fourth,{status:429},fourth.nextAt,id);
  assert.equal(fifth.nextAt,fourth.nextAt+1_800_000);
  assert.equal(recovery.conversation429Unavailable(fifth,base+30*60_000),true);
  assert.ok(recovery.conversation429Backoff(fifth,fifth.at+1000)>0);
  assert.equal(recovery.conversationApiCheck(fifth,{status:200},fifth.nextAt,id).status,200);
  assert.equal(recovery.conversation429Unavailable(
    recovery.conversationApiCheck(fifth,{status:200},fifth.nextAt,id),fifth.nextAt),false);
});

test('19.11 incident: consumed dispatch scope retires itself and new quick work opens again', async () => {
  const sha = (text) => createHash('sha256').update(text).digest('hex');
  const started = { id: 'q1', label: 'pult-q1', url: tab(1).url, open: true, send: 'follow-up from customer',
    status: 'answered-unparked' };
  const fresh = { id: 'q2', label: 'pult-q2', url: '', home: '', open: true, send: 'brand new task', status: 'new' };
  const h = harness({ tabs: [], workers: [], quickJobs: [started, fresh], serverAutostart: false,
    saved: { quickDispatchScope: { q1: { messages: [sha('approved first message')], home: '', url: '' } } } });
  await h.tick();
  assert.equal(h.data.quickDispatchScope, null, 'scope whose approvals are all delivered is retired');
  assert.ok(!Object.values(h.data.quickOpenBlocked || {}).some((reason) => /вне разрешённого набора/.test(reason)));
  assert.ok(h.createdProps.length >= 1, 'a waiting quick job gets an interface in the same tick');
});

test('dispatch scope expires after its window even if an approved message is stuck', async () => {
  const sha = (text) => createHash('sha256').update(text).digest('hex');
  const job = { id: 'q1', label: 'pult-q1', url: tab(1).url, open: true, send: 'reviewed' };
  const other = { id: 'q2', label: 'pult-q2', url: tab(2).url, open: true, send: 'other' };
  const scope = { q1: { messages: [sha('reviewed')], home: '', url: job.url } };
  const active = harness({ tabs: [], workers: [], quickJobs: [job, other], serverAutostart: false,
    saved: { quickDispatchScope: scope } });
  await active.tick();
  assert.match(active.data.quickOpenBlocked.q2, /вне разрешённого набора/);
  assert.ok(active.data.quickDispatchScopeMeta.until > Date.now(), 'a legacy scope gets a deadline on first sight');
  const expired = harness({ tabs: [], workers: [], quickJobs: [job, other], serverAutostart: false,
    saved: { quickDispatchScope: scope, quickDispatchScopeMeta: { since: Date.now() - 31 * 60_000 } } });
  await expired.tick();
  assert.equal(expired.data.quickDispatchScope, null);
  assert.ok(!/вне разрешённого набора/.test(expired.data.quickOpenBlocked?.q2 || ''));
});

test('quick open block reason reaches the server once per change (pause_alarms shows it)', async () => {
  const sha = (text) => createHash('sha256').update(text).digest('hex');
  const job = { id: 'q1', label: 'pult-q1', url: tab(1).url, open: true, send: 'reviewed' };
  const other = { id: 'q2', label: 'pult-q2', url: tab(2).url, open: true, send: 'other' };
  const h = harness({ tabs: [], workers: [], quickJobs: [job, other], serverAutostart: false,
    saved: { quickDispatchScope: { q1: { messages: [sha('reviewed')], home: '', url: job.url } } } });
  const pauses = () => h.toolCalls.filter((call) => call.name === 'fleet_quick_report')
    .map((call) => JSON.parse(call.args.report)).filter((report) => report.id === 'q2' && report.send_paused === true);
  await h.tick();
  assert.equal(pauses().length, 1);
  assert.match(pauses()[0].send_pause_reason, /расширение: отправка вне разрешённого набора/);
  await h.tick();
  assert.equal(pauses().length, 1, 'unchanged reason is not re-reported every tick');
});

test('liveness ladder: stalled quick work clears local pauses, then frees its own idle slots', async () => {
  const job = { id: 'q1', label: 'pult-q1', url: tab(1).url, open: true, send: 'stuck message' };
  const pausedUntil = Date.now() + 10 * 60_000;
  const base = { ownTabs: { 1: job.label }, quickTabJobs: { 1: job.id },
    quickSendFailures: { q1: { count: 3, reason: 'нет активной кнопки Send', at: Date.now(), pausedUntil } } };
  const clear = harness({ tabs: [tab(1)], workers: [], quickJobs: [job], serverAutostart: false,
    states: { 1: state(job.label, { url: job.url }) },
    saved: { ...base, quickLiveness: { since: Date.now() - 16 * 60_000, step: 1 } } });
  await clear.tick();
  assert.equal(clear.data.quickLiveness.lastAction, 'clear-local');
  assert.deepEqual(clear.data.quickSendFailures, {});
  const free = harness({ tabs: [tab(1)], workers: [], quickJobs: [job], serverAutostart: false,
    states: { 1: state(job.label, { url: job.url }) },
    saved: { ...base, quickLiveness: { since: Date.now() - 21 * 60_000, step: 2 } } });
  await free.tick();
  assert.equal(free.data.quickLiveness.lastAction, 'free-slots');
  assert.ok(free.removed.includes(1), 'own non-generating quick tab is closed to free its slot');
  assert.equal(free.data.ownTabs[1], undefined);
  const busy = harness({ tabs: [tab(1)], workers: [], quickJobs: [job], serverAutostart: false,
    states: { 1: state(job.label, { url: job.url, generating: true }) },
    saved: { ...base, quickLiveness: { since: Date.now() - 21 * 60_000, step: 2 } } });
  await busy.tick();
  assert.equal(busy.data.quickLiveness.step, 0, 'a generating quick tab is progress, not a stall');
  assert.deepEqual(busy.removed, []);
});

test('W11-7: 30-minute quick conversation 429 is blocked, not a working agent', async () => {
  const now=Date.now(),label='pult-q16',id='abcdefab-1',url='https://chatgpt.com/c/'+id;
  const h=harness({tabs:[{...tab(1),active:false}],workers:[],
    states:{1:state(label,{url,pageLoadFailed:'Could not load this ChatGPT conversation'})},
    quickJobs:[{id:'q16',label,url,send:'pending',open:true,idle:false}],
    saved:{ownTabs:{1:label},quickTabJobs:{1:'q16'},
      launch:{chatHistoryVersion:2,apiCheck:{1:{conv:id,status:429,at:now-31*60_000,
        first429At:now-31*60_000,consecutive:5,nextAt:now+600_000}}}}});
  await h.tick();
  assert.match(h.data.quickOpenBlocked.q16,/разговор недоступен \(429\)/);
  assert.equal(h.data.status['1'].action,'quick-blocked');
  assert.equal(h.data.fleetQuickStates.active,0,'broken conversation cannot count as a working quick agent');
  assert.deepEqual(h.reloaded,[]);
  assert.equal(h.updated.some(x=>x.url===url),false);
  assert.equal(h.reports.some(r=>r.error_class==='conversation-429'),false,
    'server reports are carried through quickReport rather than a direct extension payload');
});


test('21.11: чат под надзором исчерпан — экспорт, новый чат в том же проекте, файлы, «продолжи», надзор на преемника, старая вкладка закрыта', async () => {
  const projUrl = 'https://chatgpt.com/g/g-p-abc-proj/c/abcdefab-1', id = 'abcdefab-1';
  const conversation = { title: 'Оркестрация ниш', current_node: 'b', mapping: {
    a: { id: 'a', parent: null, children: ['b'], message: { author: { role: 'user' }, content: { parts: ['задание владельца'] } } },
    b: { id: 'b', parent: 'a', children: [], message: { author: { role: 'assistant' }, content: { parts: ['последний ответ'] } } } } };
  const states = {
    1: state('owner-sv', { url: projUrl, conversationFull: "You've reached the maximum length for this conversation" }),
    100: state('new', { url: 'https://chatgpt.com/g/g-p-abc-proj/project', lastRole: '' }),
  };
  const h = harness({ tabs: [{ ...tab(1), url: projUrl, active: true }], states, workers: [], serverAutostart: false, reservationEmpty: true,
    overseen: [{ chat: id, url: projUrl, mode: 'send', engine: 'proxy' }],
    exportPage: { ok: true, status: 200, text: JSON.stringify(conversation) },
    saved: { settings: { paceVersion: 2, enabled: true, dryRun: false, autostart: false, lockTabs: false, projects: [] },
             ownTabs: { 1: 'owner-sv' }, labels: { byTab: { 1: { label: 'owner-sv', path: id } }, byChat: { [id]: 'owner-sv' } } } });
  await h.tick();
  assert.equal(h.toolCalls.filter(c => c.name === 'fleet_chat_handoff').length, 0, 'чат под надзором не передаётся заранее (85 %)');
  assert.ok(h.createdProps.some(p => p.url === 'https://chatgpt.com/g/g-p-abc-proj/project'), JSON.stringify(h.createdProps));
  const attach = h.actions.find(a => a.action === 'attach-files');
  assert.ok(attach, JSON.stringify(h.data.fullHandoffs));
  assert.equal(attach.id, 100);
  assert.equal(JSON.stringify(attach.files.map(f => f.type)), JSON.stringify(['application/json', 'text/markdown']));
  assert.match(attach.files[0].text, /"conversation":\{"title":"Оркестрация ниш"/);
  assert.match(attach.files[1].text, /задание владельца[\s\S]*последний ответ/);
  const sent = h.actions.find(a => a.action === 'continue' && a.id === 100);
  assert.ok(sent && /Погрузись в контекст/.test(sent.text) && sent.text.includes('[fleet-continuation:abcdefab]'));
  assert.equal(h.data.ownTabs['100'], 'owner-sv-next', 'до регистрации преемник не закрывается как «надзор отключён»');
  assert.ok(!h.removed.includes(100));

  states[100].url = 'https://chatgpt.com/g/g-p-abc-proj/c/abcdef01-2222';
  await h.tick();
  const marks = h.toolCalls.filter(c => c.name === 'fleet_overseer_mark');
  assert.ok(marks.some(c => c.args.url === 'https://chatgpt.com/c/abcdef01-2222' && c.args.mode === 'send' && c.args.engine === 'proxy'), JSON.stringify(marks));
  assert.ok(marks.some(c => c.args.url === projUrl && c.args.mode === 'off'));
  assert.ok(h.removed.includes(1), 'старая вкладка закрыта');
  assert.ok(!h.removed.includes(100));
  assert.equal(h.data.ownTabs['100'], 'owner-sv');
  assert.equal(h.data.fullHandoffs[id].done, true);
});

test('21.11: режим «вписать, не отправлять» — файлы и текст в поле, Send не нажат, старая вкладка остаётся', async () => {
  const projUrl = 'https://chatgpt.com/c/abcdefab-1', id = 'abcdefab-1';
  const states = { 1: state('owner-sv', { url: projUrl, conversationFull: 'maximum conversation length' }),
    100: state('new', { url: 'https://chatgpt.com/', lastRole: '' }) };
  const h = harness({ tabs: [{ ...tab(1), url: projUrl }], states, workers: [], serverAutostart: false, reservationEmpty: true,
    overseen: [{ chat: id, url: projUrl, mode: 'send' }],
    exportPage: { ok: true, status: 200, text: JSON.stringify({ title: 'T', current_node: null, mapping: {} }) },
    saved: { settings: { paceVersion: 2, enabled: true, dryRun: false, autostart: false, lockTabs: false, projects: [],
                         fullHandoffDraftOnly: true },
             ownTabs: { 1: 'owner-sv' }, labels: { byTab: { 1: { label: 'owner-sv', path: id } }, byChat: { [id]: 'owner-sv' } } } });
  await h.tick();
  assert.ok(h.createdProps.some(p => p.url === 'https://chatgpt.com/'));
  assert.ok(h.actions.some(a => a.action === 'draft' && a.id === 100));
  assert.ok(!h.actions.some(a => a.action === 'continue' && a.id === 100));
  assert.ok(!h.removed.includes(1));
});

test('21.11: исчерпанный чат в режиме «тень» не переносится; чат вне надзора — тоже', async () => {
  const url = 'https://chatgpt.com/c/abcdefab-1', id = 'abcdefab-1';
  const h = harness({ tabs: [tab(1)], states: { 1: state('owner-sv', { url, conversationFull: 'maximum chat length' }) },
    workers: [], overseen: [{ chat: id, url, mode: 'shadow' }],
    exportPage: { ok: true, status: 200, text: '{"mapping":{}}' },
    saved: { ownTabs: { 1: 'owner-sv' }, labels: { byTab: { 1: { label: 'owner-sv', path: id } }, byChat: { [id]: 'owner-sv' } } } });
  await h.tick();
  assert.equal(h.data.fullHandoffs[id].newTabId, undefined, 'новый чат не открыт');
  assert.ok(!h.actions.some(a => a.action === 'attach-files'));
  assert.equal(h.data.fullHandoffs[id].skipped, 'режим shadow');
  const free = harness({ tabs: [tab(1)], states: { 1: state('', { url, conversationFull: 'maximum chat length' }) }, workers: [] });
  await free.tick();
  assert.equal(JSON.stringify(free.data.fullHandoffs || {}), '{}');
});

test('25.11 jev-1: a new chat message after the verdict cancels the undelivered intervention', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1',text='⟦надзор⟧ Продолжай работу над исходной целью.';
  const h=harness({tabs:[{...tab(1),active:false,autoDiscardable:true}],states:{1:state('owner-sv',{url})},
    workers:[],overseen:[{url,mode:'send'}],outcome:{ok:true},
    transcript:{ok:true,nMessages:3,lastRole:'assistant',lastAssistant:'Принял новое задание',
      userMessages:['Задача','Владелец: займись Android']},
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  h.setOverseerMemo(id,{key:'v',changedAt:Date.now()-60_000,reported:true,lastTranscriptCount:1,
    result:{action:'send',text,verdict:'continue',key:'v'}});
  await h.tick();
  assert.equal(h.actions.filter(a=>a.action==='continue').length,0,'stale intervention is not sent over the owner turn');
  assert.equal(h.data.overseerPending[id],undefined);
});

test('25.11 jev-1: server reobserve token triggers exactly one re-evaluation of the same turn', async()=>{
  const url='https://chatgpt.com/c/abcdefab-1',id='abcdefab-1';
  const t={ok:true,nMessages:2,lastRole:'assistant',lastAssistant:'Жду RAM-gate',lastAssistantId:'msg-abc123',
    userMessages:['Задача']};
  const h=harness({tabs:[{...tab(1),active:false}],states:{1:state('owner-sv',{url})},workers:[],
    overseen:[{url,mode:'send',reobserve:'w1'}],transcript:t,
    saved:{ownTabs:{1:'owner-sv'},labels:{byTab:{1:{label:'owner-sv',path:id}},byChat:{[id]:'owner-sv'}}}});
  h.setOverseerMemo(id,{key:`2|${t.lastAssistant.length}|${t.lastAssistant}`,changedAt:Date.now()-120_000,reported:true});
  await h.tick();
  const calls=()=>h.toolCalls.filter(c=>c.name==='fleet_overseer_observe');
  assert.equal(calls().length,1);
  assert.equal(JSON.parse(calls()[0].args.snapshot).last_assistant_id,'msg-abc123');
  await h.tick();
  assert.equal(calls().length,1,'same token does not re-trigger');
});
