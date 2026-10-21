# Security Policy

## Supported versions

| Branch | Supported |
|---|---|
| `rideshare-v2` (active development, deployed demo) | Yes |
| `main` (original version) | Security fixes land through the v2 merge |

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Include:

- the affected endpoint, file or commit
- steps to reproduce or a proof of concept
- the impact you observed and any suggested fix

You can expect an acknowledgement within 7 days. This is a solo-maintained project, so
fixes are handled on a best-effort basis; I will keep you updated and credit you in the
fix unless you prefer otherwise.

## Scope

In scope: this repository's backend, frontend and deployment configuration.
Out of scope: denial-of-service volume testing against the live demo, social
engineering, and third-party services. Only test against your own local instance
or with explicit permission; never access other users' data.

## Handling secrets

- Configuration is read from the environment; see `.env.example`. Never commit `.env`.
- `SECRET_KEY` must be random and at least 32/48/64 bytes for HS256/HS384/HS512;
  the app refuses to start with short or placeholder keys.
- Early history on `main` contains a development-only placeholder JWT key and a local
  database URL. They are not used by the deployed application, which requires its own
  environment-provided key. Do not reuse any value from the repository history.
