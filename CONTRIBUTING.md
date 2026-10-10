# Contributing

Thanks for taking a look. Small, focused pull requests are the easiest to review.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev,ingestion,platform,brain,interface,dashboard]"
pre-commit install

cd web && npm ci
```

## Checks

Everything CI runs can be run locally:

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy
pytest
```

```bash
cd web
npm run lint
npm run build
npm test
```

Tests never touch the network; adapters take injected clients. Keep it that way: a new adapter
needs a fake provider and offline tests.

## Ground rules

- **No secrets, no personal data.** Secret configuration goes in `.env`; everything else in
  `config/config.yaml`.
- **No third-party data in the repo.** Do not commit scraped articles, exchange data dumps or
  database files. Fixtures must be synthetic.
- **Official sources only.** New adapters must use a documented API or a publisher-provided feed,
  and the adapter docstring should note the terms of use. No HTML scraping. Integrations that rely
  on unofficial libraries stay behind their own optional extra, like `bist`.
- **Not financial advice.** Argus is a research and decision-support tool. Avoid wording that reads
  as a recommendation to trade.
- **Fail closed.** If data is missing or stale, surface that instead of guessing. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design principles.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, ...).
Add a line to `CHANGELOG.md` under "Unreleased" for user-visible changes.

## License

By contributing you agree that your contributions are licensed under the Apache License 2.0.
