#!/usr/bin/env bash
# The documented entry point: one command, a repository URL or path, every stage.
#
#   ./run.sh https://github.com/mahmoud/glom
#   ./run.sh /path/to/local/repo
#
# Requires Docker running (every verifier runs in a container, with no host
# fallback) and Python 3.12+. OpenRouter is required only when uncovered public
# callables need generated tests; semantic enrichment and instruction prose
# otherwise degrade to deterministic source-derived artifacts.
set -euo pipefail

SOURCE="${1:-}"
if [ -z "$SOURCE" ]; then
    echo "usage: ./run.sh <repo_url_or_path> [--output DIR]" >&2
    exit 64
fi
shift || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${WORKDIR:-$(pwd)}"

# Prefer the project's own virtual environment so the entry point works without
# the caller having activated anything.
if [ -x "$HERE/.venv/bin/python" ]; then
    PYTHON="$HERE/.venv/bin/python"
elif command -v stress-stack >/dev/null 2>&1; then
    PYTHON=""
else
    PYTHON="$(command -v python3)"
fi

if ! docker version >/dev/null 2>&1; then
    echo "error: Docker is not reachable. Validation runs only in a container." >&2
    exit 69
fi

cd "$WORKDIR"
if [ -n "$PYTHON" ]; then
    # A clean checkout is a src-layout project and has no committed .venv.
    # Make that source tree importable instead of requiring a prior editable
    # install merely to reach the documented entry point.
    export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
    exec "$PYTHON" -m stress_stack run "$SOURCE" "$@"
fi
exec stress-stack run "$SOURCE" "$@"
