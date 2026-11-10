"""TPV job routing (files/galaxy/tpv/*.yml.j2, issue #20), checked with TPV itself.

The TPV config and job_conf.yml are rendered from the SRC component parameters, with the
derived variables resolved by Ansible as the playbook would, and then:

* linted with TPV's linter (what ``tpv lint`` runs: schema, regexes and mypy over the
  rule code);
* routed with TPV's own mapper, the code Galaxy calls for every job (what
  ``tpv dry-run`` runs), with tool and user stand-ins carrying the attributes the
  rules read (Galaxy's ``Tool.all_ids``/``tool_type``/... and ``User.extra_preferences``);
* for job_conf.yml, loaded by Galaxy's own ``JobConfiguration`` through TPV's dry runner,
  which reads the TPV files from the ``tpv_dispatcher`` environment.

Facts about Galaxy (release_26.0) the stand-ins rely on: ``User.extra_preferences`` is a
``defaultdict(lambda: None)`` keyed ``"<section>|<input name>"``; the "default" option
of a select written as ``None`` is stored as the string ``"None"``; ``Tool.all_ids``
holds the ids job config tool mappings are matched against; anonymous users are ``None``.
"""

import logging
from collections import defaultdict
from pathlib import Path

import pytest
from galaxy.jobs.mapper import JobMappingException
from tpv.commands.dryrunner import TPVDryRunner
from tpv.commands.linter import TPVConfigLinter
from tpv.commands.test import mock_galaxy
from tpv.rules import gateway

from tests.helpers.ansible_vars import resolve_vars
from tests.helpers.render import render_template

VARS_FILES = [
    "internal_vars.yml",
    "src_galaxy_vars.yml",
    "rabbitmq_vars.yml",
    "byoc_onboarding_vars.yml",
]
PREF_KEY = "distributed_compute|remote_resources"
TPV_FILES = ["tool_defaults", "destinations"]

REST_HPC = "[{name: hpc, url: https://hpc.example.org, private_token: t0ken, docker_enabled: true}]"
AMQP_EU = "[{name: eu, label: EU node, docker_enabled: true}]"
WITH_PULSARS = {"src_galaxy_pulsar_configs": REST_HPC, "src_galaxy_pulsar_amqp_configs": AMQP_EU}

# usegalaxy-eu/infrastructure-playbook PR #1384, verbatim: what an onboarding PR adds.
EU_ONBOARDING_DESTINATION = """
  pulsar_eosc01_tpv:
    inherits: pulsar_default
    runner: pulsar_eu_eosc01
    max_accepted_cores: 4
    max_accepted_mem: 31
    min_accepted_gpus: 0
    max_accepted_gpus: 0
    scheduling:
      require:
        - eosc-eu-node-01-pulsar
"""

MAP_BWA_TO_EU = "[{tool_id: bwa, environment: amqp_eu}]"
GUID = "toolshed.g2.bx.psu.edu/repos/iuc/bwa/bwa/0.7.18"


def _variables(params, config_dir):
    resolve = resolve_vars(
        VARS_FILES,
        {"workspace_fqdn": "galaxy.example.org", "src_galaxy_rabbitmq": "true", **params},
    )
    names = [
        "_galaxy_jobs_default",
        "_galaxy_use_interactive_tools",
        "_galaxy_has_pulsar",
        "_galaxy_pulsar_configs_raw",
        "_galaxy_pulsar_amqp_configs",
        "_galaxy_tpv_tool_mappings",
        "_galaxy_tpv_config_errors",
        "_galaxy_rabbitmq_local_port",
        "_galaxy_infrastructure_url",
        "_galaxy_byoc_runners",
    ]
    return {
        **{name: resolve(name) for name in names},
        "_galaxy_tpv_config_dir": str(config_dir),
        "_galaxy_rabbitmq_passwords": {"galaxy": "GalaxyPw0123456789"},
        "galaxy_mutable_data_dir": "/srv/galaxy/datadir",
        "apptainer_volumes": ["$galaxy_root:ro", "$working_directory:rw"],
    }


def _render(config_dir, params, onboarding=""):
    """Render job_conf.yml and the TPV files into ``config_dir``; return the TPV file paths."""
    config_dir.mkdir(parents=True, exist_ok=True)
    variables = _variables(params, config_dir)
    assert variables["_galaxy_tpv_config_errors"] == []
    (config_dir / "job_conf.yml").write_text(
        render_template("galaxy/config/job_conf.yml.j2", variables)
    )
    paths = []
    for name in TPV_FILES:
        text = render_template(f"files/galaxy/tpv/{name}.yml.j2", variables)
        if name == "destinations":
            text += onboarding
        path = config_dir / f"{name}.yml"
        path.write_text(text)
        paths.append(str(path))
    return paths


class Router:
    """Route jobs with TPV's mapper over a rendered config."""

    def __init__(self, config_dir, params, onboarding=""):
        self.config_dir = config_dir
        self.paths = _render(config_dir, params, onboarding)
        self.mapper = gateway.load_destination_mapper(self.paths)

    def __call__(self, tool, user=None):
        # The rules never use Galaxy's app; job_conf.yml is checked with a real one below.
        return self.mapper.map_to_destination(None, tool, user, mock_galaxy.Job())


@pytest.fixture(scope="module")
def route_factory(tmp_path_factory):
    routers = {}

    def factory(onboarding="", **params):
        key = (onboarding, tuple(sorted(params.items())))
        if key not in routers:
            routers[key] = Router(tmp_path_factory.mktemp("tpv"), params, onboarding)
        return routers[key]

    return factory


def _tool(tool_id="cat1", tool_type="default", local=False, requires_galaxy=False, ids=None):
    tool = mock_galaxy.Tool(tool_id)
    tool.old_id = tool_id.rsplit("/", 2)[-2] if "/" in tool_id else tool_id
    tool.all_ids = ids if ids is not None else [tool_id]
    tool.tool_type = tool_type
    tool.tool_type_local = local
    tool.requires_galaxy_python_environment = requires_galaxy
    return tool


def _user(choice=None, **other_prefs):
    user = mock_galaxy.User("ada", "ada@example.org")
    user.extra_preferences = defaultdict(lambda: None, other_prefs)
    if choice is not None:
        user.extra_preferences[PREF_KEY] = choice
    return user


INTERNAL_TOOLS = [
    pytest.param(_tool("upload1"), id="upload1"),
    pytest.param(_tool("__DATA_FETCH__", local=True), id="data_fetch"),
    pytest.param(_tool("__SET_METADATA__", requires_galaxy=True), id="set_metadata"),
    pytest.param(_tool("dm_fetch", tool_type="manage_data"), id="data_manager"),
    pytest.param(_tool("param_value_from_file", tool_type="expression"), id="expression"),
    pytest.param(_tool("__FILTER_FAILED_DATASETS__", local=True), id="local_tool"),
    pytest.param(_tool("ucsc_table_direct1", tool_type="data_source"), id="data_source"),
    pytest.param(_tool("async_source", tool_type="data_source_async"), id="data_source_async"),
    pytest.param(_tool("my_yaml_tool", tool_type="user_defined"), id="user_defined"),
]


# --- tpv lint and Galaxy's job config --------------------------------------------


@pytest.mark.parametrize(
    ("params", "onboarding"),
    [
        pytest.param({}, "", id="no_pulsar"),
        pytest.param(
            {
                **WITH_PULSARS,
                "src_galaxy_pulsar_tool_mappings": "[{tool_id: bwa, environment: amqp_eu}]",
            },
            EU_ONBOARDING_DESTINATION,
            id="pulsars_mappings_and_onboarding",
        ),
    ],
)
def test_tpv_lint_reports_nothing(tmp_path, caplog, params, onboarding):
    linter = TPVConfigLinter(
        _render(tmp_path, params, onboarding), ignore=[], preserve_temp_code=False
    )

    with caplog.at_level(logging.WARNING):
        linter.lint()

    assert linter.errors == []
    # TPV 3.1.x ships without a py.typed marker, so mypy reports its own imports as untyped
    # for every config with code; anything else is a finding.
    assert [w for w in linter.warnings if not _untyped_tpv_import(w)] == []


def _untyped_tpv_import(warning):
    code, message = warning
    return code == "T103" and (
        ('"tpv.' in message and "[import-untyped]" in message) or "#missing-imports" in message
    )


def test_job_conf_sends_every_job_through_tpv(tmp_path):
    paths = _render(tmp_path, WITH_PULSARS)
    job_config = mock_galaxy.App(str(tmp_path / "job_conf.yml"), create_model=True).job_config

    dispatcher = job_config.get_destination("tpv_dispatcher")
    assert job_config.default_destination_id == "tpv_dispatcher"
    assert dispatcher.runner == "dynamic"
    assert dispatcher.params["rules_module"] == "tpv.rules"
    assert dispatcher.params["function"] == "map_tool_to_destination"
    assert dispatcher.params["tpv_config_files"] == paths


def test_dry_run_through_job_conf(tmp_path):
    """TPV's dry runner finds the TPV files through job_conf.yml, as Galaxy does."""
    _render(tmp_path, WITH_PULSARS)

    runner = TPVDryRunner(str(tmp_path / "job_conf.yml"), tool=_tool(), user=_user("eu-pulsar"))
    destination = runner.run()

    assert destination.id == "amqp_eu"
    assert destination.runner == "pulsar_mq_eu"


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
@pytest.mark.parametrize("tool_type", ["default", "data_source", "expression", "manage_data"])
@pytest.mark.parametrize("user", [None, _user()], ids=["anonymous", "user"])
def test_without_pulsar_every_tool_goes_to_the_default(
    route_factory, jobs_default, tool_type, user
):
    route = route_factory(src_galaxy_jobs_default=jobs_default)

    destination = route(_tool(tool_type=tool_type), user)

    assert destination.id == jobs_default
    assert destination.runner == "local"


def test_default_destination_falls_back_to_singularity(route_factory):
    assert route_factory(src_galaxy_jobs_default="slurm")(_tool()).id == "singularity"


@pytest.mark.parametrize("params", [{}, WITH_PULSARS], ids=["no_pulsar", "pulsars"])
@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
@pytest.mark.parametrize("user", [None, _user(), _user("hpc-pulsar")], ids=["anon", "user", "pick"])
def test_interactive_tools_go_to_docker(route_factory, params, jobs_default, user):
    route = route_factory(src_galaxy_jobs_default=jobs_default, **params)

    destination = route(_tool("interactive_tool_jupyter", tool_type="interactive"), user)

    assert destination.id == "docker"
    assert destination.params["docker_enabled"] is True


def test_local_destinations_keep_their_job_conf_settings(route_factory):
    route = route_factory(src_galaxy_jobs_default="local", src_galaxy_interactive_tools="true")
    jupyter = _tool("interactive_tool_jupyter", tool_type="interactive")

    local = route(_tool())
    docker = route(jupyter)

    assert local.env == [
        {"name": "PATH", "action": "prepend_to", "value": "/srv/galaxy/datadir/shed_tools:$PATH"}
    ]
    assert docker.params["outputs_to_working_directory"] is True
    singularity = route_factory(src_galaxy_jobs_default="singularity")(_tool())
    assert singularity.params["singularity_enabled"] is True
    assert singularity.params["singularity_volumes"] == "$galaxy_root:ro,$working_directory:rw"
    assert singularity.params["outputs_to_working_directory"] is False
    resolvers = singularity.params["container_resolvers"]
    assert resolvers[0]["type"] == "cached_explicit_singularity"


@pytest.mark.parametrize(
    ("choice", "destination", "runner"),
    [("hpc-pulsar", "remote_hpc_basic", "pulsar"), ("eu-pulsar", "amqp_eu", "pulsar_mq_eu")],
)
def test_users_choice_sends_their_jobs_to_that_pulsar(route_factory, choice, destination, runner):
    routed = route_factory(**WITH_PULSARS)(_tool(), _user(choice))

    assert (routed.id, routed.runner) == (destination, runner)


def test_rest_and_amqp_destinations_keep_their_job_conf_settings(route_factory):
    route = route_factory(**WITH_PULSARS)

    rest = route(_tool(), _user("hpc-pulsar"))
    amqp = route(_tool(), _user("eu-pulsar"))

    assert rest.params == {
        "url": "https://hpc.example.org",
        "private_token": "t0ken",
        "default_file_action": "remote_transfer",
        "dependency_resolution": "remote",
        "manager": "_default_",
        "cleanup_job": "never",
    }
    assert amqp.params == {
        "jobs_directory": "/mnt/pulsar/files/staging",
        "default_file_action": "remote_transfer",
        "dependency_resolution": "remote",
        "rewrite_parameters": True,
        "remote_metadata": False,
        "outputs_to_working_directory": False,
        "docker_enabled": True,
        "singularity_enabled": False,
    }


@pytest.mark.parametrize("tool", INTERNAL_TOOLS)
def test_internal_tools_never_go_remote(route_factory, tool):
    assert route_factory(**WITH_PULSARS)(tool, _user("hpc-pulsar")).id == "singularity"


@pytest.mark.parametrize(
    "choice",
    [None, "None", "", [], ["hpc-pulsar"], {"hpc-pulsar": 1}, 0, 1, 1.5, True],
    ids=["unset", "default", "empty", "list", "list_tag", "dict", "zero", "int", "float", "bool"],
)
def test_no_choice_or_a_non_string_choice_uses_the_default(route_factory, choice):
    """The preferences API stores any JSON value; the rule must not crash on it."""
    user = _user()
    user.extra_preferences[PREF_KEY] = choice

    assert (
        route_factory(src_galaxy_jobs_default="local", **WITH_PULSARS)(_tool(), user).id == "local"
    )


def test_anonymous_users_get_the_default(route_factory):
    assert route_factory(**WITH_PULSARS)(_tool()).id == "singularity"


def test_other_preferences_do_not_affect_routing(route_factory):
    user = _user(**{"irods_config|username": "hpc-pulsar"})

    assert route_factory(**WITH_PULSARS)(_tool(), user).id == "singularity"


@pytest.mark.parametrize(
    "choice",
    [
        "gone-pulsar",  # removed from the config
        "remote_hpc_basic",  # a destination id (the dropdown value before TPV), not a tag
        "HPC-PULSAR",
        "hpc-pulsar-docker",  # only for tool mappings
        "docker",  # a local tag
        "internal",
    ],
)
def test_unknown_or_removed_choice_fails_the_job_with_instructions(route_factory, choice):
    with pytest.raises(JobMappingException, match="is not available") as failure:
        route_factory(**WITH_PULSARS)(_tool(), _user(choice))

    assert f"'{choice}'" in str(failure.value)
    assert "Use distributed compute resources" in str(failure.value)


def test_unknown_choice_does_not_fail_jobs_that_never_go_remote(route_factory):
    route = route_factory(**WITH_PULSARS)

    assert route(_tool("upload1"), _user("gone-pulsar")).id == "singularity"
    assert route(_tool("it", tool_type="interactive"), _user("gone-pulsar")).id == "docker"


def test_without_any_pulsar_a_stale_choice_is_ignored(route_factory):
    """No Pulsar destinations: the preference form is not shown, so users cannot reset it."""
    assert route_factory()(_tool(), _user("hpc-pulsar")).id == "singularity"


@pytest.mark.parametrize(
    ("mapped_id", "ids"),
    [
        ("bwa", ["bwa"]),
        ("BWA", ["bwa"]),  # Galaxy compares tool ids case-insensitively
        (GUID, [GUID, GUID.rsplit("/", 1)[0], "bwa"]),
        (GUID.rsplit("/", 1)[0], [GUID, GUID.rsplit("/", 1)[0], "bwa"]),
        ("bwa", [GUID, GUID.rsplit("/", 1)[0], "bwa"]),
    ],
)
def test_tool_mappings_pin_tools_to_their_destination(route_factory, mapped_id, ids):
    mappings = f'[{{tool_id: "{mapped_id}", environment: amqp_eu}}]'
    route = route_factory(src_galaxy_pulsar_tool_mappings=mappings, **WITH_PULSARS)
    bwa = _tool(ids[0], ids=ids)

    assert route(bwa).id == "amqp_eu"
    assert route(bwa, _user("hpc-pulsar")).id == "amqp_eu"
    assert route(_tool(), _user("hpc-pulsar")).id == "remote_hpc_basic"
    assert route(_tool()).id == "singularity"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("remote_hpc_basic", "remote_hpc_basic"),
        ("remote_hpc", "remote_hpc"),
        ("local", "local"),
        ("docker", "docker"),
        ("singularity", "singularity"),
    ],
)
def test_tool_mappings_reach_every_destination(route_factory, environment, expected):
    mappings = f"[{{tool_id: bwa, environment: {environment}}}]"
    route = route_factory(src_galaxy_pulsar_tool_mappings=mappings, **WITH_PULSARS)

    routed = route(_tool("bwa"), _user("gone-pulsar"))

    assert routed.id == expected


def test_rest_docker_destination_settings(route_factory):
    mappings = "[{tool_id: bwa, environment: remote_hpc}]"
    routed = route_factory(src_galaxy_pulsar_tool_mappings=mappings, **WITH_PULSARS)(_tool("bwa"))

    assert routed.params["docker_enabled"] is True
    assert routed.params["container_resolvers"] == [
        {"type": "explicit"},
        {"type": "mulled"},
        {"type": "build_mulled", "require_container": True},
    ]
    assert routed.params["container_monitor_get_ip_method"] == "command:echo hpc"


def test_tool_mappings_without_any_pulsar_are_ignored(route_factory):
    route = route_factory(src_galaxy_pulsar_tool_mappings="[{tool_id: bwa, environment: docker}]")

    assert route(_tool("bwa")).id == "singularity"


# --- BYOC onboarding PRs (usegalaxy.eu layout) ----------------------------------------


@pytest.mark.parametrize("params", [{}, WITH_PULSARS], ids=["no_pulsar", "pulsars"])
def test_onboarding_destination_receives_its_users_jobs(route_factory, params):
    route = route_factory(onboarding=EU_ONBOARDING_DESTINATION, **params)

    routed = route(_tool(), _user("eosc-eu-node-01-pulsar"))

    assert routed.id == "pulsar_eosc01_tpv"
    assert routed.runner == "pulsar_eu_eosc01"
    # pulsar_default (shared settings, usegalaxy.eu style) is inherited
    assert routed.params["jobs_directory"] == "/data/share/staging"
    assert routed.params["default_file_action"] == "remote_transfer"
    assert routed.params["submit_request_cpus"] == "1"
    assert {"name": "TMPDIR", "value": "$_GALAXY_JOB_TMP_DIR"} in routed.env


def test_onboarding_destination_gets_no_other_jobs(route_factory):
    route = route_factory(onboarding=EU_ONBOARDING_DESTINATION)

    assert route(_tool()).id == "singularity"
    assert route(_tool(), _user()).id == "singularity"
    assert route(_tool("upload1"), _user("eosc-eu-node-01-pulsar")).id == "singularity"
    with pytest.raises(JobMappingException, match="'hpc-pulsar' selected"):
        route(_tool(), _user("hpc-pulsar"))


def test_onboarding_section_is_marked_and_last(tmp_path):
    destinations = Path(_render(tmp_path, WITH_PULSARS)[1]).read_text()
    marker = destinations.index("# BYOC onboarding destinations")
    anchor = destinations.index("# LOCAL CONDOR DESTINATIONS #")

    assert "pulsar_default:" in destinations[:marker]
    assert marker < anchor
    assert all(
        not line.strip() or line.lstrip().startswith("#")
        for line in destinations[marker:].splitlines()
    )
