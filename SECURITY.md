# Security and privacy

Supported release: 0.2.0 beta. This project is a concurrency convenience, not a sandbox or protection against malicious local users or plugins.

The coordinator stores session/turn identifiers, owner PIDs and birth identities, ticket ordering, and occupancy. It does not store prompts, responses, or credentials, and has no networking code. It trusts files and processes owned by the current OS user. Keep state on a private local filesystem, not NFS or a shared directory.

The optional statusline installer modifies only the statusLine setting and saves its original value. The wrapper executes the command already configured by the user; never accept an untrusted settings file.

Report vulnerabilities privately using GitHub's **Security → Report a vulnerability** for this repository. For normal bugs, open an issue and redact local paths and session IDs. Do not attach Claude debug logs containing private project data.
