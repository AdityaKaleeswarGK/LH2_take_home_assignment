#!/usr/bin/env bash
# Copy input/ elsewhere, lay these files over it, and run this from
# the tree root. These ids must fail before the change, pass after.
set -euo pipefail
python -m pytest -p no:cacheprovider --continue-on-collection-errors -q glom/test/test_error.py::test_all_public_errors glom/test/test_mutation.py::test_bad_delete_target glom/test/test_mutation.py::test_delete glom/test/test_mutation.py::test_sequence_delete glom/test/test_mutation.py::test_star_broadcast glom/test/test_mutation.py::test_unregistered_delete
