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
