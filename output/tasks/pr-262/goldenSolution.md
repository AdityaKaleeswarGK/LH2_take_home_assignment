# Golden solution — pr-262

## Provenance

- **kind**: `commit`
- **commit_sha**: `34315014fe16b4c1e144a1acdf480699bf2303fb`
- **base_sha**: `c97b71a1e4751d227898f2b31e885e001d4818cb`
- **pull_request**: `262`
- **url**: `https://github.com/mahmoud/glom/pull/262`
- **merged_at**: `2023-08-22T23:33:07Z`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 1 designated test(s) do not pass against `input/`. 1 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_cli::test_main_yaml_target`

## Diff

```diff
diff --git a/glom/cli.py b/glom/cli.py
index cd0609e..bd8752a 100644
--- a/glom/cli.py
+++ b/glom/cli.py
@@ -125,7 +125,7 @@ def mw_handle_target(target_text, target_format):
     elif target_format in ('yaml', 'yml'):
         try:
             import yaml
-            load_func = yaml.load
+            load_func = yaml.safe_load
         except ImportError:
             raise UsageError('No YAML package found. To process yaml files, run: pip install PyYAML')
     elif target_format == 'python':
diff --git a/requirements.txt b/requirements.txt
index b3b7f51..f12f4f4 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -5,4 +5,4 @@ face<22.0.0
 pytest==4.6.11;python_version<'3.6'
 pytest>=6.2.5;python_version >= '3.6'
 tox==3.7.0
-PyYAML==5.4.1
+PyYAML==6.0.1
```
