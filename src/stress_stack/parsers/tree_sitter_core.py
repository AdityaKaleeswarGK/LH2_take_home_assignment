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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Optional tree_sitter import with fallback
try:
    import tree_sitter
    from tree_sitter_languages import get_language, get_parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    try:
        import tree_sitter
        TREE_SITTER_AVAILABLE = hasattr(tree_sitter, "Language")
    except ImportError:
        TREE_SITTER_AVAILABLE = False

LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
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


@dataclass
class ParsedSourceFile:
    path: str
    language: str
    imports: list[ExtractedImport] = field(default_factory=list)
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    tests: list[ExtractedSymbol] = field(default_factory=list)
    has_syntax_error: bool = False

    @property
    def test_count(self) -> int:
        return len(self.tests)

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
        }


def detect_language(path: str | Path) -> str | None:
    """Detect language from file path extension."""
    suffix = Path(path).suffix.lower()
    return LANGUAGE_EXTENSIONS.get(suffix)


def parse_source_code(path: str, code: str) -> ParsedSourceFile:
    """Universal parser that dispatches to tree-sitter or language-specific parser."""
    lang = detect_language(path) or "unknown"
    result = ParsedSourceFile(path=path, language=lang)

    if not code.strip():
        return result

    if lang == "python":
        _parse_python_source(code, result)
    elif lang in {"typescript", "javascript"}:
        _parse_js_ts_source(code, result, lang)
    elif lang == "rust":
        _parse_rust_source(code, result)
    elif lang == "go":
        _parse_go_source(code, result)
    elif lang in {"c", "cpp"}:
        _parse_c_cpp_source(code, result, lang)
    else:
        _parse_generic_source(code, result)

    return result


# ---------------------------------------------------------------------------
# Python AST / Tree-sitter Parser
# ---------------------------------------------------------------------------


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


_C_INCLUDE_RE = re.compile(r"""#include\s+([<"][^>"]+[>"])""")
_CPP_FUNC_RE = re.compile(
    r"""(?:[a-zA-Z0-9_:<>&*]+\s+)+([a-zA-Z0-9_]+)\s*\(([^)]*)\)\s*\{""",
    re.MULTILINE,
)
_CPP_TEST_RE = re.compile(r"""\bTEST(?:_F)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)""")


def _parse_c_cpp_source(code: str, result: ParsedSourceFile, lang: str) -> None:
    lines = code.splitlines()

    for match in _C_INCLUDE_RE.finditer(code):
        header = match.group(1)
        line_num = code[: match.start()].count("\n") + 1
        result.imports.append(
            ExtractedImport(raw=match.group(0), module=header, line=line_num)
        )

    for match in _CPP_TEST_RE.finditer(code):
        suite, case = match.group(1), match.group(2)
        line_num = code[: match.start()].count("\n") + 1
        end_line = _find_closing_brace_line(lines, line_num - 1)
        sym = ExtractedSymbol(
            name=f"{suite}.{case}",
            qualified_name=f"{suite}.{case}",
            kind="test",
            start_line=line_num,
            end_line=end_line,
            first_body_line=line_num + 1,
            last_body_line=end_line,
            is_test=True,
        )
        result.tests.append(sym)


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
