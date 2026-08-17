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
    file_path: str, code: str, symbol_name: str
) -> MultiLangExcision | None:
    """Excise a target function's body in the given source code."""
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
    statement = {
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

    return MultiLangExcision(
        path=file_path,
        symbol_name=symbol_name,
        language=lang,
        original=code,
        stubbed=stubbed_code,
        first_line=target_sym.first_body_line,
        last_line=target_sym.last_body_line,
    )
