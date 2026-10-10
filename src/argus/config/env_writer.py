"""`.env` writer — updates the secrets/keys managed from the settings page.

Sets/updates the given keys **without disturbing** the existing `.env`: comments, blank lines
and unrelated lines (password/session secret, Redis/DB URL) are kept as-is. Atomic write
(`tempfile` + `os.replace`) → no partial/corrupt file. **Secret values are NEVER logged** (key
names only). `path` is injected → testable offline as pure logic.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from argus.observability.logging import get_logger

_log = get_logger("argus.config.env")


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Updates keys in the `.env` file (appends missing ones); keeps every other line.

    Args:
        path: `.env` path (created if missing, mode ``0o600``).
        updates: ``{KEY: value}`` — only these are set/updated.

    Line endings (``\\n``/``\\r``) in values are stripped for safety (injection prevention).
    """
    if not updates:
        return

    clean = {k: v.replace("\n", " ").replace("\r", " ") for k, v in updates.items()}
    remaining = dict(clean)

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        # Keep comment/blank/non-`KEY=` lines as-is.
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    # Append new keys that are not in the file yet (in input order).
    for key, value in clean.items():
        if key in remaining:
            out.append(f"{key}={value}")
            remaining.pop(key)

    text = "\n".join(out) + "\n"
    _atomic_write(path, text)
    _log.info("env-updated", keys=sorted(clean))  # key names only — NO values


def _atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir → ``os.replace`` (atomic); new file mode ``0o600``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        # Failure: remove the temp file, leave the target untouched (old content survives).
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
