# Golden solution — pr-280

## Provenance

- **kind**: `commit`
- **commit_sha**: `24c21dcc44cc81abc193aa3bfa2f66304470d205`
- **base_sha**: `5cef40707d8b4765c621d7a09ad5f35f96bf7d1a`
- **pull_request**: `280`
- **url**: `https://github.com/mahmoud/glom/pull/280`
- **merged_at**: `2024-11-02T22:43:13Z`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 1 designated test(s) do not pass against `input/`. 1 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_cli::test_cli_scalar`

## Diff

```diff
diff --git a/glom/cli.py b/glom/cli.py
index 3f8f744..21488f5 100644
--- a/glom/cli.py
+++ b/glom/cli.py
@@ -43,14 +43,14 @@ from face import (Command,
                   CommandLineError,
                   UsageError)
 from face.utils import isatty
+from boltons.iterutils import is_scalar
 
 import glom
 from glom import Path, GlomError, Inspect
 
-# TODO: --target-format scalar = unquoted if single value, error otherwise, maybe even don't output newline
-# TODO: --default
+# TODO: --default?
 
-def glom_cli(target, spec, indent, debug, inspect):
+def glom_cli(target, spec, indent, debug, inspect, scalar):
     """Command-line interface to the glom library, providing nested data
     access and data restructuring with the power of Python.
     """
@@ -70,7 +70,11 @@ def glom_cli(target, spec, indent, debug, inspect):
 
     if not indent:
         indent = None
-    print(json.dumps(result, indent=indent, sort_keys=True))
+    
+    if scalar and is_scalar(result):
+        print(result, end='')
+    else:
+        print(json.dumps(result, indent=indent, sort_keys=True))
     return
 
 
@@ -86,7 +90,10 @@ def get_command():
 
     cmd.add('--indent', int, missing=2,
             doc='number of spaces to indent the result, 0 to disable pretty-printing')
-
+    
+    cmd.add('--scalar', parse_as=True,
+            doc="if the result is a single value (not a collection), output it"
+            " without quotes or whitespace, for easier usage in scripts")
     cmd.add('--debug', parse_as=True, doc='interactively debug any errors that come up')
     cmd.add('--inspect', parse_as=True, doc='interactively explore the data')
     return cmd
diff --git a/glom/test/test_cli.py b/glom/test/test_cli.py
index 54607a3..bfd78c7 100644
--- a/glom/test/test_cli.py
+++ b/glom/test/test_cli.py
@@ -63,6 +63,14 @@ def test_cli_spec_argv_target_stdin_basic(cc):
     assert res.stdout == BASIC_OUT
 
 
+def test_cli_scalar(cc):
+    res = cc.run(['glom', 'a.b.c', '{"a": {"b": {"c": "d"}}}'])
+    assert res.stdout == '"d"\n'
+
+    res = cc.run(['glom', '--scalar', 'a.b.c', '{"a": {"b": {"c": "d"}}}'])
+    assert res.stdout == 'd'
+
+
 def test_cli_spec_target_files_basic(cc, basic_spec_path, basic_target_path):
     res = cc.run(['glom', '--indent', '0', '--target-file',
                   basic_target_path, '--spec-file', basic_spec_path])
```
