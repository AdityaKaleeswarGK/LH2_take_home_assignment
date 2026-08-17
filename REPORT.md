# REPORT

Target repository: **[mahmoud/glom](https://github.com/mahmoud/glom)** — a Python
library for restructuring nested data. Chosen artefacts from the run are committed
in this branch so every claim below can be inspected without re-running anything:

| Where | What |
|---|---|
| [`evidence/glom/`](evidence/glom) | the run's evidence — lint report, container determinism runs, knowledge layer, per-task gate logs |
| [`output/`](output) | the deliverable — ten tasks, manifest, knowledge layer, transformed repo |

---

## 1. What was broken, and how the pipeline fixes each class

glom is a mature, well-tested Python library that is nonetheless missing every piece
of reproducible engineering infrastructure the brief asks for.

| Class of problem | State before | What the pipeline does | Evidence |
|---|---|---|---|
| **No dependency pinning** | `setup.py` with unpinned ranges; a fresh clone resolves differently on different days | `uv pip compile` with `--generate-hashes`, including the `test` extra, into `requirements.lock` | [`knowledge/dependencies.json`](evidence/glom/knowledge/dependencies.json) — `hashed: true` |
| **No containerization** | none | Dockerfile generated from measured facts, base image pinned by **digest** (`python@sha256:48a11b7b…`), not tag | [`container/container.json`](evidence/glom/container/container.json) |
| **No proof of determinism** | none | suite run twice in the container and compared per test, plus against the host baseline | `run1` / `run2`: **203 tests, 203 passed, 0 failed**, `baseline_match: matches` |
| **No lint or format config** | none | `ruff.toml` generated; `ruff format` and `ruff check --fix` applied; result verified | [`hygiene/lint.json`](evidence/glom/hygiene/lint.json) — `lint_clean: true`, `format_clean: true` |
| **Formatting could silently break tests** | n/a | full suite snapshotted before and after; any regression reverts the change | [`hygiene/comparison.json`](evidence/glom/hygiene/comparison.json) |
| **No machine-readable structure** | none | symbol graph re-derived from a second parse and compared edge by edge | [`knowledge/graph_validation.json`](evidence/glom/knowledge/graph_validation.json) |

There is one number here I would rather say plainly than let you find on your own:
glom has **94 residual lint violations, and the pipeline fixed none of them**.
`violations_before: 94`, `violations_after_fix: 94`. What it actually did was write a
`ruff.toml` whose rule selection the repository already passes, which moves those 94
into ten explicitly ignored rules — 57 `F401`s, 19 `E731`s, 3 `E402`s, and so on.

I think that is the right call, but it is worth being clear about why. Fifty-seven of
those `F401`s are re-exports in `__init__.py`, which is the entire job of that file;
"fixing" them would break glom's public API to satisfy a linter. So the repo is
genuinely lint-clean, but it is clean *under a policy* — and the policy is committed
and readable rather than implied. If you read "lint-clean" as "the code changed until
the linter went quiet", that is not what happened here.

### The ten tasks generated for glom

6 history-derived, 4 excision, spanning **9 distinct modules**, quota satisfied,
zero instructions failing the leak check.

| # | Task | Source | Module | Difficulty |
|---|---|---|---|---|
| 1 | Make `PathAccessError` formatting robust for scope and non-path accesses | history | `glom.core` | hard |
| 2 | Add `--scalar` flag to CLI | history | `glom.cli` | medium |
| 3 | Make nested glom specifications and arguments evaluate consistently | history | `glom.core` | hard |
| 4 | Add TOML target support to the glom CLI | history | `glom.cli` | hard |
| 5 | Implement the grouping mode dispatcher | excision | `glom.grouping` | medium |
| 6 | Implement matching precedence | excision | `glom.matching` | medium |
| 7 | Implement `glom.core.Path.from_t` | excision | `glom.core` | medium |
| 8 | Implement scope-path resolution for `_s_first_magic` | excision | `glom.core` | medium |
| 9 | Expand and harden the glom command-line interface | history | `glom.cli` | easy |
| 10 | Better builtin roundtripping | history | `glom.core` | hard |

Every task carries its full provenance in its own `task.json` — the commit SHA, the
base SHA, and the upstream pull request it came from — so you can trace any of them
back without that detail crowding the table here.

Difficulty spread: 1 easy, 5 medium, 4 hard.

Each task's `evidence/` holds JUnit XML for every gate run. Task 1's fail-before, for
example, is classified `behavioral_exception` on both designated tests — an assertion
about behaviour, not an import error, which is what §5.4 requires.

**Net-new tasks (§5.1, max 3 of 10) are not implemented.** The quota is satisfied
without them (6 history ≥ 4 required, 4 excision ≤ 4 allowed), so the deliverable is
complete, but that task category does not exist in this codebase. See §6.

---

## 2. Design decisions and trade-offs

**A model never decides what ships.** This is the constraint everything else was
built around. Models write prose (`enrich`), phrase the task statements (`emit`), and
judge difficulty (`adjudicate`) — and all three can be switched off, because none of
them touch acceptance. Every acceptance decision is a container run someone can
re-execute. The reasoning is simple enough: if a benchmark's pass/fail criteria are
model opinions, then what you have measured is the judge, not the agent.

**Validation only ever happens in a container.** There was a host-interpreter
fallback; it was removed deliberately. A verdict reached without network isolation, a
read-only tree and a resource ceiling is a weaker claim than the brief asks for, and
a pipeline that silently produces the weaker claim is worse than one that stops.

**Doctors, not a second pipeline.** Multi-language support dispatches through
`hygiene_dispatcher` / `dependency_doctor` / `container_doctor`, each of which routes
Python straight back to the original implementation. The alternative — a parallel
multi-language pipeline — means two code paths to keep in agreement, and the Python
one is the one with all the evidence behind it.

**`unsupported` is a first-class result.** When a tool cannot run, it says
`unsupported` with a reason and `measured: false` — it never returns a zero. This one
came out of fixing my own mistake. An earlier version hard-coded `runs_identical=True`,
an "approximate" pin count of 10, and an unconditional `status: complete`. On a repo
I had already run, those looked fine. On a held-out repo they would have reported
success while measuring nothing at all, which is worse than crashing. So the rule now
is that a stage may report ignorance, but it may not report a success it did not
measure.

**Tree-sitter over regex, for a specific reason.** The knowledge layer for
non-Python languages parses with real grammars because excision needs a function
body's *exact* extent. Regex brace-counting gets `let s = "}";` wrong — it ends the
function at that line, and excising it produces an `input/` tree that never compiles.
Byte spans from the grammar are used rather than line ranges, because a single-line
definition (`func Mul(a, b int) int { return a * b }`) shares its line with the
signature, and rewriting that line deletes the declaration.

**Python keeps `ast`.** Even with tree-sitter available, Python parses with `ast`:
it is the same parser CPython uses, it resolves dotted names and decorators without a
node-type table, and every downstream stage is already built on its output.

**Automated vs. manual.** Everything in the pipeline is automated — there are no
manual fix-ups, and no glom-specific branches anywhere in `src/`. What was done by
hand is *calibration*: the adjudication turn budget (8) and the validation surplus
(14 attempted for 10 shipped) were chosen by reading measured runs, and both are
documented in the code beside the constant.

**Parallelism where the property allows it.** `adjudicate` runs ten agents
concurrently. `emit` originally generated statements serially so each prompt could
carry the titles written so far, avoiding ten near-identical titles — 915 s on glom,
the largest single cost in the pipeline. Ordering was never the goal; distinct titles
were. Statements are now drafted in parallel and any title collision is regenerated
with context: **915 s → 96 s**, 10/10 titles still unique, zero collisions in
practice.

**Model responses are cached on the request hash.** The cache is both the
determinism mechanism (`temperature=0` is not sufficient) and the transcript. It is
also the difference between a 50-minute cold run and a 16-minute warm one — `emit`
915 s → 1.5 s, `adjudicate` 818 s → 0.7 s on re-run.

---

## 3. How task-candidate selection works

Selection is a funnel, and I wanted every rejection to be inspectable rather than
just counted — so each one is recorded with the reason it was dropped.

**History candidates — 86 considered, 70 kept.** Merged pull requests linked to
commits that changed both source and tests. 16 dropped for `no_python_change`.

**Excision candidates — 635 considered, 302 kept.** Every symbol in the graph with
measured covering tests. Dropped: 249 `is_itself_a_test`, 84 `no_covering_test`. A
symbol nothing exercises cannot produce a task whose verifier means anything.

**Validation — 28 attempted, 14 eligible.** The pool is validated in ranked order
until enough survive. Rejections, all recorded in
[`knowledge/validation.json`](evidence/glom/knowledge/validation.json):

| Reason | Count | Why it disqualifies |
|---|---|---|
| `only_uncollectable_tests_changed_verdict` | 6 | the PR's tests fail to import at the base commit — an infrastructure failure, not behavioural |
| `no_test_changed_verdict` | 6 | tests pass identically before and after; nothing to measure |
| `collateral` | 2 | the reference solution broke an unrelated passing test |

**Selection — 14 eligible → 10 shipped**, under the brief's quotas (≥4 history, ≤4
excision, ≤3 net-new) plus a diversity floor. The result spans 9 modules, so the
"at least 4 distinct modules" requirement is met with margin.

Each of the eight gates a task must pass:

`fail_before` (and for the right reason) · `pass_after` · `collateral` ·
`determinism_before` · `determinism_after` · `verifier_integrity` · `solver_bundle` ·
`alternative_implementation`

---

## 4. How to run everything

Setup is in [README.md](README.md). End to end:

```bash
./run.sh https://github.com/mahmoud/glom
```

Stage by stage, in dependency order — each reads what the previous one wrote:

```bash
stress-stack ingest    https://github.com/mahmoud/glom
```
```bash
stress-stack hygiene   glom && stress-stack deps glom && stress-stack graph glom
```
```bash
stress-stack coverage  glom && stress-stack testgen glom && stress-stack container glom
```
```bash
stress-stack enrich    glom && stress-stack index glom && stress-stack mine glom
```
```bash
stress-stack validate  glom && stress-stack select glom && stress-stack adjudicate glom
```
```bash
stress-stack emit      glom && stress-stack bundle glom --output output
```

Without a model, skip the two optional stages — the ten tasks are still produced and
validated:

```bash
stress-stack run glom --skip enrich,adjudicate
```

### The container test run

```bash
docker run --rm --network=none --cap-drop=ALL stress-stack/glom:verify
```

Run it twice; the results are identical (203 passed, 0 failed). To re-verify one
task's evidence by hand, copy `input/`, overlay `verifier/`, and run the command in
that task's `task.json` — it must fail before the change and pass after.

---

## 5. Scale: what breaks at 100 repositories

**Wall clock is the wall.** A cold glom run is ~50 minutes, 68% of it model
inference. 100 repositories serially is roughly three and a half days. The fix is
process-level parallelism across repositories — each run is already independent and
writes only into its own `.stress_stack/` — plus a shared model-response cache
instead of a per-repo one.

**Container churn dominates the rest.** Each candidate costs 7+ container
invocations, and validation is serial within a repo. Two changes matter more than
anything else here: a persistent per-repo image reused across candidates (already the
case) and a bounded worker pool over the candidate pool (`TaskTracker` exists and is
tested; it is not yet wired in).

**Disk grows faster than expected.** Every task stages four full copies of the
repository. Ten tasks × 100 repos is 4,000 trees. At scale these should be git
worktrees or content-addressed overlays, not `cp -r`.

**What I would build differently.** A work queue with per-repo idempotent stages and
resumable state, rather than one long linear process; the stage table already records
per-stage status, so this is a scheduler change rather than a rewrite. And I would
make the interpreter/toolchain *per candidate* rather than per repository — see §6.

---

## 6. Honest gaps

**Language coverage is narrower than the architecture implies.** The design is
grammar-agnostic — dispatch is a table, and adding a language is adding rows — but
the current implementation only genuinely supports **Python, Go, Rust, TypeScript and
JavaScript**, and not equally:

| | parser | linter | test plan | coverage | tasks |
|---|---|---|---|---|---|
| Python | `ast` | ruff | pytest | per-test | history + excision |
| Go | tree-sitter | go vet | `go test -json` | per-test | excision |
| Rust | tree-sitter | clippy | libtest | ✗ | ✗ |
| TS / JS | tree-sitter | eslint | ✗ | ✗ | ✗ |
| C / C++ | tree-sitter | clang-tidy | ✗ | ✗ | ✗ |

*Next step:* JS/TS need a reporter the repo already provides (`vitest --reporter=json`);
C++ needs `compile_commands.json` from a CMake configure. Both are wiring, not
research. Rust coverage needs `cargo-llvm-cov` per test.

**Net-new tasks are not implemented.** §5.1 allows up to 3 of 10 tasks to be
net-new features defined entirely by tests we author. The category is de-scoped: the
quota is satisfiable without it, and a net-new task is the one category with no
ground truth to validate against — the reference solution would be ours, so
"pass-after" would only prove we can pass our own tests. *Next step:* generate the
feature from the repository's own issue tracker and require the alternative-
implementation gate at a higher bar.

**Linting is name-level and compile-level only, because of an LLM cost ceiling.**
The repair agent fixes what a linter can identify but not rewrite. Doing that
*properly* means giving a model the whole project context plus the full linter log
for the repository, and deciding per violation whether a fix is safe. On a repo the
size of glom that is a very large prompt, repeated per round — the cost scales with
repository size, not with violation count. So the current agent works from a bounded
file set (12 files, 60 KB each) and only accepts changes that lower the violation
count *and* leave suite output byte-identical. Everything else reverts. *Next step:*
cluster violations by rule and fix per-rule with a minimal witness set rather than
whole files.

**A repository that the test runner itself depends on loses most of its
candidates — measured, not hypothetical.** Validation mounts the task tree and puts
it on `PYTHONPATH`. When the repository under test *is* a pytest dependency, that
tree shadows the copy pytest is running on, and a historical version of it will not
satisfy a modern pytest.

The pipeline detects this and names it (`runner_dependency_shadowed`) rather than
reporting a generic failure — the guard lists `pluggy`, `iniconfig`, `packaging`,
`exceptiongroup`, `tomli`, `pytest`, `_pytest`. But detection is not a fix. Running
against **[pytest-dev/pluggy](https://github.com/pytest-dev/pluggy)**:

| | |
|---|---|
| candidates attempted | 35 |
| **rejected: `runner_dependency_shadowed`** | **16** |
| rejected: `no_test_changed_verdict` | 9 |
| rejected: other (collateral, uncollectable, run failed) | 3 |
| **eligible** | **7** |

Pipelines 1 and 2 completed cleanly on pluggy — hygiene, hash-pinned lockfile,
digest-pinned container with two identical runs, knowledge layer, coverage. Pipeline
3 produced 7 validated tasks, not 10, and the single largest cause was shadowing.

*Next step:* stop using `PYTHONPATH` for the Python runner. The image already
installs the project editable at `/work`, so the mounted task tree is picked up
without putting it on the import path ahead of the runner's own dependencies. The
multi-language `LanguageRunner` already sets no `PYTHONPATH` at all for this reason;
the Python `DockerRunner` is the remaining case.

**Historical PRs are validated under HEAD's interpreter.** `select_python_version`
reads HEAD's `requires-python` once, and the lockfile compiles from HEAD. A 2017 PR
gets tested under Python 3.12. This does not produce wrong results — such PRs fail
collection and are discarded by the gates — but it silently biases the pool toward
recent history and could starve the ≥4 history quota on an older repository. *Next
step:* resolve the interpreter per candidate from the base commit's metadata and
cache one image per version.

**Host vs. container asymmetry is closed for hygiene, not everywhere.** Formatting
and linting for non-Python ecosystems now run their before/after suite comparison in
a container. But `deps` probing and Python's `coverage` still execute repository code
on the host. *Next step:* move both behind the same sandbox `validate` already uses.

**Candidate validation is serial.** `--workers` is accepted and documented as
reserved; `TaskTracker` is written and tested but not wired in. This is the single
biggest available speedup after model inference.

**Three vendored C++ headers fail to parse.** `httplib.h`, `json.hpp`, `xxhash.h` —
heavy macro metaprogramming that tree-sitter's C++ grammar handles only partially. It
still extracts 2,290 symbols from them and reports `has_error` honestly rather than
claiming a clean parse.

**Held-out generality, tested once and reported honestly.** The brief says the
pipeline will be run against a repository I have not seen. I tested that claim on
pluggy rather than assuming it: Pipelines 1 and 2 held up on a repository chosen for
being hostile, and Pipeline 3 fell to 7 of 10 for the specific, named reason above.
A second held-out repository that is *not* a test-runner dependency has not been run,
so the honest statement is that generality is demonstrated for the environment and
knowledge layers and only partially for task generation.

**Testing breadth.** The Go path is proven end to end on a small purpose-built
repository (all 8 gates pass on real excision tasks). It has not been run against a
large real-world Go codebase, and the per-test coverage strategy — one process per
test — will be slow on a suite with thousands of tests. `max_tests` bounds it and the
map records how many tests were actually measured, so a truncated attribution is
never mistaken for a complete one.
