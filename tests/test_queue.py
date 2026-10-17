import concurrent.futures
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import Queue, initialize, local_call, process_identity


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'
        self.live = {10:'birth'}
        self.q = Queue(self.path, 4, self.live.get)

    def tearDown(self):
        self.q.db.close()
        self.temp.cleanup()

    def data(self, key, pool='agent', **overrides):
        return {'op':'acquire','key':key,'pool':pool,'session':'s','turn':key,
                'pid':10,'birth':'birth', **overrides}

    def test_fifth_waits_fifo_no_overtaking(self):
        for i in range(4):
            self.assertEqual(self.q.operate(self.data(str(i)))['state'], 'granted')
        a = self.q.operate(self.data('a'))
        b = self.q.operate(self.data('b'))
        self.assertEqual((a['position'], b['position']), (1, 2))
        self.q.operate(self.data('0', op='release'))
        self.assertEqual(self.q.operate(self.data('b'))['state'], 'queued')
        self.assertEqual(self.q.operate(self.data('a'))['state'], 'granted')
        self.q.operate(self.data('1', op='release'))
        self.assertEqual(self.q.operate(self.data('b'))['state'], 'granted')

    def test_simultaneous_admission_never_exceeds_limit(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
            results = list(pool.map(lambda i:self.q.operate(self.data(str(i))), range(100)))
        self.assertEqual(sum(r['state']=='granted' for r in results), 4)
        self.assertEqual(len({r['ticket'] for r in results}), 100)

    def test_repeated_acquire_and_release_are_idempotent(self):
        a = self.q.operate(self.data('a'))
        self.assertEqual(a, self.q.operate(self.data('a')))
        self.q.operate(self.data('a', op='release'))
        self.q.operate(self.data('a', op='release'))
        self.assertEqual(self.q.operate(self.data('a'))['state'], 'released')

    def test_cancel_before_acquire_tombstone(self):
        self.q.operate(self.data('late', op='release'))
        self.assertEqual(self.q.operate(self.data('late'))['state'], 'released')

    def test_restart_retains_grants_and_order(self):
        for i in range(6):
            self.q.operate(self.data(str(i)))
        self.q.db.close()
        self.q = Queue(self.path, 4, self.live.get)
        self.q.reap()
        self.assertEqual(self.q.operate(self.data('4'))['position'], 1)
        self.q.operate(self.data('0',op='release'))
        self.assertEqual(self.q.operate(self.data('4'))['state'],'granted')
        self.assertEqual(self.q.operate(self.data('5'))['state'],'queued')

    def test_dead_owner_recovered_pid_reuse_not_confused(self):
        self.q.operate(self.data('a'))
        self.live[10] = 'new-birth'
        self.q.reap()
        self.assertEqual(self.q.operate({'op':'status'})['tickets'], [])

    def test_inspection_failure_does_not_free_live_reservation(self):
        self.q.operate(self.data('a'))
        def unavailable(_): raise RuntimeError('denied')
        self.q.identity = unavailable
        self.q.reap()
        self.assertEqual(self.q.operate({'op':'status'})['tickets'][0]['state'],'granted')

    def test_main_and_children_share_one_pool(self):
        for i in range(4): self.q.operate(self.data(str(i)))
        self.assertEqual(self.q.operate(self.data('child'))['state'],'queued')

    def test_wrong_owner_cannot_release_ticket(self):
        self.q.operate(self.data('a'))
        with self.assertRaises(ValueError):
            self.q.operate(self.data('a',op='release',pid=11))

    def test_real_process_birth_and_death(self):
        self.assertTrue(process_identity(os.getpid()))
        p = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        try:
            self.assertTrue(process_identity(p.pid))
        finally:
            p.terminate(); p.wait()
        self.assertIsNone(process_identity(p.pid))

    def test_long_poll_wakes_on_release(self):
        home = Path(self.temp.name)/'home'
        initialize(home, 4)
        owner = {'pid':os.getpid(), 'birth':process_identity(os.getpid())}
        ticket = lambda key, **extra: {**self.data(key), **owner, **extra}
        for i in range(4): local_call(home, ticket(str(i)))
        self.assertEqual(local_call(home, ticket('waiter'))['state'], 'queued')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(local_call, home, ticket('waiter', wait=5))
            time.sleep(.3)
            local_call(home, ticket('0', op='release'))
            self.assertEqual(future.result(timeout=5)['state'], 'granted')

    def age(self, key, seconds):
        self.q.db.execute('UPDATE tickets SET touched=touched-?, created=created-? WHERE key=?', (seconds, seconds, key))
        self.q.db.commit()

    def state(self, key):
        return self.q.db.execute('SELECT state FROM tickets WHERE key=?', (key,)).fetchone()['state']

    def test_unrenewed_lease_expires_and_promotes_fifo(self):
        for i in range(4): self.q.operate(self.data(str(i), instance='mod'))
        self.q.operate(self.data('next', instance='mod'))
        self.age('0', 301)
        self.q.reap()
        self.assertEqual(self.state('0'), 'released')
        self.assertEqual(self.state('next'), 'granted')
        self.assertEqual(self.q.operate(self.data('0', instance='mod'))['state'], 'released')

    def test_touch_and_acquire_renew_the_lease(self):
        self.q.operate(self.data('a', instance='mod'))
        self.q.operate(self.data('b', instance='mod'))
        self.age('a', 290); self.age('b', 290)
        result = self.q.operate({'op':'touch','keys':['a','missing'],'pid':10,'birth':'birth'})
        self.assertEqual(result['states'], {'a':'granted','missing':'released'})
        self.q.operate(self.data('b', instance='mod'))
        self.age('a', 20); self.age('b', 20)
        self.q.reap()
        self.assertEqual((self.state('a'), self.state('b')), ('granted', 'granted'))

    def test_touch_ignores_other_owner(self):
        self.q.operate(self.data('a', instance='mod'))
        self.age('a', 301)
        self.q.operate({'op':'touch','keys':['a'],'pid':11,'birth':'other'})
        self.q.reap()
        self.assertEqual(self.state('a'), 'released')

    def test_legacy_rows_without_instance_never_expire(self):
        self.q.operate(self.data('old'))
        self.age('old', 10**6)
        self.q.reap()
        self.assertEqual(self.state('old'), 'granted')

    def test_adopt_releases_previous_load_of_same_owner_only(self):
        self.live[11] = 'b2'
        self.q.operate(self.data('old', instance='load1'))
        self.q.operate(self.data('mine', instance='load2'))
        self.q.operate(self.data('other', instance='load1', pid=11, birth='b2'))
        self.q.operate(self.data('legacy'))
        self.assertEqual(self.q.operate({'op':'adopt','instance':'load2','pid':10,'birth':'birth'})['released'], 1)
        self.assertEqual([self.state(k) for k in ('old','mine','other','legacy')],
                         ['released','granted','granted','granted'])

    def test_evict_frees_slot_for_next_in_line(self):
        tickets = [self.q.operate(self.data(str(i)))['ticket'] for i in range(4)]
        self.q.operate(self.data('next'))
        self.assertEqual(self.q.operate({'op':'evict','seqs':[tickets[2]]})['evicted'], 1)
        self.assertEqual(self.state('2'), 'released')
        self.assertEqual(self.state('next'), 'granted')
        with self.assertRaises(ValueError):
            self.q.operate({'op':'evict','seqs':['x']})

    def test_old_tombstones_are_pruned(self):
        self.q.operate(self.data('a', op='release', instance='mod'))
        self.age('a', 3601)
        self.q.reap()
        self.assertIsNone(self.q.db.execute("SELECT 1 FROM tickets WHERE key='a'").fetchone())

    def test_status_reports_renewal_age(self):
        self.q.operate(self.data('a', instance='mod'))
        self.age('a', 42)
        row = self.q.operate({'op':'status'})['tickets'][0]
        self.assertTrue(42 <= row['idle'] <= 44)

    def test_previous_schema_migrates_in_place(self):
        path = Path(self.temp.name)/'old.db'
        db = sqlite3.connect(path)
        db.execute('''CREATE TABLE tickets (seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
            pool TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL, pid INTEGER NOT NULL,
            birth TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)''')
        db.execute("INSERT INTO tickets(key,pool,session,turn,pid,birth,state,created) VALUES('k','agent','s','t',10,'birth','granted',0)")
        db.commit(); db.close()
        q = Queue(path, 4, self.live.get)
        try:
            q.reap()
            self.assertEqual(q.operate({'op':'status'})['tickets'][0]['state'], 'granted')
            self.assertEqual(q.operate(self.data('new', instance='mod'))['state'], 'granted')
        finally:
            q.db.close()

    def test_lock_contention_keeps_acquirer_waiting_but_fails_other_ops(self):
        home = Path(self.temp.name)/'home'
        initialize(home, 4)
        owner = {'pid':os.getpid(), 'birth':process_identity(os.getpid())}
        with (home/'coordinator.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(local_call(home, {**self.data('a'), **owner})['state'], 'queued')
            with self.assertRaises(RuntimeError):
                local_call(home, {**self.data('a', op='release'), **owner})


if __name__ == '__main__': unittest.main()
