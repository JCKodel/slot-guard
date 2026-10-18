# Changelog

## 0.5.1-beta.1

- `configure --max N`: with plenty left in every window (pace above 1.25, weekly included), the limit may rise above the normal `--agents` value up to N, one slot per 10 minutes, and returns toward normal once any window falls under 1.1. Off by default.
- A subagent paused past Claude Code's 10-minute step bound ends with a message telling its parent to dispatch it again after the renewal, instead of an empty answer.
- Pace now also judges the average burn so far: a fast start (14% used after 10% of the 5-hour window) lowers the limit at once. 0.5.0 only compared the budget left with the time left, which tolerated a fast start until late in the window.
- The weekly window is tolerant: it lowers the limit only at twice the sustainable pace, never below 2 agents.
- The first 15 minutes of a window (not 5% of it, which was 8 hours for the weekly window) are ignored.
- Pauses: lease expiry and idle release are suspended while every agent waits on purpose.
- The engine abandons a subagent's model step after 10 minutes. The mod no longer aborts a turn whose dispatch was abandoned, and a failed `$.turn.abort` (refused for subagent turns) can no longer let the model run without a slot.

## 0.5.0-beta.1

- Automatic limit (on by default): `--agents` becomes the maximum. Using the usage windows Claude Code reports (5-hour and weekly), the limit drops a slot while usage runs ahead of the window's elapsed time and rises while it runs behind, at most one slot per 10 minutes, aiming to end each window at 95%.
- When a window reaches 95%, all agents pause at their next step or tool and resume by themselves when the window renews, in FIFO order.
- `configure --auto on|off`; `status` and `/slots` show the limit, maximum, and each window's usage, elapsed time and pace; changes are logged to `auto.log`.
- Statusline: `(auto, max N)` below the maximum, `⏸ paused until HH:MM` during a pause.

## 0.4.2-beta.1

- New installations default to 3 working-agent slots instead of 4. Existing configurations keep their limit.
- README: "Choosing a limit" by Claude plan, with the measured Max 5x value and a calibration procedure.

## 0.4.1-beta.1

- Fix slot churn in auto mode: 0.4.0 yielded the slot on every permission `ask`, and auto mode asks its classifier on most tool calls, so agents released and requeued at the back of FIFO every few seconds. A slot is now yielded only when an ask is still open after 15 seconds.
- The statusline shows the machine-wide queue: `Agents ■■■■ 4/4 · 2 waiting · ⌛ #1`.

## 0.4.0-beta.1

- Slots can no longer be held forever by a living Claude process:
  - A lease: the mod renews its tickets every 30 s; the coordinator releases tickets not renewed for 5 minutes.
  - Tickets held but unused for 2 minutes are released by the mod (covers a turn whose end was never reported).
  - Failed releases are retried at the next step, at turn end and by the heartbeat, instead of being lost.
  - A reloaded or updated mod releases the tickets of its previous load.
  - `/slots evict N` and `guard.py evict N` free a ticket by hand; `/slots` lists tickets with their renewal age.
- Waiting on the person no longer holds a slot: permission prompts (`tool.check` asks), `AskUserQuestion` and `ExitPlanMode` yield.
- A `Bash` command that runs `claude` yields, avoiding deadlock with the nested session's own slot.
- Agent state is keyed by agent, not session ID, so `/clear` cannot orphan a running agent's slot.
- A new turn closes a previous turn whose completion was never reported, instead of aborting every later turn.
- The `tool.call` fallback no longer reports an already executed tool as denied.
- Coordinator lock contention keeps an acquirer waiting instead of aborting its turn.
- Released tombstones are pruned after one hour; the unused in-process long-poll was removed.
- Schema migrates in place (`touched`, `instance` columns); rows of older clients keep their old never-expire semantics.

## 0.3.0-beta.1

- Replace turn/inference pools with one global working-agent pool shared by main agents and subagents.
- Hold slots through ordinary tool execution and between inference steps.
- Yield parents waiting only for children; rejoin FIFO before resuming work, with parallel-tool accounting to avoid premature release.
- Show a single `Agents ■■■□ 3/4` counter and configure with `--agents`.
- Refuse legacy migration while recorded old owners are alive; restart all Claude sessions before updating.
- Test real foreground tools and four simultaneous delegating parents with twelve children, as well as nested lifecycle and cancellation scenarios.

## 0.2.0-beta.2

- Add a 512×512 PNG icon and its reproducible geometric source.
- Document exact subprocess commands, local data flow, retention, middleware behavior, and credential handling for directory review.
- Explain the optional statusline shell command and development-only test/CI operations.
- Use a literal `python3 guard.py client` command with the plugin directory as its explicit working directory. JSON stdin remains data, not interpreter code.
- Replace whole-environment copies in test tools with a non-credential allowlist.
- Limit the fallback `ps` environment to its required locale setting.

## 0.2.0-beta.1

First public beta, extracted from a private local prototype.

- Independent FIFO pools for main turns and inference, including subagents.
- Cross-process SQLite coordination without a daemon or network service.
- Fail-closed mod, cancellation tombstones, and recovery after owner death.
- Portable plugin and marketplace manifests; automatic private state initialization.
- Local `/slots` command and optional reversible statusline with `■`/`□` bars.
- Isolated tests with fixed local responses, packaging, and CI.
- AGPL-3.0-only license.
