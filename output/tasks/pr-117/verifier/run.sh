#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_basic.py::test_invoke glom/test/test_check.py::test_check_basic glom/test/test_fill.py::test glom/test/test_mutation.py::test_assign glom/test/test_path_and_t.py::test_path_t_roundtrip glom/test/test_reduction.py::test_flatten glom/test/test_reduction.py::test_fold glom/test/test_reduction.py::test_sum_integers glom/test/test_streaming.py::test_filter
