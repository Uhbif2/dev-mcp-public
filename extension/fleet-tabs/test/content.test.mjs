import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const chrome = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
test('popup renders owner intents; tuned parameters are read-only', {skip:!fs.existsSync(chrome)}, async () => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-tabs-popup-'));
  for (const name of ['popup.js','policy.js','fleet.js','owner-notices.js','interface-pressure.js','recovery.js','config.js']) fs.copyFileSync(new URL(`../${name}`,import.meta.url),path.join(dir,name));
  const stub=`<script>
    const errors=[];window.onerror=(message)=>errors.push(String(message));
    window.chrome={storage:{local:{get:async()=>({settings:{paceVersion:9,dryRun:false,maxWorking:18},telemetrySummary:{warnings:3,dismissed:3,samples:2,medianSec:10,p90Sec:15}})},onChanged:{addListener(){}}}};
    setTimeout(()=>document.getElementById('log').textContent=JSON.stringify({errors,
      agents:document.getElementById('maxWorking').value,tabs:document.getElementById('maxTabs').value,budgetField:document.getElementById('maxNewChatsPerHour')!==null,
      knobs:document.querySelectorAll('input[type=number]').length,tuned:document.getElementById('tuned').textContent,
      stats:document.getElementById('rateStats').textContent}),1500);
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
  assert.equal(result.knobs,2,'ручки-числа: агенты и вкладки');
  assert.equal(result.tabs,'4');
  assert.doesNotMatch(result.tuned,/вкладок одновременно/);
  assert.match(result.stats,/медиана 10 с/);
});

test('content script: real DOM acknowledgement, stale/hidden/prose exclusions, hard limits and drafts', {skip:!fs.existsSync(chrome)}, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-tabs-dom-'));
  const content = fs.readFileSync(new URL('../content.js', import.meta.url),'utf8');
  const html = `<!doctype html><meta charset="utf-8"><main><div id="fixture"></div></main><pre id="result">pending</pre>
  <script>
  const listeners=[];
  window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}}}};
  </script><script>${content}</script><script>
  const results=[];
  const call=(message)=>new Promise(resolve=>listeners[0](message,{},resolve));
  const check=(name,ok)=>{results.push({name,ok:Boolean(ok)});if(!ok)throw Error(name);};
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
  })().catch(error=>results.push({error:String(error),ok:false})).finally(()=>document.getElementById('result').textContent=JSON.stringify(results));
  </script>`;
  const file = path.join(dir,'fixture.html'); fs.writeFileSync(file,html);
  const {stdout} = await promisify(execFile)(chrome,[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-networking',`--user-data-dir=${path.join(dir,'profile')}`,
    '--dump-dom','--virtual-time-budget=140000',pathToFileURL(file).href,
  ],{timeout:45000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json && json!=='pending','headless fixture did not finish');
  const results=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&').replaceAll('&gt;','>').replaceAll('&lt;','<'));
  assert.ok(results.length>=18,JSON.stringify(results));
  assert.deepEqual(results.filter(r=>!r.ok),[]);
});
