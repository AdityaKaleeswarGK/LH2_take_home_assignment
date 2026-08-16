# Engineering report — stress_stack

Every figure below was measured against `mahmoud/glom` by the pipeline itself and
is reproducible from the artifacts in `output/`. A second repository,
`pallets/click`, was run end to end to test generality; what it found is in
section 5.

---

## 1. What was broken in the repo, and how the pipeline fixes each class of problem

**Unpinned dependencies.** glom declared dependencies without exact versions, so
two installs a week apart are two different environments and any test result is
unattributable. `deps` classifies every import as internal, stdlib or
third-party by asking the *provisioned interpreter* rather than by matching
names, then compiles a hash-pinned lock: 11 packages, hashed. It also audits in
both directions and reports what it cannot verify — glom declares `coverage` and
`tomli` without importing them, and four distributions (`setuptools`, `sphinx`,
`sphinx_rtd_theme`, `tomli`) do not resolve to an importable top level. Those are
reported rather than silently reconciled.

**No reproducible execution environment.** `container` generates a Dockerfile
from the lock, pins the base image by digest at the exact interpreter patch
level (`python:3.12.9-slim`), builds, and runs the suite **twice**, requiring
identical results: 204/204 passing on both runs, matching the host baseline.
Acceptance requires all three — build, identical repeats, and baseline parity —
so a stably-failing suite cannot be blessed as verified.

**Inconsistent style masking real defects.** `hygiene` runs `ruff check --fix`
then `ruff format`, in that order because fixing after formatting leaves the
tree unformatted and breaks byte-identical re-runs. 31 files were reformatted;
145 violations dropped to a baseline of 10 explicitly ignored rules, annotated
with per-rule counts (`F401` 57, `E731` 19, and so on) so the debt is visible
rather than hidden. The suite is snapshotted before and after: **202 passing
before, 202 after, 0 regressions.**

Unsafe fixes were measured and rejected on evidence, not on principle:
`--unsafe-fixes` resolved 28 more violations but rewrote
`assert glom(True, Fill(M | "default")) == True` into a bare truthiness check —
in a library whose entire purpose is overloading `==`. It broke two tests *and*
weakened the surviving assertions. The baseline absorbs those violations anyway,
so the trade buys nothing and costs behaviour.

**Untested public surface.** `testgen` finds public callables with no covering
test, asks a model for tests, and then refuses to take its word for it. A
generated test ships only if it (a) contains a real assertion or `pytest.raises`,
checked on the AST; (b) passes against the original; and (c) **fails against a
mutant** whose body is replaced with a raising stub. On glom one file shipped,
catching its mutation of `glom/cli.py:console_main` — exit 1, two failures
against the mutant. That is the brief's "do your tests catch deliberately
introduced bugs" measured directly rather than asserted.

**No machine-readable model of the code.** `graph` parses every file with `ast`
into 668 symbols and 2364 edges across 32 files, each edge carrying the anchor it
was derived from. `validate_graph` then re-parses the whole repository from
scratch and re-derives every edge: **edge match 1.0000, anchor match 1.0000.**
1738 references remain unresolved and are recorded *with a reason* — 653
builtins, 598 dynamic-or-local — rather than pointed at a guess.

---

## 2. Design decisions and trade-offs

**The model may contextualise; it may never gate.** Every decision about which
task ships is made by a container: does the designated test fail before, pass
after, stay deterministic, break nothing else. Model output is prose — the
instruction's wording, a feature name, this report — and is stored with its
model id and prompt hash. The consequence is a property worth stating: with no
API key the pipeline still produces ten validated tasks, with mechanical
instruction text. The model improves the deliverable; it is never load-bearing.

**Mining ranks; it does not judge.** This was reversed during development, and
the reversal is the most useful thing in this report. Mining originally rejected
pull requests that changed no test file, that touched only `docs/`, or whose
churn exceeded the 90th percentile. Each looked like a measurement and was
actually a prediction, and each encoded a fact about glom rather than about
repositories. The killer counterexample: **a bug fix whose failing test was
already committed touches no test file at all**, and its fail-before evidence is
stronger than a new test's, because the repository itself pinned the bug. An
adversarial fixture repository (`tests/adversarial.py`) was built to contain
exactly that case, plus a `src/` layout, no docstrings, one test per function,
and real code under `docs/`. Three of those filters failed against it.

What survives as a rejection is only what makes a task impossible to build: no
commit, no parent, no Python changed, no covering test, a symbol that is itself
a test. Everything else became a ranking term, where being wrong costs a
candidate a position instead of its existence.

**Thresholds are measured, never declared.** The infrastructure ceiling for
excision is a share of the suite (a symbol run by more than one test in seven is
load-bearing), not a constant — "12 covering tests" was true of glom and of
nothing else. Difficulty tiers are cut by rank into equal thirds rather than at
fixed line counts. Churn is recorded for tiering and cost, and rejects nothing.

**The verifier is applied at evaluation time, not shipped in `input/`.** A
history task's tests are the ones the pull request added. An agent that can read
them does not need the instruction — it transcribes the assertions. So `input/`
is the repository as it stood before, `verifier/` holds the post-change tests,
and the evaluation tree is rebuilt from those two on demand. Anyone can
reconstruct it, which is what makes the fail-before claim checkable rather than
merely reported.

**Validation runs only in a container.** A host fallback existed and was deleted.
An isolated run and an unisolated one do not support the same claim, and a
pipeline that silently substitutes the weaker one is worse than one that stops.

**Automated vs. manual.** Automated: everything above, plus selection,
difficulty, instruction writing, leak checking and bundling. Manual: the choice
of adversarial cases in the fixture repository, the decision to park net-new
(section 6), and refinement of this document.

---

## 3. How task-candidate selection works

**Mined.** 70 history candidates from 86 merged pull requests; 302 excision
candidates from the coverage map. Only structural impossibility was rejected: 16
pull requests changed no Python at all; 250 excision candidates were themselves
tests (excising a test deletes the verifier) and 84 had no covering test.

**Ranked.** History ranks on added test functions (0.35), verifier presence,
coordination, cross-module reach, description length and recency. Added-test
count dominates because breadth does not predict viability — an earlier weighting
that rewarded breadth put glom's "Add Python 3.12" sweep at the top with 31 files
changed and not one new assertion. Excision ranks on a continuous focus score
over coverage ratio, breadth, body size and contract presence.

**Validated.** 28 candidates attempted, **14 eligible**. Rejections: 2 for
collateral breakage, 6 whose designated tests changed no verdict, 6 where the
only tests changing verdict could not be collected before the change.

That last class is worth explaining. A feature's new test file imports the symbol
the feature adds, so before the change it cannot even be imported — pytest
reports a collection error and no test body runs. The brief excludes import
errors. Both readings were implemented and measured: the permissive one yields
**zero** additional tasks, because pytest cannot report a per-test verdict for a
test it never collected. The strict reading is therefore the default on
measurement, not on preference.

**Selected.** 10 of 14, greedily maximising score minus a penalty for reusing a
module. The penalty is not optional: on this pool a naive top-ten returns almost
entirely `glom.core` and fails the four-module floor silently. Result: **6
history + 4 excision, 9 distinct modules, difficulty 4 easy / 3 medium / 3
hard**, every quota satisfied, no instruction failing the leak check.

Every rejection is recorded with its reason in `candidates.json` and
`validation.json`. A drop with no stated reason is indistinguishable from a bug.

---

## 4. How to run everything

One command, from a clean checkout, with Docker running:

```bash
./run.sh https://github.com/mahmoud/glom
```

Stages run in dependency order — that order is load-bearing, not cosmetic:
`hygiene` reformats the tree `graph` parses, `deps` compiles the lock `container`
builds from, `validate` runs inside the image `container` verified.

Individual stages, in order:

```bash
stress-stack ingest https://github.com/mahmoud/glom
stress-stack hygiene
stress-stack deps
stress-stack graph
stress-stack coverage
stress-stack testgen
stress-stack container
stress-stack enrich
stress-stack index
stress-stack mine
stress-stack validate --history-limit 30 --excision-limit 12
stress-stack select
stress-stack emit
stress-stack bundle --output ../glom-output
```

The container test run, standalone:

```bash
docker build --tag stress-stack/glom:verify --file Dockerfile .
docker run --rm --network=none --read-only --cap-drop=ALL \
  --volume "$PWD:/work:ro" --workdir /work \
  stress-stack/glom:verify python -m pytest -q
```

Re-running one task's verifier, exactly as validation did:

```bash
cp -R output/tasks/<task_id>/input /tmp/attempt
cp -R output/tasks/<task_id>/verifier/. /tmp/attempt/
cd /tmp/attempt && bash /path/to/output/tasks/<task_id>/verifier/run.sh
```

Model calls are content-addressed and replayed from `.stress_stack/cache/llm`,
so a re-run reproduces byte-identically and spends nothing. That cache is also
the `transcripts/` deliverable.

---

## 5. Scale: what breaks at 100 repositories

**Wall-clock, immediately.** One repository takes minutes, dominated by
validation: seven container runs per candidate, executed serially. A hundred
repositories is days of serial work and there is no concurrency at any level.
Candidates are independent and each run is already capped at two CPUs, so a
process pool is a near-linear win. Two cheaper wins come first: a run cache keyed
on `(tree hash, image id, targets, policy)` — the same content-addressing already
used for model calls — collapses the excision case, where `solution/` is the
identical tree for every candidate; and stopping once a surplus exists rather
than validating the whole ranked pool.

**State, next.** Everything lives inside each cloned repository under
`.stress_stack/`, so there is no way to ask a fleet-wide question — which
repositories failed containerization, and why — and no cache is shared between
them. The SQLite schema in `index.py` generalises to Postgres almost unchanged
and already has the right tables.

**Disk, then.** Every attempted candidate keeps a full `input/` and `solution/`
tree. At a hundred repositories that is thousands of repository copies, plus a
Docker image per repository built into the local daemon and never collected.

**The GitHub API**, unauthenticated at sixty requests an hour shared across the
fleet. The client already records rate-limit state per fetch, so a token pool
with backoff is a small change.

**What I would build differently.** A two-level work queue — one job per
repository, one per candidate — so concurrency is a deployment parameter rather
than a code change. Artifacts to content-addressed object storage; image builds
as their own cached step keyed on lock hash plus base digest, pushed to a shared
registry. And a per-repository validation budget whose exhaustion is recorded in
the funnel as a legitimate outcome: at fleet scale a partial repository that says
so honestly is the common result, and the design has to treat it as first-class
rather than as failure.

---

## 6. Honest gaps

**Scope: Python repositories with a pytest suite.** This is a decision, not an
oversight. Every Pipeline 1 output binds to a language toolchain — dependency
resolution, the container's interpreter, test generation, lint — and so do the
symbol graph, coverage attribution and mutation checks. Supporting a second
language means a parallel parser and runner, not a parallel pipeline: the stage
boundaries are already language-neutral, and `graph`, `coverage` and `testgen`
are the only three modules that would need a sibling implementation. Generality
was spent instead on *repository* shape within Python, which is where the brief
places its warning — proven on three repositories with three different layouts
(flat, `src/`, and a single-module historical form) and two different ways of
declaring test dependencies.

**Net-new tasks are de-scoped, deliberately.** The brief caps them at three of
ten; history has no ceiling, so 6 + 4 satisfies every stated constraint. The
phrase "a capability the repo lacks" reads either as genuinely absent or as
adjacent-but-missing, and the reading changes what gets built. This is recorded
as a decision, not an omission — `net_new: 0 of up to 3`.

**The alternative-implementation gate is real but shallow.** It renames the
private symbols a change introduced and requires the verifier to still pass,
which proves the verifier is not pinned to internal names. It does not prove that
a genuinely different algorithm would pass. A stronger version would generate an
alternative implementation and run the verifier against it.

**Generated tests are gated on body-removal mutation only.** A test must fail
when the target's body is replaced by a raising stub. That proves the test
reaches and depends on the function; it does not prove the assertion is tight.
Operator and boundary mutations would measure assertion strength.

**Historical tasks are bounded by the container's interpreter.** The image is
built at HEAD's declared Python version, so commits from 2018 may not run under
it. `pass_after` catches this correctly and expensively; recency ranking spends
the budget where it is likely to pay. Per-era images would fix it properly.

**Hygiene and coverage still execute repository code on the host.** `sandbox.py`
argues correctly that running a repository's suite is arbitrary code execution
and belongs in a container, and validation obeys that. The three stages that run
first do not. This is an acknowledged inconsistency, not an oversight.

**The knowledge index is under-consumed.** `index.sqlite` is built every run and
its `test_modules` view answers the diversity question, but selection still counts
modules from the in-memory graph. The `shared_tests` query would answer something
currently unaskable — whether two "diverse" tasks share verifier tests — and is
not yet wired in.

**A repository the test runner depends on cannot be verified this way.**
Validation puts the task tree on `PYTHONPATH` so the mounted code wins over the
installed copy. When the repository under test *is* a dependency of pytest —
`pluggy`, `iniconfig`, `packaging` — that shadows the copy pytest is itself
running on, and a historical version will not satisfy a modern pytest. Running
`pytest-dev/pluggy` end to end, 16 of 30 history candidates failed exactly here:
pre-`src`-layout commits ship a flat `pluggy.py` at the root, pytest imports it
instead of its own, and cannot start. The funnel now names this
`runner_dependency_shadowed` rather than reporting a generic infrastructure
failure, so the cause is visible rather than mysterious. Fixing it properly
means running the verifier under an interpreter whose runner dependencies come
from a separate, unshadowed path — a real change to the execution model, and
deliberately not attempted at this stage. pluggy consequently yields 7 eligible
tasks rather than 10, and that shortfall is reported rather than papered over.

A related fix did land: setuptools-scm writes `_version.py` at build time and
git never contains it, so a package whose `__init__` imports from it cannot be
imported from any materialised tree. That version module is now synthesised into
both sides from `git describe` at the task's own commit, which repaired every
modern pluggy tree and affects the large fraction of Python projects using that
build backend.

**Second-repository findings.** Running `pallets/click` end to end found four
bugs invisible on glom, each of which would have produced confident wrong output
on a held-out repository: a container built without a test runner because click
declares test dependencies under PEP 735 `[dependency-groups]` rather than as an
extra; runs that collected zero tests being accepted as verdicts, which reported
"no candidate has a behavioural test change" when the suite had never run; a
`src/` layout where `PYTHONPATH=/work` resolved imports to the copy baked into
the image rather than the mounted task tree, so **every verdict measured frozen
code**; and an unquoted `pytest>=8,<9` in a generated `RUN` line. All four are
fixed. The lesson is recorded deliberately: none was visible to the unit suite,
and all four required running the pipeline cold against a repository that did
not share the sample's habits.
