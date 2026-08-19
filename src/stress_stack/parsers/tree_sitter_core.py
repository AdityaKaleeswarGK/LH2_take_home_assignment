"""Multi-language Tree-Sitter parser and AST extraction engine.

Integrates Tree-Sitter grammar parsing from DGAT and AlphaStack to provide
universal, language-agnostic extraction of:
- Imports and module dependencies (static and dynamic)
- Symbol definitions (Functions, Methods, Classes, Structs, Traits, Interfaces)
- Test functions and suites
- Function body ranges (for Red-Green Excision benchmark tasks)

Supports: Python, TypeScript, JavaScript, Rust, Go, C, C++.
Falls back gracefully to standard Python AST or regex when tree-sitter grammars
are not locally compiled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def tree_sitter_available() -> bool:
    """Whether real grammars are loadable, resolved through the backend.

    Previously this module probed `tree_sitter_languages`, a package that was
    neither installed nor declared, so the flag was always False and every
    non-Python language was silently parsed by regex. Routing the question to
    the backend keeps one answer instead of two that can disagree.
    """
    from stress_stack.parsers.tree_sitter_backend import TREE_SITTER_AVAILABLE

    return TREE_SITTER_AVAILABLE


LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    # Its own grammar — see the note beside SPECS["tsx"] in the backend.
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    # `.h` is ambiguous — C and C++ share it, and the majority of real `.h`
    # files in a mixed tree are C++ headers. The C++ grammar parses the C
    # constructs this layer extracts, so choosing it fails on strictly fewer
    # files than choosing C does; parsing json.hpp-style headers as C produced
    # a syntax error on almost every one.
}


@dataclass(frozen=True, slots=True)
class ExtractedImport:
    raw: str
    module: str
    symbols: list[str] = field(default_factory=list)
    is_dynamic: bool = False
    line: int = 1


@dataclass(frozen=True, slots=True)
class ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str  # function, method, class, struct, trait, interface
    start_line: int
    end_line: int
    first_body_line: int
    last_body_line: int
    is_test: bool = False
    is_async: bool = False
    is_generator: bool = False
    docstring: str = ""
    # Exact byte span of the body, including its delimiters, when a grammar
    # supplied one. Line numbers cannot express a single-line definition —
    # `func Mul(a, b int) int { return a * b }` has its body on the signature's
    # line, so replacing that line range deletes the declaration too.
    body_start_byte: int | None = None
    body_end_byte: int | None = None
    # The file this symbol was extracted from. Carried on the symbol so it can
    # answer `.id` the way the Python graph's ParsedSymbol does — every consumer
    # that walks a graph keys on `path::name`, and a symbol that cannot say
    # which file it came from forces each of them to reconstruct it.
    path: str = ""

    @property
    def id(self) -> str:
        """`path::name`, matching `symbols.ParsedSymbol.id`."""
        return f"{self.path}::{self.name}" if self.path else self.name


@dataclass
class ParsedSourceFile:
    path: str
    language: str
    imports: list[ExtractedImport] = field(default_factory=list)
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    tests: list[ExtractedSymbol] = field(default_factory=list)
    has_syntax_error: bool = False
    # Which parser produced this. "regex" is a fallback that cannot find a
    # function body's true extent, so a graph built from one must not be
    # reported as verified — a C++ file parsed by regex yields no symbols and
    # no syntax error, which reads identically to a file with nothing in it.
    # Recording the parser is what lets `validate_graph` tell those apart.
    parser: str = "regex"

    @property
    def test_count(self) -> int:
        return len(self.tests)

    # `module` and `is_test` exist so this file description satisfies the same
    # readers as the Python graph's ParsedFile — `candidates.module_index` and
    # `candidates.test_paths` need exactly these two attributes, and duplicating
    # those functions for a second graph type would mean two rankers to keep in
    # agreement.
    @property
    def module(self) -> str:
        """Dotted module name derived from the path, without its extension."""
        stem = self.path.rsplit(".", 1)[0] if "." in self.path.rsplit("/", 1)[-1] else self.path
        return stem.replace("/", ".").strip(".")

    @property
    def is_test(self) -> bool:
        if self.tests:
            return True
        name = self.path.rsplit("/", 1)[-1].lower()
        return (
            name.startswith("test")
            or "_test." in name
            or ".test." in name
            or ".spec." in name
        )

    @property
    def syntax_error(self) -> bool:
        """Alias matching the Python graph's field name."""
        return self.has_syntax_error

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "imports": [
                {"raw": imp.raw, "module": imp.module, "symbols": imp.symbols, "line": imp.line}
                for imp in self.imports
            ],
            "symbols": [
                {
                    "name": s.name,
                    "qualified_name": s.qualified_name,
                    "kind": s.kind,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "first_body_line": s.first_body_line,
                    "last_body_line": s.last_body_line,
                    "is_test": s.is_test,
                }
                for s in self.symbols
            ],
            "tests": [t.name for t in self.tests],
            "has_syntax_error": self.has_syntax_error,
            "parser": self.parser,
        }


def detect_language(path: str | Path) -> str | None:
    """Detect language from file path extension."""
    suffix = Path(path).suffix.lower()
    return LANGUAGE_EXTENSIONS.get(suffix)


def parse_source_code(path: str, code: str, *, prefer_tree_sitter: bool = True) -> ParsedSourceFile:
    """Parse a source file, preferring a real grammar over the regex fallback.

    Python keeps using `ast` even when tree-sitter is available: it is the same
    parser CPython uses, it resolves dotted names and decorators without a
    node-type table, and the rest of the pipeline is already built on its
    output. Every other language goes through tree-sitter when the grammar
    loads, and falls back to the regex extractors when it does not.
    """
    lang = detect_language(path) or "unknown"
    result = ParsedSourceFile(path=path, language=lang)

    if not code.strip():
        return result

    if prefer_tree_sitter and lang != "python":
        parsed = _tree_sitter_parse(code, lang, result)
        if parsed:
            return _stamp_paths(result)

    if lang == "python":
        result.parser = "ast"
        _parse_python_source(code, result)
    elif lang in {"typescript", "javascript"}:
        _parse_js_ts_source(code, result, lang)
    elif lang == "rust":
        _parse_rust_source(code, result)
    elif lang == "go":
        _parse_go_source(code, result)
    else:
        _parse_generic_source(code, result)

    return _stamp_paths(result)


def _stamp_paths(result: ParsedSourceFile) -> ParsedSourceFile:
    """Give every extracted symbol the file it came from.

    Done here rather than at each construction site: there are a dozen of those
    across six extractors and the tree-sitter bridge, and one that forgot would
    produce a symbol whose `.id` silently lost its path.
    """
    result.symbols = [replace(s, path=result.path) for s in result.symbols]
    by_name = {(s.name, s.start_line): s for s in result.symbols}
    result.tests = [by_name.get((t.name, t.start_line), replace(t, path=result.path))
                    for t in result.tests]
    return result


# ---------------------------------------------------------------------------
# Python AST / Tree-sitter Parser
# ---------------------------------------------------------------------------


def _tree_sitter_parse(code: str, lang: str, result: ParsedSourceFile) -> bool:
    """Fill `result` from a real grammar. Returns False if that was not possible."""
    from stress_stack.parsers import tree_sitter_backend as backend

    parsed = backend.parse(result.path, code, lang)
    if parsed is None:
        return False

    for entry in parsed["symbols"]:
        symbol = ExtractedSymbol(
            name=entry["name"],
            qualified_name=entry["name"],
            kind=entry["kind"],
            start_line=entry["start_line"],
            end_line=entry["end_line"],
            first_body_line=entry["first_body_line"],
            last_body_line=entry["last_body_line"],
            is_test=entry["is_test"],
            body_start_byte=entry.get("body_start_byte"),
            body_end_byte=entry.get("body_end_byte"),
        )
        result.symbols.append(symbol)
        if symbol.is_test:
            result.tests.append(symbol)

    for entry in parsed["imports"]:
        raw = entry["raw"]
        result.imports.append(
            ExtractedImport(raw=raw, module=_import_module(raw, lang), line=entry["line"])
        )

    result.has_syntax_error = parsed["has_syntax_error"]
    result.parser = "tree_sitter"
    return True


def _import_module(raw: str, lang: str) -> str:
    """Reduce an import statement to the module path it names."""
    text = raw.strip().rstrip(";")
    if lang == "rust":
        return text.removeprefix("use ").strip()
    if lang == "go":
        return text.removeprefix("import ").strip().strip('"')
    if lang in {"javascript", "typescript"}:
        # `import x from 'mod'` — the module is the quoted tail.
        for quote in ("'", '"'):
            if quote in text:
                return text.rsplit(quote, 2)[-2] if text.count(quote) >= 2 else text
    return text


def _parse_python_source(code: str, result: ParsedSourceFile) -> None:
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError:
        result.has_syntax_error = True
        return

    lines = code.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.imports.append(
                    ExtractedImport(
                        raw=f"import {alias.name}",
                        module=alias.name,
                        line=getattr(node, "lineno", 1),
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            syms = [a.name for a in node.names]
            result.imports.append(
                ExtractedImport(
                    raw=f"from {mod} import {', '.join(syms)}",
                    module=mod,
                    symbols=syms,
                    line=getattr(node, "lineno", 1),
                )
            )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sym = _build_py_function_symbol(node, lines, parent="")
            result.symbols.append(sym)
            if sym.is_test:
                result.tests.append(sym)
        elif isinstance(node, ast.ClassDef):
            is_test_cls = node.name.startswith("Test")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sym = _build_py_function_symbol(item, lines, parent=node.name)
                    if is_test_cls and sym.name.startswith("test_"):
                        sym = ExtractedSymbol(
                            name=sym.name,
                            qualified_name=sym.qualified_name,
                            kind="method",
                            start_line=sym.start_line,
                            end_line=sym.end_line,
                            first_body_line=sym.first_body_line,
                            last_body_line=sym.last_body_line,
                            is_test=True,
                        )
                        result.tests.append(sym)
                    result.symbols.append(sym)


def _build_py_function_symbol(
    node: Any, lines: list[str], parent: str = ""
) -> ExtractedSymbol:
    import ast

    name = node.name
    qname = f"{parent}.{name}" if parent else name
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start)

    doc = ast.get_docstring(node) or ""
    first_body = start + 1
    if node.body:
        first_body = getattr(node.body[0], "lineno", start + 1)

    is_test = name.startswith("test_")
    is_gen = any(
        isinstance(sub, (ast.Yield, ast.YieldFrom)) for sub in ast.walk(node)
    )

    return ExtractedSymbol(
        name=name,
        qualified_name=qname,
        kind="method" if parent else "function",
        start_line=start,
        end_line=end,
        first_body_line=first_body,
        last_body_line=end,
        is_test=is_test,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_generator=is_gen,
        docstring=doc,
    )


# ---------------------------------------------------------------------------
# TypeScript / JavaScript Parser
# ---------------------------------------------------------------------------


_JS_IMPORT_RE = re.compile(
    r"""import\s+(?:(?:(\w+)|\{([^}]+)\}|\*\s+as\s+(\w+))\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_JS_REQUIRE_RE = re.compile(
    r"""(?:const|let|var)\s+(?:(\w+)|\{([^}]+)\})\s*=\s*require\(['"]([^'"]+)['"]\)""",
    re.MULTILINE,
)
_JS_FUNC_RE = re.compile(
    r"""(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(([^)]*)\)""",
    re.MULTILINE,
)
_JS_TEST_RE = re.compile(
    r"""\b(it|test|describe)\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE,
)


def _parse_js_ts_source(code: str, result: ParsedSourceFile, lang: str) -> None:
    lines = code.splitlines()

    # Imports
    for match in _JS_IMPORT_RE.finditer(code):
        mod = match.group(4)
        syms_str = match.group(2) or ""
        syms = [s.strip() for s in syms_str.split(",") if s.strip()]
        line_num = code[: match.start()].count("\n") + 1
        result.imports.append(
            ExtractedImport(raw=match.group(0), module=mod, symbols=syms, line=line_num)
        )

    for match in _JS_REQUIRE_RE.finditer(code):
        mod = match.group(3)
        line_num = code[: match.start()].count("\n") + 1
        result.imports.append(
            ExtractedImport(raw=match.group(0), module=mod, line=line_num, is_dynamic=True)
        )

    # Functions
    for match in _JS_FUNC_RE.finditer(code):
        name = match.group(1)
        line_num = code[: match.start()].count("\n") + 1
        end_line = _find_closing_brace_line(lines, line_num - 1)
        sym = ExtractedSymbol(
            name=name,
            qualified_name=name,
            kind="function",
            start_line=line_num,
            end_line=end_line,
            first_body_line=line_num + 1,
            last_body_line=end_line,
            is_test=False,
        )
        result.symbols.append(sym)

    # Tests
    for match in _JS_TEST_RE.finditer(code):
        t_type = match.group(1)
        t_title = match.group(2)
        line_num = code[: match.start()].count("\n") + 1
        end_line = _find_closing_brace_line(lines, line_num - 1)
        sym = ExtractedSymbol(
            name=f"{t_type}('{t_title}')",
            qualified_name=f"{t_type}('{t_title}')",
            kind="test",
            start_line=line_num,
            end_line=end_line,
            first_body_line=line_num + 1,
            last_body_line=end_line,
            is_test=True,
        )
        result.tests.append(sym)


# ---------------------------------------------------------------------------
# Rust Parser
# ---------------------------------------------------------------------------


_RS_USE_RE = re.compile(r"""use\s+([^;]+);""", re.MULTILINE)
_RS_FN_RE = re.compile(
    r"""(?:pub\s+)?(?:async\s+)?fn\s+([a-zA-Z0-9_]+)\s*(?:<[^>]+>)?\s*\(([^)]*)\)""",
    re.MULTILINE,
)
_RS_TEST_ATTR_RE = re.compile(r"""#\[(?:tokio::)?test\]""")


def _parse_rust_source(code: str, result: ParsedSourceFile) -> None:
    lines = code.splitlines()

    for match in _RS_USE_RE.finditer(code):
        mod = match.group(1).strip()
        line_num = code[: match.start()].count("\n") + 1
        result.imports.append(
            ExtractedImport(raw=match.group(0), module=mod, line=line_num)
        )

    for match in _RS_FN_RE.finditer(code):
        name = match.group(1)
        line_num = code[: match.start()].count("\n") + 1
        end_line = _find_closing_brace_line(lines, line_num - 1)

        # Check preceding 3 lines for #[test]
        preceding = "\n".join(lines[max(0, line_num - 4) : line_num - 1])
        is_test = bool(_RS_TEST_ATTR_RE.search(preceding))

        sym = ExtractedSymbol(
            name=name,
            qualified_name=name,
            kind="function",
            start_line=line_num,
            end_line=end_line,
            first_body_line=line_num + 1,
            last_body_line=end_line,
            is_test=is_test,
        )
        result.symbols.append(sym)
        if is_test:
            result.tests.append(sym)


# ---------------------------------------------------------------------------
# Go Parser
# ---------------------------------------------------------------------------


_GO_IMPORT_SINGLE_RE = re.compile(r"""import\s+['"]([^'"]+)['"]""")
_GO_IMPORT_MULTI_RE = re.compile(r"""import\s*\(([^)]+)\)""", re.MULTILINE)
_GO_FUNC_RE = re.compile(
    r"""func\s+(?:\((?:[^)]+)\)\s+)?([a-zA-Z0-9_]+)\s*\(([^)]*)\)""",
    re.MULTILINE,
)


def _parse_go_source(code: str, result: ParsedSourceFile) -> None:
    lines = code.splitlines()

    for match in _GO_IMPORT_SINGLE_RE.finditer(code):
        mod = match.group(1).strip()
        line_num = code[: match.start()].count("\n") + 1
        result.imports.append(
            ExtractedImport(raw=match.group(0), module=mod, line=line_num)
        )

    for match in _GO_IMPORT_MULTI_RE.finditer(code):
        block = match.group(1)
        for sub_match in re.finditer(r"""['"]([^'"]+)['"]""", block):
            mod = sub_match.group(1).strip()
            line_num = code[: match.start()].count("\n") + 1
            result.imports.append(
                ExtractedImport(raw=sub_match.group(0), module=mod, line=line_num)
            )

    for match in _GO_FUNC_RE.finditer(code):
        name = match.group(1)
        line_num = code[: match.start()].count("\n") + 1
        end_line = _find_closing_brace_line(lines, line_num - 1)
        is_test = name.startswith("Test") or name.startswith("Benchmark")

        sym = ExtractedSymbol(
            name=name,
            qualified_name=name,
            kind="function",
            start_line=line_num,
            end_line=end_line,
            first_body_line=line_num + 1,
            last_body_line=end_line,
            is_test=is_test,
        )
        result.symbols.append(sym)
        if is_test:
            result.tests.append(sym)


# ---------------------------------------------------------------------------
# C / C++ Parser
# ---------------------------------------------------------------------------


def _parse_generic_source(code: str, result: ParsedSourceFile) -> None:
    lines = code.splitlines()
    for num, line in enumerate(lines, start=1):
        if line.strip().startswith(("import ", "from ", "use ", "#include")):
            result.imports.append(
                ExtractedImport(raw=line.strip(), module=line.strip(), line=num)
            )


def _find_closing_brace_line(lines: list[str], start_idx: int) -> int:
    """Find line index of the matching closing curly brace."""
    depth = 0
    found_open = False
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        for char in line:
            if char == "{":
                depth += 1
                found_open = True
            elif char == "}":
                depth -= 1
                if found_open and depth <= 0:
                    return idx + 1
    return min(start_idx + 10, len(lines))
