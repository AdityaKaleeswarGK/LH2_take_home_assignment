"""Language-agnostic AST function body excision engine.

Removes function implementations while preserving function signatures,
contracts, and docstrings across:
- Python (raise NotImplementedError / return None)
- Rust (todo!() / Default::default())
- TypeScript / JavaScript (throw new Error("Not implemented") / return undefined)
- Go (panic("not implemented") / return)
- C++ (throw std::runtime_error("Not implemented"))
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from stress_stack.parsers.tree_sitter_core import (
    ExtractedSymbol,
    detect_language,
    parse_source_code,
)


@dataclass(frozen=True, slots=True)
class MultiLangExcision:
    path: str
    symbol_name: str
    language: str
    original: str
    stubbed: str
    first_line: int
    last_line: int

    def diff(self) -> str:
        """The golden answer: unified diff required to restore the function body."""
        lines = difflib.unified_diff(
            self.stubbed.splitlines(keepends=True),
            self.original.splitlines(keepends=True),
            fromfile=f"a/{self.path}",
            tofile=f"b/{self.path}",
            n=3,
        )
        return "".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbol": self.symbol_name,
            "language": self.language,
            "first_line": self.first_line,
            "last_line": self.last_line,
            "diff_size": len(self.diff().splitlines()),
        }


def _indent_of(code: str, byte_offset: int) -> str:
    """Leading whitespace of the line the given byte offset falls on."""
    prefix = code.encode("utf-8")[:byte_offset].decode("utf-8", errors="replace")
    line = prefix.rsplit("\n", 1)[-1] if "\n" in prefix else prefix
    return line[: len(line) - len(line.lstrip())]


def excise_symbol(
    file_path: str, code: str, symbol_name: str, marker: str | None = None
) -> MultiLangExcision | None:
    """Excise a target function's body in the given source code.

    ``marker`` overrides the built-in "not implemented" statement for the
    tree-sitter languages. It comes from the resolved workflow, which probes it
    by excising a real symbol and re-parsing the result — so an ecosystem with
    no entry in the table below can still be excised, and one whose idiom the
    table has wrong can be corrected without editing it.

    Python ignores it, and that is not an oversight. Its excision has two
    strategies rather than one statement: a *neutral* body, which is the
    stronger stub because a test failing on an assertion against it pins
    behaviour rather than the shape of an exception, and an explicit raise as
    the fallback. `validate.build_and_validate` chooses between them per
    candidate on measured evidence. A single marker cannot express that, so
    substituting one would be a downgrade dressed as a generalisation.
    """
    lang = detect_language(file_path) or "python"

    if lang == "python":
        from stress_stack.excision import EXPLICIT, excise, plan_excision

        try:
            plan = plan_excision(code, symbol_name, path=file_path)
            exc = excise(code, symbol_name, strategy=EXPLICIT, path=file_path)
            return MultiLangExcision(
                path=file_path,
                symbol_name=symbol_name,
                language="python",
                original=exc.original,
                stubbed=exc.stubbed,
                first_line=plan.first_body_line,
                last_line=plan.last_body_line,
            )
        except Exception:
            return None

    # Multi-language AST excision
    parsed = parse_source_code(file_path, code)
    target_sym: ExtractedSymbol | None = None
    for sym in parsed.symbols:
        if sym.name == symbol_name or sym.qualified_name == symbol_name:
            target_sym = sym
            break

    if not target_sym:
        return None

    # The idiomatic "not implemented" marker for each ecosystem.
    statement = marker or {
        "rust": "todo!()",
        "typescript": 'throw new Error("Not implemented");',
        "javascript": 'throw new Error("Not implemented");',
        "tsx": 'throw new Error("Not implemented");',
        "go": 'panic("not implemented")',
        "c": 'throw std::runtime_error("Not implemented");',
        "cpp": 'throw std::runtime_error("Not implemented");',
    }.get(lang, 'raise NotImplementedError("Not implemented")')

    start, end = target_sym.body_start_byte, target_sym.body_end_byte
    if start is None or end is None or start >= end:
        return None

    raw = code.encode("utf-8")
    body = raw[start:end].decode("utf-8", errors="replace")
    # Replace the body's interior and keep its delimiters, so a brace-delimited
    # language keeps its block and the declaration stays syntactically whole.
    # Line-range replacement could not do this: a single-line definition shares
    # its line with the signature, and rewriting that line deleted the function.
    if body.startswith("{") and body.endswith("}"):
        indent = _indent_of(code, start)
        replacement = "{\n" + indent + "    " + statement + "\n" + indent + "}"
    else:
        replacement = statement

    stubbed_code = (
        raw[:start].decode("utf-8", errors="replace")
        + replacement
        + raw[end:].decode("utf-8", errors="replace")
    )
    stubbed_code = _prune_orphaned_imports(stubbed_code, file_path, lang)

    return MultiLangExcision(
        path=file_path,
        symbol_name=symbol_name,
        language=lang,
        original=code,
        stubbed=stubbed_code,
        first_line=target_sym.first_body_line,
        last_line=target_sym.last_body_line,
    )


# Languages where an import nothing uses is a compile error rather than a
# warning. Everywhere else the orphan is harmless and removing it would be an
# edit the task did not ask for.
_IMPORTS_MUST_BE_USED = frozenset({"go"})


def _prune_orphaned_imports(code: str, file_path: str, lang: str) -> str:
    """Drop imports the excision just orphaned, where that is a build error.

    Removing a function body removes its references, and in Go an import nothing
    uses stops the package compiling: `./conv.go:3:8: "fmt" imported and not
    used`. The task then fails its fail-before gate as a *build* failure, which
    the brief says does not count — so the candidate is correctly rejected and
    the repository yields nothing. Measured on a table-driven Go fixture, this
    was the difference between one eligible excision task and none.

    Go-only and written against Go's import syntax on purpose. A grammar-neutral
    line-drop looked general and was wrong: tree-sitter reports every spec in a
    grouped `import ( ... )` block against the block's own line, so dropping
    "the import's line" deleted the `import (` and left the specs orphaned. Go
    is currently the only ecosystem here where an unused import is fatal, and a
    second one should get its own clause rather than a shared guess.

    The prune is narrow. A name still used anywhere outside the import block
    survives, so a solver restoring the body must restore the import too —
    which is part of the work, and is in the golden diff either way.
    """
    del file_path
    if lang not in _IMPORTS_MUST_BE_USED:
        return code

    body = _GO_IMPORT_BLOCK.sub("", _GO_IMPORT_LINE.sub("", code))

    def _used(spec: str) -> bool:
        """Whether this import's local name is still referenced."""
        match = _GO_IMPORT_SPEC.match(spec.strip())
        if match is None:
            return True
        alias, path = match.group("alias"), match.group("path")
        if alias in {"_", "."}:
            # A blank import is for its side effects and a dot import injects
            # names directly; neither can be judged by looking for a qualifier.
            return True
        local = alias or path.rstrip("/").rsplit("/", 1)[-1]
        return bool(re.search(rf"\b{re.escape(local)}\s*\.", body))

    def _rewrite_block(match: re.Match[str]) -> str:
        kept = [line for line in match.group("specs").splitlines() if _used(line)]
        if not any(line.strip() for line in kept):
            return ""
        return "import (\n" + "\n".join(kept) + "\n)\n"

    code = _GO_IMPORT_BLOCK.sub(_rewrite_block, code)
    return _GO_IMPORT_LINE.sub(lambda m: m.group(0) if _used(m.group("spec")) else "", code)


# Go's two import forms. Simple and regular enough to read directly, which is
# what the grouped case needs — see `_prune_orphaned_imports`.
_GO_IMPORT_BLOCK = re.compile(r"^import\s*\(\n(?P<specs>.*?)^\)\n", re.MULTILINE | re.DOTALL)
_GO_IMPORT_LINE = re.compile(r"^import\s+(?P<spec>[^\n(]+)\n", re.MULTILINE)
_GO_IMPORT_SPEC = re.compile(r'^(?:(?P<alias>[\w.]+)\s+)?"(?P<path>[^"]+)"')
