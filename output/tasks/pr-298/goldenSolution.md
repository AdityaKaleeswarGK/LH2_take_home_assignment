# Golden solution — pr-298

## Provenance

- **kind**: `commit`
- **commit_sha**: `e515fb33c7af491c6a20b2618591736361bb1d08`
- **base_sha**: `64141ba4794eaadf63242aa76191448159306251`
- **pull_request**: `298`
- **url**: `https://github.com/mahmoud/glom/pull/298`
- **merged_at**: `2026-06-29T03:18:35Z`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 2 designated test(s) do not pass against `input/`. 2 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_error::test_pae_fallback_for_non_path`
- `glom.test.test_error::test_pae_scope_printable`

## Diff

```diff
diff --git a/.github/workflows/tests.yaml b/.github/workflows/tests.yaml
index 954b57e..dc625f3 100644
--- a/.github/workflows/tests.yaml
+++ b/.github/workflows/tests.yaml
@@ -31,26 +31,19 @@ jobs:
           - { name: "PyPy3", python: "pypy-3.9", os: ubuntu-latest, tox: pypy3 }
     steps:
       - uses: actions/checkout@v4
-      - uses: actions/setup-python@v4
+      - uses: actions/setup-python@v5
         with:
           python-version: ${{ matrix.python }}
+          cache: pip
       - name: update pip
         run: |
           pip install -U wheel
           pip install -U setuptools
           python -m pip install -U pip
-      - name: get pip cache dir
-        id: pip-cache
-        run: echo "::set-output name=dir::$(pip cache dir)"
-      - name: cache pip
-        uses: actions/cache@v3
-        with:
-          path: ${{ steps.pip-cache.outputs.dir }}
-          key: pip|${{ runner.os }}|${{ matrix.python }}|${{ hashFiles('setup.py') }}|${{ hashFiles('requirements/*.txt') }}
       - run: pip install tox
       - run: tox -e ${{ matrix.tox }},coverage-report
       - name: "Upload coverage to Codecov"
-        uses: "codecov/codecov-action@v3"
+        uses: "codecov/codecov-action@v5"
         with:
           fail_ci_if_error: true
           files: ./.tox/coverage.xml
diff --git a/codecov.yml b/codecov.yml
new file mode 100644
index 0000000..e471488
--- /dev/null
+++ b/codecov.yml
@@ -0,0 +1,9 @@
+coverage:
+  status:
+    project:
+      default:
+        target: auto    # must match base commit coverage
+        threshold: 0.5% # tolerate small measurement noise
+    patch:
+      default:
+        target: 100%    # new/changed lines must be covered
diff --git a/glom/core.py b/glom/core.py
index cee71a3..0e7bf2a 100644
--- a/glom/core.py
+++ b/glom/core.py
@@ -345,7 +345,10 @@ class PathAccessError(GlomError, AttributeError, KeyError, IndexError):
         self.part_idx = part_idx
 
     def get_message(self):
-        path_part = Path(self.path).values()[self.part_idx]
+        try:
+            path_part = self.path.values()[self.part_idx]
+        except (AttributeError, IndexError):
+            path_part = self.path
         return ('could not access %r, part %r of %r, got error: %r'
                 % (path_part, self.part_idx, self.path, self.exc))
 
diff --git a/glom/test/test_error.py b/glom/test/test_error.py
index a9334c9..c9fb382 100644
--- a/glom/test/test_error.py
+++ b/glom/test/test_error.py
@@ -5,7 +5,7 @@ import traceback
 
 import pytest
 
-from glom import glom, S, T, GlomError, Switch, Coalesce, Or, Path
+from glom import glom, S, T, GlomError, PathAccessError, Switch, Coalesce, Or, Path
 from glom.core import format_oneline_trace, format_target_spec_trace, bbrepr, ROOT, LAST_CHILD_SCOPE
 from glom.matching import M, MatchError, TypeMatchError, Match
 
@@ -40,6 +40,35 @@ def test_pae_api():
     assert exc_info.value.part_idx == 1
 
 
+def test_pae_scope_printable():
+    # A PathAccessError whose path comes from a Scope/S access must still be
+    # printable: get_message() (and therefore str()) must not raise even
+    # though Path() rejects an S-based path. Regression for #249.
+    with pytest.raises(PathAccessError) as exc_info:
+        glom({}, S['X'], scope={'x': 'y'})
+
+    exc = exc_info.value
+    msg = exc.get_message()
+    assert '<exception str() failed>' not in str(exc)
+    assert "could not access 'X'" in msg
+    assert "part 0 of T['X']" in msg
+    assert "KeyError" in msg
+
+
+def test_pae_fallback_for_non_path():
+    # get_message() should not crash even if .path lacks a .values() method
+    # (e.g. a manually constructed PathAccessError with a plain string path).
+    exc = PathAccessError(KeyError('z'), 'a.b.c', 0)
+    msg = exc.get_message()
+    assert 'a.b.c' in msg
+    assert "KeyError" in msg
+
+    # Also cover IndexError fallback: part_idx beyond path length
+    exc2 = PathAccessError(KeyError('z'), Path('a', 'b'), 99)
+    msg2 = exc2.get_message()
+    assert "Path('a', 'b')" in msg2
+
+
 def test_unfinalized_glomerror_repr():
     assert 'GlomError()' in repr(GlomError())
```
