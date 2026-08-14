from __future__ import annotations

import io
import json
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import run_git

from inverse_alpha.cli import main
from inverse_alpha.config import OpenRouterSettings, config_path
from inverse_alpha.errors import InputError
from inverse_alpha.knowledge import build_knowledge
from inverse_alpha.openrouter import OpenRouterClient, StructuredResponse


class FakeStructuredClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def complete_structured(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 3000,
    ) -> StructuredResponse:
        del schema, system_prompt, max_tokens
        with self._lock:
            self.calls.append(schema_name)
        prompt = json.loads(user_prompt)
        if schema_name == "inverse_alpha_file_knowledge":
            path = prompt["path"]
            tests = prompt["discovered_test_cases"]
            content = {
                "summary": f"Source-grounded summary for {path}.",
                "responsibilities": [f"Defines behavior found in {path}."],
                "key_behaviors": ["Uses the supplied symbols and imports."],
                "domain_concepts": ["sample behavior"],
                "test_intents": (
                    [f"Exercises {item['name']}." for item in tests] if tests else []
                ),
                "limitations": [
                    "Static source analysis does not prove runtime coverage."
                ],
            }
        else:
            file_analyses = prompt["file_analyses"]
            production = [
                item["path"]
                for item in file_analyses
                if item["classification"] == "source"
            ]
            tests = [
                item["path"]
                for item in file_analyses
                if item["classification"] == "test"
            ]
            content = {
                "repository_summary": "A sample worker repository with source-grounded behavior.",
                "architecture_overview": "Source modules provide behavior consumed by test modules.",
                "capabilities": [
                    {
                        "name": "Worker execution",
                        "description": "Runs and normalizes worker values.",
                        "implementation_paths": production[:1],
                        "related_test_paths": tests[:1],
                    }
                ],
                "workflows": [
                    {
                        "name": "Worker flow",
                        "description": "A caller enters through the worker module.",
                        "path_sequence": (production + tests)[:2],
                    }
                ],
                "testing_strategy": "Tests instantiate public behavior and assert results.",
                "testing_strengths": ["Public worker construction is exercised."],
                "testing_gaps": [
                    {
                        "area": "Error behavior",
                        "reason": "No static test mapping demonstrates failure handling.",
                        "implementation_paths": production[:1],
                        "related_test_paths": [],
                        "confidence": "low",
                    }
                ],
                "navigation": [
                    {
                        "question": "Change worker behavior",
                        "start_paths": production[:1],
                        "rationale": "This file contains the implementation symbols.",
                    }
                ],
                "important_notes": ["Semantic claims remain unverified annotations."],
            }
        return StructuredResponse(
            content=content,
            response_id="fake-response",
            resolved_model="fake/model",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


@pytest.fixture
def enrichment_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "semantic-project"
    (repository / "sample").mkdir(parents=True)
    (repository / "tests").mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Inverse Alpha Test")
    run_git(repository, "config", "user.email", "inverse-alpha@example.test")
    (repository / "README.md").write_text(
        "# Semantic Project\n\nA small worker service.\n", encoding="utf-8"
    )
    (repository / "sample" / "worker.py").write_text(
        "def run(value):\n    return value * 2\n", encoding="utf-8"
    )
    (repository / "tests" / "test_worker.py").write_text(
        "from sample.worker import run\n\n\ndef test_run():\n    assert run(2) == 4\n",
        encoding="utf-8",
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Add semantic fixture")
    return repository


def test_openrouter_enriches_blueprint_and_test_map(
    enrichment_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    client = FakeStructuredClient()

    first = build_knowledge(
        str(enrichment_repository),
        enrichment_mode="openrouter",
        openrouter_client=client,
    )

    assert first.enrichment_status == "available"
    assert first.enrichment_provider == "openrouter"
    assert first.annotation_count == first.file_count + 1
    assert client.calls.count("inverse_alpha_file_knowledge") == first.file_count
    assert client.calls.count("inverse_alpha_repository_knowledge") == 1
    annotations_text = (first.knowledge_root / "annotations.jsonl").read_text(
        encoding="utf-8"
    )
    assert "test-openrouter-key" not in annotations_text
    semantic = json.loads(
        (first.knowledge_root / "semantic_context.json").read_text(encoding="utf-8")
    )
    assert semantic["status"] == "available"
    assert semantic["repository"]["capabilities"][0]["name"] == "Worker execution"
    blueprint = (first.knowledge_root / "blueprint.md").read_text(encoding="utf-8")
    assert "## Agent Navigation Guide" in blueprint
    assert "## Deterministic Structural Evidence" in blueprint
    test_map = (first.knowledge_root / "test_map.md").read_text(encoding="utf-8")
    assert "## Candidate Testing Gaps" in test_map
    assert "## Deterministic Test Evidence" in test_map
    validation = json.loads(
        (first.knowledge_root / "validation.json").read_text(encoding="utf-8")
    )
    assert validation["enrichment"]["checks"]["known_path_references"] is True

    calls_after_first = len(client.calls)
    second = build_knowledge(
        str(enrichment_repository),
        enrichment_mode="openrouter",
        openrouter_client=client,
    )
    assert second.action == "reused"
    assert len(client.calls) == calls_after_first


def test_explicit_openrouter_requires_key(enrichment_repository: Path) -> None:
    with pytest.raises(InputError, match="no API key"):
        build_knowledge(str(enrichment_repository), enrichment_mode="openrouter")


def test_config_reads_key_from_stdin_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "test-secret-openrouter-key"
    monkeypatch.setattr("sys.stdin", io.StringIO(key + "\n"))

    assert main(["config", "--api-key-stdin", "--model", "example/model"]) == 0

    output = capsys.readouterr().out
    assert key not in output
    assert "API key: configured" in output
    stored = json.loads(config_path().read_text(encoding="utf-8"))
    assert stored["openrouter"]["api_key"] == key
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600


def test_openrouter_client_requests_strict_structured_output() -> None:
    captured: dict[str, Any] = {}

    def transport(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        captured.update(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        return {
            "id": "response-1",
            "model": "resolved/model",
            "choices": [{"message": {"content": '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    settings = OpenRouterSettings(
        "https://openrouter.example/api/v1",
        "requested/model",
        "private-key",
        "test",
    )
    client = OpenRouterClient(settings, transport=transport)
    response = client.complete_structured(
        schema_name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        system_prompt="system",
        user_prompt="user",
    )

    assert response.content == {"answer": "ok"}
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer private-key"
    response_format = captured["payload"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert captured["payload"]["provider"]["require_parameters"] is True
