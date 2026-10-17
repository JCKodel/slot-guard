import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import Queue, process_identity


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'
        self.live = {10:'birth'}
        self.q = Queue(self.path, 4, 4, self.live.get)

    def tearDown(self):
        self.q.db.close()
        self.temp.cleanup()

    def data(self, key, pool='job', **overrides):
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
        self.q = Queue(self.path, 4, 4, self.live.get)
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

    def test_child_inference_not_blocked_by_full_job_pool(self):
        for i in range(4): self.q.operate(self.data(str(i)))
        self.assertEqual(self.q.operate(self.data('child','request'))['state'],'granted')

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
        for i in range(4): self.q.operate(self.data(str(i)))
        self.q.operate(self.data('waiter'))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.q.operate, self.data('waiter',wait=5))
            self.q.operate(self.data('0',op='release'))
            self.assertEqual(future.result(timeout=2)['state'],'granted')


if __name__ == '__main__': unittest.main()
