"""Validated YAML configuration for Music Manager."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import yaml


DEFAULT_CONFIG_FILENAME = "music-manager.yml"
PATH_MODES = {"absolute", "relative"}


@dataclass(frozen=True)
class AppConfig:
    """Settings currently supported by scanning and analysis."""

    path_mode: str = "relative"
    ignore: Tuple[str, ...] = (".DS_Store",)


def default_config_path() -> Path:
    """Return the optional config path in the current working directory."""
    return Path.cwd() / DEFAULT_CONFIG_FILENAME


def _validate_mapping(data: Any) -> Mapping[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("configuration must contain a YAML mapping")
    return data


def load_config(path: Optional[Path] = None) -> AppConfig:
    """Load configuration, using safe defaults when the default file is absent."""
    config_path = path.expanduser() if path is not None else default_config_path()
    if not config_path.exists():
        if path is not None:
            raise ValueError(f"configuration file does not exist: {config_path}")
        return AppConfig()
    if not config_path.is_file():
        raise ValueError(f"configuration path is not a file: {config_path}")

    try:
        with config_path.open(encoding="utf-8") as config_file:
            data = _validate_mapping(yaml.safe_load(config_file))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML in {config_path}: {error}") from error

    path_mode = data.get("path_mode", "relative")
    if not isinstance(path_mode, str) or path_mode not in PATH_MODES:
        choices = ", ".join(sorted(PATH_MODES))
        raise ValueError(f"path_mode must be one of: {choices}")

    ignore = data.get("ignore", list(AppConfig().ignore))
    if not isinstance(ignore, list) or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in ignore
    ):
        raise ValueError("ignore must be a list of non-empty path patterns")

    return AppConfig(
        path_mode=path_mode,
        ignore=tuple(pattern.strip() for pattern in ignore),
    )
