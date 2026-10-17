export function register(on) {
  let spawned = false;
  on('turn.step', async function* ($, e) {
    const session = await $.session.id();
    const home = await $.env.get('CLAUDE_SLOT_HOME');
    const delay = await $.env.get('SLOT_TEST_DELAY');
    const children = await $.env.get('SLOT_TEST_CHILDREN');
    const name = session + (e.agentId ? '-' + e.agentId : '');
    if (children === '1' && !e.agentId && !spawned) {
      spawned = true;
      const input = {description:'Local slot test',prompt:'Reply OK.',subagent_type:'general-purpose'};
      for (let i=0; i<3; i++) {
        yield {kind:'tool',index:i,id:'slot-local-child-'+i,name:'Agent'};
        yield {kind:'input',index:i,json:JSON.stringify(input)};
      }
      yield {kind:'stop',stopReason:'tool_use',usage:null};
      return {turnId:e.turnId,index:e.index,answer:'',toolUses:[{name:'Agent',input},{name:'Agent',input},{name:'Agent',input}],stopReason:'tool_use',usage:null};
    }
    await $.fs.write(home + '/' + name + '.started', String(Date.now()));
    await $.process.run(['python3', '-c', 'import time; time.sleep(float(__import__("sys").argv[1]))', delay || '0.1']);
    yield {kind:'text',index:0,text:'LOCAL_TEST_OK'};
    yield {kind:'stop',stopReason:'end_turn',usage:null};
    await $.fs.write(home + '/' + name + '.finished', String(Date.now()));
    return {turnId:e.turnId,index:e.index,answer:'LOCAL_TEST_OK',toolUses:[],stopReason:'end_turn',usage:null};
  });
}
