"""Derived variables in vars/src_galaxy_vars.yml, evaluated by Ansible's own templar.

Since ansible-core 2.19, a template's result keeps its native type only when it
comes from an expression; literal text such as ``[]`` stays a string. These tests
pin the types the Galaxy config templates rely on.
"""

import pytest

from tests.helpers.ansible_vars import resolve_vars


def _evaluate(name, **component_params):
    return resolve_vars(["src_galaxy_vars.yml"], component_params)(name)


@pytest.mark.parametrize("value", [None, "", "[]"])
def test_no_pulsar_configs_is_an_empty_list(value):
    params = {} if value is None else {"src_galaxy_pulsar_configs": value}

    assert _evaluate("_galaxy_pulsar_configs_raw", **params) == []
    assert _evaluate("_galaxy_has_pulsar", **params) is False


def test_pulsar_configs_are_parsed_into_a_list_of_dicts():
    params = {"src_galaxy_pulsar_configs": "[{url: https://p.example.org, private_token: abc}]"}

    assert _evaluate("_galaxy_pulsar_configs_raw", **params) == [
        {"url": "https://p.example.org", "private_token": "abc"}
    ]
    assert _evaluate("_galaxy_has_pulsar", **params) is True


@pytest.mark.parametrize("value", [None, "", "[]"])
def test_no_tool_mappings_is_an_empty_list(value):
    params = {} if value is None else {"src_galaxy_pulsar_tool_mappings": value}

    assert _evaluate("_galaxy_pulsar_tool_mappings", **params) == []


def test_tool_mappings_are_parsed_into_a_list_of_dicts():
    params = {"src_galaxy_pulsar_tool_mappings": "[{tool_id: bwa, environment: remote_x}]"}

    assert _evaluate("_galaxy_pulsar_tool_mappings", **params) == [
        {"tool_id": "bwa", "environment": "remote_x"}
    ]
