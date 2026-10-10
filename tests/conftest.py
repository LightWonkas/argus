"""Test isolation — tests must be offline and **independent of the on-disk ``.env``**.

pydantic-settings ``Settings()`` reads the ``.env`` in the working directory by default. On a
developer machine that file holds **real** keys/provider choices, which would leak into the
tests (e.g. a real ``ANTHROPIC_API_KEY`` breaks the "demo mode" test, ``BUDGET_USD=0`` zeroes
the budget). This autouse fixture disables ``.env`` loading for the whole session, so tests
rely only on the values they pass explicitly (and on the schema defaults).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_env_file() -> Iterator[None]:
    from argus.config.settings import Settings

    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None  # ignore the on-disk .env (test isolation)
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
