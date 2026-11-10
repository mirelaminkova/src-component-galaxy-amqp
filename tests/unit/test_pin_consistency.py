"""Tool pins must not drift between requirements-dev.txt, the pre-commit hooks and molecule.

Regression test for #24: requirements-dev.txt moved to ansible-core ~=2.22.0 while the local
pytest hook kept ansible-core==2.21.6 (and the ansible-lint hook resolved ansible-core unpinned),
so `pre-commit run` tested a different ansible-core than CI's pytest job.
"""

import re
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[2]
ANSIBLE_CORE = "ansible-core"
# Hooks that run Ansible (directly or through ansible-lint) must pin ansible-core.
ANSIBLE_PACKAGES = {ANSIBLE_CORE, "ansible-lint"}


def _requirements(path: Path) -> dict[str, Requirement]:
    reqs = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            reqs[canonicalize_name(req.name)] = req
    return reqs


def _dev_pins() -> dict[str, str]:
    """requirements-dev.txt as {package: version}; every entry is a `~=X.Y.Z` pin."""
    pins = {}
    for name, req in _requirements(REPO / "requirements-dev.txt").items():
        (spec,) = req.specifier
        assert spec.operator == "~=", f"requirements-dev.txt: pin {req} with ~="
        pins[name] = spec.version
    return pins


def _local_hooks() -> list[dict]:
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    return [hook for repo in config["repos"] if repo["repo"] == "local" for hook in repo["hooks"]]


def _hook_pins(hook: dict) -> dict[str, str]:
    pins = {}
    for dep in hook.get("additional_dependencies", []):
        req = Requirement(dep)
        (spec,) = req.specifier
        assert spec.operator == "==", f"hook {hook['id']}: pin {dep} with =="
        pins[canonicalize_name(req.name)] = spec.version
    return pins


@pytest.mark.parametrize("hook", _local_hooks(), ids=lambda hook: hook["id"])
def test_local_hook_pins_match_requirements_dev(hook):
    dev = _dev_pins()
    drift = {
        name: (version, dev[name])
        for name, version in _hook_pins(hook).items()
        if name in dev and dev[name] != version
    }
    assert not drift, f"hook {hook['id']}: {{package: (hook ==, requirements-dev ~=)}} {drift}"


@pytest.mark.parametrize("hook", _local_hooks(), ids=lambda hook: hook["id"])
def test_ansible_hooks_pin_ansible_core(hook):
    pins = _hook_pins(hook)
    if ANSIBLE_PACKAGES & set(pins):
        assert ANSIBLE_CORE in pins, (
            f"hook {hook['id']} runs Ansible but leaves ansible-core unpinned; "
            f"add {ANSIBLE_CORE}=={_dev_pins()[ANSIBLE_CORE]}"
        )


def test_molecule_allows_dev_ansible_core():
    molecule = _requirements(REPO / "molecule" / "requirements.txt")[ANSIBLE_CORE]
    assert molecule.specifier.contains(_dev_pins()[ANSIBLE_CORE])


def test_ansible_core_2_21_ci_job_overrides_requirements_dev():
    """The 2.21 compatibility job swaps exactly the ansible-core line of requirements-dev.txt."""
    checks = (REPO / ".github" / "workflows" / "checks.yml").read_text()
    assert re.search(r"grep -v '\^ansible-core' requirements-dev\.txt", checks)
    assert "'ansible-core~=2.21.6'" in checks
