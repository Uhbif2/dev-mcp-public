import {writeFileSync, mkdirSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

// A unique canary never overwrites a setting or masks a failed work cycle.
export async function probeStorage(api, timeoutMs = 2000) {
  const within = async operation => {
    let timer;
    try { return await Promise.race([operation,new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(Error('storage probe timed out')),timeoutMs);
    })]); } finally { clearTimeout(timer); }
  };
  const key='__fleet_storage_probe_'+Date.now()+'_'+Math.random().toString(36).slice(2);
  const nonce=key; let written=false;
  try {
    await within(api.storage.local.set({[key]:nonce})); written=true;
    const saved=await within(api.storage.local.get(key));
    if(saved[key]!==nonce) throw Error('storage write readback mismatch');
    await within(api.storage.local.remove(key)); written=false;
    return {state:'ok',observedAt:Date.now()};
  } catch(error) {
    const detail=String(error?.message||error).slice(0,1200);
    return {state:/FILE_ERROR_NO_SPACE|IO error:.*(?:\.ldb|\.log|ChromeMethod)/i.test(detail)
      ? 'io-error':'unknown',detail,observedAt:Date.now()};
  } finally {
    if(written) await within(api.storage.local.remove(key)).catch(()=>{});
  }
}

async function cdp(url,method,params={}) {
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);let sent=false,settled=false;
    const finish=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);ws.close();error?reject(error):resolve(value)};
    const timer=setTimeout(()=>finish(Error(method+' timed out')),10000);
    ws.onopen=()=>{sent=true;ws.send(JSON.stringify({id:1,method,params}))};
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id!==1)return;
      finish(m.error||m.result?.exceptionDetails?Error(JSON.stringify(m.error||m.result.exceptionDetails)):null,m.result)};
    ws.onerror=()=>finish(Error('CDP connection failed'));
    ws.onclose=()=>finish(method==='Browser.close'&&sent?null:Error('CDP target closed'),{});
  });
}

async function main() {
  const base='http://localhost:9223';
  if(process.argv.includes('--close')) {
    const version=await(await fetch(base+'/json/version',{signal:AbortSignal.timeout(3000)})).json();
    await cdp(version.webSocketDebuggerUrl,'Browser.close');
    console.log(JSON.stringify({state:'close-requested'}));return;
  }
  const targets=await(await fetch(base+'/json',{signal:AbortSignal.timeout(3000)})).json();
  const target=targets.find(t=>t.type==='service_worker'&&
    t.url==='chrome-extension://ogckiochhclkelamkoidolokkaealonl/background.js');
  if(!target)throw Error('dedicated Fleet Tabs worker missing');
  const exportIndex=process.argv.indexOf('--export');
  const expression=exportIndex>=0?'chrome.storage.local.get(null)':`(${probeStorage.toString()})(chrome)`;
  const result=await cdp(target.webSocketDebuggerUrl,'Runtime.evaluate',{
    expression,awaitPromise:true,returnByValue:true});
  const data=result.result?.value;
  if(!data||typeof data!=='object')throw Error('missing storage result');
  if(exportIndex>=0) {
    const dest=resolve(process.argv[exportIndex+1]);mkdirSync(dirname(dest),{recursive:true});
    writeFileSync(dest,JSON.stringify(data),{encoding:'utf8',flag:'wx'});
    console.log(JSON.stringify({state:'exported',keys:Object.keys(data).length,path:dest}));
  } else console.log(JSON.stringify(data));
}

if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  main().catch(error=>{console.error(JSON.stringify({state:'unknown',detail:String(error)}));process.exitCode=1});
}
