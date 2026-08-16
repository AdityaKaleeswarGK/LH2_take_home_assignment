"""Remove one function's body and leave its contract standing.

An excision task hands a solver a signature, a docstring and a suite that
already knows what the function must do. Everything else about the file has to
survive untouched, because the golden answer for this source is "the body that
was there", and any incidental reformatting would show up in that diff as noise
a solver is asked to reproduce.

Two stub strategies, chosen by measurement rather than by preference:

* ``neutral`` replaces the body with ``return None``. When the covering tests
  then fail on an ``assert``, that is the strongest evidence a task can have:
  the tests pin observable behaviour rather than the shape of an exception. It
  also detects the opposite — tests that still pass against a hollow function
  are under-constrained, and the brief's "a different but correct
  implementation must pass your verifier" cuts both ways.
* ``explicit`` replaces it with ``raise NotImplementedError``. Used where a
  neutral body is not meaningful — a generator stops being a generator the
  moment its ``yield`` is removed, and the resulting ``TypeError`` in the
  caller says nothing about the function's contract.

Layouts this module refuses rather than mangles are listed in
:func:`plan_excision`; refusing is a rejected candidate, and corrupting a file
is a task that fails for the wrong reason.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.errors import StressStackError

NEUTRAL = "neutral"
EXPLICIT = "explicit"


class ExcisionError(StressStackError):
    """This symbol cannot be excised without rewriting more than its body."""


@dataclass(frozen=True, slots=True)
class ExcisionPlan:
    """Where the body lives, and what may replace it."""

    path: str
    qualified_name: str
    name: str
    first_body_line: int
    last_body_line: int
    indent: str
    is_generator: bool
    is_async: bool
    has_docstring: bool
    decorators: list[str]
    signature_first_line: int

    @property
    def body_line_count(self) -> int:
        return self.last_body_line - self.first_body_line + 1

    def strategies(self) -> list[str]:
        """Preferred first. A generator has no meaningful neutral body."""
        return [EXPLICIT] if self.is_generator else [NEUTRAL, EXPLICIT]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "first_body_line": self.first_body_line,
            "last_body_line": self.last_body_line,
            "body_line_count": self.body_line_count,
            "is_generator": self.is_generator,
            "is_async": self.is_async,
            "has_docstring": self.has_docstring,
            "decorators": self.decorators,
        }


@dataclass(frozen=True, slots=True)
class Excision:
    plan: ExcisionPlan
    strategy: str
    original: str
    stubbed: str

    def diff(self, *, label: str | None = None) -> str:
        """The golden answer: what has to be written back to restore the body."""
        name = label or self.plan.path
        lines = difflib.unified_diff(
            self.stubbed.splitlines(keepends=True),
            self.original.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=3,
        )
        return "".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy, **self.plan.to_dict()}


def find_function(
    tree: ast.Module, name: str, line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Locate the definition the graph anchored, by name and ``def`` line.

    Matching on both is what keeps overloads and same-named methods on different
    classes apart; ``lineno`` on a decorated definition is the ``def`` itself,
    not the first decorator, so the anchor lines up.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name and node.lineno == line:
                return node
    raise ExcisionError(f"No definition named {name!r} at line {line}")


def definitions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every definition in the module, paired with its dotted path.

    Built the way :mod:`stress_stack.symbols` builds a qualified name, so a
    symbol id from the graph resolves here without a line number.
    """
    found: list[tuple[str, ast.AST]] = []

    def descend(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            dotted = f"{prefix}.{node.name}" if prefix else node.name
            found.append((dotted, node))
            descend(node.body, dotted)

    descend(tree.body, "")
    return found


def find_by_dotted_path(
    tree: ast.Module, dotted: str, *, line: int | None = None
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Locate a definition by its path within the module, not by line number.

    Line numbers cannot be trusted across the boundary this runs on: the graph
    is parsed from the working tree, while the tree a task is built from comes
    out of ``git archive``. Once hygiene has reformatted a file the two disagree
    by however many lines ruff moved, and a line-addressed excision would then
    cut the wrong function.
    """
    matches = [
        node
        for path, node in definitions(tree)
        if path == dotted and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not matches:
        raise ExcisionError(f"No definition at {dotted!r}")
    if len(matches) > 1:
        if line is not None:
            exact = [node for node in matches if node.lineno == line]
            if len(exact) == 1:
                return exact[0]
        raise ExcisionError(
            f"{dotted!r} is defined {len(matches)} times; an overloaded or "
            "conditionally redefined function has no single body to remove"
        )
    return matches[0]


def plan_excision(
    source: str,
    name: str,
    line: int | None = None,
    *,
    path: str = "",
    dotted: str | None = None,
) -> ExcisionPlan:
    """Work out which lines are body, or refuse to touch the file.

    ``dotted`` addresses the definition by its path inside the module and is the
    form to use whenever the source may have moved since the graph was built.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ExcisionError(f"{path or 'source'} does not parse: {exc}") from exc

    if dotted:
        node = find_by_dotted_path(tree, dotted, line=line)
    elif line is not None:
        node = find_function(tree, name, line)
    else:
        node = find_by_dotted_path(tree, name)
    lines = source.splitlines()
    if not node.body:
        raise ExcisionError(f"{name} has no body to remove")

    docstring = _docstring_node(node)
    first_statement = node.body[1] if docstring is not None and len(node.body) > 1 else node.body[0]
    if docstring is not None and len(node.body) == 1:
        raise ExcisionError(f"{name} is a docstring with no implementation")

    first_body_line = first_statement.lineno
    # Only whitespace may precede the first statement being removed. This is the
    # one check that rules out every layout where the signature and the body
    # share a line: `def f(): return 1`, and the multi-line-signature form
    # `def f(\n    a,\n): return a`. Both would otherwise lose their `def`.
    prefix = lines[first_body_line - 1][: first_statement.col_offset]
    if prefix.strip():
        raise ExcisionError(f"{name} has its body on the signature line; refusing to rewrite it")
    if docstring is not None:
        doc_prefix = lines[docstring.lineno - 1][: docstring.col_offset]
        if doc_prefix.strip():
            raise ExcisionError(f"{name} has its docstring on the signature line")

    return ExcisionPlan(
        path=path,
        qualified_name=name,
        name=node.name,
        first_body_line=first_body_line,
        last_body_line=_extend_over_trailing_comments(
            lines, node.end_lineno or first_body_line, len(prefix)
        ),
        indent=prefix,
        is_generator=_is_generator(node),
        is_async=isinstance(node, ast.AsyncFunctionDef),
        has_docstring=docstring is not None,
        decorators=[ast.unparse(d) for d in node.decorator_list],
        signature_first_line=node.lineno,
    )


def stub_body(plan: ExcisionPlan, strategy: str) -> list[str]:
    if strategy == NEUTRAL:
        return [f"{plan.indent}return None"]
    if strategy == EXPLICIT:
        message = f"{plan.qualified_name} is not implemented"
        return [f"{plan.indent}raise NotImplementedError({message!r})"]
    raise ExcisionError(f"Unknown excision strategy {strategy!r}")


def excise(
    source: str,
    name: str,
    line: int | None = None,
    *,
    strategy: str,
    path: str = "",
    dotted: str | None = None,
) -> Excision:
    """Return the file with this one function's body replaced."""
    plan = plan_excision(source, name, line, path=path, dotted=dotted)
    lines = source.splitlines(keepends=True)
    ending = _line_ending(lines, plan.first_body_line - 1)

    replacement = [text + ending for text in stub_body(plan, strategy)]
    stubbed_lines = (
        lines[: plan.first_body_line - 1] + replacement + lines[plan.last_body_line :]
    )
    stubbed = "".join(stubbed_lines)

    # A stub that does not parse is a task that fails on syntax, which the brief
    # excludes by name. Cheap to check here, impossible to recover from later.
    try:
        ast.parse(stubbed)
    except SyntaxError as exc:
        raise ExcisionError(f"Stubbing {name} produced unparsable source: {exc}") from exc
    if stubbed == source:
        raise ExcisionError(f"Stubbing {name} changed nothing")

    return Excision(plan=plan, strategy=strategy, original=source, stubbed=stubbed)


def apply_to_tree(root: Path, excision: Excision) -> Path:
    """Write the stubbed file into a materialised tree."""
    target = root / excision.plan.path
    if not target.is_file():
        raise ExcisionError(f"{excision.plan.path} is missing from {root}")
    target.write_text(excision.stubbed, encoding="utf-8")
    return target


def _docstring_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Expr | None:
    first = node.body[0] if node.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first
    return None


def _is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether removing the body would also remove what makes this a generator.

    Nested definitions are not descended into: a ``yield`` inside an inner
    function belongs to that function, and stubbing the outer one leaves the
    outer one an ordinary callable either way.
    """
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(current))
    return False


def _extend_over_trailing_comments(lines: list[str], last_line: int, body_indent: int) -> int:
    """Swallow comments that trail the last statement but still sit in the body.

    ``end_lineno`` stops at the final statement, so a comment after it survives
    the stub — and a comment inside a removed body routinely explains the
    algorithm that was just removed. Only comments indented at least as far as
    the body are taken; one at the definition's own indentation introduces
    whatever comes next and is left alone.
    """
    index = last_line
    while index < len(lines):
        text = lines[index]
        stripped = text.strip()
        if not stripped:
            # A blank line counts only if a swallowed comment follows it.
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            following = lines[lookahead] if lookahead < len(lines) else ""
            if not (
                following.strip().startswith("#")
                and len(following) - len(following.lstrip()) >= body_indent
            ):
                break
            index = lookahead
            continue
        if not stripped.startswith("#"):
            break
        if len(text) - len(text.lstrip()) < body_indent:
            break
        index += 1
    return index


def _line_ending(lines: list[str], index: int) -> str:
    """Match the file's own line ending so a stub does not introduce a CRLF diff."""
    if 0 <= index < len(lines):
        line = lines[index]
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"
