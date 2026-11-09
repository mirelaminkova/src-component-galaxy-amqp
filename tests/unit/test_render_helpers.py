"""Tests for the Jinja2 rendering helpers used by the template and rule tests."""

import jinja2
import pytest
import yaml

from tests.helpers import render


def test_environment_loads_templates_from_the_repo():
    env = render.make_environment()

    assert "default_dispatch.py.j2" in env.list_templates()


def test_missing_variables_fail_loudly():
    with pytest.raises(jinja2.UndefinedError):
        render.render_template("default_dispatch.py.j2", {})


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("{{ true | ternary('yes', 'no') }}", "yes"),
        ("{{ 'yes' | bool }}", "True"),
        ("{{ 'a.b.c' | regex_replace('\\\\.', '-') }}", "a-b-c"),
        ("{{ ('[1, 2]' | from_json) | join(',') }}", "1,2"),
        ("{{ '' | default('fallback', true) }}", "fallback"),
    ],
)
def test_ansible_filters_are_available(expression, expected):
    env = render.make_environment()

    assert env.from_string(expression).render() == expected


def test_jinja_builtin_filters_are_not_replaced():
    env = render.make_environment()

    assert env.filters["default"] is jinja2.filters.FILTERS["default"]


def test_ansible_style_whitespace_handling():
    env = render.make_environment()

    assert env.from_string("{% if true %}\nx\n{% endif %}\n").render() == "x\n"


def test_render_module_writes_and_imports_the_rendered_source(tmp_path, monkeypatch):
    (tmp_path / "rule.py.j2").write_text("VALUE = '{{ value }}'\n")
    monkeypatch.setattr(render, "TEMPLATES_DIR", tmp_path)
    monkeypatch.setattr(render, "RENDER_DIR", tmp_path / "out")

    module = render.render_module("rule.py.j2", {"value": "hello"}, module_name="rule")

    assert module.VALUE == "hello"
    assert (tmp_path / "out" / "rule.py").read_text() == "VALUE = 'hello'\n"


def test_render_yaml_parses_the_rendered_text(tmp_path, monkeypatch):
    (tmp_path / "conf.yml.j2").write_text("key: {{ value }}\nlist: [1, 2]\n")
    monkeypatch.setattr(render, "TEMPLATES_DIR", tmp_path)

    assert render.render_yaml("conf.yml.j2", {"value": "v"}) == {"key": "v", "list": [1, 2]}


def test_render_yaml_rejects_invalid_yaml(tmp_path, monkeypatch):
    (tmp_path / "bad.yml.j2").write_text("key: [unclosed\n")
    monkeypatch.setattr(render, "TEMPLATES_DIR", tmp_path)

    with pytest.raises(yaml.YAMLError):
        render.render_yaml("bad.yml.j2", {})
