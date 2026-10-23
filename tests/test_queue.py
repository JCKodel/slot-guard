import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sqlite3
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import (ACTIVITY_BUCKET, AUTO_STEP, RESTART_CONFIRM, Queue, auto_limit, configure, duty, initialize, line_cap, line_pace,
                   line_until, local_call, pace, process_identity, rate_load, recent_rate)


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

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('cc'), 'macOS with a C compiler')
class OwnerTests(unittest.TestCase):
    def test_claude_deleted_by_its_updater_is_still_found(self):
        # Claude Code's updater deletes an old version while sessions still run it.
        with tempfile.TemporaryDirectory() as temp:
            versions = Path(temp)/'claude/versions'
            versions.mkdir(parents=True)
            source = Path(temp)/'claude.c'
            # It says when it is loaded, then waits for its stdin to close.
            source.write_text('#include <stdio.h>\n#include <stdlib.h>\n#include <unistd.h>\n'
                              'int main(int c, char **v) { char b; fputs("ready\\n", stderr); fflush(stderr);\n'
                              '  while (read(0, &b, 1) > 0); return system(v[1]); }\n')
            binary = versions/'2.1.999'
            subprocess.run(['cc', '-o', str(binary), str(source)], check=True)
            script = f'import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); ' \
                     'import guard, json; print(json.dumps(guard.claude_owner()))'
            p = subprocess.Popen([str(binary), f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(p.stderr.readline(), 'ready\n')
            binary.unlink()
            out, err = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(json.loads(out)['pid'], p.pid)


class AutoLimitTests(unittest.TestCase):
    H = 3600
    def test_pace_follows_the_recent_rate(self):
        H = self.H
        # A fast start: 14% in the first 30 minutes, 28%/h against a sustainable ~18%/h.
        self.assertLess(pace(14, 4.5*H, 5*H, 0, rate=14/(0.5*H)), 0.9)
        # This morning's case: 26% used, but only ~4%/h lately with 3.65 h left.
        self.assertGreater(pace(26, 3.65*H, 5*H, 0, rate=4/H), 1.25)
        self.assertEqual(pace(30, 2*H, 5*H, 0, rate=0), float('inf'))
        self.assertEqual(pace(99, 2*H, 5*H, 0, rate=1/H), 0)

    def test_without_a_rate_pace_compares_budget_and_time_left(self):
        self.assertAlmostEqual(pace(82, 1.1*self.H, 5*self.H, 0), (16/98)/0.22, places=6)
        self.assertGreater(pace(30, 2*self.H, 5*self.H, 0), 1)

    def test_recent_rate_needs_a_long_enough_span(self):
        H = self.H
        self.assertIsNone(recent_rate([(10, 0)], 100, 'five_hour'))
        self.assertIsNone(recent_rate([(10, 0), (11, 300)], 400, 'five_hour'))
        self.assertAlmostEqual(recent_rate([(10, 0), (12, 1200)], 1800, 'five_hour'), 2/1800)
        # Readings older than the span are left out; a quiet hour means no burn.
        self.assertIsNone(recent_rate([(5, 0), (10, 600)], 2*H, 'five_hour'))
        self.assertEqual(recent_rate([(10, 0), (10, 900)], 1000, 'five_hour'), 0)

    def test_active_rate_counts_only_working_time(self):
        H, B = self.H, ACTIVITY_BUCKET
        # 6% over 3 hours of work spread across 9 hours: 2% per active hour, not 0.67.
        work = [t for t in range(0, 9*H, B) if t % (3*H) < H]
        history = [(2, 0), (4, 3*H), (6, 6*H), (8, 9*H - 1)]
        self.assertAlmostEqual(recent_rate(history, 9*H, 'seven_day', work)*H, 2.0)
        # Minimum history in active time: two working hours of readings at least.
        self.assertIsNone(recent_rate([(2, 0), (9, 3*H)], 3*H, 'seven_day', work[:12]))
        self.assertIsNone(recent_rate(history, 9*H, 'seven_day', []))

    def test_clock_rate_and_load_both_include_idle_time(self):
        work = [0, 300, 600, 900]
        history = [(10, 0), (14, 3600)]
        rate = recent_rate(history, 3600, 'five_hour')
        load = rate_load(history, 3600, 'five_hour', work, {b: 2 for b in work})
        self.assertAlmostEqual(load, 2/3)
        self.assertAlmostEqual(rate / load * 3600, 6)  # 4 points / (2 agents * 1/3 hour).

    def test_duty_learns_hours_a_day_from_a_prior(self):
        D, B = 86400, ACTIVITY_BUCKET
        self.assertAlmostEqual(duty([], None, 10*D), 1/3)
        # A week of 4 hours a day pulls the 8-hour prior down; a week of 16 pushes it up.
        four = [d*D + t for d in range(3, 10) for t in range(0, 4*3600, B)]
        sixteen = [d*D + t for d in range(3, 10) for t in range(0, 16*3600, B)]
        self.assertAlmostEqual(duty(four, four[0], 10*D)*24, (7*4 + 8)/8, places=1)
        self.assertAlmostEqual(duty(sixteen, sixteen[0], 10*D)*24, (7*16 + 8)/8, places=1)
        # A week off: only the prior's 8 hours remain, spread over eight days.
        self.assertAlmostEqual(duty(four, four[0], 20*D)*24, 1.0)

    def test_weekly_pace_projects_over_working_hours(self):
        H = self.H
        # 2.3% per active hour, 9% used, 125 hours left: four agents' worth against 8 hours a day.
        self.assertLess(pace(9, 125*H, 7*86400, 0, rate=2.3/H), 0.5)
        self.assertGreater(pace(9, 125*H, 7*86400, 0, rate=2.3/H, duty=1/3), 0.85)
        self.assertGreater(pace(9, 125*H, 7*86400, 0, rate=1.5/H, duty=1/3), 1.1)

    def test_pace_ignores_expired_and_first_minutes(self):
        self.assertIsNone(pace(10, -1, 5*self.H, 0))
        self.assertIsNone(pace(1, 5*self.H - 600, 5*self.H, 0))
        self.assertIsNotNone(pace(1, 5*self.H - 1000, 5*self.H, 0))
        self.assertIsNone(pace(1, 6*self.H, 5*self.H, 0))

    def test_reduction_is_immediate_even_during_recovery_cooldown(self):
        signals = [('seven_day', 1.06, 1)]
        self.assertEqual(auto_limit(6, 6, signals, 1001, 1000), (1, 1001))
        self.assertEqual(auto_limit(1, 6, signals, 1002, 1001), (1, 1001))

    def test_capacity_rounds_down_and_strictest_window_wins(self):
        self.assertEqual(auto_limit(6, 6, [('five_hour', 2.9, 1)], 1000, 999), (2, 1000))
        self.assertEqual(auto_limit(6, 6, [('five_hour', 4, 2), ('seven_day', 1.5, 2)], 1000, 999), (3, 1000))
        self.assertEqual(auto_limit(6, 6, [('seven_day', 0.43)], 1000, 999), (2, 1000))

    def test_recovery_requires_cooldown_and_headroom_for_new_slot(self):
        enough = [('seven_day', 6.6, 1)]
        self.assertEqual(auto_limit(1, 6, enough, AUTO_STEP - 1, 0), (1, 0))
        self.assertEqual(auto_limit(1, 6, enough, AUTO_STEP, 0), (2, AUTO_STEP))
        self.assertEqual(auto_limit(1, 6, [('seven_day', 2.05, 1)], AUTO_STEP, 0), (1, 0))
        self.assertEqual(auto_limit(6, 6, enough, AUTO_STEP, 0, 8), (6, 0))
        self.assertEqual(auto_limit(6, 6, [('seven_day', 8, 1)], AUTO_STEP, 0, 8), (7, AUTO_STEP))

    def test_continuous_ceiling_releases_budget_every_minute_and_ends_at_98(self):
        for length in (5*self.H, 7*24*self.H):
            self.assertEqual(line_cap(length, length, 0), 0)
            self.assertAlmostEqual(line_cap(length, length, length*0.86), 84.28)
            self.assertAlmostEqual(line_cap(length, length, 60), 98*60/length)
            self.assertEqual(line_cap(length, length, length), 98)
            self.assertEqual(line_cap(length, length, length+60), 98)
        self.assertEqual(line_cap(7*24*self.H, 7*24*self.H, 24*self.H), 14)

    def test_two_points_of_margin_support_one_at_observed_rate(self):
        length = 7*24*self.H
        now = length * 86/98
        p = line_pace(84, length, length, now, 1.09/self.H)
        self.assertLess(p, 1)
        self.assertEqual(auto_limit(6, 6, [('seven_day', p, 1.03)], now, now-1)[0], 1)
        self.assertIsNone(line_until(84, length, length, now))
        # Plenty of margin sustains all six at this same consumption per agent.
        p = line_pace(55, length, length, now, 1.09/self.H)
        self.assertEqual(auto_limit(6, 6, [('seven_day', p, 1.03)], now, now-1)[0], 6)

    def test_line_allows_equality_and_waits_only_until_continuous_catchup(self):
        for length in (5*self.H, 7*24*self.H):
            self.assertIsNone(line_until(49, length, length, length/2))
            self.assertAlmostEqual(line_until(50, length, length, length/2), length*50/98)
            self.assertEqual(line_until(98, length, length, length-1), length)
            self.assertIsNone(line_until(98, length, length, length))
            self.assertIsNone(line_pace(0, length, length, length, None))

    def test_missing_or_flat_readings_do_not_unlock_all_slots(self):
        length = 7*24*self.H
        for rate in (None, 0):
            p = line_pace(48.9, length, length, length/2, rate)
            self.assertEqual(auto_limit(6, 6, [('seven_day', p, 1)], 1000, 0)[0], 1)
        self.assertEqual(auto_limit(1, 6, [], 1000, 0), (6, 0))

    def test_week_long_feedback_simulation_stays_below_continuous_ceiling(self):
        length = 7*24*self.H
        used, limit, changed = 0.0, 6, -AUTO_STEP
        # Deterministic steady workload, per-agent burn below the budget refill rate.
        # Evaluate every minute, including startup, day boundaries and the final minute.
        for now in range(0, length, 60):
            self.assertIsNone(line_until(used, length, length, now))
            p = line_pace(used, length, length, now, 0.4/self.H)
            limit, changed = auto_limit(limit, 6, [('seven_day', p, 1)], now, changed)
            used += limit * 0.4 / 60
            self.assertLessEqual(used, line_cap(length, length, now+60) + 1e-8)
        self.assertGreater(used, 95)
        self.assertLessEqual(used, 98)


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
        self.assertEqual(self.usage(75, 1.1)['limit'], 1)
        self.assertEqual(self.acquire('d'), 'queued')
        self.q.operate({'op':'release','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'})
        self.assertEqual(self.acquire('d'), 'queued')  # Still two in-flight calls: b and c; the limit is now one.
        self.now += AUTO_STEP
        self.usage(10, 1)  # A new window with enough margin: recover one slot at a time.
        self.assertEqual(self.acquire('d'), 'queued')
        self.now += AUTO_STEP
        self.usage(10, 1 - AUTO_STEP/self.H)
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
        self.usage(40, 24*4, kind='seven_day')  # Under the line: 42.9% of the week gone.
        status = self.q.operate({'op':'status'})
        self.assertEqual(status['limits']['agent'], 1)
        self.assertEqual(status['auto']['cap'], 3)
        weekly = next(w for w in status['auto']['windows'] if w['kind'] == 'seven_day')
        self.assertAlmostEqual(weekly['ceiling'], 42)
        self.assertAlmostEqual(weekly['margin'], 2)
        self.assertEqual({w['kind'] for w in status['auto']['windows']}, {'five_hour', 'seven_day'})

    def test_over_the_weekly_line_pauses_until_it_passes(self):
        for k in 'ab': self.acquire(k)
        self.usage(49, 24*3.5, kind='seven_day')  # Exactly on the line is allowed.
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 1)
        self.usage(50, 24*3.5, kind='seven_day')
        status = self.q.operate({'op':'status'})
        self.assertEqual(status['limits']['agent'], 0)
        self.assertTrue(status['auto']['line'])
        self.assertAlmostEqual(status['auto']['until'], self.now + 7*86400/98)
        self.assertEqual([t['state'] for t in status['tickets']], ['queued', 'queued'])
        self.assertEqual(self.acquire('c'), 'queued')
        self.now += 7*86400/98 + 1
        self.assertEqual(self.acquire('c'), 'queued')
        status = self.q.operate({'op':'status'})
        self.assertFalse(status['auto']['line'])
        self.assertEqual([t['state'] for t in status['tickets']], ['granted', 'queued', 'queued'])

    def test_reduced_limit_is_enforced_at_next_step_without_waiting_for_quantum(self):
        for k in 'abc': self.acquire(k)
        self.usage(75, 1.1)
        # Existing calls finish; their next acquire must yield even with no other waiters.
        for k in 'ab':
            ticket = dict(op='acquire', key=k, pool='agent', session='s', turn=k,
                          pid=10, birth='b', rotate=True)
            self.assertEqual(self.q.operate(ticket)['state'], 'queued')
        self.assertEqual(self.acquire('c'), 'granted')
        self.assertEqual(self.q.db.execute("SELECT count(*) FROM tickets WHERE state='granted'").fetchone()[0], 1)

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
        self.usage(98, 0.5)
        status = self.q.operate({'op':'status'})
        self.assertEqual(status['limits']['agent'], 0)
        self.assertEqual(status['auto']['until'], self.now + 0.5*self.H)
        self.assertEqual([t['state'] for t in status['tickets']], ['queued', 'queued'])
        self.assertEqual(self.acquire('c'), 'queued')
        self.now += 0.5*self.H + 1
        self.assertEqual(self.acquire('a'), 'granted')
        self.assertEqual([t['state'] for t in self.q.operate({'op':'status'})['tickets']], ['granted']*3)

    def test_after_a_pause_the_maximum_returns_at_once_with_both_windows(self):
        self.usage(98, 0.5)
        self.usage(30, 24*3, kind='seven_day')
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 0)
        self.now += 5*60
        self.assertIn('paused_until', self.q.operate({'op':'acquire','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'}))
        self.now += 0.5*self.H
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 3)

    def test_no_lease_expires_during_a_pause(self):
        self.q.lease = 300
        self.acquire('a')
        self.usage(98, 1)
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

    def test_a_plan_change_restarts_the_window_and_the_limit(self):
        for k in 'abc': self.acquire(k)
        resets = self.now + 128*self.H  # 24% elapsed.
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        weekly(41)  # Over the weekly line: paused.
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 0)
        self.now += AUTO_STEP
        self.assertEqual(weekly(39)['limit'], 0)  # A slightly older response.
        self.assertEqual(self.q.windows()[0]['percent'], 41)
        weekly(1)  # The upgraded account's window, or a stale response: wait for another.
        self.assertEqual(self.q.windows()[0]['percent'], 41)
        self.now += RESTART_CONFIRM
        self.assertEqual(weekly(2)['limit'], 3)
        self.assertEqual(self.q.windows()[0]['percent'], 2)
        self.assertIn('restart', (Path(self.temp.name)/'auto.log').read_text())
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM usage_log').fetchone()[0], 1)

    def test_a_stale_response_neither_restarts_nor_counts_as_burn(self):
        # An idle session reports 32% from hours ago, then a live one 38% six seconds later.
        resets = self.now + 80*self.H
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        weekly(38)
        weekly(32)
        self.now += 6
        weekly(38)
        log = Path(self.temp.name)/'auto.log'
        self.assertNotIn('restart', log.read_text() if log.exists() else '')
        self.assertEqual([p for p, in self.q.db.execute('SELECT percent FROM usage_log')], [38])
        # A stale low reading long after does not commit a restart once a live one came in between.
        self.now += RESTART_CONFIRM
        weekly(32)
        self.assertEqual(self.q.windows()[0]['percent'], 38)

    def test_a_burst_is_weighed_against_the_whole_week(self):
        H, B = self.H, ACTIVITY_BUCKET
        # Ten working hours a day; 3%/h over the last three, 41% over the week's ~38 working hours.
        resets = self.now + 78*H
        start = resets - 7*24*H
        days = [self.now - d*24*H for d in range(5)]  # The last one before the window began.
        for end in days:
            for t in range(int(end - 10*H) // B * B, int(end), B):
                self.q.db.execute('INSERT OR IGNORE INTO activity VALUES (?)', (t,))
                self.q.db.execute('INSERT OR IGNORE INTO load VALUES (?,6,2)', (t,))  # All 3 slots working.
        self.q.db.commit()
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        self.now -= 3*H; weekly(32)
        self.now += 3*H; weekly(41)
        w = self.q.windows()[0]
        self.assertEqual(w['load'], 3)
        self.assertAlmostEqual(w['rate'], 3.0, places=1)
        self.assertLess(w['average'], 1.2)
        self.assertGreater(w['pace'], 0.75)  # The burst alone would say 0.52.
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 3)

    def test_working_time_is_recorded_and_outlives_a_reset(self):
        self.usage(9, 125, kind='seven_day')
        self.q.operate({'op':'status'})
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM activity').fetchone()[0], 0)  # Nobody working.
        self.acquire('a')
        for _ in range(3):
            self.now += ACTIVITY_BUCKET
            self.q.reap()  # Every coordinator call reaps first, heartbeats included.
            self.q.operate({'op':'touch','keys':['a'],'pid':10,'birth':'b'})
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM activity').fetchone()[0], 3)
        self.q.operate({'op':'reset','usage':True})
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM activity').fetchone()[0], 3)
        self.usage(9, 125, kind='seven_day')
        weekly = self.q.windows()[0]
        self.assertIsNone(weekly['rate'])  # A restarted window waits for two working hours of readings.
        self.assertGreater(weekly['hours'], 8)

    def test_agents_working_behind_the_rate_are_recorded(self):
        resets = self.now + 4*self.H
        five = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':percent,'resets':resets}]})
        self.acquire('a'); self.acquire('b')
        five(10)
        for _ in range(4):
            self.now += ACTIVITY_BUCKET
            self.q.reap()
            self.q.operate({'op':'touch','keys':['a','b'],'pid':10,'birth':'b'})
        five(14)
        # The measured interval contains four buckets with both; the earlier bucket saw 'a' alone (granting 'b') and once with both.
        self.assertEqual(self.q.windows()[0]['load'], 2.0)
        first = int((self.now - 4*ACTIVITY_BUCKET) // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
        self.assertEqual(tuple(self.q.db.execute('SELECT agents,samples FROM load WHERE bucket=?', (first,)).fetchone()), (3, 2))

    def after_working(self, ago, limit):
        """The limit `limit`, last changed and last worked `ago` seconds before now."""
        bucket = int((self.now - ago) // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
        self.q.db.execute('INSERT INTO activity VALUES (?)', (bucket,))
        self.q.db.execute("INSERT OR REPLACE INTO control VALUES ('limit',?),('changed',?)", (limit, self.now - ago))
        self.q.db.commit()

    def test_after_a_night_the_limit_settles_at_once(self):
        self.after_working(14*self.H, 1)
        resets = self.now + 104*self.H
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        self.assertEqual(weekly(18)['limit'], 3)  # On pace: straight to normal.
        self.assertEqual(self.acquire('a'), 'granted')
        self.q.reap()  # The next call (a heartbeat) marks the work: back to one step at a time.
        self.now += AUTO_STEP
        self.assertEqual(weekly(60)['limit'], 0)  # Over the line: paused, one step down beneath.
        self.assertEqual(self.stored_limit(), 1)

    def stored_limit(self):
        return self.q.db.execute("SELECT value FROM control WHERE name='limit'").fetchone()[0]

    def test_after_a_night_far_ahead_settles_down_at_once(self):
        self.after_working(14*self.H, 3)
        self.assertEqual(self.usage(90, 72, kind='seven_day')['limit'], 0)
        self.assertEqual(self.stored_limit(), 1)

    def test_the_first_working_call_after_a_night_still_settles(self):
        self.after_working(14*self.H, 1)
        self.q.db.execute("INSERT INTO tickets (key,pool,session,turn,pid,birth,state,created) "
                          "VALUES ('a','agent','s','a',10,'b','granted',?)", (self.now,))
        self.assertEqual(self.usage(18, 104, kind='seven_day')['limit'], 3)

    def test_without_a_pause_the_limit_moves_one_step(self):
        self.after_working(AUTO_STEP, 1)
        self.assertEqual(self.usage(18, 104, kind='seven_day')['limit'], 2)

    def test_reset_forgets_readings_and_restarts_from_the_normal_limit(self):
        self.usage(41, 128, kind='seven_day'); self.usage(98, 1)
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 0)
        self.assertEqual(self.q.operate({'op':'reset'})['limit'], 0)  # Readings kept: still paused.
        self.assertEqual(self.q.operate({'op':'reset','usage':True})['limit'], 3)
        self.assertEqual(self.q.operate({'op':'status'})['auto']['windows'], [])
        self.assertEqual(self.acquire('a'), 'granted')
        # Reset does not bypass an immediate safety reduction.
        self.assertEqual(self.usage(75, 1.1)['limit'], 1)
        self.now += 1
        self.assertEqual(self.usage(76, 1.1)['limit'], 1)

    def test_readings_log_outlives_forget(self):
        self.usage(41, 128, kind='seven_day'); self.usage(41, 128, kind='seven_day'); self.usage(42, 128, kind='seven_day')
        self.q.operate({'op':'reset','usage':True})
        self.usage(43, 128, kind='seven_day')
        log = [json.loads(l) for l in self.q.readings.read_text().splitlines()]
        self.assertEqual([(e['kind'], e['percent']) for e in log], [('seven_day', 41), ('seven_day', 42), ('seven_day', 43)])

    def test_configure_applies_the_new_limit_at_once(self):
        home = Path(self.temp.name)/'home'
        initialize(home, 3)
        now = time.time()
        local_call(home, {'op':'usage','readings':[{'kind':'five_hour','percent':75,'resets':now+1.1*3600}]})
        self.assertEqual(local_call(home, {'op':'status'})['limits']['agent'], 1)
        result = configure(home, {'agents': 5})
        self.assertEqual((result['config']['agents'], result['limits']['agent']), (5, 1))
        self.assertEqual(local_call(home, {'op':'status'})['limits']['agent'], 1)
        self.assertNotIn('max', configure(home, {'max': 2})['config'])  # Never under the normal limit.
        self.assertEqual(configure(home, {'max': 8})['auto']['max'], 8)
        self.assertEqual(configure(home, {'auto': False})['auto'], {'enabled': False})
        self.assertEqual(configure(home, {'quantum': 7})['config']['quantum'], 7)
        for bad in ({}, {'agents': 0}, {'agents': '3'}, {'auto': 'on'}, {'max': 1.5}):
            with self.assertRaises(ValueError):
                configure(home, bad)

    def test_configured_maximum_reaches_queue_and_status(self):
        home = Path(self.temp.name)/'home'
        initialize(home, 3)
        result = initialize(home, None, replace=True, ceiling=5)
        self.assertEqual(result['auto']['max'], 5)
        self.assertEqual(json.loads((home/'config.json').read_text())['max'], 5)
        self.assertEqual(initialize(home, 4, replace=True)['auto']['max'], 5)
        self.assertNotIn('max', json.loads(initialize(home, 6, replace=True) and (home/'config.json').read_text()))

    def ticket(self, key, **extra):
        return {'op':'acquire','key':key,'pool':'agent','session':'s','turn':key,'pid':10,'birth':'b', **extra}

    def test_sprint_starts_one_sessions_agents_over_the_limit_until_it_ends(self):
        for k in 'abc': self.q.operate(self.ticket(k))
        self.assertEqual(self.q.operate(self.ticket('u', session='urgent'))['state'], 'queued')
        until = self.q.operate({'op':'sprint','session':'urgent','minutes':5})['sprints']['urgent']
        self.assertEqual(until, self.now + 300)
        self.assertEqual(self.state_of('u'), 'granted')
        self.assertEqual(self.q.operate(self.ticket('u2', session='urgent'))['state'], 'granted')
        self.q.operate({'op':'release','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'})
        self.assertEqual(self.q.operate(self.ticket('other'))['state'], 'queued')  # The sprint counts.
        self.age_grant('u', 120)
        self.assertEqual(self.q.operate(self.ticket('u', session='urgent', rotate=True))['state'], 'granted')
        self.assertEqual(self.q.operate({'op':'status'})['sprints'], {'urgent': self.now + 300})
        self.now += 301
        self.assertEqual(self.q.operate({'op':'status'})['sprints'], {})
        self.assertEqual(self.q.operate(self.ticket('u3', session='urgent'))['state'], 'queued')
        self.assertEqual(self.q.operate(self.ticket('u', session='urgent', rotate=True))['state'], 'queued')

    def test_a_free_slot_goes_to_the_sprint_before_the_queue(self):
        for k in 'ab': self.q.operate(self.ticket(k))
        # Both wait for the one free slot, x first in line, when the sprint starts.
        self.q.db.execute("INSERT INTO tickets(key,pool,session,turn,pid,birth,state,created) VALUES "
                          "('x','agent','s','x',10,'b','queued',1),('u','agent','urgent','u',10,'b','queued',2)")
        self.q.db.commit()
        self.q.operate({'op':'sprint','session':'urgent','minutes':5})
        self.assertEqual((self.state_of('u'), self.state_of('x')), ('granted', 'queued'))
        self.assertEqual(self.q.db.execute("SELECT count(*) FROM tickets WHERE state='granted'").fetchone()[0], 3)

    def test_sprint_for_every_session_ends_on_off_and_never_during_a_pause(self):
        for k in 'abc': self.q.operate(self.ticket(k))
        self.q.operate({'op':'sprint','session':'*','minutes':5})
        self.assertEqual(self.q.operate(self.ticket('d'))['state'], 'granted')
        self.q.operate({'op':'sprint','session':'*','minutes':0})
        self.assertEqual(self.q.operate(self.ticket('e'))['state'], 'queued')
        self.q.operate({'op':'sprint','session':'*','minutes':5})
        self.usage(96, 1)
        self.assertEqual(self.q.operate(self.ticket('f'))['state'], 'queued')
        self.assertEqual(self.state_of('d'), 'queued')
        for bad in (-1, 61, 1.5, None):
            with self.assertRaises(ValueError):
                self.q.operate({'op':'sprint','session':'s','minutes':bad})

    def test_work_under_way_goes_before_new_prompts(self):
        for k in 'abc': self.q.operate(self.ticket(k))
        self.assertEqual(self.q.operate(self.ticket('new'))['position'], 1)
        child = self.q.operate(self.ticket('child', rank=0))
        self.assertEqual((child['state'], child['position']), ('queued', 1))
        self.q.operate({'op':'release','key':'a','pool':'agent','session':'s','turn':'a','pid':10,'birth':'b'})
        self.assertEqual(self.q.operate(self.ticket('child', rank=0))['state'], 'granted')
        self.assertEqual(self.q.operate(self.ticket('new'))['state'], 'queued')

    def test_forced_admission_cannot_bypass_automatic_limit(self):
        for k in 'abc': self.q.operate(self.ticket(k))
        self.assertEqual(self.q.operate(self.ticket('late', rank=0, force=True))['state'], 'queued')
        self.q.auto = False
        self.assertEqual(self.q.operate(self.ticket('late', rank=0, force=True))['state'], 'granted')

    def test_limit_recovers_when_recent_usage_slows(self):
        for k in 'abc': self.acquire(k)
        # Window 30 min in: a burst to 14%.
        self.usage(1, 4.75); self.now += 25*60; self.usage(14, 4.5 - 25/60 + 0.25)
        self.assertLess(self.q.operate({'op':'status'})['limits']['agent'], 3)
        # Hours later, slow: 26% with 3.65 h left, only 2 points in the last hour.
        resets = self.q.windows()[0]['resets']
        for minutes in range(0, 80, 10):
            self.now += 600
            self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':24 + minutes/30,'resets':resets}]})
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 3)

    def age_grant(self, key, seconds):
        self.q.db.execute('UPDATE tickets SET since=since-? WHERE key=?', (seconds, key)); self.q.db.commit()

    def state_of(self, key):
        return self.q.db.execute('SELECT state FROM tickets WHERE key=?', (key,)).fetchone()[0]

    def test_rotation_hands_the_slot_on_after_a_quantum_only_when_someone_waits(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b'}.get)
        self.q.db.close(); self.q = q
        self.q.operate(self.ticket('a'))
        self.age_grant('a', 120)
        self.assertEqual(self.q.operate(self.ticket('a', rotate=True))['state'], 'granted')  # Nobody waits.
        self.q.operate(self.ticket('b'))
        self.age_grant('a', 30)
        self.assertEqual(self.q.operate(self.ticket('a', rotate=True))['state'], 'queued')  # Quantum over.
        self.assertEqual(self.state_of('b'), 'granted')
        self.assertEqual(self.q.operate(self.ticket('b', rotate=True))['state'], 'granted')  # Its own quantum.
        self.age_grant('b', 61)
        self.assertEqual(self.q.operate({'op':'rotate','keys':['b'],'pid':10,'birth':'b'})['rotated'], ['b'])
        self.assertEqual((self.state_of('a'), self.state_of('b')), ('granted', 'queued'))
        self.assertEqual(self.q.db.execute("SELECT count(*) FROM tickets WHERE state='granted'").fetchone()[0], 1)

    def test_rotated_agents_queue_behind_first_grants(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b'}.get)
        self.q.db.close(); self.q = q
        self.q.operate(self.ticket('a', rank=0)); self.q.operate(self.ticket('b', rank=0))
        self.age_grant('a', 61)
        self.q.operate({'op':'rotate','keys':['a'],'pid':10,'birth':'b'})
        self.q.operate(self.ticket('child', rank=0))
        # b (waiting first grant) runs; the new child goes before a, which already had its turn.
        self.assertEqual(self.state_of('b'), 'granted')
        self.q.operate({'op':'release','key':'b','pool':'agent','session':'s','turn':'b','pid':10,'birth':'b'})
        self.assertEqual((self.state_of('child'), self.state_of('a')), ('granted', 'queued'))

    def test_a_long_wait_earns_the_priority_of_work_under_way(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b'}.get)
        self.q.db.close(); self.q = q
        self.q.operate(self.ticket('run'))
        self.q.operate(self.ticket('prompt'))                 # rank 1
        self.q.operate(self.ticket('sub1', rank=0))
        self.assertEqual(self.q.operate(self.ticket('prompt'))['position'], 2)
        self.q.db.execute("UPDATE tickets SET created=created-301 WHERE key='prompt'"); self.q.db.commit()
        self.q.operate(self.ticket('sub2', rank=0))
        self.assertEqual(self.q.operate(self.ticket('prompt'))['position'], 1)

    def test_rotate_ignores_other_owners_and_fresh_restarts_the_quantum(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b', 11:'c'}.get)
        self.q.db.close(); self.q = q
        self.q.operate(self.ticket('a')); self.q.operate(self.ticket('b'))
        self.age_grant('a', 120)
        self.assertEqual(self.q.operate({'op':'rotate','keys':['a'],'pid':11,'birth':'c'})['rotated'], [])
        self.q.operate(self.ticket('a', fresh=True))
        self.assertEqual(self.q.operate({'op':'rotate','keys':['a'],'pid':10,'birth':'b'})['rotated'], [])
        self.assertEqual(self.state_of('a'), 'granted')

    def test_auto_off_ignores_usage(self):
        q = Queue(Path(self.temp.name)/'q2.db', 3, {10:'b'}.get, auto=False, clock=lambda: self.now)
        try:
            q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':99,'resets':self.now+self.H}]})
            self.assertEqual(q.operate({'op':'status'})['limits']['agent'], 3)
        finally:
            q.db.close()


if __name__ == '__main__': unittest.main()
