import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';

function harness(respond=async()=>({state:'granted'})) {
  const handlers={}, fallbacks={},calls=[],aborts=[],timers=[],afters=[],statuses=[];
  register((event,matcher,handler)=>{handlers[event]=handler||matcher;return {catch(fn){fallbacks[event]=fn;}};});
  const $={plugin:{root:'/plugin'},session:{id:async()=> 'session',usage:async()=>$.usage},
    process:{run:async(argv,opts)=>{
      assert.deepEqual(argv,['python3','guard.py','client']);assert.equal(opts.cwd,'/plugin');
      const data=JSON.parse(opts.stdin);calls.push(data);
      return {exitCode:0,stdout:JSON.stringify(await respond(data)),stderr:''};
    }},ui:{status(t){statuses.push(t);}},turn:{abort:async e=>{aborts.push(e.turnId);}},
    usage:{rateLimits:[]},
    command:{register:async()=>{}},env:{get:async()=>undefined},clock:{every:(ms,fn)=>{timers.push(fn);return {cancel(){}};},
      after:(ms,fn)=>{const t={ms,fn,cancelled:false,cancel(){t.cancelled=true;}};afters.push(t);return t;}}};
  return {handlers,fallbacks,calls,aborts,timers,afters,statuses,$};
}
const event={turnId:'turn',index:0};
function next(fn=async()=>({})) {fn.signal=new AbortController().signal;return fn;}
async function drain(g) {for(;;){const n=await g.next();if(n.done)return n.value;}}
async function step(h,e=event) {return drain(h.handlers['turn.step'](h.$,e,next(async function*(){return {};})));}
async function complete(h,e=event) {return h.handlers['turn.complete'](h.$,e,next());}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const released=(h,key)=>h.calls.filter(c=>c.op==='release'&&c.key===key).length;
async function start(h) {await h.handlers['session.start'](h.$,{},async()=>({}));}
async function beat(h) {for(const fn of h.timers) await fn(); await tick(); await tick();}

test('agent retains one slot across model steps and tools until turn ends',async()=>{
  const h=harness();await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Read',tool_use_id:'r'},next());
  await step(h,{...event,index:1});
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  assert.equal(new Set(h.calls.filter(c=>c.key).map(c=>c.key)).size,1);
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

test('turn completion cannot free a slot while an ordinary tool is still running',async()=>{
  const h=harness();await step(h);let finish;
  const tool=h.handlers['tool.call'](h.$,{tool:'Bash'},next(()=>new Promise(r=>{finish=r;})));
  await tick();await complete(h);
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  finish({});await tool;assert.equal(h.calls.at(-1).op,'release');
});
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
test('slots sprint starts this session or every session, and ends on off',async()=>{
  const status={limits:{agent:3},auto:{enabled:false},sprints:{},tickets:[]};
  const h=harness(async d=>d.op==='sprint'?{sprints:d.minutes?{[d.session]:1000+60*d.minutes}:{}}:
    {...status,sprints:{session:1300},tickets:[1,2,3,4].map(n=>({seq:n,state:'granted',session:'session',pid:1,idle:0}))});
  let r=await h.handlers['command.run'](h.$,{command:'slots',args:'sprint'});
  assert.deepEqual(h.calls.at(-2),{op:'sprint',session:'session',minutes:5});
  assert.match(r.text,/sprint for this session until/);assert.match(r.text,/Sprint: session session starts/);assert.match(r.text,/1 over the limit: a sprint/);
  await h.handlers['command.run'](h.$,{command:'slots',args:'sprint all 15'});
  assert.deepEqual(h.calls.at(-2),{op:'sprint',session:'*',minutes:15});
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'sprint off'});
  assert.deepEqual(h.calls.at(-2),{op:'sprint',session:'session',minutes:0});assert.match(r.text,/sprints ended/);
  for(const args of ['sprint 0','sprint 61','sprint all off','sprint 5 5','sprint x'])
    assert.match((await h.handlers['command.run'](h.$,{command:'slots',args})).text,/Usage: \/slots sprint/);
});
test('slots subcommands configure and reset, with help on misuse',async()=>{
  const status={limits:{agent:5},auto:{enabled:true,cap:5,max:5,target:95,windows:[]},tickets:[]};
  const h=harness(async d=>d.op==='configure'?{config:{agents:5,auto:true,schema:2},...status}:d.op==='reset'?{limit:5}:status);
  let r=await h.handlers['command.run'](h.$,{command:'slots',args:'agents 5'});
  assert.deepEqual(h.calls.at(-1),{op:'configure',agents:5});assert.match(r.text,/agents set to 5, applied now\.\nAgents: 0\/5 working/);
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'max 2'});
  assert.deepEqual(h.calls.at(-1),{op:'configure',max:2});assert.match(r.text,/^Slot Guard: max 2 is not above the normal limit \(5\): no maximum is set\.\n.*still goes up to 5\. To lower it, use "\/slots agents 2"/);
  assert.doesNotMatch(r.text,/applied now/);
  const off=harness(async d=>d.op==='configure'?{config:{agents:5,max:10,auto:false,schema:2},...status}:status);
  r=await off.handlers['command.run'](off.$,{command:'slots',args:'max 10'});
  assert.match(r.text,/max set to 10, saved\.\nThe automatic limit is off, so the maximum has no effect: the limit stays at 5/);
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'auto off'});
  assert.deepEqual(h.calls.at(-1),{op:'configure',auto:false});
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'quantum 90'});
  assert.deepEqual(h.calls.at(-1),{op:'configure',quantum:90});
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'reset'});
  assert.deepEqual(h.calls.at(-2),{op:'reset',usage:false});assert.match(r.text,/restarts from the normal limit \(now 5\); the usage readings stay/);
  r=await h.handlers['command.run'](h.$,{command:'slots',args:'forget'});
  assert.deepEqual(h.calls.at(-2),{op:'reset',usage:true});assert.match(r.text,/readings forgotten; the limit is 5 again/);
  const sent=h.calls.length;
  for(const args of ['agents','agents 0','agents x','auto maybe','max 1 2','reset now','forget all','bogus','help']) {
    r=await h.handlers['command.run'](h.$,{command:'slots',args});
    assert.match(r.text,/\/slots agents N {8}Set the normal limit/,args);
    assert.match(r.text,args==='help'?/^Slot Guard: one machine-wide/:args==='bogus'?/^Unknown subcommand "bogus"/:/^Usage: \/slots/,args);
  }
  assert.equal(h.calls.length,sent);
  assert.match((await h.handlers['command.run'](h.$,{command:'slots',args:''})).text,/"\/slots help" lists the subcommands/);
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
test('usage windows are reported on measure and by the heartbeat, once per change',async()=>{
  const h=harness(async d=>d.op==='usage'?{limit:2}:{state:'granted'});await start(h);
  const limits=[{kind:'five_hour',percentUsed:82,resetsAt:'2026-10-18T03:00:00Z'},{kind:'spend_limit',percentUsed:5}];
  await h.handlers['session.measure'](h.$,{changed:['rateLimits'],rateLimits:limits},async()=>({}));
  const sent=h.calls.filter(c=>c.op==='usage');
  assert.deepEqual(sent[0].readings,[{kind:'five_hour',percent:82,resets:Date.parse('2026-10-18T03:00:00Z')/1000}]);
  h.$.usage.rateLimits=limits;await beat(h);
  assert.equal(h.calls.filter(c=>c.op==='usage').length,1);
  h.$.usage.rateLimits=[{...limits[0],percentUsed:83}];await beat(h);
  assert.equal(h.calls.filter(c=>c.op==='usage').length,2);
  await h.handlers['session.measure'](h.$,{changed:['context'],rateLimits:[]},async()=>({}));
  assert.equal(h.calls.filter(c=>c.op==='usage').length,2);
});
test('slots shows the automatic limit and window capacity',async()=>{
  const h=harness(async()=>({limits:{agent:2},auto:{enabled:true,cap:3,target:95,
    windows:[{kind:'five_hour',percent:82,elapsed:78,ceiling:76.44,margin:-5.56,agents:2.4,rate:3.1,source:'measured'}]},tickets:[]}));
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:''});
  assert.match(r.text,/Auto limit: 2 \(normal 3, max 3\), pacing to 95% per window; five_hour 82% used, 78% elapsed, ceiling 76.44%, margin -5.56 pp, capacity 2.40 agents, 3.1%\/h per agent \(measured\)/);
});
test('slots distinguishes elapsed time from ceiling and available margin',async()=>{
  const h=harness(async()=>({limits:{agent:1},auto:{enabled:true,cap:6,target:98,
    windows:[{kind:'seven_day',percent:84,elapsed:86,ceiling:84.28,margin:0.28,agents:0.55,rate:0.8,source:'inherited',hours:11}]},tickets:[]}));
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:''});
  assert.match(r.text,/84% used, 86% elapsed, ceiling 84.28%, margin 0.28 pp, capacity 0.55 agents, 0.8%\/h per agent \(inherited\), ~11 h\/day working/);
});
test('slots explains a pause and when it ends',async()=>{
  const h=harness(async()=>({limits:{agent:0},auto:{enabled:true,cap:3,target:95,until:1792300000,windows:[]},tickets:[]}));
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:''});
  assert.match(r.text,/Paused: a usage window reached 95%; agents resume when it renews at .*"\/slots forget" or "\/slots auto off" resumes now/);
});
test('slots explains a pause over the weekly line and shows the capacity',async()=>{
  const weekly={kind:'seven_day',percent:59,elapsed:58.1,ceiling:56.94,margin:-2.06,agents:0,rate:0.92,source:'measured',hours:11.3};
  const h=harness(async()=>({limits:{agent:0},auto:{enabled:true,cap:3,target:95,until:1792300000,line:true,windows:[weekly]},tickets:[]}));
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:''});
  assert.match(r.text,/Paused: continuous usage ceiling reached; agents resume when the ceiling catches up at /);
  assert.match(r.text,/capacity 0\.00 agents, 0\.92%\/h per agent \(measured\), ~11\.3 h\/day working/);
});
test('a pause over the weekly line says so',async()=>{
  let n=0;const h=harness(async d=>d.op==='acquire'&&++n<3?{state:'queued',paused_until:1792300000,paused_line:true}:{state:'granted'});
  await step(h);
  assert.equal(h.statuses.filter(t=>t&&/continuous usage ceiling reached/.test(t)).length,1);
});
test('a paused agent says why and clears the notice when it resumes',async()=>{
  let n=0;const h=harness(async d=>d.op==='acquire'&&++n<3?{state:'queued',paused_until:1792300000}:{state:'granted'});
  await step(h);
  assert.equal(h.statuses.filter(t=>t&&/paused until/.test(t)).length,1);
  assert.equal(h.statuses.at(-1),undefined);
});
test('idle release waits while usage is paused',async(t)=>{
  let n=0;const h=harness(async d=>d.op==='acquire'&&++n===1?{state:'queued',paused_until:Date.now()/1000+3600}:{state:'granted'});
  await start(h);await step(h);const k=h.calls.find(c=>c.op==='acquire').key;
  const now=Date.now();t.mock.method(Date,'now',()=>now+121000);
  await beat(h);assert.equal(released(h,k),0);
});
test('an abandoned dispatch does not abort the turn again',async()=>{
  const controller=new AbortController();
  const h=harness(async()=>{controller.abort();throw new Error('aborted');});
  const n=async function*(){};n.signal=controller.signal;
  await drain(h.handlers['turn.step'](h.$,event,n));
  assert.equal(h.aborts.length,0);
});
test('a subagent turn that cannot be aborted still never reaches the model',async()=>{
  const h=harness(async()=>{throw new Error('offline');});
  h.$.turn.abort=async()=>{throw new Error('not the running turn');};
  let called=0;
  const out=await drain(h.handlers['turn.step'](h.$,{turnId:'child',agentId:'c',index:0},next(async function*(){called++;})));
  assert.equal(called,0);assert.equal(out.answer,'');
});
test('a paused subagent abandoned by the engine tells its parent to retry',async()=>{
  const controller=new AbortController();
  const h=harness(async d=>{if(d.op==='acquire'){controller.abort();return {state:'queued',paused_until:Date.now()/1000+600};}return {};});
  const n=async function*(){throw new Error('must not run');};n.signal=controller.signal;
  const out=await drain(h.handlers['turn.step'](h.$,{turnId:'c',agentId:'child',index:0},n));
  assert.match(out.answer,/Dispatch it again after/);assert.equal(h.aborts.length,0);
});

async function spawn(h,parent,child) {
  return h.handlers['agent.spawn'](h.$,{parentAgentId:parent},async()=>({agentId:child,model:'m'}));
}
const acquires=h=>h.calls.filter(c=>c.op==='acquire');
test('a parent waiting for its subagent hands it its slot',async()=>{
  const h=harness();await step(h);const parentKey=h.calls[0].key;
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>{
    assert.equal(h.calls.filter(c=>c.op==='release').length,0);
    await spawn(h,undefined,'child');
    await step(h,{turnId:'child-turn',agentId:'child',index:0});
    assert.equal(acquires(h).at(-1).key,parentKey);
    await complete(h,{turnId:'child-turn',agentId:'child'});
    assert.equal(released(h,parentKey),1);
    return {};
  }));
  await step(h,{...event,index:1});
  const resumed=acquires(h).at(-1);
  assert.notEqual(resumed.key,parentKey);assert.equal(resumed.rank,0);
});
test('a parent reclaims its slot when no subagent took it',async()=>{
  const h=harness();await step(h);const parentKey=h.calls[0].key;
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>({})));
  await step(h,{...event,index:1});
  assert.equal(acquires(h).at(-1).key,parentKey);
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
});
test('an unclaimed parked slot returns to the pool after a minute',async(t)=>{
  const h=harness();await start(h);await step(h);const parentKey=h.calls.find(c=>c.op==='acquire').key;
  let finish;const tool=h.handlers['tool.call'](h.$,{tool:'Agent'},next(()=>new Promise(r=>{finish=r;})));
  await tick();await beat(h);assert.equal(released(h,parentKey),0);
  assert.ok(h.calls.find(c=>c.op==='touch').keys.includes(parentKey));
  const now=Date.now();t.mock.method(Date,'now',()=>now+61000);
  await beat(h);assert.equal(released(h,parentKey),1);
  finish({});await tool;
});
test('parallel ordinary tool keeps the parent counted; then its slot goes to the child',async()=>{
  const h=harness();await step(h);const parentKey=h.calls[0].key;
  let finishRead,finishAgent;
  const read=h.handlers['tool.call'](h.$,{tool:'Read'},next(()=>new Promise(r=>{finishRead=r;})));
  await tick();
  const child=h.handlers['tool.call'](h.$,{tool:'Agent'},next(()=>new Promise(r=>{finishAgent=r;})));
  await tick();
  finishRead({});await read;
  await spawn(h,undefined,'kid');await step(h,{turnId:'k',agentId:'kid',index:0});
  assert.equal(acquires(h).at(-1).key,parentKey);
  assert.equal(h.calls.filter(c=>c.op==='release').length,0);
  finishAgent({});await child;
});
test('nested delegation passes one slot down and back: never more than one held',async()=>{
  const h=harness();await step(h);
  const child={turnId:'child-turn',agentId:'child',index:0};
  const grandchild={turnId:'grandchild-turn',agentId:'grandchild',index:0};
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>{
    await spawn(h,undefined,'child');await step(h,child);
    await h.handlers['tool.call'](h.$,{tool:'Agent',agentId:'child'},next(async()=>{
      await spawn(h,'child','grandchild');await step(h,grandchild);await complete(h,grandchild);return {};
    }));
    await step(h,{...child,index:1});await complete(h,child);return {};
  }));
  await step(h,{...event,index:1});await complete(h);
  let active=new Set(),peak=0;
  for(const c of h.calls){if(c.op==='acquire')active.add(c.key);else if(c.op==='release')active.delete(c.key);peak=Math.max(peak,active.size);}
  assert.equal(peak,1);assert.equal(active.size,0);
});
test('new prompts queue behind work under way',async()=>{
  const h=harness();await step(h);await step(h,{turnId:'c',agentId:'c',index:0});
  assert.deepEqual(acquires(h).map(c=>c.rank),[1,0]);
});
test('a subagent step waiting close to the stall limit is admitted by force',async(t)=>{
  let now=Date.now();t.mock.method(Date,'now',()=>now);
  const h=harness(async d=>{if(d.op!=='acquire') return {};now+=200000;return {state:d.force?'granted':'queued'};});
  await step(h,{turnId:'c',agentId:'c',index:0});
  const sent=acquires(h);assert.ok(sent.at(-1).force);assert.ok(!sent[0].force);assert.equal(sent.at(-1).wait,15);
  assert.ok(sent.slice(1,-1).every(c=>c.wait<=15));
  const m=harness(async d=>{now+=200000;return {state:d.op==='acquire'&&acquires(m).length>4?'granted':'queued'};});
  await step(m);assert.ok(!acquires(m).some(c=>c.force));
});
test('slots shows the rate per agent and any overdraft',async()=>{
  const h=harness(async()=>({limits:{agent:1},auto:{enabled:true,cap:3,max:3,target:95,
    windows:[{kind:'five_hour',percent:26,elapsed:27,ceiling:26.46,margin:0.46,agents:4.8,rate:3.9,source:'measured'},
             {kind:'seven_day',percent:9,elapsed:26,ceiling:25.48,margin:16.48,agents:0.73,rate:2.3,source:'default',hours:9.8}]},
    tickets:[{seq:1,state:'granted',session:'s',pid:2,idle:0},{seq:2,state:'granted',session:'s',pid:2,idle:0}]}));
  const r=await h.handlers['command.run'](h.$,{command:'slots',args:''});
  assert.match(r.text,/capacity 4.80 agents, 3.9%\/h per agent \(measured\)/);assert.match(r.text,/1 over the limit/);
  assert.match(r.text,/capacity 0.73 agents, 2.3%\/h per agent \(default\), ~9.8 h\/day working/);
});

const rotates=h=>h.calls.filter(c=>c.op==='rotate');
test('tools never wait for a slot; between model calls they offer it',async()=>{
  const h=harness(async d=>d.op==='acquire'?{state:'granted'}:{rotated:[]});await step(h);
  const k=h.calls[0].key;
  await h.handlers['tool.call'](h.$,{tool:'Read'},next(async()=>{
    assert.deepEqual(rotates(h).at(-1).keys,[k]);return {};}));
  assert.equal(acquires(h).length,1);
});
test('no slot is offered while a model call is still streaming',async()=>{
  const h=harness(async d=>d.op==='acquire'?{state:'granted'}:{rotated:[]});
  let release;const streaming=new Promise(r=>{release=r;});
  const g=h.handlers['turn.step'](h.$,event,next(async function*(){await streaming;return {};}));
  const pending=drain(g);await tick();await tick();
  await h.handlers['tool.call'](h.$,{tool:'Read'},next(async()=>({})));
  assert.equal(rotates(h).length,0);
  release();await pending;
});
test('a step after holding the slot asks to rotate; a claimed slot starts fresh',async()=>{
  const h=harness();await step(h);
  await step(h,{...event,index:1});
  assert.equal(acquires(h)[0].rotate,undefined);assert.equal(acquires(h)[1].rotate,true);
  await h.handlers['tool.call'](h.$,{tool:'Agent'},next(async()=>{
    await spawn(h,undefined,'kid');await step(h,{turnId:'k',agentId:'kid',index:0});return {};}));
  assert.equal(acquires(h).at(-1).fresh,true);assert.equal(acquires(h).at(-1).rotate,undefined);
});
test('a tool that runs past the heartbeat offers its slot; idle agents do not',async()=>{
  const h=harness(async d=>d.op==='acquire'?{state:'granted'}:{});await start(h);await step(h);
  const k=acquires(h)[0].key;
  let finish;const tool=h.handlers['tool.call'](h.$,{tool:'Bash',command:'make'},next(()=>new Promise(r=>{finish=r;})));
  await tick();const before=rotates(h).length;
  await beat(h);assert.deepEqual(rotates(h).at(-1).keys,[k]);assert.equal(rotates(h).length,before+1);
  finish({});await tool;
  await beat(h);assert.equal(rotates(h).length,before+1);
});
test('a queued (rotated) ticket makes the next model call wait, not the tools',async()=>{
  let queued=false;
  const h=harness(async d=>{if(d.op==='rotate'){queued=true;return {rotated:d.keys};}
    if(d.op==='acquire'&&queued){queued=false;return {state:'queued'};}return {state:'granted'};});
  await step(h);
  await h.handlers['tool.call'](h.$,{tool:'Read'},next(async()=>({})));
  assert.equal(acquires(h).length,1);
  await step(h,{...event,index:1});
  assert.equal(acquires(h).length,3);  // queued once, then granted
});
test('a heartbeat offer never lands on a model call that started meanwhile',async()=>{
  let holdTouch,touching;
  const h=harness(async d=>{
    if(d.op==='touch') {touching=true; await new Promise(r=>{holdTouch=r;}); return {};}
    return d.op==='acquire'?{state:'granted'}:{rotated:[]};
  });
  await start(h);await step(h);
  let finishTool;const tool=h.handlers['tool.call'](h.$,{tool:'Bash',command:'make'},next(()=>new Promise(r=>{finishTool=r;})));
  await tick();const before=rotates(h).length;
  const beating=Promise.resolve(h.timers[0]());   // picks the agent in its long tool, then waits on touch
  while(!touching) await tick();
  finishTool({});await tool;                      // the tool ends...
  let endStep;const stepping=drain(h.handlers['turn.step'](h.$,{...event,index:1},
    next(async function*(){await new Promise(r=>{endStep=r;});return {};})));
  while(!endStep) await tick();                   // ...and a model call is streaming
  holdTouch();await beating;await tick();await tick();
  assert.equal(rotates(h).length,before);
  endStep();await stepping;
});
