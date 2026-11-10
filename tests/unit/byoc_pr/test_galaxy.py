import pytest
import requests

from scripts.byoc_pr.galaxy import (
    GalaxyClient,
    GalaxyError,
    extract_byoc_section,
    fetch_forms,
    raw_byoc_preferences,
)
from tests.unit.byoc_pr.conftest import GALAXY_URL, FakeResponse, FakeSession


def test_client_sends_api_key_header_and_lists_active_users(session):
    client = GalaxyClient(GALAXY_URL + "/", "admin-key", session=session)
    users = client.list_users()
    assert session.headers["x-api-key"] == "admin-key"
    assert session.calls[0] == ("/api/users", {"deleted": "false"})
    assert [user["username"] for user in users][:2] == ["alice", "bob"]


def test_client_defaults_to_a_requests_session():
    client = GalaxyClient(GALAXY_URL, "admin-key")
    assert isinstance(client._session, requests.Session)
    assert client._session.headers["x-api-key"] == "admin-key"


def test_http_error_is_reported_without_body(session):
    client = GalaxyClient(GALAXY_URL, "admin-key", session=session)
    with pytest.raises(GalaxyError, match=r"GET /api/users/nope failed: HTTP 404"):
        client.user_details("nope")


def test_connection_error_is_reported_without_details():
    class Broken(FakeSession):
        def get(self, url, params=None, timeout=None):
            raise requests.ConnectionError(f"cannot reach {url} with admin-key")

    client = GalaxyClient(GALAXY_URL, "admin-key", session=Broken())
    with pytest.raises(GalaxyError) as excinfo:
        client.list_users()
    assert str(excinfo.value) == "GET /api/users failed: ConnectionError"


def test_extract_byoc_section(session):
    client = GalaxyClient(GALAXY_URL, "k", session=session)
    values = extract_byoc_section(client.information_inputs("f2db41e1fa331b3e"))
    assert values["username"] == "alice-hpc"
    assert values["max_accepted_gpus"] == "1"
    assert extract_byoc_section(client.information_inputs("c8bb2ac10d3c6d1a")) is None
    assert extract_byoc_section({}) is None


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"preferences": {"extra_user_preferences": '{"byoc_pulsar|password": "p"}'}}, "p"),
        ({"preferences": {"extra_user_preferences": {"byoc_pulsar|password": "p"}}}, "p"),
        ({"preferences": {}}, None),
        ({"preferences": None}, None),
        ({}, None),
    ],
)
def test_raw_byoc_preferences(details, expected):
    assert raw_byoc_preferences(details).get("password") == expected


def test_fetch_forms_skips_empty_and_missing_forms(session):
    forms = list(fetch_forms(GalaxyClient(GALAXY_URL, "k", session=session)))
    assert [form.galaxy_username for form in forms] == ["alice", "carol", "dave", "erin"]
    assert "AlicePuls4rPassw0rd" not in repr(forms)


def test_masked_secret_password_is_read_from_admin_user_view(session):
    forms = {
        f.galaxy_username: f for f in fetch_forms(GalaxyClient(GALAXY_URL, "k", session=session))
    }
    assert forms["erin"].values["password"] == "ErinPuls4rPassw0rd"
    detail_calls = [path for path, _ in session.calls if path.count("/") == 3]
    assert detail_calls == ["/api/users/5969b1f7201f12ae"]


def test_password_in_galaxy_vault_is_unreadable(session):
    session.overrides["/api/users/5969b1f7201f12ae"] = FakeResponse(
        200, {"preferences": {"extra_user_preferences": '{"byoc_pulsar|username": "erin_lab"}'}}
    )
    forms = {
        f.galaxy_username: f for f in fetch_forms(GalaxyClient(GALAXY_URL, "k", session=session))
    }
    assert forms["erin"].values["password"] is None


def test_empty_secret_form_is_skipped_without_detail_call(session):
    information = {
        "username": "gina",
        "inputs": [
            {
                "type": "section",
                "name": "byoc_pulsar",
                "inputs": [
                    {"name": "username"},
                    {"name": "password", "value": "__SECRET_PLACEHOLDER__"},
                ],
            }
        ],
    }
    session.overrides["/api/users"] = FakeResponse(200, [{"id": "g1"}])
    session.overrides["/api/users/g1/information/inputs"] = FakeResponse(200, information)
    assert list(fetch_forms(GalaxyClient(GALAXY_URL, "k", session=session))) == []
    assert [path for path, _ in session.calls] == ["/api/users", "/api/users/g1/information/inputs"]


def test_username_falls_back_to_user_index(session):
    information = {
        "inputs": [
            {
                "type": "section",
                "name": "byoc_pulsar",
                "inputs": [{"name": "username", "value": "x"}],
            }
        ]
    }
    session.overrides["/api/users"] = FakeResponse(
        200, [{"id": "g1", "username": "gina"}, {"id": "g2"}]
    )
    session.overrides["/api/users/g1/information/inputs"] = FakeResponse(200, information)
    session.overrides["/api/users/g2/information/inputs"] = FakeResponse(200, information)
    forms = list(fetch_forms(GalaxyClient(GALAXY_URL, "k", session=session)))
    assert [form.galaxy_username for form in forms] == ["gina", ""]
