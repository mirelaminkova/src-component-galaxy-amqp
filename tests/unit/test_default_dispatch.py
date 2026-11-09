"""Behaviour of the dynamic job rule rendered from templates/default_dispatch.py.j2."""

from types import SimpleNamespace

import pytest

from tests.helpers.render import render_module

TEMPLATE = "default_dispatch.py.j2"


def _dispatcher(jobs_default, function_name="default_dispatcher"):
    module = render_module(
        TEMPLATE,
        {
            "_galaxy_default_dispatcher": function_name,
            "_galaxy_jobs_default": jobs_default,
        },
        module_name=f"{function_name}_{jobs_default}",
    )
    return getattr(module, function_name)


@pytest.mark.usefixtures("galaxy_jobs_stub")
@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
def test_interactive_tools_are_sent_to_docker(jobs_default):
    dispatch = _dispatcher(jobs_default)

    assert dispatch(SimpleNamespace(tool_type="interactive")) == "docker"


@pytest.mark.usefixtures("galaxy_jobs_stub")
@pytest.mark.parametrize("jobs_default", ["singularity", "docker", "local"])
@pytest.mark.parametrize("tool_type", ["default", "data_source", "expression"])
def test_other_tools_use_the_configured_default(jobs_default, tool_type):
    dispatch = _dispatcher(jobs_default)

    assert dispatch(SimpleNamespace(tool_type=tool_type)) == jobs_default


@pytest.mark.usefixtures("galaxy_jobs_stub")
def test_function_name_comes_from_the_dispatcher_variable():
    dispatch = _dispatcher("singularity", function_name="my_dispatcher")

    assert dispatch.__name__ == "my_dispatcher"
    assert dispatch(SimpleNamespace(tool_type="interactive")) == "docker"
    assert dispatch(SimpleNamespace(tool_type="default")) == "singularity"
