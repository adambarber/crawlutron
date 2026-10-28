"""The parent process must not pay markitdown's import cost on cold start (#484).

The #6693 readiness handshake moved the *worker's* import into the acquisition
budget, but ``app.extraction.doc_pool`` still imported ``MarkItDown`` at module
level -- so the parent pays the full import on first touch of the pool module,
even when no document is ever extracted. The child (which converts) imports it
inside ``doc_worker_main`` before the ready signal, inside the acquisition
budget.
"""

import subprocess
import sys


def test_importing_the_doc_pool_does_not_import_markitdown_in_the_parent():
    """A fresh interpreter importing the pool module carries no markitdown."""
    probe = "import sys\nimport app.extraction.doc_pool\nprint('markitdown' in sys.modules)\n"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", (
        "importing app.extraction.doc_pool pulled markitdown into the parent "
        "process: the cold-start import cost must live in the worker "
        "(doc_worker_main warms it before the ready signal), not the parent"
    )
