import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';

function harness(respond=async()=>({state:'granted'})) {
  const handlers={}, fallbacks={},calls=[],aborts=[];
  register((event,matcher,handler)=>{handlers[event]=handler||matcher;return {catch(fn){fallbacks[event]=fn;}};});
  const $={plugin:{root:'/plugin'},session:{id:async()=> 'session'},
    process:{run:async(argv,opts)=>{
      assert.deepEqual(argv,['python3','guard.py','client']);assert.equal(opts.cwd,'/plugin');
      const data=JSON.parse(opts.stdin);calls.push(data);
      return {exitCode:0,stdout:JSON.stringify(await respond(data)),stderr:''};
    }},ui:{status(){}},turn:{abort:async e=>{aborts.push(e.turnId);}}};
  return {handlers,fallbacks,calls,aborts,$};
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
  assert.ok((await h.fallbacks['tool.call']()).deny);
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
