# Slot Guard

Machine-wide FIFO concurrency slots for **Claude Code**. Limit the total number of working main agents and subagents across terminals, including time spent executing tools.

```text
Agents ■■■ 3/3 · 2 waiting · ⌛ #1
```

**Beta · macOS · native Claude Code 2.1.302+ · Python 3.10+ · AGPL-3.0-only**

By default three agents can be in a model call while a fourth waits locally. Main agents and subagents share one pool. Agents take turns: one that has held its slot for a minute hands it to the next in line at its next tool, so a single slot is shared instead of monopolized. A parent waiting only for children yields its slot and reacquires before doing more work. No queue server, daemon, npm dependencies, telemetry, or model calls to manage the queue.

## Install

Make sure `python3` is on the PATH used by Claude Code. From your terminal:

```sh
claude plugin install slot-guard --marketplace JCKodel/slot-guard
```

Restart all Claude Code sessions that should participate. The first use creates private state in `~/.local/state/slot-guard` with **3 working-agent slots**. See [Choosing a limit](#choosing-a-limit) for your plan. Run `/slots` in Claude Code to inspect occupancy and the queue, and `/slots help` for the subcommands that change the limits. These are local commands, not prompts sent to a model.

Do not enable two copies of Slot Guard in one session. If you used the earlier private prototype, disable it before enabling this version. The public version intentionally uses a different state directory; the two versions do not coordinate with each other by default.

Update or remove:

```sh
claude plugin update slot-guard@slot-guard
claude plugin uninstall slot-guard@slot-guard
```

If you installed the optional statusline below, restore it before removing the checkout. Uninstalling the plugin does not remove local state or your statusline.

## Configure limits

Everything can be changed from inside Claude Code with one command and its subcommands (`/slots help` prints this list):

```
/slots                 Show the limit, who is working and who waits (FIFO).
/slots agents N        Set the normal limit to N agents, applied now. The automatic limit stays at or under it, unless "max" allows more.
/slots max N           Extra room above "agents": with the automatic limit, up to N agents while every usage window has plenty left. N must be above "agents"; to lower the limit, lower "agents".
/slots auto on|off     Pace the limit by the usage windows (on), or keep it fixed at "agents" (off).
/slots quantum N       Seconds an agent keeps its slot while others wait (default 60).
/slots reset           Restart the automatic limit from the normal limit, now. The usage readings and their rate stay.
/slots forget          Forget the usage readings too and restart from the normal limit: after a plan change, or to end a 98% pause now.
/slots evict N [N...]  Release ticket N by hand (numbers as listed by /slots).
/slots sprint [all] [M] Urgent work: this session's agents (all: every session's) start at once, over the limit, for M minutes (default 5, up to 60). The others wait until it drains; a pause still stops it.
/slots sprint off      End every sprint now.
```

A sprint follows the session ID: `/clear` starts a new session, outside the sprint.

`agents` is the limit you normally get; `max` is only extra room above it, never a lower ceiling. To run at most 5 agents, use `/slots agents 5`: `/slots max 5` with `agents` at 10 sets no maximum, and the limit still goes up to 10. A `max` at or under `agents` removes any maximum, and lowering `agents` to or past the maximum removes it too. `max` only bounds the automatic limit: with `auto off` it is kept but has no effect, and the limit is `agents`.

The same is available from a checkout, which the optional statusline installation also needs:

```sh
git clone https://github.com/JCKodel/slot-guard.git
cd slot-guard
python3 guard.py configure --agents 3
python3 guard.py status
python3 guard.py reset         # same as /slots reset
python3 guard.py forget        # same as /slots forget
python3 guard.py evict 12      # free ticket #12 by hand
python3 guard.py sprint 5      # same as /slots sprint all 5; 0 ends every sprint
```

Or `make`, which lists the same commands, with values after them as in `/slots`: `make agents 4`, `make max 6`, `make auto off`, `make quantum 60`, `make status`, `make reset`, `make forget`, `make evict 12 13`, `make sprint 5`, `make sprint-off`, and `make update` to install the latest version pushed to GitHub.

`--agents` (or `/slots agents N`) is the single global limit. Three sessions without children, two working parents plus one working child, or one working parent plus two children all use three slots. Idle terminals and parents suspended waiting only for children do not count as working. A new limit applies at once to every session; work already running finishes, and the queue drains to the new limit.

### Choosing a limit

The right limit depends on your Claude plan: the goal is for the 5-hour usage window to last its full five hours. Slot Guard limits concurrency, not tokens, so the limit sets how fast the window drains.

| Plan | Limit | Source |
|---|---:|---|
| Claude Max 5x | 3 | Measured by the maintainer: 4 drained the 5-hour window early, 3 lasts it. |
| Other plans | not measured yet | Start at 3 and calibrate. |

To calibrate, work normally and watch the 5-hour usage meter (`/usage`, or your statusline). If the window runs out before five hours, lower the limit by one; if a lot remains when it resets, raise it by one. Your usage pattern (model, effort, context size) matters as much as the plan. Measurements for other plans are welcome as issues or pull requests.

The default for new installations is 3. Upgrading keeps your configured limit. With the automatic limit below, this value is the maximum, and pacing within it happens by itself.

### Automatic limit

With the automatic limit (on by default), `--agents` is the normal maximum and `--max N` optionally allows more. Both the five-hour and seven-day windows have an explicit continuous ceiling:

```text
ceiling = 98 × elapsed_seconds / window_seconds
margin  = ceiling − usage_percent
```

The ceiling grows every second and ends at 98%. The weekly budget releases exactly 14 percentage points per day, continuously rather than in daily steps. At 86% of the week elapsed, the ceiling is **84.28%**, not 86%; 84% usage leaves 0.28 points. `/slots` displays the ceiling, margin, and estimated supported agents for each window. `Agents 0/5` means zero agents occupying five available slots, not a pause.

- **Capacity:** the greater of two paces, divided by the consumption per agent. One is what is left of 98% (less a one-point reserve for coarse readings) spread over the working time left. The other is the margin under the ceiling spent over the next three hours, on top of what the ceiling releases meanwhile: a night's unused budget goes in a morning burst that tapers off to the steady pace, instead of being spread thin over the week. The week's working time is learned: the share of the last seven days with any agent working, so office hours spread the week over office hours and a machine working around the clock spreads it over the clock. The five-hour window spreads over the clock. Capacity never exceeds what keeps usage within one point of the ceiling for the next hour. The stricter window controls concurrency; fractional capacity is rounded down, with a minimum of one while usage remains within the ceiling.
- **Consumption from spend:** the mod reports how many dollars each session's API responses cost, as Claude Code prices them (the figure `/cost` shows), as it grows. A window's points per dollar hold far steadier than its points per agent-hour, which idle agents, long tools and context size all move: on the maintainer's machine, one point of the five-hour window took $5–7 hour after hour, and one of the week $23–25, where points per agent-hour varied fivefold. The coordinator learns points per dollar for each window from the last seven days, across windows, nights and resets, and dollars per agent-hour from the last hour of work, never under the week's mean. Between whole-point readings, the dollars spent since the last one move the usage estimate up to a point, so capacity falls as soon as work speeds up. Pauses still follow readings only. Before any spend is reported (sessions on an older version), consumption is measured per agent-hour from the readings as before.
- **Immediate reductions:** every coordinator operation, including readings and 30-second heartbeats, recalculates the limit. Six slots can become one in one evaluation, even immediately after a configuration change. Calls already running finish, but excess agents yield before their next model call, without waiting for the normal rotation quantum.
- **Gradual recovery:** add at most one slot every ten minutes, only if both windows support the new concurrency with 10% headroom. After an hour idle, recovery can settle immediately at the supported limit. A configured `max` uses the same capacity rule.
- **Rates without spend:** recent weekly consumption is measured per working hour over up to two working hours, with at least one hour of history. The five-hour rate uses up to one clock hour, with at least ten minutes of history. Measured concurrency over the same interval converts each rate to consumption per agent; idle time is included in both the five-hour rate and its mean load. A renewed or flat window uses the consumption per agent its previous window ended with; without one, startup estimates of 1 percentage point per agent-hour weekly and 20 per agent-hour for the five-hour window. Without any valid usage reading, the configured normal limit applies.
- **Emergency protection:** usage more than one point above the moving ceiling (readings are whole points; the ceiling is never under 1%, so a renewed window's first 2% are not an overshoot) pauses admission only until the ceiling is back within that point. At 98%, admission pauses until reset. These are backstops, not the desired operating pattern. One agent may itself consume faster than the released budget; concurrency alone cannot guarantee uninterrupted work in that case. Delayed readings, workload changes, in-flight calls, and usage elsewhere can also cause overshoot. `/slots` exposes capacity below one instead of hiding this constraint.
- **No automatic overdraft:** a waiting subagent cannot exceed the automatic limit to avoid Claude Code's stall timeout. It may time out while queued. Explicit sprints remain a user-requested override of concurrency (not of a ceiling pause); avoid them when preserving the budget.
- **Reset and plan changes:** `/slots reset` keeps readings and immediately reapplies the budget. `/slots forget` clears readings; a later report restores protection. A drop of more than five points for the same reset time must persist for five minutes before being treated as a plan change. `readings.log` survives both commands.

Every limit change is recorded in `auto.log`. Restart all participating Claude sessions after updating so older coordinators cannot grant using the previous policy.

```sh
python3 guard.py configure --max 5      # allow up to 5 while there is plenty left
python3 guard.py configure --quantum 60 # seconds an agent keeps its slot while others wait
python3 guard.py configure --auto off   # fixed limit
python3 guard.py configure --auto on
python3 guard.py status                 # limit, maximum, and each window's usage, elapsed time and capacity
```

`/slots` shows the calculated ceiling and margin; the statusline shows occupancy, for example `Agents ■ 1/1 (auto, normal 6)`, or `Agents ⏸ paused until 14:05 (continuous usage ceiling reached)`. Changing configuration does not bypass automatic safety reductions. `/slots auto off` explicitly returns to a fixed concurrency limit.

### Subagents and the 10-minute stall limit

Claude Code fails a subagent whose model step shows no progress for 600 seconds. Waiting in the queue counts as no progress, so Slot Guard reduces that wait where the budget permits:

- **Handoff:** a parent waiting for its subagent hands it its slot directly; the first subagent starts at once. If none takes it within a minute, the slot returns to the pool (or to the parent when it resumes).
- **Work under way first:** subagents and resumed turns queue ahead of new prompts.
- **Manual-mode overdraft:** a subagent that has waited 7 minutes is admitted even over the limit, leaving the model 3 minutes to start answering before the 10-minute stall limit. Nothing new starts until the pool is back under the limit; `/slots` shows `N over the limit` and `auto.log` records each one. Automatic mode rejects this override to preserve the calculated concurrency.

### Taking turns (cooperative multitasking)

The limit controls agents admitted to model calls, which spend usage. Tools run on the agent's ticket whether it is granted or waiting. Between calls, an agent that has held its slot for the quantum (60 seconds by default) yields to a queued agent. When the automatic limit falls, excess agents yield before their next call even if their quantum has not expired and nobody else was waiting. Calls already running are never interrupted, so concurrency can temporarily exceed a newly reduced limit until they reach that boundary. Explicit sprints and manual-mode overdrafts also allow extra concurrency. An agent waiting five minutes gets the same priority as work under way to reduce starvation; this does not guarantee avoiding Claude Code's stall timeout under heavy contention.


With `n` agents sharing one slot, each waits about `(n − 1) × quantum` between turns. Up to five agents that stays under the 5-minute prompt-cache lifetime of subagents; with more, a longer quantum avoids cache rebuilds at the cost of longer turns.

An agent yields its slot at once while it waits on something else: `Agent`/legacy `Task` delegation, blocking `TaskOutput`, a question or plan approval for the person (`AskUserQuestion`, `ExitPlanMode`), a permission prompt still open after 15 seconds (auto mode's classifier answers sooner and keeps the slot), and a `Bash` command that runs `claude` (the nested session takes its own slot). A tool approved at a permission prompt then runs without a slot; the agent rejoins FIFO before its next inference. If another ordinary tool from that parent is still running in parallel, the parent keeps its ticket until that work finishes. A suspended parent joins FIFO again before its next inference or ordinary tool. This prevents four parents from holding every slot while waiting for children that cannot start.

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
Agents ■■■□ 3/4 (auto) · ⌛ #2
```

`■` is occupied; `□` is available. The single count includes all working main agents and subagents. `N waiting` counts queued agents on the whole machine; `⌛ #n` is the current session's best queue position. The display is read-only and refreshes every five seconds. It does not split or reformat your existing bars. Slow existing commands have a three-second timeout.

Restore the previous statusline:

```sh
python3 statusline_setup.py remove
```

The remover preserves unrelated settings and refuses to overwrite a statusline you changed after installation. `CLAUDE_CONFIG_DIR` is respected; explicit `--config-dir` and `--runtime-dir` options are available. Avoid editing settings concurrently with installation/removal. To update the copied helpers, remove and install again from the updated checkout.

## Guarantees and limits

- FIFO ordering applies to agent admissions and resumptions, according to the order tickets enter the database. OS scheduling may change when admitted work visibly starts.
- SQLite transactions plus an OS file lock serialize admission across processes.
- A reservation ends by explicit release, by a confirmed dead owner (PID reuse is checked against process birth identity), by eviction (`/slots evict N` or `guard.py evict N`), or by its lease: a running mod renews its tickets every 30 seconds, and a ticket not renewed for 5 minutes is released. A ticket the mod holds without using it for 2 minutes is released by the mod itself. A failed release is retried. Reloading or updating the mod releases the tickets its previous load held. An agent whose ticket was evicted or expired rejoins the back of the queue.
- If the coordinator fails, the mod aborts the turn without calling the next model handler. Unknown process state retains reservations.
- Cancellation uses tombstones so a delayed acquire cannot resurrect canceled work. Waiting polls locally and consumes no model tokens.
- This controls concurrency, **not total tokens, spend, or the Claude usage window**. It does not prevent the same amount of work from eventually consuming the same tokens.
- Only sessions that load and execute the mod participate. Disabled plugins, other clients, other OS users, and other machines are outside its control. This is not a security boundary or a billing guarantee.
- Detached background tool processes, external APIs, background traffic, and inference paths outside `turn.step` are not covered. Foreground ordinary tool execution retains its agent slot; a subprocess that outlives its tool call is not a separate admitted agent. Waiting implemented through arbitrary shell polling cannot be recognized as a child wait and holds the slot until the command ends. Tools called outside an admitted turn are denied; plugins that invoke tools independently may be incompatible. Interactions with other mods require testing; another mod may change middleware behavior.
- macOS with the native Claude executable is the initial supported platform. Linux has core CI coverage; native Linux integration is experimental. Windows and npm/Node-hosted Claude are not supported in this beta.
- Closed/dead sessions and expired leases are reaped on the next coordinator operation. The read-only badge can temporarily show stale occupancy. Do not delete the database while sessions are active: that can admit excess work.

## Permissions, data flow, and directory review

### Programs executed by the mod

The mod makes one kind of process call, in `hooks/register.js`:

```js
$.process.run(['python3', 'guard.py', 'client'], {
  cwd: $.plugin.root, stdin: JSON.stringify(data), timeoutMs: 25000,
})
```

Its complete command is the fixed argument vector `python3 guard.py client`, with the working directory set to Claude's trusted `$.plugin.root`. The script path is relative to the installed plugin, never the user's project. All command arguments are literal strings; only the installation working directory varies. Arguments are passed as an array without a shell, inline Python, downloaded code, or a network utility. The bundled `guard.py` coordinates local SQLite reservations. Python is necessary for SQLite, cross-process file locks, and process-liveness inspection using the standard library.

On macOS, process inspection uses `/usr/lib/libproc.dylib`; only when it cannot return an ancestor's executable path (Claude Code's updater deleted the version a session still runs) does ancestry inspection invoke `/bin/ps -p <pid> -o comm=`. On Linux, ancestry inspection invokes `/bin/ps -p <pid> -o ppid= -o comm=` and process identity reads `/proc/<pid>/stat` and `/proc/sys/kernel/random/boot_id`. The fallback for other Unix platforms invokes `/bin/ps -p <pid> -o lstart= -o stat=`; those platforms are not supported by this beta. PIDs are derived from the OS, not user text. No command reads process environment variables or full command-line arguments.

### Data, destinations, and credentials

The mod sends JSON **only to the local Python child's stdin**: operation (`status`, `acquire`, `release`, `touch`, `rotate`, `adopt`, `evict`, `usage`, `spend`, `reset`, or `configure`), the single `agent` pool, session ID, turn ID, ticket key (including mod load ID, agent ID and admission generation), ticket numbers to evict, the limits typed at `/slots`, the usage windows Claude Code reports to the session (kind, percentage used, reset time), how many dollars the session's cost grew by since the last report (a single number from `$.session.usage()`, the figure `/cost` shows; no token counts, model names or content), and polling wait duration. The child adds the owner PID and birth identity. JSON results return on stdout; errors return on stderr and may appear in Claude's local status UI.

`guard.py` reads/writes `config.json`, `queue.sqlite3` (including SQLite journal files), and local lock/temporary files under `~/.local/state/slot-guard`, or the explicitly configured `CLAUDE_SLOT_HOME`. Stored records contain coordination identifiers, ownership, state, sequence, and creation timestamps. Stored records also carry the last renewal time and the mod load that created them. The latest usage window figures and the automatic limit's state are kept too, with the machine's dollars spent per minute for the last eight days. Released records are deleted when their owner exits, or one hour after release. State remains after uninstall; remove it only after all participating sessions have stopped.

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
