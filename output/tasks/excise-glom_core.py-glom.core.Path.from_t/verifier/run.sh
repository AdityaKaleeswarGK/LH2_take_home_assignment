#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_mutation.py::test_delete glom/test/test_mutation.py::test_s_assign glom/test/test_path_and_t.py::test_from_t_identity
