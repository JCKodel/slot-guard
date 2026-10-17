# Changelog

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
