"""Real tree-sitter extraction, driven by a per-language node-type table.

The regex parsers this replaces could not answer the question excision actually
asks — *where does this function's body start and end* — because a regex cannot
match balanced delimiters. Brace counting gets it wrong on a string literal
containing `}`, on a nested closure, on a raw string, and on any macro that
expands to unbalanced text. Tree-sitter gives the body node's exact byte range,
so a stub replaces precisely the body and nothing else.

Language support is a table of node type names rather than six parsers: every
grammar names its declarations differently, but the shape of the question —
"which nodes are callable definitions, which are imports, which are tests" — is
the same, so only the names vary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

try:  # pragma: no cover - exercised by availability, not by branch
    from tree_sitter_language_pack import get_parser

    TREE_SITTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    get_parser = None  # type: ignore[assignment]
    TREE_SITTER_AVAILABLE = False


@dataclass(frozen=True)
class LanguageSpec:
    """Which node types carry meaning, for one grammar."""

    # Node types that declare something callable or type-like.
    definitions: dict[str, str] = field(default_factory=dict)  # node type -> kind
    # Node types that bring names into scope.
    imports: tuple[str, ...] = ()
    # Field name holding the body, tried in order.
    body_fields: tuple[str, ...] = ("body",)
    # Given a symbol name and its node, decide whether it is a test.
    is_test: Callable[[str, Any], bool] | None = None
    # Frameworks that declare tests by *calling* a function rather than
    # defining one — `describe('…', () => …)`, `it('…', …)`. These produce no
    # declaration node at all, so walking definitions alone misses every test
    # in a JavaScript or TypeScript suite.
    test_call_names: frozenset[str] = frozenset()


def _python_is_test(name: str, node: Any) -> bool:
    return name.startswith("test")


def _go_is_test(name: str, node: Any) -> bool:
    # Go's testing package requires exactly this prefix with a capital letter.
    return name.startswith("Test") or name.startswith("Benchmark") or name.startswith("Fuzz")


def _rust_is_test(name: str, node: Any) -> bool:
    """Rust marks tests with an attribute, not a name convention."""
    previous = node.prev_named_sibling
    while previous is not None and previous.type == "attribute_item":
        if b"test" in previous.text:
            return True
        previous = previous.prev_named_sibling
    return name.startswith("test_")


def _js_is_test(name: str, node: Any) -> bool:
    return name.startswith("test") or name.startswith("it") or name.startswith("describe")


def _cpp_is_test(name: str, node: Any) -> bool:
    return name.startswith("TEST") or name.startswith("test")


SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        definitions={
            "function_definition": "function",
            "class_definition": "class",
            "decorated_definition": "function",
        },
        imports=("import_statement", "import_from_statement"),
        is_test=_python_is_test,
    ),
    "rust": LanguageSpec(
        definitions={
            "function_item": "function",
            "struct_item": "struct",
            "trait_item": "trait",
            "enum_item": "enum",
            "impl_item": "impl",
        },
        imports=("use_declaration",),
        is_test=_rust_is_test,
    ),
    "go": LanguageSpec(
        definitions={
            "function_declaration": "function",
            "method_declaration": "method",
            "type_declaration": "type",
        },
        imports=("import_declaration",),
        is_test=_go_is_test,
    ),
    "javascript": LanguageSpec(
        definitions={
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
            "generator_function_declaration": "function",
        },
        imports=("import_statement",),
        # An arrow function assigned to a name lives under a body of
        # `statement_block`; the declaration itself has no `body` field.
        body_fields=("body", "statement_block"),
        is_test=_js_is_test,
        test_call_names=frozenset({"describe", "it", "test", "suite", "bench"}),
    ),
    "c": LanguageSpec(
        definitions={"function_definition": "function", "struct_specifier": "struct"},
        imports=("preproc_include",),
        is_test=_cpp_is_test,
    ),
    "cpp": LanguageSpec(
        definitions={
            "function_definition": "function",
            "class_specifier": "class",
            "struct_specifier": "struct",
        },
        imports=("preproc_include",),
        is_test=_cpp_is_test,
    ),
}
# TypeScript shares JavaScript's node names for everything extracted here.
SPECS["typescript"] = SPECS["javascript"]
# TSX is a separate grammar, not a dialect flag: parsing a `.tsx` file with the
# plain TypeScript grammar reports a syntax error on the first JSX element, so
# React codebases came back as almost entirely unparseable.
SPECS["tsx"] = SPECS["javascript"]


def supported_languages() -> frozenset[str]:
    return frozenset(SPECS) if TREE_SITTER_AVAILABLE else frozenset()


def _first_identifier(text: str) -> str | None:
    """Reduce a name node's text to the bare identifier.

    A C++ partial specialisation names itself with its whole template argument
    list, so the raw text spans several lines. A symbol name containing
    newlines is never usable as an identifier — for excision, for a task
    statement, or for an anchor check — so keep the leading identifier only.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    for index, character in enumerate(cleaned):
        if not (character.isalnum() or character in "_$"):
            cleaned = cleaned[:index]
            break
    return cleaned or None


def _node_name(node: Any) -> str | None:
    """Best-effort declared name for a definition node.

    C and C++ nest the name inside a declarator chain (`int *f(void)` puts `f`
    under `pointer_declarator > function_declarator > identifier`), so a plain
    `name` field lookup is not enough.
    """
    named = node.child_by_field_name("name")
    if named is not None:
        return _first_identifier(named.text.decode("utf-8", errors="replace"))

    declarator = node.child_by_field_name("declarator")
    seen = 0
    while declarator is not None and seen < 8:
        if declarator.type in {"identifier", "field_identifier", "type_identifier"}:
            return _first_identifier(declarator.text.decode("utf-8", errors="replace"))
        inner = declarator.child_by_field_name("declarator")
        if inner is None:
            for child in declarator.named_children:
                if child.type in {"identifier", "field_identifier", "qualified_identifier"}:
                    return _first_identifier(child.text.decode("utf-8", errors="replace"))
            break
        declarator = inner
        seen += 1
    return None


def _body_node(node: Any, spec: LanguageSpec) -> Any | None:
    for field_name in spec.body_fields:
        body = node.child_by_field_name(field_name)
        if body is not None:
            return body
    # Some grammars (Rust `function_item`) expose the body only as a typed child.
    for child in node.named_children:
        if child.type in {"block", "statement_block", "compound_statement", "field_declaration_list"}:
            return child
    return None


def walk_definitions(root: Any, spec: LanguageSpec) -> list[tuple[Any, str]]:
    """Every definition node in the tree, with the kind the spec assigns it."""
    found: list[tuple[Any, str]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        kind = spec.definitions.get(node.type)
        if kind is not None:
            # `decorated_definition` wraps the real definition; record the inner
            # node so line ranges cover the decorators but the name is correct.
            found.append((node, kind))
        stack.extend(reversed(node.named_children))
    return found


def _callee_name(node: Any) -> str | None:
    """Root identifier of a call's callee: `it` for both `it` and `it.each`."""
    function = node.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "identifier":
        return function.text.decode("utf-8", errors="replace")
    if function.type == "member_expression":
        obj = function.child_by_field_name("object")
        if obj is not None and obj.type == "identifier":
            return obj.text.decode("utf-8", errors="replace")
    return None


def _call_declared_tests(root: Any, spec: LanguageSpec) -> list[dict[str, Any]]:
    """Tests registered by calling a framework function with a name string."""
    found: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression" and _callee_name(node) in spec.test_call_names:
            arguments = node.child_by_field_name("arguments")
            label = ""
            if arguments is not None:
                for argument in arguments.named_children:
                    if argument.type in {"string", "template_string"}:
                        # Strip the surrounding quote characters.
                        label = argument.text.decode("utf-8", errors="replace")[1:-1]
                        break
            found.append(
                {
                    "name": label or _callee_name(node) or "anonymous",
                    "kind": "test",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "first_body_line": node.start_point[0] + 1,
                    "last_body_line": node.end_point[0] + 1,
                    "body_start_byte": None,
                    "body_end_byte": None,
                    "is_test": True,
                }
            )
        stack.extend(reversed(node.named_children))
    return found


def parse(path: str, code: str, language: str) -> dict[str, Any] | None:
    """Parse with tree-sitter, or return None when unavailable for this language."""
    if not TREE_SITTER_AVAILABLE or language not in SPECS:
        return None
    try:
        parser = get_parser(language)  # type: ignore[misc]
    except Exception:
        return None

    source = code.encode("utf-8")
    tree = parser.parse(source)
    spec = SPECS[language]

    symbols: list[dict[str, Any]] = []
    for node, kind in walk_definitions(tree.root_node, spec):
        target = node
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is None:
                continue
            target = inner
            kind = "class" if inner.type == "class_definition" else "function"
        name = _node_name(target)
        if not name:
            continue
        body = _body_node(target, spec)
        is_test = bool(spec.is_test and spec.is_test(name, node))
        symbols.append(
            {
                "name": name,
                "kind": kind,
                # Rows are 0-indexed in tree-sitter and 1-indexed everywhere else.
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "first_body_line": (body.start_point[0] + 1) if body else node.start_point[0] + 1,
                "last_body_line": (body.end_point[0] + 1) if body else node.end_point[0] + 1,
                "body_start_byte": body.start_byte if body else None,
                "body_end_byte": body.end_byte if body else None,
                "is_test": is_test,
            }
        )

    if spec.test_call_names:
        symbols.extend(_call_declared_tests(tree.root_node, spec))

    imports: list[dict[str, Any]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in spec.imports:
            raw = node.text.decode("utf-8", errors="replace").strip()
            imports.append({"raw": raw, "line": node.start_point[0] + 1})
        stack.extend(reversed(node.named_children))

    return {
        "symbols": symbols,
        "imports": imports,
        # `has_error` is the grammar's own verdict on whether the file parsed —
        # far more reliable than a regex noticing nothing matched.
        "has_syntax_error": tree.root_node.has_error,
    }
