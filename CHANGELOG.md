# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-10

Initial public release.

### Added

- Ingestion adapters behind a common contract: Binance public REST and WebSocket, RSS/Atom news,
  CryptoPanic, Finnhub, TCMB EVDS macro series, and optional OpenBB and BIST/KAP integrations.
- Data Quality Gate that accepts, flags, quarantines or rejects series before anything downstream
  sees them.
- Indicator and trigger engine that runs locally, so language models are only called when a
  threshold fires.
- Provider-neutral LLM layer (Anthropic Claude, local Ollama, offline demo client) with a daily
  cost governor, role-based model routing and prompt-injection handling.
- Multi-agent opinion aggregation, a deterministic risk guard, and backtesting with baselines.
- Simulated ("shadow") execution and mission tracking for research. No live orders.
- SQLite point-in-time store, outcome evaluation and retention.
- FastAPI dashboard API and a React cockpit with a draggable widget grid.
- Telegram alerts with prioritisation, quiet hours, cooldown and a daily briefing.
- Synthetic demo data seeding for an offline first run.
- Docker Compose stack, CI for backend and web, gitleaks pre-commit hook.
