#!/usr/bin/env zsh
# The pytest suite's entry point: forwards all arguments to pytest.

set -e -u -o pipefail

HERE="$(cd "$(dirname "${(%):-%N}")" && pwd)"
SUITE_DIR="${HERE}/.."

cd "${SUITE_DIR}"

# Resolve the same venv-first interpreter the gates use (#268): an explicit
# override, then the service venv, then PATH with a warning that it is unpinned.
if [[ -n "${CRAWLUTRON_PYTHON:-}" ]]; then
    PYTHON="${CRAWLUTRON_PYTHON}"
elif [[ -x .venv/bin/python ]]; then
    PYTHON=".venv/bin/python"
else
    print -u2 "run-tests.sh: no CRAWLUTRON_PYTHON and no .venv/bin/python; using unpinned PATH python3"
    PYTHON="python3"
fi

# Forward optional args (e.g. -k name) so callers can target a single test.
exec "${PYTHON}" -m pytest "${@}"
