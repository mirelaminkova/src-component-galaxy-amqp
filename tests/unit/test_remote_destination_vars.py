"""Derived Pulsar variables in vars/src_galaxy_vars.yml, evaluated by Ansible itself.

The variables are templated with ansible-core's ``Templar`` (as the playbook would,
starting from the ResearchCloud string parameters), so Ansible's conversion of
rendered text into native types is part of what is tested. The remote destination
ids are compared with the environments job_conf.yml.j2 writes, so the ids users can
pick always name a real job environment.
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
            {"id": "remote_pulsar-example-org-_basic", "label": "pulsar.example.org"},
        ),
        (
            "[{url: http://10.0.0.5:8913, private_token: t}]",
            {"id": "remote_10-0-0-5:8913_basic", "label": "10.0.0.5"},
        ),
        (
            "[{name: hpc, url: https://hpc.example.org/pulsar, private_token: t}]",
            {"id": "remote_hpc_basic", "label": "hpc.example.org"},
        ),
        (
            "[{name: hpc, label: Our HPC cluster, url: https://h.example.org, private_token: t}]",
            {"id": "remote_hpc_basic", "label": "Our HPC cluster"},
        ),
    ],
)
def test_destination_id_and_label(config, expected):
    assert _destinations(config) == [expected]


def test_every_destination_id_is_a_job_conf_environment():
    evaluate = _ansible_vars(
        src_galaxy_pulsar_configs=(
            "[{url: https://pulsar.example.org/, private_token: t},"
            " {url: http://10.0.0.5:8913, private_token: t},"
            " {name: hpc, url: https://hpc.example.org, private_token: t, docker_enabled: true}]"
        )
    )
    job_conf = render_yaml(
        "galaxy/config/job_conf.yml.j2",
        {
            "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
            "apptainer_volumes": [],
            "_galaxy_use_interactive_tools": False,
            "_galaxy_use_dispatcher": evaluate("_galaxy_use_dispatcher"),
            "_galaxy_default_dispatcher": "default_dispatcher",
            "_galaxy_jobs_default": "singularity",
            "_galaxy_has_pulsar": evaluate("_galaxy_has_pulsar"),
            "_galaxy_pulsar_configs_raw": evaluate("_galaxy_pulsar_configs_raw"),
            "_galaxy_pulsar_tool_mappings": evaluate("_galaxy_pulsar_tool_mappings"),
            "_galaxy_pulsar_amqp_configs": evaluate("_galaxy_pulsar_amqp_configs"),
        },
    )
    environments = job_conf["execution"]["environments"]
    ids = [d["id"] for d in evaluate("_galaxy_remote_destinations")]

    assert len(ids) == 3
    assert all(environments[i]["runner"] == "pulsar" for i in ids)
    assert job_conf["execution"]["default"] == "default_dispatcher"


@pytest.mark.parametrize(
    ("interactive", "configs", "expected"),
    [
        ("false", "", False),
        ("true", "", True),
        ("false", ONE_CONFIG, True),
        ("true", ONE_CONFIG, True),
    ],
)
def test_dispatcher_is_used_for_interactive_tools_or_remote_destinations(
    interactive, configs, expected
):
    evaluate = _ansible_vars(
        src_galaxy_interactive_tools=interactive, src_galaxy_pulsar_configs=configs
    )

    assert evaluate("_galaxy_use_dispatcher") is expected


@pytest.mark.parametrize(
    ("param", "expected"),
    [(None, False), ("", False), ("false", False), ("true", True), (True, True)],
)
def test_byoc_pulsar_param_defaults_to_false(param, expected):
    params = {} if param is None else {"src_galaxy_byoc_pulsar": param}

    assert _ansible_vars(**params)("_galaxy_byoc_pulsar") is expected
