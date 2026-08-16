"""Deterministic Python symbol extraction built on the standard library ``ast``.

Every symbol and reference carries a ``file:line`` anchor taken directly from the
parse tree. Nothing here infers, summarises, or guesses: a reference that cannot
be resolved later is recorded as unresolved rather than approximated.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath
from typing import Any, Literal

SymbolKind = Literal["module", "class", "function", "method"]

_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".stress_stack",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "site-packages",
        ".eggs",
    }
)


@dataclass(frozen=True, slots=True)
class Anchor:
    path: str
    line: int
    end_line: int
    column: int
    end_column: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    anchor: Anchor
    parent: str | None
    bases: list[str]
    decorators: list[str]
    signature: str | None
    docstring: str | None
    is_test: bool
    is_public: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["anchor"] = self.anchor.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class ParsedImport:
    module: str
    name: str | None
    alias: str | None
    level: int
    anchor: Anchor
    scope: str
    guard: str | None = None
    """Why this import is conditional: ``try_import``, ``type_checking``, or None.

    A guarded import is not a hard runtime requirement — the module deliberately
    tolerates its absence — so pinning it as one would overstate the manifest.
    """

    @property
    def bound_name(self) -> str:
        if self.alias:
            return self.alias
        if self.name:
            return self.name
        return self.module.split(".")[0]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["anchor"] = self.anchor.to_dict()
        value["bound_name"] = self.bound_name
        return value


@dataclass(frozen=True, slots=True)
class ParsedCall:
    expression: str
    anchor: Anchor
    scope: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["anchor"] = self.anchor.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: str
    module: str
    is_test: bool
    source_sha256: str
    symbols: list[ParsedSymbol]
    imports: list[ParsedImport]
    calls: list[ParsedCall]
    syntax_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "is_test": self.is_test,
            "source_sha256": self.source_sha256,
            "syntax_error": self.syntax_error,
            "symbols": [item.to_dict() for item in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
        }


@dataclass
class _Scope:
    qualified_name: str
    kind: str
    children: list[str] = field(default_factory=list)


def discover_python_files(root: Path) -> list[Path]:
    """Return every analysable ``.py`` file below ``root``, deterministically ordered."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in _SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file():
            found.append(path)
    return found


def package_root(root: Path, path: Path) -> Path:
    """The directory a module's dotted name is relative to.

    Walk up from the file while each directory is a package, and stop at the
    first that is not. This is how Python itself names modules, so a ``src/``
    layout yields ``multidep.core`` rather than ``src.multidep.core`` — without
    which every intra-repository import looks like a third-party package.
    """
    directory = path.parent
    while directory != root and (
        (directory / "__init__.py").is_file()
        # PEP 420: a directory with no __init__.py is still a namespace
        # subpackage when its parent is itself a package.
        or (directory.parent / "__init__.py").is_file()
    ):
        directory = directory.parent
    return directory


def module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(package_root(root, path))
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def is_test_path(relative_path: str) -> bool:
    parts = PurePath(relative_path).parts
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in {"test", "tests", "testing"} for part in parts[:-1])


def parse_file(root: Path, path: Path) -> ParsedFile:
    relative = str(path.relative_to(root))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    module = module_name(root, path)
    test_file = is_test_path(relative)
    try:
        tree = ast.parse(raw, filename=relative)
    except (SyntaxError, ValueError) as exc:
        return ParsedFile(
            path=relative,
            module=module,
            is_test=test_file,
            source_sha256=digest,
            symbols=[],
            imports=[],
            calls=[],
            syntax_error=f"{type(exc).__name__}: {exc}",
        )
    visitor = _Visitor(relative, module, test_file)
    visitor.visit_module(tree)
    return ParsedFile(
        path=relative,
        module=module,
        is_test=test_file,
        source_sha256=digest,
        symbols=visitor.symbols,
        imports=visitor.imports,
        calls=visitor.calls,
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, module: str, is_test_file: bool) -> None:
        self.path = path
        self.module = module
        self.is_test_file = is_test_file
        self.symbols: list[ParsedSymbol] = []
        self.imports: list[ParsedImport] = []
        self.calls: list[ParsedCall] = []
        self._stack: list[_Scope] = []
        self._guard: str | None = None

    def visit_Try(self, node: ast.Try) -> None:
        catches_import = any(
            _catches_import_error(handler.type) for handler in node.handlers
        )
        guard = "try_import" if catches_import else self._guard
        self._visit_guarded(node.body, guard)
        for handler in node.handlers:
            # A fallback import inside `except ImportError` is just as conditional
            # as the one it replaces (`try: import tomllib / except: import tomli`).
            handler_guard = (
                "try_import" if _catches_import_error(handler.type) else self._guard
            )
            self._visit_guarded(handler.body, handler_guard)
        for child in [*node.orelse, *node.finalbody]:
            self.visit(child)

    def visit_If(self, node: ast.If) -> None:
        guard = self._guard
        if _is_type_checking(node.test):
            guard = "type_checking"
        elif _is_version_gate(node.test):
            guard = "version_gated"
        self._visit_guarded(node.body, guard)
        self._visit_guarded(node.orelse, "version_gated" if _is_version_gate(node.test) else self._guard)

    def _visit_guarded(self, body: list[ast.stmt], guard: str | None) -> None:
        previous = self._guard
        self._guard = guard
        for child in body:
            self.visit(child)
        self._guard = previous

    def visit_module(self, tree: ast.Module) -> None:
        self._stack.append(_Scope(qualified_name=self.module, kind="module"))
        self.symbols.append(
            ParsedSymbol(
                id=f"{self.path}::{self.module}",
                kind="module",
                name=self.module.rsplit(".", 1)[-1] if self.module else self.path,
                qualified_name=self.module,
                anchor=self._anchor(tree.body[0]) if tree.body else _module_anchor(self.path),
                parent=None,
                bases=[],
                decorators=[],
                signature=None,
                docstring=ast.get_docstring(tree),
                is_test=self.is_test_file,
                is_public=True,
            )
        )
        for node in tree.body:
            self.visit(node)
        self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_definition(node, kind="class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_definition(node, kind=self._callable_kind())

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_definition(node, kind=self._callable_kind())

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    module=alias.name,
                    name=None,
                    alias=alias.asname,
                    level=0,
                    anchor=self._anchor(node),
                    scope=self._current().qualified_name,
                    guard=self._guard,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    module=node.module or "",
                    name=alias.name,
                    alias=alias.asname,
                    level=node.level or 0,
                    anchor=self._anchor(node),
                    scope=self._current().qualified_name,
                    guard=self._guard,
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        expression = _expression_text(node.func)
        if expression:
            self.calls.append(
                ParsedCall(
                    expression=expression,
                    anchor=self._anchor(node),
                    scope=self._current().qualified_name,
                )
            )
        self.generic_visit(node)

    def _record_definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, *, kind: str
    ) -> None:
        parent = self._current()
        qualified = f"{parent.qualified_name}.{node.name}" if parent.qualified_name else node.name
        bases = (
            [text for text in (_expression_text(base) for base in node.bases) if text]
            if isinstance(node, ast.ClassDef)
            else []
        )
        decorators = [
            text
            for text in (_expression_text(item) for item in node.decorator_list)
            if text
        ]
        self.symbols.append(
            ParsedSymbol(
                id=f"{self.path}::{qualified}",
                kind=kind,  # type: ignore[arg-type]
                name=node.name,
                qualified_name=qualified,
                anchor=self._anchor(node),
                parent=f"{self.path}::{parent.qualified_name}",
                bases=bases,
                decorators=decorators,
                signature=_signature(node) if not isinstance(node, ast.ClassDef) else None,
                docstring=ast.get_docstring(node),
                is_test=self.is_test_file and node.name.startswith("test"),
                is_public=not node.name.startswith("_"),
            )
        )
        parent.children.append(qualified)
        self._stack.append(_Scope(qualified_name=qualified, kind=kind))
        for child in node.body:
            self.visit(child)
        self._stack.pop()

    def _callable_kind(self) -> str:
        return "method" if self._current().kind == "class" else "function"

    def _current(self) -> _Scope:
        return self._stack[-1]

    def _anchor(self, node: ast.AST) -> Anchor:
        return Anchor(
            path=self.path,
            line=getattr(node, "lineno", 1),
            end_line=getattr(node, "end_lineno", None) or getattr(node, "lineno", 1),
            column=getattr(node, "col_offset", 0),
            end_column=getattr(node, "end_col_offset", None) or 0,
        )


def _catches_import_error(node: ast.expr | None) -> bool:
    if node is None:
        return True
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return any(
        _expression_text(item) in {"ImportError", "ModuleNotFoundError"} for item in names
    )


def _is_type_checking(node: ast.expr) -> bool:
    text = _expression_text(node)
    return text is not None and text.split(".")[-1] == "TYPE_CHECKING"


def _is_version_gate(node: ast.expr) -> bool:
    """``if sys.version_info >= (3, 11):`` — the import depends on the interpreter."""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in {
            "version_info",
            "platform",
        }:
            return True
    return False


def _module_anchor(path: str) -> Anchor:
    return Anchor(path=path, line=1, end_line=1, column=0, end_column=0)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments = node.args
    parts: list[str] = []
    positional = list(arguments.posonlyargs) + list(arguments.args)
    defaults_offset = len(positional) - len(arguments.defaults)
    for index, argument in enumerate(positional):
        text = argument.arg
        if argument.annotation is not None:
            text += f": {_expression_text(argument.annotation) or '...'}"
        if index >= defaults_offset:
            text += "=..."
        parts.append(text)
        if arguments.posonlyargs and index == len(arguments.posonlyargs) - 1:
            parts.append("/")
    if arguments.vararg is not None:
        parts.append(f"*{arguments.vararg.arg}")
    elif arguments.kwonlyargs:
        parts.append("*")
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=False):
        text = argument.arg
        if argument.annotation is not None:
            text += f": {_expression_text(argument.annotation) or '...'}"
        if default is not None:
            text += "=..."
        parts.append(text)
    if arguments.kwarg is not None:
        parts.append(f"**{arguments.kwarg.arg}")
    rendered = ", ".join(parts)
    returns = _expression_text(node.returns) if node.returns is not None else None
    return f"({rendered}) -> {returns}" if returns else f"({rendered})"


def _expression_text(node: ast.AST | None) -> str | None:
    """Render a dotted reference (``a.b.c``) or ``None`` for dynamic expressions."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expression_text(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return _expression_text(node.func)
    if isinstance(node, ast.Subscript):
        return _expression_text(node.value)
    return None
