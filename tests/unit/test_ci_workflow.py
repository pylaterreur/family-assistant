"""Exercise the required CI check with failed prerequisites and skipped tests."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "prerequisite",
    [
        "prepare",
        "detect-changes",
        "build-ci-container",
        "build-main-container",
        "build-devcontainer",
    ],
)
@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_required_check_rejects_failed_prerequisite(
    prerequisite: str, result: str
) -> None:
    """A failed build must fail the check even when every test is skipped."""
    assert _run_required_check({prerequisite: result}) == 1


def test_required_check_allows_intentionally_skipped_jobs() -> None:
    """Documentation changes may skip all builds and tests."""
    assert _run_required_check({"prepare": "success", "detect-changes": "success"}) == 0


def test_required_check_accepts_successful_jobs() -> None:
    """Passing prerequisites and tests allow merging."""
    workflow = yaml.safe_load(
        Path(".github/workflows/ci-with-devcontainer.yml").read_text(encoding="utf-8")
    )
    results = {name: "success" for name in workflow["jobs"] if name != "all-tests"}
    assert _run_required_check(results) == 0


def _run_required_check(results: dict[str, str]) -> int:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci-with-devcontainer.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["all-tests"]
    assert job["if"] == "always()"
    script = re.sub(
        r"\$\{\{ needs\.([\w-]+)\.result }}",
        lambda match: results.get(match[1], "skipped"),
        job["steps"][0]["run"],
    )
    completed = subprocess.run(
        ["bash", "-eu", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode in {0, 1}, completed.stderr
    return completed.returncode
