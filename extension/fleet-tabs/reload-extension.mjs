import { drawOwnerAttention } from "./owner-notices.js";

const DEBUG_TARGETS = "http://127.0.0.1:9223/json";
const EXTENSION_NAME = "Вкладки флота";
const CONNECT_TIMEOUT_MS = 5000;

function cdpEvaluate(target, expression) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    const timer = setTimeout(() => {
      try { ws.close(); } catch {}
      reject(new Error("CDP timeout"));
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      ws.send(JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: { expression, returnByValue: true, awaitPromise: true },
      }));
    };
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      if (message.error || message.result?.exceptionDetails) {
        reject(new Error(JSON.stringify(message.error || message.result.exceptionDetails)));
      } else {
        resolve(message.result?.result?.value);
      }
      try { ws.close(); } catch {}
    };
    ws.onerror = (event) => {
      clearTimeout(timer);
      reject(new Error(String(event?.message || "CDP websocket error")));
    };
  });
}

async function main() {
  const response = await fetch(DEBUG_TARGETS, {signal:AbortSignal.timeout(CONNECT_TIMEOUT_MS)});
  if (!response.ok) throw new Error(`DevTools targets HTTP ${response.status}`);
  const targets = await response.json();
  const workers = targets.filter((target) =>
    target.type === "service_worker" &&
    String(target.url || "").startsWith("chrome-extension://")
  );

  for (const target of workers) {
    let info;
    try {
      const raw = await cdpEvaluate(
        target,
        "JSON.stringify({name:chrome.runtime.getManifest().name,version:chrome.runtime.getManifest().version})",
      );
      info = JSON.parse(String(raw || "{}"));
    } catch {
      continue;
    }
    if (info.name !== EXTENSION_NAME) continue;

    if (process.argv.includes("--diagnostics")) {
      const raw=await cdpEvaluate(target,`(async()=>{
        const saved=await chrome.storage.local.get(["lastTickAt","tickError","status","wakeMem","recoveryMem","sessions","ownTabs","launch","interfaceQueue","accountActions"]);
        const tabs=await chrome.tabs.query({url:"https://chatgpt.com/*"});
        const rows=await Promise.all(tabs.map(async t=>{
          let p=null; try { p=await Promise.race([chrome.tabs.sendMessage(t.id,{type:"probe"}),new Promise(r=>setTimeout(()=>r(null),1500))]); } catch {}
          return {id:t.id,loading:t.status,discarded:!!t.discarded,frozen:!!t.frozen,
            label:saved.ownTabs?.[t.id]||p?.label||"",reply:!!p,generating:p?.generating,
            composer:p?.composerPresent,pageFailure:p?.pageLoadFailed||"",accountFailure:p?.accountLoadFailed||"",
            recovery:!!saved.recoveryMem?.[t.id],wakeAgeSec:saved.wakeMem?.[t.id]?Math.round((Date.now()-saved.wakeMem[t.id].at)/1000):null,
            action:saved.status?.[t.id]?.action,reason:saved.status?.[t.id]?.reason};
        }));
        return JSON.stringify({version:chrome.runtime.getManifest().version,tickAgeSec:Math.round((Date.now()-(saved.lastTickAt||0))/1000),error:saved.tickError||"",queuedInterfaces:Object.keys(saved.interfaceQueue||{}).length,navigation:saved.accountActions?.history?.slice(-4),pendingStarts:Object.keys(saved.launch?.launched||{}).length,tabs:rows});
      })()`);
      console.log(raw); return;
    }

    if (process.argv.includes("--owner-attention-probe")) {
      const raw = await cdpEvaluate(target, `(async()=>{
        // MV3 forbids dynamic import in a service worker. Execute the actual
        // production function's source with this worker's Chrome API instead.
        const attention=${drawOwnerAttention.toString()};
        const before=(await chrome.windows.getAll({windowTypes:["normal"]})).some(w=>w.focused);
        await attention(chrome);
        const after=(await chrome.windows.getAll({windowTypes:["normal"]})).some(w=>w.focused);
        return JSON.stringify({taskbar_request:true,chrome_focused_before:before,chrome_focused_after:after});
      })()`);
      console.log("extension_owner_attention_probe="+raw);
      console.log("extension_version="+info.version);
      return;
    }

    if (process.argv.includes("--notification-probe")) {
      const raw = await cdpEvaluate(target, `(async()=>{
        const permission=await chrome.notifications.getPermissionLevel();
        if(permission!=="granted") return JSON.stringify({permission,accepted:false});
        const id="fleet-channel-check";
        const returned=await chrome.notifications.create(id,{type:"basic",iconUrl:"icon128.png",
          title:"Канал уведомлений флота",message:"Проверка системной доставки уведомлений.",priority:1});
        const all=await chrome.notifications.getAll();
        const listed=Object.hasOwn(all,id);
        const cleared=await chrome.notifications.clear(id);
        return JSON.stringify({permission,accepted:returned===id,listed,cleared});
      })()`);
      console.log("extension_notification_probe="+raw);
      console.log("extension_version="+info.version);
      return;
    }

    // Health mode is read-only. Using extension storage (not fleet_list lease)
    // catches a suspended/hung MV3 work loop even while other heartbeats arrive.
    if (process.argv.includes("--tick-age")) {
      const rawTick = await cdpEvaluate(
        target,
        "chrome.storage.local.get(['lastTickAt','lastLocalRecoveryAt']).then(s => JSON.stringify({now:Date.now(),lastTickAt:Math.max(s.lastTickAt||0,s.lastLocalRecoveryAt||0)}))",
      );
      const snapshot = JSON.parse(String(rawTick || "{}"));
      const completed = Number(snapshot.lastTickAt);
      const age = Number.isFinite(completed) && completed > 0
        ? Math.max(0, (Number(snapshot.now) - completed) / 1000)
        : "missing";
      console.log("extension_tick_age_s=" + age);
      return;
    }

    const requested = await cdpEvaluate(
      target,
      "chrome.runtime.reload(); 'reload-requested'",
    ).catch((error) => {
      // The target may close before returning because reload tears down the
      // service worker. That is still a successful request if the socket closed
      // after Runtime.evaluate was sent; surface a hard error only when no target
      // could be identified.
      if (/closed|websocket/i.test(String(error))) return "reload-requested";
      throw error;
    });
    if (requested !== "reload-requested") {
      throw new Error(`unexpected reload reply: ${String(requested)}`);
    }
    console.log(`extension_reload=requested version=${info.version || "?"} target=${target.url}`);
    return;
  }

  throw new Error(`service worker for "${EXTENSION_NAME}" not found`);
}

main().catch((error) => {
  console.error(`extension_reload=error detail=${error?.stack || error}`);
  process.exitCode = 1;
});
