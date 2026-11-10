"""Session-wide test setup."""

from pathlib import Path

import pytest
from ansible.plugins.loader import add_all_plugin_dirs, init_plugin_loader

from tests.helpers.render import clean_render_dir

# Initialise Ansible's plugin loader so builtin lookups (varnames, vars, ...) resolve, and make
# the playbook's own plugins (filter_plugins/ next to galaxysrv.yml) available to
# ansible-core's Templar in tests, as ansible-playbook does for the playbook directory.
init_plugin_loader()
add_all_plugin_dirs(str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def _fresh_render_dir():
    clean_render_dir()
