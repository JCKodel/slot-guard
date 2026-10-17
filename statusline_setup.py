#!/usr/bin/env python3
"""Explicit, reversible statusline integration. Never called by plugin startup."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.slot-guard.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)


def setup(config_dir, runtime, remove=False):
    settings_path = config_dir/'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    backup_path = runtime/'original-statusline.json'
    command = shlex.join([sys.executable, str(runtime/'statusline_setup.py'), 'render'])
    if remove:
        if not backup_path.exists():
            raise ValueError('No saved statusline to restore')
        saved = json.loads(backup_path.read_text())
        if settings.get('statusLine', {}).get('command') != command:
            raise ValueError('Statusline changed since installation; refusing to overwrite it')
        if saved['present']:
            settings['statusLine'] = saved['value']
        else:
            settings.pop('statusLine', None)
        write_json(settings_path, settings)
        backup_path.unlink()
        return
    if backup_path.exists():
        raise ValueError('Already installed; remove before installing again')
    previous = settings.get('statusLine')
    if previous is not None and (not isinstance(previous, dict) or previous.get('type') != 'command'):
        raise ValueError('Unsupported existing statusline configuration')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(backup_path, {'present':'statusLine' in settings, 'value':previous})
    for name in ('statusline.py', 'statusline_setup.py'):
        shutil.copy2(Path(__file__).resolve().with_name(name), runtime/name)
    settings['statusLine'] = {'type':'command', 'command':command, 'refreshInterval':5}
    write_json(settings_path, settings)


def render():
    from statusline import badge
    data = sys.stdin.read()
    runtime = Path(__file__).resolve().parent
    saved = json.loads((runtime/'original-statusline.json').read_text())
    previous = saved['value']
    if previous:
        try:
            result = subprocess.run(previous['command'], shell=True, input=data,
                                    text=True, capture_output=True, timeout=3)
            if result.stdout.strip():
                print(result.stdout.rstrip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        session = json.loads(data).get('session_id', '')
    except ValueError:
        session = ''
    home = Path(os.environ.get('CLAUDE_SLOT_HOME', Path.home()/'.local/state/slot-guard'))
    print(badge(home, session))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install','remove','render'])
    parser.add_argument('--config-dir', type=Path, default=Path(os.environ.get('CLAUDE_CONFIG_DIR',Path.home()/'.claude')))
    parser.add_argument('--runtime-dir', type=Path, default=Path.home()/'.local/share/slot-guard/statusline')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'render':
            render()
        else:
            setup(args.config_dir.resolve(), args.runtime_dir.resolve(), args.action == 'remove')
            print('Statusline ' + ('restored.' if args.action == 'remove' else 'installed. Restart Claude Code to refresh.'))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error)+'\n')
