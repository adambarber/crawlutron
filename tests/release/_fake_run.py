"""A subprocess double that records release-script command construction."""

from __future__ import annotations

import subprocess


class FakeRun:
    """A stand-in for _run returning a fixed result and recording calls."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.result = subprocess.CompletedProcess([], returncode, "", stderr)
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        command: list[str],
        *,
        capture: bool = True,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        self.environments.append(env)
        return self.result
