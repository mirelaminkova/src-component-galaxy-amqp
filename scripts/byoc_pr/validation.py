"""Validation of the ``byoc_pulsar`` user preference form.

The rules are the ones the form promises and the ones the on-host BYOC sync (#6) applies:
RabbitMQ username ``^[a-z0-9_-]{3,32}$``, an alphanumeric password of at least 10 characters
and whole-number resource limits within sane bounds. Error messages name the field and the
rule, never the submitted value, so a password can not leak through a report.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{10,128}$")
LIMITS: dict[str, tuple[int, int]] = {
    "max_accepted_cores": (1, 1024),
    "max_accepted_mem": (1, 16384),
    "min_accepted_gpus": (0, 64),
    "max_accepted_gpus": (0, 64),
}
FIELDS = ("username", "password", *LIMITS)


@dataclass(frozen=True)
class ByocRequest:
    """A validated BYOC endpoint request. ``password`` is kept out of ``repr()``."""

    galaxy_user_id: str
    galaxy_username: str
    username: str
    password: str = field(repr=False)
    max_accepted_cores: int
    max_accepted_mem: int
    min_accepted_gpus: int
    max_accepted_gpus: int


def is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def is_empty_form(values: Mapping[str, object]) -> bool:
    """True when the user never filled in any field of the form."""
    return all(is_blank(values.get(name)) for name in FIELDS)


def parse_int(value: object) -> int | None:
    """Parse a whole number as Galaxy stores it (``4``, ``"4"``, ``4.0``); else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def validate(
    galaxy_user_id: str, galaxy_username: str, values: Mapping[str, object]
) -> tuple[ByocRequest | None, list[str]]:
    """Return ``(request, [])`` for a valid form, else ``(None, errors)``."""
    errors: list[str] = []
    username = values.get("username")
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        errors.append(f"username: must match {USERNAME_RE.pattern}")
    password = values.get("password")
    if not isinstance(password, str) or not PASSWORD_RE.fullmatch(password):
        errors.append("password: must be alphanumeric and 10 to 128 characters long")
    limits: dict[str, int] = {}
    for name, (low, high) in LIMITS.items():
        number = parse_int(values.get(name))
        if number is None or not low <= number <= high:
            errors.append(f"{name}: must be a whole number from {low} to {high}")
        else:
            limits[name] = number
    if limits.get("min_accepted_gpus", 0) > limits.get("max_accepted_gpus", 64):
        errors.append("min_accepted_gpus: must not be larger than max_accepted_gpus")
    if errors:
        return None, errors
    request = ByocRequest(
        galaxy_user_id=galaxy_user_id,
        galaxy_username=galaxy_username,
        username=str(username),
        password=str(password),
        **limits,
    )
    return request, []
