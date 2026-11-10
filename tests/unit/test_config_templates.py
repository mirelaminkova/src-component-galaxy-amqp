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
    "_galaxy_default_dispatcher": "default_dispatcher",
    "_galaxy_jobs_default": "singularity",
    "_galaxy_has_pulsar": False,
    "_galaxy_pulsar_configs_raw": [],
    "_galaxy_pulsar_tool_mappings": [],
    "_galaxy_use_dispatcher": False,
    "_galaxy_remote_destinations": [],
    "_galaxy_byoc_pulsar": False,
}

REMOTE_DESTINATIONS = [{"id": "remote_byoc_basic", "label": "BYOC 'cluster': \"GPU\""}]

PULSAR_MINIMAL = {"url": "https://pulsar.example.org/", "private_token": "t0ken"}
PULSAR_FULL = {
    "name": "byoc",
    "url": "https://byoc.example.org",
    "private_token": "t0ken",
    "docker_enabled": True,
    "pulsar_root": "/mnt/pulsar",
    "container_resolvers": [{"type": "explicit"}, {"type": "mulled", "require_container": True}],
}

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
    },
    "job_conf.yml.j2": {
        "default": {},
        "interactive": {"_galaxy_use_interactive_tools": True, "_galaxy_use_dispatcher": True},
        "pulsar_minimal": {
            "_galaxy_has_pulsar": True,
            "_galaxy_pulsar_configs_raw": [PULSAR_MINIMAL],
        },
        "pulsar_docker_default_resolvers": {
            "_galaxy_has_pulsar": True,
            "_galaxy_pulsar_configs_raw": [{**PULSAR_MINIMAL, "docker_enabled": True}],
        },
        "pulsar_full_with_mappings": {
            "_galaxy_has_pulsar": True,
            "_galaxy_pulsar_configs_raw": [PULSAR_MINIMAL, PULSAR_FULL],
            "_galaxy_pulsar_tool_mappings": [{"tool_id": "bwa", "environment": "remote_byoc"}],
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


def test_job_conf_default_environment_without_interactive_tools():
    conf = _job_conf()

    assert conf["execution"]["default"] == "singularity"
    assert "default_dispatcher" not in conf["execution"]["environments"]
    assert "pulsar" not in conf["runners"]


@pytest.mark.parametrize("interactive", [True, False])
def test_job_conf_routes_through_dispatcher_when_enabled(interactive):
    conf = _job_conf(_galaxy_use_interactive_tools=interactive, _galaxy_use_dispatcher=True)

    assert conf["execution"]["default"] == "default_dispatcher"
    assert conf["execution"]["environments"]["default_dispatcher"] == {
        "runner": "dynamic",
        "type": "python",
        "function": "default_dispatcher",
    }


def test_job_conf_pulsar_environments_and_tool_mappings():
    conf = _job_conf(
        _galaxy_has_pulsar=True,
        _galaxy_pulsar_configs_raw=[PULSAR_MINIMAL, PULSAR_FULL],
        _galaxy_pulsar_tool_mappings=[{"tool_id": "bwa", "environment": "remote_byoc"}],
    )
    environments = conf["execution"]["environments"]

    assert conf["runners"]["pulsar"]["load"] == "galaxy.jobs.runners.pulsar:PulsarRESTJobRunner"
    assert environments["remote_pulsar-example-org-_basic"]["url"] == PULSAR_MINIMAL["url"]
    assert "remote_pulsar-example-org-" not in environments
    assert environments["remote_byoc"]["jobs_directory"] == "/mnt/pulsar/jobs"
    assert environments["remote_byoc"]["container_resolvers"] == [
        {"type": "explicit"},
        {"type": "mulled", "require_container": True},
    ]
    assert {"id": "bwa", "environment": "remote_byoc"} in conf["tools"]
