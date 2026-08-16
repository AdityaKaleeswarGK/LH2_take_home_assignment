"""The queryable tier of the knowledge layer.

``repo_graph.json`` is the record; this is the thing that answers questions
about it. Selection, difficulty and blast radius each hit the same facts
thousands of times, and re-parsing a 2.9 MB document per question is the wrong
shape at glom's size and the wrong shape by two orders of magnitude on a
mid-size repository.

Three properties:

* **Derived and disposable.** Everything here is rebuilt deterministically from
  the JSON artifacts. Deleting the database loses nothing; hand-editing it is
  never correct. That is what keeps the JSON authoritative and lets a grader
  re-derive every claim.
* **``sqlite3`` is standard library**, so the package keeps zero runtime
  dependencies.
* **Generated text is quarantined.** Model output lands in ``labels`` alongside
  the model id, the prompt hash and a verification state, and no query that
  decides which task ships ever joins against it.

The relation this exists to make cheap is ``test → module``. It is implied by
two measured facts — coverage attributes a test to symbols, and the parse
attributes a symbol to a module — but it is never written down anywhere else,
and the module-diversity requirement is a join away from being unanswerable
without it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.coverage_map import CoverageMap
from stress_stack.graph import RepositoryGraph

SCHEMA_VERSION = "0.1.0"

_SCHEMA = """
CREATE TABLE files (
    path    TEXT PRIMARY KEY,
    module  TEXT,
    is_test INTEGER NOT NULL,
    sha256  TEXT
);

CREATE TABLE symbols (
    id             TEXT PRIMARY KEY,
    path           TEXT NOT NULL,
    module         TEXT,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    line           INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    signature      TEXT,
    docstring      TEXT,
    is_public      INTEGER NOT NULL,
    is_test        INTEGER NOT NULL
);

CREATE TABLE edges (
    kind   TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    path   TEXT NOT NULL,
    line   INTEGER NOT NULL
);

CREATE TABLE tests (
    test_id TEXT PRIMARY KEY,
    path    TEXT,
    name    TEXT
);

CREATE TABLE coverage (
    test_id   TEXT NOT NULL,
    symbol_id TEXT NOT NULL,
    PRIMARY KEY (test_id, symbol_id)
);

CREATE TABLE pull_requests (
    number           INTEGER PRIMARY KEY,
    title            TEXT,
    body             TEXT,
    merged_at        TEXT,
    merge_commit_sha TEXT,
    html_url         TEXT
);

CREATE TABLE commit_pr_links (
    commit_sha TEXT NOT NULL,
    pr_number  INTEGER NOT NULL,
    method     TEXT NOT NULL
);

-- Generated, never authoritative. Carries the provenance that makes it
-- auditable, and is deliberately not reachable from any gating query.
CREATE TABLE labels (
    subject_id         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    text               TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,
    verification_state TEXT NOT NULL
);

CREATE INDEX idx_symbols_path   ON symbols (path);
CREATE INDEX idx_symbols_module ON symbols (module);
CREATE INDEX idx_edges_source   ON edges (source);
CREATE INDEX idx_edges_target   ON edges (target);
CREATE INDEX idx_coverage_sym   ON coverage (symbol_id);
CREATE INDEX idx_labels_subject ON labels (subject_id, kind);

-- What a test exercises, expressed in modules rather than symbols. The
-- four-module diversity floor is counted against this.
CREATE VIEW test_modules AS
SELECT DISTINCT c.test_id AS test_id, s.module AS module
FROM coverage c
JOIN symbols s ON s.id = c.symbol_id
WHERE s.module IS NOT NULL;

CREATE VIEW module_symbols AS
SELECT module, id AS symbol_id, path, kind, qualified_name
FROM symbols
WHERE module IS NOT NULL;
"""


@dataclass(frozen=True, slots=True)
class IndexStats:
    files: int
    symbols: int
    edges: int
    tests: int
    coverage_rows: int
    modules: int
    pull_requests: int
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "files": self.files,
            "symbols": self.symbols,
            "edges": self.edges,
            "tests": self.tests,
            "coverage_rows": self.coverage_rows,
            "modules": self.modules,
            "pull_requests": self.pull_requests,
            "path": str(self.path),
        }


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def build_index(
    path: Path,
    graph: RepositoryGraph,
    coverage: CoverageMap | None,
    history_root: Path | None = None,
) -> IndexStats:
    """Rebuild the index from scratch. Never incremental, never hand-edited."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    with closing(connect(path)) as connection, connection:
        connection.executescript(_SCHEMA)
        _load_graph(connection, graph)
        if coverage is not None:
            _load_coverage(connection, coverage)
        if history_root is not None:
            _load_history(connection, history_root)
        return _stats(connection, path)


def _load_graph(connection: sqlite3.Connection, graph: RepositoryGraph) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO files (path, module, is_test, sha256) VALUES (?, ?, ?, ?)",
        [
            (parsed.path, parsed.module or None, int(parsed.is_test), parsed.source_sha256)
            for parsed in graph.files
        ],
    )
    connection.executemany(
        "INSERT OR REPLACE INTO symbols (id, path, module, kind, name, qualified_name, "
        "line, end_line, signature, docstring, is_public, is_test) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                symbol.id,
                parsed.path,
                parsed.module or None,
                symbol.kind,
                symbol.name,
                symbol.qualified_name,
                symbol.anchor.line,
                symbol.anchor.end_line,
                symbol.signature,
                symbol.docstring,
                int(symbol.is_public),
                int(symbol.is_test),
            )
            for parsed in graph.files
            for symbol in parsed.symbols
        ],
    )
    connection.executemany(
        "INSERT INTO edges (kind, source, target, path, line) VALUES (?, ?, ?, ?, ?)",
        [
            (edge.kind, edge.source, edge.target, edge.anchor.path, edge.anchor.line)
            for edge in graph.edges
        ],
    )


def _load_coverage(connection: sqlite3.Connection, coverage: CoverageMap) -> None:
    known = {row["id"] for row in connection.execute("SELECT id FROM symbols")}
    tests: dict[str, tuple[str | None, str]] = {}
    rows: list[tuple[str, str]] = []
    for symbol in coverage.symbols.values():
        for test_id in symbol.covering_tests:
            if symbol.symbol_id in known:
                rows.append((test_id, symbol.symbol_id))
            if test_id not in tests:
                tests[test_id] = _split_test_id(test_id)

    connection.executemany(
        "INSERT OR REPLACE INTO tests (test_id, path, name) VALUES (?, ?, ?)",
        [(test_id, path, name) for test_id, (path, name) in sorted(tests.items())],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO coverage (test_id, symbol_id) VALUES (?, ?)", rows
    )


def _load_history(connection: sqlite3.Connection, history_root: Path) -> None:
    pull_requests = _read_jsonl(history_root / "pull_requests.jsonl")
    connection.executemany(
        "INSERT OR REPLACE INTO pull_requests (number, title, body, merged_at, "
        "merge_commit_sha, html_url) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                record.get("number"),
                record.get("title"),
                record.get("body"),
                record.get("merged_at"),
                record.get("merge_commit_sha"),
                record.get("html_url"),
            )
            for record in pull_requests
            if isinstance(record.get("number"), int)
        ],
    )
    connection.executemany(
        "INSERT INTO commit_pr_links (commit_sha, pr_number, method) VALUES (?, ?, ?)",
        [
            (record.get("commit_sha"), record.get("pr_number"), record.get("method") or "")
            for record in _read_jsonl(history_root / "commit_pr_links.jsonl")
            if isinstance(record.get("pr_number"), int)
        ],
    )


def record_label(
    connection: sqlite3.Connection,
    *,
    subject_id: str,
    kind: str,
    text: str,
    model: str,
    prompt_hash: str,
    verification_state: str = "unverified",
) -> None:
    """Store generated text with the provenance that keeps it non-authoritative."""
    connection.execute(
        "INSERT INTO labels (subject_id, kind, text, model, prompt_hash, verification_state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subject_id, kind, text, model, prompt_hash, verification_state),
    )


# --------------------------------------------------------------------------
# Queries the later stages actually make
# --------------------------------------------------------------------------


def modules_for_test(connection: sqlite3.Connection, test_id: str) -> list[str]:
    return [
        row["module"]
        for row in connection.execute(
            "SELECT module FROM test_modules WHERE test_id = ? ORDER BY module", (test_id,)
        )
    ]


def tests_for_module(connection: sqlite3.Connection, module: str) -> list[str]:
    return [
        row["test_id"]
        for row in connection.execute(
            "SELECT test_id FROM test_modules WHERE module = ? ORDER BY test_id", (module,)
        )
    ]


def module_of_symbol(connection: sqlite3.Connection, symbol_id: str) -> str | None:
    row = connection.execute(
        "SELECT module FROM symbols WHERE id = ?", (symbol_id,)
    ).fetchone()
    return row["module"] if row else None


def module_test_matrix(connection: sqlite3.Connection) -> dict[str, int]:
    """How many distinct tests exercise each module — the diversity denominator."""
    return {
        row["module"]: row["tests"]
        for row in connection.execute(
            "SELECT module, COUNT(DISTINCT test_id) AS tests FROM test_modules "
            "GROUP BY module ORDER BY tests DESC, module"
        )
    }


def shared_tests(connection: sqlite3.Connection, left: str, right: str) -> list[str]:
    """Tests exercising both modules.

    Two tasks whose verifiers overlap are less independent than their module
    labels suggest, which is worth knowing before calling them diverse.
    """
    return [
        row["test_id"]
        for row in connection.execute(
            "SELECT a.test_id FROM test_modules a JOIN test_modules b "
            "ON a.test_id = b.test_id WHERE a.module = ? AND b.module = ? "
            "ORDER BY a.test_id",
            (left, right),
        )
    ]


def callers_of(connection: sqlite3.Connection, symbol_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT kind, source, path, line FROM edges "
            "WHERE target = ? AND kind != 'contains' ORDER BY path, line",
            (symbol_id,),
        )
    )


def _split_test_id(test_id: str) -> tuple[str | None, str]:
    head, separator, name = test_id.rpartition("::")
    if not separator:
        return None, test_id
    path = head.split("::", 1)[0]
    return (path if path.endswith(".py") else None), name


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _stats(connection: sqlite3.Connection, path: Path) -> IndexStats:
    def count(statement: str) -> int:
        row = connection.execute(statement).fetchone()
        return int(row[0]) if row else 0

    return IndexStats(
        files=count("SELECT COUNT(*) FROM files"),
        symbols=count("SELECT COUNT(*) FROM symbols"),
        edges=count("SELECT COUNT(*) FROM edges"),
        tests=count("SELECT COUNT(*) FROM tests"),
        coverage_rows=count("SELECT COUNT(*) FROM coverage"),
        modules=count("SELECT COUNT(DISTINCT module) FROM symbols WHERE module IS NOT NULL"),
        pull_requests=count("SELECT COUNT(*) FROM pull_requests"),
        path=path,
    )
