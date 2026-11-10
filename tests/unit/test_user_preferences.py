"""Extra user preferences (templates/galaxy/config/user_preferences_extra_conf.yml.j2)."""

import pytest

from tests.helpers.render import render_yaml

TEMPLATE = "galaxy/config/user_preferences_extra_conf.yml.j2"

BASE_VARS = {
    "_galaxy_ibridges_enabled": True,
    "_galaxy_remote_destinations": [],
    "_galaxy_byoc_pulsar": False,
}
REMOTES = [
    {"id": "remote_hpc_basic", "label": "HPC cluster"},
    {"id": "remote_pulsar-example-org-_basic", "label": 'It\'s a "quoted": label'},
]
DEFAULT_OPTION = ["default - Galaxy will decide where to put your job", "None"]


def _prefs(**overrides):
    return render_yaml(TEMPLATE, {**BASE_VARS, **overrides})["preferences"] or {}


def test_no_remote_destinations_means_no_distributed_compute_form():
    assert "distributed_compute" not in _prefs()


def test_distributed_compute_lists_default_then_each_remote_destination():
    form = _prefs(_galaxy_remote_destinations=REMOTES)["distributed_compute"]

    assert form["description"] == "Use distributed compute resources"
    [select] = form["inputs"]
    assert select["name"] == "remote_resources"
    assert select["type"] == "select"
    assert select["required"] is False
    assert select["options"] == [
        DEFAULT_OPTION,
        ["HPC cluster", "remote_hpc_basic"],
        ['It\'s a "quoted": label', "remote_pulsar-example-org-_basic"],
    ]


def test_default_option_is_stored_as_the_string_none():
    """Galaxy stores the option value verbatim; the job rule treats "None" as no choice."""
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
    # "secret" values are never sent back to the browser (Galaxy sends a placeholder).
    assert inputs["password"]["type"] == "secret"
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
