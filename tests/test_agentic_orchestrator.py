"""Tests for the agentic project-aware multi-language architecture."""

from __future__ import annotations

import tempfile
from pathlib import Path

from stress_stack.ci_parser import parse_ci_facts
from stress_stack.dependency_doctor import lock_dependencies
from stress_stack.excision_multilang import excise_symbol
from stress_stack.hygiene_dispatcher import dispatch_hygiene
from stress_stack.parsers.tree_sitter_core import detect_language, parse_source_code
from stress_stack.project_detector import detect_project_profile
from stress_stack.tracker import TaskTracker


def test_tree_sitter_multilang_parsing() -> None:
    # 1. Python
    py_code = """
import os
from math import sqrt

def calculate(x):
    return sqrt(x)

def test_calc():
    assert calculate(4) == 2
"""
    py_parsed = parse_source_code("test_math.py", py_code)
    assert py_parsed.language == "python"
    assert len(py_parsed.imports) == 2
    assert len(py_parsed.symbols) == 2
    assert len(py_parsed.tests) == 1

    # 2. Rust
    rs_code = """
use std::collections::HashMap;

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[test]
fn test_add() {
    assert_eq!(add(2, 2), 4);
}
"""
    rs_parsed = parse_source_code("src/lib.rs", rs_code)
    assert rs_parsed.language == "rust"
    assert len(rs_parsed.imports) == 1
    assert any(s.name == "add" for s in rs_parsed.symbols)
    assert len(rs_parsed.tests) == 1

    # 3. TypeScript
    ts_code = """
import { sum } from './math';

export function calculateTotal(items: number[]): number {
    return items.reduce((a, b) => a + b, 0);
}

describe('calculateTotal', () => {
    it('sums numbers', () => {
        expect(calculateTotal([1, 2])).toBe(3);
    });
});
"""
    ts_parsed = parse_source_code("src/calc.ts", ts_code)
    assert ts_parsed.language == "typescript"
    assert len(ts_parsed.imports) == 1
    assert any(s.name == "calculateTotal" for s in ts_parsed.symbols)
    assert len(ts_parsed.tests) >= 1

    # 4. Go
    go_code = """
package calc

import "fmt"

func Multiply(a int, b int) int {
    return a * b
}

func TestMultiply(t *testing.T) {
    if Multiply(2, 3) != 6 {
        t.Fail()
    }
}
"""
    go_parsed = parse_source_code("calc_test.go", go_code)
    assert go_parsed.language == "go"
    assert len(go_parsed.imports) == 1
    assert any(s.name == "Multiply" for s in go_parsed.symbols)
    assert any(t.name == "TestMultiply" for t in go_parsed.tests)


def test_multilang_excision() -> None:
    # Python Excision
    py_code = "def foo(x):\n    return x + 1\n"
    py_exc = excise_symbol("foo.py", py_code, "foo")
    assert py_exc is not None
    assert "def foo(x):" in py_exc.stubbed
    assert py_exc.diff() != ""

    # Rust Excision
    rs_code = "pub fn foo(x: i32) -> i32 {\n    x + 1\n}\n"
    rs_exc = excise_symbol("src/foo.rs", rs_code, "foo")
    assert rs_exc is not None
    assert "todo!()" in rs_exc.stubbed
    assert "x + 1" in rs_exc.diff()

    # TypeScript Excision
    ts_code = "export function foo(x: number): number {\n    return x + 1;\n}\n"
    ts_exc = excise_symbol("src/foo.ts", ts_code, "foo")
    assert ts_exc is not None
    assert "throw new Error" in ts_exc.stubbed


def test_project_detector_and_ci_parser() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create GitHub workflow
        wf_dir = root / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: sudo apt-get install -y libssl-dev
      - run: cargo test --workspace
""",
            encoding="utf-8",
        )
        (root / "Cargo.toml").write_text(
            """
[workspace]
members = ["crates/*"]
""",
            encoding="utf-8",
        )

        profile = detect_project_profile(root)
        assert profile.primary_language == "rust"
        assert profile.toolchain == "cargo"
        assert profile.is_monorepo is True
        assert "libssl-dev" in profile.ci_facts.system_packages
        assert "cargo test --workspace" in profile.ci_facts.test_commands


def test_task_tracker_synchronization() -> None:
    tracker = TaskTracker()
    assert not tracker.is_done("task_1")

    tracker.mark_done("task_1", {"status": "ok"})
    assert tracker.is_done("task_1")
    assert tracker.get_result("task_1") == {"status": "ok"}
    assert tracker.wait_for("task_1", timeout=0.1) == "done"
