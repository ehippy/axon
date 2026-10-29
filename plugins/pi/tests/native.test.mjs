import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

async function waitFor(check) {
  const until=Date.now()+4000;
  while(Date.now()<until) { if(check())return; await delay(10); }
  assert.fail('Native bridge did not reach the expected state');
}

async function harness(t, fail=false) {
  const root=mkdtempSync(join(tmpdir(),'axon-pi-native-'));
  const worker=join(root,'worker'),log=join(root,'calls.jsonl');
  writeFileSync(worker,`#!${process.execPath}
const fs=require('node:fs'),readline=require('node:readline');
const log=${JSON.stringify(log)};
const record=x=>fs.appendFileSync(log,JSON.stringify(x)+'\\n');
if(!process.argv.includes('inbox')){process.stdin.resume();process.stdin.on('end',()=>{console.log('{}');});}
else {
 record({pid:process.pid});let configured=false;
 readline.createInterface({input:process.stdin}).on('line',line=>{
  if(!configured){configured=true;record({config:JSON.parse(line)});console.log(JSON.stringify({messages:[{id:'one',text:'Dashboard input'}]}));}
  else record({ack:JSON.parse(line)});
 });
}
`,{mode:0o755});
  const previous=process.env.AXON_PYTHON;process.env.AXON_PYTHON=worker;
  const {default:extension}=await import('../index.ts?test='+Math.random());
  const callbacks=new Map(),sent=[];
  const pi={on:(name,callback)=>callbacks.set(name,callback),sendUserMessage:(text,options)=>{sent.push({text,options});if(fail)throw Error('Native rejection');}};
  extension(pi);
  const ctx={cwd:root,sessionManager:{getSessionId:()=> 'private-native-session'}};
  const rows=()=>{try{return readFileSync(log,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);}catch{return [];}};
  t.after(async()=>{
    await callbacks.get('session_shutdown')({reason:'reload'},ctx);
    previous===undefined?delete process.env.AXON_PYTHON:process.env.AXON_PYTHON=previous;
    await delay(100);rmSync(root,{recursive:true,force:true});
  });
  return {callbacks,sent,ctx,rows};
}

test('an idle or busy session receives a claimed batch once as followUp',async t=>{
  const h=await harness(t);
  await h.callbacks.get('session_start')({},h.ctx);
  await waitFor(()=>h.rows().some(row=>row.ack));
  assert.equal(h.sent.length,1);
  assert.match(h.sent[0].text,/Dashboard input/);
  assert.deepEqual(h.sent[0].options,{deliverAs:'followUp'});
  assert.equal(h.rows().find(row=>row.ack).ack.accepted,true);
  assert.equal(h.rows().find(row=>row.config).config.session_id,'private-native-session');
  const pid=h.rows().find(row=>row.pid).pid;
  await h.callbacks.get('session_shutdown')({reason:'reload'},h.ctx);
  await waitFor(()=>{try{process.kill(pid,0);return false;}catch{return true;}});
});

test('a native API rejection is not acknowledged as delivery',async t=>{
  const h=await harness(t,true);
  await h.callbacks.get('session_start')({},h.ctx);
  await waitFor(()=>h.rows().some(row=>row.ack));
  assert.equal(h.sent.length,1);
  assert.equal(h.rows().find(row=>row.ack).ack.accepted,false);
});
