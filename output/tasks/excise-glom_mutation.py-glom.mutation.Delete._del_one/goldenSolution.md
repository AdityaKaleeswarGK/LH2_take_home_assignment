# Golden solution — excise-glom_mutation.py-glom.mutation.Delete._del_one

## Provenance

- **kind**: `excision_target`
- **symbol_id**: `glom/mutation.py::glom.mutation.Delete._del_one`
- **qualified_name**: `glom.mutation.Delete._del_one`
- **path**: `glom/mutation.py`
- **commit_sha**: `30b477ab65560914a38f331614947d0894701044`
- **stub_strategy**: `neutral`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 6 designated test(s) do not pass against `input/`. 6 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_error::test_all_public_errors`
- `glom.test.test_mutation::test_bad_delete_target`
- `glom.test.test_mutation::test_delete`
- `glom.test.test_mutation::test_sequence_delete`
- `glom.test.test_mutation::test_star_broadcast`
- `glom.test.test_mutation::test_unregistered_delete`

## Diff

```diff
--- a/glom/mutation.py
+++ b/glom/mutation.py
@@ -289,7 +289,25 @@
         self.ignore_missing = ignore_missing
 
     def _del_one(self, dest, op, arg, scope):
-        return None
+        if op == '[':
+            try:
+                del dest[arg]
+            except IndexError as e:
+                if not self.ignore_missing:
+                    raise PathDeleteError(e, self.path, arg)
+        elif op == '.':
+            try:
+                delattr(dest, arg)
+            except AttributeError as e:
+                if not self.ignore_missing:
+                    raise PathDeleteError(e, self.path, arg)
+        elif op == 'P':
+            _delete = scope[TargetRegistry].get_handler('delete', dest)
+            try:
+                _delete(dest, arg)
+            except Exception as e:
+                if not self.ignore_missing:
+                    raise PathDeleteError(e, self.path, arg)
 
     def glomit(self, target, scope):
         op, arg, path = self.op, self.arg, self.path
```
