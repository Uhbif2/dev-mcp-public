import {test} from "node:test";
import assert from "node:assert/strict";
import {parseOwnerNotices,parseOwnerChannel,deliverOwnerNotices,installOwnerNoticeClicks,recordServerAvailability,markOwnerPanelOpened} from "../owner-notices.js";
const id="on-"+"a".repeat(24);

function fake() {
  const data={},active={},calls=[],tabs=[];
  let click;
  const api={storage:{local:{get:async(key)=>({[key]:structuredClone(data[key])}),
    set:async(values)=>Object.assign(data,structuredClone(values))}},
    notifications:{getPermissionLevel:async()=>"granted",getAll:async()=>({...active}),
      create:async(key,options)=>{calls.push(options);active[key]={};return key;},
      clear:async(key)=>{delete active[key];return true;},onClicked:{addListener:(fn)=>{click=fn;}}},
    tabs:{create:async(options)=>tabs.push(options)},runtime:{getURL:(path)=>"chrome-extension://fleet/"+path}};
  return {api,data,active,calls,tabs,click:()=>click};
}

test("acceptance survives lost receipt and MV3 restart without duplicate toast",async()=>{
  const f=fake(),reports=[];
  await deliverOwnerNotices([{id,kind:"needs"}],async()=>{throw new Error("offline")},f.api,100);
  assert.equal(f.calls.length,1);
  assert.equal(f.data.ownerNoticeDelivery[id].pendingAck,true);
  await deliverOwnerNotices([{id,kind:"needs"}],async(r)=>reports.push(r),f.api,200);
  assert.equal(f.calls.length,1);
  assert.deepEqual(reports,[{id,state:"api_accepted"}]);
  assert.equal(f.data.ownerNoticeDelivery[id].pendingAck,false);
  assert.ok(!JSON.stringify(f.calls).includes("private"));
});

test("denied permission stays pending and never produces acceptance",async()=>{
  const f=fake(),reports=[]; f.api.notifications.getPermissionLevel=async()=>"denied";
  await deliverOwnerNotices([{id,kind:"needs"}],async(r)=>reports.push(r),f.api,100);
  assert.equal(f.calls.length,0); assert.equal(reports[0].state,"denied");
  assert.notEqual(f.data.ownerNoticeDelivery[id].state,"api_accepted");
});

test("click opens packaged dashboard and reports only clicked",async()=>{
  const f=fake(),reports=[];
  installOwnerNoticeClicks(async(r)=>reports.push(r),f.api);
  await f.click()(id);
  assert.deepEqual(f.tabs,[{url:"chrome-extension://fleet/dashboard/index.html"}]);
  assert.deepEqual(reports,[{id,state:"clicked"}]);
});

test("parser refuses arbitrary URLs and unrelated fields",()=>{
  assert.deepEqual(parseOwnerNotices('owner-notice: '+JSON.stringify({id,kind:"needs"})),[{id,kind:"needs"}]);
  assert.deepEqual(parseOwnerNotices('owner-notice: '+JSON.stringify({id,kind:"needs",url:"https://evil/"})),[]);
});

test("three server failures show one alert and recovery clears it",async()=>{
  const f=fake();
  await recordServerAvailability(false,f.api);await recordServerAvailability(false,f.api);
  assert.equal(f.calls.length,0);
  await recordServerAvailability(false,f.api);await recordServerAvailability(false,f.api);
  assert.equal(f.calls.length,1);
  await recordServerAvailability(true,f.api);assert.equal(f.active["fleet-connectivity"],undefined);
});

test("disabled Windows banners use taskbar attention without changing focus",async()=>{
  const f=fake(),updates=[],reports=[];
  f.api.windows={getAll:async()=>[{id:7}],update:async(id,info)=>updates.push({id,info})};
  await deliverOwnerNotices([{id,kind:"needs"}],async(r)=>reports.push(r),f.api,100,true);
  assert.equal(f.calls.length,0);
  assert.deepEqual(updates,[{id:7,info:{drawAttention:true}}]);
  assert.deepEqual(reports,[{id,state:"browser_attention"}]);
  assert.deepEqual(parseOwnerChannel("owner-channel: attention=3; native=blocked"),{nativeBlocked:true,attention:3});
  await markOwnerPanelOpened(async(r)=>reports.push(r),f.api);
  assert.deepEqual(reports.at(-1),{id,state:"clicked"});
});

test("a previously accepted hidden banner gets browser attention exactly once",async()=>{
  const f=fake(),updates=[],reports=[];
  await deliverOwnerNotices([{id,kind:"needs"}],async()=>{},f.api,100);
  f.api.windows={getAll:async()=>[{id:7}],update:async(id,info)=>updates.push({id,info})};
  await deliverOwnerNotices([{id,kind:"needs"}],async(r)=>reports.push(r),f.api,60101,true);
  await deliverOwnerNotices([{id,kind:"needs"}],async(r)=>reports.push(r),f.api,120102,true);
  assert.equal(f.calls.length,1);
  assert.deepEqual(updates,[{id:7,info:{drawAttention:true}}]);
  assert.deepEqual(reports,[{id,state:"browser_attention"}]);
});
