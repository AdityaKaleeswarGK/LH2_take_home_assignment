#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_error.py::test_all_public_errors glom/test/test_grouping.py::test_agg glom/test/test_grouping.py::test_bucketing glom/test/test_grouping.py::test_corner_cases glom/test/test_grouping.py::test_limit glom/test/test_grouping.py::test_sample
