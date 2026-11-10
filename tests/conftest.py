"""Session-wide test setup."""

from pathlib import Path

import pytest
from ansible.plugins.loader import add_all_plugin_dirs

from tests.helpers.render import clean_render_dir

# Make the playbook's own plugins (filter_plugins/ next to galaxysrv.yml) available to
# ansible-core's Templar in tests, as ansible-playbook does for the playbook directory.
add_all_plugin_dirs(str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def _fresh_render_dir():
    clean_render_dir()
