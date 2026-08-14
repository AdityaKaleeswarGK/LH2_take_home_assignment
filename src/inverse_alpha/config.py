from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inverse_alpha.errors import InputError, MetadataError

DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-flash-lite-preview"


@dataclass(frozen=True, slots=True)
class OpenRouterSettings:
    endpoint: str
    model: str
    api_key: str | None
    key_source: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> dict[str, str | bool | None]:
        return {
            "provider": "openrouter",
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key_configured": self.configured,
            "key_source": self.key_source,
        }


def config_directory() -> Path:
    override = os.environ.get("INVERSE_ALPHA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "inverse-alpha"


def config_path() -> Path:
    return config_directory() / "config.json"


def load_openrouter_settings(
    *, model_override: str | None = None
) -> OpenRouterSettings:
    data = _read_config()
    provider = data.get("openrouter")
    if not isinstance(provider, dict):
        provider = {}

    environment_key = os.environ.get("OPENROUTER_API_KEY")
    stored_key = provider.get("api_key")
    api_key = environment_key or (stored_key if isinstance(stored_key, str) else None)
    key_source = "environment" if environment_key else ("config" if api_key else None)

    endpoint_value = os.environ.get("OPENROUTER_BASE_URL") or provider.get("endpoint")
    endpoint = (
        endpoint_value.strip().rstrip("/")
        if isinstance(endpoint_value, str) and endpoint_value.strip()
        else DEFAULT_OPENROUTER_ENDPOINT
    )
    model_value = (
        model_override or os.environ.get("OPENROUTER_MODEL") or provider.get("model")
    )
    model = (
        model_value.strip()
        if isinstance(model_value, str) and model_value.strip()
        else DEFAULT_OPENROUTER_MODEL
    )
    return OpenRouterSettings(endpoint, model, api_key, key_source)


def save_openrouter_settings(
    *,
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    clear_key: bool = False,
) -> OpenRouterSettings:
    data = _read_config()
    provider = data.setdefault("openrouter", {})
    if not isinstance(provider, dict):
        provider = {}
        data["openrouter"] = provider

    if clear_key:
        provider.pop("api_key", None)
    elif api_key is not None:
        cleaned_key = api_key.strip()
        if not cleaned_key:
            raise InputError("The OpenRouter API key cannot be empty")
        provider["api_key"] = cleaned_key
    if model is not None:
        cleaned_model = model.strip()
        if not cleaned_model:
            raise InputError("The OpenRouter model cannot be empty")
        provider["model"] = cleaned_model
    if endpoint is not None:
        cleaned_endpoint = endpoint.strip().rstrip("/")
        if not cleaned_endpoint.startswith("https://"):
            raise InputError("The OpenRouter endpoint must use HTTPS")
        provider["endpoint"] = cleaned_endpoint

    data["schema_version"] = "0.2.0"
    destination = config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent, prefix=".config.", suffix=".tmp", text=True
        )
        temporary = Path(name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise MetadataError(
            f"Could not save Inverse Alpha configuration: {exc}"
        ) from exc
    return load_openrouter_settings()


def _read_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError(
            f"Could not read Inverse Alpha configuration: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise MetadataError("Inverse Alpha configuration must contain a JSON object")
    return value
