import test from 'node:test';
import assert from 'node:assert/strict';
import { callTool, fleetState, parseLaunchGate, reportExtension } from '../fleet.js';

test('fleet_list DOWN gate is parsed without making its read into a write', async () => {
  assert.deepEqual(parseLaunchGate('gate: chatgpt-reachable=down; launches=0'),
    {known:true,state:'down',allowed:false});
  // Exact live fleet_list line (tools.py): UP is a word, not a numeric count.
  assert.deepEqual(parseLaunchGate('gate: chatgpt-reachable=up; launches=allowed'),
    {known:true,state:'up',allowed:true});
  assert.deepEqual(parseLaunchGate('gate: chatgpt-reachable=unknown; launches=allowed'),
    {known:true,state:'unknown',allowed:true});
  assert.equal(parseLaunchGate('gate: chatgpt-reachable=up; launches=3').allowed, true);
  assert.equal(parseLaunchGate('gate: chatgpt-reachable=up; launches=0').allowed, false);
  assert.equal(parseLaunchGate('очередь: выдавать нечего').known, false);

  const calls = [];
  const before = globalThis.fetch;
  const response = (data) => ({
    ok: true, status: 200,
    headers: { get: key => key.toLowerCase() === 'mcp-session-id' ? 'test-session' : 'application/json' },
    async text() { return JSON.stringify(data); },
  });
  globalThis.fetch = async (_url, options) => {
    if (options.method === 'DELETE') return response({});
    const packet = JSON.parse(options.body);
    if (packet.method === 'tools/call') {
      calls.push(packet.params);
      return response({result:{content:[{type:'text',text:
        packet.params.name === 'fleet_list'
          ? 'gate: chatgpt-reachable=down; launches=0\nочередь: выдавать нечего'
          : 'extension lease updated; consecutive ChatGPT page failures: 1'}]}});
    }
    return response({result:{}});
  };
  try {
    const state = await fleetState('http://127.0.0.1:8766/servers/dev/mcp');
    assert.equal(state.launchGate.allowed, false);
    assert.deepEqual(calls.map(x => x.name), ['fleet_list'], 'queue read must not write heartbeat');
    await reportExtension('http://127.0.0.1:8766/servers/dev/mcp', {
      version:'0.18.2',tabs:4,pending:2,
      detail:'не найдено поле ввода',page_text:'Unable to load site — VPN',
    });
    assert.deepEqual(calls.map(x => x.name), ['fleet_list','fleet_extension_report']);
    assert.deepEqual(calls[1].arguments, {
      version:'0.18.2',tabs:4,pending:2,
      detail:'не найдено поле ввода',page_text:'Unable to load site — VPN',
    });
    await reportExtension('http://127.0.0.1:8766/servers/dev/mcp', {
      version:'0.20',tabs:1,pending:0,enabled:true,tick_completed:true,runnable:2,
    });
    assert.deepEqual(calls[2].arguments, {
      version:'0.20',tabs:1,pending:0,detail:'',page_text:'',
      enabled:true,tick_completed:true,runnable:2,
    });
    const account_event=JSON.stringify({type:'overload',at:1730000000000,
      chats_1h:2,chats_3h:4,chats_5h:6,pause_min:10});
    await reportExtension('http://127.0.0.1:8766/servers/dev/mcp', {
      version:'0.20',tabs:1,pending:0,account_event,
    });
    assert.equal(calls[3].arguments.account_event,account_event);
  } finally {
    globalThis.fetch = before;
  }
});

test('MCP cleanup has its own deadline and cancels its response body', async () => {
  const original=globalThis.fetch;
  let cleanupSignal, cancelled=false;
  globalThis.fetch=async (_url,options)=>{
    if(options.method==='DELETE'){
      cleanupSignal=options.signal;
      return {body:{cancel:async()=>{cancelled=true;}}};
    }
    const packet=JSON.parse(options.body);
    return {ok:true,headers:{get:k=>k==='mcp-session-id'?'session':'application/json'},
      text:async()=>JSON.stringify(packet.method==='tools/call'?{result:{content:[{text:'ok'}]}}:{result:{}})};
  };
  try{
    assert.equal(await callTool('http://local','fleet_list'),'ok');
    await new Promise(resolve=>setImmediate(resolve));
    assert.ok(cleanupSignal instanceof AbortSignal);
    assert.equal(cancelled,true);
  }finally{globalThis.fetch=original;}
});

test('a failed initialize body still cleans up the allocated session',async()=>{
  const original=globalThis.fetch;let cleaned=false;
  globalThis.fetch=async(_url,options)=>{
    if(options.method==='DELETE'){cleaned=true;assert.ok(options.signal);return {};}
    return {ok:true,headers:{get:k=>k==='mcp-session-id'?'session':'application/json'},
      text:async()=>{throw new Error('broken initialize body');}};
  };
  try{await assert.rejects(callTool('http://local','fleet_list'),/broken initialize body/);assert.equal(cleaned,true);}
  finally{globalThis.fetch=original;}
});
