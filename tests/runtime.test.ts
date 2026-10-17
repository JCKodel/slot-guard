import {expect, test} from 'claude-code/testing'

const STEP = {turnId:'turn-test',index:0,model:'test-model',messageCount:1}
const RESULT = {...STEP,answer:'OK',toolUses:[],stopReason:'end_turn',usage:null}

async function drain(stream) {
  for (;;) {const next = await stream.next(); if (next.done) return next.value}
}

test('real runtime: no inference without an agent grant', async ($, on) => {
  on('session.id', () => ({value:'test-session'}))
  const calls = []
  let model = 0
  on('process.run', ($, e) => {
    calls.push(JSON.parse(e.init.stdin))
    return {value:{exitCode:0,stdout:JSON.stringify({state:'granted'}),stderr:''}}
  })
  on('turn.step', async function* () {model++; return RESULT})
  await drain($.turn.step(STEP))
  expect(model).toBe(1)
  expect(calls.map(d=>d.op+':'+d.pool)).toEqual(['acquire:agent'])
})

test('real runtime: broken coordinator fails closed', async ($, on) => {
  on('session.id', () => ({value:'test-session'}))
  let model = 0
  let aborted = 0
  on('process.run', () => ({value:{exitCode:1,stdout:'',stderr:'offline'}}))
  on('turn.abort', () => {aborted++; return {value:undefined}})
  on('turn.step', async function* () {model++; return RESULT})
  await drain($.turn.step(STEP))
  expect(model).toBe(0)
  expect(aborted).toBe(1)
})

test('real runtime: failure before try invokes fail-closed catch', async ($, on) => {
  let model=0, aborted=0
  on('session.id', () => {throw new Error('identity unavailable')})
  on('turn.abort', () => {aborted++; return {value:undefined}})
  on('turn.step', async function* () {model++; return RESULT})
  await drain($.turn.step(STEP))
  expect(model).toBe(0)
  expect(aborted).toBe(1)
})

test('real runtime: failed abort still never falls through to model', async ($, on) => {
  let model=0
  on('session.id', () => {throw new Error('identity unavailable')})
  on('turn.abort', () => {throw new Error('abort unavailable')})
  on('turn.step', async function* () {model++; return RESULT})
  await drain($.turn.step(STEP))
  expect(model).toBe(0)
})

test('real runtime: slots command reports local occupancy without inference', async ($, on) => {
  on('process.run', () => ({value:{exitCode:0,stderr:'',stdout:JSON.stringify({
    limits:{agent:4},tickets:[{pool:'agent',state:'granted'},{pool:'agent',state:'queued'}]
  })}}))
  const result = await $.command.run({command:'slots',args:''})
  expect(result.text).toBe('Agents: 1/4 working; 1 waiting (FIFO). Parents waiting only for children yield their slot.')
})
