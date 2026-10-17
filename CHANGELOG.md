# Changelog

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
