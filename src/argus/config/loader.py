"""YAML "data" config loader — the basis for hot-reload.

Validates ``config/config.yaml`` into a :class:`~argus.config.models.RuntimeConfig`.
When that file does not exist, the shipped ``config/config.example.yaml`` is read instead.
``reload_if_changed()`` re-reads the file when it changes (mtime); a full file
watcher / Telegram integration can build on top of it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from argus.config.models import RuntimeConfig
from argus.core.errors import ConfigError

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_CONFIG_FILE = "config.yaml"
EXAMPLE_CONFIG_FILE = "config.example.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    """Reads a YAML file into a dict (empty file → {})."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read config file: {path}") from exc
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


class ConfigLoader:
    """Loads the typed runtime config and re-reads it on change."""

    def __init__(
        self,
        config_dir: Path | str = DEFAULT_CONFIG_DIR,
        *,
        filename: str = DEFAULT_CONFIG_FILE,
    ) -> None:
        self._path = Path(config_dir) / filename
        self._config: RuntimeConfig | None = None
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def source_path(self) -> Path:
        """File that is actually read: ``config.yaml``, else the shipped example next to it."""
        if self._path.exists():
            return self._path
        example = self._path.with_name(EXAMPLE_CONFIG_FILE)
        return example if example.exists() else self._path

    def load(self) -> RuntimeConfig:
        """Reads and validates the config from disk (re-reads on every call)."""
        source = self.source_path
        if not source.exists():
            raise ConfigError(f"Config file not found: {self._path}")
        data = _read_yaml(source)
        try:
            config = RuntimeConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"Invalid config ({source}):\n{exc}") from exc
        self._config = config
        self._mtime = source.stat().st_mtime
        return config

    def get(self) -> RuntimeConfig:
        """Returns the cached config; loads it if missing."""
        if self._config is None:
            return self.load()
        return self._config

    def reload_if_changed(self) -> bool:
        """Reloads if the file changed. Returns ``True`` when a reload happened."""
        source = self.source_path
        if not source.exists():
            return False
        current_mtime = source.stat().st_mtime
        if self._mtime is None or current_mtime != self._mtime:
            self.load()
            return True
        return False

    def save(self, config: RuntimeConfig) -> None:
        """Writes the config to YAML **atomically** and refreshes cache/mtime (settings page).

        Takes a valid :class:`RuntimeConfig` (write endpoints validate with pydantic). Atomic
        write (temp + ``os.replace``) → no partial/corrupt file; on failure the old file stays.
        Note: YAML comments are not preserved (only data is written).
        """
        data = config.model_dump(mode="json")
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".config.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, self._path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        self.load()  # refresh _config + _mtime
