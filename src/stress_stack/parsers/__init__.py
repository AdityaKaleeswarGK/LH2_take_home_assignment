"""Parsers package."""

from stress_stack.parsers.tree_sitter_core import (
    ParsedSourceFile,
    detect_language,
    parse_source_code,
)

__all__ = ["ParsedSourceFile", "detect_language", "parse_source_code"]
