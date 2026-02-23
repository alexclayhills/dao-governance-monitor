"""Configuration loading from YAML with environment variable substitution."""

import os
import re
from pathlib import Path

import yaml

from .models import AppConfig


def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} patterns with environment variable values."""
    pattern = r"\$\{([^}]+)\}"

    def replacer(match):
        env_var = match.group(1)
        env_value = os.environ.get(env_var)
        if env_value is None:
            return ""  # Return empty string for unset optional vars
        return env_value

    return re.sub(pattern, replacer, value)


def _process_config_values(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _process_config_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_process_config_values(item) for item in obj]
    return obj


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load and validate configuration from YAML file.

    Supports ${ENV_VAR} syntax for environment variable substitution.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}. "
            f"Copy config.example.yaml to config.yaml and update it."
        )

    with open(path) as f:
        raw_config = yaml.safe_load(f)

    # Substitute environment variables
    processed = _process_config_values(raw_config)

    # Validate with Pydantic
    return AppConfig(**processed)
