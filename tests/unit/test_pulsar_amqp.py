"""Remote Pulsars over AMQP with PulsarMQJobRunner (issue #4, src_galaxy_pulsar_amqp_configs).

Variables are evaluated by ansible-core's Templar from the SRC string parameters, so the
filter plugin, the vars files and the templates are tested together.
"""

import re

import pytest
import yaml
from ansible.errors import AnsibleError

from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.render import render_template, render_yaml

VARS_FILES = [
    "internal_vars.yml",
    "src_galaxy_vars.yml",
    "rabbitmq_vars.yml",
    "byoc_onboarding_vars.yml",
]
TWO = "[{name: hpc, label: Our HPC, docker_enabled: true}, {name: eu, password: Abcdefghijkl1234}]"
GALAXY_PW = "GalaxyPw0123456789"
HPC_PW = "HpcPassw0rd123456"
PASSWORDS = {"galaxy": GALAXY_PW, "hpc": HPC_PW, "eu": "Abcdefghijkl1234"}


def _resolve(**params):
    return resolve_vars(VARS_FILES, {"workspace_fqdn": "galaxy.example.org", **params})


# --- parsing and validation ---------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "[]"])
def test_no_amqp_configs(value):
    params = {} if value is None else {"src_galaxy_pulsar_amqp_configs": value}
    resolve = _resolve(**params)

    assert resolve("_galaxy_pulsar_amqp_configs") == []
    assert resolve("_galaxy_pulsar_amqp_config_errors") == []
    assert resolve("_galaxy_rabbitmq_vhosts") == []
    assert resolve("_galaxy_rabbitmq_fixed_passwords") == {}
    assert resolve("_galaxy_remote_destinations") == []


def test_configs_are_parsed_with_defaults():
    configs = _resolve(src_galaxy_pulsar_amqp_configs=TWO)("_galaxy_pulsar_amqp_configs")

    assert [c["name"] for c in configs] == ["hpc", "eu"]
    assert configs[0]["docker_enabled"] is True
    assert configs[0]["label"] == "Our HPC"
    assert configs[1]["label"] == "eu"
    assert configs[1]["manager"] == "_default_"
    assert configs[1]["environment"] == "amqp_eu"


def test_invalid_configs_produce_errors_not_configs():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs="[{name: HPC}, {name: a, b: c}]")

    errors = resolve("_galaxy_pulsar_amqp_config_errors")

    assert len(errors) == 2
    assert errors[0].startswith("entry 1: 'name' must match")


def test_malformed_parameter_fails_with_the_parameter_name():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs="[{name: hpc")

    with pytest.raises(AnsibleError, match="src_galaxy_pulsar_amqp_configs: expected a list"):
        resolve("_galaxy_pulsar_amqp_config_errors")


def test_rest_pulsar_configs_and_tool_mappings_use_the_shared_parser():
    text = (REPO_ROOT / "vars" / "src_galaxy_vars.yml").read_text()

    # The SRC format is parsed in one place (filter_plugins/src_galaxy.py), not with regexes here.
    assert "regex_replace('([{,]" not in text
    assert "from_json" not in text
    for parameter in (
        "src_galaxy_pulsar_configs",
        "src_galaxy_pulsar_tool_mappings",
        "src_galaxy_pulsar_amqp_configs",
    ):
        assert f"src_config_list('{parameter}')" in text


# --- set_config.yml assertions ------------------------------------------------


def _set_config_asserts():
    tasks = yaml.safe_load((REPO_ROOT / "tasks" / "set_config.yml").read_text())
    names = [task["name"] for task in tasks]
    rabbitmq_vars = names.index("Load RabbitMQ variables")
    asserts = {
        task["name"]: task["ansible.builtin.assert"]
        for task in tasks
        if "ansible.builtin.assert" in task
    }
    return names, rabbitmq_vars, asserts


def test_set_config_validates_amqp_configs_after_loading_rabbitmq_vars():
    names, rabbitmq_vars, asserts = _set_config_asserts()
    check = asserts["Validate src_galaxy_pulsar_amqp_configs"]

    assert names.index("Validate src_galaxy_pulsar_amqp_configs") > rabbitmq_vars
    assert check["that"] == "_galaxy_pulsar_amqp_config_errors | length == 0"
    assert "_galaxy_pulsar_amqp_config_errors" in check["fail_msg"]


def test_set_config_requires_rabbitmq_for_amqp_configs():
    names, rabbitmq_vars, asserts = _set_config_asserts()
    check = asserts["AMQP Pulsar configs require RabbitMQ"]

    assert names.index("AMQP Pulsar configs require RabbitMQ") > rabbitmq_vars
    assert check["that"] == "_galaxy_rabbitmq or _galaxy_pulsar_amqp_configs | length == 0"
    assert "src_galaxy_rabbitmq" in check["fail_msg"]


# --- RabbitMQ users, vhosts and passwords --------------------------------------


def test_one_vhost_and_user_per_pulsar_and_galaxy_on_all_of_them():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs=TWO)
    full = {"configure_priv": ".*", "read_priv": ".*", "write_priv": ".*"}

    assert resolve("_galaxy_rabbitmq_vhosts") == ["/pulsar/hpc", "/pulsar/eu"]
    assert resolve("_galaxy_rabbitmq_users_resolved") == [
        {
            "name": "galaxy",
            "tags": "",
            "permissions": [{"vhost": "/pulsar/hpc", **full}, {"vhost": "/pulsar/eu", **full}],
        },
        {"name": "hpc", "tags": "", "permissions": [{"vhost": "/pulsar/hpc", **full}]},
        {"name": "eu", "tags": "", "permissions": [{"vhost": "/pulsar/eu", **full}]},
    ]


def test_given_passwords_are_kept_apart_from_the_user_list():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs=TWO)

    assert resolve("_galaxy_rabbitmq_fixed_passwords") == {"eu": "Abcdefghijkl1234"}
    assert "Abcdefghijkl1234" not in str(resolve("_galaxy_rabbitmq_users_resolved"))


def _rabbitmq_tasks():
    return yaml.safe_load((REPO_ROOT / "tasks" / "rabbitmq.yml").read_text())


def _task(name, tasks=None):
    [task] = [t for t in tasks or _rabbitmq_tasks() if t["name"] == name]
    return task


def test_given_passwords_overwrite_the_password_file_generated_ones_do_not():
    task = _task("Write passwords of RabbitMQ users")
    copy = task["ansible.builtin.copy"]

    assert copy["content"].startswith("{{ _galaxy_rabbitmq_fixed_passwords[item.name] | default(")
    assert copy["force"] == "{{ item.name in _galaxy_rabbitmq_fixed_passwords }}"
    assert task["no_log"] is True


def test_connection_hint_is_written_root_only_without_logging():
    task = _task("Write the Pulsar app.yml settings for each AMQP Pulsar")
    template = task["ansible.builtin.template"]

    assert template["src"] == "templates/rabbitmq/pulsar_amqp_app.yml.j2"
    assert template["dest"] == "{{ _galaxy_rabbitmq_secrets_dir }}/pulsar_{{ item.name }}.txt"
    assert (template["owner"], template["group"], template["mode"]) == ("root", "root", "0600")
    assert task["loop"] == "{{ _galaxy_pulsar_amqp_configs }}"
    assert task["loop_control"]["label"] == "{{ item.name }}"
    assert task["no_log"] is True
    names = [t["name"] for t in _rabbitmq_tasks()]
    assert names.index(task["name"]) > names.index("Collect RabbitMQ passwords")


def _hint(config, port=5671):
    return render_template(
        "rabbitmq/pulsar_amqp_app.yml.j2",
        {
            "item": config,
            "_galaxy_rabbitmq_passwords": PASSWORDS,
            "_galaxy_server_fqdn": "galaxy.example.org",
            "_galaxy_rabbitmq_port": port,
        },
    )


def test_connection_hint_is_the_pulsar_app_yml_snippet():
    [hpc, _] = _resolve(src_galaxy_pulsar_amqp_configs=TWO)("_galaxy_pulsar_amqp_configs")

    hint = yaml.safe_load(_hint(hpc, port=5673))

    assert hint["message_queue_url"] == (
        f"pyamqp://hpc:{HPC_PW}@galaxy.example.org:5673//pulsar/hpc?ssl=1"
    )
    assert hint["staging_directory"] == "/mnt/pulsar/files/staging"
    assert hint["managers"] == {
        "_default_": {
            "type": "queued_python",
            "num_concurrent_jobs": 1,
            "min_polling_interval": 0.5,
        }
    }
    assert hint["amqp_acknowledge"] is True
    assert "galaxy" not in str(hint).replace("galaxy.example.org", "")


def test_connection_hint_names_the_custom_manager():
    configs = _resolve(
        src_galaxy_pulsar_amqp_configs="[{name: hpc, manager: prod, jobs_directory: /x/staging}]"
    )("_galaxy_pulsar_amqp_configs")

    hint = yaml.safe_load(_hint(configs[0]))

    assert list(hint["managers"]) == ["prod"]
    assert hint["staging_directory"] == "/x/staging"


def test_galaxy_user_may_only_connect_from_loopback():
    # Its password is in job_conf.yml; Pulsar users connect from outside over AMQPS.
    resolve = _resolve()
    names = [
        "_galaxy_rabbitmq_port",
        "_galaxy_rabbitmq_local_port",
        "_galaxy_rabbitmq_tls_dir",
        "_galaxy_rabbitmq_tls_versions",
    ]
    conf = render_template("rabbitmq/rabbitmq.conf.j2", {n: resolve(n) for n in names})

    assert re.search(r"^loopback_users\.galaxy = true$", conf, re.MULTILINE)


# --- playbook order and file permissions ----------------------------------------


def test_rabbitmq_runs_before_the_galaxy_role_renders_job_conf():
    [play] = yaml.safe_load((REPO_ROOT / "galaxysrv.yml").read_text())
    pre_tasks = [task.get("ansible.builtin.include_tasks") for task in play["pre_tasks"]]

    assert "tasks/rabbitmq.yml" in pre_tasks
    assert all(
        task.get("ansible.builtin.include_tasks") != "tasks/rabbitmq.yml" for task in play["tasks"]
    )
    rabbitmq = play["pre_tasks"][pre_tasks.index("tasks/rabbitmq.yml")]
    assert rabbitmq["when"] == "_galaxy_rabbitmq"


def test_galaxy_config_files_are_not_world_readable():
    # job_conf.yml holds the galaxy RabbitMQ password; the role deploys it with this mode.
    galaxy_vars = yaml.safe_load((REPO_ROOT / "vars" / "galaxy_vars.yml").read_text())

    assert galaxy_vars["galaxy_config_perms"] == "0640"


# --- job_conf.yml -------------------------------------------------------------

JOB_CONF_VARS = {
    "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
    "apptainer_volumes": [],
    "_galaxy_tpv_config_dir": "/srv/galaxy/config/tpv",
    "_galaxy_rabbitmq_passwords": PASSWORDS,
}
JOB_CONF_NAMES = [
    "_galaxy_use_interactive_tools",
    "_galaxy_jobs_default",
    "_galaxy_has_pulsar",
    "_galaxy_pulsar_configs_raw",
    "_galaxy_tpv_tool_mappings",
    "_galaxy_pulsar_amqp_configs",
    "_galaxy_rabbitmq_local_port",
    "_galaxy_infrastructure_url",
    "_galaxy_byoc_runners",
]


def _job_conf_text(**params):
    resolve = _resolve(**params)
    variables = {**JOB_CONF_VARS, **{name: resolve(name) for name in JOB_CONF_NAMES}}
    return render_template("galaxy/config/job_conf.yml.j2", variables)


def _job_conf(**params):
    return yaml.safe_load(_job_conf_text(**params))


def _tpv_destinations(**params):
    resolve = _resolve(**params)
    variables = {**JOB_CONF_VARS, **{name: resolve(name) for name in JOB_CONF_NAMES}}
    return render_yaml("files/galaxy/tpv/destinations.yml.j2", variables)["destinations"]


def test_no_amqp_configs_render_no_mq_runner():
    conf = _job_conf()

    assert [r for r in conf["runners"] if r.startswith("pulsar_mq_")] == []
    assert [d for d in _tpv_destinations() if d.startswith("amqp_")] == []
    assert "PulsarMQJobRunner" not in _job_conf_text()


def test_one_mq_runner_per_pulsar():
    runners = _job_conf(src_galaxy_pulsar_amqp_configs=TWO)["runners"]

    assert runners["pulsar_mq_hpc"] == {
        "load": "galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
        "amqp_url": f"pyamqp://galaxy:{GALAXY_PW}@127.0.0.1:5672//pulsar/hpc",
        "galaxy_url": "https://galaxy.example.org",
        "manager": "_default_",
        "amqp_acknowledge": True,
        "amqp_ack_republish_time": 1200,
        "amqp_consumer_timeout": 2.0,
        "amqp_publish_retry": True,
        "amqp_publish_retry_max_retries": 60,
    }
    assert runners["pulsar_mq_eu"]["amqp_url"].endswith("@127.0.0.1:5672//pulsar/eu")
    assert "pulsar" not in runners  # no REST runner without src_galaxy_pulsar_configs


def test_one_tpv_destination_per_pulsar():
    destinations = _tpv_destinations(src_galaxy_pulsar_amqp_configs=TWO)

    assert destinations["amqp_hpc"] == {
        "runner": "pulsar_mq_hpc",
        "params": {
            "jobs_directory": "/mnt/pulsar/files/staging",
            "default_file_action": "remote_transfer",
            "dependency_resolution": "remote",
            "rewrite_parameters": True,
            "remote_metadata": False,
            "outputs_to_working_directory": False,
            "docker_enabled": True,
            "singularity_enabled": False,
        },
        "scheduling": {"require": ["hpc-pulsar"]},
    }
    assert destinations["amqp_eu"]["params"]["docker_enabled"] is False
    assert destinations["amqp_eu"]["scheduling"] == {"require": ["eu-pulsar"]}


def test_only_the_galaxy_password_is_in_job_conf():
    text = _job_conf_text(src_galaxy_pulsar_amqp_configs=TWO)

    assert GALAXY_PW in text
    assert HPC_PW not in text
    assert "Abcdefghijkl1234" not in text


def test_amqp_and_rest_pulsars_together():
    params = {
        "src_galaxy_pulsar_amqp_configs": TWO,
        "src_galaxy_pulsar_configs": "[{name: rest, url: https://r.example.org, private_token: t}]",
    }

    assert {"pulsar", "pulsar_mq_hpc", "pulsar_mq_eu"} <= set(_job_conf(**params)["runners"])
    assert {"remote_rest_basic", "amqp_hpc", "amqp_eu"} <= set(_tpv_destinations(**params))


def test_rest_and_amqp_pulsars_must_not_share_a_tag():
    resolve = _resolve(
        src_galaxy_pulsar_amqp_configs=TWO,
        src_galaxy_pulsar_configs="[{name: hpc, url: https://h.example.org, private_token: t}]",
    )

    assert resolve("_galaxy_tpv_config_errors") == [
        "Pulsar tag hpc-pulsar is used by more than one of src_galaxy_pulsar_configs /"
        " src_galaxy_pulsar_amqp_configs (names must differ)"
    ]


def test_tool_mappings_may_target_amqp_destinations():
    resolve = _resolve(
        src_galaxy_pulsar_amqp_configs=TWO,
        src_galaxy_pulsar_tool_mappings="[{tool_id: bwa, environment: amqp_hpc}]",
    )

    assert resolve("_galaxy_tpv_tool_mappings") == [
        {"tool_id": "bwa", "environment": "amqp_hpc", "tag": "hpc-pulsar"}
    ]


# --- user preferences and routing ------------------------------------------------


def test_amqp_pulsars_are_remote_destinations_after_rest_ones():
    resolve = _resolve(
        src_galaxy_pulsar_amqp_configs=TWO,
        src_galaxy_pulsar_configs="[{name: rest, url: https://r.example.org, private_token: t}]",
    )

    assert resolve("_galaxy_remote_destinations") == [
        {"id": "remote_rest_basic", "label": "r.example.org", "tag": "rest-pulsar"},
        {"id": "amqp_hpc", "label": "Our HPC", "tag": "hpc-pulsar"},
        {"id": "amqp_eu", "label": "eu", "tag": "eu-pulsar"},
    ]


def test_every_amqp_destination_is_a_tpv_destination_and_its_tag_in_the_dropdown():
    resolve = _resolve(src_galaxy_pulsar_amqp_configs=TWO)
    destinations = resolve("_galaxy_remote_destinations")
    tpv = _tpv_destinations(src_galaxy_pulsar_amqp_configs=TWO)
    prefs = render_yaml(
        "galaxy/config/user_preferences_extra_conf.yml.j2",
        {
            "_galaxy_ibridges_enabled": False,
            "_galaxy_byoc_pulsar": False,
            "_galaxy_remote_destinations": destinations,
            "_galaxy_byoc_preference_options": [],
        },
    )
    options = prefs["preferences"]["distributed_compute"]["inputs"][0]["options"]

    assert _job_conf(src_galaxy_pulsar_amqp_configs=TWO)["execution"]["default"] == "tpv_dispatcher"
    for destination in destinations:
        assert tpv[destination["id"]]["runner"].startswith("pulsar_mq_")
        assert tpv[destination["id"]]["scheduling"] == {"require": [destination["tag"]]}
        assert [destination["label"], destination["tag"]] in options


def test_rabbitmq_module_dependencies_are_installed():
    # community.rabbitmq.rabbitmq_vhost imports requests; it only runs once a vhost exists.
    [install] = [t for t in _rabbitmq_tasks() if t["name"] == "Install RabbitMQ"]

    assert set(install["ansible.builtin.apt"]["name"]) == {"rabbitmq-server", "python3-requests"}
