import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_environment import test_environment
from guard import process_identity, initialize, local_call
from statusline import badge
from statusline_setup import setup


class PackagingTests(unittest.TestCase):
    def test_badge_shows_machine_wide_waiting_and_session_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)/'state'
            initialize(home, 1)
            owner = {'pid':os.getpid(), 'birth':process_identity(os.getpid())}
            for key, session in (('a','s1'), ('b','s2'), ('c','s1')):
                local_call(home, {'op':'acquire','key':key,'pool':'agent','session':session,'turn':key,**owner})
            self.assertEqual(badge(home,'s1'), 'Agents ■ 1/1 · 2 waiting · ⌛ #2')
            self.assertEqual(badge(home,'s3'), 'Agents ■ 1/1 · 2 waiting')

    def test_parallel_first_install_preserves_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)/'state'
            script = 'from guard import initialize; from pathlib import Path; import sys; initialize(Path(sys.argv[1]), 2)'
            def start(_):
                return subprocess.run([sys.executable,'-c',script,str(home)],cwd=ROOT,capture_output=True,text=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as workers:
                for result in workers.map(start, range(24)):
                    self.assertEqual(result.returncode, 0, result.stderr)
            initialize(home, 4)
            self.assertEqual(local_call(home, {'op':'status'})['limits'], {'agent':2})
            self.assertEqual(badge(home,''), 'Agents □□ 0/2')
            (home/'config.json').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Missing configuration'):
                initialize(home,4)

    def test_statusline_preserves_existing_output_and_restores_only_own_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = root/'config'; runtime = root/'runtime'; state = root/'state'
            config.mkdir(); initialize(state,2)
            previous = {'type':'command', 'command':"printf 'existing line 1\\nexisting line 2\\n'"}
            settings = config/'settings.json'
            settings.write_text(json.dumps({'statusLine':previous,'other':True}))
            setup(config,runtime)
            data = json.loads(settings.read_text())
            result = subprocess.run(data['statusLine']['command'],shell=True,input='{}',text=True,capture_output=True,
                                    env={**test_environment(),'CLAUDE_SLOT_HOME':str(state)})
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout, 'existing line 1\nexisting line 2\nAgents □□ 0/2\n')
            data['other'] = 'new'; settings.write_text(json.dumps(data))
            setup(config,runtime,remove=True)
            self.assertEqual(json.loads(settings.read_text()), {'statusLine':previous,'other':'new'})

    def test_statusline_refuses_to_clobber_user_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config=root/'config'; runtime=root/'runtime'
            setup(config,runtime)
            settings=config/'settings.json'; settings.write_text('{"statusLine":{"command":"new"}}')
            with self.assertRaisesRegex(ValueError,'changed since'):
                setup(config,runtime,remove=True)
            self.assertEqual(json.loads(settings.read_text())['statusLine']['command'],'new')

    def test_statusline_install_without_previous_is_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); config=root/'config'; runtime=root/'runtime'
            setup(config,runtime)
            with self.assertRaisesRegex(ValueError,'Already installed'):
                setup(config,runtime)
            setup(config,runtime,remove=True)
            self.assertEqual(json.loads((config/'settings.json').read_text()),{})

    def test_display_failure_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing=Path(tmp)/'missing'
            self.assertEqual(badge(missing,''),'Agents —')
            self.assertFalse(missing.exists())
