import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';

function harness(respond=async()=>({state:'granted'})) {
  const handlers={}, fallbacks={},calls=[],aborts=[],timers=[],afters=[];
  register((event,matcher,handler)=>{handlers[event]=handler||matcher;return {catch(fn){fallbacks[event]=fn;}};});
  const $={plugin:{root:'/plugin'},session:{id:async()=> 'session'},
    process:{run:async(argv,opts)=>{
      assert.deepEqual(argv,['python3','guard.py','client']);assert.equal(opts.cwd,'/plugin');
      const data=JSON.parse(opts.stdin);calls.push(data);
      return {exitCode:0,stdout:JSON.stringify(await respond(data)),stderr:''};
    }},ui:{status(){}},turn:{abort:async e=>{aborts.push(e.turnId);}},
    command:{register:async()=>{}},env:{get:async()=>undefined},clock:{every:(ms,fn)=>{timers.push(fn);return {cancel(){}};},
      after:(ms,fn)=>{const t={ms,fn,cancelled:false,cancel(){t.cancelled=true;}};afters.push(t);return t;}}};
  return {handlers,fallbacks,calls,aborts,timers,afters,$};
}
const event={turnId:'turn',index:0};
function next(fn=async()=>({})) {fn.signal=new AbortController().signal;return fn;}
async function drain(g) {for(;;){const n=await g.next();if(n.done)return n.value;}}
async function step(h,e=event) {return drain(h.handlers['turn.step'](h.$,e,next(async function*(){return {};})));}
async function complete(h,e=event) {return h.handlers['turn.complete'](h.$,e,next());}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));

test('agent retains one slot across model steps and tools until turn ends',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Read',tool_use_id:'r'},next());
  await step(h,{...event,index:1});
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  assert.equal(new Set(h.calls.map(c=>c.key)).size,1);
  await complete(h);assert.equal(h.calls.at(-1).op,'release');
});
test('main and subagent acquire the same pool and release independently',async()=>{
  const h=harness();await step(h);await step(h,{...event,agentId:'child',turnId:'child-turn'});
  assert.deepEqual(h.calls.map(c=>c.pool),['agent','agent']);
  assert.notEqual(h.calls[0].key,h.calls[1].key);
  await complete(h,{...event,agentId:'child',turnId:'child-turn'});
  assert.equal(h.calls.filter(c=>c.op==='release').length,1);
  await complete(h);assert.equal(h.calls.filter(c=>c.op==='release').length,2);
});
test('foreground child wait yields parent slot; parent reacquires with a new FIFO ticket',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>{
    assert.equal(h.calls.at(-1).op,'release');
    await step(h,{...event,agentId:'child',turnId:'child-turn'});
    await complete(h,{...event,agentId:'child',turnId:'child-turn'});return {};
  }));
  const count=h.calls.length;
  await step(h,{...event,index:1});
  assert.equal(h.calls[count].op,'acquire');assert.notEqual(h.calls[0].key,h.calls[count].key);
});
test('parallel ordinary tool keeps parent counted while another tool waits for a child',async()=>{
  const h=harness();await step(h);
  let finishRead,finishAgent;
  const read=h.handlers['tool.call'](h.$,{tool:'Read'},next(()=>new Promise(r=>{finishRead=r;})));
  await tick();
  const child=h.handlers['tool.call'](h.$,{tool:'Agent'},next(()=>new Promise(r=>{finishAgent=r;})));
  await tick();assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  finishRead({});await read;assert.equal(h.calls.at(-1).op,'release');
  finishAgent({});await child;
});
test('TaskOutput waiting yields but nonblocking polling retains slot',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'TaskOutput',block:false},next());
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  await h.handlers['tool.call'](h.$,{tool:'TaskOutput',block:true},next());
  assert.equal(h.calls.at(-1).op,'release');
});
test('outage and fallback never call the model',async()=>{
  const h=harness(async()=>{throw new Error('offline');});let called=0;
  await drain(h.handlers['turn.step'](h.$,event,next(async function*(){called++;}))).catch(()=>{});
  assert.equal(called,0);
  await drain(h.fallbacks['turn.step'](h.$,event));assert.ok(h.aborts.length);
  const notCalled=next();notCalled.called=false;
  assert.ok((await h.fallbacks['tool.call'](h.$,{tool:'Bash'},notCalled)).deny);
});
test('cancel racing with grant releases ticket without entering model',async()=>{
  const controller=new AbortController();const h=harness(async d=>{if(d.op==='acquire')controller.abort();return {state:'granted'};});
  let called=0;const n=async function*(){called++;};n.signal=controller.signal;
  await drain(h.handlers['turn.step'](h.$,event,n));
  assert.equal(called,0);assert.equal(h.calls.at(-1).op,'release');
});
test('queued polls preserve one FIFO ticket',async()=>{
  let tries=0;const h=harness(async d=>({state:d.op==='acquire' && ++tries<3?'queued':'granted'}));
  await step(h);assert.equal(new Set(h.calls.map(c=>c.key)).size,1);
  assert.deepEqual(h.calls.map(c=>c.wait),[0,15,15]);
});
test('tools outside an admitted turn are denied',async()=>{
  const h=harness();let invoked=false;
  const result=await h.handlers['tool.call'](h.$,{tool:'Bash'},next(async()=>{invoked=true;}));
  assert.ok(result.deny);assert.equal(invoked,false);
});

test('nested delegation yields each ancestor and reacquires before continuing',async()=>{
  const h=harness();await step(h);
  const child={turnId:'child-turn',agentId:'child',index:0};
  const grandchild={turnId:'grandchild-turn',agentId:'grandchild',index:0};
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>{
    await step(h,child);
    await h.handlers['tool.call'](h.$,{tool:'Agent',agentId:'child'},next(async()=>{
      await step(h,grandchild);await complete(h,grandchild);return {};
    }));
    await step(h,{...child,index:1});await complete(h,child);return {};
  }));
  await step(h,{...event,index:1});await complete(h);
  let active=new Set(),peak=0;
  for(const c of h.calls){if(c.op==='acquire')active.add(c.key);else if(c.op==='release')active.delete(c.key);peak=Math.max(peak,active.size);}
  assert.equal(peak,1);assert.equal(active.size,0);
});
test('turn completion cannot free a slot while an ordinary tool is still running',async()=>{
  const h=harness();await step(h);let finish;
  const tool=h.handlers['tool.call'](h.$,{tool:'Bash'},next(()=>new Promise(r=>{finish=r;})));
  await tick();await complete(h);
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  finish({});await tool;assert.equal(h.calls.at(-1).op,'release');
});
test('ordinary work resuming while a child is pending obtains a slot first',async()=>{
  const h=harness();await step(h);let finish;
  const child=h.handlers['tool.call'](h.$,{tool:'Agent'},next(()=>new Promise(r=>{finish=r;})));
  await tick();assert.equal(h.calls.at(-1).op,'release');
  await h.handlers['tool.call'](h.$,{tool:'Read'},next(async()=>{assert.equal(h.calls.at(-1).op,'acquire');return {};}));
  assert.equal(h.calls.at(-1).op,'release');finish({});await child;
});

const released=(h,key)=>h.calls.filter(c=>c.op==='release'&&c.key===key).length;
async function start(h) {await h.handlers['session.start'](h.$,{},async()=>({}));}
async function beat(h) {for(const fn of h.timers) await fn(); await tick(); await tick();}

test('failed release is retried at turn end, then by the heartbeat',async()=>{
  let failures=2;const h=harness();
  h.$.process.run=(orig=>async(argv,opts)=>{const d=JSON.parse(opts.stdin);
    if(d.op==='release'&&failures>0){h.calls.push(d);failures--;return {exitCode:1,stdout:'',stderr:'lock timeout'};}
    return orig(argv,opts);})(h.$.process.run);
  await start(h);await step(h);const k=h.calls.find(c=>c.op==='acquire').key;
  await complete(h);assert.equal(released(h,k),2);
  await beat(h);assert.equal(released(h,k),3);
  await beat(h);assert.equal(released(h,k),3);
});
test('interrupted acquire is released by the next live dispatch',async()=>{
  const controller=new AbortController();
  const h=harness(async d=>{if(d.op==='acquire'){controller.abort();return {state:'queued'};}return {state:'released'};});
  h.$.process.run=(orig=>async(argv,opts)=>{const d=JSON.parse(opts.stdin);
    if(controller.signal.aborted&&d.op==='release'&&!h.retried){h.retried=true;h.calls.push(d);throw new Error('aborted');}
    return orig(argv,opts);})(h.$.process.run);
  const n=async function*(){};n.signal=controller.signal;
  await drain(h.handlers['turn.step'](h.$,event,n));
  const k=h.calls[0].key;assert.equal(released(h,k),1);
  await complete(h);assert.equal(released(h,k),2);
});
test('session start adopts tickets of a previous load and heartbeats held tickets',async()=>{
  const h=harness();await start(h);
  assert.equal(h.calls[0].op,'adopt');assert.ok(h.calls[0].instance);
  await step(h);await beat(h);
  const touch=h.calls.find(c=>c.op==='touch');
  assert.deepEqual(touch.keys,[h.calls.find(c=>c.op==='acquire').key]);
});
test('heartbeat releases a ticket nobody has used past the idle limit',async(t)=>{
  const h=harness();await start(h);await step(h);
  const k=h.calls.find(c=>c.op==='acquire').key;
  const now=Date.now();t.mock.method(Date,'now',()=>now+121000);
  await beat(h);assert.equal(released(h,k),1);
  assert.equal(h.calls.filter(c=>c.op==='touch').length,0);
});
test('heartbeat keeps renewing while a long tool runs',async(t)=>{
  const h=harness();await start(h);await step(h);let finish;
  const tool=h.handlers['tool.call'](h.$,{tool:'Bash',command:'make'},next(()=>new Promise(r=>{finish=r;})));
  await tick();const now=Date.now();t.mock.method(Date,'now',()=>now+600000);
  await beat(h);assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  assert.equal(h.calls.filter(c=>c.op==='touch').length,1);
  finish({});await tool;
});
test('a lost turn.complete does not wedge the agent',async()=>{
  const h=harness();await step(h);const k=h.calls[0].key;
  let called=0;
  await drain(h.handlers['turn.step'](h.$,{turnId:'turn-2',index:0},next(async function*(){called++;return {};})));
  assert.equal(called,1);assert.equal(released(h,k),1);assert.equal(h.aborts.length,0);
});
test('turn end keys by agent, not session: a session id change still releases',async()=>{
  const h=harness();await step(h);
  h.$.session.id=async()=>'after-clear';
  await complete(h);assert.equal(h.calls.at(-1).op,'release');
});
test('permission ask yields the slot while the person decides',async()=>{
  const h=harness();await step(h);let decide;
  const tool=h.handlers['tool.call'](h.$,{tool:'Bash',command:'rm x',tool_use_id:'u1'},next(async()=>{
    const verdict=await h.handlers['tool.check'](h.$,{tool:'Bash',tool_use_id:'u1'},async()=>({decision:'ask'}));
    assert.equal(verdict.decision,'ask');
    await tick();assert.equal(h.calls.filter(c=>c.op==='release').length,0);
    assert.equal(h.afters[0].ms,15000);h.afters[0].fn();
    await tick();await tick();
    assert.equal(h.calls.at(-1).op,'release');
    await new Promise(r=>{decide=r;});return {};
  }));
  while(!decide) await tick();
  decide();await tool;
  assert.equal(h.calls.filter(c=>c.op==='release').length,1);
  const before=h.calls.length;await step(h,{...event,index:1});
  assert.equal(h.calls[before].op,'acquire');
});
test('allowed tools keep the slot through tool.check',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Read',tool_use_id:'u2'},next(async()=>
    h.handlers['tool.check'](h.$,{tool:'Read',tool_use_id:'u2'},async()=>({decision:'allow'}))));
  await tick();assert.equal(h.calls.filter(c=>c.op==='release').length,0);
});
test('questions to the person and nested claude yield the slot',async()=>{
  for(const e of [{tool:'AskUserQuestion'},{tool:'ExitPlanMode'},{tool:'Bash',command:'cd x && claude -p hi'},{tool:'Bash',command:'~/.local/bin/claude -p hi'}]) {
    const h=harness();await step(h);
    await h.handlers['tool.call'](h.$,e,next(async()=>{assert.equal(h.calls.at(-1).op,'release');return {};}));
  }
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Bash',command:'echo claudette'},next());
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
});
test('tool.call fallback replays a tool that already ran instead of denying it',async()=>{
  const h=harness();const ran=next(async()=>({result:'done'}));ran.called=true;
  assert.deepEqual(await h.fallbacks['tool.call'](h.$,{tool:'Bash'},ran),{result:'done'});
});
test('an evicted or expired ticket rejoins the queue once, then stops the turn',async()=>{
  let n=0;const h=harness(async d=>({state:d.op==='acquire'&&++n===1?'released':'granted'}));
  await step(h);const keys=h.calls.filter(c=>c.op==='acquire').map(c=>c.key);
  assert.equal(keys.length,2);assert.notEqual(keys[0],keys[1]);assert.equal(h.aborts.length,0);
  const g=harness(async()=>({state:'released'}));await step(g);assert.equal(g.aborts.length,1);
});
test('slots command evicts by ticket number',async()=>{
  const h=harness(async d=>d.op==='evict'?{evicted:d.seqs.length}:{state:'granted'});
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:'evict #12 13'});
  assert.deepEqual(h.calls.at(-1),{op:'evict',seqs:[12,13]});assert.match(r.text,/2 ticket/);
  assert.match((await h.handlers['command.run'](h.$,{command:'slots',args:'evict x'})).text,/Usage/);
});
test('an ask answered quickly (auto mode classifier) keeps the slot',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Bash',command:'ls',tool_use_id:'u3'},next(async()=>
    h.handlers['tool.check'](h.$,{tool:'Bash',tool_use_id:'u3'},async()=>({decision:'ask'}))));
  assert.equal(h.afters.length,1);assert.ok(h.afters[0].cancelled);
  h.afters[0].fn();await tick();await tick();
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  await step(h,{...event,index:1});
  assert.equal(new Set(h.calls.filter(c=>c.op==='acquire').map(c=>c.key)).size,1);
});
