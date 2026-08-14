> Assumption: All GitHub URL inputs are cloned as full Git repositories, and all local codebase inputs already contain usable `.git` history.

# inverse_alpha

`inverse_alpha` ingests a public GitHub repository or an existing local Git
repository and writes machine-readable history and deterministic Python
knowledge into the repository's own `.inverse_alpha/` directory.

It does not use an LLM for history, parsing, resolution, graph construction, or
validation, and it does not modify tracked source files.

## One-time installation

The project uses Python 3.12 or newer. Runtime dependencies are locked in
`uv.lock`; the knowledge layer uses the direct Tree-sitter Python bindings and
PyYAML, with no Node.js grammar installation or C++ binary.

```bash
uv tool install --editable /Users/adityagk/Desktop/capstone/inverse_alpha
uv tool update-shell
```

Restart the terminal after `uv tool update-shell`. The command can then be run
from any directory without activating a virtual environment.

## Usage

Clone and ingest a public GitHub repository into the current directory:

```bash
cd /Users/adityagk/Desktop/projects
inverse-alpha ingest https://github.com/mahmoud/glom
```

Ingest a local repository:

```bash
inverse-alpha ingest /path/to/repository
```

Or run it from inside that repository:

```bash
inverse-alpha ingest .
```

Build the Python knowledge layer with the same URL or local-path interface:

```bash
inverse-alpha knowledge https://github.com/mahmoud/glom
inverse-alpha knowledge /path/to/repository
inverse-alpha knowledge .
```

`knowledge` first refreshes repository history through `ingest`, then analyzes
the current Git-aware working tree. Tracked and untracked Python files are
included; ignored files, `.inverse_alpha/`, environments, caches, build
outputs, vendored/generated production code, binaries, and notebooks are
excluded. Deterministic generated tests remain eligible for analysis.

For a GitHub URL, the repository is cloned with full history. If a directory
with the same repository name already contains a matching clone, Inverse Alpha
validates it, fetches branches and tags, and reuses it. An unrelated existing
directory is never overwritten.

## GitHub authentication

Commit history always comes from Git and does not depend on GitHub's API.
Public pull-request metadata is optional enrichment.

If `GITHUB_TOKEN` is set, Inverse Alpha uses it for the GitHub REST request:

```bash
export GITHUB_TOKEN=github_pat_...
inverse-alpha ingest https://github.com/mahmoud/glom
```

Without a token, the public unauthenticated API is used. API failures or rate
limits are recorded as partial or unavailable PR metadata and do not invalidate
the Git history extraction. Tokens are never written to metadata or logs.

## Repository-local metadata

Every successful run creates or refreshes:

```text
.inverse_alpha/
├── manifest.json
├── repository.json
├── availability.json
├── history/
│   ├── commits.jsonl
│   ├── pull_requests.jsonl
│   └── commit_pr_links.jsonl
├── logs/
│   ├── ingestion.jsonl
│   └── knowledge.jsonl
├── cache/
│   └── knowledge/
├── knowledge/
│   ├── repo_graph.json
│   ├── diagnostics.jsonl
│   ├── annotations.jsonl
│   ├── validation.json
│   ├── state.json
│   └── .okf/
│       ├── index.md
│       ├── repository.md
│       ├── modules/
│       └── tests/
└── state/
    └── latest.json
```

- `manifest.json` identifies the schema, tool version, and pipeline stages.
- `repository.json` captures the repository identity and resolved Git state.
- `availability.json` explicitly lists which history sources were available,
  partial, unavailable, or intentionally not collected.
- `commits.jsonl` contains commit metadata and per-file change statistics.
- `pull_requests.jsonl` contains public GitHub PR metadata when accessible.
- `commit_pr_links.jsonl` links commits to PRs using merge SHAs and recognized
  merge or squash messages.
- `ingestion.jsonl` is an append-only sanitized execution log.
- `latest.json` identifies the HEAD SHA represented by the current metadata.
- `repo_graph.json` contains repository-relative file, class, function, method,
  and external-module nodes with evidence-backed structural edges.
- `diagnostics.jsonl` records deterministic parser and AST cross-check warnings.
- `annotations.jsonl` is reserved for optional enrichment and is empty when the
  default `NullKnowledgeEnricher` is used.
- `validation.json` records graph and OKF conformance results.
- `knowledge/state.json` keys the generated artifacts by HEAD and aggregate
  source digest. Unchanged runs reuse byte-identical canonical artifacts.
- `.okf/` is an OKF v0.2 bundle containing one concept per Python source or test
  module plus repository and index concepts.

Inverse Alpha adds `.inverse_alpha/` to `.git/info/exclude`. This keeps tool
state out of `git status` without changing the repository's tracked
`.gitignore`.

## Availability semantics

Every history capability has one of four states:

- `available`: collection completed, including a valid zero-item result.
- `partial`: some records were collected before an external interruption.
- `unavailable`: the source cannot be accessed for this repository.
- `not_collected`: the source exists conceptually but is outside this milestone.

This milestone collects commits, diffs, branches, tags, merges, contributors,
and core public PR metadata. PR reviews, PR comments, issues, releases, and CI
run history are reported as `not_collected`.

## Python knowledge contract

The current knowledge implementation intentionally supports Python repositories
only. Tree-sitter is authoritative for syntax and source spans; Python's runtime
AST cross-checks definitions and imports when it can parse the same syntax.
Resolution is conservative: direct internal imports, known aliases, same-module
symbols, imported symbols, `self.method()`, and imported-module calls are
resolved. Dynamic imports, reflection, and ambiguous calls or bases are recorded
in `unresolved_references` rather than guessed.

Every resolved edge contains a repository-relative evidence path, exact byte and
line span, source-text SHA-256, and extractor identity. Canonical artifacts
contain no machine-specific absolute paths; the graph has no run timestamp.
OKF verification timestamps are preserved while the source digest is unchanged.

The `KnowledgeEnricher` protocol is the only LLM boundary in this milestone.
The shipped `NullKnowledgeEnricher` performs no provider calls. Future
annotations remain separate from structural facts and cannot mutate graph edges.

## Development

```bash
uv sync --extra dev
uv run pytest
```
