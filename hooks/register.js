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
const SPRINT_MINUTES=5;    // Default length of "/slots sprint".
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
async function acquire($, ticket, signal, registry, forceAfter, once={}) {
  let first=true, paused=false;
  const since=Date.now();
  for (;;) {
    signal.throwIfAborted();
    const force=forceAfter!=null && Date.now()-since>=forceAfter;
    const r=await call($,{...ticket,op:'acquire',wait:first||force?0:Math.min(15,forceAfter==null?15:Math.max(0,(since+forceAfter-Date.now())/1000)),
      ...(force?{force:true}:{}),...(first?once:{})});
    first=false;
    if(r.paused_until) {registry.pausedUntil=r.paused_until*1000; registry.pausedLine=!!r.paused_line;}
    signal.throwIfAborted();
    if(r.state==='granted') {if(paused) $.ui.status(undefined); return true;}
    if(r.state!=='queued') return false;
    if(r.paused_until && !paused) {
      paused=true;
      $.ui.status(`Slot Guard: paused until ${new Date(r.paused_until*1000).toLocaleTimeString()}, `+
        (r.paused_line?'weekly usage reached today\'s quota':'a usage window is near its limit'));
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
    busy:0,steps:0,waiting:0,agentWaits:0,acquiring:false,closed:false,active:Date.now(),tail:Promise.resolve()};
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
// Cooperative multitasking. Only model calls need a granted slot: tools run on the agent's
// ticket, granted or queued. Between model calls an agent that has held its slot for the
// coordinator's quantum hands it to the next in line and queues again; never mid-call.
const between = state => state.busy===0 && state.steps===0;
async function offer($,state) {
  if(state.ticket && between(state)) await call($,{op:'rotate',keys:[state.ticket.key]}).catch(()=>{});
}
async function enter($,state,signal,step=false) {
  return locked(state,async()=>{
    if(state.closed) throw new Error('Agent turn already ended');
    if(!step) {await offer($,state); state.busy++; state.active=Date.now(); return;}
    for(let attempt=0;;attempt++) {
      let once=between(state) && state.ticket ? {rotate:true} : {};
      if(!state.ticket) {state.ticket=claim(state); if(state.ticket) once={fresh:true};}
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
      try {granted=await acquire($,state.ticket,signal,state.registry,state.agent!=='main' ? FORCE_MS : null,once);}
      catch(error) {await release($,state); throw error;}
      finally {state.acquiring=false;}
      if(granted) break;
      // An evicted or expired ticket rejoins the back of the queue once.
      state.ticket=null; state.registry.held.delete(state);
      if(attempt>0) throw new Error('Reservation canceled');
    }
    state.busy++; state.steps++; state.active=Date.now();
  });
}
async function leave($,state,step=false) {
  return locked(state,async()=>{
    state.busy--; if(step) state.steps--; state.active=Date.now();
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
  const now=Date.now(), keys=[], longTools=[];
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
    if(state.busy>0 && state.steps===0 && !state.acquiring) longTools.push(state);
  }
  if(keys.length) await call($,{op:'touch',keys}).catch(()=>{});
  // An agent in a long tool, between model calls, offers its slot like at a tool's start.
  // Decided under the agent's lock: a model call cannot start until the offer is settled.
  for(const state of longTools) {
    await locked(state,async()=>{
      if(state.ticket && state.busy>0 && state.steps===0 && !state.acquiring)
        await call($,{op:'rotate',keys:[state.ticket.key]}).catch(()=>{});
    }).catch(()=>{});
  }
  await flush($,orphans);
}

const HELP=[
  'Slot Guard: one machine-wide limit on working agents (main agents and subagents, every terminal).',
  '',
  '/slots                 Show the limit, who is working and who waits (FIFO).',
  '/slots agents N        Set the normal limit to N agents, applied now. The automatic limit stays at or under it, unless "max" allows more.',
  '/slots max N           Extra room above "agents": with the automatic limit, up to N agents while every usage window has plenty left. N must be above "agents"; to lower the limit, lower "agents".',
  '/slots auto on|off     Pace the limit by the usage windows (on), or keep it fixed at "agents" (off).',
  '/slots quantum N       Seconds an agent keeps its slot while others wait (default 60).',
  '/slots reset           Restart the automatic limit from the normal limit, now. The usage readings and their rate stay.',
  '/slots forget          Forget the usage readings too and restart from the normal limit: after a plan change, or to end a 95% pause now.',
  '/slots evict N [N...]  Release ticket N by hand (numbers as listed by /slots).',
  `/slots sprint [all] [M] Urgent work: this session's agents (all: every session's) start at once, over the limit, for M minutes (default ${SPRINT_MINUTES}, up to 60). The others wait until it drains; a pause still stops it.`,
  '/slots sprint off      End every sprint now.',
  '/slots help            This text.',
  '',
  'Changes apply to every session at once; running work finishes and the queue drains to the new limit.',
  'The same is available from a checkout: python3 guard.py configure|status|reset|forget|evict|sprint, or "make" there for the list.',
].join('\n');
async function status($,s) {
  s=s||await call($,{op:'status'});
  const count=state=>s.tickets.filter(t=>t.state===state).length;
  const lines=s.tickets.map(t=>`#${t.seq} ${t.state} session ${t.session.slice(0,8)} pid ${t.pid}, renewed ${t.idle}s ago`);
  const paused=s.auto&&s.auto.enabled&&s.limits.agent===0 ?
    [(s.auto.line ? `Paused: weekly usage reached today's quota (a seventh of the week per day, from its reset hour); agents resume when a day with a higher quota starts at ${new Date(s.auto.until*1000).toLocaleTimeString()}. "/slots auto off" resumes now.`
      : `Paused: a usage window reached ${s.auto.target}%; agents resume when it renews at ${new Date(s.auto.until*1000).toLocaleTimeString()}. "/slots forget" or "/slots auto off" resumes now.`)] : [];
  const auto=s.auto&&s.auto.enabled ? [...paused,`Auto limit: ${s.limits.agent} (normal ${s.auto.cap}, max ${s.auto.max||s.auto.cap}), pacing to ${s.auto.target}% per window`+
    (s.auto.windows||[]).map(w=>`; ${w.kind} ${w.percent}% used, ${w.elapsed}% elapsed`+(w.rate==null?'':`, ${w.rate}%/h${w.hours==null?'':' working'} lately`)+(w.average==null?'':`, ${w.average}%/h over the window`)+(w.hours==null?'':`, ~${w.hours} h/day working`)+(w.pace==null?'':`, pace ${w.pace.toFixed(2)}`)+(w.line==null?'':`, line pace ${w.line.toFixed(2)}`)).join('')] : [];
  const sprints=Object.entries(s.sprints||{});
  for(const [session,until] of sprints)
    auto.push(`Sprint: ${session==='*'?'every session':'session '+session.slice(0,8)} starts its agents at once until ${new Date(until*1000).toLocaleTimeString()}. "/slots sprint off" ends it.`);
  const over=count('granted')-s.limits.agent;
  if(over>0) auto.push(`${over} over the limit: ${sprints.length?'a sprint, or ':''}subagents admitted before Claude Code's 10-minute stall limit; nothing new starts until they finish.`);
  return [`Agents: ${count('granted')}/${s.limits.agent} working; ${count('queued')} waiting (FIFO). Parents waiting only for children yield their slot. "/slots help" lists the subcommands.`,...auto,...lines].join('\n');
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
    await $.command.register({name:'slots',description:'Show or change the global working-agent limit and FIFO queue; "/slots help" lists the subcommands.',
      argumentHint:'[help | agents N | max N | auto on|off | quantum N | sprint [all] [M] | sprint off | reset | forget | evict N...]',immediate:true});
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
      const [verb,...rest]=words;
      if(verb===undefined) return {text:await status($)};
      if(verb==='evict') {
        const seqs=rest.map(w=>Number(w.replace(/^#/,'')));
        if(!seqs.length || seqs.some(n=>!Number.isInteger(n))) return {text:'Usage: /slots evict N [N...]\n\n'+HELP};
        const r=await call($,{op:'evict',seqs});
        return {text:`Slot Guard: ${r.evicted} ticket(s) released.`};
      }
      if(verb==='sprint') {
        const all=rest[0]==='all', args=all ? rest.slice(1) : rest;
        const off=!all && args.length===1 && args[0]==='off';
        const minutes=off ? 0 : args.length ? Number(args[0]) : SPRINT_MINUTES;
        if(args.length>1 || !off && !(Number.isInteger(minutes) && minutes>=1 && minutes<=60))
          return {text:'Usage: /slots sprint [all] [minutes] | /slots sprint off\n\n'+HELP};
        const session=all ? '*' : await $.session.id();
        const r=await call($,{op:'sprint',session,minutes});
        const text=off ? 'Slot Guard: sprints ended; agents queue under the limit again.' :
          `Slot Guard: sprint for ${all?'every session':'this session'} until ${new Date(r.sprints[session]*1000).toLocaleTimeString()}: `+
          'its agents start at once, over the limit; the others wait until it drains. A pause near a usage window\'s limit still stops it.';
        return {text:text+'\n'+await status($)};
      }
      if(verb==='reset' || verb==='forget') {
        if(rest.length) return {text:`Usage: /slots ${verb}\n\n`+HELP};
        const r=await call($,{op:'reset',usage:verb==='forget'});
        return {text:(verb==='forget' ? `Slot Guard: usage readings forgotten; the limit is ${r.limit} again until new readings arrive.\n`
          : `Slot Guard: the automatic limit restarts from the normal limit (now ${r.limit}); the usage readings stay. "/slots forget" also forgets them.\n`)+await status($)};
      }
      if(verb==='agents' || verb==='max' || verb==='quantum' || verb==='auto') {
        const value=verb==='auto' ? {on:true,off:false}[rest[0]] : Number(rest[0]);
        const valid=rest.length===1 && (verb==='auto' ? value!==undefined : Number.isInteger(value) && value>=1);
        if(!valid) return {text:`Usage: /slots ${verb} ${verb==='auto'?'on|off':'N'}\n\n`+HELP};
        const r=await call($,{op:'configure',[verb]:value});
        const notes=[];
        // The maximum only bounds the automatic limit: with it off, nothing changes now.
        const idle=verb==='max' && !r.config.auto;
        // The coordinator keeps a maximum only above the normal limit; anything else removes it.
        const below=verb==='max' && !idle && !r.config.max;
        if(idle) notes.push(`The automatic limit is off, so the maximum has no effect: the limit stays at ${r.config.agents}. Use "/slots agents N" for a fixed limit, or "/slots auto on".`);
        else if(below) notes.push(`The maximum only adds room above the normal limit, so the limit still goes up to ${r.config.agents}. To lower it, use "/slots agents ${value}".`);
        else if(verb==='max') notes.push(`Above ${r.config.agents} the limit rises one slot every 10 minutes, and only while every usage window has plenty left.`);
        if(verb==='agents' && !r.config.auto) notes.push('The automatic limit is off: this is a fixed limit.');
        const head=below ? `Slot Guard: max ${value} is not above the normal limit (${r.config.agents}): no maximum is set.` :
          `Slot Guard: ${verb} set to ${verb==='auto'?rest[0]:value}, ${idle?'saved':'applied now'}.`;
        return {text:[head,...notes,await status($,r)].join('\n')};
      }
      return {text:(verb==='help' ? '' : `Unknown subcommand "${verb}".\n\n`)+HELP};
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
          const text=`Slot Guard paused this subagent: ${registry.pausedLine?'weekly usage reached today\'s quota':'a usage window is near its limit'}. It did no work. `+
            `Dispatch it again after ${new Date(registry.pausedUntil).toLocaleTimeString()}, when ${registry.pausedLine?'the next day\'s quota starts':'the window renews'}.`;
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
      if(entered) await leave($,state,true);
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
