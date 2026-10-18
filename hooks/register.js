// One slot per working agent, held across model steps and ordinary tools.
const HEARTBEAT_MS=30000;  // Renews held tickets; the coordinator's lease is 300 s.
const IDLE_MS=120000;      // A ticket nobody is using this long is released.
const ASK_GRACE_MS=15000;  // An ask still open this long waits for the person, not auto mode's classifier.
const FORGET_MS=600000;    // Unused agent states are dropped after this.
const PARK_MS=60000;       // A parent's slot left for its subagent goes back to the pool after this.
// Claude Code fails a subagent whose model step shows no progress for 600 s, counted
// from the step's start; one waiting this long is admitted over the limit instead (never
// during a pause), leaving the model three minutes for its first chunk.
const FORCE_MS=420000;
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
async function acquire($, ticket, signal, registry, forceAfter) {
  let first=true, paused=false;
  const since=Date.now();
  for (;;) {
    signal.throwIfAborted();
    const force=forceAfter!=null && Date.now()-since>=forceAfter;
    const r=await call($,{...ticket,op:'acquire',wait:first||force?0:Math.min(15,forceAfter==null?15:Math.max(0,(since+forceAfter-Date.now())/1000)),...(force?{force:true}:{})});
    first=false;
    if(r.paused_until) registry.pausedUntil=r.paused_until*1000;
    signal.throwIfAborted();
    if(r.state==='granted') {if(paused) $.ui.status(undefined); return true;}
    if(r.state!=='queued') return false;
    if(r.paused_until && !paused) {
      paused=true;
      $.ui.status(`Slot Guard: paused until ${new Date(r.paused_until*1000).toLocaleTimeString()}, a usage window is near its limit`);
    }
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
    busy:0,waiting:0,agentWaits:0,acquiring:false,closed:false,active:Date.now(),tail:Promise.resolve()};
}
// Never throws: a failed release is retried later, and the lease is the last resort.
async function release($, state) {
  const ticket=state.ticket;
  if(!ticket) return;
  state.ticket=null; state.registry.held.delete(state);
  try {await call($,{...ticket,op:'release'});}
  catch {state.registry.orphans.set(ticket.key,ticket);}
}
// A parent waiting for its subagents leaves its granted ticket here for the first of them.
function park(state) {
  state.registry.parked.set(state.agent,{ticket:state.ticket,at:Date.now()});
  state.ticket=null; state.registry.held.delete(state);
}
function claim(state) {
  const {parked,parents}=state.registry;
  for(const owner of [state.agent,parents.get(state.agent)]) {
    const slot=owner!==undefined && parked.get(owner);
    if(slot) {parked.delete(owner); return slot.ticket;}
  }
  return null;
}
// Gives the slot up while waiting: to a subagent when that is what the agent waits for.
async function yieldSlot($,state) {
  if(state.agentWaits>0 && state.ticket) park(state);
  else await release($,state);
}
async function enter($,state,signal,step=false) {
  return locked(state,async()=>{
    if(state.closed) throw new Error('Agent turn already ended');
    for(let attempt=0;;attempt++) {
      if(!state.ticket) state.ticket=claim(state);
      if(!state.ticket) {
        const generation=++state.generation;
        state.ticket={pool:'agent',instance,session:state.session,turn:state.turn,
          // Work under way (a subagent, a resumed turn) goes before new prompts.
          rank:state.agent!=='main' || generation>1 ? 0 : 1,
          key:JSON.stringify([instance,state.agent,state.turn,generation])};
      }
      state.registry.held.add(state);
      let granted;
      state.acquiring=true;
      try {granted=await acquire($,state.ticket,signal,state.registry,step && state.agent!=='main' ? FORCE_MS : null);}
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
    if(state.busy===0 && state.closed) await release($,state);
    else if(state.busy===0 && state.waiting>0) await yieldSlot($,state);
  });
}
const forAgents = e => e.tool==='Agent' || e.tool==='Task';
// Waiting for others (children, a nested claude, the person) gives the slot back.
async function suspend($,state,e) {
  await locked(state,async()=>{
    state.waiting++;
    if(forAgents(e)) state.agentWaits++;
    if(state.busy===0) await yieldSlot($,state);
  });
}
async function resume(state,e) {
  await locked(state,async()=>{
    state.waiting--; state.active=Date.now();
    if(forAgents(e)) state.agentWaits--;
  });
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
// Usage windows the automatic limit paces against; the coordinator keeps the latest.
const readingsOf = limits => (limits||[])
  .filter(r=>r.kind==='five_hour' || r.kind==='seven_day')
  .map(r=>({kind:r.kind,percent:r.percentUsed,resets:Date.parse(r.resetsAt)/1000}))
  .filter(r=>Number.isFinite(r.percent) && Number.isFinite(r.resets));
async function report($,registry,limits) {
  const readings=readingsOf(limits), signature=JSON.stringify(readings);
  if(!readings.length || signature===registry.reported) return;
  await call($,{op:'usage',readings});
  registry.reported=signature;
}
async function heartbeat($,{held,orphans,pausedUntil,parked}) {
  const now=Date.now(), keys=[];
  for(const [owner,slot] of parked) {
    if(now-slot.at>PARK_MS) {
      parked.delete(owner);
      try {await call($,{...slot.ticket,op:'release'});} catch {orphans.set(slot.ticket.key,slot.ticket);}
    } else keys.push(slot.ticket.key);
  }
  const paused=now<(pausedUntil||0)+HEARTBEAT_MS;  // Agents wait on purpose during a pause.
  for(const state of held) {
    if(!state.ticket) {held.delete(state); continue;}
    if(!paused && state.busy===0 && state.waiting===0 && !state.acquiring && now-state.active>IDLE_MS) {
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
  const registry={held:new Set(),orphans:new Map(),reported:'',
    parked:new Map(),     // agent -> its granted ticket, left for its subagent
    parents:new Map()};   // subagent id -> the agent that spawned it
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
        $.session.usage().then(u=>report($,registry,u.rateLimits)).catch(()=>{});
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
      const paused=s.auto&&s.auto.enabled&&s.limits.agent===0 ?
        [`Paused: a usage window reached ${s.auto.target}%; agents resume when it renews at ${new Date(s.auto.until*1000).toLocaleTimeString()}. "python3 guard.py configure --auto off" resumes now.`] : [];
      const auto=s.auto&&s.auto.enabled ? [...paused,`Auto limit: ${s.limits.agent} (normal ${s.auto.cap}, max ${s.auto.max||s.auto.cap}), pacing to ${s.auto.target}% per window`+
        (s.auto.windows||[]).map(w=>`; ${w.kind} ${w.percent}% used, ${w.elapsed}% elapsed`+(w.rate==null?'':`, ${w.rate}%/h lately`)+(w.pace==null?'':`, pace ${w.pace.toFixed(2)}`)).join('')] : [];
      const over=count('granted')-s.limits.agent;
      if(over>0) auto.push(`${over} over the limit: subagents admitted before Claude Code's 10-minute stall limit; nothing new starts until they finish.`);
      return {text:[`Agents: ${count('granted')}/${s.limits.agent} working; ${count('queued')} waiting (FIFO). Parents waiting only for children yield their slot.`,...auto,...lines].join('\n')};
    } catch(error) {return {text:'Slot Guard: '+String(error)};}
  });
  // Pushed when a usage window moves a whole point; the heartbeat covers quiet periods.
  on('session.measure',async($,e,next)=>{
    if(e.changed.includes('rateLimits')) await report($,registry,e.rateLimits).catch(()=>{});
    return next(e);
  }).catch(($,e,next)=>next(e));
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
      await enter($,state,next.signal,true); entered=true;
      return yield* next(e);
    } catch(error) {
      // An abandoned dispatch (interrupt, reload) has already moved on: do not stop the turn again.
      if(next.signal.aborted) {
        // The engine gives up on a subagent's step after 10 minutes: tell its parent to retry.
        if(e.agentId && Date.now()<(registry.pausedUntil||0)) {
          const text=`Slot Guard paused this subagent: a usage window is near its limit. It did no work. `+
            `Dispatch it again after ${new Date(registry.pausedUntil).toLocaleTimeString()}, when the window renews.`;
          yield {kind:'text',index:0,text};
          yield {kind:'stop',stopReason:'end_turn',usage:null};
          return {...emptyStep(e),answer:text,stopReason:'end_turn'};
        }
        return emptyStep(e);
      }
      $.ui.status('Slot Guard: turn stopped — '+String(error));
      // $.turn.abort refuses a subagent's turn; failing here would let the model run unadmitted.
      try {await $.turn.abort({turnId:e.turnId});} catch {}
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
      await suspend($,state,e);
      try {return await next(e);}
      finally {await resume(state,e);}
      // Reacquire at the next actual model/tool operation, never while still waiting.
    }
    await enter($,state,next.signal);
    const admitted={state,suspended:false,done:false};
    if(e.tool_use_id) pending.set(e.tool_use_id,admitted);
    try {return await next(e);}
    finally {
      if(e.tool_use_id) pending.delete(e.tool_use_id);
      if(admitted.timer) admitted.timer.cancel();
      await locked(state,async()=>{
        admitted.done=true;
        if(admitted.suspended) {state.waiting--; state.active=Date.now(); return;}
        state.busy--; state.active=Date.now();
        if(state.busy===0 && state.closed) await release($,state);
        else if(state.busy===0 && state.waiting>0) await yieldSlot($,state);
      });
    }
  }).catch(($,e,next)=>next.called ? next(e) : {deny:'Slot Guard: could not admit tool execution'});
  // A permission ask that stays open waits for the person: yield the slot until the agent
  // works again. Auto mode asks its classifier on most calls and answers within seconds.
  on('tool.check',async($,e,next)=>{
    const verdict=await next(e);
    const admitted=e.tool_use_id && pending.get(e.tool_use_id);
    if(admitted && verdict.decision==='ask' && !admitted.timer) {
      const state=admitted.state;
      admitted.timer=$.clock.after(ASK_GRACE_MS,()=>{
        locked(state,async()=>{
          if(admitted.done || admitted.suspended) return;
          admitted.suspended=true; state.busy--; state.waiting++;
          if(state.busy===0) await yieldSlot($,state);
        }).catch(()=>{});
      });
    }
    return verdict;
  }).catch(($,e,next)=>next(e));  // Not a gate: a failure here keeps the engine's verdict.
  // Which agent spawned which subagent, so a parent's slot can pass to its child.
  on('agent.spawn',async($,e,next)=>{
    const result=await next(e);
    if(result && result.agentId) registry.parents.set(result.agentId,e.parentAgentId||'main');
    return result;
  }).catch(($,e,next)=>next(e));
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
