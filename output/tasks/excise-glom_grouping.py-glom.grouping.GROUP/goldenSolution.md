# Golden solution — excise-glom_grouping.py-glom.grouping.GROUP

## Provenance

- **kind**: `excision_target`
- **symbol_id**: `glom/grouping.py::glom.grouping.GROUP`
- **qualified_name**: `glom.grouping.GROUP`
- **path**: `glom/grouping.py`
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
- `glom.test.test_grouping::test_agg`
- `glom.test.test_grouping::test_bucketing`
- `glom.test.test_grouping::test_corner_cases`
- `glom.test.test_grouping::test_limit`
- `glom.test.test_grouping::test_sample`

## Diff

```diff
--- a/glom/grouping.py
+++ b/glom/grouping.py
@@ -99,7 +99,60 @@
     """
     Group mode dispatcher; also sentinel for current mode = group
     """
-    return None
+    recurse = lambda spec: scope[glom](target, spec, scope)
+    tree = scope[ACC_TREE]  # current accumulator support structure
+    if callable(getattr(spec, "agg", None)):
+        return spec.agg(target, tree)
+    elif callable(spec):
+        return spec(target)
+    _spec_type = type(spec)
+    if _spec_type not in (dict, list):
+        raise BadSpec("Group mode expected dict, list, callable, or"
+                      " aggregator, not: %r" % (spec,))
+    _spec_id = id(spec)
+    try:
+        acc = tree[_spec_id]  # current accumulator
+    except KeyError:
+        acc = tree[_spec_id] = _spec_type()
+    if _spec_type is dict:
+        done = True
+        for keyspec, valspec in spec.items():
+            if tree.get(keyspec, None) is STOP:
+                continue
+            key = recurse(keyspec)
+            if key is SKIP:
+                done = False  # SKIP means we still want more vals
+                continue
+            if key is STOP:
+                tree[keyspec] = STOP
+                continue
+            if key not in acc:
+                # TODO: guard against key == id(spec)
+                tree[key] = {}
+            scope[ACC_TREE] = tree[key]
+            result = recurse(valspec)
+            if result is STOP:
+                tree[keyspec] = STOP
+                continue
+            done = False  # SKIP or returning a value means we still want more vals
+            if result is not SKIP:
+                acc[key] = result
+        if done:
+            return STOP
+        return acc
+    elif _spec_type is list:
+        for valspec in spec:
+            if type(valspec) is dict:
+                # doesn't make sense due to arity mismatch. did you mean [Auto({...})] ?
+                raise BadSpec('dicts within lists are not'
+                              ' allowed while in Group mode: %r' % spec)
+            result = recurse(valspec)
+            if result is STOP:
+                return STOP
+            if result is not SKIP:
+                acc.append(result)
+        return acc
+    raise ValueError(f"{_spec_type} not a valid spec type for Group mode")  # pragma: no cover
 
 
 class First:
```
