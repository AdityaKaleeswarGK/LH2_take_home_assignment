# Golden solution — excise-glom_core.py-glom.core.Path.from_t

## Provenance

- **kind**: `excision_target`
- **symbol_id**: `glom/core.py::glom.core.Path.from_t`
- **qualified_name**: `glom.core.Path.from_t`
- **path**: `glom/core.py`
- **commit_sha**: `30b477ab65560914a38f331614947d0894701044`
- **stub_strategy**: `neutral`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 3 designated test(s) do not pass against `input/`. 3 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_mutation::test_delete`
- `glom.test.test_mutation::test_s_assign`
- `glom.test.test_path_and_t::test_from_t_identity`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -721,7 +721,12 @@
 
     def from_t(self):
         '''return the same path but starting from T'''
-        return None
+        t_path = self.path_t.__ops__
+        if t_path[0] is S:
+            new_t = TType()
+            new_t.__ops__ = (T,) + t_path[1:]
+            return Path(new_t)
+        return self
 
     def __getitem__(self, i):
         cur_t_path = self.path_t.__ops__
```
