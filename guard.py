#!/usr/bin/env python3
"""Local, durable FIFO semaphore. Standard library only; macOS/Linux.

No prompt text, credentials or model responses are accepted or stored.
Reservations end by explicit release, confirmed owner death, eviction, or an
expired lease: a mod instance renews its tickets while it is working on them.
"""
import argparse
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
TOMBSTONE = 3600  # Released tickets outlive any delayed acquire by far.
LIVE = "state IN ('queued','granted')"


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
            if n <= 0:
                raise RuntimeError('Cannot identify Claude ancestor')
            command = path.value.decode()
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
    def __init__(self, db, agents=DEFAULT_AGENTS, identity=process_identity, lease=LEASE):
        if any(type(n) is not int or n < 1 for n in (agents, lease)):
            raise ValueError('Limits must be positive integers')
        self.identity = identity
        self.limits = {'agent': agents}
        self.lease = lease
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
            # A live owner can still lose track of a ticket (failed release, reloaded mod).
            self.db.execute(f"""UPDATE tickets SET state='released', touched=? WHERE {LIVE}
                AND instance!='' AND coalesce(touched,created)<?""", (now, now - self.lease))
            self.db.execute("DELETE FROM tickets WHERE state='released' AND coalesce(touched,created)<?",
                            (now - TOMBSTONE,))
            self._promote()

    def _promote(self):
        for pool, limit in self.limits.items():
            used = self.db.execute("SELECT count(*) FROM tickets WHERE pool=? AND state='granted'", (pool,)).fetchone()[0]
            free = max(0, limit - used)
            self.db.execute("""UPDATE tickets SET state='granted' WHERE seq IN
                (SELECT seq FROM tickets WHERE pool=? AND state='queued' ORDER BY seq LIMIT ?)""", (pool, free))

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
            return {'limits': self.limits, 'lease': self.lease, 'tickets': [
                {**dict(r), 'idle': round(now - r['seen'])} for r in self.db.execute(
                    f"""SELECT seq,pool,session,turn,pid,state,coalesce(touched,created) AS seen
                    FROM tickets WHERE {LIVE} ORDER BY seq""")]}
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
            insert = 'INSERT OR IGNORE INTO tickets(key,pool,session,turn,pid,birth,state,created,touched,instance) VALUES(?,?,?,?,?,?,?,?,?,?)'
            if op == 'acquire':
                if self.identity(data['pid']) != data['birth']:
                    raise ValueError('Owner is not alive')
                self.db.execute(insert, (*values, 'queued', now, now, data.get('instance', '')))
                self.db.execute(f'UPDATE tickets SET touched=? WHERE key=? AND {LIVE}', (now, data['key']))
            else:
                # Tombstone prevents a delayed acquire from resurrecting canceled work.
                self.db.execute(insert, (*values, 'released', now, now, data.get('instance', '')))
                self.db.execute("UPDATE tickets SET state='released', touched=? WHERE key=?", (now, data['key']))
            self._promote()
        if op != 'acquire':
            return {'state': 'released'}
        row = self.db.execute('SELECT seq,state FROM tickets WHERE key=?', (data['key'],)).fetchone()
        position = self.db.execute("SELECT count(*) FROM tickets WHERE pool=? AND state='queued' AND seq<=?",
                                   (data['pool'], row['seq'])).fetchone()[0]
        return {'state': row['state'], 'ticket': row['seq'], 'position': position}


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
                q = Queue(home/'queue.sqlite3', config['agents'], lease=config.get('lease', LEASE))
                try:
                    q.reap()
                    result = q.operate(data)
                finally:
                    q.db.close()
        if result.get('state') != 'queued' or time.monotonic() >= deadline:
            return result
        time.sleep(min(0.25, max(0, deadline-time.monotonic())))


def initialize(home, agents=DEFAULT_AGENTS, replace=False):
    if type(agents) is not int or agents < 1:
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
            temporary = home/'config.json.tmp'
            with temporary.open('w') as f:
                json.dump({'agents': agents, 'schema': 2}, f)
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(config)
    return local_call(home, {'op':'status'})


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--home', type=Path, default=Path(os.environ.get('CLAUDE_SLOT_HOME', DEFAULT_HOME)))
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('init')
    s.add_argument('--agents', type=int, default=DEFAULT_AGENTS)
    c = sub.add_parser('configure')
    c.add_argument('--agents', type=int, required=True)
    sub.add_parser('status')
    e = sub.add_parser('evict', help='Release tickets by number, as listed by status or /slots')
    e.add_argument('seqs', type=int, nargs='+')
    sub.add_parser('client')
    args = p.parse_args()
    os.umask(0o077)
    if args.command == 'init':
        print(json.dumps(initialize(args.home, args.agents), indent=2))
    elif args.command == 'configure':
        print(json.dumps(initialize(args.home, args.agents, replace=True), indent=2))
    elif args.command == 'status':
        print(json.dumps(local_call(args.home, {'op':'status'}), indent=2))
    elif args.command == 'evict':
        print(json.dumps(local_call(args.home, {'op':'evict', 'seqs':args.seqs}), indent=2))
    else:
        initialize(args.home)
        data = json.loads(sys.stdin.read(MAX_MESSAGE))
        if data.get('op') not in ('status', 'evict'):
            data.update(claude_owner())
        print(json.dumps(local_call(args.home, data)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
