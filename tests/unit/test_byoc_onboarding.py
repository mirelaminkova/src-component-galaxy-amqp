"""EU-layout BYOC onboarding files (issue #21): contract tests and their validation.

The contract: an onboarding pull request shaped like usegalaxy-eu/infrastructure-playbook
#1384 and #872 (one endpoint added to the onboarding files, see
filter_plugins/byoc_onboarding.py) must deploy a working endpoint. The fixtures in
tests/fixtures/byoc_onboarding/ apply two such diffs to a copy of the files with a
throwaway Vault password; the tests load the result the way tasks/byoc_onboarding.yml does.
"""

import difflib
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible.errors import AnsibleError

from filter_plugins.byoc_onboarding import (
    RUNNER_LOAD,
    byoc_onboarding_errors,
    byoc_secret_errors,
    preference_options,
)
from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.onboarding import (
    FILES,
    apply_diffs,
    decrypt_command,
    decrypt_secrets,
    load_files,
    plaintext_secret_files,
    secret_files,
)
from tests.helpers.render import render_yaml

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "byoc_onboarding"
VAULT_PASSWORD_FILE = FIXTURES / "vault-password.txt"
DIFFS = [FIXTURES / "onboard-egi01.diff", FIXTURES / "onboard-eosc01.diff"]
VARS_FILES = [
    "internal_vars.yml",
    "src_galaxy_vars.yml",
    "rabbitmq_vars.yml",
    "byoc_onboarding_vars.yml",
]
FQDN = "galaxy.example.org"
PARAMS = {"workspace_fqdn": FQDN, "src_galaxy_rabbitmq": "true"}
TASKS_FILE = REPO_ROOT / "tasks" / "byoc_onboarding.yml"
ENDPOINTS = [f"{FQDN}:5671", "127.0.0.1:5672"]
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


def _onboarded(tmp_path, diffs=DIFFS):
    return apply_diffs(FIXTURES / "base", diffs, tmp_path / "repo")


def _resolver(root, **params):
    templated = {**load_files(root), **decrypt_secrets(root, VAULT_PASSWORD_FILE)}
    return resolve_vars(VARS_FILES, {**PARAMS, **params}, templated)


def _raw_errors(root, destinations=None):
    files = load_files(root)
    return byoc_onboarding_errors(
        files["_galaxy_onboarding_mq"]["rabbitmq_users"],
        files["_galaxy_onboarding_jobconf"]["galaxy_jobconf"]["plugins"],
        preference_options(files["_galaxy_onboarding_preferences"]),
        destinations,
    )


def _tasks(path):
    for task in yaml.safe_load(path.read_text()):
        yield task
        yield from (_tasks_in(task))


def _tasks_in(task):
    for key in ("block", "rescue", "always"):
        yield from task.get(key, [])


def _task(name):
    return next(task for task in _tasks(TASKS_FILE) if task["name"] == name)


# --- contract: onboarding diffs modelled on #872 (egi01) and #1384 (eosc01) ---------------


def test_onboarding_diffs_touch_the_eu_files_like_the_example_prs():
    expected = {*FILES.values(), "secret_group_vars/pulsar.yml"}
    for diff in DIFFS:
        touched = set(re.findall(r"^\+\+\+ b/(\S+)$", diff.read_text(), flags=re.MULTILINE))
        assert touched == expected


def test_fixture_base_has_the_layout_of_the_repository_files():
    base, repo = load_files(FIXTURES / "base"), load_files(REPO_ROOT)

    assert set(base["_galaxy_onboarding_mq"]) == set(repo["_galaxy_onboarding_mq"])
    assert base["_galaxy_onboarding_jobconf"].keys() == repo["_galaxy_onboarding_jobconf"].keys()
    assert preference_options(repo["_galaxy_onboarding_preferences"]) == []
    base_form = base["_galaxy_onboarding_preferences"]["preferences"]["distributed_compute"]
    repo_form = repo["_galaxy_onboarding_preferences"]["preferences"]["distributed_compute"]
    assert {**base_form["inputs"][0], "options": None} == {
        **repo_form["inputs"][0],
        "options": None,
    }


def test_onboarded_files_pass_the_ci_checks(tmp_path):
    root = _onboarded(tmp_path)

    assert _raw_errors(root) == []
    assert plaintext_secret_files(root) == []


def test_onboarded_secrets_decrypt_with_the_playbook_command(tmp_path):
    root = _onboarded(tmp_path)
    secrets = decrypt_secrets(root, VAULT_PASSWORD_FILE)

    assert sorted(secrets) == [
        "rabbitmq_password_galaxy_egi01",
        "rabbitmq_password_galaxy_eosc01",
        "rabbitmq_password_galaxy_sk01",
    ]
    files = load_files(root)
    assert byoc_secret_errors(secrets, files["_galaxy_onboarding_mq"]["rabbitmq_users"]) == []


def test_onboarded_playbook_vars_resolve(tmp_path):
    resolve = _resolver(_onboarded(tmp_path))
    secrets = decrypt_secrets(tmp_path / "repo", VAULT_PASSWORD_FILE)

    assert resolve("_galaxy_byoc_onboarding_errors") == []
    assert resolve("_galaxy_rabbitmq_vhosts") == [
        "/pulsar/galaxy_sk01",
        "/pulsar/galaxy_egi01",
        "/pulsar/galaxy_eosc01",
    ]
    assert resolve("_galaxy_rabbitmq_fixed_passwords") == {
        user: secrets[f"rabbitmq_password_{user}"]
        for user in ("galaxy_sk01", "galaxy_egi01", "galaxy_eosc01")
    }


def test_onboarded_rabbitmq_users_are_limited_to_their_vhost(tmp_path):
    users = {
        u["name"]: u for u in _resolver(_onboarded(tmp_path))("_galaxy_rabbitmq_users_resolved")
    }

    for endpoint in ("sk01", "egi01", "eosc01"):
        user = f"galaxy_{endpoint}"
        assert [p["vhost"] for p in users[user]["permissions"]] == [f"/pulsar/{user}"]
        assert users[user]["tags"] == ""
        assert f"/pulsar/{user}" in [p["vhost"] for p in users["galaxy"]["permissions"]]
    # No password ever travels with the user list (it is looped over without no_log elsewhere).
    assert all(set(user) == {"name", "tags", "permissions"} for user in users.values())


def test_onboarded_job_config_has_the_runners(tmp_path):
    resolve = _resolver(_onboarded(tmp_path))
    secrets = decrypt_secrets(tmp_path / "repo", VAULT_PASSWORD_FILE)
    conf = render_yaml(
        "galaxy/config/job_conf.yml.j2",
        {
            **_job_conf_vars(),
            "_galaxy_byoc_runners": resolve("_galaxy_byoc_runners"),
        },
    )

    eosc = conf["runners"]["pulsar_eu_eosc01"]
    password = secrets["rabbitmq_password_galaxy_eosc01"]
    assert eosc == {
        "load": RUNNER_LOAD,
        "amqp_url": f"pyamqp://galaxy_eosc01:{password}@{FQDN}:5671//pulsar/galaxy_eosc01?ssl=1",
        "galaxy_url": GALAXY_URL,
        "manager": "production",
        "amqp_acknowledge": "true",
        "amqp_ack_republish_time": 300,
        "amqp_consumer_timeout": 2.0,
        "amqp_publish_retry": "true",
        "amqp_publish_retry_max_retries": 60,
    }
    assert set(conf["runners"]) == {
        "local",
        *(f"pulsar_eu_{i}" for i in ("sk01", "egi01", "eosc01")),
    }


def test_onboarded_endpoints_are_in_the_dropdown(tmp_path):
    resolve = _resolver(_onboarded(tmp_path))
    prefs = render_yaml(
        "galaxy/config/user_preferences_extra_conf.yml.j2",
        {
            "_galaxy_ibridges_enabled": False,
            "_galaxy_byoc_pulsar": False,
            "_galaxy_remote_destinations": [{"id": "amqp_hpc", "label": "HPC"}],
            "_galaxy_byoc_preference_options": resolve("_galaxy_byoc_preference_options"),
        },
    )

    assert prefs["preferences"]["distributed_compute"]["inputs"][0]["options"] == [
        ["default - Galaxy will decide where to put your job", "None"],
        ["HPC", "amqp_hpc"],
        ["Bratislava (Slovakia) - IISAS", "sk-pulsar"],
        ["Bari (Italy) - INFN", "egi-pulsar"],
        ["EOSC EU Node 01", "eosc-eu-node-01-pulsar"],
    ]


def test_wrong_vault_password_fails_decryption(tmp_path):
    root = _onboarded(tmp_path)
    wrong = tmp_path / "wrong"
    wrong.write_text("not-the-password\n")

    with pytest.raises(subprocess.CalledProcessError):
        decrypt_secrets(root, wrong)


def _job_conf_vars():
    return {
        "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
        "apptainer_volumes": ["$galaxy_root:ro"],
        "_galaxy_use_interactive_tools": False,
        "_galaxy_default_dispatcher": "default_dispatcher",
        "_galaxy_jobs_default": "singularity",
        "_galaxy_has_pulsar": False,
        "_galaxy_pulsar_configs_raw": [],
        "_galaxy_pulsar_tool_mappings": [],
        "_galaxy_use_dispatcher": False,
        "_galaxy_pulsar_amqp_configs": [],
    }


# --- negative: broken onboarding pull requests fail pytest -------------------------------


def _append_diff(tmp_path, name, path, lines):
    """A unified diff that appends ``lines`` to ``path`` of the onboarded files (or creates it)."""
    target = _onboarded(tmp_path / "before") / path
    old = target.read_text().splitlines(keepends=True) if target.exists() else []
    source = f"a/{path}" if old else "/dev/null"
    diff = "".join(difflib.unified_diff(old, old + lines, source, f"b/{path}"))
    out = tmp_path / name
    out.write_text(diff)
    return out


RUNNER_WITHOUT_MQ_USER = [
    "    - id: pulsar_eu_ghost01\n",
    "      load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner\n",
    "      params:\n",
    '        amqp_url: "pyamqp://galaxy_ghost01:{{ rabbitmq_password_galaxy_ghost01 }}'
    '@{{ _galaxy_server_fqdn }}:{{ _galaxy_rabbitmq_port }}//pulsar/galaxy_ghost01?ssl=1"\n',
    '        galaxy_url: "{{ _galaxy_infrastructure_url }}"\n',
]
PLAINTEXT_IN_MQ = [
    "  - user: galaxy_leak01\n",
    "    password: Leak01PlaintextPassword\n",
    "    vhost: /pulsar/galaxy_leak01\n",
]
PLAINTEXT_SECRET_FILE = ["rabbitmq_password_galaxy_leak01: Leak01PlaintextPassword\n"]


def test_runner_without_mq_user_fails(tmp_path):
    diff = _append_diff(
        tmp_path, "ghost.diff", "templates/galaxy/config/job_conf.yml", RUNNER_WITHOUT_MQ_USER
    )
    root = _onboarded(tmp_path, [*DIFFS, diff])

    assert _raw_errors(root) == [
        f"{JOB} plugin 'pulsar_eu_ghost01': no RabbitMQ user galaxy_ghost01 in group_vars/mq.yml"
    ]


def test_plaintext_password_in_mq_yml_fails(tmp_path):
    diff = _append_diff(tmp_path, "leak.diff", "group_vars/mq.yml", PLAINTEXT_IN_MQ)
    errors = _raw_errors(_onboarded(tmp_path, [*DIFFS, diff]))

    assert errors == [
        f"{MQ} 'galaxy_leak01': 'password' must be '{{{{ rabbitmq_password_galaxy_leak01 }}}}' "
        "(the value belongs in secret_group_vars/pulsar.yml)"
    ]
    assert "Leak01PlaintextPassword" not in " ".join(errors)


def test_plaintext_file_in_secret_group_vars_fails(tmp_path):
    diff = _append_diff(tmp_path, "leak.diff", "secret_group_vars/leak.yml", PLAINTEXT_SECRET_FILE)
    root = _onboarded(tmp_path, [*DIFFS, diff])

    assert plaintext_secret_files(root) == ["secret_group_vars/leak.yml"]


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
            endpoints=ENDPOINTS,
            galaxy_url=GALAXY_URL + "/",
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
            [f"{MQ} 'pulsar-a': 'user' must match galaxy_<id> with <id> in [a-z0-9_]+"],
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
    [error] = byoc_onboarding_errors([_mq_user("a", password or "")], [], [], endpoints=ENDPOINTS)

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
            [f"{JOB} plugin 'pulsar_a': 'id' must match pulsar_eu_<id> with <id> in [a-z0-9_]+"],
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
            [_runner("a", galaxy_url=None)],
            [f"{JOB} plugin 'pulsar_eu_a': params.galaxy_url must be this Galaxy's URL"],
        ),
    ],
    ids=["not-a-list", "no-id", "bad-id", "duplicate", "load-and-key", "params", "galaxy-url"],
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
    "host", ["mq.galaxyproject.eu:5671", f"{FQDN}:5672", "127.0.0.1:5671", FQDN]
)
def test_resolved_amqp_url_must_point_at_this_rabbitmq(host):
    secret = RESOLVED
    errors = byoc_onboarding_errors(
        [_mq_user("a", secret)], [_runner("a", secret, host=host)], [], endpoints=ENDPOINTS
    )

    assert errors == [
        f"{JOB} plugin 'pulsar_eu_a': params.amqp_url must point at "
        f"this Galaxy's RabbitMQ ({FQDN}:5671 or 127.0.0.1:5672)"
    ]


def test_resolved_galaxy_url_must_be_this_galaxy():
    secret = RESOLVED
    errors = byoc_onboarding_errors(
        [_mq_user("a", secret)],
        [_runner("a", secret)],
        [],
        endpoints=ENDPOINTS,
        galaxy_url="https://other.example.org",
    )

    assert errors == [f"{JOB} plugin 'pulsar_eu_a': params.galaxy_url must be this Galaxy's URL"]


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

    with pytest.raises(AnsibleError, match="rabbitmq_password_galaxy_sk01"):
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
