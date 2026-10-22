#!/usr/bin/env zsh
# The pytest suite's entry point: forwards all arguments to pytest.

set -e -u -o pipefail

HERE="$(cd "$(dirname "${(%):-%N}")" && pwd)"
SUITE_DIR="${HERE}/.."

cd "${SUITE_DIR}"

# Forward optional args (e.g. -k name) so callers can target a single test.
exec python3 -m pytest "${@}"
