#!/usr/bin/env python3
"""Read-only global occupancy and session queue; never changes reservations."""
from contextlib import closing
import os
import json
from pathlib import Path
import sqlite3
import sys


def slot_bar(label, used, limit):
    if type(limit) is not int or limit < 1:
        raise ValueError('Invalid slot limit')
    filled = min(used, limit)
    return f"{label} {'■' * filled}{'□' * (limit - filled)} {used}/{limit}"


def badge(home, session):
    db = home / 'queue.sqlite3'
    if not db.exists():
        return 'Slots —'
    try:
        limits = json.loads((home / 'config.json').read_text())
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
            used = dict(conn.execute("SELECT pool,count(*) FROM tickets WHERE state='granted' GROUP BY pool"))
            rows = conn.execute("""SELECT t.pool,
                (SELECT count(*) FROM tickets q
                 WHERE q.pool=t.pool AND q.state='queued' AND q.seq<=t.seq)
                FROM tickets t WHERE t.session=? AND t.state='queued'
                ORDER BY t.seq""", (session,)).fetchall()
        # One badge per pool, at the earliest position held by this session.
        positions = {}
        for pool, position in rows:
            positions.setdefault(pool, position)
        labels = {'job':'turn', 'request':'inference'}
        summary = 'Slots ' + slot_bar('T', used.get('job', 0), limits['jobs'])
        summary += ' ' + slot_bar('I', used.get('request', 0), limits['requests'])
        waiting = ' · '.join('⌛ ' + labels[p] + ' #' + str(n)
                             for p,n in positions.items() if p in labels)
        return summary + (' · ' + waiting if waiting else '')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return 'Slots —'  # Display failures never alter admission.


if __name__ == '__main__':
    home = Path(os.environ.get('CLAUDE_SLOT_HOME', Path.home()/'.local/state/slot-guard'))
    print(badge(home, sys.argv[1] if len(sys.argv)>1 else ''), end='')
