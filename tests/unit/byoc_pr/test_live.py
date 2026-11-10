"""Optional read-only check against a deployed Galaxy; skipped unless both env vars are set.

    GALAXY_URL=https://galaxy.example.org GALAXY_API_KEY=... pytest tests/unit/byoc_pr/test_live.py

Needs an admin API key and, for a useful result, a test user who filled in the byoc_pulsar
form. Only GET requests are made; nothing is pushed.
"""

import os

import pytest

from scripts.byoc_pr.galaxy import GalaxyClient, fetch_forms
from scripts.byoc_pr.validation import validate

URL = os.environ.get("GALAXY_URL")
KEY = os.environ.get("GALAXY_API_KEY")

pytestmark = pytest.mark.skipif(not (URL and KEY), reason="GALAXY_URL and GALAXY_API_KEY not set")


def test_live_forms_are_readable_and_valid():  # pragma: no cover - needs a deployed Galaxy
    forms = list(fetch_forms(GalaxyClient(URL, KEY)))
    assert forms, "no user has filled in the byoc_pulsar form"
    for form in forms:
        request, errors = validate(form.galaxy_user_id, form.galaxy_username, form.values)
        assert request is not None, f"{form.galaxy_username}: {'; '.join(errors)}"
