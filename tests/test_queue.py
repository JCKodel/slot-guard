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
from guard import (ACTIVITY_BUCKET, AUTO_STEP, RESTART_CONFIRM, URGENT, Queue, auto_limit, configure, duty, initialize, line_cap,
                   capacity, line_until, local_call, process_identity, rate_load, recent_rate)


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

    def test_recent_rate_needs_a_long_enough_span(self):
        H = self.H
        self.assertIsNone(recent_rate([(10, 0)], 100, 'five_hour'))
        self.assertIsNone(recent_rate([(10, 0), (11, 300)], 400, 'five_hour'))
        self.assertAlmostEqual(recent_rate([(10, 0), (12, 1200)], 1800, 'five_hour'), 2/1800)
        # Readings older than the span are left out; a quiet hour means no burn.
        self.assertIsNone(recent_rate([(5, 0), (10, 600)], 2*H, 'five_hour'))
        self.assertEqual(recent_rate([(10, 0), (10, 900)], 1000, 'five_hour'), 0)


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


    def test_continuous_ceiling_releases_budget_every_minute_and_ends_at_98(self):
        for length in (5*self.H, 7*24*self.H):
            self.assertEqual(line_cap(length, length, 0), 1)  # A renewed window's floor.
            self.assertAlmostEqual(line_cap(length, length, length*0.86), 84.28)
            self.assertAlmostEqual(line_cap(length, length, length*0.02), 98*0.02)
            self.assertEqual(line_cap(length, length, length), 98)
            self.assertEqual(line_cap(length, length, length+60), 98)
        self.assertEqual(line_cap(7*24*self.H, 7*24*self.H, 24*self.H), 14)

    def test_active_rate_counts_only_working_time(self):
        H, B = self.H, ACTIVITY_BUCKET
        # 3% over 1.5 hours of work spread across 3 hours: 2% per active hour, not 1.
        work = [t for t in range(0, 3*H, B) if t % H < H/2]
        history = [(2, 0), (3, H), (4, 2*H), (5, 3*H - 1)]
        self.assertAlmostEqual(recent_rate(history, 3*H, 'seven_day', work)*H, 2.0)
        # Minimum history in active time: one working hour of readings at least.
        self.assertIsNone(recent_rate([(2, 0), (9, 3*H)], 3*H, 'seven_day', work[:6]))
        self.assertIsNone(recent_rate(history, 3*H, 'seven_day', []))

    def test_capacity_spreads_what_is_left_over_the_working_time_left(self):
        H, W = self.H, 7*86400
        # On the ceiling (25.08% at 125 hours left), 1%/h per agent: 72 points over 12
        # hours a day of the 125 left, where the margin left to burst is only the point.
        now = W - 125*H
        self.assertAlmostEqual(capacity(25, W, W, now, 1/H, 1/2), 72/(125/2), places=6)
        # Twice the burn per agent, half the agents.
        self.assertAlmostEqual(capacity(25, W, W, now, 2/H, 1/2), 36/(125/2), places=6)

    def test_capacity_bursts_a_margin_under_the_ceiling_over_three_hours(self):
        H, W = self.H, 7*86400
        # 9% used where the ceiling is at 25.08%: 17 points to spend over 3 hours, on top
        # of the ceiling's 0.58 an hour, rather than 88 points over the learned 8 hours a day.
        now = W - 125*H
        margin = line_cap(W, W, now) + 1 - 9
        self.assertAlmostEqual(capacity(9, W, W, now, 1/H, 1/3), margin/3 + 98*H/W, places=6)
        # Near the end the burst aims at 97%, not at the ceiling's tolerance past 98%.
        self.assertAlmostEqual(capacity(95, W, W, W - 2*H, 1/H), 2/2 + 98*H/W, places=6)
        self.assertIsNone(capacity(9, W, W, W, 1/H))
        self.assertIsNone(capacity(9, W, W, -1, 1/H))

    def test_capacity_never_outruns_the_continuous_ceiling(self):
        H, W = self.H, 7*86400
        # A renewed week at 4 hours a day would spread 3.5%/h, but the ceiling holds its floor
        # and point of tolerance plus what it releases in the next hour: 2.58 points.
        self.assertAlmostEqual(capacity(0, W, W, 60, 1/H, 1/6), (2 + 98*H/W), places=6)
        # At 8 hours a day the spread, 1.73%/h, is under that.
        self.assertAlmostEqual(capacity(0, W, W, 60, 1/H, 1/3), 97/((W - 60)/3/H), places=6)
        # On the line with no tolerance left the ceiling allows only what it releases.
        self.assertAlmostEqual(capacity(51, W, W, W/2, 1/H, 1/3), 98*H/W, places=6)
        # The 5-hour window releases 19.6 points an hour: its ceiling seldom binds.
        self.assertGreater(capacity(0, 5*H, 5*H, 60, 2/H), 9)

    def test_reduction_is_immediate_even_during_recovery_cooldown(self):
        signals = [('seven_day', 1.06)]
        self.assertEqual(auto_limit(6, 6, signals, 1001, 1000), (1, 1001))
        self.assertEqual(auto_limit(1, 6, signals, 1002, 1001), (1, 1001))

    def test_capacity_rounds_down_and_strictest_window_wins(self):
        self.assertEqual(auto_limit(6, 6, [('five_hour', 2.9)], 1000, 999), (2, 1000))
        self.assertEqual(auto_limit(6, 6, [('five_hour', 8), ('seven_day', 3)], 1000, 999), (3, 1000))
        self.assertEqual(auto_limit(6, 6, [('seven_day', 0.43)], 1000, 999), (1, 1000))

    def test_recovery_requires_cooldown_and_headroom_for_new_slot(self):
        enough = [('seven_day', 6.6)]
        self.assertEqual(auto_limit(1, 6, enough, AUTO_STEP - 1, 0), (1, 0))
        self.assertEqual(auto_limit(1, 6, enough, AUTO_STEP, 0), (2, AUTO_STEP))
        self.assertEqual(auto_limit(1, 6, [('seven_day', 2.05)], AUTO_STEP, 0), (1, 0))
        self.assertEqual(auto_limit(6, 6, enough, AUTO_STEP, 0, 8), (6, 0))
        self.assertEqual(auto_limit(6, 6, [('seven_day', 8)], AUTO_STEP, 0, 8), (7, AUTO_STEP))
        self.assertEqual(auto_limit(1, 6, [], 1000, 0), (6, 0))

    def test_line_tolerates_a_point_and_waits_only_until_continuous_catchup(self):
        for length in (5*self.H, 7*24*self.H):
            self.assertIsNone(line_until(50, length, length, length/2))  # One whole point of tolerance.
            self.assertAlmostEqual(line_until(51, length, length, length/2), length*50/98)
            self.assertIsNone(line_until(2, length, length, 60))  # A renewed window's floor and point.
            self.assertAlmostEqual(line_until(3, length, length, 60), 2*length/98)
            self.assertEqual(line_until(98, length, length, length-1), length)
            self.assertIsNone(line_until(98, length, length, length))

    def test_week_long_simulation_reaches_98_without_pausing(self):
        H, W = self.H, 7*86400
        # Six agents wanted from 9:00 to 20:00 at 0.8%/h each, the week renewing on a
        # Saturday at 18:00: the week sustains about 1.6 of them.
        used, limit, changed, cuts, worked = 0.0, 6, -AUTO_STEP, 0, 0
        for now in range(0, W, 60):
            working = 9 <= (18 + now/H) % 24 < 20
            reading = int(used)
            self.assertIsNone(line_until(reading, W, W, now))
            new, changed = auto_limit(limit, 6, [('seven_day', capacity(reading, W, W, now, 0.8/H, 11/24))], now, changed)
            cuts += new < limit
            limit = new
            if working:
                used += limit * 0.8 / 60
                worked += limit
        self.assertGreater(used, 96)
        self.assertLessEqual(used, 98)
        self.assertLess(cuts, 7*6)  # Each morning's burst tapers off a slot at a time.


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
        self.usage(50, 24*3.5, kind='seven_day')  # One point over the line is allowed.
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], 1)
        self.usage(51, 24*3.5, kind='seven_day')
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
        self.usage(20, 24*3, kind='seven_day')
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
        # The pause is over; the restarted week's margin bursts the normal limit at the startup guess.
        self.assertEqual(weekly(2)['limit'], 3)
        self.assertEqual(self.q.windows()[0]['percent'], 2)
        self.assertIn('restart', (Path(self.temp.name)/'auto.log').read_text())
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM usage_log').fetchone()[0], 1)

    def test_a_renewed_week_inherits_the_rate_per_agent_it_ended_with(self):
        H, B = self.H, ACTIVITY_BUCKET
        old = self.now + 4*H
        # Two agents working the last 4 hours of the week: 4 points, 0.5 per agent-hour.
        for t in range(0, 4*H, B):
            self.q.db.execute('INSERT INTO activity VALUES (?)', (self.now + t,))
            self.q.db.execute('INSERT INTO load VALUES (?,2,1)', (self.now + t,))
        for i in range(5):
            self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':90 + i,'resets':old}]})
            self.now += H - (i == 3)
        self.now = old + 60
        self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':0,'resets':old + 7*86400}]})
        weekly = next(w for w in self.q.windows() if w['kind'] == 'seven_day')
        self.assertEqual(weekly['source'], 'inherited')
        self.assertAlmostEqual(weekly['rate'], 0.5, places=1)
        share = weekly['hours'] / 24
        self.assertGreater(weekly['agents'], capacity(0, old + 7*86400, 7*86400, self.now, 1/H, share))

    def test_a_window_after_a_night_off_inherits_the_rate_per_agent_it_ended_with(self):
        H, B = self.H, ACTIVITY_BUCKET
        old = self.now + H
        # One agent working the last hour of a window: 3 points, 3 per agent-hour.
        for t in range(0, H, B):
            self.q.db.execute('INSERT INTO activity VALUES (?)', (self.now + t,))
            self.q.db.execute('INSERT INTO load VALUES (?,1,1)', (self.now + t,))
        for i in range(4):
            self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':10 + i,'resets':old}]})
            self.now += H / 3 - (i == 2)
        self.now = old + 14*H  # The next window starts after the night.
        self.q.operate({'op':'usage','readings':[{'kind':'five_hour','percent':0,'resets':self.now + 5*H}]})
        window = next(w for w in self.q.windows() if w['kind'] == 'five_hour')
        self.assertEqual(window['source'], 'inherited')
        self.assertAlmostEqual(window['rate'], 3, places=0)
        self.assertGreater(window['agents'], 5)

    def test_the_weekly_rate_follows_the_last_two_working_hours(self):
        H = self.H
        resets = self.now + 100*H
        for t in range(-4*H, H, ACTIVITY_BUCKET):
            self.q.db.execute('INSERT INTO activity VALUES (?)', (self.now + t,))
            self.q.db.execute('INSERT INTO load VALUES (?,1,1)', (self.now + t,))
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        start = self.now
        self.now = start - 3*H; weekly(36)
        self.now = start - H; weekly(37)
        self.now = start + H; weekly(40)  # Three points in the last hour: the slow hours fall out.
        w = next(w for w in self.q.windows() if w['kind'] == 'seven_day')
        self.assertEqual(w['source'], 'measured')
        self.assertAlmostEqual(w['rate'], 1.5, places=1)

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

    def test_a_burst_is_measured_per_agent_and_spread_over_learned_hours(self):
        H, B = self.H, ACTIVITY_BUCKET
        # Ten working hours a day; 3%/h with three agents over the last two.
        resets = self.now + 78*H
        start = resets - 7*24*H
        days = [self.now - d*24*H for d in range(5)]  # The last one before the window began.
        for end in days:
            for t in range(int(end - 10*H) // B * B, int(end), B):
                self.q.db.execute('INSERT OR IGNORE INTO activity VALUES (?)', (t,))
                self.q.db.execute('INSERT OR IGNORE INTO load VALUES (?,6,2)', (t,))  # All 3 slots working.
        self.q.db.commit()
        weekly = lambda percent: self.q.operate({'op':'usage','readings':[{'kind':'seven_day','percent':percent,'resets':resets}]})
        self.now -= 2*H; weekly(35)
        self.now += 2*H; weekly(41)
        w = self.q.windows()[0]
        self.assertAlmostEqual(w['rate'], 1.0, places=1)  # Per agent.
        self.assertAlmostEqual(w['hours'], 10, delta=1.5)
        self.assertAlmostEqual(w['agents'], capacity(41, resets, 7*24*H, self.now, 1/H, w['hours']/24), delta=0.1)
        self.assertEqual(self.q.operate({'op':'status'})['limits']['agent'], min(3, int(w['agents'])))

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
        self.assertEqual(weekly['source'], 'default')  # A restarted window waits for an hour of readings.
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
        self.assertAlmostEqual(self.q.windows()[0]['rate'], 6.0)  # 4 points / (2 agents * 1/3 hour).
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

    def test_no_request_admits_a_waiting_ticket_over_the_limit(self):
        for k in 'abc': self.q.operate(self.ticket(k))
        self.assertEqual(self.q.operate(self.ticket('late', rank=0, force=True))['state'], 'queued')
        self.q.auto = False
        self.assertEqual(self.q.operate(self.ticket('late', rank=0, force=True))['state'], 'queued')

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

    def test_an_urgent_subagent_step_goes_first_and_preempts_at_a_tool_boundary(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b'}.get)
        self.q.db.close(); self.q = q
        now = time.time()
        self.q.operate(self.ticket('holder'))
        self.q.operate(self.ticket('main', rank=0))
        # A subagent step waiting less than URGENT keeps its place: a tool boundary within
        # the quantum is not a boundary to yield at.
        sub = self.q.operate(self.ticket('sub', rank=0, step=now - 60))
        self.assertEqual((sub['state'], sub['position']), ('queued', 2))
        self.assertEqual(self.q.operate({'op':'rotate','keys':['holder'],'pid':10,'birth':'b'})['rotated'], [])
        # Two minutes on it is urgent: first in line, and the holder yields at its next tool.
        sub = self.q.operate(self.ticket('sub', rank=0, step=now - URGENT - 1))
        self.assertEqual(sub['position'], 1)
        self.assertEqual(self.q.operate(self.ticket('main', rank=0))['position'], 2)
        self.assertEqual(self.q.operate({'op':'rotate','keys':['holder'],'pid':10,'birth':'b'})['rotated'], ['holder'])
        self.assertEqual((self.state_of('sub'), self.state_of('main'), self.state_of('holder')), ('granted', 'queued', 'queued'))
        self.assertEqual(self.q.db.execute("SELECT step FROM tickets WHERE key='sub'").fetchone()[0], None)
        # Its own grant lasts a quantum like any other, even with another urgent subagent behind.
        self.q.operate(self.ticket('sub2', rank=0, step=now - URGENT - 1))
        self.assertEqual(self.q.operate(self.ticket('sub', rotate=True))['state'], 'granted')
        self.assertEqual(self.q.operate({'op':'rotate','keys':['sub'],'pid':10,'birth':'b'})['rotated'], ['sub'])
        self.assertEqual(self.state_of('sub2'), 'granted')

    def test_a_step_start_honours_the_quantum_even_with_an_urgent_waiter(self):
        q = Queue(Path(self.temp.name)/'r.db', 1, {10:'b'}.get)
        self.q.db.close(); self.q = q
        self.q.operate(self.ticket('holder'))
        self.q.operate(self.ticket('sub', rank=0, step=time.time() - URGENT - 1))
        # Granted in a tool, the holder has not made a call yet: its step runs first.
        self.assertEqual(self.q.operate(self.ticket('holder', rotate=True))['state'], 'granted')
        self.age_grant('holder', 61)
        self.assertEqual(self.q.operate(self.ticket('holder', rotate=True))['state'], 'queued')
        self.assertEqual(self.state_of('sub'), 'granted')
        # A rotated ticket carries no step until its next step acquire reports one.
        self.assertEqual(self.q.db.execute("SELECT step FROM tickets WHERE key='holder'").fetchone()[0], None)

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


class SpendScenarioTests(unittest.TestCase):
    """The cases the automatic limit was reported wrong in, with consumption learned from
    spend as measured: a point of the five-hour window per $6, of the week per $24, and
    $24 per agent-hour, so an agent spends 1% of the week an hour."""
    H = 3600
    K = {'five_hour': 1/6, 'seven_day': 1/24}
    AGENT_USD = 24 / 3600

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 10**7 - 10**7 % 86400  # Midnight.
        self.q = Queue(Path(self.temp.name)/'q.db', 5, {10:'b'}.get, auto=True, clock=lambda: self.now)

    def tearDown(self):
        self.q.db.close(); self.temp.cleanup()

    def work(self, agents, usd):
        """`agents` working in the current bucket, spending `usd`."""
        bucket = int(self.now // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
        self.q.db.execute('INSERT OR IGNORE INTO activity VALUES (?)', (bucket,))
        self.q.db.execute('INSERT OR REPLACE INTO load VALUES (?,?,1)', (bucket, agents))
        self.q.db.commit()
        self.q.operate({'op':'spend','usd':usd})

    def learn(self):
        """Ten working hours a day for a week, the last two of them by two agents a day ago,
        spending, and the points both windows grew by meanwhile."""
        H, B = self.H, ACTIVITY_BUCKET
        now = self.now
        for day in range(2, 8):
            for t in range(now - day*86400 + 9*H, now - day*86400 + 19*H, B):
                self.q.db.execute('INSERT INTO activity VALUES (?)', (t,))
        self.now -= 26*H
        start = self.now
        for _ in range(2*H // B):
            self.work(2, 2*self.AGENT_USD*B)
            self.now += B
        for kind, resets in (('five_hour', start + 3*H), ('seven_day', start + 3*86400)):
            spent = 2*self.AGENT_USD*2*H
            self.q.db.execute('INSERT INTO usage_log VALUES (?,?,0,?),(?,?,?,?)',
                              (kind, resets, start, kind, resets, spent*self.K[kind], self.now - 1))
        self.q.db.commit()
        self.now = now

    def readings(self, five=None, week=None):
        """Report (percent, ceiling) per window: the reset that puts the ceiling there."""
        def resets(kind, ceiling):
            length = 5*self.H if kind == 'five_hour' else 7*86400
            return self.now + length * (1 - ceiling / 98)
        readings = [{'kind':kind, 'percent':p, 'resets':resets(kind, c)}
                    for kind, pc in (('five_hour', five), ('seven_day', week)) if pc for p, c in [pc]]
        return self.q.operate({'op':'usage','readings':readings})['limit']

    def test_the_conversion_is_learned_from_spend(self):
        self.learn()
        self.readings(five=(0, 0.1), week=(1, 10.8))
        w = {w['kind']: w for w in self.q.windows()}
        self.assertEqual(w['seven_day']['source'], 'spend')
        self.assertAlmostEqual(w['seven_day']['usd_per_point'], 24, delta=2)
        self.assertAlmostEqual(w['five_hour']['usd_per_point'], 6, delta=0.5)
        self.assertAlmostEqual(w['seven_day']['rate'], 1.0, delta=0.1)

    def test_after_a_night_a_fresh_window_and_a_banked_week_burst(self):
        # 25/10: back in the morning at 1% of the week where the ceiling is at 10.8%, the
        # five-hour window just started, its previous one 14 hours ago. It was 1 slot.
        self.learn()
        self.assertGreaterEqual(self.readings(five=(0, 0.1), week=(1, 10.8)), 3)

    def test_plenty_left_is_not_one_agent(self):
        # 21/10: 11% of the five-hour window at 57%, 42% of the week at 54%: it was 1 slot.
        self.learn()
        self.assertGreaterEqual(self.readings(five=(11, 56), week=(42, 54)), 3)

    def test_a_small_margin_holds_few_agents(self):
        # 21/10: 53% where the week's ceiling is 58%: the 6 points left (the ceiling's point
        # included) last 3 hours at 2 agents. 23/10: 6 slots with 3 points left: now 1.
        self.learn()
        self.assertEqual(self.readings(five=(5, 50), week=(53, 58)), 2)
        self.assertEqual(self.readings(week=(55, 57)), 1)

    def test_spend_since_the_last_reading_moves_the_estimate_up_to_a_point(self):
        self.learn()
        self.assertEqual(self.readings(week=(53, 58)), 2)
        before = next(w for w in self.q.windows() if w['kind'] == 'seven_day')['agents']
        # Three agents for 20 minutes: $24 spent, a point of the week not yet read, a third
        # of an agent over the margin's three hours, less what the ceiling rose meanwhile.
        for _ in range(4):
            self.now += ACTIVITY_BUCKET
            self.work(3, 3*self.AGENT_USD*ACTIVITY_BUCKET)
        week = next(w for w in self.q.windows() if w['kind'] == 'seven_day')
        self.assertAlmostEqual(week['estimate'], 54, delta=0.1)
        self.assertLess(week['agents'], before - 0.25)
        for _ in range(12):  # Three times more: still a point at most until a reading says more.
            self.now += ACTIVITY_BUCKET
            self.work(3, 3*self.AGENT_USD*ACTIVITY_BUCKET)
        self.assertEqual(next(w for w in self.q.windows() if w['kind'] == 'seven_day')['estimate'], 54)

    def test_a_restarted_window_learns_from_the_other(self):
        # A plan change clears a window's readings: its conversion starts from the other's.
        self.learn()
        self.q.db.execute("DELETE FROM usage_log WHERE kind='five_hour'")
        self.q.db.commit()
        self.readings(five=(0, 0.1), week=(1, 10.8))
        five = next(w for w in self.q.windows() if w['kind'] == 'five_hour')
        self.assertAlmostEqual(five['usd_per_point'], 6, delta=0.5)

    def week(self, night):
        """A fresh week, every window renewing when due, five agents wanted from 9:00 to 20:00
        and `night` through the rest. Asserts the readings never pass the ceiling by more than
        its point and the five-hour window never reaches 98%; returns the week's points spent
        and how many times the limit changed."""
        H, B, W = self.H, ACTIVITY_BUCKET, 7*86400
        week, five, true, changes = self.now + W, None, {'five_hour': 0.0, 'seven_day': 0.0}, 0
        limit = self.q.operate({'op':'status'})['limits']['agent']
        for _ in range(W // B - 1):
            if five is not None and self.now >= five:
                five, true['five_hour'] = None, 0.0
            new = self.q.operate({'op':'status'})['limits']['agent']
            changes += new != limit
            limit = new
            working = min(5 if 9 <= (self.now / H) % 24 < 20 else night, limit)
            if working:
                five = five or self.now + 5*H
                usd = working * self.AGENT_USD * B
                self.work(working, usd)
                for kind in true:
                    true[kind] += usd * self.K[kind]
            readings = [{'kind':'seven_day', 'percent':int(true['seven_day']), 'resets':week}]
            if five:
                readings.append({'kind':'five_hour', 'percent':int(true['five_hour']), 'resets':five})
            self.q.operate({'op':'usage','readings':readings})
            self.assertLessEqual(true['seven_day'], line_cap(week, W, self.now) + 2)
            self.assertLess(true['five_hour'], 98)
            self.now += B
        return true['seven_day'], changes

    def test_a_working_week_holds_the_ceiling_and_uses_the_budget(self):
        spent, changes = self.week(night=0)
        self.assertGreater(spent, 90)
        self.assertLess(changes, 7 * 8)  # Each morning's burst tapers off a slot at a time.

    def test_unattended_nights_hold_the_ceiling_by_pausing(self):
        # One agent around the clock spends 24 points a day of a week that releases 14:
        # past the margin, the ceiling holds by pausing the last agent for part of each hour.
        spent, _ = self.week(night=2)
        self.assertGreater(spent, 90)


if __name__ == '__main__': unittest.main()
