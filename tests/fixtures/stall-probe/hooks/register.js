// Probe of Claude Code's subagent stall watchdog. No inference: this replaces the model.
// The parent spawns one subagent. The subagent's step behaves per STALL_VARIANT:
//   silent    : no chunk at all for STALL_SECONDS, then the answer (how a queued wait looks).
//   first     : one text chunk at once, then silence for STALL_SECONDS, then the answer.
//   keepalive : an empty text chunk every STALL_EVERY seconds for STALL_SECONDS, then the answer.
//   spaces    : a one-space text chunk every STALL_EVERY seconds, then the answer.
// Timestamps go to STALL_OUT/<variant>.<event>.
async function sleep($, seconds) {
  await $.process.run(['python3', '-c', 'import time,sys; time.sleep(float(sys.argv[1]))', String(seconds)],
    {timeoutMs: (seconds + 30) * 1000});
}
export function register(on) {
  let spawned = false;
  on('turn.step', async function* ($, e, next) {
    const out = await $.env.get('STALL_OUT');
    const label = await $.env.get('STALL_VARIANT') || 'silent';
    const variant = label.replace(/-wait$/, '');
    const total = Number(await $.env.get('STALL_SECONDS') || 660);
    const every = Number(await $.env.get('STALL_EVERY') || 60);
    const mark = (what) => $.fs.write(out + '/' + label + '.' + what, String(Date.now()));
    if (!e.agentId && !spawned) {
      spawned = true;
      await mark('parent-spawns');
      const prompt = variant === 'real' ? 'Run the Bash command `echo probe-ok`, then reply with exactly the word DONE and nothing else.' : 'Reply OK.';
      const input = {description: 'Stall probe', prompt, subagent_type: 'general-purpose'};
      yield {kind: 'tool', index: 0, id: 'stall-probe-child', name: 'Agent'};
      yield {kind: 'input', index: 0, json: JSON.stringify(input)};
      yield {kind: 'stop', stopReason: 'tool_use', usage: null};
      return {turnId: e.turnId, index: e.index, answer: '', toolUses: [{name: 'Agent', input}], stopReason: 'tool_use', usage: null};
    }
    if (e.agentId && variant === 'real') {
      // Two empty keepalive chunks, then the real model: does the answer survive intact,
      // and does the next request accept the assembled message?
      await mark('child-step' + e.index + '-start');
      yield {kind: 'text', index: 0, text: ''};
      await sleep($, 2);
      yield {kind: 'text', index: 0, text: ''};
      const result = yield* next(e);
      await $.fs.write(out + '/' + label + '.child-step' + e.index + '-result', JSON.stringify(result));
      return result;
    }
    if (e.agentId) {
      await mark('child-start');
      let elapsed = 0;
      if (variant === 'first') yield {kind: 'text', index: 0, text: 'working'};
      while (elapsed < total) {
        const chunk = Math.min(every, total - elapsed);
        await sleep($, chunk);
        elapsed += chunk;
        await mark('child-tick-' + elapsed);
        if (variant === 'keepalive') yield {kind: 'text', index: 0, text: ''};
        if (variant === 'spaces') yield {kind: 'text', index: 0, text: ' '};
      }
      await mark('child-answering');
      yield {kind: 'text', index: 0, text: 'CHILD_SURVIVED'};
      yield {kind: 'stop', stopReason: 'end_turn', usage: null};
      await mark('child-finished');
      return {turnId: e.turnId, index: e.index, answer: 'CHILD_SURVIVED', toolUses: [], stopReason: 'end_turn', usage: null};
    }
    await mark('parent-resumes');
    yield {kind: 'text', index: 0, text: 'PARENT_DONE'};
    yield {kind: 'stop', stopReason: 'end_turn', usage: null};
    return {turnId: e.turnId, index: e.index, answer: 'PARENT_DONE', toolUses: [], stopReason: 'end_turn', usage: null};
  });
}
