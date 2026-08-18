"""Which environment a candidate is judged in, and what was read to decide it.

The resolvers are pure functions of a tree, which is the point: a runtime that
depended on anything but the candidate's own files could not be re-derived from
the recorded evidence, and "this verdict was reached in image X for reason Y" is
part of the verdict once X stops being the same for every task.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_stack import runtime_matrix as rm
from stress_stack.runner import forget_source_roots
from stress_stack.runtime_matrix import (
    HeadRuntime,
    RuntimeImages,
    RuntimeSpec,
    render_dockerfile,
    resolve_runtime,
    shadowed_distributions,
    supported_languages,
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
# The common case: nothing to do
# --------------------------------------------------------------------------


def test_a_tree_matching_head_gets_no_image_of_its_own(tmp_path: Path) -> None:
    """The verify image carries the real pinned lock; a synthesized one cannot."""
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = ">=3.12"\n')
    write(tmp_path / "app" / "__init__.py", "")

    assert resolve_runtime(tmp_path, "python", HEAD_PY) is None


def test_an_ecosystem_without_a_resolver_keeps_the_head_image(tmp_path: Path) -> None:
    head = HeadRuntime(image="i", language="cpp", toolchain_version="17")
    assert resolve_runtime(tmp_path, "cpp", head) is None
    assert "cpp" not in supported_languages()


# --------------------------------------------------------------------------
# The interpreter a historical tree actually asks for
# --------------------------------------------------------------------------


def test_an_older_requires_python_moves_the_interpreter(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = "<3.10"\n')

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.base_image == "python:3.9-slim"
    assert spec.evidence["requires_python"]["from"] == "pyproject.toml"


def test_a_setup_py_declaration_is_read_when_there_is_no_pyproject(tmp_path: Path) -> None:
    """A 2014 tree has no pyproject; the answer is still in the tree."""
    write(tmp_path / "setup.py", 'setup(name="app", python_requires="<3.11")\n')

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.base_image == "python:3.10-slim"
    assert spec.evidence["requires_python"]["from"] == "setup.py"


def test_the_ci_matrix_outranks_a_bare_floor(tmp_path: Path) -> None:
    """`requires-python` names a floor; a matrix names what actually ran."""
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = ">=3.9"\n')
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "jobs:\n  test:\n    strategy:\n      matrix:\n"
        '        python-version: ["3.9", "3.10"]\n',
    )

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.base_image == "python:3.10-slim"
    assert spec.evidence["ci_matrix"]["chose"] == "3.10"


def test_a_matrix_the_declaration_forbids_is_not_chosen(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = "<3.10"\n')
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        'jobs:\n  test:\n    strategy:\n      matrix:\n        python-version: ["3.12"]\n',
    )

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.base_image == "python:3.9-slim"


# --------------------------------------------------------------------------
# The self-hosting case — the one that costs a deliverable
# --------------------------------------------------------------------------


def pluggy_tree(tmp_path: Path, *, version: str, src_layout: bool) -> Path:
    if src_layout:
        write(tmp_path / "src" / "pluggy" / "__init__.py", "")
    else:
        write(tmp_path / "pluggy.py", "")
    write(tmp_path / "setup.py", f'setup(name="pluggy", version="{version}")\n')
    return tmp_path


@pytest.mark.parametrize("src_layout", [False, True])
def test_a_repository_that_is_a_runner_dependency_is_detected(
    tmp_path: Path, src_layout: bool
) -> None:
    """Both of pluggy's historical layouts shadow the copy pytest is running on."""
    tree = pluggy_tree(tmp_path, version="0.6.0", src_layout=src_layout)

    assert shadowed_distributions(tree) == ("pluggy",)


def test_the_tree_pins_its_own_version_and_leaves_the_runner_free(tmp_path: Path) -> None:
    """The compatibility question goes to the resolver, not to a table here.

    Pinning pluggy and leaving pytest unpinned makes pip find a pytest that
    accepts that pluggy. Pinning pytest instead would require this file to know
    which pytest suits which pluggy, which is knowledge that rots.
    """
    tree = pluggy_tree(tmp_path, version="0.6.0", src_layout=False)

    spec = resolve_runtime(tree, "python", HEAD_PY)

    assert spec is not None
    assert spec.shadowed == ("pluggy",)
    assert spec.install == ('pip install "pluggy==0.6.0" "pytest"',)
    assert spec.evidence["self_hosted"] == {
        "distribution": "pluggy",
        "version": "0.6.0",
        "from": "setup.py",
    }


def test_shadowing_forces_an_image_even_on_heads_own_interpreter(tmp_path: Path) -> None:
    """Same interpreter, still unusable — the shadow is the reason, not the version."""
    write(tmp_path / "pyproject.toml", '[project]\nname = "pluggy"\nversion = "1.5.0"\n')
    write(tmp_path / "src" / "pluggy" / "__init__.py", "")

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.base_image == "python:3.12-slim"
    assert spec.install == ('pip install "pluggy==1.5.0" "pytest"',)


def test_a_shadow_without_a_readable_version_is_reported_not_invented(tmp_path: Path) -> None:
    """No version means no pin. Guessing one would pin the wrong code."""
    write(tmp_path / "pluggy.py", "")

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is None or "self_hosted" not in spec.evidence


def test_an_ordinary_repository_shadows_nothing(tmp_path: Path) -> None:
    write(tmp_path / "src" / "glom" / "__init__.py", "")
    write(tmp_path / "tests" / "test_glom.py", "")

    assert shadowed_distributions(tmp_path) == ()


# --------------------------------------------------------------------------
# Other ecosystems reach the same answer from their own files
# --------------------------------------------------------------------------


def test_go_reads_the_toolchain_from_go_mod(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/m\n\ngo 1.19\n")
    head = HeadRuntime(image="i", language="go", toolchain_version="1.22")

    spec = resolve_runtime(tmp_path, "go", head)

    assert spec is not None
    assert spec.base_image == "golang:1.19-bookworm"
    assert spec.test_command == ("go", "test", "./...")


def test_go_matching_head_needs_no_image(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/m\n\ngo 1.22\n")
    head = HeadRuntime(image="i", language="go", toolchain_version="1.22")

    assert resolve_runtime(tmp_path, "go", head) is None


def test_rust_prefers_the_toolchain_file_over_the_manifest(tmp_path: Path) -> None:
    write(tmp_path / "Cargo.toml", '[package]\nname = "app"\nrust-version = "1.70"\n')
    write(tmp_path / "rust-toolchain.toml", '[toolchain]\nchannel = "1.65"\n')
    head = HeadRuntime(image="i", language="rust", toolchain_version="1.80")

    spec = resolve_runtime(tmp_path, "rust", head)

    assert spec is not None
    assert spec.base_image == "rust:1.65-bookworm"
    assert spec.evidence["toolchain"]["from"] == "rust-toolchain.toml"


def test_rust_falls_back_to_the_manifest(tmp_path: Path) -> None:
    write(tmp_path / "Cargo.toml", '[package]\nname = "app"\nrust-version = "1.70"\n')
    head = HeadRuntime(image="i", language="rust", toolchain_version="1.80")

    spec = resolve_runtime(tmp_path, "rust", head)

    assert spec is not None
    assert spec.base_image == "rust:1.70-bookworm"


def test_node_reads_nvmrc_then_engines(tmp_path: Path) -> None:
    head = HeadRuntime(image="i", language="typescript", toolchain_version="22")
    write(tmp_path / "package.json", '{"engines": {"node": ">=18.0.0"}}')

    spec = resolve_runtime(tmp_path, "typescript", head)
    assert spec is not None and spec.base_image == "node:18-slim"

    write(tmp_path / ".nvmrc", "v16\n")
    spec = resolve_runtime(tmp_path, "typescript", head)
    assert spec is not None and spec.base_image == "node:16-slim"


# --------------------------------------------------------------------------
# What reaches the Dockerfile
# --------------------------------------------------------------------------


def test_system_packages_come_from_ci_and_are_filtered(tmp_path: Path) -> None:
    """CI files are data. Nothing from one reaches a shell unchecked."""
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "jobs:\n  test:\n    steps:\n"
        "      - run: sudo apt-get install -y libxml2-dev\n",
    )
    packages = rm.system_packages_from_ci(tmp_path)
    assert "libxml2-dev" in packages
    assert all(rm._SAFE_PACKAGE.match(p) for p in packages)


def test_the_image_mounts_the_tree_rather_than_copying_it() -> None:
    """No COPY is what makes a per-candidate image affordable."""
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
    assert 'RUN pip install "pluggy==0.6.0" "pytest"' in dockerfile
    assert "WORKDIR /work" in dockerfile


def test_the_tag_is_stable_for_the_same_runtime() -> None:
    def spec(version: str) -> RuntimeSpec:
        return RuntimeSpec(
            language="python", base_image=f"python:{version}-slim", install=("pip install x",)
        )

    assert spec("3.9").tag("repo") == spec("3.9").tag("repo")
    assert spec("3.9").tag("repo") != spec("3.10").tag("repo")


# --------------------------------------------------------------------------
# Budget and fallback
# --------------------------------------------------------------------------


def make_images(monkeypatch, *, eras: int = 12, ok: bool = True) -> RuntimeImages:
    monkeypatch.setattr(rm, "run", lambda *a, **k: _FakeResult(ok))
    monkeypatch.setattr(
        "stress_stack.container.pin_image_by_digest",
        lambda tag: (f"{tag}@sha256:deadbeef", "digest_pinned"),
    )
    return RuntimeImages(repository_name="repo", head=HEAD_PY, expected_eras=eras)


class _FakeResult:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.stdout = ""
        self.stderr = "" if ok else "build blew up"


def test_a_shadowing_tree_gets_its_own_image(tmp_path: Path, monkeypatch) -> None:
    images = make_images(monkeypatch)
    tree = pluggy_tree(tmp_path, version="0.6.0", src_layout=False)

    image, record = images.runtime_for(tree)

    assert image != HEAD_PY.image
    assert record["status"] == "per_candidate_image"
    assert record["reason"] == "tree_shadows_the_runner"


def test_an_identical_runtime_is_built_once(tmp_path: Path, monkeypatch) -> None:
    images = make_images(monkeypatch)
    for index in range(3):
        tree = pluggy_tree(tmp_path / f"c{index}", version="0.6.0", src_layout=False)
        forget_source_roots()
        images.runtime_for(tree)

    assert len(images.built) == 1


def test_the_era_ceiling_falls_back_rather_than_building_forever(
    tmp_path: Path, monkeypatch
) -> None:
    images = make_images(monkeypatch, eras=1)
    statuses = []
    for index, version in enumerate(("0.6.0", "0.7.0", "0.8.0")):
        tree = pluggy_tree(tmp_path / f"c{index}", version=version, src_layout=False)
        forget_source_roots()
        statuses.append(images.runtime_for(tree)[1])

    assert statuses[0]["status"] == "per_candidate_image"
    assert [s["reason"] for s in statuses[1:]] == ["era_ceiling_reached"] * 2
    assert all(s["image"] == HEAD_PY.image for s in statuses[1:])


def test_a_failed_build_degrades_visibly(tmp_path: Path, monkeypatch) -> None:
    """Falling back is acceptable; falling back silently is not."""
    images = make_images(monkeypatch, ok=False)
    tree = pluggy_tree(tmp_path, version="0.6.0", src_layout=False)

    image, record = images.runtime_for(tree)

    assert image == HEAD_PY.image
    assert record["reason"] == "runtime_image_build_failed"
    assert "build blew up" in record["build_log_tail"]


def test_a_resolver_crash_never_ends_the_run(tmp_path: Path, monkeypatch) -> None:
    images = make_images(monkeypatch)
    monkeypatch.setitem(
        rm._RESOLVERS,
        "python",
        lambda tree, head, hint=None: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    image, record = images.runtime_for(tmp_path)

    assert image == HEAD_PY.image
    assert record["reason"].startswith("resolution_failed: RuntimeError")


def test_a_block_style_matrix_is_read_in_full(tmp_path: Path) -> None:
    """GitHub Actions writes matrices two ways; both name what actually ran."""
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = ">=3.9"\n')
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "jobs:\n  test:\n    strategy:\n      matrix:\n"
        "        python-version:\n"
        '          - "3.9"\n'
        '          - "3.11"\n'
        "    steps:\n      - run: pytest\n",
    )

    spec = resolve_runtime(tmp_path, "python", HEAD_PY)

    assert spec is not None
    assert spec.evidence["ci_matrix"]["value"] == ["3.11", "3.9"]
    assert spec.base_image == "python:3.11-slim"


def test_a_setuptools_scm_project_is_pinned_from_git(tmp_path: Path) -> None:
    """`dynamic = ["version"]` means the version is real but not in the manifest.

    This is pluggy. Without the hint the shadow is detected and then does
    nothing, which is exactly how the first run still lost sixteen candidates
    after detection was already working.
    """
    write(tmp_path / "src" / "pluggy" / "__init__.py", "")
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pluggy"\ndynamic = ["version"]\n',
    )

    without = resolve_runtime(tmp_path, "python", HEAD_PY)
    assert without is None, "nothing to pin, and the interpreter already matches"

    forget_source_roots()
    spec = resolve_runtime(tmp_path, "python", HEAD_PY, version_hint="1.5.0")

    assert spec is not None
    assert spec.install == ('pip install "pluggy==1.5.0" "pytest"',)
    assert spec.evidence["self_hosted"]["from"] == "git_describe"


def test_a_manifest_version_outranks_the_hint(tmp_path: Path) -> None:
    """The tree's own declaration is more precise than the nearest tag."""
    tree = pluggy_tree(tmp_path, version="0.6.0", src_layout=False)

    spec = resolve_runtime(tree, "python", HEAD_PY, version_hint="0.9.9")

    assert spec is not None
    assert spec.install == ('pip install "pluggy==0.6.0" "pytest"',)


def test_an_unpinnable_shadow_says_so_rather_than_looking_ordinary(
    tmp_path: Path, monkeypatch
) -> None:
    """Both end up on the HEAD image; only one of them is a known limitation."""
    images = make_images(monkeypatch)
    write(tmp_path / "src" / "pluggy" / "__init__.py", "")
    write(tmp_path / "pyproject.toml", '[project]\nname = "pluggy"\ndynamic = ["version"]\n')

    _, record = images.runtime_for(tmp_path)

    assert record["reason"] == "shadowed_but_version_unknown"
    assert record["shadowed"] == ["pluggy"]


def test_an_image_built_only_for_the_interpreter_is_not_called_a_shadow_fix(
    tmp_path: Path, monkeypatch
) -> None:
    images = make_images(monkeypatch)
    write(tmp_path / "pyproject.toml", '[project]\nname = "app"\nrequires-python = "<3.10"\n')

    _, record = images.runtime_for(tmp_path)

    assert record["reason"] == "tree_wants_another_toolchain"


# --------------------------------------------------------------------------
# Eras: how many environments does this pool need, known before staging
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
    from stress_stack.runtime_matrix import era_count, plan_eras

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
    from stress_stack.runtime_matrix import plan_eras

    repository = _FakeRepository({"a": "1.5.0"})
    plan_eras(repository, [_FakeCandidate(f"pr-{i}", "a") for i in range(20)])

    assert repository.asked == ["a"]


def test_excision_candidates_belong_to_heads_era() -> None:
    """They are cut from HEAD, which the container stage already built and proved."""
    from stress_stack.runtime_matrix import era_count, plan_eras

    repository = _FakeRepository({})
    eras = plan_eras(repository, [_FakeCandidate("excise-a", None), _FakeCandidate("excise-b", None)])

    assert {e.key for e in eras.values()} == {"HEAD"}
    assert era_count(eras) == 1
    assert repository.asked == []


def test_an_undescribable_commit_is_its_own_era_not_a_crash() -> None:
    """A repository with no tags at all still plans, one era per base commit."""
    from stress_stack.runtime_matrix import era_count, plan_eras

    repository = _FakeRepository({})
    eras = plan_eras(
        repository,
        [_FakeCandidate("pr-1", "aaaaaaaaaaaaaaaa"), _FakeCandidate("pr-2", "bbbbbbbbbbbbbbbb")],
    )

    assert era_count(eras) == 2
    assert eras["pr-1"].key == "aaaaaaaaaaaa"


def test_a_v_prefixed_tag_is_normalised() -> None:
    from stress_stack.runtime_matrix import plan_eras

    repository = _FakeRepository({"a": "v2.1.0"})
    eras = plan_eras(repository, [_FakeCandidate("pr-1", "a")])

    assert eras["pr-1"].key == "2.1.0"
    assert eras["pr-1"].version_hint == "2.1.0"


def test_an_era_is_resolved_once_and_reused(tmp_path: Path, monkeypatch) -> None:
    """Every candidate in a window reads the same manifests to the same answer."""
    from stress_stack.runtime_matrix import Era

    images = make_images(monkeypatch)
    era = Era(key="0.6.0", version_hint="0.6.0")
    resolutions: list[Path] = []
    original = rm.resolve_runtime
    monkeypatch.setattr(
        rm,
        "resolve_runtime",
        lambda tree, lang, head, **k: (resolutions.append(tree), original(tree, lang, head, **k))[1],
    )

    for index in range(4):
        tree = pluggy_tree(tmp_path / f"c{index}", version="0.6.0", src_layout=False)
        forget_source_roots()
        image, record = images.runtime_for(tree, era=era)

    assert len(resolutions) == 1, "resolved once per era, not once per candidate"
    assert record["era"] == "0.6.0"
    assert record["reused"] is True
    assert len(images.built) == 1
