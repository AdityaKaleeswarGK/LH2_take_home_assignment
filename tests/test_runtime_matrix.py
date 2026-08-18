"""Eras, and the images they are judged in.

What a runtime *should* be is now worked out by an agent and covered by
``test_environment_agent``. What survives here is everything around it: how the
pool is grouped, how many images that implies, and what happens when a build or
a proposal fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_stack import runtime_matrix as rm
from stress_stack.environment_agent import Proposal
from stress_stack.runner import forget_source_roots
from stress_stack.runtime_matrix import (
    Era,
    HeadRuntime,
    RuntimeImages,
    RuntimeSpec,
    era_count,
    plan_eras,
    render_dockerfile,
    shadowed_distributions,
)

HEAD_PY = HeadRuntime(image="stress-stack/repo:verify", language="python", toolchain_version="3.12")


@pytest.fixture(autouse=True)
def _clean_cache():
    forget_source_roots()
    yield
    forget_source_roots()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The shadow: a fact about the harness's mount, not about the repository
# --------------------------------------------------------------------------


def pluggy_tree(tmp_path: Path, *, src_layout: bool = False) -> Path:
    if src_layout:
        write(tmp_path / "src" / "pluggy" / "__init__.py", "")
    else:
        write(tmp_path / "pluggy.py", "")
    write(tmp_path / "setup.py", 'setup(name="pluggy")\n')
    return tmp_path


@pytest.mark.parametrize("src_layout", [False, True])
def test_a_repository_that_is_a_runner_dependency_is_detected(
    tmp_path: Path, src_layout: bool
) -> None:
    """Both of pluggy's historical layouts shadow the copy pytest is running on."""
    assert shadowed_distributions(pluggy_tree(tmp_path, src_layout=src_layout)) == ("pluggy",)


def test_an_ordinary_repository_shadows_nothing(tmp_path: Path) -> None:
    write(tmp_path / "src" / "glom" / "__init__.py", "")
    write(tmp_path / "tests" / "test_glom.py", "")

    assert shadowed_distributions(tmp_path) == ()


# --------------------------------------------------------------------------
# Eras: how many environments this pool needs, known before staging
# --------------------------------------------------------------------------


class _FakeCandidate:
    def __init__(self, candidate_id: str, base_sha: str | None) -> None:
        self.candidate_id = candidate_id
        self.signals = {"base_sha": base_sha} if base_sha else {}


class _FakeRepository:
    """Answers `git describe` from a table, and records what it was asked."""

    def __init__(self, tags: dict[str, str]) -> None:
        self.tags = tags
        self.asked: list[str] = []

    def run(self, arguments, record=True):
        revision = arguments[-1]
        self.asked.append(revision)
        if revision not in self.tags:
            raise RuntimeError("no tag reachable")
        return self.tags[revision] + "\n"


def test_candidates_in_one_release_window_share_an_era() -> None:
    repository = _FakeRepository({"a": "1.5.0", "b": "1.5.0", "c": "1.6.0"})
    candidates = [
        _FakeCandidate("pr-1", "a"),
        _FakeCandidate("pr-2", "b"),
        _FakeCandidate("pr-3", "c"),
    ]

    eras = plan_eras(repository, candidates)

    assert eras["pr-1"].key == eras["pr-2"].key == "1.5.0"
    assert eras["pr-3"].key == "1.6.0"
    assert era_count(eras) == 2


def test_describe_is_asked_once_per_distinct_commit() -> None:
    """Planning is cheap by construction — one git call per base, not per candidate."""
    repository = _FakeRepository({"a": "1.5.0"})
    plan_eras(repository, [_FakeCandidate(f"pr-{i}", "a") for i in range(20)])

    assert repository.asked == ["a"]


def test_excision_candidates_belong_to_heads_era() -> None:
    """They are cut from HEAD, which the container stage already built and proved."""
    repository = _FakeRepository({})
    eras = plan_eras(
        repository, [_FakeCandidate("excise-a", None), _FakeCandidate("excise-b", None)]
    )

    assert {e.key for e in eras.values()} == {"HEAD"}
    assert era_count(eras) == 1
    assert repository.asked == []


def test_an_undescribable_commit_is_its_own_era_not_a_crash() -> None:
    repository = _FakeRepository({})
    eras = plan_eras(
        repository,
        [_FakeCandidate("pr-1", "aaaaaaaaaaaaaaaa"), _FakeCandidate("pr-2", "bbbbbbbbbbbbbbbb")],
    )

    assert era_count(eras) == 2
    assert eras["pr-1"].key == "aaaaaaaaaaaa"


def test_a_v_prefixed_tag_is_normalised() -> None:
    repository = _FakeRepository({"a": "v2.1.0"})
    eras = plan_eras(repository, [_FakeCandidate("pr-1", "a")])

    assert eras["pr-1"].key == "2.1.0"
    assert eras["pr-1"].version_hint == "2.1.0"


# --------------------------------------------------------------------------
# What reaches the Dockerfile
# --------------------------------------------------------------------------


def test_the_image_mounts_the_tree_rather_than_copying_it() -> None:
    """No COPY is what makes a per-era image affordable."""
    spec = RuntimeSpec(
        language="python",
        base_image="python:3.9-slim",
        install=('pip install "pluggy==0.6.0" "pytest"',),
        shadowed=("pluggy",),
        test_command=("python", "-m", "pytest"),
    )

    dockerfile = render_dockerfile(spec, "python:3.9-slim@sha256:abc")

    assert "COPY" not in dockerfile
    assert "FROM python:3.9-slim@sha256:abc" in dockerfile
    assert "WORKDIR /work" in dockerfile


def test_the_tag_is_stable_for_the_same_runtime() -> None:
    def spec(version: str) -> RuntimeSpec:
        return RuntimeSpec(
            language="python", base_image=f"python:{version}-slim", install=("pip install x",)
        )

    assert spec("3.9").tag("repo") == spec("3.9").tag("repo")
    assert spec("3.9").tag("repo") != spec("3.10").tag("repo")


# --------------------------------------------------------------------------
# Building, reusing, and failing
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.stdout = ""
        self.stderr = "" if ok else "build blew up"


def make_images(monkeypatch, *, eras: int = 12, ok: bool = True, proposal=None) -> RuntimeImages:
    monkeypatch.setattr(rm, "run", lambda *a, **k: _FakeResult(ok))
    monkeypatch.setattr(
        "stress_stack.container.pin_image_by_digest",
        lambda tag: (f"{tag}@sha256:deadbeef", "digest_pinned"),
    )
    monkeypatch.setattr(
        "stress_stack.environment_agent.propose_environment",
        lambda *a, **k: proposal or Proposal(
            base_image="python:3.9-slim",
            install=('pip install "pluggy==0.6.0" "pytest"',),
            test_command=("pytest",),
            evidence=[{"file": "setup.py", "says": "classifiers name 3.5"}],
        ),
    )
    return RuntimeImages(
        repository_name="repo", head=HEAD_PY, client=object(), expected_eras=eras
    )


def test_an_era_gets_its_own_image(tmp_path: Path, monkeypatch) -> None:
    images = make_images(monkeypatch)

    image, record = images.runtime_for(pluggy_tree(tmp_path), era=Era("0.6.0", "0.6.0"))

    assert image != HEAD_PY.image
    assert record["status"] == "per_candidate_image"
    assert record["era"] == "0.6.0"


def test_heads_era_keeps_the_proved_image(tmp_path: Path, monkeypatch) -> None:
    """That image carries the real hash-pinned lock; a synthesized one cannot."""
    images = make_images(monkeypatch)

    image, record = images.runtime_for(tmp_path, era=Era("HEAD"))

    assert image == HEAD_PY.image
    assert record["reason"] == "cut_from_head"


def test_an_era_is_resolved_once_and_reused(tmp_path: Path, monkeypatch) -> None:
    """Every candidate in a window would otherwise re-ask the same question."""
    calls: list[int] = []
    images = make_images(monkeypatch)
    original = rm.resolve_runtime
    monkeypatch.setattr(
        rm,
        "resolve_runtime",
        lambda *a, **k: (calls.append(1), original(*a, **k))[1],
    )
    era = Era("0.6.0", "0.6.0")

    for index in range(4):
        forget_source_roots()
        image, record = images.runtime_for(pluggy_tree(tmp_path / f"c{index}"), era=era)

    assert len(calls) == 1
    assert record["reused"] is True
    assert len(images.built) == 1


def test_the_era_ceiling_falls_back_rather_than_building_forever(
    tmp_path: Path, monkeypatch
) -> None:
    images = make_images(monkeypatch, eras=1)
    statuses = []
    for index, key in enumerate(("0.6.0", "0.7.0", "0.8.0")):
        forget_source_roots()
        monkeypatch.setattr(
            "stress_stack.environment_agent.propose_environment",
            lambda *a, _k=key, **kw: Proposal(
                base_image=f"python:3.{index + 7}-slim",
                install=(f'pip install "pluggy=={_k}"',),
                test_command=("pytest",),
            ),
        )
        statuses.append(images.runtime_for(pluggy_tree(tmp_path / f"c{index}"), era=Era(key))[1])

    assert statuses[0]["status"] == "per_candidate_image"
    assert [s["reason"] for s in statuses[1:]] == ["era_ceiling_reached"] * 2


def test_a_failed_build_degrades_visibly(tmp_path: Path, monkeypatch) -> None:
    """Falling back is acceptable; falling back silently is not."""
    images = make_images(monkeypatch, ok=False)

    image, record = images.runtime_for(pluggy_tree(tmp_path), era=Era("0.6.0"))

    assert image == HEAD_PY.image
    assert record["reason"] == "runtime_image_build_failed"
    assert "build blew up" in record["build_log_tail"]


def test_an_unusable_proposal_stops_the_candidate_rather_than_guessing(
    tmp_path: Path, monkeypatch
) -> None:
    """There is no deterministic second path, and inventing one would be worse."""
    images = make_images(
        monkeypatch,
        proposal=Proposal(rejections=["test_command_rejected: narrows_collection: -k"]),
    )

    image, record = images.runtime_for(pluggy_tree(tmp_path), era=Era("0.6.0"))

    assert image == HEAD_PY.image
    assert record["reason"].startswith("resolution_failed: EnvironmentProposalError")


def test_a_resolver_crash_never_ends_the_run(tmp_path: Path, monkeypatch) -> None:
    images = make_images(monkeypatch)
    monkeypatch.setattr(
        "stress_stack.environment_agent.propose_environment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    image, record = images.runtime_for(tmp_path, era=Era("0.6.0"))

    assert image == HEAD_PY.image
    assert record["reason"].startswith("resolution_failed: RuntimeError")
