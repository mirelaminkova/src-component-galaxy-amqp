"""EU-layout BYOC onboarding files (issue #21): contract tests and their validation.

The contract: an onboarding pull request shaped like usegalaxy-eu/infrastructure-playbook
#872 and #1384 (one endpoint added to the onboarding files, see
filter_plugins/byoc_onboarding.py) must deploy a working endpoint here. The tests copy the
repository's own onboarding files, insert the lines those PRs add, verbatim
(tests/fixtures/byoc_onboarding/eu-*), where usegalaxy.eu's PRs put them, add the passwords
to secret_group_vars/pulsar.yml with a throwaway Vault password, and load the result the
way tasks/byoc_onboarding.yml does.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ansible.errors import AnsibleError
from tpv.commands.dryrunner import TPVDryRunner
from tpv.commands.linter import TPVConfigLinter
from tpv.commands.test import mock_galaxy

from filter_plugins.byoc_onboarding import (
    RUNNER_LOAD,
    byoc_onboarding_errors,
    byoc_runners,
    byoc_secret_errors,
    preference_options,
)
from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.onboarding import (
    ANCHORS,
    FILES,
    SECRETS_FILE,
    TPV_DESTINATIONS,
    add_secrets,
    copy_repository_files,
    decrypt_command,
    decrypt_secrets,
    duplicate_keys,
    insert_before_anchor,
    load_files,
    onboard,
    plaintext_secret_files,
    secret_files,
)
from tests.helpers.render import make_environment, render_template, render_yaml

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "byoc_onboarding"
VAULT_PASSWORD_FILE = FIXTURES / "vault-password.txt"
# (lines of the PR, passwords the PR adds to secret_group_vars/pulsar.yml); throwaway values.
ONBOARDINGS = [
    (FIXTURES / "eu-872-egi01", {"rabbitmq_password_galaxy_egi01": "Egi01ThrowawayTestPassword"}),
    (
        FIXTURES / "eu-1384-eosc01",
        {"rabbitmq_password_galaxy_eosc01": "Eosc01ThrowawayTestPassword"},
    ),
]
ENDPOINTS = ("egi01", "eosc01")
VARS_FILES = [
    "internal_vars.yml",
    "src_galaxy_vars.yml",
    "rabbitmq_vars.yml",
    "byoc_onboarding_vars.yml",
]
FQDN = "galaxy.example.org"
PARAMS = {"workspace_fqdn": FQDN, "src_galaxy_rabbitmq": "true"}
TASKS_FILE = REPO_ROOT / "tasks" / "byoc_onboarding.yml"
ENDPOINTS_HOSTS = [f"{FQDN}:5671", "127.0.0.1:5672"]
GALAXY_URL = f"https://{FQDN}"
# A valid resolved password (throwaway, as in the fixtures).
RESOLVED = "Abcdefghij12"
MQ = "group_vars/mq.yml rabbitmq_users"
JOB = "templates/galaxy/config/job_conf.yml"
PREFS = "files/galaxy/config/user_preferences_extra_conf.yml"
TPV = "files/galaxy/tpv/destinations.yml.j2"
SECRETS = "secret_group_vars"


def _ref(user):
    return "{{ rabbitmq_password_" + user + " }}"


def _mq_user(endpoint_id, password=None):
    user = f"galaxy_{endpoint_id}"
    return {"user": user, "password": password or _ref(user), "vhost": f"/pulsar/{user}"}


def _runner(endpoint_id, password=None, host=FQDN + ":5671", **params):
    user = f"galaxy_{endpoint_id}"
    url = f"pyamqp://{user}:{password or _ref(user)}@{host}//pulsar/{user}?ssl=1"
    return {
        "id": f"pulsar_eu_{endpoint_id}",
        "load": RUNNER_LOAD,
        "params": {"amqp_url": url, "galaxy_url": GALAXY_URL, "manager": "production", **params},
    }


def _with_params(runner, params):
    return {**runner, "params": {**runner["params"], **params}}


def _onboarded(tmp_path, count=None, extra=None):
    """The repository's files after ``count`` onboarding PRs, then ``extra`` {path: lines}."""
    root = copy_repository_files(tmp_path / "repo")
    for snippets, secrets in ONBOARDINGS[: len(ONBOARDINGS) if count is None else count]:
        onboard(root, snippets, secrets, VAULT_PASSWORD_FILE)
    for path, lines in (extra or {}).items():
        target = root / path
        target.write_text(insert_before_anchor(target.read_text(), ANCHORS[path], lines))
    return root


def _resolver(root, **params):
    templated = {**load_files(root), **decrypt_secrets(root, VAULT_PASSWORD_FILE)}
    return resolve_vars(VARS_FILES, {**PARAMS, **params}, templated)


def _raw_errors(root):
    files = load_files(root)
    return byoc_onboarding_errors(
        files["_galaxy_onboarding_mq"]["rabbitmq_users"] or [],
        files["_galaxy_onboarding_jobconf"]["galaxy_jobconf"]["plugins"] or [],
        preference_options(files["_galaxy_onboarding_preferences"]),
        _tpv_destinations(root),
    )


# Variables the job config and TPV templates use, as the playbook resolves them.
TEMPLATE_VARS = [
    "_galaxy_jobs_default",
    "_galaxy_use_interactive_tools",
    "_galaxy_has_pulsar",
    "_galaxy_pulsar_configs_raw",
    "_galaxy_pulsar_amqp_configs",
    "_galaxy_tpv_tool_mappings",
    "_galaxy_rabbitmq_local_port",
    "_galaxy_infrastructure_url",
    "_galaxy_byoc_runners",
]


def _template_vars(resolve, config_dir):
    return {
        **{name: resolve(name) for name in TEMPLATE_VARS},
        "_galaxy_tpv_config_dir": str(config_dir),
        "_galaxy_rabbitmq_passwords": {"galaxy": "GalaxyPw0123456789"},
        "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
        "apptainer_volumes": ["$galaxy_root:ro", "$working_directory:rw"],
    }


def _render_configs(root, resolve, config_dir):
    """Render job_conf.yml and the TPV files of ``root`` into ``config_dir``; return TPV paths."""
    config_dir.mkdir(parents=True, exist_ok=True)
    variables = _template_vars(resolve, config_dir)
    (config_dir / "job_conf.yml").write_text(
        render_template("galaxy/config/job_conf.yml.j2", variables)
    )
    paths = []
    for name in ("tool_defaults", "destinations"):
        source = (root / f"files/galaxy/tpv/{name}.yml.j2").read_text()
        path = config_dir / f"{name}.yml"
        path.write_text(_render_text(source, variables))
        paths.append(str(path))
    return paths


def _render_text(source, variables):
    return make_environment().from_string(source).render(**variables)


def _rendered_destinations(root):
    """destinations.yml.j2 of ``root`` rendered with the repository defaults."""
    resolve = resolve_vars(VARS_FILES, {"src_galaxy_rabbitmq": "true"})
    variables = _template_vars(resolve, Path("/srv/tpv"))
    return _render_text((root / TPV_DESTINATIONS).read_text(), variables)


def _tpv_destinations(root):
    """The TPV destinations of ``root`` (repository defaults: no component Pulsars)."""
    return yaml.safe_load(_rendered_destinations(root))["destinations"]


def _tasks(path):
    for task in yaml.safe_load(path.read_text()):
        yield task
        yield from (_tasks_in(task))


def _tasks_in(task):
    for key in ("block", "rescue", "always"):
        yield from task.get(key, [])


def _task(name):
    return next(task for task in _tasks(TASKS_FILE) if task["name"] == name)


# --- contract: verbatim #872 (egi01) and #1384 (eosc01) lines on the repository's files ------


def test_snippets_cover_the_eu_files_like_the_example_prs():
    for snippets, _ in ONBOARDINGS:
        assert {p.name for p in snippets.iterdir()} == {Path(p).name for p in ANCHORS}


def test_repository_files_start_empty():
    files = load_files(REPO_ROOT)

    assert files["_galaxy_onboarding_mq"]["rabbitmq_users"] is None
    assert files["_galaxy_onboarding_jobconf"]["galaxy_jobconf"]["plugins"] is None
    assert preference_options(files["_galaxy_onboarding_preferences"]) == []
    assert secret_files(REPO_ROOT) == []


def test_empty_repository_files_resolve_to_nothing():
    resolve = resolve_vars(VARS_FILES, PARAMS, load_files(REPO_ROOT))

    assert resolve("_galaxy_byoc_mq_users") == []
    assert resolve("_galaxy_byoc_runners") == []
    assert resolve("_galaxy_byoc_onboarding_errors") == []


@pytest.mark.parametrize("count", [1, 2], ids=["first_onboarding", "second_onboarding"])
def test_onboarded_files_pass_the_ci_checks(tmp_path, count):
    root = _onboarded(tmp_path, count)

    assert _raw_errors(root) == []
    assert plaintext_secret_files(root) == []
    assert duplicate_keys(_rendered_destinations(root)) == []


def test_first_onboarding_creates_the_vault_file(tmp_path):
    root = _onboarded(tmp_path, 1)

    assert [p.relative_to(root).as_posix() for p in secret_files(root)] == [SECRETS_FILE]
    assert decrypt_secrets(root, VAULT_PASSWORD_FILE) == ONBOARDINGS[0][1]


def test_onboarded_secrets_decrypt_with_the_playbook_command(tmp_path):
    root = _onboarded(tmp_path)
    secrets = decrypt_secrets(root, VAULT_PASSWORD_FILE)

    assert secrets == {**ONBOARDINGS[0][1], **ONBOARDINGS[1][1]}
    files = load_files(root)
    assert byoc_secret_errors(secrets, files["_galaxy_onboarding_mq"]["rabbitmq_users"]) == []


def test_onboarded_playbook_vars_resolve(tmp_path):
    resolve = _resolver(_onboarded(tmp_path))

    assert resolve("_galaxy_byoc_onboarding_errors") == []
    assert resolve("_galaxy_rabbitmq_vhosts") == [f"/pulsar/galaxy_{i}" for i in ENDPOINTS]
    assert resolve("_galaxy_rabbitmq_fixed_passwords") == {
        "galaxy_egi01": "Egi01ThrowawayTestPassword",
        "galaxy_eosc01": "Eosc01ThrowawayTestPassword",
    }


def test_onboarded_rabbitmq_users_are_limited_to_their_vhost(tmp_path):
    users = {
        u["name"]: u for u in _resolver(_onboarded(tmp_path))("_galaxy_rabbitmq_users_resolved")
    }

    for endpoint in ENDPOINTS:
        user = f"galaxy_{endpoint}"
        assert [p["vhost"] for p in users[user]["permissions"]] == [f"/pulsar/{user}"]
        assert users[user]["tags"] == ""
        assert f"/pulsar/{user}" in [p["vhost"] for p in users["galaxy"]["permissions"]]
    # No password ever travels with the user list (it is looped over without no_log elsewhere).
    assert all(set(user) == {"name", "tags", "permissions"} for user in users.values())


@pytest.mark.parametrize("port", [None, "5673"], ids=["default_port", "custom_port"])
def test_onboarded_runners_point_at_this_galaxy(tmp_path, port):
    """The EU lines name mq.galaxyproject.eu and usegalaxy.eu; the runners use ours."""
    params = {} if port is None else {"src_galaxy_rabbitmq_port": port}
    resolve = _resolver(_onboarded(tmp_path), **params)
    conf = render_yaml("galaxy/config/job_conf.yml.j2", _template_vars(resolve, tmp_path / "c"))

    assert set(conf["runners"]) == {"local", *(f"pulsar_eu_{i}" for i in ENDPOINTS)}
    eosc = conf["runners"]["pulsar_eu_eosc01"]
    assert eosc == {
        "load": RUNNER_LOAD,
        "amqp_url": (
            f"pyamqp://galaxy_eosc01:Eosc01ThrowawayTestPassword@{FQDN}:{port or 5671}"
            "//pulsar/galaxy_eosc01?ssl=1"
        ),
        "galaxy_url": GALAXY_URL,
        "manager": "production",
        "amqp_acknowledge": "true",
        "amqp_ack_republish_time": 300,
        "amqp_consumer_timeout": 2.0,
        "amqp_publish_retry": "true",
        "amqp_publish_retry_max_retries": 60,
    }


def test_onboarded_endpoints_are_in_the_dropdown(tmp_path):
    resolve = _resolver(_onboarded(tmp_path))
    prefs = render_yaml(
        "galaxy/config/user_preferences_extra_conf.yml.j2",
        {
            "_galaxy_ibridges_enabled": False,
            "_galaxy_byoc_pulsar": False,
            "_galaxy_remote_destinations": [
                {"id": "amqp_hpc", "label": "HPC", "tag": "hpc-pulsar"}
            ],
            "_galaxy_byoc_preference_options": resolve("_galaxy_byoc_preference_options"),
        },
    )

    assert prefs["preferences"]["distributed_compute"]["inputs"][0]["options"] == [
        ["default - Galaxy will decide where to put your job", "None"],
        ["HPC", "hpc-pulsar"],
        ["Bari (Italy) - INFN", "egi-pulsar"],
        ["EOSC EU Node 01", "eosc-eu-node-01-pulsar"],
    ]


def test_option_indentation_must_follow_the_file(tmp_path):
    """#872 predates usegalaxy.eu's 18-space options; a 10-space line breaks the YAML in CI."""
    path = FILES["_galaxy_onboarding_preferences"]
    root = _onboarded(tmp_path, extra={path: '          - ["Bari (Italy) - INFN", egi-pulsar]\n'})

    with pytest.raises(AnsibleError):
        load_files(root)


def _tool(tool_id="cat1"):
    """A tool stand-in with the attributes the TPV rules read (see test_tpv.py)."""
    tool = mock_galaxy.Tool(tool_id)
    tool.old_id = tool_id
    tool.all_ids = [tool_id]
    tool.tool_type = "default"
    tool.tool_type_local = False
    tool.requires_galaxy_python_environment = False
    return tool


def _user(choice=None):
    user = mock_galaxy.User("ada", "ada@example.org")
    user.extra_preferences = {"distributed_compute|remote_resources": choice}
    return user


def test_onboarded_tpv_config_lints(tmp_path, caplog):
    root = _onboarded(tmp_path)
    linter = TPVConfigLinter(
        _render_configs(root, _resolver(root), tmp_path / "config"),
        ignore=[],
        preserve_temp_code=False,
    )

    with caplog.at_level(logging.WARNING):
        linter.lint()

    assert linter.errors == []
    # TPV 3.1.x has no py.typed marker: mypy flags its own imports (see test_tpv.py).
    assert [w for w in linter.warnings if w[0] != "T103"] == []


@pytest.mark.parametrize(
    ("tag", "destination", "runner"),
    [
        ("egi-pulsar", "pulsar_egi01_tpv", "pulsar_eu_egi01"),
        ("eosc-eu-node-01-pulsar", "pulsar_eosc01_tpv", "pulsar_eu_eosc01"),
    ],
)
def test_user_picking_the_tag_routes_to_the_onboarded_destination(
    tmp_path, tag, destination, runner
):
    """TPV dry run through the rendered job_conf.yml, as Galaxy maps a job."""
    root = _onboarded(tmp_path)
    _render_configs(root, _resolver(root), tmp_path / "config")
    job_conf = str(tmp_path / "config" / "job_conf.yml")

    routed = TPVDryRunner(job_conf, tool=_tool(), user=_user(tag)).run()

    assert (routed.id, routed.runner) == (destination, runner)
    # Galaxy's own job config loader accepts the runner with the params from job_conf.yml.
    plugins = mock_galaxy.App(job_conf, create_model=True).job_config.runner_plugins
    [plugin] = [p for p in plugins if p["id"] == runner]
    assert plugin["load"] == RUNNER_LOAD
    assert plugin["kwds"]["manager"] == "production"


def test_onboarded_tags_route_nowhere_else(tmp_path):
    root = _onboarded(tmp_path)
    _render_configs(root, _resolver(root), tmp_path / "config")
    job_conf = str(tmp_path / "config" / "job_conf.yml")

    routed = TPVDryRunner(job_conf, tool=_tool(), user=_user("None")).run()

    assert routed.id == "singularity"


def test_wrong_vault_password_fails_decryption(tmp_path):
    root = _onboarded(tmp_path)
    wrong = tmp_path / "wrong"
    wrong.write_text("not-the-password\n")

    with pytest.raises(subprocess.CalledProcessError) as failure:
        decrypt_secrets(root, wrong)
    assert "Throwaway" not in failure.value.stdout + failure.value.stderr


def test_add_secrets_keeps_existing_ones(tmp_path):
    path = tmp_path / "pulsar.yml"
    add_secrets(path, {"a": "1"}, VAULT_PASSWORD_FILE)
    add_secrets(path, {"b": "2"}, VAULT_PASSWORD_FILE)

    assert path.read_bytes().startswith(b"$ANSIBLE_VAULT;")
    result = subprocess.run(  # noqa: S603 - fixed command, paths from the test suite
        decrypt_command(VAULT_PASSWORD_FILE, path), check=True, capture_output=True, text=True
    )
    assert yaml.safe_load(result.stdout) == {"a": "1", "b": "2"}


def test_insert_before_anchor_needs_the_anchor():
    with pytest.raises(ValueError, match="anchor"):
        insert_before_anchor("a: 1\n", "b:", "c: 2\n")


# --- negative: broken onboarding pull requests fail pytest -------------------------------


RUNNER_WITHOUT_MQ_USER = (
    "    - id: pulsar_eu_ghost01\n"
    "      load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner\n"
    "      params:\n"
    '        amqp_url: "pyamqp://galaxy_ghost01:{{ rabbitmq_password_galaxy_ghost01 }}'
    '@mq.galaxyproject.eu:5671//pulsar/galaxy_ghost01?ssl=1"\n'
)
PLAINTEXT_IN_MQ = (
    "  - user: galaxy_leak01\n"
    "    password: Leak01PlaintextPassword\n"
    "    vhost: /pulsar/galaxy_leak01\n"
)
DESTINATION_WITHOUT_RUNNER = (
    "  pulsar_ghost01_tpv:\n"
    "    inherits: pulsar_default\n"
    "    runner: pulsar_eu_ghost01\n"
    "    scheduling:\n"
    "      require:\n"
    "        - ghost-pulsar\n"
)
OPTION_WITHOUT_DESTINATION = '                  - ["Nowhere", nowhere-pulsar]\n'


def test_runner_without_mq_user_fails(tmp_path):
    root = _onboarded(tmp_path, extra={JOB: RUNNER_WITHOUT_MQ_USER})

    assert _raw_errors(root) == [
        f"{JOB} plugin 'pulsar_eu_ghost01': no RabbitMQ user galaxy_ghost01 in group_vars/mq.yml"
    ]


def test_plaintext_password_in_mq_yml_fails(tmp_path):
    errors = _raw_errors(_onboarded(tmp_path, extra={"group_vars/mq.yml": PLAINTEXT_IN_MQ}))

    assert errors == [
        f"{MQ} 'galaxy_leak01': 'password' must be '{{{{ rabbitmq_password_galaxy_leak01 }}}}' "
        "(the value belongs in secret_group_vars/pulsar.yml)"
    ]
    assert "Leak01PlaintextPassword" not in " ".join(errors)


def test_plaintext_file_in_secret_group_vars_fails(tmp_path):
    root = _onboarded(tmp_path)
    (root / SECRETS / "leak.yml").write_text(
        "rabbitmq_password_galaxy_leak01: Leak01PlaintextPassword\n"
    )

    assert plaintext_secret_files(root) == ["secret_group_vars/leak.yml"]


def test_destination_without_runner_fails_in_the_repository_files(tmp_path):
    root = _onboarded(tmp_path, extra={TPV: DESTINATION_WITHOUT_RUNNER})

    assert _raw_errors(root) == [
        f"{TPV} destination 'pulsar_ghost01_tpv': runner pulsar_eu_ghost01 is not in {JOB}"
    ]


def test_option_without_destination_fails_in_the_repository_files(tmp_path):
    root = _onboarded(tmp_path, extra={PREFS: OPTION_WITHOUT_DESTINATION})

    assert _raw_errors(root) == [
        f"{TPV}: no destination requires the tag nowhere-pulsar offered in {PREFS}"
    ]


def test_duplicate_destination_is_caught(tmp_path):
    """PyYAML (and so TPV) keeps the second of two equal keys; the check sees both."""
    duplicate = (FIXTURES / "eu-1384-eosc01" / "destinations.yml.j2").read_text()
    root = _onboarded(tmp_path, extra={TPV: duplicate})

    assert duplicate_keys(_rendered_destinations(root)) == ["pulsar_eosc01_tpv"]
    assert _raw_errors(root) == []


def test_runner_params_cannot_switch_the_runner_class(tmp_path):
    """A params.load would override the checked PulsarMQJobRunner (jobs on the Galaxy host)."""
    root = copy_repository_files(tmp_path / "repo")
    onboard(root, FIXTURES / "eu-1384-eosc01", ONBOARDINGS[1][1], VAULT_PASSWORD_FILE)
    text = (root / JOB).read_text()
    manager = "        manager: production\n"
    assert text.count(manager) == 1
    switch = "        load: galaxy.jobs.runners.local:LocalJobRunner\n"
    (root / JOB).write_text(text.replace(manager, manager + switch))

    assert _raw_errors(root) == [
        f"{JOB} plugin 'pulsar_eu_eosc01': params key 'load' is not allowed "
        "(lowercase letters, digits and '_', not id or load)"
    ]


def test_runner_params_keys_cannot_add_yaml(tmp_path):
    """A quoted key with a newline must neither pass the check nor inject a runner."""
    key = "manager\n  pulsar_eu_evil:\n    load: galaxy.jobs.runners.local:LocalJobRunner\n   x"
    runner = _runner("a", RESOLVED, **{key: "y"})

    assert byoc_onboarding_errors([_mq_user("a", RESOLVED)], [runner], [], resolved=True) == [
        f"{JOB} plugin 'pulsar_eu_a': params key {key!r} is not allowed "
        "(lowercase letters, digits and '_', not id or load)"
    ]
    resolve = resolve_vars(VARS_FILES, PARAMS)
    variables = {**_template_vars(resolve, Path("/srv/tpv")), "_galaxy_byoc_runners": [runner]}
    conf = render_yaml("galaxy/config/job_conf.yml.j2", variables)
    assert set(conf["runners"]) == {"local", "pulsar_eu_a"}
    assert conf["runners"]["pulsar_eu_a"][key] == "y"


def test_destination_without_runner_fails():
    errors = byoc_onboarding_errors(
        [_mq_user("egi01")],
        [_runner("egi01")],
        [["Bari", "egi-pulsar"]],
        {
            "pulsar_egi01_tpv": {
                "runner": "pulsar_eu_egi01",
                "scheduling": {"require": ["egi-pulsar"]},
            },
            "pulsar_ghost_tpv": {"runner": "pulsar_eu_ghost", "scheduling": {"require": ["x"]}},
        },
    )

    assert errors == [
        f"{TPV} destination 'pulsar_ghost_tpv': runner "
        "pulsar_eu_ghost is not in templates/galaxy/config/job_conf.yml"
    ]


def test_option_without_destination_fails():
    errors = byoc_onboarding_errors(
        [], [], [["Bari", "egi-pulsar"]], {"local": {"runner": "local"}, "other": "not-a-mapping"}
    )

    assert errors == [
        f"{TPV}: no destination requires the tag egi-pulsar "
        "offered in files/galaxy/config/user_preferences_extra_conf.yml"
    ]


def test_destinations_must_be_a_mapping():
    assert byoc_onboarding_errors([], [], [], []) == [f"{TPV}: destinations must be a mapping"]


# --- CI on every pull request: the repository's own onboarding files ----------------------


def test_repository_onboarding_files_are_consistent():
    assert duplicate_keys(_rendered_destinations(REPO_ROOT)) == []
    assert _raw_errors(REPO_ROOT) == []


def test_repository_secret_group_vars_holds_only_vault_files():
    assert plaintext_secret_files(REPO_ROOT) == []


def test_plaintext_check_handles_a_missing_directory(tmp_path):
    assert plaintext_secret_files(tmp_path) == []


def test_repository_secrets_are_not_decrypted_without_the_password():
    # The real Vault password is never in CI; only the throwaway fixture password is.
    assert all(path.read_bytes().startswith(b"$ANSIBLE_VAULT;") for path in secret_files(REPO_ROOT))


# --- validation details ------------------------------------------------------------------


def test_valid_raw_and_resolved_entries():
    assert byoc_onboarding_errors([_mq_user("a1")], [_runner("a1")], [["A", "a-pulsar"]]) == []
    assert (
        byoc_onboarding_errors(
            [_mq_user("a1", RESOLVED)],
            [_runner("a1", RESOLVED, host="127.0.0.1:5672")],
            [],
            resolved=True,
        )
        == []
    )


@pytest.mark.parametrize(
    ("users", "expected"),
    [
        ({"user": "x"}, ["group_vars/mq.yml: rabbitmq_users must be a list"]),
        (
            ["x"],
            [f"{MQ} entry 1: must be a mapping with 'user', 'password' and 'vhost'"],
        ),
        (
            [{**_mq_user("a"), "user": "pulsar-a"}],
            [f"{MQ} entry 1: 'user' must match galaxy_<id> with <id> in [a-z0-9_]+"],
        ),
        (
            [_mq_user("a"), _mq_user("a")],
            [f"{MQ} 'galaxy_a': duplicate user"],
        ),
        (
            [{**_mq_user("a"), "vhost": "/", "tags": "administrator"}],
            [
                f"{MQ} 'galaxy_a': unknown key 'tags'",
                f"{MQ} 'galaxy_a': 'vhost' must be /pulsar/galaxy_a",
            ],
        ),
        (
            [_mq_user("hpc")],
            [f"{MQ} 'galaxy_hpc': already used by src_galaxy_pulsar_amqp_configs or RabbitMQ"],
        ),
    ],
    ids=["not-a-list", "not-a-mapping", "bad-name", "duplicate", "vhost-and-key", "reserved"],
)
def test_mq_user_errors(users, expected):
    assert byoc_onboarding_errors(users, [], [], reserved=["galaxy", "galaxy_hpc"]) == expected


@pytest.mark.parametrize("password", [None, 123, "{{ rabbitmq_password_galaxy_b }}"])
def test_raw_mq_password_must_reference_its_own_vault_variable(password):
    user = {**_mq_user("a"), "password": password}

    [error] = byoc_onboarding_errors([user], [], [])
    assert "'password' must be '{{ rabbitmq_password_galaxy_a }}'" in error


@pytest.mark.parametrize("password", [None, "short", "has space in it", "a@b:c/d?e#f!"])
def test_resolved_mq_password_must_be_url_safe(password):
    [error] = byoc_onboarding_errors([_mq_user("a", password or "")], [], [], resolved=True)

    assert error.endswith("must be at least 10 characters from A-Z a-z 0-9 . _ ~ -")
    assert password is None or password not in error


@pytest.mark.parametrize(
    ("runners", "expected"),
    [
        ({}, [f"{JOB}: galaxy_jobconf.plugins must be a list"]),
        (
            [{"load": RUNNER_LOAD}],
            [f"{JOB} plugin 1: must be a mapping with 'id', 'load' and 'params'"],
        ),
        (
            [{**_runner("a"), "id": "pulsar_a"}],
            [f"{JOB} plugin 1: 'id' must match pulsar_eu_<id> with <id> in [a-z0-9_]+"],
        ),
        (
            [_runner("a"), _runner("a")],
            [f"{JOB} plugin 'pulsar_eu_a': duplicate id"],
        ),
        (
            [{**_runner("a"), "load": "galaxy.jobs.runners.local:LocalJobRunner", "workers": 2}],
            [
                f"{JOB} plugin 'pulsar_eu_a': unknown key 'workers'",
                f"{JOB} plugin 'pulsar_eu_a': 'load' must be {RUNNER_LOAD}",
            ],
        ),
        (
            [{**_runner("a"), "params": None}],
            [f"{JOB} plugin 'pulsar_eu_a': 'params' must be a mapping"],
        ),
        (
            [_with_params(_runner("a"), {"id": "x", "Manager": "y", 3: "z"})],
            [
                f"{JOB} plugin 'pulsar_eu_a': params key {key!r} is not allowed "
                "(lowercase letters, digits and '_', not id or load)"
                for key in ("id", "Manager", 3)
            ],
        ),
    ],
    ids=["not-a-list", "no-id", "bad-id", "duplicate", "load-and-key", "params", "param-keys"],
)
def test_runner_errors(runners, expected):
    assert byoc_onboarding_errors([_mq_user("a")], runners, []) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (None, "params.amqp_url must look like pyamqp://<user>:<password>@<host>:<port>/<vhost>"),
        ("amqp://galaxy_a@h//pulsar/galaxy_a", "params.amqp_url must look like"),
        (
            "pyamqp://galaxy_b:{{ rabbitmq_password_galaxy_a }}@h:1//pulsar/galaxy_a",
            "must use the RabbitMQ user galaxy_a",
        ),
        (
            "pyamqp://galaxy_a:{{ rabbitmq_password_galaxy_a }}@h:1//pulsar/galaxy_b",
            "must use the vhost /pulsar/galaxy_a",
        ),
        (
            "pyamqp://galaxy_a:Plaintext12345@h:1//pulsar/galaxy_a",
            "password in params.amqp_url must be",
        ),
    ],
    ids=["missing", "malformed", "user", "vhost", "plaintext"],
)
def test_raw_amqp_url_errors(url, message):
    [error] = byoc_onboarding_errors([_mq_user("a")], [_runner("a", amqp_url=url)], [])

    assert error.startswith(f"{JOB} plugin 'pulsar_eu_a': ")
    assert message in error
    assert "Plaintext12345" not in error


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("mq.galaxyproject.eu:5671", f"{FQDN}:5671"),
        (f"{FQDN}:5672", f"{FQDN}:5671"),
        ("127.0.0.1:5671", f"{FQDN}:5671"),
        (FQDN, f"{FQDN}:5671"),
        (f"{FQDN}:5671", f"{FQDN}:5671"),
        ("127.0.0.1:5672", "127.0.0.1:5672"),
    ],
    ids=["usegalaxy-eu", "wrong-port", "loopback-tls-port", "no-port", "ours", "loopback"],
)
def test_runners_are_pointed_at_this_rabbitmq(host, expected):
    [runner] = byoc_runners(
        [_runner("a", RESOLVED, host=host, galaxy_url="https://usegalaxy.eu")],
        f"{FQDN}:5671",
        GALAXY_URL,
        ENDPOINTS_HOSTS,
    )

    assert (
        runner["params"]["amqp_url"]
        == f"pyamqp://galaxy_a:{RESOLVED}@{expected}//pulsar/galaxy_a?ssl=1"
    )
    assert runner["params"]["galaxy_url"] == GALAXY_URL
    assert runner["params"]["manager"] == "production"


def test_no_runners_to_point():
    assert byoc_runners(None, f"{FQDN}:5671", GALAXY_URL) == []


def test_jinja_in_labels_is_rejected_in_ci_only():
    options = [["{{ 7 * 7 }}", "a-pulsar"], ["{% if x %}", "b-pulsar"]]

    assert byoc_onboarding_errors([], [], options) == [
        f"{PREFS}: option {n}: the label must not contain Jinja ({{{{ {{%)" for n in (1, 2)
    ]
    assert byoc_onboarding_errors([], [], [["49", "a-pulsar"]], resolved=True) == []


def test_destination_names_are_checked():
    destinations = {"pulsar_{{ x }}_tpv": {}, 3: {}, "ok_name": {}}

    assert byoc_onboarding_errors([], [], [], destinations) == [
        f"{TPV}: destination name 'pulsar_{{{{ x }}}}_tpv' must match [A-Za-z0-9_]+",
        f"{TPV}: destination name 3 must match [A-Za-z0-9_]+",
    ]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            "x",
            [f"{PREFS}: remote_resources options must be a list"],
        ),
        (
            [["A", "a"], ["B"], ["C", 3], ["D", "has space"]],
            [
                f'{PREFS}: option {n} must be ["<label>", <tag>] with <tag> in [A-Za-z0-9_.-]+'
                for n in (2, 3, 4)
            ],
        ),
        (
            [["A", "a"], ["B", "a"]],
            [f"{PREFS}: duplicate option a"],
        ),
    ],
    ids=["not-a-list", "malformed", "duplicate"],
)
def test_option_errors(options, expected):
    assert byoc_onboarding_errors([], [], options) == expected


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        (None, []),
        ({}, []),
        ({"preferences": None}, []),
        ({"preferences": []}, []),
        ({"preferences": {"distributed_compute": "x"}}, []),
        ({"preferences": {"distributed_compute": {"inputs": "x"}}}, []),
        (
            {
                "preferences": {
                    "distributed_compute": {
                        "inputs": [
                            "x",
                            {"name": "other", "options": [["O", "o"]]},
                            {
                                "name": "remote_resources",
                                "options": [["default", "None"], ["A", "a"], "bad"],
                            },
                            {"name": "remote_resources"},
                        ]
                    }
                }
            },
            [["A", "a"], "bad"],
        ),
    ],
    ids=["none", "empty", "null-section", "list-section", "bad-compute", "bad-inputs", "options"],
)
def test_preference_options(preferences, expected):
    assert preference_options(preferences) == expected


CHARS = "from A-Z a-z 0-9 . _ ~ -"
ONLY = "(only rabbitmq_password_galaxy_<id>)"


def test_secret_errors():
    users = [_mq_user("a"), _mq_user("b"), "junk"]
    secrets = {
        "rabbitmq_password_galaxy_a": RESOLVED,
        "rabbitmq_password_galaxy_c": "short",
        "galaxy_admin_api_key": RESOLVED,
        1: RESOLVED,
    }

    assert byoc_secret_errors(secrets, users) == [
        f"{SECRETS}: unexpected variable '1' {ONLY}",
        f"{SECRETS}: unexpected variable 'galaxy_admin_api_key' {ONLY}",
        f"{SECRETS}: rabbitmq_password_galaxy_b is missing (needed by group_vars/mq.yml)",
        f"{SECRETS}: rabbitmq_password_galaxy_c must be at least 10 characters {CHARS}",
    ]


def test_secret_errors_on_malformed_input():
    assert byoc_secret_errors(["x"], []) == [
        f"{SECRETS}: the decrypted files must be mappings of variables"
    ]
    assert byoc_secret_errors({}, {"not": "a list"}) == []


# --- playbook wiring ------------------------------------------------------------------------


def test_onboarding_is_empty_without_the_files():
    resolve = resolve_vars(VARS_FILES, {"src_galaxy_rabbitmq": "true"})

    assert resolve("_galaxy_byoc_mq_users") == []
    assert resolve("_galaxy_byoc_runners") == []
    assert resolve("_galaxy_byoc_preference_options") == []
    assert resolve("_galaxy_byoc_onboarding_errors") == []
    assert resolve("_galaxy_vault_password") == ""


def test_reserved_users_include_amqp_pulsars():
    resolve = resolve_vars(VARS_FILES, {"src_galaxy_pulsar_amqp_configs": "[{name: galaxy_hpc}]"})

    assert resolve("_galaxy_byoc_reserved_users") == ["galaxy", "guest", "galaxy_hpc"]


def test_amqp_endpoints_follow_the_rabbitmq_port():
    resolve = resolve_vars(VARS_FILES, {"workspace_fqdn": FQDN, "src_galaxy_rabbitmq_port": "5673"})

    assert resolve("_galaxy_byoc_amqp_endpoints") == [f"{FQDN}:5673", "127.0.0.1:5672"]


def test_unresolvable_vault_variable_is_an_error(tmp_path):
    root = _onboarded(tmp_path)
    resolve = resolve_vars(VARS_FILES, PARAMS, load_files(root))

    with pytest.raises(AnsibleError, match="rabbitmq_password_galaxy_egi01"):
        resolve("_galaxy_rabbitmq_fixed_passwords")


def test_playbook_loads_onboarding_before_rabbitmq():
    playbook = yaml.safe_load((REPO_ROOT / "galaxysrv.yml").read_text())[0]
    includes = [_include_file(t) for t in playbook["pre_tasks"]]

    assert includes.index("tasks/set_config.yml") < includes.index("tasks/byoc_onboarding.yml")
    assert includes.index("tasks/byoc_onboarding.yml") < includes.index("tasks/rabbitmq.yml")


def _include_file(task):
    include = task.get("ansible.builtin.include_tasks")
    return include.get("file") if isinstance(include, dict) else include


def test_onboarding_runs_with_any_tags():
    """The CD job (issue #22) runs `--tags rabbitmq,galaxy_config_files`."""
    playbook = yaml.safe_load((REPO_ROOT / "galaxysrv.yml").read_text())[0]
    [task] = [t for t in playbook["pre_tasks"] if _include_file(t) == "tasks/byoc_onboarding.yml"]

    assert task["tags"] == "always"
    assert task["ansible.builtin.include_tasks"]["apply"] == {"tags": "always"}


def test_set_config_loads_onboarding_vars():
    tasks = yaml.safe_load((REPO_ROOT / "tasks" / "set_config.yml").read_text())

    assert "vars/byoc_onboarding_vars.yml" in [t.get("ansible.builtin.include_vars") for t in tasks]


def test_onboarding_files_are_loaded_only_with_rabbitmq():
    load = _task("Load the BYOC onboarding files")
    include = _task(
        "Load group_vars/mq.yml, templates/galaxy/config/job_conf.yml and "
        "files/galaxy/config/user_preferences_extra_conf.yml"
    )

    assert load["when"] == "_galaxy_rabbitmq"
    assert {(i["file"], i["name"]) for i in include["loop"]} == {
        (path, name) for name, path in FILES.items()
    }
    assert include["ansible.builtin.include_vars"]["file"] == "{{ playbook_dir }}/{{ item.file }}"


def test_vault_password_file_is_root_only_and_never_logged():
    write = _task("Write the Vault password file")["ansible.builtin.copy"]

    assert _task("Write the Vault password file")["no_log"] is True
    assert (write["owner"], write["group"], write["mode"]) == ("root", "root", "0600")
    assert write["dest"] == "{{ _galaxy_vault_secret_path }}"
    resolve = resolve_vars(VARS_FILES, {"src_galaxy_vault_password": "pw"})
    assert resolve("_galaxy_vault_secret_path") == "/etc/galaxy/vault_password"
    assert resolve("_galaxy_vault_password") == "pw"


def test_decryption_uses_the_tested_command_and_no_log():
    task = _task("Decrypt secret_group_vars/*.yml")
    argv = task["ansible.builtin.command"]["argv"]

    assert argv == [
        "{{ ansible_playbook_python }}",
        *decrypt_command(Path("{{ _galaxy_vault_secret_path }}"), Path("{{ item }}"))[1:],
    ]
    assert task["no_log"] is True
    assert task["changed_when"] is False
    secret_tasks = ["Collect the BYOC secrets", "Define the BYOC secret variables"]
    assert all(_task(name)["no_log"] is True for name in secret_tasks)


def test_secrets_are_validated_before_the_files():
    names = [task["name"] for task in _tasks(TASKS_FILE)]

    assert names.index("Validate the BYOC secrets") < names.index(
        "Define the BYOC secret variables"
    )
    assert names.index("Define the BYOC secret variables") < names.index(
        "Validate the BYOC onboarding files"
    )
    assert names[-1] == "Validate the BYOC onboarding files"


def test_vault_password_falls_back_to_the_existing_file():
    """CD runs (#22) pass no src_galaxy_vault_password; the file from the SRC deploy is used."""
    stat = _task("Check for the Vault password file")
    check = _task("Require the Vault password for secret_group_vars")["ansible.builtin.assert"]

    assert stat["ansible.builtin.stat"]["path"] == "{{ _galaxy_vault_secret_path }}"
    assert "_galaxy_vault_secret_stat.stat.exists" in check["that"]
    assert "_galaxy_vault_password | length > 0" in check["that"]
    write = next(t for t in _tasks(TASKS_FILE) if t["name"] == "Write the Ansible Vault password")
    assert write["when"] == "_galaxy_vault_password | length > 0"


def test_decryption_failure_is_reported_without_secrets():
    decrypt = _task("Decrypt secret_group_vars/*.yml")
    check = _task("Check that every secret_group_vars file was decrypted")

    assert decrypt["failed_when"] is False
    assert "no_log" not in check
    message = check["ansible.builtin.assert"]["fail_msg"]
    assert "stdout" not in message
    assert "stderr" not in message


# --- the task file, run with ansible-playbook (paths that need no root) ----------------------

CHECK_PLAYBOOK = """\
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.include_vars: vars/internal_vars.yml
    - ansible.builtin.include_vars: vars/src_galaxy_vars.yml
    - ansible.builtin.include_vars: vars/rabbitmq_vars.yml
    - ansible.builtin.include_vars: vars/byoc_onboarding_vars.yml
    - ansible.builtin.include_tasks: tasks/byoc_onboarding.yml
    - ansible.builtin.debug:
        msg: "RUNNERS {{ _galaxy_byoc_runners | map(attribute='id') | join(',') }}"
"""


def _run_playbook(tmp_path, root, password_file, **extra_vars):
    for directory in ("filter_plugins", "vars", "tasks"):
        shutil.copytree(REPO_ROOT / directory, root / directory)
    (root / "check.yml").write_text(CHECK_PLAYBOOK)
    variables = {
        "workspace_fqdn": FQDN,
        "src_galaxy_rabbitmq": "true",
        "_galaxy_vault_secret_path": str(password_file),
        "ansible_python_interpreter": sys.executable,
        **extra_vars,
    }
    env = {**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp")}
    env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
    return subprocess.run(  # noqa: S603 - fixed command, paths from the test suite
        [
            sys.executable,
            "-m",
            "ansible.cli.playbook",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-v",
            "check.yml",
            "-e",
            json.dumps(variables),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cd_run_uses_the_existing_vault_password_file(tmp_path):
    result = _run_playbook(tmp_path, _onboarded(tmp_path), VAULT_PASSWORD_FILE)

    assert result.returncode == 0, result.stdout[-2000:]
    assert "RUNNERS pulsar_eu_egi01,pulsar_eu_eosc01" in result.stdout
    assert "Throwaway" not in result.stdout + result.stderr


def test_missing_vault_password_is_reported(tmp_path):
    result = _run_playbook(tmp_path, _onboarded(tmp_path), tmp_path / "absent")

    assert result.returncode != 0
    assert "src_galaxy_vault_password is empty and" in result.stdout
    assert "does not exist" in result.stdout


def test_wrong_vault_password_is_reported_without_secrets(tmp_path):
    wrong = tmp_path / "wrong"
    wrong.write_text("not-the-password\n")
    result = _run_playbook(tmp_path, _onboarded(tmp_path), wrong)

    assert result.returncode != 0
    assert "Could not decrypt pulsar.yml in secret_group_vars/" in result.stdout
    assert "the password is wrong" in result.stdout
    assert "Throwaway" not in result.stdout + result.stderr
    assert "not-the-password" not in result.stdout + result.stderr


def test_repository_files_run_without_any_vault(tmp_path):
    root = copy_repository_files(tmp_path / "repo")
    result = _run_playbook(tmp_path, root, tmp_path / "absent")

    assert result.returncode == 0, result.stdout[-2000:]
    assert "RUNNERS \n" in result.stdout or '"msg": "RUNNERS "' in result.stdout
