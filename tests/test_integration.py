"""Real Claude executable, local fixed responses, zero inference/network required."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_environment import test_environment
from guard import initialize, local_call, process_identity


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slot-test-')
        self.home = Path(self.temp.name)
        initialize(self.home, 4)
        self.children = []
        self.owner = {'pid':os.getpid(),'birth':process_identity(os.getpid())}

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)
        self.temp.cleanup()

    def ticket(self, key, pool='agent', **extra):
        return {'op':'acquire','key':key,'pool':pool,'session':'holder','turn':key,
                **self.owner, **extra}

    def status(self):
        return local_call(self.home, {'op':'status'})['tickets']

    def wait_for(self, predicate, seconds=10):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            value = predicate()
            if value: return value
            time.sleep(.05)
        self.fail('Timed out waiting for condition')

    def claude(self, delay=.1, home=None, children=False, stream=False, mode="", heartbeat=None):
        sid=str(uuid.uuid4())
        home=home or self.home
        env={**test_environment(),'CLAUDE_SLOT_HOME':str(home),'SLOT_TEST_DELAY':str(delay),
             'SLOT_TEST_CHILDREN':'1' if children else '0', 'SLOT_TEST_MODE':mode,
             'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
             'ANTHROPIC_API_KEY':'local-test-not-a-key', 'ANTHROPIC_BASE_URL':'http://127.0.0.1:1'}
        if heartbeat: env['SLOT_HEARTBEAT_MS']=str(heartbeat)
        command=[shutil.which('claude'),'-p','Local test, no inference.',
            '--plugin-dir',str(ROOT),'--plugin-dir',str(ROOT/'tests/fixtures/fake-model'),
            '--setting-sources','','--strict-mcp-config','--tools','Agent,Bash' if children or mode=='tools' else '',
            '--disable-slash-commands','--no-session-persistence','--prompt-suggestions','false',
            '--system-prompt','Test.','--model','haiku','--effort','low',
            '--session-id',sid,'--max-budget-usd','0.01','--output-format','json',
            '--debug-file',str(self.home/(sid+'.debug.log'))]
        if children or mode=='tools': command += ['--allowedTools','Agent','Bash','--permission-mode','dontAsk']
        if stream:
            command.remove('Local test, no inference.')
            command[command.index('--output-format')+1]='stream-json'
            command += ['--input-format','stream-json','--verbose']
        p=subprocess.Popen(command,cwd=ROOT,env=env,stdin=subprocess.PIPE if stream else subprocess.DEVNULL,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(p)
        return sid,p

    def result(self, p):
        out,err=p.communicate(timeout=35)
        self.assertEqual(p.returncode,0,msg=err+'\n'+out)
        r=json.loads(out)
        self.assertEqual(r['result'],'LOCAL_TEST_OK')
        self.assertEqual(r['total_cost_usd'],0)
        self.assertEqual(r['duration_api_ms'],0)
        return r

    def test_real_processes_atomic_capacity(self):
        script='''import json,sys; from pathlib import Path
from guard import local_call
print(json.dumps(local_call(Path(sys.argv[1]),json.loads(sys.argv[2]))))'''
        def request(i):
            p=subprocess.run([sys.executable,'-c',script,str(self.home),json.dumps(self.ticket(str(i)))],
                             cwd=ROOT,capture_output=True,text=True,timeout=15)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as workers:
            results=list(workers.map(request,range(40)))
        self.assertEqual(sum(r['state']=='granted' for r in results),4)
        self.assertEqual(len({r['ticket'] for r in results}),40)

    def test_six_claudes_four_slots_fifo_and_long_wait(self):
        first=[]
        for _ in range(4):
            sid,p=self.claude(delay=18)
            first.append((sid,p))
            self.wait_for(lambda:(self.home/(sid+'.started')).exists())
        fifth,p5=self.claude()
        self.wait_for(lambda:any(t['session']==fifth and t['state']=='queued' for t in self.status()))
        sixth,p6=self.claude()
        self.wait_for(lambda:any(t['session']==sixth and t['state']=='queued' for t in self.status()))
        self.assertFalse((self.home/(fifth+'.started')).exists())
        self.assertFalse((self.home/(sixth+'.started')).exists())
        peak={'agent':0}
        deadline = time.monotonic() + 60
        while any(p.poll() is None for p in self.children):
            self.assertLess(time.monotonic(), deadline, 'Claude processes did not finish')
            rows=self.status()
            for pool in peak:
                used=sum(t['pool']==pool and t['state']=='granted' for t in rows)
                peak[pool]=max(peak[pool],used)
                self.assertLessEqual(used,4)
            time.sleep(.05)
        for p in self.children: self.result(p)
        fifth_time=int((self.home/(fifth+'.started')).read_text())
        sixth_time=int((self.home/(sixth+'.started')).read_text())
        earliest_end=min(int((self.home/(sid+'.finished')).read_text()) for sid,_ in first)
        self.assertGreaterEqual(fifth_time,earliest_end)
        self.assertGreaterEqual(sixth_time,fifth_time)
        self.assertEqual(self.status(),[])
        log=(self.home/(fifth+'.debug.log')).read_text()
        self.assertNotIn('Aguardando slot',log)
        self.assertNotIn('hook was skipped',log)
        print('Six real Claude processes: peak',peak,'; FIFO and >15s wait passed; API cost $0',flush=True)

    def test_kill_waiter_does_not_leave_queue_blocked(self):
        for i in range(4): local_call(self.home,self.ticket(str(i)))
        sid,p=self.claude()
        self.wait_for(lambda:any(t['session']==sid for t in self.status()))
        p.kill();p.communicate(timeout=10)
        self.assertFalse(any(t['session']==sid for t in self.status()))
        second,p2=self.claude()
        self.wait_for(lambda:any(t['session']==second for t in self.status()))
        local_call(self.home,self.ticket('0',op='release'))
        self.result(p2)

    def test_kill_holder_recovers_capacity(self):
        sid,p=self.claude(delay=30)
        self.wait_for(lambda:(self.home/(sid+'.started')).exists())
        self.assertEqual(len([t for t in self.status() if t['session']==sid]),1)
        p.kill();p.communicate(timeout=10)
        self.assertFalse(any(t['session']==sid for t in self.status()))

    def test_corrupt_coordinator_never_reaches_fake_model(self):
        missing=self.home/'missing'
        missing.mkdir()
        (missing/'config.json').write_text('{broken')
        sid,p=self.claude(home=missing)
        out,err=p.communicate(timeout=10)
        self.assertFalse((missing/(sid+'.started')).exists())
        log=(self.home/(sid+'.debug.log')).read_text()
        self.assertIn('Coordinator unavailable',log)
        self.assertNotIn('[API:request]',log)
        self.assertNotIn('API error',log)

    def test_lock_failure_never_grants(self):
        import fcntl
        with (self.home/'coordinator.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # Contention keeps an acquirer waiting; it never grants or records a ticket.
            self.assertEqual(local_call(self.home,self.ticket('blocked'))['state'],'queued')
            with self.assertRaisesRegex(RuntimeError,'lock timeout'):
                local_call(self.home,self.ticket('blocked',op='release'))
        self.assertEqual(self.status(),[])

    def test_actual_subagents_share_one_free_slot_with_three_existing_agents(self):
        (self.home/'config.json').write_text(json.dumps({'agents':4,'schema':2}))
        for i in range(3): local_call(self.home,self.ticket(str(i)))
        sid,p=self.claude(delay=.2,children=True)
        result=self.result(p)
        self.assertEqual(result['subagent_stats']['spawned'],3)
        self.assertEqual(result['subagent_stats']['completed'],3)
        self.assertEqual(len(list(self.home.glob(sid+'-*.finished'))),3)
        self.assertEqual(len(self.status()),3)  # Only the three synthetic agent holders remain.

    def test_tools_keep_agent_slots_and_fifth_session_waits(self):
        for _ in range(4):
            sid,p=self.claude(delay=5,mode='tools')
            self.wait_for(lambda:(self.home/(sid+'.tool-started')).exists())
        fifth,p5=self.claude(delay=.1,mode='tools')
        self.wait_for(lambda:any(t['session']==fifth and t['state']=='queued' for t in self.status()))
        self.assertFalse((self.home/(fifth+'.tool-started')).exists())
        self.assertEqual(sum(t['state']=='granted' for t in self.status()),4)
        for p in self.children: self.result(p)
        self.assertEqual(len(list(self.home.glob('*.tool-finished'))),5)
        self.assertEqual(self.status(),[])

    def test_four_parents_can_delegate_without_deadlock(self):
        for _ in range(4):
            sid,p=self.claude(delay=.3,children=True,mode='barrier')
            self.wait_for(lambda:(self.home/(sid+'.parent-ready')).exists())
        self.assertEqual(sum(t['state']=='granted' for t in self.status()),4)
        (self.home/'parents-go').write_text('go')
        end=time.monotonic()+30
        peak=0
        while any(p.poll() is None for p in self.children):
            self.assertLess(time.monotonic(),end,'Delegating parents deadlocked')
            used=sum(t['state']=='granted' for t in self.status())
            peak=max(peak,used)
            self.assertLessEqual(used,4)
            time.sleep(.025)
        for p in self.children:
            r=self.result(p)
            self.assertEqual(r['subagent_stats']['completed'],3)
        self.assertEqual(self.status(),[])
        print('Four parents / twelve children completed; peak working agents:',peak,flush=True)

    def test_heartbeat_renews_lease_of_long_running_work(self):
        sid,p=self.claude(delay=4,heartbeat=500)
        renewals=set()
        end=time.monotonic()+20
        while p.poll() is None and time.monotonic()<end:
            try:
                db=sqlite3.connect(self.home/'queue.sqlite3')
                try:
                    rows=db.execute("SELECT touched FROM tickets WHERE session=? AND state='granted'",(sid,)).fetchall()
                finally:
                    db.close()
            except sqlite3.Error:
                rows=[]
            renewals.update(r[0] for r in rows)
            time.sleep(.1)
        self.result(p)
        # The step held its slot for 4 s; the real engine's timer must have renewed it meanwhile.
        self.assertGreaterEqual(len(renewals),3,renewals)

    def test_spent_window_pauses_until_reset_then_resumes_unattended(self):
        local_call(self.home,{'op':'usage','readings':[{'kind':'five_hour','percent':96,'resets':time.time()+6}]})
        self.assertEqual(self.status(),[])
        sid,p=self.claude()
        time.sleep(3)
        self.assertFalse((self.home/(sid+'.started')).exists())
        self.assertEqual([t['state'] for t in self.status() if t['session']==sid],['queued'])
        self.result(p)
        self.assertTrue((self.home/(sid+'.finished')).exists())

    def test_interrupt_waiting_turn_keeps_session_usable(self):
        for i in range(4): local_call(self.home,self.ticket(str(i)))
        sid,p=self.claude(stream=True)
        def send(message):
            p.stdin.write(json.dumps(message)+'\n');p.stdin.flush()
        send({'type':'user','message':{'role':'user','content':'Local first turn.'}})
        self.wait_for(lambda:any(t['session']==sid for t in self.status()))
        time.sleep(.4)  # Interrupt the long poll, not merely the initial admission.
        send({'type':'control_request','request_id':'cancel-test','request':{'subtype':'interrupt'}})
        self.wait_for(lambda:not any(t['session']==sid for t in self.status()),seconds=20)
        self.assertIsNone(p.poll())  # Must recover without restarting Claude.
        self.assertFalse((self.home/(sid+'.started')).exists())
        local_call(self.home,self.ticket('0',op='release'))
        send({'type':'user','message':{'role':'user','content':'Local second turn.'}})
        self.wait_for(lambda:(self.home/(sid+'.finished')).exists())
        p.stdin.close();p.stdin=None
        out,err=p.communicate(timeout=10)
        self.assertEqual(p.returncode,0,err)
        results=[json.loads(line) for line in out.splitlines() if line.startswith('{')]
        self.assertTrue(any(r.get('result')=='LOCAL_TEST_OK' for r in results))


if __name__=='__main__': unittest.main(verbosity=2)
