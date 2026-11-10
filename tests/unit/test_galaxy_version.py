"""Default Galaxy version (issue #16) and how the prebuilt client is installed for it.

Facts about Galaxy release_26.1 these tests rely on:
- The checkout has no root ``package.json`` any more: the prebuilt client is the
  ``galaxy-web-client`` wheel from PyPI (``scripts/common_startup.sh``, ``make install-client``).
  The galaxyproject.galaxy role (0.12.10 up to 0.12.16) still runs ``pnpm install`` +
  ``pnpm run stage`` there for any Galaxy >= 26.0, which fails on 26.1.
- A ``release_*`` branch tip is an untagged ``.devN`` version, for which no wheel is
  published; ``common_startup.sh`` then installs the latest release below it.
"""

import pytest
import yaml

from filter_plugins.src_galaxy import galaxy_web_client_requirement
from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars

VARS_FILES = ["src_galaxy_vars.yml", "galaxy_vars.yml"]


def _evaluate(name, **component_params):
    return resolve_vars(VARS_FILES, component_params)(name)


@pytest.mark.parametrize("value", [None, ""])
def test_default_galaxy_is_release_26_1(value):
    params = {} if value is None else {"src_galaxy_version": value}

    assert _evaluate("galaxy_commit_id", **params) == "release_26.1"
    assert _evaluate("_galaxy_web_client_wheel", **params) is True
    assert _evaluate("galaxy_build_client", **params) is False


def test_galaxy_26_0_can_still_be_pinned_and_uses_the_role_client_install():
    assert _evaluate("galaxy_commit_id", src_galaxy_version="26.0") == "release_26.0"
    assert _evaluate("_galaxy_web_client_wheel", src_galaxy_version="26.0") is False
    assert _evaluate("galaxy_build_client", src_galaxy_version="26.0") is True


@pytest.mark.parametrize("version", ["26.1", "26.2", "27.0"])
def test_galaxy_26_1_and_later_install_the_web_client_wheel(version):
    assert _evaluate("_galaxy_web_client_wheel", src_galaxy_version=version) is True
    assert _evaluate("galaxy_build_client", src_galaxy_version=version) is False


@pytest.mark.parametrize(
    ("version", "requirement"),
    [
        ("26.1.2", "galaxy-web-client==26.1.2"),
        ("26.1", "galaxy-web-client==26.1"),
        (" 26.1.2\n", "galaxy-web-client==26.1.2"),
        ("26.1.3.dev0", "galaxy-web-client<26.1.3"),
        ("26.2.0rc1", "galaxy-web-client<26.2.0"),
        ("26.2.0.dev1", "galaxy-web-client<26.2.0"),
    ],
)
def test_web_client_requirement_matches_common_startup(version, requirement):
    assert galaxy_web_client_requirement(version) == requirement


def test_web_client_tasks_link_the_wheel_dist_into_static():
    tasks = yaml.safe_load((REPO_ROOT / "tasks" / "web_client.yml").read_text())
    modules = [next(key for key in task if key.startswith("ansible.builtin.")) for task in tasks]
    args = [task[module] for task, module in zip(tasks, modules, strict=True)]

    pip = args[modules.index("ansible.builtin.pip")]
    assert pip["name"] == "{{ _galaxy_checkout_version.stdout | galaxy_web_client_requirement }}"
    assert pip["virtualenv"] == "{{ galaxy_venv_dir }}"
    link = next(arg for arg in args if isinstance(arg, dict) and arg.get("state") == "link")
    assert link["path"] == "{{ galaxy_server_dir }}/static/dist"
    assert link["src"] == "{{ _galaxy_web_client_dist.stdout }}"


def test_playbook_installs_the_web_client_only_for_the_wheel_versions():
    play = yaml.safe_load((REPO_ROOT / "galaxysrv.yml").read_text())[0]
    include = next(
        task
        for task in play["tasks"]
        if task.get("ansible.builtin.include_tasks") == "tasks/web_client.yml"
    )

    assert include["when"] == "_galaxy_web_client_wheel"
