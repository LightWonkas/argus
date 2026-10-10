"""CLI entry point tests."""

from __future__ import annotations

import pytest

from argus import __version__
from argus.__main__ import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_health_flag_returns_zero() -> None:
    assert main(["--health"]) == 0


def test_default_run_returns_zero() -> None:
    assert main([]) == 0


def test_dashboard_flag_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """--dashboard calls uvicorn.run with the configured host/port (no real server starts)."""
    import uvicorn

    calls: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert main(["--dashboard"]) == 0
    assert calls["host"] == "127.0.0.1"  # localhost by default (not publicly exposed)
    assert calls["port"] == 8787


def test_consumer_flag_runs_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """--consumer builds the bus via build_consumer and calls run() (no real Redis)."""
    import argus.__main__ as m

    ran = {"called": False}

    class FakeBus:
        def run(self) -> None:
            ran["called"] = True

    monkeypatch.setattr(m, "ConfigError", RuntimeError, raising=False)
    # Intercept build_consumer in the factory (no real Telegram/Redis is set up).
    import argus.interface.factory as factory

    monkeypatch.setattr(factory, "build_consumer", lambda settings=None: FakeBus())
    assert main(["--consumer"]) == 0
    assert ran["called"] is True


def test_consumer_flag_config_error_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.interface.factory as factory

    def boom(settings: object = None) -> object:
        raise ValueError("Telegram token required")

    monkeypatch.setattr(factory, "build_consumer", boom)
    assert main(["--consumer"]) == 1  # configuration error → exit code 1


def test_crypto_refresh_runs_public_cycle_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.interface.factory as factory
    from argus.core.schemas import DataStatus
    from argus.interface.crypto_runtime import CryptoCycleResult
    from argus.platform.quality import QualityDecision

    state = {"closed": False}

    class FakeCycle:
        def run_once(self) -> list[CryptoCycleResult]:
            return [
                CryptoCycleResult(
                    symbol="BTCUSDT",
                    data_status=DataStatus.OK,
                    quality_decision=QualityDecision.ACCEPT,
                )
            ]

        def close(self) -> None:
            state["closed"] = True

    monkeypatch.setattr(factory, "build_crypto_cycle", lambda settings=None: FakeCycle())
    assert main(["--crypto-refresh"]) == 0
    assert state["closed"] is True


def test_crypto_stream_runs_and_closes_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.interface.factory as factory

    state = {"ran": False, "closed": False}

    class FakeStream:
        async def run(self) -> None:
            state["ran"] = True

        def close(self) -> None:
            state["closed"] = True

    monkeypatch.setattr(factory, "build_crypto_stream", lambda settings=None: FakeStream())
    assert main(["--crypto-stream"]) == 0
    assert state == {"ran": True, "closed": True}
