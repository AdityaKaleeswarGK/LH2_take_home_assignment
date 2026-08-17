#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_match.py::test_double_wrapping glom/test/test_match.py::test_nested_struct glom/test/test_match.py::test_sample
