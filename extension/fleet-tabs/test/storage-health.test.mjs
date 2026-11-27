import test from 'node:test';
import assert from 'node:assert/strict';
import {probeStorage} from '../storage-health.mjs';

test('storage health requires a write, readback and cleanup without changing settings',async()=>{
  const data={settings:{enabled:true},sessions:{one:'saved'}};
  const before=structuredClone(data);
  const api={storage:{local:{async set(p){Object.assign(data,p)},
    async get(k){return {[k]:data[k]}},async remove(k){delete data[k]}}}};
  assert.equal((await probeStorage(api)).state,'ok');
  assert.deepEqual(data,before);
});

test('sticky LevelDB no-space is distinguished from quota, unavailable API and silence',async()=>{
  for(const [message,expected] of [
    ['IO error: .../021116.ldb: FILE_ERROR_NO_SPACE (ChromeMethodBFE: 3::WritableFileAppend::8)','io-error'],
    ['QUOTA_BYTES quota exceeded','unknown'],['Extension context invalidated','unknown'],
  ]) {
    const api={storage:{local:{async set(){throw Error(message)}}}};
    assert.equal((await probeStorage(api)).state,expected);
  }
  assert.equal((await probeStorage({storage:{local:{set:()=>new Promise(()=>{})}}},5)).state,'unknown');
});

test('missing persistence is not reported as healthy',async()=>{
  const api={storage:{local:{async set(){},async get(){return {}},async remove(){}}}};
  assert.equal((await probeStorage(api)).state,'unknown');
});
