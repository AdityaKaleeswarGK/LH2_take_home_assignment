from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stress_stack.config import Settings, load_settings, redact, save_settings
from stress_stack.errors import ToolingError
from stress_stack.openrouter import ModelError, OpenRouterClient, parse_json


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRESS_STACK_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def response(
    content: str = '{"ok": true}',
    *,
    finish: str = "stop",
    cost: float = 0.001,
    reasoning: int = 0,
) -> dict[str, Any]:
    return {
        "model": "test/model",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost": cost,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def client_with(tmp_path: Path, replies: list[tuple[int, Any]]) -> OpenRouterClient:
    client = OpenRouterClient(api_key="test-key", cache_dir=tmp_path / "cache")
    pending = list(replies)
    client._post = lambda payload: pending.pop(0)  # type: ignore[method-assign]
    return client


def test_key_is_never_written_into_the_cache(tmp_path: Path) -> None:
    """A cache entry doubles as the submitted transcript, so it must be clean."""
    client = client_with(tmp_path, [(200, response())])
    completion = client.complete([{"role": "user", "content": "hi"}])

    entry = (tmp_path / "cache" / f"{completion.cache_key}.json").read_text(encoding="utf-8")
    assert "test-key" not in entry
    assert "sk-or-v1" not in entry
    assert json.loads(entry)["request"]["messages"][0]["content"] == "hi"


def test_cache_replays_without_a_second_call(tmp_path: Path) -> None:
    client = client_with(tmp_path, [(200, response())])
    first = client.complete([{"role": "user", "content": "hi"}])
    second = client.complete([{"role": "user", "content": "hi"}])

    assert first.cached is False
    assert second.cached is True
    assert second.content == first.content
    assert client.usage.to_dict()["live_calls"] == 1


def test_cache_key_changes_with_prompt_version(tmp_path: Path) -> None:
    one = OpenRouterClient(api_key="k", cache_dir=tmp_path, prompt_version="1")
    two = OpenRouterClient(api_key="k", cache_dir=tmp_path, prompt_version="2")
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}

    assert one._cache_key(payload) != two._cache_key(payload)


def test_truncated_response_is_an_error_not_a_result(tmp_path: Path) -> None:
    """Reasoning models spend max_tokens before emitting content."""
    client = client_with(tmp_path, [(200, response(content="'", finish="length", reasoning=54))])

    with pytest.raises(ModelError, match="max_tokens"):
        client.complete([{"role": "user", "content": "hi"}])
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_retries_transient_status_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("stress_stack.openrouter.time.sleep", lambda _: None)
    client = client_with(
        tmp_path,
        [(429, {"error": {"message": "rate limited"}}), (200, response())],
    )
    completion = client.complete([{"role": "user", "content": "hi"}])

    assert completion.attempts == 2


def test_does_not_retry_a_client_error(tmp_path: Path) -> None:
    client = client_with(tmp_path, [(400, {"error": {"message": "bad request"}})])

    with pytest.raises(ModelError, match="400"):
        client.complete([{"role": "user", "content": "hi"}])


def test_missing_key_raises_before_any_network_call() -> None:
    client = OpenRouterClient(api_key=None)

    with pytest.raises(ToolingError, match="API key"):
        client.complete([{"role": "user", "content": "hi"}])


def test_usage_ledger_tracks_cost_and_truncations(tmp_path: Path) -> None:
    client = client_with(tmp_path, [(200, response(cost=0.0025))])
    client.complete([{"role": "user", "content": "hi"}])

    ledger = client.usage.to_dict()
    assert ledger["cost_usd"] == 0.0025
    assert ledger["truncations"] == 0
    assert ledger["by_model"] == {"test/model": 1}


def test_parses_json_through_fences_and_prose() -> None:
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go:\n{"a": 1}\nhope that helps') == {"a": 1}

    with pytest.raises(ModelError):
        parse_json("no json at all")
    with pytest.raises(ModelError, match="list"):
        parse_json("[1, 2, 3]")


def test_settings_never_expose_the_key() -> None:
    save_settings(api_key="sk-or-v1-abcdefghijklmnop")
    settings = load_settings()

    assert settings.configured is True
    assert "abcdefghijklmnop" not in json.dumps(settings.to_dict())


def test_config_file_is_owner_only() -> None:
    save_settings(api_key="sk-or-v1-abcdefghijklmnop")
    from stress_stack.config import config_path

    assert oct(config_path().stat().st_mode)[-3:] == "600"


def test_redaction_catches_keys_in_free_text() -> None:
    text = "failed with sk-or-v1-deadbeefcafe1234 in the header"
    assert "deadbeef" not in redact(text)


def test_role_lookup_rejects_an_unknown_role() -> None:
    from stress_stack.errors import InputError

    with pytest.raises(InputError, match="known roles"):
        Settings().model_for("nonexistent")
