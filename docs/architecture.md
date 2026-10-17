# Architecture

`hooks/register.js` wraps `turn.step` with a streaming mod handler. Main turns acquire a `job` ticket and then a `request` ticket. Subagent turns acquire only `request`. The request ticket is released in `finally`; the main job is released at `turn.complete`. If admission fails, the handler aborts and returns an empty step. Its async-generator error fallback also prevents fallthrough to inference.

Each call launches `python3 guard.py client`. The coordinator identifies the native Claude ancestor and records its PID plus birth identity (macOS libproc microseconds; Linux boot ID plus /proc start ticks). It serializes transactions using `flock` and a WAL SQLite database. Tickets are promoted in insertion order independently for each pool. Polling preserves ticket identity and yields the OS lock between attempts.

Initialization has its own lock and writes configuration through atomic replacement. Existing configuration is never silently reset. A missing configuration with an existing database is an error. Configuration is reread under the coordinator lock for every polling iteration.

A released ticket remains as a tombstone until its owner dies. This prevents a racing or delayed acquire from recreating canceled work. On the next coordinator call, confirmed dead owners are reaped; inability to inspect an owner leaves its reservations intact. There is deliberately no time-based lease. Tombstones can grow during long-lived sessions and are cleaned after those sessions exit.

`statusline.py` uses a read-only SQLite connection and never reaps or modifies tickets. The optional installer copies a wrapper and this reader to a stable location, saves the original statusline setting, and appends the badge on a new line. It does not modify the plugin installation or other settings.
