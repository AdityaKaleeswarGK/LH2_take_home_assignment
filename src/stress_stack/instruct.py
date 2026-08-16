"""Write the task statement, and prove it does not contain the answer.

The information diet is the design. The golden diff never enters a prompt and
never enters an instruction, because it is the single richest source of leakage
and the brief forbids an instruction that reduces the task to transcription.
What the writer gets instead is evidence that provably excludes the solution:
signatures, docstrings, covering-test names, caller lists, and — for a
history-derived task — the pull request's own prose with any pasted code
stripped out of it.

The mechanical path is the default, not the fallback. A template assembled from
measured fields is dry, but it is self-contained, implementation-neutral and
leak-free by construction, and it ships whether or not a model is reachable.
Generated prose replaces it only after passing the same checks. That inversion
is what keeps the deliverable independent of an API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Five tokens is long enough that a shared run is copied rather than coincidental
# — "if the value is None" is four and appears everywhere, while any five
# consecutive tokens of real code are effectively unique to it.
_LEAK_NGRAM = 5

_FENCE = re.compile(r"```.*?```", re.S)
_INDENTED_BLOCK = re.compile(r"(?m)^(?: {4}|\t).*$")
_DIFF_LINE = re.compile(r"(?m)^[+-][^+-].*$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[^\sA-Za-z0-9_]")
_GENERIC_TITLE = re.compile(
    r"^(?:stable|main|master|merge\b|sync\b|release\b|move to src\b)", re.I
)


@dataclass
class LeakReport:
    """Whether an instruction gives the answer away, and where."""

    copied_spans: list[str] = field(default_factory=list)
    private_names: list[str] = field(default_factory=list)
    public_names: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Public names introduced by the change are permitted and recorded.

        The brief draws the line at implementation, not at vocabulary: "naming a
        new public API is fine; showing its implementation is not." An agent
        told to implement ``Iter`` still has to work out what ``Iter`` does. A
        leading-underscore name is different — it is internal structure, and
        naming it hands over a design decision the task is supposed to require.
        """
        return not self.copied_spans and not self.private_names

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "copied_spans": self.copied_spans[:5],
            "private_names_leaked": self.private_names,
            "public_names_mentioned": self.public_names,
        }


def added_lines(diff: str) -> list[str]:
    """Lines the change introduces, without the diff markers."""
    return [
        line[1:].strip()
        for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]


def solution_only_names(diff: str, base_names: set[str]) -> set[str]:
    """Identifiers the change defines that did not exist beforehand.

    Computed from the diff against the graph rather than by string search, so a
    name that merely appears in an added line — a call to something that already
    existed — is not mistaken for something the change invented.
    """
    defined: set[str] = set()
    for line in added_lines(diff):
        match = re.match(r"(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if match:
            defined.add(match.group(1))
            continue
        assignment = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=[^=]", line)
        if assignment:
            defined.add(assignment.group(1))
    return {name for name in defined if name not in base_names}


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text or "")


def leak_check(
    instruction: str, diff: str, base_names: set[str], base_text: str = ""
) -> LeakReport:
    """Two mechanical checks, both derived from artifacts that already exist.

    ``base_text`` is the pre-change tree. A span that already appears there is
    not something the change introduced, and repeating it cannot give anything
    away — the solver is looking at it. Without this the check flagged its own
    contract: an excision instruction names ``GROUP(target, spec, scope)``,
    which it must, and glom's removed body calls something with the same
    argument names, so the signature was reported as copied code.
    """
    report = LeakReport()
    window = " ".join(tokens(instruction)).lower()
    baseline = " ".join(tokens(base_text)).lower()

    for line in added_lines(diff):
        line_tokens = tokens(line)
        for start in range(0, max(0, len(line_tokens) - _LEAK_NGRAM + 1)):
            span = " ".join(line_tokens[start : start + _LEAK_NGRAM]).lower()
            if not span or span not in window:
                continue
            if baseline and span in baseline:
                continue
            report.copied_spans.append(" ".join(line_tokens[start : start + _LEAK_NGRAM]))
            break

    mentioned = set(_IDENTIFIER.findall(instruction or ""))
    for name in sorted(solution_only_names(diff, base_names) & mentioned):
        (report.private_names if name.startswith("_") else report.public_names).append(name)
    return report


def sanitize(body: str, *, limit: int = 1200) -> str:
    """Strip pasted code out of a pull request body.

    Authors routinely paste the patch into the description. Handing that to a
    writer defeats the entire point of keeping the diff out of the prompt, so
    fenced blocks, indented blocks and diff lines are removed before the prose
    is used for anything.
    """
    text = _FENCE.sub(" ", body or "")
    text = _DIFF_LINE.sub(" ", text)
    text = _INDENTED_BLOCK.sub(" ", text)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


_DIRECTIVE = re.compile(r"^\s*(\.\.\s|:\w+:|=+$|-+$|~+$)")


def first_sentence(text: str, *, limit: int = 200) -> str:
    """The first prose sentence of a docstring, skipping markup and directives.

    Taking ``splitlines()[0]`` produced ".. versionadded:: 20.7.0" for glom's
    matching module and "*glom gets results.*" for its core — reST directives
    and emphasis markers, not descriptions. Docstrings also wrap, so a first
    *line* is routinely half a sentence: "in a dict spec, target-keys may match
    many" was a real output. Paragraphs are joined and the first sentence taken.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if _DIRECTIVE.match(line):
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))

    for paragraph in paragraphs:
        cleaned = re.sub(r":\w+:`~?([^`]*)`", r"\1", paragraph)
        cleaned = re.sub(r"[*`]+", "", cleaned).strip()
        if len(cleaned) < 12:
            continue
        # The whole opening paragraph, cut on a word boundary. Splitting on the
        # first period truncated glom's matching docstring at "(e.g." — sentence
        # detection needs abbreviation handling that a docstring does not repay,
        # and a couple of sentences of context is better than half of one.
        if len(cleaned) > limit:
            cleaned = cleaned[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        return cleaned.rstrip()
    return ""


def humanise(test_id: str) -> str:
    """A test node id as the behaviour it names."""
    name = test_id.rpartition("::")[2]
    name = re.sub(r"^test_", "", name)
    return name.replace("_", " ").strip()


def behaviour_summary(test_ids: list[str], *, limit: int = 8) -> str:
    phrases = [humanise(test_id) for test_id in test_ids[:limit]]
    phrases = [phrase for phrase in phrases if phrase]
    if not phrases:
        return "the behaviour the repository's own tests describe"
    if len(phrases) == 1:
        return phrases[0]
    return "; ".join(phrases[:-1]) + "; and " + phrases[-1]


def instruction_quality(title: str, instruction: str) -> tuple[bool, list[str]]:
    """Cheap structural checks for prose that is safe to ship without a model."""
    problems: list[str] = []
    if len(instruction.strip()) < 120:
        problems.append("too_short")
    if len(instruction) > 5_000:
        problems.append("too_long")
    if _GENERIC_TITLE.search(title.strip()):
        problems.append("generic_title")
    if "Implement the behaviour so that" in instruction and " hold." in instruction:
        problems.append("placeholder_grammar")
    if instruction.count("[") != instruction.count("]"):
        problems.append("broken_markdown")
    if re.search(r"\b(?:TODO|TBD|describe-only constraint)\b", instruction, re.I):
        problems.append("template_placeholder")
    return not problems, problems


def _scenario_lines(test_ids: list[str], *, limit: int = 10) -> list[str]:
    return [f"- {humanise(test_id)}" for test_id in test_ids[:limit] if humanise(test_id)]


def _history_title(task: dict[str, Any]) -> str:
    original = str(task.get("title") or "").strip()
    if original and not _GENERIC_TITLE.search(original):
        return original
    scenario = humanise((task.get("targets") or [""])[0]).strip()
    if scenario:
        return f"{str(task.get('primary_module') or 'Repository')}: {scenario}"
    return f"Update {task.get('primary_module') or 'repository behavior'}"


def _usable_summary(value: str) -> str:
    text = str(value or "").strip()
    if (
        len(text) < 12
        or "#" in text
        or _GENERIC_TITLE.search(text)
        or text.count("[") != text.count("]")
    ):
        return ""
    return text


def mechanical_instruction(task: dict[str, Any], evidence: dict[str, Any]) -> dict[str, str]:
    """Assemble an instruction from measured fields alone.

    Never sees the diff, so it cannot leak it. Names the behaviour the verifier
    checks and the contract the answer must satisfy, and says how success is
    measured, which is what the brief asks a self-contained instruction to do.
    """
    module = task.get("primary_module") or "the repository"
    purpose = first_sentence(evidence.get("module_purpose") or "")
    context = f"{module} — {purpose.rstrip(chr(46))}" if purpose else (
        f"{module} is part of this repository"
    )
    behaviour = behaviour_summary(task.get("targets") or [])
    scope = ", ".join((task.get("files_in_scope") or [])[:6]) or module

    if task["source"] == "excision":
        # The graph stores only the parameter list, so the callable has to be
        # named or the instruction reads "the body of `(match)` was removed".
        qualified = evidence.get("qualified_name") or ""
        bare = qualified.rpartition(".")[2] or task.get("subject", "")
        params = evidence.get("signature") or "(...)"
        signature = f"{bare}{params}" if bare else params
        docstring = first_sentence(evidence.get("docstring") or "")
        contract = f"`{signature}`" if signature else "the function named below"
        title = f"Implement {evidence.get('qualified_name') or task.get('subject')}"
        body = (
            f"{context}. The body of {contract} has been removed; its signature and "
            f"documentation remain.\n\n"
            + (f"Its documented contract is: {docstring.rstrip(chr(46))}.\n\n" if docstring else "")
            + f"Restore the behaviour so that it satisfies {behaviour}.\n\n"
            f"Any implementation satisfying that contract is acceptable — the "
            f"verifier checks observable behaviour, not how you achieve it. "
            f"Success is measured by the repository's existing test suite, "
            f"including the tests covering this function, passing unchanged.\n\n"
            f"Relevant files: {scope}."
        )
    else:
        summary = _usable_summary(evidence.get("change_summary") or "")
        title = _history_title(task)
        scenarios = _scenario_lines(task.get("targets") or [])
        scenario_block = "\n".join(scenarios) or "- the designated verifier behavior"
        body = (
            f"{context}.\n\n"
            + (f"Required behavior: {summary}\n\n" if summary else "")
            + "Implement the behavior exercised by these repository scenarios:\n\n"
            f"{scenario_block}\n\n"
            "Preserve existing behavior outside these scenarios. A different "
            "implementation is acceptable when it produces the same observable "
            "results and the designated tests plus the broader suite pass.\n\n"
            f"Relevant files: {scope}."
        )
    return {"title": title, "instruction": body}


def build_evidence(
    task: dict[str, Any],
    *,
    module_purpose: str = "",
    signature: str = "",
    docstring: str = "",
    qualified_name: str = "",
    pr_body: str = "",
) -> dict[str, Any]:
    """The bounded evidence an instruction may be written from. No diff, ever."""
    return {
        "module_purpose": module_purpose,
        "signature": signature,
        "docstring": docstring,
        "qualified_name": qualified_name,
        "change_summary": sanitize(pr_body),
        "targets": task.get("targets") or [],
        "files_in_scope": task.get("files_in_scope") or [],
    }


def _behaviour_brief(task: dict[str, Any], evidence: dict[str, Any]) -> str:
    """What must exist when the work is done — and, for an excision, how much.

    Covering tests exercise a symbol without being about it. `Path.from_t` is
    covered by a deletion test and an assignment test, so an instruction written
    from those names described deletion and assignment as work to do, when the
    solver needs only `from_t` and the other two pass because it does. The scope
    is therefore stated separately from the checks: one names the single
    function to implement, the other names what will be run against it.
    """
    module = evidence.get("module_purpose", "")
    checks = behaviour_summary(task.get("targets") or [])
    if task.get("source") != "excision":
        return f"{module}\n{evidence.get('change_summary', '')}\nThe verifier checks: {checks}.".strip()

    qualified = evidence.get("qualified_name") or task.get("subject", "")
    return (
        f"{module}\n"
        f"SCOPE: exactly one function is missing its body — {qualified}. Its "
        "signature and documentation remain in place. Implement that function "
        "and nothing else; the rest of the repository is already complete and "
        "correct.\n"
        f"CHECKS: the verifier runs these existing tests, which exercise the "
        f"function directly or indirectly — {checks}. They describe what is "
        "checked, not additional work: a test named for deletion or assignment "
        "passes once the missing function behaves correctly. Do not describe "
        "those neighbouring features as things the solver must build."
    ).strip()


def generated_instruction(
    client: Any,
    task: dict[str, Any],
    evidence: dict[str, Any],
    *,
    diff: str,
    base_names: set[str],
    base_text: str,
    written_so_far: str = "",
    role: str = "worker",
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Ask a model to phrase the instruction, and keep it only if it is clean.

    The model sees the same evidence the mechanical writer does and nothing
    more — no diff, no solution tree, no source of the changed functions. Its
    output then goes through the identical leak check, so generated prose is
    held to the standard the template meets by construction rather than to a
    weaker one. A failure returns ``None`` and the template stands, which is why
    the deliverable never depends on a model being reachable.
    """
    from stress_stack.openrouter import ModelError
    from stress_stack.prompts import INSTRUCTION_SCHEMA, instruction_messages

    contract = [line for line in [evidence.get("signature"), evidence.get("docstring")] if line]
    messages = instruction_messages(
        behaviour=_behaviour_brief(task, evidence),
        feature=task.get("primary_module") or "",
        contract_lines=contract,
        verifier_tests=[humanise(test_id) for test_id in (task.get("targets") or [])],
    )
    if written_so_far:
        messages.append(
            {
                "role": "user",
                "content": (
                    "# Task statements already written for this set\n"
                    f"{written_so_far}\n\n"
                    "Avoid reusing their phrasing. This is context about the set, "
                    "not a requirement to match or differ from any of them."
                ),
            }
        )

    try:
        # Budgeted for a reasoning model, which spends tokens thinking before
        # emitting a character. At 2000 two of glom's ten tasks came back having
        # consumed the entire budget on reasoning and produced no content at
        # all — a truncation that arrives as a successful 200.
        payload, completion = client.complete_json(
            messages, schema=INSTRUCTION_SCHEMA, role=role, max_tokens=8000
        )
    except ModelError as exc:
        # Recorded rather than swallowed: a model failure and a model that
        # returned unusable prose both fall back to the template, and without
        # the reason the artifact cannot tell you which happened.
        return None, {"fell_back": "model_error", "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — never let the deliverable depend on this
        return None, {"fell_back": type(exc).__name__, "detail": str(exc)[:200]}

    text = str(payload.get("instruction") or "").strip()
    title = str(payload.get("title") or "").strip()
    if len(text) < 80:
        return None, {"fell_back": "too_short", "detail": f"{len(text)} characters"}
    report = leak_check(text, diff, base_names, base_text)
    if not report.clean:
        return None, {"fell_back": "leaked", "detail": report.to_dict()}
    return (
        {"title": title or task.get("title") or task["task_id"], "instruction": text},
        {
            "fell_back": None,
            "leak_check": report.to_dict(),
            "model": completion.model,
            "cached": completion.cached,
            "cache_key": completion.cache_key,
        },
    )
