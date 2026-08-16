from __future__ import annotations

from pathlib import Path

from stress_stack.symbols import (
    discover_python_files,
    is_test_path,
    module_name,
    parse_file,
)

_SOURCE = '''"""Module doc."""

import os
from collections import OrderedDict
from .relative import helper


class Base:
    """Base doc."""

    def method(self, value: int = 3) -> str:
        return str(value)


class Child(Base, OrderedDict):
    @property
    def name(self) -> str:
        return helper(os.sep)


async def _private(a, /, b, *args, key=None, **rest):
    return await other(a)
'''


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_extracts_classes_methods_functions_with_anchors(tmp_path: Path) -> None:
    write(tmp_path, "pkg/__init__.py", "")
    path = write(tmp_path, "pkg/mod.py", _SOURCE)

    parsed = parse_file(tmp_path, path)
    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}

    assert kinds["pkg.mod"] == "module"
    assert kinds["pkg.mod.Base"] == "class"
    assert kinds["pkg.mod.Base.method"] == "method"
    assert kinds["pkg.mod._private"] == "function"
    assert all(symbol.anchor.line >= 1 for symbol in parsed.symbols)
    assert all(symbol.anchor.path == "pkg/mod.py" for symbol in parsed.symbols)


def test_captures_bases_decorators_docstrings_and_visibility(tmp_path: Path) -> None:
    path = write(tmp_path, "mod.py", _SOURCE)
    parsed = parse_file(tmp_path, path)
    symbols = {symbol.qualified_name: symbol for symbol in parsed.symbols}

    assert symbols["mod.Child"].bases == ["Base", "OrderedDict"]
    assert symbols["mod.Child.name"].decorators == ["property"]
    assert symbols["mod.Base"].docstring == "Base doc."
    assert symbols["mod._private"].is_public is False
    assert symbols["mod.Base.method"].is_public is True


def test_signature_renders_every_argument_form(tmp_path: Path) -> None:
    path = write(tmp_path, "mod.py", _SOURCE)
    parsed = parse_file(tmp_path, path)
    signature = next(
        symbol.signature for symbol in parsed.symbols if symbol.name == "_private"
    )

    assert signature == "(a, /, b, *args, key=..., **rest)"


def test_records_imports_with_relative_level(tmp_path: Path) -> None:
    path = write(tmp_path, "pkg/mod.py", _SOURCE)
    parsed = parse_file(tmp_path, path)
    relative = [item for item in parsed.imports if item.level > 0]

    assert [(item.module, item.name, item.level) for item in relative] == [
        ("relative", "helper", 1)
    ]
    assert {item.bound_name for item in parsed.imports} >= {"os", "OrderedDict", "helper"}


def test_syntax_error_is_recorded_not_raised(tmp_path: Path) -> None:
    path = write(tmp_path, "broken.py", "def oops(:\n")
    parsed = parse_file(tmp_path, path)

    assert parsed.syntax_error is not None
    assert parsed.symbols == []


def test_discovery_skips_environments_and_caches(tmp_path: Path) -> None:
    write(tmp_path, "keep.py", "")
    write(tmp_path, "pkg/also.py", "")
    write(tmp_path, ".venv/lib/skip.py", "")
    write(tmp_path, "__pycache__/skip.py", "")
    write(tmp_path, ".stress_stack/tools/skip.py", "")

    found = {str(path.relative_to(tmp_path)) for path in discover_python_files(tmp_path)}

    assert found == {"keep.py", "pkg/also.py"}


def test_module_naming_and_test_detection(tmp_path: Path) -> None:
    write(tmp_path, "pkg/__init__.py", "")
    write(tmp_path, "pkg/mod.py", "")

    assert module_name(tmp_path, tmp_path / "pkg" / "__init__.py") == "pkg"
    assert module_name(tmp_path, tmp_path / "pkg" / "mod.py") == "pkg.mod"
    assert is_test_path("tests/test_thing.py") is True
    assert is_test_path("pkg/test/helpers.py") is True
    assert is_test_path("pkg/core.py") is False


def test_only_test_named_functions_in_test_files_are_marked(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "tests/test_mod.py",
        "def test_one():\n    pass\n\n\ndef helper():\n    pass\n",
    )
    parsed = parse_file(tmp_path, path)
    marked = {symbol.name: symbol.is_test for symbol in parsed.symbols if symbol.kind == "function"}

    assert marked == {"test_one": True, "helper": False}


def test_src_layout_module_names_strip_the_source_root(tmp_path: Path) -> None:
    """A src/ layout must yield ``pkg.mod``, not ``src.pkg.mod``."""
    write(tmp_path, "src/pkg/__init__.py", "")
    write(tmp_path, "src/pkg/core.py", "")
    write(tmp_path, "src/pkg/utils/__init__.py", "")
    write(tmp_path, "src/pkg/utils/text.py", "")
    write(tmp_path, "tests/test_core.py", "")
    write(tmp_path, "setup.py", "")

    names = {
        str(path.relative_to(tmp_path)): module_name(tmp_path, path)
        for path in discover_python_files(tmp_path)
    }

    assert names["src/pkg/__init__.py"] == "pkg"
    assert names["src/pkg/core.py"] == "pkg.core"
    assert names["src/pkg/utils/text.py"] == "pkg.utils.text"
    assert names["tests/test_core.py"] == "test_core"
    assert names["setup.py"] == "setup"


def test_flat_layout_module_names_are_unchanged(tmp_path: Path) -> None:
    write(tmp_path, "glom/__init__.py", "")
    write(tmp_path, "glom/core.py", "")
    write(tmp_path, "glom/test/__init__.py", "")
    write(tmp_path, "glom/test/test_basic.py", "")

    names = {
        str(path.relative_to(tmp_path)): module_name(tmp_path, path)
        for path in discover_python_files(tmp_path)
    }

    assert names["glom/__init__.py"] == "glom"
    assert names["glom/core.py"] == "glom.core"
    assert names["glom/test/test_basic.py"] == "glom.test.test_basic"


def test_fallback_import_in_except_handler_is_guarded(tmp_path: Path) -> None:
    """`try: import tomllib / except ImportError: import tomli` — both conditional."""
    path = write(
        tmp_path,
        "m.py",
        "try:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n",
    )
    guards = {item.module: item.guard for item in parse_file(tmp_path, path).imports}

    assert guards == {"tomllib": "try_import", "tomli": "try_import"}


def test_non_import_except_does_not_mark_a_guard(tmp_path: Path) -> None:
    path = write(tmp_path, "m.py", "try:\n    import lxml\nexcept ValueError:\n    lxml = None\n")
    guards = {item.module: item.guard for item in parse_file(tmp_path, path).imports}

    assert guards == {"lxml": None}


def test_tuple_of_import_errors_is_a_guard(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "m.py",
        "try:\n    import ujson\nexcept (ImportError, ModuleNotFoundError):\n    ujson = None\n",
    )
    assert parse_file(tmp_path, path).imports[0].guard == "try_import"


def test_version_gated_import_is_marked(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "m.py",
        "import sys\n\nif sys.version_info >= (3, 11):\n    import tomllib\nelse:\n    import tomli\n",
    )
    guards = {item.module: item.guard for item in parse_file(tmp_path, path).imports}

    assert guards["tomllib"] == "version_gated"
    assert guards["tomli"] == "version_gated"
    assert guards["sys"] is None


def test_type_checking_else_branch_is_not_guarded(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "m.py",
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import pandas\nelse:\n    import csv\n",
    )
    guards = {item.module: item.guard for item in parse_file(tmp_path, path).imports}

    assert guards["pandas"] == "type_checking"
    assert guards["csv"] is None


def test_captures_every_import_form(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "m.py",
        "import os.path\n"
        "import xml.sax as sax\n"
        "import json as _json\n"
        "from decimal import (Decimal, getcontext)\n"
        "from operator import *\n",
    )
    bound = {item.module: item.bound_name for item in parse_file(tmp_path, path).imports}

    assert bound["os.path"] == "os"
    assert bound["xml.sax"] == "sax"
    assert bound["json"] == "_json"
    assert sorted(
        item.name for item in parse_file(tmp_path, path).imports if item.module == "decimal"
    ) == ["Decimal", "getcontext"]
    assert bound["operator"] == "*"


def test_imports_are_captured_at_any_nesting_depth(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "m.py",
        "def deferred():\n    import statistics\n\n\n"
        "class Holder:\n    import string\n\n    def method(self):\n        import textwrap\n",
    )
    modules = {item.module for item in parse_file(tmp_path, path).imports}

    assert modules == {"statistics", "string", "textwrap"}


def test_namespace_subpackage_keeps_its_dotted_path(tmp_path: Path) -> None:
    """PEP 420: no __init__.py, but still a subpackage of its parent package."""
    write(tmp_path, "src/zoo/__init__.py", "")
    write(tmp_path, "src/zoo/ns_pkg/mod.py", "")
    write(tmp_path, "scripts/tool.py", "")

    assert module_name(tmp_path, tmp_path / "src/zoo/ns_pkg/mod.py") == "zoo.ns_pkg.mod"
    assert module_name(tmp_path, tmp_path / "scripts/tool.py") == "tool"
