# Contributing

Open an issue describing the behavior and platform before large changes. Small fixes can go straight to a pull request. Contributions are licensed under AGPL-3.0-only; no separate CLA is required.

Use Python's standard library for the coordinator and plain JavaScript for the mod. Keep coordination local and never include prompts, answers, credentials, or remote inference in tests. Run the commands in README before submitting. Report which Claude Code version and operating system you tested.

Changes to admission, cancellation, process ownership, or hook fallback behavior must include regression tests. Preserve FIFO ordering and fail-closed behavior. Never introduce automatic expiry of reservations held by a live process. Test with an isolated `CLAUDE_SLOT_HOME`; never reuse production state for tests.
