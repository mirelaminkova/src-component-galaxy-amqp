"""Continuous deployment of merged changes with an ansible-pull systemd timer (issue #22).

Covers the component parameters (vars/cd_vars.yml), the saved parameters
(``src_cd_params`` filter, templates/cd/params.yml.j2), the systemd units and the
``galaxy-cd`` wrapper script (rendered and run against stub ansible tools), the
files and modes tasks/cd.yml writes, and the tags a CD run is limited to.
"""

import base64
import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from filter_plugins.src_galaxy import src_cd_params
from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.render import render_template

VARS_FILES = ["internal_vars.yml", "src_galaxy_vars.yml", "cd_vars.yml"]
CD_TASKS = REPO_ROOT / "tasks" / "cd.yml"
PLAYBOOK = REPO_ROOT / "galaxysrv.yml"
DEFAULT_REPO = "https://github.com/mirelaminkova/src-component-galaxy-amqp.git"
VAULT_PASSWORD_FILE = "/etc/galaxy/vault_password"  # noqa: S105 - a path, written by #21


def _resolve(**params):
    return resolve_vars(VARS_FILES, params)


def _load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


# --- component parameters ------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, False),
        ({"src_galaxy_cd": ""}, False),
        ({"src_galaxy_cd": "false"}, False),
        ({"src_galaxy_cd": "true"}, True),
        ({"src_galaxy_cd": True}, True),
    ],
)
def test_cd_is_off_unless_enabled(params, expected):
    assert _resolve(**params)("_galaxy_cd") is expected


def test_cd_defaults():
    resolve = _resolve()

    assert resolve("_galaxy_cd_repo") == DEFAULT_REPO
    assert resolve("_galaxy_cd_branch") == "main"
    assert resolve("_galaxy_cd_interval") == "15min"
    assert resolve("_galaxy_cd_errors") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "15min"),
        ("30", "30min"),
        (5, "5min"),
        ("2h", "2h"),
        (" 45 min ", "45min"),
        ("90s", "90s"),
    ],
)
def test_interval_is_minutes_or_a_systemd_time_span(value, expected):
    resolve = _resolve(src_galaxy_cd_interval=value)

    assert resolve("_galaxy_cd_interval") == expected
    assert resolve("_galaxy_cd_errors") == []


@pytest.mark.parametrize("value", ["0", "-5", "abc", "10 fortnights", "5min; rm -rf /"])
def test_invalid_interval_is_reported(value):
    errors = _resolve(src_galaxy_cd_interval=value)("_galaxy_cd_errors")

    assert len(errors) == 1
    assert "src_galaxy_cd_interval" in errors[0]


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "ssh://git@example.org/org/repo.git",
    ],
)
def test_valid_repo_urls(repo):
    resolve = _resolve(src_galaxy_cd_repo=repo)

    assert resolve("_galaxy_cd_repo") == repo
    assert resolve("_galaxy_cd_errors") == []


@pytest.mark.parametrize(
    "repo",
    [
        "http://github.com/org/repo.git",
        "/srv/repo",
        "https://x/a b",
        "https://x/%61",
        "https://x/'a'",
    ],
)
def test_invalid_repo_url_is_reported(repo):
    errors = _resolve(src_galaxy_cd_repo=repo)("_galaxy_cd_errors")

    assert len(errors) == 1
    assert "src_galaxy_cd_repo" in errors[0]


@pytest.mark.parametrize("branch", ["-x", "a b", "a;b", "$(id)", ".hidden"])
def test_invalid_branch_is_reported(branch):
    errors = _resolve(src_galaxy_cd_branch=branch)("_galaxy_cd_errors")

    assert len(errors) == 1
    assert "src_galaxy_cd_branch" in errors[0]


def test_cd_tags_cover_rabbitmq_and_galaxy_config_files():
    # galaxy_config_files is the galaxyproject.galaxy role's tag for galaxy_config_templates
    # (job_conf.yml, user_preferences_extra_conf.yml, TPV) and galaxy.yml.
    assert _resolve()("_galaxy_cd_tags") == ["rabbitmq", "galaxy_config_files"]


def test_cd_ansible_core_matches_the_src_compat_ci_job():
    """The workspace's Ansible is what CI tests for SRC, not the dev tooling's.

    ansible-core 2.22 (requirements-dev.txt) needs Python >= 3.13; SRC workspaces (Ubuntu
    24.04) have 3.12 and run the playbook with 2.21, which the compat pytest job tests.
    """
    pins = _resolve()("_galaxy_cd_pip_packages")
    checks = (REPO_ROOT / ".github" / "workflows" / "checks.yml").read_text()
    compat = re.search(r"'ansible-core~=(\S+)'", checks).group(1)

    assert f"ansible-core=={compat}" in pins


# --- saved parameters ------------------------------------------------------------


def test_saved_parameters_come_from_the_component_parameters():
    resolve = _resolve(
        src_galaxy_rabbitmq="true",
        src_galaxy_pulsar_amqp_configs="[{name: hpc}]",
        workspace_fqdn="galaxy.example.org",
        src_galaxy_vault_password="vault-pass-value",  # noqa: S106 - test value
        unrelated="x",
    )

    params = resolve("_galaxy_cd_params")

    assert params["plain"] == {
        "src_galaxy_rabbitmq": "true",
        "src_galaxy_pulsar_amqp_configs": "[{name: hpc}]",
        "workspace_fqdn": "galaxy.example.org",
    }
    assert params["secret"] == {}


def test_parameters_holding_secrets_are_kept_apart():
    params = src_cd_params(
        {
            "src_galaxy_vault_password": "the-vault-password",
            "src_galaxy_pulsar_configs": "[{url: https://p.example.org, private_token: abc}]",
            "src_galaxy_pulsar_amqp_configs": [{"name": "hpc", "password": "x" * 12}],
            "src_galaxy_api_token": "t",
            "src_galaxy_brand": "Brand",
            "src_galaxy_cd": True,
            "src_galaxy_pulsar_tool_mappings": "[{tool_id: cat1, environment: hpc}]",
        }
    )

    assert params["plain"] == {
        "src_galaxy_brand": "Brand",
        "src_galaxy_cd": True,
        "src_galaxy_pulsar_tool_mappings": "[{tool_id: cat1, environment: hpc}]",
    }
    assert set(params["secret"]) == {
        "src_galaxy_pulsar_configs",
        "src_galaxy_pulsar_amqp_configs",
        "src_galaxy_api_token",
    }
    # The vault password is the key of the vault, never stored again.
    assert "the-vault-password" not in repr(params)


def test_params_template_holds_only_the_plain_parameters():
    cd_params = src_cd_params(
        {"src_galaxy_brand": "B: {x}", "src_galaxy_pulsar_configs": "[{private_token: SECRET}]"}
    )

    text = render_template("cd/params.yml.j2", {"_galaxy_cd_params": cd_params})

    assert "SECRET" not in text
    assert text.startswith("# Managed by")
    assert yaml.safe_load(text) == {"src_galaxy_brand": "B: {x}"}


def test_params_template_without_parameters_is_an_empty_mapping():
    text = render_template("cd/params.yml.j2", {"_galaxy_cd_params": src_cd_params({})})

    assert yaml.safe_load(text) == {}


# --- systemd units ---------------------------------------------------------------

UNIT_VARS = {
    "_galaxy_cd_interval": "15min",
    "_galaxy_cd_script": "/usr/local/sbin/galaxy-cd",
}


def _unit(name, **overrides):
    text = render_template(f"cd/{name}.j2", {**UNIT_VARS, **overrides})
    entries = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            entries.setdefault(key, []).append(value)
    return text, entries


def test_service_is_a_oneshot_root_run_of_the_wrapper():
    text, entries = _unit("galaxy-cd.service")

    assert entries["Type"] == ["oneshot"]
    assert entries["ExecStart"] == ["/usr/local/sbin/galaxy-cd"]
    assert entries["SyslogIdentifier"] == ["galaxy-cd"]
    assert "User" not in entries  # root: the playbook manages system services and files
    assert entries["After"] == ["network-online.target"]
    assert "[Install]" not in text  # started by the timer only


def test_timer_runs_the_service_every_interval_but_not_right_after_activation():
    _text, entries = _unit("galaxy-cd.timer", _galaxy_cd_interval="30min")

    assert entries["Unit"] == ["galaxy-cd.service"]
    assert entries["OnActiveSec"] == ["30min"]
    assert entries["OnUnitInactiveSec"] == ["30min"]
    # OnBootSec would fire at once when the timer is started by the deployment itself.
    assert "OnBootSec" not in entries
    assert entries["WantedBy"] == ["timers.target"]


# --- wrapper script --------------------------------------------------------------

STUB = r"""#!/bin/bash
{
    printf 'CALL %s\n' "$TOOL"
    printf 'ARG %s\n' "$@"
    printf 'ENV VAULT=%s\n' "${ANSIBLE_VAULT_PASSWORD_FILE:-}"
    printf 'ENV FORCE_HANDLERS=%s\n' "${ANSIBLE_FORCE_HANDLERS:-}"
    if [[ -e $STATE_DIR/last-run-failed ]]; then marker=yes; else marker=no; fi
    printf 'ENV MARKER=%s\n' "$marker"
} >> "$CALLS"
if [[ $TOOL == ansible-pull && -n ${STUB_PULL_REQUIREMENTS:-} ]]; then
    mkdir -p "$STATE_DIR/checkout/.git"
    printf '%s' "$STUB_PULL_REQUIREMENTS" > "$STATE_DIR/checkout/requirements.yml"
fi
rc_var="STUB_RC_${TOOL//-/_}"
exit "${!rc_var:-0}"
"""


def _script_vars(tmp_path, state_dir, config_dir):
    return {
        "_galaxy_cd_venv": str(tmp_path / "venv"),
        "_galaxy_cd_state_dir": str(state_dir),
        "_galaxy_cd_config_dir": str(config_dir),
        "_galaxy_cd_lock_file": str(state_dir / "lock"),
        "_galaxy_cd_vault_password_file": str(tmp_path / "vault_password"),
        "_galaxy_cd_deploy_key": str(config_dir / "deploy_key"),
        "_galaxy_cd_repo": "git@github.com:org/repo name.git",
        "_galaxy_cd_branch": "main",
        "_galaxy_cd_tags": ["rabbitmq", "galaxy_config_files"],
    }


@pytest.fixture
def cd_host(tmp_path):
    """A rendered galaxy-cd script whose paths point into tmp_path, with stub ansible tools."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    calls = tmp_path / "calls"
    config_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    for tool in ("ansible-pull", "ansible-galaxy"):
        stub = venv_bin / tool
        stub.write_text(STUB.replace("#!/bin/bash\n", f"#!/bin/bash\nTOOL={tool}\n", 1))
        stub.chmod(0o755)
    (config_dir / "params.yml").write_text("{}\n")
    script = tmp_path / "galaxy-cd"
    script.write_text(
        render_template("cd/galaxy-cd.sh.j2", _script_vars(tmp_path, state_dir, config_dir))
    )
    script.chmod(0o700)

    class Host:
        path = tmp_path

        def run(self, **env):
            result = subprocess.run(  # noqa: S603 - our own rendered script
                [str(script)],
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ["PATH"],
                    "CALLS": str(calls),
                    "STATE_DIR": str(state_dir),
                    **env,
                },
                check=False,
            )
            invocations = []
            if calls.exists():
                for line in calls.read_text().splitlines():
                    kind, _, value = line.partition(" ")
                    if kind == "CALL":
                        invocations.append({"tool": value, "args": [], "env": {}})
                    elif kind == "ARG":
                        invocations[-1]["args"].append(value)
                    else:
                        key, _, env_value = value.partition("=")
                        invocations[-1]["env"][key] = env_value
                calls.unlink()
            return result, invocations

    Host.state_dir = state_dir
    Host.config_dir = config_dir
    Host.checkout = state_dir / "checkout"
    return Host()


def _pull(invocations):
    pulls = [call for call in invocations if call["tool"] == "ansible-pull"]
    assert len(pulls) == 1
    return pulls[0]


def _pull_args(invocations):
    return _pull(invocations)["args"]


def _galaxy_installs(invocations):
    return [call["args"] for call in invocations if call["tool"] == "ansible-galaxy"]


def _make_checkout(cd_host, requirements=None):
    (cd_host.checkout / ".git").mkdir(parents=True)
    if requirements is not None:
        (cd_host.checkout / "requirements.yml").write_text(requirements)


def _record_requirements(cd_host, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    (cd_host.state_dir / "requirements.sha256").write_text(digest + "\n")


def test_script_is_valid_bash(tmp_path):
    variables = _script_vars(tmp_path, Path("/var/lib/galaxy-cd"), Path("/etc/galaxy-cd"))
    text = render_template("cd/galaxy-cd.sh.j2", variables)

    subprocess.run(["bash", "-n"], input=text, text=True, check=True)  # noqa: S607
    assert text.startswith("#!/bin/bash\n")


def test_first_run_pulls_the_branch_and_runs_only_the_cd_tags(cd_host):
    result, invocations = cd_host.run()

    assert result.returncode == 0, result.stderr
    args = _pull_args(invocations)
    assert args[args.index("--url") + 1] == "git@github.com:org/repo name.git"
    assert args[args.index("--checkout") + 1] == "main"
    assert args[args.index("--directory") + 1] == str(cd_host.checkout)
    assert args[args.index("--tags") + 1] == "rabbitmq,galaxy_config_files"
    assert args[args.index("--inventory") + 1] == "localhost,"
    assert "--clean" in args
    assert args[-1] == "galaxysrv.yml"
    extra_vars = [args[i + 1] for i, arg in enumerate(args) if arg == "--extra-vars"]
    assert extra_vars == [
        f"@{cd_host.config_dir / 'params.yml'}",
        "src_galaxy_bootstrap=false",
        "ansible_python_interpreter=/usr/bin/python3",
    ]
    # No checkout yet: always run the playbook.
    assert "--only-if-changed" not in args
    # Nothing to decrypt or authenticate with.
    assert _pull(invocations)["env"]["VAULT"] == ""
    assert "--private-key" not in args
    assert "galaxy-cd: OK" in result.stdout


def test_handlers_run_even_when_a_later_task_fails(cd_host):
    # Otherwise a config change followed by a failure never restarts Galaxy: the retry
    # renders identical files and notifies nothing.
    _, invocations = cd_host.run()

    assert _pull(invocations)["env"]["FORCE_HANDLERS"] == "True"


def test_later_runs_only_run_the_playbook_when_the_branch_changed(cd_host):
    _make_checkout(cd_host, "---\n")
    _record_requirements(cd_host, "---\n")

    result, invocations = cd_host.run()

    assert result.returncode == 0, result.stderr
    assert "--only-if-changed" in _pull_args(invocations)
    # requirements.yml did not change: nothing to install.
    assert _galaxy_installs(invocations) == []


def test_changed_requirements_are_force_installed_before_the_run(cd_host):
    _make_checkout(cd_host, "roles: [new]\n")
    _record_requirements(cd_host, "roles: [old]\n")

    result, invocations = cd_host.run()

    assert result.returncode == 0, result.stderr
    # --force: without it ansible-galaxy keeps an installed role of another version.
    assert _galaxy_installs(invocations) == [
        ["install", "--force", "-r", str(cd_host.checkout / "requirements.yml")]
    ]
    # The playbook runs in full, with the new roles.
    assert "--only-if-changed" not in _pull_args(invocations)
    digest = (cd_host.state_dir / "requirements.sha256").read_text().strip()
    assert digest == hashlib.sha256(b"roles: [new]\n").hexdigest()


def test_requirements_changed_by_the_pull_are_installed_and_applied_next_run(cd_host):
    _make_checkout(cd_host, "roles: [old]\n")
    _record_requirements(cd_host, "roles: [old]\n")

    result, invocations = cd_host.run(STUB_PULL_REQUIREMENTS="roles: [new]\n")

    assert result.returncode == 0, result.stderr
    assert [call["tool"] for call in invocations] == ["ansible-pull", "ansible-galaxy"]
    assert _galaxy_installs(invocations)[0][:2] == ["install", "--force"]
    assert "requirements.yml changed" in result.stdout
    # This run used the old roles, so the next one runs the playbook in full.
    assert (cd_host.state_dir / "last-run-failed").exists()

    result, invocations = cd_host.run()

    assert "--only-if-changed" not in _pull_args(invocations)
    assert _galaxy_installs(invocations) == []
    assert not (cd_host.state_dir / "last-run-failed").exists()


def test_secret_parameters_vault_password_and_deploy_key_are_used_when_present(cd_host):
    (cd_host.config_dir / "secret_params.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n")
    (cd_host.path / "vault_password").write_text("x\n")
    (cd_host.config_dir / "deploy_key").write_text("key\n")

    result, invocations = cd_host.run()

    assert result.returncode == 0, result.stderr
    args = _pull_args(invocations)
    assert f"@{cd_host.config_dir / 'secret_params.yml'}" in args
    # In the environment, not --vault-password-file: ansible-pull gives the extra vars (and so
    # the vault-encrypted secret_params.yml) to its git checkout command too, but not that option.
    assert "--vault-password-file" not in args
    assert _pull(invocations)["env"]["VAULT"] == str(cd_host.path / "vault_password")
    assert args[args.index("--private-key") + 1] == str(cd_host.config_dir / "deploy_key")
    # Host keys are pinned (tasks/cd.yml), never trusted on first use.
    assert "--accept-host-key" not in args


def test_failed_run_is_reported_and_retried_in_full(cd_host):
    _make_checkout(cd_host)

    result, _ = cd_host.run(STUB_RC_ansible_pull="2")

    assert result.returncode == 2
    assert "galaxy-cd: FAILED" in result.stderr
    assert "journalctl -u galaxy-cd" in result.stderr
    assert (cd_host.state_dir / "last-run-failed").exists()

    # The next run does not skip the playbook although the branch did not change.
    result, invocations = cd_host.run()

    assert result.returncode == 0, result.stderr
    assert "--only-if-changed" not in _pull_args(invocations)
    assert not (cd_host.state_dir / "last-run-failed").exists()


def test_an_interrupted_run_is_retried_in_full(cd_host):
    # The marker is set before ansible-pull and only removed on success, so a run killed
    # midway (timeout, reboot, OOM, systemctl stop) leaves it behind.
    _make_checkout(cd_host)

    _, invocations = cd_host.run()

    assert _pull(invocations)["env"]["MARKER"] == "yes"
    assert not (cd_host.state_dir / "last-run-failed").exists()


def test_failed_requirements_install_fails_the_run(cd_host):
    _make_checkout(cd_host, "---\n")

    result, invocations = cd_host.run(STUB_RC_ansible_galaxy="1")

    assert result.returncode == 1
    assert "galaxy-cd: FAILED" in result.stderr
    assert [call["tool"] for call in invocations] == ["ansible-galaxy"]
    assert (cd_host.state_dir / "last-run-failed").exists()
    assert not (cd_host.state_dir / "requirements.sha256").exists()


def test_runs_never_overlap(cd_host):
    lock = cd_host.state_dir / "lock"
    with subprocess.Popen(  # noqa: S603
        ["flock", str(lock), "sleep", "30"]  # noqa: S607
    ) as holder:
        try:
            # Wait until the holder has the lock.
            for _ in range(100):
                probe = subprocess.run(  # noqa: S603
                    ["flock", "--nonblock", str(lock), "true"],  # noqa: S607
                    check=False,
                )
                if probe.returncode != 0:
                    break
                subprocess.run(["sleep", "0.05"], check=True)  # noqa: S607

            result, invocations = cd_host.run()
        finally:
            holder.kill()

    assert result.returncode == 0
    assert "another run is in progress" in result.stdout
    assert invocations == []


def test_a_lock_that_cannot_be_opened_fails_the_run(cd_host):
    # Never "busy, exit 0": that would silently skip every run.
    (cd_host.state_dir / "lock").mkdir()

    result, invocations = cd_host.run()

    assert result.returncode != 0
    assert "galaxy-cd: FAILED" in result.stderr
    assert "lock" in result.stderr
    assert invocations == []


def test_lock_is_in_the_root_only_state_dir():
    resolve = _resolve()

    assert resolve("_galaxy_cd_lock_file") == resolve("_galaxy_cd_state_dir") + "/lock"


# --- tasks/cd.yml ----------------------------------------------------------------


def _tasks_by_module(tasks, module):
    return [task for task in tasks if module in task]


def test_cd_setup_writes_root_only_files():
    tasks = _load_yaml(CD_TASKS)

    (create_dirs,) = [
        task
        for task in _tasks_by_module(tasks, "ansible.builtin.file")
        if task["ansible.builtin.file"].get("state") == "directory"
    ]
    assert create_dirs["ansible.builtin.file"]["owner"] == "root"
    modes = {item["path"]: item["mode"] for item in create_dirs["loop"]}
    assert modes["{{ _galaxy_cd_config_dir }}"] == "0700"
    assert modes["{{ _galaxy_cd_state_dir }}"] == "0700"

    templates = {
        task["ansible.builtin.template"]["src"]: task["ansible.builtin.template"]
        for task in _tasks_by_module(tasks, "ansible.builtin.template")
    }
    assert templates["templates/cd/params.yml.j2"]["mode"] == "0600"
    assert templates["templates/cd/galaxy-cd.sh.j2"]["mode"] == "0700"
    assert templates["templates/cd/galaxy-cd.service.j2"]["mode"] == "0644"
    assert templates["templates/cd/galaxy-cd.timer.j2"]["mode"] == "0644"
    for template in templates.values():
        assert template["owner"] == "root"
        assert template["group"] == "root"


def test_cd_setup_records_the_requirements_it_installed():
    tasks = {task["name"]: task for task in _load_yaml(CD_TASKS)}

    install = tasks["Install the playbook's roles and collections for galaxy-cd"]
    assert install["ansible.builtin.command"]["argv"][1:4] == ["install", "--force", "-r"]
    checksum = tasks["Check the installed requirements.yml"]["ansible.builtin.stat"]
    assert checksum["checksum_algorithm"] == "sha256"
    record = tasks["Record the installed requirements.yml"]["ansible.builtin.copy"]
    # The same file name and format galaxy-cd compares (sha256sum's hex digest).
    assert record["dest"] == "{{ _galaxy_cd_state_dir }}/requirements.sha256"
    assert record["content"] == "{{ _galaxy_cd_requirements_file.stat.checksum }}\n"
    script = (REPO_ROOT / "templates" / "cd" / "galaxy-cd.sh.j2").read_text()
    assert 'requirements_record="$state_dir/requirements.sha256"' in script


def test_ssh_host_keys_are_pinned_not_trusted_on_first_use():
    tasks = {task["name"]: task for task in _load_yaml(CD_TASKS)}
    known_hosts = tasks["Pin the SSH host keys of the CD repository host"]

    assert known_hosts["ansible.builtin.known_hosts"]["path"] == "/root/.ssh/known_hosts"
    assert known_hosts["loop"] == "{{ _galaxy_cd_known_hosts }}"
    keys = _resolve()("_galaxy_cd_known_hosts")
    assert any(key.startswith("github.com ssh-ed25519 ") for key in keys)
    assert "accept-host-key" not in (REPO_ROOT / "templates" / "cd" / "galaxy-cd.sh.j2").read_text()


def test_secret_parameters_are_only_written_vault_encrypted():
    tasks = _load_yaml(CD_TASKS)
    text = CD_TASKS.read_text()

    encrypt = [
        task
        for task in _tasks_by_module(tasks, "ansible.builtin.command")
        if "encrypt" in task["ansible.builtin.command"]["argv"]
    ]
    assert len(encrypt) == 1
    command = encrypt[0]["ansible.builtin.command"]
    assert command["argv"][:2] == ["{{ _galaxy_cd_venv }}/bin/ansible-vault", "encrypt"]
    assert encrypt[0]["environment"] == {
        "ANSIBLE_VAULT_PASSWORD_FILE": "{{ _galaxy_cd_vault_password_file }}"
    }
    # The secrets go in on stdin, never on the command line.
    assert "_galaxy_cd_params.secret" in command["stdin"]
    assert not any("secret" in arg for arg in command["argv"] if "secret_params" not in arg)
    assert encrypt[0]["no_log"] is True
    # The plain parameter file never sees the secret ones.
    params_template = (REPO_ROOT / "templates" / "cd" / "params.yml.j2").read_text()
    assert "_galaxy_cd_params.plain" in params_template
    assert ".secret" not in params_template
    assert "secret_params.yml" in text
    # The vault password file is #21's; CD only reads it.
    assert not any(
        task.get(module, {}).get("dest") == "{{ _galaxy_cd_vault_password_file }}"
        for task in tasks
        for module in ("ansible.builtin.copy", "ansible.builtin.template")
    )


def test_cd_setup_runs_last_and_is_not_part_of_cd_runs():
    play = _load_yaml(PLAYBOOK)[0]
    last = play["post_tasks"][-1]

    assert last["ansible.builtin.include_tasks"] == "tasks/cd.yml"
    assert last["when"] == "_galaxy_cd"
    assert "tags" not in last


# --- tags of a CD run --------------------------------------------------------------

# What onboarding PRs change (issue #18) and the variables that carry it into tasks.
ONBOARDING_MARKERS = (
    "user_preferences_extra_conf",
    "user_preference_config_file",
    "job_conf",
    "job_config_file",
    "tpv/",
    "_galaxy_tpv",
    "group_vars/mq.yml",
    "secret_group_vars",
    "_galaxy_rabbitmq_users",
    "_galaxy_rabbitmq_vhosts",
)
INCLUDE_MODULES = (
    "ansible.builtin.include_tasks",
    "include_tasks",
    "ansible.builtin.import_tasks",
    "import_tasks",
)


def _as_set(tags):
    if tags is None:
        return set()
    return {tags} if isinstance(tags, str) else set(tags)


def _resolve_task_file(name, current_dir):
    for base in (current_dir, REPO_ROOT):
        if (base / name).is_file():
            return base / name
    raise AssertionError(f"included task file {name} not found")


def _walk(tasks, inherited, current_dir):
    """Yield (task, tags the task runs with) like ansible-playbook --tags decides it.

    Tags of a block and of an import reach the tasks inside; tags of a dynamic include
    only reach the included tasks through ``apply``.
    """
    for task in tasks or []:
        own = inherited | _as_set(task.get("tags"))
        yield task, own
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key), own, current_dir)
        module = next((m for m in INCLUDE_MODULES if m in task), None)
        if module is None:
            continue
        spec = task[module]
        file_name = spec if isinstance(spec, str) else spec["file"]
        if "{{" in file_name:
            continue
        if "import" in module:
            child = own
        else:
            apply = spec.get("apply", {}) if isinstance(spec, dict) else {}
            child = inherited | _as_set(apply.get("tags"))
        path = _resolve_task_file(file_name, current_dir)
        yield from _walk(_load_yaml(path), child, path.parent)


def _playbook_tasks():
    play = _load_yaml(PLAYBOOK)[0]
    play_tags = _as_set(play.get("tags"))
    for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
        yield from _walk(play.get(section), play_tags, REPO_ROOT)


def _runs_in_cd(tags):
    cd_tags = set(_resolve()("_galaxy_cd_tags"))
    return bool(tags & (cd_tags | {"always"}))


def test_task_walker_follows_includes():
    names = {task.get("name") for task, _ in _playbook_tasks()}

    assert "Load internal variables" in names  # tasks/set_config.yml
    assert "Determine TLS certificate paths" in names  # tasks/rabbitmq.yml
    assert "Set TLS certificate paths" in names  # tasks/tls_facts.yml, nested include
    assert "Create RabbitMQ vhosts" in names


def test_every_task_touching_onboarding_files_runs_in_cd():
    missing = []
    for task, tags in _playbook_tasks():
        body = yaml.safe_dump({k: v for k, v in task.items() if k != "name"})
        if any(marker in body for marker in ONBOARDING_MARKERS) and not _runs_in_cd(tags):
            missing.append(task.get("name"))

    assert missing == []


def test_rabbitmq_users_and_vhosts_run_in_cd():
    tasks = {task.get("name"): tags for task, tags in _playbook_tasks()}

    assert _runs_in_cd(tasks["Create RabbitMQ vhosts"])
    assert _runs_in_cd(tasks["Configure RabbitMQ user tags and permissions"])
    assert _runs_in_cd(
        tasks[
            "Set passwords of new RabbitMQ users and of users whose password file was (re)created"
        ]
    )


def test_configuration_and_facts_load_in_every_cd_run():
    tasks = {task.get("name"): tags for task, tags in _playbook_tasks()}

    # Variables, validation, and the CO admin facts that galaxy.yml's admin_users comes from.
    assert "always" in tasks["Load internal variables"]
    assert "always" in tasks["Load component variables"]
    assert "always" in tasks["Set workspace user facts"]
    play = _load_yaml(PLAYBOOK)[0]
    facts = play["pre_tasks"][0]["ansible.builtin.include_role"]
    assert facts["apply"]["tags"] == ["always"]


def test_galaxy_role_config_templates_carry_the_onboarding_files():
    """job_conf, user preferences and TPV are deployed by the galaxy role's galaxy_config_files."""
    srcs = [
        entry["src"]
        for entry in _load_yaml(REPO_ROOT / "vars" / "galaxy_vars.yml")["galaxy_config_templates"]
    ]

    assert "templates/galaxy/config/job_conf.yml.j2" in srcs
    assert "templates/galaxy/config/user_preferences_extra_conf.yml.j2" in srcs
    for tpv in sorted((REPO_ROOT / "files" / "galaxy" / "tpv").glob("*.j2")):
        assert str(tpv.relative_to(REPO_ROOT)) in srcs
    assert "galaxy_config_files" in _resolve()("_galaxy_cd_tags")


def test_cd_runs_do_not_rotate_the_remote_user_secret():
    """galaxy.yml is re-rendered in CD runs, nginx's copy of the secret is not."""
    tasks = {task.get("name"): task for task, _ in _playbook_tasks()}

    read = tasks["Read the deployed Galaxy config (to keep its remote user secret)"]
    assert read["ansible.builtin.slurp"]["src"] == "{{ _galaxy_root }}/config/galaxy.yml"
    keep = tasks["Generate a remote user secret, or keep the deployed one"]
    assert "remote_user_secret" in keep["ansible.builtin.set_fact"]["_galaxy_remote_user_secret"]


# --- no_log ------------------------------------------------------------------------

# Variables whose values are secrets (passwords, tokens, the remote user secret).
SECRET_VARIABLE = re.compile(r"\b\w*(?:_password|_passwords|_secret|private_token)\b")
JINJA = re.compile(r"{{(.*?)}}|{%(.*?)%}", re.DOTALL)
NOT_LOGGED_KEYS = {
    "name",
    "when",
    "changed_when",
    "failed_when",
    "no_log",
    "tags",
    "register",
    "loop_control",
    "notify",
    "listen",
}


def _all_task_files_tasks():
    seen = set()
    for task, _ in _playbook_tasks():
        if id(task) not in seen:
            seen.add(id(task))
            yield task
    for path in sorted((REPO_ROOT / "tasks").glob("*.yml")):
        yield from (task for task, _ in _walk(_load_yaml(path), set(), path.parent))


def _needs_no_log(task):
    if any(module in task for module in ("ansible.builtin.slurp", "slurp")):
        return True
    for key, value in task.items():
        if key in NOT_LOGGED_KEYS or key in ("block", "rescue", "always"):
            continue
        for match in JINJA.finditer(yaml.safe_dump(value, width=10_000)):
            expression = match.group(1) or match.group(2)
            if SECRET_VARIABLE.search(expression):
                return True
    return False


def test_tasks_handling_secrets_use_no_log():
    missing = sorted(
        {
            task.get("name")
            for task in _all_task_files_tasks()
            if _needs_no_log(task) and task.get("no_log") is not True
        }
    )

    assert missing == []


def test_no_log_check_detects_secrets():
    assert _needs_no_log({"ansible.builtin.set_fact": {"x": "{{ _galaxy_remote_user_secret }}"}})
    assert _needs_no_log({"ansible.builtin.slurp": {"src": "/etc/x"}})
    assert not _needs_no_log(
        {"ansible.builtin.file": {"path": "{{ _galaxy_rabbitmq_secrets_dir }}"}}
    )
    assert not _needs_no_log(
        {
            "name": "x",
            "when": "_galaxy_remote_user_secret is defined",
            "ansible.builtin.debug": {"msg": "hi"},
        }
    )


def test_cd_task_modes_are_quoted():
    # Unquoted 0600 is an int in YAML 1.1 (mode 384 to Ansible).
    assert not re.search(r"mode: 0\d{3}\b", CD_TASKS.read_text())


def _remote_user_secret(slurp_result):
    tasks = {task.get("name"): task for task, _ in _playbook_tasks()}
    expression = tasks["Generate a remote user secret, or keep the deployed one"][
        "ansible.builtin.set_fact"
    ]["_galaxy_remote_user_secret"]
    templar = Templar(loader=DataLoader(), variables={"_galaxy_deployed_config": slurp_result})
    return templar.template(trust_as_template(expression))


@pytest.mark.parametrize(
    "slurp_result",
    [
        {"failed": True, "msg": "file not found"},  # first deployment
        {"content": ""},
        {"content": "Z2FsYXh5OiB7fQo="},  # "galaxy: {}"
    ],
)
def test_a_new_remote_user_secret_is_generated_without_a_deployed_one(slurp_result):
    secret = _remote_user_secret(slurp_result)

    assert re.fullmatch(r"[a-z0-9]{50}", secret)


def test_the_deployed_remote_user_secret_is_kept():
    content = base64.b64encode(b"galaxy:\n  remote_user_secret: abc123\n  brand: x\n").decode()

    assert _remote_user_secret({"content": content}) == "abc123"
