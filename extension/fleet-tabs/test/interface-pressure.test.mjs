import {test} from 'node:test';
import assert from 'node:assert/strict';
import {interfacePlan} from '../interface-pressure.js';

test('two broken interfaces retained, draft and owner untouched, stale queue retired',()=>{
  const rows=Array.from({length:5},(_,i)=>({id:i,label:'f'+i,own:true,url:'https://chatgpt.com/c/abcdefab-'+i,
    page:{pageLoadFailed:'failed',composerEmpty:true}}));
  rows[3].page.composerEmpty=false;
  rows[4].label='owner';
  const p=interfacePlan(rows,[{label:'f2',task:'P/TZ-1'}],{gone:{url:'old',at:1}});
  assert.deepEqual(p.close,[2]);
  assert.deepEqual(Object.keys(p.queue),['f2']);
  assert.equal(p.degraded,true);
});

test('existing healthy idle tabs also obey four-interface cap and retain chat URLs',()=>{
  const rows=Array.from({length:7},(_,i)=>({id:i,label:'f'+i,own:true,url:'https://chatgpt.com/c/abcdefab-'+i,
    page:{composerEmpty:true,generating:false}}));
  const p=interfacePlan(rows,rows.map(r=>({label:r.label,task:'P/TZ-1'})));
  assert.equal(p.close.length,3);
  assert.equal(Object.keys(p.queue).length,3);
  assert.equal(rows.length-p.close.length,4);
});

test('visible reopened chats and corrupt queue entries cannot hold admission forever',()=>{
  const rows=[{id:1,label:'f1',own:true,url:'https://chatgpt.com/c/abcdefab-1',page:{composerEmpty:true}}];
  const workers=['f1','f2'].map(label=>({label,task:'P/TZ-1'}));
  const p=interfacePlan(rows,workers,{f1:{url:rows[0].url,at:1},f2:null});
  assert.deepEqual(p.queue,{});
  assert.equal(p.degraded,false);
});
