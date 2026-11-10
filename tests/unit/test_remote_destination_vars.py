"""Derived Pulsar variables in vars/src_galaxy_vars.yml, evaluated by Ansible itself.

The variables are templated with ansible-core's ``Templar`` (as the playbook would,
starting from the ResearchCloud string parameters), so Ansible's conversion of
rendered text into native types is part of what is tested. The remote destinations
are compared with the TPV destinations files/galaxy/tpv/destinations.yml.j2 writes, so
the tags users can pick always select a real destination.
"""

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from tests.helpers.render import REPO_ROOT, render_yaml

VARS_FILE = REPO_ROOT / "vars" / "src_galaxy_vars.yml"
NO_CONFIG = [{}, {"src_galaxy_pulsar_configs": ""}, {"src_galaxy_pulsar_configs": "[]"}]
ONE_CONFIG = "[{name: hpc, url: https://hpc.example.org, private_token: t0ken}]"


def _ansible_vars(**params):
    """Return a function evaluating a variable from the vars file for these parameters."""
    raw = yaml.safe_load(VARS_FILE.read_text())
    variables = {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in {**raw, **params}.items()
    }
    templar = Templar(loader=DataLoader(), variables=variables)

    def evaluate(name):
        return templar.template(trust_as_template("{{ %s }}" % name))  # noqa: UP031

    return evaluate


def _destinations(configs):
    return _ansible_vars(src_galaxy_pulsar_configs=configs)("_galaxy_remote_destinations")


@pytest.mark.parametrize("params", NO_CONFIG)
def test_no_pulsar_parameters_give_empty_lists(params):
    evaluate = _ansible_vars(**params)

    assert evaluate("_galaxy_pulsar_configs_raw") == []
    assert evaluate("_galaxy_pulsar_tool_mappings") == []
    assert evaluate("_galaxy_has_pulsar") is False
    assert evaluate("_galaxy_remote_destinations") == []


def test_pulsar_without_tool_mappings_has_an_empty_mapping_list():
    evaluate = _ansible_vars(src_galaxy_pulsar_configs=ONE_CONFIG)

    assert evaluate("_galaxy_has_pulsar") is True
    assert evaluate("_galaxy_pulsar_tool_mappings") == []


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            "[{url: https://pulsar.example.org/, private_token: t}]",
            {
                "id": "remote_pulsar-example-org-_basic",
                "label": "pulsar.example.org",
                "tag": "pulsar-example-org--pulsar",
            },
        ),
        (
            "[{url: http://10.0.0.5:8913, private_token: t}]",
            {
                "id": "remote_10-0-0-5:8913_basic",
                "label": "10.0.0.5",
                "tag": "10-0-0-5:8913-pulsar",
            },
        ),
        (
            "[{name: hpc, url: https://hpc.example.org/pulsar, private_token: t}]",
            {"id": "remote_hpc_basic", "label": "hpc.example.org", "tag": "hpc-pulsar"},
        ),
        (
            "[{name: hpc, label: Our HPC cluster, url: https://h.example.org, private_token: t}]",
            {"id": "remote_hpc_basic", "label": "Our HPC cluster", "tag": "hpc-pulsar"},
        ),
    ],
)
def test_destination_id_and_label(config, expected):
    assert _destinations(config) == [expected]


THREE_CONFIGS = (
    "[{url: https://pulsar.example.org/, private_token: t},"
    " {url: http://10.0.0.5:8913, private_token: t},"
    " {name: hpc, url: https://hpc.example.org, private_token: t, docker_enabled: true}]"
)


def test_every_remote_destination_is_a_tpv_destination_with_its_tag():
    evaluate = _ansible_vars(src_galaxy_pulsar_configs=THREE_CONFIGS)
    destinations = render_yaml(
        "files/galaxy/tpv/destinations.yml.j2",
        {
            "_galaxy_tpv_config_dir": "/srv/galaxy/config/tpv",
            "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
            "apptainer_volumes": [],
            "_galaxy_use_interactive_tools": False,
            "_galaxy_jobs_default": "singularity",
            "_galaxy_pulsar_configs_raw": evaluate("_galaxy_pulsar_configs_raw"),
            "_galaxy_pulsar_amqp_configs": [],
        },
    )["destinations"]
    remotes = evaluate("_galaxy_remote_destinations")

    assert len(remotes) == 3
    for remote in remotes:
        assert destinations[remote["id"]]["runner"] == "pulsar"
        assert destinations[remote["id"]]["scheduling"] == {"require": [remote["tag"]]}
    assert destinations["remote_hpc"]["scheduling"] == {"require": ["hpc-pulsar-docker"]}


def test_destination_tags_for_tool_mappings():
    evaluate = _ansible_vars(src_galaxy_pulsar_configs=THREE_CONFIGS)

    assert evaluate("_galaxy_tpv_destination_tags") == {
        "local": "local",
        "docker": "docker",
        "singularity": "singularity",
        "remote_pulsar-example-org-_basic": "pulsar-example-org--pulsar",
        "remote_10-0-0-5:8913_basic": "10-0-0-5:8913-pulsar",
        "remote_hpc_basic": "hpc-pulsar",
        "remote_hpc": "hpc-pulsar-docker",
    }


def test_tool_mappings_get_their_destination_tag():
    evaluate = _ansible_vars(
        src_galaxy_pulsar_configs=ONE_CONFIG,
        src_galaxy_pulsar_tool_mappings=(
            "[{tool_id: bwa, environment: remote_hpc_basic}, {tool_id: cat1, environment: docker}]"
        ),
    )

    assert evaluate("_galaxy_tpv_tool_mappings") == [
        {"tool_id": "bwa", "environment": "remote_hpc_basic", "tag": "hpc-pulsar"},
        {"tool_id": "cat1", "environment": "docker", "tag": "docker"},
    ]
    assert evaluate("_galaxy_tpv_config_errors") == []


@pytest.mark.parametrize("configs", ["", ONE_CONFIG])
def test_valid_configs_have_no_tpv_errors(configs):
    assert _ansible_vars(src_galaxy_pulsar_configs=configs)("_galaxy_tpv_config_errors") == []


def test_tool_mappings_without_a_remote_pulsar_are_ignored():
    evaluate = _ansible_vars(src_galaxy_pulsar_tool_mappings="[{tool_id: bwa, environment: nope}]")

    assert evaluate("_galaxy_tpv_tool_mappings") == []
    assert evaluate("_galaxy_tpv_config_errors") == []


@pytest.mark.parametrize(
    ("mappings", "error"),
    [
        (
            "[{tool_id: bwa, environment: remote_gone_basic}]",
            'src_galaxy_pulsar_tool_mappings: bwa maps to unknown environment "remote_gone_basic"'
            " (known: local, docker, singularity, remote_hpc_basic)",
        ),
        (
            "[{tool_id: bwa}]",
            'src_galaxy_pulsar_tool_mappings: bwa maps to unknown environment ""'
            " (known: local, docker, singularity, remote_hpc_basic)",
        ),
        (
            "[{environment: docker}]",
            'src_galaxy_pulsar_tool_mappings entry {"environment": "docker"} has no tool_id',
        ),
    ],
)
def test_invalid_tool_mappings_are_reported_and_skipped(mappings, error):
    evaluate = _ansible_vars(
        src_galaxy_pulsar_configs=ONE_CONFIG, src_galaxy_pulsar_tool_mappings=mappings
    )

    assert evaluate("_galaxy_tpv_config_errors") == [error]
    assert evaluate("_galaxy_tpv_tool_mappings") == []


@pytest.mark.parametrize(
    ("param", "expected"),
    [(None, False), ("", False), ("false", False), ("true", True), (True, True)],
)
def test_byoc_pulsar_param_defaults_to_false(param, expected):
    params = {} if param is None else {"src_galaxy_byoc_pulsar": param}

    assert _ansible_vars(**params)("_galaxy_byoc_pulsar") is expected
