#!/usr/bin/env python3
"""Read-only global occupancy and session queue; never changes reservations."""
from contextlib import closing
import os
import json
from pathlib import Path
import sqlite3
import sys
import time


def slot_bar(label, used, limit):
    if type(limit) is not int or limit < 1:
        raise ValueError('Invalid slot limit')
    filled = min(used, limit)
    return f"{label} {'■' * filled}{'□' * (limit - filled)} {used}/{limit}"


def badge(home, session):
    db = home / 'queue.sqlite3'
    if not db.exists():
        return 'Agents —'
    try:
        limits = json.loads((home / 'config.json').read_text())
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
            used = dict(conn.execute("SELECT pool,count(*) FROM tickets WHERE state='granted' GROUP BY pool"))
            queued = dict(conn.execute("SELECT pool,count(*) FROM tickets WHERE state='queued' GROUP BY pool"))
            try:
                control = dict(conn.execute('SELECT name,value FROM control'))
            except sqlite3.OperationalError:
                control = {}  # Created by the first coordinator call of 0.5.0.
            # Queue order is rank, then sequence; a database not yet migrated has no rank.
            columns = {r[1] for r in conn.execute('PRAGMA table_info(tickets)')}
            place = 'coalesce({0}.pos, {0}.seq)' if 'pos' in columns else '{0}.seq'
            ahead = (f"(q.rank<t.rank OR (q.rank=t.rank AND {place.format('q')}<={place.format('t')}))"
                     if 'rank' in columns else 'q.seq<=t.seq')
            rows = conn.execute(f"""SELECT t.pool,
                (SELECT count(*) FROM tickets q
                 WHERE q.pool=t.pool AND q.state='queued' AND {ahead})
                FROM tickets t WHERE t.session=? AND t.state='queued'
                ORDER BY t.seq""", (session,)).fetchall()
        # One badge per pool, at the earliest position held by this session.
        positions = {}
        for pool, position in rows:
            positions[pool] = min(position, positions.get(pool, position))
        cap = limits['agents']
        limit = cap
        auto = limits.get('auto', True)
        if auto and 'effective' in control:
            limit = max(0, min(max(cap, limits.get('max', cap)), int(control['effective'])))
        if limit == 0:
            until = time.strftime('%H:%M', time.localtime(control.get('until', 0)))
            summary = f'Agents ⏸ paused until {until} (usage limit near)'
        else:
            summary = slot_bar('Agents', used.get('agent', 0), limit)
        if auto and limit:
            summary += f' (auto, normal {cap})' if limit != cap else ' (auto)'
        if queued.get('agent'):
            summary += f" · {queued['agent']} waiting"  # Machine-wide, all sessions.
        waiting = ' · '.join('⌛ #' + str(n) for p,n in positions.items() if p == 'agent')
        return summary + (' · ' + waiting if waiting else '')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return 'Agents —'  # Display failures never alter admission.


if __name__ == '__main__':
    home = Path(os.environ.get('CLAUDE_SLOT_HOME', Path.home()/'.local/state/slot-guard'))
    print(badge(home, sys.argv[1] if len(sys.argv)>1 else ''), end='')
