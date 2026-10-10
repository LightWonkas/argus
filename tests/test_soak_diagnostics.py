"""Offline tests for the public soak measurement boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.public_news_soak import _allocation_owner, _measurement_capabilities


def test_allocation_owner_excludes_repo_local_virtualenv() -> None:
    root = Path.cwd()
    assert _allocation_owner(str(root / "src" / "argus" / "core" / "crypto.py")) == ("ARGUS_SOURCE")
    assert (
        _allocation_owner(
            str(root / ".venv" / "Lib" / "site-packages" / "websockets" / "protocol.py")
        )
        == "THIRD_PARTY"
    )
    assert _allocation_owner(str(root / "vendor" / "site-packages" / "x.py")) == ("THIRD_PARTY")
    assert _allocation_owner(str(Path(sys.base_prefix) / "Lib" / "asyncio" / "tasks.py")) == (
        "PYTHON_STDLIB"
    )


def test_only_deep_tracing_enables_tracemalloc() -> None:
    assert _measurement_capabilities("production") == {
        "gc_metrics": False,
        "tracemalloc": False,
        "allocation_snapshots": False,
    }
    assert _measurement_capabilities("diagnostic") == {
        "gc_metrics": True,
        "tracemalloc": False,
        "allocation_snapshots": False,
    }
    assert _measurement_capabilities("deep_tracing") == {
        "gc_metrics": True,
        "tracemalloc": True,
        "allocation_snapshots": True,
    }
