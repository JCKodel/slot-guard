# Slot Guard

Machine-wide FIFO concurrency slots for **Claude Code**. Limit the total number of working main agents and subagents across terminals, including time spent executing tools.

```text
Agents ■■■■ 4/4 · ⌛ #1
```

**Beta · macOS · native Claude Code 2.1.302+ · Python 3.10+ · AGPL-3.0-only**

Four agents can work while a fifth waits locally. Main agents and subagents share one pool. An agent holds its slot across model steps and ordinary tools, until its turn finishes. A parent waiting only for children yields its slot and reacquires before doing more work. No queue server, daemon, npm dependencies, telemetry, or model calls to manage the queue.

## Install

Make sure `python3` is on the PATH used by Claude Code. From your terminal:

```sh
claude plugin install slot-guard --marketplace JCKodel/slot-guard
```

Restart all Claude Code sessions that should participate. The first use creates private state in `~/.local/state/slot-guard` with **4 working-agent slots**. Run `/slots` in Claude Code to inspect occupancy and the queue. This is a local command, not a prompt sent to a model.

Do not enable two copies of Slot Guard in one session. If you used the earlier private prototype, disable it before enabling this version. The public version intentionally uses a different state directory; the two versions do not coordinate with each other by default.

Update or remove:

```sh
claude plugin update slot-guard@slot-guard
claude plugin uninstall slot-guard@slot-guard
```

If you installed the optional statusline below, restore it before removing the checkout. Uninstalling the plugin does not remove local state or your statusline.

## Configure limits

For configuration and optional statusline installation, keep a checkout:

```sh
git clone https://github.com/JCKodel/slot-guard.git
cd slot-guard
python3 guard.py configure --agents 4
python3 guard.py status
```

`--agents` is the single global limit. Four sessions without children, two working parents plus two working children, or one working parent plus three children all use four slots. Idle terminals and parents suspended waiting only for children do not count as working.

A slot spans inference and ordinary tools, not merely an API response. `Agent`/legacy `Task` delegation and blocking `TaskOutput` waits let a parent yield. If another ordinary tool from that parent is still running in parallel, the parent keeps its slot until that work finishes. A suspended parent joins FIFO again before its next inference or ordinary tool. This prevents four parents from holding every slot while waiting for children that cannot start.

### Upgrading from 0.2.x or the private prototype

Finish work and close **all** participating Claude sessions before updating. Do not mix versions. On first use, configuration migrates to one agent limit using the smaller of the old two limits. Migration refuses to proceed while legacy ticket owners are alive or cannot be inspected. Do not delete the database to bypass this check. Existing terminal sessions must be restarted to load the new hooks. If using the optional copied statusline helpers, remove and reinstall them from the updated checkout.

Lowering a limit does not interrupt existing work; the queue drains to the new limit. For predictable changes, configure while your sessions are idle. Limits must be positive integers. All participating terminals must run as the same OS user and use the same state path on a local filesystem. Set `CLAUDE_SLOT_HOME` to an absolute path in your shell to override that path; export it consistently in every terminal and the statusline environment.

## Optional statusline

From the checkout:

```sh
python3 statusline_setup.py install
```

This explicitly updates your Claude `settings.json`, saves the previous statusline configuration, and installs stable helper copies in `~/.local/share/slot-guard/statusline`. It runs your existing statusline command with the same JSON input, preserves its output, and adds the slots on a separate line:

```text
[your existing model / project / usage bars]
Agents ■■■□ 3/4 · ⌛ #2
```

`■` is occupied; `□` is available. The single count includes all working main agents and subagents. Queue positions are specific to the current session. The display is read-only and refreshes every five seconds. It does not split or reformat your existing bars. Slow existing commands have a three-second timeout.

Restore the previous statusline:

```sh
python3 statusline_setup.py remove
```

The remover preserves unrelated settings and refuses to overwrite a statusline you changed after installation. `CLAUDE_CONFIG_DIR` is respected; explicit `--config-dir` and `--runtime-dir` options are available. Avoid editing settings concurrently with installation/removal. To update the copied helpers, remove and install again from the updated checkout.

## Guarantees and limits

- FIFO ordering applies to agent admissions and resumptions, according to the order tickets enter the database. OS scheduling may change when admitted work visibly starts.
- SQLite transactions plus an OS file lock serialize admission across processes.
- Reservations do not expire. Explicit release or a confirmed dead owner releases them. PID reuse is checked against process birth identity.
- If the coordinator fails, the mod aborts the turn without calling the next model handler. Unknown process state retains reservations.
- Cancellation uses tombstones so a delayed acquire cannot resurrect canceled work. Waiting polls locally and consumes no model tokens.
- This controls concurrency, **not total tokens, spend, or the Claude usage window**. It does not prevent the same amount of work from eventually consuming the same tokens.
- Only sessions that load and execute the mod participate. Disabled plugins, other clients, other OS users, and other machines are outside its control. This is not a security boundary or a billing guarantee.
- Detached background tool processes, external APIs, background traffic, and inference paths outside `turn.step` are not covered. Foreground ordinary tool execution retains its agent slot; a subprocess that outlives its tool call is not a separate admitted agent. Waiting implemented through arbitrary shell polling cannot be recognized as a child wait and may cause a stall. Tools called outside an admitted turn are denied; plugins that invoke tools independently may be incompatible. Interactions with other mods require testing; another mod may change middleware behavior.
- macOS with the native Claude executable is the initial supported platform. Linux has core CI coverage; native Linux integration is experimental. Windows and npm/Node-hosted Claude are not supported in this beta.
- Closed/dead sessions are reaped on the next coordinator operation. The read-only badge can temporarily show stale occupancy. Do not delete the database while sessions are active: that can admit excess work.

## Permissions, data flow, and directory review

### Programs executed by the mod

The mod makes one kind of process call, in `hooks/register.js`:

```js
$.process.run(['python3', 'guard.py', 'client'], {
  cwd: $.plugin.root, stdin: JSON.stringify(data), timeoutMs: 25000,
})
```

Its complete command is the fixed argument vector `python3 guard.py client`, with the working directory set to Claude's trusted `$.plugin.root`. The script path is relative to the installed plugin, never the user's project. All command arguments are literal strings; only the installation working directory varies. Arguments are passed as an array without a shell, inline Python, downloaded code, or a network utility. The bundled `guard.py` coordinates local SQLite reservations. Python is necessary for SQLite, cross-process file locks, and process-liveness inspection using the standard library.

On macOS, process inspection uses `/usr/lib/libproc.dylib` without another subprocess. On Linux, ancestry inspection invokes `/bin/ps -p <pid> -o ppid= -o comm=` and process identity reads `/proc/<pid>/stat` and `/proc/sys/kernel/random/boot_id`. The fallback for other Unix platforms invokes `/bin/ps -p <pid> -o lstart= -o stat=`; those platforms are not supported by this beta. PIDs are derived from the OS, not user text. No command reads process environment variables or full command-line arguments.

### Data, destinations, and credentials

The mod sends JSON **only to the local Python child's stdin**: operation (`status`, `acquire`, or `release`), the single `agent` pool, session ID, turn ID, ticket key (including agent ID and admission generation), and polling wait duration. The child adds the owner PID and birth identity. JSON results return on stdout; errors return on stderr and may appear in Claude's local status UI.

`guard.py` reads/writes `config.json`, `queue.sqlite3` (including SQLite journal files), and local lock/temporary files under `~/.local/state/slot-guard`, or the explicitly configured `CLAUDE_SLOT_HOME`. Stored records contain coordination identifiers, ownership, state, sequence, and creation timestamps. Released records persist until their owner exits and a subsequent coordinator operation reaps them. State remains after uninstall; remove it only after all participating sessions have stopped.

The coordinator and badge reader make **no network requests** and have no remote data destination. They do not read project files, transcript files, Claude's credential store, Keychain, API keys, or authentication tokens. Child processes inherit an OS environment normally; the coordinator does not look up or use credential variables. The only environment setting it explicitly reads is `CLAUDE_SLOT_HOME` (home-directory resolution also uses standard OS/Python behavior).

The `turn.step` middleware receives a turn event and passes it to `next(e)` only after admission. It forwards stream chunks without inspecting or persisting prompts, answers, or tool results. This allows Claude's normal model request to proceed; it does not add a second request or send conversation content to the coordinator. `session.start` registers `/slots`; `command.run` handles only that local command; `turn.complete` releases the corresponding agent reservation after outstanding ordinary work settles. `tool.call` gates ordinary tools, tracks parallel work, and yields a parent reservation during child waits. All work shares the same pool. No additional model tools or agents are registered.

### Optional statusline and development-only commands

`statusline_setup.py` runs **only when explicitly installed by the user**, never at plugin startup. Installation/removal reads and rewrites Claude's `settings.json` while preserving unrelated settings, and saves only the previous `statusLine` value. Settings can contain sensitive values; this helper neither extracts credentials nor transmits settings. It uses `CLAUDE_CONFIG_DIR` for the settings location.

When rendering, its wrapper executes the user's pre-existing statusline command through the OS shell, with the same statusline JSON on stdin and a three-second timeout. That command is user-controlled and can have its own filesystem/network behavior; Slot Guard cannot guarantee its privacy. The wrapper adds the local badge using only `session_id`. Users who do not want to execute a previous command can use `statusline.py` directly in their own statusline. There is no bundled network statusline command.

Files under `tests/`, `scripts/`, and `.github/` are development/release tooling, not plugin startup hooks. Test subprocesses receive only an allowlist of runtime environment variables (`PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL`, `SYSTEMROOT`) plus explicit test settings, never a copy of the entire environment. Tests set `ANTHROPIC_API_KEY` to the literal non-secret `local-test-not-a-key` and `ANTHROPIC_BASE_URL` to an unreachable loopback address. These are test fixtures, not credentials read from the installer. The CI workflow downloads and runs Anthropic's official installer solely on an ephemeral GitHub runner to install the pinned Claude test executable. The marketplace smoke test fetches the explicitly selected plugin source. Neither operation is invoked by the installed mod. The release ZIP excludes all test, build, and CI tooling.

The directory's process/conversation/credential heuristics may require human review. This section documents the actual behavior; a successful local validator is not an assertion of directory-policy approval.

## Development

Python and Node tests have no third-party dependencies. Claude runtime/integration tests require the native Claude executable. Test fixtures return fixed local responses; integration tests point API requests at an unreachable loopback endpoint and use a dummy API key.

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/mod.test.mjs
claude plugin test .
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json
python3 scripts/smoke_install.py
python3 scripts/package.py
```

See [architecture](docs/architecture.md), [contributing](CONTRIBUTING.md), [release procedure](docs/releasing.md), and [security](SECURITY.md). This is an independent community project, not affiliated with Anthropic.

## License

Copyright © 2026 J.C.Ködel. Licensed under **GNU Affero General Public License v3.0 only**, SPDX `AGPL-3.0-only`. See [LICENSE](LICENSE).
