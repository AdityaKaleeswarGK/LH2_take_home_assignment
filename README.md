# stress-stack

stress-stack takes a real-world software repository — typically one with no pinned
dependencies, no container, no linting, and patchy tests — and turns it into three
things: a reproducible environment that builds and tests identically twice in a row,
a machine-readable knowledge layer describing every file and symbol in it, and ten
independently validated benchmark tasks for AI coding agents. Every task ships with
the tree an agent starts from, the reference solution, the tests that decide pass or
fail, and container logs proving those tests fail before the change and pass after
it. A model writes prose and judges difficulty; it never decides which tasks ship.
Every acceptance decision is a measured container run, recorded and re-runnable.

---

## Architecture

A repository comes in at the top, gets profiled once, and then flows down through the
three pipelines. The two boxes off to the side are what the stages reach for rather
than steps of their own — the ecosystem's tools, and OpenRouter for the two stages
that call a model.

The diagram shows the load-bearing stages. `testgen`, `index` and `bundle` also run,
in that order, but they follow from their neighbours and would only add width here.

```mermaid
%%{init: {"flowchart": {"htmlLabels": false, "padding": 18, "nodeSpacing": 55, "rankSpacing": 55}} }%%
flowchart TD
    SRC([repo URL or path]) --> DET

    subgraph detect["Detection"]
        DET[project_detector<br/>+ ci_parser]
    end

    subgraph env["Pipeline 1"]
        HYG[hygiene<br/>format + lint]
        DEP[deps<br/>hash-pinned lock]
        CON[container<br/>2 identical runs]
        HYG --> DEP --> CON
    end

    subgraph know["Pipeline 2"]
        GRA[graph<br/>symbols + edges]
        COV[coverage<br/>test to symbol]
        GRA --> COV
    end

    subgraph gen["Pipeline 3"]
        MIN[mine<br/>history + excision]
        VAL[validate<br/>8 gates]
        SEL[select<br/>quotas]
        EMI[emit<br/>tasks + manifest]
        MIN --> VAL --> SEL --> EMI
    end

    subgraph tools["Per-language tools"]
        T1[ruff / clippy<br/>gofmt / eslint]
        T2[uv / cargo<br/>go mod / npm]
        T3[ast / tree-sitter]
        T4[pytest / go test]
        T1 ~~~ T2 ~~~ T3 ~~~ T4
    end

    subgraph llm["OpenRouter"]
        LLM[enrich + adjudicate<br/>optional]
    end

    DET --> HYG
    CON --> GRA
    COV --> MIN
    EMI --> OUT([output])

    tools -.-> env
    tools -.-> know
    tools -.-> gen
    llm -.-> gen

    classDef stage fill:#e8f0fe,stroke:#3b6fd4,color:#12243d
    classDef gate fill:#fdefdc,stroke:#c47f2d,color:#3d2a12
    classDef model fill:#f3ecfb,stroke:#8257b5,color:#2a1a3d
    classDef io fill:#e6f7ed,stroke:#2f9e5f,color:#123d24

    class DET,HYG,DEP,CON,GRA,COV,MIN,SEL,EMI,T1,T2,T3,T4 stage
    class VAL gate
    class LLM model
    class SRC,OUT io
```

Orange is `validate`, the gate that decides what ships. Purple are the only two
stages that call a model, and you can run without either — with no API key they
degrade and you still get ten validated tasks.

The per-language box expands to this:

| Stage | Python | Go | Rust | TS / JS |
|---|---|---|---|---|
| hygiene | ruff | gofmt, go vet | cargo fmt, clippy | prettier, eslint |
| deps | uv | go mod | cargo | npm / pnpm / yarn |
| graph | `ast` | tree-sitter | tree-sitter | tree-sitter |
| coverage | coverage.py contexts | `go test -coverprofile` | `cargo llvm-cov` (lcov) | v8 (lcov), per file |
| validate | pytest | `go test -json` | libtest | jest / vitest JSON |

Each row is a *default*, not the only path: it is probed against the repository
before it is used, and where a probe fails — or where the ecosystem has no row —
an agent reads the tree and its answer is probed the same way. See
`workflow.py`.

Working state lives in `.stress_stack/` inside the target repository; the deliverable
is written to `output/`.

---

## Setup

### LLM support

**Model inference is provided exclusively through [OpenRouter](https://openrouter.ai).**
There is no direct Anthropic, OpenAI, or Google integration — every model call in the
pipeline goes through a single OpenRouter client, and roles (`worker`, `synthesis`,
`reasoning`) map to model names in config rather than to vendors.

A key is **optional**. Without one, `enrich` and `adjudicate` degrade gracefully:
file cards are skipped and each task keeps its *measured* difficulty tier instead of
a reasoned one. All ten tasks are still generated and validated, because no gate
depends on a model.

### Requirements

| | |
|---|---|
| Python | 3.12+ |
| Docker | running daemon — validation happens only in containers |
| git | any recent version |

### Install

```bash
git clone https://github.com/AdityaKaleeswarGK/LH2_take_home_assignment.git
```

```bash
cd LH2_take_home_assignment && python3.12 -m venv .venv && .venv/bin/pip install -e .
```

To run `stress-stack` from anywhere in your terminal, put the entry point on your
`PATH`:

```bash
ln -s "$PWD/.venv/bin/stress-stack" /usr/local/bin/stress-stack
```

Verify:

```bash
stress-stack --help
```

### Configure the OpenRouter key (optional)

Either export it:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

Or store it once, outside any analysed repository:

```bash
stress-stack configure --api-key sk-or-...
```

---

## Running it

One documented entry point runs all three pipelines end to end:

```bash
./run.sh https://github.com/mahmoud/glom
```

Equivalently, with the profile-aware orchestrator:

```bash
stress-stack orchestrate https://github.com/mahmoud/glom --output output
```

Both accept a URL or a local path.

### What you get

| Path | Contents |
|---|---|
| `output/tasks/` | the ten task folders — `input/`, `solution/`, `verifier/`, `evidence/` |
| `output/tasks.json` | manifest indexing all ten with provenance and validation status |
| `output/repo_graph.json` | the knowledge layer |
| `output/repo/` | the transformed repository — pinned, containerized, lint-clean |
| `<repo>/.stress_stack/` | all working state and evidence (see below) |

`.stress_stack/` inside the target repository is where every claim is backed up:

| Path | Contents |
|---|---|
| `.stress_stack/hygiene/` | lint report, before/after test runs, comparison |
| `.stress_stack/container/` | Dockerfile, build log, both determinism runs |
| `.stress_stack/knowledge/` | graph, coverage map, candidates, validation verdicts |
| `.stress_stack/tasks/<id>/evidence/` | per-task JUnit XML for every gate run |
| `.stress_stack/pipeline_run.json` | every stage, its status and duration |
| `.stress_stack/tools/`, `cache/` | provisioned virtualenvs and the model-response cache |

The glom run's evidence is committed in this repository under
[`evidence/glom/`](evidence/glom) so it can be inspected without re-running anything.
`tools/` and `cache/` are excluded from that copy — they are 67 MB of virtualenvs and
5 MB of cached model responses, reproducible rather than evidential.

---

## Commands

Each stage is also a standalone command, so any stage can be re-run without
repeating the ones before it.

| Command | What it does |
|---|---|
| `stress-stack orchestrate <src>` | detect the ecosystem, then run every stage |
| `stress-stack run <src>` | run every stage |
| `stress-stack ingest <src>` | clone, extract history and pull requests |
| `stress-stack hygiene <src>` | format, lint, verify no regression, revert if any |
| `stress-stack deps <src>` | resolve and hash-pin dependencies |
| `stress-stack graph <src>` | build and verify the symbol graph |
| `stress-stack coverage <src>` | measure which tests execute which symbols |
| `stress-stack testgen <src>` | generate tests, gated on mutation |
| `stress-stack container <src>` | build the image and prove two runs identical |
| `stress-stack enrich <src>` | per-file cards and repository blueprint (model) |
| `stress-stack index <src>` | queryable SQLite projection |
| `stress-stack mine <src>` | rank history and excision candidates |
| `stress-stack validate <src>` | run the eight gates over the ranked pool |
| `stress-stack select <src>` | apply the quotas and diversity floor |
| `stress-stack adjudicate <src>` | an agent reads the code and judges difficulty (model) |
| `stress-stack emit <src>` | write task.json, golden solutions, manifest |
| `stress-stack bundle <src>` | assemble `output/` |

Useful flags:

```bash
stress-stack run <src> --skip enrich,adjudicate
```

```bash
stress-stack orchestrate <src> --history-limit 30 --excision-limit 12 --output output
```

### Running the container test suite by hand

The image the pipeline builds is a normal image; the acceptance bar is that this
passes twice with identical results.

```bash
docker run --rm --network=none --cap-drop=ALL stress-stack/glom:verify
```

---

## Language support

| Ecosystem | Environment | Knowledge layer | Task generation |
|---|---|---|---|
| Python | ruff, uv | `ast` | full — history + excision |
| Go | gofmt, go vet, go mod | tree-sitter | full — history + excision |
| Rust | cargo fmt, clippy, cargo | tree-sitter | full — history + excision |
| TypeScript / JavaScript | prettier, eslint, npm/pnpm | tree-sitter | full — history + excision |
| C / C++ | — | — | recognised and refused |

History mining is no longer Python-only: source classification comes from the
detected profile and the test-unit delta from tree-sitter, so every ecosystem
above can satisfy the quota's `minimum: {history: 4}` rather than being capped at
the four tasks excision allows.

C and C++ are recognised and then refused with a reason, which is deliberate —
supporting them honestly needs a lock strategy, a test plan and a coverage
attributor, and half of those produce verdicts nobody measured. Dropping the
detection instead would make a C++ repository fall through to the Python default
and be analysed as Python, which is a wrong answer rather than an absent one.

Anything without a parser or test plan is reported as `unsupported` with a reason,
never as a silent zero. See `REPORT.md` for the full account of what is missing.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python -m ruff check src tests
```
