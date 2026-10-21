// Fixed non-private native notices. A transport receipt never approves work.
const ID = /^on-[a-f0-9]{24}$/;
const KEY = "ownerNoticeDelivery";

export function parseOwnerChannel(text) {
  const line=String(text||"").split(/\r?\n/).find(s=>s.startsWith("owner-channel:"))||"";
  return {nativeBlocked:line.includes("native=blocked"),attention:Number(/attention=(\d+)/.exec(line)?.[1]||0)};
}

export async function drawOwnerAttention(api=chrome) {
  const windows=await api.windows.getAll({windowTypes:["normal"]});
  if(!windows.length) throw Error("no FleetChrome window for owner attention");
  await api.windows.update(windows[0].id,{drawAttention:true});
}

export function parseOwnerNotices(text) {
  const notices = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    if (!line.startsWith("owner-notice: ")) continue;
    try {
      const item = JSON.parse(line.slice(14));
      if (Object.keys(item).sort().join(",") === "id,kind" && ID.test(item.id) &&
          ["needs", "done", "attention"].includes(item.kind)) notices.push(item);
    } catch {}
  }
  return notices.slice(0, 3);
}

export async function deliverOwnerNotices(notices, report, api = chrome, now = Date.now(), nativeBlocked=false) {
  const saved = (await api.storage.local.get(KEY))[KEY] || {};
  const pending = {...saved};
  for (const item of notices) {
    if (!ID.test(item.id)) continue;
    const old = pending[item.id] || {};
    if (["clicked","browser_attention"].includes(old.state) ||
        (old.state === "api_accepted" && !nativeBlocked) || old.retryAt > now) continue;
    let state = "error";
    try {
      if (nativeBlocked) { await drawOwnerAttention(api); state="browser_attention"; }
      else if (await api.notifications.getPermissionLevel() === "denied") state = "denied";
      else {
        const id = await api.notifications.create(item.id, {
          type:"basic",iconUrl:"icon128.png",priority:1,
          title:item.kind === "needs" ? "Флоту требуется ваше решение" :
            item.kind === "attention" ? "Флоту требуется внимание" : "Результат флота готов",
          message:"Нажмите, чтобы открыть панель результатов и решений.",
          requireInteraction:item.kind !== "done",
        });
        const active = await api.notifications.getAll();
        if (id === item.id && Object.hasOwn(active,item.id)) state = "api_accepted";
      }
    } catch {}
    pending[item.id] = {state,pendingAck:true,retryAt:now+60000,at:now};
    await api.storage.local.set({[KEY]:pending});
  }
  let reported=0;
  for (const [id,entry] of Object.entries(pending)) {
    if (!entry.pendingAck) continue;
    if(reported++ >= 2) break;
    try { await report({id,state:entry.state}); entry.pendingAck=false; } catch {}
  }
  const bounded = Object.fromEntries(Object.entries(pending)
    .sort((a,b)=>(b[1].at||0)-(a[1].at||0)).slice(0,256));
  await api.storage.local.set({[KEY]:bounded});
}

export async function markOwnerPanelOpened(report,api=chrome) {
  const all=(await api.storage.local.get(KEY))[KEY]||{};
  let reported=0;
  for(const [id,entry] of Object.entries(all)) {
    if(!ID.test(id) || entry.state==="clicked") continue;
    if(reported++ >= 2) break;
    entry.state="clicked"; entry.pendingAck=true;
    await api.storage.local.set({[KEY]:all});
    try {await report({id,state:"clicked"});entry.pendingAck=false;} catch {}
  }
  await api.storage.local.set({[KEY]:all});
}

export function installOwnerNoticeClicks(report, api = chrome) {
  api.notifications.onClicked.addListener(async (id) => {
    if (!ID.test(id) && id !== "fleet-connectivity") return;
    await api.tabs.create({url:api.runtime.getURL("dashboard/index.html")});
    if (!ID.test(id)) return;
    const all = (await api.storage.local.get(KEY))[KEY] || {};
    all[id] = {state:"clicked",pendingAck:true,at:Date.now()};
    await api.storage.local.set({[KEY]:all});
    try { await report({id,state:"clicked"}); all[id].pendingAck=false;
      await api.storage.local.set({[KEY]:all}); } catch {}
  });
}

export async function recordServerAvailability(up, api = chrome) {
  const saved = (await api.storage.local.get("ownerConnectivity")).ownerConnectivity || {};
  if (up) {
    if (saved.notified) await api.notifications.clear("fleet-connectivity").catch(()=>{});
    await api.storage.local.set({ownerConnectivity:{failures:0,notified:false}});
    return;
  }
  const failures = Math.min(3,(saved.failures||0)+1);
  let notified = !!saved.notified;
  if (failures >= 3 && !notified) {
    try {
      const blocked=(await api.storage.local.get("ownerNativeBlocked")).ownerNativeBlocked === true;
      if (blocked) { await drawOwnerAttention(api); notified=true; }
      else if (await api.notifications.getPermissionLevel() !== "denied") {
        await api.notifications.create("fleet-connectivity",{type:"basic",iconUrl:"icon128.png",
          title:"Нет связи с сервером флота",message:"Автоматика повторяет проверку. Нажмите, чтобы открыть панель.",
          priority:1,requireInteraction:true});
        notified=true;
      }
    } catch {}
  }
  if (failures>=3) await api.action?.setBadgeText({text:"!"}).catch(()=>{});
  await api.storage.local.set({ownerConnectivity:{failures,notified}});
}
