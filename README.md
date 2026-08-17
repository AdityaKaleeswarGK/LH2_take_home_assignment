# stress-stack

Takes a Python repository — usually one without pinned dependencies, tests,
linting, or a container — and turns it into a reproducible environment, a
machine-readable knowledge layer, and ten independently validated benchmark
tasks for AI coding agents. Every task ships with the tree an agent starts from,
the reference solution, the tests that decide pass or fail, and the container
logs proving those tests fail before the change and pass after it. A model
writes prose and judges difficulty; it never decides which tasks ship. Every
acceptance decision is a measured container run, recorded and re-runnable.

## Layout

```text
stress_stack/
├── run.sh                  # single documented entry point
├── REPORT.md               # the engineering write-up
├── pyproject.toml
├── src/stress_stack/
│   ├── cli.py              # every subcommand
│   ├── pipeline.py         # the fifteen stages, in dependency order
│   ├── progress.py         # live per-stage output
│   ├── graph.py            # symbol graph, plus each stage's entry point
│   │
│   │                       # Pipeline 1 — environment
│   ├── ingest.py           # clone, extract history and pull requests
│   ├── hygiene.py          # ruff lint and format, verified before/after
│   ├── dependencies.py     # hash-pinned lockfile, PEP 735 groups included
│   ├── container.py        # Dockerfile, built and proven deterministic
│   ├── testgen.py          # generated tests, gated on mutation
│   │
│   │                       # Pipeline 2 — knowledge layer
│   ├── symbols.py          # module naming and package roots
│   ├── coverage_map.py     # which tests execute which symbols
│   ├── enrich.py           # per-file cards and the repository blueprint
│   ├── cards.py            # the grounded evidence a card is built from
│   ├── index.py            # queryable SQLite projection
│   │
│   │                       # Pipeline 3 — task generation
│   ├── candidates.py       # rank every history and excision candidate
│   ├── excision.py         # remove a function body by dotted path
│   ├── tasks.py            # stage the four trees, run the eight gates
│   ├── validate.py         # walk the ranked pool until enough survive
│   ├── verification.py     # what a run means: the gates themselves
│   ├── alternatives.py     # would a different correct solution also pass
│   ├── selection.py        # the ten, under the brief's quotas
│   ├── adjudicate.py       # an agent reads the code and judges difficulty
│   ├── explore.py          # the read-only tool surface that agent gets
│   ├── instruct.py         # task statements, leak-checked
│   ├── emit.py             # task.json, golden solutions, the manifest
│   └── bundle.py           # assemble output/
│
└── tests/
    ├── adversarial.py      # a repository built to break this pipeline
    └── test_*.py
```

Working state lives in `.stress_stack/` inside the target repository, and the
deliverable is written to `output/`:

```text
output/
├── repo/                   # the transformed repository
├── .okf/                   # the knowledge layer
├── repo_graph.json
├── tasks/<task_id>/        # task.json, input/, solution/, verifier/,
│                           # goldenSolution.md, goldenSolution.diff, evidence/
├── tasks.json
└── transcripts/            # every model exchange, request and response
```

## Setup

Python 3.12 or newer, `git`, and Docker. The package itself has **no runtime
dependencies** — it is pure standard library.

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

Verification runs in containers, so Docker must be running. There is no host
fallback: an isolated run and an unisolated one do not support the same claim.

Model stages need an OpenRouter key. It is prompted for without echoing, and
stored outside any repository at `~/.config/stress-stack/config.json`, mode 600.

```bash
stress-stack model --set-key
```

Without a key the pipeline still completes — enrichment and adjudication degrade
rather than fail, and difficulty falls back to the measured tier.

## Running it

```bash
./run.sh https://github.com/mahmoud/glom --output output
```

To see the single full-run command and all fifteen stages individually:

```bash
stress-stack commands
```

Tests:

```bash
.venv/bin/python -m pytest
```
