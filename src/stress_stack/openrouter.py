"""OpenRouter client.

OpenRouter speaks the OpenAI chat-completions wire format, so no SDK is needed —
``urllib`` is enough and the package keeps zero runtime dependencies, exactly as
the GitHub client already does.

Two properties matter more than throughput:

* **The cache is the determinism mechanism.** ``temperature=0`` does not
  guarantee identical output across time or provider routing, so reproducibility
  comes from keying every call on a content hash and replaying the stored
  response. A re-run costs nothing and produces byte-identical artifacts.
* **The cache is also the transcript.** Each entry stores the full request and
  response, which is what the brief asks for when it requires key prompts or
  session logs — not merely a hash of them.

The API key is never written into a cache entry, a log line, or an error.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.config import Settings, load_settings, redact, resolve_api_key
from stress_stack.errors import StressStackError, ToolingError

PROMPT_VERSION = "1"
_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class ModelError(StressStackError):
    """A model call failed in a way the caller may want to handle."""


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    model: str
    cached: bool
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float
    finish_reason: str | None
    cache_key: str
    attempts: int = 1
    cost: float = 0.0
    reasoning_tokens: int = 0
    tool_calls: tuple[dict[str, Any], ...] = ()
    """Tools the model asked to run, in the order it asked for them."""

    @property
    def truncated(self) -> bool:
        """A length-capped response is a failure, not a short answer.

        Reasoning models spend `max_tokens` on hidden reasoning before emitting
        any content, so an under-budgeted call returns a fragment that still
        looks like a successful 200.
        """
        return self.finish_reason == "length"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "cached": self.cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_seconds": self.latency_seconds,
            "finish_reason": self.finish_reason,
            "cache_key": self.cache_key,
            "attempts": self.attempts,
            "cost": self.cost,
            "reasoning_tokens": self.reasoning_tokens,
            "truncated": self.truncated,
            "tool_calls": [call.get("name") for call in self.tool_calls],
        }


@dataclass
class UsageLedger:
    """Running totals, so cost is observable rather than discovered on a bill."""

    calls: int = 0
    cached: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cost: float = 0.0
    truncations: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    # Call counts alone could not show that one model was ten times slower than
    # the role expected; seconds per model is the field that would have.
    seconds_by_model: dict[str, float] = field(default_factory=dict)
    live_calls_by_model: dict[str, int] = field(default_factory=dict)

    def record(self, completion: Completion) -> None:
        self.calls += 1
        if completion.cached:
            self.cached += 1
        self.prompt_tokens += completion.prompt_tokens
        self.completion_tokens += completion.completion_tokens
        self.seconds += completion.latency_seconds
        self.cost += completion.cost
        self.truncations += int(completion.truncated)
        model = completion.model
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.seconds_by_model[model] = round(
            self.seconds_by_model.get(model, 0.0) + completion.latency_seconds, 3
        )
        if not completion.cached:
            self.live_calls_by_model[model] = self.live_calls_by_model.get(model, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cached,
            "live_calls": self.calls - self.cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "seconds": round(self.seconds, 2),
            "cost_usd": round(self.cost, 6),
            "truncations": self.truncations,
            "by_model": dict(sorted(self.by_model.items())),
            "seconds_by_model": dict(sorted(self.seconds_by_model.items())),
            "live_calls_by_model": dict(sorted(self.live_calls_by_model.items())),
            "mean_live_seconds_by_model": {
                model: round(self.seconds_by_model.get(model, 0.0) / live, 2)
                for model, live in sorted(self.live_calls_by_model.items())
                if live
            },
        }


class OpenRouterClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        timeout: float = 180.0,
        # Total wall clock across every attempt of one call. Five minutes is
        # long enough to ride out a rate limit and short enough that a stuck
        # call surfaces as an error rather than as a stalled stage.
        retry_deadline: float = 300.0,
        max_attempts: int = 5,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.settings = settings or load_settings()
        self._api_key = api_key or resolve_api_key()
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retry_deadline = retry_deadline
        self.max_attempts = max_attempts
        self.prompt_version = prompt_version
        self.usage = UsageLedger()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        role: str = "worker",
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        use_cache: bool = True,
    ) -> Completion:
        resolved_model = model or self.settings.model_for(role)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }

        key = self._cache_key(payload)
        if use_cache:
            cached = self._read_cache(key)
            if cached is not None:
                self.usage.record(cached)
                return cached

        if not self._api_key:
            raise ToolingError(
                "No OpenRouter API key configured. Set OPENROUTER_API_KEY or run "
                "`stress-stack model --set-key`."
            )

        completion = self._request_with_retry(payload, key)
        if completion.truncated:
            raise ModelError(
                f"{resolved_model} hit max_tokens before finishing "
                f"({completion.completion_tokens} completion tokens, "
                f"{completion.reasoning_tokens} of them reasoning). Raise max_tokens."
            )
        if use_cache:
            self._write_cache(key, payload, completion)
        self.usage.record(completion)
        return completion

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], Completion]:
        """Return parsed JSON, tolerating models that wrap it in a fence."""
        completion = self.complete(messages, json_schema=schema, **kwargs)
        return parse_json(completion.content), completion

    def converse(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        run_tool: Callable[[str, dict[str, Any]], str],
        max_turns: int = 12,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[Completion]]:
        """Let the model explore with tools, and return the whole exchange.

        Determinism survives this. Each turn is cached on the hash of the
        payload, and the payload contains every message accumulated so far, so a
        replay walks the identical sequence of keys and never reaches the
        network. That only holds while the tools are pure reads of a fixed tree —
        a tool that observed changing state would make the second turn's input
        differ from the first run's and the chain would diverge.

        The loop is bounded. A model that keeps asking for tools is stopped and
        the transcript says so, rather than spending until a budget runs out.
        """
        conversation = list(messages)
        completions: list[Completion] = []
        for _ in range(max_turns):
            completion = self.complete(conversation, tools=tools, **kwargs)
            completions.append(completion)
            if not completion.tool_calls:
                return conversation, completions
            conversation.append(
                {
                    "role": "assistant",
                    "content": completion.content or None,
                    "tool_calls": [_wire_tool_call(call) for call in completion.tool_calls],
                }
            )
            for call in completion.tool_calls:
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": _bounded(run_tool(call["name"], call["arguments"])),
                    }
                )
        conversation.append(
            {
                "role": "user",
                "content": (
                    f"You have used the {max_turns}-turn exploration budget. "
                    "Answer now from what you already know."
                ),
            }
        )
        return conversation, completions

    def _request_with_retry(self, payload: dict[str, Any], key: str) -> Completion:
        """Retry with full jitter, under a deadline the caller can survive.

        Two properties this lacked, both of which cost a pool slot rather than a
        call. Backoff without jitter makes a pool of workers that hit the same
        rate limit retry in lockstep, so they collide again on every attempt.
        And an unbounded retry budget on top of a 180s request timeout means one
        hung call could hold a slot for roughly fifteen minutes — long enough
        that the stage looks stalled rather than slow.
        """
        delay = 1.0
        last_error = ""
        began = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            status, body = self._post(payload)
            elapsed = time.monotonic() - started

            if status == 200:
                return self._completion_from(body, payload, key, elapsed, attempt)

            last_error = redact(_error_message(body) or f"HTTP {status}")
            spent = time.monotonic() - began
            if (
                status not in _RETRY_STATUS
                or attempt == self.max_attempts
                or spent >= self.retry_deadline
            ):
                reason = (
                    f" after {spent:.0f}s"
                    if spent >= self.retry_deadline
                    else ""
                )
                raise ModelError(
                    f"OpenRouter call failed ({status}) for {payload['model']}"
                    f"{reason}: {last_error}"
                )
            # Full jitter: sleep anywhere in [0, delay). Sleeping exactly `delay`
            # keeps a pool synchronised on the limit that rejected it.
            remaining = self.retry_deadline - spent
            time.sleep(min(random.uniform(0.0, delay), max(remaining, 0.0)))
            delay = min(delay * 2, 30.0)
        raise ModelError(f"OpenRouter call exhausted retries: {last_error}")

    def _post(self, payload: dict[str, Any]) -> tuple[int, Any]:
        request = urllib.request.Request(
            url=self.settings.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/stress-stack",
                "X-Title": "stress-stack",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                return exc.code, {"error": {"message": str(exc.reason)}}
        except urllib.error.URLError as exc:
            return 0, {"error": {"message": f"network: {exc.reason}"}}
        except (TimeoutError, json.JSONDecodeError) as exc:
            return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}

    def _completion_from(
        self, body: Any, payload: dict[str, Any], key: str, elapsed: float, attempts: int
    ) -> Completion:
        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices:
            raise ModelError(
                f"OpenRouter returned no choices for {payload['model']}: "
                f"{redact(json.dumps(body)[:300])}"
            )
        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        tool_calls = _tool_calls_from(message)
        # Some providers surface the answer only on `reasoning` when content is
        # empty. A turn that asked for tools is legitimately contentless, so the
        # fallback must not fire there or the reasoning trace becomes the answer.
        content = message.get("content") or ""
        if not tool_calls and not str(content).strip() and message.get("reasoning"):
            content = message["reasoning"]
        return Completion(
            content=str(content),
            model=str(body.get("model") or payload["model"]),
            cached=False,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_seconds=round(elapsed, 3),
            finish_reason=choices[0].get("finish_reason"),
            cache_key=key,
            attempts=attempts,
            cost=float(usage.get("cost") or 0.0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            tool_calls=tool_calls,
        )

    def _cache_key(self, payload: dict[str, Any]) -> str:
        material = json.dumps(
            {"prompt_version": self.prompt_version, "payload": payload},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _cache_file(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Completion | None:
        path = self._cache_file(key)
        if path is None or not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        response = entry.get("response") or {}
        usage = entry.get("usage") or {}
        return Completion(
            content=str(response.get("content") or ""),
            model=str(entry.get("model") or ""),
            cached=True,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_seconds=0.0,
            finish_reason=response.get("finish_reason"),
            cache_key=key,
            tool_calls=tuple(response.get("tool_calls") or ()),
        )

    def _write_cache(
        self, key: str, payload: dict[str, Any], completion: Completion
    ) -> None:
        path = self._cache_file(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "cache_key": key,
            "prompt_version": self.prompt_version,
            "model": completion.model,
            "request": payload,
            "response": {
                "content": completion.content,
                "finish_reason": completion.finish_reason,
                "tool_calls": [dict(call) for call in completion.tool_calls],
            },
            "usage": {
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
            },
            "latency_seconds": completion.latency_seconds,
        }
        path.write_text(
            redact(json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False)) + "\n",
            encoding="utf-8",
        )


_TOOL_RESULT_BUDGET = 8000


def _bounded(text: str) -> str:
    """Cap one tool result so a large file cannot crowd out the conversation."""
    if len(text) <= _TOOL_RESULT_BUDGET:
        return text
    return text[:_TOOL_RESULT_BUDGET] + f"\n... truncated at {_TOOL_RESULT_BUDGET} characters ..."


def _tool_calls_from(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Normalise the wire format into name/arguments, arguments already parsed.

    Providers send ``arguments`` as a JSON *string*. A model occasionally emits
    something that is not valid JSON there; that is a malformed request rather
    than a crash, so it becomes an empty argument set and the tool reports the
    problem back to the model.
    """
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        calls.append(
            {
                "id": str(raw.get("id") or f"call_{index}"),
                "name": str(function.get("name") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return tuple(calls)


def _wire_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """The inverse of :func:`_tool_calls_from`, for echoing the turn back."""
    return {
        "id": call["id"],
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": json.dumps(call["arguments"], sort_keys=True),
        },
    }


def parse_json(content: str) -> dict[str, Any]:
    """Parse a model's JSON, tolerating code fences and surrounding prose."""
    text = (content or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ModelError(f"Model did not return JSON: {text[:200]!r}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelError(f"Model returned malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelError(f"Model returned {type(parsed).__name__}, expected an object")
    return parsed


def _error_message(body: Any) -> str | None:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if body.get("message"):
            return str(body["message"])
    return None
