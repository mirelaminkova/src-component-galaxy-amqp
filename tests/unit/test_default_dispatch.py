"""Behaviour of the dynamic job rule rendered from templates/default_dispatch.py.j2.

Facts about Galaxy (release_26.0) the fakes below rely on:

* Rule arguments are injected by name (``lib/galaxy/jobs/mapper.py``); ``user`` is
  ``job.user``, i.e. ``None`` for anonymous users.
* ``User.extra_preferences`` (``lib/galaxy/model/__init__.py``) is a
  ``defaultdict(lambda: None)`` of the user's extra preferences, keyed
  ``"<section>|<input name>"`` (``lib/galaxy/webapps/galaxy/api/users.py``).
* A select option written as ``None`` in the preferences YAML is the *string*
  ``"None"``.
* ``Tool.all_ids`` holds the lower-cased ids the job config matches tool mappings
  against; ``Tool.old_id`` is the short id (e.g. ``upload1``).
"""

import inspect
from collections import defaultdict
from types import SimpleNamespace

import pytest

from tests.helpers.render import render_module

TEMPLATE = "default_dispatch.py.j2"
PREF_KEY = "distributed_compute|remote_resources"

REMOTES = [
    {"id": "remote_hpc_basic", "label": "HPC cluster"},
    {"id": "remote_pulsar-example-org-_basic", "label": "pulsar.example.org"},
]
PINNED_GUID = "toolshed.g2.bx.psu.edu/repos/iuc/bwa/bwa/0.7.18"


def _dispatcher(
    jobs_default="singularity",
    remotes=REMOTES,
    tool_mappings=(),
    function_name="default_dispatcher",
    byoc=False,
):
    variables = {
        "_galaxy_default_dispatcher": function_name,
        "_galaxy_jobs_default": jobs_default,
        "_galaxy_remote_destinations": list(remotes),
        "_galaxy_pulsar_tool_mappings": list(tool_mappings),
        "_galaxy_byoc_pulsar": byoc,
    }
    # Every variant renders to the same file: the variables only change values on
    # fixed lines, so coverage merges all variants into one 100%-branch report.
    module = render_module(TEMPLATE, variables, module_name="default_dispatcher")
    rule = getattr(module, function_name)

    def dispatch(tool, user, app=None):
        return rule(app=app or _app(), tool=tool, user=user)

    dispatch.__name__ = rule.__name__
    dispatch.rule = rule
    return dispatch


class _Destination:
    def __init__(self, params):
        self.params = params


def _app(environments=None):
    """Stand-in for ``app.job_config``: get_destination raises KeyError for unknown ids."""
    environments = environments or {}

    def get_destination(id_or_tag):
        return _Destination(dict(environments[id_or_tag]))

    return SimpleNamespace(job_config=SimpleNamespace(get_destination=get_destination))


def _tool(
    tool_id="cat1",
    tool_type="default",
    tool_type_local=False,
    requires_galaxy=False,
    all_ids=None,
):
    return SimpleNamespace(
        id=tool_id,
        old_id=tool_id.rsplit("/", 2)[-2] if "/" in tool_id else tool_id,
        all_ids=all_ids if all_ids is not None else [tool_id.lower()],
        tool_type=tool_type,
        tool_type_local=tool_type_local,
        requires_galaxy_python_environment=requires_galaxy,
    )


def _user(choice=None, user_id=1, **other_prefs):
    prefs = defaultdict(lambda: None, other_prefs)
    if choice is not None:
        prefs[PREF_KEY] = choice
    return SimpleNamespace(id=user_id, extra_preferences=prefs)


def test_rule_asks_galaxy_for_app_tool_and_user():
    dispatch = _dispatcher()

    assert inspect.getfullargspec(dispatch.rule).args == ["app", "tool", "user"]


def test_function_name_comes_from_the_dispatcher_variable():
    dispatch = _dispatcher(function_name="my_dispatcher")

    assert dispatch.__name__ == "my_dispatcher"
    assert dispatch(_tool(), None) == "singularity"


@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
def test_interactive_tools_are_sent_to_docker(jobs_default):
    dispatch = _dispatcher(jobs_default)

    assert dispatch(_tool(tool_type="interactive"), None) == "docker"
    assert dispatch(_tool(tool_type="interactive"), _user("remote_hpc_basic")) == "docker"


@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
@pytest.mark.parametrize("tool_type", ["default", "data_source", "expression"])
def test_without_a_preference_tools_use_the_configured_default(jobs_default, tool_type):
    dispatch = _dispatcher(jobs_default)

    assert dispatch(_tool(tool_type=tool_type), _user()) == jobs_default


def test_anonymous_users_get_the_default():
    assert _dispatcher()(_tool(), None) == "singularity"


@pytest.mark.parametrize("remote", [r["id"] for r in REMOTES])
def test_users_choice_of_a_configured_remote_is_honoured(remote):
    assert _dispatcher()(_tool(), _user(remote)) == remote


def test_other_preferences_do_not_affect_routing():
    user = _user(**{"irods_config|username": "remote_hpc_basic"})

    assert _dispatcher()(_tool(), user) == "singularity"


@pytest.mark.parametrize(
    "choice",
    [
        "None",  # the "default - Galaxy will decide" option
        "",
        "remote_gone_basic",  # stale: destination removed from the config
        "docker",  # an existing but non-remote environment
        "REMOTE_HPC_BASIC",
    ],
)
def test_unknown_or_default_choices_fall_back_to_the_default(choice):
    assert _dispatcher("local")(_tool(), _user(choice)) == "local"


def test_without_remote_destinations_preferences_are_ignored():
    dispatch = _dispatcher(remotes=[])

    assert dispatch(_tool(), _user("remote_hpc_basic")) == "singularity"


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param(_tool("upload1"), id="upload1"),
        pytest.param(_tool("__DATA_FETCH__"), id="data_fetch"),
        pytest.param(_tool("dm_fetch", tool_type="manage_data"), id="data_manager"),
        pytest.param(
            _tool("param_value_from_file", tool_type="expression", tool_type_local=True),
            id="expression_tool",
        ),
        pytest.param(_tool("__FILTER_FAILED_DATASETS__", tool_type_local=True), id="local_tool"),
        pytest.param(_tool("__SET_METADATA__", requires_galaxy=True), id="requires_galaxy"),
        pytest.param(_tool("ucsc_table_direct1", tool_type="data_source"), id="data_source"),
        pytest.param(_tool("async_source", tool_type="data_source_async"), id="data_source_async"),
        pytest.param(_tool("my_yaml_tool", tool_type="user_defined"), id="user_defined"),
    ],
)
def test_tools_that_must_stay_local_are_never_sent_remote(tool):
    assert _dispatcher()(tool, _user("remote_hpc_basic")) == "singularity"


@pytest.mark.parametrize(
    "mapped_id",
    [
        PINNED_GUID,
        "toolshed.g2.bx.psu.edu/repos/iuc/bwa/bwa",
        "bwa",
        "BWA",  # Galaxy compares tool ids case-insensitively
    ],
)
def test_tools_pinned_by_a_tool_mapping_are_not_rerouted(mapped_id):
    dispatch = _dispatcher(
        tool_mappings=[{"tool_id": mapped_id, "environment": "remote_pulsar-example-org-_basic"}]
    )
    pinned = _tool(
        PINNED_GUID,
        all_ids=[PINNED_GUID, PINNED_GUID.rsplit("/", 1)[0], "bwa"],
    )

    assert dispatch(pinned, _user("remote_hpc_basic")) == "singularity"
    assert dispatch(_tool(), _user("remote_hpc_basic")) == "remote_hpc_basic"


def test_labels_with_quotes_render_to_valid_python():
    dispatch = _dispatcher(remotes=[{"id": "remote_x_basic", "label": 'It\'s "quoted"'}])

    assert dispatch(_tool(), _user("remote_x_basic")) == "remote_x_basic"


@pytest.mark.parametrize(
    "choice",
    [[], ["remote_hpc_basic"], {}, {"remote_hpc_basic": 1}, None, 0, 1, 1.5, True],
    ids=["empty_list", "list", "empty_dict", "dict", "none", "zero", "int", "float", "bool"],
)
def test_non_string_choices_fall_back_to_the_default(choice):
    """The preferences API stores any JSON value; the rule must not crash on it."""
    user = SimpleNamespace(
        id=1,
        extra_preferences=defaultdict(lambda: None, {PREF_KEY: choice}),
    )

    assert _dispatcher("local")(_tool(), user) == "local"
    assert _dispatcher("local", byoc=True)(_tool(), user) == "local"


# --- bring your own Pulsar (issue #6) ----------------------------------------------------
# galaxy-byoc-sync writes environment byoc_<username> with param byoc_owner_id = the Galaxy
# user id that owns it. The owner picks "byoc" under distributed_compute|remote_resources.

BYOC_USERNAME_KEY = "byoc_pulsar|username"
ALICE_ENV = {"runner": "pulsar_mq_byoc_alice", "byoc_owner_id": 3}


def _byoc_user(user_id=3, username="alice", choice="byoc"):
    return _user(choice, user_id=user_id, **{BYOC_USERNAME_KEY: username})


@pytest.mark.parametrize("owner", [3, "3"], ids=["int", "string"])
def test_owner_choosing_their_pulsar_gets_it(owner):
    app = _app({"byoc_alice": {**ALICE_ENV, "byoc_owner_id": owner}})

    assert _dispatcher(byoc=True)(_tool(), _byoc_user(), app) == "byoc_alice"


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(_byoc_user(user_id=4), id="same-username-other-user"),
        pytest.param(_user("byoc", user_id=4), id="no-username"),
        pytest.param(_byoc_user(user_id=4, username="bob"), id="own-endpoint-not-provisioned"),
        pytest.param(_user("byoc_alice", user_id=4), id="environment-id-as-choice"),
        pytest.param(_byoc_user(user_id=4, choice="byoc_alice"), id="env-id-with-username"),
        pytest.param(None, id="anonymous"),
    ],
)
def test_nobody_else_can_reach_an_endpoint(user):
    app = _app({"byoc_alice": ALICE_ENV})

    assert _dispatcher(byoc=True)(_tool(), user, app) == "singularity"


@pytest.mark.parametrize(
    "environments",
    [
        {},  # not provisioned yet (sync runs every 15 minutes) or rejected by the sync
        {"byoc_alice": {"runner": "pulsar_mq_byoc_alice"}},  # no owner recorded
        {"byoc_alice": {**ALICE_ENV, "byoc_owner_id": 4}},
        {"byoc_alice": {**ALICE_ENV, "byoc_owner_id": None}},
    ],
    ids=["missing", "no-owner", "other-owner", "null-owner"],
)
def test_without_a_matching_owned_environment_the_owner_gets_the_default(environments):
    dispatch = _dispatcher(byoc=True)

    assert dispatch(_tool(), _byoc_user(), _app(environments)) == "singularity"


@pytest.mark.parametrize(
    "username",
    ["Alice", "al", "a" * 33, "../x", "alice\n", 3, None, ["alice"]],
)
def test_malformed_usernames_never_name_an_environment(username):
    app = _app({"byoc_alice": ALICE_ENV})

    assert _dispatcher(byoc=True)(_tool(), _byoc_user(username=username), app) == "singularity"


def test_byoc_choice_is_ignored_when_the_feature_is_off():
    app = _app({"byoc_alice": ALICE_ENV})

    assert _dispatcher(byoc=False)(_tool(), _byoc_user(), app) == "singularity"


def test_tools_that_must_stay_local_never_go_to_a_byoc_pulsar():
    app = _app({"byoc_alice": ALICE_ENV})
    dispatch = _dispatcher(byoc=True)

    assert dispatch(_tool("upload1"), _byoc_user(), app) == "singularity"
    assert dispatch(_tool(tool_type="interactive"), _byoc_user(), app) == "docker"


def test_static_remote_choices_still_work_with_byoc_enabled():
    assert _dispatcher(byoc=True)(_tool(), _user("remote_hpc_basic")) == "remote_hpc_basic"
