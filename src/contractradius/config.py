"""Configuration from an optional `.contractradius.yaml`. Every setting has a default."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contractradius.exceptions import ConfigError

DEFAULT_CONFIG_FILE = ".contractradius.yaml"
DEFAULT_CLIENT_NAMES = ["axios", "apiClient", "api", "client", "http", "httpClient"]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_names: list[str] = Field(default_factory=lambda: list(DEFAULT_CLIENT_NAMES))
    ignore_paths: list[str] = Field(default_factory=list)  # fnmatch on the spec path
    ignore_fields: list[str] = Field(default_factory=list)  # fnmatch on the dotted field
    min_confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    fail_on: Literal["breaking", "potentially_breaking", "none"] = "breaking"


def load_config(path: str | Path | None = None) -> Config:
    """Load config from `path` (default: ./.contractradius.yaml). Missing file -> defaults."""
    file = Path(path) if path is not None else Path(DEFAULT_CONFIG_FILE)
    if not file.is_file():
        return Config()

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {file}: {exc}") from exc

    if raw is None:  # empty file
        return Config()
    if not isinstance(raw, dict):
        raise ConfigError(f"{file} must contain a mapping of settings, got {type(raw).__name__}")

    try:
        return Config(**raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid settings in {file}: {problems}") from exc
