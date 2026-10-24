#!/usr/bin/env python3
"""Local, durable FIFO semaphore. Standard library only; macOS/Linux.

No prompt text, credentials or model responses are accepted or stored.
Reservations end by explicit release, confirmed owner death, eviction, or an
expired lease: a mod instance renews its tickets while it is working on them.
"""
import argparse
from bisect import bisect_left
import ctypes
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

DEFAULT_HOME = Path.home() / '.local/state/slot-guard'
MAX_MESSAGE = 16384
DEFAULT_AGENTS = 3  # Lasts a 5-hour window on Claude Max 5x; see README.
LEASE = 300  # Seconds without renewal before a heartbeating ticket is abandoned.
QUANTUM = 60  # Seconds an agent keeps its slot before yielding it to one that waits.
AGING = 300  # Seconds in line after which a ticket gets the priority of work under way.
SPRINT_MAX = 60  # Minutes a sprint may last.
TOMBSTONE = 3600  # Released tickets outlive any delayed acquire by far.
LIVE = "state IN ('queued','granted')"
# Each window's capacity is what is left of 98% spread over the working time left, never
# more than keeps it within a point of a continuous ceiling that releases 98% evenly.
WINDOWS = {'five_hour': 5 * 3600, 'seven_day': 7 * 86400}
AUTO_TARGET = 98.0
AUTO_STEP = 600     # Recovery only; unsafe concurrency is reduced on every operation.
AUTO_IDLE = 3600
AUTO_HEADROOM = 1.1  # A new slot needs 10% spare capacity, so the limit does not flap.
LINE_GUARD = 3600   # Seconds ahead the continuous ceiling must hold at the current capacity.
LINE_RESERVE = 1.0  # Whole-point readings: the tolerance over the ceiling and the reserve under 98%.
# Per-agent startup estimates, also used when a flat reading cannot measure consumption.
DEFAULT_RATE = {'five_hour': 20 / 3600, 'seven_day': 1 / 3600}
# A reading of the same window this many points under the stored one means the account's
# window started over (a plan change); readings from concurrent responses differ by less.
# An idle session can still report its last response from hours ago, so the drop only
# counts once a later reading, RESTART_CONFIRM seconds on, is still that low.
RESTART_DROP = 5.0
RESTART_CONFIRM = 300


# Recent burn rate: readings over this span, at least MIN apart (percent moves in whole points).
# For ACTIVE windows both are seconds of activity, not of the clock.
RATE_SPAN = {'five_hour': 3600, 'seven_day': 2 * 3600}
RATE_MIN = {'five_hour': 600, 'seven_day': 3600}
# Windows measured per active hour (any agent working on this machine) and spread over
# the hours a day worked lately, learned from activity: nights and days off neither
# dilute nor inflate the rate, and a machine working around the clock learns 24 hours.
ACTIVE = {'seven_day'}
ACTIVITY_BUCKET = 300  # Seconds: an agent working at any moment of one marks all of it active.
DUTY_SPAN = 7 * 86400  # Hours a day are learned from the last week,
DUTY_PRIOR = (8 * 3600, 86400)  # starting from 8 hours a day, weighted as one day.
DUTY_FLOOR = 1 / 24


def active_time(buckets, start, end):
    """Seconds of activity from start to end; buckets: sorted start times of active buckets."""
    return ACTIVITY_BUCKET * (bisect_left(buckets, end) - bisect_left(buckets, start))


def concurrency(buckets, loads):
    """Mean agents working over `buckets` (start times), each its mean over the calls that saw
    it (`loads`: bucket -> agents); a bucket without one counts as 1. None without buckets."""
    return sum(loads.get(b, 1.0) for b in buckets) / len(buckets) if buckets else None


def duty(buckets, first, now):
    """Share of the time with an agent working, over the last DUTY_SPAN since `first` (the
    oldest bucket recorded), with DUTY_PRIOR blended in so a fresh install has a guess."""
    observed = max(0.0, min(DUTY_SPAN, now - first)) if first is not None else 0.0
    active = active_time(buckets, now - observed, now + ACTIVITY_BUCKET)
    share = (active + DUTY_PRIOR[0]) / (observed + DUTY_PRIOR[1])
    return min(1.0, max(DUTY_FLOOR, share))


def recent_rate(history, now, kind, buckets=None):
    """Percent per second over the recent span, or None without enough readings.

    history: (percent, at) pairs of one window, oldest first. With `buckets`
    (see active_time), span, minimum and rate count active seconds only.
    """
    span = (lambda start, end: end - start) if buckets is None else (
        lambda start, end: active_time(buckets, start, end))
    recent = [(p, t) for p, t in history if span(t, now) <= RATE_SPAN[kind]]
    if len(recent) < 2 or span(recent[0][1], recent[-1][1]) < RATE_MIN[kind]:
        return None
    (p0, t0), (p1, _) = recent[0], recent[-1]
    return max(0.0, p1 - p0) / span(t0, now)


def rate_load(history, now, kind, buckets, loads):
    """Agents behind the same interval as recent_rate. A clock-time rate includes
    idle time, so its mean load must also include idle time; otherwise resuming work
    would appear cheaper per agent than it really is.
    """
    active = kind in ACTIVE
    recent = [(p, t) for p, t in history
              if (active_time(buckets, t, now) if active else now - t) <= RATE_SPAN[kind]]
    if not recent:
        return None
    start = recent[0][1]
    work = buckets[bisect_left(buckets, start):bisect_left(buckets, now)]
    load = concurrency(work, loads)
    if load is not None and not active and now > start:
        load *= min(1.0, active_time(buckets, start, now) / (now - start))
    return load


def line_cap(resets, length, now):
    """The explicit continuous ceiling, in percentage points (never rounded for control)."""
    return AUTO_TARGET * min(1.0, max(0.0, (now - (resets - length)) / length))


def capacity(percent, resets, length, now, per_agent, share=1.0):
    """Agents a window sustains, burning `per_agent` percent per agent-second of work.

    The lesser of two: what is left of 98% (less a point for whole-point readings)
    spread over the working time left, `share` of the clock; and what stays within
    a point of the continuous ceiling for the next LINE_GUARD seconds. None outside
    the window.
    """
    if not resets - length <= now < resets:
        return None
    left = resets - now
    spread = max(0.0, AUTO_TARGET - LINE_RESERVE - percent) / (share * left)
    guard = min(LINE_GUARD, left)
    within = max(0.0, line_cap(resets, length, now) + LINE_RESERVE - percent) / guard + AUTO_TARGET / length
    return min(spread, within) / per_agent


def line_until(percent, resets, length, now):
    """Emergency backstop only above the continuous ceiling, or at the final target.
    Readings are whole points, so one point over the moving line (LINE_RESERVE) is
    allowed: a renewed window's first 1% is not an overshoot. An overshoot resumes
    when the line is back within that point.
    """
    if not resets - length <= now < resets:
        return None
    if percent >= AUTO_TARGET:
        return resets
    if percent <= line_cap(resets, length, now) + LINE_RESERVE + 1e-9:
        return None
    return min(resets, resets - length + (percent - LINE_RESERVE) / AUTO_TARGET * length)


def auto_limit(current, cap, signals, now, changed, ceiling=None):
    """Cut directly to sustainable concurrency, even within the recovery cooldown.
    Signals are (kind, agents the window sustains). Recovery adds one slot with 10%
    headroom. A sub-unit capacity keeps one agent; only an actual ceiling breach pauses it.
    """
    ceiling = max(cap, ceiling or cap)
    if not signals:
        return cap, changed
    supported = min(agents for _, agents in signals)
    safe = ceiling if math.isinf(supported) else max(1, min(ceiling, math.floor(supported + 1e-9)))
    current = max(1, min(current, ceiling))
    if safe < current:
        return safe, now
    if now - changed >= AUTO_STEP and current < safe and supported >= (current + 1) * AUTO_HEADROOM:
        return current + 1, now
    return current, changed


class BSDInfo(ctypes.Structure):
    # macOS SDK sys/proc_info.h, PROC_PIDTBSDINFO = 3.
    _fields_ = [(k, ctypes.c_uint32) for k in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(k, ctypes.c_uint32) for k in ('nfiles','pgid','pjobc','tdev','tpgid')]
    _fields_ += [('nice',ctypes.c_int32), ('seconds',ctypes.c_uint64), ('micros',ctypes.c_uint64)]


def mac_process(pid):
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    info = BSDInfo()
    ctypes.set_errno(0)
    n = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if n == 0 and ctypes.get_errno() == errno.ESRCH:
        return None
    if n != ctypes.sizeof(info):
        raise RuntimeError('Cannot inspect process; retaining reservations')
    return info


def process_identity(pid):
    """Return start identity; None only for confirmed absence/zombie, raise on errors."""
    if sys.platform == 'darwin':
        info = mac_process(pid)
        if info is None or info.status == 5:  # SZOMB
            return None
        return str(info.seconds) + ':' + str(info.micros)
    if sys.platform.startswith('linux'):
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except FileNotFoundError:
            return None
        fields = stat[stat.rindex(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat='],
                       capture_output=True, text=True, timeout=3,
                       env={'LC_ALL': 'C'})
    if p.returncode == 1 and not p.stdout.strip() and not p.stderr.strip():
        return None
    if p.returncode != 0:
        raise RuntimeError('Cannot inspect process; retaining reservations')
    fields = p.stdout.split()
    if len(fields) < 6:
        raise RuntimeError('Invalid process identity; retaining reservations')
    if fields[-1].startswith('Z'):
        return None
    return ' '.join(fields[:-1])


def ps_command(pid):
    """The executable path of a process as ps reports it."""
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'comm='],
                       capture_output=True, text=True, timeout=3)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError('Cannot identify Claude ancestor')
    return p.stdout.strip()


def claude_owner():
    pid = os.getppid()
    for _ in range(12):
        if sys.platform == 'darwin':
            info = mac_process(pid)
            if info is None:
                break
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            path = ctypes.create_string_buffer(4096)
            n = lib.proc_pidpath(pid, path, len(path))
            # Claude Code's updater deletes old versions under running sessions, and the
            # path of a deleted executable is gone from libproc; ps still reports it.
            command = path.value.decode() if n > 0 else ps_command(pid)
            parent = info.ppid
        else:
            p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'ppid=', '-o', 'comm='],
                               capture_output=True, text=True, timeout=3)
            parts = p.stdout.strip().split(None, 1)
            if len(parts) != 2:
                break
            parent, command = parts
        name = Path(command).name
        # Native Claude executable can be named by its version in ~/.local/share/claude/versions.
        if name == 'claude' or '/claude/versions/' in command:
            birth = process_identity(pid)
            if birth:
                return {'pid': pid, 'birth': birth}
        pid = int(parent)
        if pid <= 1:
            break
    raise RuntimeError('Claude parent process not found; refusing unowned admission')


class Queue:
    def __init__(self, db, agents=DEFAULT_AGENTS, identity=process_identity, lease=LEASE,
                 auto=False, clock=time.time, ceiling=None, quantum=QUANTUM):
        if any(type(n) is not int or n < 1 for n in (agents, lease, quantum)):
            raise ValueError('Limits must be positive integers')
        self.identity = identity
        self.cap = agents
        if ceiling is not None and (type(ceiling) is not int or ceiling < agents):
            raise ValueError('The maximum must be an integer at least the agent limit')
        self.ceiling = ceiling or agents
        self.limits = {'agent': agents}
        self.auto = bool(auto)
        self.clock = clock
        self.log = Path(db).parent / 'auto.log'
        # Every reading that changes a window, never cleared: the rate log is pruned and
        # forgotten, this one is what a week can be looked back on with.
        self.readings = Path(db).parent / 'readings.log'
        self.lease = lease
        self.quantum = quantum
        self.lock = threading.RLock()  # One connection, shared by threads in tests and tools.
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS tickets (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
            pool TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
            pid INTEGER NOT NULL, birth TEXT NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL, touched REAL, instance TEXT NOT NULL DEFAULT '')''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(tickets)')}
        # Older clients may share this database: their rows keep instance '' and never expire.
        if 'touched' not in columns:
            self.db.execute('ALTER TABLE tickets ADD COLUMN touched REAL')
        if 'instance' not in columns:
            self.db.execute("ALTER TABLE tickets ADD COLUMN instance TEXT NOT NULL DEFAULT ''")
        self.db.execute('''CREATE TABLE IF NOT EXISTS usage (kind TEXT PRIMARY KEY,
            percent REAL NOT NULL, resets REAL NOT NULL, seen REAL NOT NULL)''')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (name TEXT PRIMARY KEY, value REAL NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS usage_log (kind TEXT NOT NULL, resets REAL NOT NULL,
            percent REAL NOT NULL, at REAL NOT NULL)''')
        # Start times of ACTIVITY_BUCKETs with an agent working; kept through resets and plan
        # changes, which restart the usage windows but not the user's working hours.
        self.db.execute('CREATE TABLE IF NOT EXISTS activity (bucket INTEGER PRIMARY KEY)')
        # Working agents seen per bucket (their sum over `samples` calls), apart from `activity`
        # so older coordinators sharing the database keep inserting there unchanged.
        self.db.execute('''CREATE TABLE IF NOT EXISTS load (bucket INTEGER PRIMARY KEY,
            agents REAL NOT NULL, samples INTEGER NOT NULL)''')
        # Sessions ('*': every session) whose agents start at once, over the limit, until `until`.
        self.db.execute('CREATE TABLE IF NOT EXISTS sprints (session TEXT PRIMARY KEY, until REAL NOT NULL)')
        if 'rank' not in columns:
            # 0: work already under way (subagents, resumptions) goes before new prompts (1).
            self.db.execute('ALTER TABLE tickets ADD COLUMN rank INTEGER NOT NULL DEFAULT 1')
        if 'since' not in columns:
            # When the slot was granted, and the queue position after a rotation (else seq).
            self.db.execute('ALTER TABLE tickets ADD COLUMN since REAL')
            self.db.execute('ALTER TABLE tickets ADD COLUMN pos INTEGER')
        if 'waited' not in {r['name'] for r in self.db.execute('PRAGMA table_info(tickets)')}:
            self.db.execute('ALTER TABLE tickets ADD COLUMN waited REAL')  # Back in line since (rotation).
        self.db.commit()

    def reap(self):
        with self.lock, self.db:
            owners = self.db.execute('SELECT DISTINCT pid,birth FROM tickets').fetchall()
            for owner in owners:
                try:
                    alive = self.identity(owner['pid'])
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    continue  # Unknown is not dead.
                if alive != owner['birth']:
                    self.db.execute('DELETE FROM tickets WHERE pid=? AND birth=?', tuple(owner))
            now = time.time()
            paused = self.db.execute("SELECT value FROM control WHERE name='effective'").fetchone()
            if not (paused and paused[0] == 0):
                # A live owner can still lose track of a ticket (failed release, reloaded mod).
                # During a pause every ticket waits on purpose: none expires.
                self.db.execute(f"""UPDATE tickets SET state='released', touched=? WHERE {LIVE}
                    AND instance!='' AND coalesce(touched,created)<?""", (now, now - self.lease))
            self.db.execute("DELETE FROM tickets WHERE state='released' AND coalesce(touched,created)<?",
                            (now - TOMBSTONE,))
            self.db.execute('DELETE FROM sprints WHERE until<=?', (self.clock(),))
            self._promote()

    def windows(self):
        now = self.clock()
        buckets = [b for (b,) in self.db.execute('SELECT bucket FROM activity WHERE bucket>=? ORDER BY bucket',
                                                 (now - DUTY_SPAN - ACTIVITY_BUCKET,))]
        first = self.db.execute('SELECT min(bucket) FROM activity').fetchone()[0]
        share = duty(buckets, first, now)
        loads = dict(self.db.execute('SELECT bucket,agents/samples FROM load WHERE bucket>=?',
                                     (now - DUTY_SPAN - ACTIVITY_BUCKET,)))
        result = []
        for r in self.db.execute('SELECT * FROM usage').fetchall():
            if r['kind'] not in WINDOWS:
                continue
            history = self.db.execute('SELECT percent,at FROM usage_log WHERE kind=? AND abs(resets-?)<=60 ORDER BY at',
                                      (r['kind'], r['resets'])).fetchall()
            active = r['kind'] in ACTIVE
            history = [tuple(h) for h in history]
            rate = recent_rate(history, now, r['kind'], buckets if active else None)
            # Per agent-second of work: the rate over the agents behind exactly its interval.
            load = rate_load(history, now, r['kind'], buckets, loads) if rate else None
            per_agent, source = (rate / (load or 1.0), 'measured') if rate else (None, None)
            if per_agent is None:
                # A flat or new window cannot measure consumption: each agent burns what it did
                # at the end of the window before, or the startup guess.
                per_agent, source = self._inherited(r['kind'], r['resets'], buckets, loads), 'inherited'
                if per_agent is None:
                    per_agent, source = DEFAULT_RATE[r['kind']], 'default'
            length = WINDOWS[r['kind']]
            window = {'kind': r['kind'], 'percent': r['percent'], 'resets': r['resets'],
                      'elapsed': round(100 * (1 - (r['resets'] - now) / length), 1),
                      # Percent per agent-hour of work, and where it comes from.
                      'rate': round(per_agent * 3600, 2), 'source': source,
                      'agents': capacity(r['percent'], r['resets'], length, now, per_agent,
                                         share if active else 1.0),
                      'ceiling': line_cap(r['resets'], length, now),
                      'line_until': line_until(r['percent'], r['resets'], length, now)}
            window['margin'] = window['ceiling'] - r['percent']
            if active:
                window['hours'] = round(24 * share, 1)  # Learned hours a day the budget is spread over.
            result.append(window)
        return result

    def _inherited(self, kind, resets, buckets, loads):
        """Percent per agent-second the previous window of `kind` ended with, if it ended
        within one window before this one started; None otherwise. History lasts 2 days."""
        length = WINDOWS[kind]
        previous = self.db.execute('SELECT max(resets) FROM usage_log WHERE kind=? AND resets<=?',
                                   (kind, resets - length + 60)).fetchone()[0]
        if previous is None or previous < resets - 2 * length:
            return None
        history = [tuple(h) for h in self.db.execute(
            'SELECT percent,at FROM usage_log WHERE kind=? AND abs(resets-?)<=60 ORDER BY at', (kind, previous))]
        end = history[-1][1]
        rate = recent_rate(history, end, kind, buckets if kind in ACTIVE else None)
        load = rate_load(history, end, kind, buckets, loads) if rate else None
        return rate / load if rate and load else None

    def note(self, entry, log=None):
        try:
            with (log or self.log).open('a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError:
            pass  # The log is a convenience; admission must not depend on it.

    def _control(self):
        """Set the effective limit; with auto off it is the configured one."""
        value = dict(self.db.execute('SELECT name,value FROM control'))
        # Read before this call marks its own bucket, or the first call after a pause hides it.
        last = self.db.execute('SELECT max(bucket) FROM activity').fetchone()[0]
        working = self.db.execute("SELECT count(*) FROM tickets WHERE state='granted'").fetchone()[0]
        if working:
            # Heartbeats renew granted tickets every 30 s, so working time marks every bucket.
            bucket = int(self.clock() // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
            self.db.execute('INSERT OR IGNORE INTO activity VALUES (?)', (bucket,))
            # Weighted by calls, not time: more agents heartbeat more often, so a bucket's load
            # leans to its busier minutes, which makes the scaling a little less strict.
            self.db.execute('''INSERT INTO load VALUES (?,?,1) ON CONFLICT(bucket)
                DO UPDATE SET agents=agents+excluded.agents, samples=samples+1''', (bucket, working))
        limit, until, behind = self.cap, 0, []
        if self.auto:
            now = self.clock()
            windows = [w for w in self.windows() if w['resets'] > now]
            signals = [(w['kind'], w['agents']) for w in windows if w['agents'] is not None]
            previous = int(value.get('limit', self.cap))
            spent = [w['resets'] for w in windows if w['percent'] >= AUTO_TARGET]
            if spent:
                # Pause everything until the window renews instead of running into the hard limit.
                limit, until = 0, max(spent)
                changed = now if previous != 0 else value.get('changed', 0)
            else:
                if previous == 0:
                    previous = self.cap  # A renewed window starts again from the maximum.
                limit, changed = auto_limit(previous, self.cap, signals, now, value.get('changed', 0), self.ceiling)
                if last is not None and now - last >= ACTIVITY_BUCKET + AUTO_IDLE:
                    # After a pause the readings already say where the limit belongs: the steps
                    # it would have taken meanwhile are taken now, in either direction.
                    for _ in range(self.ceiling):
                        step, _ = auto_limit(limit, self.cap, signals, now, now - AUTO_STEP, self.ceiling)
                        if step == limit:
                            break
                        limit, changed = step, now
            if limit != previous:
                self.note({'at': now, 'from': previous, 'to': limit, 'cap': self.cap, 'max': self.ceiling,
                           'windows': {w['kind']: {k: w[k] for k in ('percent', 'agents', 'rate', 'source', 'margin')}
                                       for w in windows}})
            self.db.execute("INSERT OR REPLACE INTO control VALUES ('limit',?),('changed',?)", (limit, changed))
            # Emergency ceiling breach: retain the reduced limit for recovery.
            behind = [w['line_until'] for w in windows if w.get('line_until') and w['percent'] < AUTO_TARGET]
            if behind:
                if value.get('effective') != 0:
                    self.note({'at': now, 'line': max(behind), 'limit': limit})
                limit, until = 0, max([until, *behind])
        self.db.execute("INSERT OR REPLACE INTO control VALUES ('effective',?),('until',?),('line',?)",
                        (limit, until, int(until in behind)))
        self.limits['agent'] = limit
        self.until = until
        self.line = until in behind

    def sprints(self):
        """Live sprints as {session: until}."""
        return dict(self.db.execute('SELECT session,until FROM sprints WHERE until>?', (self.clock(),)))

    def _sprinting(self, session):
        sprints = self.sprints()
        return '*' in sprints or session in sprints

    def _promote(self):
        self._control()
        sprints = self.sprints()
        for pool, limit in self.limits.items():
            if limit == 0:
                # Working agents wait at their next step or tool, keeping their FIFO places.
                self.db.execute("UPDATE tickets SET state='queued' WHERE pool=? AND state='granted'", (pool,))
            # Aging: no prompt or rotated agent waits behind an endless stream of new subagents.
            self.db.execute("""UPDATE tickets SET rank=0 WHERE pool=? AND state='queued' AND rank=1
                AND coalesce(waited, created) <= ?""", (pool, time.time() - AGING))
            if limit > 0 and sprints:
                # Urgent work starts at once, over the limit, and first: it takes any free slot,
                # so nothing new starts elsewhere until it drains. A pause stops a sprint too.
                self.db.execute(f"""UPDATE tickets SET state='granted', since=? WHERE pool=? AND state='queued'
                    AND (? OR session IN ({','.join('?' * len(sprints))}))""",
                    (time.time(), pool, '*' in sprints, *sprints))
            used = self.db.execute("SELECT count(*) FROM tickets WHERE pool=? AND state='granted'", (pool,)).fetchone()[0]
            free = max(0, limit - used)
            self.db.execute("""UPDATE tickets SET state='granted', since=? WHERE seq IN
                (SELECT seq FROM tickets WHERE pool=? AND state='queued' ORDER BY rank, coalesce(pos, seq) LIMIT ?)""",
                (time.time(), pool, free))

    def _rotate(self, key, now):
        """Cooperative multitasking: a slot held for a quantum goes to the next in line.

        The agent keeps its ticket, queued behind every agent waiting now (rank 1: an
        agent's first grant of a turn still goes first). Excess grants yield even without waiters.
        The caller must only rotate an agent between model calls.
        """
        row = self.db.execute('SELECT seq,pool,session,since FROM tickets WHERE key=? AND state=?', (key, 'granted')).fetchone()
        if not row or self._sprinting(row['session']):
            return False
        self._control()
        used = self.db.execute("SELECT count(*) FROM tickets WHERE pool=? AND state='granted'", (row['pool'],)).fetchone()[0]
        over = used > self.limits[row['pool']]
        if not over and (row['since'] is None or now - row['since'] < self.quantum):
            return False
        if not over and not self.db.execute("SELECT 1 FROM tickets WHERE pool=? AND state='queued' AND key!=?",
                               (row['pool'], key)).fetchone():
            return False
        last = self.db.execute('SELECT max(coalesce(pos, seq)) FROM tickets').fetchone()[0]
        self.db.execute("UPDATE tickets SET state='queued', rank=1, pos=?, since=NULL, waited=? WHERE key=?",
                        (last + 1, now, key))
        return True

    @staticmethod
    def validate_owner(data):
        if not isinstance(data.get('birth'), str) or not 0 < len(data['birth']) <= 512:
            raise ValueError('Invalid birth')
        if type(data.get('pid')) is not int or data['pid'] <= 1:
            raise ValueError('Invalid owner pid')

    @classmethod
    def validate(cls, data):
        if data.get('pool') != 'agent':
            raise ValueError('Invalid pool')
        for key in ('key', 'session', 'turn'):
            if not isinstance(data.get(key), str) or not 0 < len(data[key]) <= 512:
                raise ValueError('Invalid ' + key)
        if not isinstance(data.get('instance', ''), str) or len(data.get('instance', '')) > 64:
            raise ValueError('Invalid instance')
        cls.validate_owner(data)

    def operate(self, data):
        with self.lock:
            return self._operate(data)

    def _operate(self, data):
        op = data.get('op')
        if op == 'status':
            now = time.time()
            with self.db:
                self._control()
            return {'limits': self.limits, 'lease': self.lease,
                    'auto': {'enabled': self.auto, 'cap': self.cap, 'max': self.ceiling, 'target': AUTO_TARGET, 'until': self.until, 'line': self.line,
                             'windows': self.windows()} if self.auto else {'enabled': False},
                    'sprints': self.sprints(),
                    'tickets': [
                {**dict(r), 'idle': round(now - r['seen'])} for r in self.db.execute(
                    f"""SELECT seq,pool,session,turn,pid,state,coalesce(touched,created) AS seen
                    FROM tickets WHERE {LIVE} ORDER BY rank, coalesce(pos, seq)""")]}
        if op == 'evict':
            seqs = data.get('seqs')
            if not isinstance(seqs, list) or not 0 < len(seqs) <= 64 or any(type(n) is not int for n in seqs):
                raise ValueError('Invalid ticket numbers')
            with self.db:
                marks = ','.join('?' * len(seqs))
                n = self.db.execute(f"UPDATE tickets SET state='released', touched=? WHERE {LIVE} AND seq IN ({marks})",
                                    (time.time(), *seqs)).rowcount
                self._promote()
            return {'evicted': n}
        if op == 'reset':
            # The automatic limit starts again from the normal limit, now; the readings and
            # their rate stay. With `usage` (forget) the stored readings go too: after a plan
            # change the account's windows start over, but a reading of the same window never
            # goes down here. readings.log keeps them either way.
            with self.db:
                self.db.execute('DELETE FROM control')
                self.db.execute("INSERT INTO control VALUES ('changed',?)", (self.clock(),))
                if data.get('usage') is True:
                    self.db.execute('DELETE FROM usage')
                    self.db.execute('DELETE FROM usage_log')
                self._promote()
            return {'limit': self.limits['agent']}
        if op == 'sprint':
            # Urgent work: the agents of one session ('*': all) start at once for `minutes`;
            # 0 ends every sprint.
            session, minutes = data.get('session'), data.get('minutes')
            if not isinstance(session, str) or not 0 < len(session) <= 512:
                raise ValueError('Invalid session')
            if type(minutes) is not int or not 0 <= minutes <= SPRINT_MAX:
                raise ValueError(f'Sprint minutes must be 0 to {SPRINT_MAX}')
            with self.db:
                if minutes:
                    self.db.execute('INSERT OR REPLACE INTO sprints VALUES (?,?)', (session, self.clock() + 60 * minutes))
                else:
                    self.db.execute('DELETE FROM sprints')
                self.note({'at': self.clock(), 'sprint': session, 'minutes': minutes})
                self._promote()
            return {'sprints': self.sprints()}
        if op == 'usage':
            readings = data.get('readings')
            if not isinstance(readings, list) or len(readings) > 8:
                raise ValueError('Invalid readings')
            with self.db:
                for r in readings:
                    if not isinstance(r, dict) or r.get('kind') not in WINDOWS or any(
                            type(r.get(k)) not in (int, float) or not 0 <= r[k] < 1e10 for k in ('percent', 'resets')):
                        raise ValueError('Invalid reading')
                    row = self.db.execute('SELECT percent,resets FROM usage WHERE kind=?', (r['kind'],)).fetchone()
                    same = row and abs(row['resets'] - r['resets']) <= 60
                    dropped = same and r['percent'] < row['percent'] - RESTART_DROP
                    drop = f"drop:{r['kind']}"
                    first = self.db.execute('SELECT value FROM control WHERE name=?', (drop,)).fetchone()
                    if dropped and first is None:
                        # Possibly a stale response: keep the stored reading until another confirms it.
                        self.db.execute('INSERT INTO control VALUES (?,?)', (drop, self.clock()))
                        continue
                    if dropped and self.clock() - first[0] < RESTART_CONFIRM:
                        continue
                    self.db.execute('DELETE FROM control WHERE name=?', (drop,))
                    if dropped:
                        # The account's window started over (a plan change) without a new reset time.
                        # Concurrent responses never disagree this much. Its history is stale too,
                        # and the limit starts again from the normal one, as after a renewal.
                        percent = r['percent']
                        self.db.execute('DELETE FROM usage_log WHERE kind=?', (r['kind'],))
                        self.db.execute("DELETE FROM control WHERE name IN ('limit','changed')")
                        self.note({'at': self.clock(), 'restart': r['kind'], 'from': row['percent'], 'to': percent})
                    elif same:
                        # Sessions report the same window from different responses: usage only grows.
                        percent = max(row['percent'], r['percent'])
                    elif row and row['resets'] > r['resets']:
                        continue  # A reading of a window that already ended.
                    else:
                        percent = r['percent']
                    self.db.execute('INSERT OR REPLACE INTO usage VALUES (?,?,?,?)',
                                    (r['kind'], percent, r['resets'], time.time()))
                    last = self.db.execute('SELECT percent FROM usage_log WHERE kind=? ORDER BY at DESC LIMIT 1',
                                           (r['kind'],)).fetchone()
                    if last is None or last[0] != percent:
                        self.db.execute('INSERT INTO usage_log VALUES (?,?,?,?)',
                                        (r['kind'], r['resets'], percent, self.clock()))
                        self.note({'at': self.clock(), 'kind': r['kind'], 'resets': r['resets'],
                                   'percent': percent}, self.readings)
                self.db.execute('DELETE FROM usage_log WHERE at<?', (self.clock() - 2 * 86400,))
                self.db.execute('DELETE FROM activity WHERE bucket<?', (self.clock() - DUTY_SPAN - 86400,))
                self.db.execute('DELETE FROM load WHERE bucket<?', (self.clock() - DUTY_SPAN - 86400,))
                self._promote()
            return {'limit': self.limits['agent']}
        if op == 'rotate':
            # Agents between model calls (running tools) offer their slot to those waiting.
            self.validate_owner(data)
            keys = data.get('keys')
            if not isinstance(keys, list) or len(keys) > 256 or any(
                    not isinstance(k, str) or not 0 < len(k) <= 512 for k in keys):
                raise ValueError('Invalid keys')
            now = time.time()
            with self.db:
                rotated = [k for k in keys if self.db.execute('SELECT 1 FROM tickets WHERE key=? AND pid=? AND birth=?',
                           (k, data['pid'], data['birth'])).fetchone() and self._rotate(k, now)]
                self._promote()
            return {'rotated': rotated}
        if op == 'touch':
            self.validate_owner(data)
            keys = data.get('keys')
            if not isinstance(keys, list) or len(keys) > 256 or any(
                    not isinstance(k, str) or not 0 < len(k) <= 512 for k in keys):
                raise ValueError('Invalid keys')
            with self.db:
                states = {}
                for key in keys:
                    self.db.execute(f'UPDATE tickets SET touched=? WHERE key=? AND pid=? AND birth=? AND {LIVE}',
                                    (time.time(), key, data['pid'], data['birth']))
                    row = self.db.execute('SELECT state FROM tickets WHERE key=? AND pid=? AND birth=?',
                                          (key, data['pid'], data['birth'])).fetchone()
                    states[key] = row['state'] if row else 'released'
            return {'states': states}
        if op == 'adopt':
            # A reloaded mod abandons every ticket its previous load held in this process.
            self.validate_owner(data)
            if not isinstance(data.get('instance'), str) or not 0 < len(data['instance']) <= 64:
                raise ValueError('Invalid instance')
            with self.db:
                n = self.db.execute(f"""UPDATE tickets SET state='released', touched=? WHERE {LIVE}
                    AND pid=? AND birth=? AND instance!='' AND instance!=?""",
                    (time.time(), data['pid'], data['birth'], data['instance'])).rowcount
                self._promote()
            return {'released': n}
        if op not in ('acquire', 'release'):
            raise ValueError('Unknown operation')
        self.validate(data)
        values = tuple(data[k] for k in ('key', 'pool', 'session', 'turn', 'pid', 'birth'))
        now = time.time()
        with self.db:
            row = self.db.execute('SELECT * FROM tickets WHERE key=?', (data['key'],)).fetchone()
            if row and tuple(row[k] for k in ('key','pool','session','turn','pid','birth')) != values:
                raise ValueError('Ticket belongs to a different operation')
            insert = 'INSERT OR IGNORE INTO tickets(key,pool,session,turn,pid,birth,state,created,touched,instance,rank) VALUES(?,?,?,?,?,?,?,?,?,?,?)'
            rank = 0 if data.get('rank') == 0 else 1
            if op == 'acquire':
                if self.identity(data['pid']) != data['birth']:
                    raise ValueError('Owner is not alive')
                self.db.execute(insert, (*values, 'queued', now, now, data.get('instance', ''), rank))
                self.db.execute(f'UPDATE tickets SET touched=? WHERE key=? AND {LIVE}', (now, data['key']))
                if data.get('fresh') is True:
                    # A subagent taking its parent's slot starts a quantum of its own.
                    self.db.execute("UPDATE tickets SET since=? WHERE key=? AND state='granted'", (now, data['key']))
                if data.get('rotate') is True:
                    self._rotate(data['key'], now)
                if data.get('force') is True and not self.auto:
                    self._control()
                    if self.limits['agent'] > 0 and self.db.execute(
                            "UPDATE tickets SET state='granted', since=? WHERE key=? AND state='queued'", (now, data['key'])).rowcount:
                        # Claude Code abandons a subagent's step after 600 s without progress:
                        # admit it over the limit rather than fail it. Nothing new starts until it drains.
                        self.note({'at': now, 'overdraft': data['key'], 'limit': self.limits['agent']})
            else:
                # Tombstone prevents a delayed acquire from resurrecting canceled work.
                self.db.execute(insert, (*values, 'released', now, now, data.get('instance', ''), rank))
                self.db.execute("UPDATE tickets SET state='released', touched=? WHERE key=?", (now, data['key']))
            self._promote()
        if op != 'acquire':
            return {'state': 'released'}
        row = self.db.execute('SELECT seq,state,rank,coalesce(pos, seq) AS place FROM tickets WHERE key=?',
                              (data['key'],)).fetchone()
        position = self.db.execute("""SELECT count(*) FROM tickets WHERE pool=? AND state='queued'
            AND (rank<? OR (rank=? AND coalesce(pos, seq)<=?))""",
            (data['pool'], row['rank'], row['rank'], row['place'])).fetchone()[0]
        result = {'state': row['state'], 'ticket': row['seq'], 'position': position}
        if self.limits['agent'] == 0:
            result['paused_until'] = self.until
            if self.line:
                result['paused_line'] = True
        return result


def local_call(home, data):
    """One transaction at a time across processes, with a stable ticket while waiting."""
    deadline = time.monotonic() + min(20, max(0, float(data.get('wait', 0))))
    while True:
        result = None
        with (home/'coordinator.lock').open('a') as lock:
            lock_deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        if data.get('op') != 'acquire':
                            raise RuntimeError('Coordinator lock timeout')
                        # Contention is not refusal: an acquirer keeps waiting its turn.
                        result = {'state': 'queued', 'position': None}
                        break
                    time.sleep(0.025)
            if result is None:
                config = json.loads((home/'config.json').read_text())
                q = Queue(home/'queue.sqlite3', config['agents'], lease=config.get('lease', LEASE),
                          auto=config.get('auto', True), ceiling=config.get('max'),
                          quantum=config.get('quantum', QUANTUM))
                try:
                    q.reap()
                    result = q.operate(data)
                finally:
                    q.db.close()
        if result.get('state') != 'queued' or time.monotonic() >= deadline:
            return result
        # A pause lasts until a window renews: poll it gently.
        time.sleep(min(2 if result.get('paused_until') else 0.25, max(0, deadline-time.monotonic())))


def initialize(home, agents=DEFAULT_AGENTS, replace=False, auto=None, ceiling=None, quantum=None, report=True):
    """Create or (replace=True) update the configuration; None keeps a configured value."""
    if agents is not None and (type(agents) is not int or agents < 1):
        raise ValueError('Agent limit must be a positive integer')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    with (home/'initialize.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = home/'config.json'
        old = json.loads(config.read_text()) if config.exists() else None
        database = home/'queue.sqlite3'
        if old is None and database.exists():
            raise RuntimeError('Missing configuration for existing queue; admission refused')
        migrating = old is not None and 'agents' not in old
        if migrating:
            # Older processes use the old admission protocol. Never mix both pools.
            with (home/'coordinator.lock').open('a') as coordinator:
                fcntl.flock(coordinator, fcntl.LOCK_EX)
                if database.exists():
                    db = sqlite3.connect(database)
                    try:
                        for pid, birth in db.execute('SELECT DISTINCT pid,birth FROM tickets'):
                            if process_identity(pid) == birth:
                                raise RuntimeError('Close all legacy Claude sessions before migrating Slot Guard')
                        db.execute('DELETE FROM tickets')
                        db.commit()
                    finally:
                        db.close()
            if not replace:
                agents = min(old['jobs'], old['requests'])
        if replace or old is None or migrating:
            if agents is None:
                agents = old['agents'] if old and 'agents' in old else DEFAULT_AGENTS
            if auto is None:
                auto = (old or {}).get('auto', True)
            if ceiling is None:
                ceiling = (old or {}).get('max')
            if ceiling is not None and ceiling < agents:
                ceiling = agents  # The maximum is never below the normal limit.
            new = {'agents': agents, 'auto': bool(auto), 'schema': 2}
            if ceiling is not None and ceiling > agents:
                new['max'] = ceiling
            if quantum is None:
                quantum = (old or {}).get('quantum')
            if quantum is not None:
                new['quantum'] = quantum
            temporary = home/'config.json.tmp'
            with temporary.open('w') as f:
                json.dump(new, f)
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(config)
    # The client only needs the configuration: a status call here would fail under lock
    # contention before an acquire could keep waiting for its turn.
    return local_call(home, {'op':'status'}) if report else None


def configure(home, data):
    """Change the configuration from the mod or the command line, and apply it now.

    data: any of agents, max, quantum (positive integers) and auto (boolean).
    The automatic limit restarts from the new normal limit instead of climbing
    to it one slot per AUTO_STEP. Returns the configuration and the status.
    """
    fields = {k: data.get(k) for k in ('agents', 'max', 'quantum', 'auto')}
    if all(v is None for v in fields.values()):
        raise ValueError('configure needs agents, max, quantum and/or auto')
    for k in ('agents', 'max', 'quantum'):
        if fields[k] is not None and (type(fields[k]) is not int or fields[k] < 1):
            raise ValueError(f'{k} must be a positive integer')
    if fields['auto'] is not None and type(fields['auto']) is not bool:
        raise ValueError('auto must be on or off')
    initialize(home, fields['agents'], replace=True, auto=fields['auto'], ceiling=fields['max'],
               quantum=fields['quantum'], report=False)
    local_call(home, {'op': 'reset'})
    return {'config': json.loads((home/'config.json').read_text()), **local_call(home, {'op': 'status'})}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--home', type=Path, default=Path(os.environ.get('CLAUDE_SLOT_HOME', DEFAULT_HOME)))
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('init')
    s.add_argument('--agents', type=int, default=DEFAULT_AGENTS)
    c = sub.add_parser('configure')
    c.add_argument('--agents', type=int, help='Maximum working agents; the automatic limit never exceeds it')
    c.add_argument('--auto', choices=('on', 'off'), help='Lower the limit while usage runs ahead of its window')
    c.add_argument('--max', type=int, help='With --auto on, go up to this many agents while every window has plenty left')
    c.add_argument('--quantum', type=int, help='Seconds an agent keeps its slot while others wait (default 60)')
    sub.add_parser('status')
    e = sub.add_parser('evict', help='Release tickets by number, as listed by status or /slots')
    e.add_argument('seqs', type=int, nargs='+')
    sub.add_parser('reset', help='Restart the automatic limit from the normal limit; the usage readings stay')
    sub.add_parser('forget', help='Forget the usage readings too (after a plan change, or to end a 98%% pause now)')
    r = sub.add_parser('sprint', help="Urgent work: every session's agents start at once, over the limit, for MINUTES (0 ends every sprint)")
    r.add_argument('minutes', type=int)
    sub.add_parser('client')
    args = p.parse_args()
    os.umask(0o077)
    if args.command == 'init':
        print(json.dumps(initialize(args.home, args.agents), indent=2))
    elif args.command == 'configure':
        auto = None if args.auto is None else args.auto == 'on'
        try:
            result = configure(args.home, {'agents': args.agents, 'auto': auto, 'max': args.max, 'quantum': args.quantum})
        except ValueError as error:
            p.error(str(error))
        print(json.dumps(result, indent=2))
    elif args.command == 'status':
        print(json.dumps(local_call(args.home, {'op':'status'}), indent=2))
    elif args.command == 'evict':
        print(json.dumps(local_call(args.home, {'op':'evict', 'seqs':args.seqs}), indent=2))
    elif args.command in ('reset', 'forget'):
        initialize(args.home, report=False)
        print(json.dumps(local_call(args.home, {'op':'reset', 'usage':args.command == 'forget'}), indent=2))
    elif args.command == 'sprint':
        print(json.dumps(local_call(args.home, {'op':'sprint', 'session':'*', 'minutes':args.minutes}), indent=2))
    else:
        initialize(args.home, report=False)
        data = json.loads(sys.stdin.read(MAX_MESSAGE))
        if data.get('op') == 'configure':
            print(json.dumps(configure(args.home, data)))
            return
        if data.get('op') not in ('status', 'evict', 'reset', 'sprint'):
            data.update(claude_owner())
        print(json.dumps(local_call(args.home, data)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
