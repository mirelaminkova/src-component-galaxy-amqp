"""Filter plugins in filter_plugins/src_galaxy.py (SRC parameter parsing, AMQP Pulsar configs)."""

import pytest
from ansible.errors import AnsibleFilterError

from filter_plugins.src_galaxy import (
    FilterModule,
    pulsar_amqp_config_errors,
    pulsar_amqp_configs,
    src_config_list,
)

# --- src_config_list: the SRC "YAML-ish" list format ---------------------------


@pytest.mark.parametrize("value", [None, "", "  ", "[]", " [ ] "])
def test_empty_input_is_an_empty_list(value):
    assert src_config_list(value) == []


def test_unquoted_keys_and_values_are_parsed():
    value = (
        "[{name: hpc, label: Our HPC cluster, url: https://h.example.org:8913/pulsar,"
        " private_token: t0ken, docker_enabled: true, singularity_enabled: false}]"
    )

    assert src_config_list(value) == [
        {
            "name": "hpc",
            "label": "Our HPC cluster",
            "url": "https://h.example.org:8913/pulsar",
            "private_token": "t0ken",
            "docker_enabled": True,
            "singularity_enabled": False,
        }
    ]


def test_quoted_values_and_several_entries():
    value = '[{name: "a b", x: 1}, {name: c}]'

    assert src_config_list(value) == [{"name": "a b", "x": "1"}, {"name": "c"}]


def test_nested_lists_are_kept():
    value = "[{name: x, container_resolvers: [{type: explicit}, {type: mulled}]}]"

    assert src_config_list(value) == [
        {"name": "x", "container_resolvers": [{"type": "explicit"}, {"type": "mulled"}]}
    ]


def test_already_parsed_lists_pass_through():
    # SRC passes strings, but `-e` with JSON or a YAML vars file gives a real list.
    assert src_config_list([{"name": "a"}]) == [{"name": "a"}]


@pytest.mark.parametrize(
    "value",
    ["[{name: a", "{name: a}", "[{name: a}, oops]", "not a list", "[1, 2]", 42],
    ids=["unterminated", "not-a-list", "non-dict-item", "plain-text", "numbers", "int"],
)
def test_malformed_input_raises_a_clear_error(value):
    with pytest.raises(AnsibleFilterError, match="list of"):
        src_config_list(value)


def test_errors_name_the_parameter():
    with pytest.raises(AnsibleFilterError, match=r"^src_galaxy_x: "):
        src_config_list("[{a", "src_galaxy_x")


def test_filters_are_registered():
    assert FilterModule().filters() == {
        "src_config_list": src_config_list,
        "pulsar_amqp_configs": pulsar_amqp_configs,
        "pulsar_amqp_config_errors": pulsar_amqp_config_errors,
    }


# --- AMQP Pulsar configs (issue #4) ------------------------------------------


def test_minimal_entry_gets_defaults_and_derived_names():
    assert pulsar_amqp_configs([{"name": "hpc"}]) == [
        {
            "name": "hpc",
            "label": "hpc",
            "docker_enabled": False,
            "singularity_enabled": False,
            "dependency_resolution": "remote",
            "manager": "_default_",
            "jobs_directory": "/mnt/pulsar/files/staging",
            "vhost": "/pulsar/hpc",
            "runner": "pulsar_mq_hpc",
            "environment": "amqp_hpc",
        }
    ]


def test_full_entry_keeps_its_values():
    entry = {
        "name": "eu-1",
        "label": "EU cluster",
        "password": "Abcdefghijkl1234",
        "docker_enabled": "true",
        "singularity_enabled": True,
        "dependency_resolution": "local",
        "manager": "production",
        "jobs_directory": "/data/pulsar/staging",
    }

    [config] = pulsar_amqp_configs([entry])

    assert config == {
        **entry,
        "docker_enabled": True,
        "vhost": "/pulsar/eu-1",
        "runner": "pulsar_mq_eu-1",
        "environment": "amqp_eu-1",
    }


def test_password_is_only_present_when_given():
    configs = pulsar_amqp_configs([{"name": "a"}, {"name": "b", "password": "x" * 16}])

    assert "password" not in configs[0]
    assert configs[1]["password"] == "x" * 16


def test_valid_configs_have_no_errors():
    entries = [
        {"name": "a"},
        {"name": "b_2-x", "password": "Abc123._~-Abc123", "dependency_resolution": "none"},
        {"name": "c", "docker_enabled": False, "singularity_enabled": "false"},
    ]

    assert pulsar_amqp_config_errors(entries) == []


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([{}], "entry 1: 'name' is required"),
        ([{"name": ""}], "entry 1: 'name' must match [a-z0-9_-]+"),
        ([{"name": "HPC"}], "entry 1: 'name' must match [a-z0-9_-]+"),
        ([{"name": "a.b"}], "entry 1: 'name' must match [a-z0-9_-]+"),
        ([{"name": "a/b"}], "entry 1: 'name' must match [a-z0-9_-]+"),
        ([{"name": True}], "entry 1: 'name' must match [a-z0-9_-]+"),
        ([{"name": "x" * 65}], "entry 1: 'name' must be at most 64 characters"),
        ([{"name": "galaxy"}], "entry 1: 'name' must not be 'galaxy'"),
        ([{"name": "guest"}], "entry 1: 'name' must not be 'guest'"),
        ([{"name": "a"}, {"name": "a"}], "entry 2: duplicate name 'a'"),
        ([{"name": "a", "url": "https://x"}], "entry 1 (a): unknown key 'url'"),
        ([{"name": "a", "docker_enabled": "yes"}], "entry 1 (a): 'docker_enabled' must be"),
        ([{"name": "a", "singularity_enabled": 1}], "entry 1 (a): 'singularity_enabled' must be"),
        (
            [{"name": "a", "dependency_resolution": "conda"}],
            "entry 1 (a): 'dependency_resolution' must be one of none, local, remote",
        ),
        ([{"name": "a", "manager": "a b"}], "entry 1 (a): 'manager' must match"),
        ([{"name": "a", "manager": ""}], "entry 1 (a): 'manager' must match"),
        ([{"name": "a", "jobs_directory": "rel/path"}], "entry 1 (a): 'jobs_directory' must be"),
        ([{"name": "a", "jobs_directory": "/a b"}], "entry 1 (a): 'jobs_directory' must be"),
        ([{"name": "a", "label": ""}], "entry 1 (a): 'label' must be a non-empty string"),
        ([{"name": "a", "label": ["x"]}], "entry 1 (a): 'label' must be a non-empty string"),
        ([{"name": "a", "password": "short1"}], "entry 1 (a): 'password' must be"),
        ([{"name": "a", "password": "has space 123456"}], "entry 1 (a): 'password' must be"),
        ([{"name": "a", "password": "p@ss:w/rd#123456"}], "entry 1 (a): 'password' must be"),
        ([{"name": "a", "password": True}], "entry 1 (a): 'password' must be"),
        (["oops"], "entry 1: must be a mapping"),
    ],
)
def test_invalid_entries_are_reported(entries, message):
    errors = pulsar_amqp_config_errors(entries)

    assert any(error.startswith(message) for error in errors), errors


def test_all_errors_are_reported_at_once():
    errors = pulsar_amqp_config_errors([{"name": "A"}, {"name": "b", "manager": "x y"}, {}])

    assert len(errors) == 3


@pytest.mark.parametrize("password", ["p@ss:w/rd#123456", "has space 123456", "short1"])
def test_errors_never_contain_the_password(password):
    errors = pulsar_amqp_config_errors([{"name": "a", "password": password}])

    assert errors
    assert all(password not in error for error in errors)
