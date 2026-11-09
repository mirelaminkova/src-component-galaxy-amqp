"""Render the repository's Jinja2 templates outside of Ansible.

Templates are rendered with plain Jinja2 configured like Ansible's templating
(``trim_blocks``, kept trailing newline) and with Ansible's own filter plugins
(``ternary``, ``bool``, ``regex_replace``, ``from_json``, ...) registered, so the
output matches what the playbook would deploy closely enough to test it.
Undefined variables raise instead of rendering as empty strings.

Python templates (e.g. Galaxy dynamic job rules) can be rendered into
``tests/rendered/`` and imported with :func:`render_module`; that directory is a
coverage source, so the rules' branches count towards the coverage threshold.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import jinja2
import yaml
from ansible.plugins.filter.core import FilterModule

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"
RENDER_DIR = REPO_ROOT / "tests" / "rendered"


def make_environment() -> jinja2.Environment:
    """Return a Jinja2 environment that mimics Ansible's template module."""
    env = jinja2.Environment(  # noqa: S701 - renders config/code, not HTML
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    # Only add filters Jinja2 lacks; Ansible's overrides of builtins such as
    # ``default`` rely on Ansible's templar and behave the same for our use.
    for name, func in FilterModule().filters().items():
        env.filters.setdefault(name, func)
    return env


def render_template(name: str, variables: dict) -> str:
    """Render ``templates/<name>`` with ``variables`` and return the text."""
    return make_environment().get_template(name).render(**variables)


def render_yaml(name: str, variables: dict) -> object:
    """Render ``templates/<name>`` and parse the result as YAML (raises on invalid YAML)."""
    return yaml.safe_load(render_template(name, variables))


def clean_render_dir() -> None:
    """Remove modules rendered by earlier runs so coverage only reports this run's."""
    for stale in RENDER_DIR.glob("*.py"):
        stale.unlink()


def render_module(name: str, variables: dict, module_name: str) -> ModuleType:
    """Render a Python template to ``tests/rendered/<module_name>.py`` and import it.

    The module is not registered in :data:`sys.modules`, so each call returns a
    fresh module built from the current rendering.
    """
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    path = RENDER_DIR / f"{module_name}.py"
    path.write_text(render_template(name, variables))

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    # Never cache bytecode: the same file name may be re-rendered with different
    # content within the filesystem's mtime resolution.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module
