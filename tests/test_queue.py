import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import AUTO_STEP, Queue, auto_limit, initialize, local_call, pace, process_identity


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

class AutoLimitTests(unittest.TestCase):
    H = 3600
    def test_pace_takes_the_worse_of_average_and_remaining(self):
        # 14% used after 30 min of a 5 h window: the average burn decides.
        self.assertAlmostEqual(pace(14, 4.5*self.H, 5*self.H, 0), 0.1*95/14, places=6)
        # 82% used with 78% gone: the remaining budget decides.
        self.assertAlmostEqual(pace(82, 1.1*self.H, 5*self.H, 0), (13/95)/0.22, places=6)
        self.assertGreater(pace(30, 2*self.H, 5*self.H, 0), 1)
        self.assertEqual(pace(99, 2*self.H, 5*self.H, 0), 0)
        self.assertGreater(pace(0, 4*self.H, 5*self.H, 0), 1)

    def test_pace_ignores_expired_and_first_minutes(self):
        self.assertIsNone(pace(10, -1, 5*self.H, 0))
        self.assertIsNone(pace(1, 5*self.H - 600, 5*self.H, 0))
        self.assertIsNotNone(pace(1, 5*self.H - 1000, 5*self.H, 0))
        self.assertIsNone(pace(1, 6*self.H, 5*self.H, 0))

    def test_steps_one_slot_at_most_every_interval(self):
        five = lambda p: [('five_hour', p)]
        self.assertEqual(auto_limit(3, 3, five(0.6), 1000, 0), (2, 1000))
        self.assertEqual(auto_limit(2, 3, five(0.6), 1000 + AUTO_STEP - 1, 1000), (2, 1000))
        self.assertEqual(auto_limit(2, 3, five(0.6), 1000 + AUTO_STEP, 1000), (1, 1000 + AUTO_STEP))

    def test_five_hour_floor_cap_and_deadband(self):
        T = 10**6
        self.assertEqual(auto_limit(1, 3, [('five_hour', 0)], T, 0), (1, 0))
        self.assertEqual(auto_limit(3, 3, [('five_hour', 5)], T, 0), (3, 0))
        self.assertEqual(auto_limit(2, 3, [('five_hour', 1.0)], T, 0), (2, 0))
        self.assertEqual(auto_limit(2, 3, [('five_hour', 1.5)], T, 0), (3, T))
        self.assertEqual(auto_limit(5, 3, [('five_hour', 1.0)], T, 0), (3, 0))

    def test_weekly_window_is_tolerant_and_keeps_two_agents(self):
        T = 10**6
        self.assertEqual(auto_limit(3, 3, [('seven_day', 0.6)], T, 0), (3, 0))
        self.assertEqual(auto_limit(3, 3, [('seven_day', 0.4)], T, 0), (2, T))
        self.assertEqual(auto_limit(2, 3, [('seven_day', 0.1)], T, 0), (2, 0))
        # Within its tolerance the weekly window does not hold back a rise.
        self.assertEqual(auto_limit(2, 3, [('five_hour', 1.5), ('seven_day', 0.6)], T, 0), (3, T))
        # Below it, nothing rises; the five-hour window can still go under two.
        self.assertEqual(auto_limit(2, 3, [('five_hour', 1.5), ('seven_day', 0.4)], T, 0), (2, 0))
        self.assertEqual(auto_limit(2, 3, [('five_hour', 0.5), ('seven_day', 0.4)], T, 0), (1, T))
        self.assertEqual(auto_limit(1, 1, [('seven_day', 0.1)], T, 0), (1, 0))

    def test_above_normal_only_with_room_in_every_window(self):
        T = 10**6
        lots = [('five_hour', 1.4), ('seven_day', 1.3)]
        self.assertEqual(auto_limit(3, 3, lots, T, 0, 5), (4, T))
        self.assertEqual(auto_limit(5, 3, lots, T, 0, 5), (5, 0))
        self.assertEqual(auto_limit(3, 3, lots, T, 0), (3, 0))  # No maximum configured.
        self.assertEqual(auto_limit(3, 3, [('five_hour', 1.4), ('seven_day', 1.2)], T, 0, 5), (3, 0))
        self.assertEqual(auto_limit(4, 3, [('five_hour', 1.15), ('seven_day', 1.2)], T, 0, 5), (4, 0))
        self.assertEqual(auto_limit(5, 3, [('five_hour', 1.05), ('seven_day', 1.3)], T, 0, 5), (4, T))
        self.assertEqual(auto_limit(5, 3, [('five_hour', 0.8)], T, 0, 5), (4, T))
        self.assertEqual(auto_limit(5, 3, [], T, 0, 5), (3, 0))

    def test_without_readings_the_configured_limit_applies(self):
        self.assertEqual(auto_limit(1, 3, [], 10**6, 0), (3, 0))


class AutoQueueTests(unittest.TestCase):
    H = 3600
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 10**6
        self.q = Queue(Path(self.temp.name)/'q.db', 3, {10:'b'}.get, auto=True, clock=lambda: self.now)

    def tearDown(self):
        self.q.db.close(); self.temp.cleanup()

    def acquire(self, key):
        return self.q.operate({'op':'acquire','key':key,'pool':'agent','session':'s','turn':key,'pid':10,'birth':'b'})['state']

    def usage(self, percent, left_hours, kind='five_hour'):
        return self.q.operate({'op':'usage','readings':[{'kind':kind,'percent':percent,'resets':self.now+left_hours*self.H}]})

    def test_running_ahead_lowers_and_on_pace_restores_without_interrupting(self):
        for k in 'abc': self.assertEqual(self.acquire(k), 'granted')
        self.assertEqual(self.usage(82, 1.1)['limit'], 2)
        self.assertEqual(self.acquire('d'), 'queued')
        self.q.operate({'op':'release','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'})
        self.assertEqual(self.acquire('d'), 'queued')  # Still 2 working: b and c.
        self.now += AUTO_STEP
        self.usage(10, 4)  # A new window: 10% used, 20% elapsed.
        self.assertEqual(self.acquire('d'), 'granted')
        self.assertTrue((Path(self.temp.name)/'auto.log').read_text().count('\n') >= 2)

    def test_readings_merge_by_window_and_keep_the_highest(self):
        self.usage(50, 2)
        self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':45,'resets':self.now+2*self.H+5}]})
        self.assertEqual(self.q.windows()[0]['percent'], 50)
        self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':90,'resets':self.now-5*self.H}]})
        self.assertEqual(self.q.windows()[0]['percent'], 50)

    def test_weekly_window_counts_and_status_reports(self):
        self.usage(10, 4)
        self.usage(90, 24*3, kind='seven_day')
        status = self.q.operate({'op':'status'})
        self.assertEqual(status['limits']['agent'], 2)  # The weekly window, far ahead, keeps two.
        self.assertEqual(status['auto']['cap'], 3)
        self.assertEqual({w['kind'] for w in status['auto']['windows']}, {'five_hour', 'seven_day'})

    def test_expired_window_returns_to_configured_limit(self):
        self.usage(82, 1.1)
        self.now += 2*self.H
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 3)

    def test_invalid_readings_are_refused(self):
        for bad in ([{'kind':'five_hour','percent':'x','resets':1}], [{'kind':'five_hour','percent':1}], 'x'):
            with self.assertRaises(ValueError):
                self.q.operate({'op':'usage','readings':bad})

    def test_spent_window_pauses_everyone_until_it_renews(self):
        for k in 'ab': self.acquire(k)
        self.usage(95, 0.5)
        status = self.q.operate({'op':'status'})
        self.assertEqual(status['limits']['agent'], 0)
        self.assertEqual(status['auto']['until'], self.now + 0.5*self.H)
        self.assertEqual([t['state'] for t in status['tickets']], ['queued', 'queued'])
        self.assertEqual(self.acquire('c'), 'queued')
        self.now += 0.5*self.H + 1
        self.assertEqual(self.acquire('a'), 'granted')
        self.assertEqual([t['state'] for t in self.q.operate({'op':'status'})['tickets']], ['granted']*3)

    def test_after_a_pause_the_maximum_returns_at_once_with_both_windows(self):
        self.usage(95, 0.5)
        self.usage(50, 24*3, kind='seven_day')
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 0)
        self.now += 5*60
        self.assertIn('paused_until', self.q.operate({'op':'acquire','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'}))
        self.now += 0.5*self.H
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 3)

    def test_no_lease_expires_during_a_pause(self):
        self.q.lease = 300
        self.acquire('a')
        self.usage(95, 1)
        self.q.db.execute("UPDATE tickets SET instance='mod', touched=touched-1000, created=created-1000")
        self.q.reap()
        self.assertEqual(self.q.db.execute("SELECT state FROM tickets WHERE key='a'").fetchone()[0], 'queued')
        self.now += 1.01*self.H
        self.q.operate({'op':'status'})
        self.q.reap()
        self.assertEqual(self.q.db.execute("SELECT state FROM tickets WHERE key='a'").fetchone()[0], 'released')

    def test_weekly_window_pause_also_ends_at_its_reset(self):
        self.usage(96, 30, kind='seven_day')
        self.assertEqual(self.acquire('a'), 'queued')
        self.now += 31*self.H
        self.assertEqual(self.acquire('a'), 'granted')

    def test_configured_maximum_reaches_queue_and_status(self):
        home = Path(self.temp.name)/'home'
        initialize(home, 3)
        result = initialize(home, None, replace=True, ceiling=5)
        self.assertEqual(result['auto']['max'], 5)
        self.assertEqual(json.loads((home/'config.json').read_text())['max'], 5)
        self.assertEqual(initialize(home, 4, replace=True)['auto']['max'], 5)
        self.assertNotIn('max', json.loads(initialize(home, 6, replace=True) and (home/'config.json').read_text()))

    def test_auto_off_ignores_usage(self):
        q = Queue(Path(self.temp.name)/'q2.db', 3, {10:'b'}.get, auto=False, clock=lambda: self.now)
        try:
            q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':99,'resets':self.now+self.H}]})
            self.assertEqual(q.operate({'op':'status'})['limits']['agent'], 3)
        finally:
            q.db.close()


if __name__ == '__main__': unittest.main()
