#!/usr/bin/env zsh
# Run the browser-crawler pytest suite from the repository root.
# This is the suite's entry point, so a caller needs no harness knowledge:
# forward any arguments (e.g. -k name) straight to pytest.

set -e -u -o pipefail

HERE="$(cd "$(dirname "${(%):-%N}")" && pwd)"
SUITE_DIR="${HERE}/.."

cd "${SUITE_DIR}"

# Forward optional args (e.g. -k name) so callers can target a single test.
exec python3 -m pytest "${@}"
