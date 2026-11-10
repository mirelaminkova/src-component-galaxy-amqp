"""Read the ``byoc_pulsar`` preference form of every Galaxy user through the API.

Needs an admin API key: admins may list all users and read any user's
``GET /api/users/{id}/information/inputs``. Galaxy returns ``type: password`` fields in clear
there but masks ``type: secret`` fields as ``__SECRET_PLACEHOLDER__``; for a masked password the
raw value is read from ``preferences.extra_user_preferences`` of ``GET /api/users/{id}``
(admin view). A password kept in Galaxy's vault (``store: vault``) is not readable at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

from scripts.byoc_pr.validation import is_blank, is_empty_form

BYOC_SECTION = "byoc_pulsar"
SECRET_PLACEHOLDER = "__SECRET_PLACEHOLDER__"  # noqa: S105 - Galaxy's mask, not a secret


class GalaxyError(RuntimeError):
    """A Galaxy API call failed."""


@dataclass(frozen=True)
class UserForm:
    """The raw ``byoc_pulsar`` values of one Galaxy user (kept out of ``repr()``)."""

    galaxy_user_id: str
    galaxy_username: str
    values: dict[str, Any] = field(repr=False)


class GalaxyClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        session: requests.Session | None = None,
        timeout: float = 30,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._session.headers["x-api-key"] = api_key

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = self._session.get(f"{self.url}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GalaxyError(f"GET {path} failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise GalaxyError(f"GET {path} failed: HTTP {response.status_code}")
        return response.json()

    def list_users(self) -> list[dict[str, Any]]:
        return self._get("/api/users", {"deleted": "false"})

    def information_inputs(self, user_id: str) -> dict[str, Any]:
        return self._get(f"/api/users/{user_id}/information/inputs")

    def user_details(self, user_id: str) -> dict[str, Any]:
        return self._get(f"/api/users/{user_id}")


def extract_byoc_section(information: dict[str, Any]) -> dict[str, Any] | None:
    """The ``byoc_pulsar`` section of ``information/inputs`` as ``{name: value}``."""
    for item in information.get("inputs", []):
        if item.get("type") == "section" and item.get("name") == BYOC_SECTION:
            return {entry["name"]: entry.get("value") for entry in item.get("inputs", [])}
    return None


def raw_byoc_preferences(details: dict[str, Any]) -> dict[str, Any]:
    """``byoc_pulsar|*`` values from the admin view of ``GET /api/users/{id}``."""
    raw = (details.get("preferences") or {}).get("extra_user_preferences") or "{}"
    data = json.loads(raw) if isinstance(raw, str) else raw
    prefix = f"{BYOC_SECTION}|"
    return {key[len(prefix) :]: value for key, value in data.items() if key.startswith(prefix)}


def fetch_forms(client: GalaxyClient) -> Iterator[UserForm]:
    """Yield the filled-in ``byoc_pulsar`` forms of all active users."""
    for user in client.list_users():
        user_id = user["id"]
        information = client.information_inputs(user_id)
        values = extract_byoc_section(information)
        if values is None:
            continue
        others = {key: value for key, value in values.items() if key != "password"}
        password = values.get("password")
        if is_empty_form(others) and (is_blank(password) or password == SECRET_PLACEHOLDER):
            continue
        if password == SECRET_PLACEHOLDER:
            values["password"] = raw_byoc_preferences(client.user_details(user_id)).get("password")
        username = information.get("username") or user.get("username") or ""
        yield UserForm(user_id, username, values)
