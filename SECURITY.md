# Security and public source release

This repository publishes application source, synthetic fixtures and documentation. It excludes the original Git history, databases, exports, browser profiles/cookies, private media, resumes, credentials, keys, logs, screenshots, chat archives, operational manifests, deployment backups and virtual environments.

Hardcoded owner contact information and private-message examples found in source were removed or replaced with synthetic examples. Personal CV fallback responses were removed: failed AI extraction/tailoring now stops instead of inserting a fixed person's information. This is a development snapshot; historical operator-specific utilities still need generalization.

## Before enabling integrations

- Use your own accounts and independently generated keys.
- Keep `.env`, database volumes, session data, candidate files and generated artifacts private.
- Configure explicit hostnames and HTTPS for a deployment; do not expose development settings or browser control ports publicly.
- Review authorization, candidate identity, suppression state and provider safety stops before external actions.
- Use isolated databases and mocked provider transports for tests. Never test by sending messages or applications from an operational account.

Secret scanning reduces accidental disclosure but is not a guarantee that software is free of vulnerabilities. If you discover a vulnerability, use GitHub's private vulnerability-reporting feature when available. Do not include credentials or personal records in public issues.
