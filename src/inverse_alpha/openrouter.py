from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from inverse_alpha.config import OpenRouterSettings
from inverse_alpha.errors import EnrichmentError

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StructuredResponse:
    content: dict[str, Any]
    response_id: str | None
    resolved_model: str | None
    usage: dict[str, int]


class OpenRouterClient:
    def __init__(
        self,
        settings: OpenRouterSettings,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = 180.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.api_key:
            raise EnrichmentError(
                "OpenRouter enrichment requires OPENROUTER_API_KEY or a configured key"
            )
        self.settings = settings
        self._transport = transport or _urlopen_transport
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep

    def complete_structured(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 3000,
    ) -> StructuredResponse:
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "provider": {"require_parameters": True},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AdityaKaleeswarGK/LH2_take_home_assignment",
            "X-Title": "Inverse Alpha",
        }

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport(
                    f"{self.settings.endpoint}/chat/completions",
                    headers,
                    payload,
                    self._timeout_seconds,
                )
                return _parse_response(response)
            except OpenRouterRequestError as exc:
                last_error = exc
                if not exc.retryable or attempt == self._max_attempts:
                    break
                self._sleep(min(2 ** (attempt - 1), 4))
            except (OSError, TimeoutError) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                self._sleep(min(2 ** (attempt - 1), 4))
        if isinstance(last_error, OpenRouterRequestError):
            raise EnrichmentError(str(last_error)) from last_error
        raise EnrichmentError(
            f"OpenRouter request failed: {last_error}"
        ) from last_error


class OpenRouterRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.retryable = status == 429 or status >= 500
        super().__init__(f"OpenRouter returned HTTP {status}: {message}")


def _urlopen_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = _safe_error_message(body)
        raise OpenRouterRequestError(exc.code, message) from exc
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnrichmentError("OpenRouter returned a non-JSON response") from exc
    if not isinstance(value, dict):
        raise EnrichmentError("OpenRouter returned an unexpected response shape")
    return value


def _parse_response(response: dict[str, Any]) -> StructuredResponse:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        error = response.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise EnrichmentError(
            f"OpenRouter returned no completion choices{f': {message}' if message else ''}"
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    raw_content = message.get("content") if isinstance(message, dict) else None
    text = _content_text(raw_content)
    if not text.strip():
        raise EnrichmentError("OpenRouter returned an empty structured response")
    try:
        content = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise EnrichmentError("OpenRouter returned invalid structured JSON") from exc
    if not isinstance(content, dict):
        raise EnrichmentError("OpenRouter structured response must be a JSON object")

    usage_value = response.get("usage")
    usage: dict[str, int] = {}
    if isinstance(usage_value, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage_value.get(key)
            if isinstance(value, int):
                usage[key] = value
    response_id = response.get("id")
    resolved_model = response.get("model")
    return StructuredResponse(
        content=content,
        response_id=response_id if isinstance(response_id, str) else None,
        resolved_model=resolved_model if isinstance(resolved_model, str) else None,
        usage=usage,
    )


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines.pop(0)
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def _safe_error_message(body: str) -> str:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return "request failed"
    error = value.get("error") if isinstance(value, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.strip():
        return " ".join(message.split())[:300]
    return "request failed"
