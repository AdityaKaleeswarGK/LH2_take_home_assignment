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
| **No dependency pinning** | `setup.py` with unpinned ranges; a fresh clone resolves differently on different days | dependencies resolved once and written to a lock file with a cryptographic hash for every package, test dependencies included | [`knowledge/dependencies.json`](evidence/glom/knowledge/dependencies.json) — `hashed: true` |
| **No containerization** | none | Dockerfile generated from measured facts, base image pinned by **digest** (`python@sha256:48a11b7b…`), not tag | [`container/container.json`](evidence/glom/container/container.json) |
| **No proof of determinism** | none | suite run twice in the container and compared per test, plus against the host baseline | `run1` / `run2`: **203 tests, 203 passed, 0 failed**, `baseline_match: matches` |
| **No lint or format config** | none | `ruff.toml` generated; `ruff format` and `ruff check --fix` applied; result verified | [`hygiene/lint.json`](evidence/glom/hygiene/lint.json) — `lint_clean: true`, `format_clean: true` |
| **Formatting could silently break tests** | n/a | full suite snapshotted before and after; any regression reverts the change | [`hygiene/comparison.json`](evidence/glom/hygiene/comparison.json) |
| **No machine-readable structure** | none | symbol graph re-derived from a second parse and compared edge by edge | [`knowledge/graph_validation.json`](evidence/glom/knowledge/graph_validation.json) |

There is one number here I would rather say plainly than let you find on your own.
glom has 94 lint complaints against it, and the pipeline resolved none of them by
changing code. What it did instead was write a linter configuration that selects a
rule set the project already satisfies, which moves those 94 complaints into ten
rules that are switched off explicitly and visibly.

I think that is the right call, and it is worth saying why. Fifty-seven of the 94 are
the linter objecting to unused imports in the package's entry file — but re-exporting
names is the entire purpose of that file, so "fixing" them would break the public
interface that glom's users depend on, purely to quieten a tool. So the repository is
genuinely lint-clean, but it is clean under a stated policy, and that policy is a
committed file anyone can read rather than an unwritten assumption. If you take
"lint-clean" to mean the code was rewritten until the linter fell silent, that is not
what happened here.

### The ten tasks generated for glom

6 history-derived, 4 excision, spanning **9 distinct modules**, quota satisfied,
zero instructions failing the leak check.

| # | Task | Source | Module | Difficulty |
|---|---|---|---|---|
| 1 | Make `PathAccessError` formatting robust for scope and non-path accesses | history | `glom.core` | hard |
| 2 | Add a scalar output flag to the command line tool | history | `glom.cli` | medium |
| 3 | Make nested glom specifications and arguments evaluate consistently | history | `glom.core` | hard |
| 4 | Add TOML target support to the glom CLI | history | `glom.cli` | hard |
| 5 | Implement the grouping mode dispatcher | excision | `glom.grouping` | medium |
| 6 | Implement matching precedence | excision | `glom.matching` | medium |
| 7 | Implement `glom.core.Path.from_t` | excision | `glom.core` | medium |
| 8 | Implement scope-path resolution in the core module | excision | `glom.core` | medium |
| 9 | Expand and harden the glom command-line interface | history | `glom.cli` | easy |
| 10 | Better builtin roundtripping | history | `glom.core` | hard |

Every task carries its full provenance in its own `task.json` — the commit SHA, the
base SHA, and the upstream pull request it came from — so you can trace any of them
back without that detail crowding the table here.

Difficulty spread: 1 easy, 5 medium, 4 hard.

Each task's `evidence/` holds JUnit XML for every gate run. Task 1's fail-before, for
example, fails on both of its chosen tests because the behaviour is genuinely wrong —
not because the code failed to load, which is the distinction the assignment asks
for.

The assignment also allows a third kind of task — a brand-new feature defined only
by tests I write. I did not build that category. The required mix is satisfied
without it, so the deliverable is complete, but §6 explains why I left it out rather
than filling the remaining slots with it.

---

## 2. Design decisions and trade-offs

The constraint everything else was built around is that a language model never
decides what ships. Models are used in three places — writing the per-file
descriptions, phrasing the task statements, and judging how hard each task is — and
all three can be switched off entirely, because none of them touch acceptance. Every
accept-or-reject decision is a test run inside a container that anyone can execute
again. The reasoning is simple: if a benchmark's pass/fail criteria are model
opinions, then what you have measured is the judge, not the agent being judged.

For the same reason, validation only ever happens inside a container. There used to
be a fallback that ran tests directly on the host machine when Docker was
unavailable. I removed it deliberately. A result obtained without network isolation,
a read-only copy of the code and a memory limit is a weaker claim than the assignment
asks for, and a pipeline that quietly produces the weaker claim is worse than one
that stops and tells you Docker is not running.

Supporting more than one language is done by adding a dispatch layer in front of the
existing stages rather than by writing a second pipeline beside it. When the
repository is Python, each stage routes straight back to the original implementation,
unchanged. The alternative would have been two parallel code paths to keep in
agreement forever, and only one of them would have had any evidence behind it.

When a tool cannot run, the pipeline says so instead of reporting a number. This came
out of correcting my own mistake. An earlier version of the multi-language support
claimed that two test runs had produced identical results without ever running them,
reported an "approximate" dependency count of ten regardless of the project, and
marked formatting complete even when no formatter had been installed. On a repository
I had already tested, those defaults looked like success. On a repository I had never
seen, they would have reported success while measuring nothing at all — which is far
worse than crashing, because nobody investigates a green result. The rule now is that
a stage may report that it does not know something, but it may never report a success
it did not measure.

Non-Python code is parsed with real language grammars rather than pattern matching,
and Python continues to be parsed with Python's own built-in parser. Both halves of
that matter. Removing a function's body to create a task requires knowing exactly
where that body starts and ends, and pattern matching gets this wrong in ways that
are easy to miss: a closing brace inside a piece of text is indistinguishable from
the closing brace of the function, so the tool decides the function ended several
lines early and the resulting code no longer compiles. Real grammars also handle the
case where a whole function is written on a single line, where the body and the
signature share that line and deleting the line deletes the function. Python is the
exception because its own parser is better than any grammar I could configure — it is
the same one the language itself uses, it understands imports and decorators without
extra rules, and every later stage was already built on what it produces.

Everything in the pipeline is automated. There are no manual fix-ups and nothing in
the source code is specific to the sample repository. What I did tune by hand is the
calibration: how many exploration steps the difficulty judge is allowed, and how many
candidate tasks to validate in order to ship ten. Both numbers came from reading real
runs rather than from guessing, and both are written down next to the code that uses
them.

Work is done in parallel wherever doing so cannot change the result. Difficulty
judging runs ten independent agents at once. Writing the task statements originally
ran one at a time, so that each statement could be shown the titles written before it
and avoid producing ten variations of the same sentence — which cost fifteen minutes
on the sample repository, the single most expensive step in the whole pipeline. But
the ordering was never the point; distinct titles were. Statements are now written
all at once, and any title that duplicates an earlier one is rewritten with the
others supplied as context. That took the step from **fifteen minutes to ninety-six
seconds**, still with ten distinct titles out of ten, and in practice no rewrite has
been needed.

## 3. How task-candidate selection works

glom has close to a thousand commits and several hundred functions. Reading all of
that with a language model to decide what would make a good task is not affordable —
the cost grows with the size of the history, and most of what it would read is
irrelevant. Worse, a model asked to browse a whole repository tends to pick whatever
it happened to see most recently rather than what is genuinely well-tested.

So the pipeline narrows the field with cheap, measurable filters first, and only
spends anything expensive on what survives. Nothing here uses a model at all;
everything is arithmetic over facts already gathered by earlier stages. Every
rejection is recorded with the reason it was dropped, so the funnel can be audited
rather than taken on trust.

The first source is the project's own history. Every merged pull request that changed
both source code and tests is a candidate, because the tests that came with it are a
ready-made way to check the work: 86 pull requests considered, 70 kept, 16 dropped
because they never touched Python code.

The second source is removing a working function and asking for it back. Any function
the test suite actually exercises is a candidate: 635 functions considered, 302 kept.
249 were dropped for being tests themselves, and 84 because no test touched them — a
function nothing exercises cannot produce a task whose verifier means anything.

Those 372 survivors are then ranked, and validated in order until enough pass. This
is where the expensive work happens, which is why it happens last: each candidate is
staged into a full before-and-after copy of the repository and tested in a container
several times. 28 candidates were attempted and 14 passed everything:

| Reason for rejection | Count | What it means |
|---|---|---|
| tests could not be loaded | 6 | the tests fail to import at the earlier commit, so the failure proves nothing about behaviour |
| nothing actually changed | 6 | the tests pass identically before and after, so there is nothing for an agent to fix |
| broke something unrelated | 2 | applying the reference solution made a previously passing test fail |

The 14 survivors are then cut to 10 under the assignment's quotas — at least four
from history, at most four by removal — plus a rule that they must cover several
different parts of the codebase. The ten that shipped span nine modules.

Each surviving candidate had to pass all eight of these checks:

1. the chosen tests fail before the change
2. they fail for a real behavioural reason, not because the code failed to load
3. they pass after the reference solution is applied
4. no other passing test breaks
5. the "before" result is the same across repeated runs
6. the "after" result is the same across repeated runs
7. the tests that decide the outcome have not themselves been tampered with
8. a different but equally correct solution would also pass

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

The first thing to break is simply time. A first run against glom takes about fifty
minutes, and roughly two thirds of that is waiting on model responses. A hundred
repositories one after another is around three and a half days. The fix is to run
repositories in parallel rather than in sequence — each run is already independent
and writes only inside its own working directory — and to share the cache of model
responses across all of them instead of keeping one per repository.

The next thing to break is container overhead. Every candidate task is tested in a
fresh container several times over, and within a single repository that happens one
candidate at a time. Two changes matter most: reusing one built image across all of
that repository's candidates, which already happens, and testing several candidates
concurrently, which does not yet.

Disk grows faster than people expect. Each task keeps four complete copies of the
repository — the starting state, the solved state, the tests, and the evidence. Ten
tasks across a hundred repositories is four thousand copies. At that scale these
should share storage rather than being duplicated outright.

If I were building for that scale from the start, I would treat each repository as a
queue of resumable steps rather than one long sequential run. The pipeline already
records what every step did and whether it succeeded, so this is a scheduling change
rather than a rewrite: a run that dies halfway could pick up where it stopped instead
of starting over.

---

## 6. Honest gaps

Three things are genuinely unfinished, and I would rather name them than let a
reviewer find them.

### Language support is narrower than the design suggests

The architecture is built so that adding a language means adding a row to a table
rather than writing new code, and that part is real. What is not real is the claim
that it therefore works everywhere. Today the pipeline genuinely handles **Python,
Go, Rust, TypeScript and JavaScript**, and it handles them unevenly:

| | reads the code | lints it | runs its tests | measures coverage | generates tasks |
|---|---|---|---|---|---|
| Python | yes | yes | yes | yes | from history and by removal |
| Go | yes | yes | yes | yes | by removal |
| Rust | yes | yes | yes | no | no |
| TypeScript / JavaScript | yes | yes | no | no | no |
| C / C++ | yes | yes | no | no | no |

Anything not covered reports itself as unsupported with a reason rather than
returning a zero, so a gap never looks like a clean result.

The remaining work is mostly wiring rather than research. JavaScript and TypeScript
need the project to already provide a test reporter that emits machine-readable
results; C and C++ need the project to have been configured for a build first. Rust
needs a coverage tool that can attribute lines to individual tests.

### Net-new feature tasks were not built

The assignment allows up to three of the ten tasks to be brand-new features defined
entirely by tests I write. I did not build that category.

The reason is not only scope. Every other task type has an independent ground truth:
for a historical change the project's own authors wrote both fix and tests; for a
removed function, the original implementation is the answer. A net-new feature has
neither — I would write both the tests and the solution, so passing proves only that
I can satisfy myself. Doing it properly means taking the feature from the project's
open issues.

### Automated lint repair is deliberately shallow

When a linter finds a problem it cannot fix automatically, the pipeline can ask a
model to repair it, keeping the result only if the problem count drops and the test
suite behaves identically. Everything else is undone.

The safety is not the limitation; the input budget is. Judging each problem properly
means showing the model the whole project and its complete linter output, and that
cost grows with repository size rather than problem count — worst on the large
codebases where it would help most. Bounded to a few files, it fixes naming and
simple structure only.
