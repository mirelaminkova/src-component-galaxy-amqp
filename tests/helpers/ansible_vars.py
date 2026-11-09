"""Resolve the playbook's variable files with Ansible's own templating engine.

``vars/*.yml`` derive internal ``_galaxy_*`` variables from the SRC component
parameters (which ResearchCloud passes as strings such as ``'true'``).
:func:`resolve_vars` loads those files like ``include_vars`` does, adds the
component parameters, and returns a lookup that renders a variable exactly as
the playbook would see it.
"""

from collections.abc import Callable
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

REPO_ROOT = Path(__file__).resolve().parents[2]
VARS_DIR = REPO_ROOT / "vars"

# Facts and magic variables the vars files reference, as on a normal (non-molecule) run.
DEFAULT_CONTEXT = {"ansible_skip_tags": []}


def _trust(value):
    """Mark strings as templates, like Ansible does for variables read from files."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, list):
        return [_trust(item) for item in value]
    if isinstance(value, dict):
        return {key: _trust(item) for key, item in value.items()}
    return value


def resolve_vars(files: list[str], params: dict | None = None) -> Callable[[str], object]:
    """Load ``vars/<file>`` for each of ``files`` and return a variable resolver.

    ``params`` are the component parameters (or any variable to override); they
    are plain data and are not templated themselves.
    """
    loader = DataLoader()
    variables = dict(DEFAULT_CONTEXT)
    for name in files:
        variables.update(_trust(loader.load_from_file(str(VARS_DIR / name))))
    variables.update(params or {})
    templar = Templar(loader=loader, variables=variables)

    def resolve(name: str) -> object:
        return templar.template(variables[name])

    return resolve
