"""Galaxy config templates (templates/galaxy/config/*.j2) render to valid YAML."""

import pytest

from tests.helpers.render import TEMPLATES_DIR, render_yaml

CONFIG_DIR = "galaxy/config"

BASE_VARS = {
    "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
    "galaxy_job_working_directory": "/srv/galaxy/jobs",
    "galaxy_tool_dependency_dir": "/srv/galaxy/deps",
    "apptainer_volumes": ["$galaxy_root:ro", "$tool_directory:ro", "$working_directory:rw"],
    "_galaxy_ibridges_enabled": True,
    "_galaxy_use_interactive_tools": False,
    "_galaxy_tpv_config_dir": "/srv/galaxy/config/tpv",
    "_galaxy_has_pulsar": False,
    "_galaxy_remote_destinations": [],
    "_galaxy_byoc_pulsar": False,
    "_galaxy_pulsar_amqp_configs": [],
    "_galaxy_byoc_runners": [],
    "_galaxy_byoc_preference_options": [],
}

REMOTE_DESTINATIONS = [
    {"id": "remote_byoc_basic", "label": "BYOC 'cluster': \"GPU\"", "tag": "byoc-pulsar"}
]

# Every config template must appear here with at least one variable set, so a new
# template cannot be added without a rendering test.
CASES = {
    "container_resolvers_conf.yml.j2": {"default": {}},
    "file_source_templates.yml.j2": {"default": {}},
    "pulsar_app.yml.j2": {"default": {}},
    "user_preferences_extra_conf.yml.j2": {
        "ibridges": {},
        "no_ibridges": {"_galaxy_ibridges_enabled": False},
        "remotes_and_byoc": {
            "_galaxy_remote_destinations": REMOTE_DESTINATIONS,
            "_galaxy_byoc_pulsar": True,
        },
        "only_byoc": {"_galaxy_ibridges_enabled": False, "_galaxy_byoc_pulsar": True},
        "onboarded_options": {
            "_galaxy_byoc_preference_options": [["EOSC EU Node 01", "eosc-eu-node-01-pulsar"]],
        },
    },
    "job_conf.yml.j2": {
        "default": {},
        "rest_pulsar": {"_galaxy_has_pulsar": True},
        "byoc_runner": {
            "_galaxy_byoc_runners": [
                {
                    "id": "pulsar_eu_eosc01",
                    "load": "galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
                    "params": {"amqp_url": "pyamqp://u:p@h:5671//pulsar/u?ssl=1", "manager": "x"},
                }
            ],
        },
    },
}

PARAMS = [
    pytest.param(f"{CONFIG_DIR}/{template}", {**BASE_VARS, **overrides}, id=f"{template}-{case}")
    for template, cases in CASES.items()
    for case, overrides in cases.items()
]


def test_every_config_template_has_a_rendering_case():
    templates = {path.name for path in (TEMPLATES_DIR / CONFIG_DIR).glob("*.j2")}

    assert templates == set(CASES)


@pytest.mark.parametrize(("template", "variables"), PARAMS)
def test_config_template_renders_to_yaml(template, variables):
    assert render_yaml(template, variables) is not None


def _job_conf(**overrides):
    return render_yaml(f"{CONFIG_DIR}/job_conf.yml.j2", {**BASE_VARS, **overrides})


@pytest.mark.parametrize("has_pulsar", [False, True])
def test_job_conf_routes_every_job_through_tpv(has_pulsar):
    conf = _job_conf(_galaxy_has_pulsar=has_pulsar)

    assert conf["execution"]["default"] == "tpv_dispatcher"
    assert conf["execution"]["environments"]["tpv_dispatcher"] == {
        "runner": "dynamic",
        "type": "python",
        "function": "map_tool_to_destination",
        "rules_module": "tpv.rules",
        "tpv_config_files": [
            "/srv/galaxy/config/tpv/tool_defaults.yml",
            "/srv/galaxy/config/tpv/destinations.yml",
        ],
    }
    # Upload, data fetch, ... ("local" class tools) keep running in the local environment.
    assert conf["tools"] == [{"class": "local", "environment": "local"}]
    assert set(conf["execution"]["environments"]) == {"local", "tpv_dispatcher"}


def test_job_conf_rest_runner_only_with_rest_pulsars():
    assert "pulsar" not in _job_conf()["runners"]
    assert _job_conf(_galaxy_has_pulsar=True)["runners"]["pulsar"] == {
        "load": "galaxy.jobs.runners.pulsar:PulsarRESTJobRunner"
    }
