"""Modules run with the system Python, which has every module's dependencies (issue #33).

SRC runs ``ansible-playbook -i localhost, --connection=local`` from its own venv, so interpreter
discovery picked ``/etc/src/venv/ansible_env/bin/python3.12``, which ``become_user`` tasks
(postgres, galaxy, gxpriv) cannot execute. The play pins ``/usr/bin/python3``; the Python
libraries modules import on the managed host must then come from apt, before first use.
"""

import re

import pytest
import yaml

from tests.helpers.ansible_vars import REPO_ROOT

PLAYBOOK = REPO_ROOT / "galaxysrv.yml"
TASKS_DIR = REPO_ROOT / "tasks"
SYSTEM_PYTHON = "/usr/bin/python3"

# Python libraries modules import on the managed host -> the apt package providing them for
# the system Python. Keys are modules used in galaxysrv.yml/tasks/ or roles used by the play.
TARGET_DEPENDENCIES = {
    # rabbitmq_user and rabbitmq_vhost import requests (community.rabbitmq 1.7.0).
    "community.rabbitmq.rabbitmq_user": {"python3-requests"},
    "community.rabbitmq.rabbitmq_vhost": {"python3-requests"},
    # The pip module needs packaging (or setuptools' pkg_resources) to parse requirements.
    "ansible.builtin.pip": {"python3-packaging"},
    # community.postgresql modules (postgresql_user/db/privs/membership) need psycopg2.
    "role:galaxyproject.postgresql_objects": {"python3-psycopg2"},
    "community.postgresql.postgresql_query": {"python3-psycopg2"},
    # The role's pip tasks create the venv with `virtualenv -p <module Python>`
    # (no galaxy_virtualenv_command set) and need packaging like any pip task.
    "role:galaxyproject.galaxy": {"python3-virtualenv", "python3-packaging"},
}

# Modules used in galaxysrv.yml and tasks/ that import nothing outside ansible-core and the
# standard library on the managed host (apt: python3-apt ships with Ubuntu, and ansible-core
# installs it itself if missing). A new module must be added here or to TARGET_DEPENDENCIES.
NO_TARGET_DEPENDENCIES = {
    "ansible.builtin.apt",
    "ansible.builtin.apt_repository",
    "ansible.builtin.assert",
    "ansible.builtin.command",
    "ansible.builtin.copy",
    "ansible.builtin.file",
    "ansible.builtin.find",
    "ansible.builtin.get_url",
    "ansible.builtin.git",
    "ansible.builtin.group",
    "ansible.builtin.include_role",
    "ansible.builtin.include_tasks",
    "ansible.builtin.include_vars",
    "ansible.builtin.known_hosts",
    "ansible.builtin.lineinfile",
    "ansible.builtin.replace",
    "ansible.builtin.service",
    "ansible.builtin.service_facts",
    "ansible.builtin.set_fact",
    "ansible.builtin.slurp",
    "ansible.builtin.stat",
    "ansible.builtin.systemd_service",
    "ansible.builtin.template",
    "ansible.builtin.uri",
    "ansible.builtin.user",
    "community.rabbitmq.rabbitmq_plugin",
}

MODULE_KEY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")


def _load(path):
    return yaml.safe_load(path.read_text())


def _play():
    (play,) = _load(PLAYBOOK)
    return play


def _module(task):
    keys = [key for key in task if MODULE_KEY.match(key)]
    assert len(keys) <= 1, task
    return keys[0] if keys else None


def _include_file(task, base):
    """Like Ansible: relative to the including task file, else to the playbook."""
    arg = task["ansible.builtin.include_tasks"]
    name = arg["file"] if isinstance(arg, dict) else arg
    nested = base / name
    return nested if base != REPO_ROOT and nested.exists() else REPO_ROOT / name


def _walk(tasks, base=REPO_ROOT):
    """Yield (step, task) in file order: modules, roles and included task files expanded."""
    for task in tasks:
        module = _module(task)
        if module == "ansible.builtin.include_tasks":
            path = _include_file(task, base)
            yield from _walk(_load(path), path.parent)
        elif module == "ansible.builtin.include_role":
            yield f"role:{task[module]['name']}", task
        elif module:
            yield module, task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []), base)


def _steps():
    """The play's steps in execution order: pre_tasks, roles, tasks, post_tasks."""
    play = _play()
    yield from _walk(play["pre_tasks"])
    for role in play["roles"]:
        yield f"role:{role['role']}", role
    yield from _walk(play["tasks"])
    yield from _walk(play["post_tasks"])


def _apt_packages(step, task):
    if step != "ansible.builtin.apt" or "name" not in task[step]:
        return set()
    names = task[step]["name"]
    return {names} if isinstance(names, str) else set(names)


def test_play_runs_modules_with_the_system_python():
    assert _play()["vars"]["ansible_python_interpreter"] == SYSTEM_PYTHON


def test_cd_runs_use_the_same_interpreter():
    script = (REPO_ROOT / "templates" / "cd" / "galaxy-cd.sh.j2").read_text()

    assert f"ansible_python_interpreter={SYSTEM_PYTHON}" in script


def test_every_module_dependency_is_installed_before_first_use():
    installed = set()
    missing = []
    for step, task in _steps():
        missing += [(step, pkg) for pkg in TARGET_DEPENDENCIES.get(step, set()) - installed]
        installed |= _apt_packages(step, task)

    assert not missing


@pytest.mark.parametrize("step", sorted(TARGET_DEPENDENCIES))
def test_every_listed_dependency_is_still_used(step):
    assert step in {name for name, _ in _steps()}


def test_every_module_used_is_audited():
    modules = {step for step, _ in _steps() if not step.startswith("role:")}

    assert modules - NO_TARGET_DEPENDENCIES - set(TARGET_DEPENDENCIES) == set()


def test_no_task_overrides_the_interpreter():
    for path in [PLAYBOOK, *TASKS_DIR.glob("*.yml")]:
        assert "ansible_python_interpreter" not in path.read_text().replace(
            f"ansible_python_interpreter: {SYSTEM_PYTHON}", ""
        ), path


def test_ansible_cli_calls_do_not_depend_on_the_module_interpreter():
    """ansible-galaxy/-vault run from the CD venv or the controller's Python, not the target's."""
    calls = [
        task["ansible.builtin.command"]["argv"]
        for step, task in _steps()
        if step == "ansible.builtin.command"
        and any("ansible" in str(arg) for arg in task[step].get("argv", [])[:3])
    ]

    assert calls
    for argv in calls:
        assert argv[0] in {
            "{{ _galaxy_cd_venv }}/bin/ansible-galaxy",
            "{{ _galaxy_cd_venv }}/bin/ansible-vault",
            "{{ ansible_playbook_python }}",
        }, argv
