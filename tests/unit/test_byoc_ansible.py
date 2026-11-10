"""Ansible side of "bring your own Pulsar" (issue #6): variables, tasks and templates."""

import configparser
import importlib.util
import json

import pytest
import yaml

from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.render import render_template, render_yaml

VARS_FILES = [
    "internal_vars.yml",
    "src_galaxy_vars.yml",
    "rabbitmq_vars.yml",
    "byoc_vars.yml",
    "galaxy_vars.yml",
]
SCRIPT = REPO_ROOT / "files" / "byoc" / "galaxy_byoc_sync.py"
TASKS = REPO_ROOT / "tasks" / "byoc_pulsar.yml"
TASKS_OFF = REPO_ROOT / "tasks" / "byoc_pulsar_off.yml"


def _resolve(**params):
    return resolve_vars(
        VARS_FILES,
        {"workspace_fqdn": "galaxy.example.org", "src_galaxy_byoc_pulsar": "true", **params},
    )


def _script_settings():
    spec = importlib.util.spec_from_file_location("byoc_sync_settings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SETTINGS


def _yaml(path):
    return yaml.safe_load(path.read_text())


# --- parameters -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 20), ("", 20), ("20", 20), ("5", 5), (" 7 ", 7), ("0", 0), (0, 0), (3, 3)],
)
def test_byoc_pulsar_max(value, expected):
    params = {} if value is None else {"src_galaxy_byoc_pulsar_max": value}
    resolve = _resolve(**params)

    assert resolve("_galaxy_byoc_pulsar_max") == expected
    assert not resolve("_galaxy_byoc_pulsar_max_error")


@pytest.mark.parametrize("value", ["abc", "-1", "2.5", "1e3"])
def test_invalid_byoc_pulsar_max_is_rejected(value):
    error = _resolve(src_galaxy_byoc_pulsar_max=value)("_galaxy_byoc_pulsar_max_error")

    assert error.startswith("src_galaxy_byoc_pulsar_max must be a whole number")
    assert f"'{value}'" in error


@pytest.mark.parametrize(("byoc", "expected"), [("true", True), ("false", False)])
def test_byoc_alone_routes_jobs_through_the_dispatcher(byoc, expected):
    assert _resolve(src_galaxy_byoc_pulsar=byoc)("_galaxy_use_dispatcher") is expected


def test_parameters_file_documents_byoc_parameters():
    params = _yaml(REPO_ROOT / "parameters.yml")

    assert params["src_galaxy_byoc_pulsar"] == "false"
    assert params["src_galaxy_byoc_pulsar_max"] == "20"


# --- set_config.yml assertions ---------------------------------------------------------


def _set_config():
    tasks = _yaml(REPO_ROOT / "tasks" / "set_config.yml")
    names = [task["name"] for task in tasks]
    asserts = {
        t["name"]: t["ansible.builtin.assert"] for t in tasks if "ansible.builtin.assert" in t
    }
    return names, asserts


def _evaluate(resolve, expression):
    """Evaluate an assert's ``that`` with the resolved variables (as Ansible would)."""
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    names = [
        "_galaxy_rabbitmq",
        "_galaxy_byoc_pulsar",
        "_galaxy_byoc_pulsar_max_error",
        "_galaxy_pulsar_tool_mappings",
    ]
    templar = Templar(loader=DataLoader(), variables={n: resolve(n) for n in names})
    return templar.template(trust_as_template("{{ " + expression + " }}"))


def test_byoc_variables_are_loaded_before_they_are_asserted():
    names, _ = _set_config()

    loaded = names.index("Load BYOC Pulsar variables")
    assert loaded > names.index("Load RabbitMQ variables")
    for check in (
        "BYOC Pulsar requires RabbitMQ",
        "Validate src_galaxy_byoc_pulsar_max",
        "Tool mappings must not target BYOC environments",
    ):
        assert names.index(check) > loaded


@pytest.mark.parametrize(
    ("rabbitmq", "byoc", "ok"),
    [
        ("false", "false", True),
        ("true", "true", True),
        ("true", "false", True),
        ("", "true", False),
    ],
)
def test_byoc_requires_rabbitmq(rabbitmq, byoc, ok):
    _, asserts = _set_config()
    check = asserts["BYOC Pulsar requires RabbitMQ"]
    resolve = _resolve(src_galaxy_rabbitmq=rabbitmq, src_galaxy_byoc_pulsar=byoc)

    assert _evaluate(resolve, check["that"]) is ok
    assert "src_galaxy_rabbitmq" in check["fail_msg"]


@pytest.mark.parametrize(("value", "ok"), [("20", True), ("x", False)])
def test_byoc_max_is_asserted(value, ok):
    _, asserts = _set_config()
    check = asserts["Validate src_galaxy_byoc_pulsar_max"]

    assert _evaluate(_resolve(src_galaxy_byoc_pulsar_max=value), check["that"]) is ok


@pytest.mark.parametrize(
    ("mappings", "ok"),
    [
        ("", True),
        ("[{tool_id: bwa, environment: amqp_hpc}]", True),
        ("[{tool_id: bwa}]", True),
        ("[{tool_id: bwa, environment: byoc_alice}]", False),
    ],
)
def test_tool_mappings_cannot_target_byoc_environments(mappings, ok):
    _, asserts = _set_config()
    check = asserts["Tool mappings must not target BYOC environments"]

    assert _evaluate(_resolve(src_galaxy_pulsar_tool_mappings=mappings), check["that"]) is ok


def test_static_amqp_pulsars_cannot_take_byoc_names():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs="[{name: byoc_x}, {name: byocx}]")

    assert resolve("_galaxy_pulsar_amqp_config_errors") == [
        "entry 1: 'name' must not start with 'byoc_' (reserved for BYOC Pulsars)"
    ]


# --- job config files -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("byoc", "dest"),
    [
        ("true", "/srv/galaxy/config/job_conf_base.yml"),
        ("false", "/srv/galaxy/config/job_conf.yml"),
    ],
)
def test_ansible_renders_the_base_job_config_when_byoc_is_on(byoc, dest):
    resolve = _resolve(src_galaxy_byoc_pulsar=byoc)
    templates = {t["src"]: t["dest"] for t in resolve("galaxy_config_templates")}

    assert templates["templates/galaxy/config/job_conf.yml.j2"] == dest
    # Galaxy always reads job_conf.yml; with BYOC galaxy-byoc-sync writes it.
    galaxy_vars = _yaml(REPO_ROOT / "vars" / "galaxy_vars.yml")
    assert galaxy_vars["galaxy_configuration"]["galaxy"]["job_config_file"] == (
        "{{ job_config_file }}"
    )
    assert resolve("job_config_file") == "/srv/galaxy/config/job_conf.yml"


# --- galaxy-byoc-sync configuration --------------------------------------------------------


def _config(**params):
    resolve = _resolve(**params)
    text = render_template(
        "byoc/config.json.j2",
        {"_galaxy_byoc_sync_settings": resolve("_galaxy_byoc_sync_settings")},
    )
    return json.loads(text)


def test_sync_config_has_exactly_the_settings_the_script_reads():
    assert set(_config()) == set(_script_settings())


def test_sync_config_values():
    config = _config(src_galaxy_byoc_pulsar_max="7")

    assert config["max_endpoints"] == 7
    assert config["amqp_local_port"] == 5672
    assert config["node"] == "rabbit@localhost"
    assert config["galaxy_password_file"] == "/etc/galaxy-rabbitmq/galaxy.pass"  # noqa: S105 - a path
    assert config["base_job_config"] == "/srv/galaxy/config/job_conf_base.yml"
    assert config["job_config"] == "/srv/galaxy/config/job_conf.yml"
    assert config["job_config_group"] == "galaxy"
    assert config["state_file"] == "/var/lib/galaxy-byoc-sync/state.json"
    assert 0 < config["min_restart_seconds"] <= 15 * 60
    assert config["restart_command"] == ["/usr/local/bin/galaxyctl", "graceful"]
    assert config["runner"]["galaxy_url"] == "https://galaxy.example.org"
    assert config["environment"]["jobs_directory"] == "/mnt/pulsar/files/staging"


def test_sync_reads_the_database_as_galaxy_over_the_socket():
    command = _config()["database_command"]

    assert command[:4] == ["/usr/sbin/runuser", "-u", "galaxy", "--"]
    assert command[4] == "/usr/bin/psql"
    assert command[command.index("-h") + 1] == "/var/run/postgresql"
    assert command[command.index("-d") + 1] == "galaxy"
    assert "-X" in command  # no ~/.psqlrc


def test_sync_commands_use_absolute_paths():
    config = _config()

    for command in (
        config["rabbitmqctl"],
        config["restart_command"][0],
        *config["database_command"][:5:4],
    ):
        assert command.startswith("/")


def test_byoc_runner_matches_the_amqp_pulsar_runners():
    from tests.unit.test_pulsar_amqp import _job_conf

    amqp = _job_conf(src_galaxy_pulsar_amqp_configs="[{name: hpc}]")
    runner = dict(amqp["runners"]["pulsar_mq_hpc"])
    environment = dict(amqp["execution"]["environments"]["amqp_hpc"])
    config = _config()

    del runner["load"], runner["amqp_url"], environment["runner"]
    del environment["docker_enabled"], environment["singularity_enabled"]
    assert config["runner"] == runner
    assert config["environment"] == environment


# --- systemd units -------------------------------------------------------------------------

UNIT_VARS = {
    "_galaxy_byoc_sync_script": "/usr/local/sbin/galaxy-byoc-sync",
    "_galaxy_byoc_sync_config_dir": "/etc/galaxy-byoc-sync",
    "_galaxy_byoc_sync_interval_minutes": 15,
}


def _unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(render_template(f"byoc/{name}.j2", UNIT_VARS))
    return parser


def test_service_runs_the_sync_once_per_activation():
    service = _unit("galaxy-byoc-sync.service")

    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["ExecStart"] == (
        "/usr/local/sbin/galaxy-byoc-sync --config /etc/galaxy-byoc-sync/config.json"
    )
    assert service["Service"]["UMask"] == "0077"
    assert "rabbitmq-server.service" in service["Unit"]["After"]
    assert "postgresql.service" in service["Unit"]["After"]
    assert "Install" not in service  # started by the timer only


def test_timer_runs_every_interval():
    timer = _unit("galaxy-byoc-sync.timer")

    assert timer["Timer"]["OnUnitActiveSec"] == "15min"
    assert timer["Timer"]["OnBootSec"]
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_units_and_vars_agree_on_the_names():
    resolve = _resolve()

    assert resolve("_galaxy_byoc_sync_script") == UNIT_VARS["_galaxy_byoc_sync_script"]
    assert resolve("_galaxy_byoc_sync_config_dir") == UNIT_VARS["_galaxy_byoc_sync_config_dir"]
    assert resolve("_galaxy_byoc_sync_interval_minutes") == 15
    assert resolve("_galaxy_byoc_sync_unit") == "galaxy-byoc-sync"


# --- tasks -----------------------------------------------------------------------------


def _task(tasks, name):
    [task] = [t for t in tasks if t["name"] == name]
    return task


def test_sync_files_are_root_only():
    tasks = _yaml(TASKS)

    dirs = _task(tasks, "Create root-only galaxy-byoc-sync directories")["ansible.builtin.file"]
    assert (dirs["owner"], dirs["mode"]) == ("root", "0700")
    script = _task(tasks, "Install galaxy-byoc-sync")["ansible.builtin.copy"]
    assert script["src"] == "files/byoc/galaxy_byoc_sync.py"
    assert (script["owner"], script["mode"]) == ("root", "0755")
    config = _task(tasks, "Configure galaxy-byoc-sync")["ansible.builtin.template"]
    assert (config["owner"], config["mode"]) == ("root", "0600")


def test_deploy_syncs_once_and_lets_the_playbook_restart_galaxy():
    names = [t["name"] for t in _yaml(TASKS)]
    run = _task(_yaml(TASKS), "Sync BYOC endpoints and write job_conf.yml")

    assert run["ansible.builtin.command"]["argv"] == [
        "{{ _galaxy_byoc_sync_script }}",
        "--config",
        "{{ _galaxy_byoc_sync_config_dir }}/config.json",
        "--no-restart",
    ]
    assert "galaxy restart required" in run["changed_when"]
    assert run["notify"] == "restart galaxy"
    assert names.index("Sync BYOC endpoints and write job_conf.yml") > names.index(
        "Configure galaxy-byoc-sync"
    )
    assert names[-1] == "Enable the galaxy-byoc-sync timer"


def test_restart_marker_matches_the_script():
    spec = importlib.util.spec_from_file_location("byoc_sync_marker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    run = _task(_yaml(TASKS), "Sync BYOC endpoints and write job_conf.yml")
    assert f"'{module.RESTART_REQUIRED}'" in run["changed_when"]


def test_timer_is_enabled_and_restarted_when_units_change():
    timer = _task(_yaml(TASKS), "Enable the galaxy-byoc-sync timer")[
        "ansible.builtin.systemd_service"
    ]

    assert timer["enabled"] is True
    assert timer["name"] == "{{ _galaxy_byoc_sync_unit }}.timer"
    assert "restarted" in timer["state"]


def test_switching_byoc_off_stops_the_timer():
    tasks = _yaml(TASKS_OFF)
    stop = _task(tasks, "Stop and disable the galaxy-byoc-sync timer")

    assert stop["ansible.builtin.systemd_service"]["enabled"] is False
    assert stop["ansible.builtin.systemd_service"]["state"] == "stopped"
    assert stop["when"] == "_galaxy_byoc_timer.stat.exists"


def test_playbook_runs_byoc_after_the_galaxy_role_and_before_galaxy_starts():
    [play] = _yaml(REPO_ROOT / "galaxysrv.yml")
    roles = [r["role"] for r in play["roles"]]
    tasks = play["tasks"]
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    names = [t["name"] for t in tasks]

    assert "galaxyproject.galaxy" in roles  # tasks run after all roles
    on = tasks[includes.index("tasks/byoc_pulsar.yml")]
    off = tasks[includes.index("tasks/byoc_pulsar_off.yml")]
    assert on["when"] == "_galaxy_byoc_pulsar"
    assert off["when"] == "not _galaxy_byoc_pulsar"
    assert includes.index("tasks/byoc_pulsar.yml") < names.index(
        "Ensure services enabled and started"
    )
    handlers = [h.get("listen") for h in play["handlers"]]
    assert "restart galaxy" in handlers


# --- preferences form ----------------------------------------------------------------------

PREFS_VARS = {
    "_galaxy_ibridges_enabled": False,
    "_galaxy_remote_destinations": [],
    "_galaxy_byoc_pulsar": True,
    "_galaxy_server_fqdn": "galaxy.example.org",
    "_galaxy_rabbitmq_port": 5671,
    "_galaxy_byoc_jobs_directory": "/mnt/pulsar/files/staging",
    "_galaxy_byoc_sync_interval_minutes": 15,
}


def _prefs(**overrides):
    return render_yaml(
        "galaxy/config/user_preferences_extra_conf.yml.j2", {**PREFS_VARS, **overrides}
    )["preferences"]


def test_owner_selects_their_own_pulsar_under_distributed_compute():
    [select] = _prefs()["distributed_compute"]["inputs"]

    assert select["options"][-1] == ["My own Pulsar (Bring your own Pulsar, set up below)", "byoc"]


def test_form_tells_the_user_how_to_connect():
    description = _prefs()["byoc_pulsar"]["description"]

    assert (
        "pyamqp://byoc_USERNAME:PASSWORD@galaxy.example.org:5671//pulsar/byoc_USERNAME?ssl=1"
        in description
    )
    assert "/mnt/pulsar/files/staging" in description
    assert "15 minutes" in description
    assert "My own Pulsar" in description


def test_form_states_the_validation_rules():
    inputs = {i["name"]: i for i in _prefs()["byoc_pulsar"]["inputs"]}

    assert "3-32" in inputs["username"]["label"]
    assert "10-128 letters and digits" in inputs["password"]["label"]
