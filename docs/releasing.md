# Release procedure

1. Update `.claude-plugin/plugin.json` version and CHANGELOG. Run every test and both strict validators in README. Use an isolated Claude configuration to verify marketplace installation.
2. Confirm no credentials, logs, production state, machine-specific paths, or private prototype files are tracked.
3. Run `python3 scripts/package.py` and inspect the archive. The archive contains only runtime, metadata, documentation, and license files; test fixtures are never shipped in it.
4. Push the commit and wait for GitHub Actions. Tag the tested commit as `v<VERSION>` and create a GitHub release with the zip and SHA256SUMS. Keep beta versions marked as prereleases.
5. The repository's marketplace is immediately installable. Users update with `claude plugin update slot-guard@slot-guard`; marketplace auto-updates are off by default.

## Discovery

The primary listing is this GitHub repository and its bundled Claude marketplace. Use the `claude-code`, `claude-code-plugin`, `concurrency`, and `fifo` topics. A suitable announcement:

> Slot Guard is an AGPL-3.0 Claude Code mod that coordinates FIFO concurrency limits across local terminals and subagents. No daemon, no tokens spent waiting, and optional slot bars in your existing statusline. First public beta supports native Claude on macOS. Install: `claude plugin install slot-guard --marketplace JCKodel/slot-guard`.

Additional distribution can include community plugin directories and a Show HN or community discussion after beta feedback. For Anthropic's directory, submit via https://claude.ai/directory/manage using a paid Claude account. This requires a separate portal submission and review; a GitHub release does not submit it automatically. The mod is Claude Code-specific; do not advertise Cowork or claude.ai compatibility.

Official reference: https://code.claude.com/docs/en/plugins/publish
