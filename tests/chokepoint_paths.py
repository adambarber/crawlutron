"""The public test-facing name for the one path door in ``utils.paths``."""

import sys

from utils.paths import (
    ADMISSION_ROOT,
    APP_ROOT,
    APP_ROOTS,
    MCP_ROOT,
    REPO_ROOT,
    REST_ROOT,
    SAFE_URL_ROOT,
    SDK_ROOT,
    TESTS_DIR,
    TRANSPORT_DIR,
    freeze_module,
    path_components,
    relative_posix,
)

__all__ = [
    "ADMISSION_ROOT",
    "APP_ROOT",
    "APP_ROOTS",
    "MCP_ROOT",
    "REPO_ROOT",
    "REST_ROOT",
    "SAFE_URL_ROOT",
    "SDK_ROOT",
    "TESTS_DIR",
    "TRANSPORT_DIR",
    "path_components",
    "relative_posix",
]

freeze_module(sys.modules[__name__])
