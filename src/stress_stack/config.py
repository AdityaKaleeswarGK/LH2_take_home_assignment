"""Credential and model configuration, stored outside any analysed repository.

The API key never enters a repository, an artifact, a cache entry, or a log. It
lives in a user-scoped config file with owner-only permissions, and can always
be overridden by an environment variable for CI use.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.errors import InputError

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Roles map to what a call actually needs, not to a vendor. Swapping a model is
# a config change; nothing downstream names a provider.
# Chosen from a measured probe over one real glom file card, not from parameter
# count. meta/muse-glimmer-30b was the *worst* option on every axis — 67s and
# $0.0086 per call, versus 4.7s and $0.00053 for the worker model below —
# because it spends thousands of billed reasoning tokens on a description task.
DEFAULT_MODELS: dict[str, str] = {
    "worker": "openai/gpt-5.6-luna",
    "synthesis": "google/gemini-3.7-flash",
    "reasoning": "openai/gpt-5.6-luna-pro",
}

KNOWN_MODELS: tuple[str, ...] = (
    "openai/gpt-5.6-luna",
    "openai/gpt-5.6-luna-pro",
    "meta/muse-glimmer-30b",
    "z-ai/glm-5.2",
    "google/gemini-3.7-flash",
)


@dataclass(frozen=True, slots=True)
class Settings:
    endpoint: str = ENDPOINT
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    key_source: str | None = None
    configured: bool = False

    def model_for(self, role: str) -> str:
        if role in self.models:
            return self.models[role]
        raise InputError(
            f"No model configured for role {role!r}; known roles: {sorted(self.models)}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Safe to persist or print — never contains the key."""
        return {
            "endpoint": self.endpoint,
            "models": dict(sorted(self.models.items())),
            "configured": self.configured,
            "key_source": self.key_source,
        }


def config_directory() -> Path:
    override = os.environ.get("STRESS_STACK_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "stress-stack"


def config_path() -> Path:
    return config_directory() / "config.json"


def load_settings() -> Settings:
    stored: dict[str, Any] = {}
    path = config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError):
            stored = {}

    models = dict(DEFAULT_MODELS)
    configured_models = stored.get("models")
    if isinstance(configured_models, dict):
        models.update(
            {str(k): str(v) for k, v in configured_models.items() if isinstance(v, str)}
        )

    environment_key = os.environ.get("OPENROUTER_API_KEY")
    if environment_key:
        source: str | None = "environment"
    elif isinstance(stored.get("api_key"), str) and stored["api_key"]:
        source = str(path)
    else:
        source = None

    return Settings(
        endpoint=str(stored.get("endpoint") or ENDPOINT),
        models=models,
        key_source=source,
        configured=source is not None,
    )


def resolve_api_key() -> str | None:
    """Environment first, then the user config file. Never a repository file."""
    environment_key = os.environ.get("OPENROUTER_API_KEY")
    if environment_key:
        return environment_key.strip()
    path = config_path()
    if not path.is_file():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    key = stored.get("api_key") if isinstance(stored, dict) else None
    return key.strip() if isinstance(key, str) and key.strip() else None


def save_settings(
    *,
    api_key: str | None = None,
    clear_key: bool = False,
    endpoint: str | None = None,
    models: dict[str, str] | None = None,
) -> Settings:
    directory = config_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()

    stored: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError):
            stored = {}

    if clear_key:
        stored.pop("api_key", None)
    elif api_key:
        stored["api_key"] = api_key.strip()
    if endpoint:
        stored["endpoint"] = endpoint
    if models:
        merged = dict(stored.get("models") or {})
        merged.update(models)
        stored["models"] = merged

    path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    directory.chmod(stat.S_IRWXU)
    return load_settings()


def redact(text: str) -> str:
    """Strip anything shaped like a key before it reaches a log or an artifact."""
    key = resolve_api_key()
    cleaned = text
    if key:
        cleaned = cleaned.replace(key, "***redacted***")
    import re

    return re.sub(r"sk-or-v1-[A-Za-z0-9]{8,}", "***redacted***", cleaned)
