#!/usr/bin/env python3
"""Install from a marketplace into a temporary Claude configuration and run locally."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from test_environment import test_environment

root = Path(__file__).resolve().parents[1]
source = sys.argv[1] if len(sys.argv)>1 else str(root)
with tempfile.TemporaryDirectory(prefix='slot-install-') as tmp:
    env = {**test_environment(), 'CLAUDE_CONFIG_DIR':tmp+'/config', 'CLAUDE_SLOT_HOME':tmp+'/state',
           'ANTHROPIC_API_KEY':'local-test-not-a-key', 'ANTHROPIC_BASE_URL':'http://127.0.0.1:1',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1', 'DISABLE_AUTOUPDATER':'1',
           'SLOT_TEST_DELAY':'0', 'SLOT_TEST_CHILDREN':'0'}
    subprocess.run(['claude','plugin','install','slot-guard','--marketplace',source],
                   env=env, check=True, timeout=120)
    subprocess.run(['claude','plugin','install','slot-test-model','--marketplace',str(root/'tests/fixtures/fake-model')],
                   env=env, check=True, timeout=30)
    result = subprocess.run(['claude','-p','Local smoke test.',
        '--setting-sources','user',
        '--strict-mcp-config','--tools','','--disable-slash-commands','--no-session-persistence',
        '--prompt-suggestions','false','--system-prompt','Test.','--model','haiku',
        '--effort','low','--output-format','json'], cwd=tmp, env=env, capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    response=json.loads(result.stdout)
    assert response['result']=='LOCAL_TEST_OK', response
    assert response['total_cost_usd']==0, response
    assert response['duration_api_ms']==0, response
    assert (Path(tmp)/'state/queue.sqlite3').exists(), 'Guard was not invoked'
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(Path(tmp)/'state/queue.sqlite3')) as db:
        assert db.execute("SELECT count(*) FROM tickets WHERE pool='agent'").fetchone()[0] > 0
    print('Marketplace install and isolated startup passed; API cost $0.')
