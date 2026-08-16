# stress_stack

`stress_stack` ingests a public GitHub repository or an existing local Git
repository and writes machine-readable history — commits, diffs, branches, tags,
public pull requests, and commit↔PR links — into the repository's own
`.stress_stack/` directory.

It then applies **repo hygiene**: ruff lint and format, verified by a test run
before and after so any regression the fixes introduce is caught and reported.
Nothing is inferred, and no LLM is involved at any point.

## Scope

| In scope | Not yet |
| --- | --- |
| Clone a GitHub URL (full history) or analyze a local repo | Dependency pinning |
| Commit records with per-file change statistics | Containerization |
| Branches, tags, merges, contributors | Test generation |
| Public pull-request metadata | Knowledge layer / repository graph |
| Commit↔PR links by merge SHA and merge/squash messages | Task generation |
| Ruff lint + format with a documented baseline | PR reviews, comments, issues, releases, CI runs |
| Before/after pass-to-pass test verification | |

`manifest.json` tracks all four pipeline stages, so later stages can mark
themselves complete without rewriting what earlier ones produced.

## Requirements

Python 3.12 or newer and `git` on `PATH`. There are **no runtime dependencies** —
the package is pure standard library.

## Installation

```bash
uv tool install --editable /Users/adityagk/Desktop/projects/stress_stack
```

```bash
uv tool update-shell
```

Restart the terminal after `uv tool update-shell`. The command can then be run
from any directory without activating a virtual environment.

For local development instead:

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

## Usage

Clone and ingest a public GitHub repository into the current directory:

```bash
stress-stack ingest https://github.com/mahmoud/glom
```

Ingest a local repository:

```bash
stress-stack ingest /path/to/repository
```

Run it from inside a repository, or pass no source at all — the current
directory is the default:

```bash
stress-stack ingest
```

Every run prints a short summary:

```text
Repository: /path/to/glom
Action: cloned
HEAD: 30b477ab65560914a38f331614947d0894701044
Commits: 1124
Pull requests: 126 (available)
Metadata: /path/to/glom/.stress_stack
```

`Action` is `cloned` for a fresh clone, `reused` when an existing matching clone
was validated and fetched, and `analyzed_local` for a local path.

For a GitHub URL, the repository is cloned with full history. If a directory
with the same repository name already contains a matching clone, `stress_stack`
validates it, fetches branches and tags, and reuses it. An unrelated existing
directory is never overwritten — that is an error, not an overwrite. Shallow
clones are unshallowed before extraction so history is always complete.

## Repo hygiene

```bash
stress-stack hygiene https://github.com/mahmoud/glom
```

Same source interface as `ingest` — URL, path, or nothing for the current
directory. The stage runs in a fixed order:

1. **Snapshot tests.** Run the repository's existing suite and record the
   outcome of every test.
2. **`ruff check --fix`**, then **`ruff format`.** This order matters: fixing
   after formatting leaves the tree un-formatted, which breaks byte-identical
   reruns. Fix-then-format is idempotent — a second pass changes nothing.
3. **Adopt a lint baseline.** Whatever violations survive the fix pass are
   written into a generated `ruff.toml` as `[lint] ignore`, annotated with the
   per-rule counts and the adoption date.
4. **Snapshot tests again** and diff the two runs.

```text
Repository: /path/to/glom
Action: reused
Ruff: 0.15.7 (safe fixes only)
Lint: 145 -> 94 (51 fixed, 10 rules baselined)
Format: 31 files reformatted
Config: /path/to/glom/ruff.toml
Tests before: 202 passing of 202 (202 passed)
Tests after:  202 passing of 202 (202 passed)
Regressions: 0
Status: complete
```

The command exits non-zero only when a test that was passing before hygiene
stops passing after it.

### Why a baseline instead of fixing everything

The acceptance bar is `ruff check .` exiting 0. Reaching it by hand-editing
hundreds of pre-existing violations would rewrite code this pipeline is supposed
to leave functional. Instead the surviving rules are ignored explicitly, in one
file, with the count and date recorded — the rules stay enforced for all new
code, and the debt is visible rather than hidden.

### Why unsafe fixes are not applied

`ruff check --fix --unsafe-fixes` was measured against the sample repository and
rejected on evidence:

| Pass | Violations | Tests passing | Regressions |
| --- | --- | --- | --- |
| baseline | 145 | 202 / 202 | — |
| `--fix` (safe) | 94 | 202 | 0 |
| `--fix --unsafe-fixes` | 66 | 200 | **2** |

The unsafe pass rewrote `assert glom(True, Fill(M | "default")) == True` into a
bare truthiness check, in a library whose entire purpose is overloading `==`.
That both broke the tests and *weakened* the surviving assertions. The 28 extra
violations it resolves are absorbed by the baseline anyway, so the trade buys
nothing and costs behavior.

### Test verification

Tests run in a virtual environment under `.stress_stack/tools/`. Three details
make the run faithful, and each was found by a test that failed without it:

- **`PYTHONPATH` points at the working tree**, so the suite exercises the code
  that was just reformatted rather than an installed copy.
- **The repository's scripts directory is on `PATH`**, so tests that shell out
  to a console entry point (`subprocess.check_output(["glom", ...])`) find it
  instead of raising `FileNotFoundError`.
- **Declared test extras are installed.** After installing the repository,
  `pip install --report` exposes the project's own `Provides-Extra` metadata;
  any extra named `test`, `tests`, `dev`, or similar is then installed. glom
  declares `test = [pytest, PyYAML, tomli, coverage]`, so its YAML-dependent CLI
  tests run instead of failing on a missing import. This is read from metadata,
  never guessed per repository.

The repository is installed non-editable, which keeps `git status` free of
`egg-info` build artifacts. Discovered extras are recorded in
`.stress_stack/hygiene/environment.json`.

Comparison is **pass-to-pass**, not "all green". A repository that genuinely
arrives with failing tests still yields a usable verdict: only tests that were
passing before and are not passing after count as regressions. Genuine
pre-existing failures are recorded and left alone — they are material for later
task generation, not something this stage should silently repair.

If the suite cannot run at all, hygiene still completes and reports status
`complete_unverified`; a repository with no tests reports `complete_no_tests`.
Treat either as a signal to inspect `environment.json` — an unverified run is
usually a provisioning gap, not a broken repository.

## GitHub authentication

Commit history always comes from Git and does not depend on GitHub's API.
Public pull-request metadata is optional enrichment.

If `GITHUB_TOKEN` is set, `stress_stack` uses it for the GitHub REST request:

```bash
export GITHUB_TOKEN=github_pat_...
```

Without a token, the public unauthenticated API is used. API failures or rate
limits are recorded as partial or unavailable PR metadata and do not invalidate
the Git history extraction. Tokens are never written to metadata or logs, and
credentials embedded in a remote URL are stripped before anything is recorded.

## Repository-local metadata

Every successful run creates or refreshes:

```text
.stress_stack/
├── manifest.json
├── repository.json
├── availability.json
├── history/
│   ├── commits.jsonl
│   ├── pull_requests.jsonl
│   └── commit_pr_links.jsonl
├── hygiene/
│   ├── lint.json
│   ├── residual_violations.json
│   ├── tests_before.json
│   ├── tests_after.json
│   ├── environment.json
│   ├── comparison.json
│   └── report_{before,after}.xml
├── tools/
│   ├── lint/          # pinned ruff
│   └── test/          # pytest + the repository under test
├── logs/
│   ├── ingestion.jsonl
│   └── hygiene.jsonl
└── state/
    └── latest.json
```

Hygiene additionally writes `ruff.toml` at the repository root — the one file
this tool creates outside `.stress_stack/`. An existing ruff configuration is
copied to `.stress_stack/hygiene/ruff.toml.original` before being replaced.

- `manifest.json` identifies the schema, tool version, and pipeline stages.
- `repository.json` captures the repository identity and resolved Git state:
  origin, default and current branch, HEAD, shallowness, branches, and tags.
- `availability.json` explicitly lists which history sources were available,
  partial, unavailable, or intentionally not collected.
- `commits.jsonl` contains commit metadata and per-file change statistics,
  ordered deterministically (`rev-list --all --topo-order --reverse`).
- `pull_requests.jsonl` contains public GitHub PR metadata when accessible.
- `commit_pr_links.jsonl` links commits to PRs using merge SHAs and recognized
  merge or squash messages, recording which method produced each link.
- `ingestion.jsonl` is an append-only sanitized execution log, including the
  exact Git commands run, durations, counts, and rate-limit state. Failures are
  logged too, then re-raised.
- `latest.json` identifies the HEAD SHA represented by the current metadata.
- `hygiene/lint.json` records violation counts before and after the fix pass,
  the baselined rules, the pinned ruff version, and the verifying exit codes of
  `ruff check .` and `ruff format --check .`.
- `hygiene/residual_violations.json` lists every surviving violation with its
  file, line, and message, so the baseline is auditable rather than a bare list
  of rule codes.
- `hygiene/comparison.json` is the pass-to-pass verdict: regressions, repairs,
  and tests that appeared or disappeared between the two runs.
- `hygiene/report_before.xml` and `report_after.xml` are the raw JUnit reports
  the verdict is derived from.

`stress_stack` adds `.stress_stack/` to `.git/info/exclude`. This keeps tool
state out of `git status` without changing the repository's tracked
`.gitignore`.

All writes are atomic (write-temp-then-`os.replace`), so an interrupted run
never leaves a half-written record behind. Re-running replaces history in place
rather than appending, so metadata never accumulates duplicates.

## Availability semantics

Every history capability has one of four states:

- `available`: collection completed, including a valid zero-item result.
- `partial`: some records were collected before an external interruption.
- `unavailable`: the source cannot be accessed for this repository.
- `not_collected`: the source exists conceptually but is outside this stage.

This stage collects commits, diffs, branches, tags, merges, contributors, and
core public PR metadata. PR reviews, PR comments, issues, releases, and CI run
history are reported as `not_collected`.

A local repository with no public GitHub origin reports `pull_requests` as
`unavailable` with reason `no_public_github_origin`, and the run status is
`partial`. That is expected, not a failure — Git history is still complete.

## Commit ↔ PR linking

Two methods, recorded per link:

- `github_merge_sha` — the PR is merged and its `merge_commit_sha` matches a
  commit in the repository. This is exact.
- `commit_message` — the commit message matches `Merge pull request #N` or a
  trailing squash marker `(#N)`, and PR `N` exists. Only PR numbers confirmed
  present in the fetched PR set are linked, so stray `(#N)` text cannot
  fabricate a link.

Merge-SHA links take precedence when both methods agree on a pair.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite builds real temporary Git repositories (branches, a no-fast-forward
merge, a tag) and exercises clone/reuse/conflict handling, commit extraction,
URL parsing and credential sanitizing, PR pagination and rate-limit degradation
against a stubbed requester, link derivation, and full ingestion including the
guarantee that ingestion leaves `git status` clean.
