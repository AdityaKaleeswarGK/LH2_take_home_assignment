# Golden solution — excise-glom_core.py-glom.core._s_first_magic

## Provenance

- **kind**: `excision_target`
- **symbol_id**: `glom/core.py::glom.core._s_first_magic`
- **qualified_name**: `glom.core._s_first_magic`
- **path**: `glom/core.py`
- **commit_sha**: `30b477ab65560914a38f331614947d0894701044`
- **stub_strategy**: `neutral`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 9 designated test(s) do not pass against `input/`. 9 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_match::test_nested_dict`
- `glom.test.test_path_and_t::test_s_magic`
- `glom.test.test_path_and_t::test_t_subspec`
- `glom.test.test_scope_vars::test_globals`
- `glom.test.test_scope_vars::test_let`
- `glom.test.test_scope_vars::test_max_skip`
- `glom.test.test_scope_vars::test_s_scope_assign`
- `glom.test.test_scope_vars::test_scoped_vars`
- `glom.test.test_scope_vars::test_vars`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -1528,7 +1528,14 @@
     enable S.a to do S['a'] or S['a'].val as a special
     case for accessing user defined string variables
     """
-    return None
+    err = None
+    try:
+        cur = scope[key]
+    except KeyError as e:
+        err = PathAccessError(e, Path(_t), 0)  # always only one level depth, hence 0
+    if err:
+        raise err
+    return cur
 
 
 def _t_eval(target, _t, scope):
```
