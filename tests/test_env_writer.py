"""`.env` writer (config.env_writer) tests — pure, offline, temporary directory."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from argus.config.env_writer import update_env_file


def test_add_new_key_to_empty(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    update_env_file(p, {"ANTHROPIC_API_KEY": "fake-key-new"})
    assert "ANTHROPIC_API_KEY=fake-key-new" in p.read_text(encoding="utf-8")


def test_replace_existing_key_no_duplicate(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("ANTHROPIC_API_KEY=old-value\nARGUS_MODEL_DEEP=opus\n", encoding="utf-8")
    update_env_file(p, {"ANTHROPIC_API_KEY": "new-value"})
    text = p.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=new-value" in text
    assert "old-value" not in text
    assert text.count("ANTHROPIC_API_KEY") == 1  # a single line


def test_preserves_unrelated_lines(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    original = (
        "# Secrets\n"
        "ARGUS_DASHBOARD_USERS=ada:scrypt$xx\n"
        "ARGUS_DASHBOARD_SESSION_SECRET=dummy\n"
        "\n"
        "ARGUS_REDIS_URL=redis://localhost:6379/0\n"
    )
    p.write_text(original, encoding="utf-8")
    update_env_file(p, {"ARGUS_MODEL_ROUTINE": "claude-sonnet-4-6"})
    text = p.read_text(encoding="utf-8")
    # The password/session secret and the comments are preserved untouched.
    assert "ARGUS_DASHBOARD_USERS=ada:scrypt$xx" in text
    assert "ARGUS_DASHBOARD_SESSION_SECRET=dummy" in text
    assert "# Secrets" in text
    assert "ARGUS_REDIS_URL=redis://localhost:6379/0" in text
    assert "ARGUS_MODEL_ROUTINE=claude-sonnet-4-6" in text


def test_multiple_updates(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    update_env_file(
        p,
        {
            "ANTHROPIC_API_KEY": "fake-key-1",
            "ARGUS_MODEL_DEEP": "claude-opus-4-8",
            "ARGUS_LLM_DAILY_BUDGET_USD": "5.0",
        },
    )
    text = p.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=fake-key-1" in text
    assert "ARGUS_MODEL_DEEP=claude-opus-4-8" in text
    assert "ARGUS_LLM_DAILY_BUDGET_USD=5.0" in text


def test_strips_newlines_in_value(tmp_path: Path) -> None:
    # Injection prevention: line breaks in a value are stripped (it stays on one line).
    p = tmp_path / ".env"
    update_env_file(p, {"ANTHROPIC_API_KEY": "fake\nINJECTED=bad"})
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "INJECTED" not in p.read_text(encoding="utf-8").split("ANTHROPIC_API_KEY=")[0]


def test_empty_updates_noop(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("X=1\n", encoding="utf-8")
    update_env_file(p, {})
    assert p.read_text(encoding="utf-8") == "X=1\n"  # unchanged


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
def test_new_file_permissions_0600(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    update_env_file(p, {"ANTHROPIC_API_KEY": "fake-key"})
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_secret_value_not_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / ".env"
    with caplog.at_level("DEBUG"):
        update_env_file(p, {"ANTHROPIC_API_KEY": "FAKE-TOP-SECRET-VALUE"})
    assert "FAKE-TOP-SECRET-VALUE" not in caplog.text  # the secret is never logged
