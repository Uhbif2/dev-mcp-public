import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {pathToFileURL} from 'node:url';

const chrome=process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

test('quick: two composers, same-form Send and stale draft replacement', {skip:!fs.existsSync(chrome)}, async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-quick-composer-'));
  const source=fs.readFileSync(new URL('../content.js',import.meta.url),'utf8');
  const html=`<!doctype html><meta charset="utf-8">
    <aside data-app-action-sidebar-project-row><button aria-label="Project actions for Beta App">...</button>
      <button aria-label="New chat in Beta App">...</button></aside>
    <main id="main">
      <div aria-hidden="true" style="display:none"><form id="floating">
        <textarea id="prompt-textarea">foreign floating draft</textarea><button aria-label="Send">Send</button>
        <button aria-label="Restore floating chat">Restore floating chat</button>
        <button aria-label="Dock Chat">Dock Chat</button>
      </form></div>
      <fieldset id="live"><div class="ComposerLayoutRoot"><div><div><div>
        <div class="ComposerLayoutFooter"><div><div>
          <div class="ProseMirror" contenteditable="true" role="textbox" aria-label="Ask ChatGPT"
            data-composer-markdown="true">persisted quick draft</div>
        </div></div><button type="submit" aria-label="Send">Send</button></div>
      </div></div></div></div></fieldset>
    </main><pre id="result">pending</pre>
    <script>
      const listeners=[];
      window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}}}};
    </script>
    <script>${source}</script>
    <script>
      let floating=0,live=0;
      document.querySelector('#floating button').onclick=()=>floating++;
      document.querySelector('#live button[aria-label="Send"]').onclick=()=>{
        live++;
        const editor=document.querySelector('#live .ProseMirror');
        const text=editor.textContent;editor.textContent='';
        const message=document.createElement('section');
        message.dataset.turn='user';message.textContent=text;document.querySelector('#main').append(message);
      };
      listeners[0]({type:'act',action:'continue',quick:true,text:'quick unique payload'}, {}, result=>{
        document.querySelector('#result').textContent=JSON.stringify({result,live,floating,
          posted:document.querySelector('[data-turn="user"]')?.textContent,
          stale:document.querySelector('#floating textarea').value});
      });
    </script>`;
  const file=path.join(folder,'fixture.html');fs.writeFileSync(file,html);
  const {stdout}=await promisify(execFile)(chrome,[
    '--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
    `--user-data-dir=${path.join(folder,'profile')}`,'--dump-dom','--virtual-time-budget=15000',
    pathToFileURL(file).href],{timeout:25000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json && json!=='pending','browser did not finish quick-send');
  const actual=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&'));
  assert.equal(actual.result.ok,true);
  assert.equal(actual.live,1);
  assert.equal(actual.floating,0);
  assert.equal(actual.posted,'quick unique payload');
  assert.equal(actual.stale,'foreign floating draft');
});

test('owner-sv: preserved 437-char draft uses scoped Send and new user-turn acknowledgement', {skip:!fs.existsSync(chrome)}, async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-overseer-send-'));
  const source=fs.readFileSync(new URL('../content.js',import.meta.url),'utf8');
  const message='Надзор: ' + 'необходимо продолжать проверку. '.repeat(13) + 'Решение.';
  const expected=message.slice(0,437).padEnd(437,'!');
  const html=`<!doctype html><meta charset="utf-8">
    <aside><button aria-label="Send">WRONG SIDEBAR SEND</button></aside>
    <main><form><div><div><div role="textbox" aria-label="Message ChatGPT" contenteditable="true" id="draft">${expected}</div>
      <button id="composer-submit-button" type="submit" aria-label="Send">Send</button></div></div></form>
      <section id="turns"></section></main><pre id="result">pending</pre>
    <script>const listeners=[];window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}}}};</script>
    <script>${source}</script><script>
      let clicked=0,wrong=0;
      document.querySelector('aside button').onclick=()=>wrong++;
      const form=document.querySelector('form');
      form.onsubmit=(event)=>{
        event.preventDefault();clicked++;
        const text=document.querySelector('#draft').textContent;
        document.querySelector('#draft').textContent='';
        document.querySelector('#turns').innerHTML='<article><div><h4 data-conversation-role="user">You said:</h4></div><div>'+text+'</div></article>';
      };
      listeners[0]({type:'act',action:'continue',text:'foreign operator draft',overseer:true},{},blocked=>{
        const editor=document.querySelector('#draft');
        editor.textContent=${JSON.stringify(expected.slice(0,140))};
        listeners[0]({type:'act',action:'continue',text:${JSON.stringify(expected)},overseer:true},{},partial=>{
          const retained=editor.textContent===${JSON.stringify(expected.slice(0,140))};
          editor.textContent=${JSON.stringify(expected)};
          listeners[0]({type:'act',action:'continue',text:${JSON.stringify(expected)},overseer:true},{},confirmed=>{
            document.querySelector('#result').textContent=JSON.stringify({blocked,partial,retained,confirmed,clicked,wrong,
              posted:document.querySelector('#turns article')?.textContent||''});
          });
        });
      });
    </script>`;
  const file=path.join(folder,'fixture.html');fs.writeFileSync(file,html);
  const {stdout}=await promisify(execFile)(chrome,[
    '--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
    `--user-data-dir=${path.join(folder,'profile')}`,'--dump-dom','--virtual-time-budget=18000',
    pathToFileURL(file).href],{timeout:25000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json&&json!=='pending','real DOM overseer send did not finish');
  const actual=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&'));
  assert.equal(actual.blocked.ok,false);
  assert.equal(actual.partial.ok,false);
  assert.equal(actual.retained,true,'partial owner draft was not cleared or overwritten');
  assert.equal(actual.confirmed.ok,true);
  assert.equal(actual.clicked,1);
  assert.equal(actual.wrong,0);
  assert.ok(actual.posted.includes(expected.slice(0,150)));
});

test('owner-sv: legacy ⟦надзор⟧ draft recovers in the same /c/ chat without rewriting it', {skip:!fs.existsSync(chrome)}, async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-overseer-legacy-'));
  const dir=path.join(folder,'c','abcdefab-1');fs.mkdirSync(dir,{recursive:true});
  const source=fs.readFileSync(new URL('../content.js',import.meta.url),'utf8');
  const marked='⟦надзор⟧ Пройди следующий пункт плана. ' + 'тест '.repeat(90);
  const html=`<!doctype html><meta charset="utf-8">
    <main><form><div role="textbox" contenteditable="true" aria-label="Message ChatGPT" id="draft">чужой черновик</div>
      <button type="submit" aria-label="Send">Send</button></form><section id="turns"></section></main>
    <pre id="result">pending</pre>
    <script>const listeners=[];window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}}}};</script>
    <script>${source}</script><script>
      let clicked=0;document.querySelector('form').onsubmit=event=>{
        event.preventDefault();clicked++;
        const draft=document.querySelector('#draft'), text=draft.textContent;
        draft.textContent='';
        document.querySelector('#turns').innerHTML='<article><div><h4 data-conversation-role="user">You said:</h4></div><div>'+text+'</div></article>';
      };
      listeners[0]({type:'act',action:'overseer-resume-draft'},{},no=>{
        const draft=document.querySelector('#draft');draft.textContent=${JSON.stringify(marked)};
        listeners[0]({type:'probe'},{},probe=>{
          listeners[0]({type:'act',action:'overseer-resume-draft'},{},done=>{
            document.querySelector('#result').textContent=JSON.stringify({no,probe:probe.composerOverseerDraft,done,clicked,
              posted:document.querySelector('#turns article')?.textContent||''});
          });
        });
      });
    </script>`;
  const file=path.join(dir,'fixture.html');fs.writeFileSync(file,html);
  const {stdout}=await promisify(execFile)(chrome,[
    '--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
    `--user-data-dir=${path.join(folder,'profile')}`,'--dump-dom','--virtual-time-budget=18000',
    pathToFileURL(file).href],{timeout:30000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json&&json!=='pending','legacy send did not complete');
  const actual=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&'));
  assert.equal(actual.no.ok,false,'a random owner draft is protected');
  assert.equal(actual.probe,true);
  assert.equal(actual.done.ok,true);
  assert.equal(actual.clicked,1);
  assert.ok(actual.posted.includes(marked.slice(0,100)));
});

test('quick: network and conversation failure banners are detected by the real content probe', {skip:!fs.existsSync(chrome)}, async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-quick-errors-'));
  const source=fs.readFileSync(new URL('../content.js',import.meta.url),'utf8');
  const html=`<!doctype html><meta charset="utf-8">
    <main><div id="error" role="alert">No response from ChatGPT. Check your connection.</div></main>
    <pre id="result">pending</pre>
    <script>const listeners=[];window.chrome={runtime:{id:'test',onMessage:{addListener(fn){listeners.push(fn)}}}};</script>
    <script>${source}</script><script>
      listeners[0]({type:'probe'},{},first=>{
        document.querySelector('#error').textContent='Could not load this ChatGPT conversation';
        listeners[0]({type:'probe'},{},second=>{
          document.querySelector('#result').textContent=JSON.stringify({
            noResponse:first.failed,conversation:second.pageLoadFailed,failed:second.failed
          });
        });
      });
    </script>`;
  const file=path.join(folder,'fixture.html');fs.writeFileSync(file,html);
  const {stdout}=await promisify(execFile)(chrome,[
    '--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
    `--user-data-dir=${path.join(folder,'profile')}`,'--dump-dom','--virtual-time-budget=15000',
    pathToFileURL(file).href],{timeout:30000,maxBuffer:2e6,windowsHide:true});
  const json=/<pre id="result">([^<]+)<\/pre>/.exec(stdout)?.[1];
  assert.ok(json&&json!=='pending');
  const actual=JSON.parse(json.replaceAll('&quot;','"').replaceAll('&amp;','&'));
  assert.match(actual.noResponse,/No response from ChatGPT/);
  assert.match(actual.conversation,/Could not load this ChatGPT conversation/);
  assert.equal(actual.failed,'');
});
