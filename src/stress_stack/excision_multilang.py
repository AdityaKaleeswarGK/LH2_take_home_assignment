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
from pathlib import Path
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

    lines = code.splitlines()
    first_idx = target_sym.first_body_line - 1
    last_idx = target_sym.last_body_line - 1

    if first_idx >= len(lines) or last_idx >= len(lines) or first_idx > last_idx:
        return None

    # Determine indentation of the body
    body_line = lines[first_idx]
    indent = " " * (len(body_line) - len(body_line.lstrip()))
    if not indent:
        indent = "    "

    # Idiomatic stub per language
    if lang == "rust":
        stub_line = f"{indent}todo!()"
    elif lang in {"typescript", "javascript"}:
        stub_line = f'{indent}throw new Error("Not implemented");'
    elif lang == "go":
        stub_line = f'{indent}panic("not implemented")'
    elif lang in {"c", "cpp"}:
        stub_line = f'{indent}throw std::runtime_error("Not implemented");'
    else:
        stub_line = f'{indent}raise NotImplementedError("Not implemented")'

    stubbed_lines = lines[:first_idx] + [stub_line] + lines[last_idx + 1 :]
    stubbed_code = "\n".join(stubbed_lines) + ("\n" if code.endswith("\n") else "")

    return MultiLangExcision(
        path=file_path,
        symbol_name=symbol_name,
        language=lang,
        original=code,
        stubbed=stubbed_code,
        first_line=target_sym.first_body_line,
        last_line=target_sym.last_body_line,
    )
