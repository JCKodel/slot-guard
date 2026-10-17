// One slot per working agent, held across model steps and ordinary tools.
const HEARTBEAT_MS=30000;  // Renews held tickets; the coordinator's lease is 300 s.
const IDLE_MS=120000;      // A ticket nobody is using this long is released.
const FORGET_MS=600000;    // Unused agent states are dropped after this.
const instance=Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
const emptyStep = e => ({turnId:e.turnId,index:e.index,answer:'',toolUses:[],stopReason:null,usage:null});
async function call($, data) {
  const r = await $.process.run(['python3','guard.py','client'], {
    cwd:$.plugin.root, stdin:JSON.stringify(data), timeoutMs:25000,
  });
  if (r.exitCode !== 0) throw new Error('Coordinator unavailable: '+r.stderr.trim());
  const result=JSON.parse(r.stdout);
  if(result.error) throw new Error(result.error);
  return result;
}
// Resolves true when granted, false when the ticket was released (canceled, evicted, expired).
async function acquire($, ticket, signal) {
  let first=true;
  for (;;) {
    signal.throwIfAborted();
    const r=await call($,{...ticket,op:'acquire',wait:first?0:15});
    first=false;
    signal.throwIfAborted();
    if(r.state==='granted') return true;
    if(r.state!=='queued') return false;
  }
}
// Serialize state transitions, not work: tools of one agent can run in parallel.
function locked(state, fn) {
  const result=state.tail.then(fn);
  state.tail=result.catch(()=>{});
  return result;
}
function newState(registry,session,e) {
  return {registry,session,turn:e.turnId,agent:e.agentId||'main',generation:0,ticket:null,
    busy:0,waiting:0,acquiring:false,closed:false,active:Date.now(),tail:Promise.resolve()};
}
// Never throws: a failed release is retried later, and the lease is the last resort.
async function release($, state) {
  const ticket=state.ticket;
  if(!ticket) return;
  state.ticket=null; state.registry.held.delete(state);
  try {await call($,{...ticket,op:'release'});}
  catch {state.registry.orphans.set(ticket.key,ticket);}
}
async function enter($,state,signal) {
  return locked(state,async()=>{
    if(state.closed) throw new Error('Agent turn already ended');
    for(let attempt=0;;attempt++) {
      if(!state.ticket) {
        state.ticket={pool:'agent',instance,session:state.session,turn:state.turn,
          key:JSON.stringify([instance,state.agent,state.turn,++state.generation])};
        state.registry.held.add(state);
      }
      let granted;
      state.acquiring=true;
      try {granted=await acquire($,state.ticket,signal);}
      catch(error) {await release($,state); throw error;}
      finally {state.acquiring=false;}
      if(granted) break;
      // An evicted or expired ticket rejoins the back of the queue once.
      state.ticket=null; state.registry.held.delete(state);
      if(attempt>0) throw new Error('Reservation canceled');
    }
    state.busy++; state.active=Date.now();
  });
}
async function leave($,state) {
  return locked(state,async()=>{
    state.busy--; state.active=Date.now();
    if(state.busy===0 && (state.waiting>0 || state.closed)) await release($,state);
  });
}
// Waiting for others (children, a nested claude, the person) gives the slot back.
async function suspend($,state) {
  await locked(state,async()=>{
    state.waiting++;
    if(state.busy===0) await release($,state);
  });
}
async function resume(state) {
  await locked(state,async()=>{state.waiting--; state.active=Date.now();});
}
async function close($,state) {
  await locked(state,async()=>{
    state.closed=true;
    if(state.busy===0) await release($,state);
  });
}
const waitsForOthers = e => e.tool==='Agent' || e.tool==='Task' ||
  (e.tool==='TaskOutput' && e.block!==false) ||
  e.tool==='AskUserQuestion' || e.tool==='ExitPlanMode' ||
  // A nested claude takes its own slot; holding ours while it waits could deadlock.
  (e.tool==='Bash' && typeof e.command==='string' && /(^|[\s;&|(`$/])claude(\s|$)/.test(e.command));

// An interrupted dispatch aborts its own calls, its release too: retry from a live one.
async function flush($,orphans) {
  for(const [key,ticket] of orphans) {
    try {await call($,{...ticket,op:'release'}); orphans.delete(key);} catch {}
  }
}
async function heartbeat($,{held,orphans}) {
  const now=Date.now(), keys=[];
  for(const state of held) {
    if(!state.ticket) {held.delete(state); continue;}
    if(state.busy===0 && state.waiting===0 && !state.acquiring && now-state.active>IDLE_MS) {
      // A turn whose end was never heard must not keep its slot.
      await locked(state,async()=>{
        if(state.busy===0 && !state.acquiring && Date.now()-state.active>IDLE_MS) await release($,state);
      });
      continue;
    }
    keys.push(state.ticket.key);
  }
  if(keys.length) await call($,{op:'touch',keys}).catch(()=>{});
  await flush($,orphans);
}

export function register(on) {
  const states=new Map();
  // States holding a ticket (closed ones still unwinding too), and tickets whose release failed.
  const registry={held:new Set(),orphans:new Map()};
  const pending=new Map();  // tool_use_id -> the admitted call, so a permission ask can yield it.
  const key=e=>e.agentId||'main';
  let timer=null;
  on('session.start',async($,e,next)=>{
    await $.command.register({name:'slots',description:'Show the global working-agent limit and FIFO queue; "/slots evict N" frees ticket N.',
      argumentHint:'[evict N...]',immediate:true});
    if(!timer) {
      // Tickets a previous load of this mod held in this process can no longer be released by it.
      await call($,{op:'adopt',instance}).catch(()=>{});
      const every=Number(await $.env.get('SLOT_HEARTBEAT_MS'))||HEARTBEAT_MS;  // Tests shorten it.
      timer=$.clock.every(every,()=>{
        heartbeat($,registry).catch(()=>{});
        const now=Date.now();
        for(const [k,state] of states)
          if(!state.ticket && state.busy===0 && state.waiting===0 && now-state.active>FORGET_MS) states.delete(k);
      });
    }
    return next(e);
  });
  on('command.run',{command:'slots'},async($,e)=>{
    try {
      const words=String(e.args||'').trim().split(/\s+/).filter(Boolean);
      if(words[0]==='evict') {
        const seqs=words.slice(1).map(w=>Number(w.replace(/^#/,'')));
        if(!seqs.length || seqs.some(n=>!Number.isInteger(n))) return {text:'Usage: /slots evict N [N...]'};
        const r=await call($,{op:'evict',seqs});
        return {text:`Slot Guard: ${r.evicted} ticket(s) released.`};
      }
      const s=await call($,{op:'status'});
      const count=state=>s.tickets.filter(t=>t.state===state).length;
      const lines=s.tickets.map(t=>`#${t.seq} ${t.state} session ${t.session.slice(0,8)} pid ${t.pid}, renewed ${t.idle}s ago`);
      return {text:[`Agents: ${count('granted')}/${s.limits.agent} working; ${count('queued')} waiting (FIFO). Parents waiting only for children yield their slot.`,...lines].join('\n')};
    } catch(error) {return {text:'Slot Guard: '+String(error)};}
  });
  on('turn.step',async function*($,e,next){
    const k=key(e);
    let state=states.get(k);
    if(!state || state.turn!==e.turnId) {
      // A lost turn.complete must not wedge the agent or keep its slot.
      if(state && !state.closed) await close($,state);
      state=newState(registry,await $.session.id(),e); states.set(k,state);
    }
    let entered=false;
    try {
      await flush($,registry.orphans);
      await enter($,state,next.signal); entered=true;
      return yield* next(e);
    } catch(error) {
      $.ui.status('Slot Guard: turn stopped — '+String(error));
      await $.turn.abort({turnId:e.turnId});
      return emptyStep(e);
    } finally {
      if(entered) await leave($,state);
    }
  }).catch(async function*($,e){
    try {await $.turn.abort({turnId:e.turnId});} catch {}
    return emptyStep(e);
  });
  on('tool.call',async($,e,next)=>{
    const state=states.get(key(e));
    // Tools outside an admitted turn must not silently escape the limiter.
    if(!state || state.closed) return {deny:'Slot Guard: no admitted agent turn for this tool'};
    if(waitsForOthers(e)) {
      await suspend($,state);
      try {return await next(e);}
      finally {await resume(state);}
      // Reacquire at the next actual model/tool operation, never while still waiting.
    }
    await enter($,state,next.signal);
    const admitted={state,suspended:false,done:false};
    if(e.tool_use_id) pending.set(e.tool_use_id,admitted);
    try {return await next(e);}
    finally {
      if(e.tool_use_id) pending.delete(e.tool_use_id);
      await locked(state,async()=>{
        admitted.done=true;
        if(admitted.suspended) {state.waiting--; state.active=Date.now(); return;}
        state.busy--; state.active=Date.now();
        if(state.busy===0 && (state.waiting>0 || state.closed)) await release($,state);
      });
    }
  }).catch(($,e,next)=>next.called ? next(e) : {deny:'Slot Guard: could not admit tool execution'});
  // A permission ask waits for the person: yield the slot until the agent works again.
  on('tool.check',async($,e,next)=>{
    const verdict=await next(e);
    const admitted=e.tool_use_id && pending.get(e.tool_use_id);
    if(admitted && verdict.decision==='ask') {
      const state=admitted.state;
      // The agent holds a granted ticket here, so its lock is only held briefly.
      await locked(state,async()=>{
        if(admitted.done || admitted.suspended) return;
        admitted.suspended=true; state.busy--; state.waiting++;
        if(state.busy===0) await release($,state);
      });
    }
    return verdict;
  }).catch(($,e,next)=>next(e));  // Not a gate: a failure here keeps the engine's verdict.
  on('turn.complete',async($,e,next)=>{
    const k=key(e), state=states.get(k);
    if(state && state.turn===e.turnId) {
      // A tool still unwinding keeps the closed state and releases its last slot.
      states.delete(k);
      await close($,state);
    }
    await flush($,registry.orphans);
    return next(e);
  }).catch(($,e,next)=>next(e));
}
