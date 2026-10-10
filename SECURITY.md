# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on this
repository. Include what you found, how to reproduce it, and the impact you expect. You should get a
first response within a week.

## Supported versions

Only the latest release on `main` receives fixes.

## Scope notes

- Argus has **no live order execution**. It reads public market data and news; it holds no exchange
  credentials and cannot move funds.
- The dashboard API binds to `127.0.0.1` by default and is meant to run on a private network. If you
  expose it, set `ARGUS_DASHBOARD_USERS` and `ARGUS_DASHBOARD_SESSION_SECRET` and put it behind TLS.
- With `ARGUS_DASHBOARD_USERS` empty there is **no authentication at all**. Anyone who can reach the
  port can use the settings endpoints, which write API keys and the Telegram token to `.env` and
  rewrite `config.yaml`. Never run in that mode on a shared or reachable interface.
- News and web content is passed to language models as untrusted data. Prompt-injection handling
  lives in `src/argus/brain/security.py`; bypasses are in scope.
- Secrets are read from environment variables only. A code path that logs or returns a secret is a
  vulnerability.

## Keeping your own deployment safe

- Never commit `.env`. Run `pre-commit install` to get the gitleaks hook.
- Rotate any key that was ever pasted into an issue, log or chat.
