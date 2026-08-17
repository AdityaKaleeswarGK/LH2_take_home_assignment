# Golden solution — excise-glom_matching.py-glom.matching._precedence

## Provenance

- **kind**: `excision_target`
- **symbol_id**: `glom/matching.py::glom.matching._precedence`
- **qualified_name**: `glom.matching._precedence`
- **path**: `glom/matching.py`
- **commit_sha**: `30b477ab65560914a38f331614947d0894701044`
- **stub_strategy**: `neutral`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 3 designated test(s) do not pass against `input/`. 3 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_match::test_double_wrapping`
- `glom.test.test_match::test_nested_struct`
- `glom.test.test_match::test_sample`

## Diff

```diff
--- a/glom/matching.py
+++ b/glom/matching.py
@@ -661,7 +661,17 @@
     therefore we need a precedence for which order to try
     keys in; higher = later
     """
-    return None
+    if type(match) in (Required, Optional):
+        match = match.key
+    if type(match) in (tuple, frozenset):
+        if not match:
+            return 0
+        return max([_precedence(item) for item in match])
+    if isinstance(match, type):
+        return 2
+    if hasattr(match, "glomit"):
+        return 1
+    return 0  # == match
 
 
 def _handle_dict(target, spec, scope):
```
