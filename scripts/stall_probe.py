"""Measure Claude Code's subagent stall watchdog with the fixture model in tests/fixtures/stall-probe.

    python3 scripts/stall_probe.py [variants] [seconds] [every]

One `claude -p` process per variant, in parallel, no inference. Each parent spawns one subagent
whose step waits `seconds` (660) in the manner of its variant: `silent` streams nothing, `first`
one chunk at the start, `keepalive` an empty text chunk every `every` (60) seconds, `spaces` a
one-space chunk. A `-wait` suffix turns fork mode off. `real` spends: two empty chunks, then the
real model (haiku) in a two-step task, to check the assembled message is accepted; it needs the
terminal's login. Output goes to $STALL_PROBE_OUT or a fresh temporary directory.

Measured on Claude Code 2.1.310 (2026-10-25): silent, first and silent-wait aborted at 600 s
("stall watchdog fired after 600000ms with no progress"); keepalive, spaces and keepalive-wait
ran the full 660 s, with no assistant content from the empty chunks, until `-p` wound its
background tasks down at its own wait ceiling. `real` with haiku, and with STALL_MODEL=sonnet
STALL_EFFORT=high: step 0 ended in the Bash tool use alone (no empty text block), step 1 answered
DONE, so the API accepted the message assembled after the empty chunks."""
import json, os, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tests/fixtures/stall-probe'
PLUGIN = HERE
OUT = Path(os.environ.get('STALL_PROBE_OUT') or tempfile.mkdtemp(prefix='stall-probe-'))
OUT.mkdir(exist_ok=True)
print('output in', OUT, flush=True)
variants = sys.argv[1].split(',') if len(sys.argv) > 1 else ['silent', 'first', 'keepalive', 'spaces']
seconds = sys.argv[2] if len(sys.argv) > 2 else '660'
every = sys.argv[3] if len(sys.argv) > 3 else '60'

env_base = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL')}
# `real` needs the terminal's login, which the allowlist does not carry: the whole environment,
# less the variables that mark a session nested in Claude Code.
env_full = {k: v for k, v in os.environ.items() if k not in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION')}
MODEL, EFFORT = os.environ.get('STALL_MODEL', 'haiku'), os.environ.get('STALL_EFFORT', 'low')
procs = {}
for v in variants:
    sid = str(uuid.uuid4())
    # A variant suffixed "-wait" makes the parent wait for the subagent (fork mode off).
    real = v == 'real'  # The child calls the real model (haiku): the only variant that spends anything.
    env = {**(env_full if real else env_base), 'STALL_OUT': str(OUT), 'STALL_VARIANT': v, 'STALL_SECONDS': seconds, 'STALL_EVERY': every,
           **({'CLAUDE_CODE_FORK_SUBAGENT': '0'} if v.endswith('-wait') else {}),
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
           **({} if real else {'ANTHROPIC_API_KEY': 'local-test-not-a-key', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:1'})}
    tools = 'Agent,Bash' if real else 'Agent'
    cmd = [shutil.which('claude'), '-p', 'Local stall probe, no inference.',
           '--plugin-dir', str(PLUGIN), '--setting-sources', '', '--strict-mcp-config', '--tools', tools,
           '--allowedTools', tools, '--permission-mode', 'dontAsk',
           '--disable-slash-commands', '--no-session-persistence', '--prompt-suggestions', 'false',
           '--system-prompt', 'Test.', '--model', MODEL, '--effort', EFFORT,
           '--session-id', sid, '--max-budget-usd', '0.25' if real else '0.01', '--output-format', 'stream-json', '--verbose',
           '--debug-file', str(OUT / f'{v}.debug.log')]
    procs[v] = subprocess.Popen(cmd, cwd=str(OUT), env=env, stdin=subprocess.DEVNULL,
                                stdout=open(OUT / f'{v}.stdout.jsonl', 'w'), stderr=open(OUT / f'{v}.stderr', 'w'), text=True)
    print('started', v, procs[v].pid, flush=True)

start = time.time()
for v, p in procs.items():
    p.wait()
    print(f'{v}: exit {p.returncode} after {time.time()-start:.0f}s', flush=True)

print('--- summary')
for v in variants:
    marks = sorted((p.name.split('.', 1)[1], float(p.read_text()) / 1000) for p in OUT.glob(f'{v}.*')
                   if p.suffix not in ('.jsonl', '.stderr', '.log') and not p.name.endswith('-result'))
    for p in sorted(OUT.glob(f'{v}.*-result')):
        print(p.name, p.read_text()[:400])
    t0 = min(t for _, t in marks) if marks else 0
    print(v, 'marks:', ', '.join(f'{m}@{t-t0:.0f}s' for m, t in sorted(marks, key=lambda x: x[1])))
    for line in (OUT / f'{v}.stdout.jsonl').read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = json.dumps(ev)
        if 'tool_result' in text or ev.get('type') == 'result':
            print('  ', text[:600])
