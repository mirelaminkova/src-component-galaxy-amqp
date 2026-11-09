"""Fixtures shared by the unit tests."""

import sys
import types

import pytest


@pytest.fixture
def galaxy_jobs_stub(monkeypatch):
    """Provide a minimal ``galaxy.jobs`` module so rendered job rules can be imported.

    Galaxy itself is not installed in the test environment; dynamic job rules only
    import names from it, so an empty stand-in is enough.
    """
    galaxy = types.ModuleType("galaxy")
    jobs = types.ModuleType("galaxy.jobs")
    jobs.JobDestination = type("JobDestination", (dict,), {})
    galaxy.jobs = jobs
    monkeypatch.setitem(sys.modules, "galaxy", galaxy)
    monkeypatch.setitem(sys.modules, "galaxy.jobs", jobs)
    return jobs
