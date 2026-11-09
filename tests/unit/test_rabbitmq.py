"""Optional RabbitMQ (AMQPS) broker on the Galaxy host (issue #3)."""

from pathlib import Path

import pytest
import yaml

from tests.helpers.ansible_vars import REPO_ROOT, resolve_vars
from tests.helpers.render import render_template

VARS_FILES = ["internal_vars.yml", "src_galaxy_vars.yml", "rabbitmq_vars.yml"]
TASKS_FILE = REPO_ROOT / "tasks" / "rabbitmq.yml"


def _resolve(**params):
    return resolve_vars(VARS_FILES, params)


# --- component parameters ----------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, False),
        ({"src_galaxy_rabbitmq": ""}, False),
        ({"src_galaxy_rabbitmq": "false"}, False),
        ({"src_galaxy_rabbitmq": "true"}, True),
        ({"src_galaxy_rabbitmq": True}, True),
    ],
    ids=["undefined", "empty", "string-false", "string-true", "bool-true"],
)
def test_rabbitmq_is_opt_in(params, expected):
    assert _resolve(**params)("_galaxy_rabbitmq") is expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, 5671),
        ({"src_galaxy_rabbitmq_port": ""}, 5671),
        ({"src_galaxy_rabbitmq_port": "5673"}, 5673),
    ],
    ids=["undefined", "empty", "string"],
)
def test_rabbitmq_port(params, expected):
    assert _resolve(**params)("_galaxy_rabbitmq_port") == expected


def test_rabbitmq_does_not_enable_docker():
    assert _resolve(src_galaxy_rabbitmq="true")("_galaxy_enable_docker") is False


# --- users and vhosts ----------------------------------------------------------


def test_default_users_are_only_galaxy_without_vhosts_or_tags():
    resolve = _resolve()

    assert resolve("_galaxy_rabbitmq_vhosts") == []
    assert resolve("_galaxy_rabbitmq_users_resolved") == [
        {"name": "galaxy", "tags": "", "permissions": []},
    ]


def test_galaxy_user_gets_full_permissions_on_every_vhost():
    resolve = _resolve(_galaxy_rabbitmq_vhosts=["/pulsar/a", "/pulsar/b"])
    full = {"configure_priv": ".*", "read_priv": ".*", "write_priv": ".*"}

    assert resolve("_galaxy_rabbitmq_users_resolved") == [
        {
            "name": "galaxy",
            "tags": "",
            "permissions": [{"vhost": "/pulsar/a", **full}, {"vhost": "/pulsar/b", **full}],
        },
    ]


def test_extra_users_keep_their_own_vhosts_and_tags():
    users = [{"name": "pulsar_a", "vhosts": ["/pulsar/a"], "tags": "monitoring"}, {"name": "bare"}]
    resolved = _resolve(_galaxy_rabbitmq_users=users)("_galaxy_rabbitmq_users_resolved")

    assert resolved[0]["name"] == "pulsar_a"
    assert resolved[0]["tags"] == "monitoring"
    assert [p["vhost"] for p in resolved[0]["permissions"]] == ["/pulsar/a"]
    assert resolved[1] == {"name": "bare", "tags": "", "permissions": []}


def test_secrets_live_in_a_root_only_directory():
    assert _resolve()("_galaxy_rabbitmq_secrets_dir") == "/etc/galaxy-rabbitmq"


# --- rabbitmq.conf -----------------------------------------------------------


def _conf(**overrides):
    resolve = _resolve(**overrides)
    names = [
        "_galaxy_rabbitmq_port",
        "_galaxy_rabbitmq_local_port",
        "_galaxy_rabbitmq_tls_dir",
        "_galaxy_rabbitmq_tls_versions",
    ]
    text = render_template("rabbitmq/rabbitmq.conf.j2", {name: resolve(name) for name in names})
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    pairs = [line.split("=", 1) for line in lines]
    return {key.strip(): value.strip() for key, value in pairs}


def test_conf_plain_amqp_only_on_loopback():
    conf = _conf()

    assert conf["listeners.tcp.1"] == "127.0.0.1:5672"
    assert [key for key in conf if key.startswith("listeners.tcp")] == ["listeners.tcp.1"]
    assert conf["distribution.listener.interface"] == "127.0.0.1"


def test_conf_amqps_listener_uses_copied_certificate():
    conf = _conf(src_galaxy_rabbitmq_port="5673")

    assert conf["listeners.ssl.default"] == "5673"
    assert conf["ssl_options.certfile"] == "/etc/rabbitmq/tls/fullchain.pem"
    assert conf["ssl_options.cacertfile"] == "/etc/rabbitmq/tls/fullchain.pem"
    assert conf["ssl_options.keyfile"] == "/etc/rabbitmq/tls/key.pem"
    assert conf["ssl_options.versions.1"] == "tlsv1.3"
    assert conf["ssl_options.versions.2"] == "tlsv1.2"
    assert "ssl_options.versions.3" not in conf


def test_conf_has_no_management_listener():
    assert not [key for key in _conf() if key.startswith("management")]


# --- tasks/rabbitmq.yml ------------------------------------------------------

SECRET_MARKERS = ("password", ".pass", "slurp")


def _tasks(path: Path):
    return _tasks_in(yaml.safe_load(path.read_text()))


def _tasks_in(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _tasks_in(task.get(section, []))


def test_every_secret_handling_task_hides_its_output():
    def body(task):
        return yaml.safe_dump({key: value for key, value in task.items() if key != "name"})

    secret_tasks = [
        task
        for task in _tasks(TASKS_FILE)
        if any(marker in body(task) for marker in SECRET_MARKERS)
    ]

    assert secret_tasks, "expected tasks that manage RabbitMQ passwords"
    assert [task["name"] for task in secret_tasks if task.get("no_log") is not True] == []


def test_guest_user_is_removed():
    removals = [
        task["community.rabbitmq.rabbitmq_user"]
        for task in _tasks(TASKS_FILE)
        if "community.rabbitmq.rabbitmq_user" in task
        and task["community.rabbitmq.rabbitmq_user"].get("state") == "absent"
    ]

    assert [removal["user"] for removal in removals] == ["guest"]


def test_requirements_pin_community_rabbitmq_and_not_the_docker_role():
    requirements = yaml.safe_load((REPO_ROOT / "requirements.yml").read_text())
    collections = {item["name"]: item for item in requirements["collections"]}
    roles = {item.get("name", item["src"]) for item in requirements["roles"]}

    assert collections["community.rabbitmq"].get("version")
    assert "usegalaxy_eu.rabbitmqserver" not in roles


def test_rabbitmqctl_modules_target_the_loopback_node():
    assert _resolve()("_galaxy_rabbitmq_node") == "rabbit@localhost"
    modules = ("community.rabbitmq.rabbitmq_user", "community.rabbitmq.rabbitmq_vhost")
    calls = [task[module] for task in _tasks(TASKS_FILE) for module in modules if module in task]

    assert len(calls) == 3
    assert {call.get("node") for call in calls} == {"{{ _galaxy_rabbitmq_node }}"}


def test_pulsar_layout_one_vhost_per_pulsar_shared_with_galaxy():
    # Issue #4: one MQ runner per Pulsar vhost. Galaxy connects as `galaxy` over 127.0.0.1:5672,
    # the remote Pulsar as its own user over AMQPS; both need full rights on that vhost.
    users = [
        {"name": "galaxy", "vhosts": ["/pulsar/eu"]},
        {"name": "pulsar_eu", "vhosts": ["/pulsar/eu"]},
    ]
    resolved = _resolve(_galaxy_rabbitmq_vhosts=["/pulsar/eu"], _galaxy_rabbitmq_users=users)(
        "_galaxy_rabbitmq_users_resolved"
    )
    full = {"vhost": "/pulsar/eu", "configure_priv": ".*", "read_priv": ".*", "write_priv": ".*"}

    assert [(user["name"], user["permissions"]) for user in resolved] == [
        ("galaxy", [full]),
        ("pulsar_eu", [full]),
    ]


def test_generated_passwords_are_url_safe():
    # Passwords are embedded verbatim in amqp_url / message_queue_url, so no URL-special characters.
    contents = [
        task["ansible.builtin.copy"]["content"]
        for task in _tasks(TASKS_FILE)
        if "lookup('ansible.builtin.password'"
        in task.get("ansible.builtin.copy", {}).get("content", "")
    ]

    assert len(contents) == 1
    assert "chars=['ascii_letters', 'digits']" in contents[0]
