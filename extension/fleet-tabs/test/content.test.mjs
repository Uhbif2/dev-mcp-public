import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFile, spawn } from 'node:child_process';
import http from 'node:http';
import { promisify } from 'node:util';

const chrome = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// --dump-dom waits for virtual-time exhaustion, which used to silently truncate
// this fixture and can also hang while the extension's timers keep ticking.
// Inspect only the isolated headless Chrome instance and stop on fixture completion.
async function runDomFixture(file, dir) {
  const profile = path.join(dir, 'profile');
  const child = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, pathToFileURL(file).href,
  ], { windowsHide: true, stdio: 'ignore' });
  let socket;
  let last = 'pending';
  try {
    const started = Date.now();
    const portFile = path.join(profile, 'DevToolsActivePort');
    while (!fs.existsSync(portFile) && Date.now() - started < 15000) {
      if (child.exitCode !== null) throw Error(`headless Chrome exited: ${child.exitCode}`);
      await delay(100);
    }
    assert.ok(fs.existsSync(portFile), 'headless Chrome debugging port not available');
    const port = Number(fs.readFileSync(portFile, 'utf8').split(/\r?\n/)[0]);
    const getPages = () => new Promise((resolve, reject) => {
      http.get(`http://127.0.0.1:${port}/json/list`, response => {
        let body = '';
        response.on('data', chunk => { body += chunk; });
        response.on('end', () => {
          try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
        });
      }).on('error', reject);
    });
    let page;
    for (let attempt = 0; attempt < 50; attempt++) {
      page = (await getPages()).find(tab => tab.type === 'page' && tab.url.includes('fixture.html'));
      if (page) break;
      await delay(100);
    }
    assert.ok(page, 'headless fixture page not available');
    socket = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    });
    const outstanding = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(String(event.data));
      const resolve = outstanding.get(message.id);
      if (resolve) { outstanding.delete(message.id); resolve(message); }
    });
    let nextId = 0;
    const evaluate = () => new Promise((resolve, reject) => {
      const id = ++nextId;
      outstanding.set(id, reply => {
        if (reply.error) reject(Error(JSON.stringify(reply.error)));
        else resolve(reply.result?.result?.value);
      });
      socket.send(JSON.stringify({
        id, method: 'Runtime.evaluate',
        params: { expression: "document.getElementById('result')?.textContent || 'pending'", returnByValue: true },
      }));
    });
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline) {
      last = await evaluate();
      if (last && last !== 'pending') {
        const results = JSON.parse(last);
        if (results.some(result => result.name === '__fixture_done__' || result.error)) return results;
      }
      await delay(80);
    }
    throw Error('headless fixture stalled before completion: ' + String(last).slice(-1200));
  } finally {
    try { socket?.send(JSON.stringify({ id: 99999, method: 'Browser.close' })); } catch {}
    socket?.close();
    child.kill();
  }
}
for (const storageFailure of ['', 'IO error: FILE_ERROR_NO_SPACE']) {
test(`popup renders owner intents and ${storageFailure ? 'storage failure' : 'healthy state'}`, {skip:!fs.existsSync(chrome)}, async () => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-tabs-popup-'));
  for (const name of ['popup.js','policy.js','fleet.js','fleet-ui-state.js','owner-notices.js','interface-pressure.js','recovery.js','config.js']) fs.copyFileSync(new URL(`../${name}`,import.meta.url),path.join(dir,name));
  const stub=`<script>
    const errors=[];window.onerror=(message)=>errors.push(String(message));
    window.chrome={runtime:{sendMessage:async()=>({storageError:${JSON.stringify(storageFailure)}})},storage:{local:{get:async()=>({settings:{paceVersion:9,dryRun:false,maxWorking:18},fleetProjects:[{name:'deep-research',count:2}],fleetUpdatedAt:Date.now(),fleetMemoryState:{at:Date.now(),freeMemMB:159,liveTabs:7,thresholdMB:250,lowMemory:true,discarded:2},quickSendFailures:{q22:{count:3,reason:'нет нового пользовательского сообщения',pausedUntil:Date.now()+500000}},telemetrySummary:{warnings:3,dismissed:3,samples:2,medianSec:10,p90Sec:15}})},onChanged:{addListener(){}}}};
    setTimeout(()=>document.getElementById('log').textContent=JSON.stringify({errors,
      agents:document.getElementById('maxWorking').value,tabs:document.getElementById('maxTabs').value,budgetField:document.getElementById('maxNewChatsPerHour')!==null,
      knobs:document.querySelectorAll('input[type=number]').length,tuned:document.getElementById('tuned').textContent,
      stats:document.getElementById('rateStats').textContent,memory:document.getElementById('memoryGuard').textContent,quickPauses:document.getElementById('quickSendPauses').textContent,cooldown:document.getElementById('cooldown').textContent,projects:document.getElementById('projects').textContent}),1500);
  </script><script type="module" src="popup.js"></script>`;
  const html=fs.readFileSync(new URL('../popup.html',import.meta.url),'utf8').replace('<script type="module" src="popup.js"></script>',stub);
  const file=path.join(dir,'popup.html');fs.writeFileSync(file,html);
  const {stdout}=await promisify(execFile)(chrome,['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--allow-file-access-from-files',`--user-data-dir=${path.join(dir,'profile')}`,'--dump-dom','--virtual-time-budget=3000',pathToFileURL(file).href],{timeout:45000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="log">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json,'popup did not render');
  const result=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&'));
  assert.deepEqual(result.errors,[]);
  assert.equal(result.agents,'18');assert.equal(result.budgetField,false);
  // 17.10 (владелец): число вкладок — намерение владельца, вторая ручка-число.
  assert.equal(result.knobs,2,'ручки-числа: агенты и интерфейсы');
  assert.equal(result.tabs,'4');
  assert.match(result.memory,/159 МБ/);
  assert.match(result.memory,/живых вкладок агентов: 7/);
  assert.doesNotMatch(result.memory,/ждут места|заблокированы/);
  assert.match(result.quickPauses,/q22: нет нового пользовательского сообщения; повтор после/);
  assert.doesNotMatch(result.tuned,/вкладок одновременно/);
  assert.match(result.stats,/медиана 10 с/);
  if (storageFailure) {
    assert.match(result.cooldown,/Хранилище браузера не записывает данные/);
    assert.match(result.projects,/FILE_ERROR_NO_SPACE/);
    assert.doesNotMatch(result.projects,/ожидаю ответа сервера/);
  }
});
}

test('content script: real DOM acknowledgement, stale/hidden/prose exclusions, hard limits and drafts', {skip:!fs.existsSync(chrome)}, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-tabs-dom-'));
  const content = fs.readFileSync(new URL('../content.js', import.meta.url),'utf8');
  const html = `<!doctype html><meta charset="utf-8"><main><div id="fixture"></div></main><pre id="result">pending</pre>
  <script>
  const listeners=[];
  // Exercise the hidden-tab branch with an async background-worker response.
  // No real multi-second waits: the test asserts DOM state, not sleep duration.
  Object.defineProperty(document,'hidden',{configurable:true,value:true});
  window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}},
    sendMessage(message,reply){
      if(message?.type==='fleet-sleep'){queueMicrotask(()=>reply?.());return;}
      return Promise.resolve();
    }}};
  </script><script>${content}</script><script>
  const results=[];
  const call=(message)=>new Promise(resolve=>listeners[0](message,{},resolve));
  const check=(name,ok,detail)=>{results.push({name,ok:Boolean(ok),...(!ok&&detail?{detail}:{})});document.getElementById('result').textContent=JSON.stringify(results);if(!ok)throw Error(name);};
  const fixture=document.getElementById('fixture');
  const modal=(text='Слишком много запросов', button='Понятно')=>'<div role="dialog"><p>'+text+'</p><button onclick="this.parentNode.remove()">'+button+'</button></div>';
  (async()=>{
    fixture.innerHTML=modal();
    check('detect modal',(await call({type:'probe'})).rateLimited);
    check('dismiss confirmed',(await call({type:'act',action:'dismiss-rate-limit'})).ok);
    check('modal gone',!(await call({type:'probe'})).rateLimited);
    fixture.innerHTML='<article><div data-message-author-role="assistant"><div role="status">Слишком много запросов</div></div></article>';
    check('prose ignored',!(await call({type:'probe'})).rateLimited);
    fixture.innerHTML='<div role="status">Too Many requests</div>';
    const toast=await call({type:'probe'});
    check('small top toast classified',toast.rateLimited && toast.rateLimitKind==='toast');
    check('toast is not clicked',!(await call({type:'act',action:'dismiss-rate-limit'})).ok);
    fixture.innerHTML='<div style="display:none">'+modal()+'</div>';
    check('hidden ignored',!(await call({type:'probe'})).rateLimited);
    fixture.innerHTML='<div role="alert">No response from ChatGPT. Check your connection.</div>';
    check('quick failed turn banner recognised',(await call({type:'probe'})).failed.includes('No response from ChatGPT'));
    fixture.innerHTML='<div role="alert">Could not load this ChatGPT conversation</div>';
    const conversationFailure=await call({type:'probe'});
    check('quick conversation error recognised',conversationFailure.pageLoadFailed.includes('Could not load') && !conversationFailure.failed);
    fixture.innerHTML='<div role="alert">You’ve reached the maximum length for this conversation, but you can keep talking by starting a new chat.</div>';
    const full=await call({type:'probe'});
    check('conversation length is not account HARD_BLOCK',full.conversationFull && !full.blocked && full.fill===0);
    check('cannot send into full conversation',!(await call({type:'act',action:'continue',text:'продолжи'})).ok);
    fixture.innerHTML='<div role="status">Вы достигли максимальной длины этого разговора.</div>';
    check('russian conversation length banner recognized',(await call({type:'probe'})).conversationFull);
    fixture.innerHTML='<section data-turn="assistant">You’ve reached the maximum length for this conversation</section>';
    check('text in assistant prose is not a banner',!(await call({type:'probe'})).conversationFull);
    fixture.innerHTML='<section data-turn="user">тест</section><section data-turn="assistant">инструмент вывел 42</section>';
    const length=await call({type:'probe'});
    check('conversation text includes tool outputs',length.conversationUsage.turns===2 &&
      length.conversationUsage.chars>=20 && length.conversationUsage.cyrillic>0 &&
      length.conversationUsage.estimatedTokens>0 && length.fill>0);
    fixture.innerHTML='<section data-turn="assistant">'+ 'я'.repeat(68000)+'</section>';
    check('68k chars reaches conservative 85% threshold',(await call({type:'probe'})).fill>=0.85);
    fixture.innerHTML=modal('Вы достигли лимита сообщений');
    const quota=await call({type:'probe'});
    check('quota separate',quota.blocked && !quota.rateLimited);
    check('quota not clicked',!(await call({type:'act',action:'dismiss-rate-limit'})).ok && document.querySelector('[role=dialog]'));
    fixture.innerHTML=modal('Слишком много запросов. Подтвердите, что вы человек');
    check('mixed challenge not clicked',!(await call({type:'act',action:'dismiss-rate-limit'})).ok);
    fixture.innerHTML=modal('Слишком много запросов','Разрешить');
    check('unrelated approval not clicked',!(await call({type:'act',action:'dismiss-rate-limit'})).ok);
    fixture.innerHTML='<textarea id="prompt-textarea">мой черновик</textarea>'+modal();
    check('ack with draft',(await call({type:'act',action:'dismiss-rate-limit'})).ok);
    check('draft preserved',document.querySelector('textarea').value==='мой черновик');
    check('send preserves draft',!(await call({type:'act',action:'continue',text:'продолжи'})).ok && document.querySelector('textarea').value==='мой черновик');
    fixture.innerHTML='<button aria-label="New chat">New chat</button>';
    document.querySelector('button').onclick=()=>{
      fixture.innerHTML='<div role="menu"><button role="menuitem">Chat</button><button role="menuitem">Work</button></div>';
      document.querySelector('[role="menuitem"]').onclick=()=>{
        fixture.innerHTML='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>';
        document.querySelector('button').onclick=()=>{
          const text=document.querySelector('textarea').value;
          document.querySelector('textarea').value='';
          const msg=document.createElement('section');msg.dataset.turn='user';msg.textContent=text;fixture.append(msg);
        };
      };
    };
    const landing=await call({type:'act',action:'start',mention:"dev-mcp",text:"fleet_join now"});
    check('project landing New chat -> Chat -> composer sends',landing.ok && document.querySelector('[data-turn="user"]')?.textContent.includes('fleet_join now'));

    fixture.innerHTML='<main><div class="composer-shell"><div contenteditable="true" style="min-height:40px"><p data-placeholder="Новый чат в Fleet Dev-mcp" aria-label="Новый чат в Fleet Dev-mcp"><br></p></div><button id="composer-submit-button" aria-label="Отправить промпт">Send</button></div></main>';
    document.querySelector('#composer-submit-button').onclick=()=>{
      const editor=document.querySelector('[contenteditable="true"]');
      const text=editor.innerText;
      editor.innerText='';
      const msg=document.createElement('section');msg.dataset.turn='user';msg.textContent=text;fixture.append(msg);
    };
    const screenshotUi=await call({type:'act',action:'start',mention:"dev-mcp",text:"fleet_join screenshot"});
    check('project composer from screenshot is detected directly',screenshotUi.ok && document.querySelector('[data-turn="user"]')?.textContent.includes('fleet_join screenshot'));

    fixture.innerHTML='<main><div class="composer-shell"><div role="textbox" contenteditable="plaintext-only" aria-label="Новый чат в Fleet Dev-mcp" style="min-height:40px"><span data-placeholder="Новый чат в Fleet Dev-mcp">Новый чат в Fleet Dev-mcp</span></div><button id="composer-submit-button" aria-label="Отправить промпт">Send</button></div></main>';
    check('DOM placeholder still counts as empty',(await call({type:'probe'})).composerEmpty);

    fixture.innerHTML='<main><div role="textbox" contenteditable="plaintext-only" aria-label="Prompt" style="min-height:40px"><p>Новый чат в Fleet Dev-mcp</p></div><button id="composer-submit-button" aria-label="Отправить промпт">Send</button></main>';
    check('literal project placeholder from live UI counts as empty',(await call({type:'probe'})).composerEmpty);

    fixture.innerHTML='<main><div class="composer-shell"><div role="textbox" contenteditable="plaintext-only" aria-label="Новый чат в Fleet Dev-mcp" style="min-height:40px"><span data-placeholder="Новый чат в Fleet Dev-mcp">Новый чат в Fleet Dev-mcp</span></div><button id="composer-submit-button" aria-label="Отправить промпт">Send</button></div></main>';
    document.querySelector('#composer-submit-button').onclick=()=>{
      const editor=document.querySelector('[contenteditable="plaintext-only"]');
      const text=editor.innerText;
      editor.innerText='';
      const msg=document.createElement('section');msg.dataset.turn='user';msg.textContent=text;fixture.append(msg);
    };
    const plain=await call({type:'act',action:'start',mention:"dev-mcp",text:"fleet_join plaintext"});
    check('project plaintext-only composer is detected and sends',plain.ok && [...document.querySelectorAll('[data-turn="user"]')].at(-1)?.textContent.includes('fleet_join plaintext'));

    fixture.innerHTML='<div data-testid="prompt-textarea" data-lexical-editor="true" contenteditable="true" style="min-height:40px"></div><button id="composer-submit-button" aria-label="Отправить промпт">Send</button>';
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('[contenteditable="true"]').innerText;
      document.querySelector('[contenteditable="true"]').innerText='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    const modern=await call({type:'act',action:'start',mention:"dev-mcp",text:"fleet_join modern"});
    check('new ChatGPT lexical composer and submit button send',modern.ok && document.querySelector('[data-message-author-role]')?.textContent.includes('fleet_join modern'));

    fixture.innerHTML='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>';
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('textarea').value;
      document.querySelector('textarea').value='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    const noConnector=await call({type:'act',action:'start',mention:"dev-mcp",text:"fleet_join now"});
    check('start falls back to raw connector mention',noConnector.ok && /без DOM-подсказки/.test(noConnector.detail));
    check('raw connector mention sent',document.querySelector('[data-message-author-role]').textContent.includes("@dev-mcp") && document.querySelector('[data-message-author-role]').textContent.includes('fleet_join now'));

    fixture.innerHTML='<textarea id="prompt-textarea">@Dev&#39;s Computer dev-mcp+ fleet_join recovered</textarea><button data-testid="send-button">Send</button>';
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('textarea').value;
      document.querySelector('textarea').value='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    const recovered=await call({type:'act',action:'start',mention:"dev-mcp",text:'fleet_join recovered'});
    check('own start draft survives reinject and is sent',recovered.ok && /восстановлена наша стартовая заготовка/.test(recovered.detail));
    check('recovered start draft sent once',document.querySelector('[data-message-author-role]').textContent.endsWith('fleet_join recovered'));

    fixture.innerHTML='<textarea id="prompt-textarea">Dev Computer dev-mcp+ — Ты агент флота dev-mcp, метка label=&#39;f0925-9999abc&#39;. fleet_join(label=&#39;f0925-9999abc&#39;, kind=&#39;chatgpt&#39;)</textarea><button data-testid="send-button">Send</button>';
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('textarea').value; document.querySelector('textarea').value='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    const chipRecovered=await call({type:'act',action:'start',mention:"dev-mcp",text:"Ты агент флота dev-mcp, метка label='f0925-9999abc'. Работай по инструкции проекта: fleet_join(label='f0925-9999abc', kind='chatgpt')"});
    check('mention-chip transformed own starter is recovered by exact fleet label',chipRecovered.ok);

    fixture.innerHTML='<textarea id="prompt-textarea">Подключено: Dev&#39;s Computer dev-mcp+</textarea><button data-testid="send-button">Send</button>';
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('textarea').value; document.querySelector('textarea').value='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    const partialRecovered=await call({type:'act',action:'start',mention:"dev-mcp",text:"Ты агент флота dev-mcp, метка label='f0925-8888abc'. Работай по инструкции проекта: fleet_join(label='f0925-8888abc', kind='chatgpt')"});
    check('decorated connector-only partial starter is rebuilt and sent',partialRecovered.ok && document.querySelector('[data-message-author-role]')?.textContent.includes('f0925-8888abc'));

    // 18.11: общий черновик проекта — стартовое сообщение обычного агента флота — не блокирует
    // первое сообщение быстрого агента в новой вкладке; отправляется только его задание.
    for (const starter of ["@dev-mcp Ты агент флота dev-mcp, label='f1118-08015qy'. За тобой уже закреплена задача",
                           "Ты агент флота dev-mcp, метка label='f1118-0820w8n'. fleet_join(label='f1118-0820w8n')"]) {
      fixture.innerHTML='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>';
      document.querySelector('textarea').value=starter;
      document.querySelector('button').onclick=()=>{
        const text=document.querySelector('textarea').value; document.querySelector('textarea').value='';
        const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
      };
      const replaced=await call({type:'act',action:'start',mention:"dev-mcp",
        text:'⟦быстрый агент Q1-7278A5⟧ Задание быстрого агента',quick:true,confirmedStartAgents:[]});
      const sent=document.querySelector('[data-message-author-role]')?.textContent||'';
      check('fleet starter draft replaced by quick task: '+starter.slice(-30),
        replaced.ok && sent.includes('Q1-7278A5') && !sent.includes('f1118-'));
    }

    const quickDraft=(tag)=>'⟦быстрый агент '+tag+'⟧ Исходное поручение '+tag;
    const ownQuick=quickDraft('Q12-C83ACC');
    const staleQuick=quickDraft('Q10-C83ACC');
    const setupQuickDraft=(value,clearAfterSend=true)=>{
      fixture.innerHTML='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>';
      document.querySelector('textarea').value=value;
      document.querySelector('button').onclick=()=>{
        const input=document.querySelector('textarea');
        const text=input.value;
        if(clearAfterSend)input.value='';
        const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
      };
    };
    setupQuickDraft(staleQuick,false); // ChatGPT left its already-sent draft in New chat
    const stale=await call({type:'act',action:'start',mention:"dev-mcp",
      text:ownQuick,quick:true,confirmedStartAgents:['Q10-C83ACC']});
    check('delivered foreign quick draft cleared and our own start sent',stale.ok &&
      document.querySelector('[data-message-author-role]')?.textContent.includes(ownQuick) &&
      !document.querySelector('[data-message-author-role]')?.textContent.includes(staleQuick));
    check('confirmed start cleans exact residual New chat draft',
      document.querySelector('textarea').value==='');

    setupQuickDraft(quickDraft('Q11-C83ACC'));
    // A foreign agent's live draft is protected only after a conversation has turns.
    fixture.insertAdjacentHTML('beforeend',
      '<div data-message-author-role="assistant">previous reply</div>');
    const live=await call({type:'act',action:'start',mention:"dev-mcp",
      text:ownQuick,quick:true,confirmedStartAgents:['Q10-C83ACC']});
    check('live conversation foreign quick draft rejected and preserved',!live.ok &&
      /другого агента/.test(live.detail) &&
      document.querySelector('textarea').value===quickDraft('Q11-C83ACC') &&
      !document.querySelector('[data-message-author-role="user"]'));

    setupQuickDraft('Человеческий черновик без метки');
    const humanQuick=await call({type:'act',action:'start',mention:"dev-mcp",
      text:ownQuick,quick:true,confirmedStartAgents:['Q10-C83ACC']});
    check('human draft without agent tag is never replaced by quick start',!humanQuick.ok &&
      document.querySelector('textarea').value==='Человеческий черновик без метки' &&
      !document.querySelector('[data-message-author-role]'));

    fixture.innerHTML='<textarea id="prompt-textarea">human draft</textarea><button data-testid="send-button">Send</button>';
    document.querySelector('button').onclick=()=>{throw new Error('foreign draft must not be sent')};
    const foreign=await call({type:'act',action:'start',mention:"dev-mcp",text:'fleet_join expected'});
    check('foreign start draft is never sent',!foreign.ok && /чужой текст/.test(foreign.detail) && document.querySelector('textarea').value==='human draft');

    fixture.innerHTML='<article><section data-turn="assistant">previous answer</section></article><div role="status" id="resume-error">Resume stream unavailable</div><button id="resume-retry">Повторить</button>';
    document.querySelector('#resume-retry').onclick=()=>document.querySelector('#resume-error')?.remove();
    const resumeFailure=await call({type:'probe'});
    check('Resume stream unavailable is a failed turn',resumeFailure.failed && resumeFailure.hasRetryButton);
    check('Resume stream unavailable clicks Retry',(await call({type:'act',action:'click-retry',text:'продолжи'})).ok);

    fixture.innerHTML='<div role="status">A network error occurred. Please check your connection and try again. <button id="network-retry">Повторить</button></div>';
    const networkFailure=await call({type:'probe'});
    check('network connection error is retryable, not a sensitive permission gate',networkFailure.failed && networkFailure.hasRetryButton && !networkFailure.ui.gateSensitive);

    fixture.innerHTML='<main><h2>Не удалось загрузить этот разговор ChatGPT</h2><button id="conversation-retry">Повторить</button></main>';
    const loadFailure=await call({type:'probe'});
    check('full conversation load failure is detected for page reload',/не удалось загрузить этот разговор/i.test(loadFailure.pageLoadFailed));

    fixture.innerHTML='<div role="dialog"><p>Новый интерфейс готов</p><button id="safe-gate">Продолжить</button></div>';
    document.querySelector('#safe-gate').onclick=()=>document.querySelector('[role="dialog"]')?.remove();
    const gateProbe=await call({type:'probe'});
    check('safe interstitial is diagnosed',gateProbe.ui.gate && !gateProbe.ui.gateSensitive);
    check('safe interstitial is dismissed',(await call({type:'act',action:'dismiss-gate'})).ok && !document.querySelector('[role="dialog"]'));

    fixture.innerHTML='<div role="dialog"><p>Verify you are human</p><button id="human-gate">Continue</button></div>';
    const sensitive=await call({type:'probe'});
    check('human verification is sensitive',sensitive.ui.gateSensitive);
    check('human verification is never auto-bypassed',!(await call({type:'act',action:'dismiss-gate'})).ok && document.querySelector('#human-gate'));

    // Two ChatGPT composers: an off-screen floating draft and the live main form.
    fixture.innerHTML='<div aria-hidden="true"><form id="floating"><textarea id="prompt-textarea">old floating draft</textarea><button aria-label="Send">Send</button></form></div>'
      +'<form id="live" class="composer-shell"><textarea id="prompt-textarea">saved draft</textarea><button aria-label="Send">Send</button></form>';
    let floatingClicked=0, liveClicked=0;
    document.querySelector('#floating button').onclick=()=>{ floatingClicked++; };
    document.querySelector('#live button').onclick=()=>{
      liveClicked++;
      const el=document.querySelector('#live textarea');
      const text=el.value; el.value='';
      const msg=document.createElement('section'); msg.dataset.turn='user'; msg.textContent=text; fixture.append(msg);
    };
    check('two composers: selects active input',(await call({type:'probe'})).composerEmpty===false);
    const dual=await call({type:'act',action:'continue',text:'fresh quick message',quick:true});
    check('two composers: sends through the active form only',dual.ok && liveClicked===1 && floatingClicked===0);
    check('two composers: draft replaced, floating draft untouched',
      document.querySelector('#floating textarea').value==='old floating draft' &&
      document.querySelector('[data-turn="user"]')?.textContent==='fresh quick message');

    fixture.innerHTML='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>';
    check('failed send',!(await call({type:'act',action:'continue',text:'моя заготовка'})).ok);
    check('our draft recognized',(await call({type:'probe'})).composerOwned);
    document.querySelector('button').onclick=()=>{
      const text=document.querySelector('textarea').value;
      document.querySelector('textarea').value='';
      const msg=document.createElement('div');msg.dataset.messageAuthorRole='user';msg.textContent=text;fixture.append(msg);
    };
    check('our draft retried',(await call({type:'act',action:'continue',text:'другой вариант'})).ok);
    check('original sent once',document.querySelector('[data-message-author-role]').textContent==='моя заготовка');

    fixture.innerHTML='<main><h1>Unable to load site</h1><p>VPN connection is required</p></main>';
    const cloudflare=await call({type:'probe'});
    check('Cloudflare interstitial sends a bounded page-text excerpt',/Unable to load site/.test(cloudflare.cloudflarePageText) && cloudflare.cloudflarePageText.length<401);
    const missing=await call({type:'act',action:'continue',text:'продолжи'});
    check('composer failure carries matching detail plus actual Cloudflare text',!missing.ok && /не найдено поле ввода/.test(missing.detail) && /Unable to load site/.test(missing.pageText));
    fixture.innerHTML='<section data-turn="assistant">Unable to load site is a quoted phrase</section>';
    check('quoted Cloudflare phrase in a conversation is not leaked as page evidence',!(await call({type:'probe'})).cloudflarePageText);
    // 15.11.2026: разметка ChatGPT без data-message-author-role/data-turn — роль в h4[data-conversation-role].
    fixture.innerHTML='<div data-turn-key="t1"><div><h4 data-conversation-role="user" class="sr-only"><span>You said:</span></h4><div>Ты агент флота, label="f1115-1502abc". Задача</div></div>'
      +'<div><h4 data-conversation-role="assistant" class="sr-only"><span>ChatGPT said:</span></h4><div>Сделано: Слишком много запросов — это цитата</div></div></div>';
    const tr=await call({type:'transcript'});
    check('new markup: transcript sees both turns',tr.ok && tr.nMessages===2 && tr.lastRole==='assistant');
    check('new markup: role caption stripped',tr.userMessages[0].startsWith('Ты агент флота') && tr.lastAssistant.startsWith('Сделано'));
    const pr=await call({type:'probe'});
    check('new markup: label found and quoted limit text not a notice',pr.label==='f1115-1502abc' && !pr.rateLimited);
    // Live UI puts h4 in a header-only wrapper; user body is a sibling in the same turn.
    fixture.innerHTML='<main><article data-testid="conversation-turn-1"><div class="role"><h4 data-conversation-role="user">You said:</h4></div>'
      +'<div class="body"><p>@Dev&#39;s Computer dev-mcp+</p><p>Ты агент флота, label="pult-q22-9ee9d8". Задача подтверждена.</p></div></article>'
      +'<article data-testid="conversation-turn-2"><div class="role"><h4 data-conversation-role="assistant">ChatGPT said:</h4></div>'
      +'<div class="body">Сделано</div></article></main>';
    const nested=await call({type:'transcript'});
    check('nested heading: user message includes sibling body (and connector prefix)',nested.ok &&
      nested.nMessages===2 && nested.userMessages.length===1 &&
      nested.userMessages[0].includes('Ты агент флота, label="pult-q22-9ee9d8"') &&
      !nested.userMessages[0].includes('Сделано') && nested.lastAssistant==='Сделано');
    fixture.innerHTML='<main><div class="composer-shell"><textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button></div><section id="turns"></section></main>';
    document.querySelector('button').onclick=()=>{
      const body=document.querySelector('textarea').value;
      document.querySelector('textarea').value='';
      document.querySelector('#turns').innerHTML='<article><div><h4 data-conversation-role="user">You said:</h4></div>'
        +'<div>@Dev&#39;s Computer dev-mcp+ '+body+'</div></article>';
    };
    const sent=await call({type:'act',action:'continue',text:'Проверка новой разметки подтверждена',quick:true});
    const afterSend=await call({type:'transcript'});
    check('new nested markup and connector @prefix confirm send',sent.ok &&
      afterSend.userMessages[0]?.includes('Проверка новой разметки'),
      JSON.stringify({sent,afterSend}));
    results.push({name:'__fixture_done__',ok:true});
  })().catch(error=>results.push({error:String(error),ok:false})).finally(()=>document.getElementById('result').textContent=JSON.stringify(results));
  </script>`;
  const file = path.join(dir,'fixture.html'); fs.writeFileSync(file,html);
  const results=await runDomFixture(file,dir);
  assert.ok(results.length>=18,JSON.stringify(results));
  assert.deepEqual(results.filter(r=>!r.ok),[]);
  assert.equal(results.filter(r=>r.name?.startsWith('fleet starter draft replaced by quick task:')).length,2,
    'both fleet starter replacement cases must run');
  assert.ok(results.some(r=>r.name==='__fixture_done__' && r.ok),
    'headless fixture did not reach the final scenario: '+JSON.stringify(results.slice(-5)));
});
