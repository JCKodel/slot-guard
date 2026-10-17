# Slot Guard

Machine-wide FIFO concurrency slots for **Claude Code**. Keep multiple terminals and subagents from starting inference at the same time beyond your configured limit.

```text
Slots T ■■■■ 4/4 I ■■□□ 2/4 · ⌛ turn #1
```

**Beta · macOS · native Claude Code 2.1.302+ · Python 3.10+ · AGPL-3.0-only**

Four terminals can work while a fifth waits locally. Requests from subagents share the inference pool; they do not compete with their parents for turn slots. No queue server, daemon, npm dependencies, telemetry, or model calls to manage the queue.

## Install

Make sure `python3` is on the PATH used by Claude Code. From your terminal:

```sh
claude plugin install slot-guard --marketplace JCKodel/slot-guard
```

Restart all Claude Code sessions that should participate. The first use creates private state in `~/.local/state/slot-guard` with **4 turn slots and 4 inference slots**. Run `/slots` in Claude Code to inspect occupancy and the queue. This is a local command, not a prompt sent to a model.

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
python3 guard.py configure --jobs 4 --requests 4
python3 guard.py status
```

`--jobs` limits active **main turns**, including time spent executing their tools. `--requests` limits concurrent **model steps**, including subagents. A model step holds its slot for the complete response stream. A main turn acquires its turn slot before its inference slot. Subagents acquire only inference slots.

Lowering a limit does not interrupt existing work; the queue drains to the new limit. For predictable changes, configure while your sessions are idle. Limits must be positive integers. All participating terminals must run as the same OS user and use the same state path on a local filesystem. Set `CLAUDE_SLOT_HOME` in your shell to override that path; export it consistently in every terminal and the statusline environment.

## Optional statusline

From the checkout:

```sh
python3 statusline_setup.py install
```

This explicitly updates your Claude `settings.json`, saves the previous statusline configuration, and installs stable helper copies in `~/.local/share/slot-guard/statusline`. It runs your existing statusline command with the same JSON input, preserves its output, and adds the slots on a separate line:

```text
[your existing model / project / usage bars]
Slots T ■■■□ 3/4 I ■■□□ 2/4 · ⌛ inference #2
```

`■` is occupied; `□` is available. `T` counts main turns and `I` counts inference. Queue positions are specific to the current session. The display is read-only and refreshes every five seconds. It does not split or reformat your existing bars. Slow existing commands have a three-second timeout.

Restore the previous statusline:

```sh
python3 statusline_setup.py remove
```

The remover preserves unrelated settings and refuses to overwrite a statusline you changed after installation. `CLAUDE_CONFIG_DIR` is respected; explicit `--config-dir` and `--runtime-dir` options are available. Avoid editing settings concurrently with installation/removal. To update the copied helpers, remove and install again from the updated checkout.

## Guarantees and limits

- FIFO ordering applies **within each pool**, according to the order tickets enter the database. OS scheduling may change when admitted work visibly starts.
- SQLite transactions plus an OS file lock serialize admission across processes.
- Reservations do not expire. Explicit release or a confirmed dead owner releases them. PID reuse is checked against process birth identity.
- If the coordinator fails, the mod aborts the turn without calling the next model handler. Unknown process state retains reservations.
- Cancellation uses tombstones so a delayed acquire cannot resurrect canceled work. Waiting polls locally and consumes no model tokens.
- This controls concurrency, **not total tokens, spend, or the Claude usage window**. It does not prevent the same amount of work from eventually consuming the same tokens.
- Only sessions that load and execute the mod participate. Disabled plugins, other clients, other OS users, and other machines are outside its control. This is not a security boundary or a billing guarantee.
- Subprocesses launched by tools, external APIs, background traffic, and inference paths outside `turn.step` are not covered. Interactions with other mods require testing; another mod may change middleware behavior.
- macOS with the native Claude executable is the initial supported platform. Linux has core CI coverage; native Linux integration is experimental. Windows and npm/Node-hosted Claude are not supported in this beta.
- Closed/dead sessions are reaped on the next coordinator operation. The read-only badge can temporarily show stale occupancy. Do not delete the database while sessions are active: that can admit excess work.

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
