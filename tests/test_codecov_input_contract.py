"""Every Codecov step uses the action's declared snake_case input names."""

from __future__ import annotations

import yaml

from tests.chokepoint_paths import REPO_ROOT


def test_no_workflow_uses_hyphenated_codecov_inputs() -> None:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key, job in document.get("jobs", {}).items():
            for step in job.get("steps", []):
                if "codecov" not in str(step.get("uses", "")):
                    continue
                bad = sorted(name for name in step.get("with", {}) if "-" in name)
                if bad:
                    offenders.append(f"{path.name}:{key}:{step.get('name')}: {bad}")
    assert not offenders, f"codecov action inputs must be snake_case: {offenders}"
