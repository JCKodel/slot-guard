# Changelog

## 0.6.10-beta.1

- The weekly linear quota goes by the day, as statuslines show it: each day from the window's reset hour allows another seventh of the week, in whole points (14, 28, 42, 57, 71, 85, 100%). A reading at today's quota pauses every agent until a day whose quota is above it starts: at 71% on Thursday 16:12 of a week that resets Saturday 18:00, until 18:00, where the continuous line let work resume at 17:16 and run past 71% before the day ended. The proportional cut paces the margin to today's quota over 4 hours, or until the day ends if sooner.
- A session whose Claude version was deleted by Claude Code's updater no longer stops every turn with "Cannot identify Claude ancestor": libproc has no path for a deleted executable, so the owner lookup asks `ps` for it.
- `python3 guard.py sprint M` (0 ends every sprint), and a Makefile whose `make` lists every `/slots` command with its arguments.

## 0.6.9-beta.1

- The weekly linear quota is a ceiling, not a tolerance. Used share must never exceed elapsed share (at 58.1% of the week gone, 58%); it used to act only at pace 0.75, about a third over the line, and kept at least 2 agents while under it. Now a separate `line` vote cuts in proportion as soon as the margin to the line, at the recent rate taken as nonstop work, would not last 4 hours (6 agents burning 0.92% an hour on the line keep 4, which is about the line's own 0.6% an hour), and a reading over the line pauses every agent until the line reaches it. The limit beneath keeps falling during the pause and work resumes from it, not from the maximum. `/slots reset` no longer ends such a pause while the readings stay over the line; `/slots auto off` does. The minimum of 2 agents under the line is gone.
- `/slots` shows the line pace, and the statusline, `/slots` and paused subagents say when a pause is for the weekly line rather than a window at 95%.

## 0.6.8-beta.1

- A stale reading no longer fakes a plan change. An idle session reported last evening's weekly 32% while the window stood at 38%; the coordinator took it for a restart, dropped the window's history, and counted the live 38% six seconds later as 6 points burned, which held the limit at 1 for hours. A drop now counts only when another reading at least 5 minutes later is still that low, with none near the stored figure in between.
- Cuts in proportion: a window below its pace no longer lowers the limit one slot every 10 minutes all the way to 1, but only down to the normal limit times its pace (5 agents at pace 0.85 keep 4). If that is still too fast the pace falls further, and the limit with it; the 95% pause stays the backstop.
- The weekly window acts at pace 0.75 instead of 0.9, keeps at least 2 agents while its usage is under the linear quota, and judges the mean of its recent rate and its rate over all the window's working hours, so a few busy hours are weighed against the week. `/slots` shows both rates.

## 0.6.7-beta.1

- `/slots max N` at or under the normal limit no longer says "max set to N, applied now": it says no maximum is set, that the limit still goes up to `agents`, and suggests `/slots agents N` to lower it. `/slots help` and the README explain that `max` is only extra room above `agents`, never a lower ceiling.

## 0.6.6-beta.1

- `/slots sprint [all] [M]` for urgent work: for M minutes (default 5, up to 60) the agents of this session, or of every session with `all`, start at once and are not rotated out, over the limit. They still count, so other sessions start nothing new until the pool drains under the limit. A pause near a usage window's limit stops a sprint too. `/slots sprint off` ends every sprint; `/slots` shows the ones running.
- `/slots max N` with the automatic limit off says the maximum was saved but has no effect, instead of "applied now" with the limit unchanged; with it on, it says the limit climbs one slot per 10 minutes, and only while every window has plenty left.

## 0.6.5-beta.1

- After a pause the automatic limit settles at once. When no agent has worked for an hour (a night, a long break), the first coordinator call takes every step the current readings call for, up or down, instead of one slot per 10 minutes from where the evening left it: a morning after a limit of 1 started at 1 and needed 40 minutes to reach 5 although the weekly pace allowed 5 from the first call. While agents work the limit still moves one slot per 10 minutes.

## 0.6.4-beta.1

- The statusline shows `(auto)` whenever the automatic limit is on. It used to mark it only once the limit moved from the normal one, so right after `/slots auto on` (which restarts from the normal limit) it looked the same as a fixed limit.

## 0.6.3-beta.1

- The weekly window's burn rate is measured per working hour (time with any agent working on this machine, in 5-minute buckets) and projected over the hours a day worked lately, learned from the last week of activity starting from 8 hours a day. It used to project the recent rate over every hour left in the week, nights included: three busy hours after a plan change lowered the limit toward 1 although the week had plenty left. The rate covers the last 4 working hours, so it follows the current load within the day; the hours a day follow the week.
- A weekly rate needs readings spanning 2 working hours; until then (after a renewal, a plan change or `/slots reset`) the weekly pace is the share of budget left over the share of time left, with the linear quota. Working hours are kept through resets and plan changes.
- `/slots` shows the weekly rate per working hour and the hours a day it projects over.

## 0.6.2-beta.1

- `/slots` gains subcommands, listed by `/slots help`: `agents N`, `max N`, `auto on|off`, `quantum N`, `reset`, next to `evict`. No checkout needed to change the limits.
- A configuration change applies at once: the automatic limit restarts from the new normal limit instead of climbing to it one slot per 10 minutes. `guard.py configure` does the same.
- Plan changes: upgrading a plan starts the account's usage windows over without changing their reset times, and the coordinator kept the old, higher reading of each window (it keeps the highest for the same reset time, since sessions report it from different responses). The limit then stayed at 1 against the weekly line. A reading more than 5 points under the stored one now counts as a restart of the window: it replaces the reading and the limit starts again from the normal limit. `/slots reset` (or `guard.py reset`) forgets every reading by hand and ends a pause at once.

## 0.6.1-beta.1

- Fix a race in 0.6.0: the heartbeat chose agents in long tools, then offered their slots after renewing tickets; a model call starting in between could lose its slot mid-call, letting another agent in. The offer is now decided under the agent's lock, the same one a model call's admission takes.
- `guard.py client` no longer queries status while preparing its configuration, so lock contention keeps an acquiring agent waiting instead of stopping its turn.
- Aging: a ticket in line for 5 minutes gets the priority of work under way, so prompts and rotated agents never wait indefinitely behind new subagents.
- The statusline shows the session's best queue position.
- The integration test checks that simulated model calls never overlap at limit 1, with a fast heartbeat exercising mid-tool offers.

## 0.6.0-beta.1

- Cooperative multitasking. The limit now counts agents in a model call; tools run on the agent's ticket and never wait for a slot. An agent that has held its slot for the quantum (60 s, `configure --quantum`) hands it to the next in line between model calls (at a tool's start, or every 30 s during a long tool) and queues again; nothing happens when nobody waits. With the limit at 1, agents take turns instead of one holding the slot for its whole turn.
- Rotation happens only between model calls, in one coordinator transaction, so no more than the limit are ever in a model call. Rotated agents queue behind agents still waiting for their first grant of a turn.
- Verified against the real engine: two sessions at limit 1 take turns step by step, and sampling the queue every 50 ms never shows more than one grant.

## 0.5.3-beta.1

- The weekly window follows a linear quota (about 14.3% a day): while usage is above the line the limit drops, down to 1 agent, and does not rise until it is back under. It never pauses for the line. Replaces 0.5.1's tolerant weekly policy, which let a fast start borrow from the rest of the week.

## 0.5.2-beta.1

- Subagents no longer fail with "Agent stalled: no progress for 600s" while waiting for a slot: a parent waiting for its subagent hands it its slot; subagents and resumed turns queue before new prompts; a subagent waiting 7 minutes is admitted over the limit (never during a pause), logged and shown in `/slots`.
- Pace follows the recent burn rate (last hour for the 5-hour window) instead of the window's average, so the limit rises again once usage slows: an early burst no longer pins it at 1 for the rest of the window.
- Correction to 0.5.1: in interactive sessions Claude Code reports a subagent abandoned after 10 minutes as failed ("Agent stalled"); the "dispatch it again" text is not what the parent sees.

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
