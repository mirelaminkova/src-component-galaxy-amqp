"""Extra user preferences (templates/galaxy/config/user_preferences_extra_conf.yml.j2)."""

import pytest

from tests.helpers.render import render_yaml

TEMPLATE = "galaxy/config/user_preferences_extra_conf.yml.j2"

BASE_VARS = {
    "_galaxy_ibridges_enabled": True,
    "_galaxy_remote_destinations": [],
    "_galaxy_byoc_pulsar": False,
    "_galaxy_byoc_preference_options": [],
}
REMOTES = [
    {"id": "remote_hpc_basic", "label": "HPC cluster", "tag": "hpc-pulsar"},
    {
        "id": "remote_pulsar-example-org-_basic",
        "label": 'It\'s a "quoted": label',
        "tag": "pulsar-example-org--pulsar",
    },
]
DEFAULT_OPTION = ["default - Galaxy will decide where to put your job", "None"]


def _prefs(**overrides):
    return render_yaml(TEMPLATE, {**BASE_VARS, **overrides})["preferences"] or {}


def test_no_remote_destinations_means_no_distributed_compute_form():
    assert "distributed_compute" not in _prefs()


def test_distributed_compute_lists_default_then_each_remote_destination_tag():
    """The values are TPV scheduling tags (`<name>-pulsar`), as on usegalaxy.eu."""
    form = _prefs(_galaxy_remote_destinations=REMOTES)["distributed_compute"]

    assert form["description"] == "Use distributed compute resources"
    [select] = form["inputs"]
    assert select["name"] == "remote_resources"
    assert select["type"] == "select"
    assert select["required"] is False
    assert select["options"] == [
        DEFAULT_OPTION,
        ["HPC cluster", "hpc-pulsar"],
        ['It\'s a "quoted": label', "pulsar-example-org--pulsar"],
    ]


def test_default_option_is_stored_as_the_string_none():
    """Galaxy stores the option value verbatim; the TPV rule treats "None" as no choice."""
    form = _prefs(_galaxy_remote_destinations=REMOTES)["distributed_compute"]

    assert form["inputs"][0]["options"][0][1] == "None"


def test_byoc_pulsar_form_is_off_by_default():
    assert "byoc_pulsar" not in _prefs(_galaxy_remote_destinations=REMOTES)


def test_byoc_pulsar_form_when_enabled():
    form = _prefs(_galaxy_byoc_pulsar=True)["byoc_pulsar"]
    inputs = {i["name"]: i for i in form["inputs"]}

    assert "experimental" in form["description"]
    assert list(inputs) == [
        "username",
        "password",
        "max_accepted_cores",
        "max_accepted_mem",
        "min_accepted_gpus",
        "max_accepted_gpus",
    ]
    assert inputs["username"]["type"] == "text"
    # Same type as usegalaxy.eu (see test_byoc_pulsar_form_matches_usegalaxy_eu).
    assert inputs["password"]["type"] == "password"
    assert all(inputs[n]["type"] == "number" for n in list(inputs)[2:])
    assert not any(i["required"] for i in inputs.values())


@pytest.mark.parametrize("remotes", [[], REMOTES])
@pytest.mark.parametrize("byoc", [False, True])
def test_irods_block_is_kept_and_gated_by_ibridges(remotes, byoc):
    on = _prefs(_galaxy_remote_destinations=remotes, _galaxy_byoc_pulsar=byoc)
    off = _prefs(
        _galaxy_ibridges_enabled=False,
        _galaxy_remote_destinations=remotes,
        _galaxy_byoc_pulsar=byoc,
    )

    assert [i["name"] for i in on["irods_config"]["inputs"]] == ["username", "password", "env"]
    assert "irods_config" not in off
    assert ("distributed_compute" in on) is bool(remotes)
    assert ("byoc_pulsar" in on) is byoc


# usegalaxy-eu/infrastructure-playbook files/galaxy/config/user_preferences_extra_conf.yml
# (byoc_pulsar): the form the BYOC tooling reads must have the same names, types and labels.
EU_BYOC_PULSAR_INPUTS = [
    ("username", "text", "RabbitMQ custom username (e.g. mypulsarendpoint)."),
    (
        "password",
        "password",
        "RabbitMQ custom alphanumerical password, at least 10 characters long "
        "(e.g. MyPulS4rP4ssw0rd).",
    ),
    ("max_accepted_cores", "number", "Maximum number of CPU cores available."),
    ("max_accepted_mem", "number", "Maximum number of RAM available (in GB)."),
    (
        "min_accepted_gpus",
        "number",
        "Minimum number of GPU available (set this to zero if you don't want to share GPUs).",
    ),
    (
        "max_accepted_gpus",
        "number",
        "Maximum number of GPU available (set this to zero if you don't want to share GPUs).",
    ),
]


def test_byoc_pulsar_form_matches_usegalaxy_eu():
    prefs = _prefs(_galaxy_byoc_pulsar=True)
    inputs = prefs["byoc_pulsar"]["inputs"]

    assert [(i["name"], i["type"], i["label"]) for i in inputs] == EU_BYOC_PULSAR_INPUTS
    assert all(i["required"] is False for i in inputs)


BYOC_OPTIONS = [
    ["EOSC EU Node 01", "eosc-eu-node-01-pulsar"],
    ["Bari (Italy) - INFN", "egi-pulsar"],
]


def test_onboarded_options_follow_the_generated_ones():
    form = _prefs(
        _galaxy_remote_destinations=REMOTES[:1], _galaxy_byoc_preference_options=BYOC_OPTIONS
    )

    assert form["distributed_compute"]["inputs"][0]["options"] == [
        DEFAULT_OPTION,
        ["HPC cluster", "remote_hpc_basic"],
        *BYOC_OPTIONS,
    ]


def test_onboarded_options_alone_show_the_form():
    form = _prefs(_galaxy_byoc_preference_options=BYOC_OPTIONS)

    assert form["distributed_compute"]["inputs"][0]["options"] == [DEFAULT_OPTION, *BYOC_OPTIONS]


def test_onboarded_option_with_a_generated_id_is_not_listed_twice():
    duplicate = [["Same id, other label", "remote_hpc_basic"], *BYOC_OPTIONS]
    form = _prefs(
        _galaxy_remote_destinations=REMOTES[:1], _galaxy_byoc_preference_options=duplicate
    )

    assert [o[1] for o in form["distributed_compute"]["inputs"][0]["options"]] == [
        "None",
        "remote_hpc_basic",
        "eosc-eu-node-01-pulsar",
        "egi-pulsar",
    ]
