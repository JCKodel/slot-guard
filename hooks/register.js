// One slot per working agent, held across model steps and ordinary tools.
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
async function acquire($, ticket, signal) {
  let first=true;
  for (;;) {
    signal.throwIfAborted();
    const r=await call($,{...ticket,op:'acquire',wait:first?0:15});
    first=false;
    signal.throwIfAborted();
    if(r.state==='granted') return;
    if(r.state!=='queued') throw new Error('Reservation canceled');
  }
}
// Serialize state transitions, not work: tools of one agent can run in parallel.
function locked(state, fn) {
  const result=state.tail.then(fn);
  state.tail=result.catch(()=>{});
  return result;
}
function newState(session,e) {
  return {session,turn:e.turnId,agent:e.agentId||'main',generation:0,
    ticket:null,busy:0,waiting:0,closed:false,tail:Promise.resolve()};
}
async function release($, state) {
  if(!state.ticket) return;
  await call($,{...state.ticket,op:'release'});
  state.ticket=null;
}
async function enter($,state,signal) {
  return locked(state,async()=>{
    if(state.closed) throw new Error('Agent turn already ended');
    if(!state.ticket) {
      state.ticket={pool:'agent',session:state.session,turn:state.turn,
        key:JSON.stringify([state.session,state.turn,state.agent,++state.generation])};
    }
    try { await acquire($,state.ticket,signal); }
    catch(error) { await release($,state); throw error; }
    state.busy++;
  });
}
async function leave($,state) {
  return locked(state,async()=>{
    state.busy--;
    if(state.busy===0 && (state.waiting>0 || state.closed)) await release($,state);
  });
}
const waitsForAgent = e => e.tool==='Agent' || e.tool==='Task' ||
  (e.tool==='TaskOutput' && e.block!==false);

export function register(on) {
  const states=new Map();
  const key=(session,e)=>JSON.stringify([session,e.agentId||'main']);
  on('session.start',async($,e,next)=>{
    await $.command.register({name:'slots',description:'Show the global working-agent limit and FIFO queue.',immediate:true});
    return next(e);
  });
  on('command.run',{command:'slots'},async($)=>{
    try {
      const s=await call($,{op:'status'});
      const count=state=>s.tickets.filter(t=>t.state===state).length;
      return {text:`Agents: ${count('granted')}/${s.limits.agent} working; ${count('queued')} waiting (FIFO). Parents waiting only for children yield their slot.`};
    } catch(error) {return {text:'Slot Guard: '+String(error)};}
  });
  on('turn.step',async function*($,e,next){
    const session=await $.session.id(), k=key(session,e);
    let state=states.get(k);
    if(!state || state.turn!==e.turnId) {
      if(state && !state.closed) throw new Error('Overlapping turns for one agent');
      state=newState(session,e); states.set(k,state);
    }
    let entered=false;
    try {
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
    const session=await $.session.id();
    const state=states.get(key(session,e));
    // Tools outside an admitted turn must not silently escape the limiter.
    if(!state || state.closed) return {deny:'Slot Guard: no admitted agent turn for this tool'};
    if(waitsForAgent(e)) {
      await locked(state,async()=>{
        state.waiting++;
        if(state.busy===0) await release($,state);
      });
      try {return await next(e);}
      finally {await locked(state,async()=>{state.waiting--;});}
      // Reacquire at the next actual model/tool operation, never while still waiting.
    }
    let entered=false;
    try {
      await enter($,state,next.signal); entered=true;
      return await next(e);
    } finally {if(entered) await leave($,state);}
  }).catch(async()=>({deny:'Slot Guard: could not admit tool execution'}));
  on('turn.complete',async($,e,next)=>{
    const session=await $.session.id(), k=key(session,e), state=states.get(k);
    if(state && state.turn===e.turnId) {
      await locked(state,async()=>{
        state.closed=true;
        if(state.busy===0) await release($,state);
      });
      // Keep closed state while a tool is unwinding. It must release its last slot.
      if(states.get(k)===state) states.delete(k);
    }
    return next(e);
  });
}
