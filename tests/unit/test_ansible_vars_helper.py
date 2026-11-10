"""Tests for the vars resolver helper itself."""

from tests.helpers.ansible_vars import _trust, resolve_vars


def test_resolver_templates_nested_values_and_params():
    resolve = resolve_vars(
        ["internal_vars.yml", "src_galaxy_vars.yml"], {"workspace_fqdn": "gx.example.org"}
    )

    assert resolve("_galaxy_server_fqdn") == "gx.example.org"
    assert resolve("_galaxy_acme_cert_dir") == "/etc/ssl/acme/gx.example.org"
    assert resolve("_galaxy_job_destinations") == ["docker", "singularity", "local"]


def test_trust_keeps_non_string_scalars():
    assert _trust({"a": [1, True, None]}) == {"a": [1, True, None]}


def test_templated_variables_are_rendered_but_params_are_not():
    resolve = resolve_vars(
        ["internal_vars.yml", "src_galaxy_vars.yml"],
        {"workspace_fqdn": "gx.example.org", "raw": "{{ workspace_fqdn }}"},
        {"from_file": {"host": "{{ _galaxy_server_fqdn }}"}},
    )

    assert resolve("from_file") == {"host": "gx.example.org"}
    assert resolve("raw") == "{{ workspace_fqdn }}"
