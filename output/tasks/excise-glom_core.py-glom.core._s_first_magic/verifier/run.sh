#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_match.py::test_nested_dict glom/test/test_path_and_t.py::test_s_magic glom/test/test_path_and_t.py::test_t_subspec glom/test/test_scope_vars.py::test_globals glom/test/test_scope_vars.py::test_let glom/test/test_scope_vars.py::test_max_skip glom/test/test_scope_vars.py::test_s_scope_assign glom/test/test_scope_vars.py::test_scoped_vars glom/test/test_scope_vars.py::test_vars
