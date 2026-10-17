import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';

function harness(respond) {
  const handlers = {}, fallbacks = {}, calls = [], aborts = [];
  register((event, matcher, handler) => { handlers[event] = handler || matcher; return {catch(fn) {fallbacks[event]=fn;}}; });
  const $ = {plugin:{root:'/plugin'}, session:{id:async()=> 'session'},
    process:{run:async(argv,opts)=>{const data=JSON.parse(opts.stdin);calls.push(data);
      return {exitCode:0,stdout:JSON.stringify(await respond(data)),stderr:''};}},
    ui:{status(){}}, turn:{abort:async e => {aborts.push(e.turnId);}}};
  return {handlers,fallbacks,$,calls,aborts};
}
const event = {turnId:'turn',index:0};
async function consume(gen) { for(;;) {const n=await gen.next(); if(n.done)return n.value;} }

test('main acquires both pools; request released after stream, job only after turn',async()=>{
  const h=harness(async d=>({state:d.op==='acquire'?'granted':'released'}));
  let invoked=0;
  const next=async function*(){invoked++; yield {kind:'text',text:'OK'}; return {answer:'OK'};};
  next.signal=new AbortController().signal;
  await consume(h.handlers['turn.step'](h.$,event,next));
  assert.equal(invoked,1);
  assert.deepEqual(h.calls.map(d=>[d.op,d.pool]),[['acquire','job'],['acquire','request'],['release','request']]);
  await h.handlers['turn.complete'](h.$,event,async()=>({}));
  assert.equal(h.calls.at(-1).pool,'job');
  assert.equal(h.calls.at(-1).op,'release');
});

test('subagent only acquires inference; never competes with its parent job',async()=>{
  const h=harness(async d=>({state:d.op==='acquire'?'granted':'released'}));
  const next=async function*(){return {};};next.signal=new AbortController().signal;
  await consume(h.handlers['turn.step'](h.$,{...event,agentId:'child'},next));
  assert.deepEqual(h.calls.map(d=>d.pool),['request','request']);
});

test('coordinator outage aborts without any inference',async()=>{
  const h=harness(async()=>{throw Error('offline');});
  let invoked=0;
  const next=async function*(){invoked++;};next.signal=new AbortController().signal;
  await consume(h.handlers['turn.step'](h.$,event,next));
  assert.equal(invoked,0);assert.equal(h.aborts.length,1);
});

test('wait preserves the same FIFO ticket',async()=>{
  let count=0;
  const h=harness(async d=>({state:d.pool==='job'&&d.op==='acquire'&&++count<3?'queued':'granted',position:2}));
  const next=async function*(){return {};};next.signal=new AbortController().signal;
  await consume(h.handlers['turn.step'](h.$,event,next));
  const jobs=h.calls.filter(d=>d.pool==='job');
  assert.equal(jobs.length,3);assert.equal(new Set(jobs.map(d=>d.key)).size,1);
});

test('cancellation racing with grant releases both and makes no inference',async()=>{
  const controller=new AbortController();
  const h=harness(async d=>{if(d.op==='acquire')controller.abort();return {state:'granted'};});
  let invoked=0;const next=async function*(){invoked++;};next.signal=controller.signal;
  await consume(h.handlers['turn.step'](h.$,event,next));
  assert.equal(invoked,0);
  assert.deepEqual(h.calls.filter(d=>d.op==='release').map(d=>d.pool),['request','job']);
});

test('runtime failure fallback never calls next',async()=>{
  const h=harness(async()=>({}));
  const result=await consume(h.fallbacks['turn.step'](h.$,event));
  assert.equal(result.usage,null);assert.equal(h.aborts.length,1);
});
