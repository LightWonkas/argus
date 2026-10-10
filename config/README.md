# `config/` — configuration as data

The YAML in this directory holds Argus' editable runtime configuration: the universe and
watchlist, trigger and data-quality thresholds, alert routing, news feeds, retention and risk
limits. It is plain data, so behaviour can be adjusted without a code change.

| File | Contents |
|---|---|
| `config.example.yaml` | Shipped defaults. Read when `config.yaml` does not exist. |
| `config.yaml` | Your own copy. Not committed. Takes precedence when present. |

```bash
cp config/config.example.yaml config/config.yaml
```

The schema is `argus.config.models.RuntimeConfig`; an invalid file is rejected at startup with the
failing field named. Saving from the cockpit's settings page writes `config.yaml` (YAML comments
are not preserved). Under Docker Compose this directory lives in the `app-config` volume.

- **Secrets do not live here.** API keys and tokens are read from `.env` (see `.env.example`),
  which is never committed.
- **Values are illustrative defaults, not tuned recommendations.** Review every threshold and
  limit for your own use. Argus is a research and decision-support tool. Not financial advice.
