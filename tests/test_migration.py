import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from guard import Queue, initialize

class MigrationTests(unittest.TestCase):
    def legacy(self,home):
        (home/'config.json').write_text(json.dumps({'jobs':4,'requests':3}))
        q=Queue(home/'queue.sqlite3',4,lambda pid:'birth')
        q.db.execute("INSERT INTO tickets(key,pool,session,turn,pid,birth,state,created) VALUES('old','job','session','turn',99,'birth','granted',0)")
        q.db.commit();q.db.close()

    def test_live_legacy_owner_refuses_migration_without_changing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);self.legacy(home)
            with patch('guard.process_identity',return_value='birth'):
                with self.assertRaisesRegex(RuntimeError,'Close all legacy'):
                    initialize(home)
            self.assertEqual(json.loads((home/'config.json').read_text()),{'jobs':4,'requests':3})

    def test_dead_legacy_owners_migrate_to_smaller_old_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);self.legacy(home)
            with patch('guard.process_identity',return_value=None):
                result=initialize(home)
            self.assertEqual(result['limits'],{'agent':3})
            self.assertEqual(result['tickets'],[])
            self.assertEqual(json.loads((home/'config.json').read_text()),{'agents':3,'auto':True,'schema':2})

    def test_unknown_legacy_owner_refuses_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);self.legacy(home)
            with patch('guard.process_identity',side_effect=RuntimeError('unknown')):
                with self.assertRaisesRegex(RuntimeError,'unknown'): initialize(home)
            self.assertIn('jobs',json.loads((home/'config.json').read_text()))
