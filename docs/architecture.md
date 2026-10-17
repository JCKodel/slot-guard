# Architecture

`hooks/register.js` keeps one state machine per session/agent turn. The first `turn.step` acquires one ticket in the global `agent` pool. The ticket stays granted across model responses and ordinary tools. `turn.complete` closes the state; release waits until outstanding ordinary work has unwound.

Each agent tracks busy operations (model streams and ordinary tools) and child-wait operations. `Agent`, legacy `Task`, and blocking `TaskOutput` calls are child waits. An agent with child waits and no busy operations releases its ticket. It obtains a new generation ticket before resuming inference or ordinary tool work, entering at the back of FIFO. If an ordinary tool is running alongside a child wait, the agent remains counted. State transitions are serialized within the mod; tool execution itself remains concurrent.

Subagents follow exactly the same admission path as main agents and release on their own `turn.complete`. No two-pool admission or parent-only reservation remains. A tool outside an admitted agent turn is denied, rather than silently bypassing admission. Both inference and tool hook error fallbacks refuse to call their next handler.

Each coordinator call runs the fixed argv `python3 guard.py client` with `cwd` set to the installed plugin root. JSON stdin carries only coordination identifiers. Python identifies the native Claude ancestor and uses SQLite WAL plus `flock` to serialize FIFO admission across processes. Owner birth identity protects against PID reuse. Unknown liveness retains reservations; confirmed dead owners are reaped on a subsequent call.

A released generation stays as a tombstone until its process dies, preventing delayed acquires from reviving canceled work. Configuration initialization is atomic. Legacy configuration migrates only when every recorded legacy owner is confirmed dead, then replaces both limits with their minimum. Every participating session must be restarted; hot replacement of an active mod is unsupported.

`statusline.py` reads the one pool without modifying state. The optional wrapper preserves the previous statusline and appends the agent count. Detached tools and uninstrumented model paths remain outside this cooperative limiter; it is not a security boundary or token budget.
