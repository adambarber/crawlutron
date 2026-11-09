"""Warning-zero ratchet (#643): the battery prints no warning, ignores none."""

import configparser
from pathlib import Path

from tests.chokepoint_walks import REPO_ROOT

_INI = Path("pytest.ini")


def test_the_warning_filter_is_error_only_and_stays_that_way():
    """The ratchet: no `ignore::` (or any non-error) row may enter the filter.

    A filterwarnings ignore is a suppressor in disguise; the zero is reached by
    eliminating classes structurally, and this gate keeps it reached."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / _INI)
    rows = [line.strip() for line in parser["pytest"]["filterwarnings"].splitlines() if line.strip()]
    assert rows == ["error"], f"the warning filter drifted from the bare ratchet: {rows}"
