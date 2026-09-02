#!/usr/bin/env zsh
# Wrapper to run the browser-crawler pytest suite from the project root.
# Mirrors the dev-stack test.sh entry-point so this group is composable
# with the rest of the dev-stack lifecycle helpers (no harness knowledge
# needed at the call site).

set -e -u -o pipefail

HERE="$(cd "$(dirname "${(%):-%N}")" && pwd)"
SUITE_DIR="${HERE}/.."

cd "${SUITE_DIR}"

# Forward optional args (e.g. -k name) so callers can target a single test.
exec python3 -m pytest "${@}"
