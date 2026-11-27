import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../reload-extension.mjs',import.meta.url),'utf8')
  .replace(/^import .*;\r?\n/m,'')
  .replace('main().catch(', 'globalThis.finished=main().catch(');

async function helper(args,saved={},failure='',runtimeHealth=null) {
  const output=[], alarms=[], effects=[];
  let sockets=0, reads=0;
  const context=vm.createContext({
    process:{argv:['node','helper',...args],exitCode:0},
    console:{log:v=>output.push(v),error:v=>output.push(v)},
    setTimeout,clearTimeout,AbortSignal,Date,JSON,fleetRuntimeHealth:runtimeHealth,
    fetch:async()=>({ok:true,json:async()=>[
      {type:'service_worker',url:'chrome-extension://fleet/background.js',webSocketDebuggerUrl:'ws://worker'},
    ]}),
    chrome:{runtime:{getManifest:()=>({name:'Вкладки флота',version:'0.19.7'}),reload:()=>effects.push('reload')},
      storage:{local:{get:async keys=>{reads++;return Object.fromEntries(keys.map(k=>[k,saved[k]]));}}},
      alarms:{get:async()=>null,create:async(name,info)=>alarms.push({name,...info})}},
  });
  context.WebSocket=class {
    constructor() {
      this.number=++sockets;
      queueMicrotask(()=>failure==='connect'&&this.number===2
        ? this.onerror?.({message:'CDP websocket error'}) : this.onopen?.());
    }
    send(data) {
      const request=JSON.parse(data);
      Promise.resolve(vm.runInContext(request.params.expression,context)).then(value=>{
        if(failure==='close'&&this.number===2) this.onclose?.();
        else this.onmessage?.({data:JSON.stringify({id:1,result:{result:{value}}})});
      });
    }
    close(){this.onclose?.();}
  };
  vm.runInContext(source,context);
  await context.finished;
  return {output,alarms,effects,reads,code:context.process.exitCode};
}

test('health reads actual cycle progress, including disabled/failing cycles and an in-flight start',async()=>{
  for(const field of ['lastTickAt','lastLocalRecoveryAt','lastCycleStartedAt','lastCycleFinishedAt']) {
    const result=await helper(['--tick-age'],{lastTickAt:1,[field]:Date.now()-1000});
    assert.equal(result.code,0);
    const age=Number(result.output[0].split('=')[1]);
    assert.ok(age>=1&&age<5,field);
    assert.equal(result.reads,1);
    assert.deepEqual(result.effects,[]);
    assert.deepEqual(result.alarms,[]);
  }
});

test('uninitialised cycle is distinguishable from a failed probe',async()=>{
  assert.deepEqual((await helper(['--tick-age'])).output,['extension_tick_age_s=missing']);
  const failed=await helper(['--tick-age'],{},'connect');
  assert.equal(failed.code,1);
  assert.ok(!failed.output.some(x=>x.startsWith('extension_tick_age_s=')));
});

test('a storage failure cannot be hidden by a fresh completion clock',async()=>{
  const result=await helper(['--tick-age'],{lastCycleFinishedAt:Date.now()},'',
    {storageError:'FILE_ERROR_NO_SPACE'});
  assert.deepEqual(result.output,['extension_tick_age_s=missing']);
});

test('wake repairs alarms and preserves extension and browser',async()=>{
  const result=await helper(['--wake']);
  assert.deepEqual(result.output,['extension_wake=requested']);
  assert.deepEqual(result.alarms.map(x=>x.name),['tick','tick-soon']);
  assert.equal(result.alarms[0].periodInMinutes,0.5);
  assert.deepEqual(result.effects,[]);
});

test('reload connection failure is not falsely acknowledged as a successful reload',async()=>{
  const failed=await helper([],{},'connect');
  assert.equal(failed.code,1);
  assert.deepEqual(failed.effects,[]);
  assert.ok(!failed.output.some(x=>x.startsWith('extension_reload=requested')));
  const closed=await helper([],{},'close');
  assert.equal(closed.code,0);
  assert.deepEqual(closed.effects,['reload']);
});
