# Security Policy

English | [简体中文](SECURITY.md)

## Supported versions

Only the latest version on the default branch is supported. Experimental releases make no production security or stability guarantee.

## Reporting a vulnerability

Do not open a public Issue containing vulnerability details, credentials, full requests, templates, logs, or KV snapshots. Use a GitHub Security Advisory or the private contact configured by the maintainers. Include:

- affected version and commit;
- reproduction steps or a minimal example;
- impact;
- a suggested fix, if available.

Maintainers will acknowledge the report and publish an advisory or change note after a fix is available. Please allow reasonable time for remediation before public disclosure.

## Deployment boundary

- The default bind address is loopback.
- This project does not provide authentication, TLS, rate limiting, or tenant isolation.
- Do not expose the management API directly to the Internet.
- Treat KV snapshots and templates as sensitive data and protect them according to your model-input and organizational policies.
- Do not let untrusted clients bypass the middleware to access the same llama.cpp slots.
