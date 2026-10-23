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
# Automatic limit: keep each usage window on pace to end at AUTO_TARGET percent,
# and pause all agents once a window reaches it, until that window renews.
WINDOWS = {'five_hour': 5 * 3600, 'seven_day': 7 * 86400}
AUTO_TARGET = 95.0
AUTO_STEP = 600     # Seconds between changes of one slot.
AUTO_WARMUP = 900   # Seconds: a window's first minutes are too noisy to act on.
AUTO_IDLE = 3600    # Seconds without an agent working (a night): settle at once, not a step at a time.
# Per window: below `low` the window votes a slot down, not under `floor` nor under the
# normal limit times its pace (a window 15% too fast needs 15% fewer agents, not one);
# above `high` it lets the limit rise. The weekly window, judged over days, acts later.
# `line` is the linear quota of a LINEAR window, judged by line_pace.
AUTO_BOOST = (1.1, 1.25)  # Above the normal limit: rise only past 1.25 everywhere, return under 1.1.
AUTO_POLICY = {'five_hour': {'low': 0.9, 'high': 1.1, 'floor': 1},
               'seven_day': {'low': 0.75, 'high': 1.1, 'floor': 1},
               'line': {'low': 1.0, 'high': 1.1, 'floor': 1}}
# Windows also held to a linear quota by the day: each day, starting at the window's reset
# hour, allows a further 1/7 of the weekly one (14, 28, 42, 57, 71, 85, 100%), so a fast
# start cannot borrow from the rest of the week. A reading at today's quota pauses every
# agent until the next day's quota is above it; near it, the limit is cut.
LINEAR = {'seven_day'}
LINE_DAY = 86400
LINE_HORIZON = 4 * 3600  # Seconds the margin to today's quota must last at the recent rate, at least.
# A reading of the same window this many points under the stored one means the account's
# window started over (a plan change); readings from concurrent responses differ by less.
# An idle session can still report its last response from hours ago, so the drop only
# counts once a later reading, RESTART_CONFIRM seconds on, is still that low.
RESTART_DROP = 5.0
RESTART_CONFIRM = 300


# Recent burn rate: readings over this span, at least MIN apart (percent moves in whole points).
# For ACTIVE windows both are seconds of activity, not of the clock.
RATE_SPAN = {'five_hour': 3600, 'seven_day': 4 * 3600}
RATE_MIN = {'five_hour': 600, 'seven_day': 2 * 3600}
# Windows measured per active hour (any agent working on this machine) and projected over
# the hours a day worked lately: nights and days off neither dilute nor inflate the rate.
ACTIVE = {'seven_day'}
ACTIVITY_BUCKET = 300  # Seconds: an agent working at any moment of one marks all of it active.
DUTY_SPAN = 7 * 86400  # Hours a day are learned from the last week,
DUTY_PRIOR = (8 * 3600, 86400)  # starting from 8 hours a day, weighted as one day.
DUTY_FLOOR = 1 / 24


def active_time(buckets, start, end):
    """Seconds of activity from start to end; buckets: sorted start times of active buckets."""
    return ACTIVITY_BUCKET * (bisect_left(buckets, end) - bisect_left(buckets, start))


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


def pace(percent, resets, length, now, target=AUTO_TARGET, rate=None, duty=1.0):
    """How sustainable usage is: 1 on pace to end at target, below 1 too fast.

    With a recent burn rate: the budget left over what that rate spends in the
    time left, of which only the share `duty` is active (a rate per active
    second). It follows the current load, so the limit recovers once usage
    slows. Without one, the share of budget left over the share of time left.
    None when the reading cannot be judged: expired, malformed, or too early.
    """
    seconds_left = resets - now
    left = seconds_left / length
    if not 0 < left <= 1 - AUTO_WARMUP / length:
        return None
    budget = max(0.0, target - percent)
    if rate is not None:
        result = budget / (rate * duty * seconds_left) if rate > 0 else float('inf')
    else:
        result = budget / target / left
    return result


def line_day(resets, length, now):
    """(day, days, start) of a LINEAR window: the day in progress, 1-based, of `days`
    LINE_DAY long from `start`. None when the window is not in progress."""
    start, days = resets - length, max(1, round(length / LINE_DAY))
    if not start <= now < resets:
        return None
    return min(days, int((now - start) // LINE_DAY) + 1), days, start


def line_cap(day, days):
    """The linear quota of a day, in whole points: 14, 28, 42, 57, 71, 85, 100 for a week."""
    return day * 100 // days


def line_pace(percent, resets, length, now, rate, duty=None):
    """How long the margin to today's quota lasts: 1 when the recent rate (per working
    second) reaches it as today's working hours end, the share `duty` of the time left
    in the day; below 1 sooner. Work spreads over the day instead of running at full
    speed into a pause. Within LINE_HORIZON, or the day's end if sooner, the rate counts
    as nonstop (a night of work included). Without a rate (fresh readings), the margin
    over the part of today's points still to come, by the share of the day left, and only
    while it is short: a fresh day neither cuts nor holds a rise. None when the window
    is not in progress, or without a rate and with margin enough."""
    today = line_day(resets, length, now)
    if today is None:
        return None
    day, days, start = today
    left = start + day * LINE_DAY - now
    margin = max(0.0, line_cap(day, days) - percent)
    if rate is None:
        share = margin / ((line_cap(day, days) - line_cap(day - 1, days)) * left / LINE_DAY)
        return share if share < 1 else None
    if rate <= 0:
        return float('inf')
    horizon = min(left, max(LINE_HORIZON, (duty or 0) * left))
    return margin / (rate * horizon)


def line_until(percent, resets, length, now):
    """While a reading has reached today's quota, the start of the first day whose quota
    is above it (the window's reset at most); None while it is under."""
    today = line_day(resets, length, now)
    if today is None:
        return None
    day, days, start = today
    if percent < line_cap(day, days):
        return None
    later = [d for d in range(day + 1, days + 1) if line_cap(d, days) > percent]
    return start + (later[0] - 1) * LINE_DAY if later else resets


def auto_limit(current, cap, signals, now, changed, ceiling=None):
    """One slot at most every AUTO_STEP; the normal limit `cap` without data.

    signals: (kind, pace) of each judgeable window. Any window below its `low`
    lowers the limit, down to the smallest floor among those windows: its
    policy floor, or `cap` times its pace rounded, if more;
    up to `cap` it rises when every window is above its `high`. Between `cap`
    and `ceiling` every window must be above AUTO_BOOST[1], and the limit
    returns toward `cap` once any falls under AUTO_BOOST[0].
    """
    ceiling = max(cap, ceiling or cap)
    if not signals:
        return cap, changed
    current = max(1, min(current, ceiling))
    if now - changed < AUTO_STEP:
        return current, changed
    down = [max(AUTO_POLICY[k]['floor'], int(cap * p + 0.5))
            for k, p in signals if p < AUTO_POLICY[k]['low']]
    if down:
        if current > min(min(down), cap):
            return current - 1, now
        return current, changed
    paces = [p for _, p in signals]
    if current > cap and min(paces) < AUTO_BOOST[0]:
        return current - 1, now
    if current < cap and all(p > AUTO_POLICY[k]['high'] for k, p in signals):
        return current + 1, now
    if cap <= current < ceiling and min(paces) > AUTO_BOOST[1]:
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
        result = []
        for r in self.db.execute('SELECT * FROM usage').fetchall():
            if r['kind'] not in WINDOWS:
                continue
            history = self.db.execute('SELECT percent,at FROM usage_log WHERE kind=? AND abs(resets-?)<=60 ORDER BY at',
                                      (r['kind'], r['resets'])).fetchall()
            active = r['kind'] in ACTIVE
            rate = recent_rate([tuple(h) for h in history], now, r['kind'], buckets if active else None)
            average = None
            start = r['resets'] - WINDOWS[r['kind']]
            if active and first is not None and first <= start:
                worked = active_time(buckets, start, now + ACTIVITY_BUCKET)
                average = r['percent'] / worked if worked >= RATE_MIN[r['kind']] else None
            # A burst of a few hours is not the week: the recent rate counts half, the window's
            # own (all its working hours) the other half.
            judged = rate if rate is None or average is None else (rate + average) / 2
            window = {'kind': r['kind'], 'percent': r['percent'], 'resets': r['resets'],
                      'elapsed': round(100 * (1 - (r['resets'] - now) / WINDOWS[r['kind']]), 1),
                      # Percent per hour; per active hour for ACTIVE windows.
                      'rate': None if rate is None else round(rate * 3600, 2),
                      'pace': pace(r['percent'], r['resets'], WINDOWS[r['kind']], now, rate=judged,
                                   duty=share if active else 1.0)}
            if r['kind'] in LINEAR:
                # The recent rate as is, over the working hours left today.
                window['line'] = line_pace(r['percent'], r['resets'], WINDOWS[r['kind']], now, rate,
                                           share if active else None)
                window['line_until'] = line_until(r['percent'], r['resets'], WINDOWS[r['kind']], now)
            if active:
                window['hours'] = round(24 * share, 1)  # Active hours a day the rate is projected over.
                window['average'] = None if average is None else round(average * 3600, 2)
            result.append(window)
        return result

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
        if self.db.execute("SELECT 1 FROM tickets WHERE state='granted' LIMIT 1").fetchone():
            # Heartbeats renew granted tickets every 30 s, so working time marks every bucket.
            bucket = int(self.clock() // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
            self.db.execute('INSERT OR IGNORE INTO activity VALUES (?)', (bucket,))
        limit, until, behind = self.cap, 0, []
        if self.auto:
            now = self.clock()
            windows = [w for w in self.windows() if w['resets'] > now]
            signals = [(w['kind'], w['pace']) for w in windows if w['pace'] is not None]
            signals += [('line', w['line']) for w in windows if w.get('line') is not None]
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
                           'paces': {k: round(p, 2) for k, p in signals}})
            self.db.execute("INSERT OR REPLACE INTO control VALUES ('limit',?),('changed',?)", (limit, changed))
            # At today's linear quota every agent waits for a day whose quota is above the
            # reading. The stepped limit is kept, so work resumes from it, not from the maximum.
            behind = [w['line_until'] for w in windows if w.get('line_until')]
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
        agent's first grant of a turn still goes first). Nothing happens when no one waits.
        The caller must only rotate an agent between model calls.
        """
        row = self.db.execute('SELECT seq,pool,session,since FROM tickets WHERE key=? AND state=?', (key, 'granted')).fetchone()
        if not row or row['since'] is None or now - row['since'] < self.quantum or self._sprinting(row['session']):
            return False
        if not self.db.execute("SELECT 1 FROM tickets WHERE pool=? AND state='queued' AND key!=?",
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
                if data.get('force') is True:
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
    sub.add_parser('forget', help='Forget the usage readings too (after a plan change, or to end a 95%% pause now)')
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
