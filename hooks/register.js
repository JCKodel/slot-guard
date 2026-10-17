// All coordination is local. Never ask a model to wait or retry.
const emptyStep = e => ({turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: null, usage: null});

async function call($, data) {
  const result = await $.process.run(['python3', 'guard.py', 'client'], {
    cwd: $.plugin.root, stdin: JSON.stringify(data), timeoutMs: 25000,
  });
  if (result.exitCode !== 0) throw new Error('Coordinator unavailable: ' + result.stderr.trim());
  const response = JSON.parse(result.stdout);
  if (response.error) throw new Error(response.error);
  return response;
}

function ticket(session, e, pool) {
  return {pool, session, turn: e.turnId,
    key: JSON.stringify([session, e.turnId, e.agentId || 'main', pool, pool === 'request' ? e.index : 'turn'])};
}

async function acquire($, data, signal) {
  let first = true;
  for (;;) {
    signal.throwIfAborted();
    const r = await call($, {...data, op: 'acquire', wait: first ? 0 : 15});
    first = false;
    signal.throwIfAborted();
    if (r.state === 'granted') return;
    if (r.state !== 'queued') throw new Error('Reservation canceled');
    // The statusLine script reads this session's queue directly from SQLite.
  }
}

async function release($, data) {
  try { await call($, {...data, op: 'release'}); }
  catch { $.ui.status('Could not release reservation; retained for safety.'); }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.command.register({name:'slots',description:'Show global local slots and FIFO queue.',immediate:true});
    return next(e);
  });
  on('command.run', {command:'slots'}, async ($) => {
    try {
      const status = await call($, {op:'status'});
      const lines = ['Global slots (FIFO):'];
      for (const pool of ['job','request']) {
        const active = status.tickets.filter(t => t.pool === pool && t.state === 'granted').length;
        const waiting = status.tickets.filter(t => t.pool === pool && t.state === 'queued').length;
        lines.push(pool + ': ' + active + '/' + status.limits[pool] + ' occupied; ' + waiting + ' waiting');
      }
      return {text:lines.join('\n')};
    } catch (error) { return {text:'Slot guard: ' + String(error)}; }
  });
  on('turn.step', async function* ($, e, next) {
    const session = await $.session.id();
    const job = ticket(session, e, 'job');
    const request = ticket(session, e, 'request');
    let entered = false;
    try {
      if (!e.agentId) await acquire($, job, next.signal);
      await acquire($, request, next.signal);
      next.signal.throwIfAborted();
      entered = true;
      return yield* next(e);
    } catch (error) {
      $.ui.status('Slot guard: turn interrupted — ' + String(error));
      await $.turn.abort({turnId: e.turnId});
      return emptyStep(e);
    } finally {
      await release($, request);
      if (!entered && !e.agentId) await release($, job);
    }
  }).catch(async function* ($, e) {
    // A failed policy hook must never fall through to inference.
    try { await $.turn.abort({turnId: e.turnId}); } catch {}
    return emptyStep(e);
  });

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      const session = await $.session.id();
      await release($, ticket(session, e, 'job'));
    }
    return next(e);
  });
}
