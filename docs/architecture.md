# Architecture

`hooks/register.js` keeps one state machine per agent turn, keyed by agent ID (a `/clear` changes the session ID mid-flight, never the agent). The first `turn.step` acquires one ticket in the global `agent` pool. The ticket stays granted across model responses and ordinary tools. `turn.complete` closes the state; release waits until outstanding ordinary work has unwound.

Each agent tracks busy operations (model streams and ordinary tools) and wait operations. `Agent`, legacy `Task`, blocking `TaskOutput`, `AskUserQuestion`, `ExitPlanMode`, and `Bash` commands that run `claude` are waits. A `tool.check` verdict of `ask` still unanswered after 15 s turns the admitted call into a wait, so a permission prompt does not hold a slot while the person is away. The delay matters in auto mode, where most calls are an `ask` answered by the classifier within seconds: yielding at once sent every agent to the back of FIFO on each tool call. In rare parallel timing (another tool of the same agent requeued ahead of it) the `tool.check` hook can exceed its budget waiting for the agent's lock; the engine's verdict stands and the state stays consistent. An agent with child waits and no busy operations releases its ticket. It obtains a new generation ticket before resuming inference or ordinary tool work, entering at the back of FIFO. If an ordinary tool is running alongside a child wait, the agent remains counted. State transitions are serialized within the mod; tool execution itself remains concurrent.

Subagents follow exactly the same admission path as main agents and release on their own `turn.complete`. A new turn for an agent whose previous turn never reported completion closes that turn instead of failing. No two-pool admission or parent-only reservation remains. A tool outside an admitted agent turn is denied, rather than silently bypassing admission. Both inference and tool hook error fallbacks refuse to call their next handler.

Each coordinator call runs the fixed argv `python3 guard.py client` with `cwd` set to the installed plugin root. JSON stdin carries only coordination identifiers. Python identifies the native Claude ancestor and uses SQLite WAL plus `flock` to serialize FIFO admission across processes. Owner birth identity protects against PID reuse. Unknown liveness retains reservations; confirmed dead owners are reaped on a subsequent call.

A released generation stays as a tombstone until its process dies or for one hour, preventing delayed acquires from reviving canceled work.

## Liveness inside a living process

Owner death alone cannot free a ticket the owner forgot, because the owner is the long-lived Claude process. Four mechanisms bound that:

1. **Retried release.** Release never throws; a failed release (an interrupted dispatch aborts its own calls, a lock timeout) is retried at the agent's next step, at `turn.complete`, and by the heartbeat.
2. **Lease.** `session.start` starts a 30 s `$.clock.every` heartbeat that renews (`touch`) every ticket the mod is using. The coordinator releases a ticket of a heartbeating client not renewed for 300 s. Rows written by older clients (no load ID) never expire, so mixed versions stay safe.
3. **Idle release.** A ticket with no busy work, no wait and no acquisition in progress for 120 s is released by the heartbeat: a turn whose end was never heard cannot keep its slot.
4. **Adoption.** Each module load has its own ID. On `session.start` it releases every ticket of the same owner held by another load (a hot reload or plugin update discards the previous load's state and timers).

An evicted or expired ticket rejoins the back of FIFO once on the agent's next operation; a second refusal stops the turn. Under coordinator lock contention an acquirer keeps waiting rather than aborting the turn; other operations fail and are retried. Configuration initialization is atomic. Legacy configuration migrates only when every recorded legacy owner is confirmed dead, then replaces both limits with their minimum. Restarting participating sessions after an update is still recommended; a hot replacement releases the previous load's tickets.

`statusline.py` reads the one pool without modifying state. The optional wrapper preserves the previous statusline and appends the agent count. Detached tools and uninstrumented model paths remain outside this cooperative limiter; it is not a security boundary or token budget.
