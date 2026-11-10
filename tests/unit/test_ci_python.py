"""CI must run the requirements-dev.txt tooling on a Python that the pinned ansible-core supports.

Regression test for #24: ansible-core 2.22 requires Python >= 3.13, but checks.yml ran
pre-commit and pytest on Python 3.12, so `pip install -r requirements-dev.txt` failed.
"""

from importlib.metadata import metadata
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet

REPO = Path(__file__).resolve().parents[2]
CHECKS_WORKFLOW = REPO / ".github" / "workflows" / "checks.yml"
DEV_REQUIREMENTS = "-r requirements-dev.txt"


def _setup_python_version(job: dict) -> str:
    for step in job["steps"]:
        if step.get("uses", "").startswith("actions/setup-python@"):
            with_ = step["with"]
            if "python-version-file" in with_:
                return (REPO / with_["python-version-file"]).read_text().strip()
            return str(with_["python-version"])
    raise AssertionError("job installs requirements-dev.txt without actions/setup-python")


def _dev_tooling_jobs() -> dict[str, str]:
    """Jobs that install requirements-dev.txt as is, mapped to their Python version."""
    jobs = yaml.safe_load(CHECKS_WORKFLOW.read_text())["jobs"]
    return {
        name: _setup_python_version(job)
        for name, job in jobs.items()
        if any(DEV_REQUIREMENTS in step.get("run", "") for step in job["steps"])
    }


def test_checks_workflow_installs_dev_requirements():
    assert set(_dev_tooling_jobs()) >= {"pre-commit", "pytest"}


@pytest.mark.parametrize(("job", "python"), sorted(_dev_tooling_jobs().items()))
def test_ci_python_supports_pinned_ansible_core(job, python):
    requires_python = SpecifierSet(metadata("ansible-core")["Requires-Python"])
    assert requires_python.contains(python), (
        f"checks.yml job {job!r} runs Python {python}, but the installed ansible-core "
        f"requires Python {requires_python}"
    )
