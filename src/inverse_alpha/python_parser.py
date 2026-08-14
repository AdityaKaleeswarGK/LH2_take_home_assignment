from __future__ import annotations

import ast
import bisect
import gc
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from inverse_alpha.knowledge_models import SourceSpan

PYTHON_LANGUAGE = Language(tree_sitter_python.language())
EXTRACTOR_NAME = "inverse-alpha-python-tree-sitter"
EXTRACTOR_VERSION = "0.2.0"
_EXTRACTION_QUERY = Query(
    PYTHON_LANGUAGE,
    """
    (class_definition
      name: (identifier) @class.name
      superclasses: (argument_list (_) @class.base)?
    ) @class.definition
    (function_definition name: (identifier) @function.name) @function.definition
    (import_statement) @import
    (import_from_statement) @import
    (call function: (_) @call.function) @call
    (assignment left: (_) @assignment.left) @assignment
    (decorator) @decorator
    (if_statement) @conditional
    (try_statement) @conditional
    (with_statement) @conditional
    (ERROR) @error
    """,
)


@dataclass(frozen=True, slots=True)
class ExpressionReference:
    expression: str
    span: SourceSpan

    def to_dict(self) -> dict[str, Any]:
        return {"expression": self.expression, "span": self.span.to_dict()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExpressionReference:
        return cls(value["expression"], SourceSpan(**value["span"]))


@dataclass(frozen=True, slots=True)
class ImportReference:
    module: str | None
    name: str | None
    alias: str
    level: int
    span: SourceSpan
    conditional: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["span"] = self.span.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ImportReference:
        return cls(
            value["module"],
            value["name"],
            value["alias"],
            value["level"],
            SourceSpan(**value["span"]),
            value["conditional"],
        )


@dataclass(frozen=True, slots=True)
class CallReference:
    expression: str
    scope: str | None
    span: SourceSpan

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "scope": self.scope,
            "span": self.span.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CallReference:
        return cls(value["expression"], value["scope"], SourceSpan(**value["span"]))


@dataclass(frozen=True, slots=True)
class CapturedSyntax:
    node_type: str
    text: str
    span: SourceSpan

    @property
    def byte_range(self) -> tuple[int, int]:
        return (self.span.start_byte, self.span.end_byte)


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    name: str
    qualified_name: str
    kind: str
    parent: str | None
    span: SourceSpan
    bases: list[ExpressionReference] = field(default_factory=list)
    decorators: list[ExpressionReference] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "parent": self.parent,
            "span": self.span.to_dict(),
            "bases": [item.to_dict() for item in self.bases],
            "decorators": [item.to_dict() for item in self.decorators],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ParsedSymbol:
        return cls(
            value["name"],
            value["qualified_name"],
            value["kind"],
            value["parent"],
            SourceSpan(**value["span"]),
            [ExpressionReference.from_dict(item) for item in value.get("bases", [])],
            [
                ExpressionReference.from_dict(item)
                for item in value.get("decorators", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: str
    module: str
    content_hash: str
    is_test: bool
    symbols: list[ParsedSymbol]
    imports: list[ImportReference]
    calls: list[CallReference]
    assignments: list[str]
    diagnostics: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "content_hash": self.content_hash,
            "is_test": self.is_test,
            "symbols": [item.to_dict() for item in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "assignments": self.assignments,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ParsedFile:
        return cls(
            value["path"],
            value["module"],
            value["content_hash"],
            value["is_test"],
            [ParsedSymbol.from_dict(item) for item in value["symbols"]],
            [ImportReference.from_dict(item) for item in value["imports"]],
            [CallReference.from_dict(item) for item in value["calls"]],
            list(value.get("assignments", [])),
            list(value.get("diagnostics", [])),
        )


class PythonSourceParser:
    def parse(self, path: str, module: str, source: bytes, is_test: bool) -> ParsedFile:
        garbage_collection_enabled = gc.isenabled()
        if garbage_collection_enabled:
            gc.disable()
        try:
            return self._parse_without_cyclic_gc(path, module, source, is_test)
        finally:
            if garbage_collection_enabled:
                gc.enable()

    def _parse_without_cyclic_gc(
        self, path: str, module: str, source: bytes, is_test: bool
    ) -> ParsedFile:
        symbols: list[ParsedSymbol] = []
        imports: list[ImportReference] = []
        calls: list[CallReference] = []
        assignments: set[str] = set()
        diagnostics: list[dict[str, Any]] = []
        captures, has_error = _snapshot_captures(source)
        if has_error:
            error_spans = [item.span for item in captures.get("error", [])]
            locations = ", ".join(
                f"{span.start_line}:{span.start_column}" for span in error_spans[:5]
            )
            raise SyntaxError(
                f"Tree-sitter could not parse {path} reliably ({locations})"
            )
        (
            decorators_by_definition,
            raw_definitions,
            raw_imports,
            raw_calls,
            raw_assignments,
            conditional_ranges,
        ) = _prepare_raw_captures(captures)

        raw_definitions.sort(key=lambda item: (item[0], -item[1]))
        definition_records: list[tuple[int, int, ParsedSymbol]] = []
        for start_byte, end_byte, node_type, name, span, bases in raw_definitions:
            definition_range = (start_byte, end_byte)
            parent_record = _innermost_containing(definition_records, definition_range)
            parent_symbol = parent_record[2] if parent_record is not None else None
            qualified = (
                f"{parent_symbol.qualified_name}.{name}" if parent_symbol else name
            )
            if node_type == "class_definition":
                kind = "class"
            elif parent_symbol is not None and parent_symbol.kind == "class":
                kind = "method"
            else:
                kind = "function"
            symbol = ParsedSymbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                parent=parent_symbol.qualified_name if parent_symbol else None,
                span=span,
                bases=bases,
                decorators=decorators_by_definition.get(definition_range, []),
            )
            symbols.append(symbol)
            definition_records.append((start_byte, end_byte, symbol))

        for statement, span, statement_range in raw_imports:
            try:
                imports.extend(
                    _parse_import_text(
                        statement,
                        span,
                        conditional=any(
                            _pure_range_strictly_contains(conditional, statement_range)
                            for conditional in conditional_ranges
                        ),
                    )
                )
            except (SyntaxError, TypeError, ValueError) as exc:
                diagnostics.append(
                    {
                        "path": path,
                        "severity": "warning",
                        "code": "import_parse_disagreement",
                        "message": str(exc),
                        "line": span.start_line,
                        "column": span.start_column,
                    }
                )

        for expression, span, call_range in raw_calls:
            parent_record = _innermost_containing(definition_records, call_range)
            scope = parent_record[2].qualified_name if parent_record else None
            calls.append(CallReference(expression, scope, span))

        for names, assignment_range in raw_assignments:
            if _innermost_containing(definition_records, assignment_range) is None:
                assignments.update(names)
        diagnostics.extend(_ast_cross_check(path, source, symbols, imports))
        return ParsedFile(
            path=path,
            module=module,
            content_hash=hashlib.sha256(source).hexdigest(),
            is_test=is_test,
            symbols=sorted(
                symbols, key=lambda item: (item.span.start_byte, item.qualified_name)
            ),
            imports=sorted(
                imports, key=lambda item: (item.span.start_byte, item.alias)
            ),
            calls=sorted(
                calls, key=lambda item: (item.span.start_byte, item.expression)
            ),
            assignments=sorted(assignments),
            diagnostics=sorted(
                diagnostics, key=lambda item: (item.get("line", 0), item["code"])
            ),
        )


def _snapshot_captures(
    source: bytes,
) -> tuple[dict[str, list[CapturedSyntax]], bool]:
    tree = Parser(PYTHON_LANGUAGE).parse(source)
    root = tree.root_node
    has_error = root.has_error
    cursor = QueryCursor(_EXTRACTION_QUERY)
    captures = cursor.captures(root)
    line_starts = [0]
    line_starts.extend(index + 1 for index, value in enumerate(source) if value == 10)
    values: dict[str, list[CapturedSyntax]] = {}
    for name, nodes in captures.items():
        snapshots = values.setdefault(name, [])
        for node in nodes:
            snapshots.append(
                CapturedSyntax(
                    node.type,
                    _text(source, node),
                    _span_from_offsets(node.start_byte, node.end_byte, line_starts),
                )
            )
    nodes = []
    node = None
    captures.clear()
    del captures, cursor, root, tree
    gc.collect()
    return values, has_error


def _prepare_raw_captures(
    captures: dict[str, list[CapturedSyntax]],
) -> tuple[
    dict[tuple[int, int], list[ExpressionReference]],
    list[tuple[int, int, str, str, SourceSpan, list[ExpressionReference]]],
    list[tuple[str, SourceSpan, tuple[int, int]]],
    list[tuple[str, SourceSpan, tuple[int, int]]],
    list[tuple[set[str], tuple[int, int]]],
    list[tuple[int, int]],
]:
    class_definitions = captures.get("class.definition", [])
    class_names = captures.get("class.name", [])
    function_definitions = captures.get("function.definition", [])
    function_names = captures.get("function.name", [])
    call_nodes = captures.get("call", [])
    call_functions = captures.get("call.function", [])
    assignment_nodes = captures.get("assignment", [])
    assignment_left = captures.get("assignment.left", [])

    raw_definitions = []
    for definition in class_definitions:
        name = _direct_capture(definition, class_names, "class name")
        bases = []
        for base in captures.get("class.base", []):
            owners = [
                candidate
                for candidate in class_definitions
                if _pure_range_strictly_contains(candidate.byte_range, base.byte_range)
            ]
            owner = min(
                owners,
                key=lambda item: item.span.end_byte - item.span.start_byte,
                default=None,
            )
            if owner is definition:
                bases.append(ExpressionReference(base.text, base.span))
        raw_definitions.append(
            (
                definition.span.start_byte,
                definition.span.end_byte,
                definition.node_type,
                name.text,
                definition.span,
                bases,
            )
        )
    for definition in function_definitions:
        name = _direct_capture(definition, function_names, "function name")
        raw_definitions.append(
            (
                definition.span.start_byte,
                definition.span.end_byte,
                definition.node_type,
                name.text,
                definition.span,
                [],
            )
        )

    definition_ranges = [(item[0], item[1]) for item in raw_definitions]
    decorators: dict[tuple[int, int], list[ExpressionReference]] = {}
    for decorator in captures.get("decorator", []):
        candidates = [
            definition_range
            for definition_range in definition_ranges
            if definition_range[0] >= decorator.span.end_byte
        ]
        target = min(candidates, key=lambda item: item[0], default=None)
        if target is not None:
            decorators.setdefault(target, []).append(
                ExpressionReference(
                    _decorator_expression(decorator.text), decorator.span
                )
            )

    raw_imports = [
        (item.text, item.span, item.byte_range) for item in captures.get("import", [])
    ]
    raw_calls = [
        (
            _direct_capture(call, call_functions, "call function").text,
            _direct_capture(call, call_functions, "call function").span,
            call.byte_range,
        )
        for call in call_nodes
    ]
    raw_assignments = [
        (
            _assignment_names_from_text(
                _direct_capture(assignment, assignment_left, "assignment target").text
            ),
            assignment.byte_range,
        )
        for assignment in assignment_nodes
    ]
    conditional_ranges = [item.byte_range for item in captures.get("conditional", [])]
    return (
        decorators,
        raw_definitions,
        raw_imports,
        raw_calls,
        raw_assignments,
        conditional_ranges,
    )


def _direct_capture(
    container: CapturedSyntax,
    candidates: list[CapturedSyntax],
    label: str,
) -> CapturedSyntax:
    contained = [
        candidate
        for candidate in candidates
        if _pure_range_strictly_contains(container.byte_range, candidate.byte_range)
    ]
    if not contained:
        raise SyntaxError(f"Tree-sitter returned no {label} capture")
    return min(contained, key=lambda item: item.span.start_byte)


def _parse_import_text(
    statement: str, span: SourceSpan, conditional: bool
) -> list[ImportReference]:
    parsed = ast.parse(statement).body
    if len(parsed) != 1:
        raise ValueError("import statement produced more than one AST node")
    ast_node = parsed[0]
    values: list[ImportReference] = []
    if isinstance(ast_node, ast.Import):
        for alias in ast_node.names:
            values.append(
                ImportReference(
                    module=alias.name,
                    name=None,
                    alias=alias.asname or alias.name.split(".")[0],
                    level=0,
                    span=span,
                    conditional=conditional,
                )
            )
    elif isinstance(ast_node, ast.ImportFrom):
        for alias in ast_node.names:
            values.append(
                ImportReference(
                    module=ast_node.module,
                    name=alias.name,
                    alias=alias.asname or alias.name,
                    level=ast_node.level,
                    span=span,
                    conditional=conditional,
                )
            )
    else:
        raise TypeError("Tree-sitter import node did not produce a Python import AST")
    return values


def _ast_cross_check(
    path: str,
    source: bytes,
    symbols: list[ParsedSymbol],
    imports: list[ImportReference],
) -> list[dict[str, Any]]:
    try:
        tree = compile(source, path, "exec", ast.PyCF_ONLY_AST)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        return [
            {
                "path": path,
                "severity": "warning",
                "code": "ast_unavailable",
                "message": str(exc),
            }
        ]

    ast_definitions: set[tuple[str, int]] = set()
    ast_import_count = 0

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            ast_definitions.add((node.name, node.lineno))
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            ast_definitions.add((node.name, node.lineno))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            ast_definitions.add((node.name, node.lineno))
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            nonlocal ast_import_count
            ast_import_count += len(node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            nonlocal ast_import_count
            ast_import_count += len(node.names)

    Visitor().visit(tree)
    ts_definitions = {(item.name, item.span.start_line) for item in symbols}
    diagnostics: list[dict[str, Any]] = []
    if ast_definitions != ts_definitions:
        diagnostics.append(
            {
                "path": path,
                "severity": "warning",
                "code": "ast_definition_disagreement",
                "message": "Tree-sitter and runtime AST definition sets differ",
            }
        )
    if ast_import_count != len(imports):
        diagnostics.append(
            {
                "path": path,
                "severity": "warning",
                "code": "ast_import_disagreement",
                "message": "Tree-sitter and runtime AST import counts differ",
            }
        )
    return diagnostics


def _assignment_names_from_text(value: str) -> set[str]:
    try:
        statement = ast.parse(f"{value} = None").body[0]
    except SyntaxError:
        return set()
    if not isinstance(statement, ast.Assign):
        return set()
    names: set[str] = set()

    def collect(target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                collect(item)

    collect(statement.targets[0])
    return names


def _decorator_expression(value: str) -> str:
    value = value.strip()
    return value[1:].strip() if value.startswith("@") else value


def _diagnostic(
    path: str, node: Node, code: str, message: str | None = None
) -> dict[str, Any]:
    return {
        "path": path,
        "severity": "warning",
        "code": code,
        "message": message or code.replace("_", " "),
        "line": node.start_point.row + 1,
        "column": node.start_point.column,
    }


def _span_from_offsets(
    start_byte: int, end_byte: int, line_starts: list[int]
) -> SourceSpan:
    start_index = bisect.bisect_right(line_starts, start_byte) - 1
    end_index = bisect.bisect_right(line_starts, end_byte) - 1
    return SourceSpan(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=start_index + 1,
        start_column=start_byte - line_starts[start_index],
        end_line=end_index + 1,
        end_column=end_byte - line_starts[end_index],
    )


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _innermost_containing(
    records: list[tuple[int, int, ParsedSymbol]], inner: tuple[int, int]
) -> tuple[int, int, ParsedSymbol] | None:
    candidates = [
        record
        for record in records
        if _pure_range_strictly_contains((record[0], record[1]), inner)
    ]
    return min(
        candidates,
        key=lambda record: record[1] - record[0],
        default=None,
    )


def _pure_range_strictly_contains(
    outer: tuple[int, int], inner: tuple[int, int]
) -> bool:
    return outer[0] <= inner[0] and outer[1] >= inner[1] and outer != inner
