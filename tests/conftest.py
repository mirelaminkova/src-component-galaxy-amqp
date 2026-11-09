"""Session-wide test setup."""

import pytest

from tests.helpers.render import clean_render_dir


@pytest.fixture(scope="session", autouse=True)
def _fresh_render_dir():
    clean_render_dir()
